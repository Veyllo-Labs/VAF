# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Every automation mutation reaches the running scheduler: a restore arms the task, and the
clock runs only a task that still stands on disk.

THE DEFECTS. The process scheduler keeps its own copy of every task (the cache its trigger
lane lists, and the armed clock jobs), and every lane changes the store through a manager of
its own. `create` and `update` refreshed the scheduler afterwards; `restore_from_trash` and
`delete` did not, and the clock ran its copy without looking at the store. Measured on a
scratch store with a running scheduler:

- a daily task restored from the trash stayed unarmed, and a restored room trigger was not in
  the scheduler's list, until some other create or update or a restart, while the agent
  already said "restored and is now active again";
- a daily task moved to the trash by the agent, or deleted for good in the app, ran its full
  prompt at its next time and was armed again for the day after, every day, until a refresh
  or restart; a delete or switch-off made in another process (the terminal while the web app
  runs) never reached the web app's scheduler at all.

THE RULE. `restore_from_trash` mirrors and arms like `create`; `_run_due_clock_jobs` asks
`_clock_record_verdict` before every run, against the record on disk.

Synthetic scopes and a scratch store only (`Platform.vaf_dir` points into tmp_path).
"""
import ast
import json
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from vaf.core import automation as auto_mod
from vaf.core.automation import AutomationManager, AutomationTask
from vaf.core.platform import Platform

LOCAL = "ab12cd34-0000-4000-8000-0000000000a0"     # the machine owner's scope
TENANT_A = "ab12cd34-0000-4000-8000-0000000000b1"
TENANT_B = "ab12cd34-0000-4000-8000-0000000000b2"
CLOCK_ID, EVENT_ID, OTHER_ID = "aaaa0001", "eeee0001", "bbbb0001"
TENANT = {"user_scope_id": TENANT_A, "user_role": "user"}

_AUTOMATION_PY = Path(__file__).resolve().parent.parent / "vaf" / "core" / "automation.py"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A scratch store: tenant A's daily task and event task, tenant B's daily task at the
    same time. The account-name lookup answers the synthetic name without asking any
    directory, the workspace mirror records its calls, and every scheduler log line is
    kept in `logs`."""
    import vaf.core.config as config_mod
    import vaf.core.thinking_mode as thinking_mode
    import vaf.core.thinking_workspace as tw

    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(config_mod, "get_local_admin_scope_id", lambda: LOCAL)
    monkeypatch.setattr(thinking_mode, "_resolve_username_for_scope",
                        lambda scope: "scope_" + str(scope).replace("-", "")[:8])
    mirrored = []
    monkeypatch.setattr(tw, "sync_automation_status_to_workspace",
                        lambda **kw: mirrored.append(kw))
    logs = []
    monkeypatch.setattr(AutomationManager, "_log_scheduler_event",
                        lambda self, message: logs.append(message))
    monkeypatch.setattr(auto_mod, "append_domain_log", lambda domain, message: logs.append(message))
    monkeypatch.setattr(auto_mod, "_scheduler_manager", None)
    root = tmp_path / "automations"
    _write(root, AutomationTask(id=CLOCK_ID, name="Morning report", prompt="report",
                                frequency="daily", time="10:00", user_scope_id=TENANT_A,
                                incarnation="inc0a"))
    _write(root, AutomationTask(id=OTHER_ID, name="Other report", prompt="other",
                                frequency="daily", time="10:00", user_scope_id=TENANT_B,
                                incarnation="inc0b"))
    _write(root, AutomationTask(id=EVENT_ID, name="On deploy", prompt="deploy",
                                frequency="on_event", user_scope_id=TENANT_A,
                                incarnation="inc0e",
                                trigger={"kind": "room_message", "room_id": "room1",
                                         "match": "deploy", "cursor": 7}))
    return {"root": root, "mirrored": mirrored, "logs": logs}


def _write(root: Path, task: AutomationTask) -> Path:
    folder = root / task.user_scope_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{task.id}.json"
    path.write_text(json.dumps(task.to_dict()), encoding="utf-8")
    return path


def _path(store, task_id: str, scope: str = TENANT_A) -> Path:
    return store["root"] / scope / f"{task_id}.json"


