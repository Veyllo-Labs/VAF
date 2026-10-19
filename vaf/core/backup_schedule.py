# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Backups that run by themselves: when, how many stay, and who hears about a failure.

`vaf backup create` writes one backup when a person runs it (vaf/core/backup.py). A server
nobody logs into needs them to happen anyway, and to stop filling its disk. This module is
that half, built on the other one: the same `create_backup`, the same folder, the same
verification after every run.

- **When.** `backup_schedule` is `daily` or `weekly` (on Sunday); empty or `off` runs nothing.
  `backup_time` is the time of day in the admin's own timezone (vaf/core/user_time.py), the
  way an automation's time is. A run is due when no backup is newer than the most recent
  slot, so a machine that was off at that time backs up soon after it starts again instead
  of a day later, and a backup written by hand after the slot counts. A run that failed is
  tried again after RETRY_SECONDS, not every minute.
- **How many stay.** After every successful run the folder is pruned grandfather-father-son
  (`backup.prune_backups`): the newest backup of each of the last `backup_keep_daily` days,
  `backup_keep_weekly` weeks and `backup_keep_monthly` months, and the newest one always.
- **Who hears.** A failure reaches the admin once per failure, not once per retry: in the
  web UI's notifications and on the admin's main messenger (`send_to_main_messenger`, the
  lane the thinking runs and the automations use). The health check names a failed run and
  a backup older than its schedule allows (`backup_check`, read by vaf/core/health.py).

