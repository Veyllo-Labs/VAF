# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Admin-only system maintenance: are the services healthy, is an update due, the backups.

Backs Settings -> Advanced -> Update and Repair in the Web UI. The work itself
is the framework's (`vaf/core/service_health.py`, `vaf/core/update_check.py`)
and the same functions back `vaf repair` and `vaf update`, so a terminal and a
browser cannot disagree about what is wrong or what was done.

Two shapes worth knowing before reading on:

- **Repair is a job, not a request.** Starting a container engine can take
  minutes; a request held open that long dies in a proxy or a browser. POST
  starts it, GET follows the steps as they finish. A switch of the inference
  profile is a job for the same reason: back to Full it runs `compose up` for
  the speech containers, which pulls a 7.5 GB image after a prune, and the
  HTTPS proxy in front of a server gives up on a request after 60 seconds.
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
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from vaf import __version__
from vaf.api.user_routes import require_admin
from vaf.core import container_jobs
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

# One container job at a time, process-wide (vaf/core/container_jobs.py): two runs would
# fight over the same containers, and `compose up` is only idempotent one caller at a time.
# A repair, a profile switch and a backup (which dumps the database from its container) each
# claim it, and so does the backup schedule in this process. The dicts below are what the
# pages follow; the claim is what decides.
_repair_state: Dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "steps": [],
    "result": None,
    "error": None,
}
_profile_state: Dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "profile": None,
    "result": None,
    "error": None,
}

# "Back up now" from Settings > Advanced (vaf/core/backup.py): a job like the repair, since a
# backup takes from seconds to minutes and a proxy gives up on a request after 60 seconds.
_backup_state: Dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "stage": None,
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


def _claim_or_409(kind: str) -> None:
    """Claim the container job slot for ``kind``, or answer 409 naming what runs."""
    other = container_jobs.claim(kind)
    if other is not None:
        raise HTTPException(status_code=409, detail=container_jobs.busy_message(kind, other))


def _start_job(target, args, name: str, kind: str, state: Dict[str, Any]) -> None:
    """Run a claimed job in its thread; a thread that cannot start gives the claim back."""
    try:
        threading.Thread(target=target, args=args, daemon=True, name=name).start()
    except Exception:
        state["running"] = False
        container_jobs.release(kind)
        raise


