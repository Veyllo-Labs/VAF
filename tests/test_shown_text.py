# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""How a decision surface shows a recipient (vaf/core/shown_text.py).

The card a person confirms is a security control: ``alice@exаmple.com`` with a Cyrillic а,
shown as it is, reads as a known address and reaches a stranger. Nothing is normalised;
whatever could draw as another character is marked ``⟨U+XXXX⟩``.

MUTATIONS, each turns the named test red:
- write the old ``<U+XXXX>`` in `escaped`, or drop the U+27E8/U+27E9 escape in `shown_char`
  -> test_a_mark_cannot_be_forged, test_no_card_writes_the_old_mark;
- `.encode("idna")` instead of the raw punycode codec, keep non-ASCII, or skip the bracket
  -> test_mail_address;
- `isdigit()` instead of the ASCII digit set, or drop the separator rule -> test_phone;
- drop the "@" escape or the ellipsis budget in `person_name` -> test_person_name;
- count a space, a tab or an escaped character as a base again -> test_a_mark_needs_a_base;
- give `mail_recipients` a parse of its own without the dot filter -> test_one_parse;
- let `ascii_marks` pass a non-ASCII character or a literal "<" -> test_ascii_marks.
"""
import inspect
from pathlib import Path

import pytest

from vaf.core import shown_text as st
from vaf.mail.addressing import normalize_recipients, recipient_pairs

ROOT = Path(__file__).resolve().parents[1]
O, C = "⟨", "⟩"


def m(code: str) -> str:
    return f"{O}U+{code}{C}"


def test_a_mark_cannot_be_forged():
    assert st.escaped("а") == m("0430")
    assert st.escaped("\U0001F600") == m("1F600")
    # A value that writes a mark's shape itself shows its brackets escaped, so every mark on a
    # card is VAF's own.
    assert "".join(st.shown_tokens("x" + m("0041"))) == "x" + m("27E8") + "U+0041" + m("27E9")
    assert st.shown_char(O) == m("27E8") and st.shown_char(C) == m("27E9")
    assert st.shown_char("\u202e") == m("202E") and st.shown_char("\t") == "\t"


def test_no_card_writes_the_old_mark():
    """No second writer of the escape form is left: the card modules carry no "<U+" at all,
    and in shown_text only the terminal form (`ascii_marks`) writes it."""
    for rel in ("vaf/core/agent_origin.py", "vaf/core/app_link/confirmations.py"):
        assert "<U+" not in (ROOT / rel).read_text(encoding="utf-8"), rel
    for name, fn in inspect.getmembers(st, inspect.isfunction):
        if fn.__module__ == st.__name__ and name != "ascii_marks":
            assert "<U+" not in inspect.getsource(fn), name


@pytest.mark.parametrize("raw,shown", [
    ("alice@example.com", "alice@example.com"),
    ("alice@exаmple.com", f"alice@ex{m('0430')}mple.com [xn--exmple-4nf.com]"),
    ("аlice@example.com", f"{m('0430')}lice@example.com"),
    ('"a b"@x.com', f"{m('0022')}a{m('0020')}b{m('0022')}@x.com"),
    # Full-width: nameprep would fold it to "example.com", which is the whole attack.
    ("a@ｅxample.com", f"a@{m('FF45')}xample.com [xn--xample-hy68a.com]"),
    ("j@bücher.de", f"j@b{m('00FC')}cher.de [xn--bcher-kva.de]"),
    ("x<y>@a.b", f"x{m('003C')}y{m('003E')}@a.b"),
])
def test_mail_address(raw, shown):
    assert st.mail_address(raw) == shown
    assert isinstance(st.mail_address(raw), st.Shown)


@pytest.mark.parametrize("raw,shown,digits", [
    ("+491761234567", "+491761234567", "+491761234567"),
    ("+49 176 123-45.67", "+491761234567", "+491761234567"),
    # A national number is dialled as the contact book reads it; the card shows that form.
    ("(0176) 1234567", "01761234567 [+491761234567]", "01761234567"),
    ("0049 176 1234567", "00491761234567 [+491761234567]", "00491761234567"),
    ("+49 176 123456٧", f"+49176123456{m('0667')}", None),
    ("４９176", f"{m('FF14')}{m('FF19')}176", None),
    ("+49176123456²", f"+49176123456{m('00B2')}", None),
    ("+491761234567 ext 99", f"+491761234567{m('0065')}{m('0078')}{m('0074')}99", None),
    ("\u202e7654321671 94+", f"{m('202E')}765432167194{m('002B')}", None),
    ("+49\u00a0176", f"+49{m('00A0')}176", None),
    ("+49\u200b176", f"+49{m('200B')}176", None),
    ("+", "+", None),
    ("", "", None),
])
def test_phone(raw, shown, digits):
    assert st.phone_number(raw) == shown
    assert st.phone_digits(raw) == digits


def test_person_name():
    name = st.person_name("Alice " + "B" * 90)
    assert name == "Alice " + "B" * 33 + "…" and len(name) == st.NAME_MAX
    assert st.person_name("boss@firma.de (Boss)") == f"boss{m('0040')}firma.de (Boss)"
    assert st.person_name("Anna Berg") == "Anna Berg" and st.person_name("") == ""
    # The cut never splits a mark: a mark that does not fit is left out whole.
    cut = st.person_name("A" * 35 + "\u202e" + "B" * 10)
    assert cut == "A" * 35 + "…", cut
    assert st.person_name("x" * st.NAME_MAX) == "x" * st.NAME_MAX


def test_a_mark_needs_a_base():
    """A combining mark after the line's start, a space, a tab or an already escaped character
    is shown escaped: a space plus U+0338 draws as a slash."""
    assert st.shown_tokens("\u0338a") == [m("0338"), "a"]
    assert st.shown_tokens("a \u0338b") == ["a", " ", m("0338"), "b"]
    assert st.shown_tokens("a\t\u0301") == ["a", "\t", m("0301")]
    assert st.shown_tokens("\u200b\u0301") == [m("200B"), m("0301")]
    # On a real base, two pass and the third is shown.
    assert st.shown_tokens("e\u0301\u0301\u0301") == ["e", "\u0301", "\u0301", m("0301")]
    assert st.person_name("@\u0301") == m("0040") + m("0301")


CORPUS = [
    "alice@exаmple.com",
    '"boss@firma.de (Boss)" <x@evil.test>',
    "Anna Berg <anna@example.com>, b@y.com, anna@example.com",
    "junk, b@y.com",
    "nodot@localhost, c@d.org",
    '"a b"@x.com',
    "",
]


@pytest.mark.parametrize("raw", CORPUS)
def test_one_parse(raw):
    """The card names exactly the addresses the envelope is built from."""
    envelope = normalize_recipients(raw)
    assert [a for _, a in recipient_pairs(raw)] == envelope
    shown = [a for a, _ in st.mail_recipients(raw)]
    if envelope:
        assert shown == [st.mail_address(a) for a in envelope]
    else:
        # Nothing the lane would send to: the raw text is shown whole and marked instead.
        assert shown == ([st.mail_address(raw)] if raw.strip() else [])


def test_mail_recipients_and_the_line():
    pairs = st.mail_recipients('"boss@firma.de (Boss)" <x@evil.test>')
    assert st.recipients_line(pairs) == f"x@evil.test (boss{m('0040')}firma.de (Boss))"
    line = st.recipients_line(st.mail_recipients(
        "Anna <anna@example.org>, b@y.com, anna@example.org", first_name="Anna Berg"))
    assert line == "anna@example.org (Anna Berg), b@y.com"
    # A name equal to its address is not repeated; nothing found is shown whole and marked.
    assert st.recipients_line(st.mail_recipients('"c@d.org" <c@d.org>')) == "c@d.org"
    assert st.mail_recipients("not an address") == [(f"not{m('0020')}an{m('0020')}address", "")]
    assert st.mail_recipients("") == []


def test_ascii_marks():
    shown = st.mail_address("alice@exаmple.com")
    out = st.ascii_marks(shown)
    assert out == "alice@ex<U+0430>mple.com [xn--exmple-4nf.com]"
    out.encode("cp1252")
    assert st.ascii_marks("Jürgen <x>") == "J<U+00FC>rgen <U+003C>x<U+003E>"
    assert st.ascii_marks(st.person_name("A" * 60)).endswith("<U+2026>")


def test_every_at_sign_in_a_name_is_marked():
    """MUTATION: mark only the ASCII "@" in `person_name`.

    Measured: ``"boss＠firma.de (Boss)" <x@evil.test>`` put "boss＠firma.de" in the
    web title as if it were the address. Every character whose NFKC form is "@" reads as one,
    and the set is pinned against a scan of every code point, so a new Unicode version that
    adds one fails here."""
    import sys
    import unicodedata
    scanned = {chr(c) for c in range(sys.maxunicode + 1)
               if not 0xD800 <= c <= 0xDFFF and unicodedata.normalize("NFKC", chr(c)) == "@"}
    assert scanned | {"@"} == set(st.AT_SIGNS)
    assert st.person_name("boss＠firma.de (Boss)") == f"boss{m('FF20')}firma.de (Boss)"
    assert st.person_name("boss﹫firma.de") == f"boss{m('FE6B')}firma.de"
    line = st.recipients_line(st.mail_recipients('"Anna), boss＠firma.de (Boss" <x@evil.test>'))
    assert line == f"x@evil.test (Anna), boss{m('FF20')}firma.de (Boss)"
    # Ordinary names stay as they are, brackets included.
    assert st.person_name("Anna (Marketing)") == "Anna (Marketing)"
    assert st.person_name("田中（営業）") == "田中（営業）"


@pytest.mark.parametrize("space", [" ", "\u00a0", "\u2003", "\u3000", "\u202f", "\u205f", "\t"])
def test_no_space_is_a_base(space):
    """MUTATION: count only the ASCII space and the tab as no base."""
    assert st.shown_tokens(f"a{space}\u0338b") == ["a", space, m("0338"), "b"]


@pytest.mark.parametrize("mark", ["\u034f", "\u17b4", "\u17b5", "\u180b", "\u180f", "\ufe00",
                                  "\ufe0e", "\ufe0f", "\U000e0100", "\U000e01ef"])
def test_a_mark_that_draws_as_nothing_is_shown(mark):
    """MUTATION: drop `_ignorable_mark` (two of them on a letter pass unseen again).

    Measured: ``a`` U+17B4 U+17B4 ``b`` was shown as ``a\u17b4\u17b4b``, which reads as "ab"."""
    assert st.shown_tokens(f"a{mark}b") == ["a", st.escaped(mark), "b"]


def test_the_emoji_selector_after_a_symbol():
    """MUTATION: keep U+FE0F in a whole value, or after any base.

    A heart is U+2764 U+FE0F almost everywhere; marking that in every message is noise. It is
    kept in free text right after a symbol (category So), once, and marked anywhere else and
    in a value shown whole (a target, an address)."""
    assert st.shown_tokens("❤\ufe0f") == ["❤", "\ufe0f"]
    assert st.shown_tokens("❤\ufe0f", whole=True) == ["❤", m("FE0F")]
    assert st.shown_tokens("❤\ufe0f\ufe0f") == ["❤", "\ufe0f", m("FE0F")]
    assert st.shown_tokens("a\ufe0f") == ["a", m("FE0F")]
    assert st.shown_tokens("\ufe0f") == [m("FE0F")]


def test_a_mark_after_an_escaped_mark_has_no_base():
    """MUTATION: keep `base` after a mark is escaped.

    ``a`` U+034F U+0338 ``b``: U+034F is escaped, and a raw U+0338 after it would draw onto
    the escape's closing bracket. An escaped mark is no base, like any escaped character."""
    assert st.shown_tokens("a\u034f\u0338b") == ["a", m("034F"), m("0338"), "b"]
    assert st.shown_tokens("a\u0301\u0301\u0301\u0301") == ["a", "\u0301", "\u0301", m("0301"), m("0301")]
    assert st.shown_tokens("❤\u034f\ufe0f") == ["❤", m("034F"), m("FE0F")]
