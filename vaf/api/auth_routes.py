# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Auth API: needs-setup, setup-code, bootstrap, login, 2FA setup/verify, logout, me.

Endpoints:
- GET  /api/auth/needs-setup
- POST /api/auth/setup-code
- POST /api/auth/bootstrap
- POST /api/auth/login
- POST /api/auth/setup-2fa
- POST /api/auth/verify-2fa
- POST /api/auth/logout
- GET  /api/auth/me
"""

import io
import base64
import logging
import socket
import uuid as uuid_module
from datetime import datetime, timezone
from typing import Optional

import pyotp
import qrcode
from fastapi import APIRouter, HTTPException, status, Response, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from vaf.auth.models import LocalUser, UserSession
from vaf.auth.database import get_auth_db
from vaf.auth import account_seal
from vaf.auth.sessions import end_account_sessions, end_session, open_session, signed_in_from
from vaf.auth.crypto import (
    password_fingerprint,
    verify_password_async,
    create_access_token,
    decode_token,
    token_hash_for_db,
    encrypt_totp_secret,
    decrypt_totp_secret,
    generate_totp_secret,
    get_totp_uri,
)
from vaf.core.config import Config, get_local_admin_scope_id, get_local_admin_username

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE_NAME = "vaf_token"

# The life of the token /login answers a correct password with before the second factor.
TEMP_TOKEN_HOURS = 0.25

# A second-factor code works once (vaf/auth/second_factor.py).
SPENT_CODE_TEXT = "This code was already used. Wait for the next code in your authenticator app."


def _client_ip(request: Request) -> str:
    """Who this request REALLY comes from - the device, not the socket peer.

    The integrated HTTPS proxy relays every LAN device to the backend over loopback, so
    ``request.client.host`` is 127.0.0.1 for all of them. Every decision in this module that
    means "the person is sitting at this machine" (long-lived tokens, the first-run setup
    code, the account window's exemption) and every record of "where did this come from"
    (the security log, session provenance) must use this instead, or it applies to the whole
    network.

    Delegates to the one shared resolver so this module, the auth middleware, the rate limiter,
    the WebSocket handshake and the OAuth callback exception cannot disagree.
    """
    from vaf.network.binding import connection_client_ip
    return connection_client_ip(request)


def close_account_sockets(user_scope_id: str) -> None:
    """Close every open WebSocket of an account whose sessions just ended: a socket keeps the
    identity it was opened with, and one opened with an ended token cannot reconnect. Never
    raises."""
    if not user_scope_id:
        return
    try:
        from vaf.core.web_interface import WebInterfaceManager
        WebInterfaceManager().close_account_connections(user_scope_id)
    except Exception as e:
        logger.debug("Closing the account's sockets failed: %s", e)


def close_session_sockets(token: str) -> None:
    """Close the open WebSockets that were opened with this access token (its session just
    ended). Never raises."""
    if not token:
        return
    try:
        from vaf.core.web_interface import WebInterfaceManager
        WebInterfaceManager().close_session_connections(token_hash_for_db(token))
    except Exception as e:
        logger.debug("Closing the session's sockets failed: %s", e)


def _log_auth_failure(kind: str, request: Request, username: str = "", detail: str = "") -> None:
    """Record a failed login/2FA attempt in the security event log (never the
    password/code itself). Lazy + swallow-all: auditing must never break auth."""
    try:
        from vaf.core.security_events import log_security_event
        log_security_event(kind, ip=_client_ip(request), username=username, detail=detail)
    except Exception:
        pass


def _cookie_max_age_for(token: str) -> int:
    """Cookie lifetime derived from the token's own exp claim.

    INVARIANT: the cookie must never outlive the JWT inside it. Hardcoded cookie
    lifetimes (30 days) longer than the JWT expiry (default 24h) left a
    present-but-expired cookie in the jar; the server-side route gate
    (web/proxy.ts) correctly rejected it while a still-accepted bearer token
    bounced the client back - the / <-> /login navigation loop of the
    2026-07-22 live incident. Deriving the max_age here, at the single place
    that sets the cookie, removes that desync class structurally.
    """
    try:
        payload = decode_token(token)
        exp = payload.get("exp") if payload else None
        if isinstance(exp, (int, float)):
            remaining = int(exp - datetime.now(timezone.utc).timestamp())
            if remaining > 0:
                return remaining
    except Exception:
        pass
    # Fallback (fresh self-issued tokens always decode; this is belt-and-braces):
    # mirror the configured JWT expiry rather than any longer hardcoded value.
    try:
        from vaf.core.config import Config
        return int(float(Config.get("local_network_jwt_expiry_hours", 24)) * 3600)
    except Exception:
        return 24 * 3600


def _set_auth_cookie(request: Request, response: Response, token: str) -> None:
    """Set the auth cookie with dynamic Secure flag based on current request protocol.

    The max_age always comes from the token's exp claim (see _cookie_max_age_for);
    callers cannot pass a longer lifetime.
    """
    # Check if we are on HTTPS or behind an HTTPS proxy
    is_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"

    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        max_age=_cookie_max_age_for(token),
        samesite="lax",
        secure=is_https,
        path="/",
    )


# --- Request/Response models ---

class BootstrapRequest(BaseModel):
    username: str
    password: str
    # The name the agent calls itself, asked by the wizard the same way the
    # terminal setup asks it. Optional: an empty or missing value falls back to
    # the suggestion inside create_first_admin's workspace preparation.
    agent_name: Optional[str] = None
    # The first-run setup code (`vaf setup code`), needed from a browser that is not
    # on this machine (vaf/auth/user_admin.py).
    setup_code: Optional[str] = None


class SetupCodeRequest(BaseModel):
    code: str = ""


class LoginRequest(BaseModel):
    username: str
    password: str
    remember_me: bool = False


class Verify2FARequest(BaseModel):
    code: str
    temp_token: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str
    code: str = ""


class TestVeylloKeyRequest(BaseModel):
    api_key: str
    # Needed from a browser that is not on this machine, as for /bootstrap.
    setup_code: Optional[str] = None


# --- Endpoints ---

def _is_db_not_ready_error(exc: Exception) -> bool:
    """True for states expected while the stack is still booting: PostgreSQL not accepting
    connections yet (refused / "starting up"), transient overload, or the auth tables not
    created yet (the web server's background init may still be running). These must surface
    as a retryable 503, never as a hard 500 - the login page treats any needs-setup error as
    "no setup needed", so a fresh install would render login instead of the setup wizard.
    Permanent misconfiguration (DNS typo in the DSN, rejected credentials, missing
    role/database) is deliberately NOT matched: it must fail loudly, not look like a slow
    boot the frontend then polls against forever."""
    if isinstance(exc, socket.gaierror):
        return False  # unresolvable DB host = permanent DSN misconfig
    if isinstance(exc, OSError):
        return True
    s = str(exc).lower()
    if "pg_hba" in s or "password authentication failed" in s:
        return False  # the server is up and rejecting us - permanent
    # Missing auth TABLE = background init still running. Keep this narrower than a bare
    # "does not exist", which would also match permanent 'role/database ... does not exist'.
    if "undefinedtable" in s or ('relation "' in s and "does not exist" in s):
        return True
    return any(kw in s for kw in (
        "connect", "connection", "refused", "could not connect", "is the server running",
        "starting up", "shutting down", "in recovery", "too many clients", "too many connections",
    ))


def _setup_code_required(request: Request) -> bool:
    """A browser that is not on this machine needs the first-run setup code, in every mode:
    the IP check admits the LAN, the VPN and on a VPS the provider's private network, and
    any of them would otherwise create the first admin."""
    from vaf.network.binding import is_localhost
    return not is_localhost(_client_ip(request))


@router.get("/needs-setup")
async def needs_setup(request: Request):
    """
    Returns whether the first admin account must be created.
    Callable without auth. When true, show Create Admin flow instead of login.
    While PostgreSQL is still booting or the auth tables are still being created
    (both happen in parallel to this server on startup), responds 503 so the
    frontend keeps polling instead of falling back to the login form.
    """
    import asyncio
    last_exc: Exception | None = None
    # The keyring knows whether this installation has had an admin. Deleting, demoting or
    # deactivating every admin row in the database does not make it a fresh install again.
    if account_seal.admin_established():
        return {"needs_setup": False}
    for attempt in range(3):
        try:
            async with get_auth_db() as db:
                result = await db.execute(
                    select(LocalUser).where(LocalUser.role == "admin", LocalUser.is_active == True)
                )
                has_admin = result.scalar_one_or_none() is not None
            if has_admin:
                return {"needs_setup": False}
            # The suggestion travels with the answer so the wizard's name step can
            # offer the same shape the terminal setup offers - generated HERE, not
            # duplicated in JavaScript, which is also why the dice button in the
            # wizard simply asks this endpoint again.
            from vaf.auth.user_admin import suggest_agent_name
            from vaf.core.inference_profile import current as inference_profile
            # The profile tells the wizard whether skipping the provider step leaves a
            # local model to fall back on (only while no admin exists, like the rest).
            return {"needs_setup": True, "agent_name_suggestion": suggest_agent_name(),
                    "inference_profile": inference_profile(),
                    "setup_code_required": _setup_code_required(request)}
        except Exception as exc:
            if not _is_db_not_ready_error(exc):
                raise
            last_exc = exc
            if attempt < 2:
                await asyncio.sleep(1.5)
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Database is starting up",
    ) from last_exc


@router.post("/bootstrap")
async def bootstrap(body: BootstrapRequest, request: Request, response: Response):
    """
    Create the first admin account. Only allowed when no admin exists.
    No auth required. On success returns tokens and sets cookie (auto-login).
    """
    try:
        async with get_auth_db() as db:
            # The account and everything that identifies it live in
            # vaf/auth/user_admin.py, so the terminal setup (`vaf setup`)
            # creates the exact same thing this route does. What stays here is
            # what only a browser needs: tokens, a session row and the cookie.
            from vaf.auth.user_admin import UserAdminError, create_first_admin
            try:
                new_user = await create_first_admin(
                    db, username=body.username, password=body.password,
                    agent_name=(body.agent_name or "").strip() or None,
                    setup_code=body.setup_code,
                    require_setup_code=_setup_code_required(request),
                )
            except UserAdminError as e:
                if e.code == "setup_code":
                    # A 401 the address window counts; the code lets the wizard go back to
                    # the step that asks for it.
                    return JSONResponse(status_code=e.http_status,
                                        content={"detail": e.message, "code": e.code})
                raise HTTPException(status_code=e.http_status, detail=e.message)

            access = create_access_token(
                str(new_user.id),
                new_user.username,
                new_user.role,
                str(new_user.user_scope_id),
                is_2fa_verified=True,
            )
            await open_session(db, new_user, access, ip=_client_ip(request),
                               user_agent=request.headers.get("user-agent", ""))

        _set_auth_cookie(request, response, access)
        return {
            "access_token": access,
            "user": {
                "id": str(new_user.id),
                "username": new_user.username,
                "role": new_user.role,
                "user_scope_id": str(new_user.user_scope_id),
                "requires_2fa_setup": new_user.requires_2fa_setup,
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Bootstrap failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Setup failed: {e!s}",
        ) from e


@router.post("/setup-code")
async def check_setup_code(body: SetupCodeRequest, request: Request):
    """First run only: whether a code is the setup code `vaf setup code` shows, so the wizard
    can say so before the rest of the setup is typed. 401 (counted by the address window,
    `_RATE_LIMITED_PATHS`) for a wrong one, 403 once this installation has an admin."""
    import asyncio
    if account_seal.admin_established():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Setup already completed")
    from vaf.auth.user_admin import setup_code_matches
    if not await asyncio.to_thread(setup_code_matches, body.code):
        return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED,
                            content={"detail": "The setup code is not right. Show it with "
                                               "`vaf setup code` in a terminal on the machine "
                                               "VAF runs on.", "code": "setup_code"})
    return {"ok": True}


@router.post("/test-veyllo-key")
async def test_veyllo_key(body: TestVeylloKeyRequest, request: Request):
    """
    First-run-only: validate a Veyllo API key during onboarding (before an admin exists, so there is
    no session yet). Server-side so the key isn't exposed cross-origin and CORS doesn't block it.
    Gated exactly like /bootstrap (refuses once an admin exists) so it can't be abused as an open
    key-probe oracle; also rate-limited (see _RATE_LIMITED_PATHS). Returns {"ok": bool}.
    """
    # First-run gate: only usable while no admin exists, by the keyring and by the table.
    if account_seal.admin_established():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Setup already completed")
    try:
        async with get_auth_db() as db:
            result = await db.execute(
                select(LocalUser).where(LocalUser.role == "admin", LocalUser.is_active == True)
            )
            if result.scalar_one_or_none() is not None:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Setup already completed")
    except HTTPException:
        raise
    except Exception:
        # If the DB can't be reached we can't prove first-run -> refuse (fail closed).
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Setup state unavailable")

    # A browser that is not on this machine needs the setup code here as at /bootstrap: this
    # route tests any key against the provider for whoever asks while no admin exists, which
    # made it a key-validity oracle for every admitted address (security review). The wizard
    # has the code from its first step. A wrong one is a 401 the address window counts.
    if _setup_code_required(request):
        import asyncio
        from vaf.auth.user_admin import setup_code_matches
        if not await asyncio.to_thread(setup_code_matches, body.setup_code or ""):
            return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED,
                                content={"detail": "The setup code is not right.", "code": "setup_code"})

    api_key = (body.api_key or "").strip()
    if not api_key:
        return {"ok": False, "error": "empty"}

    ok = False
    try:
        import httpx
        base = (Config.load().get("veyllo_base_url") or "https://api.veyllo.app/v1").rstrip("/")
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{base}/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10.0,
            )
        ok = resp.status_code == 200
    except Exception as e:
        logger.info("Veyllo key test failed: %s", e)
        ok = False

    if not ok:
        # Feed the shared per-IP rate limiter (this route returns 200 even on failure).
        try:
            from vaf.auth.rate_limit import client_key, record_login_failure
            record_login_failure(client_key(request))
        except Exception:
            pass
    return {"ok": ok}


async def _second_factor_step_token(token: str) -> Optional[dict]:
    """The decoded token a 2FA step (/setup-2fa, /verify-2fa) was called with, or None.

    Both routes are exempt from the middleware, so they make its check themselves. They used
    to check only the signature: a token whose session had ended (a logout, an admin's 2FA
    reset, a terminal reset) still enrolled a new second factor and came back as a live
    session (measured: setup-2fa 200, verify-2fa 200, and the new token answered as the
    account). A token from after the second factor (the bootstrap's, a signed-in account's)
    must stand like on every other route; a token from before it is checked against the
    password it was minted with (`_minted_with_current_password`)."""
    payload = decode_token(token) if token else None
    if not payload or payload.get("type") != "access":
        return None
    if payload.get("is_2fa_verified") is True:
        from vaf.auth.middleware import token_account_stands
        if not await token_account_stands(payload, token):
            return None
    return payload


def _minted_with_current_password(payload: dict, user) -> bool:
    """A token from before the second factor works only while the account still has the
    password it was minted with: a reset password must not leave a waiting temp token that
    enrolls the old password holder's own second factor."""
    if payload.get("is_2fa_verified") is True:
        return True
    import hmac
    from vaf.auth.crypto import password_fingerprint
    return hmac.compare_digest(str(payload.get("pwv") or ""),
                               password_fingerprint(getattr(user, "password_hash", None)))


@router.post("/setup-2fa")
async def setup_2fa(request: Request):
    """
    Generate TOTP secret and QR code for 2FA setup. Requires Bearer token.
    """
    auth = request.headers.get("Authorization") or request.cookies.get(COOKIE_NAME)
    if not auth:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    token = auth.replace("Bearer ", "").strip() if isinstance(auth, str) else auth
    payload = await _second_factor_step_token(token)
    if payload is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    user_id = payload.get("sub")
    username = payload.get("username", "user")
    async with get_auth_db() as db:
        result = await db.execute(
            select(LocalUser).where(LocalUser.id == uuid_module.UUID(user_id), LocalUser.is_active == True)
        )
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        if not _minted_with_current_password(payload, user):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
        if not account_seal.check(user, where="/api/auth/setup-2fa", ip=_client_ip(request)):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=account_seal.REFUSAL_LOGIN)

        # Check if user already has 2FA configured and verified
        if user.totp_secret and user.totp_nonce and not user.requires_2fa_setup:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="2FA already configured"
            )

        secret = generate_totp_secret()
        uri = get_totp_uri(secret, username, issuer="VAF")
        ciphertext, nonce = encrypt_totp_secret(secret)
        user.totp_secret = ciphertext
        user.totp_nonce = nonce
        user.requires_2fa_setup = True
        await db.commit()

    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_base64 = base64.b64encode(buf.getvalue()).decode()
    # `secret` (base32) is returned so the UI can show it under the QR for manual entry
    # in an authenticator app. The QR already encodes it; this endpoint is auth-required.
    return {"qr_code_base64": qr_base64, "secret": secret}


