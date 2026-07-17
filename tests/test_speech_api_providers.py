# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Cloud speech provider lane (vaf/core/speech_api.py) contract tests.

Pins: explicit opt-in selection (no capable-main-provider cascade), verified
request shapes for ElevenLabs and OpenAI, language handling for the
reply-in-same-language pairing, and the never-raise degradation contract
(402/429/timeouts return None so callers fall back to the local lane).
All HTTP is mocked; no network, no containers.
"""
import base64

import pytest

import vaf.core.speech_api as sa


@pytest.fixture(autouse=True)
def _mute_domain_log(monkeypatch):
    """Keep unit-test runs out of the user's real logs/backend_*.log."""
    monkeypatch.setattr(sa, "_domain_log", lambda msg: None)


class _Resp:
    def __init__(self, status_code=200, json_data=None, content=b"", text=""):
        self.status_code = status_code
        self._json = json_data
        self.content = content
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


def _cfg(monkeypatch, cfg, keys=None):
    from vaf.core.config import Config
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, k, d=None: cfg.get(k, d)))
    monkeypatch.setattr(
        Config, "get_api_key",
        classmethod(lambda cls, provider: (keys or {}).get(provider, "")),
    )


def _mock_httpx(monkeypatch, handler):
    import httpx
    monkeypatch.setattr(httpx, "post", handler)


WAV = b"RIFF" + b"\x00" * 16
OGG = b"OggS" + b"\x00" * 16


# ---------------------------------------------------------------------------
# Selection cascade
# ---------------------------------------------------------------------------

def test_no_provider_selects_none(monkeypatch):
    _cfg(monkeypatch, {})
    assert sa.select_tts_backend() == (None, None, None)
    assert sa.select_stt_backend() == (None, None)


def test_provider_without_key_selects_none(monkeypatch):
    _cfg(monkeypatch, {"speech_tts_provider": "elevenlabs", "speech_stt_provider": "openai"})
    assert sa.select_tts_backend() == (None, None, None)
    assert sa.select_stt_backend() == (None, None)


def test_unknown_provider_selects_none(monkeypatch):
    _cfg(monkeypatch, {"speech_tts_provider": "openrouter"}, keys={"openrouter": "sk-x"})
    assert sa.select_tts_backend() == (None, None, None)


def test_defaults_applied(monkeypatch):
    _cfg(monkeypatch, {"speech_tts_provider": "elevenlabs", "speech_stt_provider": "openai"},
         keys={"elevenlabs": "el-key", "openai": "sk-x"})
    assert sa.select_tts_backend() == ("elevenlabs", "eleven_flash_v2_5", "21m00Tcm4TlvDq8ikWAM")
    assert sa.select_stt_backend() == ("openai", "whisper-1")


def test_explicit_model_voice_win(monkeypatch):
    _cfg(monkeypatch, {
        "speech_tts_provider": "elevenlabs",
        "speech_tts_api_model": "eleven_multilingual_v2",
        "speech_tts_api_voice": "myvoice123",
    }, keys={"elevenlabs": "el-key"})
    assert sa.select_tts_backend() == ("elevenlabs", "eleven_multilingual_v2", "myvoice123")


# ---------------------------------------------------------------------------
# ElevenLabs shapes
# ---------------------------------------------------------------------------

def test_elevenlabs_tts_request_shape(monkeypatch):
    _cfg(monkeypatch, {"speech_tts_provider": "elevenlabs"}, keys={"elevenlabs": "el-key"})
    captured = {}

    def fake_post(url, params=None, headers=None, json=None, timeout=None, **kw):
        captured.update(url=url, params=params, headers=headers, json=json)
        return _Resp(200, content=WAV)

    _mock_httpx(monkeypatch, fake_post)
    out = sa.synthesize("Hallo Welt", "de-DE")
    assert out == WAV
    assert captured["url"] == "https://api.elevenlabs.io/v1/text-to-speech/21m00Tcm4TlvDq8ikWAM"
    assert captured["headers"]["xi-api-key"] == "el-key"
    assert captured["params"]["output_format"].startswith("wav_")
    assert captured["json"]["model_id"] == "eleven_flash_v2_5"
    # flash models get the language hint (2-letter, normalized from de-DE)
    assert captured["json"]["language_code"] == "de"


def test_elevenlabs_multilingual_gets_no_language_code(monkeypatch):
    _cfg(monkeypatch, {
        "speech_tts_provider": "elevenlabs",
        "speech_tts_api_model": "eleven_multilingual_v2",
    }, keys={"elevenlabs": "el-key"})
    captured = {}

    def fake_post(url, params=None, headers=None, json=None, timeout=None, **kw):
        captured.update(json=json)
        return _Resp(200, content=WAV)

    _mock_httpx(monkeypatch, fake_post)
    assert sa.synthesize("Hallo", "de") == WAV
    assert "language_code" not in captured["json"]


