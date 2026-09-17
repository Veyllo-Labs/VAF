# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Schema v2 of the mail store: every message gets a verdict row at ingest (who wrote it,
did the sender authenticate; vaf/mail/verification.py), a v1 store migrates without
losing a row, the verdict rides on thread rows, agent rows and the phishing scorer, the
backfill recomputes under a newly learned provider id, and the sent-id ledger recognises
our own mail coming back. Isolated: tmp store, pinned key."""
import os

import pytest

import vaf.mail.crypto as mail_crypto
from vaf.mail.parser import ParsedMessage, parse_message
from vaf.mail.service import MailService
from vaf.mail.store import SCHEMA_VERSION, MailStore
from vaf.mail.verification import (
    assess, auth_policy_for_account, identity_snapshot, parsed_from_snapshot, policy_key, summary,
)

_SCOPE = "12345678-1234-1234-1234-123456789abc"
_POLICY = {"trusted_authserv_id": "mx.google.com", "auth_profile": "rfc8601",
           "own_addresses": ["bob@example.com"], "own_domains": ["example.com"]}


@pytest.fixture(autouse=True)
def _pinned_key():
    old = mail_crypto._cached_key
    mail_crypto._cached_key = os.urandom(32)
    yield
    mail_crypto._cached_key = old


@pytest.fixture()
def store(tmp_path):
    s = MailStore(_SCOPE, base_dir=tmp_path)
    yield s
    s.close()


@pytest.fixture()
def svc(store):
    s = MailService.__new__(MailService)
    s.user_scope_id = _SCOPE
    s.store = store
    return s


def _setup(store):
    apk = store.upsert_account("bob@example.com", "imap", "bob@example.com")
    fpk = store.upsert_folder(apk, "INBOX", special_use="\\Inbox", sync_tier="eager")
    return apk, fpk


def _gmail_raw(mid="<a1@example.org>", sender="Alice <alice@example.org>", extra=""):
    return (
        "Received: by 2002:a05:6000:1234 with SMTP id x1; Tue, 16 Sep 2026 10:00:00 -0700 (PDT)\n"
        "Return-Path: <alice@example.org>\n"
        "Received: from mail.example.org (mail.example.org. [203.0.113.5]) by mx.google.com with ESMTPS id y2\n"
        "Authentication-Results: mx.google.com; dkim=pass header.i=@example.org header.s=s1; "
        "spf=pass smtp.mailfrom=alice@example.org; dmarc=pass header.from=example.org\n"
        "Authentication-Results: evil.example; dkim=pass header.i=@bank.example\n"
        f"From: {sender}\nTo: bob@example.com\nSubject: Angebot\nMessage-ID: {mid}\n{extra}"
        "\nHallo Bob, hier das Angebot.\n").encode("utf-8")


_BOUNCE = b"""From: MAILER-DAEMON@example.net (Mail Delivery System)
To: bob@example.com
Subject: Undelivered Mail Returned to Sender
Return-Path: <>
Content-Type: multipart/report; report-type=delivery-status; boundary="B"
Message-ID: <dsn1@example.net>

--B
Content-Type: text/plain

Delivery failed.
--B
Content-Type: message/delivery-status

Reporting-MTA: dns; example.net

Final-Recipient: rfc822; carol@example.org
Action: failed
Status: 5.1.1

