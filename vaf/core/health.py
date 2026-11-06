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
  process that runs them;
- ``engine``: the managed vLLM (vaf/core/local_engine.py), only when it is the local engine:
  degraded when this machine cannot run it or it is not installed, ok otherwise, also when
  its server is idle (the desktop unloads the local model when nobody uses it);
- ``queues``: the listed fair queues of this process (vaf/core/fair_queue.py: the speech
  lanes, the document preview's ``gotenberg`` and the shared browser's
  ``browser:vaf-browser``; a per-user browser's queue is unlisted),
  the chat-turn queue (vaf/core/task_queue.py, in the process that runs the agent loops) and,
  with vLLM as the running local engine, how many requests wait and run in its own scheduler
  (its ``/metrics``); only when one of them is there: degraded when a queue turned somebody
  away (``timeout`` or ``full``) in the last 15 minutes, never down, and neither the chat
  queue nor vLLM ever turns anybody away. The check carries the numbers as ``metrics``
  (length, wait and service time, refusals; counts, never who);
- ``model``: the model-health score of the chat turns (vaf/core/model_health.py), only in
  the process that runs the agent loops and only when a turn was scored in the last 60
  minutes: how many, how many re-runs (forced or not) and how many stuck notes. Always ok: a
  stuck model is a condition of one chat, which that chat already tells its person honestly,
  and paging a monitor for what a new chat fixes would be noise. Counts, never who.

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


def engine_check() -> Optional[Dict[str, str]]:
    """The vLLM engine; None (no row) while llama.cpp is the local engine. The sentence is
    the one Update and Repair shows (``local_engine.status_row``), read from the same place."""
    from vaf.core import local_engine
    if not local_engine.is_vllm():
        return None
    row = local_engine._vllm_row(local_engine._cfg(), probe_health=False)
    return _check("engine", DEGRADED if row["state"] == "degraded" else OK, row["text"])


def _format_ms(value: Optional[int]) -> str:
    if value is None:
        return "-"
    return f"{value} ms" if value < 1000 else f"{value / 1000:.1f} s"


def queues_check(metrics: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The fair queues of this process; None (no row) where this process has none. A
    hang-up (``cancelled``) is somebody leaving, not somebody turned away, so it does not
    degrade."""
    if not metrics:
        return None
    lines, turned_away = [], 0
    for m in metrics:
        if m.get("kind") == "engine":
            # The engine's own scheduler queue: counts only, it never refuses anybody.
            mean = m.get("queue_time_mean_ms")
            lines.append(f"{m.get('name')}: {m.get('waiting', 0)} waiting, "
                         f"{m.get('running', 0)} running, queue time mean "
                         f"{_format_ms(mean)} since start")
            continue
        refused = m.get("refused") or {}
        turned_away += int(refused.get("timeout") or 0) + int(refused.get("full") or 0)
        total_refused = sum(int(v or 0) for v in refused.values())
        if not (m.get("served") or total_refused or m.get("waiting") or m.get("running")):
            continue
        wait, service = m.get("wait_ms") or {}, m.get("service_ms") or {}
        lines.append(f"{m.get('name')}: {m.get('waiting', 0)} waiting, {m.get('served', 0)} "
                     f"served, wait p50 {_format_ms(wait.get('p50'))} / p95 "
                     f"{_format_ms(wait.get('p95'))}, service p50 "
                     f"{_format_ms(service.get('p50'))}, {total_refused} refused")
    reason = "; ".join(lines) if lines else "Nothing queued in the last 15 minutes."
    if turned_away:
        # The degraded row says why first: people were refused (no slot in time, or full).
        reason = (f"{turned_away} turned away in the last 15 minutes (no slot in time, or "
                  f"the queue or a person's share of it full). {reason}")
    check: Dict[str, Any] = _check("queues", DEGRADED if turned_away else OK, reason)
    check["metrics"] = metrics
    return check


def model_check(metrics: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The model-health counts of this process (`model_health.metrics`); None (no row) when
    no turn was scored in the window. Status always ok, see the module docstring."""
    if not metrics or not int(metrics.get("scored") or 0):
        return None
    minutes = max(1, int(metrics.get("window_s") or 3600) // 60)
    scored = int(metrics.get("scored") or 0)
    retries, forced = int(metrics.get("retries") or 0), int(metrics.get("forced") or 0)
    notes = int(metrics.get("notes") or 0)
    reason = (f"In the last {minutes} minutes: {scored} turn{'s' if scored != 1 else ''} scored, "
              f"{retries} health retr{'ies' if retries != 1 else 'y'} ({forced} forced), "
              f"{notes} stuck note{'s' if notes != 1 else ''}.")
    check: Dict[str, Any] = _check("model", OK, reason)
    check["metrics"] = {k: int(metrics.get(k) or 0) for k in
                        ("window_s", "scored", "mid", "high", "retries", "forced", "notes")}
    return check


def _model_metrics() -> Optional[Dict[str, Any]]:
    """The model-health counts, only in the process whose agent loops score the turns (the
    agent row's rule): elsewhere nothing is ever scored."""
    import sys
    scorer = sys.modules.get("vaf.core.model_health")
    if scorer is None or _worker_status() is None:
        return None
    return scorer.metrics()


def _chat_queue_metrics() -> List[Dict[str, Any]]:
    """The chat-turn queue (``TaskQueue.metrics``), only in the process whose agent loops
    take the turns (the agent row's rule): elsewhere the queue is empty by construction."""
    import sys
    tasks = sys.modules.get("vaf.core.task_queue")
    runner = sys.modules.get("vaf.core.headless_runner")
    if tasks is None or runner is None or tasks.TaskQueue._instance is None:
        return []
    if runner.worker_status() is None:
        return []
    return [tasks.TaskQueue().metrics()]


def _engine_queue_metrics() -> List[Dict[str, Any]]:
    """The managed vLLM's own queue, read from its ``/metrics``, only when vLLM is the local
    engine, the profile allows local inference and its server process is there: no HTTP
    request otherwise. llama-server runs without ``--metrics``, so it has no such entry."""
    from vaf.core import inference_profile, local_engine, vllm_server
    if not local_engine.is_vllm() or not inference_profile.local_inference_allowed():
        return []
    if vllm_server.running_pid() is None:
        return []
    counts = vllm_server.request_counts()
    if counts is None:
        return []
    return [{"name": "vllm", "kind": "engine", **counts}]


def _queue_metrics() -> List[Dict[str, Any]]:
    """The fair queues of this process, then the chat queue, then the engine's queue."""
    import sys
    queues = sys.modules.get("vaf.core.fair_queue")
    out = list(queues.queue_metrics()) if queues is not None else []
    for probe in (_chat_queue_metrics, _engine_queue_metrics):
        try:
            out.extend(probe())
        except Exception:
            continue
    return out


def _worker_status() -> Optional[Dict[str, int]]:
    import sys
    runner = sys.modules.get("vaf.core.headless_runner")
    return runner.worker_status() if runner is not None else None


APP_LINK_GRACE_SECONDS = 300


def app_link_check(status: Optional[Dict[str, Any]] = None, *,
                   now: Optional[float] = None) -> Optional[Dict[str, str]]:
    """The Veyllo App link: no row while there is no device and no relay instance; degraded
    when the relay was refused (revoked, secret rejected, replaced by another VAF with the
    same secret) or should be connected and has not been for five minutes."""
    import time as _time

    from vaf.core.app_link import devices, settings
    rows = devices.all_devices()
    cfg = settings()
    if not rows and not cfg.get("relay_instance_id"):
        return None
    n = len(rows)
    count = f"{n} device{'s' if n != 1 else ''}"
    if not cfg.get("relay_enabled", True):
        return _check("app_link", OK, f"Relay off, direct path only, {count}.")
    if status is None:
        from vaf.core.app_link import relay
        status = relay.status()
    state = status.get("state")
    refusals = {
        "revoked": "The relay refused this installation (revoked): pair again or dissolve the "
                   "relay connection (`vaf relay dissolve`).",
        "secret_rejected": "The relay does not accept this installation's relay secret: rotate "
                           "it (`vaf relay rotate`), or dissolve and pair again.",
        "replaced": "Another VAF with this relay secret took the relay connection over (a "
                    "backup restored elsewhere?): rotate the secret on one of them.",
    }
    if state in refusals:
        return _check("app_link", DEGRADED, refusals[state])
    now = _time.time() if now is None else now
    down = status.get("down_since")
    if down and now - float(down) > APP_LINK_GRACE_SECONDS:
        minutes = int((now - float(down)) // 60)
        error = status.get("last_error") or state or "not connected"
        return _check("app_link", DEGRADED, f"The relay should be connected and has not been for "
                                            f"{minutes} minutes ({error}), {count}.")
    if state == "connected":
        return _check("app_link", OK, f"Relay connected, {count}.")
    return _check("app_link", OK, f"Relay idle until a device needs it, {count}.")


def _backup_check() -> Dict[str, str]:
    from vaf.core.backup_schedule import backup_check
    return backup_check()


def collect_health(status_probe: Optional[Callable[[], Dict[str, Any]]] = None,
                   worker_probe: Callable[[], Optional[Dict[str, int]]] = _worker_status,
                   usage: Callable[[str], Any] = shutil.disk_usage,
                   backup_probe: Callable[[], Dict[str, str]] = _backup_check,
                   engine_probe: Callable[[], Optional[Dict[str, str]]] = engine_check,
                   queue_probe: Callable[[], List[Dict[str, Any]]] = _queue_metrics,
                   model_probe: Callable[[], Optional[Dict[str, Any]]] = _model_metrics,
                   app_link_probe: Callable[[], Optional[Dict[str, str]]] = app_link_check,
                   ) -> Dict[str, Any]:
    """Every check and the verdict: {status, checks, checked_at}. Never raises: a check that
    cannot run says so as its own result."""
    checks: List[Dict[str, Any]] = []
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
    try:
        engine = engine_probe()
        if engine is not None:
            checks.append(engine)
    except Exception as e:
        checks.append(_check("engine", DEGRADED, f"The local engine could not be read: {e}"))
    try:
        queues = queues_check(queue_probe())
        if queues is not None:
            checks.append(queues)
    except Exception as e:
        checks.append(_check("queues", DEGRADED, f"The queues could not be read: {e}"))
    try:
        model = model_check(model_probe())
        if model is not None:
            checks.append(model)
    except Exception as e:
        checks.append(_check("model", OK, f"The model-health counts could not be read: {e}"))
    try:
        app_link = app_link_probe()
        if app_link is not None:
            checks.append(app_link)
    except Exception as e:
        checks.append(_check("app_link", DEGRADED, f"The Veyllo App link could not be read: {e}"))
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
