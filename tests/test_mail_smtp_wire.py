# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""What the SMTP lane writes, and what it makes of every answer, against a socket stub.

The wire carries CRLF line ends (RFC 5321 2.3.8): a server hardened against SMTP smuggling
refuses a message with a bare LF, and smtplib sends bytes as given. The stored bytes keep their
own line ends, so the Sent copy and the Message-ID do not change. And every answer of the server
is read: smtplib's `mail()`, `rcpt()` and `data()` return a refusal instead of raising it, so a
lane that ignores their return values reports a refused mail as sent.

Everything runs against `tests/mail_smtp_stub.py` on 127.0.0.1, through `queue_send` and the
real outbox drain where the ledger matters; no real server.

MUTATIONS, each turns the named test red:
- `_wire_bytes` back to `m.as_bytes()` -> test_strict_stub_takes_every_shape,
  test_wire_equals_stored_modulo_line_ends;
- the wire built with `policy.default.clone(linesep="\\r\\n")` (refolds the long headers)
  -> test_wire_equals_stored_modulo_line_ends;
- the return value of `conn.data` ignored -> test_a_refused_message_is_not_sent;
- a DATA command refused (no 354) left to the ambiguous branch
  -> test_a_refused_data_command_is_not_ambiguous;
- the MAIL code check dropped -> test_a_refused_sender_sends_no_data;
- the refused recipient skipped and the rest delivered
  -> test_a_refused_recipient_stops_the_mail.
