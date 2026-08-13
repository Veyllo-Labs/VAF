# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Which session a dispatch belongs to, and why it must be asked exactly once.

"Is this call coming from a messaging channel" has two independent signals, and either can
be the only one present:

- the chat SOURCE (`_current_chat_source == "telegram"`), set on a live bridge session;
- the SESSION ID prefix (`telegram_...`), which is all a resumed or drained turn carries.

The second signal itself has two sources: `Agent.current_session_id`, written only by
`load_session_context`, and the contextvar behind `subagent_ipc.get_current_session_id()`,
which is what a worker thread carries for the turn it is currently serving. With
`parallel_main_workers > 1` (up to `max_parallel_api_workers`, five on API providers)
several turns share one process, so the contextvar is the per-turn answer while the
attribute is per-object - and an Agent built for an automation never has the attribute at
all.

WHY THIS FILE EXISTS: splitting `execute_tool` into a shared pipeline plus chat hooks left
three separate computations of this flag where there had been one. The policy stage kept
both signals; the two tool-level guards were rewritten to read the attribute alone. For a
drained channel session the two then disagreed, and the disagreement is fail-OPEN:
`vaf/tools/host_bash.py` reads the injected `_is_channel_session` as plain truthiness, so
False means "not a channel" and the non-liftable host-command guard stops guarding.

Nothing caught it. The kwargs baseline pins that the KEY arrives, not its value; the
channel-context test sets `current_session_id` on the fake directly, so the attribute path
was covered and the contextvar path was not. That is the hole this file fills.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from conftest import bind_chat_stages

from vaf.core.agent import Agent
from vaf.tools.base import BaseTool

SCOPE = "deadbeef-0000-0000-0000-000000000000"   # synthetic; never a real scope UUID
CHANNEL_SID = "telegram_9001"                    # synthetic id, not a real session


class _Spy(BaseTool):
    description = "probe"
    permission_level = "read"
    parameters = {"type": "object", "properties": {"with_vaf_tools": {"type": "boolean"}}}

    def __init__(self, name):
        super().__init__()
        self.name = name
        self.seen = None

    def run(self, **kwargs):
        self.seen = dict(kwargs)
        return "ok"


def _dispatch(tool_name, *, attr_sid, contextvar_sid, source="", args=None):
    spy = _Spy(tool_name)
    fake = bind_chat_stages(SimpleNamespace(
        tools={tool_name: spy}, _event_sink=None, _allow_once_tools={tool_name},
        _noninteractive=True, _current_turn_thinking_mode=False,
        _current_chat_source=source, current_session_id=attr_sid,
        _current_user_scope_id=SCOPE, _current_user_role="admin",
        _current_username="tenant", _run_kind="chat", _ww_training=False,
        _active_tools=set(), _turn_ran_progress_tool=False, _session_workspace=None,
        history=[], main_persistence=None, _record_tool_used=lambda n: None,
        _plan_gate_decision=lambda n, t, tool_args=None: None,
        _working_memory_note_gate=lambda tool_args: None,
        _proactive_reply_gate_decision=lambda n, t, a: None,
        _ask_first_gate_decision=lambda n, t: None,
        _room_mode_gate_decision=lambda n, t: None,
        get_live_session_subagents=lambda: [], _extract_subagent_goal=lambda a: "",
        model_display_name="probe",
    ))
    with patch("vaf.core.trust.get_tool_policy", return_value="always"), \
         patch("vaf.core.trust.is_trusted_dir", return_value=True), \
         patch("vaf.core.config.Config.get",
               side_effect=lambda k, d=None: True if k == "channel_tools_unrestricted" else d), \
         patch("vaf.core.subagent_ipc.get_current_session_id", return_value=contextvar_sid):
        result = Agent.execute_tool(fake, tool_name, dict(args or {}))
    assert spy.seen is not None, f"{tool_name} never ran: {str(result)[:120]!r}"
    return spy.seen


# ── the flag host_bash reads ─────────────────────────────────────────────────

def test_a_channel_source_alone_is_enough():
    """The control: with a channel SOURCE the session id is not consulted at all, which is
    why the regression stayed invisible in the obvious test."""
    seen = _dispatch("host_bash", attr_sid=None, contextvar_sid=None, source="telegram")
    assert seen.get("_is_channel_session") is True


def test_the_session_on_the_agent_is_enough():
    seen = _dispatch("host_bash", attr_sid=CHANNEL_SID, contextvar_sid=None)
    assert seen.get("_is_channel_session") is True


def test_the_session_in_the_contextvar_is_enough():
    """THE regression. A drained or resumed channel turn carries its session only in the
    contextvar - an Agent built for an automation has no `current_session_id` at all. Reading
    the attribute alone answers False here, and False disarms host_bash's non-liftable guard."""
    seen = _dispatch("host_bash", attr_sid=None, contextvar_sid=CHANNEL_SID)
    assert seen.get("_is_channel_session") is True, (
        "the channel flag lost the contextvar: a drained channel turn would run host "
        "commands unconfirmed, because host_bash reads this key as plain truthiness"
    )


def test_the_contextvar_wins_over_a_stale_attribute():
    """With several main workers in one process the attribute can belong to a previous
    session; the contextvar is the one that belongs to THIS turn."""
    seen = _dispatch("host_bash", attr_sid="web_local", contextvar_sid=CHANNEL_SID)
    assert seen.get("_is_channel_session") is True


def test_a_plain_web_turn_is_not_a_channel():
    seen = _dispatch("host_bash", attr_sid="web_local", contextvar_sid="web_local",
                     source="web")
    assert seen.get("_is_channel_session") is False


# ── the second consumer, which is a different code path ──────────────────────

