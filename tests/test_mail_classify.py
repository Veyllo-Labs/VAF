# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Structural machine-mail recognition (vaf/mail/classify.py): every kind has a fixture
built from ParsedMessage fields alone, the precedence between the rules is pinned, the
lexical half (inbox.is_automated_sender) can only add a verdict, own mail is matched by
addr-spec regardless of case, and every machine verdict names the header that decided it.

Mutation proof (verified by editing classify.py and reverting): the docstrings below name
the line whose removal turns the test red."""
import dataclasses

import pytest

from vaf.core.inbox import _BULK_CATEGORIES
from vaf.mail.classify import MACHINE_KINDS, MachineVerdict, classify_machine, is_machine
from vaf.mail.parser import ParsedMessage

_PERSON = "Alice <alice@example.com>"

# (label, ParsedMessage kwargs, classify kwargs, expected kind, expected reason)
CASES = [
    # 1. bounce (RFC 3464)
    ("bounce: multipart/report delivery-status",
     dict(from_addr="Mail Delivery System <MAILER-DAEMON@mx.example.com>", report_type="delivery-status",
          dsn_action="failed", return_path_null=True), {}, "bounce", "report_type=delivery-status"),
    ("bounce: a delivered DSN is still a report",
     dict(from_addr=_PERSON, report_type="delivery-status", dsn_action="delivered"), {}, "bounce",
     "report_type=delivery-status"),
    ("bounce: null reverse-path from MAILER-DAEMON, no report body",
     dict(from_addr="MAILER-DAEMON@mx.example.com", return_path_null=True), {}, "bounce",
     "return_path=<> from=mailer-daemon"),
    ("bounce: null reverse-path from Postmaster, any case",
     dict(from_addr="The Postmaster <POSTMASTER@example.com>", return_path_null=True), {}, "bounce",
     "return_path=<> from=postmaster"),
    # 2. mdn (RFC 8098)
    ("mdn: disposition-notification",
     dict(from_addr=_PERSON, report_type="disposition-notification", original_message_id="<m1@example.com>"), {},
     "mdn", "report_type=disposition-notification"),
    # 3. calendar (RFC 5546)
    ("calendar: REQUEST", dict(from_addr=_PERSON, calendar_method="REQUEST"), {}, "calendar",
     "calendar_method=REQUEST"),
    ("calendar: CANCEL", dict(from_addr=_PERSON, calendar_method="CANCEL"), {}, "calendar",
     "calendar_method=CANCEL"),
    # 4. auto_reply (RFC 3834, MS-OXCMAIL)
    ("auto_reply: Auto-Submitted auto-replied", dict(from_addr=_PERSON, auto_submitted="auto-replied"), {},
     "auto_reply", "auto_submitted=auto-replied"),
    ("auto_reply: Auto-Submitted auto-generated with a parameter",
     dict(from_addr=_PERSON, auto_submitted="auto-generated; owner-info=x"), {}, "auto_reply",
     "auto_submitted=auto-generated"),
    ("auto_reply: X-Auto-Response-Suppress OOF among receipts",
     dict(from_addr=_PERSON, x_auto_response_suppress="dr, ndr, oof"), {}, "auto_reply",
     "x_auto_response_suppress=oof"),
    ("auto_reply: X-Auto-Response-Suppress All", dict(from_addr=_PERSON, x_auto_response_suppress="all"), {},
     "auto_reply", "x_auto_response_suppress=all"),
    ("auto_reply: an X-Autoreply family header", dict(from_addr=_PERSON, auto_reply_headers=["x-autoreply"]), {},
     "auto_reply", "auto_reply_headers=x-autoreply"),
    ("auto_reply: Precedence auto_reply", dict(from_addr=_PERSON, precedence="auto_reply"), {}, "auto_reply",
     "precedence=auto_reply"),
    ("auto_reply: Precedence auto-reply (gateway spelling)", dict(from_addr=_PERSON, precedence="auto-reply"), {},
     "auto_reply", "precedence=auto_reply"),
    # 5. list (RFC 2919, RFC 2369)
    ("list: List-Id", dict(from_addr=_PERSON, list_id="<dev.lists.example.org>"), {}, "list", "list_id"),
    ("list: List-Post without a List-Id", dict(from_addr=_PERSON, list_headers=["list-post"]), {}, "list",
     "list_headers=list-post"),
    ("list: Precedence list", dict(from_addr=_PERSON, precedence="list"), {}, "list", "precedence=list"),
    # 6. own_loop
    ("own_loop: From is an own address", dict(from_addr="Me <me@example.com>"),
     dict(own_addresses=["me@example.com"]), "own_loop", "from=own address"),
    ("own_loop: Message-ID is one this account sent", dict(from_addr=_PERSON, message_id="<sent1@example.com>"),
     dict(is_own_message_id=lambda mid: mid == "<sent1@example.com>"), "own_loop", "message_id=own"),
    # 7. bulk
    ("bulk: Precedence bulk", dict(from_addr=_PERSON, precedence="bulk"), {}, "bulk", "precedence=bulk"),
    ("bulk: Precedence junk", dict(from_addr=_PERSON, precedence="junk"), {}, "bulk", "precedence=junk"),
    ("bulk: Feedback-ID", dict(from_addr=_PERSON, feedback_id="a:b:c:shop"), {}, "bulk", "feedback_id"),
    ("bulk: List-Unsubscribe without a List-Id", dict(from_addr=_PERSON, list_headers=["list-unsubscribe"]), {},
     "bulk", "list_headers=list-unsubscribe"),
    ("bulk: any other multipart/report type (RFC 6522)", dict(from_addr=_PERSON, report_type="feedback-report"),
     {}, "bulk", "report_type=feedback-report"),
    ("bulk: a bulk category from the store", dict(from_addr=_PERSON), dict(category="Spam"), "bulk",
     "category=spam"),
    ("bulk: the lexical half, a no-reply sender", dict(from_addr="Shop <noreply@shop.example>"), {}, "bulk",
     "automated_sender"),
    ("bulk: the lexical half, a non-primary Gmail category", dict(from_addr=_PERSON),
     dict(category="promotions"), "bulk", "category=promotions"),
    # 8. null_return_path (RFC 3834 s2)
    ("null_return_path: Return-Path <> alone", dict(from_addr=_PERSON, return_path_null=True), {},
     "null_return_path", "return_path=<>"),
    # 9. a person may have written it
    ("person: a plain mail", dict(from_addr=_PERSON, message_id="<p1@example.com>", subject="Lunch?",
                                  return_path="alice@example.com", reply_to="alice@example.com"), {}, "", ""),
    ("person: Auto-Submitted no is a person", dict(from_addr=_PERSON, auto_submitted="no"), {}, "", ""),
    ("person: receipt suppression alone (DR, NDR) says nothing about the author",
     dict(from_addr=_PERSON, x_auto_response_suppress="dr, ndr, rn, nrn"), {}, "", ""),
    ("person: an unknown Precedence value", dict(from_addr=_PERSON, precedence="first-class"), {}, "", ""),
    ("person: info@ and support@ are people", dict(from_addr="Info Desk <info@example.com>"), {}, "", ""),
    ("person: an own address that is not the sender",
     dict(from_addr=_PERSON), dict(own_addresses=["me@example.com"]), "", ""),
    ("person: a Message-ID the account did not send",
     dict(from_addr=_PERSON, message_id="<p2@example.com>"), dict(is_own_message_id=lambda mid: False), "", ""),
]


@pytest.mark.parametrize("label, fields, kwargs, kind, reason", CASES, ids=[c[0] for c in CASES])
def test_table(label, fields, kwargs, kind, reason):
    verdict = classify_machine(ParsedMessage(**fields), **kwargs)
    assert verdict == MachineVerdict(kind, reason), label
    assert is_machine(verdict) is (kind != ""), label


def test_every_machine_kind_has_a_fixture_and_every_verdict_a_reason():
    """The table covers each of MACHINE_KINDS at least once, and no machine verdict comes
    back without the header or rule that decided it."""
    seen = set()
    for label, fields, kwargs, kind, _reason in CASES:
        verdict = classify_machine(ParsedMessage(**fields), **kwargs)
        seen.add(verdict.kind)
        if verdict.kind:
            assert verdict.kind in MACHINE_KINDS, label
            assert verdict.reason, label
        else:
            assert verdict.reason == "", label
    assert seen == set(MACHINE_KINDS) | {""}


# --- precedence ---------------------------------------------------------------------------

def test_a_bounce_that_also_carries_auto_submitted_is_a_bounce():
    """Mutation: dropping classify.py:131 (`if report_type == "delivery-status":` and its
    return) makes this come back auto_reply and go red."""
    p = ParsedMessage(from_addr="MAILER-DAEMON@mx.example.com", report_type="delivery-status",
                      auto_submitted="auto-generated", return_path_null=True)
    assert classify_machine(p) == MachineVerdict("bounce", "report_type=delivery-status")


def test_an_auto_reply_that_carries_list_unsubscribe_is_an_auto_reply():
    """Mutation: dropping classify.py:155 (`if auto_submitted and auto_submitted != "no":`
    and its return) makes this come back bulk via List-Unsubscribe and go red."""
    p = ParsedMessage(from_addr=_PERSON, auto_submitted="auto-replied", list_headers=["list-unsubscribe"])
    assert classify_machine(p) == MachineVerdict("auto_reply", "auto_submitted=auto-replied")


def test_a_list_post_with_precedence_bulk_is_a_list():
    """Mutation: dropping classify.py:171 (`if _s(parsed.list_id):` and its return) makes
    this come back bulk via Precedence and go red."""
    p = ParsedMessage(from_addr=_PERSON, list_id="<dev.lists.example.org>", precedence="bulk",
                      list_headers=["list-post", "list-unsubscribe"])
    assert classify_machine(p) == MachineVerdict("list", "list_id")


def test_an_invitation_with_response_suppression_is_a_calendar_mail():
    """Exchange stamps invitations with X-Auto-Response-Suppress; the calendar structure
    outranks the auto-reply header, and the mail is a calendar object, not an auto-reply."""
    p = ParsedMessage(from_addr=_PERSON, calendar_method="REQUEST", x_auto_response_suppress="all")
    assert classify_machine(p) == MachineVerdict("calendar", "calendar_method=REQUEST")


def test_an_mdn_with_a_null_reverse_path_is_an_mdn():
    p = ParsedMessage(from_addr=_PERSON, report_type="disposition-notification", return_path_null=True)
    assert classify_machine(p) == MachineVerdict("mdn", "report_type=disposition-notification")


def test_own_mail_outranks_bulk_and_the_lexical_half():
    """An own mail that came back with Precedence: bulk and a no-reply sender is own_loop:
    rule 6 sits above rule 7, and the lexical half runs last."""
    p = ParsedMessage(from_addr="noreply@example.com", precedence="bulk")
    assert classify_machine(p, own_addresses=["noreply@example.com"]) == MachineVerdict("own_loop", "from=own address")


def test_a_list_mail_from_an_own_address_is_a_list():
    p = ParsedMessage(from_addr="me@example.com", list_id="<dev.lists.example.org>")
    assert classify_machine(p, own_addresses=["me@example.com"]).kind == "list"


# --- the lexical half ------------------------------------------------------------------------

def test_the_lexical_half_raises_an_otherwise_clean_mail_to_bulk():
    """No header says machine; only the address does. Mutation: dropping classify.py:199
    (`if is_automated_sender(from_header, cat):` and its return) makes this come back ""
    and go red."""
    p = ParsedMessage(from_addr="noreply@example.com", message_id="<n1@example.com>", subject="Your order")
    assert classify_machine(p) == MachineVerdict("bulk", "automated_sender")


def test_the_lexical_half_never_removes_a_header_verdict():
    """A no-reply sender on an auto-reply stays auto_reply: the lexical half is the last
    rule and can only add."""
    p = ParsedMessage(from_addr="noreply@example.com", auto_submitted="auto-replied")
    assert classify_machine(p) == MachineVerdict("auto_reply", "auto_submitted=auto-replied")


def test_a_primary_tab_does_not_shield_a_no_reply_sender():
    """Deliberate: the inbox's is_bulk_mail lets a primary category outrank the heuristic
    because it decides VISIBILITY; this verdict decides whether an ANSWER reaches anyone,
    and nobody reads a reply to noreply@ whichever tab it was filed under."""
    p = ParsedMessage(from_addr="noreply@example.com")
    assert classify_machine(p, category="primary") == MachineVerdict("bulk", "automated_sender")


def test_the_bulk_categories_are_the_inbox_registry():
    """Rule 2: the category list is imported from vaf.core.inbox, not copied; every entry
    of the registry lands the same mail in bulk."""
    for cat in sorted(_BULK_CATEGORIES):
        assert classify_machine(ParsedMessage(from_addr=_PERSON), category=cat.upper()) == \
            MachineVerdict("bulk", f"category={cat}"), cat


def test_a_clean_personal_mail_stays_a_person():
    p = ParsedMessage(message_id="<c1@example.com>", subject="Re: Lunch?", from_addr="Bob Meier <bob@example.org>",
                      to_addrs="alice@example.com", reply_to="bob@example.org", return_path="bob@example.org",
                      in_reply_to="<a1@example.com>", refs=["<a1@example.com>"], auto_submitted="no",
                      dkim_domains=["example.org"], received=["from mx.example.org by mx.example.com"])
    verdict = classify_machine(p, own_addresses=["alice@example.com"], is_own_message_id=lambda mid: False,
                               category="primary")
    assert verdict == MachineVerdict("", "")
    assert is_machine(verdict) is False


# --- own_loop details ------------------------------------------------------------------------

def test_own_address_matching_is_case_insensitive_and_reads_the_addr_spec():
    """Mutation: dropping classify.py:183 (`if own and any(spec in own ...)` and its
    return) makes every one of these come back "" and go red."""
    for from_addr in ("Name <Addr@Example.com>", "ADDR@EXAMPLE.COM", "addr@example.com", "Addr@example.com, Other <o@example.org>"):
        assert classify_machine(ParsedMessage(from_addr=from_addr), own_addresses=["addr@example.com"]).kind == "own_loop", from_addr
    for own in (["ADDR@EXAMPLE.COM"], ["Me <addr@example.com>"], {"  addr@example.com "}, ("x@example.org", "addr@example.com")):
        assert classify_machine(ParsedMessage(from_addr="Name <Addr@Example.com>"), own_addresses=own).kind == "own_loop", own


def test_own_address_matching_uses_the_address_not_the_display_name():
    """A stranger writing with the account's name in the display part is not own mail."""
    p = ParsedMessage(from_addr="me@example.com <stranger@example.org>")
    assert classify_machine(p, own_addresses=["me@example.com"]).kind == ""


