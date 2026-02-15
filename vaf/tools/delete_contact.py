"""
Delete a contact by contact_id (from list_contacts or get_contact).
When multiple contacts share the same name, always ask the user to confirm which one to delete before calling this tool.
"""

from vaf.tools.base import BaseTool


class DeleteContactTool(BaseTool):
    """
    Delete a contact by contact_id. You must use contact_id from list_contacts or get_contact.
    If get_contact returns multiple contacts with the same name, do NOT guess – tell the user there are multiple contacts with that name, list them (with contact_id and a short label like phone/email), and ask which one to delete. Only call delete_contact with the contact_id the user confirmed.
    """
    name = "delete_contact"
    description = (
        "Delete a contact by contact_id. Required: contact_id (from list_contacts or get_contact). "
        "When multiple contacts have the same name, you must ask the user which one to delete and use the contact_id they confirm – never delete without confirmation when duplicates exist."
    )
    parameters = {
        "type": "object",
        "properties": {
            "contact_id": {"type": "string", "description": "ID of the contact to delete (from list_contacts or get_contact). Required."},
        },
        "required": ["contact_id"],
    }

    def run(self, **kwargs) -> str:
        username = (kwargs.get("username") or "admin").strip()
        contact_id = (kwargs.get("contact_id") or "").strip()
        if not contact_id:
            return "contact_id is required for delete_contact. Use list_contacts or get_contact to get contact_id."

        try:
            from vaf.core.contacts_store import delete_contact
        except ImportError as e:
            return f"Contacts unavailable: {e}"

        if delete_contact(contact_id, username):
            return f"Contact deleted (contact_id: {contact_id})."
        return f"No contact found with contact_id '{contact_id}'. Use list_contacts to see contact_ids."