def _armed_scheduler(monkeypatch):
    """The process scheduler as `refresh_scheduler_from_disk` sees it running: a global
    manager whose loop thread is alive (a thread that only waits), every enabled task armed,
    and its runs recorded instead of started: the ids in `sched.ran`, the tasks handed to
    the run in `sched.ran_tasks`."""
    stop = threading.Event()
    loop = threading.Thread(target=stop.wait, daemon=True)
    loop.start()
    sched = AutomationManager()
    sched._running = True
    sched._scheduler_thread = loop
    sched.ran, sched.ran_tasks = [], []

    def record(task, *, trigger=None):
        sched.ran.append(task.id)
        sched.ran_tasks.append(task)

    sched._run_scheduled_task = record
    for task in sched.list(enabled_only=True):
        sched._schedule_task(task)
    monkeypatch.setattr(auto_mod, "_scheduler_manager", sched)
    return sched, stop


class _Clock:
    """A clock the test sets: the scheduler's `datetime.now()` and the owner's time
    (`user_time.user_now`, no time zone) both read `self.now`, so arming a job and ticking
    it count from the same moment and never cross a real slot by chance."""

    def __init__(self, monkeypatch, start: datetime):
        import vaf.core.user_time as user_time
        self.now = start
        clock = self

        class _Now(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock.now

        monkeypatch.setattr(auto_mod, "datetime", _Now)
        monkeypatch.setattr(user_time, "user_now", lambda username=None, identity=None: clock.now)
        monkeypatch.setattr(user_time, "resolve_user_timezone",
                            lambda username=None, identity=None: None)


_DAY = datetime(2026, 3, 2)


def _set_frequency(store, frequency: str, task_id: str = CLOCK_ID) -> None:
    path = _path(store, task_id)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["frequency"] = frequency
    path.write_text(json.dumps(record), encoding="utf-8")


def _trash(task_id: str = CLOCK_ID) -> None:
    from vaf.tools.automation import DeleteAutomationTool
    assert "Moved to Trash" in DeleteAutomationTool().run(task_id=task_id, **TENANT)


def _restore(task_id: str = CLOCK_ID) -> str:
    from vaf.tools.automation import RestoreAutomationTool
    return RestoreAutomationTool().run(task_id=task_id, **TENANT)


# -- a restore arms the task (the request) ----------------------------------------------------

def test_a_restored_clock_task_is_armed_and_runs(store, monkeypatch):
    """Trashed, the refresh disarms it; restored, it is armed at its next run at once, and
    the next due tick runs it, with no other create, update or restart in between.
    MUTATION: remove the arm from `restore_from_trash`."""
    sched, stop = _armed_scheduler(monkeypatch)
    try:
        assert CLOCK_ID in sched._clock_jobs
        _trash()
        auto_mod.refresh_scheduler_from_disk(origin="test")
        assert CLOCK_ID not in sched._clock_jobs
        assert "Restored Successfully" in _restore()
        assert CLOCK_ID in sched._clock_jobs, "a restored task is armed at once"
        job = sched._clock_jobs[CLOCK_ID]
        assert job.due == sched.tasks[CLOCK_ID].calculate_next_run()
        assert job.task.incarnation == "inc0a", "the restore keeps the task's incarnation"
        sched._run_due_clock_jobs(now=job.due + timedelta(seconds=5))
        assert CLOCK_ID in sched.ran
        assert any(m.startswith(f"REFRESH_START origin='restore:{CLOCK_ID}'")
                   for m in store["logs"]), store["logs"]
    finally:
        stop.set()


def test_a_restored_event_task_is_listed_by_the_scheduler(store, monkeypatch):
    """The room-trigger lane asks `list(enabled_only=True)` of the scheduler on every tick.
    A restored event task was missing there until the next refresh. The task keeps its read
    position through the trash. MUTATION: remove the arm from `restore_from_trash`."""
    sched, stop = _armed_scheduler(monkeypatch)
    try:
        _trash(EVENT_ID)
        auto_mod.refresh_scheduler_from_disk(origin="test")
        assert EVENT_ID not in {t.id for t in sched.list(enabled_only=True)}
        assert "Restored Successfully" in _restore(EVENT_ID)
        listed = {t.id: t for t in sched.list(enabled_only=True)}
        assert EVENT_ID in listed, "a restored trigger is watched at once"
        assert listed[EVENT_ID].trigger.get("cursor") == 7, "the read position stays"
    finally:
        stop.set()


def test_a_refused_restore_does_not_refresh(store, monkeypatch):
    """A live task of the same id (an approval retry created its planned id again) refuses
    the restore: nothing is armed, mirrored or written. MUTATION: call the arm before the
    destination check."""
    mgr = AutomationManager(user_scope_id=TENANT_A)
    assert mgr.move_to_trash(CLOCK_ID)
    live = _write(store["root"], AutomationTask(id=CLOCK_ID, name="New one", prompt="new",
                                                frequency="daily", time="11:00",
                                                user_scope_id=TENANT_A, incarnation="inc1a"))
    before = live.read_bytes()
    refreshes = []
    monkeypatch.setattr(auto_mod, "refresh_scheduler_from_disk",
                        lambda origin="unknown": refreshes.append(origin) or False)
    store["mirrored"].clear()
    assert AutomationManager(user_scope_id=TENANT_A).restore_from_trash(CLOCK_ID) is False
    assert refreshes == []
    assert [m for m in store["mirrored"] if m["event"] == "automation_restored"] == []
    assert live.read_bytes() == before
    assert (store["root"] / TENANT_A / "trash" / f"{CLOCK_ID}.json").exists()


@pytest.mark.parametrize("enabled", [True, False], ids=["on", "off"])
def test_a_restore_reaches_the_workspace_mirror(store, enabled):
    """The delete mirrored the task as switched off; the restore mirrors it once more, as it
    is stored. MUTATION: remove the mirror call from `restore_from_trash`."""
    path = _path(store, CLOCK_ID)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["enabled"] = enabled
    path.write_text(json.dumps(record), encoding="utf-8")
    mgr = AutomationManager(user_scope_id=TENANT_A)
    assert mgr.move_to_trash(CLOCK_ID)
    store["mirrored"].clear()
    assert AutomationManager(user_scope_id=TENANT_A).restore_from_trash(CLOCK_ID) is True
    assert [(m["event"], m["automation_data"]["id"], m["automation_data"]["enabled"],
             m["user_scope_id"]) for m in store["mirrored"]] == [
        ("automation_restored", CLOCK_ID, enabled, TENANT_A)]


def test_a_failing_refresh_does_not_fail_the_restore(store, monkeypatch):
    """The task is back on disk before the scheduler is asked, so a scheduler that fails
    must not turn a done restore into a failure. MUTATION: drop the `try/except` around
    the arm."""
    def boom(origin="unknown"):
        raise RuntimeError("scheduler broken")
    mgr = AutomationManager(user_scope_id=TENANT_A)
    assert mgr.move_to_trash(CLOCK_ID)
    monkeypatch.setattr(auto_mod, "refresh_scheduler_from_disk", boom)
    lane = AutomationManager(user_scope_id=TENANT_A)
    assert lane.restore_from_trash(CLOCK_ID) is True
    assert _path(store, CLOCK_ID).exists() and lane.get(CLOCK_ID) is not None
    assert any(m.startswith(f"RESTORE_ARM_FAILED task_id={CLOCK_ID}") for m in store["logs"])


# -- the clock runs only a task that still stands on disk -------------------------------------

def _tick(sched):
    """One tick just past the armed tasks' common due time."""
    due = sched._clock_jobs[OTHER_ID].due
    assert sched._clock_jobs[CLOCK_ID].due == due, "both reports are armed for the same time"
    sched._run_due_clock_jobs(now=due + timedelta(seconds=5))
    return due


def _dropped(store, reason):
    return any(m == f"CLOCK_DROPPED task_id={CLOCK_ID} reason={reason!r}" for m in store["logs"])


def test_a_trashed_task_does_not_run(store, monkeypatch):
    """Moved to the trash by the agent while armed: the tick does not run it and drops the
    job; the other account's task due at the same time runs. MUTATION: remove the disk
    check from `_run_due_clock_jobs`."""
    sched, stop = _armed_scheduler(monkeypatch)
    try:
        _trash()
        assert CLOCK_ID in sched._clock_jobs, "the trash does not reach the scheduler itself"
        _tick(sched)
        assert sched.ran == [OTHER_ID]
        assert CLOCK_ID not in sched._clock_jobs
        assert _dropped(store, "task gone"), store["logs"]
    finally:
        stop.set()


@pytest.mark.parametrize("how", ["permanent", "file-removed"])
def test_a_deleted_task_does_not_run(store, monkeypatch, how):
    """Deleted for good through a scoped manager (the WebSocket lane), or its file removed
    behind the scheduler's back (another process). MUTATION: remove the disk check from
    `_run_due_clock_jobs`."""
    sched, stop = _armed_scheduler(monkeypatch)
    try:
        if how == "permanent":
            assert AutomationManager(user_scope_id=TENANT_A).delete(CLOCK_ID, permanent=True)
        else:
            os.remove(_path(store, CLOCK_ID))
        _tick(sched)
        assert sched.ran == [OTHER_ID]
        assert CLOCK_ID not in sched._clock_jobs
        assert _dropped(store, "task gone"), store["logs"]
    finally:
        stop.set()


@pytest.mark.parametrize("change, reason", [
    ({"enabled": False}, "disabled"),
    ({"incarnation": "inc1a"}, "created anew"),
    ({"frequency": "on_event", "trigger": {"kind": "room_message", "room_id": "room1"}},
     "no longer a clock task"),
    ({"frequency": "fortnightly"}, "no longer a clock task"),
    ({"id": "other001"}, "task gone"),
    ({"frequency": "once", "last_run": "2026-01-01T10:00:00"}, "once already ran"),
    ({"frequency": "once", "last_completed_local_date": "2026-01-01"}, "once already ran"),
], ids=["disabled", "created-anew", "on-event", "unknown-frequency", "other-id",
        "once-ran", "once-completed"])
def test_a_task_changed_on_disk_does_not_run(store, monkeypatch, change, reason):
    """Switched off, replaced by a task created anew under the id, turned into an event task
    or into a frequency this version does not arm (written by a newer one), or the file now
    holding another task, or a one-time task that has already run (its removal after the
    run failed), each written behind the scheduler (another process): not run, job
    dropped. MUTATION: drop that one condition from `_clock_record_verdict` (one per
    parameter); for the unknown frequency, compare with `on_event` only."""
    sched, stop = _armed_scheduler(monkeypatch)
    try:
        path = _path(store, CLOCK_ID)
        record = json.loads(path.read_text(encoding="utf-8"))
        record.update(change)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f)
        _tick(sched)
        assert sched.ran == [OTHER_ID]
        assert CLOCK_ID not in sched._clock_jobs
        assert _dropped(store, reason), store["logs"]
    finally:
        stop.set()


