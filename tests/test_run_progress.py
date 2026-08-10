# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""How far along a sub-agent run is, carried by a write that already happens.

WHAT WAS PLANNED AND DELIBERATELY NOT BUILT, so it is not re-litigated blind: a
`subagent_progress` event on the agent's event sink, plus a process-wide sink to
register for it. Three measurements killed it. In the shipped default a sub-agent
is a CHILD PROCESS constructed bare - no agent object, therefore no sink - and a
callable does not cross a process boundary. The consumer that motivated it, the
terminal task line, ALREADY polls the per-session IPC records every tick, so a
field on that record reaches it through a loop that runs anyway. And a process-
wide PUSH sink carries no identity: the two process-wide hooks this repo has
accepted are PULL resolvers that take a scope as an argument, while a push sink
cannot be filtered by whoever registered it - in a headless server that serves N
tenants as N THREADS in one process, that is a leak by construction.

THE TWO RULES THIS FILE PINS, because both are silent when broken:

1. The cell is armed ONLY inside a sub-agent child, keyed on
   `VAF_IN_SUBAGENT_TERMINAL`. A module-level cell is per-run only where the
   process IS the run. In the parent, two concurrent in-process runs would take
   turns overwriting one cell under two different users.
2. `None` is not `(0, 0)`. "This agent reports no progress" is a different answer
   from "0 of 0 planned", and only the second one is renderable. Three of the five
   sub-agents legitimately have no denominator.

