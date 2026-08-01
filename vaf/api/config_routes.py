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

from vaf.api.user_routes import require_admin
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
    # API keys leave the payload here and go into the encrypted store. Without this the
    # read side would migrate a key on first read while this path kept writing raw into a
    # file nobody asks any more - the user changes their key, the UI says saved, and the
    # agent keeps using the old one. `absorb_config_keys` also fires the Veyllo-STT seed,
    # which used to hang off the key appearing in the config dict.
    from vaf.core.api_keys import absorb_config_keys
    merged = Config.merge_preserving_nonempty_sensitive(current, absorb_config_keys(body))
    Config.save(merged)
    return merged


@router.get("/config/api-keys")
async def list_api_keys(_: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    """Which providers have a key. Booleans only - a value is never returned.

    Settings has no other way to know since keys left `config.json`: `GET /api/config`
    answers `api_key_<provider>` with the empty default, so a working key reads there as
    "not configured". Handing the secret back to the browser to fix that would be the wrong
    repair; a per-provider boolean is the whole requirement, for showing the state and for
    offering the delete this sits next to.

    An unreadable store is a 503, not an empty list: "nothing configured" and "I cannot tell
    you" must not render as the same screen.
    """
    from vaf.core.api_keys import configured_providers
    from vaf.core.secure_store import SecureStoreUnreadable

    try:
        return {"providers": configured_providers()}
    except SecureStoreUnreadable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"The stored API keys could not be read ({exc}).",
        ) from exc


@router.delete("/config/api-keys/{provider}")
async def delete_api_key_route(
    provider: str,
    _: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    """Revoke one provider key. Its own call, deliberately, not an empty field on save.

    People delete a key because it leaked, so this is a revocation and it reports like one:
    either the key is gone from every source that can answer for it, or this fails with the
    instruction to rotate it upstream. A blank field on the ordinary save path still means
    "not re-sent" and still changes nothing - that guard is what keeps a partially filled
    form from wiping a key, and it stays.
    """
    from vaf.core.api_keys import ApiKeyRevocationFailed, delete_api_key

    try:
        delete_api_key(provider)
    except ApiKeyRevocationFailed as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc
    return {"status": "revoked", "provider": (provider or "").strip().lower()}
