# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Text from other parties reaches the terminal without its escape sequences.

MEASURED BEFORE THE FIX (Textual 8.2.8, Rich 14.3.3, headless): a window-title sequence, a
screen clear, an OSC 8 link, an OSC 52 clipboard write, the C1 forms of CSI and OSC, NUL, DEL
and U+202E all reached the output of every transcript widget of the full-screen app and of
`Console.print` with `str`, `Markdown`, `Panel` and `Table`. Only the modern lane's queued web
input was cleaned, by a hand copy in run.py. Measured after the first version of the fix: a
style's LINK target (`[link=...]` markup, `Text(style=Style(link=...))`) still went out raw
inside the renderer's own OSC 8, browser_use replaced the logging setup with a raw stderr
handler on import, and `vaf audit`, `vaf env` and the research progress printed model text
raw.

The fix sits at each renderer's sink (one Textual line filter, one Rich console class, the
bare-stdout and JSON lines, the logging setup of the CLI entry point), all on one
definition, `vaf.core.terminal_text`. Every test input is built from escapes in this source,
never from literal control bytes, and no test prints one.
"""
import ast
import asyncio
import base64
import inspect
import io
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from test_a2a_cli import _guest, _lines, rooms  # noqa: F401 - fixture
from test_tui_app_smoke import smoke_app  # noqa: F401 - fixture
from vaf.core.terminal_text import TerminalConsole, terminal_json, terminal_text

ROOT = Path(__file__).resolve().parents[1]

BIDI = ["\u061c", "\u200e", "\u200f", "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
        "\u2066", "\u2067", "\u2068", "\u2069"]

#: The probe's control cases: title, clear, OSC 8 link, OSC 52 clipboard, C1 CSI, C1 OSC,
#: NUL and DEL, and a right-to-left override.
CASES = [
    "\x1b]0;PWNEDTITLE\x07",
    "\x1b[2J",
    "\x1b]8;;http://evil.test/\x1b\\click\x1b]8;;\x1b\\",
    "\x1b]52;c;aGk=\x07",
    "\x9b2J",
    "\x9d0;T\x9c",
    "a\x00b\x7fc",
    "abc\u202exe.fdp",
]
HOSTILE = " ".join(CASES)
#: What a reader still sees of HOSTILE, and what goes with the sequences.
VISIBLE = ("click", "xe.fdp")
GONE = ("PWNEDTITLE", "aGk=", "evil.test")

#: Link targets a sender can put into a style: an early BEL plus ESC c (a terminal reset),
#: and an OSC 52 clipboard write.
LINK_MARKUP = "[link=http://a/\x07\x1bcz]LINKA[/link]"
LINK_TARGET = "http://a/\x1b]52;c;aGk=\x07"
_OSC8 = re.compile("\x1b\\]8;([^;\x1b]*);([^\x1b]*)\x1b\\\\")


def _unsafe(ch: str) -> bool:
    code = ord(ch)
    return ((code < 0x20 and ch not in "\n\t") or 0x7F <= code <= 0x9F or ch in BIDI
            or 0xD800 <= code <= 0xDFFF)


def _unsafe_in(text: str) -> list:
    return sorted({hex(ord(ch)) for ch in text if _unsafe(ch)})


def _foreign(out: str) -> tuple:
    """A terminal output split into what the renderer wrote itself and what came from the
    text: its own SGR codes and OSC 8 framing are taken out, and the link targets are
    returned apart. Returns (unsafe characters outside, the link targets)."""
    targets = [m.group(2) for m in _OSC8.finditer(out)]
    rest = re.sub("\x1b\\[[0-9;]*m", "", _OSC8.sub("", out))
    return _unsafe_in(rest), targets


# ── the definition ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("ch", [chr(c) for c in (0x00, 0x01, 0x07, 0x08, 0x0B, 0x0C, 0x0D,
                                                0x1B, 0x1F, 0x7F, 0x80, 0x85, 0x9B, 0x9D,
                                                0x9F, 0xD800, 0xDC9D, 0xDFFF)] + BIDI,
                         ids=lambda ch: hex(ord(ch)))
def test_every_control_class_is_removed(ch):
    """C0 but tab and newline, DEL, C1, the twelve bidirectional controls and lone
    surrogates. A non-ASCII letter follows, so no escape sequence can end there.
    MUTATION: drop 0x7F-0x9F, the bidi set or the surrogate range - red."""
    assert terminal_text(f"ab{ch}äcd") == "abäcd"


@pytest.mark.parametrize("text", [
    "tab\tand\nnewline",
    "Grüße äöü",
    "مرحبا",
    "漢字かな",
    "\U0001F468\u200d\U0001F469\u200d\U0001F467",
    "zero\u200bwidth \u200cjoiner \u2060word \ufeffmark",
    "brackets [31m and ]0; stay when nothing escapes them",
])
def test_printable_text_and_format_characters_are_kept(text):
    """Umlauts, Arabic, CJK, an emoji with ZWJ, the zero-width characters, and the printable
    look of a sequence without its ESC. MUTATION: add U+200D to the table - red."""
    assert terminal_text(text) == text


def test_none_is_empty_and_anything_else_is_its_str():
    assert terminal_text(None) == ""
    assert terminal_text(5) == "5"
    assert terminal_text("") == ""


@pytest.mark.parametrize("raw, shown", [
    ("x\x1b]0;PWNEDTITLE\x07y", "xy"),                      # OSC title, BEL
    ("x\x1b]52;c;aGk=\x1b\\y", "xy"),                        # OSC 52, ST
    ("x\x9d0;T\x9cy", "xy"),                                 # C1 OSC, C1 ST
    ("a\x1b[31mred\x1b[0m b", "ared b"),                     # a program's colours
    ("x\x9b2Jy", "xy"),                                      # C1 CSI
    ("x\x1bcz", "xz"),                                       # ESC c, terminal reset
    ("x\x1b(Bz", "xz"),                                      # charset select
    ("x\x1bP1$r\x1b\\z", "xz"),                              # DCS
])
def test_a_complete_escape_sequence_goes_as_a_whole(raw, shown):
    """No fragments: a coloured tool output reads as its text, a title leaves no `]0;...`.
    MUTATION: drop the sequence pass from `_clean` - red."""
    assert terminal_text(raw) == shown


def test_an_unterminated_sequence_never_swallows_the_text_after_it():
    """A string sequence ends at BEL or ST and is never sought across a newline nor beyond
    2048 characters, so a missing terminator costs its ESC and nothing else.
    MUTATION: let the string body cross a newline - red."""
    assert terminal_text("x\x1b]0;tail\nnext\x07z") == "x0;tail\nnextz"
    long_body = "a" * 3000
    assert terminal_text("x\x1b]0;" + long_body + "\x07z") == "x0;" + long_body + "z"


@pytest.mark.parametrize("spam", [
    ("\x1b]0;" + "a" * 3000) * 300 + ("\x1b[" + "1;" * 40) * 5000,
    "\x9d" * 200_000,
    "\x90" * 200_000,
    ("\x1b]" + "\x9d" * 2000) * 100,
], ids=["long-osc-and-csi", "c1-osc-run", "c1-dcs-run", "osc-of-c1"])
def test_cleaning_stays_linear_on_hostile_input(spam):
    """Bounded patterns, and a string sequence's body holds no control character, so a run
    of introducers is not retried 2048 characters at every position. Measured with the
    body that admitted C1 introducers: 2.5 s for 100 KB of U+009D, about 5 s here.
    MUTATION: let the string body admit the C1 introducers again - red."""
    started = time.perf_counter()
    cleaned = terminal_text(spam)
    assert time.perf_counter() - started < 1.0
    assert not _unsafe_in(cleaned)


def test_a_json_line_escapes_what_json_dumps_leaves_raw_and_parses_back_unchanged():
    """MUTATION: return plain json.dumps - red."""
    row = {"text": HOSTILE, "who": "Bob\u202e", "n": 1, "list": ["\x85", "ä", "\udc9d"]}
    line = terminal_json(row)
    assert not _unsafe_in(line)
    assert json.loads(line) == row
    assert "ä" in line, "non-ASCII text stays readable"
    assert terminal_json({"a": "plain"}) == json.dumps({"a": "plain"}, ensure_ascii=False)
    assert json.loads(terminal_json(row, indent=2)) == row


def test_a_surrogateescape_stream_receives_no_c1_byte():
    """Under Python's UTF-8 mode (a C or POSIX locale switches it on) stdout writes a lone
    U+DC80-U+DC9F as the raw byte 0x80-0x9F, a C1 control. MUTATION: drop the surrogate
    range from the unsafe set - red."""
    code = ("from vaf.core.terminal_text import terminal_text\n"
            "print(terminal_text('A\\udc9d0;PWN\\udc9cB\\udc9b2J'))\n")
    env = dict(os.environ, PYTHONUTF8="1", LC_ALL="C", PYTHONPATH=str(ROOT))
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, env=env,
                          timeout=120)
    assert proc.returncode == 0, proc.stderr[-500:]
    assert not [b for b in proc.stdout if 0x80 <= b <= 0x9F]
    assert b"0;PWN" in proc.stdout


# ── the modern lane's echo now uses the one definition ──────────────────────

def test_the_echo_keeps_no_copy_of_its_own():
    """MUTATION: give `_terminal_echo` its own comprehension back - red."""
    import vaf.cli.cmd.run as run_mod

    assert not hasattr(run_mod, "_BIDI_CONTROLS")
    source = inspect.getsource(run_mod._terminal_echo)
    assert "terminal_text(" in source and "unicodedata" not in source


# ── Rich: every console in vaf/ is a TerminalConsole ───────────────────────

def _vaf_trees():
    for path in sorted((ROOT / "vaf").rglob("*.py")):
        if "vendor" in path.relative_to(ROOT).parts:
            continue
        yield path, ast.parse(path.read_text(encoding="utf-8"))


def test_no_plain_rich_console_is_constructed_or_imported_in_vaf():
    """A console built from `rich.console.Console` prints other parties' text with its
    escape sequences. A call named `Console` is refused, and so is the import that makes an
    alias or a later construction possible, and `rich.get_console()`. The one module that
    imports it is the one that subclasses it. MUTATION: change one site back to `Console(`,
    or import `Console` from rich.console in coder.py again - red."""
    allowed = {"vaf/core/terminal_text.py"}
    offenders = []
    for path, tree in _vaf_trees():
        rel = path.relative_to(ROOT).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else (
                    func.attr if isinstance(func, ast.Attribute) else "")
                if name in ("Console", "get_console"):
                    offenders.append(f"{rel}:{node.lineno} {name}()")
            elif isinstance(node, ast.ImportFrom) and rel not in allowed:
                if node.module == "rich.console" and any(a.name == "Console" for a in node.names):
                    offenders.append(f"{rel}:{node.lineno} imports Console")
                if node.module == "rich" and any(a.name == "get_console" for a in node.names):
                    offenders.append(f"{rel}:{node.lineno} imports get_console")
    assert not offenders, "use vaf.core.terminal_text.TerminalConsole: " + ", ".join(offenders)


def _rendered(console_kwargs, *renderables, **print_kwargs) -> str:
    out = io.StringIO()
    console = TerminalConsole(file=out, width=200, **console_kwargs)
    for renderable in renderables:
        console.print(renderable, **print_kwargs)
    return out.getvalue()


def test_the_console_cleans_str_markdown_panel_and_table():
    """No colour system, so every control character in the output would be the sender's.
    With markup off a string loses every sequence whole before layout. With markup on, and
    in a `Text` or `Markdown` object, Rich has already dropped the BEL that ended a title
    sequence before the console sees it, so that body stays readable as text; a sequence
    ended by ST still goes whole, also where the highlighter split it into several segments;
    no control character survives anywhere.
    MUTATION: let `render` pass segments unchanged (and `render_str` too) - red."""
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    def table():
        grid = Table()
        grid.add_column("c")
        grid.add_row(HOSTILE)
        return grid

    plain = _rendered({"force_terminal": False, "color_system": None, "markup": False},
                      HOSTILE, Panel(HOSTILE), table())
    marked = _rendered({"force_terminal": False, "color_system": None},
                       HOSTILE, Panel(HOSTILE), table(), Text(HOSTILE), Markdown(HOSTILE))
    for out in (plain, marked):
        assert not _unsafe_in(out)
        assert all(word in out for word in VISIBLE)
    assert not [word for word in GONE if word in plain]
    assert "evil.test" not in marked


def test_a_sequence_the_highlighter_split_still_goes_whole():
    """Rich's highlighter gives the `31` of a colour code its own segment, so a sequence is
    matched on the line's joined text, and the width it took comes back at the end of the
    run, before the next border. MUTATION: clean each segment on its own again - red."""
    from rich.table import Table

    out = _rendered({"force_terminal": False, "color_system": None},
                    "a\x1b[31mred\x1b[0m b")
    assert out.startswith("ared b"), repr(out)
    grid = Table()
    grid.add_column("a")
    grid.add_column("b")
    grid.add_row("x \x1b[31m42\x1b[0m y", "p \x1b[1m7\x1b[0m q")
    lines = [line for line in _rendered({"force_terminal": False, "color_system": None},
                                        grid).splitlines() if line]
    assert "[31m" not in "".join(lines) and "x 42 y" in lines[3] and "p 7 q" in lines[3]
    assert len({len(line) for line in lines}) == 1, lines


def test_the_console_strips_a_text_object_and_keeps_its_width():
    """A `Text` object is not a string, so it is cleaned at the segments, and a panel keeps
    its right border in place (inside a panel Rich pads each rendered line itself; the
    padding of `terminal_segments` is pinned by the line-filter and newline tests).
    MUTATION: let `render` pass segments unchanged - red."""
    from rich.panel import Panel
    from rich.text import Text

    out = _rendered({"force_terminal": False, "color_system": None},
                    Panel.fit(Text("a\x1b[31mred\x1b[0m b")))
    lines = [line for line in out.splitlines() if line]
    assert "[31m" not in out and "ared b" in out
    assert len({len(line) for line in lines}) == 1, lines


def test_the_console_cleans_a_string_before_layout_when_markup_is_off():
    """With markup off a string loses its sequences before Rich measures it, so no gap is
    left where they stood. MUTATION: drop the `render_str` override - red (the panel is laid
    out on the raw width and padded)."""
    from rich.panel import Panel

    out = _rendered({"force_terminal": False, "color_system": None, "markup": False},
                    Panel.fit("a\x1b[31mred\x1b[0m b"))
    assert "│ ared b │" in out
    out = _rendered({"force_terminal": False, "color_system": None}, "a\x1b[31mred\x1b[0m b",
                    markup=False)
    assert out == "ared b\n"


@pytest.mark.parametrize("sent", [
    "see [\x00link=http://evil.test/]click here",
    "see [\x1b[0mlink=http://evil.test/]click here",
    "a [\x00/] b",
    "[\x00/bold] after",
])
def test_markup_escaped_text_stays_text_with_markup_on(sent):
    """VAF prints a peer's text escaped (`rich.markup.escape`) into markup. escape() leaves a
    bracket followed by a control character alone, so cleaning BEFORE the markup parser
    rebuilt a live `[link=...]` tag or an unbalanced closing tag. With markup on the string
    goes to the parser as it is and the segments are cleaned after; the width is kept, so
    the panel border still lines up. MUTATION: pre-clean in `render_str` with markup on too -
    red (a link to evil.test, or a MarkupError)."""
    from rich.markup import escape
    from rich.panel import Panel

    out = _rendered({"force_terminal": True, "color_system": "truecolor"},
                    "[bold]peer:[/bold] " + escape(sent), end="")
    outside, targets = _foreign(out)
    assert not outside and not [t for t in targets if "evil" in t], (outside, targets)
    framed = _rendered({"force_terminal": False, "color_system": None},
                       Panel.fit(escape(sent)))
    lines = [line for line in framed.splitlines() if line]
    assert len({len(line) for line in lines}) == 1, lines
    assert not _unsafe_in(framed)


def test_the_console_keeps_its_own_styles_and_control_codes():
    """Rich's colours come from styles and its clear is a control segment: both stay.
    MUTATION: let `render` clean control segments too - red (the clear is gone)."""
    from rich.control import Control

    out = _rendered({"force_terminal": True, "color_system": "truecolor"},
                    "[bold]strong[/bold] " + HOSTILE, Control.clear())
    assert "\x1b[1m" in out, "the console's own style is gone"
    assert "\x1b[2J" in out, "the console's own clear is gone"
    # The only ESC left starts the console's own CSI codes; no OSC, no C1, no bidi.
    assert "\x1b]" not in out
    assert not [c for c in _unsafe_in(out) if c != "0x1b"]


def test_the_console_writes_no_control_character_into_a_link_target():
    """Rich writes a style's link target raw into its OSC 8. Two senders' forms: markup in a
    model chunk (the classic stream prints with markup on) and a `Text` with a link style.
    VAF's own `file://` link still goes out. MUTATION: drop the link handling in
    `terminal_segments` - red (the Text form); drop `render_str` as well - red (markup)."""
    from rich.style import Style
    from rich.text import Text

    for renderable, kwargs in ((LINK_MARKUP, {"markup": True}),
                               (Text("LINKB", style=Style(link=LINK_TARGET)), {}),
                               (Text.from_markup(LINK_MARKUP), {})):
        out = _rendered({"force_terminal": True, "color_system": "truecolor"}, renderable,
                        end="", **kwargs)
        outside, targets = _foreign(out)
        assert not outside, (renderable, outside)
        assert not [t for t in targets if _unsafe_in(t)], (renderable, targets)
        assert "LINK" in out
    own = _rendered({"force_terminal": True, "color_system": "truecolor"},
                    "[link=file:///tmp/project]/tmp/project[/link]", markup=True)
    assert _foreign(own)[1][0] == "file:///tmp/project"


def test_a_link_in_the_print_style_carries_no_control_character():
    """`print(..., style=...)` applies its style to the segments after `render`, so the link
    in it never reached the segment cleaning: an ST in the target ended the hyperlink early
    and the OSC after it set the title. VAF's own `file://` link in a style still goes out.
    MUTATION: drop the `print` override - red."""
    from rich.style import Style

    for style in (Style(link="http://a/\x1b\\\x1b]0;PWN\x07"), "bold link http://a/\x07"):
        out = _rendered({"force_terminal": True, "color_system": "truecolor"}, "click",
                        style=style)
        outside, targets = _foreign(out)
        assert not outside and not [t for t in targets if _unsafe_in(t)], (style, out)
        assert "click" in out and "PWN" not in out
    own = _rendered({"force_terminal": True, "color_system": "truecolor"}, "project",
                    style=Style(link="file:///tmp/project"))
    assert _foreign(own)[1][0] == "file:///tmp/project"


def test_a_segment_that_was_only_a_control_character_is_dropped():
    """An empty text segment is invalid to Textual (its debug check warns), so a segment that
    held nothing but removed characters is not yielded. MUTATION: flush a held segment whose
    text is empty and whose pad is 0 - red."""
    from rich.segment import Segment
    from rich.style import Style

    from vaf.core.terminal_text import terminal_segments

    assert [s.text for s in terminal_segments([Segment("\x00")])] == []
    assert [s.text for s in terminal_segments([Segment("ab"),
                                               Segment("\x7f", Style(bold=True))])] == ["ab"]
    assert [s.text for s in terminal_segments([Segment("\x00"), Segment("ab")])] == ["ab"]


def test_a_link_target_with_a_newline_or_a_tab_is_dropped():
    """Newline and tab are fine in text, but inside an OSC 8 target Rich and Textual write
    them raw into the sequence. MUTATION: let `_hostile_link` look at unsafe characters
    only - red."""
    from rich.style import Style
    from rich.text import Text

    for target in ("http://a/\nx", "http://a/\tx"):
        out = _rendered({"force_terminal": True, "color_system": "truecolor"},
                        Text("hi", style=Style(link=target)), end="")
        assert "\x1b]8;" not in out and "\n" not in out and "\t" not in out, repr(out)


@pytest.mark.parametrize("segments, shown", [
    # text, a sequence, then a newline in the same segment: the width comes back before
    # the newline, not at the start of the next line
    (["a\x1b[31m\nb"], ["a    ", "\n", "b"]),
    # a sequence that begins after a newline is matched with the rest of its own line
    (["ab\n\x1b[", "31mc\n"], ["ab", "\n", "c    ", "\n"]),
    # a newline in the middle of a would-be string sequence ends it there
    (["a\x1b]0;x\n\x07b"], ["a0;x ", "\n", "b"]),
])
def test_a_segment_is_split_at_its_newlines(segments, shown):
    """MUTATION: flush the line after a segment that holds a newline, unsplit - red."""
    from rich.segment import Segment

    from vaf.core.terminal_text import terminal_segments

    assert [s.text for s in terminal_segments([Segment(t) for t in segments])] == shown


def test_the_console_still_takes_the_rich_console_arguments():
    from rich.console import Console

    assert issubclass(TerminalConsole, Console)
    out = _rendered({"force_terminal": False, "color_system": None, "highlight": False},
                    "ä م \U0001F468\u200d\U0001F469")
    assert "ä م \U0001F468\u200d\U0001F469" in out


# ── the full-screen app ─────────────────────────────────────────────────────

async def _settle(pilot, pred, timeout=6.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await pilot.pause()
        if pred():
            return True
        await asyncio.sleep(0.05)
    return False


def test_the_app_shows_no_control_character_in_any_widget_toast_or_overlay(smoke_app):
    """Every transcript widget, a toast and a modal, with the probe text as message, speaker,
    badge, tool name, arguments and output; a tool's coloured output; and two link styles a
    sender can plant (a Rich renderable the drain mounts). Scanned twice: each widget's own
    rendered lines (scrolled out or not), segment texts and link targets, and the
    compositor's full update of the screen on top.
    MUTATION: delete `VafApp.get_line_filters`, let the filter return its segments
    unchanged, or drop the link handling in `terminal_segments` - red."""
    from rich.style import Style
    from rich.text import Text
    from textual.geometry import Region

    from vaf.cli.tui_app.screens import ConfirmScreen
    from vaf.cli.tui_app.widgets import (AgentMessage, EventNote, PeerMessage, RenderableNote,
                                         SystemNote, ToolCard, UserMessage, WakeMessage)
    app, bridge = smoke_app.app, smoke_app.bridge
    found = {}

    async def _drive():
        async with app.run_test(size=(220, 140)) as pilot:
            transcript = app.query_one("#transcript")
            card = ToolCard("tool " + HOSTILE, HOSTILE)
            colours = ToolCard("ls", "--color")
            await transcript.mount_all([
                UserMessage(HOSTILE), PeerMessage(HOSTILE, HOSTILE, badge=HOSTILE),
                AgentMessage(static_text=HOSTILE), SystemNote(HOSTILE),
                EventNote(HOSTILE, HOSTILE), WakeMessage(HOSTILE), card, colours,
                RenderableNote(Text("LINKB", style=Style(link=LINK_TARGET))),
                RenderableNote(Text.from_markup(LINK_MARKUP))])
            card.set_output(HOSTILE)
            card.finish(True, "1s")
            colours.set_output("\x1b[31mredfile\x1b[0m \x1b[1;34mbluedir\x1b[0m")
            colours.finish(True, "1s")
            colours.on_click(None)          # unfold its output
            app.notify(HOSTILE)
            await app.push_screen(ConfirmScreen(HOSTILE))
            assert await _settle(pilot, lambda: isinstance(app.screen, ConfirmScreen))
            await asyncio.sleep(0.2)
            await pilot.pause()

            widgets = 0
            for screen in (app.screen_stack[0], app.screen):
                for widget in screen.walk_children(with_self=True):
                    width, height = widget.size
                    if not (width and height):
                        continue
                    widgets += 1
                    for strip in widget.render_lines(Region(0, 0, width, height)):
                        for seg in strip:
                            if seg.control:
                                continue
                            bad = set(_unsafe_in(seg.text))
                            link = seg.style.link if seg.style is not None else None
                            if link and _unsafe_in(link):
                                bad.add("link:" + ",".join(_unsafe_in(link)))
                            if bad:
                                found.setdefault(type(widget).__name__, set()).update(bad)
            assert widgets > 20, "the scan reached no widgets"

            update = app.screen._compositor.render_full_update()
            screen_text = "".join(seg.text for line in update.strips for strip in line
                                  for seg in strip if not seg.control)
            if _unsafe_in(screen_text):
                found["compositor"] = set(_unsafe_in(screen_text))
            found["_visible"] = (all(word in screen_text for word in VISIBLE),
                                 "redfile" in screen_text and "bluedir" in screen_text,
                                 "[31m" not in screen_text and "[0m" not in screen_text)

    asyncio.run(_drive())
    bridge.shutdown()
    visible = found.pop("_visible")
    assert not found, f"control characters reached the screen: {found}"
    assert visible == (True, True, True), visible


def test_the_line_filter_keeps_the_width_textual_laid_out():
    """A strip that lost the printable rest of a sequence is padded to its cell length.
    MUTATION: drop the padding in `terminal_segments` - red."""
    from rich.cells import cell_len
    from rich.segment import Segment
    from rich.style import Style
    from textual.color import Color
    from textual.strip import Strip

    from vaf.cli.tui_app.app import TerminalTextFilter

    raw = "a\x1b[31mred\x1b[0m b"
    strip = Strip([Segment(raw, Style())], cell_len(raw)).apply_filter(
        TerminalTextFilter(), Color(0, 0, 0))
    text = "".join(seg.text for seg in strip)
    assert text.startswith("ared b") and cell_len(text) == cell_len(raw)


# ── vaf a2a ─────────────────────────────────────────────────────────────────

def test_a2a_read_log_and_export_print_a_peer_text_without_control_characters(rooms):
    """A room peer, possibly on another machine, wrote the text. MUTATION: `_echo` without
    `terminal_text`, or `_emit` back to plain json.dumps - red."""
    from vaf.cli.cmd import a2a as a2a_cmd

    runner = CliRunner()
    room_id = _lines(runner.invoke(a2a_cmd.app, ["create"]))[0]["room"]
    room, guest = _guest(room_id, rooms, display="Peer")
    room.say(guest, HOSTILE)

    read = runner.invoke(a2a_cmd.app, ["read", room_id, "--all"])
    assert read.exit_code == 0, read.output
    assert not _unsafe_in(read.stdout)
    assert [r["text"] for r in _lines(read) if r.get("kind") == "say"] == [HOSTILE]

    for argv in (["log", room_id], ["export", room_id]):
        result = runner.invoke(a2a_cmd.app, argv)
        assert result.exit_code == 0, (argv, result.output)
        assert not _unsafe_in(result.stdout), (argv, _unsafe_in(result.stdout))
        assert all(word in result.stdout for word in VISIBLE), argv


def test_a2a_export_to_a_file_keeps_the_text_as_stored(rooms, tmp_path):
    """A file is data: `--out` writes what the room holds."""
    from vaf.cli.cmd import a2a as a2a_cmd

    runner = CliRunner()
    room_id = _lines(runner.invoke(a2a_cmd.app, ["create"]))[0]["room"]
    room, guest = _guest(room_id, rooms, display="Peer")
    room.say(guest, HOSTILE)
    target = tmp_path / "room.md"
    assert runner.invoke(a2a_cmd.app, ["export", room_id, "--out", str(target)]).exit_code == 0
    assert HOSTILE in target.read_text(encoding="utf-8")


# ── vaf inbox and vaf outbox --json ─────────────────────────────────────────

def _json_lines_are_clean_and_equal(result, rows):
    assert result.exit_code == 0, result.output
    assert not _unsafe_in(result.stdout), _unsafe_in(result.stdout)
    assert [json.loads(line) for line in result.stdout.splitlines() if line.strip()] == rows


def test_vaf_inbox_json_escapes_a_contacts_name_and_preview(monkeypatch):
    """A contact chose the name and wrote the preview. MUTATION: plain json.dumps in
    inbox.py - red."""
    import vaf.core.inbox as inbox_core
    from vaf.cli.cmd import inbox as inbox_cmd

    rows = [{"id": "c1", "channel": "telegram", "name": "Bob\x9b2J\u202e", "preview": HOSTILE,
             "unread": 1, "waits": False, "waits_reason": "", "mode": "agent", "last_ts": 0}]
    monkeypatch.setattr(inbox_cmd, "_identity", lambda: ("alice", "scope-a"))
    monkeypatch.setattr(inbox_core, "list_conversations", lambda *a, **k: {
        "rows": rows, "counts": {"all": 1, "waits": 0, "unread": 1, "agent": 1}})
    result = CliRunner().invoke(inbox_cmd.app, ["list", "--json"])
    _json_lines_are_clean_and_equal(result, rows)


def test_vaf_outbox_json_escapes_a_held_message(monkeypatch):
    """The held message is model text addressed to somebody else. MUTATION: plain
    json.dumps in outbox.py - red."""
    import vaf.core.outbound_hold as hold
    from vaf.cli.cmd import outbox as outbox_cmd

    rows = [{"id": 7, "tool": "send_email", "to": "bob@example.org\x85", "text": HOSTILE}]
    monkeypatch.setattr(outbox_cmd, "_identity", lambda: ("alice", "scope-a"))
    monkeypatch.setattr(hold, "pending", lambda *a, **k: rows)
    result = CliRunner().invoke(outbox_cmd.app, ["list", "--json"])
    _json_lines_are_clean_and_equal(result, rows)


# ── vaf audit ───────────────────────────────────────────────────────────────

@pytest.fixture
def hostile_audit(monkeypatch, tmp_path):
    """A finished audit whose model-written parts carry the probe text: what a review of
    somebody else's code can produce."""
    import vaf.core.code_audit as ca

    finding = ca.AuditFinding(file="a.py", start_line=1, end_line=1, type="issue",
                              severity="major", category="bug", title=HOSTILE,
                              explanation=HOSTILE, verified=True, id="f1")
    report = ca.AuditReport(root=str(tmp_path), scope="uncommitted", summary=HOSTILE,
                            status_reason=HOSTILE, file_summaries={"a.py": HOSTILE},
                            findings=[finding])
    monkeypatch.setattr(ca, "code_audit", lambda *a, **k: report)
    monkeypatch.setattr(ca, "last_report", lambda *a, **k: report.to_text())
    return report


