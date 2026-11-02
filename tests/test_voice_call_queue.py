# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The speech queue as the person on the web sees it: dictation, read-aloud and the call.

Driven through websocket_endpoint's real front door with the voice-call baseline's
driver (tests/test_voice_call_baseline.py: real JWT, real receive loop, seams patched at
their source modules). The one seam put BACK to its real self is
speech_client.transcribe, because the queue sits inside it; the STT container behind it
is faked at the HTTP post. Contention is another person holding the STT slot in a thread.
"""
import asyncio
import base64
import threading
import time
from pathlib import Path

import pytest

from vaf.core import fair_queue
from vaf.core import speech_client as sc
from test_voice_call_baseline import (SCOPE, SPEECH_WAV, START, FakeWebSocket,  # noqa: F401
                                      _turn, voice_env)

_REAL_TRANSCRIBE = sc.transcribe
_REPO = Path(__file__).resolve().parents[1]


class _PacedSocket(FakeWebSocket):
    """Like the baseline's socket, but once the script is played it waits (instead of
    hanging up at once) until `until(sent)` holds - a person who stays on the line."""

    def __init__(self, frames, until, timeout=10.0):
        super().__init__(frames)
        self._until = until
        self._deadline = time.monotonic() + timeout

    async def receive_text(self) -> str:
        if self._frames:
            return self._frames.pop(0)
        while not self._until(self.sent) and time.monotonic() < self._deadline:
            await asyncio.sleep(0.01)
        raise self._disconnect(code=1000)


class _Box:
    def __init__(self, text="wie wird das wetter heute in berlin und morgen"):
        self.text = text
        self.urls = []

    def post(self, url, *a, **kw):
        self.urls.append(url)

        class R:
            status_code = 200
            ok = True

            def json(_self):
                return {"text": self.text, "language": "de"}
        return R()


class _Holder:
    """Another person holding the STT container's one slot."""

    def __init__(self, release_when=None):
        self._release = threading.Event()
        self._held = threading.Event()
        self._release_when = release_when
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()
        assert self._held.wait(5)

    def _run(self):
        with sc.speech_queue("stt").slot("another-person"):
            self._held.set()
            end = time.monotonic() + 10
            while not self._release.is_set() and time.monotonic() < end:
                if self._release_when and self._release_when():
                    time.sleep(0.2)
                    break
                time.sleep(0.01)

    def release(self):
        self._release.set()
        self._t.join(5)


@pytest.fixture
def call_env(voice_env, monkeypatch):
    from vaf.core import inference_profile, speech_api
    monkeypatch.setattr(fair_queue, "_registry", {})
    monkeypatch.setattr(sc, "transcribe", _REAL_TRANSCRIBE)
    monkeypatch.setattr(speech_api, "select_stt_backend", lambda *a, **kw: (None, None))
    monkeypatch.setattr(inference_profile, "local_inference_allowed", lambda *a, **kw: True)
    box = _Box()
    monkeypatch.setattr(sc.requests, "post", box.post)
    voice_env["box"] = box
    return voice_env


def _run(env, sock):
    asyncio.run(env["ws_mod"].websocket_endpoint(sock, token=env["token"]))
    return sock.sent


def _types(sent):
    return [p.get("type") for p in sent]


def _stt_waiting():
    m = next((m for m in fair_queue.queue_metrics() if m["name"] == "stt"), None)
    return bool(m and m["waiting"])


# ── dictation ────────────────────────────────────────────────────────────────────

class _DictationSM:
    def is_stt_enabled(self):
        return True

    def synthesize_audio(self, *a, **kw):
        return b"AUDIO"


def _dictation_frame():
    return {"type": "process_audio", "audio": SPEECH_WAV, "format": "wav"}


@pytest.fixture
def dictation_env(call_env, monkeypatch):
    from vaf.core import speech_api
    from vaf.core.speech import SpeechManager
    monkeypatch.setattr(SpeechManager, "get_instance", classmethod(lambda cls: _DictationSM()))
    monkeypatch.setattr(speech_api, "resolve_stt_engine", lambda: "docker")
    return call_env