@router.post("/verify-2fa")
async def verify_2fa(body: Verify2FARequest, request: Request, response: Response):
    """
    Verify TOTP code and complete 2FA. On success sets cookie, creates session for new token, returns tokens.
    """
    payload = await _second_factor_step_token(body.temp_token)
    if payload is None:
        _log_auth_failure("twofa_failed", request, detail="invalid temp token")
        # The code lets the page say what happened: most often the 15 minutes of the step ran
        # out, and "invalid code" would send the person typing codes into an expired step.
        return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED,
                            content={"detail": "Invalid temp token", "code": "sign_in_expired"})

    user_id = payload.get("sub")
    code = (body.code or "").strip().replace(" ", "")
    user_id_uuid = uuid_module.UUID(user_id)

    async with get_auth_db() as db:
        result = await db.execute(
            select(LocalUser).where(LocalUser.id == user_id_uuid, LocalUser.is_active == True)
        )
        user = result.scalar_one_or_none()
        if user is not None and not _minted_with_current_password(payload, user):
            _log_auth_failure("twofa_failed", request, username=user.username or "",
                              detail="temp token from before a password change")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid temp token")
        if user is not None and not account_seal.check(user, where="/api/auth/verify-2fa",
                                                       ip=_client_ip(request)):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=account_seal.REFUSAL_LOGIN)
        if not user or not user.totp_secret or not user.totp_nonce:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="2FA not set up")

        from vaf.auth import second_factor
        reason = second_factor.check_second_factor(user, code, ip=_client_ip(request))
        if reason == second_factor.THROTTLED:
            from vaf.auth.rate_limit import too_many_attempts
            return too_many_attempts(second_factor.held_for(user))
        if reason == second_factor.UNREADABLE:
            # Wrong key (e.g. config/restart changed JWT secret) or corrupted data
            user.totp_secret = None
            user.totp_nonce = None
            user.requires_2fa_setup = True
            await db.commit()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="2FA was reset (e.g. after config or restart). Please log in again and set up 2FA with the new QR code.",
            )
        if reason == second_factor.SPENT:
            # A right code, used already (a double submit): not a guess, so not a 401 the
            # address window counts.
            return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST,
                                content={"detail": SPENT_CODE_TEXT, "code": "code_spent"})
        if reason is not None:
            # 401 like a wrong password, so the address window counts it: as a 400 it was
            # counted nowhere, and whoever had the password guessed the six digits without
            # limit for the temp token's whole life (measured: 30 wrong codes, zero 429).
            # The account's own window counts it too (check_second_factor).
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid code")

        second_factor.signed_in(user)
        user.requires_2fa_setup = False
        user.last_login = datetime.utcnow()
        await db.commit()

        access = create_access_token(
            str(user.id),
            user.username,
            user.role,
            str(user.user_scope_id),
            is_2fa_verified=True,
        )
        await open_session(db, user, access, ip=_client_ip(request),
                           user_agent=request.headers.get("user-agent", ""))

    _set_auth_cookie(request, response, access)
    return {"access_token": access}