@pytest.mark.parametrize("fmt", ["text", "prompt", "json"])
def test_vaf_audit_prints_a_model_written_report_without_escape_sequences(hostile_audit,
                                                                         tmp_path, fmt):
    """MUTATION: write the report as it is, in any of the three forms - red."""
    from vaf.cli.cmd import audit as audit_cmd

    result = CliRunner().invoke(audit_cmd.app, ["run", str(tmp_path), "--no-llm",
                                                "--format", fmt])
    assert result.stdout.strip(), result.output
    assert not _unsafe_in(result.stdout), _unsafe_in(result.stdout)
    if fmt == "json":
        assert json.loads(result.stdout) == json.loads(hostile_audit.to_json())
    else:
        assert "xe.fdp" in result.stdout


def test_vaf_audit_show_prints_the_stored_report_cleaned(hostile_audit, tmp_path):
    """MUTATION: write the stored text as it is - red."""
    from vaf.cli.cmd import audit as audit_cmd

    result = CliRunner().invoke(audit_cmd.app, ["show", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert not _unsafe_in(result.stdout) and "xe.fdp" in result.stdout


# ── vaf env and the research progress ───────────────────────────────────────

class _FakeEnvs:
    def list(self, scope, everyone=False):
        return [SimpleNamespace(describe=lambda: "env-1 " + HOSTILE, owner="alice")]

    def processes(self, scope):
        return [{"handle": "p1", "state": "running", "label": "", "command": HOSTILE}]

    def render(self, scope, env_id, target):
        return {"ok": True, "screenshot_b64": base64.b64encode(b"png").decode(),
                "title": HOSTILE, "page_errors": [HOSTILE], "console": [HOSTILE],
                "text": HOSTILE}


@pytest.mark.parametrize("argv", [["list"], ["ps"], ["preview", "env-1", "http://localhost/"]])
def test_vaf_env_prints_agent_and_page_text_without_escape_sequences(monkeypatch, tmp_path,
                                                                    capfd, argv):
    """Names and commands the agent chose and a page's title, console and text. `exec` and
    `logs` stay byte for byte (named boundary). MUTATION: drop `terminal_text` at the list,
    ps or preview print - red."""
    from vaf.cli.cmd import env as env_cmd

    monkeypatch.setattr(env_cmd, "_manager", lambda: _FakeEnvs())
    monkeypatch.setattr(env_cmd, "_scope", lambda: "scope-a")
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(env_cmd.app, argv)
    out = result.stdout + capfd.readouterr().out
    assert result.exit_code == 0, result.output
    assert not _unsafe_in(out), _unsafe_in(out)
    assert "xe.fdp" in out


def test_the_research_progress_line_carries_no_escape_sequence():
    """The section titles are the model's. MUTATION: build the line without `terminal_text`,
    or print the message directly again - red."""
    import vaf.tools.research_agent as research

    line = research._progress_line("[1/3] " + HOSTILE)
    assert line.startswith("[Research] [1/3] ") and not _unsafe_in(line)
    source = inspect.getsource(research)
    assert source.count("print(_progress_line(message), flush=True)") == 2
    assert 'print(f"[Research] {message}"' not in source


# ── vaf run -p ──────────────────────────────────────────────────────────────

class _HostileAgent:
    """The one-turn lane's view of an agent whose answer is somebody else's steering."""

    def __init__(self):
        self.api_backend = object()
        self.llm = None
        self.use_server = False
        self.history = []
        self.config = {"model": "m"}
        self.last_turn = SimpleNamespace(failed=False)

    def init_chat(self):
        pass

    def set_event_sink(self, sink):
        pass

    def chat_step(self, prompt, stream_callback=None, **kwargs):
        if stream_callback:
            stream_callback(HOSTILE)
        return HOSTILE


@pytest.fixture
def hostile_prompt(monkeypatch, tmp_path):
    import vaf.cli.cmd.run as run_mod
    import vaf.cli.ui as ui_mod
    from vaf.core.platform import Platform

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    monkeypatch.setenv("VAF_NONINTERACTIVE", "1")
    # The machine formats silence the UI by assigning to the class: restored after the test.
    for name in ("event", "error", "warning", "success", "info"):
        monkeypatch.setattr(ui_mod.UI, name, getattr(ui_mod.UI, name))
    monkeypatch.setattr(run_mod, "_cleanup_before_exit", lambda: None)
    monkeypatch.setattr(run_mod, "_make_cli_agent",
                        lambda verbose=False, host_audio=False: _HostileAgent())
    return run_mod


@pytest.mark.parametrize("fmt", ["text", "json", "stream-json"])
def test_vaf_run_p_prints_a_steered_answer_without_control_characters(hostile_prompt, capsys,
                                                                      fmt):
    """MUTATION: revert the text print, the json print or the stream-json emitter to the
    raw text or plain json.dumps - red."""
    import typer

    if fmt == "stream-json":
        hostile_prompt.prompt_once("q", fmt)
    else:
        with pytest.raises(typer.Exit):
            hostile_prompt.prompt_once("q", fmt)
    out = capsys.readouterr().out
    assert not _unsafe_in(out), _unsafe_in(out)
    if fmt == "text":
        assert all(word in out for word in VISIBLE)
    elif fmt == "json":
        assert json.loads(out) == {"ok": True, "output": HOSTILE}
    else:
        events = [json.loads(line) for line in out.splitlines() if line.strip()]
        assert [e["text"] for e in events if e["type"] == "text_delta"] == [HOSTILE]


# ── the sub-agent terminal and the modern toolbar ───────────────────────────

def test_the_sub_agent_terminal_prints_a_result_without_control_characters(capsys):
    """MUTATION: print the arguments as they are - red."""
    from vaf.cli.cmd.subagent import _safe_print

    _safe_print(HOSTILE, 42, None, sep=" | ")
    out = capsys.readouterr().out
    assert not _unsafe_in(out)
    assert out.endswith(" | 42 | None\n") and "xe.fdp" in out


def test_the_modern_toolbar_shows_a_task_name_without_control_characters(monkeypatch):
    """The toolbar status carries workflow names; the old cleaning removed SGR codes only.
    MUTATION: drop `terminal_text` from `_get_live_toolbar` - red."""
    import prompt_toolkit.patch_stdout as patch_mod

    import vaf.cli.tui as tui_mod
    import vaf.core.subagent_ipc as ipc_mod

    captured = {}

    class _Session:
        def __init__(self, *a, **k):
            self.app = None

        def prompt(self, *a, **kwargs):
            captured["rprompt"] = kwargs["rprompt"]
            return "typed"

    task = SimpleNamespace(agent_type="workflow:" + HOSTILE, task_id="ab12cd34ef",
                           created_at=datetime.now().isoformat())
    ipc = SimpleNamespace(get_active_tasks=lambda session_id=None: [task],
                          get_all_paused_workflows=lambda: [],
                          get_pending_results=lambda session_id=None: [])
    monkeypatch.setattr(ipc_mod, "get_ipc", lambda: ipc)
    monkeypatch.setattr(ipc_mod, "get_current_session_id", lambda: "s-1")
    monkeypatch.setattr(tui_mod, "PromptSession", _Session)
    monkeypatch.setattr(tui_mod, "FileHistory", lambda path: None)
    monkeypatch.setattr(tui_mod, "create_input", lambda: None)
    monkeypatch.setattr(tui_mod, "create_output", lambda: None)
    monkeypatch.setattr(patch_mod, "patch_stdout", lambda *a, **k: _NullContext())

    tui = tui_mod.TUI()
    monkeypatch.setattr(tui, "_print_input_header", lambda prompt: None)
    assert tui.input_box() == "typed"
    shown = "".join(text for _style, text in captured["rprompt"]())
    assert "Workflow:" in shown and "xe.fdp" in shown
    assert not _unsafe_in(shown), _unsafe_in(shown)


class _NullContext:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ── the engine's own print and the log lines ────────────────────────────────

def test_the_engine_prints_a_chunk_without_control_characters_when_nobody_streams():
    """Reaching the fallback needs a live backend, so its line is pinned by source.
    MUTATION: drop `terminal_text` from the fallback print - red."""
    import vaf.core.agent as agent_mod

    source = inspect.getsource(agent_mod)
    assert "print(escape(terminal_text(chunk)), end=\"\")" in source
    assert "print(escape(chunk)" not in source


def _cli_process(body: str) -> subprocess.CompletedProcess:
    """`body` run after the CLI entry point set the process up (`vaf.main.main` with a
    command that does nothing), in a fresh interpreter: the logging setup is per process."""
    code = ("import logging\n"
            "import vaf.main as entry\n"
            "entry.app = lambda: None\n"
            "entry.main()\n" + body)
    env = dict(os.environ, PYTHONPATH=str(ROOT), VAF_SKIP_DEP_CHECK="1")
    return subprocess.run([sys.executable, "-c", code], capture_output=True, env=env,
                          timeout=300)


def _unsafe_bytes(data: bytes) -> list:
    return sorted({hex(b) for b in data if (b < 0x20 and b not in (9, 10)) or 0x7F <= b <= 0x9F})


def test_a_library_that_configures_logging_on_import_gets_no_raw_stderr():
    """`logging.basicConfig` (and browser_use on import) configure logging only when the
    root logger has no handler. The CLI entry point puts a cleaning one there first.
    MUTATION: drop `install_terminal_logging()` from `vaf.main.main` - red."""
    proc = _cli_process(
        "logging.basicConfig(level=logging.INFO)\n"
        "logging.getLogger('lib').info('INFO line \\x1b]0;PWN\\x07')\n"
        "logging.getLogger('lib').warning('WARN \\x1b]0;PWN\\x07 \\x9b2J end')\n")
    assert proc.returncode == 0, proc.stderr[-800:]
    assert not _unsafe_bytes(proc.stderr), _unsafe_bytes(proc.stderr)
    assert b"WARN" in proc.stderr and b"end" in proc.stderr
    assert b"INFO line" not in proc.stderr, "the INFO level is not what the terminal showed"


def test_a_lane_with_its_own_root_handler_keeps_its_output():
    """The tray adds its log file to the root logger; the stderr handler then stays quiet,
    exactly as Python's last-resort handler did. MUTATION: drop `TerminalStderrHandler.handle`
    - red."""
    proc = _cli_process(
        "import io\n"
        "logging.getLogger().addHandler(logging.StreamHandler(io.StringIO()))\n"
        "logging.getLogger('lib').warning('only in the lane log')\n")
    assert proc.returncode == 0, proc.stderr[-800:]
    assert b"only in the lane log" not in proc.stderr


def test_a_step_handler_cannot_be_built_without_its_run():
    """A handler without a run took every record logged outside any run, from a thread with
    no run context, which may be another person's line. MUTATION: give `run` a default of
    None again - red."""
    import logging
    import queue

    from vaf.tools.browser_agent import _BrowserStepLogger

    with pytest.raises(TypeError):
        _BrowserStepLogger(queue.Queue())
    q = queue.Queue()
    handler = _BrowserStepLogger(q, object())
    record = logging.LogRecord("browser_use.agent", logging.INFO, __file__, 1,
                               "a line of no run", None, None)
    handler.handle(record)
    assert q.empty()


@pytest.fixture
def browser_use_logger():
    """The process-wide `browser_use` logger, put back as it was after the test: the step
    capture changes its level, its propagation and its handlers for the whole process."""
    import logging

    logger = logging.getLogger("browser_use")
    saved = (logger.level, logger.propagate, list(logger.handlers))
    yield logger
    logger.setLevel(saved[0])
    logger.propagate = saved[1]
    logger.handlers[:] = saved[2]


def test_a_runs_step_lines_reach_its_window_and_not_the_tray_log(browser_use_logger):
    """With debug logs on, the tray puts its log file on the root logger and browser-use
    leaves its own logging alone. The capture needs the logger at INFO, and a propagating
    logger at INFO handed every account's "Next goal" and action lines to that shared file
    (measured: absent at the base of this branch, present before this fix). The step line
    reaches the run's window only; a WARNING still reaches the root's handlers once, as it
    did with the logger at WARNING. MUTATION: keep the logger propagating - red (the INFO
    line in the file); drop the relay - red (no WARNING in the file)."""
    import logging
    import queue

    from vaf.tools.browser_agent import _attach_step_logger

    stream = io.StringIO()
    tray_log = logging.StreamHandler(stream)
    tray_log.setLevel(logging.DEBUG)
    root = logging.getLogger()
    root.addHandler(tray_log)
    try:
        q = queue.Queue()
        handler = _attach_step_logger(q)
        log = logging.getLogger("browser_use.agent")
        log.info("Next goal: the info line")
        log.warning("the warning line")
        browser_use_logger.removeHandler(handler)
    finally:
        root.removeHandler(tray_log)
    assert list(q.queue) == ["Next goal: the info line", "the warning line"]
    assert "the info line" not in stream.getvalue()
    assert stream.getvalue().count("the warning line") == 1


def test_two_overlapping_browser_runs_each_capture_only_their_own_steps(browser_use_logger):
    """Every run's step handler hangs on the one process-wide `browser_use` logger, and each
    run's window shows what its queue holds. Two runs that overlap in one process (the
    per-user pool, a server for several accounts) each received the other's step lines.
    Here two runs on their own loops, the way the tool runs them, log while both are
    attached, from their task and from `asyncio.to_thread`; a record from a thread of no
    run is logged in between. MUTATION: drop `_BrowserStepLogger.filter` - red."""
    import logging
    import queue
    import threading

    from vaf.core.bounded_run import run_coroutine_on_new_loop
    from vaf.tools.browser_agent import _attach_step_logger

    bu_logger = browser_use_logger
    log = logging.getLogger("browser_use.agent")
    attached, logged = threading.Barrier(3, timeout=10), threading.Barrier(3, timeout=10)
    captured = {}

    async def run(name):
        q = queue.Queue()
        handler = _attach_step_logger(q)
        try:
            attached.wait()

            async def step():
                log.info("Next goal of %s", name)
            await asyncio.create_task(step())
            await asyncio.to_thread(log.info, "action of %s", name)
            logged.wait()
        finally:
            bu_logger.removeHandler(handler)
        captured[name] = list(q.queue)

    runs = [threading.Thread(target=run_coroutine_on_new_loop, args=(run(name),))
            for name in ("alice", "bob")]
    for thread in runs:
        thread.start()
    attached.wait()
    log.info("a line of no run")
    logged.wait()
    for thread in runs:
        thread.join(10)
    assert captured == {"alice": ["Next goal of alice", "action of alice"],
                        "bob": ["Next goal of bob", "action of bob"]}, captured


def test_browser_use_leaves_the_logging_alone_and_its_steps_still_reach_vaf():
    """On import browser_use installed a raw stderr handler at INFO; its step lines carry the
    model's next goal, steered by the page. Now its WARNINGs reach stderr cleaned, and its
    INFO steps still reach the browser window's step capture.
    MUTATION: drop the level in `_attach_step_logger` - red (no step captured)."""
    pytest.importorskip("browser_use", exc_type=ImportError)   # an optional extra
    proc = _cli_process(
        "import queue\n"
        "import browser_use  # noqa: F401\n"
        "from vaf.tools.browser_agent import _attach_step_logger\n"
        "q = queue.Queue()\n"
        "_attach_step_logger(q)\n"
        "log = logging.getLogger('browser_use.agent')\n"
        "log.info('Next goal: \\x1b]0;PWN\\x07 \\x9b2J')\n"
        "log.warning('W goal \\x1b]0;PWN\\x07 end')\n"
        "print('captured', q.qsize())\n")
    assert proc.returncode == 0, proc.stderr[-800:]
    assert not _unsafe_bytes(proc.stderr), _unsafe_bytes(proc.stderr)
    assert b"W goal" in proc.stderr
    assert b"captured 2" in proc.stdout, proc.stdout[-300:]


def test_the_last_resort_log_handler_writes_no_control_characters():
    """MUTATION: drop the last-resort formatter from `install_terminal_logging` - red."""
    import logging

    from vaf.core.terminal_text import TerminalLogFormatter, install_terminal_logging

    previous = logging.lastResort.formatter
    root = logging.getLogger()
    handlers = list(root.handlers)
    try:
        install_terminal_logging()
        assert isinstance(logging.lastResort.formatter, TerminalLogFormatter)
        record = logging.LogRecord("vaf.test", logging.WARNING, __file__, 1,
                                   "mail from %s: %s", ("Bob\x1b]0;T\x07", "\x9b2J ok"), None)
        assert logging.lastResort.format(record) == "mail from Bob:  ok"
    finally:
        logging.lastResort.setFormatter(previous)
        root.handlers[:] = handlers
