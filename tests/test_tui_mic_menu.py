# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The microphone submenu: real devices, right indices, one enumeration.

Three contracts, each against its failure mode:

1. THE INDEX IS PARSED, NOT COUNTED. `list_microphones()` pre-formats its
   PyAudio entries as "index: name" and FILTERS the list ("mapper" devices are
   dropped), so an entry's POSITION lies about its device index - the classic
   inquirer picker enumerates positions and can select the wrong microphone
   (noted there, not fixed here). The fallback path (sr.Microphone names)
   returns bare names where position IS the index, so the parser must accept
   both: prefix wins, position otherwise.

2. ONE ENUMERATION PER ENTRY. `_menu_rows` runs on every rebuild and label
   refresh; each enumeration constructs a PyAudio instance - a real
   audio-stack touch that also writes ALSA warnings on C-level fd 2, which
   shreds the alternate screen. Rows render from a cache filled when the
   submenu is ENTERED and dropped when it is left.

3. THE AUDIO STACK STAYS OFF THE MENU'S IMPORT GRAPH. Opening Settings, or
   the voice submenu, must not import vaf.core.speech - only entering the
   microphone submenu may.
"""
import subprocess
import sys
from types import SimpleNamespace

from vaf.cli.tui_app.screens import SettingsScreen


def _screen(values=None, notified=None, pushed=None):
    fake_app = SimpleNamespace(
        notify=lambda msg, **kw: (notified if notified is not None else []).append(msg),
        post_message=lambda msg: None,
        push_screen=lambda *a, **k: (pushed if pushed is not None else []).append(a),
    )

    class _Detached(SettingsScreen):
        app = property(lambda s: fake_app)

    s = _Detached.__new__(_Detached)
    s._cfg = lambda key, default=None: (values or {}).get(key, default)
    s._refresh_labels = lambda: None
    s._stack = ["main", "voice"]
    s._rows = []
    s._mic_devices = None
    return s


def _fake_speech(monkeypatch, names, calls=None, set_calls=None):
    import vaf.cli.tui_app.screens as screens_mod

    manager = SimpleNamespace(
        list_microphones=lambda: ((calls.append(1) if calls is not None else None)
                                  or list(names)),
        set_microphone=lambda idx: (set_calls if set_calls is not None else []).append(idx),
    )
    fake_module = SimpleNamespace(get_speech_manager=lambda: manager)
    monkeypatch.setitem(sys.modules, "vaf.core.speech", fake_module)
    return manager


# ── contract 1: the index ───────────────────────────────────────────────────────────

def test_the_index_comes_from_the_prefix_not_the_position(monkeypatch):
    """A filtered list: device 0 was dropped, so position 0 holds device 2.
    Counting positions would select the wrong microphone."""
    s = _screen()
    _fake_speech(monkeypatch, ["2: USB Mic", "5: Headset"])
    s._load_mics()
    assert s._mic_devices == [(2, "2: USB Mic"), (5, "5: Headset")]


def test_bare_names_fall_back_to_the_position(monkeypatch):
    """The sr.Microphone fallback returns names with no prefix; there the
    position IS the device index, and requiring a prefix would crash it."""
    s = _screen()
    _fake_speech(monkeypatch, ["Built-in Microphone", "USB Audio"])
    s._load_mics()
    assert s._mic_devices == [(0, "Built-in Microphone"), (1, "USB Audio")]


def test_picking_a_row_passes_the_parsed_index(monkeypatch):
    set_calls = []
    s = _screen(values={"speech_mic_index": None})
    _fake_speech(monkeypatch, ["3: USB Mic"], set_calls=set_calls)
    s._load_mics()
    s._rows = s._menu_rows("mic")
    idx = next(i for i, r in enumerate(s._rows) if r[0] == "mic")
    s._activate(idx)
    assert set_calls == [3], f"set_microphone got {set_calls}, not the parsed 3"


def test_the_marker_sits_on_the_stored_device(monkeypatch):
    s = _screen(values={"speech_mic_index": 5})
    _fake_speech(monkeypatch, ["2: USB Mic", "5: Headset"])
    s._load_mics()
    rows = s._menu_rows("mic")
    marked = [r[2] for r in rows if "▍" in str(r[2])]
    assert len(marked) == 1 and "Headset" in marked[0], marked


# ── contract 2: one enumeration ─────────────────────────────────────────────────────

def test_rebuilds_render_from_the_cache_not_the_audio_stack(monkeypatch):
    calls = []
    s = _screen()
    _fake_speech(monkeypatch, ["0: Mic"], calls=calls)
    s._load_mics()
    for _ in range(5):
        s._menu_rows("mic")
    assert len(calls) == 1, (
        f"{len(calls)} enumerations for one submenu entry - every rebuild "
        f"touches the audio stack again")


def test_leaving_the_submenu_drops_the_cache(monkeypatch):
    s = _screen()
    _fake_speech(monkeypatch, ["0: Mic"])
    s._load_mics()
    s._stack = ["main", "voice", "mic"]
    s._rebuild = lambda: None
    s.action_go_back()
    assert s._mic_devices is None, "stale devices survive re-entry"


def test_no_devices_yields_an_honest_note_not_a_crash(monkeypatch):
    s = _screen()
    _fake_speech(monkeypatch, [])
    s._load_mics()
    rows = s._menu_rows("mic")
    assert rows[0][0] == "note" and "no microphones" in rows[0][2]


def test_a_broken_audio_stack_yields_the_reason(monkeypatch):
    import vaf.cli.tui_app.screens as screens_mod

    def _boom():
        raise RuntimeError("pyaudio missing")

    monkeypatch.setitem(sys.modules, "vaf.core.speech",
                        SimpleNamespace(get_speech_manager=_boom))
    s = _screen()
    s._load_mics()
    rows = s._menu_rows("mic")
    assert rows[0][0] == "note" and "pyaudio missing" in rows[0][2]


# ── contract 3: the import graph ────────────────────────────────────────────────────

def test_building_the_menus_never_imports_the_audio_stack():
    """Subprocess probe: constructing every menu except the microphone one
    must not pull vaf.core.speech (whose first construction can OPEN the
    microphone). Only _load_mics may."""
    code = (
        "import sys\n"
        "from vaf.cli.tui_app.screens import SettingsScreen\n"
        "s = SettingsScreen.__new__(SettingsScreen)\n"
        "s._cfg = lambda k, d=None: {'provider': 'local'}.get(k, d)\n"
        "s._mic_devices = None\n"
        "for menu in ('main', 'voice', 'theme', 'stt_lang', 'mic'):\n"
        "    s._menu_rows(menu)\n"
        "assert 'vaf.core.speech' not in sys.modules, 'menu build imported the audio stack'\n"
        "print('clean')\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, cwd="/mnt/veyllo1/VAF")
    assert out.returncode == 0 and "clean" in out.stdout, out.stderr[-800:]
