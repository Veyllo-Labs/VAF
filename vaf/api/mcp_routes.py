# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Signing in to MCP servers, each account on its own (vaf/core/mcp_oauth.py runs the flow).

Every route acts on the CALLER's own account and nobody else's, and none ever returns a token.
Adding or changing a server stays an admin's (the WebSocket handlers in web_server.py); signing
in to one is every account's own business, admin or not.

- `GET /api/mcp/sign-in`: the servers that sign accounts in, as the caller sees them, and the
  redirect address a client registered at a service by hand has to list.
- `POST /api/mcp/sign-in/{name}`: start; answers the authorization address the browser opens.
- `DELETE /api/mcp/sign-in/{name}`: sign out (the service is asked to invalidate the tokens).
- `GET /api/mcp/oauth/callback`: where the service sends the browser back. It is not exempt
  from the auth middleware, like the email callback: a LAN browser brings its session, and the
  person must be the one who started the sign-in (`enforce_callback_actor_binding`).
"""
import asyncio
import logging
from typing import Any, Dict
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from vaf.api.contact_routes import get_current_vaf_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mcp", tags=["mcp"])


def redirect_uri() -> str:
    """The address the service sends the browser back to: this backend's callback."""
    from vaf.core.mcp_oauth import CALLBACK_PATH
    from vaf.network.oauth_redirect import oauth_callback_base_url
    return f"{oauth_callback_base_url('mcp_oauth_callback_base_url')}{CALLBACK_PATH}"


@router.get("/sign-in")
async def sign_in_list(request: Request) -> Dict[str, Any]:
    from vaf.core.mcp_oauth import sign_in_status
    user = get_current_vaf_user(request)
    servers = await asyncio.to_thread(sign_in_status, user["user_scope_id"])
    return {"redirect_uri": redirect_uri(), "servers": servers}


@router.post("/sign-in/{name}")
async def sign_in_start(name: str, request: Request) -> Dict[str, Any]:
    from vaf.api.oauth_session_binding import require_oauth_actor_in_network_mode
    from vaf.core.mcp_oauth import McpSignInError, start_sign_in
    require_oauth_actor_in_network_mode(request)
    user = get_current_vaf_user(request)
    try:
        return await asyncio.to_thread(start_sign_in, name, user_scope_id=user["user_scope_id"],
                                       username=user["username"], redirect_uri=redirect_uri())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except McpSignInError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.delete("/sign-in/{name}")
async def sign_in_stop(name: str, request: Request) -> Dict[str, Any]:
    from vaf.core.mcp_oauth import sign_out
    user = get_current_vaf_user(request)
    return await asyncio.to_thread(sign_out, name, user_scope_id=user["user_scope_id"])


def _back(outcome: str, server: str = "") -> RedirectResponse:
    """Back to the Web UI on the Connections tab, which shows the result on the server's row
    (the reason of a failure included), in the person's own language."""
    from vaf.network.oauth_redirect import frontend_base_url
    query = f"connections=1&mcp_sign_in={outcome}" + (f"&server={quote(server, safe='')}" if server else "")
    return RedirectResponse(url=f"{frontend_base_url()}/settings?{query}", status_code=302)


@router.get("/oauth/callback")
async def oauth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    from vaf.api.oauth_session_binding import enforce_callback_actor_binding
    from vaf.core.mcp_oauth import finish_sign_in, pending_owner
    owner = pending_owner(state)
    if owner is None:
        return _back("expired")
    enforce_callback_actor_binding(request, owner["username"], owner["user_scope_id"])
    result = await asyncio.to_thread(finish_sign_in, state, code, error)
    if result.get("ok"):
        _load_tools_if_missing(result["server"])
    else:
        logger.info("MCP sign-in to %s did not finish: %s", result.get("server"), result.get("error"))
    return _back("success" if result.get("ok") else "error", result.get("server") or "")


def _load_tools_if_missing(server: str) -> None:
    """The first account to sign in is the first moment the server's tools can be listed at
    all: discover them then, in the background, instead of at the next restart."""
    try:
        from vaf.core.web_interface import get_web_interface
        agent = getattr(get_web_interface(), "agent_instance", None)
        status = getattr(agent, "_mcp_server_status", {}) or {}
        if agent is None or not hasattr(agent, "reload_mcp_tools") or (status.get(server) or {}).get("connected"):
            return
        asyncio.get_running_loop().run_in_executor(None, agent.reload_mcp_tools)
    except Exception as exc:  # noqa: BLE001 - the tools come with the next reload then
        logger.info("MCP tools of %s not reloaded after the sign-in: %s", server, exc)
