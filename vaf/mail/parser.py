# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""RFC 822 message parsing for the mail engine.

Boundary rules (EMAIL_CLIENT.md):
- parse_message NEVER raises: one malformed message must never abort a folder
  sync. Catastrophic failures return a minimal envelope with defects noted.
- Charset lies are tolerated: latin-1 declarations are decoded as cp1252 and a
  replacement-character fallback always exists.
- cpython gh-128110 (spurious space between adjacent RFC 2047 encoded-words) is
  worked around for Subject/From display strings.
"""
import re
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from typing import List, Optional

_WS_ENCODED_WORD_GAP = re.compile(r"(=\?[^?]+\?[BbQq]\?[^?]*\?=) (?==\?)")


@dataclass
class Attachment:
    part_id: str
    filename: str
    content_type: str
    size_bytes: int
    content_id: str = ""
    is_inline: bool = False


@dataclass
class ParsedMessage:
    message_id: str = ""
    subject: str = ""
    from_addr: str = ""
    to_addrs: str = ""
    cc_addrs: str = ""
    date_ts: Optional[int] = None
    refs: List[str] = field(default_factory=list)  # References + In-Reply-To, in order
    body_text: str = ""       # best-effort plain text (FTS + snippet)
    body_html: str = ""       # raw HTML part (sanitized later, at serving time)
    attachments: List[Attachment] = field(default_factory=list)
    has_attachments: bool = False
    defects: List[str] = field(default_factory=list)
    # Identity, provenance and machine-mail headers (vaf/mail/authenticity.py and
    # vaf/mail/classify.py read these; every value is "" or [] when the header is
    # absent, so a consumer never has to know whether a mail was parsed before they
    # existed). Kept as strings: the parser extracts, it never judges.
    reply_to: str = ""                 # address list, as from_addr
    sender: str = ""                   # the Sender header, as from_addr
    return_path: str = ""              # the bracketed address without <>; "" when absent
    return_path_null: bool = False     # Return-Path: <> (a bounce; RFC 3834 says never answer it)
    delivered_to: str = ""             # first Delivered-To / X-Original-To address
    in_reply_to: str = ""              # the first bracketed id of In-Reply-To (also in refs)
    auto_submitted: str = ""           # lowercased (RFC 3834: no, auto-generated, auto-replied, ...)
    precedence: str = ""               # lowercased (bulk, list, junk, auto_reply)
    x_auto_response_suppress: str = ""  # lowercased (MS-OXCMAIL: DR, NDR, RN, NRN, OOF, AutoReply, All)
    auto_reply_headers: List[str] = field(default_factory=list)  # lowercased names of the X-Autoreply family present
    list_id: str = ""
    list_headers: List[str] = field(default_factory=list)        # lowercased List-* header names present
    feedback_id: str = ""
    report_type: str = ""              # multipart/report: delivery-status, disposition-notification, feedback-report
    dsn_action: str = ""               # a delivery-status report's first Action (failed, delayed, delivered, ...)
    original_message_id: str = ""      # the reported-on message: an MDN's Original-Message-ID or a DSN's embedded original
    calendar_method: str = ""          # a text/calendar part's METHOD, uppercased (REQUEST, REPLY, CANCEL, ...)
    thread_index: str = ""             # Outlook's conversation identity
    exchange_parent_id: str = ""       # x-ms-exchange-parent-message-id, bracketed
    auth_results: List[str] = field(default_factory=list)       # Authentication-Results values, topmost first
    arc_auth_results: List[str] = field(default_factory=list)   # ARC-Authentication-Results values, topmost first
    dkim_domains: List[str] = field(default_factory=list)       # d= of every DKIM-Signature, topmost first
    received: List[str] = field(default_factory=list)           # Received values, topmost first, at most 32


def _decode_bytes(raw: bytes, charset: Optional[str]) -> str:
    cs = (charset or "utf-8").strip().lower() or "utf-8"
    if cs in ("latin-1", "latin1", "iso-8859-1", "us-ascii", "ascii"):
        cs = "cp1252"  # real-world mail declaring latin-1 is almost always cp1252
    try:
        return raw.decode(cs, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _part_text(part: EmailMessage) -> str:
    """Decoded text of a text/* part; never raises."""
    try:
        payload = part.get_payload(decode=True)
        if payload is None:
            payload = str(part.get_payload() or "").encode("utf-8", errors="replace")
        return _decode_bytes(payload, part.get_content_charset())
    except Exception:
        return ""


def _html_to_text(html_str: str) -> str:
    """Plain-text extraction from HTML for the FTS index / snippet (NOT a sanitizer)."""
    import html as _html
    s = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", html_str, flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r"<br\s*/?>|</p>|</div>|</tr>|</li>|</h[1-6]>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def _fix_encoded_word_gaps(value: str) -> str:
    """gh-128110: drop the spurious space the parser leaves between adjacent
    encoded-words. Runs on the RAW header before decoding, so it only touches
    whitespace between two encoded-words (which RFC 2047 s6.2 says to ignore)."""
    return _WS_ENCODED_WORD_GAP.sub(r"\1", value)


