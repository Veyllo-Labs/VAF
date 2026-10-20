# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Creating local accounts: the rules, in one place.

WHY THIS MODULE EXISTS. Account creation lived in two HTTP routes that had
drifted apart while nobody was comparing them:

    /api/auth/bootstrap   duplicate check case-INsensitive, password >= 8,
                          keyring mirror unconditional, config identity written
    POST /api/users       duplicate check case-SENSITIVE, NO password rule,
                          mirror conditional, no config identity

So the same question - "may this username exist, is this password good enough" -
had two different answers depending on which door you came through, and
"Alice"/"alice" could both be created. A third caller (the terminal setup) would
have made it three. The rules live here now; the routes call in and carry only
what is genuinely theirs (tokens, cookies, sessions, HTTP shapes).

WHAT STAYS OUT. Anything that is about a web request rather than about an
account: access/refresh tokens, opening the UserSession row, the auth cookie. A
terminal setup needs an account, not a browser session. Ending sessions is the
exception, because it is an account rule: a reset password or second factor ends
every session of the account, whichever door made the reset. It goes through
vaf/auth/sessions.py, the one owner of the session rows.

THE FIRST-RUN SETUP CODE. While no admin exists, a browser that is not on this
machine must bring the code `vaf setup code` shows in a terminal on it
(`first_run_setup_code`, `setup_code_matches`, checked by `create_first_admin`).
The desktop window and `vaf setup` need none.

NAMED BOUNDARY (Rule 0b). This is not exported on the framework facade. An
embedder brings their own user model - EMBEDDING.md tells them `user_scope` is
an assertion THEY make after authenticating their own users, so local account
management is not a primitive they are missing. If that changes, the measurement
comes first.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from sqlalchemy import func, select

from vaf.auth.crypto import hash_password, hash_password_async
from vaf.auth.models import LocalUser

logger = logging.getLogger(__name__)

MIN_USERNAME_LEN = 2
MIN_PASSWORD_LEN = 8


class UserAdminError(Exception):
    """A rule said no. Carries the HTTP status the routes used to raise.

    The status travels with the error so both front ends can stay thin: the
    routes map it onto HTTPException, the CLI maps the `code` onto an exit
    code. Neither re-derives the rule.
    """

    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def validate_username(username: str) -> str:
    """Trim and check. Returns the cleaned name that must be stored."""
    cleaned = (username or "").strip()
    if len(cleaned) < MIN_USERNAME_LEN:
        raise UserAdminError(
            "username_invalid",
            f"Username must be at least {MIN_USERNAME_LEN} characters",
            400,
        )
    return cleaned


def validate_password(password: str) -> None:
    if not password or len(password) < MIN_PASSWORD_LEN:
        raise UserAdminError(
            "password_too_short",
            f"Password must be at least {MIN_PASSWORD_LEN} characters",
            400,
        )


async def username_taken(db, username: str) -> bool:
    """Case-insensitive, always.

    A case-sensitive check let "Alice" and "alice" coexist while every lookup
    that matters - login, and `is_local_admin_account` - compares lowercased.
    Two rows, one of them unreachable.
    """
    result = await db.execute(
        select(LocalUser).where(func.lower(LocalUser.username) == username.lower())
    )
    return result.scalar_one_or_none() is not None


async def active_admin(db) -> Optional[LocalUser]:
    """The admin that makes this machine "already set up", or None."""
    result = await db.execute(
        select(LocalUser).where(LocalUser.role == "admin", LocalUser.is_active == True)  # noqa: E712
    )
    return result.scalar_one_or_none()