@pytest.mark.parametrize("frequency", ["daily", "once"])
def test_an_unreadable_record_skips_one_slot(store, monkeypatch, frequency):
    """A file that is there but does not parse (a manual edit half written) is not taken for
    a deleted task: that one slot is not run, and the task stays armed for its next one. A
    one-time task has no next one: its job ends and its file stays, to be armed again from
    the file at the next refresh or restart. MUTATION: treat `skip` as `drop` (job gone);
    separately, as `run`; for `once`, keep a skipped one-time job armed at its slot."""
    path = _path(store, CLOCK_ID)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["frequency"] = frequency
    path.write_text(json.dumps(record), encoding="utf-8")
    sched, stop = _armed_scheduler(monkeypatch)
    try:
        path.write_text('{"id": ', encoding="utf-8")
        due = _tick(sched)
        assert sched.ran == [OTHER_ID]
        assert f"CLOCK_SKIPPED task_id={CLOCK_ID} reason='record unreadable'" in store["logs"]
        if frequency == "daily":
            assert CLOCK_ID in sched._clock_jobs, "an unreadable record does not end the task"
            assert sched._clock_jobs[CLOCK_ID].due > due, "armed for its next slot"
        else:
            assert CLOCK_ID not in sched._clock_jobs, "a one-time task has no next slot"
            assert path.read_text(encoding="utf-8") == '{"id": ', "its file stays as it is"
    finally:
        stop.set()


