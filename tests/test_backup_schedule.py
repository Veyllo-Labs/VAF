# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Backups that run by themselves (vaf/core/backup_schedule.py) and how many stay
(backup.prune_backups, grandfather-father-son).

What has to hold: a slot missed while the machine was off runs at the next start, a backup
written by hand counts, a failure is retried after an hour and reaches the admin once per
failure, the retention keeps what it promises and never the restore's safety backups, and a
server is scheduled daily unless somebody chose otherwise. Every test fails when its guard is
taken out.
"""
import datetime as dt
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vaf.core import backup
from vaf.core import backup_schedule as bs

UTC = dt.timezone.utc


def _info(when: dt.datetime, folder: Path = Path("/b")) -> backup.BackupInfo:
    stamp = when.strftime("%Y%m%dT%H%M%SZ")
    return backup.BackupInfo(folder / f"vaf-backup-{stamp}.tar.age", 1, stamp)


def _daily(days: int, start: dt.datetime = dt.datetime(2026, 10, 19, 3, tzinfo=UTC)):
    """One backup a day for `days` days, newest first (as list_backups returns them)."""
    return [_info(start - dt.timedelta(days=n)) for n in range(days)]


# ── grandfather-father-son ───────────────────────────────────────────────────────

def test_gfs_keeps_the_newest_of_each_day_week_and_month():
    """MUTATION: keep the oldest of a bucket, or count buckets wrong - red."""
    backups = _daily(120)
    kept = backup.backups_to_keep(backups, daily=7, weekly=4, monthly=3)
    names = {p.name for p in kept}
    for n in range(7):                                   # the last seven days
        assert backups[n].path.name in names
    # Sunday 2026-10-18 closed week 42; its newest backup is that Sunday.
    assert _info(dt.datetime(2026, 10, 18, 3, tzinfo=UTC)).path.name in names
    # The last three months each keep their newest: Oct 19, Sep 30, Aug 31.
    for day in (dt.datetime(2026, 9, 30, 3, tzinfo=UTC), dt.datetime(2026, 8, 31, 3, tzinfo=UTC)):
        assert _info(day).path.name in names
    assert _info(dt.datetime(2026, 7, 31, 3, tzinfo=UTC)).path.name not in names
    assert len(kept) < len(backups)


def test_each_bucket_keeps_its_newest_backup_and_only_that():
    """Two backups a day: a day keeps its later one. MUTATION: keep a bucket's older backup,
    or every backup of a kept bucket - red."""
    day = dt.datetime(2026, 10, 19, tzinfo=UTC)
    backups = [_info(day + dt.timedelta(hours=15)), _info(day + dt.timedelta(hours=3)),
               _info(day - dt.timedelta(hours=9)), _info(day - dt.timedelta(hours=21))]
    kept = backup.backups_to_keep(backups, daily=2, weekly=0, monthly=0)
    assert set(kept) == {backups[0].path, backups[2].path}


def test_the_newest_backup_stays_whatever_the_numbers():
    """MUTATION: drop the always-keep of the newest - red."""
    backups = _daily(5)
    assert backup.backups_to_keep(backups, daily=0, weekly=0, monthly=0) == [backups[0].path]


def test_the_newest_dated_backup_stays_next_to_a_foreign_name():
    """A name without a time sorts first in the list; the guarantee is for the newest dated
    backup. MUTATION: keep the first of the list instead - red."""
    foreign = backup.BackupInfo(Path("/b/vaf-backup-mine.tar.age"), 1, "mine")
    backups = [foreign] + _daily(3)
    kept = backup.backups_to_keep(backups, daily=0, weekly=0, monthly=0)
    assert set(kept) == {foreign.path, backups[1].path}


@pytest.mark.parametrize("value, expected", [(-1, 7), ("x", 7), (0, 0), (3, 3)])
def test_a_retention_value_that_is_no_count_is_the_default(value, expected):
    """MUTATION: clamp a negative value to 0 (deleting the whole bucket) - red."""
    assert bs.keep_counts({"backup_keep_daily": value})["daily"] == expected


def test_a_name_without_a_time_is_never_judged():
    """A file not written by this code is not this code's to delete. MUTATION: drop it - red."""
    foreign = backup.BackupInfo(Path("/b/vaf-backup-mine.tar.age"), 1, "mine")
    backups = _daily(3) + [foreign]
    assert foreign.path in backup.backups_to_keep(backups, daily=1, weekly=0, monthly=0)


