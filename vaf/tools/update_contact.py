# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
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
    category    = "contacts"
    identity_kwargs = ("user_scope_id", "username")
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
            "status": {"type": "string", "description": "Relationship status, a free label; the usual ones are lead, in_contact, customer, archived."},
            "add_note": {"type": "string", "description": "Append a dated note to the contact's file (e.g. 'interested in feature X', 'follow up next week'). Use this for anything worth remembering about the person; it is kept with the date."},
            "add_event_title": {"type": "string", "description": "Attach a dated event to the contact (e.g. 'Meeting'). Requires add_event_when."},
            "add_event_when": {"type": "string", "description": "When the event is, as YYYY-MM-DD HH:MM in the user's timezone or ISO 8601."},
        },
        "required": ["contact_id"],
    }

    def run(self, **kwargs) -> str:
        username = (kwargs.get("username") or "admin").strip()
        contact_id = (kwargs.get("contact_id") or "").strip()
        if not contact_id:
            return "contact_id is required for update_contact. Use list_contacts or get_contact to get contact_id."

        user_scope_id = kwargs.get("user_scope_id")
        try:
            from vaf.core.contacts_store import add_contact_event, add_contact_note, get_contact_by_id, update_contact
        except ImportError as e:
            return f"Contacts unavailable: {e}"

        updates = {}
        for key in ("name", "email", "whatsapp_phone", "telegram_username", "preferred_language", "how_to_address", "birthday", "notes", "allow_as_assistant_user", "status"):
            if key in kwargs:
                v = kwargs[key]
                if key == "allow_as_assistant_user":
                    updates[key] = bool(v)
                elif v is not None and isinstance(v, str) and v.strip():
                    updates[key] = v.strip()
                elif v is not None:
                    updates[key] = v
        note_text = (kwargs.get("add_note") or "").strip() if isinstance(kwargs.get("add_note"), str) else ""
        event_title = (kwargs.get("add_event_title") or "").strip() if isinstance(kwargs.get("add_event_title"), str) else ""
        event_when = (kwargs.get("add_event_when") or "").strip() if isinstance(kwargs.get("add_event_when"), str) else ""

        if not updates and not note_text and not event_title:
            return "No fields to update. Provide at least one of: name, email, whatsapp_phone, telegram_username, preferred_language, how_to_address, birthday, notes, allow_as_assistant_user, status, add_note, add_event_title + add_event_when."

        done = []
        contact = get_contact_by_id(contact_id, username, user_scope_id=user_scope_id)
        if not contact:
            return f"No contact found with contact_id '{contact_id}'. Use list_contacts to see contact_ids."
        if updates:
            contact = update_contact(contact_id, username, user_scope_id=user_scope_id, **updates) or contact
            done.append("fields " + ", ".join(sorted(updates)))
        if note_text:
            if add_contact_note(contact_id, note_text, username, user_scope_id=user_scope_id, source="agent"):
                done.append("note added")
        if event_title:
            if not event_when:
                return "add_event_when is required with add_event_title (YYYY-MM-DD HH:MM in the user's timezone, or ISO 8601)."
            when_ts = self._parse_when(event_when, username)
            if when_ts is None:
                return "add_event_when must be YYYY-MM-DD HH:MM (user's timezone) or ISO 8601."
            if add_contact_event(contact_id, event_title, when_ts, username, user_scope_id=user_scope_id, source="agent"):
                done.append("event added")
        return f"Contact updated: {contact.get('name', '')} (contact_id: {contact_id}); " + "; ".join(done) + "."

    @staticmethod
    def _parse_when(when: str, username: str):
        from datetime import datetime
        try:
            dt = datetime.fromisoformat(when.replace("Z", "+00:00"))
        except ValueError:
            try:
                dt = datetime.strptime(when, "%Y-%m-%d %H:%M")
            except ValueError:
                return None
        if dt.tzinfo is None:
            try:
                from vaf.core.user_time import resolve_user_timezone
                tz = resolve_user_timezone(username)
                if tz is not None:
                    dt = dt.replace(tzinfo=tz)
            except Exception:
                pass
        return dt.timestamp()
