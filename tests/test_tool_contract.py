# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.modules.setdefault("llama_cpp", MagicMock())

from conftest import bind_chat_stages
from vaf.core.agent import Agent
from vaf.core.config import Config
from vaf.core.tool_contract import evaluate_tool_policy, resolve_tool_contract
from vaf.tools.base import BaseTool
from vaf.tools.delete_contact import DeleteContactTool
from vaf.tools.list_contacts import ListContactsTool
from vaf.tools.mcp_client import MCPClientTool
from vaf.tools.thinking_workspace_write import ThinkingWorkspaceWriteTool
from vaf.tools.workflow_executor import ExecuteWorkflowTool


class DummyTool(BaseTool):
    name = "dummy_tool"
    description = "Dummy tool"

    def run(self, **kwargs) -> str:
        return "ok"


class DangerousDummyTool(BaseTool):
    name = "dangerous_dummy"
    description = "Dangerous dummy tool"
    permission_level = "dangerous"
    side_effect_class = "irreversible"

    def run(self, **kwargs) -> str:
        return "dangerous ok"


class ChannelBlockedDummyTool(BaseTool):
    name = "channel_blocked_dummy"
    description = "Channel blocked dummy tool"
    channel_restrictions = ["channel", "telegram"]

    def run(self, **kwargs) -> str:
        return "blocked"


def test_resolve_tool_contract_uses_defaults():
    contract = resolve_tool_contract("dummy_tool", DummyTool())

    assert contract.name == "dummy_tool"
    assert contract.permission_level == "read"
    assert contract.channel_restrictions == ()
    assert contract.side_effect_class == "none"


def test_resolve_tool_contract_reads_metadata_from_real_tools():
    read_contract = resolve_tool_contract("list_contacts", ListContactsTool())
    delete_contract = resolve_tool_contract("delete_contact", DeleteContactTool())

    assert read_contract.permission_level == "read"
    assert read_contract.side_effect_class == "none"
    assert delete_contract.permission_level == "write"
    assert delete_contract.side_effect_class == "irreversible"


def test_resolve_tool_contract_reads_metadata_from_workflow_and_mcp_tools():
    workflow_contract = resolve_tool_contract("execute_workflow", ExecuteWorkflowTool())
    mcp_contract = resolve_tool_contract("mcp_call", MCPClientTool())
    thinking_contract = resolve_tool_contract("thinking_workspace_write", ThinkingWorkspaceWriteTool())

    assert workflow_contract.permission_level == "write"
    assert workflow_contract.side_effect_class == "reversible"
    assert mcp_contract.permission_level == "write"
    assert mcp_contract.side_effect_class == "irreversible"
    assert thinking_contract.permission_level == "system"
    assert thinking_contract.side_effect_class == "reversible"


def _restrict_channels():
    """Force `channel_tools_unrestricted` to False so the channel-restriction block
    path is exercised. Its default is True (channel sessions get full tool access),
    and CI runs with no config.json, so without this the block never triggers."""
    real_get = Config.get

    def fake_get(key, default=None):
        if key == "channel_tools_unrestricted":
            return False
        return real_get(key, default)

    return patch.object(Config, "get", side_effect=fake_get)


def test_evaluate_tool_policy_blocks_channel_restricted_tools():
    with _restrict_channels():
        decision = evaluate_tool_policy(
            tool_name="channel_blocked_dummy",
            tool=ChannelBlockedDummyTool(),
            current_source="telegram",
            is_channel_session=True,
        )

    assert decision.blocked is True
    assert "blocked for telegram sessions" in decision.reason


def test_evaluate_tool_policy_logs_divergence_when_contract_changes_decision(caplog):
    with caplog.at_level("INFO", logger="vaf.policy"), _restrict_channels():
        decision = evaluate_tool_policy(
            tool_name="channel_blocked_dummy",
            tool=ChannelBlockedDummyTool(),
            current_source="telegram",
            is_channel_session=True,
        )

    assert decision.blocked is True
    assert "POLICY_DIVERGENCE tool=channel_blocked_dummy old=allow new=block" in caplog.text


def test_evaluate_tool_policy_requires_confirmation_for_dangerous_tools():
    decision = evaluate_tool_policy(
        tool_name="dangerous_dummy",
        tool=DangerousDummyTool(),
        current_source="web",
        is_channel_session=False,
    )

    assert decision.blocked is False
    assert decision.requires_confirmation is True
    assert "irreversible" in decision.reason.lower()


