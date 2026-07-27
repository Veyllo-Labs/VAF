# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The workflow engine runs its steps through the shared path, keeping its own three answers.

Two implementations of "run a tool with a timeout and a stop check" existed: the agent's and
the engine's. Duplicated execution control is how the two lanes drifted in the first place -
the engine also skipped policy and read identity from its own hardcoded name list - so the
copy is gone and the engine calls the same function the chat lane calls.

What makes that safe is that the engine's differences are ARGUMENTS, not a fork. It has
exactly three, each with a reason that cost something to learn:

- **the timeout floor.** A heavy agent step gets ``workflow_agent_step_timeout_seconds``
  (1800) instead of the generic sub-agent cap (300). The generic cap once killed a healthy
  coder at minute five - streaming, linter green, mid-loop. Losing this is the single most
  likely way this refactor causes an incident, and no existing test would notice, because
  ``test_workflow_step_digest`` pins ``_workflow_step_timeout`` as a FUNCTION and never
  checks that the engine still uses it.
- **browser_agent is bounded HERE.** Standalone it supervises itself, which is right - it
  runs for minutes and has its own stop monitor. As a workflow step it blocks the whole
  workflow and the agent waiting on it, so a hung browser would freeze everything.
- **the stop callback.** The chat lane polls the task queue by session id; the engine is
  handed a callback from outside and must use that one.

