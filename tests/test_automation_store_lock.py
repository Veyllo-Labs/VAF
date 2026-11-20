# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Two changes of one automation made at the same moment keep each other's fields.

THE DEFECT. `AutomationManager.update` changed the manager's CACHED copy of a task and wrote
the whole object back, and the run bookkeeping and the room-trigger cursor wrote back the
object the scheduler captured when the task fired. Every lane builds its manager per command
(the WebSocket, the agent's tool, an approved handoff in a worker thread, the CLI, a second
process), so the copy a lane wrote was as old as its manager. Measured before the fix: a lane
that loaded the task, then saved its name after another lane had saved a new time, put the
old time back (200 of 200 two-thread trials lost a field, 13 of 100 two-process trials); a run
put back a time and an off switch set while it ran; an update or a run stamp after a delete
brought the deleted task back; a restore silently replaced a live task of the same id.

THE RULE. Every write of a task file holds the lock of its directory (`.automations.lock`,
`AutomationManager._store_lock`, a kernel lock through filelock), and a change is applied to
the record ON DISK at that moment (`_mutate_record`), never to the copy the lane loaded.
A file that is gone is not written. The lock is a leaf: nothing that takes another lock runs
while it is held.

MUTATIONS, each turns the named test red:
- `_mutate_record` writes the passed object instead of re-reading the file
  -> test_a_stale_manager_keeps_the_other_lanes_field, test_two_processes_lose_no_field,
     test_concurrent_writers_fuzz;
- `_store_lock` -> `contextlib.nullcontext()` -> test_two_threads_inside_the_read_modify_write_lose_nothing,
  test_a_lock_held_by_another_process_makes_update_wait_then_busy, test_a_hit_runs_only_once_its_cursor_is_on_disk;
- the filelock `Timeout` not mapped -> test_a_lock_held_by_another_process_makes_update_wait_then_busy,
  test_busy_store_answers_without_a_path;
- the stamp sites back to `_stamp_successful_run(task); self._save_task(task)`
  -> test_a_run_keeps_edits_made_while_it_ran[prompt|workflow];
- `_mutate_record` writes the passed object when the file is missing
  -> test_update_after_delete_does_not_resurrect, test_a_run_stamp_after_trash_does_not_resurrect;
- drop the destination check of `restore_from_trash` -> test_restore_never_overwrites_a_live_task;
- `_fire_room_triggers` back to `self._save_task(task)` -> test_the_cursor_lands_on_the_current_record,
  test_a_changed_trigger_rule_gets_no_old_cursor, test_only_create_and_mutate_record_save_whole_tasks;
- `_cursor_patch` compares only kind and room again -> test_a_changed_trigger_rule_gets_no_old_cursor[match];
- run the hit when its cursor write failed, or do not rewind the watch (or rewind it to the
  task's persisted cursor instead of the read's start)
  -> test_a_hit_runs_only_once_its_cursor_is_on_disk, test_a_task_file_that_cannot_be_read_once_is_not_taken_for_gone;
- read the task file leniently in the commit -> test_a_task_file_that_cannot_be_read_once_is_not_taken_for_gone;
- try the lock again for every hit of a busy store -> test_one_busy_store_costs_one_wait_per_tick;
- let a run that cannot start end the tick -> test_a_run_that_cannot_start_leaves_the_other_hits_alone;
- write a cursor that is not past the stored one, or hand the run frames the stored cursor
  covers -> test_a_cursor_already_further_on_disk_is_kept_and_not_run_again;
- drop the frequency check of `_cursor_patch` -> test_a_task_moved_to_a_clock_does_not_run_on_room_frames,
  test_room_trigger_fuzz;
- bind the watch's cursor only to the task id (not its room, or not its incarnation)
  -> test_a_waiting_hit_never_reaches_another_room_or_a_task_created_anew;
- run a task switched off -> test_a_task_switched_off_while_its_hit_waited_does_not_run;
- `create` changes the caller's object -> test_create_publishes_a_copy_and_leaves_the_callers_object;
- let `_remove_spent_once` raise -> test_a_busy_store_after_a_one_time_run_keeps_the_runs_result;
- publish the cache after the store lock in `_mutate_record` or in `_delete`
  -> test_a_delete_between_an_update_and_its_cache_keeps_the_task_gone,
     test_a_create_between_a_delete_and_its_cache_stays_listed;
- drop the incarnation check in `_mutate_record` or in `_delete`, or let `create` keep the
  given incarnation -> test_a_run_of_a_deleted_task_leaves_the_task_recreated_under_its_id_alone;
- let `update` apply `incarnation` -> test_update_ignores_identity_and_property_keys;
- let `create` keep a given incarnation -> test_an_incarnation_follows_edits_and_the_trash;
- compare the record's incarnation raw (not through `_incarnation`), in `_mutate_record` or
  in `_delete` -> test_a_number_as_the_incarnation_still_names_its_own_task;
- write the trigger cursor without `same_incarnation`
  -> test_a_hit_of_a_deleted_task_misses_its_successor_with_the_same_rule;
- run the scheduler's copy instead of the record just written
  -> test_a_hit_runs_the_task_as_it_is_on_disk, test_room_trigger_fuzz;
- filter the update keys with `hasattr` again -> test_update_ignores_identity_and_property_keys;
- refresh the scheduler inside the store lock -> test_the_store_lock_is_a_leaf,
  test_no_deadlock_between_handoff_scheduler_and_updates;
- the fallback lock made fresh per call -> test_without_filelock_threads_still_serialize.

