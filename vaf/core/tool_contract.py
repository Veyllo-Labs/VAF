"""
Declarative Tool Contract

Small, centralized metadata + policy evaluation layer for tools.
This first version is intentionally narrow:
- permission_level: read | write | dangerous | system
- channel_restrictions: blocked chat sources
- side_effect_class: none | reversible | irreversible

It does NOT replace the router or tool implementation logic.
It only provides a consistent contract that `execute_tool()` can check before
running a tool.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

from vaf.core.trust import explain_gate, should_gate_tool

PermissionLevel = Literal["read", "write", "dangerous", "system"]
SideEffectClass = Literal["none", "reversible", "irreversible"]

ALLOWED_PERMISSION_LEVELS = {"read", "write", "dangerous", "system"}
ALLOWED_SIDE_EFFECT_CLASSES = {"none", "reversible", "irreversible"}
logger = logging.getLogger("vaf.policy")


@dataclass(frozen=True)
class ToolContract:
    name: str
    permission_level: PermissionLevel = "read"
    channel_restrictions: tuple[str, ...] = ()
    side_effect_class: SideEffectClass = "none"


@dataclass(frozen=True)
class ToolPolicyDecision:
    blocked: bool
    requires_confirmation: bool
    reason: str = ""


def _decision_label(*, blocked: bool, requires_confirmation: bool) -> str:
    if blocked:
        return "block"
    if requires_confirmation:
        return "confirm"
    return "allow"


def resolve_tool_contract(tool_name: str, tool: Any | None) -> ToolContract:
    """Resolve normalized contract metadata from a tool instance."""
    raw_permission = str(getattr(tool, "permission_level", "read") or "read").strip().lower()
    if raw_permission not in ALLOWED_PERMISSION_LEVELS:
        raw_permission = "read"

    raw_side_effect = str(getattr(tool, "side_effect_class", "none") or "none").strip().lower()
    if raw_side_effect not in ALLOWED_SIDE_EFFECT_CLASSES:
        raw_side_effect = "none"

    raw_restrictions = getattr(tool, "channel_restrictions", []) or []
    restrictions = tuple(
        str(value).strip().lower()
        for value in raw_restrictions
        if str(value).strip()
    )

    return ToolContract(
        name=str(getattr(tool, "name", tool_name) or tool_name),
        permission_level=raw_permission,  # type: ignore[arg-type]
        channel_restrictions=restrictions,
        side_effect_class=raw_side_effect,  # type: ignore[arg-type]
    )


def evaluate_tool_policy(
    tool_name: str,
    tool: Any | None,
    current_source: str,
    is_channel_session: bool,
) -> ToolPolicyDecision:
    """
    Evaluate tool access based on declarative metadata.

    Rules in this first cut:
    - channel_restrictions can block channel-origin sessions
    - permission_level=dangerous requires confirmation
    - legacy risky-tool gating remains active as a fallback
    """
    contract = resolve_tool_contract(tool_name, tool)

    source = str(current_source or "").strip().lower()

    old_blocked = False
    old_requires_confirmation = should_gate_tool(tool_name)
    old_label = _decision_label(
        blocked=old_blocked,
        requires_confirmation=old_requires_confirmation,
    )

    contract_blocked = False
    contract_block_reason = ""
    if is_channel_session and contract.channel_restrictions:
        blocked_sources = set(contract.channel_restrictions)
        effective_sources = {"channel"}
        if source:
            effective_sources.add(source)
        if blocked_sources & effective_sources:
            label = source if source else "channel-origin"
            contract_blocked = True
            contract_block_reason = f"Tool '{tool_name}' is blocked for {label} sessions by policy."

    contract_requires_confirmation = contract.permission_level == "dangerous"
    new_label = _decision_label(
        blocked=contract_blocked,
        requires_confirmation=contract_requires_confirmation,
    )
    if old_label != new_label:
        logger.info("POLICY_DIVERGENCE tool=%s old=%s new=%s", tool_name, old_label, new_label)

    if contract_blocked:
        return ToolPolicyDecision(
            blocked=True,
            requires_confirmation=False,
            reason=contract_block_reason,
        )

    requires_confirmation = contract_requires_confirmation or old_requires_confirmation
    if requires_confirmation:
        base_reason = explain_gate(tool_name)
        if contract.side_effect_class == "irreversible":
            base_reason = f"{base_reason} This action may be irreversible."
        elif contract.permission_level == "dangerous" and tool_name not in {"write_file", "move_file", "bash", "run_command", "python_exec"}:
            base_reason = "This action is marked as dangerous by the tool contract."
        return ToolPolicyDecision(
            blocked=False,
            requires_confirmation=True,
            reason=base_reason,
        )

    return ToolPolicyDecision(blocked=False, requires_confirmation=False, reason="")