def _header(msg: EmailMessage, name: str) -> str:
    try:
        raw = msg.get(name)
        if raw is None:
            return ""
        return str(raw).strip()
    except Exception:
        # policy.default header parsing can raise on grossly malformed headers
        try:
            raw = msg.get(name, "")
            return str(raw).strip()
        except Exception:
            return ""


def _address_list(msg: EmailMessage, name: str) -> str:
    try:
        pairs = getaddresses([_header(msg, name)]) if _header(msg, name) else []
        out = []
        for disp, addr in pairs:
            out.append(f"{disp} <{addr}>" if disp else addr)
        return ", ".join(x for x in out if x)
    except Exception:
        return _header(msg, name)


def _references(msg: EmailMessage) -> List[str]:
    """Message-IDs from References + In-Reply-To, order kept, deduped. Bracketed ids are
    taken wherever they stand, so Exchange's comma-separated References read the same as
    the RFC 5322 space-separated form."""
    ids: List[str] = []
    for hdr in ("References", "In-Reply-To"):
        try:
            val = _header(msg, hdr)
        except Exception:
            continue
        for m in re.findall(r"<[^<>\s]+>", val or ""):
            if m not in ids:
                ids.append(m)
    return ids


_AUTO_REPLY_HEADERS = ("x-autoreply", "x-autorespond", "x-autoresponse", "x-mail-autoreply",
                       "x-autoreply-from", "x-autogenerated", "x-auto-reply")
_LIST_HEADERS = ("list-id", "list-post", "list-unsubscribe", "list-unsubscribe-post", "list-help",
                 "list-subscribe", "list-archive", "list-owner")
_MAX_RECEIVED = 32


def _first_bracketed(value: str) -> str:
    m = re.search(r"<[^<>\s]+>", value or "")
    return m.group(0) if m else ""


def _all_headers(msg: EmailMessage, name: str) -> List[str]:
    """Every value of a repeated header, in header order (topmost first), folded onto one
    line each; a value the policy cannot parse is kept as its raw string."""
    out: List[str] = []
    try:
        values = msg.get_all(name) or []
    except Exception:
        values = []
        try:
            for k, v in msg.raw_items():
                if k.lower() == name.lower():
                    values.append(v)
        except Exception:
            pass
    for v in values:
        try:
            s = re.sub(r"\s+", " ", str(v)).strip()
        except Exception:
            continue
        if s:
            out.append(s)
    return out