Every store lives under tmp_path; the `store` fixture of tests/test_automation_access.py keeps
the thinking workspace and the account-name lookup out of it.
"""
import ast
import itertools
import json
import os
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from test_automation_access import (A_ID, TENANT_A, _handoff, _record, _write, _ws,  # noqa: F401
                                    store, workspaces)
from vaf.core import automation as auto_mod
from vaf.core.automation import (AutomationAccess, AutomationManager, AutomationStoreBusy,
                                 AutomationTask)

TID = "5707e001"
_REPO = Path(__file__).resolve().parent.parent
_AUTOMATION_PY = _REPO / "vaf" / "core" / "automation.py"

# One value per stored field an update lane changes, as the store keeps it after coercion.
FIELDS = {
    "name": "renamed",
    "description": "described",
    "prompt": "a new prompt",
    "time": "07:30",
    "frequency": "weekly",
    "weekday": "friday",
    "day": 15,
    "enabled": False,
    "output_path": "Reports",
    "parameters": {"city": "Berlin"},
    "workflow_steps": [{"tool": "web_search", "args": {"query": "x"}}],
    "trigger": {"kind": "room_message", "room_id": "room-x", "match": "go"},
}


@pytest.fixture
def one(store, tmp_path):
    """A store directory holding one root task (no scope, so no account lookup)."""
    d = tmp_path / "one"
    AutomationManager(storage_dir=str(d)).create(AutomationTask(
        id=TID, name="n0", description="d0", prompt="p0", frequency="daily", time="06:00"))
    return d


def _disk(d: Path, task_id: str = TID) -> dict:
    return json.loads((d / f"{task_id}.json").read_text(encoding="utf-8"))


def _join_all(threads, seconds=15.0):
    deadline = time.monotonic() + seconds
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    return [t.name for t in threads if t.is_alive()]


# ── AC1: a stale manager keeps the other lane's field ────────────────────────

@pytest.mark.parametrize("first,second", list(itertools.combinations(FIELDS, 2)),
                         ids=lambda v: v if isinstance(v, str) else None)
def test_a_stale_manager_keeps_the_other_lanes_field(one, first, second):
    stale = AutomationManager(storage_dir=str(one))     # built before either writes
    other = AutomationManager(storage_dir=str(one))
    assert other.update(TID, **{first: FIELDS[first]}) is not None
    assert stale.update(TID, **{second: FIELDS[second]}) is not None
    record = _disk(one)
    assert record[first] == FIELDS[first], f"{first} lost to the stale {second} write"
    assert record[second] == FIELDS[second]


# ── AC2, AC12: two threads inside the read-modify-write ─────────────────────

def _race_inside_the_write(d: Path, monkeypatch):
    """Thread 1 reads the record and pauses 0.3 s before it writes; thread 2 updates another
    field during that pause. Returns (record, the reads and writes of the task file in the
    order they happened, errors). Both are logged inside the store lock, so their order is
    the lock's order and not the threads' finishing order (the work `update` does after the
    lock, the mirror and the scheduler refresh, may finish in either order)."""
    import vaf.core.data_files as data_files

    m1 = AutomationManager(storage_dir=str(d))
    m2 = AutomationManager(storage_dir=str(d))
    real_read = auto_mod._read_task_record
    real_write = data_files.write_json_atomic
    first_read = threading.Event()
    order = []

    def slow_read(path, *a, **kw):
        record = real_read(path, *a, **kw)
        order.append(("read", threading.current_thread().name))
        if threading.current_thread().name == "rmw-1":
            first_read.set()
            time.sleep(0.3)
        return record

    def logged_write(path, *a, **kw):
        order.append(("write", threading.current_thread().name))
        return real_write(path, *a, **kw)

    monkeypatch.setattr(auto_mod, "_read_task_record", slow_read)
    monkeypatch.setattr(data_files, "write_json_atomic", logged_write)
    barrier = threading.Barrier(2)
    errors = []

    def one_():
        try:
            barrier.wait(5)
            m1.update(TID, name="by-1")
        except Exception as e:  # noqa: BLE001 - reported by the caller
            errors.append(repr(e))

    def two_():
        try:
            barrier.wait(5)
            assert first_read.wait(5)
            m2.update(TID, time="07:30")
        except Exception as e:  # noqa: BLE001 - reported by the caller
            errors.append(repr(e))

    threads = [threading.Thread(target=one_, name="rmw-1", daemon=True),
               threading.Thread(target=two_, name="rmw-2", daemon=True)]
    for t in threads:
        t.start()
    assert _join_all(threads, 10) == []
    return _disk(d), [step for step in order if step[1].startswith("rmw-")], errors


# The second writer reads only after the first one wrote: one read-modify-write at a time.
_SERIAL = [("read", "rmw-1"), ("write", "rmw-1"), ("read", "rmw-2"), ("write", "rmw-2")]


def test_two_threads_inside_the_read_modify_write_lose_nothing(one, monkeypatch):
    record, order, errors = _race_inside_the_write(one, monkeypatch)
    assert errors == []
    assert record["name"] == "by-1" and record["time"] == "07:30", record
    assert order == _SERIAL, order


def test_without_filelock_threads_still_serialize(one, monkeypatch):
    import vaf.core.secure_store as secure_store

    monkeypatch.setattr(secure_store, "_get_filelock_cls", lambda: None)
    record, order, errors = _race_inside_the_write(one, monkeypatch)
    assert errors == []
    assert record["name"] == "by-1" and record["time"] == "07:30", record
    assert order == _SERIAL, order


# ── AC3: two processes ───────────────────────────────────────────────────────

_CHILD = r'''
import json, random, sys, time
from pathlib import Path
from vaf.core import automation as A

A.AutomationManager._sync_workspace_automation_state = lambda self, *a, **k: None
mode, store = sys.argv[1], Path(sys.argv[2])
if mode == "race":
    field, ids, rounds, seed = sys.argv[3], sys.argv[4].split(","), int(sys.argv[5]), int(sys.argv[6])
    rng = random.Random(seed)
    (store.parent / f"ready-{field}").write_text("1")
    deadline = time.monotonic() + 30
    while not (store.parent / "go").exists() and time.monotonic() < deadline:
        time.sleep(0.005)
    last, losses = {}, 0
    for i in range(rounds):
        tid = ids[i % len(ids)]
        mgr = A.AutomationManager(storage_dir=str(store))
        if tid in last and getattr(mgr.get(tid), field) != last[tid]:
            losses += 1
        time.sleep(rng.uniform(0, 0.003))
        value = f"{field}-{i}"
        mgr.update(tid, **{field: value})
        last[tid] = value
    print(json.dumps({"losses": losses, "last": last}), flush=True)
elif mode == "hold":
    mgr = A.AutomationManager(storage_dir=str(store))
    mgr.update(sys.argv[3], description="from-child")
    with A.AutomationManager._store_lock(store):
        print("held", flush=True)
        sys.stdin.readline()
    print("released", flush=True)
'''


def _child_env(tmp_path: Path) -> dict:
    home = tmp_path / "child-home"
    for sub in ("", "data", "config", "cache", "tmp"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": os.pathsep.join([str(_REPO)] + [p for p in [os.environ.get("PYTHONPATH")] if p]),
        "HOME": str(home), "USERPROFILE": str(home),
        "XDG_DATA_HOME": str(home / "data"), "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_CACHE_HOME": str(home / "cache"), "TMPDIR": str(home / "tmp"),
        "PYTHONIOENCODING": "utf-8",
    })
    return env


def _child(tmp_path: Path, *args, stdin=None) -> subprocess.Popen:
    script = tmp_path / "store_child.py"
    script.write_text(_CHILD, encoding="utf-8")
    return subprocess.Popen([sys.executable, str(script), *args], env=_child_env(tmp_path),
                            stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8")


class _Lines:
    """A child's stdout, read by a daemon thread, so a marker is awaited with a deadline and
    a child that stalls without printing never blocks the test before its cleanup."""

    def __init__(self, proc: subprocess.Popen):
        import queue

        self._proc = proc
        self._queue = queue.Queue()
        self._empty = queue.Empty
        threading.Thread(target=self._pump, args=(proc.stdout,), daemon=True).start()

    def _stderr_tail(self) -> str:
        """Ends the child and returns the end of what it wrote to stderr."""
        if self._proc.poll() is None:
            self._proc.kill()
        try:
            return (self._proc.communicate(timeout=10)[1] or "")[-2000:]
        except (subprocess.TimeoutExpired, ValueError, OSError):
            return ""

    def _pump(self, stream):
        for line in stream:
            self._queue.put(line.strip())
        self._queue.put(None)

    def expect(self, marker: str, seconds: float = 30.0) -> None:
        try:
            line = self._queue.get(timeout=seconds)
        except self._empty:
            raise AssertionError(f"no {marker!r} from the child within {seconds} s; stderr: "
                                 f"{self._stderr_tail()}") from None
        if line != marker:
            raise AssertionError(f"expected {marker!r}, got {line!r}; stderr: {self._stderr_tail()}")


def test_two_processes_lose_no_field(store, tmp_path):
    """Two processes, each a fresh manager per round with a pause between loading and
    writing, each owning one field of three tasks. Each round checks that its own last
    value is still on disk, and so does the end."""
    d = tmp_path / "procs"
    ids = [f"5707e10{i}" for i in range(3)]
    mgr = AutomationManager(storage_dir=str(d))
    for tid in ids:
        mgr.create(AutomationTask(id=tid, name="n0", description="d0", time="06:00"))
    procs = {field: _child(tmp_path, "race", str(d), field, ",".join(ids), "30", str(seed))
             for field, seed in (("name", 4501), ("description", 4502))}
    try:
        deadline = time.monotonic() + 60
        while not all((tmp_path / f"ready-{f}").exists() for f in procs):
            assert time.monotonic() < deadline, "children did not start"
            assert all(p.poll() is None for p in procs.values()), \
                [p.communicate()[1][-2000:] for p in procs.values() if p.poll() is not None]
            time.sleep(0.01)
        (tmp_path / "go").write_text("1")
        out = {}
        for field, proc in procs.items():
            stdout, stderr = proc.communicate(timeout=60)
            assert proc.returncode == 0, stderr[-2000:]
            out[field] = json.loads(stdout.strip().splitlines()[-1])
    finally:
        for proc in procs.values():
            if proc.poll() is None:
                proc.kill()
                proc.wait(5)
    for field, result in out.items():
        assert result["losses"] == 0, (field, result)
        for tid, value in result["last"].items():
            assert _disk(d, tid)[field] == value, (field, tid)


def test_a_lock_held_by_another_process_makes_update_wait_then_busy(one, tmp_path, monkeypatch):
    mgr = AutomationManager(storage_dir=str(one))       # loaded before the child's write
    proc = _child(tmp_path, "hold", str(one), TID, stdin=subprocess.PIPE)
    try:
        lines = _Lines(proc)
        lines.expect("held")

        monkeypatch.setattr(auto_mod, "_STORE_LOCK_TIMEOUT_S", 0.2)
        with pytest.raises(AutomationStoreBusy) as busy:
            mgr.update(TID, name="parent")
        assert str(busy.value) == "automation store is busy"
        assert str(one) not in str(busy.value)

        monkeypatch.setattr(auto_mod, "_STORE_LOCK_TIMEOUT_S", 5.0)
        box = {}

        def update():
            started = time.monotonic()
            box["task"] = mgr.update(TID, name="parent")
            box["waited"] = time.monotonic() - started

        waiter = threading.Thread(target=update, daemon=True)
        waiter.start()
        time.sleep(0.6)
        assert waiter.is_alive(), "the update did not wait for the other process's lock"
        proc.stdin.write("release\n")
        proc.stdin.flush()
        waiter.join(10)
        assert box.get("task") is not None and box["waited"] >= 0.5, box
        lines.expect("released")
        proc.wait(10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(5)
    record = _disk(one)
    assert record["name"] == "parent" and record["description"] == "from-child", record


# ── AC4: a run keeps the edits made while it ran ────────────────────────────

class _FakeAgent:
    tools = {}
    _current_username = "alice"
    _tool_authorizer = None
    during_run = None

    def __init__(self, *a, **k):
        self.history = []

    def load_model(self):
        pass

    def init_chat(self):
        self.history = []

    def chat_step(self, prompt, stream_callback=None, **k):
        type(self).during_run()
        if stream_callback:
            stream_callback("Done.")

    def _clean_reasoning(self, text):
        return text

    def _sanitize_response(self, text):
        return text

    def shutdown(self):
        pass


@pytest.mark.parametrize("lane", ["prompt", "workflow"])
def test_a_run_keeps_edits_made_while_it_ran(one, monkeypatch, lane):
    import vaf.core.agent as agent_mod
    import vaf.workflows.engine as engine_mod
    from types import SimpleNamespace

    from vaf.core.lock_manager import LockManager

    def edit_while_running():
        AutomationManager(storage_dir=str(one)).update(TID, time="09:15", enabled=False,
                                                       name="renamed mid-run")

    class _Engine:
        def __init__(self, *a, **k):
            pass

        def execute(self, steps, variables=None, check_stop=None, **k):
            edit_while_running()
            return SimpleNamespace(success=True, paused=False, final_output="", error=None)

    monkeypatch.setattr(_FakeAgent, "during_run", staticmethod(edit_while_running))
    monkeypatch.setattr(agent_mod, "Agent", _FakeAgent)
    monkeypatch.setattr(engine_mod, "WorkflowEngine", _Engine)
    monkeypatch.setattr(auto_mod, "bind_identity", lambda *a, **k: None)
    monkeypatch.setattr(auto_mod, "resolve_scope_identity", lambda *a, **k: None)
    monkeypatch.setattr(auto_mod, "_resolve_username", lambda scope: "alice")
    monkeypatch.setattr(auto_mod, "_push_result_to_web_ui", lambda *a, **k: None)
    monkeypatch.setattr("vaf.memory.rag.turn_memory_context", lambda *a, **k: "")
    monkeypatch.setattr(LockManager, "acquire", lambda lock_id: True)
    monkeypatch.setattr(LockManager, "release", lambda lock_id: None)

    scheduler = AutomationManager(storage_dir=str(one))
    if lane == "workflow":
        AutomationManager(storage_dir=str(one)).update(
            TID, workflow_steps=[{"tool": "write_file", "args": {"path": "r.md", "content": "x"}}])
        scheduler.reload_tasks()
    task = scheduler.get(TID)                            # captured when the run fires
    scheduler.run_task(task, new_terminal=False)
    record = _disk(one)
    assert record["time"] == "09:15" and record["enabled"] is False, record
    assert record["name"] == "renamed mid-run"
    assert record["last_run"] and record["last_completed_local_date"], record


def test_a_busy_store_after_a_one_time_run_keeps_the_runs_result(one, monkeypatch):
    """The run did its work; removing the spent one-time task meets a busy store. The run
    still answers with its result, not an error, and the agent is still shut down."""
    import vaf.core.agent as agent_mod

    from vaf.core.lock_manager import LockManager

    shutdowns = []

    class _Agent(_FakeAgent):
        def shutdown(self):
            shutdowns.append(1)

    def busy_delete(self, *a, **kw):
        raise AutomationStoreBusy("automation store is busy")

    monkeypatch.setattr(_FakeAgent, "during_run", staticmethod(lambda: None))
    monkeypatch.setattr(agent_mod, "Agent", _Agent)
    monkeypatch.setattr(auto_mod, "bind_identity", lambda *a, **k: None)
    monkeypatch.setattr(auto_mod, "resolve_scope_identity", lambda *a, **k: None)
    monkeypatch.setattr(auto_mod, "_resolve_username", lambda scope: "alice")
    monkeypatch.setattr(auto_mod, "_push_result_to_web_ui", lambda *a, **k: None)
    monkeypatch.setattr("vaf.memory.rag.turn_memory_context", lambda *a, **k: "")
    monkeypatch.setattr(LockManager, "acquire", lambda lock_id: True)
    monkeypatch.setattr(LockManager, "release", lambda lock_id: None)
    AutomationManager(storage_dir=str(one)).update(TID, frequency="once")
    scheduler = AutomationManager(storage_dir=str(one))
    monkeypatch.setattr(AutomationManager, "_delete", busy_delete)
    result = scheduler.run_task(scheduler.get(TID), new_terminal=False)
    assert not str(result).startswith("Error"), result
    assert shutdowns == [1, 1], "the agent was not shut down after the run's bookkeeping"
    assert _disk(one)["last_run"], "the run was not recorded"


# ── AC5, AC6: a task that is gone stays gone; a restore never overwrites ────

@pytest.mark.parametrize("permanent", [True, False], ids=["deleted", "trashed"])
def test_update_after_delete_does_not_resurrect(one, permanent):
    lane = AutomationManager(storage_dir=str(one))
    other = AutomationManager(storage_dir=str(one))
    assert other.delete(TID, permanent=permanent)
    trashed = one / "trash" / f"{TID}.json"
    before = trashed.read_bytes() if trashed.exists() else None
    assert lane.update(TID, name="after the delete") is None
    assert not (one / f"{TID}.json").exists(), "the deleted task is back"
    assert lane.get(TID) is None
    if not permanent:
        assert trashed.read_bytes() == before


def test_a_run_stamp_after_trash_does_not_resurrect(one, monkeypatch):
    monkeypatch.setattr(auto_mod, "_resolve_username", lambda scope: "alice")
    scheduler = AutomationManager(storage_dir=str(one))
    task = scheduler.get(TID)
    assert AutomationManager(storage_dir=str(one)).move_to_trash(TID)
    trashed = one / "trash" / f"{TID}.json"
    before = trashed.read_bytes()
    assert scheduler._record_successful_run(task) is None
    assert task.last_run, "the run's own copy is still stamped"
    assert not (one / f"{TID}.json").exists(), "the trashed task is back in the store"
    assert trashed.read_bytes() == before
    assert scheduler.get(TID) is None


def test_restore_never_overwrites_a_live_task(one):
    mgr = AutomationManager(storage_dir=str(one))
    assert mgr.move_to_trash(TID)
    AutomationManager(storage_dir=str(one)).create(AutomationTask(id=TID, name="live", time="08:00"))
    live = (one / f"{TID}.json").read_bytes()
    assert mgr.restore_from_trash(TID) is False
    assert (one / f"{TID}.json").read_bytes() == live
    assert (one / "trash" / f"{TID}.json").exists()


def _pause_after_release(monkeypatch, thread_name: str, released: threading.Event):
    """Every store lock taken by `thread_name` signals `released` right after it lets go,
    then pauses 0.3 s: the window between the lock and anything done after it."""
    real = auto_mod._acquire_store_lock

    def acquire(directory):
        release = real(directory)
        if threading.current_thread().name != thread_name:
            return release

        def release_then_pause():
            release()
            released.set()
            time.sleep(0.3)
        return release_then_pause

    monkeypatch.setattr(auto_mod, "_acquire_store_lock", acquire)


def _in_threads(*named):
    errors = []

    def run(fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - reported by the caller
            errors.append(repr(e))

    threads = [threading.Thread(target=run, args=(fn,), name=name, daemon=True)
               for name, fn in named]
    for t in threads:
        t.start()
    assert _join_all(threads, 10) == []
    return errors


def test_a_delete_between_an_update_and_its_cache_keeps_the_task_gone(one, monkeypatch):
    """One manager: an update writes and lets the lock go; a delete runs in that moment.
    The update's task must not come back into `get()`, the delete removed it from disk."""
    mgr = AutomationManager(storage_dir=str(one))
    released = threading.Event()
    _pause_after_release(monkeypatch, "updater", released)

    def delete():
        assert released.wait(5)
        assert mgr.delete(TID, permanent=True)

    errors = _in_threads(("updater", lambda: mgr.update(TID, name="x")), ("deleter", delete))
    assert errors == []
    assert not (one / f"{TID}.json").exists()
    assert mgr.get(TID) is None and TID not in [t.id for t in mgr.list()], \
        "the deleted task is back in the manager"