@pytest.mark.parametrize("frequency", ["daily", "once"])
@pytest.mark.parametrize("spelling", ["spaces", "number"])
def test_an_id_spelled_differently_on_disk(store, monkeypatch, frequency, spelling):
    """A record whose `id` names the task but is written differently (spaces around it, or
    a JSON number for an all-digit id) passes the id check, which reads it stripped. The run
    and the armed job keep the id the job is armed under: deleting the job by the record's
    spelling raised KeyError out of the tick, which ended the scheduler loop for every
    account. The run's bookkeeping writes onto the same file under the same cache key: kept
    as the record spells it, the stamp went into a second file (`" aaaa0001 .json"`) and a
    number id added a second cache key. MUTATION: take the record's id in
    `_task_from_record` (no `fresh.id = task.id`); separately, read the job's key from
    `job.task` after the claim."""
    task_id = CLOCK_ID if spelling == "spaces" else "12345678"
    written = f" {CLOCK_ID} " if spelling == "spaces" else 12345678
    path = _write(store["root"], AutomationTask(id=task_id, name="Report", prompt="report",
                                                frequency=frequency, time="10:00",
                                                user_scope_id=TENANT_A, incarnation="inc0n"))
    sched, stop = _armed_scheduler(monkeypatch)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        record["id"] = written
        path.write_text(json.dumps(record), encoding="utf-8")
        due = sched._clock_jobs[task_id].due
        sched._run_due_clock_jobs(now=due + timedelta(seconds=5))
        assert task_id in sched.ran
        assert [t.id for t in sched.ran_tasks if t.id == task_id] == [task_id]
        if frequency == "daily":
            assert sched._clock_jobs[task_id].due > due, "armed for its next run"
        else:
            assert task_id not in sched._clock_jobs, "a one-time task ends"
        run = next(t for t in sched.ran_tasks if t.id == task_id)
        sched._record_successful_run(run)
        names = {p.name for p in auto_mod._task_files(path.parent)}
        assert names == {f"{CLOCK_ID}.json", f"{EVENT_ID}.json", path.name}, names
        assert json.loads(path.read_text(encoding="utf-8"))["last_run"], "the run is stamped"
        assert [k for k in sched.tasks if str(k).strip() == task_id] == [task_id]
    finally:
        stop.set()


