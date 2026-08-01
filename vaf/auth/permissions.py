# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Which tools a user's account is allowed to use - resolved per turn, enforced in the funnel.

THE GAP THIS CLOSES. The admin's per-user tool selection has been stored
(`LocalUser.permissions["tools"]`), shown in the UI and enforced by NOTHING: the JWT never
carried it, and the only readers in the whole repository were the admin routes mirroring it
back for display. An admin could untick the coder for a user and that user's chat still
received the full registry. The UI has said so honestly since a19; this module is what makes
the checkbox true.

SEMANTICS, pinned by tests because each choice has a failure mode on the other side:

- The stored list is an ALLOWLIST - the tools the user MAY use. That is what the admin UI
  builds (its presets expand to lists of permitted tool names).
- No DB row, no `permissions` dict, no `"tools"` key, or an EMPTY list -> UNRESTRICTED.
  Empty must mean unrestricted because `[]` is the API model's creation default - a user
  created through the route without the picker would otherwise be locked out of every tool.
  "Block everything" is not expressible here on purpose; the lever for that is deactivating
  the account.
- Admins are never restricted (same `is_admin_identity` rule as every other gate).
- DB unreachable -> UNRESTRICTED, and on the desktop that is CORRECT rather than merely
  safe: the auth DB lives in the Docker stack, a stopped stack means no tenant can
  authenticate either, so the only person present is the machine owner. In server mode the
  DB being down takes login down with it - a user who cannot log in does not reach a tool.

RESOLUTION IS PER TURN, NOT PER LOGIN, deliberately: a revocation has to take effect
immediately, not after the next token refresh. The auth DB is async (asyncpg) while the
dispatch path is sync worker-thread code, so the lookup runs its own loop; a short TTL
cache keeps it to one connect per burst and bounds revocation latency to seconds.
"""
import asyncio
import threading
import time
from typing import Optional

_TTL_SECONDS = 10.0
_cache: dict = {}
_cache_lock = threading.Lock()


def _lookup_allowed_tools(user_scope_id: str) -> Optional[frozenset]:
    """One DB read. Returns None for unrestricted, a frozenset for an allowlist."""
    from sqlalchemy import select

    from vaf.auth.database import get_auth_db
    from vaf.auth.models import LocalUser

    async def _query():
        async with get_auth_db() as session:
            result = await session.execute(
                select(LocalUser.permissions).where(LocalUser.user_scope_id == user_scope_id)
            )
            row = result.scalar_one_or_none()
            return row

    return _tools_from_permissions(asyncio.run(_query()))


def _tools_from_permissions(perms) -> Optional[frozenset]:
    """The pinned semantics as a pure function, so tests hold the rules without a DB."""
    if not isinstance(perms, dict):
        return None
    tools = perms.get("tools")
    if not isinstance(tools, list) or not tools:
        return None
    allowed = frozenset(str(t) for t in tools if str(t).strip())
    return allowed or None


def resolve_allowed_tools(user_scope_id: Optional[str]) -> Optional[frozenset]:
    """The funnel's question: which tools may this scope use? None = unrestricted.

    Never raises. A worker thread with a running event loop cannot start a second one; that
    case answers from the cache or as unrestricted rather than crashing a turn - the
    enforcement is a lever, not a tripwire.
    """
    scope = str(user_scope_id or "").strip()
    if not scope:
        return None

    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(scope)
        if hit and now - hit[0] < _TTL_SECONDS:
            return hit[1]

    try:
        asyncio.get_running_loop()
        return hit[1] if hit else None          # inside a loop: cached answer or open
    except RuntimeError:
        pass

    try:
        allowed = _lookup_allowed_tools(scope)
    except Exception:
        allowed = None                          # DB down = desktop default; see module docstring
    with _cache_lock:
        _cache[scope] = (now, allowed)
        if len(_cache) > 256:
            cutoff = now - _TTL_SECONDS
            for key in [k for k, v in _cache.items() if v[0] < cutoff]:
                _cache.pop(key, None)
    return allowed


def invalidate_permissions_cache(user_scope_id: Optional[str] = None) -> None:
    """Called by the admin update route so a revocation beats the TTL."""
    with _cache_lock:
        if user_scope_id is None:
            _cache.clear()
        else:
            _cache.pop(str(user_scope_id).strip(), None)
