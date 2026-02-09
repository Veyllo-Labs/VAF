"""
Send an email from a connected account. Uses the mail transport layer; credentials are never exposed.
Only available when at least one email account is configured in Settings → Connections → Email.
"""

from vaf.core.email_transport import send_mail, get_account
from vaf.core.config import Config
from vaf.tools.base import BaseTool


def _list_accounts():
    ec = Config.get("email_config") or {}
    accounts = ec.get("accounts") or []
    return [a.get("email") or a.get("account_id") for a in accounts if a.get("email") or a.get("account_id")]


class SendMailTool(BaseTool):
    """
    Send an email from a connected account. Use when the user asks to send an email.
    Requires an email account to be connected in Settings → Connections → Email.
    """
    name = "send_mail"
    description = (
        "Send an email from a connected email account. "
        "Use when the user asks to send an email. Pass account_id (sender email), to, subject, and body."
    )
    parameters = {
        "type": "object",
        "properties": {
            "account_id": {
                "type": "string",
                "description": "Email address of the connected account to send from (e.g. user@gmail.com).",
            },
            "to": {
                "type": "string",
                "description": "Recipient email address.",
            },
            "subject": {
                "type": "string",
                "description": "Email subject line.",
            },
            "body": {
                "type": "string",
                "description": "Email body (plain text).",
            },
        },
        "required": ["account_id", "to", "subject", "body"],
    }

    def run(self, **kwargs) -> str:
        account_id = (kwargs.get("account_id") or "").strip()
        to = (kwargs.get("to") or "").strip()
        subject = (kwargs.get("subject") or "").strip()
        body = (kwargs.get("body") or "").strip()
        if not account_id or not to:
            accounts = _list_accounts()
            if not accounts:
                return (
                    "No email accounts connected. The user must add an account in Settings → Connections → Email."
                )
            return f"Pass account_id (e.g. {accounts[0]}), to, subject, and body. Connected accounts: {', '.join(accounts)}."
        acc = get_account(account_id)
        if not acc:
            return f"Account '{account_id}' not found. Connected: {', '.join(_list_accounts())}."
        if not subject:
            subject = "(No subject)"
        try:
            ok = send_mail(account_id, to=to, subject=subject, body=body or "")
        except Exception as e:
            return f"Failed to send email: {e}"
        if ok:
            return f"Email sent to {to} from {account_id}."
        return "Failed to send email (check SMTP settings and credentials)."