def test_elevenlabs_tts_ogg_requests_opus(monkeypatch):
    _cfg(monkeypatch, {"speech_tts_provider": "elevenlabs"}, keys={"elevenlabs": "el-key"})
    captured = {}

    def fake_post(url, params=None, headers=None, json=None, timeout=None, **kw):
        captured.update(params=params)
        return _Resp(200, content=OGG)

    _mock_httpx(monkeypatch, fake_post)
    out = sa.synthesize("hi", "en", want_format="ogg")
    assert out == OGG
    assert captured["params"]["output_format"].startswith("opus_")


def test_elevenlabs_stt_shape_and_parsing(monkeypatch):
    _cfg(monkeypatch, {"speech_stt_provider": "elevenlabs"}, keys={"elevenlabs": "el-key"})
    captured = {}

    def fake_post(url, headers=None, files=None, data=None, timeout=None, **kw):
        captured.update(url=url, files=files, data=data)
        return _Resp(200, {"text": "hallo welt", "language_code": "de", "language_probability": 0.98})

    _mock_httpx(monkeypatch, fake_post)
    text, lang = sa.transcribe(b"OggS....", mime="audio/ogg", filename="voice.ogg")
    assert (text, lang) == ("hallo welt", "de")
    assert captured["url"] == "https://api.elevenlabs.io/v1/speech-to-text"
    # ElevenLabs multipart field is "file" (the local Whisper container uses "audio_file")
    assert "file" in captured["files"] and "audio_file" not in captured["files"]
    assert captured["data"]["model_id"] == "scribe_v2"


# ---------------------------------------------------------------------------
# OpenAI shapes
# ---------------------------------------------------------------------------

def test_openai_tts_shape(monkeypatch):
    _cfg(monkeypatch, {"speech_tts_provider": "openai"}, keys={"openai": "sk-x"})
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None, **kw):
        captured.update(url=url, headers=headers, json=json)
        return _Resp(200, content=WAV)

    _mock_httpx(monkeypatch, fake_post)
    assert sa.synthesize("hello", "en") == WAV
    assert captured["url"] == "https://api.openai.com/v1/audio/speech"
    assert captured["headers"]["Authorization"] == "Bearer sk-x"
    assert captured["json"] == {
        "model": "gpt-4o-mini-tts", "voice": "alloy",
        "input": "hello", "response_format": "wav",
    }


def test_openai_stt_language_name_mapping(monkeypatch):
    _cfg(monkeypatch, {"speech_stt_provider": "openai"}, keys={"openai": "sk-x"})

    def fake_post(url, headers=None, files=None, data=None, timeout=None, **kw):
        assert data["response_format"] == "verbose_json"
        return _Resp(200, {"text": "guten tag", "language": "german"})

    _mock_httpx(monkeypatch, fake_post)
    text, lang = sa.transcribe(b"RIFFdata", mime="audio/wav", filename="mic.wav")
    assert (text, lang) == ("guten tag", "de")


def test_openai_stt_4o_models_use_plain_json(monkeypatch):
    """verbose_json is whisper-1 only; gpt-4o-* transcribe models reject it."""
    _cfg(monkeypatch, {"speech_stt_provider": "openai",
                       "speech_stt_api_model": "gpt-4o-mini-transcribe"}, keys={"openai": "sk-x"})
    captured = {}

    def fake_post(url, headers=None, files=None, data=None, timeout=None, **kw):
        captured.update(data=data)
        return _Resp(200, {"text": "hi"})

    _mock_httpx(monkeypatch, fake_post)
    text, lang = sa.transcribe(b"RIFFdata")
    assert text == "hi" and lang is None
    assert captured["data"]["response_format"] == "json"


def test_openai_tts_truncates_to_4096(monkeypatch):
    """The /v1/audio/speech input cap is 4096 chars endpoint-wide."""
    _cfg(monkeypatch, {"speech_tts_provider": "openai"}, keys={"openai": "sk-x"})
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None, **kw):
        captured.update(json=json)
        return _Resp(200, content=WAV)

    _mock_httpx(monkeypatch, fake_post)
    assert sa.synthesize("x" * 5000, "en") == WAV
    assert len(captured["json"]["input"]) == 4096


