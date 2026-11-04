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

# Every hold and wait below ends on an event; this is only the backstop against a hung test.
# A wall-clock budget that starts before websocket_endpoint runs includes the handshake, and
# the handshake is not this file's to time. On the Windows runner it took 24 s (the account
# store's twelve refused connects, about two seconds each there, before voice_env pinned the
# store), and a 5 or 10 second hold had ended before the turn queued behind it.
_BACKSTOP_S = 120.0


class _PacedSocket(FakeWebSocket):
    """Like the baseline's socket, but once the script is played it waits (instead of
    hanging up at once) until `until(sent)` holds - a person who stays on the line. The
    timeout counts from the end of the script, so the handshake before it is not part of it."""

    def __init__(self, frames, until, timeout=10.0):
        super().__init__(frames)
        self._until = until
        self._timeout = timeout
        self._deadline = None

    async def receive_text(self) -> str:
        if self._frames:
            return self._frames.pop(0)
        if self._deadline is None:
            self._deadline = time.monotonic() + self._timeout
        while not self._until(self.sent) and time.monotonic() < self._deadline:
            await asyncio.sleep(0.01)
        raise self._disconnect(code=1000)


class _Box:
    def __init__(self, text="wie wird das wetter heute in berlin und morgen"):
        self.text = text
        self.urls = []

    def post(self, url, *a, **kw):
        # Patching `requests.post` patches it for the whole process: a background thread
        # an earlier test left running (the TUI boot's heartbeat to /api/heartbeat) posts
        # through this fake too. Only the STT container counts; anything else gets the
        # connection error of a server that is not there.
        if not str(url).startswith(sc._stt_base_url()):
            raise sc.requests.ConnectionError(f"not the STT container: {url}")
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
            end = time.monotonic() + _BACKSTOP_S
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


def _waiting(name):
    m = next((m for m in fair_queue.queue_metrics() if m["name"] == name), None)
    return bool(m and m["waiting"])


def _stt_waiting():
    return _waiting("stt")


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


# ── round 1: the refused turn, the TTS budget, the read-ahead's limits ────────────

def test_a_refused_turn_always_names_stt(call_env, monkeypatch):
    """A turn is only refused at its STT step, whatever name the recognizer's queue has.
    MUTATION: derive the resource from the queue name ("my-asr" -> "tts") - red."""
    def busy(*a, **kw):
        raise fair_queue.QueueBusy("my-asr", "timeout", position=2, waited_s=15.0)

    monkeypatch.setattr(sc, "transcribe", busy)
    sent = _run(call_env, FakeWebSocket([START, _turn()]))
    err = [p for p in sent if p.get("type") == "voice_call_error"]
    assert len(err) == 1 and err[0]["error"] == "busy" and err[0]["resource"] == "stt"


def test_a_late_tts_slot_still_gets_its_synthesis_time(call_env, monkeypatch):
    """The outer timeout of a call's TTS is the queue wait PLUS the synthesis budget: a
    greeting whose slot comes late is still spoken. MUTATION: the synthesis budget alone
    as the timeout (the greeting times out and is never sent) - red."""
    from vaf.core.speech import SpeechManager
    ws_mod = call_env["ws_mod"]
    monkeypatch.setattr(ws_mod, "_CALL_TTS_WAIT_S", 2.0)
    monkeypatch.setitem(ws_mod._CALL_TTS_SYNTH_S, "short", 0.8)

    class _QueuedTTS:
        def synthesize_audio(self, text, lang, **kw):
            with sc.speech_queue("tts").slot(kw.get("user_key"), deadline_s=kw.get("deadline_s"),
                                             cancelled=kw.get("cancelled"),
                                             on_wait=kw.get("on_wait"), poll_s=0.01):
                time.sleep(0.5)
            return b"AUDIO"

        def call_lane_speaks(self, lang):
            return True

    monkeypatch.setattr(SpeechManager, "get_instance", classmethod(lambda cls: _QueuedTTS()))
    held, release = threading.Event(), threading.Event()

    def hold():
        with sc.speech_queue("tts").slot("another-person"):
            held.set()
            # The slot comes 0.7 s after the greeting queued for it, however long the
            # handshake before took.
            end = time.monotonic() + _BACKSTOP_S
            while not _waiting("tts") and not release.is_set() and time.monotonic() < end:
                time.sleep(0.01)
            release.wait(0.7)

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    assert held.wait(5)
    try:
        sent = _run(call_env, _PacedSocket(
            [START], until=lambda s: "voice_call_reply" in _types(s), timeout=6.0))
    finally:
        release.set()
        t.join(5)
    greetings = [p for p in sent if p.get("type") == "voice_call_reply" and p.get("greeting")]
    assert greetings and greetings[0]["audio"]


