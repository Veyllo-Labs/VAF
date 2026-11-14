# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Other parties' text is shown as written in the terminal, not read as Rich markup.

MEASURED BEFORE THE FIX (Rich 14.3.3, Textual 8.2.8): every console in VAF prints a `str`
with markup on. `[link=https://e.org]x[/link]` in a model reply, a sub-agent result, an inbox
preview or a web page title became an OSC 8 link to the sender's address with the label `x`;
`[conceal]...[/conceal]` made text invisible; `arr[i]`, `- [x] done` and `vaf[speech]` lost
their brackets; a lone closing tag (`[/]`, `[/link]`, `[/etc/hosts]`) raised MarkupError out
of `print`. In the reply stream that error left `_process_agent_message` and the answer was
not saved; in the sub-agent drain it was raised before `consume_result`, so the same result
failed at every pass. `vaf session export --format json` to a pipe was wrapped at 80 columns
and did not parse.

The fix has three parts (vaf/core/terminal_text.py, "RICH MARKUP"): other parties' text is
escaped where it enters a markup string (`escape_markup`), whole replies print with markup
off, and `TerminalConsole` shows a string that does not parse as text (S1) and keeps a
markup link only when its label is the file path it opens (S2). Every input here is a Python
literal; nothing is opened or executed.
"""
import ast
import asyncio
import contextlib
import io
import json
import os
import re
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from test_tui_agent_bridge import FakeAgent, quiet_run_module  # noqa: F401 - fixture
from test_tui_app_smoke import smoke_app  # noqa: F401 - fixture
from vaf.core.terminal_text import TerminalConsole, escape_markup, own_links_as_labels

ROOT = Path(__file__).resolve().parents[1]

_OSC8 = re.compile("\x1b\\]8;([^;\x1b]*);([^\x1b]*)\x1b\\\\")
_SGR = re.compile("\x1b\\[[0-9;]*m")

#: The AC's hostile message: a lone closing tag, a style word, a package extra, hidden text
#: and a foreign link.
HOSTILE = "x [i] vaf[speech] [/y] [conceal]h[/conceal] [link=https://e.org]z[/link]"
#: The same kinds of markup without a parse error, so the console's fallback (S1) cannot
#: show it as text on its own: only the entry escape keeps these brackets.
PARSEABLE = "arr[i] [conceal]h[/conceal] [link=https://e.org]z[/link]"
OWN = "[link=file:///tmp/p]/tmp/p[/link]"


def _console(buf=None):
    return TerminalConsole(file=buf if buf is not None else io.StringIO(), force_terminal=True,
                           color_system="truecolor", width=200)


def _out(console) -> str:
    return console.file.getvalue()


def _targets(out: str) -> list:
    """The OSC 8 targets the output opens (Rich closes every link with an empty one)."""
    return [m.group(2) for m in _OSC8.finditer(out) if m.group(2)]


def _plain(out: str) -> str:
    """What the reader sees: the output without its SGR codes and OSC 8 framing."""
    return _SGR.sub("", _OSC8.sub("", out))


def _render_plain(markup: str) -> str:
    from rich.console import Console as PlainConsole
    console = PlainConsole(file=io.StringIO(), force_terminal=True, color_system="truecolor",
                           width=200)
    console.print(markup)
    return console.file.getvalue()


def _render(*renderables, **kwargs) -> str:
    console = _console()
    for renderable in renderables:
        console.print(renderable, **kwargs)
    return _out(console)


# ── the sink: TerminalConsole ─────────────────────────────────────────────────────────

def _producer_shapes():
    """VAF's own links, with the target each one opens computed by the same calls the
    producer makes: `os.path.abspath` gives `C:\\tmp\\...` on a Windows host, so a
    hard-coded `file:///tmp/...` would fail there only. Section 9 of
    tests/test_windows_path_hygiene.py catches a POSIX literal asserted back through
    `str(Path)`; a literal the code under test transforms (abspath, then percent-encoding)
    is a data-flow question no static scan answers without flagging every protocol URI
    (`file:///workspace/...` is POSIX by definition), and `os.path` cannot run as `ntpath`
    under hostile_env.py. So the expected values are computed, and the Windows semantics
    are exercised here with `PureWindowsPath`, on every host."""
    from vaf.core.terminal_text import file_link
    from vaf.tools.coder import _get_clickable_path

    project = os.path.join(os.sep, "tmp", "My Project")
    expected = Path(os.path.abspath(project)).as_uri()
    windows = PureWindowsPath("C:/tmp/My Project")
    return [
        (_get_clickable_path(project), expected),   # the coder
        (file_link(project), expected),             # the engine's project and report links
        # Windows semantics on every host: the URI of a drive path, labelled with the path.
        (f"[link={windows.as_uri()}]{windows}[/link]", "file:///C:/tmp/My%20Project"),
        ("[link=file:///C:/Users/a/p]C:\\Users\\a\\p[/link]", "file:///C:/Users/a/p"),
        ("[link=file://localhost/tmp/p]/tmp/p[/link]", "file://localhost/tmp/p"),
    ]


#: Links that are not VAF's: the label is not the local path the link opens, or the reader
#: cannot see the label as it is.
FORGED = [
    ("[link=https://e.org]x[/link]", "x"),
    ("[link=file:///etc/passwd]https://bank.example[/link]", "https://bank.example"),
    ("[link=file:///tmp/project]project[/link]", "project"),
    # A host: the file is on that machine, whatever the label says.
    ("[link=file://evil.example/tmp/p]/tmp/p[/link]", "/tmp/p"),
    ("[link=file://evil.example/share/x.exe]/evil.example/share/x.exe[/link]",
     "/evil.example/share/x.exe"),
    ("[link=file://localhost/etc/passwd]/localhost/etc/passwd[/link]", "/localhost/etc/passwd"),
    # A relative label for an absolute target.
    ("[link=file:/etc/passwd]etc/passwd[/link]", "etc/passwd"),
    # Network shares: four slashes are one on Windows, and so is a `\\host` label.
    ("[link=file:////evil/share/x]/evil/share/x[/link]", "/evil/share/x"),
    ("[link=file:////evil/share/x]\\\\evil\\share\\x[/link]", "\\\\evil\\share\\x"),
    # A query or fragment the label does not show.
    ("[link=file:///tmp/p?x=1]/tmp/p[/link]", "/tmp/p"),
    # A label the console changes or the reader cannot see as it is.
    ("[link=file:///tmp/a%E2%80%AEexe.txt]/tmp/a\u202eexe.txt[/link]", "/tmp/aexe.txt"),
    ("[link=file:///tmp/a%00b]/tmp/a\x00b[/link]", "/tmp/ab"),
    ("[link=file:///tmp/a%E2%80%8Bb]/tmp/a\u200bb[/link]", "/tmp/a"),
    ("[link=file:///tmp/a%CD%8Fb]/tmp/a\u034fb[/link]", "/tmp/a"),
    ("[link=file:///tmp/a%EF%B8%8Fb]/tmp/a\ufe0fb[/link]", "/tmp/a"),
    ("[link=file:///tmp/a%E3%85%A4b]/tmp/a\u3164b[/link]", "/tmp/a"),
    # On POSIX a backslash is part of a name, not a separator: these are other files.
    ("[link=file:///tmp/a%5Cb]/tmp/a/b[/link]", "/tmp/a/b"),
    ("[link=file:///tmp/a/b]/tmp/a\\b[/link]", "/tmp/a\\b"),
]


@pytest.mark.parametrize("shape", ["print", "table", "rule"])
def test_a_markup_link_survives_only_as_vafs_own_file_link(shape):
    """A foreign target, a file link whose label is not its path, one that names a host or a
    share, one with a query, a relative label, and a label with a control or format
    character all keep their label and lose the link; the shapes VAF itself writes keep
    theirs. MUTATION: drop the span filter in `render_str` - red (the foreign links come
    back); compare with `startswith("file:")` instead of the path - red; compare the paths
    with every leading slash stripped (the first version) - red (the host and share forms
    are linked); drop the label's control and Cf check - red."""
    from rich.rule import Rule
    from rich.table import Table

    def render(markup):
        if shape == "table":
            table = Table()
            table.add_column("c")
            table.add_row(markup)
            return _render(table)
        if shape == "rule":
            return _render(Rule(markup))
        return _render(markup)

    for markup, label in FORGED:
        out = render(markup)
        assert _targets(out) == [], (markup, out)
        assert label in _plain(out), (markup, out)
    for markup, target in _producer_shapes():
        out = render(markup)
        assert _targets(out) and set(_targets(out)) == {target}, (markup, out)


