# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""How a decision surface shows text that somebody else wrote: an argument, an address, a
number, a name. The one place that decides what is drawn as it is and what is marked.

A card a person confirms is a security control: what they read is what they agree to. So
nothing here normalises. A character that could draw as nothing, as another letter or as
another argument is shown as a visible mark, ``⟨U+XXXX⟩`` (U+27E8/U+27E9 around the
code point), and those two brackets are themselves escaped wherever they occur in a value, so
every mark on a card is VAF's own and no value can fake one or hide a real one.

THE SURFACES (measured, N = 3, plus two refusals): the confirmation card on the phone
(vaf/core/agent_origin.py `card_lines`, vaf/core/app_link/confirmations.py `send_what`), the
web UI's draft card through the rows of vaf/core/outbound_hold.py (`to_shown`, `cc_shown`,
`bcc_shown`, `name_shown`), and `vaf outbox list`; the mail funnel
(`MailService.queue_send`) refuses an address no mail server can take, and the WhatsApp park
and tool refuse what `phone_number` marks.

HOW A RECIPIENT IS SHOWN. The address or number first and whole, then the name, cut at
``NAME_MAX``: a name is written by whoever wrote the mail's To line (the model, for a draft),
so a long one must never push the address out of sight, and an ``@`` in it is marked, because
a name is never an address (``"boss@firma.de (Boss)" <x@evil.test>`` parses to that name and
the address ``x@evil.test``).

- `mail_address`: the dot-atom characters and ``@`` as they are, every other character
  marked, a space and a quote included; a domain with a character outside ASCII gets its
  per-label punycode in square brackets beside it (``alice@ex⟨U+0430⟩mple.com
  [xn--exmple-4nf.com]``). The raw ``punycode`` codec per label, never the ``idna`` codec:
  measured, ``"ｅxample".encode("idna") == b"example"``, because nameprep folds a
  full-width letter to ASCII, which is exactly the silent normalisation a look-alike needs.
  For every domain strict IDNA2008 accepts, this bracket equals the A-labels the mail is
  delivered to (`vaf.mail.addressing.envelope_address`), so the card names the domain the mail
  goes to; a domain IDNA2008 refuses still gets its bracket here, and the funnel refuses it.
- `phone_number`: what the WhatsApp lane dials, marked. Measured on the conversion the lane
  used before `contacts_store.whatsapp_send_jid`: it kept every character whose ``isdigit()``
  is true, so an Arabic-Indic seven, full-width digits or a superscript two landed in the JID,
  and ``+491761234567 ext 99`` dialled a different number than the one read. Only a leading
  ``+`` and the ASCII digits are kept, the separators the lane drops are dropped, and
  everything else is marked where it stands. A number the lane reads in another form (a
  trunk zero read as Germany, a ``00`` prefix) gets the dialled form in square brackets
  beside it (``01761234567 [+491761234567]``), the same pattern as the punycode bracket on an
  address. `phone_digits` is the matching refusal test.

ON A TERMINAL the marks are written as ``<U+XXXX>`` (`ascii_marks`): measured, with
``PYTHONIOENCODING=cp1252`` both ``print("⟨")`` and Rich raise ``UnicodeEncodeError``, so a
Windows console must never be handed the brackets.

NAMED BOUNDARIES.
- Not on the facade: the facade exports neither the outbound hold nor the cards
  (docs/EMBEDDING.md names `confirmations.py` a first-party boundary), and all three
  surfaces are VAF's own, fed from VAF's own rows. It is exported with the outbound hold.
- ASCII look-alikes (``paypa1.com``, ``rn`` for ``m``, ``I`` for ``l``) are not marked: the
  tree holds no skeleton table, and every character of them is plain ASCII the person can
  read.
