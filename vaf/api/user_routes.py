# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
User Management API routes for Local Network Admin panel.

Endpoints:
- GET  /api/users         - List all users (Admin only)
- POST /api/users         - Create new user (Admin only)
- GET  /api/users/{id}    - Get user details (Admin only)
- PUT  /api/users/{id}    - Update user (Admin only)
- DELETE /api/users/{id}  - Delete user (Admin only)
"""

import logging
import secrets
import uuid as uuid_module
from datetime import datetime, timezone

def _utc_now_naive():
    """Return naive UTC datetime for DB columns that use DateTime without timezone."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
from typing import Any, Dict, Optional, List

from fastapi import APIRouter, Depends, HTTPException, status, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vaf.auth.models import LocalUser, UserRole
from vaf.auth.database import get_auth_db
from vaf.auth.crypto import hash_password
from vaf.core.config import get_local_admin_scope_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/users", tags=["users"])


def _current_user(request: Request) -> Dict[str, Any]:
    """Current user from auth middleware or local admin (localhost)."""
    user = getattr(request.state, "user", None)
    if user and isinstance(user, dict):
        return user
    return {
        "username": "admin",
        "role": "admin",
        "user_scope_id": str(get_local_admin_scope_id()),
    }


def require_admin(request: Request) -> Dict[str, Any]:
    """Dependency: require admin role. Used for user management endpoints."""
    user = _current_user(request)
    role = (user.get("role") or "user").lower()
    if role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required for user management",
        )
    return user


# --- Request/Response Models ---

class UserCreate(BaseModel):
    username: str
    email: Optional[str] = None
    password: Optional[str] = None  # Auto-generated if not provided
    role: str = "user"
    tools: List[str] = []
    workflows: List[str] = []
    create_db: bool = True  # Whether to enable memory for this user


class UserUpdate(BaseModel):
    email: Optional[str] = None
    role: Optional[str] = None
    tools: Optional[List[str]] = None
    workflows: Optional[List[str]] = None
    is_active: Optional[bool] = None


class UserResponse(BaseModel):
    id: str
    username: str
    email: Optional[str] = None
    role: str
    is_active: bool
    requires_2fa_setup: bool
    tools: List[str] = []
    workflows: List[str] = []
    created_at: str
    last_login: Optional[str] = None

    class Config:
        from_attributes = True


# --- Endpoints ---

@router.get("")
async def list_users(_: Dict[str, Any] = Depends(require_admin)):
    """List all users (admin only). Returns empty list if DB not available."""
    try:
        # REAL online status: a user is "online" if one of their scopes currently has a live WebSocket
        # connection (the connection manager tracks the user_scope_id per socket). This is the actual
        # activity — NOT the is_active account flag, and NOT last_login (which is null for a localhost-
        # trust session that never went through password login).
        online_scopes = set()
        try:
            from vaf.core.web_server import manager as _wsmgr
            for _conn in list(getattr(_wsmgr, "active_connections", []) or []):
                try:
                    _sc = _wsmgr.get_connection_user(_conn)
                    if _sc:
                        online_scopes.add(str(_sc))
                except Exception:
                    pass
        except Exception:
            pass
        admin_scope = str(get_local_admin_scope_id())

        def _is_online(u) -> bool:
            uscope = str(u.user_scope_id) if u.user_scope_id else ""
            # The local admin connects under the configured admin scope, which may differ from this row's
            # user_scope_id — so treat any admin as online when the admin scope is connected.
            is_admin = (u.role == UserRole.ADMIN) or (bool(uscope) and uscope == admin_scope)
            return (bool(uscope) and uscope in online_scopes) or (is_admin and admin_scope in online_scopes)

        async with get_auth_db() as db:
            result = await db.execute(select(LocalUser).order_by(LocalUser.created_at.desc()))
            users = result.scalars().all()

            return [
                {
                    "id": str(user.id),
                    "username": user.username,
                    "email": user.permissions.get("email") if user.permissions else None,
                    "role": user.role,
                    "is_active": user.is_active,
                    "online": _is_online(user),
                    "requires_2fa_setup": user.requires_2fa_setup,
                    "tools": user.permissions.get("tools", []) if user.permissions else [],
                    "workflows": user.permissions.get("workflows", []) if user.permissions else [],
                    "created_at": user.created_at.isoformat() if user.created_at else None,
                    "last_login": user.last_login.isoformat() if user.last_login else None,
                }
                for user in users
            ]
    except Exception as e:
        logger.warning(f"Failed to list users: {e}")
        return []


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_user(data: UserCreate, _: Dict[str, Any] = Depends(require_admin)):
    """Create a new user account (admin only)."""
    try:
        async with get_auth_db() as db:
            # Check if username already exists
            existing = await db.execute(
                select(LocalUser).where(LocalUser.username == data.username)
            )
            if existing.scalar_one_or_none():
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Username already exists"
                )

            # Generate password if not provided
            password = data.password or secrets.token_urlsafe(12)

            # Create user
            user = LocalUser(
                id=uuid_module.uuid4(),
                username=data.username,
                password_hash=hash_password(password),
                role=data.role.lower(),
                permissions={
                    "email": data.email,
                    "tools": data.tools,
                    "workflows": data.workflows,
                    "memory_enabled": data.create_db,
                },
                is_active=True,
                requires_2fa_setup=True,
                created_at=_utc_now_naive(),
                updated_at=_utc_now_naive(),
            )

            db.add(user)
            await db.commit()
            await db.refresh(user)

            return {
                "id": str(user.id),
                "username": user.username,
                "temporary_password": password if not data.password else None,
                "message": "User created successfully"
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to create user: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create user: {str(e)}"
        )


