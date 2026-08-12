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
constants (silence 1100 ms, min-speech 350 ms, max-utterance 20 s) and are configurable;
a CI guard (tests/test_voice_endpoint_constants.py) keeps the frontend and this file
from drifting apart - three hand-rolled copies of the same decision is how the 1500 ms
era happened.
"""
from __future__ import annotations

from typing import Optional

_SILENCE_MS = 1100.0    # trailing silence that ends an utterance (frontend SILENCE_MS)
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


# ── Semantic turn-end (Smart Turn v3, optional) ──────────────────────────────
# An energy timer cannot tell a finished sentence from a mid-sentence thinking
# pause - both are silence. Smart Turn v3 (pipecat-ai, BSD-2) reads the raw
# waveform's prosody instead: Whisper-Tiny encoder + linear head, ~8M params,
# 8 MB int8 ONNX, CPU-only - the same runtime class as the reflex policy, so it
# never touches the one llama server (CLAUDE.md Rule 4.6). It runs ON the
# silence the TurnDetector already found and answers one question: did the
# speaker actually finish? DEFAULT OFF (voice_semantic_endpoint_enabled): it
# only earns its ~30-60 ms once the per-turn timings show the endpoint is the
# remaining cost, and turning it on is a config decision, not a code change.

_SMART_TURN_MODEL = "smart-turn-v3.2-cpu.onnx"
_SMART_TURN_URL = ("https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/main/"
                   + _SMART_TURN_MODEL)
_SMART_TURN_WINDOW_S = 8       # the model judges the LAST 8 s, per its contract
_SMART_TURN_THRESHOLD = 0.5    # sigmoid(complete) above this = the turn is over

_judge_lock = None  # created lazily; module must import without threading cost


class SemanticTurnJudge:
    """Fail-open wrapper around the Smart Turn v3 ONNX model.

    ``complete(pcm16, sample_rate)`` returns True when the model says the speaker
    finished (or on ANY failure - a broken judge must degrade to the plain timer
    verdict, never hold a turn hostage). Model file arrives via the same lazy
    download/atomic-replace path speaker_id uses; features come from the Whisper
    feature extractor that the installed transformers package already ships.
    """

    def __init__(self) -> None:
        self._session = None
        self._extractor = None
        self._failed = False

    def _ensure(self) -> bool:
        if self._failed:
            return False
        if self._session is not None:
            return True
        try:
            from pathlib import Path

            from vaf.core import speaker_id as _sid

            path = _sid._models_dir() / _SMART_TURN_MODEL
            if not (path.exists() and path.stat().st_size > 1_000_000):
                path.parent.mkdir(parents=True, exist_ok=True)
                import requests

                tmp = Path(str(path) + ".part")
                with requests.get(_SMART_TURN_URL, stream=True, timeout=120) as r:
                    r.raise_for_status()
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_content(chunk_size=65536):
                            f.write(chunk)
                tmp.replace(path)

            import onnxruntime as ort

            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 1
            opts.inter_op_num_threads = 1
            self._session = ort.InferenceSession(
                str(path), opts, providers=["CPUExecutionProvider"])

            from transformers import WhisperFeatureExtractor

            # Default 80 mel bins - the model's real contract (verified against the
            # session: input_features ['s6', 80, 800]). The upstream repo docs said
            # 128, and with 128 every run raised INVALID_ARGUMENT - which the
            # fail-open then swallowed into a permanent silent "complete". A smoke
            # inference below makes that class of break LOUD at load time instead.
            self._extractor = WhisperFeatureExtractor()

            import numpy as np
            _probe = self._extractor(
                np.zeros(16000, dtype=np.float32), sampling_rate=16000,
                return_tensors="np", padding="max_length",
                max_length=_SMART_TURN_WINDOW_S * 16000, truncation=True,
            )["input_features"].astype(np.float32)
            self._session.run(None, {"input_features": _probe})
            return True
        except Exception:
            self._failed = True   # do not retry per turn; a restart retries
            return False

    def complete(self, pcm16: bytes, sample_rate: int = 16000) -> bool:
        """True = the speaker finished (also on any error: fail-open to the timer)."""
        try:
            if not self._ensure():
                return True
            import numpy as np

            audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
            window = _SMART_TURN_WINDOW_S * sample_rate
            audio = audio[-window:]                       # keep the END, per contract
            feats = self._extractor(
                audio, sampling_rate=sample_rate, return_tensors="np",
                padding="max_length", max_length=window, truncation=True,
            )["input_features"].astype(np.float32)
            out = self._session.run(None, {"input_features": feats})
            prob = float(np.asarray(out[0]).reshape(-1)[0])
            return prob > _SMART_TURN_THRESHOLD
        except Exception:
            return True


class StreamEndpointer:
    """PCM-in, turn-verdict-out: Silero voicedness -> TurnDetector -> optional judge.

    One instance PER CALL (the Silero VAD holds per-stream state and the ring buffer
    holds the caller's audio - never share across users, Rule 4.4). Feed 16 kHz mono
    int16 PCM with ``feed_pcm``; it returns ``"end"`` when the utterance is over,
    ``"hold"`` when the timer said stop but the semantic judge heard an unfinished
    sentence (keep listening), else None. Time is derived from the SAMPLE COUNT, not
    the wall clock, so a delayed websocket delivery cannot fake a silence gap.
    """

    def __init__(self, sample_rate: int = 16000,
                 judge: Optional[SemanticTurnJudge] = None) -> None:
        self.sample_rate = int(sample_rate)
        self.judge = judge
        self.detector = TurnDetector()
        self._vad = None          # sherpa Silero, created lazily and per instance
        self._samples_fed = 0
        self._ring = bytearray()  # last WINDOW seconds of pcm for the judge
        self._ring_max = _SMART_TURN_WINDOW_S * self.sample_rate * 2  # int16 bytes

    def _ensure_vad(self):
        if self._vad is None:
            from vaf.core import speaker_id as _sid
            self._vad = _sid._new_vad()   # fresh per call: holds per-stream state
        return self._vad

    def reset(self) -> None:
        """Mute toggled / utterance discarded on the client: drop ALL stream state."""
        self.detector.reset()
        self._ring.clear()
        self._samples_fed = 0
        self._vad = None

    def feed_pcm(self, pcm16: bytes) -> Optional[str]:
        try:
            import numpy as np

            vad = self._ensure_vad()
            if vad is None:
                return None
            self._ring.extend(pcm16)
            if len(self._ring) > self._ring_max:
                del self._ring[: len(self._ring) - self._ring_max]

            samples = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
            window = 512   # Silero's native frame at 16 kHz
            verdict: Optional[str] = None
            for i in range(0, len(samples), window):
                vad.accept_waveform(samples[i:i + window])
                self._samples_fed += min(window, len(samples) - i)
                ts_ms = self._samples_fed / self.sample_rate * 1000.0
                ev = self.detector.feed(bool(vad.is_speech_detected()), ts_ms)
                while not vad.empty():   # drain segments; we only use the live flag
                    vad.pop()
                if ev == "end":
                    if self.judge is not None and not self.judge.complete(
                            bytes(self._ring), self.sample_rate):
                        verdict = "hold"   # mid-sentence pause: keep listening
                    else:
                        verdict = "end"
            return verdict
        except Exception:
            return None   # observation only - the browser timer remains the fallback


_judge: Optional[SemanticTurnJudge] = None


def get_turn_judge() -> Optional[SemanticTurnJudge]:
    """The process-wide judge, or None while the feature is off.

    Gated on ``voice_semantic_endpoint_enabled`` (default False). The session inside
    the judge is a singleton on purpose - the model file is 8 MB and the session is
    reusable across calls; per-call state lives in the CALLER's TurnDetector, never
    here, so user isolation (Rule 4.4) is untouched.
    """
    global _judge, _judge_lock
    try:
        from vaf.core.config import Config
        if not Config.get("voice_semantic_endpoint_enabled", False):
            return None
    except Exception:
        return None
    if _judge is None:
        import threading
        if _judge_lock is None:
            _judge_lock = threading.Lock()
        with _judge_lock:
            if _judge is None:
                _judge = SemanticTurnJudge()
    return _judge