@pytest.mark.parametrize("text", ["a [/] b", "closing [/link] alone", "path [/etc/hosts]"])
def test_markup_that_does_not_parse_is_shown_as_text(text):
    """A lone closing tag raised MarkupError out of `print`. Now the string is shown as
    written, cleaned like a markup-off string. MUTATION: remove the `except MarkupError` -
    red (MarkupError)."""
    from rich.table import Table

    assert _plain(_render(text)).strip() == text
    table = Table()
    table.add_column("c")
    table.add_row("[/x]")
    assert "[/x]" in _plain(_render(table))
    # The fallback is the markup-off lane: control characters go there too.
    assert _plain(_render("a [/] \x1b]0;T\x07b")).strip() == "a [/] b"


@pytest.mark.parametrize("target, label, own", [
    # Characters that show as nothing: a default-ignorable code point, a line separator.
    ("file:///tmp/a%CD%8Fb", "/tmp/a\u034fb", False),
    ("file:///tmp/a%EF%B8%8Fb", "/tmp/a\ufe0fb", False),
    ("file:///tmp/a%E3%85%A4b", "/tmp/a\u3164b", False),
    ("file:///tmp/a%E2%80%A8b", "/tmp/a\u2028b", False),
    # A tab shows as spaces and a line break as two lines, while the name holds the character.
    ("file:///tmp/a%09b", "/tmp/a\tb", False),
    ("file:///tmp/a%0Ab", "/tmp/a\nb", False),
    # A decomposed accent (the way macOS writes file names) is visible: kept.
    ("file:///tmp/cafe%CC%81", "/tmp/cafe\u0301", True),
    # A backslash is a separator in a drive-letter label only.
    ("file:///tmp/a%5Cb", "/tmp/a\\b", True),
    ("file:///tmp/a%5Cb", "/tmp/a/b", False),
    ("file:///tmp/a/b", "/tmp/a\\b", False),
    ("file:///C:/Users/a/p", "C:\\Users\\a\\p", True),
])
def test_the_own_link_label_is_what_the_reader_sees(target, label, own):
    """MUTATION: drop the default-ignorable table - red; refuse every combining mark as well
    - red (the accent); turn backslashes into slashes for a POSIX label - red; drop the tab
    and line-break refusal - red on those two."""
    from vaf.core.terminal_text import _own_file_link

    assert _own_file_link(target, label) is own


BRACKETED = [("app", "[id]", "page.tsx"), ("app", "[...slug]", "page.tsx"),
             ("app", "[[opt]]", "page.html")]


@pytest.mark.parametrize("parts", BRACKETED, ids=["id", "slug", "optional"])
def test_a_bracketed_path_stays_the_real_path_for_every_reader(parts, tmp_path):
    """A Next.js route. The model reads the coder's tool result as text and the workflow
    engine keeps only a link's label, so both must get the real path; printed, the link stays
    live. MUTATION: escape the label in `file_link` again - red (the model gets
    `\\[id]`); forbid brackets in `_MARKUP_LINK_RE`'s label again - red (the printed link is
    escaped into text)."""
    from rich.console import Console as PlainConsole
    from vaf.tools.coder import _format_file_links, _get_clickable_path
    from vaf.workflows.engine import _strip_rich_links

    target = tmp_path.joinpath(*parts)
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")
    real = os.path.abspath(target)
    uri = Path(real).as_uri()

    link = _get_clickable_path(str(target))
    assert link == f"[link={uri}]{real}[/link]"
    listing = _format_file_links([os.path.join(*parts)], str(tmp_path))
    assert real in listing
    assert _strip_rich_links("Saved: " + link) == "Saved: " + real
    assert own_links_as_labels("Saved: " + link) == "Saved: " + real   # the stored copy

    for console in (_console(), PlainConsole(file=io.StringIO(), force_terminal=True,
                                             color_system="truecolor", width=300)):
        console.print(escape_markup("Saved: " + link))
        out = console.file.getvalue()
        assert set(_targets(out)) == {uri}, out
        assert "Saved: " + real in _plain(out)


def test_only_file_link_writes_a_markup_link():
    """VAF's own link has one producer, so the console's rule and the link agree by
    construction. Docstrings and comments may show the shape; a regex escapes its bracket.
    MUTATION: put the engine's old `"[link=file:///" + path` f-string back in agent.py -
    red."""
    needle = re.compile(r"(?<!\\)\[link[= ]")
    found = []
    for path in sorted((ROOT / "vaf").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel == "vaf/core/terminal_text.py":
            continue
        tree = ast.parse(path.read_bytes())
        # Every string standing as a statement of its own: docstrings, and the odd string
        # used as a block comment. Neither is output.
        bare_strings = {id(node.value) for node in ast.walk(tree)
                        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)}
        found += [f"{rel}:{node.lineno}" for node in ast.walk(tree)
                  if isinstance(node, ast.Constant) and isinstance(node.value, str)
                  and id(node) not in bare_strings and needle.search(node.value)]
    assert not found, found


def test_a_run_of_link_openers_costs_linear_time():
    """`escape_markup` runs on every narration line, so somebody's text decides its input.
    A link pattern whose target allowed `[` rescanned the rest of the text from every
    `[link=` opener (measured: 4000 openers, 0.7 s; 12000 about 6 s). The limit is far
    above the linear time (milliseconds) and far below the quadratic one, so a slow runner
    does not make it flaky. MUTATION: allow `[` in the target again - red."""
    import time

    for hostile in ("[link=" * 12000,
                    ("[link=file:///a]" + "[link=" * 40 + "x") * 300):
        start = time.perf_counter()
        escaped = escape_markup(hostile)
        assert time.perf_counter() - start < 1.5
        assert escaped
    # And the escaped text prints as written, with no link.
    sample = ("[link=file:///a]" + "[link=" * 3 + "x") * 2
    out = _render(escape_markup(sample), soft_wrap=True)
    assert sample in _plain(out) and _targets(out) == []


