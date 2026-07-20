# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A paused workflow is STILL RUNNING and must never be reported as a failure.

Live incident 2026-07-20: a two-step template handed its second step to an async
sub-agent. The engine returned WorkflowResult(success=False, error=None, paused=True) -
its own comment reads `success=False,  # Not complete yet` - and the consumer branched
only on `result.success`, so the user was told:

    Workflow 'Research & Document' failed: None

("None" because `error` is None precisely when nothing failed.) The agent then apologized
to the user for a crash while the document was still being written, and the false FAILED
verdict was persisted into the session's turn summary.

The engine had six consumers and three of them had never learned that there is a third
outcome. That is the Rule-2 missing-copy pattern, so this guard pins the contract for all of
them at once and fails when a new lane appears without a paused branch.

CLAUDE.md prefers a CI guard over a prose rule; this is that guard.
"""
import ast
import threading
from pathlib import Path

import pytest

from vaf.core.subagent_ipc import PausedWorkflow, SubAgentIPC
from vaf.workflows.engine import (
    SPAWNABLE_STEP_TOOLS,
    SUBAGENT_STEP_TOOLS,
    WorkflowResult,
    paused_tool_message,
)

_REPO = Path(__file__).resolve().parents[1]

# Every module that builds a WorkflowEngine and inspects its result. Frozen on purpose: a
# new lane must add a paused branch, then add itself here.
_KNOWN_CONSUMERS = {
    "vaf/tools/workflow_executor.py",
    "vaf/cli/cmd/workflow.py",
    "vaf/tools/agent_workflow_builder.py",
    "vaf/core/automation.py",
    "vaf/core/agent.py",
    "vaf/cli/cmd/run.py",
    "vaf/workflows/resume.py",
}


def _source(rel: str) -> str:
    # read_bytes().decode("utf-8"): 77 first-party files are not cp1252-decodable, so a bare
    # read_text() passes on Linux and fails on the Windows CI runner only (house rule).
    return (_REPO / rel).read_bytes().decode("utf-8")


def test_paused_message_does_not_read_as_an_error():
    """The shared wording must not trip the repo's own error detector - otherwise the turn
    summary stamps the tool FAILED and that verdict is persisted into the session."""
    from vaf.core.context import tool_result_is_error

    msg = paused_tool_message("Research & Document", 2, 2, "document_agent", "abc12345")
    assert not tool_result_is_error(msg)
    lowered = msg.lower()
    assert "failed" not in lowered
    assert "error" not in lowered
    # It must also actively stop a weak model from redoing the work.
    assert "do not redo" in lowered
    assert "still running" in lowered


def _branches_on_paused(src: str) -> bool:
    """True when the module BRANCHES on a paused result, not merely mentions the word.

    A substring scan is not good enough and would have been blind to the actual incident:
    vaf/tools/workflow_executor.py already contained `paused=...` inside two log calls while
    its control flow knew only success and failure. Only an `if` whose condition talks about
    `paused` proves the third outcome is actually handled.
    """
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.If):
            continue
        for sub in ast.walk(node.test):
            if isinstance(sub, ast.Attribute) and sub.attr == "paused":
                return True
            # getattr(result, "paused", False)
            if isinstance(sub, ast.Constant) and sub.value == "paused":
                return True
    return False


def test_every_engine_consumer_branches_on_paused():
    """A lane that inspects a WorkflowResult must know all THREE outcomes: success, failure
    and paused. Knowing only two is exactly what produced 'failed: None'."""
    offenders = []
    for rel in sorted(_KNOWN_CONSUMERS):
        src = _source(rel)
        if "WorkflowEngine(" not in src and "resume_workflow(" not in src:
            continue
        if not _branches_on_paused(src):
            offenders.append(rel)
    assert not offenders, (
        "These lanes inspect a WorkflowResult without branching on `paused`, so a run that "
        "merely handed off to a sub-agent is reported as a failure:\n  "
        + "\n  ".join(offenders)
        + "\nSee vaf/tools/agent_workflow_builder.py for the reference branch."
    )


def test_the_paused_detector_is_not_fooled_by_a_mere_mention():
    """Pin the detector against the exact blind spot the pre-fix code had."""
    only_logged = (
        "def run(e):\n"
        "    r = e.execute([])\n"
        "    log('done', paused=getattr(r, 'paused', False))\n"
        "    if r.success:\n"
        "        return 'ok'\n"
        "    return 'failed'\n"
    )
    handled = (
        "def run(e):\n"
        "    r = e.execute([])\n"
        "    if getattr(r, 'paused', False):\n"
        "        return 'still running'\n"
        "    return 'ok' if r.success else 'failed'\n"
    )
    assert not _branches_on_paused(only_logged)
    assert _branches_on_paused(handled)


def test_no_unknown_workflow_engine_consumer_appeared():
    """Freeze the consumer inventory. The incident happened because a fix landed on one of
    several copies; a new lane must not be able to join silently."""
    found = set()
    for path in sorted((_REPO / "vaf").rglob("*.py")):
        src = path.read_bytes().decode("utf-8")
        if "WorkflowEngine(" in src or "resume_workflow(" in src:
            rel = path.relative_to(_REPO).as_posix()   # as_posix: Windows CI compares equal
            if rel == "vaf/workflows/engine.py":
                continue                               # the producer itself
            if rel == "vaf/workflows/__init__.py":
                continue                               # facade docstring example only
            found.add(rel)
    new = found - _KNOWN_CONSUMERS
    assert not new, (
        "New WorkflowEngine consumer(s) found: " + ", ".join(sorted(new))
        + "\nAdd the `paused` branch first (see vaf/tools/agent_workflow_builder.py for the "
          "reference), then add the path to _KNOWN_CONSUMERS here."
    )


def test_terminal_lane_never_fails_a_paused_task():
    """The separate-terminal runner turned a pause into ipc.fail_task("Unknown error"),
    which is worse than the in-chat lane: it reports a hard failure to the MAIN agent.
    Pin that the paused branch comes first and uses cancel, not fail."""
    src = _source("vaf/cli/cmd/workflow.py")
    paused_at = src.index('if getattr(result, "paused", False):')
    success_at = src.index("elif result.success:")
    assert paused_at < success_at, "the paused branch must be evaluated before success/failure"
    # Strip comments: the branch DESCRIBES the old fail_task behavior in prose, and a naive
    # substring scan would trip on its own explanation.
    branch = "\n".join(
        line.split("#", 1)[0] for line in src[paused_at:success_at].splitlines()
    )
    assert "cancel_task" in branch
    assert "fail_task" not in branch and "complete_task" not in branch


def test_paused_run_exits_zero():
    """A non-zero exit is turned into a red SubAgent card plus fail_task by the piped
    watcher (Platform._stream_output), which would recreate the fabricated failure through
    a second route."""
    src = _source("vaf/cli/cmd/workflow.py")
    assert "if not success and not paused:" in src


def test_step_tool_sets_relationship_is_pinned():
    """The two step-tool tuples in the engine are deliberately different: forcing the
    sub-agent environment onto document_agent silences its output entirely, which starves
    the workflow executor's silence watchdog and aborts healthy runs. The asymmetry is
    allowed; drifting into it by accident is not."""
    assert set(SUBAGENT_STEP_TOOLS) < set(SPAWNABLE_STEP_TOOLS)
    assert set(SPAWNABLE_STEP_TOOLS) - set(SUBAGENT_STEP_TOOLS) == {
        "document_agent", "browser_agent",
    }


@pytest.fixture()
def ipc(tmp_path):
    """A SubAgentIPC with every queue file repointed into tmp (never the real ~/.vaf)."""
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


def _record(wf_id="wf1", session="sess-a"):
    return PausedWorkflow(
        workflow_id=wf_id,
        waiting_for_task_id="task-1",
        current_step_index=0,
        outputs={},
        variables={},
        steps_data=[{"tool": "document_agent"}],
        workflow_name="demo",
        created_at="2026-07-20T16:44:28",
        session_id=session,
    )


def test_claim_is_exactly_once_under_concurrency(ipc):
    """Two drains can see the same finished sub-agent: the interactive CLI drain and the
    headless/web drain. Read-then-remove would let both replay the remaining steps."""
    ipc.pause_workflow(_record())
    winners = []
    barrier = threading.Barrier(2)

    def claim():
        barrier.wait()
        got = ipc.claim_paused_workflow("wf1")
        if got is not None:
            winners.append(got)

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(winners) == 1, "exactly one drain may own a paused run"
    assert ipc.get_all_paused_workflows() == []


def test_claim_returns_none_for_an_unknown_id(ipc):
    assert ipc.claim_paused_workflow("does-not-exist") is None


def test_paused_records_are_session_scoped(ipc):
    """Rule 4.4: never build user-facing state from process-global data. A falsy session
    must yield nothing rather than everything - fail closed on a cross-user read."""
    ipc.pause_workflow(_record("wf-a", "sess-a"))
    ipc.pause_workflow(_record("wf-b", "sess-b"))
    assert [w.workflow_id for w in ipc.get_paused_workflows_for_session("sess-a")] == ["wf-a"]
    assert ipc.get_paused_workflows_for_session(None) == []
    assert ipc.get_paused_workflows_for_session("") == []


def test_session_cleanup_keeps_the_current_session(ipc, monkeypatch):
    """The cleanup used to wipe every paused workflow on a session switch, silently
    discarding runs that were still waiting for their sub-agent."""
    import vaf.core.subagent_ipc as mod

    ipc.pause_workflow(_record("wf-a", "sess-a"))
    ipc.pause_workflow(_record("wf-b", "sess-b"))
    ipc.pause_workflow(_record("wf-legacy", None))

    monkeypatch.setattr(mod, "get_ipc", lambda: ipc)
    mod._cleanup_other_sessions_locked(ipc, "sess-a")

    kept = {w.workflow_id for w in ipc.get_all_paused_workflows()}
    assert kept == {"wf-a"}, "current session kept, other sessions and session-less dropped"


def test_paused_record_survives_an_unknown_field(ipc):
    """Boundary coercion (Rule 4.7): a record written by a newer build must not break an
    older drain."""
    raw = _record().to_dict()
    raw["a_field_from_the_future"] = 42
    ipc._write_json(ipc.paused_workflows_file, [raw])
    got = ipc.get_all_paused_workflows()
    assert len(got) == 1 and got[0].workflow_id == "wf1"


class _FakeTask:
    def __init__(self, task_id="task-1", status="completed", result="the document"):
        self.task_id = task_id
        self.status = status
        self.result = result


class _FakeAgent:
    def __init__(self, session="sess-a"):
        self.current_session_id = session
        self.tools = {}


def _record_with_steps(n_steps, current_index, wf_id="wf1", session="sess-a"):
    return PausedWorkflow(
        workflow_id=wf_id,
        waiting_for_task_id="task-1",
        current_step_index=current_index,
        outputs={},
        variables={},
        steps_data=[{
            "tool": f"tool{i}", "input_template": "", "output_name": f"out{i}",
            "description": "", "optional": False, "condition": None,
            "args_template": None, "on_success": None, "on_failure": None,
            "validate": None, "status": "pending", "result": None,
            "error": None, "duration": 0.0,
        } for i in range(n_steps)],
        workflow_name="demo",
        created_at="2026-07-20T16:44:28",
        session_id=session,
    )


@pytest.fixture()
def resume_mod(ipc, monkeypatch):
    """vaf.workflows.resume wired to an isolated IPC."""
    import vaf.core.subagent_ipc as ipc_mod
    import vaf.workflows.resume as mod

    monkeypatch.setattr(mod, "get_ipc", lambda: ipc)
    monkeypatch.setattr(ipc_mod, "get_ipc", lambda: ipc)
    return mod


def test_resume_finishes_a_run_whose_awaited_step_was_the_last(resume_mod, ipc):
    """The incident's own shape: a two-step template whose second step was the sub-agent.
    Nothing is left to execute, so the run is closed inline - no tools, no threads."""
    ipc.pause_workflow(_record_with_steps(2, 1))
    handled, message = resume_mod.try_resume_paused_workflow(_FakeAgent(), _FakeTask())
    assert handled is True
    assert message and "completed" in message.lower()
    assert "the document" in message
    assert ipc.get_all_paused_workflows() == [], "the finished record must be gone"


def test_resume_leaves_a_run_with_remaining_steps_untouched(resume_mod, ipc):
    """Running the remaining steps from the drain was reviewed and rejected (process-global
    env race on a worker thread; a shared single-threaded drain otherwise). Until a proper
    runner exists the record must survive INTACT - destroying it would lose the run, which is
    strictly worse than not continuing it."""
    ipc.pause_workflow(_record_with_steps(4, 1))
    handled, message = resume_mod.try_resume_paused_workflow(_FakeAgent(), _FakeTask())
    assert handled is False and message is None, "the caller must still deliver the result"
    assert len(ipc.get_all_paused_workflows()) == 1, "the record must NOT be destroyed"


def test_resume_never_claims_the_outcome_of_a_failed_helper(resume_mod, ipc):
    """If the awaited helper failed, the drain's own failure handling is the only report the
    user gets. Swallowing it here would mean total silence."""
    ipc.pause_workflow(_record_with_steps(2, 1))
    handled, message = resume_mod.try_resume_paused_workflow(
        _FakeAgent(), _FakeTask(status="failed", result=""))
    assert handled is False and message is None
    assert ipc.get_all_paused_workflows() == [], "an unresumable record must not linger"


def test_resume_refuses_a_record_from_another_session(resume_mod, ipc):
    """Rule 4.4: a paused record belongs to the session that created it."""
    ipc.pause_workflow(_record_with_steps(2, 1, session="sess-other"))
    handled, _ = resume_mod.try_resume_paused_workflow(_FakeAgent("sess-a"), _FakeTask())
    assert handled is False
    assert len(ipc.get_all_paused_workflows()) == 1, "another session's record is untouched"


def test_resume_is_a_no_op_when_nothing_is_waiting(resume_mod, ipc):
    handled, message = resume_mod.try_resume_paused_workflow(_FakeAgent(), _FakeTask())
    assert (handled, message) == (False, None)


def test_resume_never_raises_on_a_broken_record(resume_mod, ipc):
    """A problem here must not be able to break the drain - the same drain delivers ordinary
    sub-agent results (Rule 4.3)."""
    broken = _record_with_steps(2, 1).to_dict()
    broken["steps_data"] = "not-a-list"
    broken["current_step_index"] = "not-an-int"
    ipc._write_json(ipc.paused_workflows_file, [broken])
    handled, message = resume_mod.try_resume_paused_workflow(_FakeAgent(), _FakeTask())
    assert (handled, message) == (False, None)
    assert len(ipc.get_all_paused_workflows()) == 1, "unparseable shape must fail closed"


def test_remaining_step_count_fails_closed():
    """An unreadable shape must count as 'there is more work', so the safe branch is taken
    and nothing is claimed or destroyed."""
    from vaf.workflows.resume import remaining_step_count

    bad = _record_with_steps(2, 1)
    bad.steps_data = None
    bad.current_step_index = "x"
    assert remaining_step_count(bad) > 0


def test_resume_module_spawns_no_threads():
    """Pin the design decision. A worker thread here inherits no contextvars (the session
    ContextVar would be unset, so sub-agents register under the wrong session) and runs the
    engine's process-global os.environ dance next to the main thread's chat turn, which is
    the Rule 4.5 incident. Both were found in adversarial review before this shipped."""
    src = _source("vaf/workflows/resume.py")
    assert "threading" not in src
    assert "Thread(" not in src


def test_workflow_result_carries_the_awaited_agent():
    r = WorkflowResult(
        success=False, outputs={}, final_output=None, steps=[], total_duration=0.0,
        paused=True, waiting_for_task="t1", waiting_for_agent="document_agent",
    )
    assert r.paused and r.error is None and r.waiting_for_agent == "document_agent"
