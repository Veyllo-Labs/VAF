# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Address parsing behaves as the patched stdlib does on every supported Python.

Python 3.10.11 and 3.11.9 (the last Windows and macOS installers of those series, and
what the CI's setup-python runs there) predate the stdlib's strict parsing, and parsed
"me@example.com <stranger@example.org>" as the address in the display name. The
nightly CI failed on exactly those two versions for thirteen nights while every local
run was green. `strict=False` on a patched stdlib IS the pre-fix parse (email/utils.py:
`if not strict: return _AddressList(...).addresslist`), so an unpatched Python can be
reproduced here: the emulation is forced, fed the legacy parse, and compared with the
real strict parser of this interpreter."""
import email.utils
import re
from pathlib import Path

import pytest

import vaf.mail.addressing as addressing

REPO = Path(__file__).resolve().parents[1]

needs_patched_stdlib = pytest.mark.skipif(
    not getattr(email.utils, "supports_strict_parsing", False),
    reason="the reference is the patched stdlib's own strict parser")

# Header values from the measurement that found the gap, plus the shapes the strict
# checks exist for: quoted commas and parentheses, escapes, unbalanced parentheses,
# domain literals, groups, comments, an unclosed quote, and the empty value.
CORPUS = [
    "me@example.com <stranger@example.org>",
    '"me@example.com" <stranger@example.org>',
    "Alice <alice@example.org>",
    "alice@example.org",
    "Alice <alice@example.org>, bob@example.net",
    '"Doe, John" <john@example.org>',
    '"Doe, John" <john@example.org>, "Roe, Jane" <jane@example.org>',
    "alice@example.org, bad<<, bob@example.net",
    "alice@example.org (Alice)",
    "alice@example.org (Alice (the first))",
    "alice@example.org (Alice",
    "alice@example.org Alice)",
    '"Alice (quoted" <alice@example.org>',
    '"Alice \\" quote" <alice@example.org>',
    'Alice \\(escaped <alice@example.org>',
    "Alice Smith <alice@example.org",
    "alice@example.org <bob@example.org>, carol@example.org",
    "a@b.c@evil.com",
    "Bob Smith <bob@[192.168.0.1]>",
    "bob@[192.168.0.1]",
    "undisclosed-recipients:;",
    "Team: alice@example.org, bob@example.org;",
    '"unclosed <alice@example.org>',
    "=?utf-8?q?me=40example.com?= <stranger@example.org>",
    "alice@example.org,",
    ",alice@example.org",
    "",
    "   ",
]


@pytest.fixture
def unpatched(monkeypatch):
    """This interpreter, made to behave like 3.11.9: no strict parsing in the stdlib.
    On an interpreter that IS unpatched (the nightly's 3.10.11 and 3.11.9, the hostile
    run's old-mail-parser axis) nothing is replaced: the module already parses with the
    real legacy parser, which takes no `strict` keyword at all."""
    if not getattr(email.utils, "supports_strict_parsing", False):
        return
    monkeypatch.setattr(addressing, "_STDLIB_STRICT", False)
    monkeypatch.setattr(addressing, "_legacy_getaddresses",
                        lambda values: email.utils.getaddresses(values, strict=False))
    monkeypatch.setattr(addressing, "_legacy_parseaddr",
                        lambda addr: email.utils.parseaddr(addr, strict=False))


@needs_patched_stdlib
@pytest.mark.parametrize("value", CORPUS)
def test_the_emulation_matches_the_stdlib_strict_parser(value, unpatched):
    """MUTATION: drop any one of the four checks and a value here parses differently
    from the stdlib's own strict parser."""
    assert addressing.getaddresses([value]) == email.utils.getaddresses([value], strict=True)
    assert addressing.parseaddr(value) == email.utils.parseaddr(value, strict=True)


@needs_patched_stdlib
def test_several_field_values_are_counted_together(unpatched):
    values = ["Alice <alice@example.org>", '"Doe, John" <john@example.org>, bob@example.net']
    assert addressing.getaddresses(values) == email.utils.getaddresses(values, strict=True)
    bad = ["alice@example.org", "me@example.com <stranger@example.org>"]
    assert addressing.getaddresses(bad) == email.utils.getaddresses(bad, strict=True)


def test_lenient_stays_the_legacy_parse(unpatched):
    """A caller that asks for strict=False (case_token) keeps a good mailbox beside a bad
    one on every Python."""
    assert addressing.getaddresses(["alice@example.org <bob@example.org>, carol@example.org"],
                                   strict=False)[-1] == ("", "carol@example.org")


def test_on_an_unpatched_python_the_display_name_is_not_the_sender(unpatched):
    """The nightly's failing case, end to end through the two readers that decide who a
    mail is from. MUTATION: let getaddresses/parseaddr fall through to the legacy parse
    and the stranger's mail is the owner's own, attributed to the owner's address."""
    from vaf.mail.cases import sender_address
    from vaf.mail.classify import classify_machine
    from vaf.mail.parser import ParsedMessage
    p = ParsedMessage(from_addr="me@example.com <stranger@example.org>")
    assert classify_machine(p, own_addresses=["me@example.com"]).kind == ""
    assert sender_address(p.from_addr) == ""


def test_the_running_python_is_passed_through_untouched():
    """On a patched stdlib the wrapper is the stdlib: nothing changes where the fix
    already exists."""
    if not getattr(email.utils, "supports_strict_parsing", False):
        pytest.skip("unpatched stdlib: the emulation is what runs here")
    for value in CORPUS:
        assert addressing.getaddresses([value]) == email.utils.getaddresses([value])
        assert addressing.parseaddr(value) == email.utils.parseaddr(value)


_DIRECT = re.compile(
    r"from\s+email\.utils\s+import\s+[^\n]*\b(parseaddr|getaddresses)\b"
    r"|email\.utils\.(parseaddr|getaddresses)\b"
    r"|from\s+email\s+import\s+[^\n]*\butils\b")


def test_nothing_in_vaf_parses_an_address_header_past_the_one_parser():
    """A direct email.utils parse is version-dependent on the Pythons VAF supports.
    MUTATION: import parseaddr from email.utils in any vaf/ module and this names it."""
    offenders = []
    for path in sorted((REPO / "vaf").rglob("*.py")):
        if path == REPO / "vaf" / "mail" / "addressing.py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in _DIRECT.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            offenders.append(f"{path.relative_to(REPO).as_posix()}:{line}")
    assert offenders == [], ("parse addresses with vaf.mail.addressing.getaddresses/parseaddr, "
                             f"not email.utils: {offenders}")
