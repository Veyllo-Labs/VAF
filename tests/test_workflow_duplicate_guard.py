# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The workflow duplicate-run guard must be able to see the runs it guards.

Adversarial-review finding (audit of fbf9250..HEAD): execute_workflow's guard
read the IPC active-task registry, but the tool itself never wrote to it - only
the unrelated async-terminal lane in agent.py registers workflow:<id> tasks. So
two concurrent execute_workflow calls (worker threads / snapshot-reset model)
both sailed past the guard, verified live with a background-thread repro.

Fixes pinned here:
- SubAgentIPC.has_live_task: the ONE shared guard predicate for both lanes.
  It counts RUNNING tasks and *young* PENDING tasks (create_task ->
  mark_task_running spans terminal spawn + Python import; an active-only check
  was blind for that whole window), never stale pending garbage, and never
  checks globally without a session (Rule 4.4).
- ExecuteWorkflowTool registers ITSELF (create_task + mark_task_running) for
  the duration of the run and deregisters on every exit path.
"""
import threading
import time
import types
from datetime import datetime, timedelta

import pytest

from vaf.core.subagent_ipc import SubAgentIPC

SESSION = "sess-guard-test"


@pytest.fixture()
def ipc(tmp_path):
    """A SubAgentIPC with all queue files repointed to an isolated tmp dir."""
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
    return q


# ── has_live_task: the shared predicate ────────────────────────────────────

def test_running_task_of_same_type_and_session_is_live(ipc):
    tid = ipc.create_task("workflow:research", "x", session_id=SESSION)
    ipc.mark_task_running(tid)
    assert ipc.has_live_task("workflow:research", SESSION) is True


def test_other_type_or_other_session_is_not_live(ipc):
    tid = ipc.create_task("workflow:research", "x", session_id=SESSION)
    ipc.mark_task_running(tid)
    assert ipc.has_live_task("workflow:other", SESSION) is False
    assert ipc.has_live_task("workflow:research", "sess-someone-else") is False


def test_no_session_never_matches_globally(ipc):
    """Without a session the guard must NOT fall back to a global check - it
    could then block on ANOTHER user's run (cross-user coupling, Rule 4.4)."""
    tid = ipc.create_task("workflow:research", "x", session_id=SESSION)
    ipc.mark_task_running(tid)
    assert ipc.has_live_task("workflow:research", None) is False
    assert ipc.has_live_task("workflow:research", "") is False


def test_young_pending_task_counts_as_live(ipc):
    """The spawn race: create_task done, terminal/import still starting, so
    mark_task_running has not fired yet. A second identical launch in that
    window must still be blocked."""
    ipc.create_task("workflow:research", "x", session_id=SESSION)
    assert ipc.has_live_task("workflow:research", SESSION) is True


def test_stale_pending_task_does_not_wedge_future_launches(ipc):
    """A crashed spawn leaves its pending entry forever (only ACTIVE tasks are
    reaped by cleanup_stale_active_tasks) - it must age out of the guard."""
    ipc.create_task("workflow:research", "x", session_id=SESSION)
    pending = ipc._read_json(ipc.pending_file)
    pending[0]["created_at"] = (datetime.now() - timedelta(minutes=10)).isoformat()
    ipc._write_json(ipc.pending_file, pending)
    assert ipc.has_live_task("workflow:research", SESSION) is False


def test_cancel_task_clears_liveness(ipc):
    tid = ipc.create_task("workflow:research", "x", session_id=SESSION)
    ipc.mark_task_running(tid)
    assert ipc.has_live_task("workflow:research", SESSION) is True
    ipc.cancel_task(tid)
    assert ipc.has_live_task("workflow:research", SESSION) is False


# ── ExecuteWorkflowTool: self-registration end to end ──────────────────────

class _FakeStep:
    tool = "dummy_tool"
    description = "a step"


class _FakeResult:
    success = True
    final_output = "done"
    error = None
    paused = False


def _fake_engine_factory(release: threading.Event):
    release.entries = []  # one entry per engine.execute call (concurrency probe)

    class _FakeEngine:
        def __init__(self, tools=None, callback=None, **kw):
            pass

        def execute(self, steps, variables=None, check_stop=None, **kw):
            release.entries.append(1)
            release.wait(timeout=15)
            return _FakeResult()

    return _FakeEngine


