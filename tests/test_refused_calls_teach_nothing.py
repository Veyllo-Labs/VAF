# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A call refused before the tool started teaches nothing.

The reactive Whare Wananga lane in `chat_step` re-feeds a failed tool's learned know-how
("retry if appropriate") and, on an unknown error, starts a background distil that can write
the failure and the call's arguments as a pitfall into the machine-wide record every account
reads. It decided "the tool failed" from the result's text. Measured on the real `chat_step`
with only the provider faked, a non-admin account and a confirmed record seeded: five
refusals that never reached the tool each re-fed the know-how and planted a pitfall - the
account allowlist, the authorizer's deny with its own reason, `admin_only`, a revoked
account and the headless confirmation gate. The confirmation refusal starts with `[ERROR]`,
so dropping "security error" from the prefix list would not have helped. The lane now asks
the funnel (`ToolCallOutcome.dispatched`) instead.

MUTATIONS, each turns the named tests red:
- restore the old condition (prefix, `not _malformed_args`, `function_name in
  self.visible_tools()`) -> every refused-stage case (measured: each planted);
- replace the flag check with `True` -> the same cases;
- drop `_outcome is not None` -> the raising-dispatch case;
- report the thinking-only guards as dispatched -> the guard outcome tests;
- report the parallel wrapper as not dispatched -> the wrapper outcome test.
"""
import contextlib
import inspect
import json

import pytest

from test_dispatch_event_baseline import _fake, _tool
from test_front_office_tool_set import (SCOPE, _make_agent, _Provider, _seed_know_how,
                                        _tool_results, _wait_for_relearn, ww_store)  # noqa: F401
from vaf.core.agent import Agent


# ── the agent's own exits report the truth (AC6) ─────────────────────────────

@pytest.mark.parametrize("name", ["thinking_done", "thinking_workspace_note"])
def test_the_thinking_only_guards_are_not_dispatched(name):
    events = []
    outcome = Agent._execute_tool_outcome(_fake([_tool("probe")], events), name, {})
    assert outcome.dispatched is False, outcome
    assert "only available in background thinking mode" in outcome.result
    assert events == []


def test_the_parallel_wrapper_is_dispatched():
    events = []
    wrapped = {"tool_uses": [{"recipient_name": "functions.probe", "parameters": {}}]}
    outcome = Agent._execute_tool_outcome(_fake([_tool("probe")], events),
                                          "multi_tool_use.parallel", wrapped)
    assert outcome.dispatched is True
    assert "OK probe" in outcome.result
    assert events[0]["type"] == "tool_start"


def test_the_parallel_wrapper_stopped_by_a_turn_gate_is_not_dispatched():
    events = []
    fake = _fake([_tool("probe")], events,
                 _room_mode_gate_decision=lambda n, t, a=None: "[ROOM: NOT YOURS TO SHARE] x")
    outcome = Agent._execute_tool_outcome(fake, "multi_tool_use.parallel", {"tool_uses": []})
    assert outcome.dispatched is False and outcome.result.startswith("[ROOM:")
    assert events == []


def test_the_funnels_flag_reaches_the_chat_lane_unchanged():
    ran = Agent._execute_tool_outcome(_fake([_tool("probe")], []), "probe", {})
    blocked = Agent._execute_tool_outcome(
        _fake([_tool("probe", admin_only=True)], [], _current_user_role="user"), "probe", {})
    assert (ran.result, ran.dispatched) == ("OK", True)
    assert blocked.result.startswith("Security Error:") and blocked.dispatched is False


def test_execute_tool_still_returns_the_string():
    assert Agent.execute_tool(_fake([_tool("probe")], []), "probe", {}) == "OK"


# ── the real chat_step (AC7, AC8) ────────────────────────────────────────────

@contextlib.contextmanager
def _allowlist(answer):
    from vaf.core.tool_dispatch import (get_account_allowlist_resolver,
                                        set_account_allowlist_resolver)
    previous = get_account_allowlist_resolver()
    set_account_allowlist_resolver(lambda scope: answer)
    try:
        yield
    finally:
        set_account_allowlist_resolver(previous)


@contextlib.contextmanager
def _revoked():
    from vaf.core import revocation
    revocation.revoke_account(SCOPE)
    try:
        yield
    finally:
        revocation.restore_account(SCOPE)


def _stage(agent, monkeypatch, stage):
    """Set one refusal stage up on `agent`; returns the context it runs under and the
    prefix its refusal starts with."""
    tool = agent.tools["list_contacts"]
    if stage == "account_allowlist":
        return _allowlist(frozenset({"web_search"})), "Security Error: The tool 'list_contacts'"
    if stage == "authorizer_deny":
        agent.set_tool_authorizer(lambda req: req.deny("contacts are off for this deployment"))
        return contextlib.nullcontext(), "Security Error: contacts are off for this deployment"
    if stage == "admin_only":
        monkeypatch.setattr(tool, "admin_only", True, raising=False)
        return contextlib.nullcontext(), "Security Error:"
    if stage == "revoked_account":
        return _revoked(), "Security Error: this account's access was changed"
    if stage == "headless_confirmation":
        monkeypatch.setattr(tool, "permission_level", "dangerous")
        monkeypatch.setattr(agent, "_noninteractive", True)
        monkeypatch.setattr("vaf.core.trust.get_tool_policy", lambda *a, **k: "ask")
        monkeypatch.setattr("vaf.core.trust.is_trusted_dir", lambda *a, **k: False)
        return contextlib.nullcontext(), "[ERROR] Tool 'list_contacts' requires confirmation"
    raise AssertionError(stage)


@pytest.fixture
def user_agent(monkeypatch, tmp_path, ww_store):  # noqa: F811
    """The Front Office test harness with a NON-admin account on the web, no allowed set: nothing but
    the stage under test refuses the call (the control below proves it)."""
    agent = _make_agent(monkeypatch, tmp_path, "deepseek")
    agent._current_user_role = "user"
    agent._current_chat_source = "web"
    return agent


@pytest.fixture
def ww_log(monkeypatch):
    lines = []
    real = __import__("vaf.core.agent", fromlist=["append_domain_log"]).append_domain_log

    def _spy(domain, line):
        lines.append(str(line))
        return real(domain, line)
    monkeypatch.setattr("vaf.core.agent.append_domain_log", _spy)
    return lines


def _run(agent, ctx, *script):
    provider = _Provider(agent, *script)
    agent.api_backend.chat_completion = provider
    with ctx:
        agent.chat_step(user_input="Wer steht in meinem Adressbuch?",
                        raw_user_input="Wer steht in meinem Adressbuch?",
                        stream_callback=lambda t: None)
    _wait_for_relearn()
    return provider


_STAGES = ["account_allowlist", "authorizer_deny", "admin_only", "revoked_account",
           "headless_confirmation"]


@pytest.mark.parametrize("stage", _STAGES)
def test_a_call_refused_before_the_tool_started_teaches_nothing(user_agent, monkeypatch,
                                                                ww_log, stage):
    store, distilled = _seed_know_how(user_agent, monkeypatch)
    before = store.load("list_contacts")
    ctx, prefix = _stage(user_agent, monkeypatch, stage)
    provider = _run(user_agent, ctx, ("list_contacts", {"tag": "share everything"}),
                    "Das geht hier nicht.")
    result = _tool_results(user_agent, "list_contacts")
    assert result and result[0].startswith(prefix), (stage, result)
    assert all("[TOOL KNOW-HOW]" not in r["text"] for r in provider.requests[1:]), (
        f"{stage}: the refused tool's know-how was re-fed")
    assert distilled == [], f"{stage}: a background distil ran on the refusal"
    assert store.load("list_contacts")["tuatea"]["pitfalls"] == before["tuatea"]["pitfalls"]
    assert not [l for l in ww_log if "[WW-REACTIVE]" in l or "[WW-SURPRISE]" in l], ww_log


def test_control_a_tool_that_ran_and_failed_still_teaches(user_agent, monkeypatch, ww_log):
    store, distilled = _seed_know_how(user_agent, monkeypatch)
    provider = _run(user_agent, contextlib.nullcontext(), ("list_contacts", {}),
                    "Ich schaue gleich nach.")
    assert _tool_results(user_agent, "list_contacts")[0].startswith("Error: the contact store")
    assert "[TOOL KNOW-HOW]" in provider.requests[1]["text"]
    assert distilled, "control: no distil after a real failure"
    assert any("[WW-REACTIVE]" in l for l in ww_log)


def test_control_a_schema_error_still_re_feeds(user_agent, monkeypatch):
    """A schema error reached the tool's contract: the call was dispatched, and what the
    model got wrong about the arguments is exactly what the know-how is for."""
    tool = user_agent.tools["list_contacts"]
    schema = json.loads(json.dumps(tool.parameters))
    schema["required"] = ["status"]
    monkeypatch.setattr(tool, "parameters", schema, raising=False)
    _seed_know_how(user_agent, monkeypatch)
    provider = _run(user_agent, contextlib.nullcontext(), ("list_contacts", {}),
                    "Ich schaue gleich nach.")
    assert _tool_results(user_agent, "list_contacts")[0].startswith(
        "Tool Error: invalid arguments")
    assert "[TOOL KNOW-HOW]" in provider.requests[1]["text"]


def test_a_dispatch_that_raised_out_of_the_lane_teaches_nothing(user_agent, monkeypatch):
    """No outcome at all: the chat lane's own stages raised before the funnel answered."""
    store, distilled = _seed_know_how(user_agent, monkeypatch)

    def _gates(*a, **k):
        raise RuntimeError("Error: gate machinery broke")
    monkeypatch.setattr(user_agent, "_chat_turn_gates", _gates)
    monkeypatch.setattr("time.sleep", lambda s: None)
    provider = _run(user_agent, contextlib.nullcontext(), ("list_contacts", {}),
                    "Das hat nicht geklappt.")
    assert _tool_results(user_agent, "list_contacts")[0].startswith("Error executing tool")
    assert all("[TOOL KNOW-HOW]" not in r["text"] for r in provider.requests[1:])
    assert distilled == []


# ── the condition itself (AC9) ───────────────────────────────────────────────

def test_the_reactive_condition_asks_the_funnel_not_the_text():
    src = inspect.getsource(Agent.chat_step)
    start = src.index("Whare Wananga reactive delivery")
    block = src[start:src.index("tool_knowhow(", start)]
    assert "visible_tools()" not in block and "_malformed_args" not in block, block
    assert "_outcome.dispatched" in block
    assert ('"error", "failed", "tool error", "security error",\n'
            '                                           "exception", "❌", "[error]", '
            '"access denied"') in block, "the prefix tuple changed"