Not an automation (vaf/core/automation.py), deliberately: an automation is a prompt the agent
runs, and a backup must neither depend on a model answering nor cost a token. It runs in the
server process (the web server's startup starts it, once per process); a terminal never does.

Not on the facade: an embedder decides how its own data is kept; this is the product's.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger("vaf.core.backup_schedule")

SCHEDULES = ("daily", "weekly")
INTERVALS = {"daily": timedelta(days=1), "weekly": timedelta(days=7)}
WEEKDAY = 6                      # weekly runs on Sunday (Monday is 0)
DEFAULT_TIME = (3, 0)
RETRY_SECONDS = 3600             # a failed run is tried again after an hour, not every minute
CHECK_SECONDS = 60
SETTLE_SECONDS = 120             # the stack and the database come up first
STATE_NAME = "backup_schedule.json"
# How old the newest backup may grow before the health check says so: one interval and a day.
GRACE = timedelta(days=1)


def schedule(config: Optional[Dict] = None) -> Optional[str]:
    """`daily` or `weekly`, or None when nothing is scheduled (empty, `off`, anything else)."""
    if config is None:
        from vaf.core.config import Config
        value = Config.get("backup_schedule", "")
    else:
        value = config.get("backup_schedule", "")
    value = str(value or "").strip().lower()
    return value if value in SCHEDULES else None


def slot_time(config: Optional[Dict] = None) -> Tuple[int, int]:
    """`backup_time` as (hour, minute); a value that is not HH:MM means 03:00."""
    if config is None:
        from vaf.core.config import Config
        value = Config.get("backup_time", "03:00")
    else:
        value = config.get("backup_time", "03:00")
    try:
        hour, minute = (int(part) for part in str(value).strip().split(":"))
        if 0 <= hour < 24 and 0 <= minute < 60:
            return hour, minute
    except (TypeError, ValueError):
        pass
    return DEFAULT_TIME


def keep_counts(config: Optional[Dict] = None) -> Dict[str, int]:
    """The three retention numbers; a value that is not a whole number >= 0 is its default."""
    if config is None:
        from vaf.core.config import Config
        config = Config.load()
    out = {}
    for key, default in (("daily", 7), ("weekly", 4), ("monthly", 6)):
        try:
            value = int(config.get(f"backup_keep_{key}", default))
        except (TypeError, ValueError):
            value = -1
        # A negative number is no count (someone may have meant "unlimited"): the default,
        # never 0, which would delete that whole bucket.
        out[key] = value if value >= 0 else default
    return out


def now() -> datetime:
    """The admin's time, timezone-aware (their own zone, else this machine's)."""
    from vaf.core.config import get_local_admin_username
    from vaf.core.user_time import user_now
    current = user_now(get_local_admin_username())
    return current if current.tzinfo else current.astimezone()


def last_slot(current: datetime, kind: str, hour: int, minute: int) -> datetime:
    """The most recent scheduled time at or before ``current`` (aware, in its zone)."""
    slot = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if slot > current:
        slot -= timedelta(days=1)
    if kind == "weekly":
        slot -= timedelta(days=(slot.weekday() - WEEKDAY) % 7)
    return slot


def state_path() -> Path:
    from vaf.core.platform import Platform
    return Platform.vaf_dir() / STATE_NAME


def read_state() -> Dict[str, Any]:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(state: Dict[str, Any]) -> None:
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        logger.warning("backup schedule state not written: %s", e)


def _parse(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def newest_backup() -> Optional[datetime]:
    """When the newest backup in the backup folder was written, by hand or on schedule."""
    from vaf.core import backup
    folder = backup.backup_dir_path()
    if not folder.is_dir():
        return None
    for info in backup.list_backups(folder):
        when = backup.backup_time(info)
        if when is not None:
            return when
    return None


def due(state: Dict[str, Any], current: datetime, kind: str, hour: int, minute: int,
        newest: Optional[datetime] = None) -> bool:
    """Whether a run is due: no backup since the most recent slot, and no failed attempt
    since that slot within the last RETRY_SECONDS."""
    slot = last_slot(current, kind, hour, minute)
    for when in (newest, _parse(state.get("last_success_at"))):
        if when is not None and when >= slot:
            return False
    tried = _parse(state.get("last_attempt_at"))
    if tried is not None and tried >= slot and (current - tried).total_seconds() < RETRY_SECONDS:
        return False
    return True


def _notify_failure(error: str) -> None:
    """The admin hears it in the web UI and on their main messenger. Never raises."""
    text = (f"The scheduled backup failed: {error} `vaf backup create` runs it by hand and "
            "names the cause; it is tried again in an hour.")
    from vaf.core.config import get_local_admin_scope_id, get_local_admin_username
    scope = str(get_local_admin_scope_id())
    try:
        from vaf.core.user_notifications import append_notification
        append_notification(user_scope_id=scope, kind="system", title="Backup failed",
                            status="error", summary=text)
    except Exception as e:
        logger.warning("backup failure not added to the notifications: %s", e)
    try:
        from vaf.core.messaging_connections import send_to_main_messenger
        send_to_main_messenger(scope, get_local_admin_username(), text)
    except Exception as e:
        logger.warning("backup failure not sent to the main messenger: %s", e)


def run_once(current: Optional[datetime] = None, *,
             create: Optional[Callable[..., Any]] = None,
             prune: Optional[Callable[..., Any]] = None,
             notify: Callable[[str], None] = _notify_failure) -> Dict[str, Any]:
    """One scheduled run: write and verify a backup, prune the folder, record the outcome."""
    from vaf.core import backup
    from vaf.core.log_helper import append_domain_log_always
    create = create or backup.create_backup
    prune = prune or backup.prune_backups
    current = current or now()
    state = read_state()
    previous_attempt = state.get("last_attempt_at")
    # Saved before the backup starts: a run that takes the process down with it (out of
    # memory) still counts as an attempt, so the restart retries after RETRY_SECONDS instead
    # of running into the same backup again at every start.
    state["last_attempt_at"] = current.isoformat()
    write_state(state)
    try:
        result = create()
    except backup.BackupBusy:
        # Somebody is writing a backup into this folder right now (`vaf backup create`, the
        # web's "Back up now"). Not a failure, and nothing to tell anyone: the next tick sees
        # that backup and is not due, or runs again if it failed - so this was no attempt.
        if previous_attempt is None:
            state.pop("last_attempt_at", None)
        else:
            state["last_attempt_at"] = previous_attempt
        write_state(state)
        append_domain_log_always("backup", "scheduled backup skipped: another one is running")
        return state
    except Exception as e:
        error = str(e) or type(e).__name__
        state["last_error"] = error[:500]
        append_domain_log_always("backup", f"scheduled backup FAILED: {error}")
        if not state.get("failure_notified"):
            notify(error)
            state["failure_notified"] = True
        write_state(state)
        return state
    state.update({"last_success_at": current.isoformat(), "last_error": "",
                  "failure_notified": False, "last_file": str(result.path),
                  "last_size": int(result.size)})
    append_domain_log_always("backup", f"scheduled backup written: {result.path} "
                                       f"({result.size} bytes, verified)")
    try:
        removed = prune(**keep_counts())
        state["last_pruned"] = [str(p) for p in removed]
        state["last_prune_error"] = ""
        if removed:
            append_domain_log_always("backup", f"pruned {len(removed)} old backup(s)")
    except Exception as e:
        # The backup stands; a prune that did not run leaves files, which the disk check sees.
        state["last_prune_error"] = str(e)[:500]
        append_domain_log_always("backup", f"prune FAILED: {e}")
    write_state(state)
    return state


def tick(current: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """Run a backup when one is due; None when nothing is scheduled or due."""
    kind = schedule()
    if kind is None:
        return None
    current = current or now()
    hour, minute = slot_time()
    if not due(read_state(), current, kind, hour, minute, newest_backup()):
        return None
    return run_once(current)


def backup_check(config: Optional[Dict] = None, current: Optional[datetime] = None,
                 state: Optional[Dict[str, Any]] = None,
                 newest: Optional[datetime] = None) -> Dict[str, str]:
    """The health check's `backup` row (vaf/core/health.py): never down - a missing backup
    stops nothing - but degraded when the last scheduled run failed, or when the newest backup
    is older than the schedule allows."""
    kind = schedule(config)
    if kind is None:
        return {"name": "backup", "status": "ok",
                "reason": "No backups are scheduled (`vaf backup schedule daily`)."}
    current = current or now()
    state = read_state() if state is None else state
    newest = newest if newest is not None else newest_backup()
    last_ok = _parse(state.get("last_success_at"))
    if last_ok is not None and (newest is None or last_ok > newest):
        newest = last_ok
    tried = _parse(state.get("last_attempt_at"))
    error = str(state.get("last_error") or "")
    # Against the newest backup of either kind: one written by hand after the failure is a
    # verified backup, and the schedule counts it too (`due`).
    if error and tried is not None and (newest is None or tried > newest):
        return {"name": "backup", "status": "degraded",
                "reason": f"The last scheduled backup failed: {error}"}
    if newest is None:
        return {"name": "backup", "status": "degraded",
                "reason": f"Backups are scheduled {kind}, and there is none yet."}
    age = current - newest
    if age > INTERVALS[kind] + GRACE:
        return {"name": "backup", "status": "degraded",
                "reason": f"The newest backup is {age.days} days old (scheduled {kind})."}
    hours = int(age.total_seconds() // 3600)
    return {"name": "backup", "status": "ok",
            "reason": f"Scheduled {kind}; the newest backup is {hours} hours old."}


_started = False
_start_lock = threading.Lock()


def _loop() -> None:
    time.sleep(SETTLE_SECONDS)
    while True:
        try:
            tick()
        except Exception as e:          # never let one bad tick end the schedule
            logger.warning("backup schedule tick failed: %s", e)
        time.sleep(CHECK_SECONDS)


def ensure_started(origin: str = "") -> bool:
    """Start the schedule's thread once per process. True when this call started it."""
    global _started
    with _start_lock:
        if _started:
            return False
        threading.Thread(target=_loop, daemon=True, name="vaf-backup-schedule").start()
        _started = True
        logger.info("backup schedule started (%s)", origin)
        return True