@pytest.fixture()
def wf_tool(ipc, monkeypatch):
    """ExecuteWorkflowTool wired to the isolated IPC and a fake engine whose
    execution blocks until the returned event is set."""
    import vaf.core.subagent_ipc as ipc_mod
    import vaf.workflows.templates as templates_mod
    import vaf.workflows.engine as engine_mod
    import vaf.workflows.tool_overlay as overlay_mod

    release = threading.Event()
    monkeypatch.setattr(ipc_mod, "get_ipc", lambda: ipc)
    monkeypatch.setattr(templates_mod, "get_template",
                        lambda wid: {"name": "Research", "variables": {}, "defaults": {}}
                        if wid == "research" else None)
    monkeypatch.setattr(templates_mod, "list_templates",
                        lambda: [{"id": "research", "description": "d"}])
    monkeypatch.setattr(engine_mod, "create_workflow", lambda template: [_FakeStep()])
    monkeypatch.setattr(engine_mod, "WorkflowEngine", _fake_engine_factory(release))
    monkeypatch.setattr(overlay_mod, "workflow_primitives", lambda: {"dummy_tool": object()})

    from vaf.tools.workflow_executor import ExecuteWorkflowTool
    agent = types.SimpleNamespace(current_session_id=SESSION, tools={})
    return ExecuteWorkflowTool(), agent, release


def _wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_concurrent_second_call_is_blocked_and_guard_clears_after(wf_tool, ipc):
    tool, agent, release = wf_tool
    first = {}

    def _run_first():
        first["result"] = tool.run(workflow_id="research", _agent=agent)

    t = threading.Thread(target=_run_first, daemon=True)
    t.start()
    try:
        # The run must become VISIBLE in the registry while it executes -
        # exactly what the old code never did.
        assert _wait_for(lambda: ipc.has_live_task("workflow:research", SESSION)), \
            "run() never registered itself in the IPC registry"

        second = tool.run(workflow_id="research", _agent=agent)
        assert "ALREADY RUNNING" in second
    finally:
        release.set()
        t.join(timeout=15)

    assert t.is_alive() is False
    assert "completed successfully" in first["result"]
    # Deregistered on exit: the finished run must not block the next one.
    assert ipc.has_live_task("workflow:research", SESSION) is False
    third = tool.run(workflow_id="research", _agent=agent)
    assert "completed successfully" in third


def test_registration_skipped_without_session_but_run_still_works(wf_tool, ipc, monkeypatch):
    tool, _agent, release = wf_tool
    import vaf.core.subagent_ipc as ipc_mod
    monkeypatch.setattr(ipc_mod, "get_current_session_id", lambda: None)
    release.set()
    agent = types.SimpleNamespace(current_session_id=None, tools={})
    result = tool.run(workflow_id="research", _agent=agent)
    assert "completed successfully" in result
    assert ipc.get_active_tasks() == []
    assert ipc.get_pending_tasks() == []


def test_two_simultaneous_calls_exactly_one_executes(wf_tool, ipc):
    """Adversarial-review finding on the first version of this guard: the
    pre-check is check-then-act (template/variable resolution sits between it
    and the registration), so two barrier-synced concurrent calls BOTH sailed
    past it and both executed. Register-then-verify (claim_task_slot) now
    lets exactly one racer proceed."""
    tool, agent, release = wf_tool
    barrier = threading.Barrier(2)
    results = [None, None]

    def _call(i):
        barrier.wait()
        results[i] = tool.run(workflow_id="research", _agent=agent)

    threads = [threading.Thread(target=_call, args=(i,), daemon=True) for i in range(2)]
    for t in threads:
        t.start()
    try:
        assert _wait_for(lambda: len(release.entries) >= 1)
    finally:
        release.set()
        for t in threads:
            t.join(timeout=15)

    assert all(results)
    assert sum("completed successfully" in r for r in results) == 1
    assert sum("ALREADY RUNNING" in r for r in results) == 1
    assert len(release.entries) == 1  # the engine ran exactly once


