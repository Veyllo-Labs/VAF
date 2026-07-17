# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Turn-boundary state machine (vaf/core/voice_vad.py) - no model, pure logic."""
from vaf.core.voice_vad import TurnDetector


def _run(det, frames):
    """frames: list of (voiced, ts_ms); returns the list of emitted events."""
    return [ev for (v, t) in frames if (ev := det.feed(v, t)) is not None]


def test_start_on_first_voiced_frame():
    d = TurnDetector()
    assert d.feed(False, 0) is None
    assert d.feed(True, 100) == "start"
    assert d.speaking is True


def test_full_utterance_ends_after_silence():
    d = TurnDetector(silence_ms=1500, min_speech_ms=350)
    frames = [(True, t) for t in range(0, 600, 100)]        # ~500 ms voiced
    frames += [(False, t) for t in range(600, 2300, 100)]   # >1500 ms silence
    events = _run(d, frames)
    assert events[0] == "start"
    assert events[-1] == "end"
    assert d.speaking is False


def test_short_blip_is_discarded():
    d = TurnDetector(silence_ms=1500, min_speech_ms=350)
    frames = [(True, 0), (True, 100)]                       # ~100 ms voiced only
    frames += [(False, t) for t in range(200, 1900, 100)]   # then silence
    events = _run(d, frames)
    assert events[0] == "start"
    assert events[-1] == "discard"


def test_max_utterance_cap_forces_end():
    d = TurnDetector(silence_ms=1500, min_speech_ms=350, max_utter_ms=20000)
    frames = [(True, t) for t in range(0, 20100, 100)]      # never stops talking
    events = _run(d, frames)
    assert events[0] == "start"
    assert events[-1] == "end"                              # capped, not runaway


def test_reset_returns_to_idle():
    d = TurnDetector()
    d.feed(True, 0)
    d.reset()
    assert d.speaking is False
    assert d.feed(True, 100) == "start"                     # can detect a new turn


def test_onset_frame_counted_avoids_false_discard():
    """The first voiced frame after a silence must count toward min-speech, so a
    genuine ~400 ms utterance is not wrongly discarded as a blip."""
    d = TurnDetector(silence_ms=1500, min_speech_ms=350)
    d.feed(False, 0)                       # a predecessor silence frame
    for t in range(100, 500, 100):         # voiced 100..400
        d.feed(True, t)
    events = [e for t in range(500, 2100, 100) if (e := d.feed(False, t)) is not None]
    assert events[-1] == "end"             # onset counted -> "end", not "discard"
