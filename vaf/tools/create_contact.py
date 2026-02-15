"""
Create a contact in the central contact list (Settings → Connections → Contacts).
Use update_contact(contact_id, ...) to change later; use contact_id from list_contacts or get_contact.
"""

from vaf.tools.base import BaseTool


class CreateContactTool(BaseTool):
    """
    Create a new contact. Requires name; optional channels (phone/email/telegram) and personal file fields.
    If multiple contacts already have the same name, creating another is allowed; use contact_id to disambiguate for update/delete.
    """
    name = "create_contact"
    description = (
        "Create a contact in the central contact list. Required: name. Optional: email, whatsapp_phone, telegram_username, "
        "preferred_language, how_to_address, birthday, notes, allow_as_assistant_user. "
        "Returns the new contact with contact_id (use for update_contact or delete_contact)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Display name of the contact (required)."},
            "email": {"type": "string", "description": "Email address."},
            "whatsapp_phone": {"type": "string", "description": "Phone number for WhatsApp (e.g. +491234567890)."},
            "telegram_username": {"type": "string", "description": "Telegram username or user ID."},
            "preferred_language": {"type": "string", "description": "e.g. de, en."},
            "how_to_address": {"type": "string", "description": "e.g. du, Sie, first name only."},
            "birthday": {"type": "string", "description": "MM-DD or ISO date."},
            "notes": {"type": "string", "description": "Free-form notes."},
            "allow_as_assistant_user": {"type": "boolean", "description": "If true, this contact can reach your assistant (front office)."},
        },
        "required": ["name"],
    }

    def run(self, **kwargs) -> str:
        username = (kwargs.get("username") or "admin").strip()
        name = (kwargs.get("name") or "").strip()
        if not name:
            return "name is required for create_contact."

        try:
            from vaf.core.contacts_store import create_contact
        except ImportError as e:
            return f"Contacts unavailable: {e}"

        contact = create_contact(
            name,
            username,
            email=(kwargs.get("email") or "").strip() or None,
            whatsapp_phone=(kwargs.get("whatsapp_phone") or "").strip() or None,
            telegram_username=(kwargs.get("telegram_username") or "").strip() or None,
            preferred_language=(kwargs.get("preferred_language") or "").strip() or None,
            how_to_address=(kwargs.get("how_to_address") or "").strip() or None,
            birthday=(kwargs.get("birthday") or "").strip() or None,
            notes=(kwargs.get("notes") or "").strip() or None,
            allow_as_assistant_user=bool(kwargs.get("allow_as_assistant_user", False)),
        )
        cid = contact.get("id") or ""
        return f"Contact created: {contact.get('name', '')} | contact_id: {cid}. Use update_contact(contact_id='{cid}', ...) or delete_contact(contact_id='{cid}') to modify or remove."