async def create_local_user(
    db,
    *,
    username: str,
    password: str,
    role: str = "user",
    permissions: Optional[Dict[str, Any]] = None,
    requires_2fa_setup: bool = True,
) -> LocalUser:
    """Insert one account. Validation, hashing and the keyring mirror included.

    The mirror runs AFTER the commit, which is a deliberate change from the
    order bootstrap used. Mirroring first meant a failed insert could leave the
    terminal door holding a password hash for an account that does not exist -
    the door would then ask for a password nobody can be sure of. The other
    direction is harmless and self-healing: a successful login mirrors the hash
    again (auth_routes login), so a mirror that fails here repairs itself the
    first time the owner signs in.
    """
    cleaned = validate_username(username)
    validate_password(password)
    if await username_taken(db, cleaned):
        raise UserAdminError("username_taken", "Username already taken", 409)

    password_hash = await hash_password_async(password)
    user = LocalUser(
        username=cleaned,
        password_hash=password_hash,
        role=(role or "user").lower(),
        requires_2fa_setup=requires_2fa_setup,
    )
    if permissions is not None:
        user.permissions = permissions
    db.add(user)
    await db.commit()
    await db.refresh(user)

    # Only the machine owner's password opens the terminal door; a second admin
    # must not silently take that slot over.
    try:
        from vaf.cli.gate import is_local_admin_account, mirror_admin_password_hash
        if is_local_admin_account(username=cleaned):
            mirror_admin_password_hash(password_hash)
    except Exception as e:  # never fail an account over the mirror
        logger.debug("Password hash was not mirrored to the keyring: %s", e)

    return user


def apply_terminal_reset(user: LocalUser, new_password: str) -> None:
    """The fields a terminal reset sets: a new password, and the second factor set up again at
    the next login (the old one may be on a phone the owner no longer has, or the attacker's)."""
    validate_password(new_password)
    user.password_hash = hash_password(new_password)
    user.totp_secret = None
    user.totp_nonce = None
    user.requires_2fa_setup = True


async def find_user(db, username: str) -> Optional[LocalUser]:
    result = await db.execute(
        select(LocalUser).where(func.lower(LocalUser.username) == str(username or "").strip().lower())
    )
    return result.scalar_one_or_none()


async def change_own_password(db, user: LocalUser, current_password: str, new_password: str,
                              code: str = "", *, ip: str = "") -> None:
    """An account changes its own password (Settings, Account): the current password, and the
    second factor's code when one is set up, so a session left open on someone else's screen
    is not enough. The code is checked like at sign-in (vaf/auth/second_factor.py: counted in
    the account's window, spent once). The row must still verify (vaf/auth/account_seal.py);
    the write re-seals it. The owner's account also refreshes the terminal door's hash copy."""
    from vaf.auth import account_seal, second_factor
    from vaf.auth.crypto import verify_password_async
    if not account_seal.check(user, where="change own password"):
        raise UserAdminError("record_changed", account_seal.REFUSAL_LOGIN, 403)
    if not await verify_password_async(user.password_hash, current_password or ""):
        raise UserAdminError("wrong_password", "The current password is not right", 401)
    if user.totp_secret and user.totp_nonce and not user.requires_2fa_setup:
        reason = second_factor.check_second_factor(user, code, ip=ip, where="change-password")
        if reason == second_factor.THROTTLED:
            raise UserAdminError("throttled", "Too many wrong 2FA codes. Try again later.", 429)
        if reason == second_factor.UNREADABLE:
            raise UserAdminError("twofa_unreadable", "The 2FA secret cannot be read on this machine", 400)
        if reason is not None:
            raise UserAdminError("wrong_code", "The 2FA code is not right", 400)
    validate_password(new_password)
    if await verify_password_async(user.password_hash, new_password):
        raise UserAdminError("same_password", "The new password is the current one", 400)
    user.password_hash = await hash_password_async(new_password)
    await db.commit()
    try:
        from vaf.cli.gate import is_local_admin_account, mirror_admin_password_hash
        if is_local_admin_account(username=str(user.username or ""),
                                  user_scope_id=str(user.user_scope_id or "")):
            mirror_admin_password_hash(user.password_hash)
    except Exception as e:  # the door repairs itself at the next login
        logger.debug("Password hash was not mirrored to the keyring: %s", e)


async def reset_password_from_terminal(db, user: LocalUser, new_password: str) -> None:
    """The way back in when the admin's password is gone (`vaf users reset-password`).

    Only the terminal calls it, after its own proof (a person at the terminal with the admin
    password or the recovery key). The row is re-sealed as it stands now
    (`account_seal.trusted_rewrite`), which is what makes this the recovery for a row changed
    outside VAF as well; the command shows such a row and asks before it gets here.
    """
    from vaf.auth import account_seal
    from vaf.auth.sessions import end_account_sessions
    apply_terminal_reset(user, new_password)
    with account_seal.trusted_rewrite():
        await db.commit()
    # Every session ends: the reset exists because the password is gone or no longer trusted.
    # The running service sees it within its session cache's few seconds; a socket it has open
    # stays until it reconnects or the service restarts (vaf/auth/sessions.py).
    await end_account_sessions(db, user.id)
    try:
        from vaf.cli.gate import is_local_admin_account, mirror_admin_password_hash
        if is_local_admin_account(username=str(user.username or ""),
                                  user_scope_id=str(user.user_scope_id or "")):
            mirror_admin_password_hash(user.password_hash)
    except Exception as e:  # the door repairs itself at the next login
        logger.debug("Password hash was not mirrored to the keyring: %s", e)


