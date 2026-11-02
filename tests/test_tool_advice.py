# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A tool result that advises a tool hands that tool over for the rest of the turn.

Live incident: the router gave a "10 pip installs + 10 git clones" turn the workflow tools but
neither host_bash nor python_sandbox. The workflow refusal told the model to "Run it with
host_bash yourself", the model could not call a tool its turn did not carry, and it told the
person to run the commands by hand. Prose that names a tool is not the tool.

`vaf.advise_tools` is the producer side: code inside a `ToolCaller.call` names the tools its
answer points at, the funnel keeps the ones THIS caller may run (its own stages, asked without
a call), and `ToolCallOutcome.advised_tools` carries them. The chat lane adds them to the
turn's tool set (`Agent._take_tool_advice`), so the next request offers them, on every
backend, and nothing is written into the history.

MUTATIONS, each turns the named tests red:
- return () instead of None outside a scope -> test_outside_a_call_nothing_is_recorded;
  drop the dedup -> test_advice_is_recorded_for_the_call;
- drop any one stage from `_offerable` -> its own row of test_advice_never_widens; let an
  exception answer True -> the raising-resolver and raising-offer-check rows;
- let the inner scope hand its names up -> test_nested_call_does_not_leak; skip the
  `closed` check -> test_late_advice_is_ignored;
- drop the `advise_tools` call in `_dispatch` -> test_unknown_tool_advises_its_correction;
- skip `_take_tool_advice` in `_execute_tool_outcome` -> the next-request tests; append a
  note into the history -> test_no_history_message_is_added; drop the `visible_tools()`
  check in `_pin_advised_tools` -> test_all_tools_and_allowed_set_unchanged;
- let `_pin_advised_tools` keep a list `TOOLS` does not read -> the count test;
- drop the re-pin after the context-pressure narrowing ->
  test_advice_survives_the_pressure_subset; stop clearing in `_select_turn_tools` ->
  test_advice_is_not_carried_into_the_next_turn;
- drop the `advise_tools` call in the plan gate -> test_plan_gate_advises_working_memory;
- re-add the search_tools prose hook -> test_the_prose_hook_is_gone; advise without the
  filter -> test_search_does_not_advise_a_refused_tool;
- word the unknown-tool correction without `keep=self._offerable` ->
  test_the_correction_names_only_what_the_caller_may_run;
- let `_offerable` ask the policy with `quiet=False`, or let `quiet` keep the real logger ->
  test_an_offer_check_logs_nothing.