@router.get("/services")
async def system_services(_: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    """Every container's state, including whether it ANSWERS - not just whether
    it runs. Blocking probes go to the threadpool like the security overview."""
    return await run_in_threadpool(collect_service_status)


@router.get("/health")
async def system_health(_: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    """Every health check and the verdict (vaf/core/health.py), collected now: what
    `vaf health` prints and what the word at /api/health stands for."""
    from vaf.core.health import collect_health
    return await run_in_threadpool(collect_health)


@router.get("/backups")
def backups_overview(_: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    """The backups as Settings > Advanced shows them: the schedule and what it keeps (the
    config keys the page saves), the health row, the last scheduled run, the backups in the
    folder newest first, and the "Back up now" job. What `vaf backup schedule` and `vaf backup
    list` print."""
    from vaf.core import backup, backup_schedule
    folder = backup.backup_dir_path()
    rows = backup.list_backups(folder) if folder.is_dir() else []
    hour, minute = backup_schedule.slot_time()
    state = backup_schedule.read_state()
    return {
        "schedule": backup_schedule.schedule() or "off",
        "time": f"{hour:02d}:{minute:02d}",
        "keep": backup_schedule.keep_counts(),
        "check": backup_schedule.backup_check(),
        "last_run": {key: state.get(key) for key in ("last_success_at", "last_attempt_at",
                                                       "last_error", "last_file", "last_size")},
        "folder": str(folder),
        "backups": [{"name": r.path.name, "size": r.size, "created": r.created}
                    for r in rows[:BACKUP_LIST_LIMIT]],
        "count": len(rows),
        "job": dict(_backup_state),
    }


# The newest ones are what a person looks at; the folder is pruned to a few dozen anyway.
BACKUP_LIST_LIMIT = 20


def _run_backup() -> None:
    from vaf.core.backup import create_backup

    def progress(stage: str) -> None:
        _backup_state["stage"] = stage

    try:
        result = create_backup(progress=progress)
        _backup_state["result"] = {"name": result.path.name, "size": result.size,
                                   "members": result.members, "warnings": result.warnings}
    except Exception as e:      # a backup must never take the server with it
        _backup_state["error"] = str(e)[:300]
    finally:
        _backup_state["running"] = False
        _backup_state["finished_at"] = _now()
        container_jobs.release(container_jobs.BACKUP)


@router.post("/backups/run", status_code=202)
def start_backup(_: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    """Write a backup now, as a job; GET /api/system/backups follows it (``job``). Not while a
    repair or a profile switch moves the containers the backup reads."""
    _claim_or_409(container_jobs.BACKUP)
    _backup_state.update({
        "running": True,
        "started_at": _now(),
        "finished_at": None,
        "stage": None,
        "result": None,
        "error": None,
    })
    _start_job(_run_backup, (), "vaf-api-backup", container_jobs.BACKUP, _backup_state)
    return {"started": True, "started_at": _backup_state["started_at"]}


@router.get("/inference-profile")
def get_inference_profile(_: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    """The installation's inference profile (vaf/core/inference_profile.py), the services it
    runs and the settings that contradict it - what `vaf profile` prints."""
    from vaf.core import inference_profile, service_stack
    needed = service_stack.needed_services()
    return {
        "profile": inference_profile.current(),
        "services": [s.service_key for s in needed],
        "left_out": [s.service_key for s in service_stack.SERVICES if s not in needed],
        "conflicts": inference_profile.conflicts(),
        "switch": dict(_profile_state),
    }


class _ProfileSwitch(BaseModel):
    profile: str
    prune: bool = False


def _run_profile_switch(profile: str, prune: bool) -> None:
    from vaf.core import inference_profile
    try:
        _profile_state["result"] = inference_profile.apply(profile, prune=prune)
    except Exception as e:      # a switch must never take the server with it
        _profile_state["error"] = str(e)[:300]
    finally:
        _profile_state["running"] = False
        _profile_state["finished_at"] = _now()
        container_jobs.release(container_jobs.PROFILE_SWITCH)


@router.post("/inference-profile", status_code=202)
def set_inference_profile(body: _ProfileSwitch,
                          _: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    """Start a switch and return at once; GET on this path follows it (``switch``). The
    same implementation as `vaf profile`; a wrong request is refused before anything runs."""
    from vaf.core import inference_profile
    try:
        wanted = inference_profile.validate(body.profile, body.prune)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _claim_or_409(container_jobs.PROFILE_SWITCH)
    _profile_state.update({
        "running": True,
        "started_at": _now(),
        "finished_at": None,
        "profile": wanted,
        "result": None,
        "error": None,
    })
    _start_job(_run_profile_switch, (wanted, body.prune), "vaf-api-profile",
               container_jobs.PROFILE_SWITCH, _profile_state)
    return {"started": True, "profile": wanted, "started_at": _profile_state["started_at"]}


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
        container_jobs.release(container_jobs.REPAIR)


@router.post("/services/repair", status_code=202)
def start_repair(_: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    """Start a repair run and return at once; follow it with GET on this path."""
    _claim_or_409(container_jobs.REPAIR)
    _repair_state.update({
        "running": True,
        "started_at": _now(),
        "finished_at": None,
        "steps": [],
        "result": None,
        "error": None,
    })
    _start_job(_run_repair, (), "vaf-api-repair", container_jobs.REPAIR, _repair_state)
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
    already = HTTPException(
        status_code=409,
        detail=("An update was already started from here. Watch /api/version; "
                "the server restarts when it finishes."),
    )
    if _update_started_at is not None:
        raise already
    # The update stops this server: a repair, a profile switch or a backup running now (the
    # scheduled one included) would be cut off halfway. The slot is CLAIMED, not only read:
    # a backup starting between this check and the stop would be cut off the same way. It is
    # kept after a successful spawn, since this process is about to be stopped.
    other = container_jobs.claim(container_jobs.UPDATE)
    if other is not None:
        raise HTTPException(status_code=409, detail=container_jobs.busy_message("update", other))
    try:
        blocker = _restart_blocker()
        if blocker:
            raise HTTPException(status_code=409, detail=blocker)

        # Claim the lane BEFORE spawning: two clicks, or two admins, would otherwise
        # both pass every check above and start two updaters on one checkout.
        with _update_lock:
            if _update_started_at is not None:
                raise already
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
    except BaseException:
        container_jobs.release(container_jobs.UPDATE)
        raise

    # started_at is SERVER clock: the updater stamps the result file with the
    # same clock, so the client can accept only results from THIS run without
    # comparing across machines.
    return {"started": True, "poll": "/api/version", "current": __version__,
            "started_at": _update_started_at, **spawned}


def _restart_blocker() -> Optional[str]:
    """Why an update started from here would not come back, or None.

    In desktop mode the updater stops the running VAF and starts it again the
    way it ran. It can do that for any VAF it can identify: one with a service
    pid record (`vaf start`, the tray dashboard), one that recorded itself
    (vaf/core/instance.py: the app shortcut, run_vaf.sh, a bare tray), or one
    found in the process table by the singleton port it holds. A VAF it cannot
    identify at all is refused: the stop step would quietly do nothing, the
    checkout would be swapped under a running server, and the start step would
    add a SECOND instance. Saying so beats handing someone a button that
    promises a restart and delivers two servers.
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
    try:
        from vaf.core.instance import find_running
        if find_running() is not None:
            return None
    except Exception:
        pass
    return ("The updater cannot tell which process this VAF is, so it could not stop "
            "and start it for you. Update it from a terminal with `vaf update`, or "
            "start VAF with `vaf tray` or `vaf start` and try again.")