AND THE ONE THE RECORD PINS: two integers, never a phase string. The natural phase
values are the coder's task title and the browser's next goal - model text derived
from the user's prompt - and `active_tasks.json` is one global file that the
runner's sub-agent loop reads UNFILTERED, attributing a session-less record to
whichever session the current worker serves. Integers carry no user content.
"""
import dataclasses

import pytest

from vaf.core.progress import read_run_progress, set_run_progress
from vaf.core.subagent_ipc import SubAgentTask


@pytest.fixture(autouse=True)
def _cell_reset(monkeypatch):
    """Save and restore the module cell; never bare-clear a process global."""
    import vaf.core.progress as progress

    previous = progress._run_progress
    progress._run_progress = None
    yield progress
    progress._run_progress = previous


@pytest.fixture
def in_child(monkeypatch):
    monkeypatch.setenv("VAF_IN_SUBAGENT_TERMINAL", "1")


# ── the child-only guard ─────────────────────────────────────────────────────

def test_the_cell_stays_dead_outside_a_sub_agent_child(monkeypatch) -> None:
    """In the parent, N tenants run as N threads in ONE process."""
    monkeypatch.delenv("VAF_IN_SUBAGENT_TERMINAL", raising=False)
    set_run_progress(2, 5)
    assert read_run_progress() is None


def test_the_task_id_variable_does_not_arm_the_cell(monkeypatch) -> None:
    """VAF_TASK_ID would be the wrong key: the workflow engine sets it in the
    PARENT, arming the cell in exactly the multi-tenant process the guard excludes."""
    monkeypatch.delenv("VAF_IN_SUBAGENT_TERMINAL", raising=False)
    monkeypatch.setenv("VAF_TASK_ID", "abc123")
    set_run_progress(2, 5)
    assert read_run_progress() is None


def test_a_child_parks_and_reads_back_its_counts(in_child) -> None:
    set_run_progress(2, 5)
    assert read_run_progress() == (2, 5)


# ── None is a real answer ────────────────────────────────────────────────────

def test_never_reported_is_none_not_zero_zero() -> None:
    """In a FRESH interpreter, so the fixture's reset cannot mask the declared
    initial value. A cell starting at (0, 0) would make every agent report "0/0"
    from the moment its record appears, including the three that never report."""
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    probe = "import vaf.core.progress as p; print(repr(p.read_run_progress()))"
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                         text=True, timeout=120, cwd=str(root),
                         env={**__import__("os").environ, "VAF_SKIP_DEP_CHECK": "1"})
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "None", (
        f"the module cell does not start empty: {out.stdout.strip()}"
    )


def test_reading_does_not_consume(in_child) -> None:
    """The heartbeat pulses every 3s and a producer may go minutes without a
    change; take-semantics would blank the record in between and the display
    would flicker back to nothing."""
    set_run_progress(1, 3)
    assert read_run_progress() == (1, 3)
    assert read_run_progress() == (1, 3)


# ── coercion at the boundary ─────────────────────────────────────────────────

def test_counts_are_coerced_to_int(in_child) -> None:
    """The value crosses a JSON boundary into a file several readers parse."""
    set_run_progress("3", 7.0)
    assert read_run_progress() == (3, 7)


def test_a_negative_count_floors_at_zero(in_child) -> None:
    set_run_progress(-1, -5)
    assert read_run_progress() == (0, 0)


def test_an_uncoercible_count_is_refused_rather_than_parked(in_child) -> None:
    """Never raises: producers call this from inside their own loops."""
    set_run_progress(1, 4)
    set_run_progress(None, "many")
    assert read_run_progress() == (1, 4), "junk overwrote a good value"


# ── the record ───────────────────────────────────────────────────────────────

def test_the_record_carries_counts_and_no_text_field() -> None:
    """The leak ratchet. A phase string here would be user-derived model text on a
    file that is read unfiltered and mis-attributed when a record has no session."""
    names = {f.name for f in dataclasses.fields(SubAgentTask)}
    progress_fields = {n for n in names if n.startswith("progress")}
    assert progress_fields == {"progress_done", "progress_total"}
    for field in dataclasses.fields(SubAgentTask):
        if field.name.startswith("progress"):
            assert field.default is None


def test_a_record_written_before_the_fields_existed_still_loads() -> None:
    old = {"task_id": "t1", "agent_type": "coding_agent", "task_description": "x",
           "status": "running", "created_at": "2026-08-04T10:00:00"}
    task = SubAgentTask.from_dict(old)
    assert task.progress_done is None and task.progress_total is None
    assert task.session_id is None and task.last_heartbeat is None


def test_an_unknown_key_does_not_raise() -> None:
    """The forward-compat defect this round fixes. Every reader builds its list in
    a comprehension inside a caller that swallows broadly, so one record from a
    newer build did not skip one record - it emptied the whole queue, silently."""
    future = {"task_id": "t1", "agent_type": "coding_agent", "task_description": "x",
              "status": "running", "created_at": "2026-08-04T10:00:00",
              "a_field_from_a_later_build": 42}
    task = SubAgentTask.from_dict(future)
    assert task.task_id == "t1"


def test_one_future_record_cannot_empty_the_whole_queue() -> None:
    """The consequence, at the level it actually bites."""
    records = [
        {"task_id": "good", "agent_type": "coding_agent", "task_description": "x",
         "status": "running", "created_at": "2026-08-04T10:00:00"},
        {"task_id": "future", "agent_type": "coding_agent", "task_description": "y",
         "status": "running", "created_at": "2026-08-04T10:00:00", "unknown": 1},
    ]
    tasks = [SubAgentTask.from_dict(r) for r in records]
    assert [t.task_id for t in tasks] == ["good", "future"]


# ── the coder's counts and its display string cannot disagree ────────────────

def _task_manager(statuses):
    from types import SimpleNamespace

    from vaf.tools.coder import TaskManager

    mgr = TaskManager.__new__(TaskManager)
    mgr.state = SimpleNamespace(
        tasks=[SimpleNamespace(status=s) for s in statuses]) if statuses is not None else None
    return mgr


def test_no_plan_reports_zero_total_and_not_zero_percent() -> None:
    mgr = _task_manager(None)
    assert mgr.progress_counts() == (0, 0)
    assert mgr.get_progress() == "Planning..."


def test_a_failed_task_leaves_done_below_total() -> None:
    """`done == total` is NOT completion: the terminal set is completed, failed and
    skipped, while only `completed` counts. Completion is the record LEAVING the
    active file, never the counts meeting."""
    mgr = _task_manager(["completed", "failed", "completed"])
    assert mgr.progress_counts() == (2, 3)


def test_the_counts_and_the_display_string_cannot_disagree() -> None:
    """Proves the deduplication, not just its result: the string is now a consumer
    of the counts rather than a second computation of them.

    The status set deliberately contains a FAILED task, which is where two
    independent computations drift: one counting terminal tasks and one counting
    completed ones agree on every other input.
    """
    mgr = _task_manager(["completed", "failed", "pending", "skipped"])
    done, total = mgr.progress_counts()
    assert (done, total) == (1, 4)
    assert mgr.get_progress() == f"Task {done}/{total}"
    assert mgr.get_progress() == "Task 1/4"


# ── who reports, and who deliberately does not ───────────────────────────────

def test_only_the_two_agents_with_a_denominator_report() -> None:
    """Three of five have no total to report and must not invent one: research
    plans as it goes, the librarian has no work unit, and the browser's max_steps
    is a ceiling rather than a plan - a fraction against a ceiling under-reports
    and then jumps to done, which is a worse lie than an empty column."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    reporting = {"coder.py", "document_agent.py"}
    silent = {"research_agent.py", "librarian.py", "browser_agent.py"}
    for name in reporting:
        src = (root / "vaf" / "tools" / name).read_text(encoding="utf-8")
        assert "set_run_progress(" in src, f"{name} stopped reporting progress"
    for name in silent:
        src = (root / "vaf" / "tools" / name).read_text(encoding="utf-8")
        assert "set_run_progress(" not in src, (
            f"{name} started reporting progress; it has no denominator, so the "
            "named boundary in its round needs re-arguing first"
        )


