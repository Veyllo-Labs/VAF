# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The server-side stream endpointer: timer verdicts, the semantic veto, and mute.

`StreamEndpointer` (vaf/core/voice_vad.py) is the framework piece behind the opt-in
`voice_semantic_endpoint_enabled` lane: the browser streams mic PCM, Silero yields
per-frame voicedness, `TurnDetector` finds the boundary, and - when armed - Smart Turn
v3 may VETO a timer end because the speaker only paused mid-sentence. Everything here
runs without any model: the VAD is stubbed with a scripted voiced sequence and the
judge with a canned verdict, because what these tests pin is the WIRING - the exact
thing that was missing for months while the primitive sat tested and uncalled.

Time inside the endpointer comes from the SAMPLE COUNT, never the wall clock, which is
what makes these tests deterministic (and a delayed websocket unable to fake silence).
"""
from typing import List, Optional

import pytest

from vaf.core.voice_vad import StreamEndpointer, TurnDetector, _SILENCE_MS


class _ScriptedVad:
    """Silero stand-in: voiced according to a script, one entry per 512-sample window."""

    def __init__(self, script: List[bool]):
        self._script = script
        self._i = 0

    def accept_waveform(self, samples):
        self._pos = min(self._i, len(self._script) - 1)
        self._i += 1

    def is_speech_detected(self):
        return self._script[min(self._pos, len(self._script) - 1)]

    def empty(self):
        return True

    def pop(self):  # pragma: no cover - drain loop never runs with empty()==True
        pass


class _Judge:
    def __init__(self, verdict: bool):
        self.verdict = verdict
        self.calls = 0

    def complete(self, pcm, sample_rate=16000):
        self.calls += 1
        return self.verdict


def _pcm(n_windows: int) -> bytes:
    return b"\x00\x00" * (512 * n_windows)


def _endpointer(script: List[bool], judge: Optional[_Judge]) -> StreamEndpointer:
    ep = StreamEndpointer(judge=judge)
    ep._vad = _ScriptedVad(script)
    return ep


def _windows_for_silence() -> int:
    # 512 samples at 16 kHz = 32 ms per window; enough windows to pass _SILENCE_MS.
    return int(_SILENCE_MS / 32.0) + 3


def test_speech_then_silence_ends_the_turn():
    n = _windows_for_silence()
    script = [True] * 20 + [False] * n
    ep = _endpointer(script, judge=None)
    assert ep.feed_pcm(_pcm(20 + n)) == "end"


def test_the_judge_can_hold_an_unfinished_sentence_open():
    """T5: the timer says end, the semantic model says the speaker only paused - the
    verdict must be 'hold', and the mutation that ignores the model turns this red."""
    n = _windows_for_silence()
    script = [True] * 20 + [False] * n
    judge = _Judge(verdict=False)   # "incomplete - they are mid-sentence"
    ep = _endpointer(script, judge=judge)
    assert ep.feed_pcm(_pcm(20 + n)) == "hold"
    assert judge.calls == 1, "the judge was never consulted - the wiring is gone"


def test_a_completing_judge_lets_the_end_stand():
    n = _windows_for_silence()
    script = [True] * 20 + [False] * n
    ep = _endpointer(script, judge=_Judge(verdict=True))
    assert ep.feed_pcm(_pcm(20 + n)) == "end"


def test_silence_only_never_ends_anything():
    """T6's server half: a muted mic delivers silence; with no speech there is no turn
    to end, so the stream must yield NO verdict at all - a server that answers
    voice_turn_end to a muted user has broken the mute contract."""
    n = _windows_for_silence() * 2
    ep = _endpointer([False] * n, judge=None)
    assert ep.feed_pcm(_pcm(n)) is None


def test_reset_forgets_the_stream_state():
    ep = _endpointer([True] * 10, judge=None)
    ep.feed_pcm(_pcm(10))
    assert ep.detector.speaking
    ep.reset()
    assert not ep.detector.speaking
    assert len(ep._ring) == 0 and ep._samples_fed == 0


def test_the_judge_ring_is_bounded():
    """User isolation and memory: the ring may never grow past the judge window - a
    stuck client streaming forever must not grow server memory without bound."""
    ep = _endpointer([True] * 4000, judge=None)
    ep.feed_pcm(_pcm(600))
    assert len(ep._ring) <= ep._ring_max


def test_the_judge_singleton_is_config_gated(monkeypatch):
    """Default OFF: without the config key the lane must not exist, whatever is
    installed. This is the 'smart turn ships dark' decision from the plan."""
    import vaf.core.voice_vad as vv
    from vaf.core.config import Config

    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: False if key == "voice_semantic_endpoint_enabled" else default))
    monkeypatch.setattr(vv, "_judge", None)
    assert vv.get_turn_judge() is None


def test_voice_vad_has_a_production_caller():
    """T7: VOICE_REFLEX.md's foundation claim is machine-checkable now - the primitive
    must be imported from at least one production module, or the doc lies again."""
    import subprocess

    out = subprocess.run(
        ["git", "grep", "-l", "voice_vad", "--", "vaf/"],
        capture_output=True, text=True, cwd=str(__import__("pathlib").Path(__file__).parent.parent),
    ).stdout.split()
    callers = [f for f in out if not f.endswith("voice_vad.py")]
    assert callers, (
        "voice_vad has no production caller again - the tested-but-never-wired era is back"
    )
