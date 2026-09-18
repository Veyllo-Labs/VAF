# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Security misconfiguration checks (read-only, no side effects)."""

from __future__ import annotations

from typing import Any, Dict, List

from vaf.core.channel_ingress_policy import FRONT_OFFICE_CHANNELS, MAIL_CHANNEL, normalize_policy
from vaf.core.config import Config


def _finding(severity: str, code: str, message: str) -> Dict[str, str]:
    return {"severity": severity, "code": code, "message": message}


def collect_security_findings(config: Dict[str, Any] | None = None) -> List[Dict[str, str]]:
    """
    Return security findings derived from config.

    This function is pure/read-only: it does not mutate config, write files, or log secrets.
    """
    cfg = config if isinstance(config, dict) else Config.load()
    findings: List[Dict[str, str]] = []

    network_enabled = bool(cfg.get("local_network_enabled", False))
    if network_enabled:
        tls_enabled = bool(cfg.get("local_network_tls_enabled", False))
        firewall_enabled = bool(cfg.get("local_network_firewall_enabled", False))
        require_login = bool(cfg.get("local_network_require_login", False))
        require_2fa = bool(cfg.get("local_network_require_2fa", False))

        if not tls_enabled:
            findings.append(
                _finding(
                    "high",
                    "network_tls_disabled",
                    "Local network mode is enabled but TLS is disabled.",
                )
            )
        if not firewall_enabled:
            findings.append(
                _finding(
                    "medium",
                    "network_firewall_disabled",
                    "Local network mode is enabled but firewall checks are disabled.",
                )
            )
        if not require_login:
            findings.append(
                _finding(
                    "high",
                    "network_login_not_required",
                    "Local network mode is enabled but login is not required.",
                )
            )
        if not require_2fa:
            findings.append(
                _finding(
                    "medium",
                    "network_2fa_not_required",
                    "Local network mode is enabled but 2FA is not required.",
                )
            )

    # The one state that means "somebody the owner never decided about is answered": the
    # channel switch. The expert modes this used to warn about are gone (they said the same
    # thing as a contact's own permission, in a place nobody looked), so a warning about them
    # would be a check that can no longer fire.
    # Every Front Office channel, read from the one tuple rather than a hand-kept list: mail
    # has the same switch and the same meaning, and a perimeter check that skips a channel
    # reports a closed perimeter while that channel answers strangers.
    ingress = normalize_policy(cfg.get("channel_ingress_policy"))
    for channel in FRONT_OFFICE_CHANNELS:
        ch_cfg = ingress.get(channel) if isinstance(ingress.get(channel), dict) else {}
        if bool(ch_cfg.get("open_to_new_senders")):
            qualifier = (" On mail only a sender the provider verified is answered, and by "
                         "default the answer waits as a draft."
                         if channel == MAIL_CHANNEL else "")
            findings.append(
                _finding(
                    "medium",
                    f"{channel}_open_to_new_senders",
                    f"{channel.title()} Inbound is open: anybody who writes there is answered, "
                    f"unless you blocked them in the contact book.{qualifier}",
                )
            )

    telegram_cfg = cfg.get("telegram_config") if isinstance(cfg.get("telegram_config"), dict) else {}
    if telegram_cfg.get("enabled"):
        entries = list(telegram_cfg.get("whitelist") or []) + list(telegram_cfg.get("relay_whitelist") or [])
        valid_entries = [e for e in entries if isinstance(e, dict) and str(e.get("telegram_user_id") or "").strip()]
        if not valid_entries:
            findings.append(
                _finding(
                    "high",
                    "telegram_enabled_without_pairing",
                    "Telegram is enabled but no explicit paired users are configured.",
                )
            )

    whatsapp_cfg = cfg.get("whatsapp_config") if isinstance(cfg.get("whatsapp_config"), dict) else {}
    if whatsapp_cfg.get("enabled"):
        # An enabled WhatsApp connection with NO registered main-user number is the normal
        # outbound-only state (the linked account is the agent's own number; inbound stays
        # paired_only), so the whitelist is not the pairing to check. What is worth a
        # finding is a switched-on connection with nothing linked behind it.
        try:
            from vaf.core.whatsapp_auth import linked_usernames
            linked = linked_usernames()
        except Exception:
            linked = []
        if not linked:
            findings.append(
                _finding(
                    "medium",
                    "whatsapp_enabled_without_link",
                    "WhatsApp is enabled but no account is linked (no agent number).",
                )
            )

    discord_cfg = cfg.get("discord_config") if isinstance(cfg.get("discord_config"), dict) else {}
    if discord_cfg.get("enabled"):
        admin_user = str(discord_cfg.get("admin_user_id") or "").strip()
        if not admin_user:
            findings.append(
                _finding(
                    "high",
                    "discord_enabled_without_admin_pairing",
                    "Discord is enabled but no admin user is paired.",
                )
            )

    return findings
