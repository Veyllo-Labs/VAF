"""
Read the full body of one email as plain text (same cleaned output as the Mail UI).
Use after mail_inbox when the user asks what a specific email says. Token-efficient: no HTML, no MIME.
"""

from vaf.core.email_transport import get_account, get_message_body_plain
from vaf.core.config import Config
from vaf.tools.base import BaseTool


def _list_accounts():
    ec = Config.get("email_config") or {}
    accounts = ec.get("accounts") or []
    return [a.get("email") or a.get("account_id") for a in accounts if a.get("email") or a.get("account_id")]


class ReadMailTool(BaseTool):
    """
    Read the full body of one email as plain text. Use when the user asks what an email says or contains.
    Returns the same token-efficient, cleaned text shown in the Mail dashboard (no HTML, no MIME).
    Call mail_inbox first to get message_id and provider_message_id for the message you want.
    """
    name = "read_mail"
    description = (
        "Read the full body of a single email as plain text. "
        "Use when the user asks what an email says or what is in a specific message. "
        "Returns cleaned text only (no HTML). Call mail_inbox first to get message_id and provider_message_id."
    )
    parameters = {
        "type": "object",
        "properties": {
            "account_id": {
                "type": "string",
                "description": "Email address of the connected account (e.g. user@gmail.com).",
            },
            "message_id": {
                "type": "string",
                "description": "Message ID from mail_inbox output (e.g. Message-ID header or provider id).",
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
        account_id = (kwargs.get("account_id") or "").strip()
        message_id = (kwargs.get("message_id") or "").strip()
        folder = (kwargs.get("folder") or "INBOX").strip()
        provider_message_id = (kwargs.get("provider_message_id") or "").strip() or None
        if not account_id or not message_id:
            return "account_id and message_id are required. Use mail_inbox first to list messages and get their message_id (and provider_message_id for Gmail/Microsoft)."
        acc = get_account(account_id)
        if not acc:
            return f"Account '{account_id}' not found. Connected accounts: {', '.join(_list_accounts())}."
        body = get_message_body_plain(
            account_id=account_id,
            message_id=message_id,
            folder=folder,
            username=None,
            provider_message_id=provider_message_id,
        )
        if body is None or not body.strip():
            return "Could not load the message body (message not found or empty)."
        return body.strip()
