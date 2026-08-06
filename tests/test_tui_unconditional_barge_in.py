# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Any submitted input silences running speech - the classic loop's contract.

TTS is asynchronous and routinely outlives the turn that produced it. The
classic loop therefore stops speech on EVERY input, before parsing (`run.py`,
"CRITICAL: Stop TTS immediately upon any user input"); the app lane only did
it at turn start, so a slash command - or any message submitted while the
lane was busy - left the agent talking. The stop is quiet on purpose: only
the explicit `halt` command narrates.

Also pinned here: the settings tree no longer advertises a wake word. The
openWakeWord listener was removed from VAF months before this app existed,
so both the "Wake Word" row and the submenu label pointed at nothing - in
the classic menu too.
"""
import time
from types import SimpleNamespace

from vaf.cli.tui_app.agent_bridge import AgentBridge


def _wait(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _bridge(monkeypatch):
    import sys as _sys

    stops = []
    monkeypatch.setitem(
        _sys.modules, "vaf.core.speech",
        SimpleNamespace(get_speech_manager=lambda: SimpleNamespace(
            stop=lambda: stops.append(True))))
    events = []

    class _Events:
        def __getattr__(self, name):
            def _rec(*args):
                events.append((name, *args))
            return _rec

    b = AgentBridge(
        SimpleNamespace(get_token_usage=lambda: (1, 2),
                        set_event_sink=lambda s: None, shutdown=lambda: None),
        SimpleNamespace(id="green123456"), None, _Events(),
        web_interface_getter=lambda: SimpleNamespace(resolve_gate=lambda *a: True))
    return b, events, stops


def test_the_quiet_stop_stops_but_says_nothing(monkeypatch):
    b, events, stops = _bridge(monkeypatch)
    b.stop_speech(announce=False)
    assert _wait(lambda: bool(stops))
    time.sleep(0.1)
    assert not any(e[0] == "system_note" for e in events), (
        "the unconditional barge-in narrated - every submit would spam a note")
    b.shutdown()


def test_the_halt_command_still_narrates(monkeypatch):
    b, events, stops = _bridge(monkeypatch)
    b.stop_speech()
    assert _wait(lambda: any(e[0] == "system_note" and "speech stopped" in e[1]
                             for e in events)), events
    assert stops
    b.shutdown()


def _submitting_app(text):
    import vaf.cli.tui_app.app as app_mod

    calls = []

    class _A(app_mod.VafApp):
        pass

    a = _A.__new__(_A)
    a._bridge = SimpleNamespace(
        stop_speech=lambda announce=True: calls.append(("stop", announce)),
        busy=False)
    a.run_command = lambda parsed: calls.append(("command", parsed.command))
    a._send_user = lambda t: calls.append(("send", t))
    a.add_event_note = lambda *args: calls.append(("note", args))
    a._submitted(SimpleNamespace(text=text))
    return calls


def test_every_submit_stops_speech_before_parsing():
    for text in ("hallo welt", "/help", "/tpyo"):
        calls = _submitting_app(text)
        assert calls and calls[0] == ("stop", False), (
            f"submitting {text!r} did not silence speech first: {calls}")


def test_the_settings_tree_stopped_advertising_a_wake_word():
    """The wake row pointed at `vaf settings`, which has no wake word either:
    the listener was deleted from the codebase long ago. A settings row for a
    feature that exists nowhere is not a pointer, it is a lie."""
    from vaf.cli.tui_app.screens import SettingsScreen

    s = SettingsScreen.__new__(SettingsScreen)
    s._cfg = lambda k, d=None: d
    s._mic_devices = None
    voice_rows = s._menu_rows("voice")
    assert not any(a == "wake" for _, a, _ in voice_rows), voice_rows
    main_rows = s._menu_rows("main")
    labels = [label for _, _, label in main_rows]
    assert not any("Wake Word" in lbl for lbl in labels), labels
