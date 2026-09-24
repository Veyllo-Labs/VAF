# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The three ways callers legitimately differ when they run a tool.

``run_tool_bounded`` exists so the workflow engine, the librarian and the coder stop
rebuilding execution control. That is only worth anything if the things they genuinely need
to vary are ARGUMENTS and not forks - otherwise the second caller copies the function and
the drift starts again, which is the whole reason this round exists.

Three differences, each measured here rather than assumed:

- **the timeout budget.** The engine raises a floor for heavy sub-agent steps because the
  generic cap once killed a healthy coder mid-loop at minute five, streaming and with a green
  linter. If the funnel hardcoded one budget per tool name that incident comes back.
  The TOOL declares its own budget (``BaseTool.budget_seconds``); a caller replaces it, or
  adjusts it by accepting a ``default`` keyword.
- **which tools are exempt.** Self-supervised tools must not be wrapped at all: a hard
  timeout abandons the thread while the tool is still working. The tool declares it
  (``BaseTool.self_supervised``); a caller may pass its own set. The engine deliberately
  excludes ``browser_agent`` from its own set, so a workflow cannot stall forever on one
  browsing step even though a standalone call may take its time.
- **how Stop arrives.** The chat lane polls the task queue by session id; the engine is
  handed a callback from outside. Neither can be the built-in assumption.

The dispatch baselines do not cover any of this - they never let a tool run long enough to
time out - which is exactly why it needs its own file.
"""
import time

import pytest

from vaf.core.bounded_run import is_abort_sentinel
from vaf.core.tool_dispatch import run_tool_bounded, session_stop_check


class _Tool:
    def __init__(self, fn=None, *, self_supervised=False, budget=None):
        self._fn = fn or (lambda **kw: "OK")
        self.calls = 0
        self.self_supervised = self_supervised
        self._budget = budget

    def budget_seconds(self, args):
        return self._budget(args) if callable(self._budget) else self._budget

    def run(self, **kwargs):
        self.calls += 1
        return self._fn(**kwargs)


def _sleeper(seconds):
    def _fn(**kwargs):
        time.sleep(seconds)
        return "FINISHED"
    return _fn


# ── the happy path ───────────────────────────────────────────────────────────

def test_a_normal_tool_runs_and_returns_its_result():
    tool = _Tool()
    assert run_tool_bounded(tool, {"x": 1}, tool_name="probe") == "OK"
    assert tool.calls == 1


def test_the_arguments_reach_the_tool_unchanged():
    seen = {}
    tool = _Tool(lambda **kw: seen.update(kw) or "OK")
    run_tool_bounded(tool, {"path": "/tmp/x", "user_scope_id": "s"}, tool_name="probe")
    assert seen == {"path": "/tmp/x", "user_scope_id": "s"}


# ── difference 1: the timeout budget ─────────────────────────────────────────

def test_a_slow_tool_is_cut_off_at_its_budget():
    tool = _Tool(_sleeper(5))
    result = run_tool_bounded(tool, {}, tool_name="probe",
                              timeout_for=lambda name: 0.2, poll=0.05)
    assert is_abort_sentinel(result), result


def test_a_caller_can_raise_the_budget_for_its_own_steps():
    """THE workflow floor. A budget that cannot be raised per caller is the incident where a
    healthy coder was killed at minute five."""
    tool = _Tool(_sleeper(0.3))
    generous = run_tool_bounded(tool, {}, tool_name="coding_agent_probe",
                                timeout_for=lambda name: 5.0, poll=0.05)
    assert generous == "FINISHED"
    stingy = run_tool_bounded(_Tool(_sleeper(5)), {}, tool_name="coding_agent_probe",
                              timeout_for=lambda name: 0.2, poll=0.05)
    assert is_abort_sentinel(stingy)


def test_the_budget_resolver_is_asked_for_the_tool_by_name():
    """Per-tool, not one number for everything - a filesystem agent must not have to wait the
    research budget."""
    asked = []
    run_tool_bounded(_Tool(), {}, tool_name="librarian_probe",
                     timeout_for=lambda name: asked.append(name) or 5.0, poll=0.05)
    assert asked == ["librarian_probe"]


# ── difference 2: which tools are exempt ─────────────────────────────────────

def test_a_self_supervised_tool_is_not_bounded_at_all():
    """It manages its own cancellation; wrapping it would abandon it mid-work. Proven by
    giving it a budget it would blow through and seeing it finish anyway. The sleep outlasts
    run_bounded's one-second floor, or a bounded run would finish it too and prove nothing."""
    result = run_tool_bounded(_Tool(_sleeper(1.4), self_supervised=True), {},
                              tool_name="any_name", timeout_for=lambda n: 0.05, poll=0.01)
    assert result == "FINISHED"


