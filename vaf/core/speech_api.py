# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Cloud speech provider lane (TTS/STT) - mirrors vaf/core/vision_infer.py.

Audio-only vendors (ElevenLabs) and the audio endpoints of chat vendors
(OpenAI) are a separate lane from the LLM provider catalog: they are NEVER
added to PROVIDER_MODELS or api_backend.py (see docs/llm/PROVIDER_MODES.md).

Backend selection is explicit opt-in ONLY (deliberate deviation from vision's
capable-main-provider cascade): audio is metered separately from chat, so a
configured chat provider must never silently start paying for speech. Empty
``speech_tts_provider`` / ``speech_stt_provider`` means the local Docker lane.

Design: this runs in the hot path and must never break a turn. Every public
function catches everything and returns ``None`` so callers degrade to the
local engine - the local fallback IS the retry (no internal retry; an
ElevenLabs free-tier 429 will not clear within an interactive voice turn).
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

_log = logging.getLogger(__name__)

# Capability registry. Extend here for future vendors (Google, Deepgram,
# Groq, a Veyllo-hosted lane); keep it audio-only vendors + audio endpoints.
AUDIO_PROVIDERS = {
    "elevenlabs": {"tts": True, "stt": True},
    "openai": {"tts": True, "stt": True},
}

_TTS_TIMEOUT = 120.0  # long answers take time (observed ~45s for 4 min of audio);
# the web speak handler scales its budget to 130s when a provider is configured
_STT_TIMEOUT = 60.0  # matches every existing docker STT timeout

_DEFAULT_TTS_MODEL = {"elevenlabs": "eleven_flash_v2_5", "openai": "gpt-4o-mini-tts"}
_DEFAULT_TTS_VOICE = {"elevenlabs": "21m00Tcm4TlvDq8ikWAM", "openai": "alloy"}  # Rachel / alloy
_DEFAULT_STT_MODEL = {"elevenlabs": "scribe_v2", "openai": "whisper-1"}

_ELEVENLABS_BASE = "https://api.elevenlabs.io"
_OPENAI_BASE = "https://api.openai.com"

# OpenAI whisper verbose_json returns the language as a full English name.
_LANGUAGE_NAME_TO_ISO2 = {
    "english": "en", "german": "de", "french": "fr", "spanish": "es",
    "italian": "it", "portuguese": "pt", "dutch": "nl", "polish": "pl",
    "russian": "ru", "turkish": "tr", "arabic": "ar", "chinese": "zh",
    "japanese": "ja", "korean": "ko", "hindi": "hi", "ukrainian": "uk",
    "czech": "cs", "swedish": "sv", "danish": "da", "norwegian": "no",
}


def _api_key(provider: str) -> str:
    from vaf.core.config import Config
    return Config.get_api_key(provider) or ""