def test_a_create_between_a_delete_and_its_cache_stays_listed(one, monkeypatch):
    """The other order: a delete removes the file and lets the lock go; the same id is
    created on the same manager in that moment. The created task stays in `get()`."""
    mgr = AutomationManager(storage_dir=str(one))
    released = threading.Event()
    _pause_after_release(monkeypatch, "deleter", released)

    def create():
        assert released.wait(5)
        mgr.create(AutomationTask(id=TID, name="again", time="08:00"))

    errors = _in_threads(("deleter", lambda: mgr.delete(TID, permanent=True)),
                         ("creator", create))
    assert errors == []
    assert _disk(one)["name"] == "again"
    assert mgr.get(TID) is not None and mgr.get(TID).name == "again"


def test_an_incarnation_follows_edits_and_the_trash(one, monkeypatch):
    """Set once at create, kept by an update, by the trash and by a restore: a run stamp of
    the task it was loaded as still lands."""
    monkeypatch.setattr(auto_mod, "_resolve_username", lambda scope: "alice")
    incarnation = _disk(one)["incarnation"]
    assert isinstance(incarnation, str) and len(incarnation) == 12
    given = AutomationTask(id="5707e002", name="given", incarnation="given0000000")
    published = AutomationManager(storage_dir=str(one)).create(given)
    assert published.incarnation != "given0000000" and len(published.incarnation) == 12
    assert _disk(one, "5707e002")["incarnation"] == published.incarnation
    scheduler = AutomationManager(storage_dir=str(one))
    running = scheduler.get(TID)
    lane = AutomationManager(storage_dir=str(one))
    lane.update(TID, name="edited", enabled=False)
    assert lane.move_to_trash(TID) and lane.restore_from_trash(TID)
    assert _disk(one)["incarnation"] == incarnation
    assert scheduler._record_successful_run(running) is not None
    record = _disk(one)
    assert record["last_run"] and record["name"] == "edited" and record["enabled"] is False


