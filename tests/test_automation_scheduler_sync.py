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
from datetime import timedelta
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
    and its runs recorded in `sched.ran` instead of started."""
    stop = threading.Event()
    loop = threading.Thread(target=stop.wait, daemon=True)
    loop.start()
    sched = AutomationManager()
    sched._running = True
    sched._scheduler_thread = loop
    sched.ran = []
    sched._run_scheduled_task = lambda task, *, trigger=None: sched.ran.append(task.id)
    for task in sched.list(enabled_only=True):
        sched._schedule_task(task)
    monkeypatch.setattr(auto_mod, "_scheduler_manager", sched)
    return sched, stop


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
], ids=["disabled", "created-anew", "on-event"])
def test_a_task_changed_on_disk_does_not_run(store, monkeypatch, change, reason):
    """Switched off, replaced by a task created anew under the id, or turned into an event
    task, each written behind the scheduler (another process): not run, job dropped.
    MUTATION: drop that one condition from `_clock_record_verdict` (one per parameter)."""
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


def test_an_unreadable_record_skips_one_slot(store, monkeypatch):
    """A file that is there but does not parse (a manual edit half written) is not taken for
    a deleted task: that one slot is not run, and the task stays armed for its next one.
    MUTATION: treat `skip` as `drop` (job gone); separately, as `run`."""
    sched, stop = _armed_scheduler(monkeypatch)
    try:
        _path(store, CLOCK_ID).write_text('{"id": ', encoding="utf-8")
        due = _tick(sched)
        assert sched.ran == [OTHER_ID]
        assert CLOCK_ID in sched._clock_jobs, "an unreadable record does not end the task"
        assert sched._clock_jobs[CLOCK_ID].due > due, "armed for its next slot"
        assert f"CLOCK_SKIPPED task_id={CLOCK_ID} reason='record unreadable'" in store["logs"]
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
