# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""P6.5: the three defects that would become permanent when the legacy stack is
removed. Each one fails silently today, which is exactly why they need guards:

1. A send with no usable delivery lane was classified 'transient', so the outbox
   retried it five times and then parked it with nothing on screen - the user was
   told the mail went out.
2. The legacy label/answered import was marked done STORE-wide while running per
   account, so in a multi-account migration every account after the first lost its
   labels; and rows whose mail was not in the store yet were dropped for good.
3. Adding a password account for an address already connected via OAuth replaced
   that entry, and the calendar resolves its accounts by that provider - so the
   calendar silently lost the account.
"""
import asyncio
import os
import sqlite3

import pytest
from fastapi import HTTPException

import vaf.api.mail_routes as mr
import vaf.core.email_accounts as ea
import vaf.mail.crypto as mail_crypto
from vaf.mail.migrate import import_legacy_artifacts
from vaf.mail.parser import ParsedMessage
from vaf.mail.sender import OutgoingMessage, _delegate
from vaf.mail.store import MailStore

_SCOPE = "12345678-1234-1234-1234-123456789abc"
_USER = {"username": "admin", "user_scope_id": "s"}


@pytest.fixture(autouse=True)
def _pinned_key():
    old = mail_crypto._cached_key
    mail_crypto._cached_key = os.urandom(32)
    yield
    mail_crypto._cached_key = old


# ── 1. a send with no lane must fail LOUDLY, not look retryable ───────────────

def test_missing_delegate_is_permanent_not_transient(monkeypatch):
    """'transient' means retry-then-park-in-silence. A missing transport can never
    succeed on retry, so it must be permanent and name the fix.

    Modelled as the real P7 shape: the module survives (other helpers stay) and
    only the send functions are deleted."""
    import vaf.core.email_transport as et
    monkeypatch.delattr(et, "send_mail", raising=True)
    res = _delegate(OutgoingMessage(account={"provider": "gmail", "account_id": "g@x"},
                                    raw_bytes=b"", to="a@b"))
    assert res.ok is False
    assert res.classification == "permanent"      # NOT 'transient'
    assert "reconnect" in (res.error or "").lower()


def test_transport_error_stays_transient(monkeypatch):
    """A real connect/auth failure IS worth retrying - do not over-correct."""
    import vaf.core.email_transport as et
    monkeypatch.setattr(et, "send_mail", lambda *a, **k: (_ for _ in ()).throw(OSError("conn reset")))
    res = _delegate(OutgoingMessage(account={"provider": "gmail", "account_id": "g@x"},
                                    raw_bytes=b"", to="a@b"))
    assert res.ok is False and res.classification == "transient"


def test_ops_endpoint_exposes_the_reason_a_send_was_parked(monkeypatch, tmp_path):
    """The client can only warn about a parked send if the API says one exists."""
    monkeypatch.setattr(mr.Config, "get",
                        staticmethod(lambda k, d=None: True if k == "mail_engine_v2_enabled" else d))
    monkeypatch.setattr(mr, "_scope_of", lambda u: _SCOPE)
    store = MailStore(_SCOPE, base_dir=tmp_path)
    apk = store.upsert_account("a@x", "gmail", "a@x")
    store.enqueue_op(apk, "send", {"subject": "hello there"})
    op = store.pending_ops(apk)[0]
    store.claim_op(op["id"])
    store.mark_op(op["id"], "failed", error="account cannot send: reconnect it",
                  expect_state="sending")

    class _Svc:
        def __init__(self, scope):
            self.store = store

    monkeypatch.setattr("vaf.mail.service.MailService", _Svc)
    out = asyncio.run(mr.list_ops(_USER))
    store.close()
    row = next(o for o in out["ops"] if o["kind"] == "send")
    assert row["state"] == "failed"
    assert "reconnect" in (row["last_error"] or "")
    assert row["subject"] == "hello there"


# ── 2. the legacy import is per account and retries what it could not place ───

def _legacy_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE email_messages (username TEXT, account_id TEXT, "
                 "message_id TEXT, category TEXT, answered_at TEXT)")
    conn.executemany("INSERT INTO email_messages VALUES (?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    path = tmp_path / "email_sync.db"
    _legacy_db(path, [
        ("admin", "a@x", "<m-a@x>", "promotions", ""),
        ("admin", "b@x", "<m-b@x>", "social", ""),
    ])
    import vaf.core.email_sync_store as ess
    monkeypatch.setattr(ess, "_db_path", lambda u, s: path)
    monkeypatch.setattr(ess, "_user_for_query", lambda u, s: "admin")
    return path


def _seed(store, account_id, mid):
    apk = store.upsert_account(account_id, "imap", account_id)
    fpk = store.upsert_folder(apk, "INBOX", special_use="\\Inbox")
    return store.ingest_message(apk, fpk, 1, ParsedMessage(
        message_id=mid, subject="s", from_addr="a@x", to_addrs="b@x",
        date_ts=1_700_000_000, body_text="b"))


def test_import_is_per_account_not_store_wide(tmp_path, legacy):
    """The first account must not consume the marker for all the others."""
    store = MailStore(_SCOPE, base_dir=tmp_path / "v2")
    pk_a = _seed(store, "a@x", "<m-a@x>")
    pk_b = _seed(store, "b@x", "<m-b@x>")

    r1 = import_legacy_artifacts(store, "admin", _SCOPE, account_id="a@x")
    assert r1["categories"] == 1 and r1["skipped"] is False
    # second account still gets its own pass (this is what used to be skipped)
    r2 = import_legacy_artifacts(store, "admin", _SCOPE, account_id="b@x")
    assert r2["categories"] == 1 and r2["skipped"] is False

    assert store.get_message(pk_a)["category"] == "promotions"
    assert store.get_message(pk_b)["category"] == "social"
    # and each account is done exactly once
    assert import_legacy_artifacts(store, "admin", _SCOPE, account_id="a@x")["skipped"] is True
    store.close()


def test_import_retries_while_the_mail_has_not_arrived_yet(tmp_path, legacy):
    """The first v2 sync is bounded, so a label may have no target row yet. That
    must not be marked done and thrown away."""
    store = MailStore(_SCOPE, base_dir=tmp_path / "v2")
    r1 = import_legacy_artifacts(store, "admin", _SCOPE, account_id="a@x")
    assert r1["unmatched"] == 1 and r1["categories"] == 0
    assert import_legacy_artifacts(store, "admin", _SCOPE, account_id="a@x")["skipped"] is False

    pk = _seed(store, "a@x", "<m-a@x>")          # the mail arrives on a later sync
    r2 = import_legacy_artifacts(store, "admin", _SCOPE, account_id="a@x")
    assert r2["categories"] == 1 and r2["unmatched"] == 0
    assert store.get_message(pk)["category"] == "promotions"
    assert import_legacy_artifacts(store, "admin", _SCOPE, account_id="a@x")["skipped"] is True
    store.close()


def test_import_gives_up_after_the_attempt_cap(tmp_path, legacy):
    """A label whose mail will never arrive may not retry forever."""
    store = MailStore(_SCOPE, base_dir=tmp_path / "v2")
    for _ in range(5):
        assert import_legacy_artifacts(store, "admin", _SCOPE, account_id="a@x")["skipped"] is False
    assert import_legacy_artifacts(store, "admin", _SCOPE, account_id="a@x")["skipped"] is True
    store.close()


def test_a_store_wide_marker_from_the_old_scheme_still_counts_as_done(tmp_path, legacy):
    """Backward compatibility: the old marker was a bare timestamp string."""
    store = MailStore(_SCOPE, base_dir=tmp_path / "v2")
    store._conn().execute("INSERT INTO schema_meta(key, value) VALUES(?, ?)",
                          ("legacy_import_done", "2026-07-24T00:00:00"))
    store._conn().commit()
    assert import_legacy_artifacts(store, "admin", _SCOPE)["skipped"] is True
    store.close()


# ── 3. a password add must not silently unhook the calendar ───────────────────

def test_password_add_is_refused_for_an_address_already_connected_via_oauth(monkeypatch):
    monkeypatch.setattr(mr.Config, "get",
                        staticmethod(lambda k, d=None: True if k == "mail_engine_v2_enabled" else d))
    monkeypatch.setattr(mr, "_acct_identity", lambda u: ("admin", None, "s"))
    monkeypatch.setattr(ea, "oauth_provider_for",
                        lambda aid, u=None, user_scope_id=None: "gmail")

    def _must_not_run(*a, **k):
        raise AssertionError("the OAuth account entry must not be replaced")

    monkeypatch.setattr(ea, "add_account", _must_not_run)
    out = asyncio.run(mr.accounts_add({"email": "g@x", "password": "pw"}, _USER))
    assert out["ok"] is False
    assert "already connected" in out["error"]
    assert "reconnect" in out["hint"].lower()


def test_oauth_provider_for_only_reports_calendar_capable_providers(monkeypatch):
    monkeypatch.setattr(ea, "get_account",
                        lambda aid, u=None, user_scope_id=None: {"provider": "gmail"})
    assert ea.oauth_provider_for("g@x") == "gmail"
    monkeypatch.setattr(ea, "get_account",
                        lambda aid, u=None, user_scope_id=None: {"provider": "imap"})
    assert ea.oauth_provider_for("i@x") is None       # plain IMAP may be replaced
    monkeypatch.setattr(ea, "get_account", lambda aid, u=None, user_scope_id=None: None)
    assert ea.oauth_provider_for("new@x") is None     # unknown address is free


def test_password_add_still_works_for_a_fresh_address(monkeypatch):
    monkeypatch.setattr(mr.Config, "get",
                        staticmethod(lambda k, d=None: True if k == "mail_engine_v2_enabled" else d))
    monkeypatch.setattr(mr, "_acct_identity", lambda u: ("admin", None, "s"))
    monkeypatch.setattr(ea, "oauth_provider_for", lambda aid, u=None, user_scope_id=None: None)
    monkeypatch.setattr(ea, "test_imap_login", lambda *a, **k: (True, "", ""))
    added = {}
    monkeypatch.setattr("vaf.core.credential_store.set_email_imap_password",
                        lambda *a, **k: None)
    monkeypatch.setattr(ea, "add_account",
                        lambda entry, u=None, user_scope_id=None: added.update(entry))
    out = asyncio.run(mr.accounts_add({"email": "i@example.com", "password": "pw"}, _USER))
    assert out["ok"] is True and added["provider"] == "imap"


def test_accounts_add_requires_v2(monkeypatch):
    monkeypatch.setattr(mr.Config, "get",
                        staticmethod(lambda k, d=None: False if k == "mail_engine_v2_enabled" else d))
    with pytest.raises(HTTPException) as e:
        asyncio.run(mr.accounts_add({"email": "a@x", "password": "p"}, _USER))
    assert e.value.status_code == 404
