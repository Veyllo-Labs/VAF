# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The parser keeps the identity, provenance and machine-mail headers a verification and
attribution lane reads (EMAIL_CLIENT.md, "Verification and cases"): extraction only, every
field empty when the header is absent, the report sub-parts of a bounce never become its
body, and the walk still never raises."""
from vaf.mail.parser import ParsedMessage, parse_message


def _mail(headers: str, body: str = "hello\n") -> bytes:
    return (headers.strip("\n") + "\n\n" + body).encode("utf-8")


def test_a_plain_mail_leaves_every_new_field_empty():
    p = parse_message(_mail("From: Alice <alice@example.com>\nTo: bob@example.com\nSubject: Hi\nMessage-ID: <a1@example.com>"))
    empty = ParsedMessage()
    for name in ("reply_to", "sender", "return_path", "delivered_to", "in_reply_to", "auto_submitted", "precedence",
                 "x_auto_response_suppress", "list_id", "feedback_id", "report_type", "dsn_action",
                 "original_message_id", "calendar_method", "thread_index", "exchange_parent_id"):
        assert getattr(p, name) == getattr(empty, name) == "", name
    for name in ("auto_reply_headers", "list_headers", "auth_results", "arc_auth_results", "dkim_domains", "received"):
        assert getattr(p, name) == [], name
    assert p.return_path_null is False


def test_gmail_style_provenance_headers_are_kept_topmost_first():
    raw = _mail(
        "Delivered-To: bob@example.com\n"
        "Received: by 2002:a05:6000:1234 with SMTP id x1; Tue, 16 Sep 2026 10:00:00 -0700 (PDT)\n"
        "ARC-Authentication-Results: i=1; mx.google.com; dkim=pass header.i=@example.org\n"
        "Return-Path: <alice@example.org>\n"
        "Received: from mail.example.org (mail.example.org. [203.0.113.5]) by mx.google.com with ESMTPS id y2\n"
        "Authentication-Results: mx.google.com; dkim=pass header.i=@example.org header.s=s1; spf=pass smtp.mailfrom=alice@example.org; dmarc=pass header.from=example.org\n"
        "Authentication-Results: evil.example; dkim=pass header.i=@bank.example\n"
        "DKIM-Signature: v=1; a=rsa-sha256; d=example.org; s=s1; h=from:to; bh=x; b=y\n"
        "From: Alice <alice@example.org>\nReply-To: Alice Work <alice.work@example.org>\nSender: list-bot@example.org\n"
        "To: bob@example.com\nSubject: Hi\nMessage-ID: <a2@example.org>\n"
        "In-Reply-To: <b1@example.com>\nReferences: <b0@example.com>,<b1@example.com>\n"
        "Thread-Index: AQHZ1234567890abcdef\nX-MS-Exchange-Parent-Message-Id: <orig@example.org>")
    p = parse_message(raw)
    assert p.auth_results[0].startswith("mx.google.com;") and p.auth_results[1].startswith("evil.example;")
    assert p.arc_auth_results == ["i=1; mx.google.com; dkim=pass header.i=@example.org"]
    assert p.received[0].startswith("by 2002:a05") and p.received[1].startswith("from mail.example.org")
    assert p.dkim_domains == ["example.org"]
    assert p.return_path == "alice@example.org" and p.return_path_null is False
    assert p.delivered_to == "bob@example.com"
    assert p.reply_to == "Alice Work <alice.work@example.org>" and p.sender == "list-bot@example.org"
    assert p.in_reply_to == "<b1@example.com>"
    assert p.refs == ["<b0@example.com>", "<b1@example.com>"], "Exchange's comma-separated References read like the RFC form"
    assert p.thread_index == "AQHZ1234567890abcdef" and p.exchange_parent_id == "<orig@example.org>"


def test_a_folded_authentication_results_header_is_one_line():
    raw = _mail("From: a@example.org\nSubject: x\nMessage-ID: <f@example.org>\n"
                "Authentication-Results: mx.example.net;\n\tdkim=pass header.d=example.org;\n\tspf=pass smtp.mailfrom=example.org")
    p = parse_message(raw)
    assert p.auth_results == ["mx.example.net; dkim=pass header.d=example.org; spf=pass smtp.mailfrom=example.org"]


def test_the_microsoft_id_less_header_is_kept_verbatim():
    raw = _mail("From: a@fabrikam.com\nSubject: x\nMessage-ID: <m@fabrikam.com>\n"
                "Authentication-Results: spf=pass (sender IP is 10.2.3.4) smtp.mailfrom=fabrikam.com; contoso.com; "
                "dkim=none (message not signed) header.d=none; contoso.com; dmarc=bestguesspass action=none header.from=fabrikam.com; compauth=pass reason=109")
    p = parse_message(raw)
    assert p.auth_results[0].startswith("spf=pass (sender IP is 10.2.3.4)") and "compauth=pass reason=109" in p.auth_results[0]


def test_machine_mail_headers_are_extracted_lowercased():
    raw = _mail("From: noreply@example.org\nSubject: Out of office\nMessage-ID: <o@example.org>\n"
                "Auto-Submitted: Auto-Replied\nPrecedence: Bulk\nX-Auto-Response-Suppress: OOF, AutoReply\n"
                "X-Autoreply: yes\nX-Autorespond: yes\n"
                "List-Id: Chatter <chatter.example.org>\nList-Unsubscribe: <mailto:leave@example.org>\nList-Post: <mailto:chatter@example.org>\n"
                "Feedback-ID: 12345:campaign:example")
    p = parse_message(raw)
    assert p.auto_submitted == "auto-replied" and p.precedence == "bulk"
    assert p.x_auto_response_suppress == "oof, autoreply"
    assert p.auto_reply_headers == ["x-autoreply", "x-autorespond"]
    assert p.list_id == "Chatter <chatter.example.org>"
    assert p.list_headers == ["list-id", "list-post", "list-unsubscribe"]
    assert p.feedback_id == "12345:campaign:example"


_DSN = b"""From: MAILER-DAEMON@example.net (Mail Delivery System)
To: bob@example.com
Subject: Undelivered Mail Returned to Sender
Return-Path: <>
Content-Type: multipart/report; report-type=delivery-status; boundary="B"
Message-ID: <dsn1@example.net>