def test_a_caller_can_shrink_the_exempt_set():
    """The engine's browser_agent case: exempt when called on its own, bounded inside a
    workflow, because a workflow must not stall forever on one step."""
    from vaf.tools.browser_agent import BrowserAgentTool

    assert BrowserAgentTool.self_supervised is True, "premise changed - re-read engine.py"
    result = run_tool_bounded(_Tool(_sleeper(5), self_supervised=True), {},
                              tool_name="browser_agent", self_supervised=frozenset({"coding_agent"}),
                              timeout_for=lambda n: 0.2, poll=0.05)
    assert is_abort_sentinel(result), "browser_agent ran unbounded despite the shrunk set"


def test_an_empty_exempt_set_bounds_everything():
    result = run_tool_bounded(_Tool(_sleeper(5), self_supervised=True), {}, tool_name="x",
                              self_supervised=frozenset(),
                              timeout_for=lambda n: 0.2, poll=0.05)
    assert is_abort_sentinel(result)


def test_the_tools_that_used_to_be_named_declare_it_themselves():
    """The name list is gone; these five carry the reason on their own class now."""
    from vaf.tools.agent_workflow_builder import AgentWorkflowBuilderTool
    from vaf.tools.browser_agent import BrowserAgentTool
    from vaf.tools.coder import CodingAgentTool
    from vaf.tools.python_sandbox import PythonSandboxTool
    from vaf.tools.workflow_executor import ExecuteWorkflowTool

    for cls in (AgentWorkflowBuilderTool, BrowserAgentTool, CodingAgentTool,
                PythonSandboxTool, ExecuteWorkflowTool):
        assert cls.self_supervised is True, cls.__name__


# ── the tool's own budget ────────────────────────────────────────────────────

def test_without_a_caller_budget_the_tool_s_own_applies():
    """A tool registered by anybody can say how long its calls legitimately take.
    MUTATION: ignore budget_seconds in tool_budget_seconds - this goes red."""
    quick = run_tool_bounded(_Tool(_sleeper(5), budget=0.2), {}, tool_name="probe", poll=0.05)
    assert is_abort_sentinel(quick)
    patient = run_tool_bounded(_Tool(_sleeper(0.3), budget=5.0), {}, tool_name="probe", poll=0.05)
    assert patient == "FINISHED"


def test_the_budget_can_follow_the_call_s_own_arguments():
    tool = _Tool(_sleeper(0.3), budget=lambda args: args.get("t"))
    assert run_tool_bounded(tool, {"t": 5.0}, tool_name="probe", poll=0.05) == "FINISHED"
    assert is_abort_sentinel(run_tool_bounded(_Tool(_sleeper(5), budget=lambda a: a.get("t")),
                                              {"t": 0.2}, tool_name="probe", poll=0.05))


def test_a_caller_that_accepts_default_adjusts_the_tool_s_budget():
    """The workflow engine's way: it receives the tool's own budget and returns its own."""
    seen = []

    def floor(name, default=None):
        seen.append(default)
        return max(default, 5.0)

    result = run_tool_bounded(_Tool(_sleeper(0.3), budget=0.1), {}, tool_name="probe",
                              timeout_for=floor, poll=0.05)
    assert result == "FINISHED" and seen == [0.1]


