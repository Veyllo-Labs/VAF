# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The seal on an account row: who may log in, and as what, is never decided by the
database alone.

Every account lives in `local_users`, in the same PostgreSQL as the memories. Whoever
can write that table could set their own password hash and `requires_2fa_setup`, log
in and enrol a fresh second factor: the 2FA flag sits in the same row it was meant to
guard. Writing the table takes the database password (a published default until it is
rotated, `vaf secure rotate-db`), and the database is reachable from every process on
this machine and from the containers on the stack's network (Gotenberg converts
documents nobody vetted). None of those can read the data keyring.

So each row carries `seal`: an HMAC-SHA256 over the columns that grant a login or
authority (SEALED_COLUMNS), under a key that lives in the data keyring
(vaf/core/data_keyring.py), never in the database. A row whose seal does not verify
grants nothing:

- Every reader that decides on a login or on authority asks `is_sealed`: the login,
  the 2FA steps, the token refresh, the per-request standing check and the tool
  permissions (vaf/auth/permissions.py). A broken seal is refused and recorded as the
  security event `account_seal_broken`.
- Every ORM write re-seals the row on its own (`install`, a mapper hook), so the
  thirteen places that write an account need no seal call each. The hook checks the
  OLD seal first: a tampered row is frozen, and a harmless write (a login time, an
  admin's edit) can never launder it. The only ways to re-establish one are the
  trusted rewrites: the one-time adoption below and the terminal recovery.
- Rows that existed before the seal are sealed once, at the first start that has it
  (`adopt_once`, trust on first use). Whether that happened is recorded in the data
  keyring, not the database, so nulling the seals later does not re-open it.

The keyring also records that this installation has had an admin
(`mark_admin_established`). The first-run setup asks that as well as the table, so
deleting or demoting every admin row does not hand the web setup to the next caller.

NAMED BOUNDARY: whoever can read the data keyring (the VAF system user, root) can
also seal; that is the same trust boundary as the JWT secret and the memory key.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Iterator, Optional

logger = logging.getLogger(__name__)

SEALED_COLUMNS = ("id", "username", "password_hash", "role", "totp_secret", "totp_nonce",
                  "requires_2fa_setup", "is_active", "user_scope_id", "permissions")
_VERSION = "v1"
_KEY_NAME = "account_seal_key"
_ADOPTED = "account_seal_adopted"
_ESTABLISHED = "admin_established"

_key_cache: Optional[bytes] = None
_enforced_cache = False
_trusted: ContextVar[bool] = ContextVar("account_seal_trusted", default=False)


class AccountSealBroken(Exception):
    """A write to an account row whose seal does not verify."""


REFUSAL_LOGIN = ("This account cannot sign in: its record was changed outside VAF. An admin "
                 "finds the event in the security log; a terminal on this machine can reset it "
                 "(vaf users reset-password).")
REFUSAL_EDIT = ("This account's record was changed outside VAF, so it cannot be edited here. "
                "Delete it, or reset it in a terminal on this machine (vaf users reset-password).")


# ── the seal itself ────────────────────────────────────────────────────────────

def _key() -> bytes:
    global _key_cache
    if _key_cache is None:
        from vaf.core.data_keyring import get_data_key
        _key_cache = get_data_key(_KEY_NAME)
    return _key_cache


def _norm(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "b64:" + base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def compute(values: Dict[str, Any]) -> str:
    """The seal over these column values."""
    canon = json.dumps([_norm(values.get(c)) for c in SEALED_COLUMNS], sort_keys=True,
                       separators=(",", ":"), ensure_ascii=True, default=str)
    digest = hmac.new(_key(), (_VERSION + "|" + canon).encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{_VERSION}:{digest}"


def values_of(user: Any) -> Dict[str, Any]:
    return {c: getattr(user, c, None) for c in SEALED_COLUMNS}


def enforced() -> bool:
    """True once this installation's rows were adopted: from then on a missing seal is a
    broken one. Read from the keyring; once true it stays true for the process."""
    global _enforced_cache
    if not _enforced_cache:
        try:
            from vaf.core.data_keyring import peek_data_secret
            _enforced_cache = peek_data_secret(_ADOPTED) == "1"
        except Exception:
            # An unreadable keyring cannot say "not yet": enforced for THIS call (the key read
            # fails closed on its own), but not remembered, so a keyring that comes back is
            # read again instead of a passing failure deciding for the whole process.
            return True
    return _enforced_cache


def _matches(seal: Optional[str], values: Dict[str, Any]) -> bool:
    return bool(seal) and hmac.compare_digest(str(seal), compute(values))


def is_sealed(user: Any) -> bool:
    """Whether this row may decide a login or an authority. Before adoption every row may."""
    if not enforced():
        return True
    try:
        return _matches(getattr(user, "seal", None), values_of(user))
    except Exception as e:
        logger.error("Account seal could not be checked: %s", e)
        return False


def check(user: Any, *, where: str, ip: str = "") -> bool:
    """`is_sealed`, and a broken seal recorded in the security log."""
    if is_sealed(user):
        return True
    try:
        from vaf.core.security_events import log_security_event
        log_security_event("account_seal_broken", ip=ip, username=str(getattr(user, "username", "") or ""),
                           path=where, detail="the account row was changed outside VAF")
    except Exception:
        pass
    return False


# ── writes ─────────────────────────────────────────────────────────────────────

@contextmanager
def trusted_rewrite() -> Iterator[None]:
    """Re-seal rows whatever their old seal says. Only the one-time adoption and the
    terminal recovery (behind the terminal door) use it."""
    token = _trusted.set(True)
    try:
        yield
    finally:
        _trusted.reset(token)


def _materialize_defaults(target: Any) -> None:
    """Column defaults are applied by the INSERT itself, after this hook: set them here so
    the seal covers the values that are actually stored."""
    table = type(target).__table__
    for name in SEALED_COLUMNS:
        if getattr(target, name, None) is not None:
            continue
        default = table.c[name].default
        if default is None:
            continue
        if default.is_scalar:
            setattr(target, name, default.arg)
        elif default.is_callable:
            try:
                value = default.arg(None)
            except TypeError:
                value = default.arg()
            setattr(target, name, value)


def _old(state: Any, name: str) -> Any:
    hist = state.attrs[name].history
    if hist.deleted:
        return hist.deleted[0]
    if hist.unchanged:
        return hist.unchanged[0]
    return getattr(state.object, name, None)


def _before_insert(mapper, connection, target) -> None:
    _materialize_defaults(target)
    target.seal = compute(values_of(target))


def _before_update(mapper, connection, target) -> None:
    from sqlalchemy import inspect
    state = inspect(target)
    if enforced() and not _trusted.get():
        old_values = {c: _old(state, c) for c in SEALED_COLUMNS}
        if not _matches(_old(state, "seal"), old_values):
            raise AccountSealBroken(
                f"the account {getattr(target, 'username', '')!r} was changed outside VAF; it "
                "stays as it is until it is deleted or reset in a terminal on this machine "
                "(vaf users reset-password)")
    target.seal = compute(values_of(target))


def install(model: Any) -> None:
    """Seal every ORM write of `model` (LocalUser). Idempotent."""
    from sqlalchemy import event
    if not event.contains(model, "before_insert", _before_insert):
        event.listen(model, "before_insert", _before_insert)
    if not event.contains(model, "before_update", _before_update):
        event.listen(model, "before_update", _before_update)


# ── the installation's own record ──────────────────────────────────────────────

def mark_admin_established() -> None:
    from vaf.core.data_keyring import set_data_secret
    set_data_secret(_ESTABLISHED, "1")


def admin_established() -> bool:
    """Whether this installation has had an admin, by the keyring (fail closed: an
    unreadable keyring counts as yes, so the web setup stays shut)."""
    try:
        from vaf.core.data_keyring import peek_data_secret
        return peek_data_secret(_ESTABLISHED) == "1" or bool(peek_data_secret("admin_password_hash"))
    except Exception:
        return True


async def adopt_once() -> int:
    """Seal every existing row once, the first time this installation starts with the seal,
    and record it in the keyring. Returns how many rows were sealed (0 after the first time)."""
    global _enforced_cache
    # The marker itself, not enforced(): a keyring that cannot be read must not count as
    # "already adopted". The error propagates (init_auth_db logs it, the next start retries)
    # and nothing is enforced in this process on the strength of it.
    from vaf.core.data_keyring import peek_data_secret
    if _enforced_cache or peek_data_secret(_ADOPTED) == "1":
        _enforced_cache = True
        return 0
    from sqlalchemy import select

    from vaf.auth.database import get_auth_db
    from vaf.auth.models import LocalUser
    from vaf.core.config import is_admin_identity
    from vaf.core.data_keyring import set_data_secret

    count = 0
    has_admin = False
    with trusted_rewrite():
        async with get_auth_db() as db:
            for user in (await db.execute(select(LocalUser))).scalars().all():
                user.seal = compute(values_of(user))
                count += 1
                has_admin = has_admin or is_admin_identity(user.role, str(user.user_scope_id or ""))
            await db.commit()
    if has_admin:
        mark_admin_established()
    set_data_secret(_ADOPTED, "1")
    _enforced_cache = True
    return count


def reset_caches() -> None:
    """Testing hook."""
    global _key_cache, _enforced_cache
    _key_cache = None
    _enforced_cache = False
