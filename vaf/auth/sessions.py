# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The sessions of the web sign-in: the one owner of `user_sessions`.

WHY. A token stayed valid for its whole life whatever happened: logging out deleted the
cookie and nothing else, and a password change, an admin's reset or a deactivation ended no
session either (measured: after a logout and after a password change, the old token still
answered 200). Nothing in vaf/ ever set `UserSession.is_active` to false, and the middleware
never read the table; only `/me` did. So a token copied from a shared or stolen device kept
working for its 24 hours (30 days for one minted on the machine itself), and changing a
compromised password did not throw the intruder out.

WHAT. Every completed sign-in opens one row here (`open_session`, the one writer; the three
routes each built it by hand). Every reader of a token asks `session_standing_async`: a token
whose row is missing, inactive or expired has `ended`. Ending writes `user_sessions` only
(a Core UPDATE, never `local_users`, so the account seal is untouched):
- `end_session`: the presented token (logout);
- `end_account_sessions`: every session of an account, or every other one (a password
  change keeps the session that made it).

The standing is cached per token for the permission lookups' few seconds and cleared by every
ending in this process, so an ending here takes effect on the next request.

NAMED BOUNDARIES:
- A store that cannot be asked keeps the token (`unknown`), the same desktop default as the
  account standing (vaf/auth/permissions.py): a stopped database stack means nobody can sign
  in either. Failing closed instead would sign every device out on each database restart,
  and the refused cookie is deleted (vaf/auth/middleware.py), so that sign-out would stick.
  What this process ended stays ended regardless: the hashes of the sessions it ended are
  remembered (`_ended`, bounded) and answered `ended` without asking the store, so a token
  signed out here cannot ride on a store that went away afterwards.
