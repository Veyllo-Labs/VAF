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
from typing import Optional

from fastapi import APIRouter, Query
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


@router.get("/status")
def supervisor_status(session: Optional[str] = Query(None)):
    """
    Return the currently-running sub-agent units. Optional ?session=<id> filters to one
    session; otherwise all active units are returned.
    """
    try:
        from vaf.core.subagent_ipc import get_ipc
        from vaf.core.config import Config
        ipc = get_ipc()
        liveness = float(Config.get("subagent_liveness_timeout_seconds", 60))
        tasks = ipc.get_active_tasks(session_id=session) if session else ipc.get_active_tasks()
    except Exception as exc:
        return {"units": [], "error": str(exc)}

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
    units.sort(key=lambda u: (u.get("runtime_s") or 0), reverse=True)
    return {"units": units, "liveness_timeout_s": liveness, "count": len(units)}


class CancelBody(BaseModel):
    task_id: str


@router.post("/cancel")
def supervisor_cancel(body: CancelBody):
    """Kill the child process for one unit and fail its IPC task (the watchdog 'kill' action)."""
    task_id = (body.task_id or "").strip()
    if not task_id:
        return {"ok": False, "error": "task_id required"}
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
