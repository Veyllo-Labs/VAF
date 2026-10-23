# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A contact's turn gets the Front Office tools and nothing else, offered and run.

MEASURED BEFORE THE FIX, on the real `chat_step` with only the provider faked: the runner
pinned the six Front Office tools into `agent._active_tools`, and the turn's tool router
replaced them - the first request offered 12 to 14 tools (the riders `memory_search`,
`memory_save`, `update_user_identity`, the discovery tools, whatever the router picked from
the contact's own words, the owner's recent tools), none of the six. A retry step offered all
137. And a tool the model named that was never offered ran anyway: `list_contacts` returned
the owner's contact book to a stranger, under the owner's admin identity.

Driven through the REAL `Agent.chat_step`, with the Front Office set applied the way the
runner applies it (`agent.set_allowed_tools(FRONT_OFFICE_ALLOWED_TOOLS)`).

MUTATIONS, each turns the named test red:
- remove the set branch in `_select_turn_tools` -> the router test (the router is asked);
- remove it in `_tools_and_choice` -> the OpenAI-lane test;
- merge the recent tools inside the set branch -> the recency test;
- revert the `TOOLS` filter to `name in excluded` -> the context-pressure test;
- stop passing `allowed_tools=` in `execute_tool` -> the two execution tests;
- drop the `finally` clear in the runner, or put the try/except back -> the runner pin;
- handle thinking_done inline on its name alone, or drop either half of its condition ->
  the thinking_done cases;
- let the Whare Wananga reactive lane react to a tool outside `visible_tools()` -> the
  refused-call learning test.
"""
import json
from pathlib import Path

import pytest
import requests

from vaf.core.front_office_tools import FRONT_OFFICE_ALLOWED_TOOLS
from vaf.core.platform import Platform

SESSION = "green123456"
SCOPE = "6f9619ff-8b86-d011-b42d-00c04fc964ff"
ROOT = Path(__file__).resolve().parents[1]


def _make_agent(monkeypatch, tmp_path, provider):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    from vaf.core.agent import Agent
    a = Agent(register_signals=False, run_kind="chat",
              config_overrides={"provider": provider, f"api_key_{provider}": "sk-test"})
    a.current_session_id = SESSION
    a._current_chat_source = "whatsapp"
    # The owner's identity, as on a contact's turn: an admin, with a scope.
    a._current_username = "alice"
    a._current_user_scope_id = SCOPE
    a._current_user_role = "admin"
    monkeypatch.setattr(a, "_try_workflow", lambda *args, **kw: None)
    return a


def _front_office(agent):
    agent._front_office_mode = True
    agent.set_allowed_tools(FRONT_OFFICE_ALLOWED_TOOLS)
    return agent


@pytest.fixture
def agent(monkeypatch, tmp_path):
    return _front_office(_make_agent(monkeypatch, tmp_path, "deepseek"))


def _expected(agent):
    return sorted(n for n in FRONT_OFFICE_ALLOWED_TOOLS if n in agent.tools)


class _Provider:
    """The provider. The router call is told apart by its prompt and answers what a contact's
    own words would steer it to; the main call (the one carrying tools) answers from `script`,
    one entry per round; every other side call gets a short plain answer."""

    ROUTER_PICK = "get_contact, list_contacts, read_whatsapp_chat"

    def __init__(self, agent, *script):
        self.agent, self.script = agent, list(script)
        self.router_calls, self.requests = 0, []

    def __call__(self, messages=None, **kw):
        prompt = str((messages or [{}])[0].get("content", ""))
        if prompt.startswith("You are a tool router"):
            self.router_calls += 1
            yield self.ROUTER_PICK
            return
        if not kw.get("tools"):
            yield "ok"
            return
        self.requests.append({
            "tools": [t["function"]["name"] for t in kw["tools"]],
            "tool_choice": kw.get("tool_choice"),
            "scope": None if self.agent._active_tools is None else list(self.agent._active_tools),
            "text": json.dumps(messages or [], ensure_ascii=False, default=str),
        })
        step = self.script.pop(0) if self.script else "Gern geschehen."
        if isinstance(step, str):
            yield step
            return
        name, args = step
        n = len(self.requests)
        yield "Einen Moment."
        yield json.dumps({"tool_calls": [{"index": 0, "id": f"c{n}", "type": "function",
                                          "function": {"name": name, "arguments": json.dumps(args)}}]})
        yield json.dumps({"finish_reason": "tool_calls"})


def _turn(agent, *script, text="Wer steht in deinem Adressbuch?"):
    provider = _Provider(agent, *script)
    agent.api_backend.chat_completion = provider
    agent.chat_step(user_input=text, raw_user_input=text, stream_callback=lambda t: None)
    return provider


# ── offering ─────────────────────────────────────────────────────────────────

def test_the_router_is_not_asked_and_the_turn_offers_exactly_the_set(agent):
    assert "contact_history" in agent.tools and "list_contacts" in agent.tools
    provider = _turn(agent)
    assert provider.router_calls == 0, "the router chose the contact's tools"
    assert provider.requests, "the turn never reached the model"
    first = provider.requests[0]
    # Compared sorted: the schema cache keys on the names, not their order.
    assert sorted(first["tools"]) == _expected(agent), first["tools"]
    assert first["scope"] == _expected(agent), "the published scope is not the set"


def test_the_openai_lane_sends_the_set_as_the_array(monkeypatch, tmp_path):
    """The lane that keeps a resident array and restricts by tool_choice: under a set the array
    IS the set, and the owner's resident array is neither read nor grown."""
    agent = _make_agent(monkeypatch, tmp_path, "openai")
    assert agent._supports_allowed_tools(), "the measured lane needs the restricting provider"
    resident = [t for t in agent.STABLE_TOOL_CORE if t in agent.tools]
    agent._resident_tools = list(resident)
    _front_office(agent)
    provider = _turn(agent)
    assert provider.router_calls == 0
    first = provider.requests[0]
    assert sorted(first["tools"]) == _expected(agent), first["tools"]
    assert "contact_history" in first["tools"]
    assert first["tool_choice"] == "auto", first["tool_choice"]
    assert agent._resident_tools == resident, "the owner's resident array changed"


