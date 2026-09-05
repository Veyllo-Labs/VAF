# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Front Office tool allow-list.

When the agent responds to a contact (from_contact), only these tools are available.
Deliberate: a Front Office caller is a third party, and every read tool (inboxes, chat
readers, message search, memory search, the mailbox) reaches the owner's stores through a
free argument, so the one enforceable rule is not to hand them out. What a contact may
learn about their own record reaches the agent through the contact block
(contacts_store.contact_self_view), never through a tool. The platform send tools stay for
the owner back-channel; tests/test_channel_registry_sync.py pins that every platform send
tool is here and send_to_user is not.
"""

# Tool names that exist in agent.tools when loaded. At runtime the caller should
# intersect with agent.tools.keys() so missing tools do not cause errors.
FRONT_OFFICE_ALLOWED_TOOLS = frozenset({
    "send_whatsapp",
    "send_telegram",
    "send_discord",
    "send_slack",
    "web_search",
})
