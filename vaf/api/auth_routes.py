# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Auth API: needs-setup, bootstrap, login, 2FA setup/verify, refresh, logout, me.

Endpoints:
- GET  /api/auth/needs-setup
- POST /api/auth/bootstrap
- POST /api/auth/login
- POST /api/auth/setup-2fa
- POST /api/auth/verify-2fa
- POST /api/auth/refresh
- POST /api/auth/logout
- GET  /api/auth/me
"""

import io
import base64
import logging
import uuid as uuid_module
from datetime import datetime, timezone, timedelta

import pyotp
import qrcode
from fastapi import APIRouter, HTTPException, status, Response, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from vaf.auth.models import LocalUser, UserSession
from vaf.auth.database import get_auth_db
from vaf.auth.crypto import (
    hash_password,
    verify_password,
    create_access_token,
    create_refresh_token,
    decode_token,
    token_hash_for_db,
    encrypt_totp_secret,
    decrypt_totp_secret,
    generate_totp_secret,
    get_totp_uri,
    verify_totp,
)
from vaf.auth.user_config import UserConfig
from vaf.core.config import Config, get_local_admin_scope_id, get_local_admin_username

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE_NAME = "vaf_token"


def _set_auth_cookie(request: Request, response: Response, token: str, max_age: int) -> None:
    """Set the auth cookie with dynamic Secure flag based on current request protocol."""
    # Check if we are on HTTPS or behind an HTTPS proxy
    is_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        max_age=max_age,
        samesite="lax",
        secure=is_https,
        path="/",
    )


# --- Request/Response models ---

class BootstrapRequest(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str
    remember_me: bool = False


class Verify2FARequest(BaseModel):
    code: str
    temp_token: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TestVeylloKeyRequest(BaseModel):
    api_key: str


# --- Endpoints ---

@router.get("/needs-setup")
async def needs_setup():
    """
    Returns whether the first admin account must be created.
    Callable without auth. When true, show Create Admin flow instead of login.
    Retries the DB connection up to 5 times (2s apart) to handle the startup
    race where the frontend loads before PostgreSQL is ready.
    """
    import asyncio
    last_exc: Exception | None = None
    for attempt in range(5):
        try:
            async with get_auth_db() as db:
                result = await db.execute(
                    select(LocalUser).where(LocalUser.role == "admin", LocalUser.is_active == True)
                )
                has_admin = result.scalar_one_or_none() is not None
            return {"needs_setup": not has_admin}
        except OSError as exc:
            last_exc = exc
            if attempt < 4:
                await asyncio.sleep(2)
    raise last_exc  # type: ignore[misc]


@router.post("/bootstrap")
async def bootstrap(body: BootstrapRequest, request: Request, response: Response):
    """
    Create the first admin account. Only allowed when no admin exists.
    No auth required. On success returns tokens and sets cookie (auto-login).
    """
    try:
        async with get_auth_db() as db:
            result = await db.execute(
                select(LocalUser).where(LocalUser.role == "admin", LocalUser.is_active == True)
            )
            if result.scalar_one_or_none() is not None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="An admin account already exists",
                )

            username = (body.username or "").strip()
            if not username or len(username) < 2:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Username must be at least 2 characters",
                )

            result = await db.execute(
                select(LocalUser).where(func.lower(LocalUser.username) == username.lower())
            )
            if result.scalar_one_or_none():
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Username already taken",
                )

            if not body.password or len(body.password) < 8:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Password must be at least 8 characters",
                )

            password_hash = hash_password(body.password)
            new_user = LocalUser(
                username=username,
                password_hash=password_hash,
                role="admin",
                requires_2fa_setup=True,
            )
            db.add(new_user)
            await db.commit()
            await db.refresh(new_user)
            # Persist admin identity so CLI and localhost without JWT use the same scope (single identity)
            from vaf.core.config import Config
            config = Config.load()
            config["local_admin_scope_id"] = str(new_user.user_scope_id)
            config["local_admin_username"] = new_user.username
            Config.save(config)
            UserConfig.ensure_user_dir(username)

            access = create_access_token(
                str(new_user.id),
                new_user.username,
                new_user.role,
                str(new_user.user_scope_id),
                is_2fa_verified=True,
            )
            refresh = create_refresh_token(str(new_user.id))
            payload = decode_token(access)
            exp_ts = payload.get("exp") if payload else None
            expires_at = (
                datetime.fromtimestamp(exp_ts, tz=timezone.utc)
                if exp_ts
                else datetime.now(timezone.utc) + timedelta(hours=24)
            )
            # DB uses TIMESTAMP WITHOUT TIME ZONE; store naive UTC
            if expires_at.tzinfo is not None:
                expires_at = expires_at.replace(tzinfo=None)
            client_host = request.client.host if request.client else None
            user_agent = request.headers.get("user-agent", "")
            session = UserSession(
                user_id=new_user.id,
                token_hash=token_hash_for_db(access),
                device_info={"ip": client_host, "user_agent": user_agent, "device_type": "web"},
                expires_at=expires_at,
            )
            db.add(session)
            await db.commit()

        max_age = 30 * 24 * 3600  # bootstrap: 30 days cookie for first admin
        _set_auth_cookie(request, response, access, max_age)
        return {
            "access_token": access,
            "refresh_token": refresh,
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


@router.post("/test-veyllo-key")
async def test_veyllo_key(body: TestVeylloKeyRequest, request: Request):
    """
    First-run-only: validate a Veyllo API key during onboarding (before an admin exists, so there is
    no session yet). Server-side so the key isn't exposed cross-origin and CORS doesn't block it.
    Gated exactly like /bootstrap (refuses once an admin exists) so it can't be abused as an open
    key-probe oracle; also rate-limited (see _RATE_LIMITED_PATHS). Returns {"ok": bool}.
    """
    # First-run gate: only usable while no admin exists.
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
            from vaf.auth.rate_limit import record_login_failure
            client_ip = request.client.host if request.client else "unknown"
            record_login_failure(client_ip)
        except Exception:
            pass
    return {"ok": ok}


@router.post("/setup-2fa")
async def setup_2fa(request: Request):
    """
    Generate TOTP secret and QR code for 2FA setup. Requires Bearer token.
    """
    auth = request.headers.get("Authorization") or request.cookies.get(COOKIE_NAME)
    if not auth:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    token = auth.replace("Bearer ", "").strip() if isinstance(auth, str) else auth
    payload = decode_token(token)
    if not payload or payload.get("type") != "access":
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
    return {"qr_code_base64": qr_base64}


@router.post("/verify-2fa")
async def verify_2fa(body: Verify2FARequest, request: Request, response: Response):
    """
    Verify TOTP code and complete 2FA. On success sets cookie, creates session for new token, returns tokens.
    """
    payload = decode_token(body.temp_token)
    if not payload or payload.get("type") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid temp token")

    user_id = payload.get("sub")
    code = (body.code or "").strip().replace(" ", "")
    user_id_uuid = uuid_module.UUID(user_id)

    async with get_auth_db() as db:
        result = await db.execute(
            select(LocalUser).where(LocalUser.id == user_id_uuid, LocalUser.is_active == True)
        )
        user = result.scalar_one_or_none()
        if not user or not user.totp_secret or not user.totp_nonce:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="2FA not set up")

        try:
            secret = decrypt_totp_secret(user.totp_secret, user.totp_nonce)
        except Exception:
            # Wrong key (e.g. config/restart changed JWT secret) or corrupted data
            user.totp_secret = None
            user.totp_nonce = None
            user.requires_2fa_setup = True
            await db.commit()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="2FA was reset (e.g. after config or restart). Please log in again and set up 2FA with the new QR code.",
            )

        if not verify_totp(secret, code):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid code")

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
        refresh = create_refresh_token(str(user.id))

        payload_new = decode_token(access)
        exp_ts = payload_new.get("exp") if payload_new else None
        expires_at = (
            datetime.fromtimestamp(exp_ts, tz=timezone.utc)
            if exp_ts
            else datetime.now(timezone.utc) + timedelta(hours=24)
        )
        if expires_at.tzinfo is not None:
            expires_at = expires_at.replace(tzinfo=None)
        client_host = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent", "")
        session = UserSession(
            user_id=user.id,
            token_hash=token_hash_for_db(access),
            device_info={"ip": client_host, "user_agent": user_agent, "device_type": "web"},
            expires_at=expires_at,
        )
        db.add(session)
        await db.commit()

    max_age = 30 * 24 * 3600
    _set_auth_cookie(request, response, access, max_age)
    return {"access_token": access, "refresh_token": refresh}


@router.post("/login")
async def login(body: LoginRequest, request: Request, response: Response):
    """
    Username/password login. If user has 2FA, returns temp_token for verify-2fa.
    """
    username_clean = (body.username or "").strip()
    async with get_auth_db() as db:
        result = await db.execute(
            select(LocalUser).where(
                func.lower(LocalUser.username) == username_clean.lower(),
                LocalUser.is_active == True,
            )
        )
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")
        if not verify_password(user.password_hash, body.password or ""):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

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

        # Localhost (desktop tray) logins always get a long-lived token —
        # the user is on their own machine, 24h expiry serves no security purpose.
        client_ip = request.client.host if request.client else ""
        _is_localhost = client_ip in ("127.0.0.1", "::1", "localhost")
        _localhost_expiry_hours = 30 * 24  # 30 days
        access = create_access_token(
            str(user.id),
            user.username,
            user.role,
            str(user.user_scope_id),
            expires_hours=_localhost_expiry_hours if _is_localhost else None,
            is_2fa_verified=is_2fa_verified,
        )
        refresh = create_refresh_token(str(user.id))

    if not is_2fa_verified:
        return {
            "requires_2fa": True,
            "needs_2fa_setup": needs_2fa_setup,  # True = show QR code, False = only code input
            "temp_token": access,
        }

    # Create UserSession so /me can validate the token via DB (required by get_current_user_from_token)
    payload_login = decode_token(access)
    exp_ts_login = payload_login.get("exp") if payload_login else None
    expires_at_login = (
        datetime.fromtimestamp(exp_ts_login, tz=timezone.utc)
        if exp_ts_login
        else datetime.now(timezone.utc) + timedelta(hours=24)
    )
    if expires_at_login.tzinfo is not None:
        expires_at_login = expires_at_login.replace(tzinfo=None)
    client_host_login = request.client.host if request.client else None
    user_agent_login = request.headers.get("user-agent", "")
    async with get_auth_db() as db:
        login_session = UserSession(
            user_id=user.id,
            token_hash=token_hash_for_db(access),
            device_info={"ip": client_host_login, "user_agent": user_agent_login, "device_type": "web"},
            expires_at=expires_at_login,
        )
        db.add(login_session)
        await db.commit()

    max_age = 30 * 24 * 3600 if (body.remember_me or _is_localhost) else 24 * 3600
    _set_auth_cookie(request, response, access, max_age)
    return {
        "access_token": access,
        "refresh_token": refresh,
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
                if not user:
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
                    client_ip = request.client.host if request.client else "unknown"
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
async def logout(response: Response):
    """Clear auth cookie."""
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return {"ok": True}


@router.post("/refresh")
async def refresh(body: RefreshRequest, request: Request, response: Response):
    """Exchange refresh token for new access token."""
    payload = decode_token(body.refresh_token)
    if not payload or payload.get("type") != "refresh":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")
    user_id = payload.get("sub")
    async with get_auth_db() as db:
        result = await db.execute(select(LocalUser).where(LocalUser.id == uuid_module.UUID(user_id), LocalUser.is_active == True))
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
        access = create_access_token(
            str(user.id),
            user.username,
            user.role,
            str(user.user_scope_id),
            is_2fa_verified=True,
        )
    _set_auth_cookie(request, response, access, 24 * 3600)
    return {"access_token": access}