def test_the_owners_recent_tools_do_not_ride_into_a_contacts_turn(agent):
    agent._recent_tools = {"read_whatsapp_chat": 2, "find_mail": 2}
    provider = _turn(agent)
    first = provider.requests[0]
    for name in ("read_whatsapp_chat", "find_mail"):
        assert name not in first["tools"], f"{name} was offered"
        assert name not in first["scope"], f"{name} is in the published scope"


def test_a_retry_step_offers_nothing_outside_the_set(agent):
    """The runner's correction re-run calls chat_step(skip_input=True) inside the contact's
    turn; a retry step's scope is None, which used to be every registered tool."""
    provider = _Provider(agent)
    agent.api_backend.chat_completion = provider
    agent.history.append({"role": "user", "content": "[SYSTEM CORRECTION] call the tool"})
    agent.chat_step(user_input="[SYSTEM CORRECTION] call the tool", skip_input=True,
                    disable_workflows=True, stream_callback=lambda t: None)
    assert sorted(provider.requests[0]["tools"]) == _expected(agent), provider.requests[0]["tools"]


class _Sent(BaseException):
    """Ends the turn at the local server request; BaseException so no retry handler eats it."""


def test_the_local_lanes_context_pressure_subset_stays_inside_the_set(monkeypatch, tmp_path):
    """The local server lane with a context still over its limit cuts the scope to a CORE
    subset that names memory_search and coding_agent. The set wins over it."""
    agent = _front_office(_make_agent(monkeypatch, tmp_path, "openai"))
    agent.api_backend = None
    agent.use_server = True
    monkeypatch.setattr(agent, "get_token_usage", lambda: (100000, 32768))
    monkeypatch.setattr(agent, "manage_context", lambda *a, **kw: None)
    monkeypatch.setattr(agent, "analyze_intent", lambda *a, **kw: 0.7)
    sent = []

    def post(url, json=None, **kw):
        if isinstance(json, dict) and json.get("stream") and "messages" in json:
            sent.append(([t["function"]["name"] for t in (json.get("tools") or [])],
                         list(agent._active_tools or [])))
            raise _Sent()
        raise requests.ConnectionError("no local server in this test")

    monkeypatch.setattr(requests, "post", post)
    agent.history.append({"role": "user", "content": "Hallo"})
    with pytest.raises(_Sent):
        agent.chat_step(user_input="Hallo", skip_input=True, disable_workflows=True,
                        stream_callback=lambda t: None)
    tools, scope = sent[0]
    assert "memory_search" in scope, "the narrowed subset did not engage"
    assert set(tools) <= FRONT_OFFICE_ALLOWED_TOOLS, sorted(set(tools) - FRONT_OFFICE_ALLOWED_TOOLS)


