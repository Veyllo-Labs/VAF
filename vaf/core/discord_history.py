# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Discord channel-history sync — a thin, channel-bound wrapper over :mod:`vaf.core.channel_history`.

Discord conversations are persisted as per-chat session JSONs
(``~/.vaf/sessions/discord_<author_id>.json``, bidirectional: user + assistant turns). Those sessions
are the authoritative record; the searchable channel store (channel='discord') is a derived index.
``sync_discord_history`` rebuilds that index from the sessions (so the agent's auto-replies — which
bypass the live write-hooks — are never missing), and ``read_discord_session`` reads a single chat
straight from its session JSON.
"""

from typing import Any, Dict, List, Optional

from vaf.core.channel_history import read_channel_session, sync_channel_history


def sync_discord_history(chat_id: Optional[str] = None) -> int:
    """Re-sync the Discord store index from session JSON. With ``chat_id``: just that chat;
    without: all Discord chats (mtime-cached). Returns rows written. Never raises."""
    return sync_channel_history("discord", chat_id)


def read_discord_session(chat_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Read one Discord chat straight from its session JSON (newest-first, always complete)."""
    return read_channel_session("discord", chat_id, limit)


def backfill_discord_history() -> int:
    """Back-compat alias for the former one-time backfill; now triggers a full re-sync."""
    return sync_discord_history()
