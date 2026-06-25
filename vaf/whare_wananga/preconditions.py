# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Whare Wananga -- tool preconditions.

A tool can only be learned if its dependency is configured. There is no single
"configured" flag per tool; connection config lives per integration in Config
(telegram_config / discord_config / whatsapp_config / email_config). This module maps
connection tools to those existing flags and exposes one precondition check.

Tools without a known connection dependency are treated as always configured
(requires_config = False) -- they have no setup to do. Connections beyond the four
covered here (calendar / github / cloud) currently default to configured; extend the
map below as their checks are wired.
"""

from __future__ import annotations

from typing import Dict, Optional


def _cfg(key: str):
    try:
        from vaf.core.config import Config
        return Config.get(key)
    except Exception:
        return None


def _telegram_configured() -> bool:
    c = _cfg("telegram_config")
    return bool(isinstance(c, dict) and c.get("bot_token"))


def _discord_configured() -> bool:
    c = _cfg("discord_config")
    return bool(isinstance(c, dict) and c.get("bot_token"))


def _whatsapp_configured() -> bool:
    c = _cfg("whatsapp_config")
    # whatsapp_config is None until set up; a present dict means it was configured.
    return bool(isinstance(c, dict) and (c.get("enabled") or c.get("whitelist")))


def _email_configured() -> bool:
    for key in ("email_config", "email_config_by_scope", "email_config_by_user"):
        c = _cfg(key)
        if isinstance(c, dict):
            if c.get("accounts"):
                return True
            # by_scope / by_user are nested: { "<scope>": { "accounts": [...] } }
            for v in c.values():
                if isinstance(v, dict) and v.get("accounts"):
                    return True
    return False


# connection key -> configured-check
_CONNECTION_CHECKS = {
    "telegram": _telegram_configured,
    "discord": _discord_configured,
    "whatsapp": _whatsapp_configured,
    "email": _email_configured,
}


def _connection_for_tool(tool: str) -> Optional[str]:
    """Map a tool name to the connection it depends on, or None (no dependency)."""
    n = (tool or "").lower()
    if "telegram" in n:
        return "telegram"
    if "discord" in n:
        return "discord"
    if "whatsapp" in n:
        return "whatsapp"
    if "mail" in n or "email" in n:
        return "email"
    return None  # calendar / github / cloud / everything else: no precondition yet


def tool_class(tool: str, all_tools=None) -> set:
    """The set of tools allowed during training of `tool` (the training "sandbox").

    = the tool itself plus its connection-class siblings (e.g. all whatsapp_* tools share
    the whatsapp class). Tools without a connection form a singleton class {tool}. This is
    the scope the trainer may call -- not OS-level isolation.
    """
    conn = _connection_for_tool(tool)
    if not conn:
        return {tool}
    siblings = {tool}
    for t in (all_tools or []):
        if _connection_for_tool(t) == conn:
            siblings.add(t)
    return siblings


def tool_precondition(tool: str) -> Dict[str, object]:
    """Return {'requires_config', 'configured', 'connection'} for a tool.

    Tools with no known connection dependency are always configured.
    """
    conn = _connection_for_tool(tool)
    if not conn:
        return {"requires_config": False, "configured": True, "connection": None}
    check = _CONNECTION_CHECKS.get(conn)
    configured = bool(check()) if check else True
    return {"requires_config": True, "configured": configured, "connection": conn}
