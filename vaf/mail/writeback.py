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


class _SendRetry(RuntimeError):
    """Raised by _op_send for a PRE-hand-off transient send failure (connect / 4xx
    before the DATA command): safe to retry, so process() re-pends it (still
    attempt-capped). Any other send failure (permanent, or post-hand-off ambiguous)
    raises a plain RuntimeError and is parked, never re-sent."""


def _b(v) -> str:
    return v.decode() if isinstance(v, (bytes, bytearray)) else str(v)


def _uidvalidity_ok(select_info: Any, pinned: Any) -> bool:
    """True unless there is a POSITIVELY KNOWN UIDVALIDITY mismatch between the
    op's pinned value and the SELECT response. If either is missing we cannot
    verify and proceed (backward compatible with ops enqueued without a pin)."""
    if pinned is None:
        return True
    got = None
    if isinstance(select_info, dict):
        got = select_info.get(b"UIDVALIDITY", select_info.get("UIDVALIDITY"))
    if got is None:
        return True
    return int(got) == int(pinned)


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
                now_ts: Optional[int] = None, allowed_kinds: Optional[set] = None) -> Dict[str, int]:
        """Drain pending ops. allowed_kinds (optional) restricts the pass to a
        subset of kinds and leaves the rest UNTOUCHED - used for a send-only
        drain over a session-less (Null) client, where flags/move/append must
        not be attempted (and their attempts not burned)."""
        stats = {"done": 0, "failed": 0, "deferred": 0}
        # Re-arm ops stranded in 'sending' by a crashed worker before this pass
        # (idempotent kinds retry; interrupted sends are parked, never re-sent).
        self.store.reclaim_stale_ops(self.account_pk)
        for op in self.store.pending_ops(self.account_pk, now_ts=now_ts):
            kind = op["kind"]
            if allowed_kinds is not None and kind not in allowed_kinds:
                continue  # not this pass - leave the op untouched
            needs_write = kind in ("flags", "move", "append")
            if (needs_write and not write_enabled) or not v2_enabled:
                stats["deferred"] += 1
                continue
            # Cap BEFORE claiming so a capped op is parked without burning a claim.
            if int(op.get("attempts") or 0) >= MAX_ATTEMPTS:
                self.store.mark_op(op["id"], "failed", error="max attempts reached")
                stats["failed"] += 1
                continue
            handler = getattr(self, f"_op_{kind}", None)
            if handler is None:
                self.store.mark_op(op["id"], "failed", error=f"unknown kind {kind}")
                stats["failed"] += 1
                continue
            # Atomic claim: exactly ONE executor runs this op. The loser (another
            # process/thread, or a meanwhile-cancelled op) gets False and skips,
            # so a non-idempotent send can never be delivered twice.
            if not self.store.claim_op(op["id"]):
                continue
            try:
                handler(op["payload"], write_enabled)
                self.store.mark_op(op["id"], "done", expect_state="sending")
                stats["done"] += 1
            except Exception as e:
                logger.warning("op %s (%s) failed: %s", op["id"], kind, e)
                if kind == "send":
                    # A pre-hand-off transient failure (connect / 4xx before DATA)
                    # is safe to retry; the sender proved delivery had not started.
                    # Anything else (permanent, or post-hand-off ambiguous where the
                    # server may already have accepted) is parked - SMTP has no
                    # idempotency key, so a possibly-delivered mail is never re-sent.
                    retry = isinstance(e, _SendRetry)
                    self.store.mark_op(op["id"], "pending" if retry else "failed",
                                       error=str(e), expect_state="sending")
                else:
                    self.store.mark_op(op["id"], "pending", error=str(e),
                                       expect_state="sending")
                stats["failed"] += 1
        return stats

    # ── op handlers ────────────────────────────────────────────────────────

    def _op_flags(self, payload: Dict[str, Any], write_enabled: bool) -> None:
        folder = payload["folder"]
        uid = payload.get("uid")
        # Resolve the CURRENT server coordinates from the stable local pk when
        # present (a move may have changed folder/uid since enqueue). If the uid
        # is still unknown (the move has not been reconciled by a sync yet), defer
        # by raising - the op stays pending and retries on a later pass.
        mpk = payload.get("message_pk")
        if mpk is not None:
            cur = self.store.get_message(int(mpk))
            if cur is not None:
                frow = self.store._conn().execute(
                    "SELECT name FROM folders WHERE id=?", (cur["folder_id"],)).fetchone()
                if frow:
                    folder = frow["name"]
                uid = cur.get("uid")
        if uid is None:
            raise RuntimeError("flag op deferred: message has no server uid yet")
        uid = int(uid)
        add = [str(f) for f in payload.get("add") or []]
        remove = [str(f) for f in payload.get("remove") or []]
        info = self.client.select_folder(folder, readonly=False)
        if not _uidvalidity_ok(info, payload.get("uidvalidity")):
            # UIDVALIDITY rotated since enqueue: this uid now denotes a DIFFERENT
            # message. Drop the op (the resync already reconciled the folder).
            logger.warning("flag op dropped: UIDVALIDITY rotated in %s", folder)
            return
        if add:
            self.client.add_flags([uid], add)
        if remove:
            self.client.remove_flags([uid], remove)
        # Update the server shadow by the DELTA we actually pushed (add/remove),
        # not the full local flag list - the server only received this delta, so
        # asserting it matches every local flag would widen the resync stomp.
        fpk_row = self.store.get_folder(self.account_pk, folder)
        if fpk_row:
            self.store.apply_server_flags_delta(int(fpk_row["id"]), uid,
                                                add=add, remove=remove)

    def _op_move(self, payload: Dict[str, Any], write_enabled: bool) -> None:
        src, dest, uid = payload["folder"], payload["dest"], int(payload["uid"])
        info = self.client.select_folder(src, readonly=False)
        if not _uidvalidity_ok(info, payload.get("uidvalidity")):
            # UIDVALIDITY rotated since enqueue: uid now denotes a DIFFERENT
            # message - dropping the op avoids moving the wrong mail to Trash.
            logger.warning("move op dropped: UIDVALIDITY rotated in %s", src)
            return
        if self.client.has_capability("MOVE"):
            self.client.move([uid], dest)  # RFC 6851, atomic
            return
        # Fallback for servers without MOVE: COPY + \Deleted + UID EXPUNGE of the
        # SOURCE copy. UID EXPUNGE is RFC 4315 UIDPLUS - without it we must NOT
        # fall back to a plain EXPUNGE (that would expunge unrelated \Deleted mail
        # and break the trash-only invariant). Park the op instead.
        if not self.client.has_capability("UIDPLUS"):
            raise RuntimeError("MOVE fallback needs UIDPLUS (UID EXPUNGE); parking op")
        # Idempotency: a retry (e.g. after EXPUNGE failed) must not COPY twice.
        # If a prior attempt already flagged the source \Deleted, the COPY has
        # happened - skip it and just (re)issue the UID EXPUNGE.
        try:
            src_flags = self.client.get_flags([uid]).get(uid, [])
        except Exception:
            src_flags = []
        if not any(_b(f) == "\\Deleted" for f in src_flags):
            self.client.copy([uid], dest)
            self.client.add_flags([uid], ["\\Deleted"])
        self.client.expunge([uid])

    def _op_append(self, payload: Dict[str, Any], write_enabled: bool) -> None:
        folder = payload["folder"]
        raw = base64.b64decode(payload["raw_b64"])
        flags = [str(f) for f in payload.get("flags") or []]
        self.client.append(folder, raw, flags=flags)

    def _op_send(self, payload: Dict[str, Any], write_enabled: bool) -> None:
        """Outbox delivery through the native sender (vaf/mail/sender.py). The
        stored RFC822 bytes are sent verbatim, so the delivered Message-ID matches
        the Sent copy byte-for-byte. A pre-hand-off transient failure raises
        _SendRetry (re-pend, capped); any permanent or post-hand-off ambiguous
        failure raises RuntimeError (park, never re-sent). On success: optional
        Sent-APPEND for plain IMAP accounts (Gmail/Graph file Sent server-side)
        when writes are enabled."""
        from vaf.mail import sender
        raw = base64.b64decode(payload["raw_b64"]) if payload.get("raw_b64") else b""
        msg = sender.OutgoingMessage(
            account=self.account,
            raw_bytes=raw,
            to=payload.get("to") or "",
            cc=payload.get("cc") or "",
            bcc=payload.get("bcc") or "",
            username=self.cred_username,
            user_scope_id=self.scope,
            subject=payload.get("subject") or "",
            body=payload.get("body") or "",
            message_id=payload.get("message_id") or None,
            in_reply_to=payload.get("in_reply_to") or None,
            references=payload.get("references") or None,
            attachments=payload.get("attachments") or None,
        )
        res = sender.send(msg)
        if not res.ok:
            if res.classification == "transient" and not res.handed_off:
                raise _SendRetry(res.error or "transient send failure")
            raise RuntimeError(res.error or f"send failed ({res.classification})")
        # Everything after a successful send is best-effort and MUST NOT raise
        # out of the handler - otherwise process() would treat the (already
        # delivered) mail as failed and, without an atomic claim, re-send it.
        # The whole tail (find_special_folder + b64decode + append) is wrapped.
        try:
            provider = (self.account.get("provider") or "imap").lower()
            if (provider == "imap" and write_enabled and payload.get("raw_b64")
                    and not self._server_files_sent()):
                sent = self.store.find_special_folder(self.account_pk, "\\Sent")
                if sent:
                    self.client.append(sent["name"], base64.b64decode(payload["raw_b64"]),
                                       flags=["\\Seen"])
        except Exception as e:
            logger.warning("post-send tail failed (mail WAS sent): %s", e)

    def _server_files_sent(self) -> bool:
        """True when the account's own SMTP files the Sent copy automatically
        (Gmail), so a client APPEND would create a duplicate in Sent."""
        host = (self.account.get("smtp_host") or self.account.get("imap_host") or "").lower()
        email = (self.account.get("email") or self.account.get("account_id") or "").lower()
        return ("gmail.com" in host or "googlemail.com" in host or "google" in host
                or email.endswith("@gmail.com") or email.endswith("@googlemail.com"))
