# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Whare Wananga probes tools without a human, and that exemption has a hard edge.

`vaf/whare_wananga/runner.py:242-255` drives `Agent.execute_tool` with `_ww_training=True`
so that learning a tool's contract does not stall on a confirmation dialog nobody is there
to answer. Without it a write tool answers `[CANCELLED]`, and that string gets recorded as
what the tool does - the learned contract is then confidently wrong.

The exemption is exactly two stages wide: the four chat turn gates and the confirmation
gate. It is NOT a policy bypass. An `admin_only` tool must still be refused while training,
because a training run is not an authorisation.

This file exists because a mutation test found the property unguarded: deleting
`gate_enabled=not _ww` from the dispatcher left the whole suite green. It was never covered,
including before the dispatcher was split - `_ww_training` appeared in tests only as a fake
attribute that was always False.
"""
from types import SimpleNamespace
from unittest.mock import patch

from conftest import bind_chat_stages

from vaf.core.agent import Agent
from vaf.tools.base import BaseTool

SCOPE = "deadbeef-0000-0000-0000-000000000000"   # synthetic; never a real scope UUID


class _Probe(BaseTool):
    name = "ww_probe"
    description = "probe"
    permission_level = "dangerous"      # would normally be gated
    parameters = {"type": "object", "properties": {}}

    def __init__(self, **attrs):
        super().__init__()
        self.ran = False
        for k, v in attrs.items():
            setattr(self, k, v)

    def run(self, **kwargs):
        self.ran = True
        return "probe result"


def _agent(tool, *, training, gates_would_block=False):
    def _gate(*a, **kw):
        return "[PLAN REQUIRED] make a plan first" if gates_would_block else None

    return bind_chat_stages(SimpleNamespace(
        tools={tool.name: tool}, _event_sink=None, _allow_once_tools=set(),
        _noninteractive=True, _current_turn_thinking_mode=False,
        _current_chat_source="web", current_session_id=None,
        _current_user_scope_id=SCOPE, _current_user_role="user",
        _current_username="tenant", _run_kind="chat", _ww_training=training,
        _active_tools=set(), _turn_ran_progress_tool=False, _session_workspace=None,
        history=[], main_persistence=None, _record_tool_used=lambda n: None,
        _plan_gate_decision=_gate,
        _working_memory_note_gate=lambda tool_args: None,
        _proactive_reply_gate_decision=lambda n, t, a: None,
        _ask_first_gate_decision=lambda n, t: None,
        get_live_session_subagents=lambda: [], _extract_subagent_goal=lambda a: "",
        model_display_name="probe",
    ))


def _run(agent, tool_name="ww_probe"):
    """Nobody can answer, and the directory is not trusted: the gate would refuse."""
    with patch("vaf.core.trust.get_tool_policy", return_value="ask"), \
         patch("vaf.core.trust.is_trusted_dir", return_value=False):
        return Agent.execute_tool(agent, tool_name, {})


def test_a_gated_tool_is_refused_when_training_is_off():
    """The control. Without this the next assertion would prove nothing - a tool that was
    never gated in the first place also "runs while training"."""
    tool = _Probe()
    result = _run(_agent(tool, training=False))
    assert tool.ran is False
    assert "confirmation" in result.lower() or result.startswith("[")


def test_the_same_tool_runs_while_training():
    tool = _Probe()
    assert _run(_agent(tool, training=True)) == "probe result"
    assert tool.ran is True


def test_the_turn_gates_are_skipped_while_training():
    """A plan gate firing mid-probe would record its own message as the tool's answer."""
    tool = _Probe()
    assert _run(_agent(tool, training=False, gates_would_block=True)).startswith("[PLAN REQUIRED]")
    tool2 = _Probe()
    assert _run(_agent(tool2, training=True, gates_would_block=True)) == "probe result"


def test_training_is_not_a_policy_bypass():
    """The edge. Skipping a question a human cannot answer is not the same as skipping a
    rule about who this caller is - a training run must not become a privilege escalation."""
    tool = _Probe(admin_only=True)
    result = _run(_agent(tool, training=True))
    assert result.startswith("Security Error:")
    assert tool.ran is False


def test_training_still_blocks_a_tool_restricted_to_other_channels():
    tool = _Probe(channel_restrictions=("telegram",))
    with patch("vaf.core.config.Config.get",
               side_effect=lambda k, d=None: False if k == "channel_tools_unrestricted" else d):
        agent = _agent(tool, training=True)
        agent._current_chat_source = "telegram"
        result = _run(agent)
    assert result.startswith("Security Error:")
    assert tool.ran is False
