# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The UI console-sink hook: the narration channel, made subscribable.

WHY THIS EXISTS: the engine narrates through `UI.event` (~150 call sites -
Router, Context, Memory), which only ever PRINTED. The full-screen lane needs
those lines as data, and the only prior art was the gateway monkeypatching four
UI methods - the pattern its own comment calls "a hacky global patch". The hook
makes the ONE funnel (`UI.event`; error/success/warning/info all delegate to
it) subscribable, with two invariants pinned here: with no sink and app mode
off, behavior is identical to before; and app mode suppresses only the console
print - the Web UI bridge still runs, so web parity is untouched.
"""
import pytest

from vaf.cli.tui import UI


@pytest.fixture(autouse=True)
def _sink_isolated():
    """Save/restore the process-global sink state - never leak across tests."""
    sinks_before = list(UI._console_sinks)
    app_mode_before = UI._app_mode
    UI._console_sinks.clear()
    UI._app_mode = False
    yield
    UI._console_sinks.clear()
    UI._console_sinks.extend(sinks_before)
    UI._app_mode = app_mode_before


def test_a_sink_receives_every_event(capsys):
    seen = []
    UI.add_console_sink(lambda t, m, s: seen.append((t, m, s)))

    UI.event("Router", "3 tools selected", style="info")

    assert seen == [("Router", "3 tools selected", "info")]


def test_the_helper_methods_funnel_through_the_sink():
    """error/success/warning/info delegate to UI.event - one funnel, not five.
    A helper printing directly would silently vanish from every app-mode lane."""
    seen = []
    UI.add_console_sink(lambda t, m, s: seen.append((t, s)))

    UI.error("boom")
    UI.success("done")
    UI.warning("careful")
    UI.info("fyi")

    assert seen == [("Error", "error"), ("Success", "success"),
                    ("Warning", "warning"), ("Info", "info")]


def test_app_mode_suppresses_the_console_print(capsys):
    """While a full-screen app owns the terminal, the raw print must not fire -
    it would land inside (or after) the app's alternate screen."""
    UI.add_console_sink(lambda t, m, s: None)
    UI.set_app_mode(True)

    UI.event("Router", "APP_MODE_PROBE", style="info")

    assert "APP_MODE_PROBE" not in capsys.readouterr().out


def test_without_a_sink_the_print_still_happens_even_in_app_mode(capsys):
    """App mode without a subscriber must not swallow narration into nothing."""
    UI.set_app_mode(True)

    UI.event("Router", "NO_SINK_PROBE", style="info")

    assert "NO_SINK_PROBE" in capsys.readouterr().out


def test_default_state_prints_exactly_as_before(capsys):
    """No sink, no app mode - the state of every non-TUI lane, always."""
    UI.event("Router", "DEFAULT_PROBE", style="info")

    assert "DEFAULT_PROBE" in capsys.readouterr().out


def test_the_web_bridge_survives_app_mode(monkeypatch):
    """Suppression is about the CONSOLE only: the Web UI log bridge must keep
    running while the TUI is up, or web parity silently dies for TUI sessions."""
    logged = []

    class _FakeWeb:
        def log(self, message, level="info", source="", session_id=None):
            logged.append((source, message))

    import vaf.core.web_interface as wi
    monkeypatch.setattr(wi, "get_web_interface", lambda: _FakeWeb())

    UI.add_console_sink(lambda t, m, s: None)
    UI.set_app_mode(True)
    UI.event("Router", "WEB_BRIDGE_PROBE", style="info")

    assert ("Router", "WEB_BRIDGE_PROBE") in logged


def test_a_raising_sink_never_breaks_the_event(capsys):
    """The event-sink polarity: a broken observer must not fail a run."""
    def _boom(t, m, s):
        raise RuntimeError("broken sink")

    seen = []
    UI.add_console_sink(_boom)
    UI.add_console_sink(lambda t, m, s: seen.append(t))

    UI.event("Router", "RAISING_SINK_PROBE", style="info")

    assert seen == ["Router"], "the raising sink starved its neighbor"


def test_remove_and_double_add_are_safe():
    def _sink(t, m, s):
        pass

    UI.add_console_sink(_sink)
    UI.add_console_sink(_sink)
    assert UI._console_sinks.count(_sink) == 1
    UI.remove_console_sink(_sink)
    UI.remove_console_sink(_sink)   # second remove: no raise
    assert _sink not in UI._console_sinks
