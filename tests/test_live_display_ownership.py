# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Who may paint on the terminal, and who has to ask first.

A long-running tool that animates its own panel takes the whole screen. That
was harmless while the only front end was a scrolling prompt; with a
full-screen app owning the terminal it shreds the display. Every tool decided
this for itself and each got it wrong differently: the coder read an env var
that ALSO silenced the app's narration channel, the librarian never asked, and
the researcher tested `isatty()` - which is True under a full-screen app, so
its guard passed and the screen was overwritten anyway.

`UI.live()` is the one answer, and this file keeps it the only one. The grep
guard is deliberate (Rule 2 prefers an executable guard over a prose rule):
`research_agent` imports `Live` function-locally, so an import-based check
would not have caught it.
"""
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
SCANNED = ("vaf/tools", "vaf/core", "vaf/workflows")

# `vaf/cli/tui.py` is the factory's own home - it is the one place allowed to
# build a real Live. Everything else asks it.
ALLOWED = {"vaf/cli/tui.py"}

_DIRECT_LIVE = re.compile(r"(?<![\w.])Live\s*\(")


def _sources():
    for area in SCANNED:
        for path in sorted((REPO / area).rglob("*.py")):
            rel = path.relative_to(REPO).as_posix()
            if rel in ALLOWED:
                continue
            yield rel, path.read_text(encoding="utf-8", errors="replace")


def test_no_tool_constructs_a_rich_live_directly():
    """A tool that builds its own Live has decided, on its own, that the screen
    is free. It is not free while a full-screen app is up."""
    offenders = []
    for rel, text in _sources():
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if _DIRECT_LIVE.search(line) and "UI.live(" not in line:
                offenders.append(f"{rel}:{lineno}: {stripped[:90]}")
    assert not offenders, (
        "these build a Rich Live directly instead of asking UI.live():\n  "
        + "\n  ".join(offenders))


def test_the_factory_hands_back_a_silent_stand_in_while_an_app_owns_the_screen():
    from vaf.cli.tui import UI, _NoopLive

    before = UI._app_mode
    try:
        UI.set_app_mode(True)
        live = UI.live("anything", refresh_per_second=12)
        assert isinstance(live, _NoopLive)
        # Unguarded call sites must keep working against it.
        live.start()
        live.update("x")
        live.refresh()
        live.stop()
        with live:
            pass
    finally:
        UI._app_mode = before


def test_the_factory_hands_back_a_real_live_when_the_screen_is_free():
    from rich.live import Live

    from vaf.cli.tui import UI

    before = UI._app_mode
    try:
        UI.set_app_mode(False)
        live = UI.live("anything", refresh_per_second=12)
        assert isinstance(live, Live)
    finally:
        UI._app_mode = before


def test_app_mode_reader_matches_the_writer():
    from vaf.cli.tui import UI

    before = UI._app_mode
    try:
        UI.set_app_mode(True)
        assert UI.app_mode_active() is True
        UI.set_app_mode(False)
        assert UI.app_mode_active() is False
    finally:
        UI._app_mode = before


def test_the_workflow_env_var_is_not_reused_as_the_app_switch():
    """VAF_IN_WORKFLOW_TERMINAL carries four unrelated meanings - among them an
    EARLY, sink-free return in UI.event that would cut the app's own narration.
    Using it as the screen-ownership switch would silence the very channel the
    app listens on."""
    import inspect

    from vaf.cli import tui as tui_mod

    source = inspect.getsource(tui_mod.UI.live)
    assert "VAF_IN_WORKFLOW_TERMINAL" not in source
    source = inspect.getsource(tui_mod.UI.app_mode_active)
    assert "environ" not in source


@pytest.mark.parametrize("mode", [True, False])
def test_coder_narration_goes_to_the_transcript_in_app_mode(mode, capsys):
    """In app mode the coder's simple-mode lines must reach the console sink
    (which the app subscribes to), not stdout under the alternate screen."""
    from vaf.cli.tui import UI
    from vaf.tools.coder import CoderTUI

    seen = []
    before_mode, before_sinks = UI._app_mode, list(UI._console_sinks)
    UI._console_sinks.clear()
    try:
        UI.add_console_sink(lambda t, m, s: seen.append((t, m)))
        UI.set_app_mode(mode)
        tui = CoderTUI.__new__(CoderTUI)
        tui._say("wrote three files")
        printed = capsys.readouterr().out
        if mode:
            assert seen == [("Coder", "wrote three files")]
            assert "wrote three files" not in printed
        else:
            assert "[Coder] wrote three files" in printed
    finally:
        UI._app_mode = before_mode
        UI._console_sinks.clear()
        UI._console_sinks.extend(before_sinks)


# ── the two decisions that produce TOTAL SILENCE when they get it wrong ─────────────

@pytest.mark.parametrize("app_mode,in_workflow,expected", [
    (False, False, False),   # a normal terminal: full panel
    (False, True,  True),    # workflow terminal: lines, so the workflow keeps its output
    (True,  False, True),    # a full-screen app owns the screen: lines, into the transcript
    (True,  True,  True),
])
def test_coder_goes_quiet_whenever_the_screen_is_not_its_own(monkeypatch, app_mode,
                                                             in_workflow, expected):
    """Without this, `simple_mode` stays False under a full-screen app: the
    narration is buffered into a panel that renders into a no-op Live, so the
    user sees NO sign the coder is working."""
    from vaf.cli.tui import UI
    from vaf.tools.coder import CodingAgentTool

    monkeypatch.setenv("VAF_IN_WORKFLOW_TERMINAL", "1" if in_workflow else "")
    before = UI._app_mode
    try:
        UI.set_app_mode(app_mode)
        assert CodingAgentTool.quiet_output_mode() is expected
    finally:
        UI._app_mode = before


def test_research_panel_is_refused_while_an_app_owns_the_screen():
    """Same shape, worse failure: when the panel is considered appropriate,
    `_emit_progress` returns early because the panel is meant to BE the
    progress. Under a full-screen app the panel renders into nothing, so a
    wrong answer here means the research agent reports nothing at all."""
    from vaf.cli.tui import UI
    from vaf.tools.research_agent import live_panel_is_appropriate

    kwargs = dict(live_available=True, noninteractive=False,
                  is_tty=True, is_fragment_mode=False,
                  console=SimpleNamespace(is_terminal=True, is_jupyter=False))
    before = UI._app_mode
    try:
        UI.set_app_mode(False)
        assert live_panel_is_appropriate(**kwargs) is True, (
            "premise broken: in a plain terminal the panel IS appropriate")
        UI.set_app_mode(True)
        assert live_panel_is_appropriate(**kwargs) is False
    finally:
        UI._app_mode = before


@pytest.mark.parametrize("flag", ["live_available", "is_tty"])
def test_research_panel_still_honours_its_original_guards(flag):
    """The app-mode clause is added, not substituted: the terminal probes that
    kept panels out of dumb terminals must keep working."""
    from vaf.cli.tui import UI
    from vaf.tools.research_agent import live_panel_is_appropriate

    kwargs = dict(live_available=True, noninteractive=False,
                  is_tty=True, is_fragment_mode=False,
                  console=SimpleNamespace(is_terminal=True, is_jupyter=False))
    kwargs[flag] = False
    before = UI._app_mode
    try:
        UI.set_app_mode(False)
        assert live_panel_is_appropriate(**kwargs) is False
    finally:
        UI._app_mode = before


def _code_lines(func):
    """Source of a function with comment-only lines removed.

    Scanning source is a last resort - it is used here because both decisions
    sit inside very large `run()` methods that cannot be invoked in a test.
    Comments are stripped deliberately: an earlier guard in this repo matched
    its own explanatory comment and passed while the code was wrong.
    """
    import inspect
    return "\n".join(line for line in inspect.getsource(func).splitlines()
                     if not line.strip().startswith("#"))


def test_the_coder_run_actually_asks_for_quiet_mode():
    """The helper being right is worth nothing if `run()` hardcodes the flag -
    this is the wiring half, and it is the half that broke before."""
    from vaf.tools.coder import CodingAgentTool

    body = _code_lines(CodingAgentTool.run)
    assert "quiet_output_mode()" in body, (
        "run() no longer consults the quiet-mode decision")
    assert "simple_mode=False" not in body.replace(" ", "")


def test_the_research_run_actually_asks_whether_a_panel_is_appropriate():
    from vaf.tools.research_agent import ResearchAgentTool

    body = _code_lines(ResearchAgentTool.run)
    assert "live_panel_is_appropriate(" in body, (
        "run() no longer consults the panel decision")
