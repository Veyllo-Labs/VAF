# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Derived-index re-sync + session-direct read for messaging-channel history (Telegram/Discord/...).

The authoritative record of a channel conversation is its per-chat session JSON
(``~/.vaf/sessions/<channel>_<chat_id>.json``), which holds BOTH the user's incoming messages and the
agent's outgoing replies (including auto-replies). The channel message store
(:mod:`vaf.core.channel_message_store`) is a *derived* search index over those sessions.

This module keeps the two in sync without touching the hot path:

* :func:`read_channel_session` reads a single chat straight from its session JSON (always complete —
  auto-replies the live write-hooks never captured are included), shaped like
  ``channel_message_store.get_chat_messages``. The ``read_*_chat`` tools use it so they can never
  drift from the conversation.
* :func:`sync_channel_history` rebuilds the store rows for one chat (``chat_id``) or every chat of a
  channel (``chat_id=None``) from the session JSON via ``replace_chat_rows``, so the index exactly
  mirrors the sessions (stale rows drop, re-syncs never accumulate duplicates). The ``find_*`` and
  ``*_inbox`` tools call it before they query the index. An in-process mtime cache skips sessions
  unchanged since the last sync, keeping the bulk path cheap on every tool call.

This replaces the former one-time, sentinel-gated backfill, which drifted permanently the moment an
auto-reply bypassed the live write-hooks.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("vaf.core.channel_history")

# session_id -> session-file mtime at last successful sync. In-process only: a restart clears it,
# which simply forces one full re-sync (correct, just not free).
_sync_mtime_cache: Dict[str, float] = {}


def _sessions_dir() -> Path:
    return Path.home() / ".vaf" / "sessions"


def _session_id(channel: str, chat_id: str) -> str:
    return f"{channel}_{chat_id}"


def _iso_to_ts(value: str) -> Optional[float]:
    from datetime import datetime
    try:
        return datetime.fromisoformat(value).timestamp()
    except Exception:
        return None


def _session_rows(session, channel: str) -> List[Dict[str, Any]]:
    """Project a session's user/assistant turns into store-row dicts (chronological order).
    Synthetic per-index message ids keep the bulk insert collision-free; ``replace_chat_rows`` wipes
    before writing, so the index is stable across re-syncs."""
    try:
        meta = getattr(session, "metadata", None) or {}
        chat_name = str(meta.get("chat_name") or getattr(session, "name", "") or "").strip()
    except Exception:
        chat_name = ""
    rows: List[Dict[str, Any]] = []
    for idx, m in enumerate(getattr(session, "messages", []) or []):
        role = getattr(m, "role", "")
        if role not in ("user", "assistant"):
            continue
        body = getattr(m, "content", "")
        if not isinstance(body, str):
            body = str(body)
        body = body.strip()
        if not body:
            continue
        direction = "in" if role == "user" else "out"
        rows.append({
            "body": body,
            "direction": direction,
            "ts": _iso_to_ts(getattr(m, "timestamp", "")),
            "message_id": f"_sync_{idx}_{direction}",
            "content_type": "text",
            "chat_name": chat_name,
        })
    return rows


def _session_identity(session) -> Tuple[str, Optional[str]]:
    """Owning (username, user_scope_id) for store isolation, from session metadata."""
    meta = getattr(session, "metadata", None) or {}
    username = str(meta.get("username") or "admin")
    scope = meta.get("user_scope_id")
    return username, scope


def read_channel_session(channel: str, chat_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Read one chat straight from its session JSON, newest-first, shaped like
    ``channel_message_store.get_chat_messages``. Returns ``[]`` if the session is missing/empty.
    Never raises."""
    try:
        from vaf.core.session import SessionManager
        sm = SessionManager()
        try:
            session = sm.load(_session_id(channel, chat_id), restore_state=False)
        except Exception:
            return []
        rows = _session_rows(session, channel)
        for r in rows:
            r["channel"] = channel
        rows.sort(key=lambda r: -(r.get("ts") or 0))
        return rows[: min(max(int(limit or 50), 1), 200)]
    except Exception as e:
        logger.warning("read_channel_session(%s, %s) failed: %s", channel, chat_id, e)
        return []


def _session_file(sid: str) -> Optional[Path]:
    for cand in (_sessions_dir() / f"{sid}.json", _sessions_dir() / f"{sid}.json.gz"):
        if cand.exists():
            return cand
    return None


def _sync_one(sm, channel: str, chat_id: str, *, use_cache: bool) -> int:
    """Mirror one chat's session into the store via replace_chat_rows. Returns rows written
    (0 if skipped by the mtime cache or the session is missing)."""
    sid = _session_id(channel, chat_id)
    path = _session_file(sid)
    if path is not None and use_cache:
        try:
            if _sync_mtime_cache.get(sid) == path.stat().st_mtime:
                return 0  # unchanged since last sync — store already mirrors it
        except Exception:
            pass
    try:
        session = sm.load(sid, restore_state=False)
    except Exception:
        return 0
    rows = _session_rows(session, channel)
    username, scope = _session_identity(session)
    from vaf.core.channel_message_store import replace_chat_rows
    n = replace_chat_rows(username, str(chat_id), channel, rows, user_scope_id=scope)
    if path is not None:
        try:
            _sync_mtime_cache[sid] = path.stat().st_mtime
        except Exception:
            pass
    return n


def sync_channel_history(channel: str, chat_id: Optional[str] = None) -> int:
    """Re-sync the derived store index from the authoritative session JSON.

    With ``chat_id``: re-sync just that chat (always, ignoring the mtime cache). Without it: re-sync
    every chat of the channel, skipping sessions unchanged since the last sync. Returns the number of
    rows written this call. Never raises."""
    try:
        from vaf.core.session import SessionManager
        sm = SessionManager()
        if chat_id is not None:
            return _sync_one(sm, channel, str(chat_id), use_cache=False)
        sessions_dir = _sessions_dir()
        if not sessions_dir.is_dir():
            return 0
        total = 0
        seen = set()
        prefix = f"{channel}_"
        for f in sorted(sessions_dir.glob(f"{channel}_*.json*")):
            sid = f.name
            for suffix in (".json.gz", ".json"):
                if sid.endswith(suffix):
                    sid = sid[: -len(suffix)]
                    break
            if not sid.startswith(prefix) or sid in seen:
                continue
            seen.add(sid)
            total += _sync_one(sm, channel, sid[len(prefix):], use_cache=True)
        return total
    except Exception as e:
        logger.warning("sync_channel_history(%s) failed: %s", channel, e)
        return 0
