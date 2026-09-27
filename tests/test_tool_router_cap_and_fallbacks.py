# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The router's cap counts the turn's tools and never the two discovery tools, every narrowed
tool set keeps `search_tools`, and a failed router call does not load every tool.

MEASURED BEFORE THE FIX, on the real `chat_step`: `list_tools` and `search_tools` took two of
the twelve places, so a turn whose router picked twelve tools lost the two that sort last
(`send_mail`, `web_search`), and a one-tool weather turn already filled all twelve. The
context-pressure subset of the local server lane carried `list_tools` but not `search_tools`,
the one that finds a tool by what it does, although the design doc promised both in every
restricted set. And the doc said a failed router call loads ALL tools; it loads the discovery
tools and the recent ones.

MUTATION: count the pins against the cap again and the first two tests go red; drop
`search_tools` from the context-pressure subset and the third goes red; make a failed router
call load every tool and the fourth goes red.
"""
import pytest
import requests

from vaf.core.agent import _apply_tool_cap
from vaf.core.platform import Platform

DISCOVERY = {"list_tools", "search_tools"}
TWELVE = ["web_search", "research_agent", "coding_agent", "git_status", "git_add_commit", "git_log",
          "list_calendar_events", "create_calendar_event", "inbox", "find_mail",
          "list_email_accounts", "send_mail"]


@pytest.fixture
def agent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    from vaf.core.agent import Agent
    a = Agent(register_signals=False, run_kind="chat",
              config_overrides={"provider": "openai", "api_key_openai": "sk-test"})
    a.current_session_id = "green123456"
    monkeypatch.setattr(a, "_try_workflow", lambda *args, **kw: None)
    return a


def _tools_the_model_sees(agent, text="Hallo"):
    """Run one turn and return the tool set of the first main-model call (None = ALL)."""
    seen = []

    def model(messages=None, **kw):
        if not seen:
            seen.append(None if agent._active_tools is None else list(agent._active_tools))
        yield "Fertig."

    agent.api_backend.chat_completion = model
    agent.chat_step(user_input=text, raw_user_input=text, stream_callback=lambda t: None)
    return seen[0]


def test_the_discovery_tools_come_on_top_of_the_cap():
    incoming = [f"tool_{i:02d}" for i in range(12)] + ["list_tools", "search_tools"]
    out = _apply_tool_cap(incoming, 12, DISCOVERY)
    assert set(out) == set(incoming), "twelve places for the turn, the two discovery tools on top"


def test_a_turn_with_twelve_task_tools_loses_none_of_them(agent, monkeypatch):
    task = [t for t in TWELVE if t in agent.tools]
    assert len(task) == 12, "the measured shape needs all twelve registered"
    monkeypatch.setattr(agent, "_route_tools", lambda text: list(task))
    tools = _tools_the_model_sees(agent)
    assert set(task) <= set(tools), f"cut: {sorted(set(task) - set(tools))}"
    assert DISCOVERY <= set(tools)
    assert len(tools) == 14


class _Sent(BaseException):
    """Ends the turn at the local server request; BaseException so no retry handler eats it."""


def test_the_context_pressure_subset_keeps_search_tools(agent, monkeypatch):
    """The local server lane, context still over its limit after compression: the tool set is
    cut to the CORE subset. `search_tools` must be in it."""
    agent.config["router_max_tools"] = 40
    agent.api_backend = None
    agent.use_server = True
    many = sorted(agent.tools)[:20]
    monkeypatch.setattr(agent, "_route_tools", lambda text: list(many))
    monkeypatch.setattr(agent, "get_token_usage", lambda: (100000, 32768))
    monkeypatch.setattr(agent, "manage_context", lambda *a, **kw: None)
    monkeypatch.setattr(agent, "analyze_intent", lambda *a, **kw: 0.7)
    sent = []

    def post(url, json=None, **kw):
        if isinstance(json, dict) and json.get("stream") and "messages" in json:
            sent.append([t["function"]["name"] for t in (json.get("tools") or [])])
            raise _Sent()
        raise requests.ConnectionError("no local server in this test")

    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(_Sent):
        agent.chat_step(user_input="Hallo", raw_user_input="Hallo", stream_callback=lambda t: None)
    assert sent, "the turn never reached the local server request"
    assert len(sent[0]) < len(many), "the context-pressure subset did not engage"
    assert DISCOVERY <= set(sent[0])


def test_a_failed_router_call_offers_discovery_not_every_tool(agent):
    """The router's own LLM call raises: the turn gets the discovery tools (plus the tools of
    the last turns, none in a fresh chat), not ALL tools."""
    main = []

    def backend(messages=None, **kw):
        prompt = str((messages or [{}])[0].get("content", ""))
        if prompt.startswith("You are a tool router"):
            raise RuntimeError("provider down")
        if not main:
            main.append(None if agent._active_tools is None else list(agent._active_tools))
        yield "Fertig."

    agent.api_backend.chat_completion = backend
    agent.chat_step(user_input="Hallo", raw_user_input="Hallo", stream_callback=lambda t: None)
    assert main and main[0] is not None, "a failed router call loaded every tool"
    assert set(main[0]) == DISCOVERY
