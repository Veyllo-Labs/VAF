# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Admin-only system maintenance: are the services healthy, is an update due.

Backs Settings -> Advanced -> Update and Repair in the Web UI. The work itself
is the framework's (`vaf/core/service_health.py`, `vaf/core/update_check.py`)
and the same functions back `vaf repair` and `vaf update`, so a terminal and a
browser cannot disagree about what is wrong or what was done.

Two shapes worth knowing before reading on:

- **Repair is a job, not a request.** Starting a container engine can take
  minutes; a request held open that long dies in a proxy or a browser. POST
  starts it, GET follows the steps as they finish.
- **An update is spawned, not awaited.** The update stops the VAF service, and
  this server IS that service. The updater is therefore detached and outlives
  us; the client watches `GET /api/version` and sees the answer change when the
  new version is up.

No network call happens here unless a person pressed a button: the version
cache is read from disk, and only `POST /update/check` reaches GitHub.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from starlette.concurrency import run_in_threadpool

from vaf import __version__
from vaf.api.user_routes import require_admin
from vaf.core.service_health import collect_service_status, repair_service_stack
from vaf.core.update_check import (
    check_now,
    describe_update_ability,
    read_last_update,
    read_update_cache,
    read_update_result,
    spawn_update_process,
)

router = APIRouter(prefix="/api/system", tags=["system"])

# One repair at a time, process-wide: two runs would fight over the same
# containers, and `compose up` is only idempotent one caller at a time.
_repair_lock = threading.Lock()
_repair_state: Dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "steps": [],
    "result": None,
    "error": None,
}

# An update is spawned and never awaited, so nothing downstream would notice a
# second one: two detached `vaf update` processes would fetch, stop and reset
# the same checkout against each other. The marker is never cleared, because
# there is no "after" to clear it in - this process is about to be stopped by
# the update it just started.
_update_lock = threading.Lock()
_update_started_at: Optional[str] = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.get("/services")
async def system_services(_: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    """Every container's state, including whether it ANSWERS - not just whether
    it runs. Blocking probes go to the threadpool like the security overview."""
    return await run_in_threadpool(collect_service_status)


def _run_repair() -> None:
    def progress(step: Dict[str, Any]) -> None:
        _repair_state["steps"].append(step)

    try:
        result = repair_service_stack(progress=progress)
        _repair_state["result"] = result
    except Exception as e:      # a repair must never take the server with it
        _repair_state["error"] = str(e)[:300]
    finally:
        _repair_state["running"] = False
        _repair_state["finished_at"] = _now()


@router.post("/services/repair", status_code=202)
def start_repair(_: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    """Start a repair run and return at once; follow it with GET on this path."""
    with _repair_lock:
        if _repair_state["running"]:
            raise HTTPException(status_code=409, detail="A repair is already running")
        _repair_state.update({
            "running": True,
            "started_at": _now(),
            "finished_at": None,
            "steps": [],
            "result": None,
            "error": None,
        })
    threading.Thread(target=_run_repair, daemon=True, name="vaf-api-repair").start()
    return {"started": True, "started_at": _repair_state["started_at"]}


@router.get("/services/repair")
def repair_progress(_: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    """The run so far: the steps that have finished, and the result once done."""
    state = dict(_repair_state)
    state["steps"] = list(_repair_state["steps"])
    return state


@router.get("/update")
def update_state(_: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    """What is installed, what the last check found, and whether this install
    can update itself. Reads disk only - the network is the button's job."""
    can_apply, reason = describe_update_ability()
    return {
        "current": __version__,
        "cache": read_update_cache(),
        "last_update": read_last_update(),
        # How the LAST update run ended (succeeded/rolled_back/recover_needed/
        # failed). The waiting dialog reads this to tell a rollback (the old
        # version answering again WITH a fresh result) from an updater that
        # simply has not stopped the service yet (old version, no result).
        "last_result": read_update_result(),
        "can_apply": can_apply,
        "reason": reason,
    }


@router.post("/update/check")
async def update_check_now(_: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    """Ask GitHub now and record when. Only ever reached by an explicit press."""
    result = await run_in_threadpool(check_now)
    can_apply, reason = describe_update_ability()
    result["can_apply"] = can_apply
    result["reason"] = reason
    return result


@router.post("/update/apply")
def update_apply(_: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    """Start the update and answer immediately, because the answer is the last
    thing this process does before it stops itself.

    The client's contract from here: poll `GET /api/version`. Unreachable means
    the restart is in progress; a NEWER version means it worked; the OLD version
    coming back means the update failed and rolled itself back, and
    `GET /api/system/update` then says whether a breadcrumb was left behind.
    """
    global _update_started_at

    can_apply, reason = describe_update_ability()
    if not can_apply:
        raise HTTPException(status_code=409, detail=reason)

    unfinished = read_last_update()
    if unfinished:
        raise HTTPException(
            status_code=409,
            detail=("An earlier update did not finish. Run `vaf update --recover` in a "
                    "terminal first, so this one does not start from a half-swapped "
                    "checkout."),
        )
    if _repair_state["running"]:
        raise HTTPException(status_code=409,
                            detail="A repair is running; wait for it to finish first.")

    blocker = _restart_blocker()
    if blocker:
        raise HTTPException(status_code=409, detail=blocker)

    # Claim the lane BEFORE spawning: two clicks, or two admins, would otherwise
    # both pass every check above and start two updaters on one checkout.
    with _update_lock:
        if _update_started_at is not None:
            raise HTTPException(
                status_code=409,
                detail=("An update was already started from here. Watch /api/version; "
                        "the server restarts when it finishes."),
            )
        _update_started_at = _now()

    try:
        spawned = spawn_update_process()
    except RuntimeError as e:       # server mode without a way to survive the stop
        with _update_lock:
            _update_started_at = None
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        with _update_lock:
            _update_started_at = None
        raise HTTPException(status_code=500, detail=f"Could not start the updater: {e}")

    # started_at is SERVER clock: the updater stamps the result file with the
    # same clock, so the client can accept only results from THIS run without
    # comparing across machines.
    return {"started": True, "poll": "/api/version", "current": __version__,
            "started_at": _update_started_at, **spawned}


def _restart_blocker() -> Optional[str]:
    """Why an update started from here would not come back, or None.

    In desktop mode the updater stops VAF through the pidfile `vaf start`
    writes. Started any other way (`vaf tray` by hand, the crash supervisor,
    a desktop entry), there is no pidfile: the stop step would quietly do
    nothing, the checkout would be swapped under a running server, and the
    start step would add a SECOND instance. Saying so beats handing someone a
    button that promises a restart and delivers two servers.
    """
    try:
        from vaf.core.config import Config
        if bool(Config.get("server_mode", False)):
            return None                     # systemd owns the lifecycle there
    except Exception:
        return None
    try:
        from pathlib import Path
        # The SERVICE pid file (vaf start / the tray dashboard lane). Not
        # server.pid - that one is the llama backend's and means something else.
        pid_file = Path.home() / ".vaf" / "service.pid"
        if pid_file.exists():
            return None
    except Exception:
        return None
    return ("This VAF was not started as a background service, so the updater cannot "
            "stop and start it for you. Update it from a terminal with `vaf update`, "
            "or start VAF with `vaf start` once and try again.")