def _write(folder: Path, when: dt.datetime) -> Path:
    path = _info(when, folder).path
    path.write_bytes(b"age")
    path.with_name(path.name + ".sha256").write_text("x")
    return path


def test_prune_deletes_with_the_checksum_and_spares_the_safety_backups(tmp_path):
    """MUTATION: leave the .sha256 behind, delete on a dry run, or walk into pre-restore - red."""
    old = _write(tmp_path, dt.datetime(2026, 1, 1, 3, tzinfo=UTC))
    new = _write(tmp_path, dt.datetime(2026, 10, 19, 3, tzinfo=UTC))
    safety = tmp_path / "pre-restore"
    safety.mkdir()
    spared = _write(safety, dt.datetime(2025, 1, 1, 3, tzinfo=UTC))
    assert backup.prune_backups(tmp_path, daily=1, weekly=0, monthly=0, dry_run=True) == [old]
    assert old.exists()
    assert backup.prune_backups(tmp_path, daily=1, weekly=0, monthly=0) == [old]
    assert not old.exists() and not old.with_name(old.name + ".sha256").exists()
    assert new.exists() and spared.exists()


def test_prune_waits_for_a_backup_being_written(tmp_path, monkeypatch):
    """MUTATION: prune without the folder's lock - red."""
    from filelock import FileLock
    _write(tmp_path, dt.datetime(2026, 1, 1, 3, tzinfo=UTC))
    _write(tmp_path, dt.datetime(2026, 10, 19, 3, tzinfo=UTC))
    held = FileLock(str(tmp_path / ".backup.lock"))
    held.acquire()
    try:
        from vaf.core import secure_store
        monkeypatch.setattr(secure_store, "_get_filelock_cls",
                            lambda: lambda path, timeout=5: FileLock(path, timeout=0.2))
        with pytest.raises(backup.BackupError):
            backup.prune_backups(tmp_path, daily=1, weekly=0, monthly=0)
    finally:
        held.release()
    assert len(list(tmp_path.glob("vaf-backup-*.tar.age"))) == 2


# ── when ─────────────────────────────────────────────────────────────────────────

BERLIN = dt.timezone(dt.timedelta(hours=2))


@pytest.mark.parametrize("now, kind, expected", [
    (dt.datetime(2026, 10, 19, 4, 0, tzinfo=BERLIN), "daily", dt.datetime(2026, 10, 19, 3, 0, tzinfo=BERLIN)),
    (dt.datetime(2026, 10, 19, 2, 0, tzinfo=BERLIN), "daily", dt.datetime(2026, 10, 18, 3, 0, tzinfo=BERLIN)),
    (dt.datetime(2026, 10, 21, 9, 0, tzinfo=BERLIN), "weekly", dt.datetime(2026, 10, 18, 3, 0, tzinfo=BERLIN)),
    (dt.datetime(2026, 10, 18, 2, 0, tzinfo=BERLIN), "weekly", dt.datetime(2026, 10, 11, 3, 0, tzinfo=BERLIN)),
])
def test_the_most_recent_slot(now, kind, expected):
    """MUTATION: forget the day before, or the Sunday - red."""
    assert bs.last_slot(now, kind, 3, 0) == expected


def test_a_missed_slot_runs_at_the_next_start_and_a_backup_by_hand_counts():
    """MUTATION: ignore the backups on disk, or only run at the exact slot - red."""
    now = dt.datetime(2026, 10, 19, 15, 0, tzinfo=BERLIN)        # VAF was off at 03:00
    assert bs.due({}, now, "daily", 3, 0)
    by_hand = dt.datetime(2026, 10, 19, 9, 0, tzinfo=BERLIN)
    assert not bs.due({}, now, "daily", 3, 0, newest=by_hand)
    assert not bs.due({"last_success_at": "2026-10-19T03:01:00+02:00"}, now, "daily", 3, 0)


def test_a_failed_run_is_tried_again_after_an_hour_not_every_minute():
    """MUTATION: drop the retry wait - red."""
    now = dt.datetime(2026, 10, 19, 15, 0, tzinfo=BERLIN)
    tried = {"last_attempt_at": (now - dt.timedelta(minutes=10)).isoformat()}
    assert not bs.due(tried, now, "daily", 3, 0)
    tried = {"last_attempt_at": (now - dt.timedelta(minutes=61)).isoformat()}
    assert bs.due(tried, now, "daily", 3, 0)


