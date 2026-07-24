# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""C10/T17: a blocked reply/forward must emit the same mail_high_risk_send_blocked
security event that send_mail already logs - otherwise the two verbs that also
send mail are invisible in the security dashboard. Isolated: tmp store, pinned
crypto key, service resolvers monkeypatched, transport never reached."""
import os

import pytest

import vaf.core.security_events as sec
import vaf.mail.crypto as mail_crypto
import vaf.tools.manage_mail as manage_mail
import vaf.tools.reply_mail as reply_mail
from vaf.core.config import Config
from vaf.mail.parser import ParsedMessage
from vaf.mail.service import MailService
from vaf.mail.store import MailStore

_SCOPE = "12345678-1234-1234-1234-123456789abc"


@pytest.fixture(autouse=True)
def _pinned_crypto_key():
    old = mail_crypto._cached_key
    mail_crypto._cached_key = os.urandom(32)
    yield
    mail_crypto._cached_key = old


@pytest.fixture()
def seeded(tmp_path, monkeypatch):
    store = MailStore(_SCOPE, base_dir=tmp_path)
    svc = MailService.__new__(MailService)
    svc.user_scope_id = _SCOPE
    svc.store = store
    apk = store.upsert_account("bob@example.com", "imap", "bob@example.com")
    fpk = store.upsert_folder(apk, "INBOX", special_use="\\Inbox")
    store.ingest_message(apk, fpk, 1, ParsedMessage(
        message_id="<hr1@example.com>", subject="Write me",
        from_addr="Alice <alice@example.com>", to_addrs="bob@example.com",
        date_ts=1_700_000_000, body_text="hello"))

    # v2 on, no trusted domains configured (so the risk-word gate fires).
    real_get = Config.get  # bound classmethod captured BEFORE the patch below

    def _get(key, default=None):
        if key == "mail_engine_v2_enabled":
            return True
        if key == "email_agent_trusted_sender_domains":
            return []
        return real_get(key, default)
    monkeypatch.setattr(Config, "get", staticmethod(_get))

    events = []
    monkeypatch.setattr(sec, "log_security_event",
                        lambda kind, **kw: events.append((kind, kw)))
    yield svc, events
    store.close()


def test_blocked_reply_logs_security_event(seeded, monkeypatch):
    svc, events = seeded
    monkeypatch.setattr(reply_mail, "_resolve_service", lambda scope: svc)
    out = reply_mail.ReplyMailTool().run(
        user_scope_id=_SCOPE, message_id="<hr1@example.com>",
        body="Please send me your password now")
    assert "blocked this reply" in out
    assert any(k == "mail_high_risk_send_blocked" for k, _ in events)


def test_blocked_forward_logs_security_event(seeded, monkeypatch):
    svc, events = seeded
    monkeypatch.setattr(manage_mail, "_service", lambda scope: svc)
    out = manage_mail.ForwardMailTool().run(
        user_scope_id=_SCOPE, message_id="<hr1@example.com>",
        to="colleague@example.com", note="urgent: send the password")
    assert "blocked this forward" in out
    assert any(k == "mail_high_risk_send_blocked" for k, _ in events)