def test_create_publishes_a_copy_and_leaves_the_callers_object(one, monkeypatch):
    """A task object a run holds is reused to create its replacement: the run's object keeps
    the incarnation it ran as, so its stamp still misses the replacement."""
    monkeypatch.setattr(auto_mod, "_resolve_username", lambda scope: "alice")
    mgr = AutomationManager(storage_dir=str(one))
    running = mgr.get(TID)
    held = running.incarnation
    assert AutomationManager(storage_dir=str(one)).delete(TID, permanent=True)
    published = AutomationManager(storage_dir=str(one)).create(running)
    assert running.incarnation == held, "create changed the object a run holds"
    assert published is not running and published.incarnation != held
    assert _disk(one)["incarnation"] == published.incarnation
    assert mgr._record_successful_run(running) is None
    assert _disk(one)["last_run"] is None


def test_a_record_without_an_incarnation_still_loads_and_takes_its_stamp(one, monkeypatch):
    """A file written before the field existed: no incarnation on disk, None when loaded,
    and two Nones are the same task, so its run is stamped as it always was."""
    monkeypatch.setattr(auto_mod, "_resolve_username", lambda scope: "alice")
    path = one / f"{TID}.json"
    legacy = json.loads(path.read_text(encoding="utf-8"))
    del legacy["incarnation"]
    path.write_text(json.dumps(legacy), encoding="utf-8")
    scheduler = AutomationManager(storage_dir=str(one))
    running = scheduler.get(TID)
    assert running.incarnation is None
    assert AutomationManager(storage_dir=str(one)).update(TID, name="edited").incarnation is None
    assert scheduler._record_successful_run(running) is not None
    record = _disk(one)
    assert record["last_run"] and "incarnation" in record and record["incarnation"] is None


def test_a_number_as_the_incarnation_still_names_its_own_task(one, monkeypatch):
    """A file holding its incarnation as a number (written by hand or by another program):
    the loaded task reads it as text, and its own run stamp and its one-time removal still
    find it, so a spent one-time task is not armed again on every refresh."""
    monkeypatch.setattr(auto_mod, "_resolve_username", lambda scope: "alice")
    path = one / f"{TID}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record.update(incarnation=12345, frequency="once")
    path.write_text(json.dumps(record), encoding="utf-8")
    scheduler = AutomationManager(storage_dir=str(one))
    running = scheduler.get(TID)
    assert running.incarnation == "12345"
    assert scheduler._record_successful_run(running) is not None
    assert _disk(one)["last_run"]
    # The stamp wrote the record back with the incarnation as text; the removal is asked
    # about a number on disk too.
    record = json.loads(path.read_text(encoding="utf-8"))
    record["incarnation"] = 12345
    path.write_text(json.dumps(record), encoding="utf-8")
    assert scheduler._remove_spent_once(running) is True
    assert not path.exists()


def test_a_run_of_a_deleted_task_leaves_the_task_recreated_under_its_id_alone(
        store, workspaces, monkeypatch):
    """A claimed create whose decision could not be saved leaves its task and its claim; the
    task runs, is deleted while it runs, and the next approve creates the planned id again.
    The old run ends: its stamp, and for a one-time task its removal, must not land on the
    new task, which has not run (measured before the fix: stamped, shown as done today)."""
    ws, tw = workspaces, workspaces.tw
    monkeypatch.setattr(auto_mod, "_resolve_username", lambda scope: "alice")
    hid = tw.create_handoff(TENANT_A, ws.task_a, "Create", "c", automation_action={
        "operation": "create", "prompt": "write the report", "time": "15:30",
        "frequency": "once"})["id"]
    real_save, failed = tw._save_decision, []

    def save_fails_once_approved(hpath, handoff):
        if handoff.get("status") == "approved" and not failed:
            failed.append(1)
            raise tw.HandoffNotSaved("handoff decision not saved")
        return real_save(hpath, handoff)

    monkeypatch.setattr(tw, "_save_decision", save_fails_once_approved)
    with pytest.raises(tw.HandoffNotSaved):
        tw.approve_handoff(TENANT_A, ws.task_a, hid)
    planned = _record(ws, hid)["claim"]["automation_id"]
    path = store / TENANT_A / f"{planned}.json"
    scheduler = AutomationManager()
    running = scheduler.get(planned)                     # the run fires
    assert AutomationManager(user_scope_id=TENANT_A).delete(planned, permanent=True)
    assert tw.approve_handoff(TENANT_A, ws.task_a, hid)  # creates the planned id again
    replacement = path.read_bytes()
    assert json.loads(replacement)["incarnation"] != running.incarnation

    assert scheduler._record_successful_run(running) is None
    assert scheduler._remove_spent_once(running) is False
    assert path.read_bytes() == replacement, "the old run wrote onto the new task"
    assert json.loads(replacement)["last_run"] is None


# ── AC7: the trigger cursor lands on the current record ─────────────────────

_TRIGGER = {"kind": "room_message", "room_id": "room-1", "match": "go"}


def _scheduler_with_a_hit(d: Path, newest: int):
    """A scheduler that loads the store now, and a watch whose next tick reports one hit
    (read up to `newest`) for the task it hands in."""
    from vaf.core.automation_triggers import TriggerHit

    class _Watch:
        def tick(self, tasks):
            from types import SimpleNamespace

            frame = SimpleNamespace(id="f-1", sender="p-x", kind="say", body={"text": "go"},
                                    reply_to=None, lamport=newest)
            return [TriggerHit(t, [frame], newest, {}) for t in tasks if t.id == "e7e00001"]

    return AutomationManager(storage_dir=str(d)), _Watch()


@pytest.fixture
def event_task(store, tmp_path):
    d = tmp_path / "events"
    AutomationManager(storage_dir=str(d)).create(AutomationTask(
        id="e7e00001", name="on deploy", prompt="p0", frequency="on_event", time="",
        trigger=dict(_TRIGGER)))
    return d


def _fire(scheduler, watch):
    started = []
    scheduler._trigger_watch = watch
    scheduler._run_scheduled_task = lambda task, *, trigger=None: started.append(task.id)
    assert scheduler._fire_room_triggers() == 1
    return started


def test_the_cursor_lands_on_the_current_record(event_task):
    scheduler, watch = _scheduler_with_a_hit(event_task, 42)
    AutomationManager(storage_dir=str(event_task)).update("e7e00001", prompt="p1")
    assert _fire(scheduler, watch) == ["e7e00001"]
    record = _disk(event_task, "e7e00001")
    assert record["prompt"] == "p1", "the prompt edited after the scheduler loaded it is lost"
    assert record["trigger"] == dict(_TRIGGER, cursor=42)