@router.get("/{user_id}")
async def get_user(user_id: str, _: Dict[str, Any] = Depends(require_admin)):
    """Get a specific user by ID (admin only)."""
    try:
        async with get_auth_db() as db:
            result = await db.execute(
                select(LocalUser).where(LocalUser.id == uuid_module.UUID(user_id))
            )
            user = result.scalar_one_or_none()

            if not user:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="User not found"
                )

            return {
                "id": str(user.id),
                "username": user.username,
                "email": user.permissions.get("email") if user.permissions else None,
                "role": user.role,
                "is_active": user.is_active,
                "requires_2fa_setup": user.requires_2fa_setup,
                "tools": user.permissions.get("tools", []) if user.permissions else [],
                "workflows": user.permissions.get("workflows", []) if user.permissions else [],
                "created_at": user.created_at.isoformat() if user.created_at else None,
                "last_login": user.last_login.isoformat() if user.last_login else None,
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get user: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get user: {str(e)}"
        )


@router.put("/{user_id}")
async def update_user(user_id: str, data: UserUpdate, _: Dict[str, Any] = Depends(require_admin)):
    """Update a user's details (admin only)."""
    try:
        async with get_auth_db() as db:
            result = await db.execute(
                select(LocalUser).where(LocalUser.id == uuid_module.UUID(user_id))
            )
            user = result.scalar_one_or_none()

            if not user:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="User not found"
                )

            # Update fields
            if data.role is not None:
                user.role = data.role.lower()
            if data.is_active is not None:
                user.is_active = data.is_active

            # Update permissions
            permissions = user.permissions or {}
            if data.email is not None:
                permissions["email"] = data.email
            if data.tools is not None:
                permissions["tools"] = data.tools
            if data.workflows is not None:
                permissions["workflows"] = data.workflows
            user.permissions = permissions

            user.updated_at = _utc_now_naive()

            await db.commit()

            return {"message": "User updated successfully"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update user: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update user: {str(e)}"
        )


@router.post("/{user_id}/reset-password")
async def reset_password(user_id: str, _: Dict[str, Any] = Depends(require_admin)):
    """Reset a user's password to a freshly generated temporary one (admin only). Returns the temporary
    password ONCE so the admin can hand it over; it is hashed (Argon2) before storage."""
    try:
        async with get_auth_db() as db:
            result = await db.execute(select(LocalUser).where(LocalUser.id == uuid_module.UUID(user_id)))
            user = result.scalar_one_or_none()
            if not user:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
            new_password = secrets.token_urlsafe(12)
            user.password_hash = hash_password(new_password)
            user.updated_at = _utc_now_naive()
            await db.commit()
            return {"temporary_password": new_password}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to reset password: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to reset password: {str(e)}"
        )


@router.post("/{user_id}/reset-2fa")
async def reset_2fa(user_id: str, _: Dict[str, Any] = Depends(require_admin)):
    """Clear a user's 2FA (admin only): removes the stored TOTP secret and forces a fresh 2FA setup on
    the user's next login."""
    try:
        async with get_auth_db() as db:
            result = await db.execute(select(LocalUser).where(LocalUser.id == uuid_module.UUID(user_id)))
            user = result.scalar_one_or_none()
            if not user:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
            user.totp_secret = None
            user.totp_nonce = None
            user.requires_2fa_setup = True
            user.updated_at = _utc_now_naive()
            await db.commit()
            return {"message": "2FA reset; the user will set it up again on next login"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to reset 2FA: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to reset 2FA: {str(e)}"
        )


@router.delete("/{user_id}")
async def delete_user(user_id: str, _: Dict[str, Any] = Depends(require_admin)):
    """Delete a user account (admin only)."""
    try:
        async with get_auth_db() as db:
            result = await db.execute(
                select(LocalUser).where(LocalUser.id == uuid_module.UUID(user_id))
            )
            user = result.scalar_one_or_none()

            if not user:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="User not found"
                )

            # Don't allow deleting the last admin
            if user.role == "admin":
                admin_count = await db.execute(
                    select(LocalUser).where(LocalUser.role == "admin")
                )
                admins = admin_count.scalars().all()
                if len(admins) <= 1:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Cannot delete the last admin account"
                    )

            await db.delete(user)
            await db.commit()

            return {"message": "User deleted successfully"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete user: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete user: {str(e)}"
        )
