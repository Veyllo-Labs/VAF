# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Cloud speech provider lane (TTS/STT) - mirrors vaf/core/vision_infer.py.

Audio-only vendors (ElevenLabs) and the audio endpoints of chat vendors
(OpenAI) are a separate lane from the LLM provider catalog: they are NEVER
added to PROVIDER_MODELS or api_backend.py (see docs/llm/PROVIDER_MODES.md).

Backend selection at RUNTIME is explicit opt-in ONLY (deliberate deviation from
vision's capable-main-provider cascade): audio is metered separately from chat,
so a configured chat provider must never silently start paying for speech. Empty
``speech_tts_provider`` / ``speech_stt_provider`` means the local Docker lane.
One owner-decided exception, applied at CONFIG-WRITE time (not here): when a
Veyllo API key is first added and no STT provider was chosen, the config seeds
``speech_stt_provider = "veyllo"`` (``Config.apply_veyllo_stt_default``). Runtime
selection still just reads that explicit value; the always-local fallback below
still covers empty credits / offline.

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
# Groq); keep it audio-only vendors + audio endpoints. Veyllo is the one
# vendor that is ALSO a chat provider (in PROVIDER_MODELS), but its audio
# endpoint lives in THIS lane, exactly like OpenAI's - the chat catalog never
# routes audio. Veyllo offers STT (veyllo-transcribe) today; TTS is not live
# yet, so tts=False (a speech_tts_provider='veyllo' harmlessly falls to local).
AUDIO_PROVIDERS = {
    "elevenlabs": {"tts": True, "stt": True},
    "openai": {"tts": True, "stt": True},
    "veyllo": {"tts": False, "stt": True},
}

_TTS_TIMEOUT = 120.0  # long answers take time (observed ~45s for 4 min of audio);
# the web speak handler scales its budget to 130s when a provider is configured
_STT_TIMEOUT = 60.0  # matches every existing docker STT timeout

_DEFAULT_TTS_MODEL = {"elevenlabs": "eleven_flash_v2_5", "openai": "gpt-4o-mini-tts"}
_DEFAULT_TTS_VOICE = {"elevenlabs": "21m00Tcm4TlvDq8ikWAM", "openai": "alloy"}  # Rachel / alloy
_DEFAULT_STT_MODEL = {"elevenlabs": "scribe_v2", "openai": "whisper-1", "veyllo": "veyllo-transcribe"}

_ELEVENLABS_BASE = "https://api.elevenlabs.io"
_OPENAI_BASE = "https://api.openai.com"
_VEYLLO_DEFAULT_BASE = "https://api.veyllo.app/v1"  # veyllo_base_url already carries the /v1 suffix

# OpenAI whisper verbose_json returns the language as a full English name.
_LANGUAGE_NAME_TO_ISO2 = {
    "english": "en", "german": "de", "french": "fr", "spanish": "es",
    "italian": "it", "portuguese": "pt", "dutch": "nl", "polish": "pl",
    "russian": "ru", "turkish": "tr", "arabic": "ar", "chinese": "zh",
    "japanese": "ja", "korean": "ko", "hindi": "hi", "ukrainian": "uk",
    "czech": "cs", "swedish": "sv", "danish": "da", "norwegian": "no",
}

# Some STT providers (e.g. ElevenLabs Scribe) report the DETECTED language as an
# ISO-639-3 code (eng/spa/tur...). A blind [:2] truncation mangles many of them
# (spa->sp not es, tur->tu not tr, swe->sw = Swahili not Swedish), which is wrong
# even cosmetically and actively harmful once the value is fed back as an input
# hint. Map the common ones properly; unknown codes normalize to None (auto-detect).
_ISO639_3_TO_1 = {
    "eng": "en", "deu": "de", "ger": "de", "fra": "fr", "fre": "fr", "spa": "es",
    "por": "pt", "ita": "it", "nld": "nl", "dut": "nl", "pol": "pl", "rus": "ru",
    "tur": "tr", "ukr": "uk", "ces": "cs", "cze": "cs", "swe": "sv", "dan": "da",
    "nor": "no", "nob": "no", "fin": "fi", "ell": "el", "gre": "el", "hun": "hu",
    "ron": "ro", "rum": "ro", "bul": "bg", "hrv": "hr", "srp": "sr", "slk": "sk",
    "slo": "sk", "slv": "sl", "lit": "lt", "lav": "lv", "est": "et", "ara": "ar",
    "heb": "he", "hin": "hi", "ben": "bn", "tam": "ta", "tel": "te", "urd": "ur",
    "fas": "fa", "per": "fa", "tha": "th", "vie": "vi", "ind": "id", "msa": "ms",
    "may": "ms", "jpn": "ja", "kor": "ko", "zho": "zh", "chi": "zh", "cmn": "zh",
    "cat": "ca", "eus": "eu", "baq": "eu", "glg": "gl", "isl": "is", "ice": "is",
    "gle": "ga", "cym": "cy", "wel": "cy", "mlt": "mt", "afr": "af", "swa": "sw",
}


def _norm_iso_lang(raw) -> Optional[str]:
    """Normalize a provider-reported language to an ISO-639-1 code, or None.

    Handles a bare 2-letter code (trusted as 639-1), a locale form like `en-US`
    (keeps `en`), and an ISO-639-3 code (mapped via `_ISO639_3_TO_1`). An unknown
    or unparseable value returns None so it is never cached or sent as a hint - a
    wrong hint would be worse than the provider's own auto-detect."""
    code = str(raw or "").strip().lower()
    code = code.split("-", 1)[0].split("_", 1)[0]  # locale -> base language
    if len(code) == 2 and code.isalpha():
        return code
    if len(code) == 3:
        return _ISO639_3_TO_1.get(code)
    return None


def _norm_stt_hint(raw) -> Optional[str]:
    """Normalize a language HINT to send to a provider. `multi` passes through (Veyllo
    code-switching for mixed-language audio); anything else normalizes to an ISO-639-1
    base code, or None to auto-detect. Never blind-truncates - a raw ``[:2]`` would turn
    ``multi`` into ``mu`` and a locale like ``zh-TW`` into an unintended code."""
    if str(raw or "").strip().lower() == "multi":
        return "multi"
    return _norm_iso_lang(raw)


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
    language: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Cloud STT: audio bytes to (text, iso2_language). (None, None) on any problem.

    `language` is an OPTIONAL hint (e.g. from a previous turn's detected language, or
    `multi` for known code-switching audio): when set it is passed to the provider for
    a more precise/cheaper call; when None the provider auto-detects (Veyllo defaults
    to `multi`, robust across all its supported languages). Callers still get the
    ACTUALLY detected language back in the result, so a wrong hint self-reports."""
    try:
        provider, model = select_stt_backend()
        if not provider or not audio:
            return None, None
        lang_hint = _norm_stt_hint(language)
        if provider == "elevenlabs":
            result = _elevenlabs_stt(audio, mime, filename, model, lang_hint)
        elif provider == "veyllo":
            result = _veyllo_stt(audio, mime, filename, model, lang_hint)
        else:
            result = _openai_stt(audio, mime, filename, model, lang_hint)
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


def _elevenlabs_stt(audio: bytes, mime: str, filename: str, model: str,
                    language: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    import httpx

    data = {"model_id": model}
    if language and language != "multi":  # Scribe has no `multi`; omit -> auto-detect
        data["language_code"] = language  # ElevenLabs STT hint field
    resp = httpx.post(
        f"{_ELEVENLABS_BASE}/v1/speech-to-text",
        headers={"xi-api-key": _api_key("elevenlabs")},
        files={"file": (filename, audio, mime)},
        data=data,
        timeout=_STT_TIMEOUT,
    )
    if resp.status_code != 200:
        _warn_api("elevenlabs", "STT", resp)
        return None, None
    data = resp.json()
    text = (data.get("text") or "").strip()
    # Scribe reports ISO-639-3 (eng/spa/...): map it, never blind-truncate.
    lang = _norm_iso_lang(data.get("language_code"))
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


def _openai_stt(audio: bytes, mime: str, filename: str, model: str,
                language: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    import httpx

    # verbose_json (and with it the detected-language field) is whisper-1
    # only; the gpt-4o-*-transcribe models reject it and support plain json.
    response_format = "verbose_json" if model.startswith("whisper") else "json"
    data = {"model": model, "response_format": response_format}
    if language and language != "multi":  # OpenAI has no `multi`; omit -> auto-detect
        data["language"] = language  # ISO-639-1 hint (OpenAI transcriptions)
    resp = httpx.post(
        f"{_OPENAI_BASE}/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {_api_key('openai')}"},
        files={"file": (filename, audio, mime)},
        data=data,
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
# Veyllo audio endpoint (OpenAI-compatible; Bearer api_key_veyllo,
# veyllo_base_url already includes the /v1 suffix). STT only for now.
# ---------------------------------------------------------------------------

def _veyllo_stt(audio: bytes, mime: str, filename: str, model: str,
                language: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    import httpx
    from vaf.core.config import Config

    base = (Config.get("veyllo_base_url", "") or _VEYLLO_DEFAULT_BASE).rstrip("/")
    # verbose_json adds the detected language + duration; unlike OpenAI whisper
    # (which returns an English language NAME), Veyllo returns an ISO-639-1 code
    # directly. Omitting `language` lets the server auto-detect; a known hint (from
    # a previous turn) yields a more precise, cheaper call, and the response still
    # reports the ACTUALLY detected language so a stale hint is caught by the caller.
    # Veyllo/Deepgram supports `multi` (automatic code-switching across its languages);
    # default to it when no specific hint so detection is robust for ANY of the
    # supported languages, then a confidently detected language pins subsequent turns.
    data = {"model": model, "response_format": "verbose_json", "language": language or "multi"}
    resp = httpx.post(
        f"{base}/audio/transcriptions",
        headers={"Authorization": f"Bearer {_api_key('veyllo')}"},
        files={"file": (filename, audio, mime)},
        data=data,
        timeout=_STT_TIMEOUT,
    )
    if resp.status_code != 200:
        _warn_api("veyllo", "STT", resp)
        return None, None
    data = resp.json()
    text = (data.get("text") or "").strip()
    # Veyllo returns ISO-639-1 already; normalize defensively (locale / stray 639-3).
    lang = _norm_iso_lang(data.get("language"))
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
