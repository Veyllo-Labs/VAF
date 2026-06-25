# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
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


@router.get("/provider-models")
async def get_provider_models() -> Dict[str, Any]:
    """Static per-provider model metadata (default + fallback list) — the single source
    (Config.PROVIDER_MODELS) the web UI reads to populate provider/model dropdowns. Static,
    non-sensitive: no auth required. The live /v1/models list still takes precedence in the UI."""
    return Config.PROVIDER_MODELS


@router.patch("/config")
async def patch_config(
    body: Dict[str, Any],
    request: Request,
    _user: Dict[str, Any] = Depends(get_current_user_or_local_admin),
) -> Dict[str, Any]:
    """Merge provided keys into config and save. Non-admins: global keys ignored; connection toggles (Telegram/WhatsApp/Discord) stored per-user only."""
    current = Config.load()

    # In server_mode: LAN settings are locked — they cannot be disabled via the API.
    if current.get("server_mode", False):
        _SERVER_LOCKED = {"local_network_enabled", "local_network_tls_enabled", "server_mode"}
        body = {k: v for k, v in body.items() if k not in _SERVER_LOCKED}

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
    merged = Config.merge_preserving_nonempty_sensitive(current, body)
    Config.save(merged)
    return merged