"""
import base64
import os
import re
from email import message_from_bytes

import pytest
from mail_smtp_stub import SmtpStub, verb_lines

import vaf.mail.crypto as mail_crypto
from vaf.mail import case_token, compose
from vaf.mail import sender as sender_mod
from vaf.mail.imap_client import NullImapClient
from vaf.mail.service import MailService
from vaf.mail.store import MailStore
from vaf.mail.writeback import OpExecutor

SCOPE = "12345678-1234-1234-1234-123456789abc"
OWN = "bob@example.com"
_BARE_LF = re.compile(rb"(?<!\r)\n")
_BARE_CR = re.compile(rb"\r(?!\n)")


@pytest.fixture(autouse=True)
def _pinned(monkeypatch):
    old = mail_crypto._cached_key
    mail_crypto._cached_key = os.urandom(32)
    monkeypatch.setattr(case_token, "_root_secret", lambda: "unit-test-root-secret-of-thirty-two-bytes")
    yield
    mail_crypto._cached_key = old


@pytest.fixture()
def svc(tmp_path):
    store = MailStore(SCOPE, base_dir=tmp_path)
    s = MailService.__new__(MailService)
    s.user_scope_id = SCOPE
    s.store = store
    store.upsert_account(OWN, "imap", OWN)
    yield s
    store.close()


@pytest.fixture()
def stub(monkeypatch):
    made = []

    def make(**kw) -> SmtpStub:
        s = SmtpStub(**kw)
        made.append(s)
        return s

    import smtplib
    monkeypatch.setattr(smtplib.SMTP, "starttls", lambda self, *a, **k: (220, b""))
    monkeypatch.setattr(sender_mod, "assert_safe_remote_host", lambda *a, **k: None)
    monkeypatch.setattr(sender_mod, "get_email_credentials",
                        lambda *a, **k: {"type": "imap", "password": "pw"})
    yield make
    for s in made:
        s.close()


def _account(port: int):
    return {"provider": "imap", "account_id": OWN, "email": OWN,
            "smtp_host": "127.0.0.1", "smtp_port": port}


def _drain(svc, port):
    apk = svc.store.account_pk(OWN)
    OpExecutor(svc.store, apk, NullImapClient(), _account(port), SCOPE).process(
        write_enabled=False, allowed_kinds={"send"})


def _raw(svc, op_id) -> bytes:
    return base64.b64decode(svc.store.get_op(int(op_id))["payload"]["raw_b64"])


def _old_wire(raw: bytes) -> bytes:
    """The wire as the lane wrote it before: Bcc dropped, the compat32 default line end."""
    m = message_from_bytes(raw)
    del m["Bcc"]
    return m.as_bytes()


_PDF = b"%PDF-1.4\n" + bytes(range(256)) * 40 + b"\n%%EOF\n"
_REFS = " ".join(f"<ref{i}.{'x' * 30}@example.org>" for i in range(10))


def _shapes():
    """The six message shapes the lane delivers: plain, a body with its own CRLF and a lone
    CR, long folded headers with a Bcc and ten References, attachments, a UTF-8 mail, and a
    revised draft."""
    long_subject = "Quarterly figures " * 12
    plain = bytes(compose.build_message(OWN, "anna@example.org", "Hallo", "Text\nzweite Zeile"))
    crlf = bytes(compose.build_message(OWN, "anna@example.org", "CR", "a\r\nb\rc\nd"))
    folded = bytes(compose.build_message(
        OWN, ", ".join(f"Person Number {i} <p{i}@example.org>" for i in range(8)), long_subject,
        "Text", bcc="hidden@example.org", in_reply_to="<ref9@example.org>", references=_REFS))
    attach = bytes(compose.build_message(
        OWN, "anna@example.org", "Anhang", "Siehe Anhang",
        attachments=[{"filename": "a.pdf", "content_type": "application/pdf", "payload": _PDF},
                     {"filename": "notes.txt", "content_type": "text/plain",
                      "payload": b"line one\nline two\r\nline three\n"}]))
    utf8 = bytes(compose.build_message(OWN, "Иван <иван@пример.рф>", "Привет", "Текст\nещё",
                                       utf8=True))
    revised = compose.revise_message(folded, subject="Neu", body_text="Neuer Text\nmit Zeilen")
    return {"plain": plain, "crlf": crlf, "folded": folded, "attach": attach, "utf8": utf8,
            "revised": revised}


def _clean(data: bytes) -> bool:
    return not _BARE_LF.search(data) and not _BARE_CR.search(data)


def test_wire_equals_stored_modulo_line_ends():
    for name, raw in _shapes().items():
        wire = sender_mod._wire_bytes(raw)
        assert _clean(wire), name
        assert wire.replace(b"\r\n", b"\n") == _old_wire(raw), name
        assert message_from_bytes(wire)["Message-ID"] == message_from_bytes(raw)["Message-ID"], name
        assert b"Bcc" not in wire.split(b"\r\n\r\n", 1)[0], name


@pytest.mark.parametrize("shape", ["plain", "attach", "utf8", "revised"])
def test_strict_stub_takes_every_shape(svc, stub, shape):
    """Through `queue_send` and the outbox drain: a server that refuses bare LF answers 250,
    and the stored bytes are what was queued."""
    s = stub(smtputf8=True, strict=True)
    if shape == "utf8":
        out = svc.queue_send(OWN, "Иван <иван@пример.рф>", "Привет", "Текст\nещё", undo_seconds=0)
    elif shape == "attach":
        out = svc.queue_send(OWN, "anna@example.org", "Anhang", "Siehe\nAnhang", undo_seconds=0,
                             attachments=[{"filename": "a.pdf", "content_type": "application/pdf",
                                           "payload": _PDF}])
    elif shape == "revised":
        out = svc.queue_send(OWN, "anna@example.org", "Entwurf", "Alt", hold=True)
        assert svc.revise_draft(int(out["op_id"]), subject="Neu", body="Neu\nmit Zeilen")
        assert svc.approve_draft(int(out["op_id"]))
    else:
        out = svc.queue_send(OWN, "anna@example.org", "Hallo", "Text\nzweite Zeile",
                             undo_seconds=0)
    stored = _raw(svc, out["op_id"])
    _drain(svc, s.port)
    outcome = svc.send_outcome(out["op_id"])
    assert outcome["state"] == "done" and outcome["delivery"] == "sent", outcome
    assert s.message and _clean(s.message)
    assert len(verb_lines(s, b"MAIL")) == 1 and len(verb_lines(s, b"RCPT")) == 1
    assert _raw(svc, out["op_id"]) == stored


def _plain_msg(port: int, to: str = "anna@example.org"):
    raw = bytes(compose.build_message(OWN, to, "Hallo", "Text"))
    return sender_mod.OutgoingMessage(account=_account(port), raw_bytes=raw, to=to)


@pytest.mark.parametrize("end, cls", [(b"550 5.7.1 rejected", "permanent"),
                                      (b"451 4.3.0 try later", "transient")])
def test_a_refused_message_is_not_sent(svc, stub, end, cls):
    s = stub(end=end)
    res = sender_mod.send(_plain_msg(s.port))
    assert not res.ok and res.classification == cls and res.handed_off is False, res
    assert end.split()[0].decode() in res.error
    # Through the drain: the ledger says failed, never sent.
    s2 = stub(end=end)
    out = svc.queue_send(OWN, "anna@example.org", "Hallo", "Text", undo_seconds=0)
    _drain(svc, s2.port)
    outcome = svc.send_outcome(out["op_id"])
    assert outcome["delivery"] != "sent" and outcome["state"] != "done", outcome
    if cls == "permanent":
        assert outcome["delivery"] == "failed" and outcome["state"] == "failed", outcome


def test_a_disconnect_after_the_dot_stays_ambiguous(stub):
    s = stub(drop_after_dot=True)
    res = sender_mod.send(_plain_msg(s.port))
    assert not res.ok and res.classification == "ambiguous" and res.handed_off is True, res


@pytest.mark.parametrize("reply, cls", [(b"554 5.5.1 no valid recipients", "permanent"),
                                        (b"451 4.3.0 busy", "transient")])
def test_a_refused_data_command_is_not_ambiguous(stub, reply, cls):
    """smtplib raises for a DATA command not answered with 354, before any byte of the
    message went out: the server said no, so the mail is not 'maybe delivered'."""
    s = stub(data=reply)
    res = sender_mod.send(_plain_msg(s.port))
    assert not res.ok and res.classification == cls and res.handed_off is False, res


@pytest.mark.parametrize("mail, cls", [(b"550 5.7.1 sender denied", "permanent"),
                                       (b"421 4.7.0 slow down", "transient")])
def test_a_refused_sender_sends_no_data(stub, mail, cls):
    s = stub(mail=mail)
    res = sender_mod.send(_plain_msg(s.port))
    assert not res.ok and res.classification == cls and res.handed_off is False, res
    s.close()
    assert s.command(b"DATA") == [] and s.command(b"RCPT") == [], s.lines


@pytest.mark.parametrize("replies, cls", [
    ([b"250 ok", b"550 5.1.1 no such user"], "permanent"),
    ([b"550 5.1.1 no such user"], "permanent"),
    ([b"451 4.2.0 later", b"452 4.2.2 full"], "transient"),
    ([b"450 4.2.0 later", b"550 5.1.1 no such user"], "permanent"),
])
def test_a_refused_recipient_stops_the_mail(stub, replies, cls):
    s = stub(rcpt=replies)
    res = sender_mod.send(_plain_msg(s.port, to="anna@example.org, otto@example.org"))
    assert not res.ok and res.classification == cls and res.handed_off is False, res
    refused = [a for a, r in zip(["anna@example.org", "otto@example.org"],
                                 replies + replies[-1:]) if not r.startswith(b"25")]
    for addr in refused:
        assert addr in res.error, res.error
    s.close()
    assert s.command(b"DATA") == [], s.lines
    assert s.command(b"RSET"), s.lines


def test_no_dead_recipients_refused_branch():
    src = open(sender_mod.__file__, encoding="utf-8").read()
    assert "SMTPRecipientsRefused" not in src
