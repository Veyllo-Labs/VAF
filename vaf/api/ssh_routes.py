# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The caller's own SSH identity for the agent (vaf/core/ssh.py): its public key and the
servers it confirmed. Settings, Connections, SSH reads this.

Only for an account that may use the ssh tool (`account_allows_tool` with the tool, so the
tool's `account_opt_in` counts): anybody else gets 403 and the section is not shown. A key is
created here only while none exists - never replaced, because a new key would lock the account
out of every server the old one is installed on. No route returns anything secret: the public
key is meant to be handed out, and a server entry is a name and a fingerprint.
"""
import asyncio
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from vaf.api.contact_routes import get_current_vaf_user

router = APIRouter(prefix="/api/ssh", tags=["ssh"])


def _caller(request: Request) -> Dict[str, Any]:
    from vaf.core.tool_dispatch import account_allows_tool
    from vaf.tools.ssh import SshTool
    user = get_current_vaf_user(request)
    role = (getattr(request.state, "user", None) or {}).get("role")
    try:
        allowed = account_allows_tool("ssh", user["user_scope_id"], role, tool=SshTool)
    except Exception:
        allowed = False                  # a broken guard is no guard: refuse
    if not allowed:
        raise HTTPException(status_code=403, detail="SSH is not enabled for this account")
    return user


def _overview(scope) -> Dict[str, Any]:
    from vaf.core import ssh
    try:
        ssh.require_openssh()
    except ssh.SshError as e:
        return {"available": False, "reason": str(e), "public_key": None, "hosts": []}
    return {"available": True, "public_key": ssh.public_key(scope),
            "hosts": ssh.known_hosts(scope)}


@router.get("")
async def ssh_overview(request: Request) -> Dict[str, Any]:
    """This account's public key (None until one exists) and its confirmed servers."""
    user = _caller(request)
    return await asyncio.to_thread(_overview, user["user_scope_id"])


@router.post("/key")
async def create_key(request: Request) -> Dict[str, Any]:
    """Create this account's key if it has none; an existing key is returned as it is."""
    from vaf.core import ssh
    user = _caller(request)
    try:
        await asyncio.to_thread(ssh.ensure_identity, user["user_scope_id"])
    except ssh.SshError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return await asyncio.to_thread(_overview, user["user_scope_id"])


@router.delete("/hosts/{host:path}")
async def forget_host(host: str, request: Request) -> Dict[str, Any]:
    """Remove one confirmed server; the agent's next connection to it asks again."""
    from vaf.core import ssh
    user = _caller(request)
    if not await asyncio.to_thread(ssh.forget_host, host, user["user_scope_id"]):
        raise HTTPException(status_code=404, detail="no server with that name")
    return {"deleted": True}
