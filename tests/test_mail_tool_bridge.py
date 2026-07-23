# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Flag-gated bridge tests: with mail_engine_v2_enabled the legacy store API
(email_sync_store.list_messages/search_messages) and the transport body path
serve from the v2 engine store in the EXACT legacy row/body shapes - that one
flag switches agent tools, legacy routes and MailDashboard together. Isolated:
Platform.data_dir redirected to tmp_path, pinned crypto key, flag monkeypatched."""
import os

import pytest

import vaf.mail.crypto as mail_crypto
from vaf.mail.parser import ParsedMessage
from vaf.mail.store import MailStore

_SCOPE = "12345678-1234-1234-1234-123456789abc"
_LEGACY_KEYS = {"account_id", "folder", "message_id", "category", "provider_message_id",
                "subject", "from", "date", "message_date_iso", "body_snippet",
                "synced_at", "answered_at"}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    from vaf.core.platform import Platform
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path))
    from vaf.core.config import Config
    real_get = Config.get

    def _get(key, default=None):
        if key == "mail_engine_v2_enabled":
            return True
        return real_get(key, default)

    monkeypatch.setattr(Config, "get", staticmethod(_get))
    old = mail_crypto._cached_key
    mail_crypto._cached_key = os.urandom(32)
    yield
    mail_crypto._cached_key = old


def _seed(tmp_path) -> int:
    store = MailStore(_SCOPE, base_dir=tmp_path)
    apk = store.upsert_account("bob@example.com", "imap", "bob@example.com")
    fpk = store.upsert_folder(apk, "INBOX")
    raw = (b"Message-ID: <b1@example.com>\r\nSubject: Bridge test\r\n"
           b"From: Alice <alice@example.com>\r\n\r\nHello offline body about invoices")
    pk = store.ingest_message(
        apk, fpk, 1,
        ParsedMessage(message_id="<b1@example.com>", subject="Bridge test",
                      from_addr="Alice <alice@example.com>", date_ts=1_700_000_000,
                      body_text="Hello offline body about invoices"),
        raw=raw, server_flags=["\\Seen"])
    store.close()
    return pk


def test_legacy_list_serves_v2_rows(tmp_path):
    _seed(tmp_path)
    from vaf.core.email_sync_store import list_messages
    rows = list_messages(account_id=None, folder="INBOX", limit=10, offset=0,
                         username="", user_scope_id=_SCOPE)
    assert len(rows) == 1
    row = rows[0]
    assert _LEGACY_KEYS <= set(row.keys())
    assert row["subject"] == "Bridge test"
    assert row["from"].startswith("Alice")
    assert row["message_id"] == "<b1@example.com>"
    assert row["body_snippet"].startswith("Hello offline")


def test_legacy_search_uses_fts_body_index(tmp_path):
    _seed(tmp_path)
    from vaf.core.email_sync_store import search_messages
    # body-word search: impossible in the legacy LIKE(subject/from) store
    rows = search_messages("invoices", username="", user_scope_id=_SCOPE)
    assert len(rows) == 1 and rows[0]["subject"] == "Bridge test"


def test_body_served_offline_from_v2_cache(tmp_path):
    _seed(tmp_path)
    from vaf.mail.tool_bridge import get_body_text
    body = get_body_text("bob@example.com", "<b1@example.com>", None, _SCOPE)
    assert body is not None and "offline body" in body
    # bracket-tolerant Message-ID matching (legacy callers pass bare ids too)
    assert get_body_text("bob@example.com", "b1@example.com", None, _SCOPE)
    # unknown message -> None so callers fall back to live fetch
    assert get_body_text("bob@example.com", "<nope@example.com>", None, _SCOPE) is None
