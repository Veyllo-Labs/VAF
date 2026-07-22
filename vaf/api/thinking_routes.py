# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Background-agent (thinking mode) status API for the admin dashboard.

One read-only, ADMIN-gated endpoint that reports what the proactive agent is
doing PER USER: active run, waiting-for-a-reply question (with channel and
nudge state), minutes since the last completed run, tools of the newest run,
and the recent question lifecycle (asked/replied/done/declined). The admin
oversight view is deliberately cross-user - the Logs window is the admin's
audit surface; non-admins get 403 and there is no per-user variant of this
route (a user sees their own background agent through the chat itself).
"""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends

from vaf.api.user_routes import require_admin

router = APIRouter(prefix="/api/thinking", tags=["thinking"])

# Whitelisted request fields for the dashboard (never the full record: replies
# and details stay in the chat/session surfaces).
_REQUEST_FIELDS = ("id", "question", "status", "needs_reconfirm", "created_at", "updated_at")


def _requests_by_key(admin_scope: str) -> Dict[str, List[Dict[str, Any]]]:
    """Recent question records per canonical storage key (newest first)."""
    from vaf.core.platform import Platform
    from vaf.core.thinking_mode import scope_storage_key
    from vaf.core.thinking_requests import list_requests

    out: Dict[str, List[Dict[str, Any]]] = {}
    base = Platform.vaf_dir() / "thinking_requests"
    try:
        dirs = [d.name for d in base.iterdir() if d.is_dir()] if base.exists() else []
    except OSError:
        dirs = []
    for raw in dirs:
        # requests use "_default" for the no-scope dir; thinking_mode uses
        # "default" - and the admin's raw scope id also collapses to "default".
        key = "default" if raw == "_default" else scope_storage_key(raw)
        rows = list_requests(None if raw == "_default" else raw)[:8]
        sanitized = [
            {f: r.get(f) for f in _REQUEST_FIELDS} | {"question": str(r.get("question") or "")[:200]}
            for r in rows
        ]
        if key in out:  # "_default" and the admin uuid dir can both map here
            out[key] = sorted(out[key] + sanitized, key=lambda r: r.get("created_at") or "", reverse=True)[:8]
        else:
            out[key] = sanitized
    return out


@router.get("/status")
async def thinking_status(_: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    """Cross-user background-agent status for the Logs > Overview panel."""
    from vaf.core.config import Config, get_local_admin_scope_id, get_local_admin_username
    from vaf.core.thinking_mode import thinking_status_snapshot

    snapshot = thinking_status_snapshot()
    admin_scope = str(get_local_admin_scope_id())
    requests_map = _requests_by_key(admin_scope)

    try:
        from vaf.api.security_routes import _scope_username_map
        names = await _scope_username_map()
    except Exception:
        names = {}
    try:
        admin_name = str(get_local_admin_username() or "admin")
    except Exception:
        admin_name = "admin"

    users: List[Dict[str, Any]] = []
    for key in set(snapshot) | set(requests_map):
        st = snapshot.get(key) or {
            "running": False, "run_started_ts": None, "waiting": None,
            "minutes_since_last_run": None, "last_run": None,
        }
        username = admin_name if key == "default" else names.get(key, key[:8])
        users.append({
            "username": username,
            "scope": "" if key == "default" else key[:8],
            **st,
            "requests": requests_map.get(key, []),
        })
    # Most attention-worthy first: waiting, then running, then most recently active.
    users.sort(key=lambda u: (
        0 if u["waiting"] else 1 if u["running"] else 2,
        u["minutes_since_last_run"] if u["minutes_since_last_run"] is not None else float("inf"),
    ))
    return {
        "enabled": bool(Config.get("thinking_enabled", True)),
        "users": users,
    }
