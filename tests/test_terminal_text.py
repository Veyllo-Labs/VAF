# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Text from other parties reaches the terminal without its control characters.

MEASURED BEFORE THE FIX (Textual 8.2.8, Rich 14.3.3, headless): a window-title sequence, a
screen clear, an OSC 8 link, an OSC 52 clipboard write, the C1 forms of CSI and OSC, NUL, DEL
and U+202E all reached the output of every transcript widget of the full-screen app and of
`Console.print` with `str`, `Markdown`, `Panel` and `Table`. Only the modern lane's queued web
input was cleaned, by a hand copy in run.py.

The fix sits at each renderer's sink (one Textual line filter, one Rich console class, the
bare-stdout and JSON lines, the last-resort log formatter), all on one definition,
`vaf.core.terminal_text`. Every test input is built from escapes in this source, never from
literal control bytes, and no test prints one.
"""
import ast
import asyncio
import inspect
import io
import json
import logging
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


def _unsafe(ch: str) -> bool:
    code = ord(ch)
    return (code < 0x20 and ch not in "\n\t") or 0x7F <= code <= 0x9F or ch in BIDI


def _unsafe_in(text: str) -> list:
    return sorted({hex(ord(ch)) for ch in text if _unsafe(ch)})


# ── the definition ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("ch", [chr(c) for c in (0x00, 0x01, 0x07, 0x08, 0x0B, 0x0C, 0x0D,
                                                0x1B, 0x1F, 0x7F, 0x80, 0x85, 0x9B, 0x9D,
                                                0x9F)] + BIDI)
def test_every_control_class_is_removed(ch):
    """C0 but tab and newline, DEL, C1 and the twelve bidirectional controls.
    MUTATION: drop 0x7F-0x9F from the table, or the bidi set - red."""
    assert terminal_text(f"ab{ch}cd") == "abcd"


@pytest.mark.parametrize("text", [
    "tab\tand\nnewline",
    "Grüße äöü",
    "مرحبا",
    "漢字かな",
    "\U0001F468\u200d\U0001F469\u200d\U0001F467",
    "zero\u200bwidth \u200cjoiner \u2060word \ufeffmark",
])
def test_printable_text_and_format_characters_are_kept(text):
    """Umlauts, Arabic, CJK, an emoji with ZWJ, and the zero-width characters, which drive no
    terminal and belong to real text. MUTATION: add U+200D to the table - red."""
    assert terminal_text(text) == text


def test_none_is_empty_and_anything_else_is_its_str():
    assert terminal_text(None) == ""
    assert terminal_text(5) == "5"
    assert terminal_text("") == ""


def test_only_the_characters_go_never_the_rest_of_a_sequence():
    """The visible remainder stays on screen: honest, and no malformed sequence can make a
    parser swallow the wrong amount of text."""
    cleaned = terminal_text(HOSTILE)
    assert not _unsafe_in(cleaned)
    assert "]0;PWNEDTITLE" in cleaned and "xe.fdp" in cleaned and "]52;c;aGk=" in cleaned


def test_a_json_line_escapes_what_json_dumps_leaves_raw_and_parses_back_unchanged():
    row = {"text": HOSTILE, "who": "Bob\u202e", "n": 1, "list": ["\x85", "ä"]}
    line = terminal_json(row)
    assert not _unsafe_in(line)
    assert json.loads(line) == row
    assert "ä" in line, "non-ASCII text stays readable"
    assert terminal_json({"a": "plain"}) == json.dumps({"a": "plain"}, ensure_ascii=False)


# ── the modern lane's echo now uses the one definition ──────────────────────

def test_the_echo_keeps_no_copy_of_its_own():
    """MUTATION: give `_terminal_echo` its own comprehension back - red."""
    import vaf.cli.cmd.run as run_mod

    assert not hasattr(run_mod, "_BIDI_CONTROLS")
    source = inspect.getsource(run_mod._terminal_echo)
    assert "terminal_text(" in source and "unicodedata" not in source


# ── Rich: every console in vaf/ is a TerminalConsole ───────────────────────

def test_no_plain_rich_console_is_constructed_in_vaf():
    """A console built from `rich.console.Console` prints other parties' text with its
    control characters. MUTATION: change one site back to `Console(` - red."""
    offenders = []
    for path in sorted((ROOT / "vaf").rglob("*.py")):
        if "vendor" in path.relative_to(ROOT).parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else (
                func.attr if isinstance(func, ast.Attribute) else "")
            if name == "Console":
                offenders.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}")
    assert not offenders, "use vaf.core.terminal_text.TerminalConsole: " + ", ".join(offenders)


def _rendered(console_kwargs, *renderables) -> str:
    out = io.StringIO()
    console = TerminalConsole(file=out, width=200, **console_kwargs)
    for renderable in renderables:
        console.print(renderable)
    return out.getvalue()


def test_the_console_cleans_str_markdown_panel_and_table():
    """No colour system, so every control character in the output would be the sender's.
    MUTATION: let `render` pass segments unchanged - red."""
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.table import Table

    table = Table()
    table.add_column("c")
    table.add_row(HOSTILE)
    out = _rendered({"force_terminal": False, "color_system": None},
                    HOSTILE, Markdown(HOSTILE), Panel(HOSTILE), table)
    assert not _unsafe_in(out)
    assert "]0;PWNEDTITLE" in out and "xe.fdp" in out


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
    badge, tool name, arguments and output. Scanned twice: each widget's own rendered lines
    (scrolled out or not), and the compositor's full update of the screen on top.
    MUTATION: delete `VafApp.get_line_filters`, or let the filter return its segments
    unchanged - red."""
    from textual.geometry import Region

    from vaf.cli.tui_app.screens import ConfirmScreen
    from vaf.cli.tui_app.widgets import (AgentMessage, EventNote, PeerMessage, SystemNote,
                                         ToolCard, UserMessage, WakeMessage)
    app, bridge = smoke_app.app, smoke_app.bridge
    found = {}

    async def _drive():
        async with app.run_test(size=(220, 120)) as pilot:
            transcript = app.query_one("#transcript")
            card = ToolCard("tool " + HOSTILE, HOSTILE)
            await transcript.mount_all([
                UserMessage(HOSTILE), PeerMessage(HOSTILE, HOSTILE, badge=HOSTILE),
                AgentMessage(static_text=HOSTILE), SystemNote(HOSTILE),
                EventNote(HOSTILE, HOSTILE), WakeMessage(HOSTILE), card])
            card.set_output(HOSTILE)
            card.finish(True, "1s")
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
                            if not seg.control and _unsafe_in(seg.text):
                                found.setdefault(type(widget).__name__, set()).update(
                                    _unsafe_in(seg.text))
            assert widgets > 20, "the scan reached no widgets"

            update = app.screen._compositor.render_full_update()
            screen_text = "".join(seg.text for line in update.strips for strip in line
                                  for seg in strip if not seg.control)
            if _unsafe_in(screen_text):
                found["compositor"] = set(_unsafe_in(screen_text))
            found["_visible"] = ("]0;PWNEDTITLE" in screen_text, "xe.fdp" in screen_text)

    asyncio.run(_drive())
    bridge.shutdown()
    visible = found.pop("_visible")
    assert not found, f"control characters reached the screen: {found}"
    assert visible == (True, True), "the text around a removed character is still shown"


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
        assert "]0;PWNEDTITLE" in result.stdout and "xe.fdp" in result.stdout, argv


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
        assert "]0;PWNEDTITLE" in out
    elif fmt == "json":
        assert json.loads(out) == {"ok": True, "output": HOSTILE}
    else:
        events = [json.loads(line) for line in out.splitlines() if line.strip()]
        assert [e["text"] for e in events if e["type"] == "text_delta"] == [HOSTILE]


