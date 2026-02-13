"""
Send an email from a connected account. Uses the mail transport layer; credentials are never exposed.
Only available when at least one email account is configured in Settings → Connections → Email.
Supports optional file attachments (e.g. invoices, documents).
"""

from pathlib import Path

from vaf.core.email_transport import send_mail, get_account
from vaf.tools.base import BaseTool
from vaf.tools.filesystem import is_safe_path
from vaf.tools.mail_utils import cred_username_from_kwargs, list_accounts_for_user


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
    When account_id is omitted and only one account is connected, uses that account automatically.
    When sending a document (invoice, contract, PDF), pass attachment_paths with full paths.
    """
    name = "send_mail"
    description = (
        "Send an email from a connected email account. "
        "Use when the user asks to send an email. Pass to, subject, body; account_id is optional. "
        "When account_id is omitted, uses the first connected account. Use list_email_accounts to see connected accounts. "
        "For documents (invoice, contract, PDF), pass attachment_paths with full file paths."
    )
    parameters = {
        "type": "object",
        "properties": {
            "account_id": {
                "type": "string",
                "description": "Optional. Email of the connected account to send from. When omitted, uses the first connected account. Use list_email_accounts to see options.",
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
        "required": ["to", "subject", "body"],
    }

    def run(self, **kwargs) -> str:
        cred_username = cred_username_from_kwargs(kwargs)
        account_id = (kwargs.get("account_id") or "").strip()
        to = (kwargs.get("to") or "").strip()
        subject = (kwargs.get("subject") or "").strip()
        body = (kwargs.get("body") or "").strip()
        attachment_paths = kwargs.get("attachment_paths") or []
        if not isinstance(attachment_paths, list):
            attachment_paths = []

        if not to:
            accounts = list_accounts_for_user(cred_username)
            if not accounts:
                return (
                    "No email accounts connected. The user must add an account in Settings → Connections → Email."
                )
            return f"Pass to, subject, and body. Optionally account_id. Connected accounts: {', '.join(accounts)}."
        if not account_id:
            accounts = list_accounts_for_user(cred_username)
            if not accounts:
                return (
                    "No email accounts connected. The user must add an account in Settings → Connections → Email."
                )
            account_id = accounts[0]
        acc = get_account(account_id, username=cred_username)
        if not acc:
            return f"Account '{account_id}' not found. Connected: {', '.join(list_accounts_for_user(cred_username))}."
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
                username=cred_username,
            )
        except Exception as e:
            return f"Failed to send email: {e}"
        if ok:
            suffix = f" with {len(attachments)} attachment(s)" if attachments else ""
            return f"Email{suffix} sent to {to} from {account_id}."
        return "Failed to send email (check SMTP settings and credentials)."