@pytest.mark.parametrize("value, expected", [
    ("daily", "daily"), (" Weekly ", "weekly"), ("", None), ("off", None), ("hourly", None),
])
def test_only_daily_and_weekly_schedule_anything(value, expected):
    assert bs.schedule({"backup_schedule": value}) == expected


@pytest.mark.parametrize("value, expected", [("04:30", (4, 30)), ("25:00", (3, 0)), ("x", (3, 0))])
def test_the_time_of_day(value, expected):
    assert bs.slot_time({"backup_time": value}) == expected


# ── a run ────────────────────────────────────────────────────────────────────────

@pytest.fixture
def state_home(tmp_path, monkeypatch):
    monkeypatch.setattr(bs, "state_path", lambda: tmp_path / "backup_schedule.json")
    monkeypatch.setattr(bs, "keep_counts", lambda config=None: {"daily": 7, "weekly": 4,
                                                                 "monthly": 6})
    from vaf.core import log_helper
    monkeypatch.setattr(log_helper, "append_domain_log_always", lambda *a, **k: None)
    return tmp_path


class _Result:
    path = Path("/b/vaf-backup-20261019T010000Z.tar.age")
    size = 430


def test_a_run_writes_prunes_and_records(state_home):
    """MUTATION: skip the prune, or prune with other numbers - red."""
    pruned = []
    now = dt.datetime(2026, 10, 19, 3, 0, tzinfo=BERLIN)
    state = bs.run_once(now, create=lambda: _Result(),
                        prune=lambda **keep: pruned.append(keep) or [Path("/b/old")])
    assert pruned == [{"daily": 7, "weekly": 4, "monthly": 6}]
    assert state["last_success_at"] == now.isoformat() and state["last_error"] == ""
    assert bs.read_state()["last_pruned"] == ["/b/old"]


def test_a_failure_reaches_the_admin_once_per_failure(state_home):
    """Retried hourly, it must not message the admin every hour. MUTATION: notify on every
    failure, or never reset after a success - red."""
    told = []

    def fail():
        raise backup.BackupError("The memory database did not answer.")

    now = dt.datetime(2026, 10, 19, 3, 0, tzinfo=BERLIN)
    bs.run_once(now, create=fail, notify=told.append)
    bs.run_once(now + dt.timedelta(hours=1), create=fail, notify=told.append)
    assert told == ["The memory database did not answer."]
    bs.run_once(now + dt.timedelta(hours=2), create=lambda: _Result(), prune=lambda **k: [],
                notify=told.append)
    bs.run_once(now + dt.timedelta(days=1), create=fail, notify=told.append)
    assert len(told) == 2


def test_a_prune_that_fails_does_not_fail_the_backup(state_home):
    """MUTATION: let the prune error escape - red."""
    def prune(**keep):
        raise backup.BackupError("A backup is being written into this folder.")

    state = bs.run_once(dt.datetime(2026, 10, 19, 3, 0, tzinfo=BERLIN),
                        create=lambda: _Result(), prune=prune, notify=lambda e: None)
    assert state["last_error"] == "" and "being written" in state["last_prune_error"]


def test_nothing_runs_while_nothing_is_scheduled(state_home, monkeypatch):
    """MUTATION: run regardless of the schedule - red."""
    monkeypatch.setattr(bs, "schedule", lambda config=None: None)
    monkeypatch.setattr(bs, "run_once", lambda *a, **k: pytest.fail("ran"))
    assert bs.tick(dt.datetime(2026, 10, 19, 15, 0, tzinfo=BERLIN)) is None


# ── the health row ───────────────────────────────────────────────────────────────

NOW = dt.datetime(2026, 10, 19, 15, 0, tzinfo=UTC)


