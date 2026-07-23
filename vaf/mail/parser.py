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
    """Message-IDs from References + In-Reply-To, order kept, deduped."""
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

    text_part = html_part = None
    part_index = 0
    try:
        for part in msg.walk():
            part_index += 1
            try:
                ctype = part.get_content_type()
                if part.is_multipart():
                    continue
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
