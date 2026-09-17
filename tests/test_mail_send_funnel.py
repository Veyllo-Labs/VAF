# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The one send funnel (EMAIL_CLIENT.md, "Native send"): every lane queues through
MailService.queue_send, the outbox records the Message-ID before the wire, stamps the
delivery, marks the answered mail, and a held draft never leaves until approved. The
agent's send, reply and forward tools reach the sender only through the outbox, and their
private Message-ID resolvers are gone. Isolated: tmp store, pinned key, the native sender
monkeypatched.

MUTATION: drop record_sent_id from queue_send and the ledger test goes red; drop the
set_answered call from _op_send and the answered test goes red; let pending_ops read held
rows and the draft test goes red; put the direct sender.send call back into a tool and the
guard goes red."""
import ast
import os
import re
import time
from pathlib import Path

import pytest

import vaf.mail.crypto as mail_crypto
import vaf.mail.sender as sender
from vaf.core.platform import Platform
from vaf.mail import case_token
from vaf.mail.parser import ParsedMessage, parse_message
from vaf.mail.service import MailService
from vaf.mail.store import MailStore
from vaf.mail.writeback import OpExecutor

REPO = Path(__file__).resolve().parents[1]
_SCOPE = "12345678-1234-1234-1234-123456789abc"
_ACC = {"provider": "imap", "account_id": "bob@example.com", "email": "bob@example.com"}


@pytest.fixture(autouse=True)
def _pinned(monkeypatch):
    old = mail_crypto._cached_key
    mail_crypto._cached_key = os.urandom(32)
    monkeypatch.setattr(case_token, "_root_secret", lambda: "unit-test-root-secret-of-thirty-two-bytes")
    yield
    mail_crypto._cached_key = old


class FakeImap:
    def __init__(self):
        self.appended = []

    def has_capability(self, cap):
        return False

    def select_folder(self, name, readonly=True):
        return {}

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


def _seed(svc):
    apk = svc.store.upsert_account("bob@example.com", "imap", "bob@example.com")
    fpk = svc.store.upsert_folder(apk, "INBOX", special_use="\\Inbox")
    svc.store.upsert_folder(apk, "Sent", special_use="\\Sent")
    pk = svc.store.ingest_message(apk, fpk, 1, ParsedMessage(
        message_id="<q@example.org>", subject="Angebot", from_addr="Alice <alice@example.org>",
        to_addrs="bob@example.com", date_ts=1_700_000_000, body_text="Passt das?"), server_flags=[])
    return apk, fpk, pk


def _drain(svc, apk, client=None, write=True, now_ts=None):
    return OpExecutor(svc.store, apk, client or FakeImap(), _ACC, _SCOPE).process(
        write_enabled=write, now_ts=now_ts if now_ts is not None else int(time.time()) + 5)


def test_queue_send_records_the_id_before_the_wire_and_the_outbox_stamps_the_delivery(svc, monkeypatch):
    apk, fpk, pk = _seed(svc)
    sent = []
    monkeypatch.setattr(sender, "send", lambda msg: sent.append(msg) or sender.SendResult(True, "ok", handed_off=True))
    out = svc.queue_send("bob@example.com", "alice@example.org", "Re: Angebot", "Ja, passt.",
                         in_reply_to="<q@example.org>", references="<q@example.org>", undo_seconds=0,
                         sent_by="agent", reply_to_pk=pk, thread_id=1)
    mid = out["message_id"]
    assert mid.startswith("<") and mid.endswith("@example.com>"), "minted on the sending address's domain"
    row = svc.store.sent_id(apk, mid)
    assert row and row["delivery"] == "queued" and row["sent_by"] == "agent" and row["op_id"] == out["op_id"]
    assert svc.store.is_sent_id(apk, mid), "recognised as ours before it left"
    client = FakeImap()
    assert _drain(svc, apk, client)["done"] == 1
    assert sent[0].message_id == mid and b"In-Reply-To: <q@example.org>" in sent[0].raw_bytes
    assert svc.store.sent_id(apk, mid)["delivery"] == "sent" and svc.store.sent_id(apk, mid)["sent_at"]
    assert svc.store.get_message(pk)["answered_at"], "the answered mail is marked when the reply left"
    assert client.appended and client.appended[0][0] == "Sent"


def test_an_in_reply_to_alone_marks_the_answered_mail(svc, monkeypatch):
    apk, fpk, pk = _seed(svc)
    monkeypatch.setattr(sender, "send", lambda msg: sender.SendResult(True, "ok"))
    svc.queue_send("bob@example.com", "alice@example.org", "Re: Angebot", "Ja.", in_reply_to="<q@example.org>", undo_seconds=0)
    _drain(svc, apk)
    assert svc.store.get_message(pk)["answered_at"], "the compose window's reply resolves through pk_by_message_id"


def test_a_failed_and_an_ambiguous_send_stamp_the_ledger(svc, monkeypatch):
    apk, fpk, pk = _seed(svc)
    monkeypatch.setattr(sender, "send", lambda msg: sender.SendResult(False, "ambiguous", handed_off=True, error="lost after DATA"))
    out = svc.queue_send("bob@example.com", "a@example.org", "s", "b", undo_seconds=0)
    _drain(svc, apk)
    assert svc.store.sent_id(apk, out["message_id"])["delivery"] == "ambiguous"
    assert svc.store.get_op(out["op_id"])["state"] == "failed"
    monkeypatch.setattr(sender, "send", lambda msg: sender.SendResult(False, "permanent", error="no lane"))
    out2 = svc.queue_send("bob@example.com", "a@example.org", "s", "b", undo_seconds=0)
    _drain(svc, apk)
    assert svc.store.sent_id(apk, out2["message_id"])["delivery"] == "failed"
    assert not svc.store.get_message(pk)["answered_at"], "nothing left, nothing answered"


def test_the_rate_cap_counts_whole_mailboxes_not_substrings(svc):
    apk, _fpk, _pk = _seed(svc)
    for to in ("Ann <ann@example.org>", "hann@example.org", "ANN@example.org, bob@example.org"):
        svc.queue_send("bob@example.com", to, "s", "b", undo_seconds=0, sent_by="front_office", hold=True)
    assert svc.store.front_office_replies_since(apk, "ann@example.org", "2000-01-01T00:00:00") == 2, "a mailbox, never a substring"
    assert svc.store.front_office_replies_since(apk, "hann@example.org", "2000-01-01T00:00:00") == 1
    assert svc.store.front_office_replies_since(apk, "bob@example.org", "2000-01-01T00:00:00") == 1


def test_a_held_draft_never_leaves_until_approved_and_a_discard_records_itself(svc, monkeypatch):
    apk, fpk, pk = _seed(svc)
    sent = []
    monkeypatch.setattr(sender, "send", lambda msg: sent.append(msg) or sender.SendResult(True, "ok"))
    out = svc.queue_send("bob@example.com", "alice@example.org", "Re: Angebot", "Entwurf", undo_seconds=0,
                         hold=True, sent_by="front_office", reply_to_pk=pk, thread_id=1)
    assert out["held"] and svc.store.get_op(out["op_id"])["state"] == "held"
    assert svc.store.pending_ops(apk) == [], "the drain never sees a held op"
    assert _drain(svc, apk)["done"] == 0 and sent == []
    assert svc.store.reclaim_stale_ops(apk) == 0 and svc.store.get_op(out["op_id"])["state"] == "held", "the sweep leaves it alone"
    drafts = svc.list_drafts(thread_id=1)
    assert len(drafts) == 1 and drafts[0]["op_id"] == out["op_id"] and drafts[0]["body"] == "Entwurf"
    assert drafts[0]["sent_by"] == "front_office" and drafts[0]["reply_to_pk"] == pk
    assert svc.list_drafts(account_id="nobody@example.com") == []
    assert svc.approve_draft(out["op_id"]) is True and svc.approve_draft(out["op_id"]) is False
    assert svc.store.sent_id(apk, out["message_id"])["delivery"] == "queued"
    assert _drain(svc, apk)["done"] == 1 and len(sent) == 1
    assert svc.store.get_message(pk)["answered_at"]
    second = svc.queue_send("bob@example.com", "alice@example.org", "Re: Angebot", "Zweiter", undo_seconds=0, hold=True)
    assert svc.discard_draft(second["op_id"]) is True and svc.store.get_op(second["op_id"])["state"] == "discarded"
    assert svc.store.sent_id(apk, second["message_id"])["delivery"] == "discarded"
    assert svc.discard_draft(out["op_id"]) is False, "only a held op can be discarded"
    assert svc.list_drafts() == []


def test_a_case_stamps_its_anchor_and_the_agents_mail_carries_the_loop_guard(svc, monkeypatch):
    apk, fpk, pk = _seed(svc)
    monkeypatch.setattr(sender, "send", lambda msg: sender.SendResult(True, "ok"))
    case_id = case_token.mint_case_id()
    out = svc.queue_send("bob@example.com", "alice@example.org", "Re: Angebot", "Antwort", undo_seconds=0,
                         case_id=case_id, agent_written=True, root_anchor="<root.x.y@example.com>",
                         references="<q@example.org>", sent_by="front_office")
    mid = out["message_id"]
    assert case_token.verify_anchor(_SCOPE, "bob@example.com", mid) == case_id
    assert case_token.verify_anchor("00000000-0000-0000-0000-000000000000", "bob@example.com", mid) is None
    raw = svc.store.get_op(out["op_id"])["payload"]["raw_b64"]
    import base64
    wire = base64.b64decode(raw)
    assert b"Auto-Submitted: auto-replied" in wire and b"X-Auto-Response-Suppress: OOF, AutoReply" in wire
    assert b"References: <root.x.y@example.com> <q@example.org>" in wire
    assert svc.store.sent_id(apk, mid)["case_id"] == case_id
    plain = svc.queue_send("bob@example.com", "alice@example.org", "Hallo", "Von mir", undo_seconds=0)
    wire2 = base64.b64decode(svc.store.get_op(plain["op_id"])["payload"]["raw_b64"])
    assert b"Auto-Submitted" not in wire2, "the person's own mail is never marked automatic"


def test_our_own_mail_coming_back_is_recognised_at_ingest(svc, monkeypatch):
    apk, fpk, pk = _seed(svc)
    monkeypatch.setattr(sender, "send", lambda msg: sender.SendResult(True, "ok"))
    out = svc.queue_send("bob@example.com", "alice@example.org", "Hallo", "Text", undo_seconds=0)
    _drain(svc, apk)
    raw = (f"From: Bob <bob@example.com>\nTo: alice@example.org\nSubject: Hallo\nMessage-ID: {out['message_id']}\n\nText\n").encode()
    back = svc.store.ingest_message(apk, fpk, 2, parse_message(raw), raw=raw)
    assert svc.store.message_auth([back])[back]["machine_kind"] == "own_loop"


# ── the tools reach the sender only through the outbox ────────────────────────────────

_TOOLS = ("send_mail", "reply_mail", "manage_mail")


def _calls(path: Path, attr: str) -> list:
    out = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == attr:
            out.append(node.lineno)
    return out


def test_no_tool_calls_the_sender_directly_and_the_private_resolvers_are_gone():
    for name in _TOOLS:
        path = REPO / "vaf" / "tools" / f"{name}.py"
        src = path.read_text(encoding="utf-8")
        assert not re.search(r"\bsender\.send\(", src), f"{name} sends directly"
        assert "OutgoingMessage(" not in src, f"{name} builds wire bytes itself"
        assert "queue_send(" in src and "deliver_queued_sends(" in src, f"{name} does not use the funnel"
        assert "_pk_by_message_id" not in src and "_find_pk_by_message_id" not in src, f"{name} keeps a private resolver"
    writeback = REPO / "vaf" / "mail" / "writeback.py"
    assert _calls(writeback, "send"), "the outbox is the one caller of sender.send"


def test_the_send_tool_delivers_through_the_outbox(monkeypatch, tmp_path):
    import vaf.tools.send_mail as sm
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    monkeypatch.setattr(sm, "list_accounts_for_user", lambda *a, **k: ["bob@example.com"])
    monkeypatch.setattr(sm, "get_account", lambda *a, **k: dict(_ACC))
    sent = []
    monkeypatch.setattr(sender, "send", lambda msg: sent.append(msg) or sender.SendResult(True, "ok"))
    out = sm.SendMailTool().run(to="alice@example.org", subject="Hallo", body="Text", username="alice", user_scope_id=_SCOPE)
    assert "sent to alice@example.org" in out and len(sent) == 1
    store = MailStore(_SCOPE)
    try:
        apk = store.account_pk("bob@example.com")
        row = store.sent_id(apk, sent[0].message_id)
        assert row and row["delivery"] == "sent" and row["sent_by"] == "agent"
        assert store.get_op(row["op_id"])["state"] == "done"
    finally:
        store.close()
    monkeypatch.setattr(sender, "send", lambda msg: sender.SendResult(False, "ambiguous", handed_off=True, error="lost"))
    assert "do NOT resend" in sm.SendMailTool().run(to="alice@example.org", subject="x", body="y", username="alice", user_scope_id=_SCOPE)
    monkeypatch.setattr(sender, "send", lambda msg: sender.SendResult(False, "transient", error="4xx"))
    assert "queued in the outbox" in sm.SendMailTool().run(to="alice@example.org", subject="x", body="y", username="alice", user_scope_id=_SCOPE)


def test_the_reply_tool_marks_the_answered_mail_through_the_outbox(monkeypatch, tmp_path):
    import vaf.tools.reply_mail as rm
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    store = MailStore(_SCOPE)
    apk = store.upsert_account("bob@example.com", "imap", "bob@example.com")
    fpk = store.upsert_folder(apk, "INBOX", special_use="\\Inbox")
    pk = store.ingest_message(apk, fpk, 1, ParsedMessage(
        message_id="<q@example.org>", subject="Angebot", from_addr="Alice <alice@example.org>",
        to_addrs="bob@example.com", date_ts=1_700_000_000, body_text="Passt das?"), server_flags=[])
    store.close()
    monkeypatch.setattr("vaf.core.email_transport.get_account", lambda *a, **k: dict(_ACC))
    sent = []
    monkeypatch.setattr(sender, "send", lambda msg: sent.append(msg) or sender.SendResult(True, "ok"))
    out = rm.ReplyMailTool().run(message_id="<q@example.org>", body="Ja, passt.", username="alice", user_scope_id=_SCOPE)
    assert out.startswith("Reply sent to") and len(sent) == 1
    assert b"In-Reply-To: <q@example.org>" in sent[0].raw_bytes
    again = MailStore(_SCOPE)
    try:
        assert again.get_message(pk)["answered_at"]
        assert again.is_sent_id(apk, sent[0].message_id)
    finally:
        again.close()