@pytest.mark.parametrize("config, state, newest, status, says", [
    ({"backup_schedule": ""}, {}, None, "ok", "No backups are scheduled"),
    ({"backup_schedule": "daily"}, {}, NOW - dt.timedelta(hours=12), "ok", "12 hours"),
    ({"backup_schedule": "daily"}, {}, NOW - dt.timedelta(days=3), "degraded", "3 days old"),
    ({"backup_schedule": "weekly"}, {}, NOW - dt.timedelta(days=6), "ok", "weekly"),
    ({"backup_schedule": "daily"}, {}, None, "degraded", "none yet"),
    ({"backup_schedule": "daily"},
     {"last_success_at": "2026-10-18T03:00:00+00:00", "last_attempt_at": "2026-10-19T03:00:00+00:00",
      "last_error": "disk full"}, NOW - dt.timedelta(days=1, hours=12), "degraded", "disk full"),
    ({"backup_schedule": "daily"},
     {"last_success_at": "2026-10-18T03:00:00+00:00", "last_attempt_at": "2026-10-19T03:00:00+00:00",
      "last_error": "disk full"}, NOW - dt.timedelta(hours=6), "ok", "6 hours"),
])
def test_the_backup_row_of_the_health_check(config, state, newest, status, says):
    """Never down, degraded when a run failed or the newest backup is too old; a backup by hand
    after the failed run clears it. MUTATION: ignore the failed run, the age, or the backup by
    hand - red."""
    row = bs.backup_check(config, current=NOW, state=state, newest=newest)
    assert row["status"] == status and says in row["reason"]


# ── who turns it on ──────────────────────────────────────────────────────────────

def _run_provision(monkeypatch, current):
    from vaf.cli.cmd import server as server_cmd
    from vaf.core import db_credentials
    from vaf.core.config import Config
    from vaf.network import binding, firewall, ssl_utils
    written = []
    real_get = Config.get
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: current if key == "backup_schedule" else real_get(key, default)))
    monkeypatch.setattr(Config, "set", lambda key, value: written.append((key, value)))
    monkeypatch.setattr(ssl_utils, "ensure_ssl_certificates", lambda: (None, None))
    monkeypatch.setattr(binding, "resolve_lan_access_ports", lambda wait_for_proxy=False: (8443, 8001))
    monkeypatch.setattr(binding, "lan_ip_is_dhcp", lambda: None)
    monkeypatch.setattr(binding, "access_addresses", lambda: [])
    monkeypatch.setattr(firewall, "setup_firewall", lambda a, f: "present")
    monkeypatch.setattr(server_cmd.platform, "system", lambda: "Linux")
    monkeypatch.setattr(db_credentials, "shipped_password_in_use", lambda: False)
    out = CliRunner().invoke(server_cmd.app, ["provision", "--no-firewall"])
    assert out.exit_code == 0, out.output
    return [v for k, v in written if k == "backup_schedule"]


@pytest.mark.parametrize("current, expected", [("", ["daily"]), ("off", []), ("weekly", [])])
def test_provision_schedules_backups_only_where_nobody_chose(monkeypatch, current, expected):
    """MUTATION: overwrite an "off", or leave a server without backups - red."""
    assert _run_provision(monkeypatch, current) == expected


# ── the terminal ─────────────────────────────────────────────────────────────────

def _backup_app(monkeypatch):
    from vaf.cli import gate
    from vaf.cli.cmd import backup as backup_cmd
    monkeypatch.setattr(gate, "require_admin_password", lambda *a, **k: True)
    return backup_cmd.app


def test_vaf_backup_schedule_sets_and_refuses(monkeypatch, state_home):
    """MUTATION: accept any word, or any time - red."""
    from vaf.core.config import Config
    written = {}
    real_get = Config.get
    monkeypatch.setattr(Config, "set", staticmethod(lambda k, v: written.__setitem__(k, v)))
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda k, d=None: written.get(k, real_get(k, d))))
    app = _backup_app(monkeypatch)
    out = CliRunner().invoke(app, ["schedule", "daily", "--at", "4:30"])
    assert out.exit_code == 0, out.output
    assert written == {"backup_schedule": "daily", "backup_time": "04:30"}
    written.clear()
    assert CliRunner().invoke(app, ["schedule", "hourly"]).exit_code == 2
    assert CliRunner().invoke(app, ["schedule", "weekly", "--at", "25:00"]).exit_code == 2
    assert written == {}, "a refused --at left the schedule written"


def test_the_backup_schedule_thread_starts_once(monkeypatch):
    """The web server's startup runs once per uvicorn server (8001 and 8005). MUTATION: drop
    the once-guard - red."""
    started = []
    monkeypatch.setattr(bs, "_started", False)
    monkeypatch.setattr(bs.threading, "Thread", lambda **kw: type(
        "T", (), {"start": lambda self: started.append(kw["name"])})())
    assert bs.ensure_started("a") is True
    assert bs.ensure_started("b") is False
    assert started == ["vaf-backup-schedule"]