def test_registration_heartbeats_so_the_zombie_reaper_keeps_it(wf_tool, ipc, monkeypatch):
    """Adversarial-review finding: the first version registered WITHOUT a
    heartbeat, so check_zombies (fired ~1s by the runner drain, timeout 90s)
    reaped the registration mid-run - reopening the duplicate guard for
    exactly the multi-minute runs it protects and enqueueing a spurious
    CRASH DETECTED result. The registration must heartbeat like the terminal
    lane does."""
    import vaf.tools.workflow_executor as wx

    monkeypatch.setattr(wx, "_HEARTBEAT_INTERVAL_S", 0.05)
    tool, agent, release = wf_tool
    first = {}

    def _run_first():
        first["result"] = tool.run(workflow_id="research", _agent=agent)

    t = threading.Thread(target=_run_first, daemon=True)
    t.start()
    try:
        assert _wait_for(lambda: ipc.has_live_task("workflow:research", SESSION))

        # Backdate created_at far into the past: WITHOUT a live heartbeat the
        # reaper would fail this task instantly. The heartbeat thread may
        # rewrite the file concurrently, so keep re-applying the backdate
        # until both it and a fresh last_heartbeat are visible together.
        def _backdated_and_heartbeaten():
            tasks = ipc._read_json(ipc.active_file)
            if not tasks:
                return False
            task = tasks[0]
            if not str(task.get("created_at", "")).startswith("2000-"):
                task["created_at"] = "2000-01-01T00:00:00"
                ipc._write_json(ipc.active_file, tasks)
                return False
            return bool(task.get("last_heartbeat"))

        assert _wait_for(_backdated_and_heartbeaten), "heartbeat never appeared"

        ipc.check_zombies(timeout_seconds=90)
        assert ipc.has_live_task("workflow:research", SESSION) is True  # survived
        assert ipc.get_pending_results(session_id=SESSION) == []  # no CRASH result
    finally:
        release.set()
        t.join(timeout=15)

    assert "completed successfully" in first["result"]


def test_claim_task_slot_single_registrant_wins(ipc):
    tid = ipc.create_task("workflow:research", "x", session_id=SESSION)
    ipc.mark_task_running(tid)
    assert ipc.claim_task_slot(tid, "workflow:research", SESSION) is True


def test_claim_task_slot_exactly_one_of_two_racers_wins(ipc):
    t1 = ipc.create_task("workflow:research", "x", session_id=SESSION)
    ipc.mark_task_running(t1)
    t2 = ipc.create_task("workflow:research", "x", session_id=SESSION)
    ipc.mark_task_running(t2)
    c1 = ipc.claim_task_slot(t1, "workflow:research", SESSION)
    c2 = ipc.claim_task_slot(t2, "workflow:research", SESSION)
    assert [c1, c2].count(True) == 1
    assert c1 is True  # deterministic: the earlier registration wins


def test_claim_task_slot_withdraws_when_own_entry_is_missing(ipc):
    """A missing OWN entry this soon after registering means a concurrent
    racer's read-modify-write clobbered it (lost update, seen under real
    full-suite load) - the claim must WITHDRAW, never proceed: a fail-open
    here let both racers run (the racer whose entry survived wins)."""
    assert ipc.claim_task_slot("ghost-id", "workflow:research", SESSION) is False


