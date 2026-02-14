"""
List WhatsApp chats (like mail_inbox for email).
Uses chat list from the bridge; same data as WhatsApp Dashboard.
"""

from vaf.tools.base import BaseTool


class WhatsAppInboxTool(BaseTool):
    """
    List WhatsApp chats (conversations). Uses the same chat list as the WhatsApp Dashboard.
    Call find_whatsapp_messages to search for messages, or read_whatsapp_chat to read a specific chat.
    """
    name = "whatsapp_inbox"
    description = (
        "List WhatsApp chats (conversations). Same list as the WhatsApp Dashboard. "
        "Returns chat_id, name, last_ts. Use find_whatsapp_messages to search by name or text; "
        "use read_whatsapp_chat with chat_id to read messages in a chat."
    )
    parameters = {
        "type": "object",
        "properties": {
            "max_chats": {
                "type": "integer",
                "description": "Maximum number of chats to list (default 50, max 200).",
            },
        },
        "required": [],
    }

    def run(self, **kwargs) -> str:
        username = (kwargs.get("username") or "admin").strip()
        user_scope_id = kwargs.get("user_scope_id")
        max_chats = min(max(int(kwargs.get("max_chats") or 50), 1), 200)

        try:
            from vaf.api.whatsapp_bridge import get_whatsapp_chats, is_bridge_running
        except ImportError as e:
            return f"WhatsApp unavailable: {e}"

        if not is_bridge_running():
            return "WhatsApp bridge not running. Enable it in Settings → Connections → WhatsApp."

        raw_chats = get_whatsapp_chats(username, wait_timeout=3.0)
        if not raw_chats:
            return "No WhatsApp chats found. The chat list may be empty (link WhatsApp and wait for messages)."

        def _jid_to_phone(jid: str) -> str:
            if not jid or not isinstance(jid, str):
                return ""
            if "@lid" in jid or jid.endswith("@broadcast") or jid.endswith("@status"):
                return ""
            part = jid.split("@")[0].split(":")[0].strip()
            if not part or not part.isdigit() or len(part) < 7 or len(part) > 15:
                return ""
            return f"+{part}"

        lines = []
        for i, c in enumerate(raw_chats[:max_chats], 1):
            jid = c.get("jid") or c.get("phone") or ""
            phone = c.get("phone") or _jid_to_phone(jid)
            chat_id = phone if phone and phone.startswith("+") else _jid_to_phone(jid) if jid else jid
            name = c.get("name") or phone or jid
            last_ts = c.get("last_ts") or 0
            if last_ts:
                from datetime import datetime
                dt = datetime.fromtimestamp(last_ts)
                ts_str = dt.strftime("%Y-%m-%d %H:%M")
            else:
                ts_str = "—"
            lines.append(f"{i}. {name} | {chat_id} | last: {ts_str}")
        out = f"WhatsApp chats (first {len(lines)}):\n" + "\n".join(lines)
        out += "\n\nTo search messages, use find_whatsapp_messages(query='...', chat_id=...). To read a chat, use read_whatsapp_chat(chat_id='+49...')."
        return out
