"""
Read the full body of one email as plain text (same cleaned output as the Mail UI).
Use after mail_inbox when the user asks what a specific email says. Token-efficient: no HTML, no MIME.
Scoped to the current user in network mode (only that user's connected accounts).
"""

from vaf.core.email_transport import get_account, get_message_body_plain
from vaf.tools.base import BaseTool
from vaf.tools.mail_utils import cred_scope_from_kwargs, cred_username_from_kwargs, list_accounts_for_user


class ReadMailTool(BaseTool):
    """
    Read the full body of one email as plain text. Use when the user asks what an email says or contains.
    Returns the same token-efficient, cleaned text shown in the Mail dashboard (no HTML, no MIME).
    Call mail_inbox first to get message_id and provider_message_id for the message you want.
    """
    name = "read_mail"
    description = (
        "Read the full body of a single email as plain text. "
        "When the user asks 'what does the [Subject] mail say?' (e.g. Postman, Twitch), use the account_id, message_id, and provider_message_id from your recent mail_inbox output for the line with that subject – do NOT ask the user for these. "
        "If you have not listed the inbox yet, call mail_inbox first, then read_mail with the matching row's IDs."
    )
    parameters = {
        "type": "object",
        "properties": {
            "account_id": {
                "type": "string",
                "description": "From the mail_inbox line for this message (e.g. user@gmail.com). Use the account_id from the list, do not ask the user.",
            },
            "message_id": {
                "type": "string",
                "description": "From the mail_inbox line for this message. Match the subject the user asked about and use that line's message_id.",
            },
            "folder": {
                "type": "string",
                "description": "Folder name (default: INBOX).",
            },
            "provider_message_id": {
                "type": "string",
                "description": "Optional. From mail_inbox output; use for Gmail/Microsoft for reliable fetch.",
            },
        },
        "required": ["account_id", "message_id"],
    }

    def run(self, **kwargs) -> str:
        cred_username = cred_username_from_kwargs(kwargs)
        user_scope_id = cred_scope_from_kwargs(kwargs)
        account_id = (kwargs.get("account_id") or "").strip()
        message_id = (kwargs.get("message_id") or "").strip()
        folder = (kwargs.get("folder") or "INBOX").strip()
        provider_message_id = (kwargs.get("provider_message_id") or "").strip() or None
        if not account_id or not message_id:
            return "account_id and message_id are required. Use mail_inbox first to list messages and get their message_id (and provider_message_id for Gmail/Microsoft)."
        acc = get_account(account_id, username=cred_username, user_scope_id=user_scope_id)
        if not acc:
            return f"Account '{account_id}' not found. Connected accounts: {', '.join(list_accounts_for_user(cred_username, user_scope_id=user_scope_id))}."
        body = get_message_body_plain(
            account_id=account_id,
            message_id=message_id,
            folder=folder,
            username=cred_username,
            user_scope_id=user_scope_id,
            provider_message_id=provider_message_id,
        )
        if body is None or not body.strip():
            return "Could not load the message body (message not found or empty)."
        return body.strip()
