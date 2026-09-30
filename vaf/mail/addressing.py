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
guard test pins them to one object, Rule 2 single-source)."""
import email.utils as _stdlib
from typing import Any, List, Set, Tuple

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


def normalize_recipients(value: Any) -> List[str]:
    """Parse a recipient string ("a@x.com, b@y.com") or list into validated address
    strings. Invalid/empty entries are dropped; order is preserved and duplicates
    removed."""
    if not value:
        return []
    items = value if isinstance(value, list) else [value]
    raw = ", ".join(str(x) for x in items if x)
    out: List[str] = []
    for _name, addr in getaddresses([raw]):
        addr = (addr or "").strip()
        if "@" in addr and "." in addr.rsplit("@", 1)[-1] and addr not in out:
            out.append(addr)
    return out