def test_a_broken_declaration_falls_back_to_the_default_not_to_forever(monkeypatch):
    from vaf.core import bounded_run

    monkeypatch.setattr(bounded_run, "default_timeout_seconds", lambda: 0.2)

    class _Broken(_Tool):
        def budget_seconds(self, args):
            raise RuntimeError("boom")

    for bad in (_Broken(_sleeper(5)), _Tool(_sleeper(5), budget=-1), _Tool(_sleeper(5), budget="x")):
        assert is_abort_sentinel(run_tool_bounded(bad, {}, tool_name="probe", poll=0.05))


def test_host_bash_waits_as_long_as_its_command_may_run():
    """It accepted a 300-second command and was abandoned at the generic 120."""
    from vaf.tools.host_bash import HostBashTool

    tool = HostBashTool()
    assert tool.budget_seconds({"timeout": 300}) > 300
    assert tool.budget_seconds({"timeout": 600}) > 600
    assert tool.budget_seconds({"timeout": 99999}) <= HostBashTool.MAX_TIMEOUT_SECONDS + 60
    assert tool.budget_seconds({}) > 120


# ── difference 3: how Stop arrives ───────────────────────────────────────────

def test_stop_is_polled_during_the_call_not_only_before_it():
    """A tool that has already started would otherwise run to completion, which is what makes
    the Stop button feel broken."""
    started = time.monotonic()
    result = run_tool_bounded(_Tool(_sleeper(5)), {}, tool_name="probe",
                              stop_check=lambda: time.monotonic() - started > 0.1,
                              timeout_for=lambda n: 30.0, poll=0.05)
    assert is_abort_sentinel(result), result
    assert time.monotonic() - started < 3, "stop was not noticed until the tool finished"


def test_no_stop_check_means_the_tool_simply_runs():
    assert run_tool_bounded(_Tool(), {}, tool_name="probe", timeout_for=lambda n: 5.0) == "OK"


@pytest.mark.parametrize("session_id", [None, "", 0])
def test_without_a_session_the_stop_predicate_never_fires(session_id):
    """Direct consumers (the CLI, automations) carry no session; they must not be stopped by
    somebody else's flag, and must not crash for the lack of one."""
    assert session_stop_check(session_id)() is False


def test_the_stop_predicate_fails_safe_when_the_queue_is_unreachable(monkeypatch):
    """A false stop kills work nobody cancelled, so an unreachable queue must answer 'no'."""
    import vaf.core.task_queue as tq

    class _Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("queue down")

    monkeypatch.setattr(tq, "TaskQueue", _Boom)
    assert session_stop_check("some-session")() is False


# ── what actually crosses into the worker thread ─────────────────────────────

def test_the_callers_context_reaches_the_worker_thread():
    """A contextvar set before the call IS visible inside the tool.

    Eleven places in the tree used to justify themselves with the opposite claim - that a
    contextvar set in the dispatcher "would not reach" the tool, because tools run on a
    worker thread. A bare `threading.Thread` would indeed start with a fresh context, but
    `run_bounded` copies the caller's context on purpose (`vaf/core/bounded_run.py`), and it
    does so for a reason worth keeping: an ABANDONED worker (freed on timeout but still
    running) then keeps its OWN session, so its late writes are tagged with the session that
    started it rather than whatever a later turn set process-globally.

    The practice those comments describe - install the file jail inside `run()` - stays
    correct; only the stated reason was wrong. The real reason is that a dispatcher is not
    always in the picture: the coder, the workflow engine and automations call tools
    directly. This test pins the fact so the false justification cannot come back, and so
    that anything relying on identity crossing into the worker keeps working.
    """
    import contextvars

    probe = contextvars.ContextVar("dispatch_probe", default="<did not cross>")
    probe.set("set by the caller")
    tool = _Tool(lambda **kw: probe.get())
    assert run_tool_bounded(tool, {}, tool_name="probe") == "set by the caller"


def test_the_worker_cannot_leak_its_context_back():
    """The copy is one-way: a tool that sets a contextvar must not change the caller's."""
    import contextvars

    probe = contextvars.ContextVar("dispatch_probe_back", default="caller value")

    def _mutate(**kw):
        probe.set("worker value")
        return "done"

    run_tool_bounded(_Tool(_mutate), {}, tool_name="probe")
    assert probe.get() == "caller value"
