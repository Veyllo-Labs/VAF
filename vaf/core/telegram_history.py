# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Telegram channel-history sync — a thin, channel-bound wrapper over :mod:`vaf.core.channel_history`.

Telegram conversations are persisted as per-chat session JSONs
(``~/.vaf/sessions/telegram_<chat_id>.json``, bidirectional: user + assistant turns). Those sessions
are the authoritative record; the searchable channel store (channel='telegram') is a derived index.
``sync_telegram_history`` rebuilds that index from the sessions (so the agent's auto-replies — which
bypass the live write-hooks — are never missing), and ``read_telegram_session`` reads a single chat
straight from its session JSON.
"""

from typing import Any, Dict, List, Optional

from vaf.core.channel_history import read_channel_session, sync_channel_history


def sync_telegram_history(chat_id: Optional[str] = None) -> int:
    """Re-sync the Telegram store index from session JSON. With ``chat_id``: just that chat;
    without: all Telegram chats (mtime-cached). Returns rows written. Never raises."""
    return sync_channel_history("telegram", chat_id)


def read_telegram_session(chat_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Read one Telegram chat straight from its session JSON (newest-first, always complete)."""
    return read_channel_session("telegram", chat_id, limit)


def backfill_telegram_history() -> int:
    """Back-compat alias for the former one-time backfill; now triggers a full re-sync."""
    return sync_telegram_history()
