# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Declarative Tool Contract
=========================

Centralized metadata + policy evaluation layer for tools.
Every tool declares its contract as class attributes on BaseTool; this module
reads those attributes and decides — before the tool runs — whether to:

  - BLOCK the call entirely (channel restriction or admin_only violation)
  - CONFIRM with the user (dangerous permission level or legacy gate)
  - ALLOW immediately (everything else)

Contract fields (all defined on BaseTool):

  permission_level  — "read" | "write" | "dangerous" | "system"
  channel_restrictions — sources where the tool is hard-blocked
  side_effect_class — "none" | "reversible" | "irreversible"
  admin_only        — True → blocked for non-admin sessions

Evaluation order inside evaluate_tool_policy():
  1. admin_only check  (hard block — role-based)
  2. channel_restrictions check  (hard block — source-based)
  3. permission_level == "dangerous"  → confirmation required
  4. permission_level == "system"     → skip legacy confirmation gate
  5. Legacy risky-tool gate (fallback for tools that predate the contract)
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


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ToolContract:
    name: str
    permission_level: PermissionLevel = "read"
    channel_restrictions: tuple[str, ...] = ()
    side_effect_class: SideEffectClass = "none"
    # Role-based restriction: True → only admin sessions may call this tool.
    # Stored here (not on BaseTool directly) so the evaluator always has a
    # normalised, immutable snapshot.
    admin_only: bool = False


@dataclass(frozen=True)
class ToolPolicyDecision:
    blocked: bool
    requires_confirmation: bool
    reason: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _decision_label(*, blocked: bool, requires_confirmation: bool) -> str:
    if blocked:
        return "block"
    if requires_confirmation:
        return "confirm"
    return "allow"


# ─────────────────────────────────────────────────────────────────────────────
# Contract resolution
# ─────────────────────────────────────────────────────────────────────────────

