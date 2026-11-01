# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The context count before routing, the emergency purge, and the read-only broadcast.

Live incident on a local engine (32768 served tokens): every turn of a long chat logged
"CRITICAL OVERFLOW" before routing and purged the history to the last six messages. The count
added the schema of EVERY tool (an internal step leaves the active set at None), about 40k
tokens, while the request that is sent carries only the routed set. The same turn then
compressed against a second limit (128000) and lost the summary it had just written: the
context broadcast loaded the session file with the state registry and put its older snapshot,
window included, back into the live context manager.

MUTATIONS, each turns the named tests red:
- `_counted_tool_schemas` ignores `include_tools` -> the server-count, estimate, broadcast
  and purge-before-routing tests;
- the estimate leaves the schema out on an API backend again -> the API-session test;
- `_counted_tool_schemas` back on TOOLS instead of `_tools_and_choice` -> the resident-array
  test; a post-routing count without `not disable_tools` -> the disable_tools case of the
  chat_step test;
- the API count without tools back on the provider's last figure -> the before-routing test;
- chat_step drops `include_tools=not _will_route` from a pre-routing call -> the chat_step test;
- the purge without the anchor insertion -> the purge test;
- the broadcast back on `SessionManager(state_registry=...).load(session_id)` -> the broadcast
  test;
- `ContextStateProvider.restore_state` assigns `max_tokens` / `trigger_threshold` again ->
  the restore test.
"""
import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import vaf.core.local_engine as local_engine
from vaf.core.agent import Agent
from vaf.core.context import ContextManager
from vaf.core.state_providers.context_state import ContextStateProvider

_REPO = Path(__file__).resolve().parents[1]
_SCHEMA = [{"type": "function", "function": {"name": f"tool_{i}", "description": "x" * 4000,
                                             "parameters": {"type": "object", "properties": {}}}}
           for i in range(40)]


class _ServerAgent(Agent):
    """The server path with a fixed, large tool schema and no model behind it."""

    @property
    def TOOLS(self):  # noqa: N802 - the attribute the agent loop reads
        return self._schema


@pytest.fixture
def server_agent(monkeypatch, tmp_path):
    monkeypatch.setattr(ContextManager, "ARCHIVE_DIR", tmp_path / "context_archive")
    # A counter that is exact enough to reason about: one token per four characters.
    monkeypatch.setattr(local_engine, "local_token_count",
                        lambda text, *a, **kw: len(text) // 4)
    monkeypatch.setattr(local_engine, "current", lambda *a, **kw: local_engine.VLLM)
    monkeypatch.setattr(local_engine, "local_context_window", lambda *a, **kw: 32768)

    a = _ServerAgent.__new__(_ServerAgent)
    a._schema = list(_SCHEMA)
    a.api_backend = None
    a.use_server = True
    a.llm = None
    a.tools = {}
    a.config = {"n_ctx": 128000}
    a.current_session_id = None
    a.main_persistence = None
    a.context_manager = ContextManager(max_tokens=32768)
    a._generate_summary = lambda *args, **kw: None
    a.history = [{"role": "system", "content": "You are VAF."},
                 {"role": "user", "content": "Hallo"},
                 {"role": "assistant", "content": "Hallo, was kann ich tun?"}]
    return a


def _schema_tokens():
    return len(json.dumps(_SCHEMA)) // 4


def test_the_server_count_leaves_the_schema_out_only_when_asked(server_agent):
    with_tools, window = server_agent.get_token_usage()
    without, window2 = server_agent.get_token_usage(include_tools=False)
    assert window == window2 == 32768
    assert with_tools - without == _schema_tokens()
    assert with_tools > 32768 > without, "the fixture no longer models the live case"


def test_the_estimate_leaves_the_schema_out_only_when_asked(server_agent):
    with_tools, _ = server_agent._estimate_token_usage()
    without, _ = server_agent._estimate_token_usage(include_tools=False)
    assert with_tools - without == int(len(json.dumps(_SCHEMA)) / 3.0)


def test_an_api_session_without_a_request_counts_the_routed_schema(server_agent):
    """An API request carries its tool array as input tokens. Until the provider reported a
    request's usage, the estimate is all there is, so it must count the schema too."""
    server_agent.use_server = False
    server_agent.api_backend = SimpleNamespace(last_request_usage={},
                                               get_model_context_window=lambda model=None: 128000)
    server_agent.provider = "openai"
    with_tools, window = server_agent.get_token_usage()
    without, _ = server_agent.get_token_usage(include_tools=False)
    assert window == 128000
    assert with_tools - without == int(len(json.dumps(_SCHEMA)) / 3.0)


