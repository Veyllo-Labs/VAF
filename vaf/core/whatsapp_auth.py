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


def harden_auth_dir(auth_dir: Path) -> int:
    """Make a linked account's session directory owner-only: the directory 0700, every file
    in it 0600. Returns how many files it touched.

    The directory is the WhatsApp login itself. Baileys keeps the Signal-protocol state in
    it, one file per key (creds.json with the identity key, pre-keys, sessions, sender
    keys, app-state keys), and wrote them with the process's default umask: measured on a
    linked install, 379 of 381 files were readable by every account on the machine and only
    creds.json was not. The bridge now starts Node with umask 077, so new files are born
    0600; this pass fixes what an earlier start already wrote. POSIX only, like every
    chmod: on Windows the profile directory's ACL is what protects the files
    (secure_store.harden_path says why).
    """
    from vaf.core.secure_store import harden_dir, harden_path
    if not auth_dir.is_dir():
        return 0
    harden_dir(auth_dir)
    touched = 0
    for entry in auth_dir.iterdir():
        if entry.is_file() and not entry.is_symlink():
            harden_path(entry)
            touched += 1
    return touched


def harden_linked_auth_dirs() -> int:
    """harden_auth_dir over every linked account; returns how many files it touched.

    Run at startup whatever the bridge's switch says. The bridge corrects a directory each
    time it starts Node, but it only starts when WhatsApp is switched on: a linked account
    that is switched off still holds a working login on disk and would otherwise keep the
    files an older release wrote readable by every account on the machine indefinitely."""
    return sum(harden_auth_dir(get_whatsapp_auth_dir(name)) for name in linked_usernames())


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
