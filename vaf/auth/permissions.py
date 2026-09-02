# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Which tools and workflows a user's account may use - resolved per turn/run.

THE GAP THIS CLOSES. The admin's per-user tool selection has been stored
(`LocalUser.permissions["tools"]`), shown in the UI and enforced by NOTHING: the JWT never
carried it, and the only readers in the whole repository were the admin routes mirroring it
back for display. An admin could untick the coder for a user and that user's chat still
received the full registry. The UI has said so honestly since a19; this module is what makes
the checkbox true. The workflow half (`permissions["workflows"]`, saved-template ids,
consumed by the engine's start gate) had the identical history and gets the identical
resolution: same store, same semantics, same cache, same invalidation.

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
_cache: dict = {}          # tools:     scope -> (monotonic_ts, frozenset | None)
_wf_cache: dict = {}       # workflows: scope -> (monotonic_ts, frozenset | None)
_cache_lock = threading.Lock()


def _fetch_permissions_row(user_scope_id: str):
    """One DB read of the raw permissions dict (or None)."""
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

    return asyncio.run(_query())


def _lookup_allowed_tools(user_scope_id: str) -> Optional[frozenset]:
    """One DB read. Returns None for unrestricted, a frozenset for an allowlist."""
    return _tools_from_permissions(_fetch_permissions_row(user_scope_id))


def _lookup_confirmation_bypass(user_scope_id: str) -> bool:
    """True only when the admin explicitly granted this user the hands-off switch.

    Absence, a missing row, a malformed dict - everything that is not a literal
    True - is False. The bypass fails CLOSED, the opposite polarity of the tool
    allowlist above: an unreachable DB must widen nothing here, because this
    flag does not restrict a user, it removes a question the machine owner
    normally gets to answer.
    """
    perms = _fetch_permissions_row(user_scope_id)
    return bool(isinstance(perms, dict) and perms.get("confirmation_bypass") is True)


def _lookup_allowed_workflows(user_scope_id: str) -> Optional[frozenset]:
    """The workflow twin of _lookup_allowed_tools; same read, other key."""
    return _workflows_from_permissions(_fetch_permissions_row(user_scope_id))


def _entries_from_permissions(perms, key: str) -> Optional[frozenset]:
    """The pinned semantics as a pure function, so tests hold the rules without a DB.

    Identical for tools and workflows: non-dict, absent key, non-list or EMPTY list all
    mean UNRESTRICTED - `[]` is the API model's creation default for BOTH keys, and a
    user created without the picker must not be locked out of everything.
    """
    if not isinstance(perms, dict):
        return None
    entries = perms.get(key)
    if not isinstance(entries, list) or not entries:
        return None
    allowed = frozenset(str(t) for t in entries if str(t).strip())
    return allowed or None


def _tools_from_permissions(perms) -> Optional[frozenset]:
    return _entries_from_permissions(perms, "tools")


def _workflows_from_permissions(perms) -> Optional[frozenset]:
    return _entries_from_permissions(perms, "workflows")


def _resolve_cached(scope: str, cache: dict, lookup) -> Optional[frozenset]:
    """TTL-cached, never-raising resolution shared by both allowlists.

    A worker thread with a running event loop cannot start a second one; that case
    answers from the cache or as unrestricted rather than crashing a turn - the
    enforcement is a lever, not a tripwire.
    """
    now = time.monotonic()
    with _cache_lock:
        hit = cache.get(scope)
        if hit and now - hit[0] < _TTL_SECONDS:
            return hit[1]

    try:
        asyncio.get_running_loop()
        return hit[1] if hit else None          # inside a loop: cached answer or open
    except RuntimeError:
        pass

    try:
        allowed = lookup(scope)
    except Exception:
        allowed = None                          # DB down = desktop default; see module docstring
    with _cache_lock:
        cache[scope] = (now, allowed)
        if len(cache) > 256:
            cutoff = now - _TTL_SECONDS
            for key in [k for k, v in cache.items() if v[0] < cutoff]:
                cache.pop(key, None)
    return allowed


def resolve_allowed_tools(user_scope_id: Optional[str]) -> Optional[frozenset]:
    """The funnel's question: which tools may this scope use? None = unrestricted.

    Never raises (see _resolve_cached).
    """
    scope = str(user_scope_id or "").strip()
    if not scope:
        return None
    return _resolve_cached(scope, _cache, _lookup_allowed_tools)


def resolve_allowed_workflows(user_scope_id: Optional[str]) -> Optional[frozenset]:
    """The start gate's question: which saved workflow templates may this scope start?

    None = unrestricted. Never raises (see _resolve_cached).
    """
    scope = str(user_scope_id or "").strip()
    if not scope:
        return None
    return _resolve_cached(scope, _wf_cache, _lookup_allowed_workflows)


_bypass_cache: dict = {}


def resolve_confirmation_bypass(user_scope_id: Optional[str]) -> bool:
    """The gate's question: did the admin grant this scope the hands-off switch?

    False for no scope, False when nothing is known (fail-closed; see the
    lookup's docstring), cached like the allowlists and invalidated with them.
    Never raises.
    """
    scope = str(user_scope_id or "").strip()
    if not scope:
        return False
    return _resolve_cached(scope, _bypass_cache, _lookup_confirmation_bypass) is True


def list_accounts() -> list:
    """The account directory resolver the harness registers (`set_account_directory_resolver`).

    Every account in the auth store as ``{"username", "user_scope_id", "active"}``,
    names as stored. Never raises: an unreachable store is an empty directory, which
    for the one thing built on it - finding somebody to invite - is the safe answer.
    Runs the async store from a sync caller the way the other resolvers here do; when
    an event loop is already running (a WebSocket handler) the lookup goes to a side
    thread, so callers on the loop should hand this to an executor.
    """
    import asyncio

    async def _lookup() -> list:
        from sqlalchemy import select
        from vaf.auth.database import get_auth_db
        from vaf.auth.models import LocalUser
        async with get_auth_db() as db:
            rows = (await db.execute(select(LocalUser.username, LocalUser.user_scope_id,
                                            LocalUser.is_active))).all()
        return [{"username": str(name), "user_scope_id": str(scope), "active": bool(active)}
                for name, scope, active in rows if name and scope]

    try:
        try:
            asyncio.get_running_loop()
            running = True
        except RuntimeError:
            running = False
        if not running:
            return asyncio.run(_lookup())
        import threading
        box: list = [[]]

        def _run() -> None:
            try:
                box[0] = asyncio.run(_lookup())
            except Exception:
                box[0] = []

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        worker.join(timeout=5.0)
        return list(box[0] or [])
    except Exception:
        return []


def invalidate_permissions_cache(user_scope_id: Optional[str] = None) -> None:
    """Called by the admin update route so a revocation beats the TTL. Clears BOTH
    allowlists: the route writes tools and workflows in one save."""
    with _cache_lock:
        if user_scope_id is None:
            _cache.clear()
            _wf_cache.clear()
            _bypass_cache.clear()
        else:
            _cache.pop(str(user_scope_id).strip(), None)
            _wf_cache.pop(str(user_scope_id).strip(), None)
            _bypass_cache.pop(str(user_scope_id).strip(), None)