# How long an address that completed a sign-in for an account stays exempt from that
# account's window: the owner's own devices keep working while a stranger holds the name.
_KNOWN_ADDRESS_DAYS = 30


async def _account_window_refusal(request: Request, username: str):
    """The account window is closed for this name: its refusal, or None when this client is
    not held by it. A held account costs no hashing, and the answer is the same for known and
    unknown names.

    Not held: the machine itself, and an address that completed a sign-in for this account
    before (vaf/auth/rate_limit.py). Both are still held by the address window."""
    from vaf.auth.rate_limit import account_refusal_is_new, announce_throttled, too_many_attempts
    from vaf.network.binding import is_localhost
    ip = _client_ip(request)
    if is_localhost(ip):
        return None
    try:
        async with get_auth_db() as db:
            if await signed_in_from(db, username, ip, days=_KNOWN_ADDRESS_DAYS):
                return None
    except Exception as e:
        logger.warning("Known-address lookup failed, the account window holds: %s", e)
    if account_refusal_is_new(username):
        announce_throttled(ip, username, "account", "/api/auth/login")
    return too_many_attempts()


@router.post("/login")
async def login(body: LoginRequest, request: Request, response: Response):
    """
    Username/password login. If user has 2FA, returns temp_token for verify-2fa.
    """
    from vaf.auth.rate_limit import release_account_attempt, reserve_account_attempt
    username_clean = (body.username or "").strip()
    # The account window counts this login BEFORE anything is awaited (check and count in one
    # step) and takes it back once the login did not fail: checked here and counted after the
    # awaited password work, a burst of concurrent logins all passed (measured: 60 of 60
    # reached the password check, threshold 10). A client the closed window does not hold
    # (the machine itself, a known address) goes on uncounted and is counted if it fails.
    attempt = {"mark": reserve_account_attempt(username_clean), "failed": False}
    if attempt["mark"] is None:
        refusal = await _account_window_refusal(request, username_clean)
        if refusal is not None:
            return refusal
    try:
        return await _sign_in(body, request, response, username_clean, attempt)
    finally:
        if not attempt["failed"]:
            release_account_attempt(username_clean, attempt["mark"])


