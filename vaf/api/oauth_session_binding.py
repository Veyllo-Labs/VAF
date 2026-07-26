# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Shared OAuth session binding guards for network mode."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import HTTPException, Request

from vaf.core.config import Config

logger = logging.getLogger("vaf.api.oauth_binding")


def _real_client_ip(request: Request) -> str:
    """The true remote peer IP, accounting for the integrated HTTPS proxy.

    Delegates to the single shared resolver so this loopback exception, the auth middleware and
    the WebSocket handshake can never disagree about who the client is.

    History worth keeping: this used to reimplement the rule with a docstring asserting that the
    proxy "OVERWRITES X-Forwarded-For, discarding any client-supplied value". It did not - it
    ADDED a second header under a different case, and the backend's Headers.get() returned the
    client's forged copy, so a LAN device could claim loopback and skip the callback actor
    binding below. The proxy now strips client copies before setting its own.
    """
    from vaf.network.binding import effective_client_ip
    return effective_client_ip(request.client.host if request.client else "", request.headers.get("x-forwarded-for"))


def require_oauth_actor_in_network_mode(request: Request) -> dict:
    """
    Require authenticated actor in network mode.

    Returns request.state.user for callers that need it.
    """
    network_on = bool(Config.get("local_network_enabled", False))
    user = getattr(request.state, "user", None)
    if network_on and not isinstance(user, dict):
        raise HTTPException(status_code=401, detail="Login required before starting OAuth in network mode")
    return user if isinstance(user, dict) else {}


def enforce_callback_actor_binding(
    request: Request,
    state_username: Optional[str],
    state_scope_id: Optional[str],
) -> None:
    """
    In network mode, ensure callback actor matches the user encoded in OAuth state.

    Loopback exception (desktop): when the OAuth provider redirects the user's SYSTEM browser to the
    callback, that browser is a different context than the Qt desktop window and carries no vaf_token
    cookie (the cookie lives on http://127.0.0.1:3000, the callback is https://localhost:8443) — so
    request.state.user is unset and the strict check would 401. For a genuine loopback callback we
    therefore trust the server-side OAuth state alone (128-bit, 0600 on disk, 10-min TTL, single-use,
    PKCE-bound, and already carrying the initiating user set at the authenticated /oauth/start). LAN
    callers are unaffected and still require a matching session actor.
    """
    if not bool(Config.get("local_network_enabled", False)):
        return

    from vaf.network.binding import is_localhost
    if is_localhost(_real_client_ip(request)):
        logger.info("OAuth callback from loopback — trusting signed state (desktop system-browser flow)")
        return

    user = require_oauth_actor_in_network_mode(request)

    actor_username = str(user.get("username") or "").strip()
    actor_scope = str(user.get("user_scope_id") or "").strip()
    expected_username = str(state_username or "").strip()
    expected_scope = str(state_scope_id or "").strip()

    if expected_scope and actor_scope and expected_scope != actor_scope:
        raise HTTPException(status_code=403, detail="OAuth callback user scope mismatch")
    if expected_username and actor_username and expected_username.lower() != actor_username.lower():
        raise HTTPException(status_code=403, detail="OAuth callback user mismatch")
