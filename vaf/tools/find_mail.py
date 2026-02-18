"""
Search the user's synced mailbox by subject or sender (like Ctrl+F).
Use when the user asks "what does the X mail say?" or "details about the [sender/subject] email".
If exactly one match, returns the full body so the agent can answer in one call.
"""

from vaf.core.email_sync_store import search_messages
from vaf.core.email_transport import get_message_body_plain
from vaf.tools.base import BaseTool
from vaf.tools.mail_utils import cred_scope_from_kwargs, cred_username_from_kwargs, store_scope_from_kwargs, store_username_from_kwargs


class FindMailTool(BaseTool):
    """
    Search the user's synced mailbox by subject or sender. Use when the user asks
    "what does the X mail say?" or "more details about the [sender/subject] email".
    Pass a short query (e.g. "Postman", "postman.com"). Returns matching messages with
    account_id, message_id, provider_message_id; if exactly one match, returns the full
    body so you can answer without calling read_mail.
    """
    name = "find_mail"
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
        matches = search_messages(
            query=query,
            folder=folder,
            limit=limit,
            username=store_username,
            user_scope_id=user_scope_id,
        )
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
        out = f"Found {len(matches)} match(es) for '{query}':\n" + "\n".join(lines)
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