@pytest.mark.parametrize("changed", [{"room_id": "room-2"}, {"match": "stop"}],
                         ids=["room", "match"])
def test_a_changed_trigger_rule_gets_no_old_cursor(event_task, changed):
    """The hit matched the old rule; the edited rule was never asked about those frames.
    No cursor is written into it and the run does not start."""
    scheduler, watch = _scheduler_with_a_hit(event_task, 42)
    moved = dict(_TRIGGER, **changed)
    AutomationManager(storage_dir=str(event_task)).update("e7e00001", trigger=moved)
    started = []
    scheduler._trigger_watch = watch
    scheduler._run_scheduled_task = lambda task, *, trigger=None: started.append(task.id)
    assert scheduler._fire_room_triggers() == 0
    assert started == []
    assert _disk(event_task, "e7e00001")["trigger"] == moved


# ── Room triggers: a hit runs only once its cursor is on disk ───────────────
#
# A real `RoomTriggerWatch` over real rooms (membership, matching and re-reading are its
# own), with the scheduler's run replaced by a recorder.

EID = "e7e00001"


def _event(task_id: str, room_id: str = "room-1", match: str = "go", **kw) -> AutomationTask:
    base = dict(id=task_id, name="on room", prompt="p0", frequency="on_event", time="",
                user_scope_id=TENANT_A,
                trigger={"kind": "room_message", "room_id": room_id, "match": match})
    base.update(kw)
    return AutomationTask(**base)


@pytest.fixture
def rooms(store, tmp_path, monkeypatch):
    """room-1 and room-2, each with tenant A's person and agent and a guest who speaks;
    tenant A's event task EID watches room-1 for "go"."""
    import vaf.core.a2a.store as store_mod
    from types import SimpleNamespace

    from vaf.core.a2a.room import Room, derive_peer_id, participant_key

    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path / "rooms")
    (tmp_path / "rooms").mkdir()
    AutomationManager(user_scope_id=TENANT_A).create(_event(EID))
    return SimpleNamespace(dir=store / TENANT_A, **_two_rooms("room-1", "room-2"))


def _two_rooms(first: str, second: str) -> dict:
    """Two rooms, each with tenant A's person and agent and a guest who speaks."""
    from types import SimpleNamespace

    from vaf.core.a2a.room import Room, derive_peer_id, participant_key

    made = {}
    for name, room_id in (("r1", first), ("r2", second)):
        room = Room.create(kind="round", owner_scope=TENANT_A, room_id=room_id)
        owners = []
        for lane in ("cli", "agent"):
            key = participant_key(lane, TENANT_A)
            owners.append(room.join(display=f"Alice-{lane}", scope_id=TENANT_A,
                                    peer_id=derive_peer_id(key, room_id), participant_key=key))
        made[name] = SimpleNamespace(room=room, owners=owners, room_id=room_id,
                                     guest=room.join(display="Codex", scope_id=None,
                                                     peer_id="p-codex"))
    return made


@pytest.fixture
def busy(monkeypatch):
    """`busy.on = True` makes every store lock answer "busy" at once; `busy.tries` counts."""
    from types import SimpleNamespace

    real = auto_mod._acquire_store_lock
    state = SimpleNamespace(on=False, tries=0)

    def acquire(directory):
        if state.on:
            state.tries += 1
            raise AutomationStoreBusy("automation store is busy")
        return real(directory)

    monkeypatch.setattr(auto_mod, "_acquire_store_lock", acquire)
    return state


def _scheduler(monkeypatch, fail_first: bool = False):
    """A tenant-A scheduler whose runs are recorded as (task, [texts]). `fail_first`: the
    first run cannot be started."""
    import vaf.core.automation_triggers as triggers

    scheduler = AutomationManager(user_scope_id=TENANT_A)
    runs, calls = [], []

    def run(task, *, trigger=None):
        calls.append(task.id)
        if fail_first and len(calls) == 1:
            raise RuntimeError("thread could not start")
        runs.append((task, trigger))

    scheduler._run_scheduled_task = run
    monkeypatch.setattr(triggers, "trigger_context",
                        lambda t, frames, labels: [f.body.get("text") for f in frames])
    return scheduler, runs


def _tick(scheduler, busy=None, on=False) -> int:
    if busy is not None:
        busy.on = on
    try:
        return scheduler._fire_room_triggers()
    finally:
        if busy is not None:
            busy.on = False


def _texts(runs):
    return [texts for _task, texts in runs]


def _cursor(rooms, task_id=EID):
    return (_disk(rooms.dir, task_id).get("trigger") or {}).get("cursor")


def test_a_hit_runs_only_once_its_cursor_is_on_disk(rooms, busy, monkeypatch):
    """The store is busy for three ticks: nothing runs and no cursor is written, and the
    frames are not lost (the watch is rewound); the first free tick runs them once."""
    scheduler, runs = _scheduler(monkeypatch)
    assert _tick(scheduler) == 0, "arming"
    r1 = rooms.r1
    r1.room.say(r1.guest, "go one")
    last = r1.room.say(r1.guest, "go two")
    for _ in range(3):
        assert _tick(scheduler, busy, on=True) == 0
    assert runs == [] and _cursor(rooms) is None
    assert _tick(scheduler) == 1
    assert _texts(runs) == [["go one", "go two"]]
    assert _cursor(rooms) == last.lamport
    assert _tick(scheduler) == 0, "a committed hit ran twice"


@pytest.mark.parametrize("reload", [False, True], ids=["scheduler-stale", "scheduler-reloaded"])
def test_a_task_switched_off_while_its_hit_waited_does_not_run(rooms, busy, monkeypatch, reload):
    scheduler, runs = _scheduler(monkeypatch)
    _tick(scheduler)
    rooms.r1.room.say(rooms.r1.guest, "go one")
    _tick(scheduler, busy, on=True)
    AutomationManager(user_scope_id=TENANT_A).update(EID, enabled=False, prompt="NEW")
    if reload:
        scheduler.reload_tasks()
    assert _tick(scheduler) == 0 and runs == []
    assert _disk(rooms.dir, EID)["prompt"] == "NEW"


def test_a_changed_match_judges_the_frames_again(rooms, busy, monkeypatch):
    """The hit for "go one" waits; the rule becomes "stop": the next report is judged by
    the new rule, so "go one" never runs and "stop three" does."""
    scheduler, runs = _scheduler(monkeypatch)
    _tick(scheduler)
    rooms.r1.room.say(rooms.r1.guest, "go one")
    _tick(scheduler, busy, on=True)
    AutomationManager(user_scope_id=TENANT_A).update(
        EID, trigger={"kind": "room_message", "room_id": "room-1", "match": "stop"})
    scheduler.reload_tasks()
    rooms.r1.room.say(rooms.r1.guest, "stop three")
    assert _tick(scheduler) == 1
    assert _texts(runs) == [["stop three"]]


@pytest.mark.parametrize("move", ["room", "recreated", "recreated-same-rule"])
def test_a_waiting_hit_never_reaches_another_room_or_a_task_created_anew(rooms, busy,
                                                                         monkeypatch, move):
    """The hit for room-1's "go one" waits. Then the trigger moves to room-2, or the task is
    deleted and created anew (in room-2, or with the very same rule): room-1's frame, and
    anything said before the new task existed, run under none of them. What room-2 says
    afterwards runs. Room-1 has said more than room-2 by then: positions count per room, so
    a cursor carried over from room-1 would skip room-2's next frames."""
    scheduler, runs = _scheduler(monkeypatch)
    _tick(scheduler)
    for n in range(12):
        rooms.r1.room.say(rooms.r1.guest, f"idle {n}")
    assert _tick(scheduler) == 0, "idle frames match nothing; the watch reads past them"
    rooms.r1.room.say(rooms.r1.guest, "go one")
    _tick(scheduler, busy, on=True)
    editor = AutomationManager(user_scope_id=TENANT_A)
    room_id = "room-1" if move == "recreated-same-rule" else "room-2"
    if move == "room":
        editor.update(EID, trigger={"kind": "room_message", "room_id": "room-2", "match": "go"})
    else:
        assert editor.delete(EID, permanent=True)
        editor.create(_event(EID, room_id=room_id, prompt="p-new"))
    scheduler.reload_tasks()
    assert _tick(scheduler) == 0 and runs == [], "a frame said before the change ran"
    where = rooms.r1 if room_id == "room-1" else rooms.r2
    where.room.say(where.guest, "go later")
    assert _tick(scheduler) == 1
    assert _texts(runs) == [["go later"]]


