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
   tests/test_terminal_text.py). It cleans every rendered line, and a string printed with
   markup OFF already before layout (`render_str`); a string printed with markup ON is
   parsed as it is, because cleaning it first rebuilds tags that `rich.markup.escape` left
   alone (`[` NUL `link=...]`), and gets the markup rules S1 and S2 (RICH MARKUP below).
   Both use `terminal_segments`, which matches sequences on a line's joined text (a renderer
   splits text by style, Rich's highlighter even inside a colour code), drops a style's LINK
   target when it holds an unsafe character (Rich and Textual write that target raw into
   the OSC 8 sequence), and gives the width a run of segments lost back as spaces at the end
   of the run, so a line keeps its width and a panel its border. Rich's and Textual's own
   cursor, clear and colour codes are control segments or styles and pass.
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

Named boundaries: the Rich console class, `escape_markup` and `terminal_json` stay internal;
the facade's own examples print with `print`, which is what `vaf.terminal_text` serves, no
embedder lane has shown a Rich console or a JSON writer of its own, and an embedder who
prints through Rich has Rich's own `rich.markup.escape` and `markup=False` (docs/EMBEDDING.md
names them). `own_links_as_labels` is on the facade: VAF's own answers carry its file link
(a workflow's `run()` answer, an inline coder result the embedder's model repeats), and
VAF's own lanes call it at twelve sites in seven modules (RICH MARKUP below lists them). Log
records written before
`vaf.main.main` runs (the dependency bootstrap, module imports) and in a process that does
not start through it are formatted by whatever handler Python has then. The cleaning covers
the records that reach the root logger while VAF's handler is its only handler: a handler a
library attaches to its own logger (with `propagate=False`, or beside VAF's), a root
handler a library adds later (which also silences VAF's, by design), and
`logging.basicConfig(force=True)` write raw. The engine's other
`rich.print` lines (the process scan, the server stop) print VAF's own strings and local
command lines. `vaf env exec` and `vaf env logs` print a container program's output byte for
byte, as `docker exec` and `docker logs` do.

RICH MARKUP. Rich reads `[...]` as markup in every string a console prints with markup on,
which is every console's default: `[link=https://...]label[/link]` from another party became
an OSC 8 link whose address the reader never saw, `[conceal]` hid text, `[i]` in `arr[i]`
vanished and italicised the rest, and a lone closing tag (`[/]`, `[/link]`, `[/etc/hosts]`)
raised `MarkupError` out of `print`. Three parts, from the narrowest to the widest:

1. Where other parties' text enters a markup string (the narration lines of `vaf.cli.tui`,
   sub-agent results, the inbox, session, automation and security commands, `vaf git`'s
   commit lists, `vaf workflow list`, the coder's and research agent's live views), it goes
   through `escape_markup`: Rich's own escape, except that VAF's own file link (below) stays
   a link. A table cell takes it as a `Text` instead (see `escape_markup`).
2. Where whole replies are printed (the classic and modern reply stream, `vaf debug`'s
   answers, `vaf git`'s suggested commit message, the automation output, `vaf session
   export`), markup is OFF: text is the definition there.
3. `TerminalConsole.render_str`, for every remaining string with markup on: a string that
   does not parse is shown as text, cleaned like a markup-off string (S1), and a markup
   link survives only as VAF's own file link (S2), its label kept and shown as it is: no
   part of it concealed, reversed or on a background colour. And the console prints
   emoji codes as text unless asked otherwise: VAF writes none of its own.

Every copy of a reply that leaves the terminal is not markup at all: it keeps the reply as
written, brackets included, and shows only VAF's own file link as its label
(`own_links_as_labels`). Those copies are the terminal turn's stored and mirrored copy
(`vaf.cli.cmd.run._reply_web_copy`), the browser bridge (`WebInterface.emit_agent_message`,
`emit_agent_message_append` for an assistant message, `emit_message_complete`), the web
chat's and the sub-agent drain's stored copies and the drain's fallback message
(`vaf.core.headless_runner`), the messenger and Veyllo App chain
(`_prepare_channel_outbound`; the Veyllo App's live deltas hold back a link until it closes,
`before_open_link`), the main-messenger lane (`send_to_main_messenger`), an
automation's result (`vaf.core.automation._push_result_to_web_ui`) and a question the agent
asks in the web chat (`vaf.core.thinking_mode.emit_message_to_web_ui`), and a chat reloaded
in the browser (`_stored_reply_text` in `vaf/core/web_server.py`, for a chat stored before
those copies converted the link). The label is plain
text there, never a web link: the link's shape does not prove VAF wrote it,
the main one names a folder, and the files the coder writes reach the browser as download
chips of their own.

Named boundaries of that rule: the terminal keeps the link live (above), and the model reads
the markup in the tool result it was given, so a tool card's Output in the browser shows that
raw result. The contact send tools (`send_telegram`, `send_whatsapp`, `send_discord`,
`send_veyllo_app`) send the argument the model wrote, byte for byte as a held send is approved,
and no VAF code puts `file_link` there. An automation's notification summary is a short
preview. The Veyllo App room's lines are stored as they were exchanged with the app's agent.

VAF's own file link is `file_link(path)`: the `file:` URI of an absolute local path,
labelled with that path, raw (`_own_file_link` is the check; the coder's
`_get_clickable_path` and the engine's project and report links call it, and no other
string in `vaf/` writes `[link=`, pinned by tests/test_terminal_markup.py). A link whose
label is not the path it opens, one that names a host or a network share, or one whose
label holds a character the reader cannot see is not VAF's, whoever wrote it.

Named boundaries: about 900 interpolations into markup strings outside the full-screen app
print mostly VAF's own values (paths, counts, configuration); those without an entry escape
rely on S1 and S2 alone, so a foreign value there can restyle itself, hide itself
(`[conceal]`, a colour) or lose a `[word]`, but it cannot raise, and the only link it can
produce is a local file link whose label is exactly the absolute path it opens (as any
foreign text can that writes VAF's own shape) and is shown as it is: a link part of whose
label is concealed, reversed or on a background colour is not kept. A foreground colour
alone that matches the terminal's own background is not seen as hiding, since VAF does not
know the terminal's colours; the link it keeps still opens only the path its full label
names. Rich's `Panel` titles and `Text.from_markup` parse without the console, so neither
S1 nor S2 reaches them: a foreign value there needs the entry escape (the coder's file name
in its preview title has one). A link from a `Text` or `Style` object, from `Markdown` or
from the `style` argument of `print` is not markup and keeps the link handling of
`terminal_segments` and `TerminalConsole.print`. A `style` argument of `print` that hides
text (a background, `reverse`, `conceal`) lies over every label printed with it, so no
markup link survives it; VAF passes none. A background from any other style laid over the
output (a style set on the console itself, `TerminalConsole(style=...)`, a `Panel` or
table style, a table's `row_styles`, `Console.log(style=...)`) is applied where S2 does not
see it, and VAF sets none of them.
"""
import json
import logging
import re
import sys
import threading
import unicodedata
from bisect import bisect_left
from typing import Any, Iterable, Iterator, Optional
from urllib.parse import unquote, urlsplit

from rich.cells import cell_len
from rich.console import Console, ConsoleOptions, RenderableType
from rich.errors import MarkupError
from rich.segment import Segment
from rich.text import Span, Text

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
#: escape sequence (ESC, intermediates, one final byte). Bounded, and a string sequence's
#: body holds no control character at all (C0, DEL, C1): a nested introducer means the first
#: one is unterminated anyway, and an introducer in the body made a run of them retry 2048
#: characters at every position (measured: 2.5 s for 100 KB of U+009D, linear now).
#: `vaf.core.web_ticker.ANSI_RE` is a narrower copy kept on purpose: that module promises to
#: stay stdlib-only, and this one imports Rich.
ESCAPE_SEQUENCE_RE = re.compile(
    "(?:\x1b\\[|\x9b)[\x30-\x3f]{0,64}[\x20-\x2f]{0,8}[\x40-\x7e]"
    "|(?:\x1b[\\]PX^_]|[\x90\x98\x9d\x9e\x9f])[^\x00-\x1f\x7f-\x9f]{0,%d}(?:\x07|\x1b\\\\|\x9c)"
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


#: A drive-letter path in slash form: `C:/...`.
_DRIVE_RE = re.compile(r"[A-Za-z]:/")

#: The Unicode default-ignorable code points (DerivedCoreProperties.txt): a terminal shows
#: them as nothing, so a label holding one reads as a different name than the one the link
#: opens. Combining marks are not among them: a decomposed accent (as macOS writes file
#: names) is visible and stays allowed.
_DEFAULT_IGNORABLE = (
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160),
    (0x17B4, 0x17B5), (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E),
    (0x2060, 0x206F), (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)


def _unseen(ch: str) -> bool:
    """A character the reader cannot see as it is: a format character (Cf), a line or
    paragraph separator (Zl, Zp: they break the line where none is shown) or a
    default-ignorable code point (a variation selector, the Hangul filler, U+034F)."""
    if unicodedata.category(ch) in ("Cf", "Zl", "Zp"):
        return True
    code = ord(ch)
    return any(low <= code <= high for low, high in _DEFAULT_IGNORABLE)


def _absolute_local(path: str) -> bool:
    """A local absolute path in slash form: `/x` (one leading slash) or `C:/x`. Two leading
    slashes are a network share on Windows (`//host/share`), never a local file."""
    return (path.startswith("/") and not path.startswith("//")) or bool(_DRIVE_RE.match(path))


def _own_file_link(target: str, label: str) -> bool:
    """VAF's own markup link: a local `file:` URI whose decoded path IS its label, so the
    reader sees exactly what the link opens. `file_link` writes it (`Path.as_uri()`, labelled
    with the absolute path): `file:///home/user/My%20Project` labelled `/home/user/My
    Project`, `file:///C:/Users/a/p` labelled `C:\\Users\\a\\p` on Windows.

    Refused: a host (`file://evil.example/share/x` opens a file on that host, whatever the
    label says; `localhost` alone is the local machine and is compared without it), a path
    that is a share (`file:////host/share` is one on Windows), a query or fragment the label
    would not show, a label that is not an absolute local path, and a label the reader
    cannot see as it is: one with a control character the console removes, a tab or a line
    break (shown as spaces or as two lines, while the name holds the character), or a
    character that shows as nothing (`_unseen`), which would show a different name than
    the one the link opens. A backslash is a separator only in a drive-letter (Windows)
    label; in a POSIX path it is part of the name, so `/tmp/a\\b` and `/tmp/a/b` are
    different files.

    The checks are all required, so their order changes no answer, only the cost: the
    target is checked first and the label is read character by character only once it has
    its decoded path's length. A label longer than its target (nested links, where every
    label runs to the end of the text) is refused without being read, so a run of links
    costs the size of their own text instead of the square of it."""
    if not label:
        return False
    try:
        parts = urlsplit(target)
    except ValueError:
        return False
    if (parts.scheme.lower() != "file" or parts.netloc not in ("", "localhost")
            or parts.query or parts.fragment):
        return False
    path, shown = unquote(parts.path), label
    # The decoded path equals the label at its length, or one more (`/C:/x` for `C:\\x`).
    if not 0 <= len(path) - len(shown) <= 1:
        return False
    if _DRIVE_RE.match(shown[:3].replace("\\", "/")):
        path, shown = path.replace("\\", "/"), shown.replace("\\", "/")
        if path.startswith("/") and _DRIVE_RE.match(path[1:]):
            path = path[1:]                   # `/C:/x` is the URI form of `C:/x`
    if not (_absolute_local(path) and path == shown):
        return False
    return ("\t" not in label and "\n" not in label and terminal_text(label) == label
            and not any(_unseen(ch) for ch in label))


def file_link(path: Any) -> str:
    """VAF's own clickable link to a local file or folder, as Rich markup: the `file:` URI of
    its absolute path, labelled with that path AS IT IS. The label stays raw because this
    text is also read: the model gets it in a tool result and the workflow engine keeps only
    the label (`vaf.workflows.engine._strip_rich_links`), so a Next.js `app/[id]/page.tsx`
    must not arrive as `app/\\[id]/page.tsx`. The escape happens where it is printed:
    `escape_markup` keeps this link live and escapes its label, and `TerminalConsole` keeps
    it (`_own_file_link`). A network share (a UNC path on Windows) carries a host and is
    shown as a plain label.

    The result is markup with a raw label: print it through `escape_markup` (or with
    `markup=False`), never straight into a markup string, where a `[id]` in the path would
    be read as a tag."""
    import os
    from pathlib import Path

    absolute = os.path.abspath(os.fspath(path))
    return f"[link={Path(absolute).as_uri()}]{absolute}[/link]"


#: A complete markup link with a plain target, on one line. Its label may hold brackets (a
#: Next.js route `[id]`, `[...slug]`, `[[opt]]`) but no other link tag: it ends at the
#: first `[/link]`. The target holds no bracket at all (`Path.as_uri()` writes `[` as `%5B`):
#: a target class that allowed `[` let a run of `[link=` openers in somebody's text cost
#: quadratic time in every narration line (measured: 4000 openers, 0.7 s; now linear).
_MARKUP_LINK_RE = re.compile(r"\[link=([^\[\]\s]+)\]((?:(?!\[/?link[=\]])[^\n])*)\[/link\]")


# Rich's render turns every `\[` of plain text into `[` (rich/markup.py), but its escape doubles
# the backslashes only in front of a bracket that looks like a tag (`[a-z#/@]...]`). A Windows
# path to a Next.js catch-all route, `app\[...slug]\page.tsx`, therefore lost the backslash in
# front of its bracket: the label no longer read as the path it opens, and the own link was
# dropped. One more backslash in front of every other bracket keeps them all.
# Both patterns start only where a run of backslashes starts (`(?<!\\)`): Rich's own pattern,
# unanchored, rescanned such a run from each of its positions when no tag followed, and this one
# when a tag did, quadratic on other parties' text (16 K backslashes: 1.1 and 1.3 s, measured).
# Anchored they give Rich's result unchanged: its leftmost match in a run starts at the run.
_TAG_BRACKET = re.compile(r"(?<!\\)(\\*)(\[[a-z#/@][^\[]*?\])")
_UNTAGGED_BRACKET = re.compile(r"(?<!\\)(\\+)(?=\[(?![a-z#/@][^\[]*?\]))")


def _escape(text: str) -> str:
    """`rich.markup.escape` in linear time, and the backslashes in front of a bracket it
    leaves alone."""
    text = _TAG_BRACKET.sub(lambda m: m.group(1) * 2 + "\\" + m.group(2), text)
    if text.endswith("\\") and not text.endswith("\\\\"):
        text += "\\"                   # Rich's rule: a final backslash would escape the next tag
    return _UNTAGGED_BRACKET.sub(r"\1\\", text)


def escape_markup(value: Any) -> str:
    """Other parties' `value`, safe to put into a Rich markup string: shown as written.

    Escaped with `rich.markup.escape`, plus one backslash in front of each bracket it leaves
    alone (`_escape`: Rich's render takes one off every `\\[`), except that VAF's own file link
    (`[link=file:...]<path>[/link]` whose label is the path it opens) stays a live link,
    with its label escaped. The text between two such links is escaped as one piece, so
    Rich's rule for a trailing backslash (it would escape the next tag) still holds. Control
    characters are not this function's job: the console removes them (`TerminalConsole`).
    `None` gives `""`; anything else is `str()`-ed.

    Named boundary: that trailing-backslash rule doubles a final backslash, which a closing
    tag right after it takes back (`[cyan]x\\\\[/cyan]` shows `x\\`), but which shows twice
    where plain text follows. A table cell therefore takes other parties' text as a `Text`,
    not through this function."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    if "[" not in text:
        return _escape(text)
    pieces, pos = [], 0
    for match in _MARKUP_LINK_RE.finditer(text):
        target, label = match.group(1), match.group(2)
        if not _own_file_link(target, label):
            continue
        pieces.append(_escape(text[pos:match.start()]))
        pieces.append(f"[link={target}]{_escape(label)}[/link]")
        pos = match.end()
    pieces.append(_escape(text[pos:]))
    return "".join(pieces)


def own_links_as_labels(value: Any) -> str:
    """Other parties' `value` as written, with VAF's own file link shown as its label.

    For a copy of a reply that leaves the terminal: the copy a chat stores and the browser
    shows, which a person reads as rendered Markdown and a model reads back later. Brackets
    there are text (`arr[i]`, `[label](url)`, `- [x]`, `app/[id]/page.tsx`, a status line
    such as `[Generation stopped by user]`), so nothing is removed or escaped: only a complete
    link that `_own_file_link` accepts (the one `file_link` writes, which a model repeats from
    the coder's tool result) becomes its label, the path it opens. A foreign link stays as
    written, its address visible. Control characters are not this function's job either: a
    browser does not obey them and a terminal prints through `TerminalConsole`. `None` gives
    `""`; anything else is `str()`-ed. Exported as `vaf.own_links_as_labels` for every place
    that shows VAF's text outside a Rich console: a web page, a messenger, a transcript.

    `vaf.workflows.engine._strip_rich_links` is wider on purpose: it keeps the label of
    EVERY link, because the next workflow step reads its paths with a regex and never shows
    them to a person."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    if "[link=" not in text:
        return text
    pieces, pos = [], 0
    for match in _MARKUP_LINK_RE.finditer(text):
        target, label = match.group(1), match.group(2)
        if not _own_file_link(target, label):
            continue
        pieces.append(text[pos:match.start()])
        pieces.append(label)
        pos = match.end()
    pieces.append(text[pos:])
    return "".join(pieces)


_LINK_OPEN, _LINK_CLOSE = "[link=", "[/link]"


def before_open_link(text: str) -> str:
    """The part of a growing text that is settled for `own_links_as_labels`: all of it but a
    link still open on the last line (from its opener on) or a trailing start of an opener
    (`[`, `[li`). VAF's own link becomes its path only once its closing tag has arrived, so a
    stream that sends increments of the converted text holds this rest back until then;
    otherwise the reader sees the raw markup first and the converted text no longer extends
    what was sent. A link cannot span a line break, so an opener on an earlier line is text.
    Named boundary: internal, its one caller is the Veyllo App's live deltas
    (`vaf.core.app_link.bridge.stream_round`); the web chat re-sends its whole text per
    update and needs no increment."""
    line = text.rfind("\n") + 1
    close = text.rfind(_LINK_CLOSE)
    opener = text.find(_LINK_OPEN, max(line, close + len(_LINK_CLOSE) if close >= 0 else 0))
    if opener >= 0:
        return text[:opener]
    for size in range(len(_LINK_OPEN) - 1, 0, -1):
        if text.endswith(_LINK_OPEN[:size]):
            return text[:-size]
    return text


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

    The text of a LINE of segments (up to a newline or a control segment) is cleaned like
    `terminal_text`, across the segment boundaries: a renderer splits text into segments by
    style, and Rich's highlighter puts the `31` of a colour code into a segment of its own,
    so a sequence is matched on the line's joined text and each segment loses its part of
    it. A run of segments that lost printable characters gets the lost width back as spaces at
    its end, so the line keeps its layout (the removed control characters themselves are
    zero-width). A style whose LINK target holds an unsafe character loses the link: Rich and
    Textual write the target raw into the OSC 8 sequence, and a URL never legitimately holds
    one. A control segment (the renderer's own cursor, clear and title codes) passes
    unchanged; a segment left with no text is dropped, since an empty text segment is
    invalid to Textual. Shared by `TerminalConsole` and the full-screen app's line filter,
    so both renderers apply one definition."""
    line: list = []
    for segment in segments:
        if segment.control:
            yield from _clean_line(line)
            line = []
            yield segment
            continue
        if "\n" not in segment.text:
            line.append(segment)
            continue
        # A segment that holds a newline is split at it, and the line ends there: the width a
        # run lost is given back before the newline, not at the start of the next line, and
        # a sequence after the newline is matched with the rest of its own line.
        parts = segment.text.split("\n")
        for index, part in enumerate(parts):
            if part:
                line.append(Segment(part, segment.style, None))
            if index < len(parts) - 1:
                line.append(Segment("\n", segment.style, None))
                yield from _clean_line(line)
                line = []
    yield from _clean_line(line)


def _hostile_link(style) -> bool:
    """A link target holding an unsafe character, a newline or a tab: Rich and Textual write
    the target raw into their OSC 8 sequence, and a URL never holds any of them."""
    link = style.link if style is not None else None
    return bool(link) and (_UNSAFE_RE.search(link) is not None or "\n" in link
                           or "\t" in link)


def _clean_line(line: list) -> Iterator[Segment]:
    if not line:
        return
    texts = [segment.text for segment in line]
    joined = "".join(texts)
    if not _UNSAFE_RE.search(joined) and not any(_hostile_link(seg.style) for seg in line):
        yield from line
        return
    # Whole sequences on the joined text; a sequence never crosses a newline, and a line
    # ends at the first segment that holds one.
    spans = [match.span() for match in ESCAPE_SEQUENCE_RE.finditer(joined)]
    first, offset = 0, 0
    held, pad = None, 0          # the last cleaned segment, and the width its run lost
    for segment, text in zip(line, texts):
        start, end = offset, offset + len(text)
        offset = end
        style = segment.style
        if _hostile_link(style):
            style = style.update_link(None)
        while first < len(spans) and spans[first][1] <= start:
            first += 1
        pieces, pos, index = [], start, first
        while index < len(spans) and spans[index][0] < end:
            span_start, span_end = spans[index]
            if span_start > pos:
                pieces.append(joined[pos:span_start])
            pos = max(pos, span_end)
            index += 1
        if pos < end:
            pieces.append(joined[pos:end])
        cleaned = "".join(pieces).translate(_REMOVE)
        if cleaned == text and style is segment.style:
            if held is not None and (held.text or pad > 0):
                yield _padded(held, pad)
            held, pad = None, 0
            yield segment
            continue
        if held is not None and held.text:
            yield held
        held = Segment(cleaned, style, None)
        pad += cell_len(text) - cell_len(cleaned)
    if held is not None and (held.text or pad > 0):
        yield _padded(held, pad)


def _padded(segment: Segment, pad: int) -> Segment:
    """A run of cleaned segments gives back the width it lost at its end, so the gap sits at
    the end of the cell (before a border, which is a segment of its own) and not mid-text."""
    if pad > 0:
        return Segment(segment.text + " " * pad, segment.style, None)
    return segment


class TerminalConsole(Console):
    """A Rich console that removes escape sequences and control characters from what it
    prints.

    Every rendered segment is cleaned (`render`, which every output path renders through:
    print, log, input prompts, status and Live updates, `Text` objects, Markdown, link
    targets), padded back to its width where a sequence took printable characters with it.
    A string printed with markup OFF is also cleaned before Rich lays it out (`render_str`),
    so no gap is left. With markup ON it is not: cleaning before the markup parser would
    rebuild a tag that `rich.markup.escape` left alone because a control character stood
    in it (`[` NUL `link=...]` became a live link, `[` NUL `/]` a MarkupError). Instead, a
    markup string that does not parse is shown as text, cleaned like a markup-off string, and
    a markup link is kept only when its label is the file path it opens (VAF's own
    `[link=file://...]`); any other markup link keeps its label and loses the link. Rich's own
    control codes (`Control.clear()`, cursor moves) are control segments and pass; its colours
    come from styles and are untouched. The one style Rich applies after `render`, the
    `style` argument of `print` (and so of `out`), has an unsafe link target dropped before
    Rich sees it, and when it hides text (a background, `reverse`, `conceal`) no markup link
    printed under it is kept (S2). A CR in `end` is removed like any other, which no caller
    relies on. Constructed exactly like
    `rich.console.Console` (whose arguments are all keywords), except that emoji codes are
    OFF unless `emoji=True` is passed: VAF writes no Rich emoji code in any string of its own
    (an AST scan of every string literal in `vaf/` found none), so a `:CD:` or `:smile:` in a
    printed string is somebody's text and shows as written instead of as a symbol."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("emoji", False)
        super().__init__(**kwargs)
        self._markup_default = bool(kwargs.get("markup", True))
        # Whether the `print` running on this thread lays a hiding style over its output.
        self._print_backdrop = threading.local()

    def print(self, *objects: Any, style: Any = None, **kwargs: Any) -> None:
        # `print` applies its `style` to the segments AFTER `render`, so a link in it would
        # skip `terminal_segments`; one holding an unsafe character is dropped here instead.
        # For the same reason S2 cannot see that style's background on a label: it is noted
        # for `_own_links_only` while this call renders (per thread, restored for a nested
        # call). A style Rich cannot resolve is passed on unchanged, so Rich reports it as
        # before.
        hides = False
        if style is not None:
            try:
                resolved = self.get_style(style)
            except Exception:
                resolved = None
            if resolved is not None:
                if _hostile_link(resolved):
                    style = resolved.update_link(None)
                hides = _hides_text(resolved)
        outer = getattr(self._print_backdrop, "hides", False)
        self._print_backdrop.hides = outer or hides
        try:
            super().print(*objects, style=style, **kwargs)
        finally:
            self._print_backdrop.hides = outer

    def render_str(self, text: str, **kwargs: Any) -> Text:
        markup = kwargs.get("markup")
        if not (self._markup_default if markup is None else markup):
            text = terminal_text(text)
            return super().render_str(text, **kwargs)
        try:
            rich_text = super().render_str(text, **kwargs)
        except MarkupError:
            # S1: markup that does not parse is somebody's text (`[/]`, `[/etc/hosts]`), and
            # Rich would raise it out of `print`; shown as written, cleaned like markup off.
            return self.render_str(text, **{**kwargs, "markup": False})
        return self._own_links_only(rich_text)

    def _own_links_only(self, rich_text: Text) -> Text:
        """S2: a link that markup put on a span stays only as VAF's own file link (its label
        is the path it opens); any other loses the link and keeps its label. So does an own
        link any character of whose label is shown with `conceal`, `reverse` or a background
        colour: `/tmp/a[conceal]b[/conceal]` or `/tmp/a[black on black]b[/]` links `/tmp/ab`
        while the reader sees `/tmp/a`. Not equal colours but any background counts, because
        `#000000 on #010101` is equal to the eye and unequal as values, and a foreground and
        a background can come from two spans; VAF puts none of the three on markup of its
        own, and no style of Rich's highlighter has one. A hiding span that only borders the
        label does not count. Named boundary: a foreground colour alone that matches the
        terminal's own background (`[black]` on a black terminal) is not seen, since VAF does
        not know the terminal's colours; the link still opens only the path the full label
        names. A style that does not resolve is left as it is: Rich renders it as no style,
        so no link. A hiding style over the whole text, the `style` of the `print` call or
        the text's own base style, lies over every label, so every link drops: its own colour
        may equal its background, and the highlighter colours every path label anyway.

        Cost: one sort and one binary search per link, O(n log n) in the spans, plus the
        own-link check of each link, which neither copies nor reads a label longer than its
        target and reads a shorter one only once it has its decoded path's length
        (`_own_file_link`), so in all no more than the size of the link tags. The hiding
        spans are merged into disjoint intervals once, and each link looks up the last
        interval starting before its end, instead of testing every hiding span per link
        (quadratic: 12000 link and conceal pairs took about ten seconds, and 4000 nested
        links, each label read in full, as long)."""
        spans = rich_text.spans
        resolved = []
        for span in spans:
            try:
                resolved.append(self.get_style(span.style) if span.style else None)
            except Exception:
                resolved.append(None)
        starts, ends = _hiding_intervals(
            (span.start, span.end) for span, style in zip(spans, resolved)
            if style is not None and _hides_text(style))
        try:
            base = self.get_style(rich_text.style) if rich_text.style else None
        except Exception:
            base = None
        if getattr(self._print_backdrop, "hides", False) or (
                base is not None and _hides_text(base)):
            starts, ends = [0], [len(rich_text.plain)]
        for index, (span, style) in enumerate(zip(spans, resolved)):
            if style is None or not style.link:
                continue
            # The last hiding interval that starts before the label ends; it overlaps the
            # label only when it ends after the label starts (both strict: touching is not
            # hiding). The intervals are disjoint and sorted, so no earlier one can.
            last = bisect_left(starts, span.end) - 1
            hidden = last >= 0 and ends[last] > span.start
            # A label longer than its target is never the target's decoded path (decoding
            # only shortens), so it is refused before it is copied out of the text: in
            # nested links every label runs to the end of the text.
            if (hidden or span.end - span.start > len(style.link)
                    or not _own_file_link(style.link, rich_text.plain[span.start:span.end])):
                spans[index] = Span(span.start, span.end, style.update_link(None))
        return rich_text

    def render(self, renderable: RenderableType,
               options: Optional[ConsoleOptions] = None) -> Iterable[Segment]:
        return terminal_segments(super().render(renderable, options))


def _hides_text(style: Any) -> bool:
    """A style that can keep text from the reader as it is: `conceal`, `reverse`, or any
    background colour (equal or near-equal to the foreground, or set by another span)."""
    return bool(style.conceal or style.reverse or style.bgcolor is not None)


def _hiding_intervals(intervals: Iterable[tuple]) -> tuple:
    """`(start, end)` intervals merged into disjoint ones, as two lists sorted by start.
    Overlapping and touching intervals merge. Sorted here, not trusted to be: Rich's markup
    spans come in start order, but the highlighter's are listed before them and a `Text`
    built by hand can hold any order."""
    starts: list = []
    ends: list = []
    for start, end in sorted(intervals):
        if ends and start <= ends[-1]:
            ends[-1] = max(ends[-1], end)
        else:
            starts.append(start)
            ends.append(end)
    return starts, ends


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
