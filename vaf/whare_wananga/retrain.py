# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Whare Wananga -- persistent RE-TRAINING queue for gate-failing records.

Before this queue, a record that failed the delivery quality gate (stale after a
schema change, declare-mode, draft, failed challenge) simply rotted: it was never
delivered AND nothing ever re-trained it -- on 2026-07-11, 18 of 67 live records
were in that state, including the two whose know-how would have prevented the
original incident. Now every gate reject is enqueued here, and the queue is
drained either manually (``vaf ww retrain --pending`` / ``vaf ww queue``) or
automatically inside the eager worker thread (opt-in via the existing
``whare_wananga_eager_enabled`` flag; one training at a time).

Storage: a single JSON file at ``~/.vaf/whare_wananga_retrain.json`` --
deliberately OUTSIDE the record store directory, because ``store.list_tools()``
globs ``*.json`` there and would mistake the queue for a tool record.

Limits: per-tool attempt cap (a never-converging tool must not re-train forever
at ~50 LLM calls per attempt) and a 24h cooldown between attempts. Declare-mode
records are excluded from draining by default: re-training deterministically
reproduces declare (the runner's safety tiering, not a transient failure) --
``--include-declare`` is the escape hatch.

Cross-process note: training job status (``jobs.py``) is process-local, so a CLI
drain cannot see an in-app training and vice versa. Both would serialize-fight a
single local llama server. Drain from ONE side at a time; the in-app auto-drain
already serializes through the eager worker.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from vaf.core.platform import Platform
from vaf.whare_wananga import store

MAX_ATTEMPTS = 3
COOLDOWN_SECONDS = 24 * 3600

_lock = threading.Lock()


def _queue_path() -> Path:
    return Platform.vaf_dir() / "whare_wananga_retrain.json"


def _load_queue() -> Dict[str, Dict[str, Any]]:
    try:
        raw = json.loads(_queue_path().read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _save_queue(q: Dict[str, Dict[str, Any]]) -> None:
    path = _queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".ww_retrain_", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(q, f, indent=1)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        raise


def classify(rec: Optional[Dict[str, Any]]) -> str:
    """Why a record fails the delivery gate (or 'verified' / 'missing').

    'interrupted' = status 'learning': only written mid-run (a crash marker, not in
    store.VALID_STATUS), distinct from 'draft', which is a legitimate terminal
    outcome of a run that never converged."""
    if not rec:
        return "missing"
    status = rec.get("status")
    if status == "stale":
        return "stale"
    if status == "learning":
        return "interrupted"
    if status == "draft":
        return "draft"
    if rec.get("learn_mode") == "declare":
        return "declare"
    if rec.get("challenge_passed") is not True:
        return "challenge_failed"
    if rec.get("learn_mode") not in ("probe", "error_path", "runtime", "teacher"):
        return "unprobed"
    return "verified"


def enqueue(tool: str, reason: str = "") -> bool:
    """Add a tool to the re-training queue (dedup by name). Fail-safe: never raises
    (called from the delivery hot path via the cached gate loader)."""
    try:
        name = (tool or "").strip()
        if not name:
            return False
        with _lock:
            q = _load_queue()
            if name in q:
                return False
            q[name] = {
                "reason": reason or "gate_failed",
                "enqueued_at": time.time(),
                "attempts": 0,
                "last_attempt_at": 0.0,
            }
            _save_queue(q)
        return True
    except Exception:
        return False


def remove(tool: str) -> None:
    try:
        with _lock:
            q = _load_queue()
            if q.pop(tool, None) is not None:
                _save_queue(q)
    except Exception:
        pass


def mark_attempt(tool: str) -> None:
    try:
        with _lock:
            q = _load_queue()
            e = q.get(tool)
            if e is not None:
                e["attempts"] = int(e.get("attempts", 0)) + 1
                e["last_attempt_at"] = time.time()
                _save_queue(q)
    except Exception:
        pass


def _drainable(entry: Dict[str, Any], reason: str, include_declare: bool) -> bool:
    if reason in ("verified", "missing"):
        return False
    if reason == "declare" and not include_declare:
        return False
    if int(entry.get("attempts", 0)) >= MAX_ATTEMPTS:
        return False
    if time.time() - float(entry.get("last_attempt_at", 0.0)) < COOLDOWN_SECONDS \
            and int(entry.get("attempts", 0)) > 0:
        return False
    return True


def pending(include_declare: bool = False, all_entries: bool = False) -> List[Dict[str, Any]]:
    """Queue entries with their LIVE classification (recomputed from the store).

    Entries whose record became verified or vanished are pruned from the file.
    Default view: only entries eligible for draining now (attempt cap, cooldown,
    declare exclusion); ``all_entries=True`` returns everything for display."""
    out: List[Dict[str, Any]] = []
    try:
        with _lock:
            q = _load_queue()
            pruned = False
            for name in list(q.keys()):
                reason = classify(store.load(name))
                if reason in ("verified", "missing"):
                    q.pop(name)
                    pruned = True
                    continue
                q[name]["reason"] = reason
            if pruned:
                _save_queue(q)
            entries = {n: dict(e) for n, e in q.items()}
        for name, e in sorted(entries.items()):
            e["tool"] = name
            if all_entries or _drainable(e, e.get("reason", ""), include_declare):
                out.append(e)
    except Exception:
        pass
    return out


def has_pending(include_declare: bool = False) -> bool:
    return bool(pending(include_declare=include_declare))


def scan_store(tools=None) -> int:
    """Seed the queue from the store: enqueue every record that fails the delivery
    gate. ``tools`` (optional registry) limits the scan to tools that still exist.
    Returns the number of newly enqueued tools."""
    added = 0
    try:
        for name in store.list_tools():
            if tools is not None and hasattr(tools, "get") and tools.get(name) is None:
                continue
            reason = classify(store.load(name))
            if reason in ("verified", "missing"):
                continue
            if enqueue(name, reason):
                added += 1
    except Exception:
        pass
    return added


def drain_one(agent, include_declare: bool = False) -> Optional[Dict[str, Any]]:
    """Re-train ONE eligible queued tool via the shared background-jobs runner and
    wait for it. Returns the job status (or None if nothing was eligible).

    Serialization is the CALLER's job: this is invoked from the eager worker
    thread (in-app) or a foreground CLI loop -- never from two places at once in
    the same process (jobs.start_training additionally refuses double-starts)."""
    try:
        from vaf.whare_wananga import jobs
        for e in pending(include_declare=include_declare):
            name = e["tool"]
            if (getattr(agent, "tools", {}) or {}).get(name) is None:
                continue
            if jobs.is_running(name):
                continue
            mark_attempt(name)
            try:
                from vaf.core.log_helper import append_domain_log
                append_domain_log("backend", f"[WW-RETRAIN] start {name} (reason={e.get('reason')}, "
                                             f"attempt {e.get('attempts', 0) + 1}/{MAX_ATTEMPTS})")
            except Exception:
                pass
            jobs.start_training(agent, name)
            while jobs.is_running(name):
                time.sleep(1.0)
            if classify(store.load(name)) == "verified":
                remove(name)
            try:
                from vaf.core.log_helper import append_domain_log
                st = jobs.get_status(name) or {}
                append_domain_log("backend", f"[WW-RETRAIN] done {name}: state={st.get('state')}")
            except Exception:
                pass
            return jobs.get_status(name) or {"tool": name}
    except Exception:
        pass
    return None
