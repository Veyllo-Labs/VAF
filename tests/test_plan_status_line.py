# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The plan line of the turn block says what the plan gate does, as of THIS step.

MEASURED LIVE: with the orchestrator module active, the turn block said "PLAN LOADED: NO ...
SYSTEM LOCKED: All heavy tools are disabled ... You cannot act or search until a plan is
persisted" for the whole turn - also after the model had set a plan in that turn. The status
was computed once, with the system prompt, and the turn block (rebuilt for every step) reused
it. The agent set its plan again and again, and reported the line as a loop; in other runs it
called the block an injected instruction and told the user so. The text also claimed more than
the gate does: the gate holds state-changing tools only, never reading or searching, and not
at all in an automation run - and it treated a placeholder plan as none while the line counted
it as a plan.

Driven through the REAL Agent.chat_step with only the model replaced.

MUTATION: compute the line once per turn again and the first test goes red; let the line count
a placeholder plan as set and the placeholder test goes red; drop the gate-active check and
the automation test goes red.
"""
import json

import pytest

from vaf.core.platform import Platform

SESSION = "green123456"
REQUEST = "Vergleiche alle drei Konfigurationsdateien im Projekt"


@pytest.fixture
def agent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    # The chat the runner binds for a turn; the working-memory tool reads it from here.
    import vaf.core.subagent_ipc as ipc
    monkeypatch.setattr(ipc, "get_current_session_id", lambda: SESSION)
    # An interactive chat in the main process, stated rather than inherited: another test's
    # leftover env would make this a non-interactive run, which the gate never holds.
    monkeypatch.delenv("VAF_NONINTERACTIVE", raising=False)
    monkeypatch.delenv("VAF_IN_SUBAGENT_TERMINAL", raising=False)
    from vaf.core.config import Config
    _get = Config.get
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: True if key == "plan_gate_enabled" else _get(key, default)))
    from vaf.core.agent import Agent
    a = Agent(register_signals=False, run_kind="chat",
              config_overrides={"provider": "openai", "api_key_openai": "sk-test",
                                "plan_gate_enabled": True})
    a._noninteractive = False
    a.current_session_id = SESSION
    a._current_chat_source = "web"
    a.init_chat()
    a._bind_session_persistence(SESSION)
    a._active_tools = None
    return a


class _Model:
    """Sets a plan until the tool result is in the history, then answers. Records, for every
    request, whether the plan call had already been answered and what the turn block said."""

    def __init__(self, plan):
        self.plan, self.requests = plan, []

    def __call__(self, messages=None, **kw):
        if not kw.get("tools"):
            yield "ok"
            return
        messages = messages or []
        answered = any(m.get("role") == "tool" and m.get("name") == "update_working_memory"
                       for m in messages)
        block = next((str(m.get("content")) for m in messages if m.get("role") == "user"
                      and str(m.get("content")).lstrip().startswith("<turn>")), "")
        self.requests.append((answered, block))
        if answered:
            yield "Fertig."
            return
        call = {"index": 0, "id": "c1", "type": "function",
                "function": {"name": "update_working_memory",
                             "arguments": json.dumps({"plan": self.plan})}}
        yield json.dumps({"tool_calls": [call]})
        yield json.dumps({"finish_reason": "tool_calls"})

    def before(self):
        return [b for answered, b in self.requests if not answered]

    def after(self):
        return [b for answered, b in self.requests if answered]


def _turn(agent, model):
    agent.api_backend.chat_completion = model
    agent.chat_step(user_input=REQUEST, stream_callback=lambda t: None)
    return model


def test_the_line_follows_a_plan_set_in_the_same_turn(agent):
    model = _turn(agent, _Model(["Die drei Konfigurationsdateien lesen und Unterschiede auflisten"]))
    assert model.before() and all("Plan set: no" in b for b in model.before()), \
        "the setup is real: the module is on, no plan yet"
    assert model.after() and all("Plan set: yes" in b for b in model.after()), \
        "every step of the SAME turn after the plan call sees the plan"
    assert all("SYSTEM LOCKED" not in b and "cannot act or search" not in b
               for _a, b in model.requests)


def test_a_placeholder_plan_is_no_plan_for_the_line_either(agent):
    """The gate does not accept "test" as a plan; the line must not say it is set."""
    model = _turn(agent, _Model(["test"]))
    assert model.after() and all("Plan set: no" in b for b in model.after())


def test_an_automation_run_is_told_nothing_is_held(agent):
    """Non-interactive runs are never gated; the line must not claim that tools are held."""
    agent._noninteractive = True
    model = _turn(agent, _Model(["Die drei Konfigurationsdateien lesen und Unterschiede auflisten"]))
    assert model.before() and all("held" not in b for b in model.before())


def test_the_gate_itself_does_not_take_a_stored_placeholder_plan(agent):
    """Stored plan entries are {"t", "text"} dicts. Judged as joined dicts, a stored "test" was
    never recognised as a placeholder, so the gate opened on it."""
    tool = agent.tools["write_file"]
    agent.main_persistence.update_working_memory(plan=["test"])
    assert str(agent._plan_gate_decision("write_file", tool)).startswith("[PLAN REQUIRED]")
    agent.main_persistence.update_working_memory(plan=["Die Konfiguration anpassen und speichern"])
    assert agent._plan_gate_decision("write_file", tool) is None


def test_a_tool_the_gate_does_not_cover_reads_no_working_memory(agent, monkeypatch):
    """The gate's state reads the chat's working memory from disk; a read tool, a system tool
    and python_sandbox are never gated, so their calls must not pay for that read."""
    calls = []
    monkeypatch.setattr(agent, "_plan_gate_state", lambda: calls.append(1) or (True, False))
    for name in ("read_file", "update_working_memory", "python_sandbox"):
        assert agent._plan_gate_decision(name, agent.tools.get(name)) is None
    assert calls == []
    assert str(agent._plan_gate_decision("write_file", agent.tools["write_file"])).startswith("[PLAN REQUIRED]")
    assert calls == [1]