- Another process (the terminal's `vaf users reset-password`) ends the rows in the database
  and then asks the running service, through the loopback lane the CLI uses
  (`POST /api/users/<id>/sign-out`), to forget its cache and close the account's sockets: a
  socket is checked only at its handshake. When the service does not answer, the command
  says so and names a restart.
- The rows are not sealed: whoever can write the database can reactivate an ended session of
  a token they hold.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

LIVE = "live"
ENDED = "ended"
UNKNOWN = "unknown"

_cache: dict = {}        # token hash -> (monotonic ts, standing)
_cache_lock = threading.Lock()
# Hashes of the sessions this process ended, newest last. A session never comes back (a
# reactivation revives none), so the answer stays right; bounded, oldest dropped first: a
# token lives at most 30 days, and the store answers for anything dropped.
_ended: dict = {}
_ENDED_MAX = 20_000


def _remember_ended(hashes) -> None:
    with _cache_lock:
        for h in hashes:
            if h:
                _ended.pop(h, None)
                _ended[h] = True
        while len(_ended) > _ENDED_MAX:
            _ended.pop(next(iter(_ended)))


def forget_ended_sessions() -> None:
    """Testing hook."""
    with _cache_lock:
        _ended.clear()


def _ttl() -> float:
    from vaf.auth.permissions import _TTL_SECONDS
    return _TTL_SECONDS


def _utc_naive() -> datetime:
    """The database stores naive UTC (TIMESTAMP WITHOUT TIME ZONE)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def forget_cached_sessions() -> None:
    with _cache_lock:
        _cache.clear()


async def open_session(db, user, access_token: str, *, ip: str = "", user_agent: str = "") -> None:
    """The row of one completed sign-in, for exactly this token. Commits."""
    from vaf.auth.crypto import decode_token, token_hash_for_db
    from vaf.auth.models import UserSession

    payload = decode_token(access_token) or {}
    exp = payload.get("exp")
    expires_at = (datetime.fromtimestamp(exp, tz=timezone.utc) if isinstance(exp, (int, float))
                  else datetime.now(timezone.utc) + timedelta(hours=24)).replace(tzinfo=None)
    db.add(UserSession(
        user_id=user.id,
        token_hash=token_hash_for_db(access_token),
        device_info={"ip": ip or None, "user_agent": user_agent or "", "device_type": "web"},
        expires_at=expires_at,
        is_2fa_verified=bool(payload.get("is_2fa_verified")),
    ))
    await db.commit()


async def session_standing_async(token: str) -> str:
    """`live`, `ended` (no row, ended, or expired) or `unknown` (the store cannot be asked).
    Never raises."""
    from vaf.auth.crypto import token_hash_for_db

    key = token_hash_for_db(str(token or ""))
    now = time.monotonic()
    with _cache_lock:
        if key in _ended:
            return ENDED
        hit = _cache.get(key)
        if hit and now - hit[0] < _ttl():
            return hit[1]
    try:
        from sqlalchemy import select

        from vaf.auth.database import get_auth_db
        from vaf.auth.models import UserSession
        async with get_auth_db() as db:
            row = (await db.execute(
                select(UserSession.is_active, UserSession.expires_at)
                .where(UserSession.token_hash == key))).first()
        if row is None or not row[0] or (row[1] is not None and row[1] <= _utc_naive()):
            answer = ENDED
        else:
            answer = LIVE
    except Exception:
        return UNKNOWN
    with _cache_lock:
        _cache[key] = (now, answer)
        if len(_cache) > 1024:
            cutoff = now - _ttl()
            for k in [k for k, v in _cache.items() if v[0] < cutoff]:
                _cache.pop(k, None)
    return answer


async def end_session(db, token: str) -> int:
    """End the session of this token. Returns how many rows ended (0 or 1). Commits."""
    from sqlalchemy import update

    from vaf.auth.crypto import token_hash_for_db
    from vaf.auth.models import UserSession

    key = token_hash_for_db(str(token or ""))
    result = await db.execute(
        update(UserSession)
        .where(UserSession.token_hash == key,
               UserSession.is_active == True)  # noqa: E712 - SQLAlchemy column comparison
        .values(is_active=False)
        .execution_options(synchronize_session=False))
    await db.commit()
    _remember_ended([key])
    forget_cached_sessions()
    return int(result.rowcount or 0)


async def end_account_sessions(db, user_id, *, keep_token: Optional[str] = None) -> int:
    """End every session of an account, except the one of `keep_token`. Returns how many
    ended. A reactivated account revives none of them. Commits."""
    from sqlalchemy import update

    from vaf.auth.crypto import token_hash_for_db
    from vaf.auth.models import UserSession

    stmt = (update(UserSession)
            .where(UserSession.user_id == user_id,
                   UserSession.is_active == True))  # noqa: E712 - SQLAlchemy column comparison
    if keep_token:
        stmt = stmt.where(UserSession.token_hash != token_hash_for_db(keep_token))
    result = await db.execute(stmt.values(is_active=False)
                              .returning(UserSession.token_hash)
                              .execution_options(synchronize_session=False))
    ended = [h for h in result.scalars().all()]
    await db.commit()
    _remember_ended(ended)
    forget_cached_sessions()
    return len(ended)


async def remember_account_endings(db, user_id) -> None:
    """Note every ended, unexpired session of an account as ended in THIS process: for an
    ending another process wrote (the terminal's reset), so the service holds it even when
    the store cannot be asked later."""
    from sqlalchemy import select

    from vaf.auth.models import UserSession
    rows = (await db.execute(
        select(UserSession.token_hash)
        .where(UserSession.user_id == user_id,
               UserSession.is_active == False,  # noqa: E712 - SQLAlchemy column comparison
               UserSession.expires_at > _utc_naive()))).scalars().all()
    _remember_ended(rows)
    forget_cached_sessions()


async def signed_in_from(db, username: str, ip: str, *, days: int = 30) -> bool:
    """Whether `ip` completed a sign-in for this account name within `days` (the account
    window's exemption, vaf/auth/rate_limit.py). One query keyed on the name, run the same way
    for a name that does not exist. A row is written only after the second factor, so a
    password alone never makes an address known."""
    from sqlalchemy import func, select

    from vaf.auth.models import LocalUser, UserSession
    from vaf.auth.rate_limit import account_key

    if not ip:
        return False
    rows = (await db.execute(
        select(UserSession.device_info)
        .join(LocalUser, LocalUser.id == UserSession.user_id)
        .where(func.lower(LocalUser.username) == account_key(username),
               UserSession.created_at >= _utc_naive() - timedelta(days=days))
    )).all()
    return any(isinstance(r[0], dict) and r[0].get("ip") == ip for r in rows)
