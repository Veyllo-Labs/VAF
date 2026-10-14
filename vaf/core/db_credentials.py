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


def shipped_password_in(dsn: str) -> bool:
    return any(p in str(dsn or "") for p in SHIPPED_PASSWORDS)


def shipped_password_in_use() -> bool:
    """Either role still on its published password. Both DSNs: the owner role is the stronger
    one, and checking only the app DSN read "all clear" while the superuser still had it."""
    from vaf.core.config import Config
    return any(shipped_password_in(str(Config.get(k, "") or "")) for k in _DSN_KEYS)


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
        raise RuntimeError(
            f"The password of the database role {role!r} was changed, but a connection with "
            f"the new one did not verify ({type(e).__name__}: {e}). Nothing was saved to the "
            f"config. The new connection string is: {rotated}") from e
    finally:
        await verify.dispose()
    return rotated


def rotate_shipped_passwords() -> List[str]:
    """Replace every role password that is still the shipped one, and persist each verified
    DSN. Returns the config keys that were rotated. Raises on the first failure; what was
    rotated before it stays rotated and persisted (each DSN is verified first)."""
    import asyncio

    from vaf.core.config import Config
    done: List[str] = []
    for key in _DSN_KEYS:
        dsn = str(Config.get(key, "") or "")
        if not dsn or not shipped_password_in(dsn):
            continue
        Config.set(key, asyncio.run(rotate_role_password(dsn)))
        done.append(key)
    return done