# ── the sub-agent terminal and the modern toolbar ───────────────────────────

def test_the_sub_agent_terminal_prints_a_result_without_control_characters(capsys):
    """MUTATION: print the arguments as they are - red."""
    from vaf.cli.cmd.subagent import _safe_print

    _safe_print(HOSTILE, 42, sep=" | ")
    out = capsys.readouterr().out
    assert not _unsafe_in(out)
    assert out.endswith(" | 42\n") and "]0;PWNEDTITLE" in out


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
    assert "Workflow:" in shown and "]0;PWNEDTITLE" in shown
    assert not _unsafe_in(shown), _unsafe_in(shown)


class _NullContext:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ── the engine's own print and the last-resort log handler ──────────────────

def test_the_engine_prints_a_chunk_without_control_characters_when_nobody_streams():
    """Reaching the fallback needs a live backend, so its line is pinned by source.
    MUTATION: drop `terminal_text` from the fallback print - red."""
    import vaf.core.agent as agent_mod

    source = inspect.getsource(agent_mod)
    assert "print(escape(terminal_text(chunk)), end=\"\")" in source
    assert "print(escape(chunk)" not in source


def test_the_last_resort_log_handler_writes_no_control_characters():
    """MUTATION: delete the `setFormatter` line in vaf/main.py - red."""
    import vaf.main  # noqa: F401 - installs the formatter
    from vaf.core.terminal_text import TerminalLogFormatter

    assert isinstance(logging.lastResort.formatter, TerminalLogFormatter)
    record = logging.LogRecord("vaf.test", logging.WARNING, __file__, 1,
                               "mail from %s: %s", ("Bob\x1b]0;T\x07", "\x9b2J ok"), None)
    line = logging.lastResort.format(record)
    assert line == "mail from Bob]0;T: 2J ok"
