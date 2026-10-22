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
routes each built it by hand), and so does every token for programs (`mint_program_token`).
Every reader of a token asks `session_standing_async`: a token whose row is missing,
inactive, expired or idle too long has `ended`. One rule decides that, `session_row_live`,
asked by the standing check, the session list and `/me`. Ending writes `user_sessions` only
(a Core UPDATE, never `local_users`, so the account seal is untouched):
- `end_session`: the presented token (logout);
- `end_account_session`: one session of an account, by its id (Settings, Account);
- `end_account_sessions`: every session of an account, or every other one (a password
  change and "sign out all other devices" keep the session that made it).

LIFETIMES. A row is one of three kinds (`device_info.device_type` and its address):
- a browser on another device (`web`, an address that is not this machine): it lives
  `local_network_jwt_expiry_hours` (30 days) at most and ends after
  `local_network_session_idle_days` (7) without use. Only a session that can outlive a day
  makes an inactivity limit mean something; both limits are the token's and the row's.
- the window on this machine (`web` from loopback): 30 days, no inactivity limit; the
  person is at their own desk. That reason holds only where the token is USED, so such a
  session opens nothing for a client that is not this machine (`session_standing_async`
  with `from_this_machine=False` answers `ended`): a copied desktop cookie does not carry
  the exemption to another device, fresh or idle. The token itself says it is such a
  session (`crypto.THIS_MACHINE_CLAIM`, set when the sign-in completes on this machine), so
  the refusal needs no store and holds while the store cannot be asked too. A token minted
  before the claim existed is judged by its row only.
- a token for programs (`program`): 7, 30, 90 or 365 days, chosen when it is made, and no
  inactivity limit: a backup script that runs monthly must not die of disuse.
This departs from NIST 800-63B-4 at AAL2 (24 hours overall and one hour of inactivity, both
SHOULD) for a server that only the LAN and VPNs reach, with the second factor always on, the
token in an HttpOnly cookie a page script cannot read, every session listed and endable, and
a sign-in from a new address announced (ASVS 7.1.1 asks for the reason to be written down).

LAST USE. A standing check that asks the store (a cache miss) writes `last_activity = now`
when the row is live and its last use is older than TOUCH_EVERY_S, at most once an hour per
session; a failed write never changes the answer, and an ended row is never written, so a
session that idled out cannot be revived by a retry.

THE LIST AND TOKENS FOR PROGRAMS. `list_account_sessions` answers an account's live sessions
for Settings, Account and `vaf users sessions`, never a token hash. `mint_program_token`
makes a token for a script of the account's own after the caller proved it (the 2FA code in
the web UI, the terminal's proof in `vaf users token`): the token is shown once, acts as
the account, and is listed and revoked like a device.

The standing is cached per token for the permission lookups' few seconds and cleared by every
ending in this process, so an ending here takes effect on the next request.

NAMED BOUNDARIES:
- A store that cannot be asked keeps the token (`unknown`), the same desktop default as the
  account standing (vaf/auth/permissions.py): a stopped database stack means nobody can sign
  in either. A session of this machine presented by another client is refused all the same
  (its claim); one minted before the claim existed is kept then, like any other token, until
  it expires. Failing closed instead would sign every device out on each database restart,
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
- A socket does not count as use: a tab that only chats over its open socket is not touched
  and keeps working until it reconnects; its reconnect is a standing check like any other.
