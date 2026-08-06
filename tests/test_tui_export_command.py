# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`/export <file>` and `session current`/`list` arrive in the terminal app.

The classic export contract, ported rather than reinvented: the path is taken
verbatim (expanduser only), there is NO default filename - no argument is a
usage line, not a surprise file - the format is markdown unless the extension
says json, and success reads "Exported to: <path>". The write runs on the
agent lane because it reads `self.session`, which a running turn mutates:
exactly the reason `load_session` queues.

`session list` and `session current` were words the classic lane understood;
the app treated every argument as an ID, so both came back as a red
"cannot load list" note.

And the sessions panel finally shows the ID - the one field a user genuinely
NEEDS from that list, because `vaf run --session <id>` takes nothing else,
and it was visible only for unnamed sessions (as their stand-in name).
"""
import threading
import time
from types import SimpleNamespace

import pytest

from vaf.cli.tui_app.agent_bridge import AgentBridge


def _drain(bridge, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not bridge._busy and bridge._queue.empty():
            time.sleep(0.05)
            if not bridge._busy and bridge._queue.empty():
                return
        time.sleep(0.02)


def _bridge(session=None, export_result="# md"):
    notes = []
    events = SimpleNamespace(
        system_note=lambda t: notes.append(("note", t)),
        event_note=lambda t, m, s: notes.append((t, m)),
        presence=lambda *a, **k: None,
        context=lambda *a: None)
    b = AgentBridge(
        SimpleNamespace(get_token_usage=lambda: (1, 2),
                        set_event_sink=lambda s: None, shutdown=lambda: None),
        session or SimpleNamespace(id="green123456", name="probe", messages=[]),
        None, events,
        web_interface_getter=lambda: SimpleNamespace(resolve_gate=lambda *a: True))
    b.exports = []
    b.session_mgr = SimpleNamespace(
        export=lambda s, format="markdown": (b.exports.append((s, format))
                                             or export_result),
        list=lambda limit=20: [])
    return b, notes


# ── the export contract ─────────────────────────────────────────────────────────────

def test_markdown_is_the_default_and_the_note_names_the_path(tmp_path):
    b, notes = _bridge()
    target = tmp_path / "chat.md"
    b.export_session(str(target))
    _drain(b)
    assert target.read_text(encoding="utf-8") == "# md"
    assert b.exports[0][1] == "markdown"
    assert any(k == "note" and f"Exported to: {target}" in m for k, m in notes), notes
    b.shutdown()


def test_a_json_extension_selects_json_case_insensitively(tmp_path):
    b, _ = _bridge(export_result="{}")
    b.export_session(str(tmp_path / "chat.JSON"))
    _drain(b)
    assert b.exports[0][1] == "json"
    b.shutdown()


def test_tilde_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    b, notes = _bridge()
    b.export_session("~/chat.md")
    _drain(b)
    assert (tmp_path / "chat.md").exists()
    b.shutdown()


def test_a_bad_path_is_an_export_note_not_a_crash(tmp_path):
    b, notes = _bridge()
    b.export_session(str(tmp_path / "no" / "such" / "dir" / "x.md"))
    _drain(b)
    assert any(t == "Export" and "failed" in m for t, m in notes), notes
    b.shutdown()


def test_the_write_runs_on_the_lane_thread(tmp_path):
    """It reads self.session, which a running turn mutates - the same reason
    load_session queues instead of touching the session from the UI thread."""
    b, _ = _bridge()
    seen = []
    b.session_mgr = SimpleNamespace(
        export=lambda s, format="markdown": (seen.append(threading.current_thread())
                                             or "# md"))
    b.export_session(str(tmp_path / "c.md"))
    _drain(b)
    assert seen and seen[0] is not threading.main_thread()
    b.shutdown()


def test_no_argument_is_a_usage_line_and_nothing_runs():
    """The classic contract: no default filename. The app half - checked at
    the handler, so nothing is even queued."""
    from vaf.cli.tui_app.app import VafApp

    noted = []
    app = VafApp.__new__(VafApp)
    app.add_event_note = lambda t, m, s: noted.append((t, m, s))
    submitted = []
    app._bridge = SimpleNamespace(export_session=lambda p: submitted.append(p))
    app._cmd_export([])
    assert submitted == []
    assert noted and "usage" in noted[0][1]


# ── the registry and the classic lane ───────────────────────────────────────────────

def test_export_is_registered_with_the_classic_shape():
    from vaf.cli.commands import lookup

    cmd = lookup("export")
    assert cmd is not None
    assert cmd.args == "<file>"
    assert cmd.lane == "agent"


def test_the_classic_lane_branch_exists():
    """KNOWN_COMMANDS is bare_words(): a registered word the modern loop does
    not branch on would be swallowed as a no-op instead of exporting."""
    from pathlib import Path

    import vaf.cli.cmd.run as run_mod

    src = Path(run_mod.__file__).read_text(encoding="utf-8")
    assert 'elif cmd == "export":' in src


# ── session current / list ──────────────────────────────────────────────────────────

def test_session_current_names_the_full_id():
    session = SimpleNamespace(id="green123456", name="probe",
                              messages=[1, 2, 3], created_at="2026-08-05T10:00:00",
                              updated_at="2026-08-05T11:00:00")
    b, notes = _bridge(session=session)
    b.describe_session()
    _drain(b)
    joined = " ".join(m for k, m in notes if k == "note")
    assert "green123456" in joined and "3 messages" in joined, notes
    b.shutdown()


def test_session_words_are_not_treated_as_ids():
    """`session list` and `session current` used to land in load_session and
    come back as a red "cannot load" note."""
    from vaf.cli.tui_app.app import VafApp

    calls = []
    app = VafApp.__new__(VafApp)
    app._bridge = SimpleNamespace(
        load_session=lambda sid: calls.append(("load", sid)),
        describe_session=lambda: calls.append(("describe",)),
        request_session_list=lambda: calls.append(("list",)))
    app._cmd_session(["current"])
    assert calls == [("describe",)]
    calls.clear()
    app._cmd_session(["red654321"])
    assert calls == [("load", "red654321")]


# ── the panel shows the id ──────────────────────────────────────────────────────────

def test_the_panel_rows_carry_the_short_id_and_the_summary():
    import asyncio

    from textual.app import App, ComposeResult
    from textual.widgets import ListView, Static

    from vaf.cli.tui_app.screens import SessionsPanel

    rows = {}

    class _Host(App):
        def compose(self) -> ComposeResult:
            yield SessionsPanel(id="sessions")

    async def _drive():
        app = _Host()
        async with app.run_test(size=(90, 30)) as pilot:
            panel = app.query_one("#sessions", SessionsPanel)
            panel.refresh_sessions([
                {"id": "green123456abcdef", "name": "planning",
                 "message_count": 7, "summary": "a very long summary line that must truncate",
                 "updated_at": "2026-08-05T10:00:00"},
            ], active_id="green123456abcdef")
            await pilot.pause()
            texts = [str(s.content) for s in panel.query(Static)]
            rows["joined"] = " | ".join(texts)

    asyncio.run(_drive())
    joined = rows["joined"]
    assert "green123456a" in joined, joined            # sid[:12]
    assert "7 msg" in joined, joined
    assert "…" in joined and "must truncate" not in joined, (
        f"the summary is not truncated to the panel budget: {joined}")
