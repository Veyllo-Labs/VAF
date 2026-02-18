"""
Get one contact by name from the central contact list.
Returns channel IDs (whatsapp_phone, telegram_user_id, email) and personal file (language, how to address, birthday, notes).
Use the returned channel IDs with read_whatsapp_chat(chat_id=...), find_whatsapp_messages(chat_id=...), find_mail, etc.
"""

from vaf.tools.base import BaseTool


class GetContactTool(BaseTool):
    """
    Get a contact by name from the central contact list.
    Returns channel IDs (whatsapp_phone, telegram_user_id, email) and personal file (preferred_language, how_to_address, birthday, notes).
    Use the contact's whatsapp_phone with read_whatsapp_chat(chat_id=...) or find_whatsapp_messages(chat_id=...) to check if they wrote; use email with find_mail.
    """
    name = "get_contact"
    description = (
        "Get a contact by name from the central contact list. Returns channel IDs (whatsapp_phone, telegram_user_id, email) "
        "and personal file (preferred_language, how_to_address, birthday, notes). "
        "Use for queries like 'has Max written to me?' – get_contact(name='Max') then read_whatsapp_chat(chat_id=contact's whatsapp_phone) or find_mail/find_whatsapp_messages."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Contact name (e.g. 'Max'). Case-insensitive match.",
            },
        },
        "required": ["name"],
    }

    def run(self, **kwargs) -> str:
        username = (kwargs.get("username") or "admin").strip()
        user_scope_id = kwargs.get("user_scope_id")
        name = (kwargs.get("name") or "").strip()
        if not name:
            return "name is required (e.g. get_contact(name='Max'))."

        try:
            from vaf.core.contacts_store import get_contacts_by_name
        except ImportError as e:
            return f"Contacts unavailable: {e}"

        matches = get_contacts_by_name(name, username, user_scope_id=user_scope_id)
        if not matches:
            return f"No contact found with name '{name}'. Use list_contacts to see existing contacts."
        if len(matches) > 1:
            lines = [
                f"Multiple contacts have the name \"{name}\". You must ask the user which one they mean before updating or deleting.",
                "Contacts (use contact_id with update_contact or delete_contact after user confirms):",
            ]
            for c in matches:
                cid = c.get("id") or "(no id)"
                short = []
                for ch in (c.get("channels") or [])[:3]:
                    v = (ch.get("value") or "").strip()
                    if v:
                        short.append(v[:30] + ("..." if len(v) > 30 else ""))
                if not short and c.get("email"):
                    short.append((c.get("email") or "")[:30])
                label = ", ".join(short) if short else "no channels"
                lines.append(f"  - contact_id: {cid} | {label}")
            return "\n".join(lines)

        contact = matches[0]
        parts = [f"Contact: {contact.get('name', '')}", f"contact_id: {contact.get('id', '')} (use for update_contact or delete_contact)"]
        if contact.get("channels"):
            for ch in contact["channels"]:
                t, v = ch.get("type", ""), ch.get("value", "")
                if not v:
                    continue
                if t == "whatsapp" or t == "phone":
                    parts.append(f"WhatsApp/Phone: {v} (use as chat_id in read_whatsapp_chat, find_whatsapp_messages; or as to_phone in send_whatsapp to send them a message)")
                elif t == "telegram":
                    parts.append(f"Telegram: {v}")
                elif t == "email":
                    parts.append(f"Email: {v}")
                elif t == "discord":
                    parts.append(f"Discord: {v}")
        else:
            if contact.get("whatsapp_phone"):
                parts.append(f"WhatsApp: {contact['whatsapp_phone']} (use as chat_id in read_whatsapp_chat, find_whatsapp_messages)")
            if contact.get("telegram_user_id"):
                parts.append(f"Telegram user ID: {contact['telegram_user_id']}")
            if contact.get("telegram_username"):
                parts.append(f"Telegram username: {contact['telegram_username']}")
            if contact.get("email"):
                parts.append(f"Email: {contact['email']}")
        if contact.get("preferred_language"):
            parts.append(f"Preferred language: {contact['preferred_language']} (use for send_whatsapp(voice_lang='...') when sending voice messages to this contact)")
        if contact.get("how_to_address"):
            parts.append(f"How to address: {contact['how_to_address']}")
        if contact.get("birthday"):
            parts.append(f"Birthday: {contact['birthday']}")
        if contact.get("notes"):
            parts.append(f"Notes: {contact['notes']}")
        if contact.get("allow_as_assistant_user"):
            parts.append("Allowed as assistant user: yes")
        return "\n".join(parts)
