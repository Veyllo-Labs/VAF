# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The caller's own credentials for their agent's commands (vaf/core/user_secrets.py).

Write-only, like every secret the config API handles: the list answers with names, and no route
ever returns a value - a browser that could read one back would be one more place it lives.
Each route acts on the caller's own store and nobody else's.
"""
import asyncio
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from vaf.api.contact_routes import get_current_vaf_user

router = APIRouter(prefix="/api/secrets", tags=["secrets"])


class SecretValue(BaseModel):
    value: str


@router.get("")
async def list_secrets(request: Request) -> Dict[str, Any]:
    """The caller's stored names, with the variable the agent uses for each."""
    from vaf.core.user_secrets import ENV_PREFIX, names
    user = get_current_vaf_user(request)
    stored = await asyncio.to_thread(names, user_scope_id=user["user_scope_id"],
                                     username=user["username"])
    return {"names": [{"name": n, "env": f"{ENV_PREFIX}{n}"} for n in stored]}


@router.put("/{name}")
async def put_secret(name: str, body: SecretValue, request: Request) -> Dict[str, Any]:
    """Store or replace one value. Answers with the stored name, never the value."""
    from vaf.core.user_secrets import ENV_PREFIX, set_secret
    user = get_current_vaf_user(request)
    try:
        stored = await asyncio.to_thread(set_secret, name, body.value,
                                         user_scope_id=user["user_scope_id"],
                                         username=user["username"])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"name": stored, "env": f"{ENV_PREFIX}{stored}"}


@router.delete("/{name}")
async def delete_secret_route(name: str, request: Request) -> Dict[str, Any]:
    from vaf.core.user_secrets import delete_secret
    user = get_current_vaf_user(request)
    if not await asyncio.to_thread(delete_secret, name, user_scope_id=user["user_scope_id"],
                                   username=user["username"]):
        raise HTTPException(status_code=404, detail="no credential with that name")
    return {"deleted": True}