def test_a_reply_whose_voice_runs_out_of_time_arrives_as_text(call_env, monkeypatch):
    """No audio within the budget is not a silent drop: the reply carries tts_busy and
    its text. MUTATION: catch only QueueBusy (a timeout loses the text) - red."""
    from vaf.core.speech import SpeechManager
    ws_mod = call_env["ws_mod"]
    monkeypatch.setattr(ws_mod, "_CALL_TTS_WAIT_S", 0.1)
    monkeypatch.setitem(ws_mod._CALL_TTS_SYNTH_S, "short", 0.2)
    monkeypatch.setitem(ws_mod._CALL_TTS_SYNTH_S, "reply", 0.2)

    class _SlowTTS:
        def synthesize_audio(self, text, lang, **kw):
            time.sleep(0.8)
            return b"AUDIO"

        def call_lane_speaks(self, lang):
            return True

    monkeypatch.setattr(SpeechManager, "get_instance", classmethod(lambda cls: _SlowTTS()))
    sent = _run(call_env, FakeWebSocket([START, _turn()]))
    replies = [p for p in sent if p.get("type") == "voice_call_reply"]
    assert len(replies) == 1
    assert replies[0]["tts_busy"] is True and replies[0]["audio"] is None
    assert len(replies[0]["reply"]) > 100


class _StreamSocket:
    """Frames on demand: `size` characters each, one every `every` seconds."""

    def __init__(self, size, every=0.0):
        self.size, self.every, self.reads = size, every, 0

    async def receive_text(self):
        self.reads += 1
        await asyncio.sleep(self.every)
        return "x" * self.size


def test_the_read_ahead_stops_at_its_byte_budget():
    """A waiting turn parks at most a few MiB of frames, not 32 frames of up to 200 MiB.
    MUTATION: drop the byte check - red."""
    import vaf.core.web_server as ws_mod
    sock = _StreamSocket(1024 * 1024)
    rx = ws_mod._ReadAhead(sock)

    async def main():
        armed = asyncio.Event()
        armed.set()
        await rx.wait(asyncio.sleep(0.3), armed, threading.Event())

    asyncio.run(main())
    assert rx._buffered_bytes <= ws_mod._READ_AHEAD_MAX_BYTES + 1024 * 1024
    assert len(rx._buffer) <= 5


def test_the_read_ahead_stops_when_the_wait_is_over():
    """The grant clears `armed`: the rest of the turn (its LLM step) reads nothing ahead.
    MUTATION: keep reading once anything was buffered - red."""
    import vaf.core.web_server as ws_mod
    sock = _StreamSocket(10, every=0.02)
    rx = ws_mod._ReadAhead(sock)

    async def main():
        armed = asyncio.Event()
        armed.set()
        asyncio.get_running_loop().call_later(0.07, armed.clear)
        await rx.wait(asyncio.sleep(0.5), armed, threading.Event())

    asyncio.run(main())
    assert sock.reads <= 6, f"read {sock.reads} frames after the wait was over"


def test_the_grant_disarms_the_read_ahead():
    """MUTATION: arm on every report, the grant included - red."""
    import vaf.core.web_server as ws_mod

    class _Sock:
        async def send_json(self, payload):
            pass

    async def main():
        loop = asyncio.get_running_loop()
        armed = asyncio.Event()
        on_wait = ws_mod._queue_wait_sender(_Sock(), loop, "voice_call_wait", armed=armed)
        await loop.run_in_executor(None, on_wait, fair_queue.WaitInfo("stt", 1, None, 0.0))
        await asyncio.sleep(0.01)
        first = armed.is_set()
        await loop.run_in_executor(None, on_wait, fair_queue.WaitInfo("stt", 0, None, 0.4))
        await asyncio.sleep(0.01)
        return first, armed.is_set()

    assert asyncio.run(main()) == (True, False)


# ── round 1: the frontend's half ─────────────────────────────────────────────────

def test_every_call_status_has_its_words():
    """Each statusKey the call window sets is a `status_<key>` in every locale, and
    'waiting' never asks for its parametrised string without a place (a refused wait has
    no grant event). MUTATION: leave 'waiting' in the tts_busy branch, or drop the
    no-place fallback - red."""
    import json
    import re
    src = (_REPO / "web" / "components" / "VoiceCallLayer.tsx").read_text(encoding="utf-8")
    keys = set(re.findall(r"statusKey: '(\w+)'", src))
    assert {"waiting", "busy", "tts_busy"} <= keys
    for locale in ("de", "en", "ja", "ko", "th", "tr", "zh"):
        cat = json.loads((_REPO / "web" / "messages" / f"{locale}.json")
                         .read_text(encoding="utf-8"))["voiceCall"]
        assert all(f"status_{k}" in cat for k in keys), locale
    branch = src[src.index("if (saysSomething && (data.tts_busy || !data.audio)) {"):][:900]
    assert "statusKey: 'tts_busy'" in branch
    assert re.search(r": store\.statusKey === 'waiting'\s*\?\s*\(!store\.wait \? t\('status_thinking'\)",
                     src), "'waiting' without a place reaches the parametrised string"