def test_a_concealed_part_of_a_label_drops_the_link():
    """Text printed raw into markup (the long tail) can write VAF's own shape with part of
    the label hidden: the link opens `/tmp/ab` while the reader sees `/tmp/a`. MUTATION:
    drop the conceal check in `_own_links_only` - red."""
    for markup in ("[link=file:///tmp/ab]/tmp/a[conceal]b[/conceal][/link]",
                   "[link=file:///tmp/ab conceal]/tmp/ab[/link]"):
        out = _render(markup)
        assert _targets(out) == [], (markup, out)
    assert set(_targets(_render("[link=file:///tmp/ab]/tmp/ab[/link]"))) == {"file:///tmp/ab"}


@pytest.mark.parametrize("markup", [
    "[link=file:///tmp/ab]/tmp/a[#000000 on #000000]b[/][/link]",
    # Contrast 1.006: equal to the eye, unequal as values, so an equality check misses it.
    "[link=file:///tmp/ab]/tmp/a[#000000 on #010101]b[/][/link]",
    "[link=file:///tmp/ab]/tmp/a[reverse #ffffff on #ffffff]b[/][/link]",
    # Reverse with no background: black text becomes black cells on a light terminal.
    "[link=file:///tmp/ab]/tmp/a[reverse black]b[/][/link]",
    # The foreground and the background come from two different spans.
    "[link=file:///tmp/ab][on black]/tmp/a[black]b[/black][/on black][/link]",
    "[link=file:///tmp/ab][#000000 on #000000]/tmp/ab[/][/link]",
    # A background over the whole line, with a shorter hiding span inside it that ends
    # before the link starts: the merged interval must keep the longer end.
    "[on black]x[reverse]y[/reverse]z[link=file:///tmp/ab]/tmp/ab[/link][/on black]",
])
def test_a_label_hidden_by_colour_drops_the_link(markup):
    """Text printed raw into markup can write VAF's own shape and hide part of the label
    with colours instead of `[conceal]`: the link opens `/tmp/ab` while the reader sees
    `/tmp/a` or a blank. Any background or `reverse` on the label drops the link; the label
    stays as text. MUTATIONS: drop the `bgcolor` clause of `_hides_text` - red on the
    background cases; drop its `reverse` clause - red on `[reverse black]`; keep the end of
    the last merged interval instead of the larger one - red on the last case."""
    out = _render(markup)
    assert _targets(out) == [], (markup, out)
    assert "/tmp/ab" in _plain(out), (markup, out)


@pytest.mark.parametrize("markup", [
    "[link=file:///tmp/ab]/tmp/ab[/link]",
    "[cyan]see [link=file:///tmp/ab]/tmp/ab[/link] [bold]x[/][/cyan]",
    "[dim]see [link=file:///tmp/ab]/tmp/ab[/link][/dim]",
    "[bold][link=file:///tmp/ab]/tmp/ab[/link][/bold]",
])
def test_a_visible_style_keeps_the_own_link(markup):
    """A foreground colour, `dim` or `bold` hides nothing, and the console's highlighter
    (on by default) colours parts of a path label: the own link stays. MUTATION: count any
    style with a foreground colour as hiding - red on the `[cyan]` case (and on the
    highlighter's colours)."""
    out = _render(markup)
    assert set(_targets(out)) == {"file:///tmp/ab"}, (markup, out)


@pytest.mark.parametrize("markup", [
    "[conceal]x[/conceal][link=file:///tmp/ab]/tmp/ab[/link]",
    "[link=file:///tmp/ab]/tmp/ab[/link][conceal]x[/conceal]",
    "[on red]x[/][link=file:///tmp/ab]/tmp/ab[/link][on red]y[/]",
])
def test_a_hiding_span_that_only_touches_the_label_keeps_the_link(markup):
    """A hiding span that ends where the label starts, or starts where it ends, hides no
    character of it. MUTATIONS: the end test `ends[last] > span.start` made `>=` - red on
    the span directly before; `bisect_left` made `bisect_right` - red on the span directly
    after."""
    out = _render(markup)
    assert set(_targets(out)) == {"file:///tmp/ab"}, (markup, out)


def test_hiding_spans_out_of_order_still_hide():
    """Rich lists the highlighter's spans before the markup's, and a `Text` built by hand can
    hold any order, so the hiding intervals are sorted before they are merged. Here a
    background over the whole text is listed after a later `conceal` span. MUTATION: merge
    the intervals in the order given (no sort) - red."""
    from rich.text import Span, Text

    console = _console()
    text = Text("xx/tmp/abyyz")
    text.spans[:] = [Span(10, 11, "conceal"), Span(0, 12, "on black"),
                     Span(2, 9, "link file:///tmp/ab")]
    console._own_links_only(text)
    assert not console.get_style(text.spans[2].style).link
    # The same spans without the background keep the link: the check is not just dropping
    # every link of a hand-built text.
    text = Text("xx/tmp/abyyz")
    text.spans[:] = [Span(10, 11, "conceal"), Span(2, 9, "link file:///tmp/ab")]
    console._own_links_only(text)
    assert console.get_style(text.spans[1].style).link == "file:///tmp/ab"


def test_the_own_link_check_costs_linear_time():
    """S2 runs on every string printed with markup on, so somebody's text decides how many
    link and hiding spans it gets. Testing every hiding span per link was quadratic
    (measured: 8000 pairs 4.2 s, 20000 pairs 28 s, about 9.5 s at the 12000 here); the sweep
    takes about 0.08 s. The limit of 1.5 s is far above the sweep and far below the quadratic
    pass, and the best of three runs counts, so a briefly loaded runner does not make it
    flaky. MUTATION: put back the per-link `any(...)` over all hiding spans - red."""
    import time

    from rich.text import Span, Text

    console = _console()
    pairs = 12000
    best = None
    for _ in range(3):
        text = Text("xy" * pairs)
        text.spans[:] = [span for i in range(pairs)
                         for span in (Span(2 * i, 2 * i + 1, "conceal"),
                                      Span(2 * i + 1, 2 * i + 2, "link https://e.org"))]
        start = time.perf_counter()
        console._own_links_only(text)
        elapsed = time.perf_counter() - start
        best = elapsed if best is None else min(best, elapsed)
        assert not any(console.get_style(span.style).link for span in text.spans)
        if best < 1.5:
            break
    assert best < 1.5, best


@pytest.mark.parametrize("style", ["white on black", "on #000000", "reverse", "conceal"])
def test_a_hiding_print_style_drops_every_markup_link(style):
    """`print` lays its `style` over the output after `render`, so S2 never sees that
    background on a label: under `white on black`, a `[black]` part of the label is black on
    black. A print style that hides text drops every markup link printed under it; the
    label stays. MUTATIONS: ignore the print style in `_own_links_only` - red; do not restore
    it after the call - red on the plain print after it."""
    console = _console()
    for markup in ("[link=file:///tmp/ab]/tmp/a[black]b[/][/link]",
                   "[link=file:///tmp/ab]/tmp/ab[/link]"):
        start = len(_out(console))
        console.print(markup, style=style)
        out = _out(console)[start:]
        assert _targets(out) == [], (style, markup, out)
    start = len(_out(console))
    console.print("[link=file:///tmp/ab]/tmp/ab[/link]")
    assert set(_targets(_out(console)[start:])) == {"file:///tmp/ab"}


def test_a_print_style_without_a_background_keeps_the_own_link():
    """Only a hiding print style counts: a colour or `bold` keeps VAF's own link. MUTATION:
    treat every print style as hiding - red."""
    for style in ("bold", "cyan", "bold magenta"):
        console = _console()
        console.print("[link=file:///tmp/ab]/tmp/ab[/link]", style=style)
        assert set(_targets(_out(console))) == {"file:///tmp/ab"}, style


