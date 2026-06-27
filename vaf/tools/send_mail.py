# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Send an email from a connected account. Uses the mail transport layer; credentials are never exposed.
Only available when at least one email account is configured in Settings → Connections → Email.
Supports optional file attachments (e.g. invoices, documents).
"""

import re
from email.utils import parseaddr
from pathlib import Path

from vaf.core.config import Config
from vaf.core.email_transport import send_mail, get_account
from vaf.tools.base import BaseTool
from vaf.tools.filesystem import is_safe_path
from vaf.tools.mail_utils import cred_scope_from_kwargs, cred_username_from_kwargs, list_accounts_for_user


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


_FREE_MAIL_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "outlook.com",
    "hotmail.com",
    "live.com",
    "yahoo.com",
    "icloud.com",
    "gmx.de",
    "gmx.net",
    "mail.com",
    "proton.me",
    "protonmail.com",
}

_EXEC_IMPERSONATION_WORDS = (
    "ceo",
    "cfo",
    "finance",
    "accounts payable",
    "buchhaltung",
    "geschaeftsfuehrung",
    "geschäftsführung",
    "director",
    "vorstand",
)

_HIGH_RISK_REQUEST_WORDS = (
    "urgent",
    "dringend",
    "immediately",
    "sofort",
    "wire transfer",
    "bank transfer",
    "überweisung",
    "gift card",
    "amazon card",
    "credentials",
    "passwort",
    "password",
    "api key",
    "secret",
    "bank details",
    "account number",
)


def _domain_from_address(address_or_header: str) -> str:
    _, addr = parseaddr(address_or_header or "")
    if "@" not in addr:
        return ""
    return addr.rsplit("@", 1)[-1].strip().lower()


def _high_risk_send_reasons(to: str, subject: str, body: str, attachments: list[dict]) -> list[str]:
    reasons: list[str] = []
    to_domain = _domain_from_address(to)
    text = f"{subject}\n{body}".lower()

    trusted_domains_cfg = Config.get("email_agent_trusted_sender_domains") or []
    trusted_domains = {str(x).strip().lower() for x in trusted_domains_cfg if str(x).strip()}

    if to_domain and to_domain not in trusted_domains and to_domain in _FREE_MAIL_DOMAINS:
        if any(word in text for word in _EXEC_IMPERSONATION_WORDS):
            reasons.append("possible_exec_impersonation_to_free_mail_domain")

    if any(word in text for word in _HIGH_RISK_REQUEST_WORDS):
        reasons.append("high_risk_request_language_detected")

    if attachments and any(
        token in text for token in ("send", "forward", "share", "daten", "export", "credentials", "secret")
    ):
        reasons.append("attachment_exfiltration_pattern")

    if re.search(r"\b(asap|immediate action required|confidential transfer)\b", text):
        reasons.append("coercive_urgency_pattern")

    return reasons


class SendMailTool(BaseTool):
    """
    Send an email from a connected account. Use when the user asks to send an email.
    When account_id is omitted and only one account is connected, uses that account automatically.
    When sending a document (invoice, contract, PDF), pass attachment_paths with full paths.
    """
    name = "send_mail"
    permission_level = "write"
    side_effect_class = "irreversible"
    description = (
        "Send an email from a connected email account. "
        "Use when the user asks to send an email. Pass to, subject, body; account_id is optional. "
        "When account_id is omitted, uses the first connected account. Use list_email_accounts to see connected accounts. "
        "For documents (invoice, contract, PDF), pass attachment_paths with full file paths."
    )
    input_examples = [
        {"to": "max@example.com", "subject": "Meeting tomorrow", "body": "Hi Max, are you free at 10am?"},
        {"to": "client@example.com", "subject": "Invoice Q1", "body": "Please find the invoice attached.",
         "attachment_paths": ["/home/user/invoices/q1_2025.pdf"]},
    ]
    parameters = {
        "type": "object",
        "properties": {
            "account_id": {
                "type": "string",
                "description": "Optional. Email of the connected account to send from. When omitted, uses the first connected account. Use list_email_accounts to see options.",
            },
            "to": {
                "type": "string",
                "description": "Recipient email address(es). Multiple allowed, comma-separated.",
            },
            "cc": {
                "type": "string",
                "description": "Optional. Cc recipient address(es), comma-separated.",
            },
            "bcc": {
                "type": "string",
                "description": "Optional. Bcc recipient address(es), comma-separated.",
            },
            "in_reply_to": {
                "type": "string",
                "description": "Optional. When replying, pass the original email's message_id (from mail_inbox/read_mail) so the reply threads correctly in the recipient's client.",
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
            "confirm_high_risk": {
                "type": "boolean",
                "description": "Optional safety override. Set true only if the user explicitly confirmed sending a high-risk email request.",
            },
        },
        "required": ["to", "subject", "body"],
    }

    def run(self, **kwargs) -> str:
        cred_username = cred_username_from_kwargs(kwargs)
        user_scope_id = cred_scope_from_kwargs(kwargs)
        account_id = (kwargs.get("account_id") or "").strip()
        to = (kwargs.get("to") or "").strip()
        cc = (kwargs.get("cc") or "").strip() or None
        bcc = (kwargs.get("bcc") or "").strip() or None
        in_reply_to = (kwargs.get("in_reply_to") or "").strip() or None
        subject = (kwargs.get("subject") or "").strip()
        body = (kwargs.get("body") or "").strip()
        confirm_high_risk = bool(kwargs.get("confirm_high_risk", False))
        attachment_paths = kwargs.get("attachment_paths") or []
        # A single path passed as a bare string must be wrapped, not dropped
        # (the central input-repair layer normally does this upstream; this is a
        # non-lossy local guard for direct callers / if repair is unavailable).
        if isinstance(attachment_paths, str):
            attachment_paths = [attachment_paths] if attachment_paths.strip() else []
        elif not isinstance(attachment_paths, list):
            attachment_paths = []

        if not to:
            accounts = list_accounts_for_user(cred_username, user_scope_id=user_scope_id)
            if not accounts:
                return (
                    "No email accounts connected. The user must add an account in Settings → Connections → Email."
                )
            return f"Pass to, subject, and body. Optionally account_id. Connected accounts: {', '.join(accounts)}."
        if not account_id:
            accounts = list_accounts_for_user(cred_username, user_scope_id=user_scope_id)
            if not accounts:
                return (
                    "No email accounts connected. The user must add an account in Settings → Connections → Email."
                )
            account_id = accounts[0]
        acc = get_account(account_id, username=cred_username, user_scope_id=user_scope_id)
        if not acc:
            return f"Account '{account_id}' not found. Connected: {', '.join(list_accounts_for_user(cred_username, user_scope_id=user_scope_id))}."
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

        # Safety gate: do not auto-send potentially fraudulent/social-engineering requests.
        # The user must explicitly confirm before we allow risky messages.
        risk_reasons = _high_risk_send_reasons(to=to, subject=subject, body=body or "", attachments=attachments)
        if risk_reasons and not confirm_high_risk:
            reasons = ", ".join(risk_reasons)
            return (
                "Security check blocked this email as potentially high-risk. "
                f"Reasons: {reasons}. "
                "If the user confirms this exact send action is legitimate, call send_mail again with confirm_high_risk=true."
            )

        try:
            ok = send_mail(
                account_id,
                to=to,
                subject=subject,
                body=body or "",
                attachments=attachments if attachments else None,
                cc=cc,
                bcc=bcc,
                in_reply_to=in_reply_to,
                username=cred_username,
                user_scope_id=user_scope_id,
            )
        except Exception as e:
            return f"Failed to send email: {e}"
        if ok:
            suffix = f" with {len(attachments)} attachment(s)" if attachments else ""
            cc_suffix = f", cc {cc}" if cc else ""
            return f"Email{suffix} sent to {to}{cc_suffix} from {account_id}."
        
        # Determine provider for better error hint
        prov_hint = ""
        acc = get_account(account_id, username=cred_username, user_scope_id=user_scope_id)
        if acc:
            prov = (acc.get("provider") or "imap").lower()
            if prov in ("gmail", "microsoft"):
                prov_hint = f" ({prov.upper()} API failed - check connection in Settings -> Connections -> Email)"
            else:
                prov_hint = " (check SMTP settings and credentials in Settings)"
        
        return f"Failed to send email{prov_hint}."