"""
import ast
import contextlib
import contextvars
import json
import threading
from pathlib import Path

import pytest
import requests

from vaf.core.platform import Platform
from vaf.core.tool_dispatch import (ToolCaller, advise_tools, get_account_allowlist_resolver,
                                    set_account_allowlist_resolver)
from vaf.tools.base import BaseTool

ROOT = Path(__file__).resolve().parents[1]
SESSION = "green123456"
REGULAR = "ab12cd34-0000-0000-0000-000000000000"     # synthetic scope of a regular account


class _Tool(BaseTool):
    description = "a probe"
    permission_level = "read"
    parameters = {"type": "object", "properties": {}}

    def __init__(self, name, fn=None, **attrs):
        super().__init__()
        self.name = name
        self._fn = fn or (lambda **kw: "OK")
        for key, value in attrs.items():
            setattr(self, key, value)

    def run(self, **kwargs):
        return self._fn(**kwargs)


def _registry(*tools):
    return {t.name: t for t in tools}


# ── 1: the producer side ─────────────────────────────────────────────────────

def test_advice_is_recorded_for_the_call():
    seen = []
    advisor = _Tool("advisor", lambda **kw: seen.append(advise_tools("a", "b", "a")) or "OK")
    outcome = ToolCaller(_registry(advisor, _Tool("a"), _Tool("b"))).call("advisor", {})
    assert seen == [("a", "b")]
    assert outcome.advised_tools == ("a", "b")
    assert outcome.result == "OK" and outcome.dispatched is True


def test_outside_a_call_nothing_is_recorded():
    assert advise_tools("a") is None
    # And nothing lingers into the next call: a call that advises nothing carries nothing.
    outcome = ToolCaller(_registry(_Tool("plain"), _Tool("a"))).call("plain", {})
    assert outcome.advised_tools == ()


# ── 2: advice never widens ───────────────────────────────────────────────────

@contextlib.contextmanager
def _resolver(answer):
    previous = get_account_allowlist_resolver()
    if isinstance(answer, Exception):
        def _raise(scope):
            raise answer
        set_account_allowlist_resolver(_raise)
    else:
        set_account_allowlist_resolver(lambda scope: answer)
    try:
        yield
    finally:
        set_account_allowlist_resolver(previous)


@contextlib.contextmanager
def _revoked():
    from vaf.core import revocation
    revocation.revoke_account(REGULAR)
    try:
        yield
    finally:
        revocation.restore_account(REGULAR)


def _host_bash():
    from vaf.tools.host_bash import HostBashTool
    return HostBashTool()


# (case, advised name, extra tools, caller kwargs, context): every case refuses the name.
_NEVER = [
    ("unregistered", "ghost", [], {}, contextlib.nullcontext),
    ("outside_the_allowed_set", "target", [], {"allowed_tools": {"advisor"}},
     contextlib.nullcontext),
    ("admin_only_for_a_regular_account", "admin_tool",
     [_Tool("admin_tool", admin_only=True)], {"user_scope_id": REGULAR, "user_role": "user"},
     contextlib.nullcontext),
    ("host_bash_on_a_channel", "host_bash", [_host_bash()],
     {"source": "telegram", "session_id": "telegram_123456"}, contextlib.nullcontext),
    ("host_bash_without_the_grant", "host_bash", [_host_bash()],
     {"user_scope_id": REGULAR, "user_role": "user"}, contextlib.nullcontext),
    ("resolver_raises", "target", [], {"user_scope_id": REGULAR, "user_role": "user"},
     lambda: _resolver(RuntimeError("db down"))),
    ("revoked_account", "target", [], {"user_scope_id": REGULAR, "user_role": "user"},
     _revoked),
    ("offer_check_says_no", "target", [], {"offer_check": lambda name: name != "target"},
     contextlib.nullcontext),
    ("offer_check_raises", "target", [], {"offer_check": lambda name: 1 / 0},
     contextlib.nullcontext),
]


# The account cannot start the advisor in these two cases (the call is refused first), so the
# filter is asked from an open scope of the same caller, the one `call` opens.
_CANNOT_START = {"resolver_raises", "revoked_account"}


@pytest.mark.parametrize("case,name,extra,kw,ctx", _NEVER, ids=[c[0] for c in _NEVER])
def test_advice_never_widens(case, name, extra, kw, ctx):
    """Each stage the call itself would run refuses the name on its own; the control calls
    below accept the same names when nothing refuses them."""
    seen = []
    advisor = _Tool("advisor", lambda **kw: seen.append(advise_tools(name)) or "OK")
    tools = _registry(advisor, _Tool("target"), *extra)
    with ctx():
        caller = ToolCaller(tools, **kw)
        if case in _CANNOT_START:
            with _advice_scope_of(caller) as scope:
                seen.append(advise_tools(name))
            assert scope.names == [], case
        else:
            outcome = caller.call("advisor", {})
            assert outcome.result == "OK", (case, outcome.result)
            assert outcome.advised_tools == (), case
    assert seen == [()], (case, seen)


@contextlib.contextmanager
def _advice_scope_of(caller):
    """An open advice scope of `caller`, the one `ToolCaller.call` opens."""
    from vaf.core import tool_dispatch as td
    scope = td._AdviceScope(caller)
    token = td._advice_ctx.set(scope)
    try:
        yield scope
    finally:
        td._advice_ctx.reset(token)
        scope.close()


@pytest.mark.parametrize("case", ["admin", "granted_regular", "plain"])
def test_the_control_calls_accept(case):
    """The same names pass when nothing refuses them, so each refusal above is the stage's."""
    seen = []
    want = "host_bash" if case != "plain" else "target"
    advisor = _Tool("advisor", lambda **kw: seen.append(advise_tools(want)) or "OK")
    tools = _registry(advisor, _Tool("target"), _host_bash())
    if case == "granted_regular":
        with _resolver(frozenset({"advisor", "host_bash"})):
            outcome = ToolCaller(tools, user_scope_id=REGULAR, user_role="user").call("advisor", {})
    else:
        outcome = ToolCaller(tools).call("advisor", {})
    assert seen == [(want,)] and outcome.advised_tools == (want,)