def test_a_hiding_base_style_drops_the_link():
    """`render_str(text, style=...)` gives the parsed text a base style under every span,
    which Rich renders with the text: a background there lies under the whole label. (With
    the highlighter on, Rich returns a new text without that base style, so nothing is
    drawn under the label and the link rightly stays.) MUTATION: ignore the base style -
    red."""
    console = _console()
    markup = "[link=file:///tmp/ab]/tmp/ab[/link]"
    text = console.render_str(markup, style="on black", highlight=False)
    assert text.style == "on black"
    assert not any(console.get_style(span.style).link for span in text.spans)
    text = console.render_str(markup, style="bold", highlight=False)
    assert any(console.get_style(span.style).link == "file:///tmp/ab" for span in text.spans)


def test_nested_links_cost_linear_time():
    """In nested links every label runs to the end of the text, so reading each label
    before its target is quadratic (measured: `"[link=file:///a]x" * 4000 + "[/link]" *
    4000` took 9.5 s against Rich's own 0.15 s). The label is read only once it has its
    target's length. Spans built directly, so Rich's own parse is not timed; the limit and
    the best of three runs as in the test above. MUTATION: read the label (control
    characters, `_unseen`) before the target checks again - red."""
    import time

    from rich.text import Span, Text

    console = _console()
    links = 4000
    best = None
    for _ in range(3):
        text = Text("x" * links)
        text.spans[:] = [Span(i, links, "link file:///a") for i in range(links)]
        start = time.perf_counter()
        console._own_links_only(text)
        elapsed = time.perf_counter() - start
        best = elapsed if best is None else min(best, elapsed)
        assert not any(console.get_style(span.style).link for span in text.spans)
        if best < 1.5:
            break
    assert best < 1.5, best


def test_a_label_longer_than_its_target_is_neither_copied_nor_read(monkeypatch):
    """The order the timing test above relies on, pinned without a clock: a label longer
    than its own target cannot be the target's decoded path, so the check refuses it
    before it scans the label (a tab, a line break, control characters), and the console
    refuses it before it copies the label out of the text. Nested links over a long text
    would otherwise copy and scan every label. MUTATIONS: test the tab and line break
    before the length - red; drop the length test before the copy in `_own_links_only` -
    red."""
    import vaf.core.terminal_text as tt
    from rich.text import Span, Text

    class Unread(str):
        def __contains__(self, item):
            raise AssertionError("label scanned")

        def __iter__(self):
            raise AssertionError("label read")

    assert tt._own_file_link("file:///a", Unread("/" + "x" * 50)) is False

    real = tt._own_file_link
    checked = []
    def recording(target, label):
        checked.append((target, label))
        return real(target, label)

    monkeypatch.setattr(tt, "_own_file_link", recording)
    links = 50
    text = Text("x" * links)
    text.spans[:] = [Span(i, links, "link file:///a") for i in range(links)]
    _console()._own_links_only(text)
    assert checked, "the short labels still reach the check"
    assert all(len(label) <= len(target) for target, label in checked), checked


def test_escape_markup_keeps_only_vafs_own_link():
    """The entry escape is Rich's own, except for VAF's file link. MUTATION: keep every
    `[link=...]` match - red (the foreign link stays live)."""
    assert escape_markup(None) == ""
    assert escape_markup(42) == "42"
    assert escape_markup("arr[i]") == "arr\\[i]"
    # Through a plain Rich console, as `Panel` titles and Textual parse it: no sink rule.
    out = _render_plain(escape_markup("see [link=https://e.org]x[/link] and " + OWN))
    assert set(_targets(out)) == {"file:///tmp/p"}
    assert "see [link=https://e.org]x[/link] and /tmp/p" in _plain(out)
    # A trailing backslash before a kept link escapes nothing: Rich's rule still applies.
    out = _render_plain(escape_markup("dir\\" + OWN))
    assert set(_targets(out)) == {"file:///tmp/p"} and "dir\\/tmp/p" in _plain(out)


@pytest.mark.parametrize("text, expected", [
    ("[link=https://evil.example]click[/link]", "[link=https://evil.example]click[/link]"),
    ("[link=file://evil.example/share/x]/share/x[/link]",
     "[link=file://evil.example/share/x]/share/x[/link]"),
    ("[link=file:///tmp/a]/tmp/b[/link]", "[link=file:///tmp/a]/tmp/b[/link]"),
    ("arr[i] [docs](https://e.org) - [x] [dim]x[/dim]",
     "arr[i] [docs](https://e.org) - [x] [dim]x[/dim]"),
    ("[link=file:///C:/Users/a/p]C:\\Users\\a\\p[/link]", "C:\\Users\\a\\p"),
    ("see " + OWN + " and [link=https://e.org]x[/link]", "see /tmp/p and [link=https://e.org]x[/link]"),
    (None, ""),
    (42, "42"),
], ids=["https", "host", "label-mismatch", "brackets", "windows", "own-and-foreign", "none",
        "number"])
def test_only_vafs_own_link_becomes_its_label(text, expected):
    """The copy of a reply that leaves the terminal keeps the text as written; only VAF's
    own file link becomes the path it opens. MUTATION: replace every link by its label (no
    `_own_file_link` check) - red on the https, host and mismatch cases."""
    assert own_links_as_labels(text) == expected


def test_an_own_link_with_a_bracketed_path_becomes_that_path():
    """`file_link` of a Next.js route: the label is the raw path. MUTATION: `_rich_escape`
    the label - red (`\\[id]`); return the text unchanged - red."""
    from vaf.core.terminal_text import file_link

    path = os.path.abspath(os.path.join(os.sep, "home", "user", "app", "[id]", "page.tsx"))
    assert own_links_as_labels("Saved: " + file_link(path)) == "Saved: " + path


# ── the narration funnels (vaf/cli/tui.py) ───────────────────────────────────────────

@pytest.fixture
def ui_console(monkeypatch):
    from vaf.cli.tui import UI
    console = _console()
    monkeypatch.setattr(UI, "console", console)
    monkeypatch.setattr(UI, "_console_sinks", [])
    monkeypatch.setattr(UI, "_app_mode", False)
    monkeypatch.delenv("VAF_IN_WORKFLOW_TERMINAL", raising=False)
    monkeypatch.delenv("VAF_IN_SUBAGENT_TERMINAL", raising=False)
    return console


@pytest.fixture
def tui_obj():
    """A TUI without its history and autosuggest files: only the theme and the console."""
    from vaf.cli.themes import ThemeManager
    from vaf.cli.tui import TUI
    tui = TUI.__new__(TUI)
    tui.theme_name = "vaf"
    tui.theme = ThemeManager.get_theme("vaf")
    tui.console = _console()
    return tui


def _assert_as_written(out: str, message: str):
    assert message in _plain(out), out
    assert _targets(out) == [], out
    assert "\x1b[8m" not in out and ";8m" not in out, "hidden text"


