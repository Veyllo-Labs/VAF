"""
Front Office tool allow-list.

When the agent responds to a contact (from_contact), only these tools are available.
Excluded: code execution, update_user_identity, file/workspace changes, coder/librarian agents.
"""

# Tool names that exist in agent.tools when loaded. At runtime the caller should
# intersect with agent.tools.keys() so missing tools do not cause errors.
FRONT_OFFICE_ALLOWED_TOOLS = frozenset({
    "memory_search",
    "memory_save",
    "list_contacts",
    "get_contact",
    "send_whatsapp",
    "send_telegram",
    "send_discord",
    "send_slack",
    "read_whatsapp_chat",
    "find_whatsapp_messages",
    "whatsapp_inbox",
    "mail_inbox",
    "find_mail",
    "read_mail",
    "send_mail",
    "list_email_accounts",
    "mark_mail_answered",
    "web_search",
})
