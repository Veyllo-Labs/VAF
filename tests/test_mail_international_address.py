# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Internationalised mail addresses are delivered: IDNA2008 domains, SMTPUTF8 local parts.

The envelope form of an address is `vaf.mail.addressing.envelope_address`: the A-label of a
domain outside ASCII under strict IDNA2008 (the standard library's codec turns ``faß.de`` into
``fass.de``, another domain), the local part as written. A mail with a local part outside
ASCII is built with UTF-8 headers and sent with SMTPUTF8, which the server must offer; every
SMTP session records the answer per account, and a fresh "no" refuses the next such mail
before it is queued. The SMTP side runs against a socket stub on 127.0.0.1, never a real
server.

MUTATIONS, each turns the named test red:
- restore the blanket ``isascii`` refusal in `queue_send` -> test_international_addresses_queue;
- `domain.encode("idna")` in `envelope_address` -> test_envelope_address_forms,
  test_invalid_domains_are_refused_with_a_code, test_the_card_bracket_is_the_delivered_domain;
- `idna.encode(..., uts46=True)` -> test_invalid_domains_are_refused_with_a_code (UTS46 agrees
  with strict IDNA2008 on every valid domain, so only the refusals tell them apart);
- drop the recipient check in `queue_send` -> test_invalid_domains_are_refused_with_a_code
  (the Bcc of a UTF-8 mail);
- drop the From check in `queue_send` -> test_the_accounts_own_address_is_checked_too;
- drop the 253-octet cap -> test_the_whole_domain_is_capped_at_253_octets,
  test_invalid_domains_are_refused_with_a_code;
- the control-character check only for a local part outside ASCII
  -> test_a_control_character_is_refused_in_any_local_part,
  test_invalid_domains_are_refused_with_a_code;
- drop the A-label conversion in `compose.message_id_domain`
  -> test_the_message_id_domain_is_an_a_label,
  test_a_case_answer_from_an_idn_account_builds_and_sends;
- drop its `vaf.invalid` fallback -> test_the_message_id_domain_is_an_a_label;
- log in with the address as written -> test_a_case_answer_from_an_idn_account_builds_and_sends;
- log the refusal text in `OpExecutor.process` -> test_the_app_log_names_the_refusal_not_the_address;
- drop a code from page.tsx `REFUSED_ADDRESS`, or a sentence from a catalogue
  -> test_every_refusal_code_has_a_sentence_in_every_catalogue;
- no capital-letter code, no control-character check
  -> test_invalid_domains_are_refused_with_a_code;
- `str.lower()` on the whole label -> test_invalid_domains_are_refused_with_a_code (ПРИМЕР);
- drop `envelope_header` in `queue_send` -> test_ascii_mail_headers_use_a_labels;
- drop `utf8=` in `queue_send` -> test_utf8_mail_keeps_the_address,
  test_revising_a_utf8_draft_keeps_the_addresses;
- always `utf8=True` -> test_plain_mail_bytes_unchanged;
- drop the SMTPUTF8 mail options -> test_stub_with_smtputf8_delivers;
- move the extension check after the login -> test_stub_without_smtputf8_refuses_before_auth;
- drop the record in `writeback._op_send` -> test_the_record_refuses_at_queue_time,
  test_any_session_records_the_answer;
- drop the queue-time check -> test_the_record_refuses_at_queue_time;
- drop the age test -> test_a_stale_record_does_not_refuse;
- drop the A-labels from RCPT -> test_stub_with_smtputf8_delivers,
  test_an_ascii_local_part_needs_no_smtputf8;
