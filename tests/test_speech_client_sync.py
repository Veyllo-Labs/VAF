# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Speech client centralization guards + unit tests.

Drift guard (CLAUDE.md Rule 2): the Whisper (/asr, /transcribe) and Piper
(/synthesize) HTTP contracts live ONLY in vaf/core/speech_client.py. Before
centralization they were copy-pasted at five call sites; this test fails when
someone adds a new direct call site instead of using the shared client.

Unit tests pin the client contracts with mocked HTTP (no containers needed).
"""
from pathlib import Path

import vaf.core.speech_client as sc


# ---------------------------------------------------------------------------
# Drift guards (source-text, pattern from test_channel_registry_sync.py)
# ---------------------------------------------------------------------------

def _src(module) -> str:
    """Module source with comment-only content stripped (comments may name the
    endpoints when explaining the shared client; only real code counts)."""
    raw = Path(module.__file__).read_text(encoding="utf-8")
    lines = []
    for line in raw.splitlines():
        stripped = line.split("#", 1)[0] if "#" in line and not line.lstrip().startswith(('"', "'")) else line
        lines.append(stripped)
    return "\n".join(lines)


def test_no_direct_speech_endpoints_outside_client():
    import vaf.api.telegram_bridge as tg
    import vaf.api.whatsapp_bridge as wa
    import vaf.tools.send_whatsapp as swa

    for mod in (tg, wa, swa):
        src = _src(mod)
        assert "/asr" not in src, f"{mod.__name__} bypasses speech_client (found /asr)"
        assert "/synthesize" not in src, f"{mod.__name__} bypasses speech_client (found /synthesize)"
        assert "/transcribe" not in src, f"{mod.__name__} bypasses speech_client (found /transcribe)"
        assert "speech_client" in src, f"{mod.__name__} does not use speech_client"


def test_web_server_process_audio_uses_client():
    import vaf.core.web_server as ws

    src = _src(ws)
    start = src.index('"process_audio"')
    end = src.index('"speak"', start)
    region = src[start:end]
    assert "/asr" not in region, "web_server process_audio bypasses speech_client"
    assert "speech_client" in region


def test_speech_manager_docker_branch_uses_client():
    import vaf.core.speech as sp

    src = _src(sp)
    assert "speech_client" in src
    assert "urllib.request.urlopen" not in src.split("def synthesize_audio", 1)[1].split("def ", 1)[0]


# ---------------------------------------------------------------------------
# Unit tests (mocked HTTP, pattern from test_update_check.py)
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status_code=200, json_data=None, content=b"", text=""):
        self.status_code = status_code
        self._json = json_data
        self.content = content
        self.text = text or (str(json_data) if json_data else "")
        self.ok = status_code < 400

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


def _cfg(monkeypatch, cfg):
    monkeypatch.setattr(sc.Config, "get", classmethod(lambda cls, k, d=None: cfg.get(k, d)))


def test_transcribe_posts_audio_file_field(monkeypatch, tmp_path):
    _cfg(monkeypatch, {"speech_stt_docker_url": "http://stt:5003"})
    calls = []

    def fake_post(endpoint, files=None, params=None, timeout=None, **kw):
        calls.append((endpoint, files, params))
        return _Resp(200, {"text": "hallo welt", "language": "de"})

    monkeypatch.setattr(sc.requests, "post", fake_post)
    f = tmp_path / "voice.ogg"
    f.write_bytes(b"OggS....")
    text, lang = sc.transcribe(f, mime="audio/ogg", filename="voice.ogg")
    assert (text, lang) == ("hallo welt", "de")
    endpoint, files, params = calls[0]
    assert endpoint == "http://stt:5003/asr"
    assert "audio_file" in files
    assert params == {"encode": "true", "output": "json"}


def test_transcribe_fallback_on_404(monkeypatch):
    _cfg(monkeypatch, {"speech_stt_docker_url": "http://stt:5003"})
    responses = [_Resp(404), _Resp(200, {"text": "ok", "language": "en"})]
    seen = []

    def fake_post(endpoint, **kw):
        seen.append(endpoint)
        return responses[len(seen) - 1]

    monkeypatch.setattr(sc.requests, "post", fake_post)
    text, lang = sc.transcribe(b"OggS....")
    assert (text, lang) == ("ok", "en")
    assert seen == ["http://stt:5003/asr", "http://stt:5003/transcribe"]


def test_transcribe_response_parsing_variants(monkeypatch):
    _cfg(monkeypatch, {})
    for payload in (
        {"text": "abc"},
        {"transcript": "abc"},
        {"results": [{"transcript": "abc"}]},
    ):
        monkeypatch.setattr(sc.requests, "post", lambda *a, _p=payload, **kw: _Resp(200, _p))
        text, _lang = sc.transcribe(b"RIFFdata")
        assert text == "abc", f"failed for payload {payload}"


def test_transcribe_never_raises(monkeypatch):
    _cfg(monkeypatch, {})

    def boom(*a, **kw):
        raise OSError("network down")

    monkeypatch.setattr(sc.requests, "post", boom)
    assert sc.transcribe(b"OggS....") == (None, None)


def test_synthesize_wav(monkeypatch):
    _cfg(monkeypatch, {"speech_tts_docker_url": "http://tts:5002"})
    calls = []

    def fake_post(endpoint, json=None, timeout=None, **kw):
        calls.append((endpoint, json))
        return _Resp(200, content=b"RIFF" + b"\x00" * 16)

    monkeypatch.setattr(sc.requests, "post", fake_post)
    out = sc.synthesize("Hallo", "de-DE")
    assert out and out[:4] == b"RIFF"
    endpoint, body = calls[0]
    assert endpoint == "http://tts:5002/synthesize"
    assert body["language"] == "de"  # normalized to 2 letters
    assert "format" not in body


def test_synthesize_ogg_passthrough_and_wav_fallback(monkeypatch):
    _cfg(monkeypatch, {"speech_tts_docker_url": "http://tts:5002"})
    # OggS passes through untouched
    monkeypatch.setattr(sc.requests, "post", lambda *a, **kw: _Resp(200, content=b"OggS" + b"\x00" * 8))
    out = sc.synthesize("hi", "en", want_format="ogg")
    assert out[:4] == b"OggS"
    # RIFF with no ffmpeg conversion available returns the RIFF bytes
    monkeypatch.setattr(sc.requests, "post", lambda *a, **kw: _Resp(200, content=b"RIFF" + b"\x00" * 8))
    monkeypatch.setattr(sc, "wav_to_ogg", lambda b: None)
    out = sc.synthesize("hi", "en", want_format="ogg")
    assert out[:4] == b"RIFF"


def test_synthesize_rejects_unknown_magic(monkeypatch):
    _cfg(monkeypatch, {})
    monkeypatch.setattr(sc.requests, "post", lambda *a, **kw: _Resp(200, content=b'{"err":1}'))
    assert sc.synthesize("hi", "en") is None


def test_synthesize_legacy_fallback_base64(monkeypatch):
    import base64

    _cfg(monkeypatch, {"speech_tts_docker_url": "http://tts:5002"})
    wav = b"RIFF" + b"\x00" * 8

    def fake_post(endpoint, json=None, timeout=None, **kw):
        if endpoint.endswith("/synthesize"):
            return _Resp(500)
        # legacy direct POST to the base URL answers JSON base64
        assert json == {"text": "hi", "lang": "en"}
        return _Resp(200, json_data={"audio_base64": base64.b64encode(wav).decode()},
                     content=b"not-wav")

    monkeypatch.setattr(sc.requests, "post", fake_post)
    assert sc.synthesize("hi", "en") == wav