- An admin's open tab counts as use: it polls `/api/security/alert-count` every minute.
- Devices behind one address (a router's VPN, a household NAT) share one "known address"
  for the new-sign-in alert and the account window's exemption (`signed_in_from`).
- A token for programs carries the account's full rights; tokens limited to some routes are
  not built.
- Accounts, sign-in and sessions are not on the framework facade: an embedder authenticates
  its own users and asserts `user_scope` (docs/EMBEDDING.md), so nothing an embedder needs
  is missing here.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

LIVE = "live"
ENDED = "ended"
UNKNOWN = "unknown"

# `device_info.device_type`: a sign-in (`web`, a browser or the window on this machine) or a
# token for programs.
WEB = "web"
PROGRAM = "program"
# How a session is shown and limited (`list_account_sessions`).
KIND_BROWSER = "browser"
KIND_PROGRAM = "program"
KIND_THIS_MACHINE = "this_machine"

# Last use is written at most this often per session (seconds).
TOUCH_EVERY_S = 3600
# The lifetimes a token for programs may be given, in days.
PROGRAM_TOKEN_DAYS = (7, 30, 90, 365)
PROGRAM_NAME_MAX = 60
_IDLE_DAYS_DEFAULT = 7

_cache: dict = {}        # token hash -> (monotonic ts, standing, bound to this machine)
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


def idle_days() -> int:
    """`local_network_session_idle_days`, read on every call and held to 1..365."""
    try:
        from vaf.core.config import Config
        days = int(Config.get("local_network_session_idle_days", _IDLE_DAYS_DEFAULT))
    except Exception:
        days = _IDLE_DAYS_DEFAULT
    return max(1, min(365, days))


def _from_this_machine(ip) -> bool:
    if not ip:
        return False
    from vaf.network.binding import is_localhost
    return is_localhost(str(ip))


def _info(device_info) -> dict:
    return device_info if isinstance(device_info, dict) else {}


def _idle_limited(device_info) -> bool:
    """A sign-in from another device: the only kind that ends after a time without use."""
    info = _info(device_info)
    return info.get("device_type") != PROGRAM and not _from_this_machine(info.get("ip"))


def session_row_live(is_active, expires_at, last_activity, device_info, now: datetime) -> bool:
    """The one rule for whether a session row still opens anything: active, not expired, and,
    for a sign-in from another device, used within the inactivity limit."""
    if not is_active:
        return False
    if expires_at is not None and expires_at <= now:
        return False
    if _idle_limited(device_info):
        if last_activity is None or now - last_activity > timedelta(days=idle_days()):
            return False
    return True


async def open_session(db, user, access_token: str, *, ip: str = "", user_agent: str = "",
                       kind: str = WEB, name: str = "") -> str:
    """The row of one completed sign-in (or one token for programs, `kind=PROGRAM`), for
    exactly this token. Returns the row's id. Commits."""
    from vaf.auth.crypto import decode_token, token_hash_for_db
    from vaf.auth.models import UserSession

    payload = decode_token(access_token) or {}
    exp = payload.get("exp")
    now = _utc_naive()
    expires_at = (datetime.fromtimestamp(exp, tz=timezone.utc).replace(tzinfo=None)
                  if isinstance(exp, (int, float)) else now + timedelta(hours=24))
    row_id = uuid.uuid4()
    db.add(UserSession(
        id=row_id,
        user_id=user.id,
        token_hash=token_hash_for_db(access_token),
        device_info={"ip": ip or None, "user_agent": user_agent or "", "device_type": kind,
                     "name": name or ""},
        created_at=now,
        last_activity=now,
        expires_at=expires_at,
        is_2fa_verified=bool(payload.get("is_2fa_verified")),
    ))
    await db.commit()
    return str(row_id)


def _refused_elsewhere() -> str:
    logger.warning("A session of this machine was presented by another address; refused")
    return ENDED


def refused_by_claim(claims, from_this_machine: Optional[bool]) -> bool:
    """Whether a token's verified claims say it is a session of the window on this machine
    (`crypto.THIS_MACHINE_CLAIM`) while another client presents it. Decided from the token
    alone, with no I/O, so a reader refuses it before it asks any store (the account lookup
    included); logs the refusal. `session_standing_async` asks the same."""
    from vaf.auth.crypto import THIS_MACHINE_CLAIM
    if (from_this_machine is False and isinstance(claims, dict)
            and claims.get(THIS_MACHINE_CLAIM) is True):
        _refused_elsewhere()
        return True
    return False


def _for_client(answer: str, machine_bound: bool, from_this_machine: Optional[bool]) -> str:
    """A live session of the window on this machine opens nothing for another client."""
    if answer == LIVE and machine_bound and from_this_machine is False:
        return _refused_elsewhere()
    return answer


async def session_standing_async(token: str, *, from_this_machine: Optional[bool] = None,
                                 claims: Optional[dict] = None) -> str:
    """`live`, `ended` (no row, ended, expired, idle too long, or a session of this machine
    presented by another client: `from_this_machine=False`) or `unknown` (the store cannot be
    asked). `from_this_machine=None` (the caller does not know the client) skips the address
    rule. `claims` is the token's verified payload: one that says it is a session of this
    machine is refused for another client before the store is asked. Never raises."""
    from vaf.auth.crypto import token_hash_for_db

    if refused_by_claim(claims, from_this_machine):
        return ENDED
    key = token_hash_for_db(str(token or ""))
    now = time.monotonic()
    with _cache_lock:
        if key in _ended:
            return ENDED
        hit = _cache.get(key)
        if hit and now - hit[0] < _ttl():
            return _for_client(hit[1], hit[2], from_this_machine)
    try:
        from sqlalchemy import select

        from vaf.auth.database import get_auth_db
        from vaf.auth.models import UserSession
        async with get_auth_db() as db:
            row = (await db.execute(
                select(UserSession.is_active, UserSession.expires_at, UserSession.last_activity,
                       UserSession.device_info)
                .where(UserSession.token_hash == key))).first()
            current = _utc_naive()
            live = row is not None and session_row_live(row[0], row[1], row[2], row[3], current)
            answer = LIVE if live else ENDED
            machine_bound = row is not None and session_kind(row[3]) == KIND_THIS_MACHINE
            if live and not (machine_bound and from_this_machine is False):
                await _touch(db, key, row[2], current)
    except Exception:
        return UNKNOWN
    with _cache_lock:
        _cache[key] = (now, answer, machine_bound)
        if len(_cache) > 1024:
            cutoff = now - _ttl()
            for k in [k for k, v in _cache.items() if v[0] < cutoff]:
                _cache.pop(k, None)
    return _for_client(answer, machine_bound, from_this_machine)


async def _touch(db, key: str, last_activity, now: datetime) -> None:
    """Last use, at most once per TOUCH_EVERY_S. Only a live row reaches here, and the write
    keeps to an active, unexpired row, so an ended session is never revived. Never raises."""
    cutoff = now - timedelta(seconds=TOUCH_EVERY_S)
    if last_activity is not None and last_activity >= cutoff:
        return
    try:
        from sqlalchemy import update

        from vaf.auth.models import UserSession
        await db.execute(
            update(UserSession)
            .where(UserSession.token_hash == key,
                   UserSession.is_active == True,  # noqa: E712 - SQLAlchemy column comparison
                   UserSession.expires_at > now,
                   UserSession.last_activity < cutoff)
            .values(last_activity=now)
            .execution_options(synchronize_session=False))
        await db.commit()
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass


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


async def end_account_session(db, user_id, session_id) -> Optional[str]:
    """End one session of an account, by its row id: only when the row is that account's.
    Returns the ended session's token hash (to close the sockets it opened), or None when no
    such live row of this account exists. Commits."""
    from sqlalchemy import update

    from vaf.auth.models import UserSession
    try:
        wanted = session_id if isinstance(session_id, uuid.UUID) else uuid.UUID(str(session_id))
    except (ValueError, TypeError):
        return None
    result = await db.execute(
        update(UserSession)
        .where(UserSession.id == wanted,
               UserSession.user_id == user_id,
               UserSession.is_active == True)  # noqa: E712 - SQLAlchemy column comparison
        .values(is_active=False)
        .returning(UserSession.token_hash)
        .execution_options(synchronize_session=False))
    ended = [h for h in result.scalars().all()]
    await db.commit()
    _remember_ended(ended)
    forget_cached_sessions()
    return ended[0] if ended else None


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


# ── the list, and tokens for programs ─────────────────────────────────────────────

def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.replace(tzinfo=timezone.utc).isoformat() if value is not None else None


def session_kind(device_info) -> str:
    """How a session is shown: a browser on another device, the window on this machine, or a
    token for programs."""
    info = _info(device_info)
    if info.get("device_type") == PROGRAM:
        return KIND_PROGRAM
    return KIND_THIS_MACHINE if _from_this_machine(info.get("ip")) else KIND_BROWSER


async def list_account_sessions(db, user_id, *, current_token: Optional[str] = None) -> list:
    """The live sessions of one account, most recent use first. `current` marks the session
    of `current_token`. Never a token hash."""
    from sqlalchemy import select

    from vaf.auth.crypto import token_hash_for_db
    from vaf.auth.models import UserSession

    now = _utc_naive()
    current = token_hash_for_db(current_token) if current_token else None
    rows = (await db.execute(
        select(UserSession)
        .where(UserSession.user_id == user_id,
               UserSession.is_active == True))).scalars().all()  # noqa: E712
    out = []
    for row in rows:
        if not session_row_live(row.is_active, row.expires_at, row.last_activity,
                                row.device_info, now):
            continue
        info = _info(row.device_info)
        limited = _idle_limited(info)
        browser, system = device_parts(str(info.get("user_agent") or ""))
        out.append({
            "id": str(row.id),
            "kind": session_kind(info),
            "name": str(info.get("name") or ""),
            "device": device_label(str(info.get("user_agent") or "")),
            "browser": browser,
            "os": system,
            "ip": info.get("ip") or "",
            "created_at": _iso(row.created_at),
            "last_used_at": _iso(row.last_activity),
            "expires_at": _iso(row.expires_at),
            "idle_until": (_iso(row.last_activity + timedelta(days=idle_days()))
                           if limited and row.last_activity is not None else None),
            "current": bool(current) and row.token_hash == current,
            "_sort": row.last_activity or row.created_at or now,
        })
    out.sort(key=lambda s: s["_sort"], reverse=True)
    for entry in out:
        entry.pop("_sort")
    return out


class ProgramTokenRefused(ValueError):
    """A token for programs was not made. `code`: `invalid` (a name or a lifetime the rule
    does not take) or `record_changed` (the account's seal does not verify)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def program_token_name(name) -> str:
    """The name a token for programs is listed by: stripped, 1 to PROGRAM_NAME_MAX characters,
    no control characters. Raises ProgramTokenRefused('invalid')."""
    import unicodedata
    cleaned = str(name or "").strip()
    if (not cleaned or len(cleaned) > PROGRAM_NAME_MAX
            or any(unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") for ch in cleaned)):
        raise ProgramTokenRefused(
            "invalid", f"A name of 1 to {PROGRAM_NAME_MAX} characters, on one line.")
    return cleaned


def program_token_days(days) -> int:
    """A lifetime a token for programs may have (PROGRAM_TOKEN_DAYS). Raises
    ProgramTokenRefused('invalid')."""
    if isinstance(days, bool) or days not in PROGRAM_TOKEN_DAYS:
        raise ProgramTokenRefused(
            "invalid", "The lifetime is one of " + ", ".join(map(str, PROGRAM_TOKEN_DAYS)) + " days.")
    return int(days)


async def mint_program_token(db, user, *, name: str, days: int, ip: str = "",
                             user_agent: str = "", where: str = "") -> Tuple[str, str]:
    """A token for a program of the account's own, after the caller proved it is the account
    (the 2FA code in the web UI, the terminal's proof for `vaf users token`). It acts as the
    account, lives `days` (one of PROGRAM_TOKEN_DAYS) with no inactivity limit, and is listed
    and revoked like a device. Returns (token, session id); the token is never logged. The
    security event `program_token_created` names the account, the name, the days and `where`.
    Raises ProgramTokenRefused."""
    from vaf.auth import account_seal
    from vaf.auth.crypto import create_access_token

    cleaned = program_token_name(name)
    days = program_token_days(days)
    if not account_seal.check(user, where="token for programs", ip=ip):
        raise ProgramTokenRefused("record_changed", account_seal.REFUSAL_LOGIN)
    token = create_access_token(str(user.id), user.username, user.role, str(user.user_scope_id),
                                expires_hours=days * 24, is_2fa_verified=True)
    session_id = await open_session(db, user, token, ip=ip, user_agent=user_agent,
                                    kind=PROGRAM, name=cleaned)
    try:
        from vaf.core.security_events import log_security_event
        log_security_event("program_token_created", ip=ip, username=str(user.username or ""),
                           detail=f"{cleaned}, {days} days" + (f", {where}" if where else ""))
    except Exception:
        pass
    return token, session_id


# ── a person's word for a user agent ──────────────────────────────────────────────

# Order matters: Edge and every Chromium browser carry "Chrome/", and Chrome carries
# "Safari/", so the more specific marks are asked first.
_BROWSERS = (
    ("Edge", ("Edg/", "EdgA/", "EdgiOS/", "Edge/")),
    ("Firefox", ("Firefox/", "FxiOS/")),
    ("Chrome", ("Chrome/", "CriOS/", "Chromium/")),
    ("Safari", ("Safari/",)),
)
_SYSTEMS = (
    ("iOS", ("iPhone", "iPad", "iPod")),
    ("Android", ("Android",)),
    ("Windows", ("Windows",)),
    ("macOS", ("Macintosh", "Mac OS X")),
    ("Linux", ("Linux", "X11", "CrOS")),
)


def device_parts(user_agent: str) -> Tuple[str, str]:
    """(browser, system) a user agent names, each "" when unknown; a client that is not a
    browser is named by its product ("curl"). The pages word the pair in the reader's
    language. Pure."""
    ua = str(user_agent or "").strip()
    if not ua:
        return "", ""
    browser = next((label for label, marks in _BROWSERS if any(m in ua for m in marks)), "")
    system = next((label for label, marks in _SYSTEMS if any(m in ua for m in marks)), "")
    if not browser:
        if not ua.startswith("Mozilla/"):
            return ua.split("/", 1)[0].split(" ", 1)[0][:40], ""
        browser = "Browser"
    return browser, system


def device_label(user_agent: str) -> str:
    """"Firefox on Linux", "Safari on iOS": what a person recognises their device by. A client
    that is not a browser shows its product name ("curl"); no agent at all is "Unknown
    device". Pure."""
    browser, system = device_parts(user_agent)
    if not browser:
        return "Unknown device"
    return f"{browser} on {system}" if system else browser
