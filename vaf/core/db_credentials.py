# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The memory database's passwords: whether the shipped ones are still in use, and replacing
them.

A default install connects with two roles, the app role (`memory_db_url`) and the owner role
for DDL (`memory_db_owner_url`), and both start with passwords published in this repository.
Whoever has one can write every table, the accounts included: the account seal
(vaf/auth/account_seal.py) keeps such a write from granting a login, and replacing the
passwords keeps it from happening at all. The check lived three times (`vaf secure status`,
the rotation, the stack start's warning); it lives here once, beside the rotation that
`vaf secure rotate-db` and `vaf server provision` share.
"""
from __future__ import annotations

import secrets
from typing import List

SHIPPED_PASSWORDS = ("vaf_dev_secret", "vaf_app_dev_secret")
_DSN_KEYS = ("memory_db_url", "memory_db_owner_url")


class RotationUnsaved(RuntimeError):
    """The role's password WAS changed, but the new connection string was not saved: it did
    not verify, or the config refused it. `rotated` is the only copy of the new password, and
    the message ends with it."""

    def __init__(self, message: str, rotated: str):
        super().__init__(message)
        self.rotated = rotated


class RotationIncomplete(RuntimeError):
    """`rotate_shipped_passwords` stopped at `key`: `done` names the config keys already
    rotated and saved, `cause` says why. A RotationUnsaved cause means the role behind `key`
    no longer has the published password either."""

    def __init__(self, done: List[str], key: str, cause: Exception):
        super().__init__(f"{key}: {type(cause).__name__}: {cause}")
        self.done = list(done)
        self.key = key
        self.cause = cause


def shipped_password_in(dsn: str) -> bool:
    return any(p in str(dsn or "") for p in SHIPPED_PASSWORDS)


def keys_on_shipped_password() -> List[str]:
    """The config keys whose DSN still carries a published password."""
    from vaf.core.config import Config
    return [k for k in _DSN_KEYS if shipped_password_in(str(Config.get(k, "") or ""))]


def shipped_password_in_use() -> bool:
    """Either role still on its published password. Both DSNs: the owner role is the stronger
    one, and checking only the app DSN read "all clear" while the superuser still had it."""
    return bool(keys_on_shipped_password())


async def rotate_role_password(source_dsn: str) -> str:
    """Give the role named in this DSN a fresh random password; return the DSN with it.

    Verified by a new connection BEFORE it is returned, so a caller never persists a password
    that does not work. Each role gets its own password: the two DSNs name roles with
    different power, and one shared secret would undo the reason they were separated."""
    from urllib.parse import quote, unquote, urlsplit, urlunsplit

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from vaf.memory.database import normalize_async_dsn

    new_password = secrets.token_urlsafe(24)
    parts = urlsplit(source_dsn)
    # The role as Postgres knows it (a DSN percent-encodes its user), quoted as an identifier.
    role = unquote(parts.username or "vaf")
    ident = '"' + role.replace('"', '""') + '"'
    engine = create_async_engine(normalize_async_dsn(source_dsn), poolclass=None)
    try:
        async with engine.begin() as conn:
            # Parameter binding is not allowed in ALTER ROLE; the password is generated here
            # from token_urlsafe, so it carries no quotes.
            await conn.execute(text(f"ALTER ROLE {ident} WITH PASSWORD '{new_password}'"))
    finally:
        await engine.dispose()

    # The host and port exactly as written (an IPv6 address keeps its brackets), the role
    # encoded again; token_urlsafe needs no encoding.
    hostport = parts.netloc.rsplit("@", 1)[-1] or "localhost"
    netloc = f"{quote(role, safe='')}:{new_password}@{hostport}"
    rotated = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))

    verify = create_async_engine(normalize_async_dsn(rotated), poolclass=None)
    try:
        async with verify.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as e:
        # The role's password IS changed at this point and the old one is gone: the operator
        # needs the new one to get back in. Said here, on the operator's terminal, once.
        raise RotationUnsaved(
            f"The password of the database role {role!r} was changed, but a connection with "
            f"the new one did not verify ({type(e).__name__}: {e}). Nothing was saved to the "
            f"config. The new connection string is: {rotated}", rotated) from e
    finally:
        await verify.dispose()
    return rotated


def rotate_and_save(key: str, dsn: str) -> str:
    """Rotate the role behind `dsn` and save the verified DSN as config `key`; return it.

    The one path `vaf secure rotate-db` and `vaf server provision` share. A RotationUnsaved
    from here (the new password did not verify, or the config refused it) ends with the new
    connection string and the key to save it under; any other error means nothing changed."""
    import asyncio

    from vaf.core.config import Config
    save = f" Save it as {key} in the config, then restart VAF."
    try:
        rotated = asyncio.run(rotate_role_password(dsn))
    except RotationUnsaved as e:
        raise RotationUnsaved(f"{e}{save}", e.rotated) from e
    try:
        Config.set(key, rotated)
    except Exception as e:
        raise RotationUnsaved(
            f"The password behind {key} was changed and verified, but the config did not save "
            f"it ({type(e).__name__}: {e}). The new connection string is: {rotated}{save}",
            rotated) from e
    return rotated


def rotate_shipped_passwords() -> List[str]:
    """Replace every role password that is still the shipped one, and persist each verified
    DSN. Returns the config keys that were rotated. Stops at the first failure with
    RotationIncomplete: what was rotated before it stays rotated and persisted (each DSN is
    verified first), and the error says whether the failing role's password changed anyway."""
    from vaf.core.config import Config
    done: List[str] = []
    for key in _DSN_KEYS:
        dsn = str(Config.get(key, "") or "")
        if not dsn or not shipped_password_in(dsn):
            continue
        try:
            rotate_and_save(key, dsn)
        except Exception as e:
            raise RotationIncomplete(done, key, e) from e
        done.append(key)
    return done
