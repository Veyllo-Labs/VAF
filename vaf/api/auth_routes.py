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

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE_NAME = "vaf_token"


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


# --- Endpoints ---

@router.get("/needs-setup")
async def needs_setup():
    """
    Returns whether the first admin account must be created.
    Callable without auth. When true, show Create Admin flow instead of login.
    """
    async with get_auth_db() as db:
        result = await db.execute(
            select(LocalUser).where(LocalUser.role == "admin", LocalUser.is_active == True)
        )
        has_admin = result.scalar_one_or_none() is not None
    return {"needs_setup": not has_admin}


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
            UserConfig.ensure_user_dir(username)

            access = create_access_token(
                str(new_user.id),
                new_user.username,
                new_user.role,
                str(new_user.user_scope_id),
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
        response.set_cookie(
            key=COOKIE_NAME,
            value=access,
            httponly=True,
            max_age=max_age,
            samesite="lax",
            path="/",
        )
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

        secret = decrypt_totp_secret(user.totp_secret, user.totp_nonce)
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
    response.set_cookie(
        key=COOKIE_NAME,
        value=access,
        httponly=True,
        max_age=max_age,
        samesite="lax",
        path="/",
    )
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

        user.last_login = datetime.utcnow()
        await db.commit()

        access = create_access_token(
            str(user.id),
            user.username,
            user.role,
            str(user.user_scope_id),
        )
        refresh = create_refresh_token(str(user.id))

    # Check if 2FA is required
    has_2fa_configured = user.totp_secret and user.totp_nonce and not user.requires_2fa_setup
    needs_2fa_setup = user.requires_2fa_setup

    if needs_2fa_setup or has_2fa_configured:
        return {
            "requires_2fa": True,
            "needs_2fa_setup": needs_2fa_setup,  # True = show QR code, False = only code input
            "temp_token": access,
        }

    max_age = 30 * 24 * 3600 if body.remember_me else 24 * 3600
    response.set_cookie(
        key=COOKIE_NAME,
        value=access,
        httponly=True,
        max_age=max_age,
        samesite="lax",
        path="/",
    )
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


@router.get("/me")
async def me(request: Request, response: Response):
    """Get current user from JWT (cookie or Bearer). Validates user and session exist in DB."""
    token = request.cookies.get(COOKIE_NAME) or request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    payload = decode_token(token)
    if not payload or payload.get("type") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    user_id_str = payload.get("sub")
    if not user_id_str:
        _clear_auth_cookie(response)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    try:
        user_id = uuid_module.UUID(user_id_str)
    except (ValueError, TypeError):
        _clear_auth_cookie(response)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    async with get_auth_db() as db:
        result = await db.execute(
            select(LocalUser).where(LocalUser.id == user_id, LocalUser.is_active == True)
        )
        user = result.scalar_one_or_none()
        if not user:
            _clear_auth_cookie(response)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

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
            _clear_auth_cookie(response)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session invalid or expired")

    return {
        "id": payload.get("sub"),
        "username": payload.get("username"),
        "role": payload.get("role"),
        "user_scope_id": payload.get("user_scope_id"),
    }


@router.post("/logout")
async def logout(response: Response):
    """Clear auth cookie."""
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return {"ok": True}


@router.post("/refresh")
async def refresh(body: RefreshRequest, response: Response):
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
        )
    response.set_cookie(
        key=COOKIE_NAME,
        value=access,
        httponly=True,
        max_age=24 * 3600,
        samesite="lax",
        path="/",
    )
    return {"access_token": access}