def test_the_funnels_show_a_message_as_written(ui_console, tui_obj):
    """MUTATION: drop `escape_markup` in `UI.event` - red; then in `TUI.event`; then in
    `UI.notification` (and `TUI.notification`)."""
    from vaf.cli.tui import UI

    for message in (HOSTILE, PARSEABLE):
        calls = [lambda m: UI.event("Router", m), UI.info, UI.success, UI.warning, UI.error,
                 UI.notification, lambda m: tui_obj.event("Router", m), tui_obj.info,
                 tui_obj.success, tui_obj.warning, tui_obj.error, tui_obj.notification]
        for call in calls:
            for console in (ui_console, tui_obj.console):
                console.file.seek(0)
                console.file.truncate()
            call(message)
            out = _out(ui_console) + _out(tui_obj.console)
            _assert_as_written(out, message)
            # VAF's own colour tags around it were applied, not shown.
            assert "[/" not in _plain(out).replace(message, ""), out
    UI.info("saved " + OWN)
    assert set(_targets(_out(ui_console))) == {"file:///tmp/p"}


def test_the_funnel_sinks_still_get_the_raw_message(ui_console):
    """The sinks and the web log are not markup consumers: they get the message unchanged."""
    from vaf.cli.tui import UI

    seen = []
    UI.add_console_sink(lambda t, m, s: seen.append(m))
    UI.warning(HOSTILE)
    assert seen == [HOSTILE]


def test_a_message_without_brackets_prints_as_before(ui_console):
    """The escape changes nothing but brackets: the line is byte for byte what the old
    format string printed (emoji codes, highlighting and width included). MUTATION: escape
    with a function that also changes `:`-codes or wraps - red."""
    from vaf.cli.tui import UI

    message = "3 tools selected :CD: https://example.org/a_b 42.5s C:/path ok"
    UI.event("Router", message, style="info")
    reference = _console()
    reference.print(f"[cyan]|[/cyan][dim] {'Router':<7}[/dim] [cyan]{message}[/cyan]")
    assert _out(ui_console) == _out(reference)


def test_no_funnel_message_is_escaped_by_hand():
    """The funnels escape; an escape inside a funnel call shows backslashes. MUTATION:
    re-add `escape(model_name)` at vaf/cli/cmd/models.py - red."""
    funnels = {"event", "info", "success", "warning", "error", "notification"}
    escapes = {"escape", "escape_markup", "_terminal_echo"}
    found = []
    for path in sorted((ROOT / "vaf").rglob("*.py")):
        tree = ast.parse(path.read_bytes())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in funnels):
                continue
            receiver = node.func.value
            name = receiver.id if isinstance(receiver, ast.Name) else (
                receiver.attr if isinstance(receiver, ast.Attribute) else None)
            if name not in ("UI", "tui"):
                continue
            for arg in list(node.args) + [k.value for k in node.keywords]:
                for sub in ast.walk(arg):
                    if isinstance(sub, ast.Call):
                        func = sub.func
                        called = func.id if isinstance(func, ast.Name) else (
                            func.attr if isinstance(func, ast.Attribute) else None)
                        if called in escapes:
                            found.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}")
    assert not found, found


def test_message_box_and_status_panels_show_text_as_written(tui_obj):
    """A user's message in its box is text. The `/context` and `/session current` panels
    interpolate the model-written intent goal and the session name inside the run loop,
    which no unit can drive, so their escapes are pinned in the source. MUTATION: drop
    `escape_markup` in `message_box` - red."""
    tui_obj.message_box("[/x] [i] " + PARSEABLE, role="user")
    _assert_as_written(_out(tui_obj.console), "[/x] [i] ")
    assert "arr[i]" in _plain(_out(tui_obj.console))
    # Without a parse error the console's fallback cannot help: only the escape does.
    tui_obj.console.file.seek(0)
    tui_obj.console.file.truncate()
    tui_obj.message_box(PARSEABLE, role="user")
    _assert_as_written(_out(tui_obj.console), PARSEABLE)
    source = (ROOT / "vaf" / "cli" / "cmd" / "run.py").read_text(encoding="utf-8")
    assert "Intent: {escape_markup(status['intent_goal']) or 'Not set'}" in source
    assert "Name: {escape_markup(current_session.name)}" in source


# ── the reply stream (vaf/cli/cmd/run.py) ────────────────────────────────────────────

@pytest.mark.parametrize("module", ["run.py", "debug.py"])
def test_run_prints_no_reply_with_markup_on(module):
    """The reply stream and `vaf debug`'s answers are model text. MUTATION: set one stream
    print back to `markup=True` - red; one of `vaf debug`'s answer prints - red."""
    tree = ast.parse((ROOT / "vaf" / "cli" / "cmd" / module).read_bytes())
    on = [node.lineno for node in ast.walk(tree)
          if isinstance(node, ast.keyword) and node.arg == "markup"
          and isinstance(node.value, ast.Constant) and node.value.value is True]
    assert not on, on


class _StreamTui:
    primary = "#00d4ff"

    def __init__(self):
        self.console = _console()
        self.errors = []

    def spinner(self, message=""):
        return contextlib.nullcontext()

    def newline(self):
        self.console.print()

    def error(self, message):
        self.errors.append(message)


class _Session:
    id = "sess-markup-test"
    metadata = {}

    def __init__(self):
        self.messages = []

    def add_message(self, role, content, **kwargs):
        self.messages.append((role, content))


def test_a_streamed_reply_prints_as_written_and_is_saved(quiet_run_module):
    """The real `_process_agent_message`, 7-character chunks. MUTATION: `markup=True` at
    the answer print - red (MarkupError on a chunk, the answer is not saved)."""
    import vaf.cli.cmd.run as run_mod

    reply = "[link=https://e.org]x[/link] Use arr[i] and - [x] done [/etc/hosts] :CD:"
    tui, session = _StreamTui(), _Session()
    run_mod._process_agent_message(FakeAgent(reply), "hi", tui, session)
    out = _out(tui.console)
    assert tui.errors == []
    assert reply in _plain(out), out
    assert _targets(out) == []
    assert [role for role, _ in session.messages] == ["assistant"]


def _mirror(quiet_run_module, monkeypatch) -> list:
    """What `_process_agent_message` sends to the browser as the reply, in order."""
    sent = []
    monkeypatch.setattr(quiet_run_module.web, "emit_agent_message",
                        lambda role, content, **k: sent.append(content))
    return sent


#: Brackets a reply holds as text: an index, a Markdown link, a task box, a Next.js route,
#: a bracket after a word holding "dim", a foreign link and the engine's own status line.
BRACKET_REPLY = ("Use arr[i] and matrix[i][j]. See [the docs](https://e.org). - [x] done "
                 "app/[id]/page.tsx a[0] and the dimension[1] [link=https://e.org]x[/link] "
                 "[Generation stopped by user]")


def test_the_stored_and_mirrored_reply_keeps_its_brackets(quiet_run_module, monkeypatch):
    """The REAL `_process_agent_message`, 7-character chunks: the copy the chat stores and
    the browser is shown is the reply as written. MUTATION: delete every `[...]` group in
    `_reply_web_copy` again - red. MUTATION: put back only the `[...dim...]` to `<think>`
    rule - red (the `dimension[1]` case)."""
    import inspect

    import vaf.cli.cmd.run as run_mod

    sent = _mirror(quiet_run_module, monkeypatch)
    tui, session = _StreamTui(), _Session()
    run_mod._process_agent_message(FakeAgent(BRACKET_REPLY), "hi", tui, session)
    assert session.messages == [("assistant", BRACKET_REPLY)]
    assert sent and sent[-1] == BRACKET_REPLY
    assert BRACKET_REPLY in _plain(_out(tui.console))           # the terminal, unchanged

    # One copy rule at every exit, and none of it rewrites a bracket group.
    assert "convert_rich_to_xml" not in (ROOT / "vaf" / "cli" / "cmd" / "run.py").read_text(
        encoding="utf-8")
    body = " ".join(inspect.getsource(run_mod._process_agent_message).split())
    assert "clean_text = _reply_web_copy(current_full_text)" in body
    assert "clean_ack = _reply_web_copy(str(result_text))" in body
    assert "agent.last_turn, _reply_web_copy)" in body
    assert "\\[" not in inspect.getsource(run_mod._reply_web_copy)


