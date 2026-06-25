# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Search the user's synced mailbox by subject or sender (like Ctrl+F).
Use when the user asks "what does the X mail say?" or "details about the [sender/subject] email".
If exactly one match, returns the full body so the agent can answer in one call.
"""

from vaf.core.email_sync_store import search_messages
from vaf.core.email_transport import get_message_body_plain
from vaf.tools.base import BaseTool
from vaf.tools.mail_utils import (
    cred_scope_from_kwargs,
    cred_username_from_kwargs,
    filter_phishing_messages_for_agent,
    store_candidates_for_mail,
    store_scope_from_kwargs,
    store_username_from_kwargs,
)


class FindMailTool(BaseTool):
    """
    Search the user's synced mailbox by subject or sender. Use when the user asks
    "what does the X mail say?" or "more details about the [sender/subject] email".
    Pass a short query (e.g. "Postman", "postman.com"). Returns matching messages with
    account_id, message_id, provider_message_id; if exactly one match, returns the full
    body so you can answer without calling read_mail.
    """
    name = "find_mail"
    permission_level = "read"
    side_effect_class = "none"
    description = (
        "Search the user's synced mailbox by subject or sender. "
        "Use when the user asks 'what does the X mail say?' or 'more details about the [sender/subject] email'. "
        "Pass a short query (e.g. 'Postman', 'postman.com'). Returns matching messages with account_id, message_id, provider_message_id; "
        "if exactly one match, returns the full body so you can answer without calling read_mail."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search term matched against subject and sender (e.g. 'Postman', 'twitch', 'artlist').",
            },
            "folder": {
                "type": "string",
                "description": "Folder to search (default: INBOX).",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of matches to return (default: 10).",
            },
        },
        "required": ["query"],
    }

    def run(self, **kwargs) -> str:
        store_username = store_username_from_kwargs(kwargs)
        cred_username = cred_username_from_kwargs(kwargs)
        user_scope_id = store_scope_from_kwargs(kwargs) or cred_scope_from_kwargs(kwargs)
        query = (kwargs.get("query") or "").strip()
        folder = (kwargs.get("folder") or "INBOX").strip()
        limit = int(kwargs.get("limit") or 10)
        if limit < 1 or limit > 50:
            limit = 10
        if not query:
            return "query is required (e.g. 'Postman' or 'postman.com')."
        # Use same store fallback chain as mail_inbox so we search the same DB as the Mail dashboard
        matches = []
        for try_username, try_scope_id in store_candidates_for_mail(store_username, user_scope_id):
            matches = search_messages(
                query=query,
                folder=folder,
                limit=limit,
                username=try_username,
                user_scope_id=try_scope_id,
            )
            if matches:
                break
        matches, blocked_count = filter_phishing_messages_for_agent(matches)
        if blocked_count and not matches:
            return f"No safe emails matching '{query}' in {folder}. Hidden {blocked_count} suspicious message(s) by phishing filter."
        if not matches:
            return f"No emails matching '{query}' in {folder}. Sync in Settings → Connections → Email if needed."
        lines = []
        for i, m in enumerate(matches, 1):
            acc = m.get("account_id") or ""
            mid = m.get("message_id") or ""
            pid = m.get("provider_message_id") or ""
            lines.append(
                f"{i}. From: {m.get('from', '')} | Date: {m.get('date', '')} | Subject: {m.get('subject', '')} | "
                f"account_id: {acc} | message_id: {mid} | provider_message_id: {pid}"
            )
        out = f"Found {len(matches)} safe match(es) for '{query}':\n" + "\n".join(lines)
        if blocked_count:
            out += f"\n\n(Security) Hidden {blocked_count} suspicious message(s) by phishing filter."
        if len(matches) == 1:
            m = matches[0]
            body = get_message_body_plain(
                account_id=m.get("account_id") or "",
                message_id=m.get("message_id") or "",
                folder=m.get("folder") or "INBOX",
                username=cred_username,
                user_scope_id=user_scope_id,
                provider_message_id=(m.get("provider_message_id") or "").strip() or None,
            )
            if body and body.strip():
                out += "\n\n--- Full body ---\n" + body.strip()
            else:
                out += "\n\n(Could not load full body; use read_mail with the account_id, message_id, provider_message_id above.)"
        else:
            out += "\n\nTo read the full body of one, use read_mail with account_id, message_id, provider_message_id from the list above."
        return out