# ── the first-run setup code ───────────────────────────────────────────────────

# While no admin exists, the web setup creates one for whoever asks first, and every admitted
# address may ask (measured: from 10.1.2.3, needs-setup true, then bootstrap 200 with role
# admin). On a VPS the admitted private ranges include the provider's network and the Docker
# bridges. So a browser that is not on this machine must bring a code that only a terminal on
# the machine can show (`vaf setup code`). The desktop window and `vaf setup` never need it.
SETUP_CODE_NAME = "first_run_setup_code"
# Crockford's base32: no I, L, O or U, so a code read off a screen is typed back right.
_SETUP_CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_SETUP_CODE_LENGTH = 16          # 80 bits


def _setup_code_shape(raw: str) -> str:
    return "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))


def first_run_setup_code() -> str:
    """The code a browser on another device needs to create the first admin, as
    XXXX-XXXX-XXXX-XXXX. Minted once into the data keyring (never config.json) and shown
    again on every call; "" once this installation has had an admin. Raises RuntimeError when
    the keyring cannot be read."""
    import secrets as _secrets

    from vaf.auth import account_seal
    from vaf.core.data_keyring import peek_data_secret, set_data_secret
    stored = peek_data_secret(SETUP_CODE_NAME)
    if account_seal.admin_established():
        return ""
    if not stored:
        stored = "".join(_secrets.choice(_SETUP_CODE_ALPHABET) for _ in range(_SETUP_CODE_LENGTH))
        set_data_secret(SETUP_CODE_NAME, stored)
    return _setup_code_shape(stored)


def setup_code_matches(candidate: str) -> bool:
    """Whether `candidate` is the stored setup code: case, dashes and spaces do not matter,
    compared in constant time. False when no code was ever shown (nobody ran `vaf setup code`)
    or the keyring cannot be read."""
    import hmac
    try:
        from vaf.core.data_keyring import peek_data_secret
        stored = peek_data_secret(SETUP_CODE_NAME)
    except Exception:
        return False
    if not stored:
        return False
    given = "".join(ch for ch in str(candidate or "").upper() if not ch.isspace() and ch != "-")
    return hmac.compare_digest(given.encode("utf-8"), stored.encode("utf-8"))


def _retire_setup_code() -> None:
    try:
        from vaf.core.data_keyring import delete_data_secret
        delete_data_secret(SETUP_CODE_NAME)
    except Exception as e:
        logger.warning("The first-run setup code could not be removed from the keyring: %s", e)


