# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
One-time backfill of existing Telegram chat history into the shared channel message store.

Telegram conversations have always been persisted as per-chat session JSONs
(~/.vaf/sessions/telegram_<chat_id>.json). The read_telegram_chat / find_telegram_messages /
telegram_inbox tools read from the searchable channel store (whatsapp_message_store, channel
='telegram'), which is only filled by the live write-hooks. This imports the pre-existing
session history once so those tools see history that predates the hooks.

Idempotent: deterministic message ids + INSERT OR REPLACE. Gated by a sentinel file AND an
in-process flag, so the read tools can call it cheaply on every invocation.
"""

import logging

logger = logging.getLogger("vaf.core.telegram_history")

_backfill_done_in_process = False


def backfill_telegram_history() -> int:
    """Import existing telegram_<id> sessions into the channel store. Returns messages imported
    (0 if already done / nothing to do). Never raises."""
    global _backfill_done_in_process
    if _backfill_done_in_process:
        return 0
    try:
        from pathlib import Path
        from datetime import datetime
        from vaf.core.platform import Platform
        from vaf.core.session import SessionManager
        from vaf.core.whatsapp_message_store import append_message

        sentinel = Path(Platform.data_dir()) / ".telegram_backfill_done"
        if sentinel.exists():
            _backfill_done_in_process = True
            return 0

        sessions_dir = Path.home() / ".vaf" / "sessions"
        if not sessions_dir.is_dir():
            sentinel.write_text("no-sessions")
            _backfill_done_in_process = True
            return 0

        sm = SessionManager()
        imported = 0
        seen_ids = set()
        for f in sorted(sessions_dir.glob("telegram_*.json*")):
            sid = f.name
            for suffix in (".json.gz", ".json"):
                if sid.endswith(suffix):
                    sid = sid[: -len(suffix)]
                    break
            if not sid.startswith("telegram_") or sid in seen_ids:
                continue
            seen_ids.add(sid)
            chat_id = sid[len("telegram_"):]
            try:
                session = sm.load(sid, restore_state=False)
            except Exception:
                continue
            meta = getattr(session, "metadata", None) or {}
            username = str(meta.get("username") or "admin")
            scope = meta.get("user_scope_id")
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
                ts = None
                try:
                    ts = datetime.fromisoformat(getattr(m, "timestamp", "")).timestamp()
                except Exception:
                    ts = None
                try:
                    append_message(
                        username=username, chat_id=str(chat_id), body=body,
                        direction=("in" if role == "user" else "out"),
                        message_id=f"bf_{idx}", channel="telegram",
                        user_scope_id=scope, ts=ts,
                    )
                    imported += 1
                except Exception:
                    pass

        sentinel.write_text(f"imported={imported}")
        _backfill_done_in_process = True
        if imported:
            logger.info("Telegram history backfill: imported %d message(s) into the channel store", imported)
        return imported
    except Exception as e:
        logger.warning("Telegram history backfill failed: %s", e)
        return 0
