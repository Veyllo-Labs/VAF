# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Read messages from a WhatsApp chat (like read_mail for email).
"""

from vaf.tools.base import BaseTool


class ReadWhatsAppChatTool(BaseTool):
    """
    Read messages from a WhatsApp chat. Use chat_id from whatsapp_inbox or find_whatsapp_messages.
    """
    name = "read_whatsapp_chat"
    permission_level = "read"
    side_effect_class = "none"
    description = (
        "Read messages from a WhatsApp chat. Use chat_id from whatsapp_inbox or find_whatsapp_messages. "
        "Returns recent messages (in/out) with timestamps."
    )
    parameters = {
        "type": "object",
        "properties": {
            "chat_id": {
                "type": "string",
                "description": "Chat identifier (e.g. +49123456789 from whatsapp_inbox).",
            },
            "limit": {
                "type": "integer",
                "description": "Max messages to return (default 50).",
            },
        },
        "required": ["chat_id"],
    }

    def run(self, **kwargs) -> str:
        username = (kwargs.get("username") or "admin").strip()
        chat_id = (kwargs.get("chat_id") or "").strip()
        limit = min(max(int(kwargs.get("limit") or 50), 1), 200)

        if not chat_id:
            return "chat_id is required (e.g. +49123456789). Use whatsapp_inbox to list chats."

        try:
            from vaf.core.channel_message_store import get_chat_messages
        except ImportError as e:
            return f"WhatsApp store unavailable: {e}"

        messages = get_chat_messages(username, chat_id, limit=limit)
        if not messages:
            return f"No messages found for chat {chat_id}. Messages are stored as they arrive; this chat may not have any yet."

        lines = []
        for m in reversed(messages):
            body = m.get("body") or ""
            direction = m.get("direction") or "in"
            label = "IN" if direction == "in" else "OUT"
            ts = m.get("ts")
            if ts:
                from datetime import datetime
                dt = datetime.fromtimestamp(ts)
                ts_str = dt.strftime("%Y-%m-%d %H:%M")
            else:
                ts_str = "—"
            lines.append(f"[{ts_str}] {label}: {body}")
        out = f"Chat {chat_id} (last {len(messages)} messages):\n" + "\n".join(lines)
        return out
