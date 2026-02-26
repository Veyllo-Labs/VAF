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
async def get_config(request: Request) -> Dict[str, Any]:
    """Return app config. Non-admins receive a scoped view (only their own connections)."""
    user = get_current_user_or_local_admin(request)
    full = Config.load()
    return Config.config_for_user(
        full,
        user.get("user_scope_id"),
        user.get("role", "user"),
    )


@router.patch("/config")
async def patch_config(
    body: Dict[str, Any],
    request: Request,
    _user: Dict[str, Any] = Depends(get_current_user_or_local_admin),
) -> Dict[str, Any]:
    """Merge provided keys into config and save. Non-admins: global keys ignored; connection toggles (Telegram/WhatsApp/Discord) stored per-user only."""
    current = Config.load()
    if _user.get("role") != "admin":
        body_filtered, scope_toggles = Config.extract_connection_toggles_for_scope(body, _user.get("user_scope_id"))
        body = Config.filter_for_non_admin(body_filtered)
        if scope_toggles:
            by_scope = current.get("connection_enabled_by_scope") or {}
            if not isinstance(by_scope, dict):
                by_scope = {}
            for scope_id, toggles in scope_toggles.items():
                by_scope[scope_id] = {**(by_scope.get(scope_id) or {}), **toggles}
            current["connection_enabled_by_scope"] = by_scope
    merged = {**current, **body}
    Config.save(merged)
    return merged