- A name keeps ``(``, ``)``, ``,``, ``<`` and ``>`` and their full-width forms: every entry of
  a recipient line starts with an address, every address carries an ``@``, and every ``@``
  and look-alike in a name is marked, so no name can add an entry that reads as an address
  (``x@evil.test (Anna), boss⟨U+FF20⟩firma.de (Boss)``). Marking them would mark ordinary
  names such as ``Anna (Marketing)`` or a Japanese name with its department in full-width
  brackets. A WhatsApp name comes from the person's own contact book, not from the call.
- Bidi: an Arabic or Hebrew name next to an address that starts with digits can, by the
  Unicode bidi algorithm, draw those digits beside the name. The characters stay visible; the
  phone app isolates the whole card text and the web UI wraps names in ``<bdi>``. The card
  text cannot carry isolation controls itself, because they are exactly what gets marked.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, List, Optional, Sequence, Tuple

OPEN, CLOSE = "\u27e8", "\u27e9"
#: The longest a name is shown on a decision surface, in code points with the ellipsis
#: (escapes count as the text they are shown as).
NAME_MAX = 40
ELLIPSIS = "\u2026"
# Characters a renderer draws as nothing although their category is a letter or a symbol:
# the Hangul fillers and the Braille blank.
_FILLERS = frozenset((0x115F, 0x1160, 0x3164, 0xFFA0, 0x2800))
# Combining marks stacked on one base character beyond this are shown escaped.
_MARKS_ON_ONE_BASE = 2
# Combining marks that are default-ignorable: a renderer draws them as nothing at all, so
# even on a base they hide what the value holds ("a" U+17B4 "b" reads as "ab"). Always shown
# as a mark, except the two presentation selectors below in free text.
_IGNORABLE_MARKS = ((0x034F, 0x034F), (0x17B4, 0x17B5), (0x180B, 0x180F), (0xFE00, 0xFE0F),
                    (0xE0100, 0xE01EF))
# Text and emoji presentation (U+FE0E, U+FE0F): the second follows almost every emoji heart
# or sign ("\u2764\ufe0f"), so in free text it is kept when it directly follows a symbol
# (category So), the base it selects the look of. Anywhere else it is marked, and a value shown
# whole (a target, an address) never keeps it.
_PRESENTATION_SELECTORS = ("\ufe0e", "\ufe0f")
# Characters a name never shows as they are, because they make it read as an address: the
# "@" and every character whose NFKC form is "@" (measured over all code points: U+FF20 and
# U+FE6B). Pinned against a full NFKC scan in tests/test_shown_text.py.
AT_SIGNS = "@\uff20\ufe6b"
# The characters of an addr-spec shown as they are: the dot-atom set and the "@".
_ADDRESS_KEEP = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    "!#$%&'*+-/=?^_`{|}~.@")
# What the WhatsApp lane drops from a number without changing what it dials.
_PHONE_SEPARATORS = frozenset(" -./()")
_ASCII_DIGITS = frozenset("0123456789")
_MARK_RE = re.compile("(" + OPEN + r"U\+[0-9A-F]{4,6}" + CLOSE + ")")


class Shown(str):
    """Text already shown safely by this module. A card lays it out as it is instead of
    escaping it again: a second pass would escape the marks' own brackets."""


def escaped(char: str) -> str:
    """One character as its visible mark: ``⟨U+0430⟩``, five or six hex digits above
    U+FFFF."""
    return f"{OPEN}U+{ord(char):04X}{CLOSE}"


def shown_char(char: str) -> str:
    """One character as a card shows it: a control, format, separator, private-use or
    unassigned character (a direction override, a zero-width space, a carriage return), a
    filler and the two mark brackets themselves as a visible ``⟨U+XXXX⟩``; a tab
    kept."""
    if char == "\t":
        return char
    if unicodedata.category(char) in ("Cc", "Cf", "Zl", "Zp", "Co", "Cn", "Cs") \
            or ord(char) in _FILLERS or char in (OPEN, CLOSE):
        return escaped(char)
    return char


def _ignorable_mark(char: str) -> bool:
    code = ord(char)
    return any(low <= code <= high for low, high in _IGNORABLE_MARKS)


