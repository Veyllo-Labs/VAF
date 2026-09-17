# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Structural recognition of machine mail at ingest (EMAIL_CLIENT.md, the verification
and attribution lane).

Before anything decides to ANSWER an inbound mail, this module says whether a machine
wrote it, from the headers and the MIME structure the parser already extracted
(vaf/mail/parser.py, ParsedMessage) and never from the body text. It is pure: no IO, no
Config, and classify_machine never raises on any ParsedMessage (a field left None by a
hand-built message reads as absent).

The lexical half lives in vaf/core/inbox.py (is_automated_sender: no-reply and
notification addresses, newsletters, non-primary Gmail categories) and is REUSED here as
the very last rule: it can add a verdict, it can never remove a header verdict.

Precedence, first hit wins:

1. bounce            multipart/report with report-type delivery-status (RFC 3464), or a
                     null reverse-path together with a From whose local part is
                     mailer-daemon or postmaster (RFC 5321 s4.5.1 role names).
2. mdn               report-type disposition-notification (RFC 8098).
3. calendar          a text/calendar part with a METHOD (RFC 5546: REQUEST, REPLY,
                     CANCEL, ...). The person behind it is the ORGANIZER inside the
                     calendar object, not the From address.
4. auto_reply        Auto-Submitted other than "no" (RFC 3834 s5), X-Auto-Response-Suppress
                     carrying All, OOF or AutoReply (MS-OXCMAIL s2.2.1.1), any header of the
                     X-Autoreply family, or Precedence: auto_reply.
5. list              List-Id (RFC 2919), List-Post (RFC 2369) or Precedence: list.
6. own_loop          the Message-ID is one this account sent, or the From address is one
                     of the account's own: own mail re-entering the mailbox is never
                     answered.
7. bulk              Precedence: bulk or junk, a Feedback-ID (feedback-loop mail),
                     List-Unsubscribe without a List-Id (RFC 2369, RFC 8058: marketing
                     mail carries it without being a list), any other multipart/report
                     type (RFC 6522 reports are machine-made by definition, e.g. an
                     RFC 5965 feedback-report), a bulk category from the mail store, or
                     the lexical half (is_automated_sender).
8. null_return_path  Return-Path: <> and nothing above matched (RFC 3834 s2: never
                     respond to a null reverse-path).
9. ""                a person may have written it.

