# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Front Office tool allow-list.

When the agent responds to a contact (from_contact), only these tools are available.
Deliberate: a Front Office caller is a third party, and every read tool that takes a chat,
a name, an address or a query (inboxes, chat readers, message search, memory search, the
mailbox) reaches the owner's stores through that free argument, so the one enforceable
rule is not to hand them out. The one read tool here, contact_history, takes no such
argument: the headless runner pins the person being answered on the agent and the tool
reads that person alone. What a contact may learn about their own record reaches the
agent through the contact block (contacts_store.contact_self_view), never through a tool. The platform send tools stay for
the owner back-channel; tests/test_channel_registry_sync.py pins that every platform send
tool is here and send_to_user is not.
"""

from vaf.core.channels import ALL_SEND_TOOLS

# Tool names that exist in agent.tools when loaded. At runtime the caller should
# intersect with agent.tools.keys() so missing tools do not cause errors.
FRONT_OFFICE_ALLOWED_TOOLS = frozenset(ALL_SEND_TOOLS) | frozenset({
    "web_search",
    # The one read tool with no free argument: it reads the correspondence of the contact
    # the runner pinned on the agent for this turn (agent._front_office_contact), across
    # every channel, and nothing else (vaf/tools/contact_history.py).
    "contact_history",
})
