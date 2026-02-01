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
import uuid as uuid_module
from datetime import datetime, timezone
from typing import Optional, List

from fastapi import APIRouter, HTTPException, status, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vaf.auth.models import LocalUser, UserRole
from vaf.auth.database import get_auth_db
from vaf.auth.crypto import hash_password

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/users", tags=["users"])


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
async def list_users():
    """List all users. Returns empty list if DB not available."""
    try:
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
async def create_user(data: UserCreate):
    """Create a new user account."""
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
            import secrets
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
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
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
async def get_user(user_id: str):
    """Get a specific user by ID."""
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
async def update_user(user_id: str, data: UserUpdate):
    """Update a user's details."""
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

            user.updated_at = datetime.now(timezone.utc)

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


@router.delete("/{user_id}")
async def delete_user(user_id: str):
    """Delete a user account."""
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