def test_a_text_only_reply_can_be_read_whole():
    """Small talk never enters the chat, so the caption is the only copy: never clamped,
    scrollable, kept apart from the current status (textReply), and replaced only by a
    reply that says something - not by the next utterance, a silent reply, or a refused
    turn. MUTATION: clamp it, clear it on accept, let a silent reply clear it, or let the
    busy branch touch it - red."""
    import re
    src = (_REPO / "web" / "components" / "VoiceCallLayer.tsx").read_text(encoding="utf-8")
    caption = src[src.index("{store.textReply && ("):][:900]
    assert "line-clamp" not in caption and "overflow-y-auto" in caption
    assert "{store.textReply}" in caption
    accept = src[src.index("playEarcon('accept');\n            // A new turn starts"):][:600]
    assert "textReply" not in accept.split("try {")[0].split("//")[-1]
    assert "busyNotice: null" in accept
    assert "const saysSomething = !data.greeting && !data.silent && !!String(data.reply || '').trim();" in src
    # Any reply that says something without audio becomes the caption, tts_busy or not;
    # only a reply that says something WITH audio clears it.
    assert re.search(r"if \(saysSomething && \(data\.tts_busy \|\| !data\.audio\)\)\s*\{[^}]*"
                     r"textReply: String\(data\.reply", src)
    assert re.search(r"\} else if \(saysSomething\) \{[^}]*textReply: null", src)
    busy = src[src.index("data.type === 'voice_call_error' && data.error === 'busy'"):][:700]
    assert "textReply" not in busy