def test_the_count_before_routing_ignores_the_providers_last_figure(server_agent):
    """The reported usage of the last request includes its tool array and its output; a count
    that leaves the schema out must not take it, or the router's safety net reads a short
    history as tight."""
    server_agent.use_server = False
    server_agent.api_backend = SimpleNamespace(
        last_request_usage={"input_tokens": 90000, "output_tokens": 500},
        get_model_context_window=lambda model=None: 128000)
    server_agent.provider = "openai"
    without, _ = server_agent.get_token_usage(include_tools=False)
    with_tools, _ = server_agent.get_token_usage()
    assert without < 1000, without
    assert with_tools == 90500


def test_the_broadcast_counts_the_tools_the_call_counts(server_agent, monkeypatch):
    import vaf.core.agent as agent_module
    import vaf.core.web_interface as web_interface

    sent = []

    class _Web:
        def _push_session_update(self, session_id, payload):
            sent.append(payload["stats"])

    monkeypatch.setattr(agent_module, "_emit_to_web_ui", lambda: True)
    monkeypatch.setattr(web_interface, "get_web_interface", lambda: _Web())
    server_agent.current_session_id = "web-test-session"
    server_agent._broadcast_context_status(include_tools=False)
    server_agent._broadcast_context_status()
    assert [s["tools_tokens"] for s in sent] == [0, _schema_tokens()]
    assert sent[1]["tokens"] - sent[0]["tokens"] == _schema_tokens()


def test_every_count_reads_the_one_schema_rule():
    """No hand copy of "which schema counts" is left: each of the four counts asks
    `_counted_tool_schemas`, and nothing serializes `self.TOOLS` for a count by itself."""
    source = (_REPO / "vaf" / "core" / "agent.py").read_text(encoding="utf-8")
    assert "dumps(self.TOOLS)" not in source
    tree = ast.parse(source)
    readers = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            calls = [c for c in ast.walk(node) if isinstance(c, ast.Call)
                     and isinstance(c.func, ast.Attribute) and c.func.attr == "_counted_tool_schemas"]
            if calls:
                readers[node.name] = len(calls)
    # get_token_usage has two counting lanes (the server's /tokenize and the local tokenizer).
    assert readers == {"_estimate_token_usage": 1, "get_token_usage": 2,
                       "_broadcast_context_status": 1}, readers


def test_a_history_that_fits_is_not_purged_before_routing(server_agent):
    """The history fits the window, the whole registry's schema would not."""
    before = list(server_agent.history)
    server_agent.manage_context(include_tools=False)
    assert server_agent.history == before

    # The same call counting the full schema is the live incident: an emergency purge.
    server_agent.history = before + [{"role": "user", "content": f"Nachricht {i}"} for i in range(8)]
    server_agent.context_manager.compress = lambda history, working_memory=None: history
    server_agent.manage_context()
    assert len(server_agent.history) == 7, "the full schema no longer overflows the window"


def test_the_purge_keeps_the_turns_request(server_agent):
    """Inside a tool loop the person's request lies more than six messages back."""
    request = {"role": "user", "content": "Installiere bitte zehn Pakete"}
    loop = []
    for i in range(6):
        loop.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": "host_bash", "arguments": "{}"}}]})
        loop.append({"role": "tool", "tool_call_id": f"c{i}", "content": f"result {i}"})
    old = [{"role": "user", "content": "Frueher"}, {"role": "assistant", "content": "Ja."}]
    server_agent.history = [server_agent.history[0]] + old + [request] + loop
    server_agent.context_manager.compress = lambda history, working_memory=None: history

    server_agent.manage_context()

    assert server_agent.history == [server_agent.history[0], request] + loop[-6:]


class _Stop(BaseException):
    """Ends the turn at its first post-routing count; BaseException so no handler eats it."""


