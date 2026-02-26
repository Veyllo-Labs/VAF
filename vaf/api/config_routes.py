"""
REST API for app config (for onboarding and other clients that cannot use WebSocket).

Endpoints:
- GET   /api/config - Get full config (auth required when local_network_enabled)
- PATCH /api/config - Merge and save config (auth required when local_network_enabled)
"""

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request, status

from vaf.core.config import Config, get_local_admin_scope_id, get_local_admin_username

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["config"])


def get_current_user_or_local_admin(request: Request) -> Dict[str, Any]:
    """Return current user from request.state (set by auth middleware) or treat as local admin.
    Includes user_scope_id for UUID-based data isolation."""
    user = getattr(request.state, "user", None)
    if user and isinstance(user, dict):
        scope = user.get("user_scope_id")
        return {
            "username": user.get("username", "admin"),
            "role": (user.get("role") or "user").lower(),
            "user_scope_id": str(scope) if scope else get_local_admin_scope_id(),
        }
    return {
        "username": get_local_admin_username(),
        "role": "admin",
        "user_scope_id": get_local_admin_scope_id(),
    }


def get_current_scope_id(request: Request) -> str:
    """Return current user's user_scope_id (for data scoping). Use get_current_user_or_local_admin when you need username/role too."""
    return get_current_user_or_local_admin(request).get("user_scope_id", get_local_admin_scope_id())


def get_current_username(request: Request) -> str:
    return get_current_user_or_local_admin(request).get("username", "admin")


@router.get("/config")
async def get_config(_username: str = Depends(get_current_username)) -> Dict[str, Any]:
    """Return current app config. Used by login page during onboarding connections step."""
    return Config.load()


@router.patch("/config")
async def patch_config(
    body: Dict[str, Any],
    request: Request,
    _user: Dict[str, Any] = Depends(get_current_user_or_local_admin),
) -> Dict[str, Any]:
    """Merge provided keys into config and save. Non-admins can only change user-scoped settings (e.g. language); global/backend/network keys are ignored."""
    if _user.get("role") != "admin":
        body = Config.filter_for_non_admin(body)
    current = Config.load()
    merged = {**current, **body}
    Config.save(merged)
    return merged
