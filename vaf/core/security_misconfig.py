# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Security misconfiguration checks (read-only, no side effects)."""

from __future__ import annotations

from typing import Any, Dict, List

from vaf.core.channel_ingress_policy import normalize_policy
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

    ingress = normalize_policy(cfg.get("channel_ingress_policy"))
    global_mode = ingress.get("mode", "paired_only")
    if global_mode == "permissive":
        findings.append(
            _finding(
                "high",
                "channel_policy_permissive",
                "Channel ingress policy mode is permissive; unknown senders may be accepted.",
            )
        )

    for channel in ("telegram", "whatsapp", "discord"):
        ch_cfg = ingress.get(channel) if isinstance(ingress.get(channel), dict) else {}
        ch_mode = ch_cfg.get("mode", "paired_only")
        if ch_mode == "permissive":
            findings.append(
                _finding(
                    "medium",
                    f"{channel}_policy_permissive",
                    f"{channel.title()} ingress policy is permissive.",
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
        entries = list(whatsapp_cfg.get("whitelist") or [])
        valid_entries = [e for e in entries if isinstance(e, dict) and str(e.get("phone_number") or "").strip()]
        if not valid_entries:
            findings.append(
                _finding(
                    "high",
                    "whatsapp_enabled_without_pairing",
                    "WhatsApp is enabled but no explicit paired phone numbers are configured.",
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