def test_openai_stt_missing_language(monkeypatch):
    _cfg(monkeypatch, {"speech_stt_provider": "openai"}, keys={"openai": "sk-x"})
    _mock_httpx(monkeypatch, lambda *a, **kw: _Resp(200, {"text": "hi"}))
    text, lang = sa.transcribe(b"RIFFdata")
    assert text == "hi" and lang is None


# ---------------------------------------------------------------------------
# Never-raise degradation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("resp", [
    _Resp(402, {"detail": {"code": "quota_exceeded"}}),
    _Resp(429, {"detail": {"code": "concurrent_limit_exceeded"}}),
    _Resp(500, text="internal"),
])
def test_api_errors_return_none(monkeypatch, resp):
    _cfg(monkeypatch, {"speech_tts_provider": "elevenlabs", "speech_stt_provider": "elevenlabs"},
         keys={"elevenlabs": "el-key"})
    _mock_httpx(monkeypatch, lambda *a, **kw: resp)
    assert sa.synthesize("hi", "en") is None
    assert sa.transcribe(b"OggS....") == (None, None)


def test_timeout_returns_none(monkeypatch):
    _cfg(monkeypatch, {"speech_tts_provider": "openai", "speech_stt_provider": "openai"},
         keys={"openai": "sk-x"})

    def boom(*a, **kw):
        raise TimeoutError("timed out")

    _mock_httpx(monkeypatch, boom)
    assert sa.synthesize("hi", "en") is None
    assert sa.transcribe(b"RIFFdata") == (None, None)


def test_200_with_json_body_is_not_audio(monkeypatch):
    _cfg(monkeypatch, {"speech_tts_provider": "elevenlabs"}, keys={"elevenlabs": "el-key"})
    _mock_httpx(monkeypatch, lambda *a, **kw: _Resp(200, content=b'{"detail": "oops"}'))
    assert sa.synthesize("hi", "en") is None


def test_ogg_request_with_wav_response_converts(monkeypatch):
    _cfg(monkeypatch, {"speech_tts_provider": "openai"}, keys={"openai": "sk-x"})
    _mock_httpx(monkeypatch, lambda *a, **kw: _Resp(200, content=WAV))
    import vaf.core.speech_client as sc
    monkeypatch.setattr(sc, "wav_to_ogg", lambda b: OGG)
    assert sa.synthesize("hi", "en", want_format="ogg") == OGG
    # and when no converter is available, the WAV is handed back
    monkeypatch.setattr(sc, "wav_to_ogg", lambda b: None)
    assert sa.synthesize("hi", "en", want_format="ogg") == WAV


def test_provider_precedence_in_speech_client(monkeypatch):
    """speech_client.synthesize/transcribe consult the provider lane first and
    never hit the Docker lane when the provider succeeds."""
    import vaf.core.speech_client as sc

    monkeypatch.setattr(sa, "select_tts_backend", lambda: ("elevenlabs", "m", "v"))
    monkeypatch.setattr(sa, "synthesize", lambda text, lang, want_format="wav": WAV)
    monkeypatch.setattr(sc, "synthesize_docker", lambda *a, **kw: pytest.fail("docker lane must not be hit"))
    assert sc.synthesize("hi", "en") == WAV

    monkeypatch.setattr(sa, "select_stt_backend", lambda: ("elevenlabs", "scribe_v2"))
    monkeypatch.setattr(sa, "transcribe", lambda payload, mime=None, filename=None, language=None: ("hi", "en"))
    monkeypatch.setattr(sc, "_post_stt", lambda *a, **kw: pytest.fail("docker lane must not be hit"))
    assert sc.transcribe(b"OggS....") == ("hi", "en")


def test_get_api_key_roundtrip_for_elevenlabs():
    """api_key_elevenlabs works through the generic Config helpers (base64)."""
    from vaf.core.config import Config
    encoded = base64.b64encode(b"el-key").decode()
    assert base64.b64decode(encoded).decode() == "el-key"
    assert "api_key_elevenlabs" in Config.DEFAULTS


# ---------------------------------------------------------------------------
# Veyllo STT lane (OpenAI-compatible; api_key_veyllo + veyllo_base_url)
# ---------------------------------------------------------------------------

def test_veyllo_stt_selection_default_model(monkeypatch):
    _cfg(monkeypatch, {"speech_stt_provider": "veyllo"}, keys={"veyllo": "vaf_live_x"})
    assert sa.select_stt_backend() == ("veyllo", "veyllo-transcribe")


def test_veyllo_not_tts_capable_falls_back_local(monkeypatch):
    # Veyllo has no hosted TTS yet (tts=False) -> local lane even with a key.
    _cfg(monkeypatch, {"speech_tts_provider": "veyllo"}, keys={"veyllo": "vaf_live_x"})
    assert sa.select_tts_backend() == (None, None, None)


