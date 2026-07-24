# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""C12/T12: format=flowed (RFC 3676) generation. A trailing space is the soft
line-break signal, so content lines must be rstripped (no accidental word
gluing); empty quoted lines must be a bare '>' (a hard break) so paragraphs in
the quote stay separated; space-stuffing for 'From '/leading-space/quote lines
must survive, but a pure quote-marker line must NOT be stuffed."""
from vaf.mail.compose import _flow_encode, quote_reply


def _lines(s: str):
    return s.split("\r\n")


def test_trailing_space_is_stripped_no_flow():
    # A line ending in a space would be flowed (glued to the next line); rstrip it.
    out = _flow_encode("hello world \nsecond line")
    assert out == "hello world\r\nsecond line"
    assert not any(ln.endswith(" ") for ln in _lines(out))


def test_space_stuffing_preserved():
    # 'From ' boundary, a leading-space line, and a bare '>' content line each
    # get one stuffed leading space so the recipient does not misread them.
    assert _flow_encode("From here to there") == " From here to there"
    assert _flow_encode(" indented") == "  indented"
    assert _flow_encode(">notaquote") == " >notaquote"


def test_real_quote_line_not_stuffed():
    assert _flow_encode("> quoted text") == "> quoted text"
    assert _flow_encode("> quoted text ") == "> quoted text"  # trailing space still stripped


def test_pure_quote_marker_lines_emitted_verbatim():
    # Empty quoted paragraphs (bare '>' / '> >') are structural: never stuffed.
    assert _flow_encode(">") == ">"
    assert _flow_encode("> >") == "> >"
    assert _flow_encode("> ") == ">"   # was a trailing-space empty quote -> hard '>'


def test_quote_reply_empty_lines_are_bare_gt_and_content_rstripped():
    body = quote_reply("Alice <alice@example.com>", None, "para one \n\npara two")
    lines = body.split("\n")
    assert "> para one" in lines          # trailing space removed
    assert ">" in lines                   # blank source line -> bare '>' (hard break)
    assert "> para two" in lines
    assert "> " not in lines              # never a trailing-space empty quote


def test_quote_reply_survives_flow_encode_roundtrip():
    # The quoted block, re-encoded on send, must keep bare '>' as '>' (a hard
    # empty quote), NOT ' >' (which would demote it to plain text).
    quoted = quote_reply("Alice <alice@example.com>", None, "para one\n\npara two")
    encoded = _lines(_flow_encode(quoted))
    assert ">" in encoded
    assert " >" not in encoded
    assert "> para one" in encoded
    assert "> para two" in encoded
