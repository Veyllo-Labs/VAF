# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Is this installation healthy? One answer for a monitor, the terminal and the admin.

`vaf repair --check` and the Update and Repair dialog answer for the containers
(vaf/core/service_health.py). A server needs that answered from outside too, by a monitor
that polls an address, and two failures leave every container healthy while VAF stops
working, with no check for either before this:

- the disk fills up: the database stops writing, a backup cannot be written;
- the agent loop of the server dies: the server keeps answering every request, the page
  loads, and no chat gets an answer again.

Checks, each ``{name, status, reason}``:

- ``services``: `service_health.stack_problems` over one status snapshot, the same reading
  `vaf repair --check` exits on;
- ``disk``: free space where VAF keeps its data (`data_stores.roots`), its backups, and
  where the container engine keeps the database's volume (Docker's root directory, when it
  is a path on this machine);
- ``backup``: whether the scheduled backups run (`backup_schedule.backup_check`): degraded
  when the last scheduled run failed or the newest backup is older than the schedule allows,
  never down (a missing backup stops nothing);
- ``agent``: the agent loops of this process (`headless_runner.worker_status`), only in the
  process that runs them.

The whole is the worst of them: ``ok``; ``degraded`` (VAF works, a part of it does not);
``down`` (chats or data are not safe: a required service is unusable, the disk is nearly
full, no agent loop is left).

Who asks: ``GET /api/health`` (no sign-in, the status word only, HTTP 503 when down, served
from a snapshot at most SNAPSHOT_SECONDS old so a poller cannot make it run Docker on every
request, and waited for on the event loop so a crowd of pollers holds no worker thread),
``GET /api/system/health`` (an admin: every check) and ``vaf health`` (exit 0, 1 or 2, the
convention of monitoring plugins).

Not on the facade: an embedder runs its own process and watches it its own way; this is a
rule about what an installation of the product needs.
"""
from __future__ import annotations

import concurrent.futures
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

OK = "ok"
DEGRADED = "degraded"
DOWN = "down"
_RANK = {OK: 0, DEGRADED: 1, DOWN: 2}

# Free space below which the disk check warns, and below which it calls the installation
# down. A backup of a used desktop measured 430 MB encrypted, staged once before it is
# written; the database's write-ahead log and an image pull need room on top. Below 1 GB
# the next backup or a busy day of the database fills the disk.
DISK_LOW_BYTES = 5 * 1024 ** 3
DISK_CRITICAL_BYTES = 1 * 1024 ** 3

# How old the snapshot behind the unauthenticated endpoint may be. One snapshot runs
# `docker info` and `docker inspect` (measured 0.15 s with the containers stopped) plus one
# probe per running service (up to service_health.PROBE_TIMEOUT each); a monitor polls about
# once a minute, so it still sees every change within one poll.
SNAPSHOT_SECONDS = 15.0


def worst(statuses: Iterable[str]) -> str:
    """The worst of the statuses (an empty set is ok)."""
    result = OK
    for status in statuses:
        if _RANK.get(status, 2) > _RANK[result]:
            result = status if status in _RANK else DOWN
    return result


def _check(name: str, status: str, reason: str) -> Dict[str, str]:
    return {"name": name, "status": status, "reason": reason}


def services_check(status: Dict[str, Any]) -> Dict[str, str]:
    """The containers, read the way `vaf repair --check` reads them."""
    from vaf.core.service_health import stack_problems
    problems = stack_problems(status)
    if not problems:
        if not status.get("stack_root"):
            return _check("services", OK, "No service stack on this installation.")
        return _check("services", OK, "Every service this installation needs answers.")
    return _check("services", worst(p["severity"] for p in problems),
                  " ".join(f"{p['name']}: {p['reason']}" for p in problems))


def _existing(path: Path) -> Optional[Path]:
    """The path, or its nearest ancestor that exists (a backup folder not created yet)."""
    for candidate in (path, *path.parents):
        try:
            if candidate.exists():
                return candidate
        except OSError:
            continue
    return None


def disk_paths(status: Optional[Dict[str, Any]] = None) -> List[Path]:
    """Where VAF writes: its data roots, its backups, Docker's root when it is local."""
    paths: List[Path] = []
    try:
        from vaf.core import data_stores
        paths.extend(Path(p) for p in data_stores.roots().values())
    except Exception:
        pass
    try:
        from vaf.core.backup import backup_dir_path
        paths.append(backup_dir_path())
    except Exception:
        pass
    root_dir = str(((status or {}).get("docker") or {}).get("root_dir") or "").strip()
    # Only where it exists here: under Docker Desktop and Colima the engine's root is a path
    # inside its VM, and the nearest ancestor on the host would measure an unrelated disk.
    if root_dir and Path(root_dir).exists():
        paths.append(Path(root_dir))
    return paths


