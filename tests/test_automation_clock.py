# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The automation scheduler's clock is the task's own next run, in the owner's timezone.

MEASURED LIVE: every VAF start logged "Automation scheduler start error: No module named
'pytz'". The scheduler handed each task's time and the owner's timezone to a third-party
scheduler, and that package imports pytz for any timezone. pytz was never a dependency of
VAF; it had only been installed by something else. So once an owner had a timezone set,
registering the first task raised, the scheduler did not start, and no timed automation
ran. The suite never saw it: no test gave an owner a timezone.

The clock is now AutomationTask.calculate_next_run - the next run the lists and the calendar
already show, computed with the standard library's zoneinfo - asked again after every run.

MUTATION: restore the third-party clock and the first test goes red (pytz is blocked here,
as it is missing on a fresh install); restore the old monthly branch and the 31st test
goes red.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import vaf.core.user_time as user_time
from vaf.core.automation import AutomationManager, AutomationTask
from vaf.core.platform import Platform

TOKYO = ZoneInfo("Asia/Tokyo")


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path))
    # The owner has a timezone, the way a real account does after onboarding.
    monkeypatch.setattr(user_time, "_load_identity", lambda username, identity: {"timezone": "Asia/Tokyo"})
    # A fresh install has no pytz; block it even where something else installed it.
    monkeypatch.setitem(__import__("sys").modules, "pytz", None)
    mgr = AutomationManager(storage_dir=str(tmp_path / "automations"))
    mgr._log_scheduler_event = lambda msg: None
    ran = []
    mgr._run_scheduled_task = lambda task: ran.append(task.id)
    mgr.ran = ran
    return mgr


def _task(**kw):
    base = dict(id="t1", name="Morgenbericht", frequency="daily", time="08:00")
    return AutomationTask(**{**base, **kw})


def test_a_task_with_an_owner_timezone_is_armed_without_pytz(manager):
    task = _task()
    manager._schedule_task(task)
    job = manager._clock_jobs["t1"]
    assert job.due == task.calculate_next_run(), "the scheduler fires at the next run the lists show"
    assert job.due.astimezone(TOKYO).strftime("%H:%M") == "08:00", "08:00 in the owner's zone"


def test_a_due_task_runs_once_and_is_armed_for_its_next_run(manager):
    manager._schedule_task(_task())
    first = manager._clock_jobs["t1"].due
    manager._run_due_clock_jobs(now=first - timedelta(seconds=30))
    assert manager.ran == [], "not before its time"
    manager._run_due_clock_jobs(now=first + timedelta(seconds=10))
    manager._run_due_clock_jobs(now=first + timedelta(seconds=40))
    assert manager.ran == ["t1"], "once per due time, however many ticks"
    assert manager._clock_jobs["t1"].due > first


def test_a_one_time_task_ends_after_it_fired(manager):
    manager._schedule_task(_task(id="once", frequency="once"))
    due = manager._clock_jobs["once"].due
    manager._run_due_clock_jobs(now=due + timedelta(seconds=5))
    assert manager.ran == ["once"] and "once" not in manager._clock_jobs


def test_a_failing_run_does_not_stop_the_clock(manager):
    def boom(task):
        raise RuntimeError("provider down")
    manager._run_scheduled_task = boom
    manager._schedule_task(_task())
    due = manager._clock_jobs["t1"].due
    manager._run_due_clock_jobs(now=due + timedelta(seconds=5))
    assert manager._clock_jobs["t1"].due > due


def test_the_31st_waits_for_a_month_that_has_it(monkeypatch):
    """September has 30 days. The old monthly rule found no next run at all, so such a task
    showed "-" and would have had nothing to arm."""
    monkeypatch.setattr(user_time, "user_now",
                        lambda username=None, identity=None: datetime(2026, 9, 15, 10, 0, tzinfo=TOKYO))
    task = _task(frequency="monthly", day=31)
    assert task.calculate_next_run().astimezone(TOKYO) == datetime(2026, 10, 31, 8, 0, tzinfo=TOKYO)
    assert _task(frequency="monthly", day=32).calculate_next_run() is None, "a day no month has"
