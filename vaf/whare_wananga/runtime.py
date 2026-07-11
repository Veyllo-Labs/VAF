# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Whare Wananga -- LAZY-corrective: learn from a runtime surprise (observation-distill).

When a tool call fails at runtime with an error that is NOT already in the tool's learned pitfalls
(the B-track's ``[WW-SURPRISE]`` signal), this turns that real observation -- the call arguments and
the exact error -- into a new learned PITFALL via one LLM distil. No tool re-execution. The updated
record flows straight back into proactive (A) and reactive (B) delivery on the next turn (the
delivery cache invalidates on save).

Everything here is fire-and-forget and hard fail-safe: it runs on a background daemon thread, never
blocks or raises into the caller (the critical tool loop), only ever APPENDS pitfalls (never removes
or rewrites), and leaves the record untouched on any error.
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, List, Optional

from vaf.whare_wananga import store

# Environmental / transient errors are NOT a usage lesson -> never learn a pitfall from them (they
# would only add noise like "DNS resolution failed").
_ENVIRONMENTAL = (
    # phrase-based on purpose: bare HTTP codes ("500", "429") over-match unrelated text like
    # "exceeds the 500-character limit", so we match the descriptive form instead.
    "timeout", "timed out", "connection", "refused", "unreachable", "could not resolve",
    "name or service not known", "dns", "max retries", "rate limit", "too many requests",
    "service unavailable", "temporarily unavailable", "internal server error", "bad gateway",
    "gateway timeout", "ssl", "network is", "connection reset", "broken pipe",
)

_COOLDOWN = 3600.0          # per tool: at most one runtime re-learn per hour
_MAX_PITFALLS = 10
_MAX_TOKENS = 1200

_lock = threading.Lock()           # serialize: at most one background distil call at a time
_inflight: set = set()             # tools currently being re-learned
_inflight_lock = threading.Lock()


def _is_learnable_error(error: str) -> bool:
    """True if the error looks like a USAGE/contract problem worth learning, not environmental."""
    e = (error or "").lower()
    if not e:
        return False
    return not any(m in e for m in _ENVIRONMENTAL)


def _norm(s) -> str:
    return " ".join(str(s or "").split())


def _is_dup(new: str, existing: List[str]) -> bool:
    """True if `new` substantially duplicates an existing pitfall (cheap normalized overlap)."""
    n = _norm(new).lower()
    if not n:
        return True
    ntok = set(re.findall(r"[a-z0-9']{3,}", n))
    for ex in existing:
        e = _norm(ex).lower()
        if not e:
            continue
        if n in e or e in n:
            return True
        if ntok:
            etok = set(re.findall(r"[a-z0-9']{3,}", e))
            if etok and len(ntok & etok) / len(ntok) >= 0.6:
                return True
    return False


def _extract_json(text: str):
    try:
        t = re.sub(r"<think>[\s\S]*?</think>", "", text or "", flags=re.IGNORECASE)
        try:
            return json.loads(t.strip())
        except Exception:
            pass
        s = t.find("{")
        if s < 0:
            return None
        depth = 0
        for e in range(s, len(t)):
            if t[e] == "{":
                depth += 1
            elif t[e] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(t[s:e + 1])
                    except Exception:
                        return None
        return None
    except Exception:
        return None


def async_failure_hint(agent, tool: str, error: str):
    """B-track know-how for an ASYNC sub-agent failure (IPC drain lane), or None.

    Sub-agent failures never trigger the sync reactive lane: the tool result was
    only the "[!] TASK DELEGATED" marker, and the real error arrives later via
    the runner/CLI drain (blue378604 audit). Both drains call this instead:
    returns the failed tool's know-how (relaxed gate, tagged UNVERIFIED when the
    record is gate-failing) and triggers the background re-learn for novel
    errors EVEN when no deliverable know-how exists (a confirmed record whose
    baskets yield no blocks must still learn from the surprise). Shared by
    Agent._process_subagent_result and the CLI TUI drain (vaf/cli/cmd/run.py) so
    the two failure messages cannot drift apart. Fail-safe: never raises, never
    blocks (the distil runs in a background thread)."""
    try:
        from vaf.whare_wananga.delivery import known_pitfall_hit, tool_knowhow
        err = str(error or "")
        known = known_pitfall_hit(tool, err, allow_unverified=True)
        kh = tool_knowhow(tool, procedure_first=known, allow_unverified=True)
        if not known:
            try:
                maybe_relearn(agent, tool, None, err)
            except Exception:
                pass
        return kh
    except Exception:
        return None


def maybe_relearn(agent, tool: str, args: Any, error: str) -> None:
    """Maybe spawn a background re-learn from a runtime surprise. Cheap, non-blocking, fail-safe."""
    try:
        if not _is_learnable_error(error):
            return
        rec = store.load(tool)
        if not rec or rec.get("status") != "confirmed":           # only refine reliable knowledge
            return
        if time.time() - float(rec.get("runtime_refreshed_at") or 0) < _COOLDOWN:
            return
        with _inflight_lock:
            if tool in _inflight:
                return
            _inflight.add(tool)
        threading.Thread(target=_relearn, args=(agent, tool, args, error),
                         name=f"ww-relearn-{tool}", daemon=True).start()
    except Exception:
        try:
            with _inflight_lock:
                _inflight.discard(tool)
        except Exception:
            pass


def _relearn(agent, tool: str, args: Any, error: str) -> None:
    try:
        with _lock:                                # at most one background distil at a time
            rec = store.load(tool)
            if not rec or rec.get("status") != "confirmed":
                return
            existing = [e for e in (
                _norm(p.get("text") if isinstance(p, dict) else p)
                for p in (rec.get("tuatea") or {}).get("pitfalls") or []
            ) if e]
            tool_obj = getattr(agent, "tools", {}).get(tool)
            if tool_obj is None or not hasattr(tool_obj, "query_llm"):
                return
            try:
                args_s = json.dumps(args)[:400]
            except Exception:
                args_s = str(args)[:400]
            prompt = [
                {"role": "system", "content": (
                    "A tool call FAILED at runtime. From the call arguments and the exact error, write "
                    "1-2 CONCISE pitfall sentences capturing the lesson (the contract rule to follow / "
                    "what to avoid) so the agent does not repeat it. Quote the key part of the error. Do "
                    "NOT repeat an existing pitfall. Respond ONLY with JSON: {\"pitfalls\": [str]}.")},
                {"role": "user", "content": (
                    f"Tool: {tool}\nArguments: {args_s}\nError: {_norm(error)[:400]}\n"
                    f"Existing pitfalls: {json.dumps(existing)[:1200]}")},
            ]
            raw = tool_obj.query_llm(prompt, max_tokens=_MAX_TOKENS, temperature=0.2) or ""
            data = _extract_json(raw) or {}
            added: List[str] = []
            for p in (data.get("pitfalls") or []):
                p = _norm(p)
                if p and not _is_dup(p, existing + added):
                    added.append(p)
            if not added:
                rec["runtime_refreshed_at"] = time.time()   # bump cooldown so we don't retry at once
                store.save(rec)
                return
            pits = (rec.get("tuatea") or {}).get("pitfalls") or []
            for p in added:
                pits.append({"text": p, "source": "runtime"})
            rec.setdefault("tuatea", {})["pitfalls"] = pits[:_MAX_PITFALLS]
            rec["runtime_refreshed_at"] = time.time()
            store.save(rec)
            try:
                from vaf.core.log_helper import append_domain_log
                append_domain_log("backend", f"[WW-RELEARN] {tool}: +{len(added)} pitfall(s) from runtime surprise")
            except Exception:
                pass
    except Exception:
        pass
    finally:
        try:
            with _inflight_lock:
                _inflight.discard(tool)
        except Exception:
            pass