--B--
"""


def test_a_v1_store_migrates_to_v2_and_keeps_its_rows(tmp_path):
    s = MailStore(_SCOPE, base_dir=tmp_path)
    apk, fpk = _setup(s)
    pk = s.ingest_message(apk, fpk, 1, parse_message(_gmail_raw()), raw=_gmail_raw())
    conn = s._conn()
    for table in ("message_auth", "cases", "case_messages", "sent_ids"):
        conn.execute(f"DROP TABLE {table}")
    conn.execute("UPDATE schema_meta SET value='1' WHERE key='schema_version'")
    conn.commit()
    s.close()
    again = MailStore(_SCOPE, base_dir=tmp_path)
    try:
        meta = {r["key"]: r["value"] for r in again._conn().execute("SELECT key, value FROM schema_meta").fetchall()}
        assert meta["schema_version"] == str(SCHEMA_VERSION) == "2" and meta.get("migrated_to_2_at")
        assert again.get_message(pk)["subject"] == "Angebot", "the v1 rows survive"
        names = {r["name"] for r in again._conn().execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert {"message_auth", "cases", "case_messages", "sent_ids"} <= names
        assert again.message_auth([pk]) == {}, "a migrated row has no verdict until the backfill"
        assert again.list_threads()[0]["newest_auth_state"] == "unknown"
    finally:
        again.close()


def test_a_store_newer_than_this_build_is_refused(tmp_path):
    s = MailStore(_SCOPE, base_dir=tmp_path)
    s._conn().execute("UPDATE schema_meta SET value='99' WHERE key='schema_version'")
    s._conn().commit()
    s.close()
    with pytest.raises(RuntimeError):
        MailStore(_SCOPE, base_dir=tmp_path)


def test_ingest_writes_the_verdict_from_the_bytes_fetched_now(store):
    """MUTATION: drop the message_auth write from ingest_message and every row here is missing."""
    apk, fpk = _setup(store)
    raw = _gmail_raw()
    pk = store.ingest_message(apk, fpk, 1, parse_message(raw), raw=raw, auth_policy=_POLICY)
    row = store.message_auth([pk])[pk]
    assert row["auth_state"] == "verified" and row["aligned_by"] == "dmarc" and row["auth_source"] == "provider"
    assert row["authserv_id"] == "mx.google.com" == row["topmost_authserv_id"]
    assert row["machine_kind"] == "" and row["policy_key"] == policy_key(_POLICY)
    assert row["headers"]["auth_results"][0].startswith("mx.google.com;"), "the identity headers are snapshotted"
    assert row["computed_at"]
    bounce = store.ingest_message(apk, fpk, 2, parse_message(_BOUNCE), raw=_BOUNCE, auth_policy=_POLICY)
    b = store.message_auth([bounce])[bounce]
    assert b["machine_kind"] == "bounce" and b["auth_state"] in ("unknown", "unverified")
    assert b["headers"]["dsn_action"] == "failed" and b["headers"]["return_path_null"] is True


def test_without_a_policy_the_sender_stays_unknown_and_the_machine_kind_is_still_recorded(store):
    apk, fpk = _setup(store)
    pk = store.ingest_message(apk, fpk, 1, parse_message(_BOUNCE), raw=_BOUNCE)
    row = store.message_auth([pk])[pk]
    assert row["auth_state"] == "unknown" and row["machine_kind"] == "bounce"


def test_a_forged_header_below_the_providers_is_never_read(store):
    apk, fpk = _setup(store)
    raw = _gmail_raw()
    pk = store.ingest_message(apk, fpk, 1, parse_message(raw), raw=raw,
                              auth_policy=dict(_POLICY, trusted_authserv_id="evil.example"))
    row = store.message_auth([pk])[pk]
    # the trusted id is evil.example, and a header carrying it exists below the genuine
    # one: it is read, because the topmost matching header is the rule and the provider
    # is expected to strip forged copies of ITS id, not of every id. The test pins that a
    # trusted id which is not the provider's yields a verdict from the wrong header, so
    # the id must be learned from the mailbox, never typed from memory.
    assert row["authserv_id"] == "evil.example"
    pk2 = store.ingest_message(apk, fpk, 2, parse_message(_gmail_raw(mid="<a2@example.org>")), raw=raw,
                               auth_policy=_POLICY)
    assert store.message_auth([pk2])[pk2]["authserv_id"] == "mx.google.com"


def test_an_oversized_message_still_gets_a_verdict(store):
    apk, fpk = _setup(store)
    raw = _gmail_raw() + b"x" * (300 * 1024)
    pk = store.ingest_message(apk, fpk, 1, parse_message(raw), raw=raw, auth_policy=_POLICY)
    assert store.get_message(pk)["body_state"] == "too_large"
    assert store.message_auth([pk])[pk]["auth_state"] == "verified"


def test_list_threads_carries_the_newest_messages_verdict(store):
    apk, fpk = _setup(store)
    raw = _gmail_raw()
    store.ingest_message(apk, fpk, 1, parse_message(raw), raw=raw, auth_policy=_POLICY)
    store.ingest_message(apk, fpk, 2, parse_message(_BOUNCE), raw=_BOUNCE, auth_policy=_POLICY)
    rows = {r["subject"]: r for r in store.list_threads()}
    assert rows["Angebot"]["newest_auth_state"] == "verified" and rows["Angebot"]["newest_machine_kind"] == ""
    assert rows["Undelivered Mail Returned to Sender"]["newest_machine_kind"] == "bounce"


def test_the_backfill_recomputes_under_a_new_policy_from_the_snapshot(svc):
    """MUTATION: make messages_for_verification ignore policy_key and the second run rewrites every row."""
    store = svc.store
    apk, fpk = _setup(store)
    raw = _gmail_raw()
    pk = store.ingest_message(apk, fpk, 1, parse_message(raw), raw=raw)
    assert store.message_auth([pk])[pk]["auth_state"] == "unknown"
    # the raw bytes are gone (retention, or a header-only tier): the snapshot must do
    store._conn().execute("DELETE FROM message_raw WHERE message_pk=?", (pk,))
    store._conn().commit()
    assert svc.backfill_verification("bob@example.com", _POLICY) == 1
    row = store.message_auth([pk])[pk]
    assert row["auth_state"] == "verified" and row["policy_key"] == policy_key(_POLICY)
    assert svc.backfill_verification("bob@example.com", _POLICY) == 0, "already at this policy"
    assert svc.backfill_verification("bob@example.com", dict(_POLICY, trusted_authserv_id="other.example")) == 1


def test_a_migrated_row_without_a_snapshot_is_reparsed_from_its_cached_raw(svc):
    store = svc.store
    apk, fpk = _setup(store)
    raw = _gmail_raw()
    pk = store.ingest_message(apk, fpk, 1, parse_message(raw), raw=raw)
    store._conn().execute("DELETE FROM message_auth WHERE message_pk=?", (pk,))
    store._conn().commit()
    assert svc.backfill_verification("bob@example.com", _POLICY) == 1
    assert store.message_auth([pk])[pk]["auth_state"] == "verified"


def test_learn_provider_reads_the_majority_topmost_id(svc):
    store = svc.store
    apk, fpk = _setup(store)
    for i in range(3):
        raw = _gmail_raw(mid=f"<m{i}@example.org>")
        store.ingest_message(apk, fpk, i + 1, parse_message(raw), raw=raw)
    learned = svc.learn_provider("bob@example.com")
    assert learned == {"authserv_id": "mx.google.com", "profile": "rfc8601", "count": 3, "total": 3}
    assert svc.learn_provider("nobody@example.com")["authserv_id"] == ""


def test_learn_provider_recognises_the_microsoft_form(svc):
    store = svc.store
    apk, fpk = _setup(store)
    ms = ("Authentication-Results: spf=pass (sender IP is 10.2.3.4) smtp.mailfrom=fabrikam.com; contoso.com; "
          "dkim=none (message not signed) header.d=none; contoso.com; dmarc=pass action=none header.from=fabrikam.com; "
          "compauth=pass reason=100\n")
    for i in range(3):
        raw = (ms + f"From: a@fabrikam.com\nTo: bob@example.com\nSubject: x\nMessage-ID: <ms{i}@fabrikam.com>\n\nhi\n").encode()
        store.ingest_message(apk, fpk, i + 1, parse_message(raw), raw=raw)
    learned = svc.learn_provider("bob@example.com")
    assert learned["profile"] == "microsoft" and learned["authserv_id"] == ""
    assert svc.backfill_verification("bob@example.com", {"trusted_authserv_id": "", "auth_profile": "microsoft",
                                                          "own_addresses": [], "own_domains": []}) == 3
    states = {r["auth_state"] for r in store.message_auth([1, 2, 3]).values()}
    assert states == {"verified"}, "dmarc=pass in the id-less header verifies; compauth alone never would"


def test_the_sent_id_ledger_and_our_own_mail_coming_back(store):
    apk, fpk = _setup(store)
    assert not store.is_sent_id(apk, "<ours@example.com>")
    rid = store.record_sent_id(apk, "<ours@example.com>", to_addrs="alice@example.org", sent_by="agent", case_id="ABCDE12345")
    assert store.record_sent_id(apk, "ours@example.com") == rid, "idempotent, brackets normalised"
    assert store.is_sent_id(apk, "ours@example.com") and store.sent_id(apk, "<ours@example.com>")["case_id"] == "ABCDE12345"
    assert store.mark_sent_delivery(apk, "<ours@example.com>", "sent") and store.sent_id(apk, "<ours@example.com>")["sent_at"]
    raw = _gmail_raw(mid="<ours@example.com>", sender="Bob <bob@example.com>")
    pk = store.ingest_message(apk, fpk, 1, parse_message(raw), raw=raw, auth_policy=_POLICY)
    assert store.message_auth([pk])[pk]["machine_kind"] == "own_loop"
    other = store.upsert_account("carol@example.net", "imap", "carol@example.net")
    assert not store.is_sent_id(other, "<ours@example.com>"), "the ledger is per account"


def test_the_service_attaches_the_summary_to_thread_message_and_agent_rows(svc):
    store = svc.store
    apk, fpk = _setup(store)
    raw = _gmail_raw()
    pk = store.ingest_message(apk, fpk, 1, parse_message(raw), raw=raw, auth_policy=_POLICY)
    threads = svc.annotate_visibility(svc.list_threads())
    assert threads[0]["auth"]["state"] == "verified" and threads[0]["auth"]["aligned_by"] == "dmarc"
    msgs = svc.annotate_visibility(svc.thread_messages(threads[0]["thread_id"]))
    assert msgs[0]["auth"]["state"] == "verified" and msgs[0]["auth"]["machine_kind"] == ""
    agent = svc.list_for_agent(folder="INBOX")
    assert agent[0]["auth"]["state"] == "verified"
    assert svc.message_verdict(pk)["headers"]["auth_results"]
    assert summary(None) == {"state": "unknown", "source": "none", "aligned_by": "", "via_domain": "",
                             "dkim_domain": "", "from_domain": "", "dmarc": "", "flags": [],
                             "machine_kind": "", "machine_reason": ""}


def test_the_phishing_scorer_reads_the_verdict(monkeypatch):
    from vaf.tools import mail_utils
    score, reasons = mail_utils._phishing_score({"from": "ceo@example.org", "subject": "hi", "body_snippet": "",
                                                 "auth": {"flags": ["own_domain_spoof", "reply_to_mismatch"], "dmarc": "fail"}})
    assert score == 12 and reasons == ["own_domain_spoof", "authentication_failed", "reply_to_mismatch"]
    assert mail_utils._phishing_score({"from": "ceo@example.org", "subject": "hi"})[0] == 0, "no verdict, no change"
    monkeypatch.setattr(mail_utils, "_phishing_filter_policy", lambda: (True, 3, {"example.org"}))
    base = {"from": "ceo@example.org", "subject": "urgent wire transfer needed", "body_snippet": "click", "category": ""}
    high = dict(base, auth={"state": "unverified", "flags": ["dmarc_fail"], "dmarc": "fail"})
    ok = dict(base, auth={"state": "verified", "flags": [], "dmarc": "pass"})
    rows = mail_utils.annotate_messages_with_agent_visibility([high, ok, dict(base)])
    assert rows[0]["suspicious_for_agent"] is True, "a trusted domain that did not authenticate gets no bypass"
    assert rows[1]["suspicious_for_agent"] is False and rows[2]["suspicious_for_agent"] is False
    safe, blocked = mail_utils.filter_phishing_messages_for_agent([high, ok])
    assert blocked == 1 and safe == [ok]


def test_the_inbox_keeps_machine_mail_off_waits_and_lists_it_as_bulk():
    from vaf.core import inbox
    assert inbox.is_bulk_mail({"category": "primary", "from_addr": "a@b", "newest_machine_kind": "bounce"})
    assert inbox.is_bulk_mail({"category": "work", "from_addr": "a@b", "newest_machine_kind": "auto_reply"})
    assert inbox.is_bulk_mail({"category": "primary", "from_addr": "a@b", "newest_machine_kind": "bounce"}), "a header fact outranks the label"
    assert not inbox.is_bulk_mail({"category": "primary", "from_addr": "no-reply@b", "newest_machine_kind": "bulk"}), "the person's primary label is the last word on a guess"
    assert inbox.is_bulk_mail({"category": "", "from_addr": "Shop <shop@b>", "newest_machine_kind": "bulk"}), "a List-Unsubscribe with no label is bulk"
    assert not inbox.is_bulk_mail({"category": "primary", "from_addr": "a@b", "newest_machine_kind": "calendar"})
    thread = {"newest_special_use": "\\Inbox", "newest_answered_at": None, "last_date_ts": 100.0, "unread_count": 1,
              "snippet": "Können wir telefonieren?", "from_addr": "Lena <lena@example.com>"}
    assert inbox.mail_thread_state(dict(thread, newest_machine_kind="auto_reply"), None, waits_threshold_value=0.6)["waits"] is False
    assert inbox.mail_thread_state(dict(thread, newest_machine_kind="calendar"), None, waits_threshold_value=0.6)["waits"] is True
    assert inbox.mail_thread_state(thread, None, waits_threshold_value=0.6)["waits"] is True


def test_the_policy_of_an_account_and_its_key():
    pol = auth_policy_for_account({"email": "Bob@Example.com", "account_id": "bob@example.com",
                                   "aliases": ["info@example.com"], "trusted_authserv_id": "MX.Google.com"})
    assert pol == {"trusted_authserv_id": "mx.google.com", "auth_profile": "rfc8601",
                   "own_addresses": ["bob@example.com", "info@example.com"], "own_domains": ["example.com"]}
    assert auth_policy_for_account({"provider": "microsoft", "email": "x@contoso.com"})["auth_profile"] == "microsoft"
    assert auth_policy_for_account(None)["trusted_authserv_id"] == ""
    assert policy_key(pol) == policy_key(dict(pol)) and policy_key(pol) != policy_key(dict(pol, trusted_authserv_id=""))
    assert len(policy_key(pol)) == 16


def test_the_snapshot_round_trips_into_the_same_verdict():
    parsed = parse_message(_gmail_raw())
    direct = assess(parsed, policy=_POLICY)
    again = assess(parsed_from_snapshot(identity_snapshot(parsed), from_addr=parsed.from_addr,
                                        message_id=parsed.message_id, subject=parsed.subject), policy=_POLICY)
    for key in ("auth_state", "aligned_by", "authserv_id", "machine_kind", "flags"):
        assert direct[key] == again[key], key
    assert assess(ParsedMessage(), policy=None)["auth_state"] == "unknown", "an empty message never raises"