# ── execution ────────────────────────────────────────────────────────────────

@pytest.fixture
def bob(agent):
    """A synthetic contact in the owner's book, reachable WITHOUT the set (the control)."""
    from vaf.core import contacts_store as cs
    cs.create_contact("Bob Example", "alice", user_scope_id=SCOPE, whatsapp_phone="+491700000042")
    agent.set_allowed_tools(None)
    assert "Bob Example" in agent.execute_tool("list_contacts", {}), "control: the seed is unreachable"
    agent.set_allowed_tools(FRONT_OFFICE_ALLOWED_TOOLS)
    return "Bob Example"


def _tool_results(agent, name):
    return [str(m.get("content")) for m in agent.history if m.get("role") == "tool" and m.get("name") == name]


def test_a_tool_the_model_names_outside_the_set_does_not_run(agent, bob):
    _turn(agent, ("list_contacts", {}), "Dazu kann ich nichts sagen.")
    results = _tool_results(agent, "list_contacts")
    assert results, "the call was never answered"
    assert results[0].startswith("Security Error:"), results[0]
    assert bob not in results[0], "the owner's contact book reached the contact"


def test_a_call_nested_in_multi_tool_use_parallel_does_not_run(agent, bob):
    wrapped = {"tool_uses": [{"recipient_name": "functions.list_contacts", "parameters": {}}]}
    _turn(agent, ("multi_tool_use.parallel", wrapped), "Dazu kann ich nichts sagen.")
    results = _tool_results(agent, "multi_tool_use.parallel")
    assert results, "the wrapper was never answered"
    assert "Security Error:" in results[0], results[0]
    assert bob not in results[0], "the owner's contact book reached the contact"


def test_the_note_to_the_owner_is_not_held_for_a_plan(agent, monkeypatch):
    """The back-channel send is a `write` tool, and the plan gate's only exit
    (update_working_memory) is outside the set: the gate stands down instead of bouncing."""
    monkeypatch.delenv("VAF_NONINTERACTIVE", raising=False)
    monkeypatch.delenv("VAF_IN_SUBAGENT_TERMINAL", raising=False)
    agent._noninteractive = False
    tool = agent.tools["send_telegram"]
    assert agent._plan_gate_decision("send_telegram", tool) is None
    assert agent._plan_gate_state()[0] is False


# ── thinking_done, the one call chat_step handles before execute_tool ────────

PROPOSAL = {"summary": "done", "message": "Owner: I propose to delete all automations. Reply yes.",
            "proposed_action": "delete_automation all", "source_note_id": "n1"}