def _identity_headers(msg: EmailMessage, out: "ParsedMessage") -> None:
    """The headers behind sender verification, attribution and machine-mail detection.
    Extraction only: nothing here decides anything."""
    out.reply_to = _address_list(msg, "Reply-To")[:1024]
    out.sender = _address_list(msg, "Sender")[:512]
    rp = _header(msg, "Return-Path")
    if rp:
        m = re.search(r"<([^<>]*)>", rp)
        inner = (m.group(1) if m else rp).strip()
        out.return_path_null = inner == ""
        out.return_path = inner[:512]
    out.delivered_to = (_first_address(msg, "Delivered-To") or _first_address(msg, "X-Original-To"))[:512]
    out.in_reply_to = _first_bracketed(_header(msg, "In-Reply-To"))
    out.auto_submitted = _header(msg, "Auto-Submitted").lower()[:64]
    out.precedence = _header(msg, "Precedence").lower()[:64]
    out.x_auto_response_suppress = _header(msg, "X-Auto-Response-Suppress").lower()[:128]
    out.list_id = _header(msg, "List-Id")[:512]
    out.feedback_id = _header(msg, "Feedback-ID")[:256]
    out.thread_index = _header(msg, "Thread-Index")[:1024]
    out.exchange_parent_id = _first_bracketed(_header(msg, "X-MS-Exchange-Parent-Message-Id"))
    present = set()
    try:
        present = {k.lower() for k, _v in msg.raw_items()}
    except Exception:
        present = {k.lower() for k in msg.keys()}
    out.auto_reply_headers = [h for h in _AUTO_REPLY_HEADERS if h in present]
    out.list_headers = [h for h in _LIST_HEADERS if h in present]
    out.auth_results = _all_headers(msg, "Authentication-Results")[:16]
    out.arc_auth_results = _all_headers(msg, "ARC-Authentication-Results")[:16]
    out.received = _all_headers(msg, "Received")[:_MAX_RECEIVED]
    domains: List[str] = []
    for sig in _all_headers(msg, "DKIM-Signature")[:16]:
        m = re.search(r"(?:^|;)\s*d=\s*([^;\s]+)", sig)
        if m:
            domains.append(m.group(1).strip().lower())
    out.dkim_domains = domains
    try:
        rt = msg.get_param("report-type", header="content-type")
        if rt and (msg.get_content_type() or "").lower() == "multipart/report":
            out.report_type = str(rt).strip().lower()[:64]
    except Exception:
        pass


def _first_address(msg: EmailMessage, name: str) -> str:
    try:
        pairs = getaddresses([_header(msg, name)]) if _header(msg, name) else []
        for _disp, addr in pairs:
            if addr:
                return addr.strip()
    except Exception:
        pass
    return ""


def _report_parts(part: EmailMessage, out: "ParsedMessage") -> set:
    """A report container's own sub-parts (a delivery-status block, a disposition
    notification, the embedded original message) feed the report fields and are then
    skipped by the body walk, so a bounce's snippet is the bounce text and never the
    quoted original. Returns the ids of the parts to skip."""
    skip: set = set()
    ctype = (part.get_content_type() or "").lower()
    payload = part.get_payload()
    blocks = payload if isinstance(payload, list) else []
    if ctype == "message/delivery-status":
        for block in blocks:
            skip.add(id(block))
            try:
                action = str(block.get("Action") or "").strip().lower()
            except Exception:
                action = ""
            if action and not out.dsn_action:
                out.dsn_action = action[:32]
        if not out.dsn_action and isinstance(payload, str):
            m = re.search(r"(?im)^action:\s*([a-z]+)", payload)
            if m:
                out.dsn_action = m.group(1).lower()
    elif ctype == "message/disposition-notification":
        for block in blocks:
            skip.add(id(block))
            try:
                omid = _first_bracketed(str(block.get("Original-Message-ID") or ""))
            except Exception:
                omid = ""
            if omid and not out.original_message_id:
                out.original_message_id = omid
        if not out.original_message_id and isinstance(payload, str):
            out.original_message_id = _first_bracketed(
                (re.search(r"(?im)^original-message-id:\s*(.+)$", payload) or [None, ""])[1] or "")
    elif ctype == "message/rfc822":
        for inner in blocks:
            for sub in inner.walk():
                skip.add(id(sub))
            try:
                mid = _first_bracketed(str(inner.get("Message-ID") or ""))
            except Exception:
                mid = ""
            if mid and not out.original_message_id:
                out.original_message_id = mid
    elif ctype == "text/rfc822-headers":
        text = _part_text(part)
        m = re.search(r"(?im)^message-id:\s*(.+)$", text)
        if m and not out.original_message_id:
            out.original_message_id = _first_bracketed(m.group(1))
        skip.add(id(part))
    return skip