# ── 3: one call's advice ─────────────────────────────────────────────────────

def test_nested_call_does_not_leak():
    """A workflow step inside a tool runs through a ToolCaller of its own: its advice goes to
    the engine that reads it, not to the model of the turn. The outer scope is current again
    after the inner call returned."""
    inner_tools = _registry(_Tool("inner", lambda **kw: advise_tools("x") and "inner"),
                            _Tool("x"))
    inner_outcomes = []

    def _outer(**kw):
        inner_outcomes.append(ToolCaller(inner_tools).call("inner", {}))
        advise_tools("y")
        return "outer"

    outcome = ToolCaller(_registry(_Tool("outer", _outer), _Tool("x"), _Tool("y"))).call(
        "outer", {})
    assert inner_outcomes[0].advised_tools == ("x",)
    assert outcome.advised_tools == ("y",)


def test_late_advice_is_ignored():
    """A worker the call left behind (it outlived its call) advises into a closed scope: it is
    told nothing was handed over, and the answer the call returned keeps what it had."""
    release, done = threading.Event(), threading.Event()
    late = []

    def _worker():
        release.wait(5)
        late.append(advise_tools("x"))
        done.set()

    def _starts_worker(**kw):
        advise_tools("y")
        ctx = contextvars.copy_context()
        threading.Thread(target=ctx.run, args=(_worker,), daemon=True).start()
        return "started"

    caller = ToolCaller(_registry(_Tool("starter", _starts_worker), _Tool("x"), _Tool("y")))
    outcome = caller.call("starter", {})
    release.set()
    assert done.wait(5)
    assert outcome.advised_tools == ("y",)
    # The worker ran inside the copied context, so it reached the scope, closed by then.
    assert late == [()]


# ── 4: the unknown-tool correction advises ──────────────────────────────────

def _wm():
    return _Tool("update_working_memory", parameters={
        "type": "object", "properties": {"mark_task_done": {"type": "integer"}}})


@pytest.mark.parametrize("called, registry, advised", [
    ("mark_task_done", lambda: [_wm()], ("update_working_memory",)),
    ("web_serch", lambda: [_Tool("web_search")], ("web_search",)),
    ("zzz_unrelated", lambda: [_Tool("search_tools"), _Tool("web_search")], ("search_tools",)),
])
def test_unknown_tool_advises_its_correction(called, registry, advised):
    from vaf.core.tool_dispatch import unknown_tool_hint
    tools = _registry(*registry())
    outcome = ToolCaller(tools).call(called, {})
    assert outcome.advised_tools == advised, outcome.result
    hint = unknown_tool_hint(called, tools, {})
    assert outcome.result == f"Error: Unknown tool '{called}'. {hint}"


# ── 5-9: the chat lane ───────────────────────────────────────────────────────

def _make_agent(monkeypatch, tmp_path, provider):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    from vaf.core.agent import Agent
    agent = Agent(register_signals=False, run_kind="chat",
                  config_overrides={"provider": provider, f"api_key_{provider}": "sk-test"})
    agent.current_session_id = SESSION
    agent._current_chat_source = "web"
    agent._current_username = "alice"
    agent._current_user_scope_id = None
    agent._current_user_role = "admin"
    monkeypatch.setattr(agent, "_try_workflow", lambda *args, **kw: None)
    agent.tools["advisor"] = _Tool("advisor", lambda **kw: advise_tools("host_bash") and
                                   "Run it with host_bash instead.")
    assert "host_bash" in agent.tools, "the measured shape needs host_bash registered"
    return agent


