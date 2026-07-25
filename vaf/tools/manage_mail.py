# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Mailbox management tools for the v2 engine: forward, archive, trash.

All verbs are local-first: the change is visible immediately and replays to
the mail server via the op queue once mail_engine_write_enabled is on.
Delete semantics are TRASH-ONLY by design (EMAIL_CLIENT.md) - nothing here can
expunge. These tools are NOT in the front-office allow-list on purpose."""
import logging
from typing import Optional

from vaf.core.config import Config, get_local_admin_scope_id
from vaf.tools.base import BaseTool
from vaf.tools.mail_utils import cred_scope_from_kwargs, cred_username_from_kwargs

logger = logging.getLogger("vaf.tools.manage_mail")


def _service(user_scope_id: Optional[str]):
    from vaf.mail.service import MailService
    scope = (user_scope_id or "").strip() or get_local_admin_scope_id()
    return MailService(scope)


def _pk_by_message_id(svc, message_id: str) -> Optional[int]:
    mid = (message_id or "").strip()
    variants = {mid, mid.strip("<>"), f"<{mid.strip('<>')}>"}
    q = ",".join("?" for _ in variants)
    row = svc.store._conn().execute(
        f"SELECT id FROM messages WHERE message_id IN ({q}) ORDER BY id DESC LIMIT 1",
        (*variants,)).fetchone()
    return int(row["id"]) if row else None


def _write_note() -> str:
    if not bool(Config.get("mail_engine_write_enabled", False)):
        return (" Note: server-side writes are disabled (mail_engine_write_enabled), "
                "so the change is local for now and replays once writes are enabled.")
    return ""


class ForwardMailTool(BaseTool):
    """Forward an email to someone. Original attachments are not included."""
    name = "forward_mail"
    permission_level = "write"
    side_effect_class = "irreversible"
    description = (
        "Forward an email. Pass message_id (from mail_inbox/read_mail), the recipient "
        "in to, and an optional note that is placed above the forwarded content. "
        "Original attachments are NOT forwarded."
    )
    input_examples = [
        {"message_id": "<abc@example.com>", "to": "colleague@example.com", "note": "FYI"},
    ]
    parameters = {
        "type": "object",
        "properties": {
            "message_id": {"type": "string", "description": "Message-ID of the mail to forward."},
            "to": {"type": "string", "description": "Recipient address(es), comma-separated."},
            "note": {"type": "string", "description": "Optional note above the forwarded content."},
            "confirm_high_risk": {"type": "boolean", "description": "Optional safety override, only after explicit user confirmation."},
        },
        "required": ["message_id", "to"],
    }

    def run(self, **kwargs) -> str:
        user_scope_id = cred_scope_from_kwargs(kwargs)
        cred_username = cred_username_from_kwargs(kwargs)
        to = (kwargs.get("to") or "").strip()
        message_id = (kwargs.get("message_id") or "").strip()
        if not to or not message_id:
            return "Pass message_id and to."
        svc = _service(user_scope_id)
        pk = _pk_by_message_id(svc, message_id)
        if pk is None:
            return f"Message '{message_id}' not found in the local mail store."
        pre = svc.forward_prefill(pk)
        if not pre:
            return "Could not build the forward from that message."
        note = (kwargs.get("note") or "").strip()
        full_body = f"{note}{pre['body']}" if note else pre["body"].lstrip("\n")
        from vaf.tools.send_mail import _high_risk_send_reasons
        reasons = _high_risk_send_reasons(to, pre["subject"], full_body, [])
        if reasons and not bool(kwargs.get("confirm_high_risk", False)):
            try:
                from vaf.core.security_events import log_security_event
                log_security_event("mail_high_risk_send_blocked",
                                   username=cred_username or "",
                                   detail=f"forward blocked, reasons: {', '.join(reasons)}")
            except Exception:
                pass
            return ("Security check blocked this forward as potentially high-risk. "
                    f"Reasons: {', '.join(reasons)}. If the user confirms, call "
                    "forward_mail again with confirm_high_risk=true.")
        from vaf.core.email_transport import get_account
        from vaf.mail import compose, sender
        acc = get_account(pre["account_id"], username=cred_username, user_scope_id=user_scope_id)
        if not acc:
            return f"Account '{pre['account_id']}' not found."
        try:
            from_addr = acc.get("email") or pre["account_id"]
            mime = compose.build_message(from_addr, to, pre["subject"], full_body)
            msg = sender.OutgoingMessage(
                account=acc, raw_bytes=bytes(mime), to=to,
                username=cred_username, user_scope_id=user_scope_id,
                subject=pre["subject"], body=full_body, message_id=mime["Message-ID"])
            res = sender.send(msg)
        except Exception as e:
            return f"Failed to forward: {e}"
        if res.classification == "ambiguous":
            return ("The forward may already have been delivered but the server did not confirm "
                    "it - do NOT resend without checking the Sent folder first.")
        return (f"Forwarded to {to} (subject: {pre['subject']})." if res.ok
                else "Failed to forward (check the account connection in Settings).")


class ArchiveMailTool(BaseTool):
    """Archive an email (move out of the inbox)."""
    name = "archive_mail"
    permission_level = "write"
    side_effect_class = "reversible"
    description = (
        "Archive an email: moves it out of the inbox into the archive folder "
        "(Gmail: All Mail). Pass message_id from mail_inbox/read_mail."
    )
    input_examples = [{"message_id": "<abc@example.com>"}]
    parameters = {
        "type": "object",
        "properties": {
            "message_id": {"type": "string", "description": "Message-ID of the mail to archive."},
        },
        "required": ["message_id"],
    }

    def run(self, **kwargs) -> str:
        svc = _service(cred_scope_from_kwargs(kwargs))
        pk = _pk_by_message_id(svc, kwargs.get("message_id") or "")
        if pk is None:
            return "Message not found in the local mail store."
        out = svc.archive(pk)
        if not out.get("ok"):
            return f"Archive failed: {out.get('error')}"
        return f"Archived (moved to {out.get('dest', 'archive')}).{_write_note()}"


class DeleteMailTool(BaseTool):
    """Move an email to the trash folder (never permanently deletes)."""
    name = "delete_mail"
    permission_level = "write"
    side_effect_class = "reversible"
    description = (
        "Move an email to the trash folder. This NEVER deletes permanently - "
        "the mail stays in trash and can be restored there. Pass message_id."
    )
    input_examples = [{"message_id": "<abc@example.com>"}]
    parameters = {
        "type": "object",
        "properties": {
            "message_id": {"type": "string", "description": "Message-ID of the mail to move to trash."},
        },
        "required": ["message_id"],
    }

    def run(self, **kwargs) -> str:
        svc = _service(cred_scope_from_kwargs(kwargs))
        pk = _pk_by_message_id(svc, kwargs.get("message_id") or "")
        if pk is None:
            return "Message not found in the local mail store."
        out = svc.trash(pk)
        if not out.get("ok"):
            return f"Move to trash failed: {out.get('error')}"
        return f"Moved to trash ({out.get('dest', 'Trash')}).{_write_note()}"
