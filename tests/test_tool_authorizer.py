# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The application's own say over a tool call.

VAF decides three things about a tool before it runs: is this caller allowed the tool at all
(policy), does a human have to confirm it (the gate), and who is calling (identity). An
application embedding VAF had no way into any of that. It could refuse a call only by not
registering the tool, which is a decision taken once at startup rather than per call, per
user, per argument.

`set_tool_authorizer` is that way in. The design is deliberately narrow, and the narrowness
is what the tests below protect:

- THREE METHODS, NOT A RETURN VALUE. A callback returning None would have to mean something,
  and the tempting meaning ("no objection") turns every forgotten `return` into an approval.
  Saying nothing here means having no opinion, which is the status quo.
- FAIL-CLOSED. An exception inside the callback is a refusal. This is the opposite polarity
  from the event sink, which swallows failures on purpose: a broken observer must not fail a
  run it only watches, while a broken guard must not quietly become no guard. A crash and a
  guard that never ran look identical from outside, and one of those is what an attacker
  wants.
- IT CANNOT ESCALATE. `allow()` skips the confirmation question, never a policy block, and it
  writes nothing durable. An authorizer is a second lock, not a master key.
- IT NEVER READS IDENTITY FROM THE ARGUMENTS. The identity on the request comes from the
  caller's context; `args` is the model's own output and is a snapshot, so an authorizer can
  neither be fooled by it nor use it to rewrite the call.
