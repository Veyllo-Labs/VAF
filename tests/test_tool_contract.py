# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.modules.setdefault("llama_cpp", MagicMock())

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
    fake_agent = SimpleNamespace(
        tools={"dangerous_dummy": DangerousDummyTool()},
        _event_sink=None,
        _allow_once_tools=set(),
        _noninteractive=True,
        _current_turn_thinking_mode=False,
        _current_chat_source="web",
        current_session_id=None,
        _record_tool_used=lambda name: None,
        _plan_gate_decision=lambda name, tool, tool_args=None: None,  # plan gate is a no-op here (noninteractive)
        _proactive_reply_gate_decision=lambda name, tool, args: None,  # incident gates: no-op
        _ask_first_gate_decision=lambda name, tool: None,
    )

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


def test_channel_full_access_allows_restricted_tool_when_enabled():
    # Flag ON: a channel-restricted, dangerous tool runs on Telegram with no confirmation.
    with _patch_channel_flag(True):
        decision = evaluate_tool_policy(
            tool_name="channel_restricted_dangerous_dummy",
            tool=ChannelRestrictedDangerousDummyTool(),
            current_source="telegram",
            is_channel_session=True,
            is_admin=True,
        )
    assert decision.blocked is False
    assert decision.requires_confirmation is False


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