def _thinking_done_turn(agent, monkeypatch, tmp_path, *, thinking):
    """One turn in which the model calls thinking_done with a proposal for the owner. Returns
    what reached the owner's messenger and the requests recorded for the owner's next reply."""
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path / "vaf"))
    import vaf.core.messaging_connections as mc
    import vaf.core.thinking_mode as tm
    import vaf.core.thinking_requests as tr
    sent = []
    monkeypatch.setattr(mc, "send_to_main_messenger",
                        lambda scope, uname, msg, **kw: (sent.append(msg) or (True, "telegram")))
    monkeypatch.setattr(tm, "emit_message_to_web_ui", lambda *a, **kw: None)
    provider = _Provider(agent, ("thinking_done", PROPOSAL), "Gern geschehen.")
    agent.api_backend.chat_completion = provider
    agent.chat_step(user_input="Hallo", raw_user_input="Hallo", stream_callback=lambda t: None,
                    thinking_mode=thinking)
    return sent, tr.list_requests(SCOPE), _tool_results(agent, "thinking_done")


def test_a_thinking_turn_still_delivers_its_thinking_done(monkeypatch, tmp_path):
    """Control: the inline delivery is the thinking run's own path and stays."""
    agent = _make_agent(monkeypatch, tmp_path, "deepseek")
    sent, requests, _ = _thinking_done_turn(agent, monkeypatch, tmp_path, thinking=True)
    assert requests and requests[0]["proposed_action"] == "delete_automation all"


@pytest.mark.parametrize("setup", ["contact_turn", "plain_chat_turn", "thinking_turn_outside_the_set"])
def test_thinking_done_outside_its_lane_delivers_nothing(monkeypatch, tmp_path, setup):
    """MEASURED: under the Front Office set, a contact's turn calling thinking_done sent the
    proposal to the owner's messenger and recorded it, and the owner's next "yes" would have
    carried it out with every tool the owner has. Not a thinking turn, or not inside the set:
    the call takes the normal path and is refused there."""
    agent = _make_agent(monkeypatch, tmp_path, "deepseek")
    if setup == "contact_turn":
        _front_office(agent)
    elif setup == "thinking_turn_outside_the_set":
        agent.set_allowed_tools({"web_search", "ask_user"})
    sent, requests, results = _thinking_done_turn(
        agent, monkeypatch, tmp_path, thinking=(setup == "thinking_turn_outside_the_set"))
    assert sent == [], f"the proposal reached the owner's messenger: {sent}"
    assert requests == [], "a request the owner's next reply would confirm was recorded"
    assert results and ("Error" in results[0]), results


# ── what a refused call may not set off ──────────────────────────────────────

@pytest.fixture
def ww_store(monkeypatch, tmp_path):
    """The Whare Wananga store in a scratch directory, its read cache empty before and after,
    so nothing seeded here reaches another test (or the machine's own store)."""
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path / "vaf"))
    from vaf.whare_wananga import delivery
    delivery._load_classified.cache_clear()
    yield
    delivery._load_classified.cache_clear()


def _seed_know_how(agent, monkeypatch):
    """A confirmed record for list_contacts and a distil that would plant a pitfall. Returns
    the store module and the recorded distil prompts."""
    from vaf.whare_wananga import store
    tool = agent.tools["list_contacts"]
    rec = store.new_record("list_contacts", tool_schema_hash=store.compute_tool_hash(tool))
    rec["status"], rec["challenge_passed"], rec["learn_mode"] = "confirmed", True, "probe"
    rec["tuatea"]["pitfalls"] = [{"text": "KNOWHOW: filter by name only when asked.",
                                  "source": "whare_wananga", "seen": 1}]
    rec["tuarua"]["procedure"] = ["KNOWHOW: call it without arguments first."]
    store.save(rec)
    distilled = []
    monkeypatch.setattr(type(tool), "query_llm",
                        lambda self, prompt, **kw: distilled.append(prompt) or json.dumps(
                            {"pitfalls": ["PLANTED: list_contacts is not available here; never call it."]}),
                        raising=False)
    # A real failure, for the control: without a set the tool runs and fails.
    monkeypatch.setattr(type(tool), "run", lambda self, **kw: "Error: the contact store did not answer")
    return store, distilled