def test_an_own_link_in_a_reply_is_stored_as_its_path(quiet_run_module, monkeypatch,
                                                       tmp_path):
    """A coder link the model repeats is VAF's own markup: the copy shows the path it opens,
    and nothing else changes. MUTATION: drop the `own_links_as_labels` call from
    `_reply_web_copy` - red."""
    import vaf.cli.cmd.run as run_mod
    from vaf.core.terminal_text import file_link

    path = os.path.abspath(tmp_path / "app" / "[id]")
    reply = "Created " + file_link(path) + " with arr[i]."
    sent = _mirror(quiet_run_module, monkeypatch)
    session = _Session()
    run_mod._process_agent_message(FakeAgent(reply), "hi", _StreamTui(), session)
    assert session.messages == [("assistant", "Created " + path + " with arr[i].")]
    assert sent[-1] == "Created " + path + " with arr[i]."


def test_an_async_ack_is_mirrored_as_written(quiet_run_module, monkeypatch):
    """The acknowledgement of a started background task is not streamed; its own mirror
    keeps its brackets too. MUTATION: delete the `[...]` groups at the async-ack mirror -
    red."""
    import vaf.cli.cmd.run as run_mod

    sent = _mirror(quiet_run_module, monkeypatch)
    session = _Session()
    run_mod._process_agent_message(
        FakeAgent(lambda cb: "[ASYNC_ACK]Started task [abc] for arr[i]"), "go", _StreamTui(),
        session)
    assert sent == ["Started task [abc] for arr[i]"]
    assert session.messages == [("assistant", "Started task [abc] for arr[i]")]


def test_the_german_answer_heuristic_still_folds_english_reasoning():
    """A local model's English reasoning before a German answer, without `<think>` tags, is
    still folded away as reasoning in the browser's copy. MUTATION: delete the heuristic -
    red."""
    import vaf.cli.cmd.run as run_mod

    reply = ("Let me think about this. The user wants a greeting, so I should answer in "
             "German. Hallo! Wie kann ich dir heute helfen?")
    copy = run_mod._reply_web_copy(reply)
    assert copy.startswith("<think>Let me think about this.")
    assert copy.endswith("</think>\n\nHallo! Wie kann ich dir heute helfen?")
    assert run_mod._reply_web_copy("") == "" and run_mod._reply_web_copy(None) == ""


# ── the sub-agent drain ──────────────────────────────────────────────────────────────

class _Ipc:
    def __init__(self, tasks):
        self.tasks = tasks
        self.consumed = []

    def get_pending_results_for(self, session_id, own_task_ids=None):
        return list(self.tasks)

    def get_paused_workflow_for_task(self, task_id):
        return PAUSED if task_id == "t-pause" else None

    def claim_paused_workflow(self, workflow_id):
        return PAUSED

    def consume_result(self, task_id):
        self.consumed.append(task_id)


DRAIN_RESULT = ("[/x] [conceal]h[/conceal] [link=https://e.org]y[/link] arr[i] "
                "[link=file:///tmp/p]/tmp/p[/link]")
#: A workflow paused on a sub-agent; its run resumes with a final output of its own.
PAUSED = SimpleNamespace(workflow_id="w1", workflow_name="[i]resumed [/x]", template_id="",
                         user_scope_id=None, username=None, user_role=None)
FINAL_OUTPUT = "[/y] final [conceal]f[/conceal]"


def _drain_tasks():
    done = SimpleNamespace(task_id="t-ok", status="completed", agent_type="coder[i]",
                           result=DRAIN_RESULT, error=None, created_at=None,
                           completed_at=None)
    failed = SimpleNamespace(task_id="t-err", status="failed", agent_type="coder[i]",
                             result=None, error="[/]", created_at=None, completed_at=None)
    workflow = SimpleNamespace(task_id="t-wf", status="completed",
                               agent_type="workflow:[i]flow [/x]", result=DRAIN_RESULT,
                               error=None, created_at=None, completed_at=None)
    resumes = SimpleNamespace(task_id="t-pause", status="completed", agent_type="coder[i]",
                              result="ok", error=None, created_at=None, completed_at=None)
    return [done, failed, workflow, resumes]


class _Engine:
    """The workflow engine the drain resumes a paused run with: it finishes at once."""

    def __init__(self, *args, **kwargs):
        pass

    def resume_workflow(self, paused, result):
        return SimpleNamespace(paused=False, success=True, final_output=FINAL_OUTPUT)


def _drain(monkeypatch, console):
    import vaf.cli.cmd.run as run_mod
    import vaf.core.subagent_ipc as ipc_mod

    import vaf.workflows.engine as engine_mod

    ipc = _Ipc(_drain_tasks())
    monkeypatch.setattr(ipc_mod, "get_ipc", lambda: ipc)
    monkeypatch.setattr(ipc_mod, "get_current_session_id", lambda: "sess-markup-test")
    monkeypatch.setattr(engine_mod, "WorkflowEngine", _Engine)
    monkeypatch.setattr(engine_mod, "identity_for_engine", lambda *a, **k: {})
    agent = SimpleNamespace(history=[], _async_subagent_tasks={}, tools=[])
    errors = []
    tui = SimpleNamespace(console=console, info=lambda m: None, error=errors.append,
                          primary="#00d4ff")
    found = run_mod._check_subagent_results(tui, agent)
    assert errors == [], errors
    return ipc, found


def test_the_drain_shows_results_as_written_and_consumes_them(monkeypatch):
    """Every task is consumed and every panel (a result, an error, a finished workflow, a
    paused workflow resumed to its end) shows the text as written, through VAF's console and
    through a plain Rich console (the way the app's Textual lane renders the panels, with no
    TerminalConsole fallback). MUTATION: drop `escape_markup` on `result_preview` - red
    (VAF's own tags show, and through a plain console MarkupError and no `consume_result`);
    on `task.error` - red; on `workflow_name`, `paused_wf.workflow_name` or `output_preview`
    - red, each."""
    from rich.console import Console as PlainConsole

    everything = ["t-ok", "t-err", "t-wf", "t-pause"]
    console = _console()
    ipc, found = _drain(monkeypatch, console)
    assert ipc.consumed == everything
    assert len(found) == 3
    out = _out(console)
    plain = _plain(out)
    assert "[/x] [conceal]h[/conceal] [link=https://e.org]y[/link] arr[i] /tmp/p" in plain
    assert "Agent: coder[i]" in plain and "Error: [/]" in plain
    assert "Workflow: [i]flow [/x]" in plain and "Workflow: [i]resumed [/x]" in plain
    assert FINAL_OUTPUT in plain
    assert "[bold" not in plain and "[cyan]" not in plain, "VAF's own markup shown as text"
    assert set(_targets(out)) == {"file:///tmp/p"}

    plain_console = PlainConsole(file=io.StringIO(), force_terminal=True,
                                 color_system="truecolor", width=200)
    ipc, _ = _drain(monkeypatch, plain_console)
    assert ipc.consumed == everything
    out = plain_console.file.getvalue()
    assert set(_targets(out)) == {"file:///tmp/p"}
    assert "Error: [/]" in _plain(out) and FINAL_OUTPUT in _plain(out)


