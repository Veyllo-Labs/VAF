# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
WhatsApp auth directory resolution per VAF user.

Each user has isolated credentials at ~/.vaf/users/<username>/whatsapp/. The linked
WhatsApp account is that user's AGENT number: the agent writes to contacts from it, and
the user never chats with the agent from that phone. One user, one account, one Node
process; there is deliberately no fallback to another user's credentials, because two
Baileys sockets on one credential set evict each other.
"""
import json
from pathlib import Path
from typing import List, Optional

from vaf.core.config import Config


def get_whatsapp_auth_dir(username: str) -> Path:
    """
    Return the WhatsApp auth directory for the given VAF username.
    Credentials (creds.json, Baileys multi-file state) are stored here.

    Args:
        username: VAF username (e.g. from session or local_admin_username)

    Returns:
        Path to user's WhatsApp auth directory
    """
    return Config.APP_DIR / "users" / username / "whatsapp"


def whatsapp_auth_exists(username: str) -> bool:
    """Check if this user has linked WhatsApp (creds.json present)."""
    auth_dir = get_whatsapp_auth_dir(username)
    creds_path = auth_dir / "creds.json"
    return creds_path.exists() and creds_path.is_file()


def get_linked_phone(username: str) -> Optional[str]:
    """E.164 number of the WhatsApp account this user linked (the agent's own number), or
    None when nothing is linked. Baileys records the paired account as ``me.id``
    (``<digits>[:<device>]@s.whatsapp.net``) in creds.json at pairing time."""
    creds_path = get_whatsapp_auth_dir(username) / "creds.json"
    try:
        data = json.loads(creds_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    me = data.get("me") if isinstance(data, dict) else None
    jid = str((me or {}).get("id") or "") if isinstance(me, dict) else ""
    digits = jid.split("@", 1)[0].split(":", 1)[0].strip()
    if not digits.isdigit() or not (7 <= len(digits) <= 15):
        return None
    return "+" + digits


def linked_usernames() -> List[str]:
    """Every VAF username with a linked WhatsApp account (a creds.json under its user dir).
    The bridge starts one Node process per name; the whitelist plays no part in it."""
    users_root = Config.APP_DIR / "users"
    try:
        entries = sorted(p for p in users_root.iterdir() if p.is_dir())
    except OSError:
        return []
    return [p.name for p in entries if (p / "whatsapp" / "creds.json").is_file()]