def test_veyllo_stt_request_shape_and_iso_language(monkeypatch):
    _cfg(monkeypatch, {"speech_stt_provider": "veyllo",
                       "veyllo_base_url": "https://api.veyllo.app/v1"},
         keys={"veyllo": "vaf_live_x"})
    captured = {}

    def fake_post(url, headers=None, files=None, data=None, timeout=None, **kw):
        captured.update(url=url, headers=headers, files=files, data=data)
        return _Resp(200, {"text": "guten tag", "language": "de", "duration": 1.5})

    _mock_httpx(monkeypatch, fake_post)
    text, lang = sa.transcribe(b"RIFFdata", mime="audio/wav", filename="mic.wav")
    # Veyllo returns the ISO code directly (no English-name mapping needed).
    assert (text, lang) == ("guten tag", "de")
    assert captured["url"] == "https://api.veyllo.app/v1/audio/transcriptions"
    assert captured["headers"]["Authorization"] == "Bearer vaf_live_x"
    assert captured["data"]["model"] == "veyllo-transcribe"
    assert captured["data"]["response_format"] == "verbose_json"
    assert "file" in captured["files"] and "audio_file" not in captured["files"]


def test_veyllo_stt_default_base_url_when_unset(monkeypatch):
    _cfg(monkeypatch, {"speech_stt_provider": "veyllo"}, keys={"veyllo": "vaf_live_x"})
    captured = {}

    def fake_post(url, **kw):
        captured["url"] = url
        return _Resp(200, {"text": "hi", "language": "en"})

    _mock_httpx(monkeypatch, fake_post)
    sa.transcribe(b"RIFFdata")
    assert captured["url"] == "https://api.veyllo.app/v1/audio/transcriptions"


def test_veyllo_stt_long_language_code_trimmed(monkeypatch):
    _cfg(monkeypatch, {"speech_stt_provider": "veyllo"}, keys={"veyllo": "vaf_live_x"})
    _mock_httpx(monkeypatch, lambda *a, **kw: _Resp(200, {"text": "hi", "language": "en-US"}))
    assert sa.transcribe(b"RIFFdata") == ("hi", "en")


def test_veyllo_stt_quota_error_falls_back(monkeypatch):
    _cfg(monkeypatch, {"speech_stt_provider": "veyllo"}, keys={"veyllo": "vaf_live_x"})
    _mock_httpx(monkeypatch, lambda *a, **kw: _Resp(402, {"error": {"code": "insufficient_quota"}}))
    assert sa.transcribe(b"RIFFdata") == (None, None)


def test_veyllo_transcribe_filtered_from_chat_models():
    from vaf.core.provider_registry import is_veyllo_chat_model
    assert is_veyllo_chat_model("veyllo-chat") is True
    assert is_veyllo_chat_model("veyllo-transcribe") is False
    assert is_veyllo_chat_model("veyllo-tts") is False


# ---------------------------------------------------------------------------
# Language hint threading (per-speaker cache -> provider request)
# ---------------------------------------------------------------------------

def test_veyllo_stt_passes_language_hint(monkeypatch):
    _cfg(monkeypatch, {"speech_stt_provider": "veyllo"}, keys={"veyllo": "vaf_live_x"})
    captured = {}

    def fake_post(url, headers=None, files=None, data=None, timeout=None, **kw):
        captured.update(data=data)
        return _Resp(200, {"text": "hallo", "language": "de"})

    _mock_httpx(monkeypatch, fake_post)
    sa.transcribe(b"RIFFdata", language="de")
    assert captured["data"]["language"] == "de"


def test_veyllo_stt_defaults_to_multi_when_no_hint(monkeypatch):
    """No specific hint -> Veyllo auto-detects with `multi` (code-switching, robust
    across all supported languages), not a plain omit."""
    _cfg(monkeypatch, {"speech_stt_provider": "veyllo"}, keys={"veyllo": "vaf_live_x"})
    captured = {}

    def fake_post(url, headers=None, files=None, data=None, timeout=None, **kw):
        captured.update(data=data)
        return _Resp(200, {"text": "hi", "language": "en"})

    _mock_httpx(monkeypatch, fake_post)
    sa.transcribe(b"RIFFdata")  # no hint
    assert captured["data"]["language"] == "multi"


