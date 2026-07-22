# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Watchdog / supervisor status API.

Exposes the live sub-agent child processes (from the IPC active queue) so the WebUI can show
what is running *right now* — agent type, runtime, heartbeat age, staleness — and lets the
user kill a specific unit. This is the observability + control surface over the bounded,
killable sub-agent execution (each heavy sub-agent runs in its own process and heartbeats
~every 3 s).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

router = APIRouter(prefix="/api/supervisor", tags=["supervisor"])


def _age_seconds(iso_ts: Optional[str]) -> Optional[float]:
    """Seconds since an ISO timestamp, or None if missing/unparseable."""
    if not iso_ts:
        return None
    try:
        return max(0.0, (datetime.now() - datetime.fromisoformat(iso_ts)).total_seconds())
    except Exception:
        return None


def _caller(request: Request) -> Dict[str, Any]:
    """Caller from auth middleware; tokenless localhost = the local admin."""
    user = getattr(request.state, "user", None)
    if user and isinstance(user, dict):
        return user
    from vaf.core.config import get_local_admin_scope_id
    return {"username": "admin", "role": "admin", "user_scope_id": str(get_local_admin_scope_id())}


def _is_admin(user: Dict[str, Any]) -> bool:
    """Role-aware admin check, mirroring _ws_session_owner_ok (web_server.py)."""
    if str(user.get("role") or "").lower() == "admin":
        return True
    try:
        from vaf.core.config import get_local_admin_scope_id
        scope = user.get("user_scope_id")
        return scope is not None and str(scope) == str(get_local_admin_scope_id())
    except Exception:
        return False


def _owned_session_ids(user_scope_id: Optional[str]) -> Set[str]:
    """Chat-session ids owned by this scope (STRICT: sessions without a recorded
    scope are admin-only, matching the WS ownership policy - the list() helper's
    legacy-inclusion convenience is deliberately filtered back out here)."""
    if not user_scope_id:
        return set()
    try:
        from vaf.core.session import SessionManager
        rows = SessionManager().list(limit=500, user_scope_id=str(user_scope_id))
        return {
            str(r.get("id"))
            for r in rows
            if r.get("id") and str(r.get("user_scope_id") or "") == str(user_scope_id)
        }
    except Exception:
        return set()  # fail CLOSED: no ownership info -> a non-admin sees nothing


async def _usernames_for_sessions(session_ids: List[str]) -> Dict[str, str]:
    """session_id -> username for the admin view (defensive, {} on any failure)."""
    out: Dict[str, str] = {}
    try:
        from vaf.api.security_routes import _scope_username_map
        from vaf.core.config import get_local_admin_username
        from vaf.core.session import SessionManager
        names = await _scope_username_map()
        mgr = SessionManager()
        for sid in session_ids:
            try:
                loaded = mgr.load(sid)
                scope = (getattr(loaded, "metadata", None) or {}).get("user_scope_id")
                out[sid] = names.get(str(scope), "") if scope else str(get_local_admin_username() or "admin")
            except Exception:
                continue
    except Exception:
        return {}
    return out


@router.get("/status")
async def supervisor_status(request: Request, session: Optional[str] = Query(None)):
    """
    Return the currently-running sub-agent units.

    Authorization (unit payloads carry user-authored task text, and task_ids
    enable /cancel - both leaked cross-user before this gate):
      - admin / local desktop: unchanged - ?session filters, no ?session = all
        units (the dashboard watchdog view), each attributed with a username.
      - non-admin: restricted to units of sessions OWNED by the caller's scope;
        a foreign or unowned ?session yields an empty list, never 403 noise
        (the web tool bubble polls generically).
    """
    user = _caller(request)
    admin = _is_admin(user)
    try:
        from vaf.core.subagent_ipc import get_ipc
        from vaf.core.config import Config
        ipc = get_ipc()
        liveness = float(Config.get("subagent_liveness_timeout_seconds", 60))
        tasks = ipc.get_active_tasks(session_id=session) if session else ipc.get_active_tasks()
    except Exception as exc:
        return {"units": [], "error": str(exc)}

    if not admin:
        owned = _owned_session_ids(user.get("user_scope_id"))
        tasks = [t for t in tasks if str(getattr(t, "session_id", "") or "") in owned]

    units = []
    for t in tasks:
        hb_age = _age_seconds(getattr(t, "last_heartbeat", None))
        runtime = _age_seconds(getattr(t, "created_at", None))
        units.append({
            "task_id": t.task_id,
            "agent_type": t.agent_type,
            "session_id": t.session_id,
            "status": t.status,
            "task": (t.task_description or "")[:160],
            "runtime_s": round(runtime, 1) if runtime is not None else None,
            "heartbeat_age_s": round(hb_age, 1) if hb_age is not None else None,
            # No heartbeat for longer than the liveness window → likely dead/stuck.
            "stale": (hb_age is not None and hb_age > liveness),
        })
    if admin and units:
        by_session = await _usernames_for_sessions(sorted({u["session_id"] for u in units if u["session_id"]}))
        for u in units:
            u["username"] = by_session.get(u["session_id"] or "", "")
    units.sort(key=lambda u: (u.get("runtime_s") or 0), reverse=True)
    return {"units": units, "liveness_timeout_s": liveness, "count": len(units)}


class CancelBody(BaseModel):
    task_id: str


@router.post("/cancel")
def supervisor_cancel(body: CancelBody, request: Request):
    """Kill the child process for one unit and fail its IPC task (the watchdog 'kill' action).

    Same authorization as /status: a non-admin may only kill units belonging to
    their OWN sessions (before this gate any authenticated user could enumerate
    task_ids via /status and kill other users' sub-agents)."""
    task_id = (body.task_id or "").strip()
    if not task_id:
        return {"ok": False, "error": "task_id required"}
    user = _caller(request)
    if not _is_admin(user):
        try:
            from vaf.core.subagent_ipc import get_ipc as _get_ipc
            target = next((t for t in _get_ipc().get_active_tasks() if t.task_id == task_id), None)
        except Exception:
            target = None
        owned = _owned_session_ids(user.get("user_scope_id"))
        if target is None or str(getattr(target, "session_id", "") or "") not in owned:
            return {"ok": False, "error": "not permitted"}
    killed = 0
    try:
        from vaf.core.platform import Platform
        killed = Platform.stop_webui_subagent_process_by_task(task_id)
    except Exception:
        killed = 0
    # Always fail the IPC task too — even if no process was tracked (it may have already
    # exited or run only in-process); this unblocks any engine wait on the result.
    try:
        from vaf.core.subagent_ipc import get_ipc
        get_ipc().fail_task(task_id, "[USER_CANCELLED] Killed via watchdog.")
    except Exception:
        pass
    return {"ok": True, "task_id": task_id, "killed_processes": killed}