async def create_first_admin(
    db,
    *,
    username: str,
    password: str,
    agent_name: Optional[str] = None,
    allow_established: bool = False,
    setup_code: Optional[str] = None,
    require_setup_code: bool = False,
) -> LocalUser:
    """Create THE admin of this machine: the account plus the identity around it.

    Ordering is chosen so that every prefix of this function leaves a state a
    human can understand:

    1. refuse if an admin already exists (the "first" in the name is a rule)
    2. the account row
    3. the config identity - who the CLI and tokenless localhost are
    4. the user directory and the agent workspace

    If step 3 fails, the row from step 2 is removed again: a machine with an
    admin account whose identity was never recorded resolves every local caller
    to the legacy fallback scope, which is worse than having no account at all.
    If even the removal fails, the raised error names the two config lines to
    write by hand rather than leaving the operator guessing.
    """
    if await active_admin(db) is not None:
        raise UserAdminError("admin_exists", "An admin account already exists", 403)
    # The keyring knows whether this installation has had an admin; an empty table alone is
    # not a fresh install (rows deleted or demoted in the database). Only the terminal setup,
    # behind the terminal door, may set an admin up again (`allow_established`).
    from vaf.auth import account_seal
    if not allow_established and account_seal.admin_established():
        raise UserAdminError(
            "admin_established",
            "This installation already had an admin. Restore the database, or set the admin up "
            "again in a terminal on this machine: vaf setup", 403)
    # Before anything is created or hashed: a stranger's attempt costs nothing and leaves no row.
    if require_setup_code and not setup_code_matches(setup_code or ""):
        raise UserAdminError(
            "setup_code",
            "The setup code is not right. Show it with `vaf setup code` in a terminal on the "
            "machine VAF runs on.", 401)

    user = await create_local_user(
        db,
        username=username,
        password=password,
        role="admin",
        requires_2fa_setup=True,
    )

    try:
        from vaf.core.config import Config
        config = Config.load()
        config["local_admin_scope_id"] = str(user.user_scope_id)
        config["local_admin_username"] = user.username
        Config.save(config)
    except Exception as e:
        try:
            await db.delete(user)
            await db.commit()
        except Exception as cleanup_error:
            raise UserAdminError(
                "identity_not_written",
                "The account was created but its identity could not be written to "
                f"the config, and the account could not be removed either ({e}). "
                "Set these two keys by hand before starting VAF: "
                f'local_admin_scope_id = "{user.user_scope_id}", '
                f'local_admin_username = "{user.username}".',
                500,
            ) from cleanup_error
        raise UserAdminError(
            "identity_not_written",
            f"The admin identity could not be written to the config ({e}); "
            "nothing was created.",
            500,
        ) from e

    # Mirror AFTER the identity exists, and unconditionally. `create_local_user`
    # asks `is_local_admin_account` first, which reads the config - and at that
    # moment the config still says the owner is called "admin", so an account
    # named anything else fails the test and the terminal door never gets its
    # hash. Found live: `vaf setup` created the account, then the door still
    # believed no account existed. There is no ambiguity to resolve here - the
    # FIRST admin is the machine owner by definition.
    try:
        from vaf.cli.gate import mirror_admin_password_hash
        mirror_admin_password_hash(user.password_hash)
    except Exception as e:
        logger.warning("The terminal door did not receive the password hash: %s", e)
    try:
        account_seal.mark_admin_established()
    except Exception as e:
        logger.warning("The keyring did not record the first admin: %s", e)
    # The code opened exactly this; it opens nothing more.
    _retire_setup_code()

    _prepare_user_home(user.username, agent_name)
    return user


def _prepare_user_home(username: str, agent_name: Optional[str]) -> None:
    """The directories and files an account needs, best-effort.

    Both halves are needed and they are NOT the same place: `ensure_user_dir`
    makes the per-user config directory under the platform data dir, while the
    agent workspace (identity.json, soul.md) lives under ~/.vaf/users/<name>.
    Bootstrap only ever created the first one, so a web-created account never
    got the workspace its own system prompt reads - it was written later, on
    first access, with a random agent name.

    Failures here are logged and swallowed: the account exists and works, and
    every reader of these files creates them on demand.
    """
    try:
        from vaf.auth.user_config import UserConfig
        UserConfig.ensure_user_dir(username)
    except Exception as e:
        logger.warning("Could not create the user config directory: %s", e)

    try:
        from vaf.auth.user_workspace import UserWorkspace
        workspace = UserWorkspace(username)
        workspace.ensure_exists()
        wanted = (agent_name or "").strip()
        if wanted:
            identity = workspace.get_identity()
            identity["name"] = wanted
            workspace.save_identity(identity)
    except Exception as e:
        logger.warning("Could not prepare the agent workspace: %s", e)


def suggest_agent_name() -> str:
    """A name to offer when the user does not want to think of one.

    Same shape as the workspace default, generated here so a caller can SHOW it
    before the account (and therefore the workspace) exists.
    """
    import random
    return f"Nobel{random.randint(1, 9999)}{random.choice(_NAME_COLORS)}"


_NAME_COLORS = (
    "Red", "Green", "Blue", "Cyan", "Magenta", "Teal", "Navy", "Orange",
    "Lime", "Gold", "Indigo", "Violet", "SkyBlue", "Coral", "Crimson",
    "Khaki", "Lavender", "Amber", "Jade", "Onyx",
)
