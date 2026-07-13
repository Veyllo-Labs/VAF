# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Channel registry drift guard (CLAUDE Rule 2: CI guard over prose rule).

The messaging platform list exists in many copies: schema enums, dispatch maps,
send-tool tuples, thinking-mode strip set, front-office allow-list, ingress
policy and channel_restrictions guard tuples. Two real drifts existed before
this guard (the persona API route and the front-office prompt each missed a
platform). Single source of truth: KNOWN_CHANNELS / ROUTABLE_CHANNELS /
CHANNEL_SEND_TOOLS in vaf/core/messaging_connections.py - extend THERE first,
then follow the checklist in docs/integrations/CONNECTIONS.md (Channel model).
"""
from pathlib import Path

from vaf.core.messaging_connections import (
    CHANNEL_SEND_TOOLS,
    KNOWN_CHANNELS,
    ROUTABLE_CHANNELS,
)

SEND_TOOLS = set(CHANNEL_SEND_TOOLS.values())


def _src(module) -> str:
    return Path(module.__file__).read_text(encoding="utf-8")


# ── the SSOT itself ──────────────────────────────────────────────────────────

def test_ssot_shape():
    assert set(ROUTABLE_CHANNELS) <= set(KNOWN_CHANNELS)
    assert set(CHANNEL_SEND_TOOLS) == set(KNOWN_CHANNELS)
    for ch, tool in CHANNEL_SEND_TOOLS.items():
        assert tool == f"send_{ch}"


def test_router_dispatches_every_routable_channel():
    import vaf.core.messaging_connections as mc
    src = _src(mc)
    for ch in ROUTABLE_CHANNELS:
        assert f'main == "{ch}"' in src, (
            f"send_to_main_messenger lost the dispatch branch for {ch}"
        )


# ── identity allowlist copies ────────────────────────────────────────────────

def test_user_workspace_allowlist_uses_ssot():
    import vaf.auth.user_workspace as uw
    assert "KNOWN_CHANNELS as VALID_MAIN_MESSENGERS" in _src(uw), (
        "user_workspace healing validator no longer imports the SSOT"
    )


def test_persona_route_allowlist_uses_ssot():
    import vaf.api.user_persona_routes as pr
    assert "KNOWN_CHANNELS as valid_main_messengers" in _src(pr), (
        "persona API route re-grew its own platform tuple (this copy drifted before: it missed email)"
    )


def test_update_user_identity_enum_covers_all_channels():
    from vaf.tools.user_identity import UpdateUserIdentityTool
    enum = set(
        UpdateUserIdentityTool.parameters["properties"]["main_messenger"]["enum"]
    )
    missing = set(KNOWN_CHANNELS) - enum
    assert not missing, f"update_user_identity enum misses platforms: {missing}"
    unknown = enum - set(KNOWN_CHANNELS) - {"email"}
    assert not unknown, (
        f"update_user_identity enum has platforms unknown to KNOWN_CHANNELS: {unknown} "
        f"- extend the SSOT first"
    )


# ── send-tool set copies ─────────────────────────────────────────────────────

def test_thinking_strip_set_covers_all_send_tools():
    from vaf.core.thinking_mode import _SENT_TOOLS
    missing = (SEND_TOOLS | {"send_to_user", "send_mail"}) - _SENT_TOOLS
    assert not missing, (
        f"thinking-mode _SENT_TOOLS misses {missing} - an unstripped send tool is an "
        f"untracked outbound channel in background runs"
    )


def test_automation_dedup_set_covers_all_send_tools():
    from vaf.core.automation import _SEND_STEP_TOOLS
    missing = (SEND_TOOLS | {"send_to_user", "send_mail"}) - _SEND_STEP_TOOLS
    assert not missing, f"automation double-delivery dedup misses {missing}"


def test_front_office_allowlist_covers_platform_send_tools():
    # Per-platform send tools are the contact-to-OWNER back-channel; the
    # channel-agnostic send_to_user stays out by design (default deny).
    from vaf.core.front_office_tools import FRONT_OFFICE_ALLOWED_TOOLS
    missing = SEND_TOOLS - FRONT_OFFICE_ALLOWED_TOOLS
    assert not missing, f"front-office allow-list misses platform send tools: {missing}"
    assert "send_to_user" not in FRONT_OFFICE_ALLOWED_TOOLS


def test_agent_channel_tool_map_and_injection():
    import vaf.core.agent as agent_mod
    src = _src(agent_mod)
    for ch in KNOWN_CHANNELS:
        assert f'"{ch}": "send_{ch}"' in src, (
            f"agent.py router channel-to-tool map lost {ch}"
        )
    for tool in sorted(SEND_TOOLS | {"send_to_user"}):
        assert f'"{tool}"' in src, f"agent.py lost every mention of {tool}"


def test_engine_injection_tuple_covers_all_send_tools():
    import vaf.workflows.engine as engine_mod
    src = _src(engine_mod)
    for tool in sorted(SEND_TOOLS | {"send_to_user"}):
        assert f'"{tool}"' in src, (
            f"workflow engine scope injection lost {tool} (cross-user leak risk)"
        )


# ── ingress / security guard copies (fail OPEN for unknown channels) ─────────

def test_ingress_policy_supports_exactly_the_routable_channels():
    from vaf.core.channel_ingress_policy import _SUPPORTED_CHANNELS
    assert set(_SUPPORTED_CHANNELS) == set(ROUTABLE_CHANNELS), (
        "channel_ingress_policy supported channels drifted from ROUTABLE_CHANNELS - "
        "a bridge without ingress policy (or vice versa) is a conscious decision, "
        "update the SSOT and this guard together"
    )


def test_channel_restriction_tuples_cover_all_routable_channels():
    # channel_restrictions is a BLOCKlist per source: a channel missing from the
    # tuple means the tool is ALLOWED from that channel (fail open). Every tool
    # that blocks messaging channels must block ALL routable ones.
    from vaf.tools.host_bash import HostBashTool
    from vaf.tools.timer import SetTimerTool
    from vaf.tools.python_exec import PythonExecTool
    from vaf.tools.agent_tool_builder import AgentToolBuilderTool
    from vaf.tools.agent_workflow_builder import AgentWorkflowBuilderTool

    for tool_cls in (HostBashTool, SetTimerTool, PythonExecTool,
                     AgentToolBuilderTool, AgentWorkflowBuilderTool):
        restricted = set(getattr(tool_cls, "channel_restrictions", ()) or ())
        missing = set(ROUTABLE_CHANNELS) - restricted
        assert not missing, (
            f"{tool_cls.__name__}.channel_restrictions misses {missing} - "
            f"the tool is silently ALLOWED from that channel"
        )
