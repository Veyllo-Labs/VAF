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

The folder is keyed by the account NAME, and the bridge runs every linked folder whose name
resolves to an active account. So a login must not outlive its account under that name:
deleting an account removes its login and its picture cache (`remove_login`, called by the
bridge once no Node runs on the folder any more), and creating an account moves aside a
login an earlier version left under a name that one of the three file systems opens as the
new name's folder (`set_aside_login`, to `whatsapp.previous-<UTC time>` beside it, which
`linked_usernames` never reads). Measured before: a new account named like a deleted one ran
the deleted account's linked WhatsApp as its own agent number, with its `linked_phone`.

Named boundary: the folder is not re-keyed by scope. That would move every existing login on
disk; the name is kept to one account instead (`user_admin.username_taken`).
"""
import json
import logging
import os
import time
from pathlib import Path
from typing import List, Optional

from vaf.core.config import Config

logger = logging.getLogger(__name__)

# The name a login found under a reused account name is moved to, plus a UTC time stamp.
LEFTOVER_PREFIX = "whatsapp.previous-"


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


def get_whatsapp_avatar_dir(username: str) -> Path:
    """The account's cache of profile pictures, beside its login (never shared between
    accounts: the pictures belong to the people this account's agent talks to)."""
    return Config.APP_DIR / "users" / username / "whatsapp_avatars"


def remove_login(username: str, *, avatars: bool = False) -> bool:
    """Remove this account's WhatsApp login folder and, with `avatars`, its picture cache.
    A link in their place is unlinked, never followed (`verified_download.remove_tree_or_link`).
    True when neither is there afterwards; False (logged) when something of them is left.
    The caller makes sure no Node runs on the folder (`whatsapp_bridge.login_in_use`): a
    running one would write a new login into the path."""
    from vaf.core.verified_download import remove_tree_or_link
    targets = [get_whatsapp_auth_dir(username)]
    if avatars:
        targets.append(get_whatsapp_avatar_dir(username))
    ok = True
    for target in targets:
        try:
            remove_tree_or_link(os.fspath(target))
        except OSError as e:
            logger.warning("WhatsApp: %s could not be removed: %s", target, e)
            ok = False
    return ok


def logins_named_like(username: str) -> List[Path]:
    """Every login folder (`users/<entry>/whatsapp`, a link in its place included) whose
    account folder name has the same `path_jail.portable_name_key` as `username`: the
    folders Windows or macOS may open as this name's, and the ones the bridge resolves to it
    by a casefolded match (`config.account_for_username`). Sorted; empty when there is no
    `users` folder."""
    from vaf.core.path_jail import portable_name_key
    key = portable_name_key(str(username or "").strip())
    if not key:
        return []
    try:
        entries = sorted((Config.APP_DIR / "users").iterdir())
    except OSError:
        return []
    out = []
    for entry in entries:
        login = entry / "whatsapp"
        if portable_name_key(entry.name) == key and (login.is_symlink() or login.exists()):
            out.append(login)
    return out


def set_aside_login(username: str) -> List[Path]:
    """Move every login an earlier account left under this name aside, before an account of
    that name exists: `users/<entry>/whatsapp` becomes `whatsapp.previous-<UTC
    %Y%m%dT%H%M%SZ>` (`-2`, `-3` when taken; no colon, which Windows refuses), and the picture
    cache beside it is removed. A link in place of the folder is moved itself, never followed.
    Returns the new paths. An OSError propagates: the account must not be created while the
    old login is still under its name.

    Moved, not removed: whose login it is cannot be known here (an account deleted before
    deletion removed its login, or a row removed outside VAF), and an admin who created the
    same person again renames it back. `linked_usernames` reads only `whatsapp/creds.json`,
    so a moved folder is never run and never hardened again (it keeps its 0700)."""
    from vaf.core.verified_download import remove_tree_or_link
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    moved = []
    for login in logins_named_like(username):
        target = login.with_name(LEFTOVER_PREFIX + stamp)
        n = 2
        while target.exists() or target.is_symlink():
            target = login.with_name(f"{LEFTOVER_PREFIX}{stamp}-{n}")
            n += 1
        os.rename(login, target)
        remove_tree_or_link(os.fspath(login.with_name("whatsapp_avatars")))
        moved.append(target)
    return moved


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
