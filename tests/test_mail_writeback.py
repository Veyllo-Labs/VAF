# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Phase-2 write-path tests: local-first verbs, op-queue replay (flags, move
with and without MOVE capability, trash-only semantics), undo-send outbox
(cancel window, delayed delivery via the v1 transport, Sent-APPEND gating),
write-flag deferral and the attempts cap. Isolated: tmp store, pinned crypto
key, transport monkeypatched - no real servers, no real config."""
import os
import time

import pytest

import vaf.mail.crypto as mail_crypto
from vaf.mail.parser import ParsedMessage
from vaf.mail.service import MailService
from vaf.mail.store import MailStore
from vaf.mail.writeback import OpExecutor

_SCOPE = "12345678-1234-1234-1234-123456789abc"


@pytest.fixture(autouse=True)
def _pinned_crypto_key():
    old = mail_crypto._cached_key
    mail_crypto._cached_key = os.urandom(32)
    yield
    mail_crypto._cached_key = old


class FakeWriteImap:
    def __init__(self, caps=("IMAP4REV1", "MOVE")):
        self.caps = set(caps)
        self.calls = []
        self.appended = []
        self.flags = {}  # uid -> set of flags (for get_flags-based idempotency)
        self.uidvalidity = None  # when set, returned from select_folder (UIDVALIDITY pin tests)

    def has_capability(self, cap):
        return cap in self.caps

    def select_folder(self, name, readonly=True):
        self.calls.append(("select", name, readonly))
        return {b"UIDVALIDITY": self.uidvalidity} if self.uidvalidity is not None else {}

    def add_flags(self, uids, flags):
        self.calls.append(("add_flags", tuple(uids), tuple(flags)))
        for u in uids:
            self.flags.setdefault(u, set()).update(flags)

    def remove_flags(self, uids, flags):
        self.calls.append(("remove_flags", tuple(uids), tuple(flags)))
        for u in uids:
            self.flags.setdefault(u, set()).difference_update(flags)

    def get_flags(self, uids):
        return {u: sorted(self.flags.get(u, set())) for u in uids}

    def move(self, uids, dest):
        self.calls.append(("move", tuple(uids), dest))

    def copy(self, uids, dest):
        self.calls.append(("copy", tuple(uids), dest))

    def expunge(self, uids=None):
        self.calls.append(("expunge", tuple(uids or ())))

    def append(self, folder, raw, flags=None):
        self.appended.append((folder, bytes(raw), tuple(flags or ())))


@pytest.fixture()
def svc(tmp_path):
    store = MailStore(_SCOPE, base_dir=tmp_path)
    s = MailService.__new__(MailService)
    s.user_scope_id = _SCOPE
    s.store = store
    yield s
    store.close()


def _seed(svc, uid=1, folder="INBOX", special=None):
    apk = svc.store.upsert_account("bob@example.com", "imap", "bob@example.com")
    fpk = svc.store.upsert_folder(apk, folder, special_use=special or "\\Inbox")
    pk = svc.store.ingest_message(
        apk, fpk, uid,
        ParsedMessage(message_id=f"<w{uid}@example.com>", subject="Write me",
                      from_addr="Alice <alice@example.com>", to_addrs="bob@example.com",
                      date_ts=1_700_000_000, body_text="hello"),
        server_flags=[])
    return apk, fpk, pk


def _exec(svc, apk, client, write=True, v2=True, now_ts=None):
    ex = OpExecutor(svc.store, apk, client, {"provider": "imap"}, _SCOPE)
    return ex.process(write_enabled=write, v2_enabled=v2, now_ts=now_ts)


def test_mark_read_local_first_and_replay(svc):
    apk, fpk, pk = _seed(svc)
    flags = svc.mark_read(pk)
    assert "\\Seen" in flags  # local truth immediately
    ops = svc.store.pending_ops(apk)
    assert len(ops) == 1 and ops[0]["kind"] == "flags"
    client = FakeWriteImap()
    stats = _exec(svc, apk, client)
    assert stats["done"] == 1
    assert ("add_flags", (1,), ("\\Seen",)) in client.calls
    assert ("select", "INBOX", False) in client.calls
    msg = svc.store.get_message(pk)
    assert "\\Seen" in msg["server_flags"]  # shadow updated
    assert svc.store.pending_ops(apk) == []


def test_write_flag_off_defers_mailbox_writes(svc):
    apk, fpk, pk = _seed(svc)
    svc.mark_read(pk)
    client = FakeWriteImap()
    stats = _exec(svc, apk, client, write=False)
    assert stats["deferred"] == 1 and client.calls == []
    assert len(svc.store.pending_ops(apk)) == 1  # still queued


def test_trash_is_move_only_and_uses_fallback_without_move_cap(svc):
    apk, fpk, pk = _seed(svc)
    svc.store.upsert_folder(apk, "Papierkorb", special_use="\\Trash")
    out = svc.trash(pk)
    assert out["ok"] and out["dest"] == "Papierkorb"
    # local-first: message already lives in trash folder
    msg = svc.store.get_message(pk)
    assert svc.store.get_folder(apk, "Papierkorb")["id"] == msg["folder_id"]
    client = FakeWriteImap(caps=("IMAP4REV1", "UIDPLUS"))  # no MOVE, but UIDPLUS present
    stats = _exec(svc, apk, client)
    assert stats["done"] == 1
    assert ("copy", (1,), "Papierkorb") in client.calls
    assert ("add_flags", (1,), ("\\Deleted",)) in client.calls
    assert ("expunge", (1,)) in client.calls
    assert not any(c[0] == "move" for c in client.calls)


def test_move_uses_move_capability_when_available(svc):
    apk, fpk, pk = _seed(svc)
    svc.store.upsert_folder(apk, "Archiv", special_use="\\Archive")
    assert svc.archive(pk)["ok"]
    client = FakeWriteImap()
    stats = _exec(svc, apk, client)
    assert stats["done"] == 1
    assert ("move", (1,), "Archiv") in client.calls


def test_undo_send_cancel_and_delayed_delivery(svc, monkeypatch):
    apk, fpk, pk = _seed(svc)
    svc.store.upsert_folder(apk, "Gesendet", special_use="\\Sent")
    sent_calls = []

    def _fake_send(account_id, **kw):
        sent_calls.append((account_id, kw.get("to")))
        return True

    import vaf.core.email_transport as transport
    monkeypatch.setattr(transport, "send_mail", _fake_send)

    # cancel inside the undo window: nothing is ever delivered
    out = svc.queue_send("bob@example.com", "rcpt@example.com", "Hi", "Body", undo_seconds=15)
    assert svc.cancel_send(out["op_id"]) is True
    client = FakeWriteImap()
    _exec(svc, apk, client, now_ts=int(time.time()) + 999)
    assert sent_calls == []

    # no cancel: before the window nothing runs, after it the send fires
    out2 = svc.queue_send("bob@example.com", "rcpt2@example.com", "Hi", "Body", undo_seconds=15)
    stats = _exec(svc, apk, client, now_ts=int(time.time()))
    assert stats["done"] == 0 and sent_calls == []
    stats2 = _exec(svc, apk, client, now_ts=int(time.time()) + 60)
    assert stats2["done"] == 1
    assert sent_calls == [("bob@example.com", "rcpt2@example.com")]
    # Sent-APPEND for plain IMAP accounts (write flag on)
    assert client.appended and client.appended[0][0] == "Gesendet"
    assert b"rcpt2@example.com" in client.appended[0][1]


def test_send_runs_without_write_flag_but_skips_sent_append(svc, monkeypatch):
    apk, fpk, pk = _seed(svc)
    svc.store.upsert_folder(apk, "Gesendet", special_use="\\Sent")
    import vaf.core.email_transport as transport
    monkeypatch.setattr(transport, "send_mail", lambda a, **k: True)
    svc.queue_send("bob@example.com", "x@example.com", "S", "B", undo_seconds=0)
    client = FakeWriteImap()
    stats = _exec(svc, apk, client, write=False, now_ts=int(time.time()) + 5)
    assert stats["done"] == 1
    assert client.appended == []  # mailbox write gated off


def test_attempts_cap_parks_op_as_failed(svc, monkeypatch):
    apk, fpk, pk = _seed(svc)
    svc.mark_read(pk)

    class Boom(FakeWriteImap):
        def add_flags(self, uids, flags):
            raise OSError("server rejects")

    client = Boom()
    for _ in range(6):
        _exec(svc, apk, client)
    ops = [svc.store.get_op(o["id"]) for o in
           svc.store._conn().execute("SELECT id FROM ops").fetchall()]
    assert all(o["state"] == "failed" for o in ops)


def test_reply_prefill_quotes_and_threads(svc):
    apk, fpk, pk = _seed(svc)
    pre = svc.reply_prefill(pk)
    assert pre["to"].startswith("Alice")
    assert pre["subject"] == "Re: Write me"
    assert "> hello" in pre["body"]
    assert pre["in_reply_to"] == "<w1@example.com>"
    assert "<w1@example.com>" in pre["references"]
    # reply-all excludes own address
    pre2 = svc.reply_prefill(pk, reply_all=True)
    assert "bob@example.com" not in (pre2["cc"] or "")


# ── C1: op-queue exactly-once + honest undo (T1/T2/T23) ──────────────────────


def test_claim_op_is_atomic_exactly_once(svc):
    apk, fpk, pk = _seed(svc)
    op_id = svc.store.enqueue_op(apk, "flags",
                                 {"folder": "INBOX", "uid": 1, "add": ["\\Seen"], "remove": []})
    assert svc.store.claim_op(op_id) is True    # first executor wins pending->sending
    assert svc.store.claim_op(op_id) is False   # second loses (already 'sending') -> no double-run


def test_a_sending_op_is_invisible_to_a_second_executor(svc):
    apk, fpk, pk = _seed(svc)
    import time as _t
    svc.queue_send("bob@example.com", "x@example.com", "s", "b", undo_seconds=0)
    op_id = svc.store._conn().execute("SELECT id FROM ops WHERE kind='send'").fetchone()["id"]
    svc.store.claim_op(int(op_id))  # executor A claims it (now 'sending', mid-delivery)
    # executor B must not pick up the in-flight op
    assert svc.store.pending_ops(apk, now_ts=int(_t.time()) + 999) == []


def test_cancel_cannot_clobber_a_cancelled_op(svc):
    apk, fpk, pk = _seed(svc)
    op_id = svc.store.enqueue_op(apk, "send",
                                 {"account_id": "bob@example.com", "to": "x@example.com",
                                  "subject": "s", "body": "b"})
    assert svc.store.cancel_op(op_id) is True                        # cancelled while pending
    # a late executor's mark_op must not flip 'cancelled' -> 'done'
    assert svc.store.mark_op(op_id, "done", expect_state="sending") is False
    assert svc.store.get_op(op_id)["state"] == "cancelled"


def test_cancel_is_rejected_once_sending(svc):
    apk, fpk, pk = _seed(svc)
    op_id = svc.store.enqueue_op(apk, "send",
                                 {"account_id": "bob@example.com", "to": "x@example.com",
                                  "subject": "s", "body": "b"})
    svc.store.claim_op(op_id)                                        # now 'sending' (delivering)
    assert svc.store.cancel_op(op_id) is False                      # undo is honest: too late


def test_send_failure_is_parked_not_retried(svc, monkeypatch):
    apk, fpk, pk = _seed(svc)
    calls = []
    import vaf.core.email_transport as transport
    monkeypatch.setattr(transport, "send_mail", lambda a, **k: calls.append(a) or False)
    svc.queue_send("bob@example.com", "x@example.com", "s", "b", undo_seconds=0)
    client = FakeWriteImap()
    now = int(time.time()) + 5
    _exec(svc, apk, client, now_ts=now)
    states = [r["state"] for r in
              svc.store._conn().execute("SELECT state FROM ops WHERE kind='send'").fetchall()]
    assert states == ["failed"] and len(calls) == 1        # parked, sent exactly once (the failure)
    _exec(svc, apk, client, now_ts=now)                    # second pass must not re-send
    assert len(calls) == 1


def test_reclaim_stale_ops_parks_send_rearms_idempotent(svc):
    apk, fpk, pk = _seed(svc)
    send_id = svc.store.enqueue_op(apk, "send",
                                   {"account_id": "bob@example.com", "to": "x@example.com",
                                    "subject": "s", "body": "b"})
    flag_id = svc.store.enqueue_op(apk, "flags",
                                   {"folder": "INBOX", "uid": 1, "add": ["\\Seen"], "remove": []})
    assert svc.store.claim_op(send_id) and svc.store.claim_op(flag_id)  # both 'sending'
    svc.store._conn().execute("UPDATE ops SET updated_at='2000-01-01T00:00:00+00:00' "
                              "WHERE id IN (?,?)", (send_id, flag_id))
    svc.store._conn().commit()
    assert svc.store.reclaim_stale_ops(apk, lease_seconds=300) == 2
    assert svc.store.get_op(send_id)["state"] == "failed"      # ambiguous send never auto-retried
    assert svc.store.get_op(flag_id)["state"] == "pending"     # idempotent op re-armed


# ── C2: provider-agnostic, restart-safe send drain (T3) ──────────────────────


def test_process_allowed_kinds_only_touches_that_kind(svc, monkeypatch):
    apk, fpk, pk = _seed(svc)
    svc.store.enqueue_op(apk, "flags",
                         {"folder": "INBOX", "uid": 1, "add": ["\\Seen"], "remove": []})
    import vaf.core.email_transport as transport
    sent = []
    monkeypatch.setattr(transport, "send_mail", lambda a, **k: sent.append(a) or True)
    svc.queue_send("bob@example.com", "x@example.com", "s", "b", undo_seconds=0)
    from vaf.mail.imap_client import NullImapClient
    ex = OpExecutor(svc.store, apk, NullImapClient(), {"provider": "gmail"}, _SCOPE)
    stats = ex.process(write_enabled=False, now_ts=int(time.time()) + 5, allowed_kinds={"send"})
    assert stats["done"] == 1 and sent == ["bob@example.com"]        # send delivered
    pending_kinds = [r["kind"] for r in svc.store._conn().execute(
        "SELECT kind FROM ops WHERE state='pending'").fetchall()]
    assert "flags" in pending_kinds and "send" not in pending_kinds  # flags op untouched


def test_send_drain_with_null_client_delivers_and_skips_sent_append(svc, monkeypatch):
    apk, fpk, pk = _seed(svc)
    svc.store.upsert_folder(apk, "Gesendet", special_use="\\Sent")
    import vaf.core.email_transport as transport
    sent = []
    monkeypatch.setattr(transport, "send_mail", lambda a, **k: sent.append(a) or True)
    svc.queue_send("bob@example.com", "x@example.com", "s", "b", undo_seconds=0)
    from vaf.mail.imap_client import NullImapClient
    ex = OpExecutor(svc.store, apk, NullImapClient(), {"provider": "imap"}, _SCOPE)
    # IMAP account but no session: send still delivers; the Sent-APPEND raises on
    # the null client and is swallowed by the post-send tail (mail WAS sent).
    stats = ex.process(write_enabled=True, now_ts=int(time.time()) + 5, allowed_kinds={"send"})
    assert stats["done"] == 1 and sent == ["bob@example.com"]


# ── C3: uid-NULL move reconciliation (T4) ────────────────────────────────────


def test_flag_op_on_moved_message_defers_then_applies(svc):
    apk, fpk, pk = _seed(svc)
    svc.set_star(pk, True)                                        # flags op carries message_pk
    svc.store._conn().execute("UPDATE messages SET uid=NULL WHERE id=?", (pk,))  # simulate move
    svc.store._conn().commit()
    client = FakeWriteImap()
    stats = _exec(svc, apk, client)
    assert not any(c[0] == "add_flags" for c in client.calls)     # deferred, not applied
    assert stats["failed"] == 1                                   # op re-armed pending (not lost)
    svc.store._conn().execute("UPDATE messages SET uid=9 WHERE id=?", (pk,))     # sync adopted a uid
    svc.store._conn().commit()
    _exec(svc, apk, client)
    assert ("add_flags", (9,), ("\\Flagged",)) in client.calls    # now applies on the new uid


# ── C4: MOVE fallback UIDPLUS gate + idempotency (T5) ────────────────────────


def test_move_fallback_without_uidplus_is_parked_never_expunges(svc):
    apk, fpk, pk = _seed(svc)
    svc.store.upsert_folder(apk, "Papierkorb", special_use="\\Trash")
    svc.trash(pk)
    client = FakeWriteImap(caps=("IMAP4REV1",))  # no MOVE AND no UIDPLUS
    _exec(svc, apk, client)
    # trash-only invariant: without UIDPLUS we never COPY or EXPUNGE - the op parks
    assert not any(c[0] in ("copy", "expunge") for c in client.calls)
    for _ in range(6):
        _exec(svc, apk, client)
    st = svc.store._conn().execute("SELECT state FROM ops WHERE kind='move'").fetchone()["state"]
    assert st == "failed"                     # parked, no mailbox corruption


def test_move_fallback_retry_does_not_copy_twice(svc):
    apk, fpk, pk = _seed(svc)
    svc.store.upsert_folder(apk, "Papierkorb", special_use="\\Trash")
    svc.trash(pk)

    class ExpungeFailsOnce(FakeWriteImap):
        def __init__(self):
            super().__init__(caps=("IMAP4REV1", "UIDPLUS"))
            self._expunge_fail = True

        def expunge(self, uids=None):
            if self._expunge_fail:
                self._expunge_fail = False
                raise OSError("expunge dropped")
            super().expunge(uids)

    client = ExpungeFailsOnce()
    _exec(svc, apk, client)                   # copy + \Deleted done, expunge fails -> op re-armed
    _exec(svc, apk, client)                   # retry: source already \Deleted -> NO second copy
    copies = [c for c in client.calls if c[0] == "copy"]
    assert len(copies) == 1                   # idempotent: exactly one COPY
    assert any(c[0] == "expunge" for c in client.calls)


# ── C5: UIDVALIDITY pin on flag/move ops (T10) ───────────────────────────────


def test_flag_op_dropped_on_uidvalidity_rotation(svc):
    apk, fpk, pk = _seed(svc)
    svc.store.set_folder_state(fpk, uidvalidity=100)     # pin the source folder
    svc.set_star(pk, True)                               # flags op pins uidvalidity=100
    client = FakeWriteImap()
    client.uidvalidity = 999                             # server rotated since enqueue
    stats = _exec(svc, apk, client)
    assert stats["done"] == 1                            # op consumed (dropped), not retried
    assert not any(c[0] == "add_flags" for c in client.calls)  # wrong-message write avoided


def test_move_op_dropped_on_uidvalidity_rotation(svc):
    apk, fpk, pk = _seed(svc)
    svc.store.set_folder_state(fpk, uidvalidity=100)
    svc.store.upsert_folder(apk, "Archiv", special_use="\\Archive")
    svc.archive(pk)                                      # move op pins uidvalidity=100
    client = FakeWriteImap()
    client.uidvalidity = 999
    stats = _exec(svc, apk, client)
    assert stats["done"] == 1
    assert not any(c[0] in ("move", "copy", "expunge") for c in client.calls)


# ── C6: flag resync is pending-op aware; shadow uses the delta (T6) ──────────


def test_flag_resync_preserves_local_star_with_pending_op(svc):
    apk, fpk, pk = _seed(svc)
    svc.set_star(pk, True)                                   # local \Flagged, flags op pending
    # server independently marks it \Seen (another client) while our op is pending
    svc.store.apply_server_flags(fpk, {1: ["\\Seen"]})
    m = svc.store.get_message(pk)
    assert m["flags"] == ["\\Flagged"]                       # local star NOT stomped
    assert m["server_flags"] == ["\\Seen"]                   # shadow tracks server truth


def test_op_flags_shadow_uses_delta_not_full_local_list(svc):
    import json as _json
    apk, fpk, pk = _seed(svc)
    # a pre-existing local flag the server never received
    svc.store._conn().execute("UPDATE messages SET flags=?, server_flags=? WHERE id=?",
                              (_json.dumps(["\\Flagged"]), _json.dumps([]), pk))
    svc.store._conn().commit()
    svc.mark_read(pk)                                        # push only \Seen
    _exec(svc, apk, FakeWriteImap())
    m = svc.store.get_message(pk)
    # shadow reflects ONLY the pushed delta (\Seen), not the untouched local \Flagged
    assert m["server_flags"] == ["\\Seen"]


# ── C7: Sent copy == delivered message (T7) + Gmail Sent dedup (T22) ─────────


def test_send_delivers_with_same_message_id_as_sent_copy(svc, monkeypatch):
    apk, fpk, pk = _seed(svc)
    svc.store.upsert_folder(apk, "Gesendet", special_use="\\Sent")
    captured = {}
    import vaf.core.email_transport as transport
    monkeypatch.setattr(transport, "send_mail",
                        lambda a, **k: captured.update(message_id=k.get("message_id")) or True)
    svc.queue_send("bob@example.com", "x@example.com", "Hi", "Body", undo_seconds=0)
    client = FakeWriteImap()
    _exec(svc, apk, client, now_ts=int(time.time()) + 5)
    assert captured["message_id"]                                # delivered with an explicit id
    assert captured["message_id"].encode() in client.appended[0][1]   # SAME id in the Sent copy


def test_gmail_over_imap_skips_sent_append(svc, monkeypatch):
    apk = svc.store.upsert_account("me@gmail.com", "imap", "me@gmail.com")
    svc.store.upsert_folder(apk, "INBOX", special_use="\\Inbox")
    svc.store.upsert_folder(apk, "[Gmail]/Sent Mail", special_use="\\Sent")
    import vaf.core.email_transport as transport
    monkeypatch.setattr(transport, "send_mail", lambda a, **k: True)
    svc.queue_send("me@gmail.com", "x@example.com", "Hi", "Body", undo_seconds=0)
    client = FakeWriteImap()
    ex = OpExecutor(svc.store, apk, client, {"provider": "imap", "email": "me@gmail.com"}, _SCOPE)
    ex.process(write_enabled=True, now_ts=int(time.time()) + 5)
    assert client.appended == []                                 # Gmail files Sent itself, no duplicate