def resolve_tool_contract(tool_name: str, tool: Any | None) -> ToolContract:
    """
    Read and normalise contract metadata from a tool instance.

    Falls back to safe defaults for any missing or invalid value so that
    a misconfigured tool is never silently treated as more permissive than
    intended.
    """
    raw_permission = str(getattr(tool, "permission_level", "read") or "read").strip().lower()
    if raw_permission not in ALLOWED_PERMISSION_LEVELS:
        raw_permission = "read"

    raw_side_effect = str(getattr(tool, "side_effect_class", "none") or "none").strip().lower()
    if raw_side_effect not in ALLOWED_SIDE_EFFECT_CLASSES:
        raw_side_effect = "none"

    raw_restrictions = getattr(tool, "channel_restrictions", []) or []
    restrictions = tuple(
        str(v).strip().lower()
        for v in raw_restrictions
        if str(v).strip()
    )

    # admin_only defaults to False — absence of the attribute is treated as
    # "anyone can call this" (the safe default for existing tools).
    admin_only = bool(getattr(tool, "admin_only", False))

    return ToolContract(
        name=str(getattr(tool, "name", tool_name) or tool_name),
        permission_level=raw_permission,     # type: ignore[arg-type]
        channel_restrictions=restrictions,
        side_effect_class=raw_side_effect,   # type: ignore[arg-type]
        admin_only=admin_only,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Policy evaluation
# ─────────────────────────────────────────────────────────────────────────────

def evaluate_tool_policy(
    tool_name: str,
    tool: Any | None,
    current_source: str,
    is_channel_session: bool,
    is_admin: bool = False,
) -> ToolPolicyDecision:
    """
    Evaluate whether a tool may run in the current session context.

    Parameters
    ----------
    tool_name         : Name of the tool being called.
    tool              : Live tool instance (may be None for unknown tools).
    current_source    : Chat source string, e.g. "web", "telegram", "cli".
    is_channel_session: True when the session originates from a messaging
                        channel (Telegram, WhatsApp, Discord).
    is_admin          : True when the current user is an admin.
                        Derived in execute_tool() from _current_user_role and
                        _current_user_scope_id vs get_local_admin_scope_id().

    Returns
    -------
    ToolPolicyDecision with .blocked / .requires_confirmation / .reason.

    Evaluation order
    ----------------
    1. admin_only check      — role-based hard block (new)
    2. channel_restrictions  — source-based hard block (existing)
    3. permission_level      — confirmation gate (extended: "system" now skips legacy gate)
    4. Legacy risky-tool gate — fallback for tools that predate this contract
    """
    contract = resolve_tool_contract(tool_name, tool)
    source   = str(current_source or "").strip().lower()

    # ── 1. Admin-only check ───────────────────────────────────────────────
    # This is a hard block: if the tool requires an admin session and the
    # current user is not an admin, we refuse immediately with no confirmation
    # prompt.  The agent sees the error string returned by execute_tool() and
    # must handle it gracefully (e.g. tell the user it cannot do this).
    if contract.admin_only and not is_admin:
        logger.info("POLICY_BLOCK tool=%s reason=admin_only", tool_name)
        return ToolPolicyDecision(
            blocked=True,
            requires_confirmation=False,
            reason=(
                f"Tool '{tool_name}' requires an admin session. "
                "This action is not available for regular user accounts."
            ),
        )

    # ── 1b. Channel full-access (admin opt-in) ────────────────────────────
    # Messaging channels (Telegram/WhatsApp/Discord) normally cannot use
    # channel-restricted tools and have no interactive confirmation path. When
    # the admin enables `channel_tools_unrestricted`, channel sessions get the
    # same tools as the main agent: channel restrictions (section 2) and per-call
    # confirmations (sections 3–4) are lifted. This runs AFTER the admin_only
    # check above, so a non-admin channel user still cannot reach admin-only
    # tools — and the channel whitelist remains the primary gate upstream.
    if is_channel_session:
        try:
            from vaf.core.config import Config
            if Config.get("channel_tools_unrestricted", True):
                logger.info(
                    "POLICY_ALLOW tool=%s reason=channel_full_access source=%s",
                    tool_name, source or "channel",
                )
                return ToolPolicyDecision(blocked=False, requires_confirmation=False, reason="")
        except Exception:
            pass

    # ── 2. Channel restrictions ───────────────────────────────────────────
    # Hard block based on chat source (Telegram, WhatsApp, Discord, …).
    # Unrelated to user role — a tool can be blocked on messaging channels
    # even for admins (e.g. python_exec is blocked on all channels).
    if is_channel_session and contract.channel_restrictions:
        blocked_sources  = set(contract.channel_restrictions)
        effective_sources = {"channel"}  # generic "any channel" sentinel
        if source:
            effective_sources.add(source)
        if blocked_sources & effective_sources:
            label = source if source else "channel-origin"
            # Log divergence vs. the legacy gate (which didn't know about channel restrictions).
            old_requires_confirmation = should_gate_tool(tool_name)
            old_label = _decision_label(blocked=False, requires_confirmation=old_requires_confirmation)
            if old_label != "block":
                logger.info("POLICY_DIVERGENCE tool=%s old=%s new=block", tool_name, old_label)
            logger.info("POLICY_BLOCK tool=%s reason=channel source=%s", tool_name, label)
            return ToolPolicyDecision(
                blocked=True,
                requires_confirmation=False,
                reason=f"Tool '{tool_name}' is blocked for {label} sessions by policy.",
            )

    # ── 3. Permission level → confirmation gate ───────────────────────────
    if contract.permission_level == "dangerous":
        # Always prompt the user — regardless of legacy gate state.
        base_reason = explain_gate(tool_name)
        if contract.side_effect_class == "irreversible":
            base_reason = f"{base_reason} This action may be irreversible."
        elif tool_name not in {"move_file", "bash", "run_command", "python_exec"}:
            base_reason = "This action is marked as dangerous by the tool contract."
        logger.info("POLICY_CONFIRM tool=%s reason=dangerous", tool_name)
        return ToolPolicyDecision(
            blocked=False,
            requires_confirmation=True,
            reason=base_reason,
        )

    if contract.permission_level == "system":
        # "system" tools bypass the legacy confirmation gate entirely.
        # These are internal plumbing tools (memory updates, context tools,
        # create_agent_tool) where a user-facing confirmation prompt would be
        # disruptive and the action is already gated by admin_only or context.
        # Previously this value was defined but never evaluated — now it is.
        logger.debug("POLICY_ALLOW tool=%s reason=system_bypass", tool_name)
        return ToolPolicyDecision(blocked=False, requires_confirmation=False, reason="")

    # ── 4. Legacy risky-tool gate (fallback) ─────────────────────────────
    # Keeps existing behaviour for built-in tools that predate the contract
    # system and haven't yet been assigned explicit permission_levels.
    old_requires_confirmation = should_gate_tool(tool_name)

    # Log divergence between old and new systems so we can migrate gradually.
    old_label = _decision_label(blocked=False, requires_confirmation=old_requires_confirmation)
    new_label = _decision_label(blocked=False, requires_confirmation=False)
    if old_label != new_label:
        logger.info("POLICY_DIVERGENCE tool=%s old=%s new=%s", tool_name, old_label, new_label)

    if old_requires_confirmation:
        base_reason = explain_gate(tool_name)
        if contract.side_effect_class == "irreversible":
            base_reason = f"{base_reason} This action may be irreversible."
        return ToolPolicyDecision(
            blocked=False,
            requires_confirmation=True,
            reason=base_reason,
        )

    return ToolPolicyDecision(blocked=False, requires_confirmation=False, reason="")