def test_is_own_message_id_is_consulted_only_when_given_and_only_with_an_id():
    calls = []

    def probe(mid):
        calls.append(mid)
        return True

    assert classify_machine(ParsedMessage(from_addr=_PERSON, message_id="<x1@example.com>")).kind == ""
    assert classify_machine(ParsedMessage(from_addr=_PERSON, message_id=""), is_own_message_id=probe).kind == ""
    assert calls == []
    verdict = classify_machine(ParsedMessage(from_addr=_PERSON, message_id="<x1@example.com>"), is_own_message_id=probe)
    assert verdict == MachineVerdict("own_loop", "message_id=own")
    assert calls == ["<x1@example.com>"]


def test_is_own_message_id_is_not_asked_once_a_header_rule_decided():
    calls = []
    p = ParsedMessage(from_addr=_PERSON, message_id="<x2@example.com>", report_type="delivery-status")
    assert classify_machine(p, is_own_message_id=lambda mid: calls.append(mid) or True).kind == "bounce"
    assert calls == []


# --- null reverse-path ------------------------------------------------------------------------

def test_a_null_return_path_alone_is_null_return_path():
    """Mutation: dropping classify.py:203 (`if return_path_null:` and its return, the rule-8
    one) makes this come back "" and go red."""
    p = ParsedMessage(from_addr=_PERSON, return_path_null=True, message_id="<z1@example.com>")
    assert classify_machine(p) == MachineVerdict("null_return_path", "return_path=<>")


