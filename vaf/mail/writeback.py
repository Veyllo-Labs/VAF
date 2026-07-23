# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Op-queue replay: pushes queued local writes to the server (phase 2).

Gating (EMAIL_CLIENT.md): mailbox WRITES (flags/move/append) run only when
mail_engine_write_enabled is on - until then queued ops stay pending and the
local-first UI remains consistent. SEND ops ride the existing v1 transport
(email_transport.send_mail - Bcc/provider semantics live there) and are gated
on the v2 flag only; the Sent-folder APPEND after an SMTP send is a mailbox
write and therefore respects the write flag.

Safety rules:
- Delete semantics are TRASH-ONLY: this module can MOVE to \\Trash but has no
  EXPUNGE-everything path; UID EXPUNGE is used solely for the source copy in
  the COPY+flag fallback when the server lacks MOVE (RFC 6851/4315 pattern).
- Ops are idempotent and capped at MAX_ATTEMPTS, then parked as 'failed'
  (visible via the ops API) instead of retrying forever.
"""
import base64
import logging
from typing import Any, Dict, Optional

from vaf.mail.store import MailStore

logger = logging.getLogger("vaf.mail.writeback")

MAX_ATTEMPTS = 5


def _b(v) -> str:
    return v.decode() if isinstance(v, (bytes, bytearray)) else str(v)


class OpExecutor:
    """Executes pending ops for one account over a duck-typed IMAP client
    (subset used: has_capability, select_folder, add_flags, remove_flags,
    move, copy, delete_messages, expunge, append)."""

    def __init__(self, store: MailStore, account_pk: int, client,
                 account: Dict[str, Any], scope: str,
                 cred_username: Optional[str] = None):
        self.store = store
        self.account_pk = account_pk
        self.client = client
        self.account = account
        self.scope = scope
        self.cred_username = cred_username

    def process(self, write_enabled: bool, v2_enabled: bool = True,
                now_ts: Optional[int] = None) -> Dict[str, int]:
        stats = {"done": 0, "failed": 0, "deferred": 0}
        for op in self.store.pending_ops(self.account_pk, now_ts=now_ts):
            kind = op["kind"]
            needs_write = kind in ("flags", "move", "append")
            if (needs_write and not write_enabled) or not v2_enabled:
                stats["deferred"] += 1
                continue
            if int(op.get("attempts") or 0) >= MAX_ATTEMPTS:
                self.store.mark_op(op["id"], "failed", error="max attempts reached")
                stats["failed"] += 1
                continue
            try:
                handler = getattr(self, f"_op_{kind}", None)
                if handler is None:
                    self.store.mark_op(op["id"], "failed", error=f"unknown kind {kind}")
                    stats["failed"] += 1
                    continue
                handler(op["payload"], write_enabled)
                self.store.mark_op(op["id"], "done")
                stats["done"] += 1
            except Exception as e:
                logger.warning("op %s (%s) failed: %s", op["id"], kind, e)
                self.store.mark_op(op["id"], "pending", error=str(e))
                stats["failed"] += 1
        return stats

    # ── op handlers ────────────────────────────────────────────────────────

    def _op_flags(self, payload: Dict[str, Any], write_enabled: bool) -> None:
        folder, uid = payload["folder"], int(payload["uid"])
        add = [str(f) for f in payload.get("add") or []]
        remove = [str(f) for f in payload.get("remove") or []]
        self.client.select_folder(folder, readonly=False)
        if add:
            self.client.add_flags([uid], add)
        if remove:
            self.client.remove_flags([uid], remove)
        # shadow copy: server now matches local for these flags
        fpk_row = self.store.get_folder(self.account_pk, folder)
        if fpk_row:
            uid_map = self.store.message_uid_map(int(fpk_row["id"]))
            pk = uid_map.get(uid)
            if pk:
                msg = self.store.get_message(pk)
                if msg:
                    self.store.apply_server_flags(int(fpk_row["id"]), {uid: msg["flags"]})

    def _op_move(self, payload: Dict[str, Any], write_enabled: bool) -> None:
        src, dest, uid = payload["folder"], payload["dest"], int(payload["uid"])
        self.client.select_folder(src, readonly=False)
        if self.client.has_capability("MOVE"):
            self.client.move([uid], dest)
        else:
            # RFC 6851 fallback: COPY + \Deleted + UID EXPUNGE of the source copy
            self.client.copy([uid], dest)
            self.client.add_flags([uid], ["\\Deleted"])
            self.client.expunge([uid])

    def _op_append(self, payload: Dict[str, Any], write_enabled: bool) -> None:
        folder = payload["folder"]
        raw = base64.b64decode(payload["raw_b64"])
        flags = [str(f) for f in payload.get("flags") or []]
        self.client.append(folder, raw, flags=flags)

    def _op_send(self, payload: Dict[str, Any], write_enabled: bool) -> None:
        """Outbox delivery through the v1 transport (provider-correct Bcc and
        auth live there). On success: optional Sent-APPEND for plain IMAP
        accounts (Gmail/Graph file Sent server-side) when writes are enabled."""
        from vaf.core.email_transport import send_mail as transport_send
        ok = transport_send(
            payload["account_id"],
            to=payload.get("to") or "",
            subject=payload.get("subject") or "",
            body=payload.get("body") or "",
            attachments=payload.get("attachments") or None,
            cc=payload.get("cc") or None,
            bcc=payload.get("bcc") or None,
            in_reply_to=payload.get("in_reply_to") or None,
            references=payload.get("references") or None,
            username=self.cred_username,
            user_scope_id=self.scope,
        )
        if not ok:
            raise RuntimeError("transport send failed")
        provider = (self.account.get("provider") or "imap").lower()
        if provider == "imap" and write_enabled and payload.get("raw_b64"):
            sent = self.store.find_special_folder(self.account_pk, "\\Sent")
            if sent:
                try:
                    self.client.append(sent["name"], base64.b64decode(payload["raw_b64"]),
                                       flags=["\\Seen"])
                except Exception as e:
                    logger.warning("sent-append failed (mail WAS sent): %s", e)