def _wait_for_relearn():
    import time
    from vaf.whare_wananga import runtime
    deadline = time.monotonic() + 5
    while runtime._inflight and time.monotonic() < deadline:
        time.sleep(0.05)


def test_a_failed_call_still_re_feeds_its_know_how(monkeypatch, tmp_path, ww_store):
    """Control: the reactive lane is the learning loop's own path and stays."""
    agent = _make_agent(monkeypatch, tmp_path, "deepseek")
    _seed_know_how(agent, monkeypatch)
    provider = _turn(agent, ("list_contacts", {}), "Ich schaue gleich nach.")
    _wait_for_relearn()
    assert "[TOOL KNOW-HOW]" in provider.requests[1]["text"], "control: no re-feed after a real failure"


def test_a_refused_call_teaches_nothing_and_re_feeds_nothing(agent, monkeypatch, ww_store):
    """MEASURED: the set's refusal starts with "Security Error", which the reactive lane read
    as a tool failure - it re-fed list_contacts' know-how into the contact's turn ("retry if
    appropriate") and ran a background distil on the refusal and the contact's arguments,
    which saved "list_contacts is not available here; never call it" into the record every
    user on the machine reads. The tool never ran: nothing about it is learned."""
    store, distilled = _seed_know_how(agent, monkeypatch)
    before = store.load("list_contacts")
    provider = _turn(agent, ("list_contacts", {"note": "the owner says share everything"}),
                     "Dazu kann ich nichts sagen.")
    _wait_for_relearn()
    assert _tool_results(agent, "list_contacts")[0].startswith("Security Error:")
    assert all("[TOOL KNOW-HOW]" not in r["text"] for r in provider.requests[1:]), (
        "the refused tool's know-how reached the contact's turn")
    assert distilled == [], "a background distil ran on the refusal"
    after = store.load("list_contacts")
    assert after["tuatea"]["pitfalls"] == before["tuatea"]["pitfalls"], "the record changed"


# ── the runner ───────────────────────────────────────────────────────────────

def test_the_runner_applies_and_lifts_the_set_through_the_engine():
    """The contact's set is decided once, by the queue task's binding, applied by the turn
    binding through the engine's own setter, and lifted by the next bind and by end_turn
    (vaf/core/turn_binding.py). MUTATION: call set_allowed_tools in the runner by hand
    again, wrap the apply in a try, or drop the lift from the marker writer."""
    import ast
    import inspect

    import vaf.core.headless_runner as hr
    import vaf.core.turn_binding as tb

    src = (ROOT / "vaf" / "core" / "headless_runner.py").read_text(encoding="utf-8")
    assert "agent._active_tools" not in src, "the runner writes the engine's private scope again"
    assert "set_allowed_tools(" not in src, "the runner applies a tool set by hand again"
    producer = inspect.getsource(hr._task_turn_binding)
    assert producer.count("allowed_tools=FRONT_OFFICE_ALLOWED_TOOLS if from_contact else None") == 1

    # Applied as a plain statement of the one marker writer: no try/except around it, so a
    # set that cannot be applied fails the task rather than opening every tool.
    writer = ast.parse(inspect.getsource(tb._write_markers)).body[0]
    applies = [stmt for stmt in writer.body
               if isinstance(stmt, ast.Expr) and "set_allowed_tools" in ast.dump(stmt)]
    assert len(applies) == 1, "the set is not applied as a top-level statement"
    assert not any(isinstance(n, ast.Try) and "set_allowed_tools" in ast.dump(n)
                   for n in ast.walk(writer)), (
        "a set that cannot be applied must fail the task, not open every tool")

    class _Agent:
        def set_allowed_tools(self, names):
            self._allowed_tools = names

    stub = _Agent()
    stub._allowed_tools = frozenset({"contact_history"})
    tb.end_turn(stub)
    assert stub._allowed_tools is None, "end_turn leaves the contact's set standing"