# ── the CLI commands ─────────────────────────────────────────────────────────────────

@pytest.fixture
def session_cli(monkeypatch, tmp_path):
    import vaf.core.session as session_mod
    import vaf.core.identity_binding as identity_binding

    real = session_mod.SessionManager(storage_dir=str(tmp_path))
    session = session_mod.Session(id="s1", name="[i]Plan[/i] [/x]")
    session.add_message("user", "[/x] " + "a" * 600 + " :CD: arr[i] - [x] done")
    # Without a lone closing tag the console's fallback cannot show it as text by itself.
    parseable = session_mod.Session(id="s2", name="[i]Plan[/i]")
    parseable.add_message("user", "arr[i] - [x] done [conceal]h[/conceal] :CD:")
    sessions = {"s1": session, "s2": parseable}
    rows = [{"id": "s1", "name": "[i]Plan[/i]", "message_count": 1,
             "updated_at": "2026-01-01T00:00:00", "summary": "[conceal]h[/conceal] C:\\d\\"}]
    found = [{"session_id": "s1", "session_name": "[i]Plan[/i]", "role": "user",
              "match": "[conceal]h[/conceal]"}]
    manager = SimpleNamespace(list=lambda limit=20: rows, load=lambda sid: sessions[sid],
                              search=lambda q, limit=10, user_scope_id=None: found,
                              export=real.export)
    monkeypatch.setattr(session_mod, "get_manager", lambda: manager)
    monkeypatch.setattr(identity_binding, "resolve_owner_identity",
                        lambda: SimpleNamespace(scope="ab12cd34", username="alice"))
    return SimpleNamespace(app=session_mod.session_app, session=session, real=real)


#: A fixed width, so a table's columns do not depend on the terminal running the tests.
WIDE = {"COLUMNS": "120"}


def test_session_list_load_search_show_names_as_written(session_cli):
    """MUTATION: the name cell of `vaf session list` back to a markup string - red; the
    summary cell through `escape_markup` instead of `Text` - red (the final backslash of
    `C:\\d\\` shows twice)."""
    runner = CliRunner(env=WIDE)
    out = runner.invoke(session_cli.app, ["list"])
    assert out.exit_code == 0, out.output
    assert "[i]Plan[/i]" in out.output and "[conceal]h[/conceal] C:\\d\\ " in out.output
    out = runner.invoke(session_cli.app, ["load", "s1"])
    assert out.exit_code == 0, out.output
    assert "[i]Plan[/i] [/x]" in out.output and "[green]" not in out.output
    out = runner.invoke(session_cli.app, ["search", "[i]q"])
    assert out.exit_code == 0, out.output
    assert "[i]q" in out.output and "[i]Plan[/i]" in out.output
    assert "[conceal]h[/conceal]" in out.output


def test_session_export_to_a_pipe_is_the_export(session_cli):
    """MUTATION: drop `soft_wrap=True` - red (wrapped at 80 columns, the JSON does not
    parse); drop `markup=False` - red (`[x]` vanishes); drop `emoji=False` together with the
    console's emoji default - red (`:CD:` becomes a symbol)."""
    runner = CliRunner()
    out = runner.invoke(session_cli.app, ["export", "s1", "--format", "json"])
    assert out.exit_code == 0, out.output
    assert json.loads(out.output) == json.loads(
        session_cli.real.export(session_cli.session, format="json"))
    out = runner.invoke(session_cli.app, ["export", "s1"])
    assert out.exit_code == 0, out.output
    assert "[/x] " + "a" * 600 + " :CD: arr[i] - [x] done" in out.output
    out = runner.invoke(session_cli.app, ["export", "s2"])
    assert out.exit_code == 0, out.output
    assert "# [i]Plan[/i]" in out.output
    assert "arr[i] - [x] done [conceal]h[/conceal] :CD:" in out.output


@pytest.fixture
def automation_cli(monkeypatch):
    import vaf.cli.autoclose as autoclose
    import vaf.core.automation as automation_mod

    task = SimpleNamespace(id="a1", name="[i]daily [/x]", enabled=True, frequency="daily",
                           time="06:00", schedule_label="daily 06:00",
                           next_run_label="tomorrow")
    listed = SimpleNamespace(**{**vars(task), "name": "[i]daily"})

    def run_task(t, callback=None, new_terminal=False):
        callback("[/x] arr[i] :CD: [conceal]h[/conceal]")
        raise RuntimeError("[/y] [i]boom")

    manager = SimpleNamespace(list=lambda enabled_only=False: [listed],
                              get=lambda tid: task, run_task=run_task)
    monkeypatch.setattr(automation_mod, "get_manager", lambda: manager)
    monkeypatch.setattr(autoclose, "finish_terminal", lambda **k: None)
    return automation_mod.automation_app


def test_automation_run_prints_the_output_as_written(automation_cli):
    """MUTATION: `markup=True` in `print_output` - red; drop `escape_markup` on the failure
    line - red (`[i]` vanishes)."""
    out = CliRunner().invoke(automation_cli, ["run", "a1"])
    assert "[/x] arr[i] :CD: [conceal]h[/conceal]" in out.output, out.output
    assert "Running: [i]daily [/x]" in out.output
    assert "Automation failed: [/y] [i]boom" in out.output
    # VAF's own colour tags around the name and the error were applied, not shown.
    assert "[cyan]" not in out.output and "[red]" not in out.output, out.output


def test_automation_list_and_run_show_names_as_written(automation_cli):
    """MUTATION: the name cell of `vaf automation list` back to a markup string - red."""
    out = CliRunner(env=WIDE).invoke(automation_cli, ["list"])
    assert out.exit_code == 0, out.output
    assert "[i]daily" in out.output


def test_security_lists_show_names_as_written(monkeypatch, ui_console):
    """A scanned skill chooses its own name. MUTATION: drop `escape_markup` on `name` -
    red; on the finding's message - red."""
    import vaf.cli.cmd.security as security_cmd
    import vaf.core.threat_db as threat_db

    monkeypatch.setattr(threat_db, "list_threats", lambda: [
        {"sha256": "ab" * 32, "kind": "skill", "name": "[conceal]evil[/conceal]",
         "reason": "[/x]", "listed_at": "2026-01-01"}])
    monkeypatch.setattr(threat_db, "threat_db_path", lambda: Path("/tmp/threats.json"))
    result = CliRunner().invoke(security_cmd.app, ["threats", "list"])
    assert result.exit_code == 0, result.output
    out = _out(ui_console)
    _assert_as_written(out, "skill [conceal]evil[/conceal] [/x] (2026-01-01)")

    ui_console.file.seek(0)
    ui_console.file.truncate()
    monkeypatch.setattr(security_cmd, "collect_security_findings", lambda: [
        {"severity": "high", "code": "c[i]1", "message": "[i]open [conceal]x[/conceal]"}])
    result = CliRunner().invoke(security_cmd.app, ["doctor"])
    assert result.exit_code == 0, result.output
    _assert_as_written(_out(ui_console), "c[i]1 [i]open [conceal]x[/conceal]")


