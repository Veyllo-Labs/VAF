# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The caller's own confirmed FTP servers (vaf/core/ftp.py). Settings, Connections, FTP reads
this.

Only for an account that may use the ftp tool (`account_allows_tool` with the tool, so the
tool's `account_opt_in` counts): anybody else gets 403 and the section is not shown. Nothing
here is secret: a server entry is a name, how its certificate is trusted, and a fingerprint.
Removing one is what a server whose certificate changed needs; the next connection asks again.
"""
import asyncio
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from vaf.api.contact_routes import get_current_vaf_user

router = APIRouter(prefix="/api/ftp", tags=["ftp"])


def _caller(request: Request) -> Dict[str, Any]:
    from vaf.core.tool_dispatch import account_allows_tool
    from vaf.tools.ftp import FtpTool
    user = get_current_vaf_user(request)
    role = (getattr(request.state, "user", None) or {}).get("role")
    try:
        allowed = account_allows_tool("ftp", user["user_scope_id"], role, tool=FtpTool)
    except Exception:
        allowed = False                  # a broken guard is no guard: refuse
    if not allowed:
        raise HTTPException(status_code=403, detail="FTP is not enabled for this account")
    return user


@router.get("")
async def ftp_overview(request: Request) -> Dict[str, Any]:
    """This account's confirmed servers."""
    from vaf.core import ftp
    user = _caller(request)
    try:
        servers = await asyncio.to_thread(ftp.servers, user["user_scope_id"])
    except ftp.FtpError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"servers": servers}


@router.delete("/servers/{name:path}")
async def forget_server(name: str, request: Request) -> Dict[str, Any]:
    """Remove one confirmed server; the agent's next connection to it asks again."""
    from vaf.core import ftp
    user = _caller(request)
    if not await asyncio.to_thread(ftp.forget, name, user["user_scope_id"]):
        raise HTTPException(status_code=404, detail="no server with that name")
    return {"deleted": True}