The verdict says only THAT a machine wrote the mail and which header decided it. The
lane that answers mail reads it; the parser's other fields (dsn_action,
original_message_id, calendar_method) say what to do with a bounce, an MDN or an
invitation.
"""
from __future__ import annotations

from dataclasses import dataclass
from email.utils import getaddresses
from typing import Callable, Iterable, List, Optional, Set, Union

from vaf.core.inbox import _BULK_CATEGORIES, is_automated_sender
from vaf.mail.parser import ParsedMessage

MACHINE_KINDS = ("bounce", "mdn", "auto_reply", "list", "bulk", "calendar", "own_loop", "null_return_path")

# RFC 5321 s4.5.1: the two mailbox names a mail system answers from; with a null
# reverse-path, a mail from one of them is a bounce even without a multipart/report body.
_BOUNCE_LOCALS = frozenset(("mailer-daemon", "postmaster"))
# MS-OXCMAIL s2.2.1.1: the X-Auto-Response-Suppress values that mark the MAIL as automatic.
# DR, NDR, RN and NRN only suppress receipts and say nothing about who wrote it.
_SUPPRESS_TOKENS = frozenset(("all", "oof", "autoreply"))


@dataclass(frozen=True)
class MachineVerdict:
    kind: str    # "" (a person may have written it) or one of MACHINE_KINDS
    reason: str  # the deciding header or rule, e.g. "report_type=delivery-status"


def _s(value: object) -> str:
    """A header field as the parser stores it; None (a hand-built message) reads as absent."""
    try:
        return str(value or "").strip()
    except Exception:
        return ""


def _names(values: object) -> List[str]:
    """Lowercased header names from a list field, order kept; None reads as empty."""
    try:
        return [str(v or "").strip().lower() for v in (values or ()) if str(v or "").strip()]
    except Exception:
        return []


def _addr_specs(header: str) -> List[str]:
    """Every addr-spec of an address-list header, lowercased. getaddresses rather than
    parseaddr so a From carrying two mailboxes (RFC 5322 allows it) still yields both."""
    try:
        pairs = getaddresses([header]) if header else []
    except Exception:
        return []
    return [addr.strip().lower() for _disp, addr in pairs if addr and addr.strip()]


def _own_set(own_addresses: Iterable[str]) -> Set[str]:
    """The account's own addr-specs, lowercased; an entry may be a bare address or the
    "Name <addr>" form the account settings display."""
    out: Set[str] = set()
    try:
        for entry in own_addresses or ():
            out.update(_addr_specs(_s(entry)))
    except Exception:
        return out
    return out


def classify_machine(
    parsed: ParsedMessage,
    *,
    own_addresses: Iterable[str] = (),
    is_own_message_id: Optional[Callable[[str], bool]] = None,
    category: str = "",
) -> MachineVerdict:
    """Whether a machine wrote `parsed`, by the precedence in the module docstring.

    `own_addresses` are the account's addresses (any case, bare or "Name <addr>");
    `is_own_message_id` answers whether a Message-ID is one this account sent and is
    consulted only when given and only for a non-empty id; `category` is the folder or
    tab the sync filed the mail under (Gmail's promotions, a Junk label, ...).
    """
    report_type = _s(parsed.report_type).lower()
    from_header = _s(parsed.from_addr)
    from_specs = _addr_specs(from_header)
    return_path_null = bool(parsed.return_path_null)

    # 1. bounce (RFC 3464)
    if report_type == "delivery-status":
        return MachineVerdict("bounce", "report_type=delivery-status")
    if return_path_null:
        for spec in from_specs:
            local = spec.split("@", 1)[0]
            if local in _BOUNCE_LOCALS:
                return MachineVerdict("bounce", f"return_path=<> from={local}")
    # Exim's X-Failed-Recipients is not available here: the parser does not keep it. A
    # bounce carrying only that header still has a null reverse-path and lands in rule 8,
    # which is never answered either; only the kind differs.

    # 2. mdn (RFC 8098)
    if report_type == "disposition-notification":
        return MachineVerdict("mdn", "report_type=disposition-notification")

    # 3. calendar (RFC 5546)
    calendar_method = _s(parsed.calendar_method).upper()
    if calendar_method:
        return MachineVerdict("calendar", f"calendar_method={calendar_method}")

    # 4. auto_reply (RFC 3834, MS-OXCMAIL)
    # RFC 3834 s5 allows parameters after the keyword ("auto-replied; owner-info=..."),
    # so only the token before the first ";" is the verdict.
    auto_submitted = _s(parsed.auto_submitted).lower().split(";", 1)[0].strip()
    if auto_submitted and auto_submitted != "no":
        return MachineVerdict("auto_reply", f"auto_submitted={auto_submitted}")
    for token in _s(parsed.x_auto_response_suppress).lower().split(","):
        token = token.strip()
        if token in _SUPPRESS_TOKENS:
            return MachineVerdict("auto_reply", f"x_auto_response_suppress={token}")
    auto_reply_headers = _names(parsed.auto_reply_headers)
    if auto_reply_headers:
        return MachineVerdict("auto_reply", f"auto_reply_headers={auto_reply_headers[0]}")
    # Sendmail's vacation writes "auto_reply", some gateways "auto-reply": one spelling.
    precedence = _s(parsed.precedence).lower().replace("-", "_")
    if precedence == "auto_reply":
        return MachineVerdict("auto_reply", "precedence=auto_reply")

    # 5. list (RFC 2919, RFC 2369)
    list_headers = _names(parsed.list_headers)
    if _s(parsed.list_id):
        return MachineVerdict("list", "list_id")
    if "list-post" in list_headers:
        return MachineVerdict("list", "list_headers=list-post")
    if precedence == "list":
        return MachineVerdict("list", "precedence=list")

    # 6. own_loop
    message_id = _s(parsed.message_id)
    if is_own_message_id is not None and message_id and is_own_message_id(message_id):
        return MachineVerdict("own_loop", "message_id=own")
    own = _own_set(own_addresses)
    if own and any(spec in own for spec in from_specs):
        return MachineVerdict("own_loop", "from=own address")

    # 7. bulk
    if precedence in ("bulk", "junk"):
        return MachineVerdict("bulk", f"precedence={precedence}")
    if _s(parsed.feedback_id):
        return MachineVerdict("bulk", "feedback_id")
    if "list-unsubscribe" in list_headers:
        return MachineVerdict("bulk", "list_headers=list-unsubscribe")
    if report_type:
        return MachineVerdict("bulk", f"report_type={report_type}")
    cat = _s(category).lower()
    if cat in _BULK_CATEGORIES:
        return MachineVerdict("bulk", f"category={cat}")
    # The lexical half, last: it may add a verdict, never remove one from above.
    if is_automated_sender(from_header, cat):
        return MachineVerdict("bulk", "automated_sender")

    # 8. null_return_path (RFC 3834 s2)
    if return_path_null:
        return MachineVerdict("null_return_path", "return_path=<>")

    # 9. a person may have written it
    return MachineVerdict("", "")


def is_machine(verdict_or_kind: Union[MachineVerdict, str, None]) -> bool:
    """True when a verdict (or a bare kind string) says a machine wrote the mail."""
    kind = verdict_or_kind.kind if isinstance(verdict_or_kind, MachineVerdict) else verdict_or_kind
    return bool(_s(kind))