async def _sign_in(body: LoginRequest, request: Request, response: Response,
                   username_clean: str, attempt: dict):
    """The login past the account window. Sets attempt["failed"] for a wrong password or an
    unknown name, the only outcomes the account window keeps."""
    from vaf.auth.rate_limit import record_account_failure
    async with get_auth_db() as db:
        result = await db.execute(
            select(LocalUser).where(
                func.lower(LocalUser.username) == username_clean.lower(),
                LocalUser.is_active == True,
            )
        )
        user = result.scalar_one_or_none()
        # Exactly one Argon2 verification per login that passes the windows, a name without an
        # account included (against a dummy hash), so the answer's timing does not say which
        # names exist; on its own pool, so logins never stall the chats on this loop.
        password_ok = await verify_password_async(user.password_hash if user else None,
                                                  body.password or "")
        if not user or not password_ok:
            attempt["failed"] = True
            if attempt["mark"] is None:
                record_account_failure(username_clean)
            _log_auth_failure("login_failed", request, username=username_clean,
                              detail="unknown user" if not user else "wrong password")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")
        # After the password, so a stranger learns nothing about a row; before the terminal
        # door's hash copy below and before any token, so a hash set in the database alone
        # neither signs in nor unlocks the terminal.
        if not account_seal.check(user, where="/api/auth/login", ip=_client_ip(request)):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=account_seal.REFUSAL_LOGIN)

        # Backfill the terminal door's hash copy. Every installation that
        # predates the door has an admin account whose password was set before
        # any mirroring existed, so without this the terminal would stay open
        # forever and tell the user no password was set. A verified login is the
        # one moment the correct hash and the confirmed identity are both here.
        try:
            from vaf.cli.gate import is_local_admin_account, mirror_admin_password_hash
            if is_local_admin_account(username=str(user.username or ""),
                                      user_scope_id=str(user.user_scope_id or "")):
                mirror_admin_password_hash(user.password_hash)
        except Exception:
            pass

        # 2FA data may become undecryptable after key/config changes.
        # Repair this state here so login can continue with fresh 2FA setup,
        # instead of failing later in /verify-2fa.
        if user.totp_secret and user.totp_nonce and not user.requires_2fa_setup:
            try:
                _ = decrypt_totp_secret(user.totp_secret, user.totp_nonce)
            except Exception:
                user.totp_secret = None
                user.totp_nonce = None
                user.requires_2fa_setup = True

        user.last_login = datetime.utcnow()
        await db.commit()

        # Check if 2FA is required
        has_2fa_configured = user.totp_secret and user.totp_nonce and not user.requires_2fa_setup
        needs_2fa_setup = user.requires_2fa_setup
        is_2fa_verified = not (needs_2fa_setup or has_2fa_configured)

        # Logins from the machine VAF runs on always get a long-lived token — the user is at
        # their own desk, 24h expiry serves no security purpose there. This must key on the REAL
        # client: behind the integrated HTTPS proxy every LAN device also arrives as 127.0.0.1,
        # so the peer address handed a 30-day token to phones and laptops the rule never meant.
        from vaf.network.binding import is_localhost
        client_ip = _client_ip(request)
        _is_localhost = is_localhost(client_ip)
        _localhost_expiry_hours = 30 * 24  # 30 days
        if is_2fa_verified:
            expires_hours = _localhost_expiry_hours if _is_localhost else None
        else:
            # The temp token only reaches /setup-2fa and /verify-2fa: a quarter of an hour
            # for the code, not the day it used to live.
            expires_hours = TEMP_TOKEN_HOURS
        access = create_access_token(
            str(user.id),
            user.username,
            user.role,
            str(user.user_scope_id),
            expires_hours=expires_hours,
            is_2fa_verified=is_2fa_verified,
            # A token from before the second factor is bound to this password.
            password_version=None if is_2fa_verified else password_fingerprint(user.password_hash),
        )

    if not is_2fa_verified:
        return {
            "requires_2fa": True,
            "needs_2fa_setup": needs_2fa_setup,  # True = show QR code, False = only code input
            "temp_token": access,
        }

    # The session row: every reader of the token asks it (vaf/auth/sessions.py).
    async with get_auth_db() as db:
        await open_session(db, user, access, ip=_client_ip(request),
                           user_agent=request.headers.get("user-agent", ""))

    # NOTE: remember_me cannot extend the session beyond the JWT expiry - a cookie
    # that outlives its token only recreates the login-loop desync. A real
    # remember-me needs a longer-lived token (local_network_jwt_expiry_hours).
    _set_auth_cookie(request, response, access)
    return {
        "access_token": access,
        "user": {
            "id": str(user.id),
            "username": user.username,
            "role": user.role,
            "user_scope_id": str(user.user_scope_id),
        },
    }