def test_a_hit_runs_the_task_as_it_is_on_disk(rooms, monkeypatch):
    """The scheduler has not refreshed since the prompt was edited: the run gets the edited
    prompt, never the scheduler's older copy."""
    scheduler, runs = _scheduler(monkeypatch)
    _tick(scheduler)
    rooms.r1.room.say(rooms.r1.guest, "go one")
    AutomationManager(user_scope_id=TENANT_A).update(EID, prompt="EDITED")
    assert _tick(scheduler) == 1
    (task, texts), = runs
    assert task.prompt == "EDITED" and texts == ["go one"]


def test_a_run_that_cannot_start_leaves_the_other_hits_alone(rooms, monkeypatch):
    """Two tasks hit in one tick and the first run cannot be started: the second still
    starts, both cursors are written, and the next tick runs neither again."""
    AutomationManager(user_scope_id=TENANT_A).create(_event("e7e00002"))
    scheduler, runs = _scheduler(monkeypatch, fail_first=True)
    _tick(scheduler)
    said = rooms.r1.room.say(rooms.r1.guest, "go one")
    assert _tick(scheduler) == 1
    assert len(runs) == 1
    assert _cursor(rooms, EID) == said.lamport and _cursor(rooms, "e7e00002") == said.lamport
    assert _tick(scheduler) == 0 and len(runs) == 1


def test_a_waiting_hit_does_not_run_after_the_account_left_the_room(rooms, busy, monkeypatch):
    scheduler, runs = _scheduler(monkeypatch)
    _tick(scheduler)
    rooms.r1.room.say(rooms.r1.guest, "go one")
    _tick(scheduler, busy, on=True)
    for owner in rooms.r1.owners:
        rooms.r1.room.leave(owner)
    assert _tick(scheduler) == 0 and runs == []


@pytest.mark.parametrize("reload", [False, True], ids=["scheduler-stale", "scheduler-reloaded"])
def test_a_task_moved_to_a_clock_does_not_run_on_room_frames(rooms, busy, monkeypatch, reload):
    """The task became a daily one; its trigger dict is still on the record. A hit for it
    writes nothing and starts nothing."""
    scheduler, runs = _scheduler(monkeypatch)
    _tick(scheduler)
    rooms.r1.room.say(rooms.r1.guest, "go one")
    _tick(scheduler, busy, on=True)
    AutomationManager(user_scope_id=TENANT_A).update(EID, frequency="daily", time="09:00")
    if reload:
        scheduler.reload_tasks()
    assert _tick(scheduler) == 0 and runs == []
    assert _cursor(rooms) is None


def test_a_cursor_already_further_on_disk_is_kept_and_not_run_again(rooms, monkeypatch):
    """Another writer (another process) already read up to "go one": only "go two" runs,
    and a cursor that covers everything is neither moved back nor run."""
    scheduler, runs = _scheduler(monkeypatch)
    _tick(scheduler)
    r1 = rooms.r1
    first = r1.room.say(r1.guest, "go one")
    second = r1.room.say(r1.guest, "go two")
    rule = {"kind": "room_message", "room_id": "room-1", "match": "go"}
    editor = AutomationManager(user_scope_id=TENANT_A)
    editor.update(EID, trigger=dict(rule, cursor=first.lamport))
    assert _tick(scheduler) == 1
    assert _texts(runs) == [["go two"]] and _cursor(rooms) == second.lamport
    r1.room.say(r1.guest, "go three")
    ahead = second.lamport + 1000
    editor.update(EID, trigger=dict(rule, cursor=ahead))
    assert _tick(scheduler) == 0 and len(runs) == 1
    assert _cursor(rooms) == ahead, "a newer cursor was moved back"


def test_a_task_file_that_cannot_be_read_once_is_not_taken_for_gone(rooms, monkeypatch):
    """Reading the task file fails once while it is there (an I/O error): no run, no
    cursor, and the next tick runs the frames."""
    import builtins

    path = rooms.dir / f"{EID}.json"
    failing = []

    def open_(file, *a, **kw):
        if failing and Path(file) == path:
            failing.pop()
            raise OSError(5, "Input/output error", str(file))
        return builtins.open(file, *a, **kw)

    monkeypatch.setattr(auto_mod, "open", open_, raising=False)
    scheduler, runs = _scheduler(monkeypatch)
    _tick(scheduler)
    rooms.r1.room.say(rooms.r1.guest, "go one")
    failing.append(1)
    assert _tick(scheduler) == 0 and runs == [] and _cursor(rooms) is None
    assert not failing, "the read never failed"
    assert _tick(scheduler) == 1 and _texts(runs) == [["go one"]]


def test_one_busy_store_costs_one_wait_per_tick(rooms, monkeypatch):
    """Ten tasks of one account hit in the same tick while another writer holds that
    account's store: one wait for its lock, not ten; the next free tick runs all ten."""
    for n in range(2, 11):
        AutomationManager(user_scope_id=TENANT_A).create(_event(f"e7e0000{n:x}"))
    scheduler, runs = _scheduler(monkeypatch)
    _tick(scheduler)
    rooms.r1.room.say(rooms.r1.guest, "go one")
    real = auto_mod._acquire_store_lock
    tries = []
    monkeypatch.setattr(auto_mod, "_acquire_store_lock",
                        lambda directory: tries.append(1) or real(directory))
    monkeypatch.setattr(auto_mod, "_STORE_LOCK_TIMEOUT_S", 0.2)
    with _Held(rooms.dir):
        tries.clear()
        started = time.monotonic()
        assert _tick(scheduler) == 0
        took = time.monotonic() - started
    assert len(tries) == 1 and took < 0.6, (len(tries), round(took, 2))
    assert _tick(scheduler) == 10 and len(runs) == 10


def test_a_hit_of_a_deleted_task_misses_its_successor_with_the_same_rule(rooms, monkeypatch):
    """The scheduler still holds the old task when the id is deleted and created anew with
    an IDENTICAL rule: the old task's hit writes no cursor into the new task and starts no
    run."""
    scheduler, runs = _scheduler(monkeypatch)
    _tick(scheduler)
    rooms.r1.room.say(rooms.r1.guest, "go one")
    editor = AutomationManager(user_scope_id=TENANT_A)
    assert editor.delete(EID, permanent=True)
    editor.create(_event(EID, prompt="p-new"))
    assert _tick(scheduler) == 0 and runs == []
    assert _cursor(rooms) is None


def _room_trigger_draw(rooms, busy, monkeypatch, i: int, seed: int) -> list:
    import builtins

    rng = random.Random(seed)
    tid = f"e7f0{i:04x}"
    # Rooms of its own per draw: a transcript shared by every draw grows with the draw
    # count, and each tick opens it, so the cost would grow with the square of the draws.
    pair = _two_rooms(f"fz1-{i}", f"fz2-{i}")
    r1, r2 = pair["r1"], pair["r2"]
    rules = [(r1.room_id, "go"), (r1.room_id, "stop"), (r2.room_id, "go")]
    editor = lambda: AutomationManager(user_scope_id=TENANT_A)  # noqa: E731

    def make(rule, step):
        return _event(tid, room_id=rule[0], match=rule[1], prompt=f"p{step}")

    said, born, ran, bad = {}, {}, set(), []
    editor().create(make(rules[0], 0))
    born[_disk(rooms.dir, tid)["incarnation"]] = set()
    scheduler = AutomationManager(user_scope_id=TENANT_A)
    path = rooms.dir / f"{tid}.json"
    failing = []

    def open_(file, *a, **kw):
        if failing and Path(file) == path:
            failing.pop()
            raise OSError(5, "Input/output error", str(file))
        return builtins.open(file, *a, **kw)

    monkeypatch.setattr(auto_mod, "open", open_, raising=False)

    def run(task, *, trigger=None):
        record = _disk(rooms.dir, tid)
        rule = (record["trigger"]["room_id"], record["trigger"]["match"])
        if not record.get("enabled") or record.get("frequency") != "on_event":
            bad.append("ran a task that is off or not an event task")
        if (record.get("prompt"), record.get("incarnation")) != (task.prompt, task.incarnation):
            bad.append("ran a task that is not the one on disk")
        for frame in trigger:
            room, text = said.get(frame.id, ("?", ""))
            if room != rule[0] or rule[1] not in text:
                bad.append(f"{text!r} in {room} ran under {rule}")
            if frame.id in ran:
                bad.append(f"{text!r} ran twice")
            if frame.id in born.get(task.incarnation, set()):
                bad.append(f"{text!r} was said before its task existed")
            ran.add(frame.id)

    import vaf.core.automation_triggers as triggers
    monkeypatch.setattr(triggers, "trigger_context", lambda t, frames, labels: list(frames))
    scheduler._run_scheduled_task = run
    for step in range(1, rng.randint(10, 40)):
        op = rng.choice(("say1", "say1", "say2", "tick", "tick", "busy", "readfail", "rule",
                         "recreate", "off", "on", "prompt", "clock", "event", "reload"))
        if op in ("say1", "say2"):
            where = r1 if op == "say1" else r2
            room_id = where.room_id
            text = f"{rng.choice(('go', 'stop', 'idle'))} {step}"
            said[where.room.say(where.guest, text).id] = (room_id, text)
        elif op in ("tick", "busy"):
            _tick(scheduler, busy, on=op == "busy")
        elif op == "readfail":
            failing.append(1)
            _tick(scheduler)
            failing.clear()
        elif op == "rule":
            room_id, match = rng.choice(rules)
            editor().update(tid, trigger={"kind": "room_message", "room_id": room_id,
                                          "match": match})
        elif op == "recreate":
            editor().delete(tid, permanent=True)
            editor().create(make(rng.choice(rules), step))
            born[_disk(rooms.dir, tid)["incarnation"]] = set(said)
        elif op in ("off", "on"):
            editor().update(tid, enabled=op == "on")
        elif op == "prompt":
            editor().update(tid, prompt=f"p{step}")
        elif op == "clock":
            editor().update(tid, frequency="daily", time="09:00")
        elif op == "event":
            editor().update(tid, frequency="on_event", time="")
        else:
            scheduler.reload_tasks()
    _tick(scheduler)
    before = len(ran)
    _tick(scheduler)
    if len(ran) != before:
        bad.append("a free tick with nothing said ran frames again")
    editor().delete(tid, permanent=True)
    return bad


