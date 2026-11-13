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
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from test_tui_agent_bridge import FakeAgent, quiet_run_module  # noqa: F401 - fixture
from test_tui_app_smoke import smoke_app  # noqa: F401 - fixture
from vaf.core.terminal_text import TerminalConsole, escape_markup

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
    from vaf.tools.coder import _get_clickable_path
    coder = _get_clickable_path("/tmp/My Project")
    return [
        (coder, "file:///tmp/My%20Project"),
        # agent.py: "file:///" + the path with backslashes turned to slashes
        ("[link=file:////home/user/p]/home/user/p[/link]", "file:////home/user/p"),
        ("[link=file:///C:/Users/a/p]C:\\Users\\a\\p[/link]", "file:///C:/Users/a/p"),
    ]


@pytest.mark.parametrize("shape", ["print", "table", "rule"])
def test_a_markup_link_survives_only_as_vafs_own_file_link(shape):
    """A foreign target, a file link whose label is not its path and a file link labelled
    with a bare name all keep their label and lose the link; the three shapes VAF itself
    writes keep theirs. MUTATION: drop the span filter in `render_str` - red (the foreign
    links come back); compare with `startswith("file:")` instead of the path - red (the
    mismatched labels are linked)."""
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

    for markup, label in (("[link=https://e.org]x[/link]", "x"),
                          ("[link=file:///etc/passwd]https://bank.example[/link]",
                           "https://bank.example"),
                          ("[link=file:///tmp/project]project[/link]", "project")):
        out = render(markup)
        assert _targets(out) == [], (markup, out)
        assert label in _plain(out)
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
                            found.append(f"{path.relative_to(ROOT)}:{node.lineno}")
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

def test_run_prints_no_reply_with_markup_on():
    """MUTATION: set one stream print back to `markup=True` - red."""
    tree = ast.parse((ROOT / "vaf" / "cli" / "cmd" / "run.py").read_bytes())
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


# ── the sub-agent drain ──────────────────────────────────────────────────────────────

class _Ipc:
    def __init__(self, tasks):
        self.tasks = tasks
        self.consumed = []

    def get_pending_results_for(self, session_id, own_task_ids=None):
        return list(self.tasks)

    def get_paused_workflow_for_task(self, task_id):
        return None

    def consume_result(self, task_id):
        self.consumed.append(task_id)


DRAIN_RESULT = ("[/x] [conceal]h[/conceal] [link=https://e.org]y[/link] arr[i] "
                "[link=file:///tmp/p]/tmp/p[/link]")


def _drain_tasks():
    done = SimpleNamespace(task_id="t-ok", status="completed", agent_type="coder[i]",
                           result=DRAIN_RESULT, error=None, created_at=None,
                           completed_at=None)
    failed = SimpleNamespace(task_id="t-err", status="failed", agent_type="coder[i]",
                             result=None, error="[/]", created_at=None, completed_at=None)
    return [done, failed]


def _drain(monkeypatch, console):
    import vaf.cli.cmd.run as run_mod
    import vaf.core.subagent_ipc as ipc_mod

    ipc = _Ipc(_drain_tasks())
    monkeypatch.setattr(ipc_mod, "get_ipc", lambda: ipc)
    monkeypatch.setattr(ipc_mod, "get_current_session_id", lambda: "sess-markup-test")
    agent = SimpleNamespace(history=[], _async_subagent_tasks={})
    tui = SimpleNamespace(console=console, info=lambda m: None, primary="#00d4ff")
    found = run_mod._check_subagent_results(tui, agent)
    return ipc, found