- `revise_message` back to plain `policy.default` -> test_revising_a_utf8_draft_keeps_the_addresses;
- `set_account_state` without its write lock -> test_account_state_writes_keep_each_others_keys.
"""
import base64
import os
import socket
import threading
import time

import pytest

import vaf.mail.crypto as mail_crypto
from vaf.core.shown_text import mail_address
from vaf.mail import case_token, compose
from vaf.mail import sender as sender_mod
from vaf.mail.addressing import UndeliverableAddress, envelope_address
from vaf.mail.imap_client import NullImapClient
from vaf.mail.service import SMTPUTF8_RECORD_MAX_AGE_SEC, MailService
from vaf.mail.store import MailStore
from vaf.mail.writeback import OpExecutor

SCOPE = "12345678-1234-1234-1234-123456789abc"
OWN = "bob@example.com"
EAI = "иван@пример.рф"
EAI_ENVELOPE = "иван@xn--e1afmkfd.xn--p1ai"
IDN = "alice@bücher.de"
CONFUSABLE = "alice@exаmple.com"


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


def _ops(svc) -> int:
    return int(svc.store._conn().execute("SELECT COUNT(*) FROM ops").fetchone()[0])


def _raw(svc, op_id) -> bytes:
    return base64.b64decode(svc.store.get_op(int(op_id))["payload"]["raw_b64"])


def _headers(raw: bytes) -> bytes:
    return raw.split(b"\n\n", 1)[0]


# ---------------------------------------------------------------- the funnel


@pytest.mark.parametrize("field", ["to", "cc", "bcc"])
@pytest.mark.parametrize("addr", [EAI, f"Иван <{EAI}>", IDN, CONFUSABLE])
def test_international_addresses_queue(svc, field, addr):
    before = _ops(svc)
    args = {"to": "anna@example.org", "cc": "", "bcc": ""}
    args[field] = addr
    out = svc.queue_send(OWN, args["to"], "Hallo", "Text", cc=args["cc"], bcc=args["bcc"],
                         hold=True)
    assert out["op_id"] and _ops(svc) == before + 1
    # The payload keeps what was written: the cards show and mark exactly that.
    assert svc.store.get_op(out["op_id"])["payload"][field] == addr


def test_envelope_address_forms():
    assert envelope_address(IDN) == "alice@xn--bcher-kva.de"
    # IDNA 2003 would deliver this to fass.de, a different domain.
    assert envelope_address("x@faß.de") == "x@xn--fa-hia.de"
    assert envelope_address(EAI) == EAI_ENVELOPE
    assert envelope_address("a@Bücher.de") == "a@xn--bcher-kva.de"
    assert envelope_address("Anna.Smith@Example.COM") == "Anna.Smith@Example.COM"
    assert envelope_address("a@例え.テスト") == "a@xn--r8jz45g.xn--zckzah"


@pytest.mark.parametrize("addr,code", [
    ("alice@ｅxample.com", "domain"),     # full-width e: UTS46 would map it to example.com
    ("a@ⅷ.com", "domain"),               # Roman numeral eight: mapped to viii.com
    ("a@ﬁle.com", "domain"),             # ligature: mapped to file.com
    ("a@ПРИМЕР.рф", "domain_case"),
    ("a@\u212aü.de", "domain"),     # KELVIN SIGN: lower() would turn it into k
    ("a\u0085b@пример.рф", "local_part"),
    ("a\x00b@bücher.de", "local_part"),  # an ASCII local part at an IDN domain
    ("a\x00b@example.com", "local_part"),  # a plain ASCII address
    ("a@" + ".".join(["bücher"] * 20) + ".de", "domain"),  # A-form over 253 octets
])
@pytest.mark.parametrize("field", ["to", "bcc"])
def test_invalid_domains_are_refused_with_a_code(svc, addr, code, field):
    """In the To of a mail with ASCII headers, and in the Bcc of one that needs SMTPUTF8 (a
    UTF-8 mail keeps its headers as written, so only the funnel's own check refuses there)."""
    before = _ops(svc)
    args = {"to": "anna@example.org" if field == "to" else EAI, "bcc": ""}
    args[field] = addr
    with pytest.raises(UndeliverableAddress) as caught:
        svc.queue_send(OWN, args["to"], "Hallo", "Text", bcc=args["bcc"], hold=True)
    assert caught.value.code == code and caught.value.address == addr
    message = str(caught.value)
    message.encode("cp1252")
    assert "Nothing was queued" in message
    assert "SMTPUTF8" not in message and "IDNA" not in message
    assert ascii_shown(addr) in message
    assert _ops(svc) == before


def test_the_whole_domain_is_capped_at_253_octets():
    """Every label is valid on its own; only the length of the whole A-form refuses it."""
    fits = "a@" + ".".join(["bücher"] * 17) + ".de"
    assert len(envelope_address(fits).rsplit("@", 1)[1]) <= 253
    with pytest.raises(UndeliverableAddress) as caught:
        envelope_address("a@" + ".".join(["bücher"] * 20) + ".de")
    assert caught.value.code == "domain"


def test_a_control_character_is_refused_in_any_local_part():
    for addr in ["a\x00b@bücher.de", "a\x00b@example.com", "a\x7fb@example.com"]:
        with pytest.raises(UndeliverableAddress) as caught:
            envelope_address(addr)
        assert caught.value.code == "local_part", addr
    # A plain address is untouched.
    assert envelope_address("first.last+tag@example.com") == "first.last+tag@example.com"


def test_the_message_id_domain_is_an_a_label():
    """A Message-ID carries no SMTPUTF8: a U-label there makes the mail unserialisable."""
    assert compose.message_id_domain("a@Пример.рф") == "xn--e1afmkfd.xn--p1ai"
    assert compose.message_id_domain("Bob <bob@bücher.de>") == "xn--bcher-kva.de"
    # A domain no server can take falls back to the reserved one.
    assert compose.message_id_domain("a@ｅxample.com") == "vaf.invalid"
    assert compose.message_id_domain("bob@example.com") == "example.com"


def test_the_accounts_own_address_is_checked_too(svc):
    """The From of a mail that needs SMTPUTF8 is written as is, so only the funnel's check
    keeps an own address no server can take off the wire."""
    svc.store.upsert_account("me@ｅxample.com", "imap", "me@ｅxample.com")
    before = _ops(svc)
    with pytest.raises(UndeliverableAddress) as caught:
        svc.queue_send("me@ｅxample.com", EAI, "Hallo", "Text", hold=True)
    assert caught.value.code == "domain" and caught.value.address == "me@ｅxample.com"
    assert _ops(svc) == before


def ascii_shown(addr: str) -> str:
    from vaf.core.shown_text import ascii_marks
    return ascii_marks(mail_address(addr))


def test_ascii_mail_headers_use_a_labels(svc):
    out = svc.queue_send(OWN, "Jürgen M <jm@bücher.de>", "Hallo", "Text", hold=True)
    head = _headers(_raw(svc, out["op_id"]))
    assert head.isascii(), head
    assert b"To: =?utf-8?q?J=C3=BCrgen?= M <jm@xn--bcher-kva.de>" in head, head


def test_utf8_mail_keeps_the_address(svc):
    out = svc.queue_send(OWN, f"Иван <{EAI}>", "Привет", "Text", cc=IDN, hold=True)
    head = _headers(_raw(svc, out["op_id"]))
    assert f"To: Иван <{EAI}>".encode() in head, head
    assert f"Cc: {IDN}".encode() in head, head
    assert b"From: bob@example.com" in head


def test_plain_mail_bytes_unchanged(svc, monkeypatch):
    """A mail with plain addresses is built exactly as before, a non-ASCII name included."""
    monkeypatch.setattr(compose, "formatdate", lambda **k: "Sat, 07 Nov 2026 08:00:00 -0000")
    monkeypatch.setattr(compose, "make_msgid", lambda domain=None: f"<fixed@{domain}>")
    out = svc.queue_send(OWN, "Jürgen <j@x.de>, anna@example.org", "Grüße", "Text",
                         cc="c@example.org", bcc="b@example.org", hold=True)
    expected = bytes(compose.build_message(OWN, "Jürgen <j@x.de>, anna@example.org", "Grüße",
                                           "Text", cc="c@example.org", bcc="b@example.org"))
    raw = _raw(svc, out["op_id"])
    assert raw == expected
    assert b"To: =?utf-8?q?J=C3=BCrgen?= <j@x.de>, anna@example.org" in raw


def test_the_card_bracket_is_the_delivered_domain():
    for addr in [IDN, EAI, CONFUSABLE, "x@faß.de", "a@例え.テスト", "a@مثال.إختبار",
                 "a@βάση.gr", "a@日本.jp", "a@Bücher.de"]:
        shown = str(mail_address(addr))
        bracket = shown.rsplit(" [", 1)[1].rstrip("]")
        assert bracket.lower() == envelope_address(addr).rsplit("@", 1)[1].lower(), addr


# ---------------------------------------------------------------- the wire


class _Stub:
    """A one-connection SMTP server on 127.0.0.1 that records every line it reads."""

    def __init__(self, smtputf8: bool):
        self.smtputf8, self.lines = smtputf8, []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.sock.settimeout(10)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        conn.settimeout(10)
        f = conn.makefile("rb")
        try:
            conn.sendall(b"220 stub\r\n")
            while True:
                line = f.readline()
                if not line:
                    break
                self.lines.append(line)
                up = line.upper()
                if up.startswith(b"EHLO"):
                    conn.sendall(b"250-stub\r\n250-8BITMIME\r\n"
                                 + (b"250-SMTPUTF8\r\n" if self.smtputf8 else b"")
                                 + b"250 AUTH PLAIN\r\n")
                elif up.startswith(b"AUTH"):
                    conn.sendall(b"235 ok\r\n")
                elif up.startswith(b"DATA"):
                    conn.sendall(b"354 go\r\n")
                    while True:
                        body = f.readline()
                        self.lines.append(body)
                        if body in (b".\r\n", b""):
                            break
                    conn.sendall(b"250 queued\r\n")
                elif up.startswith(b"QUIT"):
                    conn.sendall(b"221 bye\r\n")
                    break
                else:
                    conn.sendall(b"250 ok\r\n")
        except OSError:
            pass
        finally:
            f.close()
            conn.close()

    def close(self):
        self.sock.close()
        self.thread.join(timeout=5)

    def command(self, verb: bytes):
        return [line for line in self.lines if line.upper().startswith(verb)]


@pytest.fixture()
def stub(monkeypatch):
    made = []

    def make(smtputf8: bool) -> _Stub:
        s = _Stub(smtputf8)
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


def _account(port: int, own: str = OWN):
    return {"provider": "imap", "account_id": own, "email": own,
            "smtp_host": "127.0.0.1", "smtp_port": port}


def _send(svc, port, to, cc="", bcc="", own=OWN, case_id=""):
    """queue_send, then the outbox drain the supervisor runs: the real path, sender included."""
    out = svc.queue_send(own, to, "Привет", "Text", cc=cc, bcc=bcc, undo_seconds=0,
                         case_id=case_id)
    apk = svc.store.account_pk(own)
    OpExecutor(svc.store, apk, NullImapClient(), _account(port, own), SCOPE).process(
        write_enabled=False, allowed_kinds={"send"})
    return out, svc.send_outcome(out["op_id"])



def _mail_lines(stub):
    """The MAIL commands with the verb lowercased: smtplib writes "mail FROM:" on older Pythons
    (3.12 on CI) and "mail from:" on 3.13; the arguments stay exactly as sent."""
    return [line[:10].lower() + line[10:] for line in stub.command(b"MAIL")]


def test_stub_with_smtputf8_delivers(svc, stub):
    s = stub(True)
    raw = bytes(compose.build_message(OWN, f"Иван <{EAI}>", "Привет", "Text", bcc="b@example.org",
                                      utf8=True))
    res = sender_mod.send(sender_mod.OutgoingMessage(
        account=_account(s.port), raw_bytes=raw, to=f"Иван <{EAI}>", bcc="b@example.org"))
    assert res.ok and res.classification == "ok" and res.smtputf8 is True, res
    assert _mail_lines(s) == [b"mail from:<bob@example.com> SMTPUTF8 BODY=8BITMIME\r\n"]
    assert s.command(b"RCPT") == [f"rcpt to:<{EAI_ENVELOPE}>\r\n".encode(),
                                  b"rcpt to:<b@example.org>\r\n"]
    data = b"".join(s.lines[s.lines.index(s.command(b"DATA")[0]) + 1:])
    assert f"To: Иван <{EAI}>".encode() in data
    assert b"Bcc" not in data and b"b@example.org" not in data


def test_an_ascii_local_part_needs_no_smtputf8(svc, stub):
    s = stub(False)
    out, outcome = _send(svc, s.port, "Jürgen <jm@bücher.de>")
    assert outcome["state"] == "done", outcome
    assert _mail_lines(s) == [b"mail from:<bob@example.com>\r\n"]
    assert s.command(b"RCPT") == [b"rcpt to:<jm@xn--bcher-kva.de>\r\n"]
    assert all(line.isascii() for line in s.lines)


def test_stub_without_smtputf8_refuses_before_auth(svc, stub):
    s = stub(False)
    raw = bytes(compose.build_message(OWN, EAI, "Привет", "Text", utf8=True))
    res = sender_mod.send(sender_mod.OutgoingMessage(account=_account(s.port), raw_bytes=raw, to=EAI))
    assert not res.ok and res.classification == "permanent" and res.handed_off is False, res
    assert res.smtputf8 is False
    assert "Nothing was sent" in res.error and "SMTPUTF8" not in res.error
    res.error.encode("cp1252")
    s.close()
    assert s.command(b"AUTH") == [] and s.command(b"MAIL") == [], s.lines


def test_a_case_answer_from_an_idn_account_builds_and_sends(svc, stub):
    """The Front Office answers with a case anchor as Message-ID, minted on the sending
    address's own domain: from an IDN account that domain has to be its A-label. The login
    is written in ASCII by smtplib, so it carries the A-label too."""
    own = "bob@пример.рф"
    svc.store.upsert_account(own, "imap", own)
    s = stub(False)
    out, outcome = _send(svc, s.port, "anna@example.org", own=own,
                         case_id=case_token.mint_case_id())
    assert outcome["state"] == "done", outcome
    assert out["message_id"].endswith("@xn--e1afmkfd.xn--p1ai>"), out["message_id"]
    assert _mail_lines(s) == [b"mail from:<bob@xn--e1afmkfd.xn--p1ai>\r\n"]
    auth = s.command(b"AUTH")[0].split()[-1]
    assert base64.b64decode(auth) == b"\0bob@xn--e1afmkfd.xn--p1ai\0pw"
    data = b"".join(s.lines[s.lines.index(s.command(b"DATA")[0]) + 1:])
    assert f"Message-ID: {out['message_id']}".encode() in data
    assert b"From: bob@xn--e1afmkfd.xn--p1ai" in data
    assert all(line.isascii() for line in s.lines)


def test_the_app_log_names_the_refusal_not_the_address(svc, stub, caplog):
    """The card gets the whole refusal through the op's error; the app log gets its reason
    only, because for a Front Office answer the address is the remote sender's."""
    s = stub(False)
    with caplog.at_level("WARNING", logger="vaf.mail.writeback"):
        out, outcome = _send(svc, s.port, EAI)
    assert "Nothing was sent" in outcome["error"] and "<U+0438>" in outcome["error"]
    logged = " ".join(r.getMessage() for r in caplog.records if r.name == "vaf.mail.writeback")
    assert "address refused" in logged and "before the @" in logged, logged
    assert "<U+0438>" not in logged and "xn--" not in logged and "иван" not in logged, logged


def test_every_refusal_code_has_a_sentence_in_every_catalogue():
    """The compose window says a refusal in the reader's language, by the route's code: the
    server's codes, the window's list and every catalogue's sentences move together."""
    import json
    import re
    from pathlib import Path
    from vaf.mail.addressing import _REFUSAL_TEXT, REFUSAL_REASON
    web = Path(__file__).resolve().parents[1] / "web"
    codes = set(_REFUSAL_TEXT)
    assert codes == set(REFUSAL_REASON)
    page = (web / "app" / "mail" / "page.tsx").read_text(encoding="utf-8")
    listed = re.search(r"const REFUSED_ADDRESS = \[([^\]]*)\]", page)
    assert listed, "page.tsx lost its REFUSED_ADDRESS list"
    assert set(re.findall(r"'([a-z0-9_]+)'", listed.group(1))) == codes
    # The sentence is looked up by code and the address rendered isolated, as on the cards.
    assert "t(`compose.refused.${refused.code}`, { address: SLOT })" in page
    assert "<Recipient text={refused.address} />" in page
    for catalogue in sorted((web / "messages").glob("*.json")):
        refused = json.loads(catalogue.read_bytes())["mailV2"]["compose"]["refused"]
        assert set(refused) == codes, catalogue.name
        for code, sentence in refused.items():
            assert sentence.count("{address}") == 1, (catalogue.name, code)
            assert "SMTPUTF8" not in sentence and "IDNA" not in sentence, (catalogue.name, code)


def test_the_record_refuses_at_queue_time(svc, stub):
    s = stub(False)
    out, outcome = _send(svc, s.port, EAI)
    assert outcome["state"] == "failed" and "Nothing was sent" in outcome["error"], outcome
    apk = svc.store.account_pk(OWN)
    rec = svc.store.account_state(apk)["smtp_utf8"]
    assert rec["offered"] is False and abs(rec["checked_ts"] - time.time()) < 60
    before = _ops(svc)
    with pytest.raises(UndeliverableAddress) as caught:
        svc.queue_send(OWN, f"Anna <anna@example.org>, {EAI}", "Hallo", "Text", hold=True)
    assert caught.value.code == "smtputf8" and "Nothing was queued" in str(caught.value)
    str(caught.value).encode("cp1252")
    assert _ops(svc) == before
    # The record refuses only what the server cannot take: an IDN domain still queues.
    assert svc.queue_send(OWN, IDN, "Hallo", "Text", hold=True)["op_id"]


def test_any_session_records_the_answer(svc, stub):
    s = stub(True)
    out, outcome = _send(svc, s.port, "anna@example.org")
    assert outcome["state"] == "done", outcome
    assert svc.store.account_state(svc.store.account_pk(OWN))["smtp_utf8"]["offered"] is True


def test_a_stale_record_does_not_refuse(svc):
    apk = svc.store.account_pk(OWN)
    svc.store.set_account_state(apk, smtp_utf8={
        "offered": False, "checked_ts": int(time.time()) - SMTPUTF8_RECORD_MAX_AGE_SEC - 60})
    assert svc.queue_send(OWN, EAI, "Hallo", "Text", hold=True)["op_id"]
    svc.store.set_account_state(apk, smtp_utf8={"offered": False, "checked_ts": int(time.time())})
    with pytest.raises(UndeliverableAddress):
        svc.queue_send(OWN, EAI, "Hallo", "Text", hold=True)


def test_revising_a_utf8_draft_keeps_the_addresses(svc):
    out = svc.queue_send(OWN, f"Иван <{EAI}>", "Привет", "Text", cc=IDN, bcc="марта@пример.рф",
                         hold=True)
    assert svc.revise_draft(out["op_id"], subject="Neu", body="Anderer Text")
    head = _headers(_raw(svc, out["op_id"]))
    assert f"To: Иван <{EAI}>".encode() in head, head
    assert f"Cc: {IDN}".encode() in head and "Bcc: марта@пример.рф".encode() in head, head
    assert b"Subject: Neu" in head


def test_account_state_writes_keep_each_others_keys(svc, tmp_path):
    """Two lanes write different keys from different connections at once (the answering
    lane's cursor, the outbox's SMTPUTF8 answer): neither writes the other's key back stale."""
    apk = svc.store.account_pk(OWN)
    svc.store.set_account_state(apk, inbound_cursor=1)
    other = MailStore(SCOPE, base_dir=tmp_path)
    real = svc.store.account_state
    calls = []

    def read_then_let_the_other_lane_write(account_pk):
        state = real(account_pk)
        if not calls:
            calls.append(1)
            t = threading.Thread(target=lambda: other.set_account_state(apk, inbound_cursor=99))
            t.start()
            t.join(timeout=0.5)  # with the lock it waits for this write to commit
            calls.append(t)
        return state

    svc.store.account_state = read_then_let_the_other_lane_write
    try:
        svc.store.set_account_state(apk, smtp_utf8={"offered": True, "checked_ts": 1})
    finally:
        svc.store.account_state = real
        calls[1].join(timeout=20)
    state = svc.store.account_state(apk)
    other.close()
    assert state["inbound_cursor"] == 99 and state["smtp_utf8"]["offered"] is True, state