class _Provider:
    """The router call answers the routed set; the main call (with tools) answers from
    `script`, one entry per round, and records what each request offered. A round that
    calls a tool streams a line of text first, the shape tests/test_front_office_tool_set.py
    uses (measured: a stream of only the tool call was requested a second time)."""

    def __init__(self, agent, *script):
        self.agent, self.script = agent, list(script)
        self.requests = []

    def __call__(self, messages=None, **kw):
        prompt = str((messages or [{}])[0].get("content", ""))
        if prompt.startswith("You are a tool router") or not kw.get("tools"):
            yield "ok"
            return
        self.requests.append({"tools": [t["function"]["name"] for t in kw["tools"]],
                              "tool_choice": kw.get("tool_choice")})
        step = self.script.pop(0) if self.script else "Fertig."
        if isinstance(step, str):
            yield step
            return
        name, args = step
        n = len(self.requests)
        yield "Einen Moment."
        yield json.dumps({"tool_calls": [{"index": 0, "id": f"c{n}", "type": "function",
                                          "function": {"name": name,
                                                       "arguments": json.dumps(args)}}]})
        yield json.dumps({"finish_reason": "tool_calls"})


_ROUTED = ["advisor", "web_search"]


def _turn(agent, monkeypatch, *script, text="Installiere zehn Pakete"):
    monkeypatch.setattr(agent, "_route_tools", lambda route_text: list(_ROUTED))
    provider = _Provider(agent, *script)
    agent.api_backend.chat_completion = provider
    agent.chat_step(user_input=text, raw_user_input=text, stream_callback=lambda t: None)
    return provider


def test_advised_tool_reaches_the_next_request_api(monkeypatch, tmp_path):
    """The plain-API lane (and the local one) send `TOOLS`, which reads `_active_tools`."""
    agent = _make_agent(monkeypatch, tmp_path, "deepseek")
    assert not agent._supports_allowed_tools()
    provider = _turn(agent, monkeypatch, ("advisor", {}), "Fertig.")
    assert len(provider.requests) == 2, provider.requests
    assert "host_bash" not in provider.requests[0]["tools"]
    assert "host_bash" in provider.requests[1]["tools"]


def test_advised_tool_reaches_allowed_tools_choice(monkeypatch, tmp_path):
    """The lane with a resident array and `tool_choice.allowed_tools`: both carry it."""
    agent = _make_agent(monkeypatch, tmp_path, "openai")
    assert agent._supports_allowed_tools()
    provider = _turn(agent, monkeypatch, ("advisor", {}), "Fertig.")
    assert len(provider.requests) == 2, provider.requests
    first, second = provider.requests
    allowed = lambda req: [t for t in (req["tool_choice"] or {}).get(          # noqa: E731
        "allowed_tools", {}).get("tools", [])]
    names = lambda req: {t.get("function", {}).get("name") or t.get("name")  # noqa: E731
                         for t in allowed(req)}
    assert "host_bash" not in names(first)
    assert "host_bash" in second["tools"]
    assert "host_bash" in names(second), second["tool_choice"]


def test_no_history_message_is_added(monkeypatch, tmp_path):
    """Rule 4.1: the advice changes the next request's tool set and nothing in the history.
    Compared with the same turn whose tool returns the same text without advising."""
    def _history(advising, home):
        home.mkdir()
        agent = _make_agent(monkeypatch, home, "deepseek")
        if not advising:
            agent.tools["advisor"] = _Tool("advisor",
                                           lambda **kw: "Run it with host_bash instead.")
        _turn(agent, monkeypatch, ("advisor", {}), "Fertig.")
        # Contents only where no clock is in them (the turn block carries the time).
        return [(m.get("role"), m.get("content") if m.get("role") in ("tool", "assistant")
                 else None) for m in agent.history]

    advised = _history(True, tmp_path / "advised")
    control = _history(False, tmp_path / "control")
    assert advised == control, (advised, control)
    assert [r for r, _ in advised][-2:] == ["tool", "assistant"], advised