def disk_check(paths: Iterable[Path],
               usage: Callable[[str], Any] = shutil.disk_usage) -> Dict[str, str]:
    """The fullest disk among the paths, one disk counted once."""
    seen = set()
    tightest = None
    for path in paths:
        existing = _existing(Path(path))
        if existing is None:
            continue
        try:
            device = os.stat(existing).st_dev
        except OSError:
            continue
        if device in seen:
            continue
        seen.add(device)
        try:
            free = int(usage(str(existing)).free)
        except Exception:
            continue
        if tightest is None or free < tightest[0]:
            tightest = (free, existing)
    if tightest is None:
        return _check("disk", OK, "No disk to check.")
    free, where = tightest
    gb = free / 1024 ** 3
    if free < DISK_CRITICAL_BYTES:
        return _check("disk", DOWN, f"Only {gb:.1f} GB free at {where}: the database and the "
                                    "backups fail when it is full.")
    if free < DISK_LOW_BYTES:
        return _check("disk", DEGRADED, f"Only {gb:.1f} GB free at {where}.")
    return _check("disk", OK, f"{gb:.1f} GB free on the fullest disk ({where}).")


def agent_check(workers: Optional[Dict[str, int]]) -> Optional[Dict[str, str]]:
    """The agent loops of this process; None where this process runs none."""
    if workers is None:
        return None
    started, alive = int(workers.get("started") or 0), int(workers.get("alive") or 0)
    if alive == 0:
        return _check("agent", DOWN, "The agent loop has stopped: chats get no answer. "
                                     "Restart VAF (`vaf restart`).")
    if alive < started:
        return _check("agent", DEGRADED, f"{started - alive} of {started} agent loops have "
                                         "stopped; chats wait longer.")
    return _check("agent", OK, f"{alive} agent loop{'s' if alive != 1 else ''} running.")


def _worker_status() -> Optional[Dict[str, int]]:
    import sys
    runner = sys.modules.get("vaf.core.headless_runner")
    return runner.worker_status() if runner is not None else None


def _backup_check() -> Dict[str, str]:
    from vaf.core.backup_schedule import backup_check
    return backup_check()


def collect_health(status_probe: Optional[Callable[[], Dict[str, Any]]] = None,
                   worker_probe: Callable[[], Optional[Dict[str, int]]] = _worker_status,
                   usage: Callable[[str], Any] = shutil.disk_usage,
                   backup_probe: Callable[[], Dict[str, str]] = _backup_check) -> Dict[str, Any]:
    """Every check and the verdict: {status, checks, checked_at}. Never raises: a check that
    cannot run says so as its own result."""
    checks: List[Dict[str, str]] = []
    status: Dict[str, Any] = {}
    try:
        if status_probe is None:
            from vaf.core.service_health import collect_service_status
            status_probe = collect_service_status
        status = status_probe() or {}
        checks.append(services_check(status))
    except Exception as e:
        checks.append(_check("services", DOWN, f"The service status could not be read: {e}"))
    try:
        checks.append(disk_check(disk_paths(status), usage=usage))
    except Exception as e:
        checks.append(_check("disk", DEGRADED, f"The free space could not be read: {e}"))
    try:
        checks.append(backup_probe())
    except Exception as e:
        checks.append(_check("backup", DEGRADED, f"The backup state could not be read: {e}"))
    try:
        agent = agent_check(worker_probe())
        if agent is not None:
            checks.append(agent)
    except Exception as e:
        checks.append(_check("agent", DEGRADED, f"The agent loops could not be read: {e}"))
    return {"status": worst(c["status"] for c in checks), "checks": checks,
            "checked_at": datetime.now(timezone.utc).isoformat()}


_snapshot: Dict[str, Any] = {}
_snapshot_at = 0.0
_inflight: Optional[concurrent.futures.Future] = None
# Held for bookkeeping only, never while a collection runs.
_snapshot_lock = threading.Lock()


def _refresh(collect: Callable[[], Dict[str, Any]]) -> concurrent.futures.Future:
    """The collection under way, or a new one in its own thread. Caller holds the lock."""
    global _inflight
    if _inflight is not None and not _inflight.done():
        return _inflight
    future: concurrent.futures.Future = concurrent.futures.Future()
    # Running before anyone can wait on it: a running future cannot be cancelled, so a monitor
    # that hangs up (its request task cancelled, which wrap_future passes on to this future)
    # cannot cancel the collection every other caller is waiting for.
    future.set_running_or_notify_cancel()

    def run() -> None:
        global _snapshot, _snapshot_at
        try:
            result = collect()
        except BaseException as e:      # collect_health never raises; a substitute might
            future.set_exception(e)
            return
        with _snapshot_lock:
            _snapshot, _snapshot_at = result, time.monotonic()
        future.set_result(result)

    threading.Thread(target=run, daemon=True, name="vaf-health").start()
    _inflight = future
    return future


async def snapshot(max_age: float = SNAPSHOT_SECONDS,
                   collect: Callable[[], Dict[str, Any]] = collect_health) -> Dict[str, Any]:
    """A health result at most `max_age` seconds old, for the endpoint that needs no account.

    One collection runs at a time, in its own thread, and every caller that finds the result
    expired waits for that one - on the event loop, not in a worker thread. A check can take
    seconds (a Docker that does not answer: 10 s for `docker info`, 10 s for the inspect), and
    callers blocked in the thread pool during it would hold the workers every other route
    runs in. An expired result is never handed out: the guarantee is the point of the word."""
    import asyncio
    with _snapshot_lock:
        if _snapshot and time.monotonic() - _snapshot_at < max_age:
            return _snapshot
        future = _refresh(collect)
    return await asyncio.wrap_future(future)


def http_status(health: Dict[str, Any]) -> int:
    """503 when down, 200 otherwise: a monitor that only reads the code alarms on down, and a
    degraded installation still serves its people."""
    return 503 if health.get("status") == DOWN else 200