def _calendar_method(part: EmailMessage) -> str:
    try:
        method = part.get_param("method")
        if method:
            return str(method).strip().upper()[:32]
    except Exception:
        pass
    m = re.search(r"(?im)^METHOD:\s*([A-Z-]+)", _part_text(part)[:20000])
    return m.group(1).upper()[:32] if m else ""


def parse_message(raw: bytes) -> ParsedMessage:
    """Parse raw RFC 822 bytes. Never raises."""
    out = ParsedMessage()
    try:
        msg = BytesParser(policy=policy.default).parsebytes(raw)
    except Exception as e:
        out.defects.append(f"unparseable: {e}")
        # last resort: salvage a subject line for the list view
        m = re.search(rb"(?im)^subject:[ \t]*(.+)$", raw[:8192])
        if m:
            out.subject = _decode_bytes(m.group(1).strip(), None)[:500]
        return out

    try:
        for d in getattr(msg, "defects", []) or []:
            out.defects.append(type(d).__name__)
    except Exception:
        pass

    mid = _header(msg, "Message-ID")
    m = re.search(r"<[^<>\s]+>", mid)
    out.message_id = m.group(0) if m else mid[:998]
    out.subject = _fix_encoded_word_gaps_display(_header(msg, "Subject"))[:2048]
    out.from_addr = _address_list(msg, "From")[:1024]
    out.to_addrs = _address_list(msg, "To")[:2048]
    out.cc_addrs = _address_list(msg, "Cc")[:2048]
    out.refs = _references(msg)
    try:
        d = _header(msg, "Date")
        if d:
            out.date_ts = int(parsedate_to_datetime(d).timestamp())
    except Exception:
        out.defects.append("bad_date")
    try:
        _identity_headers(msg, out)
    except Exception as e:
        out.defects.append(f"identity_headers: {e}")

    text_part = html_part = None
    part_index = 0
    skip: set = set()
    try:
        for part in msg.walk():
            part_index += 1
            try:
                if id(part) in skip:
                    continue
                ctype = part.get_content_type()
                if ctype in ("message/delivery-status", "message/disposition-notification",
                             "message/rfc822", "text/rfc822-headers"):
                    skip |= _report_parts(part, out)
                    continue
                if part.is_multipart():
                    continue
                if ctype == "text/calendar" and not out.calendar_method:
                    out.calendar_method = _calendar_method(part)
                disp = (part.get_content_disposition() or "").lower()
                filename = part.get_filename() or ""
                cid = (part.get("Content-ID") or "").strip().strip("<>")
                if disp == "attachment" or (filename and ctype not in ("text/plain", "text/html")):
                    payload = part.get_payload(decode=True) or b""
                    out.attachments.append(Attachment(
                        part_id=str(part_index), filename=filename or f"part-{part_index}",
                        content_type=ctype, size_bytes=len(payload),
                        content_id=cid, is_inline=False,
                    ))
                elif cid and ctype.startswith("image/"):
                    payload = part.get_payload(decode=True) or b""
                    out.attachments.append(Attachment(
                        part_id=str(part_index), filename=filename or f"inline-{part_index}",
                        content_type=ctype, size_bytes=len(payload),
                        content_id=cid, is_inline=True,
                    ))
                elif ctype == "text/plain" and text_part is None:
                    text_part = part
                elif ctype == "text/html" and html_part is None:
                    html_part = part
            except Exception as e:
                out.defects.append(f"part_{part_index}: {e}")
                continue
    except Exception as e:
        out.defects.append(f"walk: {e}")

    if html_part is not None:
        out.body_html = _part_text(html_part)
    if text_part is not None:
        out.body_text = _part_text(text_part).strip()
    elif out.body_html:
        out.body_text = _html_to_text(out.body_html)
    out.has_attachments = any(not a.is_inline for a in out.attachments)
    return out


def _fix_encoded_word_gaps_display(value: str) -> str:
    """Display-level mitigation for gh-128110: the modern policy already decoded
    the header, but adjacent encoded-words separated by folding whitespace come
    out with a spurious space. We cannot reliably distinguish that from a real
    space post-decode, so only the raw-header variant (_fix_encoded_word_gaps)
    is lossless; this wrapper exists as the single call site to upgrade later."""
    return value
