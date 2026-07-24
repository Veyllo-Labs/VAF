# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Reply to an email with proper quoting and threading (mail engine v2).
Requires mail_engine_v2_enabled; sends immediately through the same transport
and high-risk gate as send_mail."""
import logging
from typing import Optional

from vaf.core.config import Config, get_local_admin_scope_id
from vaf.tools.base import BaseTool
from vaf.tools.mail_utils import cred_scope_from_kwargs, cred_username_from_kwargs
from vaf.tools.send_mail import _high_risk_send_reasons

logger = logging.getLogger("vaf.tools.reply_mail")


def _resolve_service(user_scope_id: Optional[str]):
    from vaf.mail.service import MailService
    scope = (user_scope_id or "").strip() or get_local_admin_scope_id()
    return MailService(scope)


def _find_pk_by_message_id(svc, message_id: str) -> Optional[int]:
    mid = (message_id or "").strip()
    variants = {mid, mid.strip("<>"), f"<{mid.strip('<>')}>"}
    q = ",".join("?" for _ in variants)
    row = svc.store._conn().execute(
        f"SELECT id FROM messages WHERE message_id IN ({q}) ORDER BY id DESC LIMIT 1",
        (*variants,)).fetchone()
    return int(row["id"]) if row else None


class ReplyMailTool(BaseTool):
    """Reply to an email (quoted, correctly threaded). Use instead of send_mail
    when the user wants to answer a specific mail."""
    name = "reply_mail"
    permission_level = "write"
    side_effect_class = "irreversible"
    description = (
        "Reply to an email with correct quoting and threading. Pass the message_id "
        "(from mail_inbox/read_mail) and the reply body. reply_all=true answers every "
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
        if not bool(Config.get("mail_engine_v2_enabled", False)):
            return ("reply_mail needs the v2 mail engine (mail_engine_v2_enabled). "
                    "Use send_mail with in_reply_to instead.")
        user_scope_id = cred_scope_from_kwargs(kwargs)
        cred_username = cred_username_from_kwargs(kwargs)
        message_id = (kwargs.get("message_id") or "").strip()
        body = (kwargs.get("body") or "").strip()
        if not message_id or not body:
            return "Pass message_id and body."
        svc = _resolve_service(user_scope_id)
        pk = _find_pk_by_message_id(svc, message_id)
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
        from vaf.core.email_transport import send_mail as transport_send
        try:
            ok = transport_send(
                pre["account_id"], to=pre["to"], subject=pre["subject"], body=full_body,
                cc=pre.get("cc") or None, in_reply_to=pre.get("in_reply_to") or None,
                references=pre.get("references") or None,
                username=cred_username, user_scope_id=user_scope_id)
        except Exception as e:
            return f"Failed to send reply: {e}"
        if not ok:
            return "Failed to send reply (check the account connection in Settings)."
        try:
            svc.store._conn().execute(
                "UPDATE messages SET answered_at=datetime('now') WHERE id=?", (pk,))
            svc.store._conn().commit()
        except Exception:
            pass
        return f"Reply sent to {pre['to']} (subject: {pre['subject']})."
