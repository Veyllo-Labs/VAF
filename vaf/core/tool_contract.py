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
  category          — which bundle the tool appears under in tool lists

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

# Human-facing bundles for tool lists (the web tools window, the CLI table, the
# TUI overlay, list_tools). The order is the display order.
#
# The four messaging keys are the KNOWN_CHANNELS literals from
# vaf/core/messaging_connections.py. They are repeated rather than imported so
# this module keeps its import surface; the subset relation is pinned by
# tests/test_tool_category_registry_sync.py, which fails if the two drift.
#
# This vocabulary is OPEN at runtime: resolve_tool_contract keeps a value it has
# never seen. Closing it would mean an MCP server or a third-party tool could
# never form a bundle of its own, which is exactly the capability the attribute
# exists for. The guard pins the IN-TREE declarations only.
TOOL_CATEGORIES = (
    "web", "files", "documents", "memory", "context", "code", "git", "github",
    "workflows", "skills", "automations", "timers", "calendar", "contacts",
    "mail", "whatsapp", "telegram", "discord", "slack", "messaging", "rooms",
    "tool_catalog", "cloud", "mcp", "general",
)

# Bundles belonging to a tool a USER uploaded live in their own namespace and can
# never be one of the above. A bundle is a statement about origin: a tool that
# ships with VAF is reviewed, versioned and trained, and a Python file dropped
# into the custom-tools store must not borrow that standing by declaring
# `category = "github"` and landing among the shipped GitHub tools.
#
# The prefix is stamped at the ONE boundary that knows a class came from a user
# file - load_custom_tool_class() in vaf/core/custom_tools_registry.py - and not
# in any of the four surfaces that render the list, which would each have to
# repeat the rule and would each eventually forget it.
#
# It is reserved, not merely conventional: an in-tree tool declaring it fails
# tests/test_tool_category_registry_sync.py.
CUSTOM_CATEGORY_PREFIX = "custom"


def namespaced_custom_category(raw: str | None) -> str:
    """The bundle a user-uploaded tool belongs to.

    A declared bundle is kept but moved into the custom namespace
    ("github" -> "custom_github"); an undeclared one becomes the plain
    "custom" bundle. Idempotent, so re-stamping an already-stamped class on a
    hot reload cannot produce "custom_custom_github".
    """
    key = str(raw or "").strip().lower()
    if not key or key == CUSTOM_CATEGORY_PREFIX:
        return CUSTOM_CATEGORY_PREFIX
    if key.startswith(f"{CUSTOM_CATEGORY_PREFIX}_"):
        return key
    if key == "general":
        return CUSTOM_CATEGORY_PREFIX
    return f"{CUSTOM_CATEGORY_PREFIX}_{key}"

# The canonical English name of each bundle, for the surfaces that have no
# translation catalogue of their own (the CLI table, the TUI overlay,
# list_tools). The web UI translates via its own message catalogues; the guard
# only pins that both know the same KEYS, not that they choose the same words.
CATEGORY_LABELS = {
    "web": "Web & research",        "files": "Files",
    "documents": "Documents",       "memory": "Memory",
    "context": "Working memory",    "code": "Code & execution",
    "git": "Git (local)",           "github": "GitHub",
    "workflows": "Workflows",       "skills": "Skills",
    "automations": "Automations",   "timers": "Timers & reminders",
    "calendar": "Calendar",         "contacts": "Contacts",
    "mail": "Email",                "whatsapp": "WhatsApp",
    "telegram": "Telegram",         "discord": "Discord",
    "slack": "Slack",               "messaging": "Messaging",
    "rooms": "Agent rooms",         "tool_catalog": "Tool catalogue",
    "cloud": "Cloud storage",       "mcp": "MCP",
    "general": "Other",
}


def category_label(key: str) -> str:
    """Human name for a bundle.

    A bundle in the custom namespace is named after the bundle it mirrors, so
    "custom_github" reads "Custom GitHub" and sits recognisably beside the
    shipped "GitHub" without being mistaken for it. An unknown key (a
    third-party tool or an MCP server naming its own) is title-cased rather
    than dropped.
    """
    if key == CUSTOM_CATEGORY_PREFIX:
        return "Custom tools"
    if key.startswith(f"{CUSTOM_CATEGORY_PREFIX}_"):
        return f"Custom {category_label(key[len(CUSTOM_CATEGORY_PREFIX) + 1:])}"
    return CATEGORY_LABELS.get(key) or key.replace("_", " ").strip().title()

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
    # Declared on BaseTool; kept here as a normalised, immutable snapshot so the
    # evaluator never reads a live attribute mid-decision.
    admin_only: bool = False
    # Presentation only: which bundle the tool appears under in tool lists.
    # No policy reads this field.
    category: str = "general"


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

    # An UNKNOWN category is kept verbatim, deliberately: that is how an MCP
    # server or a third-party tool names a bundle of its own. Only an empty or
    # malformed value collapses to "general", so a typo cannot produce a bundle
    # whose name is punctuation.
    raw_category = str(getattr(tool, "category", "general") or "general").strip().lower()
    if not raw_category.replace("_", "").replace("-", "").isalnum():
        raw_category = "general"

    return ToolContract(
        name=str(getattr(tool, "name", tool_name) or tool_name),
        permission_level=raw_permission,     # type: ignore[arg-type]
        channel_restrictions=restrictions,
        side_effect_class=raw_side_effect,   # type: ignore[arg-type]
        admin_only=admin_only,
        category=raw_category,
    )


def tool_category(tool_name: str, tool: Any | None) -> str:
    """
    Which bundle does this tool belong to in a human-facing list?

    The one place that answers it. Every list surface calls this instead of
    reading the attribute itself; before it, six sites hand-rolled the same
    getattr with the same default and none of them normalised.
    """
    return resolve_tool_contract(tool_name, tool).category


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