def select_tts_backend() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (provider, model, voice) for cloud TTS, or (None, None, None)."""
    try:
        from vaf.core.config import Config
        provider = (Config.get("speech_tts_provider", "") or "").strip().lower()
        if not provider:
            return None, None, None
        if provider not in AUDIO_PROVIDERS or not AUDIO_PROVIDERS[provider].get("tts"):
            _log.warning("speech_tts_provider '%s' is unknown/not TTS-capable; using local lane", provider)
            return None, None, None
        if not _api_key(provider):
            _log.warning("speech_tts_provider '%s' has no API key configured; using local lane", provider)
            return None, None, None
        model = (Config.get("speech_tts_api_model", "") or "").strip() or _DEFAULT_TTS_MODEL[provider]
        voice = (Config.get("speech_tts_api_voice", "") or "").strip() or _DEFAULT_TTS_VOICE[provider]
        return provider, model, voice
    except Exception:
        return None, None, None


def select_stt_backend() -> Tuple[Optional[str], Optional[str]]:
    """Return (provider, model) for cloud STT, or (None, None)."""
    try:
        from vaf.core.config import Config
        provider = (Config.get("speech_stt_provider", "") or "").strip().lower()
        if not provider:
            return None, None
        if provider not in AUDIO_PROVIDERS or not AUDIO_PROVIDERS[provider].get("stt"):
            _log.warning("speech_stt_provider '%s' is unknown/not STT-capable; using local lane", provider)
            return None, None
        if not _api_key(provider):
            _log.warning("speech_stt_provider '%s' has no API key configured; using local lane", provider)
            return None, None
        model = (Config.get("speech_stt_api_model", "") or "").strip() or _DEFAULT_STT_MODEL[provider]
        return provider, model
    except Exception:
        return None, None


def synthesize(text: str, lang: str = "en", *, want_format: str = "wav") -> Optional[bytes]:
    """Cloud TTS: text to audio bytes (RIFF, or OggS when want_format='ogg').

    Returns None when no provider is configured or on ANY API problem, so the
    caller falls through to the local engine.
    """
    try:
        provider, model, voice = select_tts_backend()
        if not provider or not (text or "").strip():
            return None
        lang_short = (lang or "")[:2].lower()
        if provider == "elevenlabs":
            audio = _elevenlabs_tts(text, lang_short, model, voice, want_format)
        else:
            audio = _openai_tts(text, model, voice)
        if not audio:
            return None
        # Magic-byte sanity check: a 200 with a JSON error body must never
        # be treated as audio.
        if audio[:4] not in (b"RIFF", b"OggS"):
            _log.warning("[SPEECH_API] %s TTS returned non-audio payload (magic=%s)",
                         provider, audio[:4].hex() if audio[:4] else "empty")
            return None
        if want_format == "ogg" and audio[:4] == b"RIFF":
            from vaf.core import speech_client
            ogg = speech_client.wav_to_ogg(audio)
            if ogg:
                return ogg
            # Hand back the WAV; callers keep their magic-byte fallbacks.
        _domain_log(f"{provider} TTS ok ({len(audio)} bytes, model={model})")
        return audio
    except Exception as e:
        _log.warning("[SPEECH_API] TTS failed: %s", e)
        return None


def transcribe(
    audio: bytes,
    *,
    mime: str = "audio/ogg",
    filename: str = "voice.ogg",
) -> Tuple[Optional[str], Optional[str]]:
    """Cloud STT: audio bytes to (text, iso2_language). (None, None) on any problem."""
    try:
        provider, model = select_stt_backend()
        if not provider or not audio:
            return None, None
        if provider == "elevenlabs":
            result = _elevenlabs_stt(audio, mime, filename, model)
        else:
            result = _openai_stt(audio, mime, filename, model)
        if result and result[0]:
            _domain_log(f"{provider} STT ok (lang={result[1]}, model={model})")
            return result
        return None, None
    except Exception as e:
        _log.warning("[SPEECH_API] STT failed: %s", e)
        return None, None


# ---------------------------------------------------------------------------
# ElevenLabs (https://api.elevenlabs.io, header xi-api-key)
# ---------------------------------------------------------------------------

def _elevenlabs_tts(text: str, lang_short: str, model: str, voice: str, want_format: str) -> Optional[bytes]:
    import httpx

    output_format = "opus_48000_64" if want_format == "ogg" else "wav_24000"
    body = {"text": text, "model_id": model}
    # language_code is only supported by the flash models; multilingual_v2
    # auto-detects from the text and would reject/ignore it.
    if "flash" in model and len(lang_short) == 2:
        body["language_code"] = lang_short
    resp = httpx.post(
        f"{_ELEVENLABS_BASE}/v1/text-to-speech/{voice}",
        params={"output_format": output_format},
        headers={"xi-api-key": _api_key("elevenlabs")},
        json=body,
        timeout=_TTS_TIMEOUT,
    )
    if resp.status_code != 200:
        _warn_api("elevenlabs", "TTS", resp)
        return None
    return resp.content


def _elevenlabs_stt(audio: bytes, mime: str, filename: str, model: str) -> Tuple[Optional[str], Optional[str]]:
    import httpx

    resp = httpx.post(
        f"{_ELEVENLABS_BASE}/v1/speech-to-text",
        headers={"xi-api-key": _api_key("elevenlabs")},
        files={"file": (filename, audio, mime)},
        data={"model_id": model},
        timeout=_STT_TIMEOUT,
    )
    if resp.status_code != 200:
        _warn_api("elevenlabs", "STT", resp)
        return None, None
    data = resp.json()
    text = (data.get("text") or "").strip()
    lang = (data.get("language_code") or "").strip().lower() or None
    if lang and len(lang) > 2:
        lang = lang[:2]
    return (text or None), lang


# ---------------------------------------------------------------------------
# OpenAI audio endpoints (Bearer api_key_openai)
# ---------------------------------------------------------------------------

def _openai_tts(text: str, model: str, voice: str) -> Optional[bytes]:
    import httpx

    # Hard endpoint-wide API limit: input maxLength 4096 characters. Reading
    # the first 4096 chars beats failing the whole request (which would fall
    # back to the local engine with a different voice mid-conversation).
    if len(text) > 4096:
        _log.info("[SPEECH_API] openai TTS input truncated %d -> 4096 chars", len(text))
        text = text[:4096]
    resp = httpx.post(
        f"{_OPENAI_BASE}/v1/audio/speech",
        headers={"Authorization": f"Bearer {_api_key('openai')}"},
        json={"model": model, "voice": voice, "input": text, "response_format": "wav"},
        timeout=_TTS_TIMEOUT,
    )
    if resp.status_code != 200:
        _warn_api("openai", "TTS", resp)
        return None
    return resp.content


def _openai_stt(audio: bytes, mime: str, filename: str, model: str) -> Tuple[Optional[str], Optional[str]]:
    import httpx

    # verbose_json (and with it the detected-language field) is whisper-1
    # only; the gpt-4o-*-transcribe models reject it and support plain json.
    response_format = "verbose_json" if model.startswith("whisper") else "json"
    resp = httpx.post(
        f"{_OPENAI_BASE}/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {_api_key('openai')}"},
        files={"file": (filename, audio, mime)},
        data={"model": model, "response_format": response_format},
        timeout=_STT_TIMEOUT,
    )
    if resp.status_code != 200:
        _warn_api("openai", "STT", resp)
        return None, None
    data = resp.json()
    text = (data.get("text") or "").strip()
    # verbose_json returns e.g. "german"; the json-only 4o models return no
    # language - then None (voice replies default to 'en').
    lang_name = (data.get("language") or "").strip().lower()
    lang = _LANGUAGE_NAME_TO_ISO2.get(lang_name) or (lang_name if len(lang_name) == 2 else None)
    return (text or None), lang


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _warn_api(provider: str, kind: str, resp) -> None:
    detail = ""
    try:
        payload = resp.json()
        detail = str((payload.get("detail") or {}).get("code") or payload.get("error") or "")[:200]
    except Exception:
        detail = (getattr(resp, "text", "") or "")[:200]
    _log.warning("[SPEECH_API] %s %s failed: HTTP %s %s", provider, kind, resp.status_code, detail)
    _domain_log(f"{provider} {kind} failed: HTTP {resp.status_code} {detail}")


def _domain_log(msg: str) -> None:
    try:
        from vaf.core.log_helper import append_domain_log
        append_domain_log("backend", f"[SPEECH_API] {msg}")
    except Exception:
        pass
