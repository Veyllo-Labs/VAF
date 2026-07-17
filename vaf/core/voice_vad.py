# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Streaming turn-boundary detection for the voice reflex system.

The hard-realtime layer (docs/agents/VOICE_REFLEX.md) needs to know, live and without
an LLM, when an utterance STARTS (for barge-in) and ENDS (for turn-taking). Today the
only turn-end signal is a fixed silence timeout on the browser side; this is the
server-side, streaming, reusable state machine.

`TurnDetector` is deliberately decoupled from HOW voicedness is measured: it is fed a
stream of per-frame `voiced` booleans (from the browser energy gate, or from the Silero
VAD already shipped in `speaker_id.py` `_new_vad`, driven in streaming mode) and emits
`start` / `end` / `discard` events. That keeps the acoustic detector swappable and the
boundary logic unit-testable without loading any model. Thresholds mirror the frontend
constants (silence 1500 ms, min-speech 350 ms, max-utterance 20 s) and are configurable.
"""
from __future__ import annotations

from typing import Optional

_SILENCE_MS = 1500.0    # trailing silence that ends an utterance (frontend SILENCE_MS)
_MIN_SPEECH_MS = 350.0  # voiced floor below which an utterance is discarded as a blip
_MAX_UTTER_MS = 20000.0  # hard cap so one turn cannot run forever (frontend MAX_UTTER_MS)
_MAX_FRAME_MS = 200.0   # a single frame's voiced contribution is capped, so an irregular
# or delayed feed (or the onset frame after a long idle gap) cannot over-count voiced time


class TurnDetector:
    """Frame-fed speech start/end state machine (no acoustic model, no LLM).

    Feed `feed(voiced, ts_ms)` once per frame. Returns:
      - "start"   the first voiced frame of an utterance (earliest signal, for barge-in),
      - "end"     an utterance completed with enough voiced time,
      - "discard" an utterance ended but was too short (a blip / noise),
      - None      nothing to report this frame.
    """

    def __init__(self, silence_ms: float = _SILENCE_MS, min_speech_ms: float = _MIN_SPEECH_MS,
                 max_utter_ms: float = _MAX_UTTER_MS) -> None:
        self.silence_ms = float(silence_ms)
        self.min_speech_ms = float(min_speech_ms)
        self.max_utter_ms = float(max_utter_ms)
        self.reset()

    def reset(self) -> None:
        self._speaking = False
        self._start_ts = 0.0
        self._last_voiced_ts = 0.0
        self._voiced_ms = 0.0
        self._prev_ts: Optional[float] = None

    @property
    def speaking(self) -> bool:
        return self._speaking

    def feed(self, voiced: bool, ts_ms: float) -> Optional[str]:
        ts = float(ts_ms)
        # Per-frame voiced contribution, capped so a delayed/irregular feed cannot
        # over-count (and 0 on the very first feed, which has no predecessor).
        dt = 0.0 if self._prev_ts is None else max(0.0, min(ts - self._prev_ts, _MAX_FRAME_MS))
        self._prev_ts = ts

        if not self._speaking:
            if voiced:
                self._speaking = True
                self._start_ts = ts
                self._last_voiced_ts = ts
                self._voiced_ms = dt   # count the onset frame (was 0.0 = one-frame under-count)
                return "start"
            return None

        # speaking
        if voiced:
            self._voiced_ms += dt
            self._last_voiced_ts = ts

        if (ts - self._last_voiced_ts) >= self.silence_ms:
            ended = "end" if self._voiced_ms >= self.min_speech_ms else "discard"
            self.reset()
            return ended
        if (ts - self._start_ts) >= self.max_utter_ms:
            self.reset()
            return "end"
        return None