"""
from pathlib import Path
from unittest.mock import patch

import pytest

from vaf.core.tool_dispatch import ToolCaller, ToolCallHooks, ToolRequest, consult_authorizer
from vaf.tools.base import BaseTool

SCOPE = "deadbeef-0000-0000-0000-000000000000"   # synthetic; never a real scope UUID


class _Probe(BaseTool):
    name = "probe"
    description = "probe"
    permission_level = "read"
    parameters = {"type": "object", "properties": {"x": {"type": "string"}}}

    def __init__(self, **attrs):
        super().__init__()
        self.seen = None
        for k, v in attrs.items():
            setattr(self, k, v)

    def run(self, **kwargs):
        self.seen = dict(kwargs)
        return "OK"


def _caller(tool, authorize=None, events=None, **kw):
    kw.setdefault("user_scope_id", SCOPE)
    kw.setdefault("user_role", "user")
    kw.setdefault("username", "tenant")
    return ToolCaller({tool.name: tool}, authorize=authorize,
                      on_event=(events.append if events is not None else None), **kw)


# ── no authorizer, and an authorizer with no opinion, are the same thing ─────

def test_without_an_authorizer_nothing_changes():
    assert _caller(_Probe()).execute("probe", {}) == "OK"


def test_an_authorizer_that_says_nothing_changes_nothing():
    """The forgotten-return case. It must be indistinguishable from having no authorizer."""
    seen = []
    tool = _Probe()
    assert _caller(tool, authorize=seen.append).execute("probe", {}) == "OK"
    assert len(seen) == 1, "the authorizer was not consulted at all"


def test_an_authorizer_that_returns_a_value_is_not_mistaken_for_a_decision():
    """A callback may return anything; only the three methods decide. Returning False must
    not be read as a refusal, and returning True must not be read as an approval."""
    assert _caller(_Probe(), authorize=lambda req: False).execute("probe", {}) == "OK"
    assert _caller(_Probe(), authorize=lambda req: True).execute("probe", {}) == "OK"


# ── deny ─────────────────────────────────────────────────────────────────────

def test_deny_refuses_the_call_and_the_tool_never_runs():
    tool = _Probe()
    result = _caller(tool, authorize=lambda req: req.deny("not for this tenant")).execute("probe", {})
    assert result == "Security Error: not for this tenant"
    assert tool.seen is None


def test_a_denied_call_emits_nothing():
    """Same silence as a policy block: an observer must never see a refused call reported as
    one that ran."""
    events = []
    _caller(_Probe(), authorize=lambda req: req.deny("no"), events=events).execute("probe", {})
    assert events == []


def test_deny_wins_over_allow_whichever_order_they_are_called_in():
    for body in (lambda req: (req.deny("no"), req.allow()),
                 lambda req: (req.allow(), req.deny("no"))):
        assert _caller(_Probe(), authorize=body).execute("probe", {}).startswith("Security Error:")


def test_deny_wins_over_ask():
    result = _caller(_Probe(permission_level="dangerous"),
                     authorize=lambda req: (req.ask("hm"), req.deny("no"))).execute("probe", {})
    assert result.startswith("Security Error:")


# ── fail-closed ──────────────────────────────────────────────────────────────

def test_a_raising_authorizer_is_a_refusal():
    """THE property. The event sink swallows failures because a broken observer must not fail
    a run; a broken guard must not become no guard."""
    def _boom(req):
        raise RuntimeError("kaputt")

    tool = _Probe()
    result = _caller(tool, authorize=_boom).execute("probe", {})
    assert result.startswith("Security Error:")
    assert "kaputt" in result
    assert tool.seen is None


def test_a_raising_authorizer_cannot_undo_an_earlier_allow():
    """A callback that allows and then crashes has not finished deciding, so its half-made
    decision must not stand."""
    def _half(req):
        req.allow()
        raise RuntimeError("kaputt")

    assert _caller(_Probe(), authorize=_half).execute("probe", {}).startswith("Security Error:")


def test_a_non_callable_authorizer_is_ignored_rather_than_fatal():
    assert _caller(_Probe(), authorize="not a function").execute("probe", {}) == "OK"


# ── allow: a second lock, never a master key ─────────────────────────────────

def _gated(tool, authorize=None, policy="ask", trusted=False, **kw):
    with patch("vaf.core.trust.get_tool_policy", return_value=policy), \
         patch("vaf.core.trust.is_trusted_dir", return_value=trusted):
        return _caller(tool, authorize=authorize, trust_dir=Path("/tmp/p"), **kw).execute("probe", {})


def test_allow_skips_the_confirmation_question():
    from vaf import markers

    tool = _Probe(permission_level="dangerous")
    assert _gated(tool, authorize=lambda req: req.allow()) == "OK"
    assert tool.seen is not None
    # Control: the same call without the authorizer is refused, so the assertion above is
    # about allow() and not about a tool that was never gated.
    assert markers.TOOL_CONFIRMATION_REQUIRED in _gated(_Probe(permission_level="dangerous"))


def test_allow_writes_nothing_durable():
    """Skipping one question must not widen into a standing grant - that is the difference
    between allow() and the interactive "always" answer."""
    writes = []
    tool = _Probe(permission_level="dangerous")
    with patch("vaf.core.trust.set_tool_policy", lambda *a: writes.append("policy")), \
         patch("vaf.core.trust.mark_trusted_dir", lambda *a: writes.append("dir")):
        _gated(tool, authorize=lambda req: req.allow())
    assert writes == []


def test_allow_is_only_for_this_call():
    caller_allow_once = set()
    tool = _Probe(permission_level="dangerous")
    with patch("vaf.core.trust.get_tool_policy", return_value="ask"), \
         patch("vaf.core.trust.is_trusted_dir", return_value=False):
        c = ToolCaller({"probe": tool}, authorize=lambda req: req.allow(),
                       allow_once=caller_allow_once, trust_dir=Path("/tmp/p"))
        c.execute("probe", {})
    assert caller_allow_once == set(), "allow() leaked into the per-turn grant set"


def test_allow_cannot_reach_an_admin_only_tool():
    """The escalation this must not permit. Hard policy is decided before an authorizer is
    consulted, so allow() has nothing to override."""
    tool = _Probe(admin_only=True)
    result = _caller(tool, authorize=lambda req: req.allow()).execute("probe", {})
    assert result.startswith("Security Error:")
    assert "admin" in result.lower()
    assert tool.seen is None


def test_allow_cannot_lift_a_channel_restriction():
    tool = _Probe(channel_restrictions=("telegram",))
    with patch("vaf.core.config.Config.get",
               side_effect=lambda k, d=None: False if k == "channel_tools_unrestricted" else d):
        result = _caller(tool, authorize=lambda req: req.allow(),
                         source="telegram").execute("probe", {})
    assert result.startswith("Security Error:")
    assert tool.seen is None


def test_allow_does_not_skip_schema_validation():
    """It is an answer about permission, not about correctness."""
    tool = _Probe(parameters={"type": "object", "properties": {"x": {"type": "string"}},
                              "required": ["x"]})
    result = _caller(tool, authorize=lambda req: req.allow()).execute("probe", {})
    assert result.startswith("Tool Error: invalid arguments")


# ── ask: a decision, not a suggestion ────────────────────────────────────────

def test_ask_forces_the_gate_on_a_tool_that_would_not_be_gated():
    from vaf import markers

    tool = _Probe(permission_level="read")
    result = _gated(tool, authorize=lambda req: req.ask("this one needs a human"))
    assert markers.TOOL_CONFIRMATION_REQUIRED in result
    assert tool.seen is None


def test_ask_survives_a_standing_grant():
    """The half that makes ask() a decision rather than a wish: one previous "always" would
    otherwise silence it forever, and silencing it is exactly why an application overrode the
    default."""
    from vaf import markers

    tool = _Probe(permission_level="dangerous")
    result = _gated(tool, authorize=lambda req: req.ask("ask anyway"),
                    policy="allow", trusted=True)
    assert markers.TOOL_CONFIRMATION_REQUIRED in result
    assert tool.seen is None
    # Control: without the authorizer the same standing grant runs it silently.
    assert _gated(_Probe(permission_level="dangerous"), policy="allow", trusted=True) == "OK"


def test_ask_reaches_a_human_who_can_answer():
    asked = []
    tool = _Probe(permission_level="read")
    with patch("vaf.core.trust.get_tool_policy", return_value="ask"), \
         patch("vaf.core.trust.is_trusted_dir", return_value=False):
        result = _caller(tool, authorize=lambda req: req.ask("please confirm"),
                         interactive=True, trust_dir=Path("/tmp/p"),
                         decide=lambda n, r: asked.append((n, r)) or "allow_once").execute("probe", {})
    assert result == "OK"
    assert asked == [("probe", "please confirm")], "the authorizer's reason never reached the person"


# ── what the request shows, and what it does not ─────────────────────────────

def test_the_request_carries_the_callers_identity_not_the_models():
    seen = {}

    def _inspect(req):
        seen.update(scope=req.user_scope_id, role=req.user_role, user=req.username)

    _caller(_Probe(), authorize=_inspect).execute(
        "probe", {"user_role": "admin", "user_scope_id": "ffffffff-0000"})
    assert seen == {"scope": SCOPE, "role": "user", "user": "tenant"}


def test_the_request_carries_the_tools_declared_contract():
    seen = {}
    tool = _Probe(permission_level="dangerous", side_effect_class="irreversible",
                  admin_only=False, channel_restrictions=("telegram",))

    def _inspect(req):
        seen.update(level=req.permission_level, effect=req.side_effect_class,
                    admin=req.admin_only, channels=req.channel_restrictions)
        req.allow()

    _gated(tool, authorize=_inspect)
    assert seen == {"level": "dangerous", "effect": "irreversible", "admin": False,
                    "channels": ("telegram",)}


def test_the_arguments_are_a_snapshot_the_authorizer_cannot_rewrite():
    """Deciding is not editing. A hook that could quietly change a call would be a far larger
    surface than one that answers yes or no."""
    tool = _Probe()

    def _tamper(req):
        req.args["x"] = "rewritten"
        req.args["user_role"] = "admin"

    _caller(tool, authorize=_tamper).execute("probe", {"x": "original"})
    assert tool.seen["x"] == "original"
    assert "user_role" not in tool.seen


def test_an_unknown_tool_still_reaches_the_authorizer():
    """A call for a tool that does not exist is still a call the application may want to see
    (a probing model looks exactly like this), and its contract fields are simply empty."""
    seen = []
    result = _caller(_Probe(), authorize=lambda req: seen.append(
        (req.tool_name, req.permission_level, req.admin_only))).execute("nope", {})
    assert seen == [("nope", None, False)]
    assert result.startswith("Error: Unknown tool")


def test_the_authorizer_can_refuse_a_tool_by_its_arguments():
    """The case that motivates per-call authorization at all: the tool is fine, this call
    is not."""
    def _no_writes_to_etc(req):
        if str(req.args.get("x", "")).startswith("/etc"):
            req.deny("not outside the project")

    tool = _Probe()
    assert _caller(tool, authorize=_no_writes_to_etc).execute("probe", {"x": "notes.md"}) == "OK"
    assert _caller(tool, authorize=_no_writes_to_etc).execute(
        "probe", {"x": "/etc/passwd"}).startswith("Security Error:")


# ── position in the pipeline ─────────────────────────────────────────────────

def test_the_authorizer_runs_before_the_chat_gates():
    """Whether an application's guard is consulted must not depend on turn bookkeeping it
    cannot see. A chat gate answering first would make the authorizer's coverage a function
    of VAF's internal state."""
    order = []
    hooks = ToolCallHooks(after_policy=lambda n, t, a: order.append("chat-gate") or "[PLAN REQUIRED] x")
    result = _caller(_Probe(), authorize=lambda req: order.append("authorizer"),
                     hooks=hooks).execute("probe", {})
    assert order == ["authorizer", "chat-gate"]
    assert result.startswith("[PLAN REQUIRED]")