def test_a_markup_like_name_and_preview_show_as_written_in_the_inbox(monkeypatch,
                                                                     ui_console):
    """MUTATION: the preview cell back to a markup string - red; the name cell - red; the
    name cell through `escape_markup` instead of `Text` - red (its final backslash shows
    twice). A `Text` cell never turns `:CD:` into a symbol, whatever the console's emoji
    default (that one is pinned by `test_emoji_codes_are_text_on_vafs_consoles`)."""
    from vaf.cli.cmd import inbox as inbox_cmd
    from vaf.core import inbox as inbox_mod

    monkeypatch.setattr(inbox_cmd, "_identity", lambda: ("alice", "ab12cd34"))
    row = {"name": "[i]Bob C:\\x\\", "id": "x",
           "preview": "[link=https://e.org]see[/link] [i] :CD:",
           "last_ts": 0, "channel": "whatsapp", "unread": 0, "waits": False,
           "waits_reason": "", "mode": "agent"}
    monkeypatch.setattr(inbox_mod, "list_conversations", lambda *a, **k: {
        "rows": [row], "counts": {"all": 1, "waits": 0, "unread": 0, "agent": 0}})
    result = CliRunner().invoke(inbox_cmd.app, ["list"])
    assert result.exit_code == 0, result.output
    out = _out(ui_console)
    _assert_as_written(out, "[link=https://e.org]see[/link] [i] :CD:")
    assert "[i]Bob C:\\x\\ " in _plain(out)


def test_vaf_git_shows_commit_text_as_written(monkeypatch, ui_console):
    """The suggested commit message is model text the user confirms; a log's subjects and
    authors are whoever committed. MUTATION: drop `markup=False` on the suggestion - red
    (`[conceal]` hides half of what the user confirms); drop `escape_markup` on a log
    subject - red; on its author - red."""
    import vaf.cli.cmd.git as git_cmd

    calls = []

    def run_git(args, cwd=None):
        calls.append(args)
        if args[:1] == ["log"]:
            return 0, "abc123|Fix [conceal]x[/conceal] arr[i]|[i]Mallory|2 days ago", ""
        return 0, "", ""

    monkeypatch.setattr(git_cmd, "run_git", run_git)
    monkeypatch.setattr(git_cmd, "get_changed_files", lambda: (["a.py"], [], []))
    monkeypatch.setattr(git_cmd, "get_staged_diff", lambda: "diff --git a/a.py b/a.py")
    monkeypatch.setattr(git_cmd, "_generate_commit_message_llm",
                        lambda prompt: "Fix login [conceal]and add a backdoor[/conceal] arr[i]")
    result = CliRunner().invoke(git_cmd.app, ["commit", "--auto"], input="n\nmine\n")
    assert result.exit_code == 0, result.output
    _assert_as_written(_out(ui_console), "Fix login [conceal]and add a backdoor[/conceal] arr[i]")

    ui_console.file.seek(0)
    ui_console.file.truncate()
    result = CliRunner().invoke(git_cmd.app, ["log"])
    assert result.exit_code == 0, result.output
    out = _out(ui_console)
    _assert_as_written(out, "abc123 Fix [conceal]x[/conceal] arr[i]")
    assert "[i]Mallory" in _plain(out)


def test_workflow_list_shows_names_as_written(monkeypatch):
    """A workflow in the user's folder is a file the agent wrote. MUTATION: drop
    `escape_markup` on the name - red; on the description - red."""
    import vaf.workflows.templates as templates
    from vaf.cli.cmd import workflow as workflow_cmd

    monkeypatch.setattr(templates, "WORKFLOW_TEMPLATES", {
        "mine": {"name": "[i]Daily [conceal]x[/conceal]",
                 "description": "Reads arr[i] [conceal]hidden[/conceal]"}})
    buf = io.StringIO()
    monkeypatch.setattr("vaf.core.terminal_text.TerminalConsole",
                        lambda **kw: _console(buf))
    workflow_cmd.list_workflows()
    out = buf.getvalue()
    _assert_as_written(out, "mine: [i]Daily [conceal]x[/conceal]")
    _assert_as_written(out, "Reads arr[i] [conceal]hidden[/conceal]")


def test_emoji_codes_are_text_on_vafs_consoles(ui_console):
    """VAF writes no Rich emoji code of its own, so a code in a printed string is somebody's
    text. MUTATION: drop the console's emoji default - red."""
    from rich.table import Table
    from vaf.cli.tui import UI

    UI.info("deploy :CD: done")
    table = Table()
    table.add_column("c")
    table.add_row("a :smile: b")
    ui_console.print(table)
    plain = _plain(_out(ui_console))
    assert "deploy :CD: done" in plain and "a :smile: b" in plain
    # Asked for, the codes still work.
    assert ":smile:" not in _plain(_render_emoji_on("a :smile: b"))


def _render_emoji_on(text: str) -> str:
    console = TerminalConsole(file=io.StringIO(), force_terminal=True, emoji=True, width=200)
    console.print(text)
    return console.file.getvalue()


# ── the tools' live views ────────────────────────────────────────────────────────────

def test_the_coder_panel_shows_a_bracketed_file_name():
    """A Next.js route file. The preview title is parsed by `Text.from_markup`, outside the
    console. MUTATION: drop the escape in the title - red; the files table's name cell back
    to a markup string - red."""
    from vaf.tools.coder import CoderTUI

    console = _console()
    coder = CoderTUI(console, "build", animate=False)
    coder.set_code_preview("app/[id].tsx", "export default 1\n", "code")
    coder.add_file("[slug].tsx", 10)
    console.print(coder.render())
    plain = _plain(_out(console))
    assert "Editing: app/[id].tsx" in plain
    assert "[slug].tsx" in plain


def test_the_research_log_is_text():
    """MUTATION: `Panel(logs)` back - red (`[i]` vanishes and the concealed text is
    hidden; with a lone closing tag among the lines the console's fallback alone would
    show them, so the first log has none)."""
    from vaf.tools.research_agent import ResearchTUI

    console = _console()
    research = ResearchTUI(console, "topic", animate=False)
    research.log("section arr[i] [conceal]h[/conceal]")
    console.print(research.render())
    _assert_as_written(_out(console), "section arr[i] [conceal]h[/conceal]")
    research.log("search failed [/x]")
    console.print(research.render())
    assert "search failed [/x]" in _plain(_out(console))


# ── the full-screen app's toasts ─────────────────────────────────────────────────────

def test_app_toasts_are_text(smoke_app, monkeypatch):  # noqa: F811 - fixture
    """MUTATION: remove the `notify` override - red; make `markup=False` a default instead
    of forced - red (the explicit `markup=True` of a widget's notify)."""
    from textual.app import App
    from textual.widgets._toast import Toast

    seen = []
    real = App.notify

    def spy(self, message, **kwargs):
        seen.append((message, kwargs.get("markup")))
        return real(self, message, **kwargs)

    monkeypatch.setattr(App, "notify", spy)
    app = smoke_app.app

    async def _drive():
        async with app.run_test(size=(110, 32)) as pilot:
            app.notify("a [/] b")
            app.screen.notify("c [i]d", markup=True)
            for _ in range(20):
                await pilot.pause()
                if len(app._notifications) >= 2:
                    break
            # A headless test app mounts no toast rack; the toast a terminal would show is
            # rendered from the stored notification by Textual's own Toast.
            return [str(Toast(note).render()) for note in app._notifications]

    shown = asyncio.run(_drive())
    smoke_app.bridge.shutdown()
    assert seen == [("a [/] b", False), ("c [i]d", False)]
    assert any("a [/] b" in text for text in shown), shown
    assert any("c [i]d" in text for text in shown), shown
