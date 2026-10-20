# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""An agent's allowed tool set holds for offering AND execution, for every identity.

MEASURED BEFORE THE PRIMITIVE: the only way to bound an agent's tools was a private write of
the turn's scope (`agent._active_tools`), and the turn overwrote it - the router replaced it
with its own pick plus the riders and the owner's recent tools, a retry step widened it to
every tool - while the dispatch funnel read neither, so any registered tool the model named
ran. An admin owner passes `admin_only` and is exempt from the account allowlist, so nothing
else bounded it either.

These tests pin the framework half: `ToolCaller(allowed_tools=)` refuses everything outside
the set before the policy stage, and `CoreAgent.set_allowed_tools` makes `visible_tools()`,
the schema and the readers that name a tool to the model agree with it.

MUTATIONS, each turns the named test red:
- delete the set check in `ToolCaller.execute` -> the refusal and the bridge tests;
- exempt admins from it, or move it after `_authorized` -> the identity test;
- check only registered names, or hand the hint `self.tools` unchanged -> the hint tests;
- ignore the set in `visible_tools()` -> the visibility test;
- drop the set from the `TOOLS` cache key -> the cache test;
- drop the visibility condition in `_plan_gate_state` -> the plan gate test;
- read the raw `_active_tools` in `_turn_pitfalls` or in the false-promise candidates -> the
  two reader tests.