def test_python_sandbox_loses_the_tool_bridge_on_a_contextvar_only_channel_turn():
    """`with_vaf_tools` hands the sandbox a bridge back into the tool registry. On a channel
    it is forced off, and that decision reads the same flag from a DIFFERENT method than the
    one host_bash's key is written in - so both need proving separately."""
    seen = _dispatch("python_sandbox", attr_sid=None, contextvar_sid=CHANNEL_SID,
                     args={"with_vaf_tools": True})
    assert seen.get("with_vaf_tools") is False


def test_python_sandbox_keeps_the_bridge_off_a_channel():
    seen = _dispatch("python_sandbox", attr_sid="web_local", contextvar_sid="web_local",
                     source="web", args={"with_vaf_tools": True})
    assert seen.get("with_vaf_tools") is True


# ── the hard block, which reads the same answer one stage earlier ────────────

def _blocked(*, attr_sid, contextvar_sid, source=""):
    """Dispatch a channel-restricted tool with the channel lift OFF, and report the result."""
    tool = _Spy("send_mail")
    tool.channel_restrictions = ("channel", "telegram")
    fake = bind_chat_stages(SimpleNamespace(
        tools={"send_mail": tool}, _event_sink=None, _allow_once_tools={"send_mail"},
        _noninteractive=True, _current_turn_thinking_mode=False,
        _current_chat_source=source, current_session_id=attr_sid,
        _current_user_scope_id=SCOPE, _current_user_role="user",
        _current_username="tenant", _run_kind="chat", _ww_training=False,
        _active_tools=set(), _turn_ran_progress_tool=False, _session_workspace=None,
        history=[], main_persistence=None, _record_tool_used=lambda n: None,
        _plan_gate_decision=lambda n, t, tool_args=None: None,
        _working_memory_note_gate=lambda tool_args: None,
        _proactive_reply_gate_decision=lambda n, t, a: None,
        _ask_first_gate_decision=lambda n, t: None,
        _room_mode_gate_decision=lambda n, t: None,
        get_live_session_subagents=lambda: [], _extract_subagent_goal=lambda a: "",
        model_display_name="probe",
    ))
    with patch("vaf.core.trust.get_tool_policy", return_value="always"), \
         patch("vaf.core.trust.is_trusted_dir", return_value=True), \
         patch("vaf.core.config.Config.get",
               side_effect=lambda k, d=None: False if k == "channel_tools_unrestricted" else d), \
         patch("vaf.core.subagent_ipc.get_current_session_id", return_value=contextvar_sid):
        result = Agent.execute_tool(fake, "send_mail", {})
    return result, tool.seen


def test_the_generic_channel_sentinel_fires_for_a_contextvar_only_session():
    """`channel_restrictions` is the HARD half - it refuses outright rather than injecting a
    flag for the tool to honour, and it reads the session one stage earlier than the guards
    above (from `execute_tool`, not from the hooks), so it needs its own proof.

    The tool here carries the GENERIC `"channel"` sentinel, which is the only entry that can
    fire without a matching source string: `evaluate_tool_policy` intersects the tool's list
    with `{"channel"} | {source}`, so a named restriction like `("telegram",)` deliberately
    does NOT block a source-less drained turn. `host_bash` is the real tool that carries the
    sentinel, and it is exactly the one whose second guard reads truthiness."""
    result, seen = _blocked(attr_sid=None, contextvar_sid=CHANNEL_SID)
    assert result.startswith("Security Error:"), (
        "a channel-restricted tool ran on a drained channel session: the policy stage lost "
        "the contextvar and believed the turn was a web turn"
    )
    assert seen is None


def test_the_sentinel_does_not_fire_off_a_channel():
    """The control - otherwise the assertion above would also pass if everything were blocked."""
    result, seen = _blocked(attr_sid="web_local", contextvar_sid="web_local", source="web")
    assert seen is not None, f"blocked off-channel: {str(result)[:120]!r}"


# ── the resolution itself ────────────────────────────────────────────────────

@pytest.mark.parametrize("attr,ctx,expected", [
    (None, None, None),
    ("web_local", None, "web_local"),
    (None, CHANNEL_SID, CHANNEL_SID),
    ("web_local", CHANNEL_SID, CHANNEL_SID),
])
def test_the_resolution_prefers_the_contextvar(attr, ctx, expected):
    fake = SimpleNamespace(current_session_id=attr)
    with patch("vaf.core.subagent_ipc.get_current_session_id", return_value=ctx):
        assert Agent._dispatch_session_id(fake) == expected


def test_an_agent_without_the_attribute_does_not_crash():
    """Automations construct an Agent that never calls load_session_context, so the
    attribute is absent rather than None."""
    with patch("vaf.core.subagent_ipc.get_current_session_id", return_value=CHANNEL_SID):
        assert Agent._dispatch_session_id(SimpleNamespace()) == CHANNEL_SID


def test_there_is_only_one_resolution():
    """The guard against the split happening again: every consumer of the channel flag in
    the dispatcher must go through the shared helper, not recompute it.

    Scoped to the CHANNEL flag on purpose. The `_session_id` plumbing kwarg in the same
    method reads `current_session_id` directly and always has - it is per-tool plumbing
    rather than a security decision, it is pinned by the kwargs baseline, and widening this
    guard to cover it would be an unmeasured behaviour change smuggled in under a fix."""
    import inspect

    import vaf.core.agent as agent_mod

    for method in (agent_mod.Agent._chat_session_plumbing, agent_mod.Agent._chat_post_dispatch):
        src = inspect.getsource(method)
        assert "self._is_channel_turn()" in src, f"{method.__name__} stopped using the helper"
        assert "_is_channel_session(" not in src, (
            f"{method.__name__} computes the channel flag itself again - a second computation "
            "is how it came to drop the contextvar and re-open the drained-channel hole"
        )