Pinned at the call, not by wall-clock: what each argument DOES is already proven in
tests/test_tool_dispatch_bounded_run.py, so what is left to show is that the engine passes
its own values. One end-to-end case follows anyway, because "passes the argument" and
"the argument takes effect" are different claims.
"""
from unittest.mock import patch

import pytest

from vaf.core.bounded_run import SELF_SUPERVISED_TOOLS, is_abort_sentinel
from vaf.workflows.engine import WorkflowEngine, WorkflowStep, _workflow_step_timeout


class _Tool:
    def __init__(self, fn=None):
        self._fn = fn or (lambda **kw: "OK")

    def run(self, **kwargs):
        return self._fn(**kwargs)


def _run_step(tool_name, tool, check_stop=None, **engine_kwargs):
    engine = WorkflowEngine(tools={tool_name: tool}, callback=lambda *a, **k: None,
                            **engine_kwargs)
    return engine.execute(
        [WorkflowStep(tool=tool_name, input_template="do it", output_name="out")],
        variables={}, check_stop=check_stop,
    )


def _captured_call(tool_name, tool=None, check_stop=None):
    """Run one step and report the kwargs the engine handed the shared path."""
    seen = {}

    def _spy(tool_obj, args, **kwargs):
        seen.update(kwargs)
        seen["tool"] = tool_obj
        seen["args"] = args
        return "OK"

    with patch("vaf.core.tool_dispatch.run_tool_bounded", _spy):
        _run_step(tool_name, tool or _Tool(), check_stop=check_stop)
    assert seen, f"the engine never reached the shared execution path for {tool_name}"
    return seen


# ── the engine uses the shared path at all ───────────────────────────────────

def test_a_workflow_step_goes_through_the_shared_path():
    """The deletion this change is for: no second bounded-run implementation."""
    assert _captured_call("web_search")["tool_name"] == "web_search"


def test_the_engine_no_longer_carries_its_own_bounded_run():
    import inspect

    src = inspect.getsource(WorkflowEngine.execute)
    assert "run_bounded(" not in src, (
        "the engine calls run_bounded directly again - that is the duplicate execution "
        "control this change removed, and duplicates drift"
    )


# ── difference 1: the timeout floor ──────────────────────────────────────────

def test_the_engine_passes_its_own_timeout_resolver():
    """Not agent_timeout_seconds. The difference is 300 vs 1800 seconds for a coder step."""
    assert _captured_call("coding_agent")["timeout_for"] is _workflow_step_timeout


@pytest.mark.parametrize("tool_name", ["coding_agent", "research_agent", "document_agent"])
def test_the_floor_is_still_worth_passing(tool_name):
    """Guards the premise: if the floor ever equalled the generic budget, the argument above
    would be pinning nothing and this file would pass while protecting nothing."""
    from vaf.core.bounded_run import agent_timeout_seconds

    assert _workflow_step_timeout(tool_name) > agent_timeout_seconds(tool_name)
    assert _workflow_step_timeout(tool_name) >= 1800


def test_an_ordinary_tool_keeps_its_normal_budget():
    """The floor is for heavy agent steps only - it must not silently make every step
    unbounded in practice."""
    from vaf.core.bounded_run import agent_timeout_seconds

    assert _workflow_step_timeout("web_search") == agent_timeout_seconds("web_search")


# ── difference 2: browser_agent is bounded in a workflow ─────────────────────

def test_browser_agent_is_not_exempt_inside_a_workflow():
    exempt = _captured_call("browser_agent")["self_supervised"]
    assert "browser_agent" not in exempt, (
        "a hung browser step would freeze the whole workflow and the agent waiting on it"
    )


def test_the_other_self_supervised_tools_stay_exempt():
    """Only browser_agent is reversed. Bounding e.g. coding_agent here would abandon it
    mid-edit, which is what SELF_SUPERVISED_TOOLS exists to prevent."""
    exempt = _captured_call("coding_agent")["self_supervised"]
    assert exempt == SELF_SUPERVISED_TOOLS - {"browser_agent"}


def test_browser_agent_really_is_cut_off_and_not_just_declared_so():
    """The end-to-end half: passing the argument and the argument taking effect are two
    different claims. Runs a browser_agent step that would outlast a tiny budget."""
    import time

    def _slow(**kwargs):
        time.sleep(5)
        return "FINISHED"

    with patch("vaf.core.config.Config.get",
               side_effect=lambda k, d=None: 0.2 if k == "browser_timeout_seconds"
               else (0.05 if k == "tool_stop_poll_seconds" else d)):
        result = _run_step("browser_agent", _Tool(_slow))
    step_result = str(result.steps[0].result or "")
    assert is_abort_sentinel(step_result) or not result.success, (
        f"browser_agent ran unbounded as a workflow step: {step_result[:120]!r}"
    )


# ── difference 3: the stop callback ──────────────────────────────────────────

def test_the_engines_own_stop_callback_is_the_one_used():
    """The chat lane polls the task queue by session; this lane is handed a callback and must
    use THAT one, or the Stop button stops the wrong thing - or nothing."""
    def _my_stop():
        return False

    assert _captured_call("web_search", check_stop=_my_stop)["stop_check"] is _my_stop


def test_a_step_stops_when_the_callback_says_so():
    import time

    started = time.monotonic()

    def _slow(**kwargs):
        time.sleep(5)
        return "FINISHED"

    with patch("vaf.core.config.Config.get",
               side_effect=lambda k, d=None: 0.05 if k == "tool_stop_poll_seconds" else d):
        result = _run_step("web_search", _Tool(_slow),
                           check_stop=lambda: time.monotonic() - started > 0.1)
    assert time.monotonic() - started < 4, "stop was not noticed until the tool finished"
    assert not result.success or is_abort_sentinel(str(result.steps[0].result or ""))


# ── the spawn branch is deliberately NOT routed through the funnel ───────────

def test_the_spawn_branch_still_bypasses_the_shared_path():
    """Honest partial adoption: spawning a sub-agent is not a tool run, it is a spawn plus an
    IPC wait with liveness, a kill tree and a watchdog. Routing it through the funnel would
    put the funnel in charge of IPC, and the raw [SUBAGENT_ASYNC:...] marker it returns is
    what the pause branch matches on."""
    import inspect

    src = inspect.getsource(WorkflowEngine.execute)
    assert "_spawn_out = tool.run(**args)" in src
    assert "_await_subagent(" in src
