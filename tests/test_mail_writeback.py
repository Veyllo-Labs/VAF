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

    def has_capability(self, cap):
        return cap in self.caps

    def select_folder(self, name, readonly=True):
        self.calls.append(("select", name, readonly))
        return {}

    def add_flags(self, uids, flags):
        self.calls.append(("add_flags", tuple(uids), tuple(flags)))

    def remove_flags(self, uids, flags):
        self.calls.append(("remove_flags", tuple(uids), tuple(flags)))

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
    client = FakeWriteImap(caps=("IMAP4REV1",))  # no MOVE capability
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
