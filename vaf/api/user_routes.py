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

async def count_other_active_admins(db, target_id) -> int:
    """How many ACTIVE admins besides this one - the number both lockout guards need.

    Shared rather than written twice: the two call sites disagreeing about whether
    ``is_active`` counts is exactly how the delete route ended up permitting the lockout
    the update route refuses.
    """
    result = await db.execute(
        select(LocalUser).where(
            LocalUser.role == "admin",
            LocalUser.is_active == True,  # noqa: E712 - SQLAlchemy column comparison
            LocalUser.id != target_id,
        )
    )
    return len(result.scalars().all())


def caller_identity(caller: dict) -> tuple:
    """The (id, scope) of whoever is making the request, from the auth middleware's dict.

    The key is ``user_id``: the middleware maps the JWT ``sub`` claim onto that name
    (vaf/auth/middleware.py). Reading ``sub`` here yields None on every request and
    silently disables the id half of the self-check - it did, until a review measured it.
    The tokenless local-admin caller has no id at all, which is why the scope is the
    second half rather than a nicety.
    """
    caller = caller or {}
    return caller.get("user_id") or caller.get("sub"), caller.get("user_scope_id")


def refuse_dangerous_user_change(*, caller_id, caller_scope, target_id, target_scope,
                                 target_role, target_active, other_active_admins: int,
                                 new_role=None, new_is_active=None, deleting: bool = False):
    """Refusal reasons for changes that would saw off the branch someone sits on.

    ONE function for both the update and the delete route, because they can reach the same
    lockout by three different doors - deactivate, demote, delete - and a rule that lives
    in only one of them is not a rule. (Measured: the delete route's own guard counted
    admin rows by EXISTENCE, so deleting the last ACTIVE admin was allowed whenever a
    deactivated admin row remained. That leaves an instance where no admin can sign in,
    which flips it back to "needs setup" and re-opens the unauthenticated bootstrap
    endpoint to the LAN.)

    Self-protection: you cannot deactivate, demote or delete YOUR OWN account - the
    session issuing the request would keep rights its account no longer has, and another
    admin can always do it for you. Last-admin protection: nobody may deactivate, demote
    or delete the last ACTIVE admin. "Active" is the operative word - an
    existing-but-deactivated admin cannot log in to repair anything, so counting rows
    would call a system repairable that is not.

    Self is matched by id AND by scope; see :func:`caller_identity`. Returns a readable
    reason, or None to allow.
    """
    is_admin_target = (target_role or "").lower() == "admin"
    losing_access = deleting or (new_is_active is False and bool(target_active))
    demoting = (
        not deleting
        and new_role is not None
        and str(new_role).lower() != "admin"
        and is_admin_target
    )
    if not losing_access and not demoting:
        return None

    is_self = bool(
        (caller_id and target_id and str(caller_id) == str(target_id))
        or (caller_scope and target_scope and str(caller_scope) == str(target_scope))
    )
    if is_self and deleting:
        return "You cannot delete your own account - another admin can."
    if is_self and losing_access:
        return "You cannot deactivate your own account - another admin can."
    if is_self and demoting:
        return "You cannot remove your own admin role - another admin can."
    if is_admin_target and other_active_admins == 0:
        return (
            "Cannot delete the last active admin account."
            if deleting else
            "Cannot deactivate or demote the last active admin account."
        )
    return None


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
    # The hands-off switch: the agent skips the tool-confirmation dialog for this
    # user. Admin-granted (this route is require_admin), announced per use via
    # gate_bypassed events, default absent = off.
    confirmation_bypass: Optional[bool] = None


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

@router.get("/tool-universe")
async def tool_universe(_: dict = Depends(require_admin)):
    """Every tool name the per-user picker may govern beyond the main registry.

    The picker builds its list from the live registry, and the coder's inner tools -
    bash above all - are deliberately NOT in that registry. Without this, "disable bash
    for this user" is not expressible: the allowlist could never contain or omit a name
    the admin was never shown. `coder_only` names come from the coder module's own
    declaration, so the picker cannot drift from what the child actually runs.
    """
    try:
        from vaf.tools.coder import CODER_ONLY_TOOL_NAMES
        return {"coder_only": sorted(CODER_ONLY_TOOL_NAMES)}
    except Exception:
        return {"coder_only": []}


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
                    "confirmation_bypass": bool(user.permissions.get("confirmation_bypass")) if user.permissions else False,
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
            # Generate password if not provided
            password = data.password or secrets.token_urlsafe(12)

            # Same rules as the first admin gets - this route used to check
            # duplicates case-SENSITIVELY and to accept any password length,
            # so "Alice" and "alice" could both exist and an admin could hand
            # out a two-character password.
            from vaf.auth.user_admin import UserAdminError, create_local_user
            try:
                user = await create_local_user(
                    db,
                    username=data.username,
                    password=password,
                    role=data.role,
                    permissions={
                        "email": data.email,
                        "tools": data.tools,
                        "workflows": data.workflows,
                        "memory_enabled": data.create_db,
                    },
                )
            except UserAdminError as e:
                raise HTTPException(status_code=e.http_status, detail=e.message)

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
                "confirmation_bypass": bool(user.permissions.get("confirmation_bypass")) if user.permissions else False,
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
async def update_user(user_id: str, data: UserUpdate, admin: Dict[str, Any] = Depends(require_admin)):
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

            if data.role is not None or data.is_active is not None:
                caller_id, caller_scope = caller_identity(admin)
                reason = refuse_dangerous_user_change(
                    caller_id=caller_id,
                    caller_scope=caller_scope,
                    target_id=user.id,
                    target_scope=user.user_scope_id,
                    target_role=user.role,
                    target_active=user.is_active,
                    new_role=data.role,
                    new_is_active=data.is_active,
                    other_active_admins=await count_other_active_admins(db, user.id),
                )
                if reason:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=reason)

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
            if data.confirmation_bypass is not None:
                permissions["confirmation_bypass"] = bool(data.confirmation_bypass)
            user.permissions = permissions
            # A revocation must beat the resolver's TTL: the funnel caches the allowlist
            # for a few seconds per scope, and "the admin just unticked it" is exactly the
            # moment that cache would lie.
            try:
                from vaf.auth.permissions import invalidate_permissions_cache
                invalidate_permissions_cache(str(user.user_scope_id))
            except Exception:
                pass

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
            from vaf.cli.gate import is_local_admin_account, mirror_admin_password_hash
            if is_local_admin_account(username=str(user.username or ""),
                                      user_scope_id=str(user.user_scope_id or "")):
                mirror_admin_password_hash(user.password_hash)
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
async def delete_user(user_id: str, admin: Dict[str, Any] = Depends(require_admin)):
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

            # Same three rules as the update route, through the same function: deleting is
            # the third door to the identical lockout, and this guard used to count admin
            # ROWS - so with a deactivated admin row present, the last ACTIVE admin could
            # be deleted and nobody could sign in to repair it.
            caller_id, caller_scope = caller_identity(admin)
            reason = refuse_dangerous_user_change(
                caller_id=caller_id,
                caller_scope=caller_scope,
                target_id=user.id,
                target_scope=user.user_scope_id,
                target_role=user.role,
                target_active=user.is_active,
                deleting=True,
                other_active_admins=await count_other_active_admins(db, user.id),
            )
            if reason:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=reason)

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
