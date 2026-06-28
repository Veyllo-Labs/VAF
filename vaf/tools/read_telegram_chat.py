# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Read messages from a Telegram chat (the Telegram counterpart of read_whatsapp_chat).
"""

from vaf.tools.base import BaseTool


class ReadTelegramChatTool(BaseTool):
    """
    Read messages from a Telegram chat. chat_id is optional and defaults to the user's own
    Telegram conversation, so 'what did we talk about on Telegram' works without an id.
    """
    name = "read_telegram_chat"
    permission_level = "read"
    side_effect_class = "none"
    description = (
        "Read past messages from a Telegram chat (in/out, with timestamps). "
        "Use when the user asks 'what did we discuss on Telegram' or to recall an earlier Telegram message. "
        "chat_id is optional — it defaults to the user's own Telegram chat. "
        "Use telegram_inbox to list chats or find_telegram_messages to search."
    )
    parameters = {
        "type": "object",
        "properties": {
            "chat_id": {
                "type": "string",
                "description": "Optional Telegram chat id (numeric). Defaults to the user's own Telegram chat.",
            },
            "limit": {
                "type": "integer",
                "description": "Max messages to return (default 50).",
            },
        },
        "required": [],
    }

    def run(self, **kwargs) -> str:
        username = (kwargs.get("username") or "admin").strip()
        user_scope_id = kwargs.get("user_scope_id")
        chat_id = (kwargs.get("chat_id") or "").strip()
        limit = min(max(int(kwargs.get("limit") or 50), 1), 200)

        if not chat_id:
            # Default to the user's own Telegram chat.
            try:
                from vaf.core.messaging_connections import get_telegram_chat_id
                chat_id = str(get_telegram_chat_id(user_scope_id, username) or "").strip()
            except Exception:
                chat_id = ""
        if not chat_id:
            return (
                "No Telegram chat found. The user must have Telegram connected "
                "(Settings → Connections → Telegram), or pass an explicit chat_id from telegram_inbox."
            )

        try:
            from vaf.core.whatsapp_message_store import get_chat_messages
        except ImportError as e:
            return f"Message store unavailable: {e}"

        try:
            from vaf.core.telegram_history import backfill_telegram_history
            backfill_telegram_history()
        except Exception:
            pass

        messages = get_chat_messages(username, chat_id, limit=limit, user_scope_id=user_scope_id, channel="telegram")
        if not messages:
            return f"No Telegram messages found for chat {chat_id}. Messages are stored as they arrive."

        lines = []
        for m in reversed(messages):
            body = m.get("body") or ""
            label = "IN" if (m.get("direction") or "in") == "in" else "OUT"
            ts = m.get("ts")
            if ts:
                from datetime import datetime
                ts_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
            else:
                ts_str = "—"
            lines.append(f"[{ts_str}] {label}: {body}")
        return f"Telegram chat {chat_id} (last {len(messages)} messages):\n" + "\n".join(lines)
