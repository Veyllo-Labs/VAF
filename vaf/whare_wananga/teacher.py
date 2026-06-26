# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Whare Wananga -- Teacher / Noho: offline co-learning booster.

When a LOCAL (student) training run leaves a tool below the delivery bar (challenge not passed, or
low hit rate), a STRONGER configured API model co-learns the tool with the student over the existing
predict-then-verify loop (`runner.train_tool`): the teacher DEMONSTRATES an initial draft, the
student PREDICTS, the teacher JUDGES / DISTILS / INVENTS -- several rounds -- and the result is gated
and delivered exactly like normal know-how (`source="teacher"`).

Opt-in (`whare_wananga_teacher_enabled`, default off). Only when the student is LOCAL and an API is
configured (a stronger teacher exists). Automatic, serialized (one session at a time), rate-limited
(24h per tool). NEVER runs send/irreversible tools: it reuses the safe runner path (irreversible ->
skipped, reversible -> error path). Fail-safe and non-blocking.
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from vaf.whare_wananga import store

_SETTING = "whare_wananga_teacher_enabled"
_OVERRIDE_SETTING = "whare_wananga_teacher_model"   # optional explicit "provider:model" override
_COOLDOWN = 24 * 3600.0
_MAX_ROUNDS = 3
_MAX_TOKENS = 8000
_PROVIDERS = ("veyllo", "anthropic", "openai", "google", "openrouter", "deepseek")

# Coarse capability tiers over model FAMILIES (higher = stronger). Pragmatic substring match on the
# configured model id; small/fast variants are demoted regardless of family. Needs upkeep as models
# change -- an explicit `whare_wananga_teacher_model` override is the escape hatch.
_STRONG = ("opus", "gpt-5", "o1", "o3", "o4", "gemini-3-pro", "gemini-3.1-pro", "gemini-2.5-pro",
           "ultra", "grok-4", "deepseek-r1", "405b", "-large")
_MID = ("sonnet", "gpt-4.1", "gpt-4o", "gpt-4-turbo", "grok-3",
        "deepseek-v3", "deepseek-chat", "70b", "72b")
# word-ish boundary so "mini" does NOT match "geMINI", etc. (a marker must start at a non-letter).
_SMALL_RE = re.compile(r"(?<![a-z])(flash|mini|nano|lite|haiku|small|8b|7b)")

# Live model discovery: each provider's available models are fetched from its API and cached ~12h, so
# the teacher can pick the strongest AVAILABLE model (not just the configured one). The tier table
# above only RANKS them; offline we fall back to the configured model.
_MODEL_TTL = 12 * 3600.0
_model_cache: Dict[str, Tuple[float, List[str]]] = {}   # provider -> (fetched_at, [model ids])
_model_cache_lock = threading.Lock()

_lock = threading.Lock()              # guards the queue; one worker => sessions are serialized
_queue: List[str] = []
_done: List[str] = []
_current: Optional[str] = None
_worker: Optional[threading.Thread] = None


def is_enabled() -> bool:
    try:
        from vaf.core.config import Config
        return bool(Config.get(_SETTING, False))
    except Exception:
        return False


def set_enabled(on: bool) -> None:
    from vaf.core.config import Config
    Config.set(_SETTING, bool(on))


def _model_tier(model: str) -> int:
    m = (model or "").lower()
    if _SMALL_RE.search(m):
        return 2
    if any(x in m for x in _STRONG):
        return 5
    if any(x in m for x in _MID):
        return 4
    if "gpt-4" in m:
        return 3
    return 1


def _live_models(provider: str) -> List[str]:
    """Available model ids for a provider, live-fetched and cached ~12h. [] on failure (offline)."""
    now = time.time()
    with _model_cache_lock:
        ent = _model_cache.get(provider)
        if ent and now - ent[0] < _MODEL_TTL:
            return ent[1]
    try:
        from vaf.core.api_backend import APIBackendManager
        live = APIBackendManager.list_models(provider) or []
    except Exception:
        live = []
    if live:                       # cache only a SUCCESSFUL fetch (so an offline blip retries soon)
        with _model_cache_lock:
            _model_cache[provider] = (now, live)
    return live


