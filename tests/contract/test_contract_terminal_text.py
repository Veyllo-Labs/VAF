# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Contract: `vaf.terminal_text` (docs/EMBEDDING.md, "Showing text on a terminal" and "What
is and isn't stable").

A model reply, a room entry or any peer's text can carry escape sequences that set a
terminal's title, clear its screen, plant a link or write its clipboard. This is the call an
embedder makes before printing such text. Inputs are built from escapes, never literal bytes.
"""
import pytest

import vaf

REMOVED = ([chr(c) for c in range(0x00, 0x20) if chr(c) not in "\n\t"]
           + [chr(c) for c in range(0x7F, 0xA0)]
           + ["\u061c", "\u200e", "\u200f", "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
              "\u2066", "\u2067", "\u2068", "\u2069"]
           + ["\ud800", "\udc9b", "\udfff"])


@pytest.mark.parametrize("ch", REMOVED, ids=[hex(ord(c)) for c in REMOVED])
def test_every_c0_but_tab_and_newline_del_every_c1_the_bidi_controls_and_surrogates_go(ch):
    # A non-ASCII letter follows, so no escape sequence can end there.
    assert vaf.terminal_text(f"a{ch}\u00e4b") == "a\u00e4b"


@pytest.mark.parametrize("text", [
    "tab\tnewline\n",
    "Grüße",
    "مرحبا",
    "漢字",
    "\U0001F468\u200d\U0001F469\u200d\U0001F467",
    "a\u200bb",
])
def test_printable_text_and_zero_width_characters_are_kept(text):
    assert vaf.terminal_text(text) == text


def test_a_complete_escape_sequence_goes_as_a_whole():
    assert vaf.terminal_text("x\x1b]0;title\x07y") == "xy"
    assert vaf.terminal_text("a\x1b[31mred\x1b[0m b") == "ared b"
    assert vaf.terminal_text("x\x9b2Jy") == "xy"
    assert vaf.terminal_text("x\x1bcy") == "xy"


def test_an_unterminated_sequence_loses_only_its_control_characters():
    assert vaf.terminal_text("x\x1b]0;title\nnext") == "x0;title\nnext"


def test_none_is_empty_and_anything_else_is_its_str():
    assert vaf.terminal_text(None) == ""
    assert vaf.terminal_text(3) == "3"


def test_it_is_the_engine_function_itself():
    import vaf.core.terminal_text as tt  # re-export source named in EMBEDDING.md

    assert vaf.terminal_text is tt.terminal_text