@pytest.mark.parametrize("scope", [123, ["x"], True], ids=["number", "list", "bool"])
def test_a_scope_that_is_not_text_is_read_from_the_folder(store, monkeypatch, scope):
    """A `user_scope_id` that is not text made every path of the task raise: the clock
    failed on that job on every tick, before the other accounts' jobs due after it. A
    scope is text or nothing (`AutomationTask.__post_init__`); nothing is the scope of the
    folder the file lives in, so the task runs as the account whose folder holds it.
    MUTATION: keep a scope that is not text."""
    path = _path(store, CLOCK_ID)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["user_scope_id"] = scope
    path.write_text(json.dumps(record), encoding="utf-8")
    assert AutomationTask.from_dict(record).user_scope_id is None
    sched, stop = _armed_scheduler(monkeypatch)
    try:
        due = _tick(sched)
        assert sorted(sched.ran) == sorted([CLOCK_ID, OTHER_ID])
        run = next(t for t in sched.ran_tasks if t.id == CLOCK_ID)
        assert run.user_scope_id == TENANT_A
        assert sched._clock_jobs[CLOCK_ID].due > due
    finally:
        stop.set()


def test_one_job_that_raises_does_not_stop_the_others(store, monkeypatch):
    """An exception out of one job's handling gives up that job's slot (it is armed for the
    next one) and the tick goes on with the jobs after it. MUTATION: handle the due jobs
    without the try around each one."""
    sched, stop = _armed_scheduler(monkeypatch)
    try:
        real = sched._clock_record_verdict

        def verdict(task):
            if task.id == CLOCK_ID:
                raise TypeError("a job that breaks")
            return real(task)

        monkeypatch.setattr(sched, "_clock_record_verdict", verdict)
        due = _tick(sched)
        assert sched.ran == [OTHER_ID]
        assert sched._clock_jobs[CLOCK_ID].due > due, "the failing job gave up this slot"
        assert any(m.startswith(f"CLOCK_JOB_FAILED task_id={CLOCK_ID}") for m in store["logs"])
    finally:
        stop.set()


def test_a_path_that_cannot_be_built_skips_one_slot(store, monkeypatch):
    """The verdict answers skip when even the task's path cannot be built, as for a record
    it cannot read. MUTATION: build the path before the verdict's try."""
    sched, stop = _armed_scheduler(monkeypatch)
    try:
        real = sched._path_for_task

        def path_for(task):
            if task.id == CLOCK_ID:
                raise TypeError("no path")
            return real(task)

        monkeypatch.setattr(sched, "_path_for_task", path_for)
        due = _tick(sched)
        assert sched.ran == [OTHER_ID]
        assert sched._clock_jobs[CLOCK_ID].due > due
        assert f"CLOCK_SKIPPED task_id={CLOCK_ID} reason='record unreadable'" in store["logs"]
    finally:
        stop.set()


def test_a_failing_tick_does_not_end_the_scheduler_loop(store, monkeypatch):
    """The loop that runs the clock also delivers every account's reminders and room
    triggers, so an exception out of the clock is logged and the loop goes on, as for the
    lanes beside it. MUTATION: call `_run_due_clock_jobs` without its try."""
    import time as real_time
    import types

    import vaf.core.calendar_sync as calendar_sync
    import vaf.core.reminders as reminders
    monkeypatch.setattr(reminders, "fire_due_reminders", lambda: None)
    monkeypatch.setattr(calendar_sync, "fire_due_calendar_reminders", lambda: None)
    sched = AutomationManager()
    sched._fire_room_triggers = lambda: 0
    ticks = []

    def failing_tick(now=None):
        ticks.append(now)
        raise KeyError("a record that breaks the tick")

    def sleep(_seconds):
        if len(ticks) >= 2:
            sched._running = False

    sched._run_due_clock_jobs = failing_tick
    # Bound in this module only: the loop's 30-second wait returns at once.
    monkeypatch.setattr(auto_mod, "time", types.SimpleNamespace(
        sleep=sleep, monotonic=real_time.monotonic))
    sched.start_scheduler()
    sched._scheduler_thread.join(10)
    sched._running = False
    assert not sched._scheduler_thread.is_alive()
    assert len(ticks) == 2, "the loop went on after the failing tick"
    assert any(m.startswith("CLOCK_TICK_FAILED") for m in store["logs"]), store["logs"]


