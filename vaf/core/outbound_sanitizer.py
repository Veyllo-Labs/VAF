# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The internal-phrase net for outgoing channel messages, in a neutral home.

This lived as a private function inside the headless runner, and four send TOOLS
(telegram, whatsapp, discord, send_to_user) imported it from there - the layer VAF
offers to third parties for extension depended on a private symbol of the product's
worker loop. Moving the net here inverts nothing and changes no behavior; it makes the
dependency point the right way: tools depend on framework modules, the runner's own
outbound chain (_prepare_channel_outbound, the CoT-prefix guard, the WORKFLOW_ASYNC
strip) stays in the runner and calls the same net.

Stdlib-only on purpose: a send tool must be importable on the slim base.
"""
import logging

# Phrases that must never appear in messages sent to contacts via Telegram/WhatsApp/
# Discord. If any of these are found in the outgoing text, the message is blocked.
_INTERNAL_PHRASES = [
    "[SYSTEM_LOG_ONLY]",
    "[FRONT OFFICE",
    "[TOOL BLOCKED]",
    "MESSAGE FROM A CONTACT",
    "NOT FROM THE ACCOUNT OWNER",
    "API returned empty responses",
    "Do NOT report to the account owner",
    "Do NOT repeat or echo the contact",
    "REPLY IN:",
    "Contact details (use Language",
    "contact preferred_language",
]


def sanitize_outgoing_message(text: str) -> str:
    """
    Safety net: strip internal system phrases from outgoing messages before sending
    to external channels (Telegram/WhatsApp/Discord). If the entire message is just
    internal content, return empty string.
    """
    if not text or not text.strip():
        return ""
    # Check if any internal phrase is present
    text_lower = text.lower()
    for phrase in _INTERNAL_PHRASES:
        if phrase.lower() in text_lower:
            # Try to extract just the agent's actual response by removing the contaminated block.
            # If the FRONT OFFICE prompt leaked, it's typically at the start or end - drop the whole thing.
            logging.getLogger(__name__).warning(
                "SANITIZE: blocked internal phrase %r in outgoing message (len=%d)", phrase, len(text)
            )
            return ""
    return text