def test_dictation_waits_and_reports_busy(dictation_env, monkeypatch):
    """A dictation behind another person's STT hears its place, and past the live
    deadline an honest busy with a code the page can show. MUTATION: omit busy_raises
    (the generic "no text" error, no code), or stop sending stt_wait - red."""
    monkeypatch.setattr(sc, "WAIT_LIVE_S", 0.4)
    holder = _Holder()
    try:
        sent = _run(dictation_env, FakeWebSocket([_dictation_frame()]))
    finally:
        holder.release()
    waits = [p for p in sent if p.get("type") == "stt_wait"]
    errors = [p for p in sent if p.get("type") == "stt_error"]
    assert waits and waits[0]["position"] == 1 and "eta_ms" in waits[0]
    assert len(errors) == 1
    assert errors[0]["code"] == "busy" and errors[0]["reason"] == "timeout"
    assert errors[0]["waited_ms"] >= 300
    assert dictation_env["box"].urls == []


def test_page_forwards_the_dictation_queue_fields():
    """Rule 2: page.tsx rebuilds what it reads field by field, so every new field is
    pinned where it is read. MUTATION: drop one forward - red."""
    src = (_REPO / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    wait = src[src.index("data.type === 'stt_wait'"):][:900]
    assert "data.position" in wait and "data.eta_ms" in wait
    err = src[src.index("data.type === 'stt_error'"):][:900]
    assert "data.code === 'busy'" in err
    tts = src[src.index("data.type === 'tts_state'"):][:6000]
    assert "data.reason === 'busy'" in tts


def test_local_whisper_does_not_block_the_loop(dictation_env, monkeypatch):
    """The local faster-whisper lane runs in an executor thread under stt_local, so the
    event loop serves everyone else meanwhile. MUTATION: call _transcribe_local directly
    in the coroutine - red."""
    from vaf.core import speech_api
    ws_mod = dictation_env["ws_mod"]
    monkeypatch.setattr(speech_api, "resolve_stt_engine", lambda: "local")
    window = {}

    class _Seg:
        text = "lokal erkannt"

    class _Model:
        def transcribe(self, path, beam_size=5):
            def segments():
                window["thread"] = threading.get_ident()
                window["start"] = time.monotonic()
                time.sleep(0.3)
                window["end"] = time.monotonic()
                yield _Seg()
            return segments(), None

    monkeypatch.setattr(ws_mod, "get_whisper_model", lambda: _Model())
    ticks = []
    sock = FakeWebSocket([_dictation_frame()])

    async def main():
        window["loop_thread"] = threading.get_ident()
        done = asyncio.Event()

        async def ticker():
            while not done.is_set():
                ticks.append(time.monotonic())
                await asyncio.sleep(0.01)

        tick_task = asyncio.ensure_future(ticker())
        await ws_mod.websocket_endpoint(sock, token=dictation_env["token"])
        done.set()
        await tick_task

    asyncio.run(main())
    assert [p["text"] for p in sock.sent if p.get("type") == "stt_result"] == ["lokal erkannt"]
    during = [t for t in ticks if window["start"] < t < window["end"]]
    assert len(during) >= 5, "the event loop stood still while whisper ran"
    assert window["thread"] != window["loop_thread"]
    m = next(m for m in fair_queue.queue_metrics() if m["name"] == "stt_local")
    assert m["served"] == 1


def test_read_aloud_busy_says_so(dictation_env, monkeypatch):
    """A read-aloud the TTS queue turned away stops with reason busy (the page shows a
    toast), never as a silent stop."""
    from vaf.core.config import Config
    from vaf.core.speech import SpeechManager

    class _BusySM(_DictationSM):
        def synthesize_audio(self, *a, **kw):
            assert kw.get("busy_raises") is True and kw.get("user_key") == SCOPE
            raise fair_queue.QueueBusy("tts", "timeout", position=2, waited_s=15.0)

    monkeypatch.setattr(SpeechManager, "get_instance", classmethod(lambda cls: _BusySM()))
    dictation_env["cfg"]["speech_tts_enabled"] = True
    assert Config.get("speech_tts_enabled") is True
    sent = _run(dictation_env, FakeWebSocket([{"type": "speak", "text": "Hallo zusammen"}]))
    stops = [p for p in sent if p.get("type") == "tts_state" and p.get("status") == "stopped"]
    assert stops and stops[-1].get("reason") == "busy"


# ── the voice call ───────────────────────────────────────────────────────────────

def test_call_turn_reports_its_place(call_env):
    """A turn behind another person's STT hears its place, then position 0, then its
    reply. MUTATION: do not pass on_wait from the handler to turn() - red."""
    holder = _Holder(release_when=_stt_waiting)
    try:
        sent = _run(call_env, _PacedSocket(
            [START, _turn()], until=lambda s: _types(s).count("voice_call_reply") >= 2))
    finally:
        holder.release()
    voice = [p for p in sent if str(p.get("type", "")).startswith("voice_call")]
    waits = [p for p in voice if p["type"] == "voice_call_wait"]
    assert waits, "the waiting turn was never told its place"
    assert waits[0]["resource"] == "stt" and waits[0]["position"] >= 1
    assert waits[-1]["position"] == 0
    after = _types(voice[voice.index(waits[-1]):])
    assert "voice_call_reply" in after
    assert call_env["box"].urls, "the turn never reached the container"


def test_call_busy_is_spoken_not_silent(call_env, monkeypatch):
    """A turn whose STT wait runs out gets the busy line in the call language, with audio,
    instead of silence. MUTATION: map busy to the generic error (no text, no audio) - red."""
    from vaf.core import vocab
    monkeypatch.setattr(sc, "WAIT_LIVE_S", 0.4)
    holder = _Holder()
    try:
        sent = _run(call_env, _PacedSocket(
            [START, _turn()], until=lambda s: "voice_call_error" in _types(s)))
    finally:
        holder.release()
    errors = [p for p in sent if p.get("type") == "voice_call_error"]
    assert len(errors) == 1
    err = errors[0]
    assert err["error"] == "busy" and err["resource"] == "stt" and err["reason"] == "timeout"
    assert err["text"] in vocab.phrasings("voice_busy", "de")
    assert err["audio"]
    assert call_env["box"].urls == []


def test_tts_busy_keeps_the_reply_text(call_env, monkeypatch):
    """A reply whose voice could not get a TTS slot still arrives, as text, flagged.
    MUTATION: drop tts_busy, or clear the reply on TTS busy - red."""
    from vaf.core.speech import SpeechManager

    class _BusyTTS:
        def synthesize_audio(self, text, lang, **kw):
            if kw.get("busy_raises"):
                raise fair_queue.QueueBusy("tts", "timeout", waited_s=20.0)
            return b"AUDIO"

        def call_lane_speaks(self, lang):
            return True

    monkeypatch.setattr(SpeechManager, "get_instance", classmethod(lambda cls: _BusyTTS()))
    sent = _run(call_env, FakeWebSocket([START, _turn()]))
    replies = [p for p in sent if p.get("type") == "voice_call_reply"]
    assert len(replies) == 1, "a refused greeting is not sent, the reply is"
    reply = replies[0]
    assert reply["tts_busy"] is True and reply["audio"] is None
    assert len(reply["reply"]) > 100


def test_hangup_cancels_the_waiting_turn(call_env, monkeypatch):
    """A voice_call_end that arrives while the turn waits for STT takes the turn out: the
    container is never called for it, nothing is said, and the end is still processed, in
    order. MUTATION: remove the read-ahead (the turn waits out its deadline and reports
    busy), or peek the end and drop it (no voice_call_ended) - red."""
    monkeypatch.setattr(sc, "WAIT_LIVE_S", 1.5)
    holder = _Holder()
    try:
        sent = _run(call_env, _PacedSocket(
            [START, _turn(), {"type": "voice_call_end"}],
            until=lambda s: "voice_call_ended" in _types(s), timeout=5.0))
        stt = next(m for m in fair_queue.queue_metrics() if m["name"] == "stt")
    finally:
        holder.release()
    types = _types(sent)
    assert "voice_call_error" not in types
    assert "voice_call_ended" in types
    assert types.index("voice_call_wait") < types.index("voice_call_ended")
    assert stt["refused"]["cancelled"] == 1
    assert call_env["box"].urls == []
    assert not call_env["ws_mod"]._VOICE_CALLS
