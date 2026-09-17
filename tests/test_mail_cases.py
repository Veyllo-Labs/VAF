# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Cases (vaf/mail/cases.py): attribution never guesses (a token proves knowledge of a
case, the participant check proves membership, two cases are a conflict, a verified anchor
without a case is an orphan), the trust ladder caps at T0 on any identity flag, and the
decision answers only a verified sender through an open door, within the caps. Isolated:
tmp store, pinned key, pinned case secret.

MUTATION: drop the participant check from `attribute` and the foreign test goes red; return
"T2" for a via state and the ladder test goes red; drop the cap comparison from `decide`
and the cap test goes red."""
import os

import pytest

import vaf.mail.crypto as mail_crypto
from vaf.mail import case_token, cases
from vaf.mail.parser import ParsedMessage
from vaf.mail.store import MailStore

_SCOPE = "12345678-1234-1234-1234-123456789abc"
_OTHER = "00000000-0000-0000-0000-000000000000"
_ACC = "bob@example.com"


@pytest.fixture(autouse=True)
def _pinned(monkeypatch):
    old = mail_crypto._cached_key
    mail_crypto._cached_key = os.urandom(32)
    monkeypatch.setattr(case_token, "_root_secret", lambda: "unit-test-root-secret-of-thirty-two-bytes")
    yield
    mail_crypto._cached_key = old


@pytest.fixture()
def store(tmp_path):
    s = MailStore(_SCOPE, base_dir=tmp_path)
    yield s
    s.close()


def _seed(store):
    apk = store.upsert_account(_ACC, "imap", _ACC)
    fpk = store.upsert_folder(apk, "INBOX", special_use="\\Inbox")
    pk = store.ingest_message(apk, fpk, 1, ParsedMessage(
        message_id="<q@example.org>", subject="Angebot", from_addr="Alice <alice@example.org>",
        to_addrs=_ACC, cc_addrs="carol@example.org", date_ts=1_700_000_000, body_text="Passt das?"), server_flags=[])
    thread_id = store.get_message(pk)["thread_id"]
    case_id = case_token.mint_case_id()
    store.open_case(apk, case_id, thread_id=thread_id, correspondent="alice@example.org", subject_norm="angebot")
    anchor = case_token.mint_message_id(_SCOPE, _ACC, case_id, "example.com")
    store.record_sent_id(apk, anchor, case_id=case_id, to_addrs="alice@example.org", sent_by="front_office", in_reply_to="<q@example.org>")
    return apk, pk, thread_id, case_id, anchor


def _reply(sender, refs, subject="Re: Angebot", **kw):
    p = ParsedMessage(message_id="<r@example.org>", subject=subject, from_addr=sender, refs=list(refs), **kw)
    p.in_reply_to = refs[-1] if refs else ""
    return p


def test_a_reply_to_our_anchor_from_a_participant_is_certain(store):
    apk, pk, thread_id, case_id, anchor = _seed(store)
    out = cases.attribute(store, apk, user_scope_id=_SCOPE, account_id=_ACC,
                          parsed=_reply("Alice <alice@example.org>", ["<q@example.org>", anchor]))
    assert out.outcome == "case" and out.case_id == case_id and out.signal == "own_id" and out.certainty == "certain"
    cc = cases.attribute(store, apk, user_scope_id=_SCOPE, account_id=_ACC,
                         parsed=_reply("Carol <carol@example.org>", [anchor]))
    assert cc.outcome == "case", "a Cc of the thread is a participant"


def test_a_sent_id_without_an_anchor_attributes_too(store):
    apk, pk, thread_id, case_id, anchor = _seed(store)
    store.record_sent_id(apk, "<plain@example.com>", case_id=case_id, to_addrs="alice@example.org")
    out = cases.attribute(store, apk, user_scope_id=_SCOPE, account_id=_ACC,
                          parsed=_reply("alice@example.org", ["<plain@example.com>"]))
    assert out.outcome == "case" and out.signal == "sent_id"


def test_a_stranger_quoting_our_anchor_is_foreign(store):
    apk, pk, thread_id, case_id, anchor = _seed(store)
    out = cases.attribute(store, apk, user_scope_id=_SCOPE, account_id=_ACC,
                          parsed=_reply("Mallory <mallory@evil.example>", [anchor]))
    assert out.outcome == "foreign" and out.case_id == case_id
    second = cases.attribute(store, apk, user_scope_id=_SCOPE, account_id=_ACC,
                             parsed=_reply("alice@example.net", [anchor]), extra_participants=["alice@example.net"])
    assert second.outcome == "case", "the contact book's second address of the same person passes"


def test_two_cases_in_one_mail_are_a_conflict_and_an_anchor_without_a_case_is_an_orphan(store):
    apk, pk, thread_id, case_id, anchor = _seed(store)
    other = case_token.mint_case_id()
    store.open_case(apk, other, correspondent="alice@example.org")
    other_anchor = case_token.mint_message_id(_SCOPE, _ACC, other, "example.com")
    store.record_sent_id(apk, other_anchor, case_id=other)
    out = cases.attribute(store, apk, user_scope_id=_SCOPE, account_id=_ACC,
                          parsed=_reply("alice@example.org", [anchor, other_anchor]))
    assert out.outcome == "conflict" and set(out.hints) >= {case_id, other}
    ghost = case_token.mint_message_id(_SCOPE, _ACC, case_token.mint_case_id(), "example.com")
    orphan = cases.attribute(store, apk, user_scope_id=_SCOPE, account_id=_ACC,
                             parsed=_reply("alice@example.org", [ghost]))
    assert orphan.outcome == "orphan" and orphan.signal == "own_id"


def test_another_scopes_anchor_is_reported_never_trusted(store):
    apk, pk, thread_id, case_id, anchor = _seed(store)
    foreign = case_token.mint_message_id(_OTHER, _ACC, case_id, "example.com")
    out = cases.attribute(store, apk, user_scope_id=_SCOPE, account_id=_ACC,
                          parsed=_reply("alice@example.org", [foreign]), thread_id=thread_id)
    assert "anchor_unverified" in out.hints
    assert out.outcome == "case" and out.signal == "thread", "the store's thread still carries the case, strong not certain"
    assert out.certainty == "strong"


def test_the_thread_alone_is_strong_and_a_stranger_on_the_thread_is_still_foreign(store):
    apk, pk, thread_id, case_id, anchor = _seed(store)
    out = cases.attribute(store, apk, user_scope_id=_SCOPE, account_id=_ACC,
                          parsed=_reply("alice@example.org", ["<q@example.org>"]), thread_id=thread_id)
    assert out.outcome == "case" and out.signal == "thread" and out.certainty == "strong"
    stranger = cases.attribute(store, apk, user_scope_id=_SCOPE, account_id=_ACC,
                               parsed=_reply("mallory@evil.example", ["<q@example.org>"]), thread_id=thread_id)
    assert stranger.outcome == "foreign"


def test_a_subject_tag_and_a_plus_address_attribute_and_a_wrong_check_is_only_a_hint(store):
    apk, pk, thread_id, case_id, anchor = _seed(store)
    tag = case_token.subject_tag(_SCOPE, _ACC, case_id)
    out = cases.attribute(store, apk, user_scope_id=_SCOPE, account_id=_ACC,
                          parsed=_reply("alice@example.org", [], subject=f"AW: {tag} Angebot"))
    assert out.outcome == "case" and out.signal == "subject_tag"
    wrong = tag[:-5] + "ZZZZ]"
    bad = cases.attribute(store, apk, user_scope_id=_SCOPE, account_id=_ACC,
                          parsed=_reply("alice@example.org", [], subject=f"Re: {wrong}"))
    assert bad.outcome == "new" and "subject_tag_unverified" in bad.hints
    plus = _reply("alice@example.org", [])
    plus.to_addrs = case_token.plus_address("bob", "example.com", case_id)
    out2 = cases.attribute(store, apk, user_scope_id=_SCOPE, account_id=_ACC, parsed=plus, own_domains=["example.com"])
    assert out2.outcome == "case" and out2.signal == "plus_address"


def test_a_bounce_is_a_report_about_our_send_never_a_reply(store):
    apk, pk, thread_id, case_id, anchor = _seed(store)
    dsn = ParsedMessage(message_id="<dsn@example.net>", from_addr="MAILER-DAEMON@example.net", report_type="delivery-status",
                        dsn_action="failed", original_message_id=anchor)
    out = cases.attribute(store, apk, user_scope_id=_SCOPE, account_id=_ACC, parsed=dsn, machine_kind="bounce")
    assert out.outcome == "report" and out.case_id == case_id and out.report_of == anchor
    unknown = ParsedMessage(message_id="<dsn2@example.net>", from_addr="MAILER-DAEMON@example.net", report_type="delivery-status",
                            original_message_id="<nobody@example.com>")
    assert cases.attribute(store, apk, user_scope_id=_SCOPE, account_id=_ACC, parsed=unknown, machine_kind="bounce").outcome == "new"


def test_nothing_is_ever_guessed_from_a_subject_or_a_name(store):
    apk, pk, thread_id, case_id, anchor = _seed(store)
    out = cases.attribute(store, apk, user_scope_id=_SCOPE, account_id=_ACC,
                          parsed=_reply("Alice <alice@example.org>", [], subject="Re: Angebot"))
    assert out.outcome == "new" and out.case_id == "" and out.signal == ""


def test_candidate_ids_prefer_in_reply_to_then_newest_reference_then_exchanges_parent():
    p = ParsedMessage(message_id="<me@x>", refs=["<a@x>", "<b@x>", "<c@x>"])
    p.in_reply_to = "<c@x>"
    p.exchange_parent_id = "<p@x>"
    assert cases.candidate_ids(p) == ["<c@x>", "<b@x>", "<a@x>", "<p@x>"]
    assert cases.sender_address("Alice <Alice@Example.ORG>") == "alice@example.org" and cases.sender_address("nope") == ""
    assert cases.sender_name("Alice <alice@example.org>") == "Alice" and cases.sender_name("alice@example.org") == "alice@example.org"


def test_the_trust_ladder():
    verified = {"auth_state": "verified", "flags": []}
    assert cases.trust_level(auth=verified) == "T2"
    assert cases.trust_level(auth=verified, contact={"allow_as_assistant_user": True}) == "T3"
    assert cases.trust_level(auth=verified, contact={"allow_as_assistant_user": False}) == "T2", "an opted-out contact is not T3"
    attributed = cases.Attribution("case", "ABCDE12345", "own_id", "certain")
    assert cases.trust_level(auth=verified, attribution=attributed, agent_wrote_in_case=True) == "T4"
    assert cases.trust_level(auth=verified, attribution=attributed, agent_wrote_in_case=False) == "T2"
    assert cases.trust_level(auth={"state": "via"}) == "T1"
    assert cases.trust_level(auth={"state": "unverified"}) == "T0"
    assert cases.trust_level(auth={"state": "unknown"}) == "T0"
    assert cases.trust_level(auth=None) == "T0"
    for flag in sorted(cases.CAPPING_FLAGS):
        assert cases.trust_level(auth={"auth_state": "verified", "flags": [flag]}) == "T0", flag
    assert cases.trust_level(auth=verified, machine_kind="auto_reply") == "T0"


def test_the_decision_answers_only_a_verified_sender_through_an_open_door():
    from vaf.core.channel_ingress_policy import set_front_office
    new = cases.Attribution("new")
    closed = None
    opened = set_front_office(None, True, "email", now=1)
    assert cases.decide(trust="T2", attribution=new, raw_policy=closed).action == "ignore"
    assert cases.decide(trust="T2", attribution=new, raw_policy=closed).reason == "not_paired"
    d = cases.decide(trust="T2", attribution=new, raw_policy=opened)
    assert d.action == "draft" and d.ingress_reason == "front_office_open" and d.reason == "ok"
    assert cases.decide(trust="T2", attribution=new, raw_policy=opened, reply_mode="send").action == "answer"
    # a contact under an open channel comes through the contact door the switch opened with it
    assert cases.decide(trust="T3", attribution=new, raw_policy=opened).ingress_reason == "contact_fallback_override"
    assert cases.decide(trust="T2", attribution=new, raw_policy=opened, opted_out=True).reason == "not_paired"
    assert cases.decide(trust="T1", attribution=new, raw_policy=opened).reason == "via"
    assert cases.decide(trust="T0", attribution=new, raw_policy=opened).reason == "unverified"
    assert cases.decide(trust="T2", attribution=new, raw_policy=opened, machine_kind="list").reason == "machine:list"
    assert cases.decide(trust="T2", attribution=cases.Attribution("report"), raw_policy=opened).reason == "report"
    for outcome in ("conflict", "foreign", "orphan"):
        assert cases.decide(trust="T2", attribution=cases.Attribution(outcome, "X"), raw_policy=opened).reason == outcome
    # T4: a reply into a case the agent wrote in gets through a CLOSED channel (the reply window rule)
    t4 = cases.decide(trust="T4", attribution=cases.Attribution("case", "X"), raw_policy=closed)
    assert t4.action == "draft" and t4.ingress_reason == "open_conversation"
    # the expert contact door: a T3 contact with the channel off
    door = {"email": {"allow_contact_fallback": True}}
    assert cases.decide(trust="T3", attribution=new, raw_policy=door).ingress_reason == "contact_fallback_override"
    assert cases.decide(trust="T2", attribution=new, raw_policy=door).action == "ignore"


def test_the_caps_park_the_rest_of_the_day():
    from vaf.core.channel_ingress_policy import set_front_office
    opened = set_front_office(None, True, "email", now=1)
    new = cases.Attribution("new")
    assert cases.decide(trust="T2", attribution=new, raw_policy=opened, replies_last_hour=2).action == "draft"
    assert cases.decide(trust="T2", attribution=new, raw_policy=opened, replies_last_hour=3).reason == "capped"
    assert cases.decide(trust="T2", attribution=new, raw_policy=opened, replies_last_day=10).reason == "capped"
    assert cases.decide(trust="T2", attribution=new, raw_policy=opened, replies_last_day=10, max_per_day=20).action == "draft"


def test_the_lock_window():
    from datetime import datetime, timezone
    now = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
    assert cases.lock_expired("2026-08-01T12:00:00+00:00", lock_days=30, now=now) is True
    assert cases.lock_expired("2026-09-10T12:00:00+00:00", lock_days=30, now=now) is False
    assert cases.lock_expired(None, lock_days=30, now=now) is False
    assert cases.lock_expired("garbage", lock_days=30, now=now) is False