def _candidate_models(provider: str) -> List[str]:
    """The configured model PLUS the live-discovered ones (deduped) -- so offline we still have the
    configured model as a candidate, and online we can pick a stronger available one."""
    out = []
    try:
        from vaf.core.config import Config
        cfg = Config.get(f"api_model_{provider}", "") or ""
        if cfg:
            out.append(cfg)
    except Exception:
        pass
    out += _live_models(provider)
    return list(dict.fromkeys(out))


def select_teacher() -> Optional[Tuple[str, str]]:
    """(provider, model) of the strongest AVAILABLE API model across configured providers, or None.
    Discovers each provider's live model list (cached ~12h) and ranks by the capability tier; falls
    back to the configured model offline. Honors an explicit 'provider:model' override. Fail-safe."""
    try:
        from vaf.core.config import Config
        ov = (Config.get(_OVERRIDE_SETTING, "") or "").strip()
        if ov and ":" in ov:
            p, m = ov.split(":", 1)
            return (p.strip(), m.strip())
        best = None  # (tier, provider, model)
        for p in _PROVIDERS:
            try:
                if not Config.get_api_key(p):
                    continue
            except Exception:
                continue
            for model in _candidate_models(p):
                tier = _model_tier(model)
                if best is None or tier > best[0]:
                    best = (tier, p, model)
        return (best[1], best[2]) if best else None
    except Exception:
        return None


def teacher_available() -> bool:
    """A teacher exists iff the student is LOCAL and a stronger API is configured."""
    try:
        from vaf.core.config import Config
        if (Config.get("provider", "local") or "local") != "local":
            return False
        return select_teacher() is not None
    except Exception:
        return False


def _is_weak(summary: Dict[str, Any]) -> bool:
    """A real run that did not reach the delivery bar: challenge not passed OR confidence < 0.5."""
    if not isinstance(summary, dict) or summary.get("skipped") or not summary.get("ok"):
        return False
    if summary.get("challenge_passed") is not True:
        return True
    try:
        return float(summary.get("confidence") or 0.0) < 0.5
    except Exception:
        return False


def maybe_teach(agent, tool: str, summary: Dict[str, Any]) -> None:
    """After a STUDENT train run: if the result is weak and a teacher is available + opt-in, enqueue
    a Noho co-learning session. Cheap, non-blocking, fail-safe."""
    try:
        if not is_enabled() or not _is_weak(summary) or not teacher_available():
            return
        rec = store.load(tool)
        if rec is not None and time.time() - float(rec.get("teacher_refreshed_at") or 0) < _COOLDOWN:
            return
        with _lock:
            if tool == _current or tool in _queue:
                return
            _queue.append(tool)
        _ensure_worker(agent)
    except Exception:
        pass


def teach_now(agent, tool: str, summary: Dict[str, Any]) -> bool:
    """Synchronous Noho session for the foreground (CLI): same gates as maybe_teach, then run the
    session and BLOCK until it finishes (so a short-lived CLI process doesn't exit mid-session).
    Returns True if a session ran. Fail-safe."""
    try:
        if not is_enabled() or not _is_weak(summary) or not teacher_available():
            return False
        rec = store.load(tool)
        if rec is not None and time.time() - float(rec.get("teacher_refreshed_at") or 0) < _COOLDOWN:
            return False
        _teach_session(agent, tool)
        return True
    except Exception:
        return False


def status() -> Dict[str, Any]:
    with _lock:
        return {"enabled": is_enabled(), "teacher": select_teacher(), "current": _current,
                "queued": list(_queue), "done": list(_done)}


def _ensure_worker(agent) -> None:
    global _worker
    with _lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_run_worker, args=(agent,),
                                   name="ww-teacher-worker", daemon=True)
        _worker.start()