def _clear_auth_cookie(response: Response) -> None:
    """Clear the auth cookie so the client stops sending a stale token."""
    response.delete_cookie(key=COOKIE_NAME, path="/")


def _authorization_bearer(request: Request) -> str:
    """Return Bearer token from Authorization header, or empty string."""
    auth = (request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _access_token_candidates(request: Request) -> list[str]:
    """Ordered JWT candidates for /me.

    Bearer (e.g. sessionStorage) is tried before the httpOnly cookie so a stale
    cookie cannot shadow a valid Authorization token — a common SPA redirect loop.
    """
    bearer = _authorization_bearer(request)
    cookie_tok = (request.cookies.get(COOKIE_NAME) or "").strip()
    out: list[str] = []
    seen: set[str] = set()
    for t in (bearer, cookie_tok):
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


async def _me_user_from_token(request: Request, response: Response, token: str) -> dict | None:
    """Resolve current user for one access JWT, or None to try the next candidate."""
    payload = decode_token(token)
    if not payload or payload.get("type") != "access":
        return None
    user_id_str = payload.get("sub")
    if not user_id_str:
        return None
    try:
        user_id = uuid_module.UUID(user_id_str)
    except (ValueError, TypeError) as e:
        logger.warning("Invalid UUID in token sub: %s", e)
        return None

    import asyncio as _asyncio

    _DB_RETRIES = 5
    _DB_RETRY_DELAY = 1.0  # seconds between attempts

    for _attempt in range(_DB_RETRIES):
        try:
            async with get_auth_db() as db:
                result = await db.execute(
                    select(LocalUser).where(LocalUser.id == user_id, LocalUser.is_active == True)
                )
                user = result.scalar_one_or_none()
                if not user or not account_seal.check(user, where="/api/auth/me"):
                    return None

                token_hash = token_hash_for_db(token)
                now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
                session_result = await db.execute(
                    select(UserSession).where(
                        UserSession.token_hash == token_hash,
                        UserSession.is_active == True,
                        UserSession.expires_at > now_utc,
                    )
                )
                session = session_result.scalar_one_or_none()
                if not session:
                    return None

                require_2fa = Config.get("local_network_require_2fa", True)
                if require_2fa and not payload.get("is_2fa_verified", False):
                    # The admin physically at the machine is exempt from re-proving 2FA here.
                    # Must be the REAL client: behind the proxy every LAN device also looks like
                    # 127.0.0.1, which extended the exemption to remote admins.
                    client_ip = _client_ip(request) or "unknown"
                    try:
                        from vaf.network.binding import is_localhost
                    except ImportError:
                        is_localhost = lambda ip: ip in ("127.0.0.1", "::1", "localhost")

                    if not is_localhost(client_ip) or user.role != "admin":
                        _clear_auth_cookie(response)
                        raise HTTPException(
                            status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="2FA verification required",
                        )

                return {
                    "id": str(user.id),
                    "username": user.username,
                    "role": user.role,
                    "user_scope_id": str(user.user_scope_id),
                }
        except HTTPException:
            raise
        except OSError:
            pass  # connection refused — retry
        except Exception as _db_err:
            _err_str = str(_db_err).lower()
            # Transient states must be RETRIED (not 401'd): a restart leaves Postgres briefly
            # "starting up" (asyncpg CannotConnectNowError) — that message has no "connect" keyword, so
            # it used to escape here → /me returned 401 + cleared the cookie → the user got logged out
            # after every backend/Docker restart. Include the PG startup/shutdown/recovery/overload states.
            if not any(kw in _err_str for kw in (
                "connect", "connection", "refused", "could not connect", "is the server running",
                "starting up", "shutting down", "in recovery", "too many clients", "too many connections",
            )):
                raise
            # transient DB error — retry

        if _attempt < _DB_RETRIES - 1:
            logger.warning("DB not ready for /me (attempt %d/%d) — retrying in %.0fs", _attempt + 1, _DB_RETRIES, _DB_RETRY_DELAY)
            await _asyncio.sleep(_DB_RETRY_DELAY)

    # All retries exhausted — fall back to JWT-only (DB temporarily unavailable)
    username_jwt = payload.get("username", "")
    role_jwt = payload.get("role", "user")
    scope_jwt = payload.get("user_scope_id", "")
    if not username_jwt:
        return None
    logger.warning("DB unavailable after %d retries for /me — falling back to JWT-only auth for user %s", _DB_RETRIES, username_jwt)
    return {
        "id": str(user_id),
        "username": username_jwt,
        "role": role_jwt,
        "user_scope_id": scope_jwt,
    }


@router.get("/me")
async def me(request: Request, response: Response):
    """Get current user from JWT (Bearer preferred, then cookie). Validates user and session exist in DB."""
    try:
        for token in _access_token_candidates(request):
            user_dict = await _me_user_from_token(request, response, token)
            if user_dict:
                return user_dict
        # Return JSONResponse directly so Set-Cookie (clear) is preserved.
        # HTTPException creates a new response object and drops headers set on `response`.
        resp = JSONResponse(status_code=401, content={"detail": "Not authenticated"})
        resp.delete_cookie(key=COOKIE_NAME, path="/")
        return resp
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unexpected error in /me: %s", e)
        resp = JSONResponse(status_code=401, content={"detail": "Auth session error"})
        resp.delete_cookie(key=COOKIE_NAME, path="/")
        return resp


@router.post("/logout")
async def logout(request: Request, response: Response):
    """End the session of every presented token (Bearer and cookie), then clear the cookie.

    Logging out used to delete the cookie and nothing else: the token itself kept answering
    200 for its whole life (measured), so a token copied from a shared or stolen device
    outlived every sign-out. A tokenless caller only loses the cookie, as before. The sockets
    opened with those tokens close too: a socket is checked only at its handshake."""
    tokens = _access_token_candidates(request)
    if tokens:
        try:
            async with get_auth_db() as db:
                for token in tokens:
                    await end_session(db, token)
        except Exception as e:
            logger.warning("Logout could not end the session: %s", e)
        for token in tokens:
            close_session_sockets(token)
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return {"ok": True}


@router.post("/change-password")
async def change_password(body: ChangePasswordRequest, request: Request):
    """The caller changes their OWN password: the current one, and the 2FA code when 2FA is set
    up (vaf/auth/user_admin.py change_own_password). The account is the authenticated caller's;
    the tokenless desktop is the machine owner's account. Rate-limited like the login, and a
    wrong password or code counts against the same window."""
    from vaf.auth.rate_limit import client_key, record_login_failure
    from vaf.auth.user_admin import UserAdminError, change_own_password
    caller = getattr(request.state, "user", None) or {}
    try:
        async with get_auth_db() as db:
            if caller.get("user_id"):
                user = (await db.execute(select(LocalUser).where(
                    LocalUser.id == uuid_module.UUID(str(caller["user_id"])),
                    LocalUser.is_active == True))).scalar_one_or_none()  # noqa: E712
            else:
                user = (await db.execute(select(LocalUser).where(
                    func.lower(LocalUser.username) == get_local_admin_username().lower(),
                    LocalUser.is_active == True))).scalar_one_or_none()  # noqa: E712
            if user is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                    detail="No account to change the password of")
            try:
                await change_own_password(db, user, body.current_password, body.new_password, body.code,
                                          ip=_client_ip(request))
            except UserAdminError as e:
                if e.code == "wrong_code":
                    # A 400, which RateLimitMiddleware does not count. A wrong password is a
                    # 401 and is counted there already; counting it here too made it two.
                    # The account's own window and the event come from check_second_factor.
                    record_login_failure(client_key(request))
                if e.code == "wrong_password":
                    _log_auth_failure("login_failed", request, username=user.username or "",
                                      detail="change-password: wrong password")
                # The code travels with the sentence so the page can say it in the reader's
                # language; the sentence stays for every other client. A hold names its wait.
                headers = None
                if e.code == "throttled":
                    from vaf.auth.second_factor import held_for
                    headers = {"Retry-After": str(max(1, int(held_for(user))))}
                return JSONResponse(status_code=e.http_status, headers=headers,
                                    content={"detail": e.message, "code": e.code})
            username = user.username or ""
            scope = str(user.user_scope_id or "")
            # Every other session of the account ends: a password changed because it leaked
            # must throw the intruder out (measured before: the old token still answered 200).
            # The session that made the change stays; its own sockets reconnect with it.
            from vaf.auth.middleware import _extract_token
            ended = await end_account_sessions(db, user.id, keep_token=_extract_token(request))
    except HTTPException:
        raise
    except Exception as e:
        logger.error("change-password failed: %s", e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail="The password could not be changed")
    close_account_sockets(scope)
    logger.info("Password changed for %s; %d other session(s) ended", username, ended)
    try:
        from vaf.core.security_events import log_security_event
        log_security_event("password_changed", ip=_client_ip(request), username=username,
                           detail="changed by the account itself in Settings")
    except Exception:
        pass
    return {"ok": True}

