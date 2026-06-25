# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Shared OAuth session binding guards for network mode."""

from __future__ import annotations

from typing import Optional

from fastapi import HTTPException, Request

from vaf.core.config import Config


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
    """
    if not bool(Config.get("local_network_enabled", False)):
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