def test_room_trigger_fuzz(rooms, busy, monkeypatch):
    """Seeded single-thread schedules over one event task in two real rooms: frames said in
    either room, ticks with the store free or busy or the task file unreadable once, rule
    changes, recreates under the id, switches, clock and event frequency, prompt edits and
    scheduler refreshes. Oracle, at every run: the task is enabled and an event task on
    disk, the run carries the disk's prompt and incarnation, every frame it gets was said
    in the rule's room and matches it, was said after that task was created, and runs only
    once; a free tick with nothing new said runs nothing. `VAF_FUZZ_DRAWS` raises the
    draw count."""
    assert AutomationManager(user_scope_id=TENANT_A).delete(EID, permanent=True)
    draws = int(os.environ.get("VAF_FUZZ_DRAWS", "15"))
    for i in range(draws):
        seed = 4700 + i
        bad = _room_trigger_draw(rooms, busy, monkeypatch, i, seed)
        assert bad == [], f"seed {seed}: {bad}"


# ── AC8: identity and property keys are never applied ───────────────────────

def test_update_ignores_identity_and_property_keys(one):
    mgr = AutomationManager(storage_dir=str(one))
    incarnation = _disk(one)["incarnation"]
    task = mgr.update(TID, id="0ther001", user_scope_id="ab12cd34-0000-4000-8000-0000000000f1",
                      incarnation="f0f0f0f0f0f0", next_run_label="soon", name="kept")
    assert task is not None and task.id == TID and task.user_scope_id is None
    assert task.incarnation == incarnation and _disk(one)["incarnation"] == incarnation
    assert sorted(p.name for p in one.glob("*.json")) == [f"{TID}.json"]
    assert not [p for p in one.iterdir() if p.is_dir() and p.name != "trash"]
    record = _disk(one)
    assert record["id"] == TID and record["user_scope_id"] is None and record["name"] == "kept"


# ── AC9, AC10: no deadlock, and the guards that keep it so ──────────────────

def test_no_deadlock_between_handoff_scheduler_and_updates(store, workspaces, monkeypatch):
    """An approved handoff update (decision lock, then the store lock, then the scheduler
    refresh), scheduler refreshes that delete spent one-time tasks in the same directory
    (the scheduler's lock, then the store lock) and WebSocket-style updates, all at once.
    None may wait for a lock the other holds: each step finishes well inside the store
    lock's timeout."""
    ws = workspaces
    hid = ws.tw.create_handoff(TENANT_A, ws.task_a, "A handoff", "c", automation_action={
        "operation": "update", "task_id": A_ID, "name": "approved name"})["id"]
    stop = threading.Event()
    alive = threading.Thread(target=stop.wait, args=(30,), daemon=True)
    alive.start()
    scheduler = AutomationManager()
    scheduler._running = True
    scheduler._scheduler_thread = alive
    monkeypatch.setattr(auto_mod, "_scheduler_manager", scheduler)
    errors, slow = [], []

    def timed(label, fn):
        started = time.monotonic()
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - reported by the assert below
            errors.append((label, repr(e)))
        took = time.monotonic() - started
        if took > 2.0:
            slow.append((label, round(took, 2)))

    def refreshes():
        for i in range(20):
            _write(store, AutomationTask(id=f"0cce{i:04d}", name="spent", prompt="p",
                                         frequency="once", time="23:59",
                                         last_run="2026-01-01T00:00:00", user_scope_id=TENANT_A))
            timed("refresh", lambda: auto_mod.refresh_scheduler_from_disk(origin="test"))

    def updates():
        for i in range(20):
            def one_update(i=i):
                mgr, _task = AutomationAccess(TENANT_A, "user").get(A_ID)
                assert mgr.update(A_ID, description=f"ws-{i}") is not None
            timed("update", one_update)

    def approve():
        timed("approve", lambda: ws.tw.approve_handoff(TENANT_A, ws.task_a, hid))

    threads = [threading.Thread(target=f, daemon=True, name=f.__name__)
               for f in (refreshes, updates, approve)]
    try:
        for t in threads:
            t.start()
        assert _join_all(threads, 15) == [], "a lane is stuck"
    finally:
        stop.set()
    assert errors == [] and slow == [], (errors, slow)
    record = json.loads((store / TENANT_A / f"{A_ID}.json").read_text(encoding="utf-8"))
    assert record["name"] == "approved name" and record["description"] == "ws-19", record
    assert not list((store / TENANT_A).glob("0cce*.json")), "a spent one-time task was kept"


