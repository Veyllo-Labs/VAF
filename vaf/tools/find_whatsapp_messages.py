# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Search WhatsApp messages by query (like find_mail for email).
"""

from vaf.tools.base import BaseTool


class FindWhatsAppMessagesTool(BaseTool):
    """
    Search WhatsApp messages by query (matches body, chat name, sender).
    Use when the user asks 'find messages from Alice' or 'what did X say in WhatsApp'.
    """
    name = "find_whatsapp_messages"
    permission_level = "read"
    side_effect_class = "none"
    description = (
        "Search WhatsApp messages by query. Matches message body, chat name, and sender. "
        "Use when user asks 'find messages from Alice', 'what did X say in WhatsApp', etc. "
        "Optional chat_id to limit search to one chat. Returns matches with chat_id; use read_whatsapp_chat to get full thread."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search term (e.g. 'Alice', 'meeting', 'tomorrow').",
            },
            "chat_id": {
                "type": "string",
                "description": "Optional. Limit search to this chat (e.g. +49123456789).",
            },
            "limit": {
                "type": "integer",
                "description": "Max matches to return (default 20).",
            },
        },
        "required": ["query"],
    }

    def run(self, **kwargs) -> str:
        username = (kwargs.get("username") or "admin").strip()
        query = (kwargs.get("query") or "").strip()
        chat_id = (kwargs.get("chat_id") or "").strip() or None
        limit = min(max(int(kwargs.get("limit") or 20), 1), 100)

        if not query:
            return "query is required (e.g. 'Alice' or 'meeting')."

        try:
            from vaf.core.whatsapp_message_store import search_messages
        except ImportError as e:
            return f"WhatsApp store unavailable: {e}"

        matches = search_messages(username, query, chat_id=chat_id, limit=limit)
        if not matches:
            scope = f" in chat {chat_id}" if chat_id else ""
            return f"No WhatsApp messages matching '{query}'{scope}. Messages are stored as they arrive; older chats may not be indexed yet."

        lines = []
        for i, m in enumerate(matches, 1):
            chat = m.get("chat_id") or ""
            name = m.get("chat_name") or chat
            body = (m.get("body") or "")[:100]
            if len((m.get("body") or "")) > 100:
                body += "..."
            direction = m.get("direction") or "in"
            arrow = "←" if direction == "in" else "→"
            ts = m.get("ts")
            if ts:
                from datetime import datetime
                dt = datetime.fromtimestamp(ts)
                ts_str = dt.strftime("%Y-%m-%d %H:%M")
            else:
                ts_str = "—"
            lines.append(f"{i}. [{arrow}] {name} | {ts_str} | {body}")
        out = f"Found {len(matches)} match(es) for '{query}':\n" + "\n".join(lines)
        out += "\n\nTo read full chat, use read_whatsapp_chat(chat_id='...') with the chat_id from the list."
        return out