def test_stop_all_during_run_leaves_no_phantom_result(wf_tool, ipc):
    """A stop-all press while execute_workflow runs fail_task's the live
    registration into the RESULTS queue. This lane returns its result
    synchronously as the tool result - a queued result for its task id would
    be a phantom second delivery via the drain. The finally must consume it."""
    tool, agent, release = wf_tool
    first = {}

    def _run_first():
        first["result"] = tool.run(workflow_id="research", _agent=agent)

    t = threading.Thread(target=_run_first, daemon=True)
    t.start()
    try:
        # Wait for the task to be genuinely ACTIVE, not merely has_live_task:
        # the latter is also True during the young-PENDING window (create_task
        # done, mark_task_running not yet), where get_active_tasks - which the
        # stop-all handler below iterates, exactly like the WS handler in prod -
        # is still empty. On slower file I/O (Windows CI) that window is wide
        # enough to catch, so the fail_task loop would run over nothing and no
        # phantom would be produced. Waiting on active_tasks is deterministic.
        assert _wait_for(lambda: ipc.get_active_tasks(session_id=SESSION))
        # Simulate the WS stop-all handler: fail every active task of the session.
        for task in ipc.get_active_tasks(session_id=SESSION):
            ipc.fail_task(task.task_id, "[USER_CANCELLED] Stopped by user.")
        assert ipc.get_pending_results(session_id=SESSION)  # phantom exists now
    finally:
        release.set()
        t.join(timeout=15)

    assert "completed successfully" in first["result"]
    assert ipc.get_pending_results(session_id=SESSION) == []  # phantom consumed


def test_own_tool_name_as_workflow_id_gets_a_non_circular_message(wf_tool):
    tool, agent, release = wf_tool
    release.set()
    agent.tools = {"execute_workflow": object(), "create_agent_workflow": object()}
    msg = tool.run(workflow_id="execute_workflow", _agent=agent)
    assert "this tool itself" in msg
    assert "call the 'execute_workflow' tool directly" not in msg.lower()
    # The genuinely-useful redirect for OTHER tool names stays intact.
    msg2 = tool.run(workflow_id="create_agent_workflow", _agent=agent)
    assert "run_temp" in msg2


def test_wrong_wrapper_with_full_payload_echoes_a_copyable_call(wf_tool):
    """Live incident: the model merged the suggestion's execute_workflow(...)
    with the run_temp advisory into execute_workflow(workflow_id=
    'create_agent_workflow', variables={action, steps}) - a CORRECT payload in
    the wrong wrapper. After a prose-only redirect it gave up on workflows and
    did every step manually. The redirect must hand back the exact call to
    copy (weak models copy reliably, they do not rephrase)."""
    tool, agent, release = wf_tool
    release.set()
    agent.tools = {"create_agent_workflow": object()}
    payload = {
        "action": "run_temp",
        "steps": [
            {"name": "Wetter Berlin suchen", "tool": "web_search",
             "params": {"query": "wetter Berlin heute"}},
            {"name": "New York News suchen", "tool": "web_search",
             "params": {"query": "latest news New York today"}},
        ],
    }
    msg = tool.run(workflow_id="create_agent_workflow", variables=payload, _agent=agent)
    assert "Copy this call exactly" in msg
    assert 'create_agent_workflow(action="run_temp", steps=[' in msg
    assert "wetter Berlin heute" in msg  # THEIR steps, echoed verbatim
    assert "do NOT fall" in msg  # forbids the manual-fallback escape hatch


def test_wrong_wrapper_payload_as_json_string_still_echoes(wf_tool):
    import json

    tool, agent, release = wf_tool
    release.set()
    agent.tools = {"create_agent_workflow": object()}
    payload = json.dumps({"steps": [{"tool": "web_search", "params": {"query": "x"}}]})
    msg = tool.run(workflow_id="create_agent_workflow", variables=payload, _agent=agent)
    assert "Copy this call exactly" in msg
    # action defaulted in for a payload that lacked it
    assert 'action="run_temp"' in msg


def test_wrong_wrapper_without_steps_falls_back_to_generic_redirect(wf_tool):
    tool, agent, release = wf_tool
    release.set()
    agent.tools = {"create_agent_workflow": object()}
    msg = tool.run(workflow_id="create_agent_workflow",
                   variables={"topic": "wetter"}, _agent=agent)
    assert "Copy this call exactly" not in msg
    assert "run_temp" in msg  # the generic advice still points the right way


def test_wrong_wrapper_oversized_payload_falls_back(wf_tool):
    tool, agent, release = wf_tool
    release.set()
    agent.tools = {"create_agent_workflow": object()}
    huge = {"steps": [{"tool": "web_search", "params": {"query": "q" * 5000}}]}
    msg = tool.run(workflow_id="create_agent_workflow", variables=huge, _agent=agent)
    assert "Copy this call exactly" not in msg
    assert "run_temp" in msg