def test_a_null_return_path_from_a_person_is_not_a_bounce():
    """Rule 1's second half needs BOTH the null reverse-path and the mailer-daemon or
    postmaster local part: a null path from any other address is rule 8, and a postmaster
    mail with a real reverse-path is no bounce (the lexical half still reads postmaster@
    as an address nobody answers from, so it lands in bulk, not in "")."""
    assert classify_machine(ParsedMessage(from_addr=_PERSON, return_path_null=True)).kind == "null_return_path"
    assert classify_machine(ParsedMessage(from_addr="postmaster@example.com")) == MachineVerdict("bulk", "automated_sender")


# --- API shape ------------------------------------------------------------------------------

def test_is_machine_accepts_a_verdict_or_a_kind():
    assert is_machine(MachineVerdict("bounce", "report_type=delivery-status")) is True
    assert is_machine(MachineVerdict("", "")) is False
    for kind in MACHINE_KINDS:
        assert is_machine(kind) is True
    assert is_machine("") is False
    assert is_machine(None) is False


def test_the_verdict_is_frozen():
    verdict = classify_machine(ParsedMessage(from_addr=_PERSON, precedence="bulk"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        verdict.kind = ""


def test_classify_never_raises_on_a_hand_built_message():
    """Every field None or garbage still yields a verdict: one odd message must never abort
    an ingest."""
    p = ParsedMessage(message_id=None, from_addr=None, auto_submitted=None, precedence=None,
                      x_auto_response_suppress=None, auto_reply_headers=None, list_id=None, list_headers=None,
                      feedback_id=None, report_type=None, calendar_method=None, return_path_null=None)
    assert classify_machine(p, own_addresses=None, category=None) == MachineVerdict("", "")
    odd = ParsedMessage(from_addr="<>", list_headers=[None, 7], auto_reply_headers=[""], report_type=42)
    assert classify_machine(odd, own_addresses=[None, "", "garbage"]).kind == "bulk"  # report_type=42 is a report
    assert classify_machine(ParsedMessage(from_addr="not an address")).kind == ""