"""
import json
import re

import pytest

import vaf
from vaf.core.platform import Platform
from vaf.core.tool_dispatch import ToolCaller, ToolCallHooks

SCOPE = "6f9619ff-8b86-d011-b42d-00c04fc964ff"
SESSION = "green123456"


class _Recorder(vaf.BaseTool):
    """A tool that records whether it ran."""
    permission_level = "read"

    def __init__(self, name, properties=None):
        self.name = name
        self.description = f"{name} does a thing"
        self.parameters = {"type": "object", "properties": dict(properties or {})}
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return f"{self.name} ran"


@pytest.fixture
def audit(monkeypatch):
    lines = []
    import vaf.core.log_helper as log_helper
    monkeypatch.setattr(log_helper, "log_tool_use", lambda name, **kw: lines.append(name))
    return lines


# ── the funnel: ToolCaller(allowed_tools=) ───────────────────────────────────

def test_a_name_outside_the_set_is_refused_before_anything_runs(audit):
    inside, outside = _Recorder("web_search"), _Recorder("memory_search")
    events = []
    caller = ToolCaller({"web_search": inside, "memory_search": outside},
                        allowed_tools={"web_search"}, on_event=events.append)

    out = caller.execute("memory_search", {"query": "owner"})

    assert out.startswith("Security Error:"), out
    assert "memory_search" in out
    assert outside.calls == [], "the tool outside the set ran"
    assert events == [], "a refused call was reported on the event stream"
    assert audit == ["memory_search"], "the attempt left no audit line"

    assert caller.execute("web_search", {}) == "web_search ran"
    assert [e["type"] for e in events] == ["tool_start", "tool_end"]


def test_no_identity_lifts_the_set_and_nothing_after_it_is_asked(audit):
    """An admin, a caller with no scope, an ungated lane: the set is a ceiling over identity.
    The account resolver, the authorizer and the hooks never see a refused call."""
    asked = []

    def authorize(req):
        asked.append(("authorizer", req.tool_name))

    def after_policy(name, tool, args):
        asked.append(("hook", name))

    def resolver(scope):
        asked.append(("resolver", scope))

    from vaf.core.tool_dispatch import get_account_allowlist_resolver
    previous = get_account_allowlist_resolver()
    vaf.set_account_allowlist_resolver(resolver)
    try:
        for identity in ({"user_role": "admin", "user_scope_id": SCOPE},
                         {"user_role": None, "user_scope_id": None},
                         {"user_role": "user", "user_scope_id": SCOPE}):
            outside = _Recorder("list_contacts")
            caller = ToolCaller({"list_contacts": outside, "web_search": _Recorder("web_search")},
                                allowed_tools=frozenset({"web_search"}), gate_enabled=False,
                                authorize=authorize, hooks=ToolCallHooks(after_policy=after_policy),
                                **identity)
            out = caller.execute("list_contacts", {})
            assert out.startswith("Security Error:"), (identity, out)
            assert outside.calls == [], identity
        assert asked == [], f"a refused call reached {asked}"
    finally:
        vaf.set_account_allowlist_resolver(previous)


def test_an_unregistered_name_outside_the_set_gets_the_refusal_not_a_hint(audit):
    tools = {"web_search": _Recorder("web_search"), "search_tools": _Recorder("search_tools")}
    out = ToolCaller(tools, allowed_tools={"web_search"}).execute("list_contacts", {})
    assert out.startswith("Security Error:"), out
    assert "Unknown tool" not in out


def test_the_hint_for_a_name_inside_the_set_names_nothing_outside(audit):
    """`web_serch` is in the set but no tool carries it: the correction may not point at
    `web_search`, `memory_search` or `search_tools`, which the set leaves out."""
    tools = {"web_search": _Recorder("web_search"),
             "memory_search": _Recorder("memory_search", {"query": {"type": "string"}}),
             "search_tools": _Recorder("search_tools")}
    for name in ("web_serch", "query"):
        out = ToolCaller(tools, allowed_tools={name}).execute(name, {"query": "x"})
        assert out.startswith(f"Error: Unknown tool '{name}'"), out
        for outside in tools:
            assert outside not in out, f"the hint for {name!r} names {outside!r}: {out}"
    # Inside the set the correction still works.
    out = ToolCaller(tools, allowed_tools={"web_search", "web_serch"}).execute("web_serch", {})
    assert "web_search" in out, out


def test_no_set_is_the_old_funnel():
    tools = {"web_search": _Recorder("web_search")}
    assert ToolCaller(tools).allowed_tools is None
    assert ToolCaller(tools).execute("web_search", {}) == "web_search ran"
    assert ToolCaller(tools, allowed_tools=[]).execute("web_search", {}).startswith("Security Error:"), (
        "an empty set allows nothing")
    with pytest.raises(TypeError):
        ToolCaller(tools, allowed_tools="web_search")
    assert ToolCaller(tools, allowed_tools=[" web_search ", ""]).allowed_tools == frozenset({"web_search"})


# ── the engine: CoreAgent.set_allowed_tools ──────────────────────────────────

@pytest.fixture
def agent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    from vaf.core.agent import Agent
    a = Agent(register_signals=False, run_kind="chat",
              config_overrides={"provider": "openai", "api_key_openai": "sk-test"})
    a.current_session_id = SESSION
    return a


ALLOWED = {"web_search", "contact_history", "send_telegram", "not_registered_anywhere"}


def test_the_setter_takes_names_and_refuses_one_string(agent):
    agent.set_allowed_tools(ALLOWED)
    assert agent._allowed_tools == frozenset(ALLOWED)
    with pytest.raises(TypeError):
        agent.set_allowed_tools("web_search")
    agent.set_allowed_tools(None)
    assert agent._allowed_tools is None


def test_everything_the_model_reads_names_only_tools_inside_the_set(agent):
    from vaf.tools.python_sandbox import PythonSandboxTool
    assert {"web_search", "contact_history", "memory_search", "list_contacts"} <= set(agent.tools)
    agent._excluded_tools = {"send_telegram"}
    agent.set_allowed_tools(ALLOWED)

    assert set(agent.visible_tools()) == {"web_search", "contact_history"}, (
        "inside the set, minus the hidden ones, registered only")
    schema = [t["function"]["name"] for t in agent.TOOLS]
    assert sorted(schema) == ["contact_history", "web_search"]

    listed = re.findall(r"^- ([a-z_]+):", agent.tools["list_tools"].run(_agent=agent), re.M)
    assert set(listed) == {"web_search", "contact_history"}, listed
    found = agent.tools["search_tools"].run(_agent=agent, query="contact memory search whatsapp")
    assert "memory_search" not in found and "list_contacts" not in found, found

    documented = agent.prompt_manager._build_tool_documentation()
    assert "web_search" in documented and "memory_search" not in documented

    call, names = PythonSandboxTool()._build_call_tool_fn({"_agent": agent})
    assert set(names) == {"web_search", "contact_history"}
    assert call("list_contacts", {}).startswith("Security Error:"), "the sandbox bridge ran it"

    agent.set_allowed_tools(None)
    assert "memory_search" in agent.visible_tools()


def test_the_schema_cache_knows_the_set(agent):
    agent._active_tools = ["web_search", "memory_search"]
    assert sorted(t["function"]["name"] for t in agent.TOOLS) == ["memory_search", "web_search"]
    agent.set_allowed_tools({"web_search"})
    agent._active_tools = ["web_search", "memory_search"]
    assert [t["function"]["name"] for t in agent.TOOLS] == ["web_search"], (
        "a schema built before the set was served again")


@pytest.fixture
def gated(agent, monkeypatch):
    """The chat agent with the plan gate on, an interactive turn and no plan."""
    monkeypatch.delenv("VAF_NONINTERACTIVE", raising=False)
    monkeypatch.delenv("VAF_IN_SUBAGENT_TERMINAL", raising=False)
    from vaf.core.config import Config
    _get = Config.get
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: True if key == "plan_gate_enabled" else _get(key, default)))
    import vaf.core.subagent_ipc as ipc
    monkeypatch.setattr(ipc, "get_current_session_id", lambda: SESSION)
    agent._noninteractive = False
    agent._plan_gate_blocks = 0
    agent.init_chat()
    agent._bind_session_persistence(SESSION)
    return agent


def test_the_plan_gate_stands_down_when_the_model_cannot_write_a_plan(gated):
    """The four send tools are `write`; the gate's only exit is update_working_memory. Under a
    set without it, a gate is a lock: every send to the owner was bounced."""
    tool = gated.tools["send_telegram"]
    assert tool.permission_level == "write"
    assert str(gated._plan_gate_decision("send_telegram", tool)).startswith("[PLAN REQUIRED]"), (
        "control: without a set the gate holds a write tool")
    gated._plan_gate_blocks = 0

    gated.set_allowed_tools({"send_telegram", "web_search"})
    assert gated._plan_gate_state()[0] is False
    assert gated._plan_gate_decision("send_telegram", tool) is None

    gated.set_allowed_tools({"send_telegram", "update_working_memory"})
    assert str(gated._plan_gate_decision("send_telegram", tool)).startswith("[PLAN REQUIRED]"), (
        "a set that keeps the way out keeps the gate")


def test_the_pitfall_block_names_no_tool_outside_the_set(agent, monkeypatch):
    import vaf.whare_wananga.delivery as delivery
    monkeypatch.setattr(delivery, "tool_pitfalls", lambda name, **kw: f"careful with {name}")
    agent.set_allowed_tools({"web_search"})
    agent._active_tools = ["web_search", "memory_search"]
    block = agent.prompt_manager._turn_pitfalls()
    assert "web_search" in block
    assert "memory_search" not in block, block


class _Sent(BaseException):
    """Ends the turn at the second main request; BaseException so no retry handler eats it."""


def _retry_step_corrections(agent, monkeypatch):
    """Run one retry step with a tight context (the scope narrows to the internal subset,
    which carries memory_search) where the model announces memory_search in its <Action>
    block and calls nothing. Returns the tool names of the first main request, the turn's
    scope and the false-promise corrections the step wrote into the history. The guard is
    opt-in (`false_promise_detection_enabled`), so it is switched on here."""
    from vaf.core.config import Config
    _get = Config.get
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: True if key == "false_promise_detection_enabled" else _get(key, default)))
    monkeypatch.setattr(agent, "get_token_usage", lambda: (100000, 32768))
    monkeypatch.setattr(agent, "manage_context", lambda *a, **kw: None)
    monkeypatch.setattr(agent, "_try_workflow", lambda *a, **kw: None)
    main, scope = [], []

    def backend(messages=None, **kw):
        if not kw.get("tools"):
            yield "ok"
            return
        main.append([t["function"]["name"] for t in kw["tools"]])
        scope.append(list(agent._active_tools or []))
        if len(main) > 1:
            raise _Sent()
        yield "<Action>memory_search: look up what I know about the owner</Action> Moment."

    agent.api_backend.chat_completion = backend
    agent.history.append({"role": "user", "content": "Was weisst du ueber deinen Chef?"})
    try:
        agent.chat_step(user_input="Was weisst du ueber deinen Chef?", skip_input=True,
                        disable_workflows=True, stream_callback=lambda t: None)
    except _Sent:
        pass
    corrections = [str(m.get("content")) for m in agent.history
                   if "CORRECTION NEEDED" in str(m.get("content"))]
    return main[0], scope[0], corrections


def test_the_false_promise_correction_names_no_tool_outside_the_set(agent, monkeypatch):
    """The nudge says "Call `<tool>` NOW": a tool the set leaves out would be ordered and then
    refused."""
    _, scope, corrections = _retry_step_corrections(agent, monkeypatch)
    assert "memory_search" in scope, "the internal subset did not engage"
    assert any("Call `memory_search` NOW" in c for c in corrections), (
        "control: without a set the guard orders the announced tool")


def test_under_a_set_the_correction_orders_nothing_outside_it(agent, monkeypatch):
    agent.set_allowed_tools({"web_search", "send_telegram"})
    offered, scope, corrections = _retry_step_corrections(agent, monkeypatch)
    assert "memory_search" in scope, "the internal subset did not engage"
    assert set(offered) <= {"web_search", "send_telegram"}, offered
    assert not any("memory_search" in c for c in corrections), corrections
    assert agent._offered_tool_names() == ["web_search"]


def test_the_facade_passes_the_set_to_a_live_engine(agent):
    """Not the replay (test_public_facade.py pins that): a REAL engine behind the facade
    ends up with the set, and refuses through it."""
    facade = vaf.Agent(config={"provider": "openai", "api_key_openai": "sk-test"})
    facade._agent = agent
    facade.set_allowed_tools(["web_search"])
    assert agent._allowed_tools == frozenset({"web_search"})
    assert json.dumps(sorted(agent.visible_tools())) == '["web_search"]'
    assert agent.execute_tool("list_contacts", {}).startswith("Security Error:")
    facade.set_allowed_tools(None)
    assert agent._allowed_tools is None
