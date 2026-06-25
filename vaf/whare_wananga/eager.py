# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Whare Wananga -- EAGER training: opt-in, proactively train SAFE, configured, not-yet-attempted tools
in the background, one at a time.

Off by default (Config ``whare_wananga_eager_enabled``). It NEVER auto-trains send/communication or
``irreversible`` tools -- probing one of those could act on the user's behalf (e.g. actually send a
message). A single serialized worker IS the concurrency queue: exactly one training runs at a time,
reusing ``jobs.start_training`` so the existing dashboard status reflects progress. The trigger is a
lightweight periodic scan of tool preconditions (no per-connection config hooks needed): a
connection configured between ticks is picked up on the next scan -- the "on-connect" behaviour.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Dict, List, Optional

from vaf.whare_wananga import store, jobs
from vaf.whare_wananga.preconditions import tool_precondition

_SETTING = "whare_wananga_eager_enabled"
_SCAN_INTERVAL = 60.0
# send/communication verbs -- never auto-train (defense in depth vs a mis-classified side_effect_class)
_SEND_RE = re.compile(r"(send|post|reply|publish|broadcast|notify|forward|sms|tweet|share|\bdm\b)",
                      re.IGNORECASE)

_lock = threading.Lock()
_queue: List[str] = []          # pending tool names (FIFO)
_done: List[str] = []           # tools processed this process
_current: Optional[str] = None
_worker: Optional[threading.Thread] = None
_scanner: Optional[threading.Thread] = None


def is_enabled() -> bool:
    try:
        from vaf.core.config import Config
        return bool(Config.get(_SETTING, False))
    except Exception:
        return False


def set_enabled(on: bool) -> None:
    from vaf.core.config import Config
    Config.set(_SETTING, bool(on))


def _is_send_tool(name: str) -> bool:
    return bool(_SEND_RE.search(name or ""))


def _is_safe_class(tool_obj) -> bool:
    return getattr(tool_obj, "side_effect_class", "none") != "irreversible"


def _already_attempted(name: str) -> bool:
    """True if the tool already has a record that is NOT stale (learned/draft/declare/halted) --
    don't re-attempt it. A `stale` record (tool changed) IS eligible for a fresh attempt."""
    rec = store.load(name)
    return rec is not None and rec.get("status") != "stale"


def _eligible(name: str, tool_obj) -> bool:
    """A tool is eager-eligible iff it is SAFE, configured, trainable and not already attempted."""
    try:
        if not name or _is_send_tool(name) or not _is_safe_class(tool_obj):
            return False
        if _already_attempted(name) or jobs.is_running(name):
            return False
        with _lock:
            if name == _current or name in _queue:
                return False
        if not tool_precondition(name).get("configured", True):
            return False
        return True
    except Exception:
        return False


def eligible_tools(agent) -> List[str]:
    tools = getattr(agent, "tools", {}) or {}
    return sorted(n for n, obj in tools.items() if _eligible(n, obj))


def enqueue(agent, names) -> int:
    """Add eligible tools to the queue (dedup) and ensure the worker is running. Returns count added."""
    added = 0
    with _lock:
        for n in names:
            if n and n != _current and n not in _queue:
                _queue.append(n)
                added += 1
    if added:
        _ensure_worker(agent)
    return added


def scan(agent) -> int:
    """If enabled, enqueue every eligible safe tool. Returns count enqueued. Fail-safe."""
    try:
        if not is_enabled():
            return 0
        return enqueue(agent, eligible_tools(agent))
    except Exception:
        return 0


def _ensure_worker(agent) -> None:
    global _worker
    with _lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_run_worker, args=(agent,),
                                   name="ww-eager-worker", daemon=True)
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
            obj = (getattr(agent, "tools", {}) or {}).get(tool)
            # re-check at run time (state may have changed since enqueue)
            if obj is None or _is_send_tool(tool) or not _is_safe_class(obj) \
                    or _already_attempted(tool) or jobs.is_running(tool):
                continue
            jobs.start_training(agent, tool)
            while jobs.is_running(tool):       # serialize: one training at a time
                time.sleep(1.0)
            with _lock:
                _done.append(tool)
            try:
                from vaf.core.log_helper import append_domain_log
                st = jobs.get_status(tool) or {}
                append_domain_log("backend", f"[WW-EAGER] trained {tool}: state={st.get('state')}")
            except Exception:
                pass
        except Exception:
            continue
        finally:
            with _lock:
                _current = None


def start(get_agent) -> None:
    """Start the periodic background scanner once. `get_agent` is a callable returning the agent
    (or None if not built yet) -- so this can be wired before the agent exists. Fail-safe."""
    global _scanner
    try:
        with _lock:
            if _scanner is not None and _scanner.is_alive():
                return
            _scanner = threading.Thread(target=_run_scanner, args=(get_agent,),
                                        name="ww-eager-scanner", daemon=True)
            _scanner.start()
    except Exception:
        pass


def _run_scanner(get_agent) -> None:
    while True:
        try:
            ag = get_agent() if callable(get_agent) else get_agent
            if ag is not None and getattr(ag, "tools", None) and is_enabled():
                scan(ag)
        except Exception:
            pass
        time.sleep(_SCAN_INTERVAL)


def status() -> Dict[str, Any]:
    """Process-local queue snapshot + the (shared) enabled flag. Note: the live queue lives in the
    app process; a separate CLI process sees only the setting."""
    with _lock:
        return {"enabled": is_enabled(), "current": _current,
                "queued": list(_queue), "done": list(_done)}
