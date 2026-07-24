# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Compose helpers: reply/forward subjects, quoting, and RFC 822 building.

Conventions (EMAIL_CLIENT.md / RFC 3676): plain-text bodies are sent as
format=flowed with delsp=yes; replies quote with "> " depth markers under an
"On <date>, <name> wrote:" attribution; In-Reply-To/References emission keeps
threading alive on the recipient side (JWZ input). Subject prefixes are
normalized so "Re:" never stacks (German AW:/WG: variants recognized)."""
import re
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from typing import Dict, Iterable, List, Optional

_RE_PREFIX = re.compile(r"^\s*((re|aw|antw|sv)(\[\d+\])?:\s*)+", re.IGNORECASE)
_FWD_PREFIX = re.compile(r"^\s*((fw|fwd|wg)(\[\d+\])?:\s*)+", re.IGNORECASE)


def reply_subject(subject: str) -> str:
    base = _RE_PREFIX.sub("", (subject or "").strip())
    return f"Re: {base}" if base else "Re:"


def forward_subject(subject: str) -> str:
    base = _FWD_PREFIX.sub("", (subject or "").strip())
    return f"Fwd: {base}" if base else "Fwd:"


def _display_name(from_addr: str) -> str:
    name, addr = parseaddr(from_addr or "")
    return name or addr or "unknown sender"


def quote_reply(orig_from: str, orig_date_ts: Optional[int], orig_text: str) -> str:
    """Quoted block for a reply body: attribution line + '> ' prefixed lines.
    Existing quote markers deepen naturally ('> ' + '> ' = '> > ')."""
    when = ""
    if orig_date_ts:
        when = datetime.fromtimestamp(int(orig_date_ts), tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC")
    attribution = f"On {when}, {_display_name(orig_from)} wrote:" if when else \
        f"{_display_name(orig_from)} wrote:"
    # Empty source lines become a bare '>' (NOT '> ') so the blank quoted line
    # stays a hard break and paragraphs are not flowed together; content lines
    # are rstripped for the same reason (see _flow_encode).
    quoted_lines = [f"> {s}" if (s := line.rstrip()) else ">"
                    for line in (orig_text or "").splitlines()]
    quoted = "\n".join(quoted_lines)
    return f"{attribution}\n{quoted}"


def forward_block(orig_from: str, orig_to: str, orig_date_ts: Optional[int],
                  orig_subject: str, orig_text: str) -> str:
    when = ""
    if orig_date_ts:
        when = datetime.fromtimestamp(int(orig_date_ts), tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC")
    header = ("---------- Forwarded message ----------\n"
              f"From: {orig_from}\nDate: {when}\nSubject: {orig_subject}\nTo: {orig_to}\n")
    return f"{header}\n{orig_text or ''}"


def reply_recipients(orig_from: str, orig_to: str, orig_cc: str, reply_to: Optional[str],
                     own_addresses: Iterable[str], reply_all: bool) -> Dict[str, str]:
    """Reply targets: Reply-To (or From); reply-all adds To+Cc minus own
    addresses, deduplicated with order kept."""
    own = {a.strip().lower() for a in own_addresses if a and a.strip()}
    primary = (reply_to or "").strip() or (orig_from or "").strip()

    def _addrs(value: str) -> List[str]:
        out = []
        for part in (value or "").split(","):
            _, addr = parseaddr(part)
            if addr and addr.lower() not in own:
                out.append(part.strip())
        return out

    to = [primary] if primary else []
    cc: List[str] = []
    if reply_all:
        seen = {parseaddr(primary)[1].lower()} if primary else set()
        for part in _addrs(orig_to) + _addrs(orig_cc):
            addr = parseaddr(part)[1].lower()
            if addr and addr not in seen:
                seen.add(addr)
                cc.append(part)
    return {"to": ", ".join(to), "cc": ", ".join(cc)}


def reply_reference_headers(orig_message_id: str, orig_refs: Iterable[str]) -> Dict[str, str]:
    """In-Reply-To = parent Message-ID; References = parent chain + parent id
    (RFC 5322 s3.6.4) - this is what keeps threading alive downstream."""
    mid = (orig_message_id or "").strip()
    refs = [r for r in (orig_refs or []) if r and r != mid]
    if mid:
        refs.append(mid)
    return {"in_reply_to": mid, "references": " ".join(refs[-20:])}


def _flow_encode(text: str) -> str:
    """format=flowed body encoding (RFC 3676). Two rules matter here:

    - A trailing space is the *flowed* (soft line-break) signal, so we rstrip
      every line first: we never soft-wrap, so each line is a HARD line and a
      stray trailing space would otherwise make the recipient glue it to the
      next line (words run together).
    - Space-stuffing re-adds ONE leading space to a line that would otherwise be
      misread as a quote or the 'From ' envelope boundary. A line that is only
      quote markers ('>', '> >', an empty quoted paragraph) is structural and is
      emitted verbatim - stuffing it would demote the quote to plain text."""
    out = []
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if line and line.replace(" ", "").strip(">") == "":
            out.append(line)  # pure quote-marker line (empty quoted paragraph)
        elif line.startswith((" ", "From ")) or (line.startswith(">") and not line.startswith("> ")):
            out.append(" " + line)
        else:
            out.append(line)
    return "\r\n".join(out)


def build_message(from_addr: str, to: str, subject: str, body_text: str,
                  cc: Optional[str] = None, bcc: Optional[str] = None,
                  in_reply_to: Optional[str] = None, references: Optional[str] = None,
                  attachments: Optional[List[Dict[str, bytes]]] = None) -> EmailMessage:
    """RFC 822 message with format=flowed plain text. attachments: list of
    {filename, content_type, payload}. Bcc handling is the TRANSPORT's job
    (envelope vs header semantics differ per provider - v1 rules apply)."""
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    if bcc:
        # The Sent copy records the Bcc so the sender keeps the blind-copy list;
        # the delivered message (built by the transport) never carries it.
        msg["Bcc"] = bcc
    msg["Subject"] = subject or "(No subject)"
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid()
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(_flow_encode(body_text or ""), subtype="plain",
                    cte="quoted-printable",
                    params={"format": "flowed", "delsp": "yes"})
    for att in attachments or []:
        maintype, _, subtype = (att.get("content_type") or "application/octet-stream").partition("/")
        msg.add_attachment(att.get("payload") or b"", maintype=maintype or "application",
                           subtype=subtype or "octet-stream",
                           filename=att.get("filename") or "attachment")
    return msg
