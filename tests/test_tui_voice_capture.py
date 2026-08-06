# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`l` records: the bridge's listen thread, the overlay's real meter, the turn.

The app ADVERTISED voice from day one - mic chip in the top bar, `L Voice` in
the key hints, a finished VoiceScreen - while `action_voice` was a toast
saying "next round". This is that round. The contracts pinned here:

- The capture runs on its OWN daemon thread: the classic promise is
  "listening works any time", and the blocking loop parked on the serialized
  agent lane would wait behind a running turn, parked on the UI thread it
  would freeze the app. The captured TEXT is then submitted to the lane like
  any typed message.
- One capture at a time; a second `l` is told so instead of opening a second
  microphone stream.
- Escape cancels the CAPTURE (cooperatively, through the framework's
  `should_stop`), not just the view - and a cancelled capture sends NOTHING.
- Disabled STT is an honest note, not a dead key.
"""
import threading
import time
from types import SimpleNamespace

import pytest

from vaf.cli.tui_app.agent_bridge import AgentBridge


def _wait(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _bridge(monkeypatch, *, enabled=True, text="hallo welt", block=None):
    import sys as _sys

    events = []

    class _Events:
        KNOWN = {"voice_level", "voice_done", "event_note", "system_note",
                 "presence", "context"}

        def __getattr__(self, name):
            if name not in self.KNOWN:
                raise AttributeError(name)

            def _rec(*args):
                events.append((name, *args))
            return _rec

    def listen(timeout=10, on_state=None, should_stop=None, **kw):
        if on_state:
            on_state("calibrating", 0, 0)
            on_state("speaking", 500, 300)
        if block is not None:
            while not (should_stop and should_stop()):
                if block.wait(0.02):
                    break
            if should_stop and should_stop():
                return None
        return text

    manager = SimpleNamespace(
        is_stt_enabled=lambda: enabled,
        stop=lambda: None,
        listen=listen,
    )
    monkeypatch.setitem(_sys.modules, "vaf.core.speech",
                        SimpleNamespace(get_speech_manager=lambda: manager))

    b = AgentBridge(
        SimpleNamespace(get_token_usage=lambda: (1, 2),
                        set_event_sink=lambda s: None, shutdown=lambda: None),
        SimpleNamespace(id="green123456"), None, _Events(),
        web_interface_getter=lambda: SimpleNamespace(resolve_gate=lambda *a: True))
    b.turns = []
    b._run_turn = lambda t, **kw: b.turns.append(t)
    return b, events


def test_the_bridge_reports_and_never_submits(monkeypatch):
    """The transcript goes back to the APP, which routes it through the same
    send path a typed message takes - a lane-side submit from here streamed
    an answer into a transcript with NO visible question, and it left the
    review-before-send preference no place to act."""
    b, events = _bridge(monkeypatch)
    b.listen_voice()
    assert _wait(lambda: any(e[0] == "voice_done" for e in events))
    phases = [e[1] for e in events if e[0] == "voice_level"]
    assert "calibrating" in phases and "speaking" in phases
    done = next(e for e in events if e[0] == "voice_done")
    assert done[1] == "hallo welt"
    time.sleep(0.1)
    assert b.turns == [], "the bridge submitted the turn itself again"
    b.shutdown()


def test_the_capture_never_runs_on_the_ui_thread(monkeypatch):
    b, events = _bridge(monkeypatch)
    threads = []
    orig_listen = None

    import sys as _sys
    mod = _sys.modules["vaf.core.speech"]
    manager = mod.get_speech_manager()
    orig_listen = manager.listen

    def spying(*a, **kw):
        threads.append(threading.current_thread())
        return orig_listen(*a, **kw)

    manager.listen = spying
    b.listen_voice()
    assert _wait(lambda: bool(threads))
    assert threads[0] is not threading.main_thread()
    b.shutdown()


def test_no_speech_is_an_honest_note_not_a_turn(monkeypatch):
    b, events = _bridge(monkeypatch, text=None)
    b.listen_voice()
    assert _wait(lambda: any(e[0] == "voice_done" for e in events))
    done = next(e for e in events if e[0] == "voice_done")
    assert done[1] is None and "no speech" in done[2]
    assert b.turns == []
    b.shutdown()


def test_disabled_stt_says_where_to_enable_it(monkeypatch):
    b, events = _bridge(monkeypatch, enabled=False)
    b.listen_voice()
    assert _wait(lambda: any(e[0] == "voice_done" for e in events))
    done = next(e for e in events if e[0] == "voice_done")
    assert done[1] is None and "Settings" in done[2]
    b.shutdown()


def test_a_second_press_does_not_open_a_second_microphone(monkeypatch):
    block = threading.Event()
    b, events = _bridge(monkeypatch, block=block)
    b.listen_voice()
    assert _wait(lambda: any(e[0] == "voice_level" for e in events))
    b.listen_voice()
    warned = [e for e in events if e[0] == "event_note" and "already" in e[2]]
    assert warned, events
    block.set()
    assert _wait(lambda: any(e[0] == "voice_done" for e in events))
    b.shutdown()


def test_cancel_stops_the_capture_and_sends_nothing(monkeypatch):
    """Escape must not close a view over a live microphone that later sends a
    message nobody watched being taken."""
    block = threading.Event()
    b, events = _bridge(monkeypatch, block=block)
    b.listen_voice()
    assert _wait(lambda: any(e[0] == "voice_level" for e in events))
    b.cancel_listen()
    assert _wait(lambda: any(e[0] == "voice_done" for e in events))
    done = next(e for e in events if e[0] == "voice_done")
    assert done[1] is None and done[2] == "cancelled"
    time.sleep(0.1)
    assert b.turns == [], "a cancelled capture still sent a turn"
    b.shutdown()


def test_a_capture_can_start_again_after_the_last_one(monkeypatch):
    b, events = _bridge(monkeypatch)
    b.listen_voice()
    assert _wait(lambda: any(e[0] == "voice_done" and e[1] for e in events))
    events.clear()
    b.listen_voice()
    assert _wait(lambda: any(e[0] == "voice_done" and e[1] for e in events)), (
        "the guard flag never cleared - a second capture can never start")
    b.shutdown()


# ── the app wiring ──────────────────────────────────────────────────────────────────

def test_the_l_key_opens_the_overlay_and_starts_the_bridge():
    import vaf.cli.tui_app.app as app_mod
    from vaf.cli.tui_app.screens import VoiceScreen

    calls = []
    pushed = []

    class _A(app_mod.VafApp):
        screen = property(lambda s: SimpleNamespace())

        def push_screen(self, scr, cb=None):
            pushed.append((scr, cb))

    a = _A.__new__(_A)
    a._bridge = SimpleNamespace(listen_voice=lambda: calls.append("listen"),
                                cancel_listen=lambda: calls.append("cancel"))
    a.action_voice()
    assert calls == ["listen"]
    assert pushed and isinstance(pushed[0][0], VoiceScreen)
    # Closing the overlay - however it closes - cancels the capture.
    pushed[0][1]("")
    assert calls == ["listen", "cancel"]


def test_the_toast_is_gone():
    from pathlib import Path

    import vaf.cli.tui_app.app as app_mod

    src = Path(app_mod.__file__).read_text(encoding="utf-8")
    assert "lands in the next round" not in src, "voice is a toast again"


# ── the routing decision (the owner's ask) ──────────────────────────────────────────

def _routing_app(review: bool, monkeypatch):
    import vaf.cli.tui_app.app as app_mod
    import vaf.core.config as config_mod

    monkeypatch.setattr(config_mod.Config, "get",
                        classmethod(lambda cls, k, d=None:
                                    review if k == "ux_voice_review" else d))
    record = {"sent": [], "box": SimpleNamespace(text="", focused=False),
              "notes": []}
    record["box"].focus = lambda: record.__setitem__("focused", True)

    class _A(app_mod.VafApp):
        screen = property(lambda s: SimpleNamespace())   # no VoiceScreen up

    a = _A.__new__(_A)
    a._send_user = lambda t: record["sent"].append(t)
    a.query_one = lambda sel, cls=None: record["box"]
    a.add_system_note = lambda t: record["notes"].append(t)
    a.add_event_note = lambda *args: record["notes"].append(args)
    return a, record


def test_by_default_the_transcript_takes_the_typed_message_path(monkeypatch):
    """`_send_user`, not a lane-side submit: the turn gets its "You" bubble
    and its history entry, exactly like a typed message."""
    a, record = _routing_app(review=False, monkeypatch=monkeypatch)
    a.voice_done("hallo welt", "")
    assert record["sent"] == ["hallo welt"]
    assert record["box"].text == "", "review-off still touched the input box"


def test_review_mode_puts_the_transcript_into_the_input_box(monkeypatch):
    """The owner's ask: read what the transcription heard, fix it, enter
    sends - or edit it away, which costs nothing."""
    a, record = _routing_app(review=True, monkeypatch=monkeypatch)
    a.voice_done("hallo welt", "")
    assert record["sent"] == [], "review mode sent anyway"
    assert record["box"].text == "hallo welt"
    assert record["focused"] is True, "the box holds the text but not the cursor"


def test_the_review_toggle_lives_in_the_voice_settings():
    from vaf.cli.tui_app.screens import SettingsScreen

    assert "ux_voice_review" in SettingsScreen.TOGGLES
    s = SettingsScreen.__new__(SettingsScreen)
    s._cfg = lambda k, d=None: d
    s._mic_devices = None
    rows = s._menu_rows("voice")
    assert ("toggle", "ux_voice_review", "") in rows