def test_all_tools_and_allowed_set_unchanged(monkeypatch, tmp_path):
    agent = _make_agent(monkeypatch, tmp_path, "deepseek")
    agent._active_tools = None
    agent._take_tool_advice("advisor", ("host_bash",))
    assert agent._active_tools is None, "every tool was offered already"
    # A name the model may not see is never pinned, even when a caller advised it.
    agent._active_tools = ["advisor"]
    agent._excluded_tools = {"host_bash"}
    agent._take_tool_advice("advisor", ("host_bash",))
    assert agent._active_tools == ["advisor"]
    agent._excluded_tools = set()
    # Under an allowed set the set is the turn's whole tool set: the funnel refuses the
    # advice for a name outside it, and the offered tools stay the set.
    agent._active_tools = None
    agent.set_allowed_tools({"advisor", "web_search"})
    provider = _turn(agent, monkeypatch, ("advisor", {}), "Fertig.")
    assert len(provider.requests) == 2
    for request in provider.requests:
        assert sorted(request["tools"]) == ["advisor", "web_search"], request["tools"]


def test_the_count_includes_the_advised_schema(monkeypatch, tmp_path):
    for provider_name in ("deepseek", "openai"):
        (tmp_path / provider_name).mkdir()
        agent = _make_agent(monkeypatch, tmp_path / provider_name, provider_name)
        agent._active_tools = list(_ROUTED)
        before = {s["function"]["name"] for s in agent._counted_tool_schemas()}
        assert "host_bash" not in before, provider_name
        agent._take_tool_advice("advisor", ("host_bash",))
        after = {s["function"]["name"] for s in agent._counted_tool_schemas()}
        assert "host_bash" in after, provider_name


class _Sent(BaseException):
    """Ends the turn at the local server request; BaseException so no retry handler eats it."""


def test_advice_survives_the_pressure_subset(monkeypatch, tmp_path):
    """The local server lane, context still over its limit after compression: the set is cut
    to the CORE subset, and the advised tool is put back, because the turn was told to use it."""
    agent = _make_agent(monkeypatch, tmp_path, "openai")
    agent.config["router_max_tools"] = 40
    agent.api_backend = None
    agent.use_server = True
    many = sorted(n for n in agent.tools if n != "host_bash")[:20]
    monkeypatch.setattr(agent, "_route_tools", lambda text: list(many))
    monkeypatch.setattr(agent, "get_token_usage", lambda **kw: (100000, 32768))
    monkeypatch.setattr(agent, "manage_context", lambda *a, **kw: None)
    monkeypatch.setattr(agent, "analyze_intent", lambda *a, **kw: 0.7)
    original = agent._select_turn_tools

    def _route_then_advise(*args, **kw):
        original(*args, **kw)
        agent._take_tool_advice("probe", ("host_bash",))
    monkeypatch.setattr(agent, "_select_turn_tools", _route_then_advise)
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
    assert "host_bash" in sent[0], sent[0]


def test_advice_is_not_carried_into_the_next_turn(monkeypatch, tmp_path):
    """Advised and not used: the next turn routes fresh. (A tool that RAN stays two turns
    through the router's recency, which this does not touch.)"""
    agent = _make_agent(monkeypatch, tmp_path, "deepseek")
    _turn(agent, monkeypatch, ("advisor", {}), "Fertig.")
    assert agent._advised_tools_this_turn == ["host_bash"]
    provider = _turn(agent, monkeypatch, "Fertig.", text="Und jetzt?")
    assert agent._advised_tools_this_turn == []
    assert "host_bash" not in provider.requests[0]["tools"], provider.requests[0]["tools"]


