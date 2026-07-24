# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""P3.2: the v2 agent-facing MailService API (legacy-row lists, Message-ID
resolution, category/answered writes, and on-demand body fetch). Shipped unused;
the agent tools repoint onto it in P3.3-P3.5. Isolated: tmp store, pinned key,
IMAP mocked."""
import os

import pytest

import vaf.mail.crypto as mail_crypto
from vaf.mail.parser import ParsedMessage
from vaf.mail.service import MailService
from vaf.mail.store import MailStore

_SCOPE = "12345678-1234-1234-1234-123456789abc"


@pytest.fixture(autouse=True)
def _pinned_key():
    old = mail_crypto._cached_key
    mail_crypto._cached_key = os.urandom(32)
    yield
    mail_crypto._cached_key = old


@pytest.fixture
def svc(tmp_path):
    s = MailService.__new__(MailService)
    s.user_scope_id = _SCOPE
    s.store = MailStore(_SCOPE, base_dir=tmp_path)
    yield s
    s.store.close()


def _seed(store, *, cached=True, uid=5):
    apk = store.upsert_account("bob@example.com", "imap", "bob@example.com")
    fpk = store.upsert_folder(apk, "INBOX", special_use="\\Inbox")
    raw = (b"Message-ID: <a1@example.com>\r\nSubject: hello\r\n"
           b"From: Alice <alice@example.com>\r\n\r\nbody text here") if cached else None
    pk = store.ingest_message(apk, fpk, uid, ParsedMessage(
        message_id="<a1@example.com>", subject="hello",
        from_addr="Alice <alice@example.com>", to_addrs="bob@example.com",
        date_ts=1_700_000_000, body_text="body text here"), raw=raw, server_flags=[])
    return apk, fpk, pk


def test_list_and_search_for_agent_legacy_shape(svc):
    _seed(svc.store)
    rows = svc.list_for_agent(folder="INBOX")
    assert len(rows) == 1
    r = rows[0]
    assert r["message_id"] == "<a1@example.com>" and r["subject"] == "hello"
    assert r["from"].startswith("Alice") and r["account_id"] == "bob@example.com"
    assert {"account_id", "folder", "message_id", "category", "from", "date",
            "body_snippet", "answered_at"} <= set(r)
    found = svc.search_for_agent("hello")
    assert found and found[0]["message_id"] == "<a1@example.com>"


def test_find_pk_from_addr_and_metadata_writes(svc):
    _apk, _fpk, pk = _seed(svc.store)
    assert svc.find_pk_by_message_id("<a1@example.com>") == pk
    assert svc.find_pk_by_message_id("a1@example.com") == pk  # bracket-tolerant
    assert svc.message_from_addr("bob@example.com", "<a1@example.com>").startswith("Alice")
    assert svc.set_category("bob@example.com", "<a1@example.com>", "social")
    assert svc.store.get_message(pk)["category"] == "social"
    assert svc.mark_answered("bob@example.com", "<a1@example.com>")
    assert svc.store.get_message(pk)["answered_at"]
    assert not svc.set_category("bob@example.com", "<nope@x>", "x")  # unknown -> False


def test_body_text_from_cache(svc):
    _seed(svc.store)
    assert "body text here" in (svc.body_text("<a1@example.com>") or "")


def test_body_text_fetches_on_demand_when_uncached(svc, monkeypatch):
    _apk, _fpk, pk = _seed(svc.store, cached=False)
    assert svc.store.get_raw(pk) is None  # header-only, no body cached
    fetched = (b"Message-ID: <a1@example.com>\r\nSubject: hello\r\n\r\nfetched body!")

    class FakeClient:
        def select_folder(self, name, readonly=True):
            return {}

        def fetch(self, uids, items):
            return {int(uids[0]): {b"BODY[]": fetched}}

    monkeypatch.setattr("vaf.core.email_accounts.get_account",
                        lambda *a, **k: {"provider": "imap", "email": "bob@example.com", "account_id": "bob@example.com"})
    monkeypatch.setattr("vaf.mail.imap_client.build_imap_client", lambda *a, **k: FakeClient())
    monkeypatch.setattr("vaf.mail.imap_client._safe_logout", lambda c: None)

    body = svc.body_text("<a1@example.com>", cred_username=None)
    assert "fetched body!" in (body or "")
    assert svc.store.get_raw(pk) is not None  # cached for next time
