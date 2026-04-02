"""
Update a contact by contact_id (from list_contacts or get_contact).
When multiple contacts share the same name, always use contact_id and confirm with the user which contact they mean.
"""

from vaf.tools.base import BaseTool


class UpdateContactTool(BaseTool):
    """
    Update an existing contact by contact_id. Only provided fields are updated.
    You must use contact_id (from list_contacts or get_contact). If get_contact returns multiple contacts with the same name, do NOT guess – ask the user which one to update and use the contact_id they confirm.
    """
    name = "update_contact"
    permission_level = "write"
    side_effect_class = "reversible"
    description = (
        "Update a contact by contact_id. Required: contact_id (from list_contacts or get_contact). "
        "Optional: name, email, whatsapp_phone, telegram_username, preferred_language, how_to_address, birthday, notes, allow_as_assistant_user. "
        "When multiple contacts have the same name, always ask the user which one they mean before updating."
    )
    parameters = {
        "type": "object",
        "properties": {
            "contact_id": {"type": "string", "description": "ID of the contact (from list_contacts or get_contact). Required."},
            "name": {"type": "string", "description": "New display name."},
            "email": {"type": "string", "description": "Email address."},
            "whatsapp_phone": {"type": "string", "description": "Phone for WhatsApp."},
            "telegram_username": {"type": "string", "description": "Telegram username or ID."},
            "preferred_language": {"type": "string", "description": "e.g. de, en."},
            "how_to_address": {"type": "string", "description": "e.g. du, Sie."},
            "birthday": {"type": "string", "description": "MM-DD or ISO date."},
            "notes": {"type": "string", "description": "Free-form notes."},
            "allow_as_assistant_user": {"type": "boolean", "description": "Can reach your assistant (front office)."},
        },
        "required": ["contact_id"],
    }

    def run(self, **kwargs) -> str:
        username = (kwargs.get("username") or "admin").strip()
        contact_id = (kwargs.get("contact_id") or "").strip()
        if not contact_id:
            return "contact_id is required for update_contact. Use list_contacts or get_contact to get contact_id."

        try:
            from vaf.core.contacts_store import update_contact
        except ImportError as e:
            return f"Contacts unavailable: {e}"

        updates = {}
        for key in ("name", "email", "whatsapp_phone", "telegram_username", "preferred_language", "how_to_address", "birthday", "notes", "allow_as_assistant_user"):
            if key in kwargs:
                v = kwargs[key]
                if key == "allow_as_assistant_user":
                    updates[key] = bool(v)
                elif v is not None and isinstance(v, str) and v.strip():
                    updates[key] = v.strip()
                elif v is not None:
                    updates[key] = v

        if not updates:
            return "No fields to update. Provide at least one of: name, email, whatsapp_phone, telegram_username, preferred_language, how_to_address, birthday, notes, allow_as_assistant_user."

        contact = update_contact(contact_id, username, **updates)
        if not contact:
            return f"No contact found with contact_id '{contact_id}'. Use list_contacts to see contact_ids."
        return f"Contact updated: {contact.get('name', '')} (contact_id: {contact_id})."