def test_the_run_is_the_record_on_disk(store, monkeypatch):
    """The run gets the task as it is on disk, as a room trigger's run does: a prompt
    changed behind the scheduler (another process) is the prompt that runs, and a record
    written without a scope keeps the scope of the place it lives in. MUTATION: run the
    scheduler's copy `job.task`; separately, build the task without `_task_from_record`."""
    sched, stop = _armed_scheduler(monkeypatch)
    try:
        path = _path(store, CLOCK_ID)
        record = json.loads(path.read_text(encoding="utf-8"))
        record["prompt"] = "changed on disk"
        del record["user_scope_id"]
        path.write_text(json.dumps(record), encoding="utf-8")
        _tick(sched)
        run = {task.id: task for task in sched.ran_tasks}[CLOCK_ID]
        assert run.prompt == "changed on disk"
        assert run.user_scope_id == TENANT_A
        assert sched._clock_jobs[CLOCK_ID].task.prompt == "changed on disk"
    finally:
        stop.set()


# -- a refresh during the tick, and a refresh keeps a pending slot ----------------------------

def _refresh_inside_the_verdict(sched, monkeypatch, answer=None):
    """Make every verdict of the tick first refresh the scheduler, the way a restore or an
    update on another thread can land between the tick's due list and its claim. `answer`
    replaces the verdict; None keeps the real one. Returns the real verdict, to put back."""
    real = sched._clock_record_verdict

    def verdict(task):
        auto_mod.refresh_scheduler_from_disk(origin="test-during-tick")
        return answer if answer is not None else real(task)

    monkeypatch.setattr(sched, "_clock_record_verdict", verdict)
    return real


def test_a_job_replaced_during_the_tick_does_not_run(store, monkeypatch):
    """A refresh between the tick's due list and the run replaces the job. The replaced job
    starts no run; the job the refresh put there keeps the pending slot and runs at the next
    tick, once. MUTATION: check that the job is still registered only after the run starts,
    not before."""
    sched, stop = _armed_scheduler(monkeypatch)
    try:
        old = sched._clock_jobs[CLOCK_ID]
        real = _refresh_inside_the_verdict(sched, monkeypatch)
        due = _tick(sched)
        assert sched.ran == [], "a replaced job starts no run"
        new = sched._clock_jobs[CLOCK_ID]
        assert new is not old and new.due == due, "the job the refresh armed keeps the slot"
        monkeypatch.setattr(sched, "_clock_record_verdict", real)
        sched._run_due_clock_jobs(now=due + timedelta(seconds=35))
        assert sorted(sched.ran) == [CLOCK_ID, OTHER_ID], "each runs once"
    finally:
        stop.set()


def test_a_drop_never_removes_the_job_a_refresh_armed(store, monkeypatch):
    """A drop removes only the job it judged: a restore or refresh that armed a new job for
    the id in the meantime keeps it. MUTATION: remove the job by its id on a drop, whatever
    job is registered."""
    sched, stop = _armed_scheduler(monkeypatch)
    try:
        old = sched._clock_jobs[CLOCK_ID]
        _refresh_inside_the_verdict(sched, monkeypatch, answer=("drop", "test", None))
        _tick(sched)
        assert CLOCK_ID in sched._clock_jobs and sched._clock_jobs[CLOCK_ID] is not old
    finally:
        stop.set()


def _pending(sched, task_id=CLOCK_ID):
    """Put the task's slot a few seconds in the past: due, not yet reached by the tick."""
    slot = datetime.now() - timedelta(seconds=5)
    sched._clock_jobs[task_id].due = slot
    return slot


@pytest.mark.parametrize("lane", ["restore", "update", "create"])
def test_a_refresh_keeps_a_pending_slot(store, monkeypatch, lane):
    """A task whose slot has come but which the tick has not reached yet keeps that slot
    through a refresh started by a change to ANOTHER account's task. Measured before: an
    update, a create or a restore of any task re-armed every task from now, so the due task
    moved to the next day and did not run that day. MUTATION: keep no slot in
    `_swap_clock_jobs`."""
    other = AutomationManager(user_scope_id=TENANT_B)
    if lane == "restore":
        assert other.move_to_trash(OTHER_ID)
    sched, stop = _armed_scheduler(monkeypatch)
    try:
        slot = _pending(sched)
        if lane == "restore":
            assert AutomationManager(user_scope_id=TENANT_B).restore_from_trash(OTHER_ID)
        elif lane == "update":
            assert other.update(OTHER_ID, prompt="changed") is not None
        else:
            other.create(AutomationTask(id="cccc0001", name="New", prompt="new",
                                        frequency="daily", time="15:00",
                                        user_scope_id=TENANT_B))
        assert any(m.startswith("REFRESH_DONE") for m in store["logs"]), "the lane refreshed"
        assert sched._clock_jobs[CLOCK_ID].due == slot
        sched._run_due_clock_jobs()
        assert CLOCK_ID in sched.ran
    finally:
        stop.set()