def test_plan_gate_advises_working_memory(monkeypatch, tmp_path):
    """`[PLAN REQUIRED]` names update_working_memory as its one exit; the cap cuts riders
    first, so a narrowed turn may not carry it."""
    agent = _make_agent(monkeypatch, tmp_path, "deepseek")
    agent.tools["writer"] = _Tool("writer", permission_level="write")
    monkeypatch.setattr(agent, "_plan_gate_state", lambda: (True, False))
    agent._active_tools = ["writer"]
    outcome = agent._execute_tool_outcome("writer", {})
    assert outcome.result.startswith("[PLAN REQUIRED]"), outcome.result
    assert outcome.dispatched is False
    assert outcome.advised_tools == ("update_working_memory",)
    assert agent._active_tools == ["writer", "update_working_memory"]


def test_search_does_not_advise_a_refused_tool(monkeypatch, tmp_path):
    """search_tools lists what the model may see, and a regular account without the grant
    still sees host_bash in the catalogue: it is listed, but it does not join the set."""
    agent = _make_agent(monkeypatch, tmp_path, "deepseek")
    agent._current_user_scope_id = REGULAR
    agent._current_user_role = "user"
    agent._active_tools = ["search_tools"]
    with _resolver(None):
        outcome = agent._execute_tool_outcome("search_tools", {"query": "host shell command"})
    assert "host_bash:" in outcome.result, outcome.result
    assert "host_bash" not in outcome.advised_tools
    assert outcome.advised_tools, "the other listed tools are advised"
    assert "host_bash" not in agent._active_tools
    assert set(outcome.advised_tools) <= set(agent._active_tools)


def test_the_prose_hook_is_gone():
    """Names come from the code that lists them, never from parsing a result's text."""
    source = (ROOT / "vaf" / "core" / "agent.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_chat_post_dispatch")
    assert "search_tools" not in ast.unparse(fn)
    import vaf.tools.search_tools as st
    assert not hasattr(st, "extract_discovered_tool_names")


# ── the correction names only what the caller may run; an offer check is quiet ──

@pytest.mark.parametrize("role, scope, expected", [
    ("admin", None, ("host_bash",)),
    ("user", REGULAR, ("search_tools",)),
])
def test_the_correction_names_only_what_the_caller_may_run(role, scope, expected):
    """A near miss of host_bash for an account without the grant: the text must not name a
    tool the account may not run (advice would refuse it, and the next request would not
    carry it), so the correction falls through to search_tools."""
    tools = _registry(_host_bash(), _Tool("search_tools"))
    outcome = ToolCaller(tools, user_scope_id=scope, user_role=role).call("host_bsh", {})
    assert outcome.advised_tools == expected, outcome.result
    if expected == ("host_bash",):
        assert "Did you mean host_bash(" in outcome.result
    else:
        assert "host_bash" not in outcome.result.split("'host_bsh'", 1)[1], outcome.result
        assert "call search_tools(" in outcome.result


def test_an_offer_check_logs_nothing(caplog):
    """POLICY_* lines say a call was blocked or put to a person. Asking what may be OFFERED
    is no call: host_bash advised (a confirmation) and host_bash on a channel (a block) leave
    no line. The control shows the logger is captured: a real call writes one."""
    import logging
    with caplog.at_level(logging.DEBUG, logger="vaf.policy"):
        for kw in ({}, {"source": "telegram", "session_id": "telegram_123456"}):
            advisor = _Tool("advisor", lambda **kw: advise_tools("host_bash") and "OK")
            ToolCaller(_registry(advisor, _host_bash()), **kw).call("advisor", {})
        offered = [r.getMessage() for r in caplog.records if r.name == "vaf.policy"]
        assert [m for m in offered if "host_bash" in m] == [], offered
        ToolCaller(_registry(_host_bash()), interactive=False).call("host_bash", {"command": "true"})
    called = [r.getMessage() for r in caplog.records if r.name == "vaf.policy"]
    assert any(m.startswith("POLICY_CONFIRM tool=host_bash") for m in called), called