def test_the_dictation_place_is_outside_the_clipping_form():
    """The form around the composer clips (overflow-hidden); the place in line is drawn
    in the row around it, not inside the mic button. MUTATION: move it back into the
    button - red."""
    src = (_REPO / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    bubble = src.index("{sttWaitText}\n")
    form = src.index("onSubmit={sendMessage}")
    assert bubble < form
    mic = src[src.index("onClick={isRecording ? stopRecording : startRecording}"):][:1500]
    assert "sttWaitText}" not in mic.split("</button>")[0].replace(
        "(isProcessingAudio && sttWaitText) ? sttWaitText", "")


# ── round 2 ──────────────────────────────────────────────────────────────────────

def test_the_read_ahead_budget_counts_bytes_not_characters():
    """A frame of 3-byte characters weighs three times its len(). MUTATION: count
    characters (about 12 MiB parked instead of 4) - red."""
    import vaf.core.web_server as ws_mod
    sock = _StreamSocket(1)
    sock_frame = "€" * (512 * 1024)        # 1.5 MiB as UTF-8, 0.5 M characters

    async def receive_text():
        sock.reads += 1
        await asyncio.sleep(0)
        return sock_frame

    sock.receive_text = receive_text
    rx = ws_mod._ReadAhead(sock)

    async def main():
        armed = asyncio.Event()
        armed.set()
        await rx.wait(asyncio.sleep(0.3), armed, threading.Event())

    asyncio.run(main())
    parked = sum(len(f.encode("utf-8")) for f in rx._buffer)
    assert parked <= ws_mod._READ_AHEAD_MAX_BYTES + len(sock_frame.encode("utf-8"))
    assert rx._buffered_bytes == parked


def test_a_reply_whose_budget_ran_out_in_the_queue_leaves_the_line(call_env, monkeypatch):
    """A TTS request still queued when its outer budget passes (its executor started
    late) leaves the queue instead of synthesizing for nobody; the call's own cancel stays
    untouched for its next turn. MUTATION: do not mark the request gone - it waits out its
    own deadline instead (refused.timeout, not cancelled) - red."""
    from vaf.core.speech import SpeechManager
    ws_mod = call_env["ws_mod"]
    monkeypatch.setattr(ws_mod, "_CALL_TTS_WAIT_S", 0.6)
    monkeypatch.setitem(ws_mod._CALL_TTS_SYNTH_S, "reply", 0.1)

    class _LateTTS:
        def synthesize_audio(self, text, lang, **kw):
            if len(text) < 100:
                return b"AUDIO"                 # the greeting: no contention
            time.sleep(0.5)                     # the executor reached this request late
            with sc.speech_queue("tts").slot(kw.get("user_key"), deadline_s=kw["deadline_s"],
                                             cancelled=kw.get("cancelled"), poll_s=0.02):
                return b"AUDIO"

        def call_lane_speaks(self, lang):
            return True

    monkeypatch.setattr(SpeechManager, "get_instance", classmethod(lambda cls: _LateTTS()))
    held, release = threading.Event(), threading.Event()

    def hold():
        with sc.speech_queue("tts").slot("another-person"):
            held.set()
            release.wait(_BACKSTOP_S)

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    assert held.wait(5)
    try:
        sent = _run(call_env, FakeWebSocket([START, _turn()]))
        tts = lambda: next(m for m in fair_queue.queue_metrics() if m["name"] == "tts")  # noqa: E731
        end = time.monotonic() + 3
        while time.monotonic() < end and not sum(tts()["refused"].values()):
            time.sleep(0.02)
        refused = tts()["refused"]
    finally:
        release.set()
        t.join(5)
    replies = [p for p in sent if p.get("type") == "voice_call_reply" and not p.get("greeting")]
    assert replies and replies[0]["tts_busy"] is True
    assert refused["cancelled"] == 1 and refused["timeout"] == 0


# ── round 3 ──────────────────────────────────────────────────────────────────────

import concurrent.futures as _cf


class _DeferringExecutor(_cf.ThreadPoolExecutor):
    """Runs every job at once in a thread, except the Nth synthesize_audio job, which
    is held back until the test runs it: an executor that reaches a request late."""

    def __init__(self, hold_nth):
        super().__init__(max_workers=8)
        self._hold_nth, self._seen, self.held = hold_nth, 0, None

    @staticmethod
    def _is_tts(fn):
        func = getattr(fn, "func", None)
        if getattr(func, "__name__", "") == "synthesize_audio":
            return True
        return "synthesize_audio" in getattr(getattr(fn, "__code__", None), "co_names", ())

    def submit(self, fn, *args, **kwargs):
        if self._is_tts(fn):
            self._seen += 1
            if self._seen == self._hold_nth:
                self.held = fn
                return _cf.Future()       # never resolves: the request is still queued
        return super().submit(fn, *args, **kwargs)


def test_a_late_request_keeps_its_own_cancel_and_text(call_env, monkeypatch):
    """A reply TTS whose executor starts only after the call moved on (its budget ran out,
    the next turn answered) must carry its OWN text and its own expired cancel flag: it
    leaves the queue at once instead of waiting, or synthesizing the next turn's text.
    MUTATION: hand the executor a lambda (late binding: it reads the next turn's unset
    flag and text) - red."""
    import vaf.core.voice_agent as va
    from vaf.core.speech import SpeechManager
    ws_mod = call_env["ws_mod"]
    monkeypatch.setattr(ws_mod, "_CALL_TTS_WAIT_S", 0.3)
    monkeypatch.setitem(ws_mod._CALL_TTS_SYNTH_S, "reply", 0.1)
    replies = iter(["erste antwort " * 10, "zweite antwort " * 10])
    monkeypatch.setattr(va, "voice_reply",
                        lambda *a, **kw: {"reply": next(replies), "delegate": None,
                                          "silent": False})
    seen = []

    class _QueuedTTS:
        def synthesize_audio(self, text, lang, **kw):
            if len(text) < 100:
                return b"AUDIO"           # greeting
            seen.append(text)
            with sc.speech_queue("tts").slot(kw.get("user_key"), deadline_s=kw["deadline_s"],
                                             cancelled=kw.get("cancelled"), poll_s=0.02):
                return b"AUDIO"

        def call_lane_speaks(self, lang):
            return True

    monkeypatch.setattr(SpeechManager, "get_instance", classmethod(lambda cls: _QueuedTTS()))
    executor = _DeferringExecutor(hold_nth=2)     # 1 = greeting, 2 = the first reply

    async def main():
        asyncio.get_running_loop().set_default_executor(executor)
        sock = FakeWebSocket([START, _turn(), _turn()])
        await ws_mod.websocket_endpoint(sock, token=call_env["token"])
        return sock.sent

    sent = asyncio.run(main())
    replies_sent = [p for p in sent if p.get("type") == "voice_call_reply" and not p.get("greeting")]
    assert len(replies_sent) == 2 and replies_sent[0]["tts_busy"] is True
    assert executor.held is not None
    held, release = threading.Event(), threading.Event()

    def hold():
        with sc.speech_queue("tts").slot("another-person"):
            held.set()
            release.wait(5)

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    assert held.wait(5)
    try:
        started = time.monotonic()
        try:
            executor.held()
        except fair_queue.QueueBusy as busy:
            refusal = busy.reason
        else:
            refusal = "admitted"
        took = time.monotonic() - started
    finally:
        release.set()
        t.join(5)
    assert seen[-1].startswith("erste antwort"), "the late request synthesized the next turn's text"
    assert refusal == "cancelled" and took < 0.2
