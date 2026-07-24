# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Native v2 send core (mail v2-only port, P2.1). SMTP is fully mocked (no socket);
these lock the dispatch matrix, Bcc/envelope + Message-ID handling, and the
honest transient/permanent/ambiguous hand-off classification."""
import smtplib

import pytest

import vaf.mail.addressing as addressing
import vaf.mail.sender as sender
import vaf.core.email_transport as email_transport

RAW = (b"From: u@example.com\r\nTo: a@x.com\r\nCc: c@x.com\r\nBcc: secret@x.com\r\n"
       b"Message-ID: <keep-me@example.com>\r\nSubject: hi\r\n\r\nbody\r\n")


class FakeSMTP:
    """Records the SMTP conversation; hooks let a test inject a failure at a step."""
    last = None

    def __init__(self, *a, **k):
        self.calls, self.rcpts, self.data_sent = [], [], None
        self.fail_at, self.exc = None, None
        FakeSMTP.last = self

    def _maybe(self, step):
        self.calls.append(step)
        if self.fail_at == step and self.exc:
            raise self.exc

    def ehlo(self, *a): self._maybe("ehlo")
    def starttls(self, *a, **k): self._maybe("starttls")
    def login(self, user, pw): self._maybe("login")
    def auth(self, mech, authobject, initial_response_ok=True):
        self.calls.append(("auth", mech, authobject()))
        if self.fail_at == "auth" and self.exc:
            raise self.exc
    def mail(self, sender): self._maybe(("mail", sender))
    def rcpt(self, r): self.rcpts.append(r); self._maybe("rcpt")
    def data(self, msg): self.data_sent = msg; self._maybe("data")
    def quit(self): self.calls.append("quit")


@pytest.fixture
def patched(monkeypatch):
    """No socket, no SSRF/cred/token I/O; capture token-lane + delegate calls."""
    monkeypatch.setattr(sender.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(sender.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(sender, "assert_safe_remote_host", lambda *a, **k: None)
    monkeypatch.setattr(sender, "get_email_credentials", lambda *a, **k: {"type": "imap", "password": "pw"})
    seen = {"token_provider": None, "delegate": 0}
    monkeypatch.setattr(sender, "get_valid_access_token",
                        lambda aid, provider, *a, **k: seen.__setitem__("token_provider", provider) or "tok")
    def _fake_send_mail(*a, **k):
        seen["delegate"] += 1
        return True
    monkeypatch.setattr(email_transport, "send_mail", _fake_send_mail)
    return seen


def _msg(**acc):
    a = {"provider": "imap", "email": "u@example.com", "account_id": "u@example.com",
         "smtp_host": "smtp.example.com", "smtp_port": 587}
    a.update(acc)
    return sender.OutgoingMessage(account=a, raw_bytes=RAW, to="a@x.com", cc="c@x.com",
                                  bcc="secret@x.com", subject="hi", body="body")


def test_normalize_recipients_is_single_object():
    # Rule 2 anti-drift: the historical email_transport name IS the addressing one.
    assert email_transport.normalize_recipients is addressing.normalize_recipients


def test_imap_password_dispatch(patched):
    r = sender.send(_msg(provider="imap"))
    assert r.ok and r.classification == "ok" and r.handed_off
    assert "login" in FakeSMTP.last.calls  # password AUTH, not XOAUTH2
    assert not any(isinstance(c, tuple) and c[0] == "auth" for c in FakeSMTP.last.calls)


def test_gmail_uses_gmail_token_lane(patched):
    r = sender.send(_msg(provider="gmail", imap_ready=True))
    assert r.ok and patched["token_provider"] == "gmail"
    assert any(isinstance(c, tuple) and c[0] == "auth" for c in FakeSMTP.last.calls)


def test_microsoft_uses_microsoft_imap_token_lane(patched):
    r = sender.send(_msg(provider="microsoft", imap_ready=True))
    assert r.ok and patched["token_provider"] == "microsoft_imap"


def test_oauth_not_imap_ready_delegates_once(patched):
    r = sender.send(_msg(provider="gmail", imap_ready=False))
    assert r.ok and r.used_delegate and patched["delegate"] == 1


def test_bcc_stripped_from_wire_but_in_envelope(patched):
    sender.send(_msg(provider="imap"))
    wire = FakeSMTP.last.data_sent
    assert b"secret@x.com" in b",".join(r.encode() for r in FakeSMTP.last.rcpts)
    assert FakeSMTP.last.rcpts == ["a@x.com", "c@x.com", "secret@x.com"]
    assert b"Bcc" not in wire and b"secret@x.com" not in wire
    assert b"<keep-me@example.com>" in wire  # Message-ID preserved byte-exact


def test_ambiguous_when_failure_after_handoff(patched):
    m = _msg(provider="imap")
    r_holder = {}
    def run():
        FakeSMTP.last = None
        res = sender.send(m)
        FakeSMTP.last.fail_at = None
        return res
    # inject a disconnect at DATA (post hand-off) -> ambiguous, never re-send
    orig_init = FakeSMTP.__init__
    def init(self, *a, **k):
        orig_init(self, *a, **k)
        self.fail_at, self.exc = "data", smtplib.SMTPServerDisconnected("boom")
    FakeSMTP.__init__ = init
    try:
        res = sender.send(m)
    finally:
        FakeSMTP.__init__ = orig_init
    assert not res.ok and res.classification == "ambiguous" and res.handed_off


def test_permanent_on_auth_reject(patched):
    m = _msg(provider="imap")
    orig_init = FakeSMTP.__init__
    def init(self, *a, **k):
        orig_init(self, *a, **k)
        self.fail_at, self.exc = "login", smtplib.SMTPAuthenticationError(535, b"bad")
    FakeSMTP.__init__ = init
    try:
        res = sender.send(m)
    finally:
        FakeSMTP.__init__ = orig_init
    assert not res.ok and res.classification == "permanent" and not res.handed_off


def test_transient_on_4xx_before_handoff(patched):
    m = _msg(provider="imap")
    orig_init = FakeSMTP.__init__
    def init(self, *a, **k):
        orig_init(self, *a, **k)
        self.fail_at, self.exc = "rcpt", smtplib.SMTPResponseException(451, b"try later")
    FakeSMTP.__init__ = init
    try:
        res = sender.send(m)
    finally:
        FakeSMTP.__init__ = orig_init
    assert not res.ok and res.classification == "transient" and not res.handed_off


def test_ssrf_refusal_is_permanent(patched, monkeypatch):
    def _deny(*a, **k):
        raise ValueError("host is not a public address")
    monkeypatch.setattr(sender, "assert_safe_remote_host", _deny)
    res = sender.send(_msg(provider="imap"))
    assert not res.ok and res.classification == "permanent"


# ── P2.3: the send_mail tool routes through the native sender ─────────────────

def test_send_mail_tool_routes_through_native_sender(monkeypatch):
    import vaf.tools.send_mail as sm
    monkeypatch.setattr(sm, "get_account",
                        lambda *a, **k: {"provider": "imap", "email": "u@example.com", "account_id": "u@example.com"})
    cap = {}

    def _snd(msg):
        cap["raw"] = msg.raw_bytes
        return sm.sender.SendResult(True, "ok")

    monkeypatch.setattr(sm.sender, "send", _snd)
    out = sm.SendMailTool().run(account_id="u@example.com", to="a@b.com", subject="s", body="b")
    assert "sent to a@b.com" in out
    assert b"a@b.com" in cap["raw"]  # native path received the built MIME bytes


def test_send_mail_tool_ambiguous_says_possibly_delivered(monkeypatch):
    import vaf.tools.send_mail as sm
    monkeypatch.setattr(sm, "get_account",
                        lambda *a, **k: {"provider": "imap", "email": "u@example.com", "account_id": "u@example.com"})
    monkeypatch.setattr(sm.sender, "send",
                        lambda msg: sm.sender.SendResult(False, "ambiguous", handed_off=True))
    out = sm.SendMailTool().run(account_id="u@example.com", to="a@b.com", subject="s", body="b")
    assert "do NOT resend" in out  # never a false 'failed' for a possibly-delivered mail
