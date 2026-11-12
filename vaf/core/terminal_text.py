# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Text from other parties, as a terminal may show it: without its control characters.

A terminal obeys what it is sent. ESC ] 0 sets the window title, ESC [ 2J clears the
screen, OSC 8 plants a link and OSC 52 writes the clipboard where the emulator allows it;
the C1 bytes 0x9B and 0x9D start the same sequences in one byte. Text that somebody else
controls reaches VAF's terminals all the time: a room peer, the Veyllo App, a mail or
messenger contact, a sub-agent's result, and the model itself, whose output a web page or a
mail can steer. Rich and Textual remove only BEL, BS, VT, FF and CR from what they print;
measured with Textual 8.2.8 and Rich 14.3.3, the other cases (both title and clear
sequences, OSC 8, OSC 52, the C1 forms, NUL, DEL, U+202E) reached the output of every
transcript widget and of `Console.print` with `str`, `Markdown`, `Panel` and `Table`.

What is removed (`terminal_text`): every C0 character except tab and newline (CR included,
which both libraries drop anyway), DEL, every C1 character, and the twelve bidirectional
controls (U+061C, U+200E, U+200F, U+202A-U+202E, U+2066-U+2069), which reorder how a line
reads on a bidi-capable terminal (`abc<RLO>xe.fdp` shows as a different file name). Only the
characters go, never the rest of a sequence: `ESC ] 0 ; x BEL` shows as `]0;x`, which is
honest, and no malformed sequence can make a parser skip the wrong amount of text.

Named boundary: zero-width and other format characters stay (U+200B, ZWJ U+200D, ZWNJ,
U+2060, U+FEFF). They drive no terminal, ZWJ is part of emoji sequences and of Persian and
Indic text, and look-alike deception on a decision surface is `vaf.core.shown_text`'s job,
not this module's.

THE SINKS. A filter per call site cannot be complete: the full-screen app alone has more than
110 entry points (`Static(` 73 times, `.update(` 23, `notify(` 20), so the filter sits where
each renderer writes:

1. Textual: one line filter on the app (`vaf.cli.tui_app.app.TerminalTextFilter`, through
   `App.get_line_filters`), applied to every strip of every widget, toast and overlay.
   Removing a character of cell width 0 keeps the strip's layout.
2. Rich: `TerminalConsole`, which every console in `vaf/` is (24 constructions in 9 files,
   pinned by an AST scan in tests/test_terminal_text.py). Rich's own cursor and clear codes
   travel as control segments and its colours and links are rendered from styles, so both
   pass untouched.
3. Bare stdout: `vaf a2a`, `vaf run -p`, the sub-agent terminal and the engine's own
   no-callback print call `terminal_text`; the JSON lines (`vaf a2a`, `vaf inbox --json`,
   `vaf outbox --json`, `vaf run -p` json and stream-json) go through `terminal_json`,
   whose output parses back to the identical value.
4. Python's last-resort log handler gets `TerminalLogFormatter` at the CLI entry point.

Named boundaries: the Rich console class and `terminal_json` stay internal; the facade's own
examples print with `print`, which is what `vaf.terminal_text` serves, and no embedder lane
has shown a Rich console or a JSON writer of its own. The engine's other `rich.print` lines
(the process scan, the server stop) print VAF's own strings and local command lines. Rich
MARKUP in other parties' text is a different class: `[link=...]` in text printed with
markup on becomes an OSC 8 link without any control character in the input, and separating
VAF's own markup from model text there is a design change of its own.
"""
import json
import logging
import re
from typing import Any, Iterable, Iterator, Optional

from rich.console import Console, ConsoleOptions, RenderableType
from rich.segment import Segment

#: The bidirectional marks, embeddings, overrides and isolates: they reorder how a line reads.
_BIDI_CONTROLS = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e"
                           "\u2066\u2067\u2068\u2069")

_UNSAFE = frozenset(
    [chr(code) for code in range(0x00, 0x20) if chr(code) not in "\n\t"]
    + [chr(code) for code in range(0x7F, 0xA0)]
    + sorted(_BIDI_CONTROLS))

_REMOVE = str.maketrans(dict.fromkeys(_UNSAFE))
_UNSAFE_RE = re.compile("[" + "".join(sorted(re.escape(ch) for ch in _UNSAFE)) + "]")


def terminal_text(value: Any) -> str:
    """`value` as a terminal may show it: its control characters removed.

    Removed: every C0 character except tab and newline, DEL, every C1 character and the
    twelve bidirectional controls. Everything else is kept as it is, including umlauts,
    Arabic, CJK, emoji with ZWJ and zero-width spaces, so the visible text is unchanged.
    Only the characters are removed, never the rest of an escape sequence, so a title
    sequence shows as the text it carried. `None` gives `""`; anything else is `str()`-ed.
    Never raises for a value whose `str()` does not.
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    if not _UNSAFE_RE.search(text):
        return text
    return text.translate(_REMOVE)


def _json_escape(match: "re.Match[str]") -> str:
    return "\\u%04x" % ord(match.group(0))


def terminal_json(obj: Any) -> str:
    """One JSON document for a terminal line: `json.dumps(obj, ensure_ascii=False)` with
    every control character json.dumps leaves raw (DEL, C1, the bidirectional controls)
    written as a `\\uXXXX` escape. Those characters can only stand inside a JSON string,
    where the escape parses back to the same character, so `json.loads` of the result
    equals `json.loads(json.dumps(obj))`. Non-ASCII text stays readable, as before."""
    line = json.dumps(obj, ensure_ascii=False)
    if not _UNSAFE_RE.search(line):
        return line
    return _UNSAFE_RE.sub(_json_escape, line)


def terminal_segments(segments: Iterable[Segment]) -> Iterator[Segment]:
    """Rendered segments with the control characters removed from their text.

    A control segment (the renderer's own cursor, clear and title codes) passes unchanged,
    and so does a segment with no text; a segment that is empty once cleaned is dropped,
    since an empty text segment is invalid to Textual. Shared by `TerminalConsole` and the
    full-screen app's line filter, so both renderers apply one definition."""
    search = _UNSAFE_RE.search
    for segment in segments:
        text = segment.text
        if segment.control or not text or not search(text):
            yield segment
            continue
        cleaned = text.translate(_REMOVE)
        if cleaned:
            yield Segment(cleaned, segment.style, segment.control)


class TerminalConsole(Console):
    """A Rich console that removes control characters from every text segment it renders.

    Every Rich output path renders through `Console.render` (print, log, input prompts,
    status and Live updates, tables, panels, Markdown), so overriding it covers all of them
    with one definition. Rich's own control codes (`Control.clear()`, cursor moves) are
    control segments and pass; its colours and OSC 8 links come from styles at write time
    and are untouched. Constructed exactly like `rich.console.Console`."""

    def render(self, renderable: RenderableType,
               options: Optional[ConsoleOptions] = None) -> Iterable[Segment]:
        return terminal_segments(super().render(renderable, options))


class TerminalLogFormatter(logging.Formatter):
    """A log formatter whose lines carry no control characters. A log record's arguments
    are often somebody else's text (a sender, a subject, a URL), and Python's last-resort
    handler writes them to the terminal's stderr unformatted."""

    def format(self, record: logging.LogRecord) -> str:
        return terminal_text(super().format(record))
