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
account: access/refresh tokens, the UserSession row, the auth cookie. A terminal
setup needs an account, not a browser session.

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

from vaf.auth.crypto import hash_password
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

    password_hash = hash_password(password)
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


async def create_first_admin(
    db,
    *,
    username: str,
    password: str,
    agent_name: Optional[str] = None,
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