def _run_worker(agent) -> None:
    global _current
    while True:
        with _lock:
            if not _queue:
                _current = None
                return
            tool = _queue.pop(0)
            _current = tool
        try:
            _teach_session(agent, tool)
            with _lock:
                _done.append(tool)
        except Exception:
            pass
        finally:
            with _lock:
                _current = None


def _demonstrate(tool: str, tool_obj, teacher_llm) -> Optional[dict]:
    """Teacher writes an initial 3-basket draft from the tool schema + the student's failed attempts.
    Returns a seed_record dict (or None). Fail-safe."""
    try:
        from vaf.whare_wananga.runner import _extract_json
        rec = store.load(tool) or {}
        params = (getattr(tool_obj, "parameters", None) or getattr(tool_obj, "input_schema", None) or {})
        desc = getattr(tool_obj, "description", "") or ""
        fails = [r for r in (rec.get("predict_records") or []) if not r.get("match")][:8]
        prompt = [
            {"role": "system", "content": (
                "You are an expert TEACHER preparing a weaker student to use a tool correctly. From the "
                "tool's schema and the student's FAILED attempts, write a corrected, concise draft of "
                "how to operate it. Capture the ARGUMENT CONTRACT in tuatea.pitfalls (which args are "
                "required, the exact rejection error when one is missing/empty/wrong-type). Respond ONLY "
                "with JSON: {\"aronui\": {\"when_to_use\": str, \"output_shape\": str}, "
                "\"tuatea\": {\"pitfalls\": [str]}, "
                "\"tuarua\": {\"procedure\": [str], \"verification\": [str]}}.")},
            {"role": "user", "content": (
                f"Tool: {tool}\nDescription: {desc}\nParameters: {json.dumps(params)[:1200]}\n"
                f"Student's FAILED attempts: {json.dumps(fails)[:2000]}\n"
                f"Student's current (weak) pitfalls: "
                f"{json.dumps([p.get('text') for p in (rec.get('tuatea', {}) or {}).get('pitfalls', [])])[:600]}")},
        ]
        raw = teacher_llm(prompt, max_tokens=_MAX_TOKENS, temperature=0.2) or ""
        return _extract_json(raw)
    except Exception:
        return None


def _teach_session(agent, tool: str) -> None:
    sel = select_teacher()
    if not sel:
        return
    tp, tm = sel
    tool_obj = (getattr(agent, "tools", {}) or {}).get(tool)
    if tool_obj is None or not hasattr(tool_obj, "query_llm"):
        return

    def teacher_llm(messages, max_tokens=_MAX_TOKENS, temperature=0.3, **kw):
        return tool_obj.query_llm(messages, max_tokens=max_tokens, temperature=temperature,
                                  provider=tp, model=tm, **kw)

    # 1) Teacher demonstration -> seed.
    seed = _demonstrate(tool, tool_obj, teacher_llm)

    # 2) Rate-limit up front so a failed/looping session can't re-trigger within the cooldown.
    #    train_tool preserves this field (it only resets the predict catalogue + baskets).
    try:
        r0 = store.load(tool)
        if r0 is not None:
            r0["teacher_refreshed_at"] = time.time()
            store.save(r0)
    except Exception:
        pass

    # 3) Co-learning run via jobs (dashboard status for free); the "teacher_llm" kwarg also tells
    #    jobs._run NOT to re-escalate -> no recursion. Wait for it (serialize the worker).
    from vaf.whare_wananga import jobs
    try:
        jobs.start_training(agent, tool, teacher_llm=teacher_llm, seed_record=seed,
                            source="teacher", max_rounds=_MAX_ROUNDS)
        while jobs.is_running(tool):
            time.sleep(1.0)
    except Exception:
        pass

    try:
        from vaf.core.log_helper import append_domain_log
        st = jobs.get_status(tool) or {}
        append_domain_log("backend", f"[WW-TEACHER] {tool}: teacher={tp}/{tm} -> state={st.get('state')} "
                          f"status={st.get('status')} challenge_passed={st.get('challenge_passed')}")
    except Exception:
        pass