--B
Content-Type: text/plain

This is the mail system at host example.net. Delivery failed.
--B
Content-Type: message/delivery-status

Reporting-MTA: dns; example.net

Final-Recipient: rfc822; carol@example.org
Action: failed
Status: 5.1.1

--B
Content-Type: message/rfc822

From: bob@example.com
To: carol@example.org
Subject: hi
Message-ID: <orig9@example.com>

hello carol, the original text must not become the bounce's body
--B--
"""


def test_a_bounce_reports_its_action_and_the_original_id_and_keeps_its_own_body():
    p = parse_message(_DSN)
    assert p.report_type == "delivery-status" and p.dsn_action == "failed"
    assert p.original_message_id == "<orig9@example.com>"
    assert p.return_path_null is True and p.return_path == ""
    assert "Delivery failed" in p.body_text and "hello carol" not in p.body_text
    assert p.attachments == [] and not p.has_attachments


_MDN = b"""From: carol@example.org
To: bob@example.com
Subject: Read: hi
Content-Type: multipart/report; report-type=disposition-notification; boundary="M"
Message-ID: <mdn1@example.org>

--M
Content-Type: text/plain

Your message was read.
--M
Content-Type: message/disposition-notification

Reporting-UA: mail.example.org
Original-Message-ID: <orig9@example.com>
Disposition: manual-action/MDN-sent-manually; displayed

--M--
"""


def test_a_read_receipt_names_the_message_it_reports_on():
    p = parse_message(_MDN)
    assert p.report_type == "disposition-notification"
    assert p.original_message_id == "<orig9@example.com>"
    assert "was read" in p.body_text


_CAL = b"""From: dave@example.org
To: bob@example.com
Subject: Invitation: Lunch
Content-Type: multipart/alternative; boundary="C"
Message-ID: <cal1@example.org>

--C
Content-Type: text/plain

Lunch on Friday
--C
Content-Type: text/calendar; method=REQUEST; charset=UTF-8

BEGIN:VCALENDAR
METHOD:REQUEST
BEGIN:VEVENT
SUMMARY:Lunch
END:VEVENT
END:VCALENDAR
--C--
"""


def test_a_calendar_invitation_carries_its_method():
    p = parse_message(_CAL)
    assert p.calendar_method == "REQUEST"
    assert p.body_text.startswith("Lunch on Friday")


def test_the_walk_never_raises_on_a_broken_report_part():
    raw = b"""From: x@example.org
Subject: broken
Content-Type: multipart/report; report-type=delivery-status; boundary="Z"
Message-ID: <z@example.org>

--Z
Content-Type: message/delivery-status

Action failed without a colon
--Z--
"""
    p = parse_message(raw)
    assert p.report_type == "delivery-status" and p.dsn_action == ""