def test_a_denial_answers_instead_of_reaching_the_gate():
    """A refused call must not be parked on a confirmation dialog nobody intends to answer."""
    asked = []
    tool = _Probe(permission_level="dangerous")
    result = _gated(tool, authorize=lambda req: req.deny("no"), interactive=True,
                    decide=lambda n, r: asked.append(n) or "allow_once")
    assert result.startswith("Security Error:")
    assert asked == []


def test_the_authorizer_is_not_consulted_for_a_hard_blocked_tool():
    """Nothing to decide: the answer is already no, and consulting would invite an
    application to believe it could say otherwise."""
    consulted = []
    _caller(_Probe(admin_only=True), authorize=consulted.append).execute("probe", {})
    assert consulted == []


# ── the request object on its own ────────────────────────────────────────────

def _req(**kw):
    base = dict(tool_name="probe", tool=_Probe(), args={}, user_scope_id=SCOPE,
                username="tenant", user_role="user", source="", session_id=None)
    base.update(kw)
    return ToolRequest(**base)


def test_a_fresh_request_has_no_opinion():
    assert _req().decision is None


@pytest.mark.parametrize("calls,expected", [
    (("allow",), "allow"),
    (("ask",), "ask"),
    (("deny",), "deny"),
    (("allow", "ask"), "ask"),
    (("ask", "allow"), "ask"),
    (("ask", "deny"), "deny"),
    (("deny", "ask"), "deny"),
    (("allow", "deny", "ask"), "deny"),
])
def test_the_most_restrictive_answer_wins_regardless_of_order(calls, expected):
    req = _req()
    for c in calls:
        getattr(req, c)("because") if c != "allow" else req.allow()
    assert req.decision == expected


