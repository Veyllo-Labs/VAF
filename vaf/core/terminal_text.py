# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Text from other parties, as a terminal may show it: without its control characters.

A terminal obeys what it is sent. ESC ] 0 sets the window title, ESC [ 2J clears the
screen, ESC c resets the terminal, OSC 8 plants a link and OSC 52 writes the clipboard where
the emulator allows it; the C1 bytes 0x9B and 0x9D start the same sequences in one byte. Text
that somebody else controls reaches VAF's terminals all the time: a room peer, the Veyllo
App, a mail or messenger contact, a sub-agent's result, and the model itself, whose output a
web page or a mail can steer. Rich and Textual remove only BEL, BS, VT, FF and CR from what
they print; measured with Textual 8.2.8 and Rich 14.3.3, the other cases (both title and
clear sequences, OSC 8, OSC 52, the C1 forms, NUL, DEL, U+202E) reached the output of every
transcript widget and of `Console.print` with `str`, `Markdown`, `Panel` and `Table`.

What is removed (`terminal_text`), in two steps. First every COMPLETE escape sequence as a
whole: a CSI (ESC [ or 0x9B, parameters, one final byte), a string sequence (OSC, DCS, SOS, PM,
APC: ESC ] P X ^ _ or their C1 bytes, up to BEL or ST), and every other escape sequence (ESC,
intermediates, one final byte, such as ESC c or ESC ( B). So a colour code from a program's
output leaves no `[31m` behind and a title sequence leaves no `]0;...`. A string sequence is
recognised only up to 2048 characters and never across a newline, so an unterminated one
cannot swallow the text after it. Then every remaining unsafe character alone: every C0
character except tab and newline (CR included, which both libraries drop anyway), DEL,
every C1 character, the twelve bidirectional controls (U+061C, U+200E, U+200F,
U+202A-U+202E, U+2066-U+2069), which reorder how a line reads on a bidi-capable terminal
(`abc<RLO>xe.fdp` shows as a different file name), and lone surrogates (U+D800-U+DFFF; a
Python str never holds a valid pair, and under a surrogateescape stream U+DC80-U+DC9F are
written as the raw C1 bytes). A malformed or unterminated sequence therefore loses its
control characters and shows the rest as text.

Named boundary: zero-width and other format characters stay (U+200B, ZWJ U+200D, ZWNJ,
U+2060, U+FEFF). They drive no terminal, ZWJ is part of emoji sequences and of Persian and
Indic text, and look-alike deception on a decision surface is `vaf.core.shown_text`'s job,
not this module's.

THE SINKS. A filter per call site cannot be complete: the full-screen app alone has more than
110 entry points (`Static(` 73 times, `.update(` 23, `notify(` 20), so the filter sits where
each renderer writes:

1. Textual: one line filter on the app (`vaf.cli.tui_app.app.TerminalTextFilter`, through
   `App.get_line_filters`), applied to every strip of every widget, toast and overlay.
2. Rich: `TerminalConsole`, which every console in `vaf/` is (pinned by an AST scan in
   tests/test_terminal_text.py). Its strings are cleaned before layout (`render_str`), and
   every rendered segment again.
   Both use `terminal_segments`, which also drops a style's LINK target when it holds an
   unsafe character (Rich and Textual write that target raw into the OSC 8 sequence), and
   pads a segment that lost the printable rest of a sequence with spaces, so a line keeps
   its width and a panel its border. Rich's and Textual's own cursor, clear and colour codes
   are control segments or styles and pass.
3. Bare stdout: `vaf a2a`, `vaf run -p`, `vaf audit`, `vaf env` (but exec and logs), the
   sub-agent terminal, the research progress lines and the engine's own no-callback print
   call `terminal_text`; the JSON lines (`vaf a2a`, `vaf inbox --json`, `vaf outbox --json`,
   `vaf run -p` json and stream-json, `vaf audit --format json`) go through `terminal_json`,
   whose output parses back to the identical value.
4. Logging: `install_terminal_logging`, called by the CLI entry point (`vaf.main.main`),
   puts a stderr handler with `TerminalLogFormatter` on an unconfigured root logger and
   the same formatter on Python's last-resort handler. A library that configures logging
   only when the root logger has no handler (browser_use on import, `logging.basicConfig`)
   then leaves it alone instead of writing every record raw.

Named boundaries: the Rich console class and `terminal_json` stay internal; the facade's own
examples print with `print`, which is what `vaf.terminal_text` serves, and no embedder lane
has shown a Rich console or a JSON writer of its own. Log records written before
`vaf.main.main` runs (the dependency bootstrap, module imports) and in a process that does
not start through it are formatted by whatever handler Python has then. The engine's other
`rich.print` lines (the process scan, the server stop) print VAF's own strings and local
command lines. `vaf env exec` and `vaf env logs` print a container program's output byte for
byte, as `docker exec` and `docker logs` do. Rich MARKUP in other parties' text is a
different class: `[link=...]` with clean characters in text printed with markup on still
becomes an OSC 8 link whose label and target the sender chose, and separating VAF's own
markup from model text there is a design change of its own.
"""
import json
import logging
import re
import sys
from typing import Any, Iterable, Iterator, Optional

from rich.cells import cell_len
from rich.console import Console, ConsoleOptions, RenderableType
from rich.segment import Segment
from rich.text import Text

#: The bidirectional marks, embeddings, overrides and isolates: they reorder how a line reads.
_BIDI_CONTROLS = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e"
                           "\u2066\u2067\u2068\u2069")

_UNSAFE_CODES = ([code for code in range(0x00, 0x20) if chr(code) not in "\n\t"]
                 + list(range(0x7F, 0xA0))
                 + sorted(ord(ch) for ch in _BIDI_CONTROLS)
                 + list(range(0xD800, 0xE000)))

_REMOVE = dict.fromkeys(_UNSAFE_CODES)


def _char_class(codes) -> str:
    """A regex character class of `codes`, as ranges, from the same list `_REMOVE` holds."""
    codes = sorted(codes)
    parts, start = [], codes[0]
    for prev, code in zip(codes, codes[1:] + [None]):
        if code != prev + 1:
            parts.append(re.escape(chr(start)) if start == prev
                         else re.escape(chr(start)) + "-" + re.escape(chr(prev)))
            start = code
    return "[" + "".join(parts) + "]"


_UNSAFE_RE = re.compile(_char_class(_UNSAFE_CODES))

#: The longest string sequence (OSC and its kin) removed as a whole.
_STRING_SEQUENCE_MAX = 2048

#: A complete escape sequence: CSI, a string sequence ended by BEL or ST, or any other
#: escape sequence (ESC, intermediates, one final byte). Bounded, so the search stays linear.
#: `vaf.core.web_ticker.ANSI_RE` is a narrower copy kept on purpose: that module is stdlib-only
#: so that vaf.cli, vaf.tools and vaf.core.platform can import it without a cycle.
ESCAPE_SEQUENCE_RE = re.compile(
    "(?:\x1b\\[|\x9b)[\x30-\x3f]{0,64}[\x20-\x2f]{0,8}[\x40-\x7e]"
    "|(?:\x1b[\\]PX^_]|[\x90\x98\x9d\x9e\x9f])[^\x07\x1b\x9c\n]{0,%d}(?:\x07|\x1b\\\\|\x9c)"
    "|\x1b[\x20-\x2f]{0,8}[\x30-\x7e]" % _STRING_SEQUENCE_MAX)


def _clean(text: str) -> str:
    return ESCAPE_SEQUENCE_RE.sub("", text).translate(_REMOVE)


def terminal_text(value: Any) -> str:
    """`value` as a terminal may show it: its escape sequences and control characters removed.

    A complete escape sequence goes as a whole (a colour code, a title or clipboard sequence,
    a terminal reset); then every remaining C0 character except tab and newline, DEL, every
    C1 character, the twelve bidirectional controls and any lone surrogate. Everything else
    is kept as it is, including umlauts, Arabic, CJK, emoji with ZWJ and zero-width spaces.
    A malformed or unterminated sequence loses its control characters and the rest shows as
    text, so nothing a parser could misread decides how much text disappears. `None` gives
    `""`; anything else is `str()`-ed. Never raises for a value whose `str()` does not.
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    if not _UNSAFE_RE.search(text):
        return text
    return _clean(text)


def _json_escape(match: "re.Match[str]") -> str:
    return "\\u%04x" % ord(match.group(0))


def terminal_json(obj: Any, *, indent: Optional[int] = None) -> str:
    """One JSON document for a terminal: `json.dumps(obj, ensure_ascii=False, indent=indent)`
    with every unsafe character json.dumps leaves raw (DEL, C1, the bidirectional controls,
    lone surrogates) written as a `\\uXXXX` escape. Those characters can only stand inside a
    JSON string, where the escape parses back to the same character, so `json.loads` of the
    result equals `json.loads(json.dumps(obj))`. Non-ASCII text stays readable, as before."""
    line = json.dumps(obj, ensure_ascii=False, indent=indent)
    if not _UNSAFE_RE.search(line):
        return line
    return _UNSAFE_RE.sub(_json_escape, line)


def terminal_segments(segments: Iterable[Segment]) -> Iterator[Segment]:
    """Rendered segments as a terminal may receive them.

    A segment's text is cleaned like `terminal_text`; when a whole sequence took printable
    characters with it, the segment is padded with spaces to its old cell width, so the line
    keeps its layout (the removed control characters themselves are zero-width). A style
    whose LINK target holds an unsafe character loses the link: Rich and Textual write the
    target raw into the OSC 8 sequence, and a URL never legitimately holds one. A control
    segment (the renderer's own cursor, clear and title codes) passes unchanged; a segment
    left with no text is dropped, since an empty text segment is invalid to Textual. Shared
    by `TerminalConsole` and the full-screen app's line filter, so both renderers apply one
    definition."""
    search = _UNSAFE_RE.search
    for segment in segments:
        if segment.control:
            yield segment
            continue
        text, style = segment.text, segment.style
        link = style.link if style is not None else None
        hostile_link = bool(link) and search(link) is not None
        hostile_text = bool(text) and search(text) is not None
        if not (hostile_link or hostile_text):
            yield segment
            continue
        if hostile_link:
            style = style.update_link(None)
        if hostile_text:
            cleaned = _clean(text)
            missing = cell_len(text) - cell_len(cleaned)
            text = cleaned + " " * missing if missing > 0 else cleaned
        if text:
            yield Segment(text, style, None)


class TerminalConsole(Console):
    """A Rich console that removes escape sequences and control characters from what it
    prints.

    Strings are cleaned before Rich lays them out (`render_str`, which `print`, `log`,
    `input` and every renderable built from a string go through), so a removed sequence
    never leaves a gap. Every rendered segment is cleaned again (`render`, which every output
    path renders through), which covers `Text` objects, Markdown and link targets. Rich's own
    control codes (`Control.clear()`, cursor moves) are control segments and pass; its colours
    and VAF's own `[link=file://...]` come from styles and are untouched. A CR in `end` is
    removed like any other, which no caller relies on. Constructed exactly like
    `rich.console.Console`."""

    def render_str(self, text: str, **kwargs: Any) -> Text:
        return super().render_str(terminal_text(text), **kwargs)

    def render(self, renderable: RenderableType,
               options: Optional[ConsoleOptions] = None) -> Iterable[Segment]:
        return terminal_segments(super().render(renderable, options))


class TerminalLogFormatter(logging.Formatter):
    """A log formatter whose lines carry no escape sequences or control characters. A log
    record's arguments are often somebody else's text (a sender, a subject, a URL, the
    browser agent's next goal)."""

    def format(self, record: logging.LogRecord) -> str:
        return terminal_text(super().format(record))


class TerminalStderrHandler(logging.StreamHandler):
    """WARNING and above to the CURRENT `sys.stderr`, cleaned, while nothing else handles
    the root logger: the job of Python's last-resort handler, kept as a real handler.

    A real handler on the root logger is what keeps a library that configures logging only
    when the root logger has none (browser_use on import, `logging.basicConfig`) from
    installing a raw stderr handler of its own. It writes only while it is the root
    logger's only handler, so a lane that adds its own (the tray's log file) gets the same
    output as before, and it resolves `sys.stderr` on every record like `logging.lastResort`,
    so a full-screen app that captures stderr still captures these lines."""

    def __init__(self, level: int = logging.WARNING) -> None:
        logging.Handler.__init__(self, level)
        self.setFormatter(TerminalLogFormatter())

    @property
    def stream(self):  # type: ignore[override]
        return sys.stderr

    def handle(self, record: logging.LogRecord) -> bool:
        if logging.getLogger().handlers != [self]:
            return False
        return super().handle(record)


def install_terminal_logging() -> None:
    """Clean log lines on this process's terminal. Called by the CLI entry point; idempotent.

    Gives `logging.lastResort` the `TerminalLogFormatter`, and puts a
    `TerminalStderrHandler` on the root logger when it has no handler yet."""
    if logging.lastResort is not None:
        logging.lastResort.setFormatter(TerminalLogFormatter())
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(TerminalStderrHandler())