def test_veyllo_stt_explicit_multi_passes_through(monkeypatch):
    """An explicit `multi` hint is NOT truncated to `mu` and reaches Veyllo intact."""
    _cfg(monkeypatch, {"speech_stt_provider": "veyllo"}, keys={"veyllo": "vaf_live_x"})
    captured = {}

    def fake_post(url, headers=None, files=None, data=None, timeout=None, **kw):
        captured.update(data=data)
        return _Resp(200, {"text": "hallo hello", "language": "de"})

    _mock_httpx(monkeypatch, fake_post)
    sa.transcribe(b"RIFFdata", language="multi")
    assert captured["data"]["language"] == "multi"


def test_openai_elevenlabs_never_get_multi(monkeypatch):
    """OpenAI/ElevenLabs have no `multi`; it must be dropped (auto-detect), not sent."""
    for provider, key, field in (("openai", "sk-x", "language"), ("elevenlabs", "el-key", "language_code")):
        _cfg(monkeypatch, {"speech_stt_provider": provider}, keys={provider: key})
        captured = {}

        def fake_post(url, headers=None, files=None, data=None, timeout=None, **kw):
            captured.update(data=data)
            return _Resp(200, {"text": "hi", "language": "english", "language_code": "eng"})

        _mock_httpx(monkeypatch, fake_post)
        sa.transcribe(b"RIFFdata", language="multi")
        assert field not in captured["data"], f"{provider} must not receive multi"


def test_norm_stt_hint_allows_multi_and_codes():
    assert sa._norm_stt_hint("multi") == "multi"
    assert sa._norm_stt_hint("MULTI") == "multi"
    assert sa._norm_stt_hint("de") == "de"
    assert sa._norm_stt_hint("zh-TW") == "zh"   # locale base, not truncated garbage
    assert sa._norm_stt_hint("german") is None  # a name is not a valid hint


def test_openai_stt_passes_language_hint(monkeypatch):
    _cfg(monkeypatch, {"speech_stt_provider": "openai"}, keys={"openai": "sk-x"})
    captured = {}

    def fake_post(url, headers=None, files=None, data=None, timeout=None, **kw):
        captured.update(data=data)
        return _Resp(200, {"text": "guten tag", "language": "german"})

    _mock_httpx(monkeypatch, fake_post)
    sa.transcribe(b"RIFFdata", language="de")
    assert captured["data"]["language"] == "de"


def test_elevenlabs_stt_passes_language_code_hint(monkeypatch):
    _cfg(monkeypatch, {"speech_stt_provider": "elevenlabs"}, keys={"elevenlabs": "el-key"})
    captured = {}

    def fake_post(url, headers=None, files=None, data=None, timeout=None, **kw):
        captured.update(data=data)
        return _Resp(200, {"text": "hi", "language_code": "de"})

    _mock_httpx(monkeypatch, fake_post)
    sa.transcribe(b"OggS....", language="de")
    assert captured["data"]["language_code"] == "de"


# ---------------------------------------------------------------------------
# Language-code normalization (ISO-639-3 -> 639-1), so a cached hint is valid
# ---------------------------------------------------------------------------

def test_norm_iso_lang_maps_and_rejects():
    assert sa._norm_iso_lang("de") == "de"
    assert sa._norm_iso_lang("en-US") == "en"      # locale -> base
    assert sa._norm_iso_lang("spa") == "es"        # NOT "sp"
    assert sa._norm_iso_lang("swe") == "sv"        # NOT "sw" (Swahili)
    assert sa._norm_iso_lang("tur") == "tr"        # NOT "tu"
    assert sa._norm_iso_lang("zzz") is None        # unknown 639-3 -> None
    assert sa._norm_iso_lang("") is None
    assert sa._norm_iso_lang(None) is None


def test_elevenlabs_stt_maps_iso639_3_not_truncate(monkeypatch):
    """Scribe reports ISO-639-3; must be MAPPED (spa->es), never truncated (spa->sp),
    so the value is a valid hint when fed back next turn."""
    _cfg(monkeypatch, {"speech_stt_provider": "elevenlabs"}, keys={"elevenlabs": "el-key"})
    _mock_httpx(monkeypatch, lambda *a, **kw: _Resp(200, {"text": "hola", "language_code": "spa"}))
    assert sa.transcribe(b"OggS....") == ("hola", "es")


def test_elevenlabs_stt_unknown_iso639_3_is_none(monkeypatch):
    _cfg(monkeypatch, {"speech_stt_provider": "elevenlabs"}, keys={"elevenlabs": "el-key"})
    _mock_httpx(monkeypatch, lambda *a, **kw: _Resp(200, {"text": "hi", "language_code": "zzz"}))
    text, lang = sa.transcribe(b"OggS....")
    assert text == "hi" and lang is None   # unknown -> no bad hint
