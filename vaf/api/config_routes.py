"""
REST API for app config (for onboarding and other clients that cannot use WebSocket).

Endpoints:
- GET   /api/config - Get full config (auth required when local_network_enabled)
- PATCH /api/config - Merge and save config (auth required when local_network_enabled)
"""

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, Request

from vaf.core.config import Config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["config"])


def get_current_username(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if not user:
        return Config.get("local_admin_username", "admin")
    return user.get("username", "admin")


@router.get("/config")
async def get_config(_username: str = Depends(get_current_username)) -> Dict[str, Any]:
    """Return current app config. Used by login page during onboarding connections step."""
    return Config.load()


@router.patch("/config")
async def patch_config(
    body: Dict[str, Any],
    _username: str = Depends(get_current_username),
) -> Dict[str, Any]:
    """Merge provided keys into config and save. Used by onboarding to persist Discord etc."""
    current = Config.load()
    merged = {**current, **body}
    Config.save(merged)
    return merged
