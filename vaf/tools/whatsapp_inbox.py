# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
List WhatsApp chats (like mail_inbox for email).
Uses bridge chat list plus persistent message store so all chats with messages appear (sync = live store).
"""

from vaf.tools.base import BaseTool


class WhatsAppInboxTool(BaseTool):
    """
    List WhatsApp chats (inbox). Same data as the WhatsApp Dashboard: bridge list plus chats from the message store.
    Every received/sent message is stored; chats that have messages appear here even after a bridge reconnect.
    Call find_whatsapp_messages to search, or read_whatsapp_chat to read a chat.
    """
    name = "whatsapp_inbox"
    permission_level = "read"
    side_effect_class = "none"
    description = (
        "List WhatsApp chats (inbox). Bridge list plus all chats that have messages in the store (like mail/Telegram). "
        "Returns chat_id, name, last_ts. Use find_whatsapp_messages to search; read_whatsapp_chat(chat_id=...) to read."
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
            from vaf.core.whatsapp_message_store import list_chats_from_store
        except ImportError as e:
            return f"WhatsApp unavailable: {e}"

        if not is_bridge_running():
            return "WhatsApp bridge not running. Enable it in Settings → Connections → WhatsApp."

        def _jid_to_phone(jid: str) -> str:
            if not jid or not isinstance(jid, str):
                return ""
            if "@lid" in jid or jid.endswith("@broadcast") or jid.endswith("@status"):
                return ""
            part = jid.split("@")[0].split(":")[0].strip()
            if not part or not part.isdigit() or len(part) < 7 or len(part) > 15:
                return ""
            return f"+{part}"

        # Merge bridge chats + store chats (by chat_id), keep latest last_ts and best name
        by_cid: dict = {}
        raw_chats = get_whatsapp_chats(username, wait_timeout=3.0) or []
        for c in raw_chats:
            jid = c.get("jid") or c.get("phone") or ""
            phone = c.get("phone") or _jid_to_phone(jid)
            chat_id = phone if phone and phone.startswith("+") else _jid_to_phone(jid) if jid else jid
            if not chat_id and str(jid).endswith("@lid"):
                chat_id = str(jid)
            if not chat_id:
                continue
            last_ts = int(c.get("last_ts") or 0)
            name = (c.get("name") or "").strip() or phone or jid
            if chat_id not in by_cid or last_ts > (by_cid[chat_id].get("last_ts") or 0):
                by_cid[chat_id] = {"chat_id": chat_id, "name": name, "last_ts": last_ts}
        for row in list_chats_from_store(username, limit=500, user_scope_id=user_scope_id):
            cid = (row.get("chat_id") or "").strip()
            if not cid:
                continue
            last_ts = int(row.get("last_ts") or 0)
            name = (row.get("chat_name") or "").strip() or cid
            if cid not in by_cid or last_ts > (by_cid[cid].get("last_ts") or 0):
                by_cid[cid] = {"chat_id": cid, "name": name or cid, "last_ts": last_ts}
            elif not (by_cid[cid].get("name") or "").strip() and name:
                by_cid[cid]["name"] = name
        chats = sorted(by_cid.values(), key=lambda x: -(x.get("last_ts") or 0))[:max_chats]

        if not chats:
            return "No WhatsApp chats found. Link WhatsApp, wait for messages, or check Settings → Connections."

        from datetime import datetime
        lines = []
        for i, item in enumerate(chats, 1):
            name = item.get("name") or item.get("chat_id") or "—"
            last_ts = item.get("last_ts") or 0
            ts_str = datetime.fromtimestamp(last_ts).strftime("%Y-%m-%d %H:%M") if last_ts else "—"
            lines.append(f"{i}. {name} | {item.get('chat_id')} | last: {ts_str}")
        out = f"WhatsApp inbox ({len(lines)} chats):\n" + "\n".join(lines)
        out += "\n\nTo search: find_whatsapp_messages(query='...'). To read: read_whatsapp_chat(chat_id='+49...')."
        return out
