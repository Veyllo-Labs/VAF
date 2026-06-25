# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
List contacts from the central contact list (Settings → Connections → Contacts).
Returns contact names and which channels they have (WhatsApp, Telegram, email).
Use get_contact(name) to get full details including personal file (language, how to address, etc.).
"""

from vaf.tools.base import BaseTool


class ListContactsTool(BaseTool):
    """
    List all contacts from the central contact list.
    Returns name and which channels each contact has (WhatsApp, Telegram, email).
    Use get_contact(name) to get full details (e.g. for 'has Max written to me?' then read_whatsapp_chat(chat_id=contact's whatsapp_phone)).
    """
    name = "list_contacts"
    permission_level = "read"
    side_effect_class = "none"
    description = (
        "List all contacts from the central contact list (Settings → Connections → Contacts). "
        "Returns each contact's name and which channels they have (WhatsApp, Telegram, email). "
        "Use get_contact(name) to get full details and channel IDs for reading messages or sending."
    )
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def run(self, **kwargs) -> str:
        username = (kwargs.get("username") or "admin").strip()
        user_scope_id = kwargs.get("user_scope_id")
        try:
            from vaf.core.contacts_store import list_contacts
        except ImportError as e:
            return f"Contacts unavailable: {e}"

        contacts = list_contacts(username, user_scope_id=user_scope_id)
        if not contacts:
            return "No contacts yet. Add contacts in Settings → Connections → Contacts."

        lines = []
        for i, c in enumerate(contacts, 1):
            name = (c.get("name") or "").strip() or "(no name)"
            cid = c.get("id") or ""
            if c.get("channels"):
                types = sorted({ch["type"].capitalize() for ch in c["channels"] if ch.get("type")})
                ch = ", ".join(types) + (f" ({len(c['channels'])} entries)" if len(c["channels"]) > len(types) else "")
            else:
                channels = []
                if c.get("whatsapp_phone"):
                    channels.append("WhatsApp")
                if c.get("telegram_username") or c.get("telegram_user_id"):
                    channels.append("Telegram")
                if c.get("email"):
                    channels.append("Email")
                ch = ", ".join(channels) if channels else "—"
            lines.append(f"{i}. {name} | contact_id: {cid} | Channels: {ch}")
        return "Contacts:\n" + "\n".join(lines) + "\n\nUse get_contact(name=\"...\") for full details. For update_contact or delete_contact use contact_id; if multiple contacts share a name, always ask the user which one they mean."