def test_execute_tool_uses_contract_for_noninteractive_gating():
    fake_agent = bind_chat_stages(SimpleNamespace(
        tools={"dangerous_dummy": DangerousDummyTool()},
        _event_sink=None,
        _noninteractive=True,
        _current_turn_thinking_mode=False,
        _current_chat_source="web",
        current_session_id=None,
        _record_tool_used=lambda name: None,
        _plan_gate_decision=lambda name, tool, tool_args=None: None,  # plan gate is a no-op here (noninteractive)
        _proactive_reply_gate_decision=lambda name, tool, args: None,  # incident gates: no-op
        _ask_first_gate_decision=lambda name, tool: None,
        _room_mode_gate_decision=lambda name, tool, args=None: None,
    ))

    with patch("vaf.core.trust.get_tool_policy", return_value="ask"), patch(
        "vaf.core.trust.is_trusted_dir", return_value=False
    ):
        result = Agent.execute_tool(fake_agent, "dangerous_dummy", {})

    assert result.startswith("[ERROR] Tool 'dangerous_dummy' requires confirmation")


# ── Channel full-access opt-in (channel_tools_unrestricted) ────────────────────

class ChannelRestrictedDangerousDummyTool(BaseTool):
    name = "channel_restricted_dangerous_dummy"
    description = "Channel-restricted + dangerous dummy tool"
    permission_level = "dangerous"
    side_effect_class = "irreversible"
    channel_restrictions = ["channel", "telegram"]

    def run(self, **kwargs) -> str:
        return "ok"


class AdminOnlyChannelDummyTool(BaseTool):
    name = "admin_only_channel_dummy"
    description = "Admin-only + channel-restricted dummy tool"
    admin_only = True
    channel_restrictions = ["channel", "telegram"]

    def run(self, **kwargs) -> str:
        return "ok"


def _patch_channel_flag(value):
    """Patch Config.get so channel_tools_unrestricted reads `value`; other keys pass through."""
    orig = Config.get

    def fake_get(key, default=None):
        if key == "channel_tools_unrestricted":
            return value
        return orig(key, default)

    return patch.object(Config, "get", new=fake_get)


class ChannelRestrictedWriteDummyTool(ChannelRestrictedDangerousDummyTool):
    name = "channel_restricted_write_dummy"
    permission_level = "write"


def test_channel_full_access_allows_restricted_tool_when_enabled():
    # Flag ON: a channel-restricted convenience tool runs on Telegram with no confirmation.
    with _patch_channel_flag(True):
        decision = evaluate_tool_policy(
            tool_name="channel_restricted_write_dummy",
            tool=ChannelRestrictedWriteDummyTool(),
            current_source="telegram",
            is_channel_session=True,
            is_admin=True,
        )
    assert decision.blocked is False
    assert decision.requires_confirmation is False


def test_channel_full_access_does_not_reach_a_tool_that_needs_a_person():
    """Dangerous AND restricted on channels: the lift would also lift its confirmation, which
    no channel can show. Deliberately changed: this test used to pin that such a tool RAN on
    Telegram unconfirmed, and python_exec did exactly that (measured). The unattended lanes
    (gated_lane=False: the coder, workflow steps) keep the lift."""
    def decide(gated_lane):
        with _patch_channel_flag(True):
            return evaluate_tool_policy(
                tool_name="channel_restricted_dangerous_dummy",
                tool=ChannelRestrictedDangerousDummyTool(),
                current_source="telegram",
                is_channel_session=True,
                is_admin=True,
                gated_lane=gated_lane,
            )
    assert decide(True).blocked is True
    assert decide(False).blocked is False


def test_channel_full_access_off_still_blocks():
    # Flag OFF (default): channel restriction is still enforced.
    with _patch_channel_flag(False):
        decision = evaluate_tool_policy(
            tool_name="channel_blocked_dummy",
            tool=ChannelBlockedDummyTool(),
            current_source="telegram",
            is_channel_session=True,
            is_admin=True,
        )
    assert decision.blocked is True


def test_channel_full_access_still_enforces_admin_only():
    # Even with full channel access, a NON-admin cannot reach an admin-only tool.
    with _patch_channel_flag(True):
        decision = evaluate_tool_policy(
            tool_name="admin_only_channel_dummy",
            tool=AdminOnlyChannelDummyTool(),
            current_source="telegram",
            is_channel_session=True,
            is_admin=False,
        )
    assert decision.blocked is True
    assert "admin session" in decision.reason


def test_channel_full_access_does_not_affect_web_sessions():
    # The opt-in only touches channel sessions; web still gates dangerous tools.
    with _patch_channel_flag(True):
        decision = evaluate_tool_policy(
            tool_name="dangerous_dummy",
            tool=DangerousDummyTool(),
            current_source="web",
            is_channel_session=False,
            is_admin=True,
        )
    assert decision.blocked is False
    assert decision.requires_confirmation is True


# ── section 1h: a tool kept off chats, on the account holder's own device ───────────────

from app_link_env import channel_tool_classes  # noqa: E402

_CHANNEL_TOOLS = channel_tool_classes()


