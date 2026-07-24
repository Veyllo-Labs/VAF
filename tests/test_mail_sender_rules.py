# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""P5.4: relabeling a mail learns a sender rule and backfills every stored mail of
that sender (owner decision = legacy MailDashboard parity). Exercises the v2
MailService store path against a mocked, scope-routed config blob (the SSOT
sender_category_rules)."""
import os

import pytest

import vaf.core.email_accounts as ea
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


@pytest.fixture
def cfg(monkeypatch):
    # config blob routed by scope; _SCOPE != admin-scope -> reads/writes by_scope[_SCOPE]
    store = {}
    monkeypatch.setattr(ea, "get_local_admin_scope_id", lambda: "admin-scope")
    monkeypatch.setattr(ea, "get_local_admin_username", lambda: "admin")
    monkeypatch.setattr(ea.Config, "get", staticmethod(lambda k, d=None: store.get(k, d)))
    monkeypatch.setattr(ea.Config, "load", staticmethod(lambda: dict(store)))
    monkeypatch.setattr(ea.Config, "save", staticmethod(lambda c: store.update(c)))
    return store


def _ingest(store, uid, from_addr, subject="hi", cat=""):
    apk = store.upsert_account("me@example.com", "imap", "me@example.com")
    fpk = store.upsert_folder(apk, "INBOX", special_use="\\Inbox")
    return store.ingest_message(apk, fpk, uid, ParsedMessage(
        message_id=f"<{uid}@example.com>", subject=subject, from_addr=from_addr,
        to_addrs="me@example.com", date_ts=1_700_000_000, body_text="b"), category=cat)


def test_relabel_learns_rule_and_backfills_same_sender(svc, cfg):
    a1 = _ingest(svc.store, 1, "Twitch <no-reply@twitch.tv>", "you got a follower")
    a2 = _ingest(svc.store, 2, "Twitch <no-reply@twitch.tv>", "clip is ready")
    other = _ingest(svc.store, 3, "Alice <alice@example.com>", "lunch")

    out = svc.relabel_and_learn(a1, "Social", username=None)
    assert out["category"] == "social"
    # a1 (explicit) + a2 (backfill) changed; 'other' sender untouched
    assert out["updated"] == 2
    assert svc.store.get_message(a1)["category"] == "social"
    assert svc.store.get_message(a2)["category"] == "social"
    assert svc.store.get_message(other)["category"] in ("", "primary")

    # the rule was persisted to the scope's config blob
    rules = cfg["email_config_by_scope"][_SCOPE]["sender_category_rules"]
    assert {"pattern": "no-reply@twitch.tv", "category": "social"} in rules


def test_relabel_unknown_pk_returns_none(svc, cfg):
    assert svc.relabel_and_learn(999999, "social", username=None) is None


def test_backfill_alone_applies_existing_rules(svc, cfg):
    m = _ingest(svc.store, 5, "News <n@twitch.tv>", "digest")
    ea.upsert_sender_rule("twitch.tv", "social", username=None, user_scope_id=_SCOPE)
    assert svc.apply_sender_rules_backfill(username=None) == 1
    assert svc.store.get_message(m)["category"] == "social"
    # idempotent: a second pass changes nothing
    assert svc.apply_sender_rules_backfill(username=None) == 0


def test_upsert_sender_rule_replaces_same_pattern(cfg):
    assert ea.upsert_sender_rule("twitch.tv", "social", user_scope_id=_SCOPE)
    assert ea.upsert_sender_rule("Twitch.TV", "promotions", user_scope_id=_SCOPE)  # case-fold replace
    rules = cfg["email_config_by_scope"][_SCOPE]["sender_category_rules"]
    assert rules == [{"pattern": "twitch.tv", "category": "promotions"}]
    assert ea.upsert_sender_rule("", "social", user_scope_id=_SCOPE) is False


def test_pattern_from_from_addr_variants():
    assert ea.pattern_from_from_addr("Twitch <no-reply@twitch.tv>") == "no-reply@twitch.tv"
    assert ea.pattern_from_from_addr("plain@example.com") == "plain@example.com"
    assert ea.pattern_from_from_addr("No Address Here") == "No Address Here"  # no @ -> as-is
    assert ea.pattern_from_from_addr("") == ""
