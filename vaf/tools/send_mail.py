"""
Send an email from a connected account. Uses the mail transport layer; credentials are never exposed.
Only available when at least one email account is configured in Settings → Connections → Email.
Supports optional file attachments (e.g. invoices, documents).
"""

from pathlib import Path

from vaf.core.email_transport import send_mail, get_account
from vaf.core.config import Config
from vaf.tools.base import BaseTool
from vaf.tools.filesystem import is_safe_path


def _list_accounts():
    ec = Config.get("email_config") or {}
    accounts = ec.get("accounts") or []
    return [a.get("email") or a.get("account_id") for a in accounts if a.get("email") or a.get("account_id")]


def _resolve_path(path_str: str) -> tuple[Path | None, str | None]:
    """Resolve file path (supports file:// URLs, folder aliases like Downloads).
    Returns (resolved_path, error_message). Exactly one is None."""
    s = (path_str or "").strip()
    if not s:
        return None, None
    if s.lower().startswith("file://"):
        s = s[7:]
    safe, result = is_safe_path(s)
    if not safe:
        return None, result  # result = error message
    return Path(result), None


class SendMailTool(BaseTool):
    """
    Send an email from a connected account. Use when the user asks to send an email.
    When sending a document (invoice, contract, PDF), pass attachment_paths with full paths.
    """
    name = "send_mail"
    description = (
        "Send an email from a connected email account. "
        "Use when the user asks to send an email. Pass account_id, to, subject, body. "
        "For documents (invoice, contract, PDF), pass attachment_paths with full file paths."
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
            "attachment_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional. Full paths to files to attach (e.g. invoice PDF, contract).",
            },
        },
        "required": ["account_id", "to", "subject", "body"],
    }

    def run(self, **kwargs) -> str:
        account_id = (kwargs.get("account_id") or "").strip()
        to = (kwargs.get("to") or "").strip()
        subject = (kwargs.get("subject") or "").strip()
        body = (kwargs.get("body") or "").strip()
        attachment_paths = kwargs.get("attachment_paths") or []
        if not isinstance(attachment_paths, list):
            attachment_paths = []

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

        attachments = []
        for p in attachment_paths:
            if not p:
                continue
            resolved, path_error = _resolve_path(str(p))
            if path_error:
                return path_error
            if resolved and resolved.is_file():
                attachments.append({"path": str(resolved), "filename": resolved.name})

        try:
            ok = send_mail(
                account_id,
                to=to,
                subject=subject,
                body=body or "",
                attachments=attachments if attachments else None,
            )
        except Exception as e:
            return f"Failed to send email: {e}"
        if ok:
            suffix = f" with {len(attachments)} attachment(s)" if attachments else ""
            return f"Email{suffix} sent to {to} from {account_id}."
        return "Failed to send email (check SMTP settings and credentials)."
