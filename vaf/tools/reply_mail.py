# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Reply to an email with proper quoting and threading (mail engine v2).
Sends immediately through the same transport
and high-risk gate as send_mail."""
import logging
from typing import Optional

from vaf.core.config import get_local_admin_scope_id
from vaf.tools.base import BaseTool
from vaf.tools.mail_utils import cred_scope_from_kwargs, cred_username_from_kwargs
from vaf.tools.send_mail import _high_risk_send_reasons

logger = logging.getLogger("vaf.tools.reply_mail")


def _resolve_service(user_scope_id: Optional[str]):
    from vaf.mail.service import MailService
    scope = (user_scope_id or "").strip() or get_local_admin_scope_id()
    return MailService(scope)


class ReplyMailTool(BaseTool):
    """Reply to an email (quoted, correctly threaded). Use instead of send_mail
    when the user wants to answer a specific mail."""
    name = "reply_mail"
    category    = "mail"
    identity_kwargs = ("user_scope_id", "username")
    permission_level = "write"
    side_effect_class = "irreversible"
    description = (
        "Reply to an email with correct quoting and threading. Pass the message_id "
        "(from inbox/read_mail) and the reply body. reply_all=true answers every "
        "recipient. Requires the v2 mail engine; falls back with a hint when disabled."
    )
    input_examples = [
        {"message_id": "<abc@example.com>", "body": "Thanks, works for me!"},
    ]
    parameters = {
        "type": "object",
        "properties": {
            "message_id": {"type": "string", "description": "Message-ID of the mail to answer."},
            "body": {"type": "string", "description": "Reply text (plain text, without the quote - it is added automatically)."},
            "reply_all": {"type": "boolean", "description": "Optional. Answer all recipients (default false)."},
            "confirm_high_risk": {"type": "boolean", "description": "Optional safety override, only after explicit user confirmation."},
        },
        "required": ["message_id", "body"],
    }

    def run(self, **kwargs) -> str:
        user_scope_id = cred_scope_from_kwargs(kwargs)
        cred_username = cred_username_from_kwargs(kwargs)
        message_id = (kwargs.get("message_id") or "").strip()
        body = (kwargs.get("body") or "").strip()
        if not message_id or not body:
            return "Pass message_id and body."
        svc = _resolve_service(user_scope_id)
        pk = svc.store.pk_by_message_id(message_id)
        if pk is None:
            return f"Message '{message_id}' not found in the local mail store."
        pre = svc.reply_prefill(pk, reply_all=bool(kwargs.get("reply_all", False)))
        if not pre or not pre.get("to"):
            return "Could not derive reply recipients from that message."
        full_body = f"{body}{pre['body']}"
        reasons = _high_risk_send_reasons(pre["to"], pre["subject"], full_body, [])
        if reasons and not bool(kwargs.get("confirm_high_risk", False)):
            try:
                from vaf.core.security_events import log_security_event
                log_security_event("mail_high_risk_send_blocked",
                                   username=cred_username or "",
                                   detail=f"reply blocked, reasons: {', '.join(reasons)}")
            except Exception:
                pass
            return ("Security check blocked this reply as potentially high-risk. "
                    f"Reasons: {', '.join(reasons)}. If the user confirms, call "
                    "reply_mail again with confirm_high_risk=true.")
        from vaf.core.email_transport import get_account
        from vaf.mail.service import deliver_queued_sends
        acc = get_account(pre["account_id"], username=cred_username, user_scope_id=user_scope_id)
        if not acc:
            return f"Account '{pre['account_id']}' not found."
        # The one send funnel: the reply is queued and delivered right away; the outbox
        # files the Sent copy, records the id and marks the answered mail when it left.
        # `hold` is set by the chat lane (vaf/core/outbound_hold.py) when the person ordered
        # this reply in the web UI: it is parked as a draft for them instead of leaving.
        hold = bool(kwargs.get("hold", False))
        try:
            original = svc.store.get_message(pk) or {}
            queued = svc.queue_send(pre["account_id"], pre["to"], pre["subject"], full_body,
                                    cc=pre.get("cc") or "", in_reply_to=pre.get("in_reply_to") or "",
                                    references=pre.get("references") or "", undo_seconds=0,
                                    sent_by="agent", reply_to_pk=pk, thread_id=original.get("thread_id"),
                                    hold=hold,
                                    chat_session_id=str(kwargs.get("hold_session") or ""))
            if hold:
                from vaf.core.outbound_hold import held_result
                return held_result("reply_mail",
                                   {"to": pre["to"], "subject": pre["subject"], "body": full_body},
                                   entry_id=int(queued["op_id"]))
            deliver_queued_sends(svc.user_scope_id, acc, cred_username, pre["account_id"], service=svc)
            outcome = svc.send_outcome(int(queued["op_id"]))
        except Exception as e:
            return f"Failed to send reply: {e}"
        state, error = outcome["state"], outcome["error"]
        if state == "done":
            return f"Reply sent to {pre['to']} (subject: {pre['subject']})."
        if state == "pending":
            return f"The reply to {pre['to']} is queued in the outbox and will be retried shortly."
        if outcome["delivery"] == "ambiguous":
            return ("The reply may already have been delivered but the server did not confirm "
                    "it - do NOT resend without checking the Sent folder first.")
        return "Failed to send reply (check the account connection in Settings)." + (f" Detail: {error}" if error else "")
