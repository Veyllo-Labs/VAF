# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Taking an account's access away takes effect at once, for work already running too.

WHY THIS EXISTS. Deactivating, deleting or narrowing an account changed the store and
nothing else. The account's token kept working for up to a day, a turn it had started ran
on with the role it was queued with (an admin demoted mid-turn kept the admin exemption),
its sub-agents and background commands kept running, and a deleted account's permission
lookup read "no row" as "no restriction". Stopping a chat existed - the Stop button - but
only per chat and only from the chat itself; nothing could stop everything one account had
running.

WHAT IT DOES.
- `stop_session(session_id)` is the Stop button as a function: the generation stops at the
  next check, queued follow-ups are dropped, a waiting confirmation dialog is answered
  "cancel", and - unless `include_subagents=False` - the session's sub-agent processes are
  ended and their tasks reported as cancelled. The WebSocket Stop handler calls it.
- `stop_account_work(user_scope_id)` does that for every session the account has queued or
  running, stops the account's background commands (`vaf.core.processes`) - which Stop
  deliberately spares and a revocation does not - and tells the registered listeners (the
  web server closes the account's sockets, which carry the old role).
- `revoke_account(user_scope_id)` marks the account revoked for this process and stops its
  work. While marked, the tool funnel refuses every call of that scope BEFORE the admin
  exemption (a queued turn still carries its old role), its own stop check ends a call
  already running, and the runner drops its queued turns. `restore_account` lifts the mark
  (reactivation).
- `account_stands(user_scope_id)` is the question the lanes that START work without a token
  ask (the runner for a queued turn, the scheduler for an automation): the mark, and the
  application's account directory, so a deactivation outlives a restart.

Framework, not harness: nothing here knows about tokens, users or the auth store. The
application decides WHEN an account lost its access (VAF's admin routes do) and calls these.

NAMED BOUNDARY: the mark lives in this process. A second VAF process on the same machine
(a `vaf run` session) learns of a deactivation through its own permission lookup, which now
answers "nothing" for a missing or inactive account within its cache lifetime (seconds), and
through `account_stands` for the work it would start.
"""
from __future__ import annotations

import threading
from typing import Callable, Dict, List, Optional

_lock = threading.Lock()
_revoked: set = set()
_listeners: List[Callable[[str], None]] = []


def _key(user_scope_id) -> str:
    return str(user_scope_id or "").strip()


def is_revoked(user_scope_id) -> bool:
    """Whether this process has been told the account lost its access."""
    key = _key(user_scope_id)
    if not key:
        return False
    with _lock:
        return key in _revoked


def account_stands(user_scope_id) -> bool:
    """Whether work may START for this account: the lanes that begin work for a scope without
    a token in hand (a scheduled automation, a queued turn) ask this first.

    No: revoked in this process, or the application's account directory
    (``tool_dispatch.set_account_directory_resolver``) names the account inactive or no
    longer names it at all (deleted). Yes: no scope or the machine owner's (neither is an
    account that can be taken away), or a directory that is empty - nothing registered, or a
    store that cannot be reached, which is the desktop default the permission lookup keeps
    too. A directory lookup is a store query, so it is asked after the cheap answers."""
    key = _key(user_scope_id)
    if not key:
        return True
    if is_revoked(key):
        return False
    try:
        # trust's canonical key, as the runner and the queue compare scopes: the machine
        # owner's scope and its aliases ("default") are "default", never an account.
        from vaf.core.trust import _scope_key
        canonical = _scope_key(key)
        if canonical == "default":
            return True
        from vaf.core.tool_dispatch import resolve_account_directory
        rows = resolve_account_directory()
    except Exception:
        return True
    if not rows:
        return True
    for row in rows:
        if _scope_key(row["user_scope_id"]) == canonical:
            return bool(row.get("active", True))
    return False


def add_revocation_listener(listener: Callable[[str], None]) -> None:
    """`listener(user_scope_id)` runs when an account's work is stopped; it must not raise
    (an exception is swallowed). The web server registers one that closes the sockets."""
    with _lock:
        if listener not in _listeners:
            _listeners.append(listener)


def remove_revocation_listener(listener: Callable[[str], None]) -> None:
    with _lock:
        if listener in _listeners:
            _listeners.remove(listener)


def stop_session(session_id: str, *, include_subagents: bool = True,
                 reason: str = "[USER_CANCELLED] Stopped/Cancelled by user via stop button.") -> Dict:
    """Stop one chat: generation, queued follow-ups, a waiting confirmation and - unless
    told otherwise - its sub-agents. Returns what it did. Never raises."""
    sid = str(session_id or "").strip()
    result = {"dropped": 0, "killed": 0, "subagents_kept": False, "gate_cancelled": False}
    if not sid:
        return result
    try:
        from vaf.core.task_queue import TaskQueue
        tq = TaskQueue()
        tq.request_stop(sid)
        result["dropped"] = tq.drop_queued_tasks_for_session(sid)
    except Exception:
        pass
    try:
        from vaf.core.web_interface import get_web_interface
        result["gate_cancelled"] = get_web_interface().cancel_gate(sid)
    except Exception:
        pass
    has_subagents = False
    try:
        from vaf.core.subagent_ipc import get_ipc
        has_subagents = bool(get_ipc().get_active_tasks(session_id=sid))
    except Exception:
        has_subagents = False
    if has_subagents and not include_subagents:
        result["subagents_kept"] = True
        return result
    try:
        from vaf.core.platform import Platform
        result["killed"] = Platform.stop_webui_subagent_processes(sid)
    except Exception:
        result["killed"] = 0
    try:
        from vaf.core.subagent_ipc import get_ipc
        ipc = get_ipc()
        for task in ipc.get_active_tasks(session_id=sid):
            try:
                ipc.fail_task(task.task_id, reason)
            except Exception:
                pass
    except Exception:
        pass
    return result


def stop_account_work(user_scope_id) -> Dict:
    """Stop everything one account has queued or running in this process. Never raises."""
    key = _key(user_scope_id)
    summary = {"sessions": 0, "processes": 0}
    if not key:
        return summary
    reason = "[ACCESS_REVOKED] Stopped: the account's access was changed by an administrator."
    sessions = set()
    try:
        from vaf.core.task_queue import TaskQueue
        sessions |= TaskQueue().sessions_for_scope(key)
    except Exception:
        pass
    # A chat known only from a background command has no turn running or queued, so the stop
    # flag stop_session leaves would wait for that chat's NEXT turn and swallow it - for an
    # account whose tools were only narrowed, that is the person's next message.
    queued = set(sessions)
    try:
        from vaf.core import processes
        for record in processes.list_for_scope(key):
            if record.session_id:
                sessions.add(str(record.session_id))
            try:
                processes.stop(record)
                summary["processes"] += 1
            except Exception:
                pass
    except Exception:
        pass
    for sid in sorted(sessions):
        stop_session(sid, include_subagents=True, reason=reason)
        if sid not in queued:
            try:
                from vaf.core.task_queue import TaskQueue
                TaskQueue().clear_stop(sid)
            except Exception:
                pass
    summary["sessions"] = len(sessions)
    with _lock:
        listeners = list(_listeners)
    for listener in listeners:
        try:
            listener(key)
        except Exception:
            pass
    return summary


def revoke_account(user_scope_id) -> Optional[Dict]:
    """Mark the account revoked in this process and stop its work."""
    key = _key(user_scope_id)
    if not key:
        return None
    with _lock:
        _revoked.add(key)
    return stop_account_work(key)


def restore_account(user_scope_id) -> None:
    """Lift the mark (the account was reactivated)."""
    key = _key(user_scope_id)
    with _lock:
        _revoked.discard(key)
