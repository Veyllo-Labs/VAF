# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Capture separated from presentation: the contract that lets a full-screen
surface host voice input at all.

`SpeechManager.listen()` paints its own level meter with raw cursor codes on
stdout. Fine when the caller owns a plain terminal; fatal under an alternate
screen - which is why the terminal app advertised voice (mic chip, `L Voice`,
a finished overlay) while its action was a toast. The framework half of the
fix: `on_state` delivers every phase change and level tick as DATA, and with
it NOTHING is painted; `should_stop` is a cooperative cancel checked once per
chunk - one the painted path never had.

Also pinned: the painted path itself wrote Rich markup to a RAW fd, so the
classic recording line showed the tags literally ("[bold red]● SPEAKING...").
The tags are gone; a plain status paints instead.

The rig fakes the microphone (scripted PCM chunks) and the clock (0.4s per
read), so silence-detection ends the capture deterministically instead of
this suite sleeping through real 1.5-second pauses.
"""
import struct
import sys
from types import SimpleNamespace

import pytest

import vaf.core.speech as speech_mod
from vaf.core.speech import SpeechManager

QUIET = b"\x00\x00" * 512                     # RMS 0
LOUD = struct.pack("<h", 20000) * 512          # RMS 20000


class _Clock:
    """time.time advancing 0.4s per call - four quiet reads after speech pass
    the 1.5s silence window without a single real sleep."""

    def __init__(self):
        self.now = 1000.0

    def time(self):
        self.now += 0.4
        return self.now


class _Source:
    CHUNK = 512
    SAMPLE_RATE = 16000
    SAMPLE_WIDTH = 2

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.stream = SimpleNamespace(read=self._read)

    def _read(self, n):
        return self._chunks.pop(0) if self._chunks else QUIET

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _manager(monkeypatch, chunks, recognized="hallo welt", engine="google"):
    """`engine` defaults to the legacy google path so the pre-existing chain
    stays pinned; the docker tests below pass "docker". Config is stubbed
    BECAUSE the capture now ensures its own stack via is_stt_enabled - a rig
    reading the machine's real config would flip with the owner's settings
    (it did: these tests broke the moment the docker lane landed)."""
    import vaf.core.config as config_mod

    values = {"speech_stt_enabled": True, "speech_stt_engine": engine}
    monkeypatch.setattr(config_mod.Config, "get",
                        classmethod(lambda cls, k, d=None: values.get(k, d)))
    monkeypatch.setattr(speech_mod, "HAS_STT", True)
    monkeypatch.setattr(speech_mod, "time", _Clock())
    recognizer = SimpleNamespace(
        adjust_for_ambient_noise=lambda source, duration=1.0: None,
        energy_threshold=300,
        recognize_google=lambda audio, language="en-US": recognized,
    )
    fake_sr = SimpleNamespace(
        AudioData=lambda data, rate, width: SimpleNamespace(
            get_wav_data=lambda: b"wav"),
        Recognizer=lambda: recognizer,        # ensure_stt_capture builds one
        UnknownValueError=type("UnknownValueError", (Exception,), {}),
        RequestError=type("RequestError", (Exception,), {}),
    )
    monkeypatch.setattr(speech_mod, "_lazy_load_sr", lambda: fake_sr)
    import vaf.core.speech_api as speech_api_mod
    monkeypatch.setattr(speech_api_mod, "select_stt_backend",
                        lambda: (None, None))

    sm = SpeechManager.__new__(SpeechManager)
    sm.stt_mic = _Source(chunks)
    sm.stt_recognizer = recognizer
    sm.stop = lambda: None
    sm._play_success_sound = lambda: None
    return sm


UTTERANCE = [QUIET, LOUD, LOUD, QUIET, QUIET, QUIET, QUIET, QUIET]


# ── the callback contract ───────────────────────────────────────────────────────────

def test_with_a_callback_nothing_is_painted(monkeypatch, capsys):
    """The headline: data out, zero bytes on stdout - the alternate screen
    stays intact."""
    sm = _manager(monkeypatch, UTTERANCE)
    states = []
    text = sm.listen(lang="en-US",
                     on_state=lambda p, e, t: states.append((p, e, t)))
    assert text == "hallo welt"
    assert capsys.readouterr().out == "", "the capture painted despite a callback"
    phases = [p for p, _e, _t in states]
    assert phases[0] == "calibrating"
    assert "speaking" in phases and "processing" in phases
    speak = next(s for s in states if s[0] == "speaking")
    assert speak[1] > speak[2] > 0, "energy/threshold did not travel as data"


def test_a_broken_meter_does_not_eat_the_utterance(monkeypatch):
    """Observer polarity: the callback may die, the recording may not."""
    sm = _manager(monkeypatch, UTTERANCE)

    def boom(p, e, t):
        raise RuntimeError("meter died")

    assert sm.listen(lang="en-US", on_state=boom) == "hallo welt"


def test_should_stop_abandons_the_capture(monkeypatch):
    """The cancel the painted path never had: checked per chunk, answer None,
    and the recognizer is never consulted with a half recording."""
    sm = _manager(monkeypatch, [QUIET] * 50)
    consulted = []
    sm.stt_recognizer.recognize_google = (
        lambda audio, language="en-US": consulted.append(1) or "x")
    calls = {"n": 0}

    def stop_after_three():
        calls["n"] += 1
        return calls["n"] > 3

    assert sm.listen(lang="en-US", on_state=lambda *a: None,
                     should_stop=stop_after_three) is None
    assert consulted == [], "a cancelled capture was still transcribed"
    # The discriminator against passing FOR THE WRONG REASON: with the check
    # ignored, the capture ends via TIMEOUT (also None) and should_stop is
    # never consulted at all. Honored, it is asked once per chunk and the
    # fourth answer ends the loop.
    assert 3 < calls["n"] <= 6, (
        f"should_stop consulted {calls['n']} times - the capture did not end "
        f"through the cancel")


def test_timeout_reports_its_phase(monkeypatch):
    sm = _manager(monkeypatch, [QUIET] * 200)
    states = []
    text = sm.listen(lang="en-US", timeout=3,
                     on_state=lambda p, e, t: states.append(p))
    assert text is None
    assert states[-1] == "timeout"


# ── the painted path ────────────────────────────────────────────────────────────────

def test_without_a_callback_it_paints_plainly(monkeypatch, capsys):
    """The classic lane keeps its meter - minus the literal markup tags it
    printed to a raw fd since the day it was written."""
    sm = _manager(monkeypatch, UTTERANCE)
    assert sm.listen(lang="en-US") == "hallo welt"
    out = capsys.readouterr().out
    assert "Recording" in out or "SPEAKING" in out, "the painted meter is gone"
    assert "[bold red]" not in out, "raw markup tags are printed literally again"


# ── the docker engine (the live 0.5s incident) ─────────────────────────────────────

def test_the_docker_engine_no_longer_blocks_capture(monkeypatch):
    """THE LIVE INCIDENT: with the DEFAULT engine ("docker") the constructor
    never builds the mic, so `l` answered "no speech" within half a second on
    every machine running the docker STT stack - terminal app and classic
    lane alike. listen() now ensures its own capture stack."""
    import vaf.core.speech_client as client_mod

    sent = []
    monkeypatch.setattr(client_mod, "transcribe",
                        lambda payload, **kw: (sent.append((payload, kw))
                                               or ("hallo welt", "de")))
    sm = _manager(monkeypatch, UTTERANCE, engine="docker")
    mic = sm.stt_mic
    sm.stt_mic = None                       # what the docker-engine ctor leaves
    sm.stt_recognizer = None
    monkeypatch.setattr(sm, "_init_mic",
                        lambda: setattr(sm, "stt_mic", mic), raising=False)
    states = []
    assert sm.listen(lang="en-US",
                     on_state=lambda p, e, t: states.append(p)) == "hallo welt"
    assert "speaking" in states, "the capture never ran"


def test_the_docker_engine_transcribes_through_the_shared_client(monkeypatch):
    """The engine the user CHOSE decides where the audio goes: the shared
    client (cloud lane, else the Whisper container - the Telegram/WhatsApp
    path). Google's free web API is never consulted."""
    import vaf.core.speech_client as client_mod

    sent = []
    monkeypatch.setattr(client_mod, "transcribe",
                        lambda payload, **kw: (sent.append(kw) or ("hallo", None)))
    sm = _manager(monkeypatch, UTTERANCE, engine="docker")
    googled = []
    sm.stt_recognizer.recognize_google = (
        lambda audio, language="en-US": googled.append(1) or "x")
    assert sm.listen(lang="en-US", on_state=lambda *a: None) == "hallo"
    assert sent and sent[0]["mime"] == "audio/wav"
    assert googled == [], "docker-engine audio was rerouted to Google"


def test_a_dead_speech_stack_is_named_not_reworded(monkeypatch, capsys):
    """The container down (or no cloud STT) must not read as "no speech
    detected" - the user spoke; the transcription failed."""
    import vaf.core.speech_client as client_mod

    monkeypatch.setattr(client_mod, "transcribe", lambda payload, **kw: (None, None))
    sm = _manager(monkeypatch, UTTERANCE, engine="docker")
    assert sm.listen(lang="en-US", on_state=lambda *a: None) is None
    out = capsys.readouterr().out
    assert "speech stack" in out or "Whisper" in out, out