@pytest.fixture
def store(tmp_path):
    """A real IPC store on a scratch directory - the counts are only meaningful
    after a round trip through the file the parent actually reads."""
    from vaf.core.subagent_ipc import SubAgentIPC

    q = SubAgentIPC()
    q.queue_dir = tmp_path
    q.pending_file = tmp_path / "pending_tasks.json"
    q.results_file = tmp_path / "completed_results.json"
    q.active_file = tmp_path / "active_tasks.json"
    q.paused_workflows_file = tmp_path / "paused_workflows.json"
    q.task_payloads_dir = tmp_path / "task_payloads"
    q._mutation_lock_file = tmp_path / ".mutation.lock"
    for f in (q.pending_file, q.results_file, q.active_file, q.paused_workflows_file):
        f.write_text("[]", encoding="utf-8")
    q.active_file.write_text(
        '[{"task_id": "t1", "agent_type": "coding_agent", "task_description": "x",'
        ' "status": "running", "created_at": "2026-08-04T10:00:00"}]', encoding="utf-8")
    return q


def _active(store):
    import json

    from vaf.core import data_files
    return json.loads(data_files.read_bytes(store.active_file).decode("utf-8"))[0]


def test_the_heartbeat_stamps_both_counts_or_neither(store) -> None:
    """A record carrying done from this pulse and total from the last would render
    a ratio that never existed."""
    store.update_heartbeat("t1", progress=(2, 5))
    row = _active(store)
    assert (row["progress_done"], row["progress_total"]) == (2, 5)

    store.update_heartbeat("t1", progress=(3, 5))
    row = _active(store)
    assert (row["progress_done"], row["progress_total"]) == (3, 5)


def test_a_heartbeat_without_counts_leaves_them_alone(store) -> None:
    """None means "this agent does not report progress". A writer that translated
    it into (None, None) would let any pulse blank a child's counts."""
    store.update_heartbeat("t1", progress=(2, 5))
    store.update_heartbeat("t1")
    row = _active(store)
    assert (row["progress_done"], row["progress_total"]) == (2, 5)


def test_the_counts_survive_the_round_trip_into_a_record(store) -> None:
    store.update_heartbeat("t1", progress=(4, 9))
    task = store.get_active_tasks()[0]
    assert (task.progress_done, task.progress_total) == (4, 9)


def test_the_heartbeat_is_the_only_writer_of_the_counts() -> None:
    """The whole cost argument: the counts ride a write that already happens. A
    second writer at a producer's loop speed would multiply the mutation rate on a
    shared file whose guard degrades to an unlocked read-modify-write."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    src = (root / "vaf" / "core" / "subagent_ipc.py").read_text(encoding="utf-8")
    assert src.count("task['progress_done']") == 1
    assert "def update_heartbeat(self, task_id: str, progress" in src