def _no_base(char: str) -> bool:
    """A character that cannot carry a combining mark on a card: a tab or any space (category
    Zs, the no-break and the wide ones included), because a space plus U+0338 draws as a
    slash."""
    return char == "\t" or unicodedata.category(char) == "Zs"


def shown_tokens(line: str, also: str = "", *, whole: bool = False) -> List[str]:
    """One line as a card shows it, one token per character (an escape is one token, so a
    cut never splits it). Every character as `shown_char` shows it, the characters in
    ``also`` escaped too, and a combining mark escaped when it has no base before it (the
    line's start, a space of any kind, a tab or an escaped character, an escaped mark
    included), when more than
    ``_MARKS_ON_ONE_BASE`` are stacked on one base, or when it is default-ignorable
    (`_IGNORABLE_MARKS`; an emoji presentation selector right after a symbol is kept unless
    the value is shown ``whole``)."""
    shown: List[str] = []
    marks, base, symbol = 0, False, False
    for char in line:
        if unicodedata.category(char) in ("Mn", "Me"):
            marks += 1
            if _ignorable_mark(char):
                kept = (not whole and char in _PRESENTATION_SELECTORS and symbol
                        and marks == 1)
            else:
                kept = base and marks <= _MARKS_ON_ONE_BASE
            shown.append(char if kept else escaped(char))
            if not kept:
                # An escaped mark is a mark's closing bracket on screen: a mark after it would
                # draw onto that bracket, so it has no base either.
                base = symbol = False
            continue
        token = escaped(char) if char in also else shown_char(char)
        marks, base = 0, token == char and not _no_base(char)
        symbol = base and unicodedata.category(char) == "So"
        shown.append(token)
    return shown


def tokens_of(text: str) -> List[str]:
    """`Shown` text back as its tokens: every mark one token, every other character one."""
    out: List[str] = []
    for part in _MARK_RE.split(text):
        if _MARK_RE.fullmatch(part):
            out.append(part)
        else:
            out.extend(part)
    return out


def _punycode_domain(domain: str) -> Optional[str]:
    """The domain with every label outside ASCII as ``xn--`` plus its raw punycode, the ASCII
    labels as they are; None when the codec refuses a label."""
    labels = []
    for label in domain.split("."):
        if label.isascii():
            labels.append(label)
            continue
        try:
            labels.append("xn--" + label.encode("punycode").decode("ascii"))
        except Exception:
            return None
    return ".".join(labels)


def mail_address(addr: Any) -> Shown:
    """One addr-spec as a decision surface shows it: the dot-atom characters and ``@`` as
    they are, every other character marked; when the part after the last ``@`` holds a
    character outside ASCII, its punycode follows in square brackets. A plain ASCII address
    is unchanged."""
    text = str(addr or "")
    shown = "".join(c if c in _ADDRESS_KEEP else escaped(c) for c in text)
    domain = text.rsplit("@", 1)[-1] if "@" in text else ""
    if domain and not domain.isascii():
        ascii_domain = _punycode_domain(domain)
        if ascii_domain is not None:
            shown += f" [{ascii_domain}]"
    return Shown(shown)


def _phone_body(raw: Any) -> str:
    return str(raw or "").strip(" \t\r\n\f\v")


def phone_number(raw: Any) -> Shown:
    """What the WhatsApp lane dials, marked: one leading ``+`` and the ASCII digits kept, the
    separators ``space - . / ( )`` dropped as the lane drops them, every other character
    marked where it stands. When the lane reads the number in another form
    (`contacts_store.phone_digits_canonical`: a trunk zero as Germany, a ``00`` prefix
    dropped), that form follows in square brackets, so the card names the number dialled."""
    text = _phone_body(raw)
    plain = phone_digits(text)
    out: List[str] = []
    if text.startswith("+"):
        out.append("+")
        text = text[1:]
    for char in text:
        if char in _ASCII_DIGITS:
            out.append(char)
        elif char not in _PHONE_SEPARATORS:
            out.append(escaped(char))
    if plain is not None:
        from vaf.core.contacts_store import phone_digits_canonical
        dialled = phone_digits_canonical(plain)
        if dialled and dialled != plain.lstrip("+"):
            out.append(f" [+{dialled}]")
    return Shown("".join(out))