def test_the_drain_shows_results_as_written_and_consumes_them(monkeypatch):
    """Both tasks are consumed and their panels show the text as written, through VAF's
    console and through a plain Rich console (the way the app's Textual lane renders the
    panels, with no TerminalConsole fallback). MUTATION: drop `escape_markup` on
    `result_preview` - red (VAF's own tags show, and through a plain console MarkupError
    and no `consume_result`); drop it on `task.error` - red likewise."""
    from rich.console import Console as PlainConsole

    console = _console()
    ipc, found = _drain(monkeypatch, console)
    assert ipc.consumed == ["t-ok", "t-err"]
    assert len(found) == 2
    out = _out(console)
    plain = _plain(out)
    assert "[/x] [conceal]h[/conceal] [link=https://e.org]y[/link] arr[i] /tmp/p" in plain
    assert "Agent: coder[i]" in plain and "Error: [/]" in plain
    assert "[bold" not in plain and "[cyan]" not in plain, "VAF's own markup shown as text"
    assert set(_targets(out)) == {"file:///tmp/p"}

    plain_console = PlainConsole(file=io.StringIO(), force_terminal=True,
                                 color_system="truecolor", width=200)
    ipc, _ = _drain(monkeypatch, plain_console)
    assert ipc.consumed == ["t-ok", "t-err"]
    out = plain_console.file.getvalue()
    assert set(_targets(out)) == {"file:///tmp/p"}
    assert "Error: [/]" in _plain(out)


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
             "updated_at": "2026-01-01T00:00:00", "summary": "[conceal]h[/conceal]"}]
    found = [{"session_id": "s1", "session_name": "[i]Plan[/i]", "role": "user",
              "match": "[conceal]h[/conceal]"}]
    manager = SimpleNamespace(list=lambda limit=20: rows, load=lambda sid: sessions[sid],
                              search=lambda q, limit=10, user_scope_id=None: found,
                              export=real.export)
    monkeypatch.setattr(session_mod, "get_manager", lambda: manager)
    monkeypatch.setattr(identity_binding, "resolve_owner_identity",
                        lambda: SimpleNamespace(scope="ab12cd34", username="alice"))
    return SimpleNamespace(app=session_mod.session_app, session=session, real=real)


def test_session_list_load_search_show_names_as_written(session_cli):
    """MUTATION: drop `escape_markup` on the name cell of `vaf session list` - red."""
    runner = CliRunner()
    out = runner.invoke(session_cli.app, ["list"])
    assert out.exit_code == 0, out.output
    assert "[i]Plan[/i]" in out.output and "[conceal]h[/conceal]" in out.output
    out = runner.invoke(session_cli.app, ["load", "s1"])
    assert out.exit_code == 0, out.output
    assert "[i]Plan[/i] [/x]" in out.output and "[green]" not in out.output
    out = runner.invoke(session_cli.app, ["search", "[i]q"])
    assert out.exit_code == 0, out.output
    assert "[i]q" in out.output and "[i]Plan[/i]" in out.output
    assert "[conceal]h[/conceal]" in out.output


def test_session_export_to_a_pipe_is_the_export(session_cli):
    """MUTATION: drop `soft_wrap=True` - red (wrapped at 80 columns, the JSON does not
    parse); drop `markup=False` - red (`[x]` vanishes); drop `emoji=False` - red (`:CD:`
    becomes a symbol)."""
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
    """MUTATION: drop `escape_markup` at the name cell of `vaf automation list` - red."""
    out = CliRunner().invoke(automation_cli, ["list"])
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
    """MUTATION: drop `escape_markup` on the preview cell - red; on the name cell - red."""
    from vaf.cli.cmd import inbox as inbox_cmd
    from vaf.core import inbox as inbox_mod

    monkeypatch.setattr(inbox_cmd, "_identity", lambda: ("alice", "ab12cd34"))
    row = {"name": "[i]Bob", "id": "x", "preview": "[link=https://e.org]see[/link] [i]",
           "last_ts": 0, "channel": "whatsapp", "unread": 0, "waits": False,
           "waits_reason": "", "mode": "agent"}
    monkeypatch.setattr(inbox_mod, "list_conversations", lambda *a, **k: {
        "rows": [row], "counts": {"all": 1, "waits": 0, "unread": 0, "agent": 0}})
    result = CliRunner().invoke(inbox_cmd.app, ["list"])
    assert result.exit_code == 0, result.output
    out = _out(ui_console)
    _assert_as_written(out, "[link=https://e.org]see[/link] [i]")
    assert "[i]Bob" in _plain(out)


# ── the tools' live views ────────────────────────────────────────────────────────────

def test_the_coder_panel_shows_a_bracketed_file_name():
    """A Next.js route file. The preview title is parsed by `Text.from_markup`, outside the
    console. MUTATION: drop the escape in the title - red; in the files table - red."""
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
