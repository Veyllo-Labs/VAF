# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
List Telegram chats that have stored messages (the Telegram counterpart of whatsapp_inbox).
Also resolves the dead `telegram_inbox` reference in context.py's read-only auto-approve list.
"""

from vaf.tools.base import BaseTool


class TelegramInboxTool(BaseTool):
    """
    List Telegram chats that have message history, most recent first.
    Use to discover which chats exist before calling read_telegram_chat.
    """
    name = "telegram_inbox"
    permission_level = "read"
    side_effect_class = "none"
    description = (
        "List Telegram chats that have stored messages (most recent first), with chat_id, name, "
        "last activity and message count. Use to find a chat_id for read_telegram_chat."
    )
    parameters = {
        "type": "object",
        "properties": {
            "max_chats": {
                "type": "integer",
                "description": "Max chats to return (default 50).",
            },
        },
        "required": [],
    }

    def run(self, **kwargs) -> str:
        username = (kwargs.get("username") or "admin").strip()
        user_scope_id = kwargs.get("user_scope_id")
        max_chats = min(max(int(kwargs.get("max_chats") or 50), 1), 200)

        try:
            from vaf.core.whatsapp_message_store import list_chats_from_store
        except ImportError as e:
            return f"Message store unavailable: {e}"

        try:
            from vaf.core.telegram_history import backfill_telegram_history
            backfill_telegram_history()
        except Exception:
            pass

        chats = list_chats_from_store(username, limit=max_chats, user_scope_id=user_scope_id, channel="telegram")
        if not chats:
            return "No Telegram chats with stored messages yet. Messages are stored as they arrive."

        lines = []
        for i, c in enumerate(chats, 1):
            cid = c.get("chat_id") or ""
            name = (c.get("chat_name") or "").strip() or cid
            count = c.get("message_count") or 0
            ts = c.get("last_ts")
            if ts:
                from datetime import datetime
                ts_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
            else:
                ts_str = "—"
            lines.append(f"{i}. {name} (chat_id={cid}) | last {ts_str} | {count} msg(s)")
        out = f"{len(chats)} Telegram chat(s):\n" + "\n".join(lines)
        out += "\n\nUse read_telegram_chat(chat_id='...') to read a chat."
        return out