def phone_digits(raw: Any) -> Optional[str]:
    """The number as ``+`` (when given) and its ASCII digits, or None when it holds anything
    but an optional leading ``+``, ASCII digits and the separators ``space - . / ( )``, or no
    digit at all: a letter, a digit of another script, a no-break space, a zero-width or
    direction character and a ``+`` inside the number are all refused. The length rule stays
    with the lane (`contacts_store.whatsapp_send_jid`)."""
    text = _phone_body(raw)
    plus = text.startswith("+")
    body = text[1:] if plus else text
    digits = []
    for char in body:
        if char in _ASCII_DIGITS:
            digits.append(char)
        elif char not in _PHONE_SEPARATORS:
            return None
    if not digits:
        return None
    return ("+" if plus else "") + "".join(digits)


def person_name(name: Any, limit: int = NAME_MAX) -> Shown:
    """A name as a decision surface shows it: every character as `shown_tokens` shows it,
    an ``@`` and its look-alikes (`AT_SIGNS`) marked too (a name is never an address, and
    every address on a card carries one), cut so that the shown text with its
    ``…`` is at most ``limit`` code points, never inside a mark. An empty name is ""."""
    tokens = shown_tokens(str(name or "").strip(), also=AT_SIGNS)
    if sum(len(t) for t in tokens) <= limit:
        return Shown("".join(tokens))
    head: List[str] = []
    size = 0
    for token in tokens:
        if size + len(token) > limit - len(ELLIPSIS):
            break
        head.append(token)
        size += len(token)
    return Shown("".join(head) + ELLIPSIS)


def mail_recipients(raw: Any, first_name: Any = "") -> List[Tuple[Shown, Shown]]:
    """``(address, name)`` per recipient of a mail header, both as shown, in the order of
    `vaf.mail.addressing.recipient_pairs`, the same parse the envelope is built from. The
    first entry's name is ``first_name`` when given (the contact book's), else its own
    display name; a name equal to its address is dropped. A header the parse finds nothing
    in comes back whole and marked as one address, so a value the lane would drop is still
    shown rather than hidden."""
    text = str(raw or "").strip()
    if not text:
        return []
    from vaf.mail.addressing import recipient_pairs
    pairs = recipient_pairs(text)
    if not pairs:
        return [(mail_address(text), Shown(""))]
    out: List[Tuple[Shown, Shown]] = []
    for index, (name, addr) in enumerate(pairs):
        if index == 0 and str(first_name or "").strip():
            name = str(first_name)
        name = str(name or "").strip()
        out.append((mail_address(addr), person_name(name if name != addr else "")))
    return out


def recipients_line(pairs: Sequence[Tuple[str, str]]) -> Shown:
    """``addr (name), addr2, addr3 (name3)``: every address first, each name after its own."""
    return Shown(", ".join(f"{addr} ({name})" if name else str(addr) for addr, name in pairs))


def ascii_marks(text: Any) -> str:
    """Shown text for a terminal that may not encode the marks (cp1252): the brackets become
    ``<`` and ``>``, every other character outside ASCII ``<U+XXXX>``, and a literal ``<`` or
    ``>`` ``<U+003C>`` / ``<U+003E>``, so a mark stays told apart from text. Always ASCII."""
    out: List[str] = []
    for part in _MARK_RE.split(str(text or "")):
        if _MARK_RE.fullmatch(part):
            out.append("<" + part[1:-1] + ">")
            continue
        for char in part:
            if char in "<>" or not char.isascii():
                out.append(f"<U+{ord(char):04X}>")
            else:
                out.append(char)
    return "".join(out)