def _lift(monkeypatch, on):
    real = Config.get
    monkeypatch.setattr(Config, "get", staticmethod(lambda key, default=None: (
        on if key == "channel_tools_unrestricted" else real(key, default))))


def _policy(name, tool, *, admin=True, gated=True, channel_lift=True, **holder):
    return evaluate_tool_policy(name, tool, "veyllo_app", is_channel_session=True,
                                is_admin=admin, gated_lane=gated, channel_lift=channel_lift,
                                **holder)


def test_the_registry_holds_the_reviewed_channel_tools():
    assert len(_CHANNEL_TOOLS) == 15 and {"host_bash", "ssh", "ftp", "set_timer"} <= set(
        _CHANNEL_TOOLS)


@pytest.mark.parametrize("name", sorted(_CHANNEL_TOOLS))
@pytest.mark.parametrize("lift", [True, False])
@pytest.mark.parametrize("admin", [True, False])
def test_holder_present_confirms_every_channel_tool(monkeypatch, name, lift, admin):
    """MUTATION: move 1h after 1b - red (lift on); drop `holder_present` from the condition
    - red in test_without_holder_nothing_changes."""
    _lift(monkeypatch, lift)
    tool = _CHANNEL_TOOLS[name]
    decision = _policy(name, tool, admin=admin, holder_present=True, holder_device=True)
    if getattr(tool, "admin_only", False) and not admin:
        assert decision.blocked and "admin session" in decision.reason
        assert not decision.ask_each_call
    else:
        assert (decision.blocked, decision.requires_confirmation, decision.ask_each_call) \
            == (False, True, True), (name, decision)
        assert "Veyllo App" in decision.reason


@pytest.mark.parametrize("name", sorted(_CHANNEL_TOOLS))
def test_without_holder_nothing_changes(monkeypatch, name):
    tool = _CHANNEL_TOOLS[name]
    for lift in (True, False):
        _lift(monkeypatch, lift)
        for admin in (True, False):
            for channel_lift in (True, False):
                today = _policy(name, tool, admin=admin, channel_lift=channel_lift)
                for holder in ({"holder_present": False, "holder_device": True},
                               {"holder_present": True, "holder_device": False},
                               {"holder_present": False, "holder_device": False}):
                    assert _policy(name, tool, admin=admin, channel_lift=channel_lift,
                                   **holder) == today, (name, lift, admin, holder)
                assert not today.ask_each_call


@pytest.mark.parametrize("restrictions", [("veyllo_app",), ("channel", "veyllo_app")])
@pytest.mark.parametrize("level", ["write", "dangerous"])
def test_a_tool_naming_the_app_stays_blocked(monkeypatch, restrictions, level):
    """Its author excluded the app on purpose: in the lane that asks a person it is refused
    there whatever the admin's lift, the caller's own lift and whoever started the turn say,
    never weaker than the generic sentinel. Another channel and the unattended lanes keep
    today's answer. MUTATION: drop the named-channel check from 1h, or let the exclusion
    fall through to the lift - red."""
    class AppExcluded(BaseTool):
        name = "app_excluded"
        description = "Excluded from the app."
        permission_level = level
        channel_restrictions = restrictions

        def run(self, **kwargs):
            return "ran"

    tool = AppExcluded()
    for lift in (True, False):
        _lift(monkeypatch, lift)
        for channel_lift in (True, False):
            for holder in ({}, {"holder_device": True},
                           {"holder_device": True, "holder_present": True}):
                decision = _policy("app_excluded", tool, channel_lift=channel_lift, **holder)
                assert decision.blocked and not decision.ask_each_call, (lift, holder)
                assert "Veyllo App" in decision.reason
            # Unattended: the coder and workflow steps decide by today's rules.
            today = evaluate_tool_policy("app_excluded", tool, "veyllo_app",
                                         is_channel_session=True, gated_lane=False,
                                         channel_lift=channel_lift)
            assert _policy("app_excluded", tool, gated=False, channel_lift=channel_lift,
                           holder_present=True, holder_device=True) == today
        # On Telegram, a tool that names only the Veyllo App is no business of 1h.
        on_telegram = evaluate_tool_policy("app_excluded", tool, "telegram",
                                           is_channel_session=True, is_admin=True)
        assert not on_telegram.ask_each_call
        if lift and level == "write" and restrictions == ("veyllo_app",):
            assert not on_telegram.blocked


@pytest.mark.parametrize("name", sorted(_CHANNEL_TOOLS))
def test_unattended_lane_unchanged(monkeypatch, name):
    """The coder and workflow steps (no gate) decide as before. MUTATION: drop `gated_lane`
    from 1h - red."""
    tool = _CHANNEL_TOOLS[name]
    for lift in (True, False):
        _lift(monkeypatch, lift)
        today = _policy(name, tool, gated=False)
        assert _policy(name, tool, gated=False, holder_present=True, holder_device=True) == today