@pytest.mark.parametrize("disable_tools", [False, True])
def test_chat_step_counts_without_the_schema_only_before_routing(monkeypatch, tmp_path,
                                                                  disable_tools):
    """Every count chat_step takes before routing leaves the schema out; once the turn's
    tools are chosen, the counts include them again, unless the request goes out without
    tools (disable_tools), which sends no schema to count."""
    from vaf.core.platform import Platform

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    a = Agent(register_signals=False, run_kind="chat",
              config_overrides={"provider": "openai", "api_key_openai": "sk-test"})
    a.current_session_id = "green123456"
    monkeypatch.setattr(a, "_try_workflow", lambda *args, **kw: None)
    seen, routed = [], []

    def record(name):
        def _call(*args, include_tools=True, **kw):
            seen.append((name, include_tools, bool(routed)))
            if name == "get_token_usage":
                if routed:
                    raise _Stop()
                return 100, 32768
            return None
        return _call

    for name in ("get_token_usage", "manage_context", "_broadcast_context_status"):
        monkeypatch.setattr(a, name, record(name))
    monkeypatch.setattr(a, "_select_turn_tools", lambda *args, **kw: routed.append(1))

    with pytest.raises(_Stop):
        a.chat_step(user_input="Hallo", raw_user_input="Hallo", stream_callback=lambda t: None,
                    disable_tools=disable_tools)

    before = [s for s in seen if not s[2]]
    after = [s for s in seen if s[2]]
    assert {s[0] for s in before} == {"get_token_usage", "manage_context", "_broadcast_context_status"}
    assert all(s[1] is False for s in before), before
    assert after and all(s[1] is (not disable_tools) for s in after), after


def test_the_count_reads_the_array_the_request_sends(server_agent):
    """On a provider with `allowed_tools` the request sends the resident array, which can be
    larger than the routed set: the count asks the request builder, not TOOLS."""
    resident = _SCHEMA + _SCHEMA
    server_agent._tools_and_choice = lambda disable_tools: (resident, {"type": "allowed_tools"})
    with_tools, _ = server_agent.get_token_usage()
    without, _ = server_agent.get_token_usage(include_tools=False)
    assert with_tools - without == len(json.dumps(resident)) // 4


def test_the_broadcast_does_not_restore_the_saved_snapshot(server_agent, monkeypatch, tmp_path):
    import vaf.core.agent as agent_module
    import vaf.core.session as session_module
    import vaf.core.web_interface as web_interface
    from vaf.core.session import Session, SessionManager
    from vaf.core.session_state import StateRegistry

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(session_module, "default_sessions_dir", lambda: sessions)
    monkeypatch.setattr(session_module, "_manager", None)

    old = ContextManager(max_tokens=128000)
    old.state.narrative_summary = "OLD SUMMARY"
    old.intent.primary_goal = "OLD GOAL"
    saved_registry = StateRegistry()
    saved_registry.register("context", ContextStateProvider(old))
    session = Session(id="web-broadcast-test", name="t")
    session.runtime_state = saved_registry.capture_snapshot().to_dict()
    session.runtime_state["user_turn_count"] = 7
    SessionManager().save(session, sync_state=False)

    sent = []

    class _Web:
        def _push_session_update(self, session_id, payload):
            sent.append(payload["stats"])

    monkeypatch.setattr(agent_module, "_emit_to_web_ui", lambda: True)
    monkeypatch.setattr(web_interface, "get_web_interface", lambda: _Web())
    a = server_agent
    a.current_session_id = "web-broadcast-test"
    a.context_manager.state.narrative_summary = "NEW SUMMARY"
    a.context_manager.intent.primary_goal = "NEW GOAL"
    a.state_registry = StateRegistry()
    a.state_registry.register("context", ContextStateProvider(a.context_manager))

    a._broadcast_context_status()

    assert sent and sent[0]["user_turn_count"] == 7, "the broadcast did not read the session file"
    cm = a.context_manager
    assert (cm.max_tokens, cm.state.narrative_summary, cm.intent.primary_goal) == \
        (32768, "NEW SUMMARY", "NEW GOAL")


def test_a_restore_keeps_the_engines_window():
    cm = ContextManager(max_tokens=32768)
    limits = (cm.max_tokens, cm.trigger_threshold)
    provider = ContextStateProvider(cm)
    provider.restore_state({"max_tokens": 128000, "trigger_threshold": 0.5,
                            "state": {"narrative_summary": "S"}})
    assert (cm.max_tokens, cm.trigger_threshold) == limits
    assert cm.state.narrative_summary == "S", "the chat's own state is still restored"
    state = provider.get_state()
    assert state["max_tokens"] == 32768 and "trigger_threshold" in state
