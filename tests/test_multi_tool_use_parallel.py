# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The provider wrapper that carries other tool calls inside itself.

Some providers answer a multi-call turn with a single `multi_tool_use.parallel` call whose
payload is a list of real calls. It is not a tool - there is no entry for it in the registry -
so the dispatcher unwraps it and runs each inner call through `execute_tool` again.

The name says "parallel" and the implementation is deliberately SEQUENTIAL: each inner call
is entitled to its own policy evaluation, its own confirmation gate and its own UI prompt,
and dispatching them concurrently would mean answering one dialog while another tool has
already run. That property is what this file protects.

Written after the wrapper spent a refactor in a state where any use of it raised NameError:
the whole suite stayed green because nothing had ever dispatched it. Ruff's undefined-name
check caught it, which is luck rather than coverage - a typo in a string literal would have
shipped.
"""
from types import SimpleNamespace

import pytest
from conftest import bind_chat_stages

from vaf.core.agent import Agent
from vaf.tools.base import BaseTool

SCOPE = "deadbeef-0000-0000-0000-000000000000"   # synthetic; never a real scope UUID

WRAPPER = "multi_tool_use.parallel"


class _Recorder(BaseTool):
    description = "probe"
    permission_level = "read"
    parameters = {"type": "object", "properties": {"x": {"type": "string"}}}

    def __init__(self, name, log, fn=None):
        super().__init__()
        self.name = name
        self._log = log
        self._fn = fn or (lambda **kw: f"{name} ok")

    def run(self, **kwargs):
        self._log.append(self.name)
        return self._fn(**kwargs)


def _agent(tools, events=None):
    return bind_chat_stages(SimpleNamespace(
        tools={t.name: t for t in tools},
        _event_sink=(events.append if events is not None else None),
        _allow_once_tools={t.name for t in tools}, _noninteractive=True,
        _current_turn_thinking_mode=False, _current_chat_source="web",
        current_session_id=None, _current_user_scope_id=SCOPE,
        _current_user_role="admin", _current_username="tenant", _run_kind="chat",
        _ww_training=False, _active_tools=set(), _turn_ran_progress_tool=False,
        _session_workspace=None, history=[], main_persistence=None,
        _record_tool_used=lambda n: None,
        _plan_gate_decision=lambda n, t, tool_args=None: None,
        _working_memory_note_gate=lambda tool_args: None,
        _proactive_reply_gate_decision=lambda n, t, a: None,
        _ask_first_gate_decision=lambda n, t: None,
        get_live_session_subagents=lambda: [], _extract_subagent_goal=lambda a: "",
        model_display_name="probe",
    ))


def _call(*names):
    return {"tool_uses": [{"recipient_name": n, "parameters": {}} for n in names]}


def test_the_wrapper_dispatches_every_inner_call():
    log = []
    agent = _agent([_Recorder("alpha", log), _Recorder("beta", log)])
    result = Agent.execute_tool(agent, WRAPPER, _call("alpha", "beta"))
    assert log == ["alpha", "beta"]
    assert "alpha ok" in result and "beta ok" in result


def test_inner_calls_run_in_order_not_concurrently():
    """Sequential is the security property, not an implementation detail: a gate belonging
    to the second call must not be answered while the first has already run."""
    log = []
    agent = _agent([_Recorder(n, log) for n in ("a", "b", "c")])
    Agent.execute_tool(agent, WRAPPER, _call("c", "a", "b"))
    assert log == ["c", "a", "b"]


def test_an_inner_failure_is_reported_per_call_not_as_a_wrapper_failure():
    log = []

    def _boom(**kw):
        raise ValueError("kaputt")

    agent = _agent([_Recorder("good", log), _Recorder("bad", log, _boom)])
    result = Agent.execute_tool(agent, WRAPPER, _call("good", "bad"))
    assert "OK good" in result
    assert "ERR bad" in result


def test_the_wrapper_reports_itself_as_one_event_pair():
    """The inner calls emit their own pairs; the wrapper adds exactly one around them, and
    always reports ok - its own success says nothing about the calls it carried."""
    events = []
    agent = _agent([_Recorder("alpha", [])], events)
    Agent.execute_tool(agent, WRAPPER, _call("alpha"))
    outer = [e for e in events if e.get("tool") == WRAPPER]
    assert [(e["type"], e.get("ok")) for e in outer] == [("tool_start", None), ("tool_end", True)]
    assert events[0]["tool"] == WRAPPER and events[-1]["tool"] == WRAPPER


def test_a_nested_wrapper_is_refused():
    """Self-recursion would be unbounded, and the payload is model-supplied."""
    result = Agent.execute_tool(_agent([]), WRAPPER, _call(WRAPPER))
    assert "Invalid tool name" in result


@pytest.mark.parametrize("payload", [{}, {"tool_uses": []}, None])
def test_an_empty_payload_is_an_error_string_not_a_crash(payload):
    assert Agent.execute_tool(_agent([]), WRAPPER, payload).startswith("Error:")


def test_a_malformed_entry_does_not_stop_the_others():
    log = []
    agent = _agent([_Recorder("alpha", log)])
    args = {"tool_uses": ["not a dict", {"recipient_name": "alpha", "parameters": {}}]}
    result = Agent.execute_tool(agent, WRAPPER, args)
    assert log == ["alpha"]
    assert "Invalid tool entry" in result


def test_inner_arguments_arrive_as_a_json_string_too():
    """Providers have been observed sending the inner parameters pre-encoded."""
    seen = {}
    agent = _agent([_Recorder("alpha", [], lambda **kw: seen.update(kw) or "ok")])
    Agent.execute_tool(agent, WRAPPER,
                       {"tool_uses": [{"recipient_name": "alpha", "parameters": '{"x": "1"}'}]})
    assert seen.get("x") == "1"


def test_the_wrapper_counts_as_progress_for_the_turn():
    """The wrapper is not a tool, so it cannot take the shared pipeline - but it is a step of
    the turn, and the anti-spin guard reads `_turn_ran_progress_tool`. Routing it around the
    pipeline briefly routed it around the turn gates as well, so a turn whose only action was
    a wrapper call looked like a turn that had done nothing."""
    agent = _agent([_Recorder("alpha", [])])
    assert agent._turn_ran_progress_tool is False
    Agent.execute_tool(agent, WRAPPER, _call("alpha"))
    assert agent._turn_ran_progress_tool is True


def test_the_wrapper_is_still_subject_to_the_turn_gates():
    """A plan gate that blocks the inner tools must block the wrapper too - otherwise the
    wrapper is a way around the gate rather than a way to batch calls."""
    log = []
    agent = _agent([_Recorder("alpha", log)])
    agent._plan_gate_decision = lambda n, t, tool_args=None: "[PLAN REQUIRED] plan first"
    result = Agent.execute_tool(agent, WRAPPER, _call("alpha"))
    assert result.startswith("[PLAN REQUIRED]")
    assert log == [], "the wrapper ran its payload despite a blocking turn gate"
