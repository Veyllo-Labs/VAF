# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Every speech lane waits in its fair queue, at exactly one place (vaf/core/speech_client.py).

The containers are faked at the HTTP seam with a counter of concurrent requests: the
measured fact the queue rests on is that a speech container runs one request at a time,
so the property under test is "at most one request is in flight per container".
"""
import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

from vaf.core import fair_queue
from vaf.core import speech_client as sc

_REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _local_lanes(monkeypatch):
    """No cloud provider, local models allowed, a fresh queue registry."""
    from vaf.core import inference_profile, speech_api
    monkeypatch.setattr(fair_queue, "_registry", {})
    monkeypatch.setattr(speech_api, "select_stt_backend", lambda *a, **kw: (None, None))
    monkeypatch.setattr(speech_api, "select_tts_backend", lambda *a, **kw: (None, None, None))
    monkeypatch.setattr(speech_api, "synthesize", lambda *a, **kw: None)
    monkeypatch.setattr(inference_profile, "local_inference_allowed", lambda *a, **kw: True)


class _Resp:
    def __init__(self, status=200, data=None, content=b""):
        self.status_code = status
        self.ok = status < 400
        self._data = data
        self.content = content
        self.text = json.dumps(data) if data is not None else ""

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


def _speech_endpoint(url) -> bool:
    """Patching `requests.post` patches it for the whole process: a background thread an
    earlier test left running (the TUI boot's heartbeat to /api/heartbeat) posts through
    the same fake. Only the speech services count; anything else gets the connection
    error of a server that is not there."""
    return any(str(url).startswith(base) for base in
               (sc._stt_base_url(), sc._tts_base_url(), sc.chatterbox_base_url()))


class _Container:
    """A fake speech container: records the URLs it got and the peak concurrency."""

    def __init__(self, answer, hold_s=0.05):
        self.answer = answer
        self.hold_s = hold_s
        self.urls = []
        self.inflight = 0
        self.peak = 0
        self._lock = threading.Lock()

    def http_post(self, url, *a, **kw):
        """The `requests.post` stand-in: speech endpoints only (see _speech_endpoint)."""
        if not _speech_endpoint(url):
            raise sc.requests.ConnectionError(f"not a speech endpoint: {url}")
        return self.post(url, *a, **kw)

    def post(self, url, *a, **kw):
        with self._lock:
            self.urls.append(url)
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
        try:
            time.sleep(self.hold_s)
            return self.answer(url)
        finally:
            with self._lock:
                self.inflight -= 1


def _metric(name):
    return next((m for m in fair_queue.queue_metrics() if m["name"] == name), None)


def _in_threads(fn, n=3):
    results = [None] * n

    def run(i):
        results[i] = fn(i)

    threads = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    return results


RIFF = b"RIFF" + b"\x00" * 64


# ── STT ──────────────────────────────────────────────────────────────────────────

def test_container_stt_runs_one_at_a_time(monkeypatch):
    """Three people dictate at once: the container sees one request at a time, and every
    one is answered. MUTATION: remove the slot around the container posts - red."""
    box = _Container(lambda url: _Resp(200, {"text": "hallo", "language": "de"}))
    monkeypatch.setattr(sc.requests, "post", box.http_post)
    results = _in_threads(lambda i: sc.transcribe(b"audio", user_key=f"person-{i}"))
    assert results == [("hallo", "de")] * 3
    assert box.peak == 1
    assert _metric("stt")["served"] == 3


def test_asr_fallback_shares_one_slot(monkeypatch):
    """/asr answering 404 is retried on /transcribe inside the SAME slot. MUTATION: take a
    second slot for the fallback (the re-entrancy guard refuses it, no text) - red."""
    box = _Container(lambda url: _Resp(404) if url.endswith("/asr")
                     else _Resp(200, {"text": "ok", "language": "en"}), hold_s=0)
    monkeypatch.setattr(sc.requests, "post", box.http_post)
    assert sc.transcribe(b"audio", user_key="p") == ("ok", "en")
    assert [u.rsplit("/", 1)[1] for u in box.urls] == ["asr", "transcribe"]
    assert _metric("stt")["served"] == 1


def test_api_lane_is_not_gated(monkeypatch):
    """A cloud STT has no local capacity to protect: never serialized, only measured.
    MUTATION: gate stt_api with one slot - red."""
    from vaf.core import speech_api
    box = _Container(lambda _: ("cloud", "en"), hold_s=0.1)
    monkeypatch.setattr(speech_api, "select_stt_backend", lambda *a, **kw: ("veyllo", "m"))
    monkeypatch.setattr(speech_api, "transcribe", lambda *a, **kw: box.post("api"))
    results = _in_threads(lambda i: sc.transcribe(b"audio", user_key="same-person"))
    assert results == [("cloud", "en")] * 3
    assert box.peak == 3
    m = _metric("stt_api")
    assert m["slots"] is None and m["served"] == 3


def test_a_refused_stt_is_busy_only_when_asked(monkeypatch):
    """busy_raises separates "busy" from "failed"; without it the old contract holds."""
    box = _Container(lambda url: _Resp(200, {"text": "x"}), hold_s=0)
    monkeypatch.setattr(sc.requests, "post", box.http_post)
    q = sc.speech_queue("stt")
    release, held = threading.Event(), threading.Event()

    def hold():
        with q.slot("someone-else"):
            held.set()
            release.wait(5)

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    assert held.wait(5)
    assert sc.transcribe(b"audio", user_key="p", deadline_s=0.1) == (None, None)
    with pytest.raises(fair_queue.QueueBusy) as excinfo:
        sc.transcribe(b"audio", user_key="p", deadline_s=0.1, busy_raises=True)
    assert excinfo.value.reason == "timeout" and excinfo.value.resource == "stt"
    release.set()
    t.join(5)
    assert box.urls == []


# ── TTS ──────────────────────────────────────────────────────────────────────────

def test_tts_queue_by_url(monkeypatch):
    """The chatterbox server and the TTS container are different resources with their
    own queues. MUTATION: map the chatterbox URL to `tts` - red."""
    box = _Container(lambda url: _Resp(200, content=RIFF), hold_s=0)
    monkeypatch.setattr(sc.requests, "post", box.http_post)
    assert sc.synthesize_docker("hallo", "de", base_url=sc.chatterbox_base_url()) == RIFF
    assert sc.synthesize_docker("hallo", "de") == RIFF
    assert _metric("tts_chatterbox")["served"] == 1
    assert _metric("tts")["served"] == 1


def test_container_tts_runs_one_at_a_time(monkeypatch):
    box = _Container(lambda url: _Resp(200, content=RIFF))
    monkeypatch.setattr(sc.requests, "post", box.http_post)
    assert _in_threads(lambda i: sc.synthesize("hallo", "de", user_key=f"p{i}")) == [RIFF] * 3
    assert box.peak == 1


def _bare_speech_manager():
    from vaf.core.speech import SpeechManager
    sm = SpeechManager.__new__(SpeechManager)
    sm._is_speaking = False
    return sm


def test_synthesize_audio_takes_one_slot(monkeypatch):
    """SpeechManager's docker lane hands the queue to synthesize_docker: one slot, no
    nesting. MUTATION: a second admission around the docker branch (refused as re-entrant,
    no audio) - red."""
    box = _Container(lambda url: _Resp(200, content=RIFF), hold_s=0)
    monkeypatch.setattr(sc.requests, "post", box.http_post)
    sm = _bare_speech_manager()
    assert sm.synthesize_audio("Hallo du", "de", force_engine="docker", user_key="p") == RIFF
    assert _metric("tts")["served"] == 1


def test_piper_lane_is_queued(monkeypatch, tmp_path):
    """The Piper binary (the CLI read-aloud) runs one synthesis at a time. MUTATION: drop
    the tts_piper admission - red."""
    import subprocess

    from vaf.core import speech as speech_mod
    sm = _bare_speech_manager()
    monkeypatch.setattr(sm, "_check_piper", lambda: True, raising=False)
    monkeypatch.setattr(sm, "_ensure_voice_model", lambda lang: tmp_path / "voice.onnx",
                        raising=False)
    monkeypatch.setattr(sm, "_get_piper_binary", lambda: tmp_path / "piper", raising=False)
    box = _Container(lambda _: None, hold_s=0.05)

    def fake_run(args, **kw):
        out = args[args.index("--output_file") + 1]
        box.post("piper")
        Path(out).write_bytes(RIFF * 20)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(speech_mod.subprocess, "run", fake_run)
    results = _in_threads(lambda i: sm.synthesize_audio("Hallo", "de", force_engine="piper",
                                                        user_key=f"p{i}"))
    assert all(r == RIFF * 20 for r in results)
    assert box.peak == 1
    assert _metric("tts_piper")["served"] == 3


# ── channels ─────────────────────────────────────────────────────────────────────

class _Get:
    def __init__(self, data=None, content=b""):
        self.ok = True
        self.status_code = 200
        self._data = data
        self.content = content

    def json(self):
        return self._data


def _telegram_downloads(monkeypatch):
    import vaf.api.telegram_bridge as tg
    from vaf.core import threat_db

    def fake_get(url, timeout=None):
        if "getFile" in url:
            return _Get({"result": {"file_path": "voice/file_1.oga"}})
        return _Get(content=b"OggS-voice")

    monkeypatch.setattr(tg.requests, "get", fake_get)
    monkeypatch.setattr(threat_db, "refuse_known_bad", lambda *a, **kw: False)
    return tg


def test_telegram_transcribes_off_the_loop_with_the_scope(monkeypatch):
    """The transcription, and its wait for an STT slot, run off the bridge's event loop,
    keyed on the sender's scope. MUTATION: call transcribe in the coroutine, or omit
    user_key - red."""
    tg = _telegram_downloads(monkeypatch)
    seen = {}

    def fake_transcribe(path, **kw):
        seen["thread"] = threading.get_ident()
        seen["kw"] = kw
        return "hallo", "de"

    monkeypatch.setattr(sc, "transcribe", fake_transcribe)

    async def run():
        seen["loop_thread"] = threading.get_ident()
        return await tg._transcribe_voice("token", "file-1", "tenant-scope-1")

    assert asyncio.run(run()) == ("hallo", "de")
    assert seen["thread"] != seen["loop_thread"]
    assert seen["kw"]["user_key"] == "tenant-scope-1"
    assert seen["kw"]["busy_raises"] is True


def test_telegram_busy_says_busy(monkeypatch):
    """A voice note the STT queue turned away is answered with the busy line in the
    sender's language, not "could not understand". MUTATION: reply with the old line - red."""
    from vaf.core import vocab
    tg = _telegram_downloads(monkeypatch)

    def busy(*a, **kw):
        raise fair_queue.QueueBusy("stt", "timeout", position=4, waited_s=180.0)

    monkeypatch.setattr(sc, "transcribe", busy)
    monkeypatch.setattr(vocab, "resolve_user_language", lambda *a, **kw: "de")

    class _Msg:
        def __init__(self):
            self.replies = []

        async def reply_text(self, text):
            self.replies.append(text)

    msg = _Msg()
    result = asyncio.run(tg._voice_note_text(msg, "token", "file-1", "tenant-scope-1", "tenant"))
    assert result == (None, None)
    assert len(msg.replies) == 1
    assert msg.replies[0] in vocab.phrasings("speech_busy_text", "de")


def test_whatsapp_passes_the_scope(monkeypatch, tmp_path):
    """WhatsApp's STT and TTS, and the send tool's TTS, wait under the scope they hold.
    MUTATION: omit user_key in any of the three - red."""
    import vaf.api.whatsapp_bridge as wa
    from vaf.core import threat_db
    seen = []
    monkeypatch.setattr(threat_db, "inspect_upload_file",
                        lambda *a, **kw: type("V", (), {"blocked": False})())
    monkeypatch.setattr(sc, "transcribe",
                        lambda path, **kw: seen.append(("stt", kw.get("user_key"))) or ("hi", "en"))
    monkeypatch.setattr(sc, "synthesize",
                        lambda text, lang, **kw: seen.append(("tts", kw.get("user_key"))) or b"OggS")
    note = tmp_path / "n.ogg"
    note.write_bytes(b"OggS")
    assert wa._transcribe_voice_file(str(note), "wa-scope-1") == ("hi", "en")
    path = wa._synthesize_voice_for_reply("hallo", "de", "wa-scope-1")
    assert path
    Path(path).unlink()
    from vaf.tools.send_whatsapp import SendWhatsAppTool
    out = SendWhatsAppTool()._synthesize_voice("hallo", "de", "wa-scope-1")
    assert out
    Path(out).unlink()
    assert seen == [("stt", "wa-scope-1"), ("tts", "wa-scope-1"), ("tts", "wa-scope-1")]


def test_whatsapp_busy_keeps_the_media_marker(monkeypatch, tmp_path):
    import vaf.api.whatsapp_bridge as wa
    from vaf.core import threat_db
    monkeypatch.setattr(threat_db, "inspect_upload_file",
                        lambda *a, **kw: type("V", (), {"blocked": False})())

    def busy(*a, **kw):
        assert kw.get("busy_raises") is True
        raise fair_queue.QueueBusy("stt", "full", position=13)

    monkeypatch.setattr(sc, "transcribe", busy)
    note = tmp_path / "n.ogg"
    note.write_bytes(b"OggS")
    assert wa._transcribe_voice_file(str(note), "wa-scope-1") == (None, None)


# ── the spoken and written lines ─────────────────────────────────────────────────

@pytest.mark.parametrize("key", ["voice_busy", "speech_busy_text"])
def test_the_busy_lines_exist_in_the_seven_languages(key):
    """MUTATION: delete `th` from voice_busy - red."""
    data = json.loads((_REPO / "vaf" / "core" / "vocab" / "data" / f"{key}.json")
                      .read_text(encoding="utf-8"))
    for lang in ("en", "de", "tr", "zh", "ja", "ko", "th"):
        assert data.get(lang) and all(s.strip() for s in data[lang]), f"{key}: {lang} missing"
    seed = json.loads((_REPO / "vaf" / "core" / "vocab" / "source" / f"{key}.json")
                      .read_text(encoding="utf-8"))
    assert seed["en"] == data["en"] and seed["de"] == data["de"]


def test_a_busy_whatsapp_voice_reply_goes_out_as_text_without_a_second_wait(monkeypatch):
    """A reply to a voice note whose TTS got no slot is sent as text at once: busy is no
    reason to wait a second time for an English voice. MUTATION: let the busy refusal
    fall through to the English retry - two waits, red."""
    import queue as _queue

    import vaf.api.whatsapp_bridge as wa
    calls = []

    def busy(text, lang, **kw):
        calls.append(lang)
        if kw.get("busy_raises"):
            raise fair_queue.QueueBusy("tts", "timeout", waited_s=180.0)
        return None

    out = _queue.Queue()
    monkeypatch.setattr(sc, "synthesize", busy)
    monkeypatch.setattr(wa, "_is_reply_allowed", lambda *a, **kw: True)
    monkeypatch.setattr(wa, "_outgoing_queue", out)
    monkeypatch.setitem(wa._voice_reply_pending, "tenant|123@s.whatsapp.net", "de")
    assert wa._enqueue_reply("tenant", "123@s.whatsapp.net", "Hallo!", user_scope_id="wa-scope-1")
    assert calls == ["de"]
    assert out.get_nowait() == ("tenant", "123@s.whatsapp.net", "Hallo!", None)


def test_local_tts_counts_no_cloud_request(monkeypatch):
    """Without a TTS provider, SpeechManager never enters the cloud lane, so `vaf health`
    shows no tts_api traffic that did not happen. MUTATION: call synthesize_api without
    asking for a provider - red."""
    box = _Container(lambda url: _Resp(200, content=RIFF), hold_s=0)
    monkeypatch.setattr(sc.requests, "post", box.http_post)
    sm = _bare_speech_manager()
    assert sm.synthesize_audio("Hallo du", "de", force_engine="docker", user_key="p") == RIFF
    assert _metric("tts_api") is None and _metric("tts")["served"] == 1


def test_telegram_voice_reply_waits_briefly_on_the_one_sender_thread(monkeypatch):
    """Every Telegram chat is sent by one thread: a voice reply waits at most WAIT_LIVE_S
    for a TTS slot, then the caller sends the text. MUTATION: the 180 s default - red."""
    import vaf.api.telegram_bridge as tg
    seen = {}
    monkeypatch.setattr(sc, "synthesize", lambda text, lang, **kw: seen.update(kw) or None)
    monkeypatch.setitem(tg._voice_reply_scope, "77", "tenant-scope-1")
    assert asyncio.run(tg._send_voice_reply("token", "77", "Hallo", "de")) is False
    assert seen["deadline_s"] == sc.WAIT_LIVE_S and seen["user_key"] == "tenant-scope-1"


def test_send_whatsapp_says_busy_not_misconfigured(monkeypatch):
    """A busy TTS queue is a load problem: the tool says so and suggests text or a retry,
    without the settings hint a failed TTS gets, and waits no longer than WAIT_LIVE_S.
    MUTATION: drop busy_raises in the tool (the "TTS failed, check settings" answer) - red."""
    from vaf.tools.send_whatsapp import SendWhatsAppTool
    seen = {}

    def busy(text, lang, **kw):
        seen.update(kw)
        if kw.get("busy_raises"):
            raise fair_queue.QueueBusy("tts", "timeout", waited_s=15.0)
        return None

    monkeypatch.setattr(sc, "synthesize", busy)
    answer = SendWhatsAppTool().run(message="Hallo", to_phone="+491761234567",
                                    voice_lang="de", user_scope_id="wa-scope-1",
                                    username="tenant")
    assert "speech service is busy" in answer and "Settings" not in answer
    assert seen["deadline_s"] == sc.WAIT_LIVE_S and seen["user_key"] == "wa-scope-1"


def test_every_speech_queue_has_a_share_per_person():
    """MUTATION: build the speech queues without max_waiting_per_key - red."""
    for name in ("stt", "stt_local", "tts", "tts_chatterbox", "tts_piper"):
        q = sc.speech_queue(name)
        assert q.max_waiting == 12 and q.max_waiting_per_key == sc.SPEECH_MAX_WAITING_PER_KEY == 3
