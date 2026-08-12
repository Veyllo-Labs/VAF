# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The door for the interactive terminal: authenticate before entering the shield.

VAF's protection model is a shield around the running instance. Inside it, the
agents work unattended - automations fire, channels are answered, compaction
runs, and none of that may depend on someone typing a password after a reboot.
Everything arriving from OUTSIDE authenticates first: the web UI has always had
a login, and the terminal had nothing at all, because "the local user is the
admin" was true when the only local user was the owner.

So this is a DOOR, not a key. It does not unlock anything - the data keys are
machine-held either way - and the honest way to say what it buys is: it stops
the person who sits down at an already-unlocked machine from reading the
owner's chats by typing `vaf`. Against a stolen disk it does nothing; that is
what the at-rest encryption underneath is for.

Two rules follow from the shield model, and both are load-bearing:

- **Only interactive lanes ask.** `vaf run -p`, the tray, the headless runner,
  sub-agent spawns, the workflow engine and every automation run inside the
  shield already; prompting there would mean an unattended machine stops
  working after a reboot, which is the one thing the shield model must not cost.
- **The check is offline.** It verifies an Argon2 hash mirrored into the data
  keyring, not the auth database: the Postgres container is frequently down on
  a desktop, and a door that cannot be opened when the database is asleep is a
  lockout, not a security feature.
"""
from __future__ import annotations

import getpass
import logging
import os
import sys

logger = logging.getLogger("vaf.cli.gate")

RING_KEY = "admin_password_hash"
_MAX_ATTEMPTS = 3
_unlocked = False


def is_local_admin_account(username: str = "", user_scope_id: str = "") -> bool:
    """Is this the machine owner - the identity the terminal door belongs to?

    One slot, so it has to be the RIGHT account: keying it on "any admin role"
    let a second admin, or an admin password reset for someone else, silently
    take over who can open the terminal.
    """
    try:
        from vaf.core.config import Config
        owner_name = str(Config.get("local_admin_username", "") or "").strip().lower()
        owner_scope = str(Config.get("local_admin_scope_id", "") or "").strip()
    except Exception:
        return False
    if user_scope_id and owner_scope:
        return str(user_scope_id).strip() == owner_scope
    if username and owner_name:
        return str(username).strip().lower() == owner_name
    # Nothing recorded yet: the bootstrap account IS the owner.
    return not owner_name and not owner_scope


def mirror_admin_password_hash(password_hash: str) -> None:
    """Copy an Argon2 hash into the keyring so the terminal can verify offline.

    Called wherever an admin password is set or changed. A failure here must
    never break the password change itself - the worst case is that the CLI
    keeps letting the local user in, which is the behaviour of every release
    before this one.
    """
    try:
        if not password_hash:
            return
        from vaf.core.data_keyring import set_data_secret
        set_data_secret(RING_KEY, password_hash)
    except Exception as e:  # noqa: BLE001 - never block a password change
        logger.debug("Could not mirror the admin password hash: %s", e)


def _stored_hash() -> tuple:
    """(hash, unreadable). Those two must not collapse into one empty string.

    An unreadable keyring answered "" like a fresh install, so the door opened
    AND told the user no password was set - the most misleading combination
    available: it fails open exactly when something is wrong.
    """
    try:
        from vaf.core.data_keyring import peek_data_secret
        return (peek_data_secret(RING_KEY) or "", False)
    except Exception as e:
        logger.error("The key store could not be read while checking the password: %s", e)
        return ("", True)


def is_interactive() -> bool:
    """A human at a terminal, able to answer a prompt.

    STDIN decides, and stdin alone. Requiring stdout to be a tty as well looked
    like the same question and was a way through the door: with a real terminal
    in front of them, a caller only had to redirect output -
    `vaf session export <id> > chat.txt` - and the gate concluded "not
    interactive" and let the export run unchallenged. That is the exact command
    the group gate was added for.

    Redirecting output does not stop anyone from answering: getpass reads the
    terminal directly (/dev/tty on POSIX, the console handle on Windows) and
    writes its prompt there too, so the pipe stays clean and the question still
    reaches the human. A pipeline that feeds stdin - `echo x | vaf ...` - has no
    tty there and is still treated as a script, which is the case this test is
    actually for.
    """
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except Exception:
        return False


def gate_enabled() -> bool:
    try:
        from vaf.core.config import Config
        return bool(Config.get("cli_password_gate", True))
    except Exception:
        return True


def require_admin_password(*, force: bool = False) -> bool:
    """Ask for the admin password once per process. True = may proceed.

    Returns True without asking when there is nothing to verify against (a
    fresh install whose web account was never created), when the gate is turned
    off, or when this is not an interactive terminal. Those are not silent
    weakenings: each is a case where prompting would lock a working setup out
    rather than keep an intruder in.
    """
    global _unlocked
    if _unlocked and not force:
        return True
    if not gate_enabled():
        return True
    if os.environ.get("VAF_SKIP_PASSWORD_GATE") == "1":
        # The escape hatch for wrappers and service units that ARE inside the
        # shield but still run on a tty (supervisor scripts, systemd with a pty).
        return True
    if not is_interactive():
        return True

    stored, unreadable = _stored_hash()
    if unreadable:
        print("VAF: the key store cannot be read, so the password cannot be checked. "
              "Refusing to start rather than opening the door on an error.",
              file=sys.stderr)
        return False
    if not stored:
        print("VAF: no admin password is set yet - open the web UI once to create "
              "your account, then this terminal will ask for it.", file=sys.stderr)
        _unlocked = True
        return True

    from vaf.auth.crypto import verify_password

    for remaining in range(_MAX_ATTEMPTS, 0, -1):
        try:
            entered = getpass.getpass("VAF admin password: ")
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if entered and verify_password(stored, entered):
            _unlocked = True
            return True
        print(f"Wrong password. {remaining - 1} attempt(s) left.", file=sys.stderr)

    try:
        from vaf.core.security_events import log_security_event
        log_security_event("cli_password_gate_failed",
                           detail="Three failed admin password attempts at the terminal")
    except Exception:
        pass
    return False