@pytest.mark.parametrize("change", [
    {"time": "11:00"}, {"incarnation": "inc1a"}, {"user_scope_id": TENANT_B},
    {"frequency": "hourly"}, {"weekday": "monday"}, {"day": 15},
], ids=["time", "created-anew", "owner", "frequency", "weekday", "day"])
def test_a_refresh_arms_a_changed_task_by_its_new_rule(store, monkeypatch, change):
    """A pending slot is kept only for the same task with the same rule: a task whose time,
    frequency, weekday or day was changed, that was created anew under the id, or that now
    belongs to another account under the same id and incarnation (its slots are counted in
    that account's time zone), is armed at its own next run. MUTATION: keep every pending
    slot whatever the rule (`_same_clock_rule` always True); separately, leave that one
    field out of `_same_clock_rule` (one per parameter)."""
    sched, stop = _armed_scheduler(monkeypatch)
    try:
        slot = _pending(sched)
        path = _path(store, CLOCK_ID)
        record = json.loads(path.read_text(encoding="utf-8"))
        record.update(change)
        path.unlink()
        target = _path(store, CLOCK_ID, record["user_scope_id"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(record), encoding="utf-8")
        auto_mod.refresh_scheduler_from_disk(origin="test")
        job = sched._clock_jobs[CLOCK_ID]
        assert job.due > slot and job.due == sched.tasks[CLOCK_ID].calculate_next_run()
    finally:
        stop.set()


def test_a_slot_that_comes_due_during_the_refresh_is_kept(store, monkeypatch):
    """What is kept is decided when the new jobs are swapped in, not before the store is
    read: a slot that comes due while the refresh reads the store (hundreds of tasks take
    a noticeable time) keeps that day's run. MUTATION: decide which jobs are pending
    before `reload_tasks`."""
    sched, stop = _armed_scheduler(monkeypatch)
    try:
        real_reload = sched.reload_tasks
        slot = datetime.now() - timedelta(seconds=1)

        def reload_while_the_slot_comes():
            sched._clock_jobs[CLOCK_ID].due = slot
            real_reload()

        monkeypatch.setattr(sched, "reload_tasks", reload_while_the_slot_comes)
        auto_mod.refresh_scheduler_from_disk(origin="test")
        assert sched._clock_jobs[CLOCK_ID].due == slot
        sched._run_due_clock_jobs()
        assert CLOCK_ID in sched.ran
    finally:
        stop.set()


def test_a_one_time_slot_claimed_during_the_refresh_runs_once(store, monkeypatch):
    """A tick that claims a one-time task while a refresh reads the store ends its job; the
    refresh arms the task again from its file (still there when the run has not removed it
    yet, or its removal failed) and must not give it back the slot just claimed. Measured
    before: two runs. On a controlled clock: on the wall clock the forced slot could fall
    just before the task's real time, which the refresh then armed and the later tick ran.
    MUTATION: decide which slots to keep from the jobs as they were before
    `reload_tasks`."""
    _set_frequency(store, "once")
    clock = _Clock(monkeypatch, _DAY.replace(hour=9, minute=59))
    sched, stop = _armed_scheduler(monkeypatch)
    try:
        slot = sched._clock_jobs[CLOCK_ID].due
        assert slot == _DAY.replace(hour=10)
        clock.now = slot + timedelta(seconds=5)
        real_reload = sched.reload_tasks

        def tick_while_reading():
            sched._run_due_clock_jobs()
            real_reload()

        monkeypatch.setattr(sched, "reload_tasks", tick_while_reading)
        auto_mod.refresh_scheduler_from_disk(origin="test")
        monkeypatch.setattr(sched, "reload_tasks", real_reload)
        clock.now = slot + timedelta(seconds=35)
        sched._run_due_clock_jobs()
        assert sched.ran.count(CLOCK_ID) == 1
    finally:
        stop.set()


@pytest.mark.parametrize("frequency", ["daily", "once"])
def test_a_slot_claimed_between_arming_and_swap_runs_once(store, monkeypatch, frequency):
    """The refresh arms its new jobs while the old ones are in force. A job armed before
    its slot, whose slot the tick then claims on the OLD job before the swap, must not bring
    that slot back. Measured before: two runs of the slot, daily and one-time. MUTATION:
    swap without the record of claimed slots."""
    _set_frequency(store, frequency)
    clock = _Clock(monkeypatch, _DAY.replace(hour=9, minute=59))
    sched, stop = _armed_scheduler(monkeypatch)
    try:
        slot = sched._clock_jobs[CLOCK_ID].due
        assert slot == _DAY.replace(hour=10)
        real_swap = sched._swap_clock_jobs

        def tick_then_swap(fresh):
            assert fresh[CLOCK_ID].due == slot, "armed before the slot"
            clock.now = slot + timedelta(seconds=5)
            sched._run_due_clock_jobs()
            real_swap(fresh)

        monkeypatch.setattr(sched, "_swap_clock_jobs", tick_then_swap)
        clock.now = slot - timedelta(seconds=1)
        auto_mod.refresh_scheduler_from_disk(origin="test")
        monkeypatch.setattr(sched, "_swap_clock_jobs", real_swap)
        clock.now = slot + timedelta(seconds=35)
        sched._run_due_clock_jobs()
        assert sched.ran.count(CLOCK_ID) == 1 and sched.ran.count(OTHER_ID) == 1
        if frequency == "daily":
            assert sched._clock_jobs[CLOCK_ID].due == slot + timedelta(days=1)
        else:
            assert CLOCK_ID not in sched._clock_jobs
        assert f"CLAIM_KEPT task_id={CLOCK_ID} reason='slot already claimed'" in store["logs"]
    finally:
        stop.set()


def test_a_refresh_during_the_run_does_not_run_the_slot_again(store, monkeypatch):
    """The next slot is set when the job is claimed, before the run starts: a create,
    update or restore during the run (its refresh) finds the slot taken. MUTATION: set the
    next slot only after the run has started."""
    sched, stop = _armed_scheduler(monkeypatch)
    try:
        record = sched._run_scheduled_task

        def run_and_refresh(task, *, trigger=None):
            record(task)
            auto_mod.refresh_scheduler_from_disk(origin="test-during-run")

        _pending(sched)
        sched._run_scheduled_task = run_and_refresh
        sched._run_due_clock_jobs()
        sched._run_scheduled_task = record
        sched._run_due_clock_jobs()
        assert sched.ran.count(CLOCK_ID) == 1
        assert sched._clock_jobs[CLOCK_ID].due > datetime.now()
    finally:
        stop.set()


def test_an_unchanged_task_runs(store, monkeypatch):
    """The control: a task on disk as it was armed runs at its due tick and is armed for
    its next run."""
    sched, stop = _armed_scheduler(monkeypatch)
    try:
        due = _tick(sched)
        assert sorted(sched.ran) == sorted([CLOCK_ID, OTHER_ID])
        assert sched._clock_jobs[CLOCK_ID].due > due
    finally:
        stop.set()


# -- the delete path never takes the scheduler lock -------------------------------------------

def _functions(tree):
    return {node.name: node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _call_name(call: ast.Call) -> str:
    func = call.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""


_NOT_FROM_A_DELETE = {"refresh_scheduler_from_disk", "ensure_scheduler_started",
                      "_scheduler_manager_lock"}


def test_delete_never_takes_the_scheduler_lock():
    """`_schedule_task` deletes a spent one-time task while `refresh_scheduler_from_disk` or
    `ensure_scheduler_started` holds the non-reentrant `_scheduler_manager_lock`, so a
    delete that refreshed the scheduler (or took that lock) would deadlock the scheduler.
    Deletes reach the scheduler through the clock's disk check instead. Walks `delete` and
    `_delete` and every function of this module they call, resolved by bare name as the
    store-lock leaf guard does. MUTATION: add `refresh_scheduler_from_disk()` to `_delete`."""
    tree = ast.parse(_AUTOMATION_PY.read_text(encoding="utf-8"))
    functions = _functions(tree)
    assert {"delete", "_delete", "_schedule_task"} <= set(functions)
    bad = []

    def visit(name, where, seen):
        for node in ast.walk(functions[name]):
            if isinstance(node, ast.Name) and node.id in _NOT_FROM_A_DELETE:
                bad.append((where, node.id))
            if not isinstance(node, ast.Call):
                continue
            called = _call_name(node)
            if called in _NOT_FROM_A_DELETE:
                bad.append((where, called))
            elif called in functions and called not in seen:
                visit(called, f"{where} -> {called}", seen | {called})

    for start in ("delete", "_delete"):
        visit(start, start, frozenset({start}))
    assert bad == []