def test_consult_returns_the_request_untouched_when_there_is_no_authorizer():
    req = _req()
    assert consult_authorizer(None, req) is req
    assert req.decision is None


def test_the_reason_survives_to_the_caller():
    req = _req()
    req.deny("because the tenant is over quota")
    assert req.reason == "because the tenant is over quota"


# ── the wiring, which is a separate thing from the pipeline ──────────────────

def _agent_with(authorize, tool):
    from types import SimpleNamespace

    from conftest import bind_chat_stages

    agent = bind_chat_stages(SimpleNamespace(
        tools={tool.name: tool}, _event_sink=None, _allow_once_tools=set(),
        _noninteractive=True, _current_turn_thinking_mode=False,
        _current_chat_source="web", current_session_id=None,
        _current_user_scope_id=SCOPE, _current_user_role="user",
        _current_username="tenant", _run_kind="chat", _ww_training=False,
        _active_tools=set(), _turn_ran_progress_tool=False, _session_workspace=None,
        history=[], main_persistence=None, _record_tool_used=lambda n: None,
        _plan_gate_decision=lambda n, t, tool_args=None: None,
        _working_memory_note_gate=lambda tool_args: None,
        _proactive_reply_gate_decision=lambda n, t, a: None,
        _ask_first_gate_decision=lambda n, t: None,
        get_live_session_subagents=lambda: [], _extract_subagent_goal=lambda a: "",
        model_display_name="probe", _tool_authorizer=None,
    ))
    from vaf.core.agent import Agent
    Agent.set_tool_authorizer(agent, authorize)
    return agent


def test_the_agent_actually_hands_its_authorizer_to_the_pipeline():
    """A correct pipeline reached through a dispatcher that never passes the callback is a
    guard that does not exist. Twice in this round a stage was right and its wiring was not,
    and neither time did a unit test notice - so the wiring gets its own assertion."""
    from vaf.core.agent import Agent

    tool = _Probe()
    agent = _agent_with(lambda req: req.deny("wired"), tool)
    result = Agent.execute_tool(agent, "probe", {})
    assert result == "Security Error: wired", (
        "the agent dispatched without consulting its authorizer - set_tool_authorizer is "
        "installed but never reaches the shared pipeline"
    )
    assert tool.seen is None


def test_an_agent_without_an_authorizer_behaves_as_before():
    from vaf.core.agent import Agent

    tool = _Probe()
    assert Agent.execute_tool(_agent_with(None, tool), "probe", {}) == "OK"


def test_detaching_the_authorizer_on_the_agent_takes_effect():
    from vaf.core.agent import Agent

    tool = _Probe()
    agent = _agent_with(lambda req: req.deny("no"), tool)
    assert Agent.execute_tool(agent, "probe", {}).startswith("Security Error:")
    Agent.set_tool_authorizer(agent, None)
    assert Agent.execute_tool(agent, "probe", {}) == "OK"
