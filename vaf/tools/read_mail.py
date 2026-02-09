"""
Read emails from a connected account. Uses the mail transport layer; credentials are never exposed.
Only available when at least one email account is configured in Settings → Connections → Email.
"""

from vaf.core.email_transport import fetch_mail, get_account
from vaf.core.config import Config
from vaf.tools.base import BaseTool


def _list_accounts():
    ec = Config.get("email_config") or {}
    accounts = ec.get("accounts") or []
    return [a.get("email") or a.get("account_id") for a in accounts if a.get("email") or a.get("account_id")]


class ReadMailTool(BaseTool):
    """
    Read recent emails from a connected mailbox. Use when the user asks to check email,
    read inbox, or summarize messages. Requires an email account to be connected in Settings.
    """
    name = "read_mail"
    description = (
        "Read recent emails from a connected email account. "
        "Use when the user asks to check email, read inbox, or summarize messages. "
        "Pass account_id (the email address of the connected account) and optionally folder and max_messages."
    )
    parameters = {
        "type": "object",
        "properties": {
            "account_id": {
                "type": "string",
                "description": "Email address of the connected account (e.g. user@gmail.com). Use list to see connected accounts.",
            },
            "folder": {
                "type": "string",
                "description": "IMAP folder name (default: INBOX).",
            },
            "max_messages": {
                "type": "integer",
                "description": "Maximum number of messages to return (default 50).",
            },
        },
        "required": ["account_id"],
    }

    def run(self, **kwargs) -> str:
        account_id = (kwargs.get("account_id") or "").strip()
        if not account_id:
            accounts = _list_accounts()
            if not accounts:
                return (
                    "No email accounts connected. The user must add an account in Settings → Connections → Email "
                    "(Google, Microsoft, or other IMAP)."
                )
            return f"Connected accounts: {', '.join(accounts)}. Pass one as account_id to read mail."
        acc = get_account(account_id)
        if not acc:
            return f"Account '{account_id}' not found. Connected accounts: {', '.join(_list_accounts())}."
        folder = (kwargs.get("folder") or "INBOX").strip()
        max_messages = int(kwargs.get("max_messages") or 50)
        if max_messages < 1 or max_messages > 200:
            max_messages = 50
        try:
            messages = fetch_mail(account_id, folder=folder, max_messages=max_messages)
        except Exception as e:
            return f"Failed to fetch mail: {e}"
        if not messages:
            return f"No messages in {folder} (or fetch failed)."
        lines = []
        for i, m in enumerate(messages, 1):
            lines.append(f"{i}. From: {m.get('from', '')} | Date: {m.get('date', '')} | Subject: {m.get('subject', '')}")
        return "Recent emails:\n" + "\n".join(lines)
