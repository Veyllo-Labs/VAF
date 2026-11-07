# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Address parsing for the mail subsystem: the one place VAF parses an address header.

`getaddresses` and `parseaddr` behave as the patched standard library does on every
supported Python. The stdlib's strict parsing (the CVE-2023-27043 fix) arrived in
3.10.15, 3.11.10 and 3.12.6; before it, "me@example.com <stranger@example.org>" parsed
as the address in the DISPLAY NAME, so the sender a check saw was the one the attacker
wrote there. python.org ships no Windows or macOS installer after 3.10.11 and 3.11.9,
so those unpatched versions are what a supported install really runs (the CI's own
setup-python lands there). A patched stdlib is used as is; on an older one the four
documented checks of the fix are applied here to the stdlib's own legacy parse.
tests/test_mail_addressing_strict.py compares the two on the running Python, and
refuses a direct email.utils parse anywhere else in vaf/.

Also the route-independent home for normalize_recipients so the native sender does
not have to import the heavy vaf.core.email_transport module for a pure helper. The
historical name email_transport.normalize_recipients is re-exported from here (a
guard test pins them to one object, Rule 2 single-source).

And the one place an address gets its ENVELOPE form (`envelope_address`): the form the SMTP
conversation and the ASCII headers carry. A domain outside ASCII becomes its A-labels under
strict IDNA2008 (RFC 5891-5893, the ``idna`` package's `alabel`); a local part outside ASCII
stays as written and needs the server's SMTPUTF8 (RFC 6531, `needs_smtputf8`). Measured on
the alternatives: the standard library's ``idna`` codec (IDNA 2003 with nameprep) turns
``faß.de`` into ``fass.de`` and a full-width ``ｅxample.com`` into
``example.com``, and UTS46 mapping still folds the full-width letter, ligatures and Roman
numerals; each of those is a DIFFERENT, separately registrable domain, so a mapping codec
delivers to a stranger. Strict IDNA2008 refuses every one of them, and for every label it
accepts the A-label equals ``xn--`` plus the label's raw punycode, which is the bracket the
cards show beside such an address (`vaf.core.shown_text.mail_address`)."""
import email.utils as _stdlib
import unicodedata
from typing import Any, Dict, List, Set, Tuple

# Set by the fix itself (email/utils.py of a patched release).
_STDLIB_STRICT = bool(getattr(_stdlib, "supports_strict_parsing", False))


def _legacy_getaddresses(fieldvalues) -> List[Tuple[str, str]]:
    """The pre-fix parse: exactly what an unpatched stdlib's getaddresses returns."""
    if _STDLIB_STRICT:
        return _stdlib.getaddresses(fieldvalues, strict=False)
    return _stdlib.getaddresses(fieldvalues)


def _legacy_parseaddr(addr) -> Tuple[str, str]:
    """The pre-fix parseaddr."""
    if _STDLIB_STRICT:
        return _stdlib.parseaddr(addr, strict=False)
    return _stdlib.parseaddr(addr)


def _strip_quoted(value: str) -> str:
    """value without its double-quoted strings, so a comma or parenthesis inside a
    quoted display name ("Doe, John") does not count. A backslash escapes the next
    character; an unclosed quote keeps the rest of the value."""
    kept, start, open_at, escaped = [], 0, None, False
    for pos, ch in enumerate(value):
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == '"':
            if open_at is None:
                open_at = pos
            else:
                kept.append(value[start:open_at])
                start, open_at = pos + 1, None
    kept.append(value[start:])
    return "".join(kept)


def _parens_balanced(value: str) -> bool:
    depth, escaped = 0, False
    for ch in _strip_quoted(value):
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _strict_parse(values: List[str]) -> List[Tuple[str, str]]:
    # A value with unbalanced parentheses is replaced before the parse, and an addr-spec
    # still holding "[" after it is a domain literal the parser failed on.
    values = [v if _parens_balanced(v) else "('', '')" for v in values]
    return [("", "") if "[" in addr else (name, addr)
            for name, addr in _legacy_getaddresses(values)]


def _strict_getaddresses(fieldvalues) -> List[Tuple[str, str]]:
    values = [str(v) for v in fieldvalues]
    pairs = _strict_parse(values)
    # One mailbox per unquoted comma, plus one: more means the parser split a display
    # name into addresses, which is the ambiguity the fix refuses.
    expected = sum(1 + _strip_quoted(v).count(",") for v in values)
    return pairs if len(pairs) == expected else [("", "")]


def _strict_parseaddr(addr) -> Tuple[str, str]:
    if isinstance(addr, list):
        addr = addr[0]
    if not isinstance(addr, str):
        return ("", "")
    pairs = _strict_parse([addr])
    return pairs[0] if len(pairs) == 1 else ("", "")


def getaddresses(fieldvalues, *, strict: bool = True) -> List[Tuple[str, str]]:
    """email.utils.getaddresses as the patched stdlib defines it, on every Python.
    strict=False is the lenient legacy parse, for a caller that must not lose a good
    mailbox beside a bad one."""
    if _STDLIB_STRICT:
        return _stdlib.getaddresses(fieldvalues, strict=strict)
    return _strict_getaddresses(fieldvalues) if strict else _legacy_getaddresses(fieldvalues)


def parseaddr(addr, *, strict: bool = True) -> Tuple[str, str]:
    """email.utils.parseaddr as the patched stdlib defines it, on every Python."""
    if _STDLIB_STRICT:
        return _stdlib.parseaddr(addr, strict=strict)
    return _strict_parseaddr(addr) if strict else _legacy_parseaddr(addr)


def header_addresses(value: Any) -> Set[str]:
    """The complete mailboxes named in one address header, lowercased: "Bob <Bob@Example.com>,
    ann@example.com" gives {"bob@example.com", "ann@example.com"}. This is the matching
    counterpart of normalize_recipients: a query for ann@example.com must match a message
    from ann@example.com and NOT one from joann@example.com, which a substring test on the
    stored header string would. Display names are dropped; an empty or unparseable header
    gives an empty set."""
    if not value:
        return set()
    out: Set[str] = set()
    for _name, addr in getaddresses([str(value)]):
        addr = (addr or "").strip().lower()
        if "@" in addr:
            out.add(addr)
    return out


def recipient_pairs(value: Any) -> List[Tuple[str, str]]:
    """Parse a recipient string ("Anna <a@x.com>, b@y.com") or list into ``(display name,
    address)`` pairs, the addresses validated. Invalid/empty entries are dropped; order is
    preserved and a repeated address is dropped (the first name wins). The one parse behind
    the envelope (`normalize_recipients`) and what a confirmation card shows
    (`vaf.core.shown_text.mail_recipients`), so the card names exactly who the mail reaches."""
    if not value:
        return []
    items = value if isinstance(value, list) else [value]
    raw = ", ".join(str(x) for x in items if x)
    out: List[Tuple[str, str]] = []
    seen: Set[str] = set()
    for name, addr in getaddresses([raw]):
        addr = (addr or "").strip()
        if "@" in addr and "." in addr.rsplit("@", 1)[-1] and addr not in seen:
            seen.add(addr)
            out.append((name or "", addr))
    return out


def normalize_recipients(value: Any) -> List[str]:
    """Parse a recipient string ("a@x.com, b@y.com") or list into validated address
    strings (`recipient_pairs` without the names). Invalid/empty entries are dropped; order
    is preserved and duplicates removed."""
    return [addr for _name, addr in recipient_pairs(value)]



#: What a refusal says, per code: the sentence about the address and the next step. The
#: address is filled in as the cards show it, in ASCII marks, so the text is safe on a cp1252
#: console. It names no protocol: the model relays it to a person.
_REFUSAL_TEXT: Dict[str, Tuple[str, str]] = {
    "domain": ("the domain of {address} is not a valid internationalised domain name as "
               "written, so no mail server can deliver to it",
               "Check the address with the person, or reach them another way."),
    "domain_case": ("the domain of {address} has a capital letter outside plain ASCII, which "
                    "no mail server accepts",
                    "Write the domain in small letters and send it again."),
    "local_part": ("the part before the @ in {address} holds a control character, which no "
                   "mail server accepts",
                   "Check the address with the person, or reach them another way."),
    "smtputf8": ("the mail server of this account does not accept addresses with characters "
                 "outside plain ASCII before the @, such as {address}",
                 "Send it from another account, or ask for the address in plain letters."),
}

#: The same refusals without the address, for a log that must not carry it (the Front Office
#: lane log masks every address it names).
REFUSAL_REASON: Dict[str, str] = {
    "domain": "an address's domain is not a valid internationalised domain name",
    "domain_case": "an address's domain has a capital letter outside plain ASCII",
    "local_part": "an address holds a control character before the @",
    "smtputf8": ("the account's mail server does not accept addresses with characters "
                 "outside plain ASCII before the @"),
}


def refusal_text(code: str, address: str, outcome: str = "Nothing was queued.") -> str:
    """The refusal of `address` for `code`, ending in `outcome` and the next step."""
    from vaf.core.shown_text import ascii_marks, mail_address
    sentence, step = _REFUSAL_TEXT[code]
    shown = ascii_marks(mail_address(address))
    return f"cannot send this mail: {sentence.format(address=shown)}. {outcome} {step}"


class UndeliverableAddress(ValueError):
    """An address no mail server can take, or one this account's server cannot take, refused
    before anything is built or queued. ``code`` says why (``domain``, ``domain_case``,
    ``local_part``, ``smtputf8``) and ``address`` is the address as written. A ``ValueError``,
    so every caller that already reports one reports this one; ``reason`` is the refusal
    without the address, for a log that masks addresses."""

    def __init__(self, code: str, address: str, outcome: str = "Nothing was queued."):
        self.code = code
        self.address = str(address or "")
        super().__init__(refusal_text(code, self.address, outcome))

    @property
    def reason(self) -> str:
        return REFUSAL_REASON[self.code]


#: The longest domain DNS carries in its presentation form (RFC 1035 2.3.4, 255 octets on the
#: wire less the length and root octets).
_MAX_DOMAIN_OCTETS = 253


def _lower_ascii(label: str) -> str:
    """Only A-Z lowered. DNS compares ASCII case-insensitively (RFC 4343), so this loses
    nothing; ``str.lower()`` would also map characters outside ASCII (U+212A KELVIN SIGN to
    ``k``), which is the silent normalisation that delivers to another domain."""
    return "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in label)


def _a_label(label: str, addr: str) -> str:
    import idna
    try:
        return idna.alabel(label).decode("ascii")
    except idna.IDNAError:
        pass
    # A capital outside ASCII is the one refusal the writer can fix by retyping: the label is
    # valid in small letters and nothing else about it changes (NFKC-stable, so no Kelvin
    # sign or full-width letter that lower() would turn into another character).
    try:
        idna.alabel(label.lower())
        fixable = unicodedata.normalize("NFKC", label) == label
    except idna.IDNAError:
        fixable = False
    raise UndeliverableAddress("domain_case" if fixable else "domain", addr)


def envelope_address(addr: str) -> str:
    """The form of one addr-spec that goes on the wire: the local part as written, every
    domain label outside ASCII as its strict IDNA2008 A-label (ASCII letters in such a label
    lowered first), every ASCII label as written. An all-ASCII address without a control
    character comes back unchanged. Raises `UndeliverableAddress` for a domain IDNA2008 refuses
    and for a local part, ASCII or not, that holds a control or surrogate character (a NUL
    or a C1 control in an address is a typo or a forgery, and no server takes it); nothing
    else is refused, look-alikes included (the cards mark them)."""
    addr = str(addr or "")
    local, at, domain = addr.rpartition("@")
    if not at:
        local, domain = addr, ""
    if any(unicodedata.category(c) in ("Cc", "Cs") for c in local):
        raise UndeliverableAddress("local_part", addr)
    if domain.isascii():
        return addr
    labels = [label if label.isascii() else _a_label(_lower_ascii(label), addr)
              for label in domain.split(".")]
    ascii_domain = ".".join(labels)
    if len(ascii_domain) > _MAX_DOMAIN_OCTETS:
        raise UndeliverableAddress("domain", addr)
    return f"{local}@{ascii_domain}"


def needs_smtputf8(addr: str) -> bool:
    """The part before the @ holds a character outside ASCII: only a server that offers
    SMTPUTF8 (RFC 6531) can take the address, and the mail's headers must be UTF-8 (RFC 6532)."""
    local = str(addr or "").rpartition("@")[0]
    return not local.isascii()


def envelope_header(value: str) -> str:
    """An address header for a mail with ASCII headers: every addr-spec in its envelope form,
    names RFC 2047-encoded by the header policy as before. A header whose addresses all have
    ASCII domains is returned exactly as given, so a plain mail is built byte for byte as it
    always was."""
    pairs = recipient_pairs(value)
    if all(addr.rpartition("@")[2].isascii() for _name, addr in pairs):
        return value
    return ", ".join(_stdlib.formataddr((name, envelope_address(addr))) for name, addr in pairs)