def _functions(tree):
    return {node.name: node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _call_name(call: ast.Call) -> str:
    func = call.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""


_NOT_UNDER_THE_STORE_LOCK = {"refresh_scheduler_from_disk", "ensure_scheduler_started",
                             "_sync_workspace_automation_state", "LockManager", "acquire",
                             "_decision_lock", "approve_handoff", "reject_handoff",
                             "sync_automation_status_to_workspace", "_schedule_task",
                             "reload_tasks", "start_scheduler"}


def test_the_store_lock_is_a_leaf():
    """No body that holds the store lock reaches a call that takes another lock, directly
    or through a function of this module it calls (resolved by bare name).

    It does NOT see a callable handed in as a value: the `change` that `_mutate_record` runs
    under the lock is a lambda or `_cursor_patch` at every call site today, each a dict
    update, but a new caller passing a function that takes a lock would pass this guard."""
    tree = ast.parse(_AUTOMATION_PY.read_text(encoding="utf-8"))
    functions = _functions(tree)
    locked = [node for node in ast.walk(tree) if isinstance(node, ast.With)
              and any(isinstance(item.context_expr, ast.Call)
                      and _call_name(item.context_expr) == "_store_lock" for item in node.items)]
    assert len(locked) >= 4, "the guard found too few locked blocks to mean anything"
    bad = []

    def visit(nodes, where, seen):
        for stmt in nodes:
            for node in ast.walk(stmt):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [getattr(node, "module", "") or ""] + [a.name for a in node.names]
                    if any("thinking_workspace" in n or n == "LockManager" for n in names):
                        bad.append((where, "import " + ", ".join(names)))
                if isinstance(node, ast.Name) and node.id == "LockManager":
                    bad.append((where, "LockManager"))
                if not isinstance(node, ast.Call):
                    continue
                name = _call_name(node)
                if name in _NOT_UNDER_THE_STORE_LOCK:
                    bad.append((where, name))
                elif name in functions and name not in seen:
                    visit(functions[name].body, f"{where} -> {name}", seen | {name})

    for block in locked:
        visit(block.body, f"line {block.lineno}", frozenset({"_store_lock"}))
    assert bad == []


def test_only_create_and_mutate_record_save_whole_tasks():
    tree = ast.parse(_AUTOMATION_PY.read_text(encoding="utf-8"))
    callers = []
    for func in _functions(tree).values():
        for node in ast.walk(func):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "_save_task":
                callers.append(func.name)
    assert callers and set(callers) <= {"create", "_mutate_record"}, callers


# ── AC11: a lock failure reaches no client with a path ──────────────────────

class _Held:
    """Hold one directory's store lock in another thread."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.held, self.done = threading.Event(), threading.Event()

    def _hold(self):
        with AutomationManager._store_lock(self.directory):
            self.held.set()
            self.done.wait(20)

    def __enter__(self):
        self.thread = threading.Thread(target=self._hold, daemon=True)
        self.thread.start()
        assert self.held.wait(10)
        return self

    def __exit__(self, *exc):
        self.done.set()
        self.thread.join(10)


def _upd(task_id, **fields):
    return ({"type": "update_automation", "task_id": task_id, **fields}, "update_automation_result")


def test_busy_store_answers_without_a_path(store, monkeypatch):
    import vaf.core.secure_store as secure_store

    monkeypatch.setattr(auto_mod, "_STORE_LOCK_TIMEOUT_S", 0.2)
    before = (store / TENANT_A / f"{A_ID}.json").read_bytes()
    with _Held(store / TENANT_A):
        busy = _ws(monkeypatch, role="user", scope=TENANT_A, frames=[_upd(A_ID, name="x")])[0]
    assert busy == {"type": "update_automation_result", "ok": False,
                    "error": "automation store is busy"}

    real_cls = secure_store._get_filelock_cls()

    class Unopenable:
        def __init__(self, path):
            self.path = path

        def acquire(self):
            raise PermissionError(13, "Permission denied", self.path)

        def release(self):
            pass

    def lock_for(path, timeout=-1, **kw):
        # Only the store lock fails: the handshake's own stores lock through the same accessor.
        if Path(path).name == ".automations.lock":
            return Unopenable(path)
        return real_cls(path, timeout=timeout, **kw)

    monkeypatch.setattr(secure_store, "_get_filelock_cls", lambda: lock_for)
    unopenable = _ws(monkeypatch, role="user", scope=TENANT_A, frames=[_upd(A_ID, name="x")])[0]
    assert unopenable == {"type": "update_automation_result", "ok": False,
                          "error": "automation store could not be locked"}
    for reply in (busy, unopenable):
        assert str(store) not in json.dumps(reply) and "Errno" not in json.dumps(reply)
    assert (store / TENANT_A / f"{A_ID}.json").read_bytes() == before


def test_a_busy_store_keeps_the_handoff_claim(store, workspaces, monkeypatch):
    ws = workspaces
    hid = ws.tw.create_handoff(TENANT_A, ws.task_a, "A handoff", "c", automation_action={
        "operation": "update", "task_id": A_ID, "name": "approved name"})["id"]
    monkeypatch.setattr(auto_mod, "_STORE_LOCK_TIMEOUT_S", 0.2)
    with _Held(store / TENANT_A):
        first = _ws(monkeypatch, role="user", scope=TENANT_A,
                    frames=[_handoff("approve", ws.task_a, hid)])[0]
    assert first["ok"] is False and first["error"] == "handoff decision not saved", first
    assert str(store) not in json.dumps(first) and "Errno" not in json.dumps(first)
    pending = _record(ws, hid)
    assert pending["status"] == "pending" and pending.get("claim"), pending
    second = _ws(monkeypatch, role="user", scope=TENANT_A,
                 frames=[_handoff("approve", ws.task_a, hid)])[0]
    assert second["ok"] is True, second
    record = json.loads((store / TENANT_A / f"{A_ID}.json").read_text(encoding="utf-8"))
    assert record["name"] == "approved name"
    assert _record(ws, hid)["status"] == "approved"


# ── Fuzz: seeded concurrent writers, run stamps and cursor patches ──────────

_FUZZ_FIELDS = ("name", "description", "prompt", "time", "output_path", "parameters")


def _fuzz_value(field: str, writer: int, rnd: int):
    return {"w": f"{writer}-{rnd}"} if field == "parameters" else f"{field}-{writer}-{rnd}"


def _fuzz_draw(d: Path, seed: int) -> list:
    """One draw; the list of oracle violations (empty when the invariant holds)."""
    rng = random.Random(seed)
    trigger = {"kind": "room_message", "room_id": "room-f", "match": "go"}
    AutomationManager(storage_dir=str(d)).create(AutomationTask(
        id=TID, name="n0", prompt="p0", frequency="on_event", time="", trigger=dict(trigger)))
    fields = list(_FUZZ_FIELDS)
    rng.shuffle(fields)
    writers = rng.randint(2, 4)
    owned = [fields[i::writers] for i in range(writers)]
    plans = [[rng.uniform(0, 0.002) for _ in range(rng.randint(1, 15))] for _ in range(writers)]
    # The scheduler's own writes, from the copy it captured before any writer ran.
    sched_ops = [(rng.choice(("stamp", "cursor")), rng.uniform(0, 0.002))
                 for _ in range(rng.randint(0, 6))]
    scheduler = AutomationManager(storage_dir=str(d))
    captured = scheduler.get(TID)
    expect_cursor = [None]
    # The task is never deleted in a draw, so every planned stamp must land.
    stamp_planned = any(op == "stamp" for op, _pause in sched_ops)
    errors = []
    barrier = threading.Barrier(writers + 1)

    def writer(w):
        try:
            barrier.wait(5)
            for rnd, pause in enumerate(plans[w]):
                mgr = AutomationManager(storage_dir=str(d))
                time.sleep(pause)
                mgr.update(TID, **{f: _fuzz_value(f, w, rnd) for f in owned[w]})
        except Exception as e:  # noqa: BLE001 - reported as a violation
            errors.append(f"writer {w}: {e!r}")

    def sched():
        try:
            barrier.wait(5)
            for n, (op, pause) in enumerate(sched_ops, 1):
                time.sleep(pause)
                if op == "stamp":
                    if scheduler._record_successful_run(captured) is None:
                        errors.append(f"scheduler: stamp {n} was not written")
                else:
                    scheduler._mutate_record(captured, auto_mod._cursor_patch(dict(trigger, cursor=n)))
                    expect_cursor[0] = n
        except Exception as e:  # noqa: BLE001 - reported as a violation
            errors.append(f"scheduler: {e!r}")

    threads = [threading.Thread(target=writer, args=(w,), daemon=True) for w in range(writers)]
    threads.append(threading.Thread(target=sched, daemon=True))
    for t in threads:
        t.start()
    stuck = _join_all(threads, 20)
    bad = list(errors) + [f"stuck: {stuck}"] * bool(stuck)
    try:
        record = _disk(d)
    except (OSError, ValueError) as e:
        return bad + [f"record unreadable: {e!r}"]
    for w in range(writers):
        for f in owned[w]:
            want = _fuzz_value(f, w, len(plans[w]) - 1)
            if record.get(f) != want:
                bad.append(f"{f}: {record.get(f)!r} != {want!r}")
    if stamp_planned and not record.get("last_run"):
        bad.append("the run stamp is lost")
    if not stamp_planned and record.get("last_run"):
        bad.append("a stamp nobody made")
    want_trigger = dict(trigger) if expect_cursor[0] is None else dict(trigger, cursor=expect_cursor[0])
    if record.get("trigger") != want_trigger:
        bad.append(f"trigger {record.get('trigger')!r} != {want_trigger!r}")
    if record.get("id") != TID or record.get("user_scope_id") is not None:
        bad.append("identity changed")
    names = sorted(p.name for p in d.iterdir() if p.is_file())
    if [n for n in names if n.endswith(".json")] != [f"{TID}.json"] \
            or [n for n in names if n.startswith(".tmp-")]:
        bad.append(f"store holds {names}")
    return bad


def test_concurrent_writers_fuzz(store, tmp_path, monkeypatch):
    """Seeded draws of 2-4 writers, each owning disjoint fields, with run stamps and cursor
    patches from a scheduler copy captured before they start. Oracle: every field ends at
    its owner's last value, a stamp and the last cursor are on disk, the identity is
    unchanged, and the directory holds the one task file and no temp file.
    `VAF_FUZZ_DRAWS` raises the draw count for a run outside the suite."""
    monkeypatch.setattr(auto_mod, "_resolve_username", lambda scope: "alice")
    draws = int(os.environ.get("VAF_FUZZ_DRAWS", "25"))
    for i in range(draws):
        seed = 4500 + i
        bad = _fuzz_draw(tmp_path / f"draw-{i}", seed)
        assert bad == [], f"seed {seed}: {bad}"
