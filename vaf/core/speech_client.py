# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Shared speech HTTP client - the single place that talks to the STT/TTS services.

Before this module existed, the Whisper (/asr) and Piper (/synthesize) HTTP
contracts were re-implemented at five independent call sites (telegram_bridge,
whatsapp_bridge, send_whatsapp, web_server process_audio, speech.py), which is
exactly the registry-copy drift CLAUDE.md Rule 2 warns about. All speech
transcription/synthesis now goes through here; tests/test_speech_client_sync.py
guards against new direct call sites.

Contracts (all functions log instead of raising):
- synthesize(): returns audio bytes or None. want_format='ogg' prefers OggS and
  falls back to local ffmpeg WAV->OGG conversion; if conversion is unavailable
  the RIFF bytes are returned so callers can keep their own magic-byte
  fallbacks (e.g. Telegram's send-as-document path).
- transcribe(): returns (text, language) or (None, None); never deletes input
  files (file lifecycle stays with the caller).
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Optional, Tuple, Union

import requests

from vaf.core.config import Config

logger = logging.getLogger(__name__)

_STT_TIMEOUT = 60
_TTS_TIMEOUT = 60


def _stt_base_url() -> str:
    return (Config.get("speech_stt_docker_url") or "http://localhost:5003").strip().rstrip("/")


def _tts_base_url() -> str:
    return (Config.get("speech_tts_docker_url") or "http://localhost:5002").strip().rstrip("/")


# ---------------------------------------------------------------------------
# STT
# ---------------------------------------------------------------------------

def transcribe(
    audio: Union[str, Path, bytes],
    *,
    mime: str = "audio/ogg",
    filename: str = "voice.ogg",
) -> Tuple[Optional[str], Optional[str]]:
    """Transcribe an audio file or raw bytes via the Docker Whisper service.

    Returns (text, detected_language); (None, None) on any failure. The
    /asr endpoint is tried first, with a /transcribe fallback on 404 (older
    whisper-asr-webservice images).
    """
    try:
        if isinstance(audio, (str, Path)):
            payload = Path(audio).read_bytes()
        else:
            payload = audio
        if not payload:
            logger.warning("STT: empty audio input")
            return None, None

        # Cloud provider lane first (speech_stt_provider); returns (None, None)
        # when unconfigured or on any API error -> Docker lane below.
        from vaf.core import speech_api
        if speech_api.select_stt_backend()[0]:
            text, lang = speech_api.transcribe(payload, mime=mime, filename=filename)
            if text:
                return text, lang

        base_url = _stt_base_url()
        resp = _post_stt(f"{base_url}/asr", payload, mime, filename)
        if resp is not None and resp.status_code == 404:
            resp = _post_stt(f"{base_url}/transcribe", payload, mime, filename)
        if resp is None or not resp.ok:
            status = getattr(resp, "status_code", "n/a")
            body = (getattr(resp, "text", "") or "")[:200]
            logger.warning("STT request failed: %s - %s", status, body)
            return None, None

        try:
            data = resp.json()
        except Exception:
            data = {"text": (resp.text or "").strip()}

        text = (data.get("text") or data.get("transcript") or "").strip()
        if not text and isinstance(data.get("results"), list) and data["results"]:
            text = (data["results"][0].get("transcript") or "").strip()
        language = data.get("language") or None

        logger.info("STT transcribed: lang=%s, text=%s...", language, (text or "")[:50])
        return (text or None), language
    except Exception as e:
        logger.warning("STT transcription error: %s", e)
        return None, None


def _post_stt(endpoint: str, payload: bytes, mime: str, filename: str):
    try:
        return requests.post(
            endpoint,
            files={"audio_file": (filename, payload, mime)},
            params={"encode": "true", "output": "json"},
            timeout=_STT_TIMEOUT,
        )
    except Exception as e:
        logger.warning("STT POST %s failed: %s", endpoint, e)
        return None


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------

def synthesize(
    text: str,
    lang: str = "en",
    *,
    want_format: str = "wav",
    docker_url: Optional[str] = None,
) -> Optional[bytes]:
    """Synthesize text to audio bytes.

    want_format='wav' returns RIFF bytes; want_format='ogg' prefers OggS
    (container-side or local ffmpeg conversion) but may return RIFF bytes when
    no converter is available - callers keep their magic-byte handling.

    A configured cloud provider (speech_tts_provider) takes precedence; on any
    API error the Docker lane below is used automatically.
    """
    try:
        from vaf.core import speech_api
        if speech_api.select_tts_backend()[0]:
            audio = speech_api.synthesize(text, lang, want_format=want_format)
            if audio:
                return audio
    except Exception as e:
        logger.warning("TTS provider lane error: %s", e)
    return synthesize_docker(text, lang, want_format=want_format, base_url=docker_url)


def synthesize_docker(
    text: str,
    lang: str = "en",
    *,
    want_format: str = "wav",
    base_url: Optional[str] = None,
) -> Optional[bytes]:
    """Docker/HTTP TTS lane only (Piper container or chatterbox-style server).

    Used directly by SpeechManager.synthesize_audio, which resolves the engine
    URL itself (docker vs chatterbox).
    """
    try:
        url = (base_url or _tts_base_url()).strip().rstrip("/")
        if not url or not (text or "").strip():
            return None
        lang_short = (lang or "en")[:2].lower()

        body: dict = {"text": text, "language": lang_short}
        if want_format == "ogg":
            # The multi-lang container has ffmpeg and converts internally.
            body["format"] = "ogg"

        resp = None
        try:
            resp = requests.post(f"{url}/synthesize", json=body, timeout=_TTS_TIMEOUT)
        except Exception as e:
            logger.warning("TTS POST %s/synthesize failed: %s", url, e)

        audio: Optional[bytes] = None
        if resp is not None and resp.ok and resp.content:
            audio = resp.content
        else:
            # Legacy fallback: old-style direct POST to the base URL with
            # {"text","lang"}; may answer with raw WAV or JSON base64 audio.
            audio = _legacy_tts_fallback(url, text, lang_short)

        if not audio:
            status = getattr(resp, "status_code", "n/a")
            logger.warning("TTS returned no audio (status=%s)", status)
            return None

        magic = audio[:4]
        if magic not in (b"RIFF", b"OggS"):
            logger.warning("TTS returned unknown format (magic: %s)", magic.hex() if magic else "empty")
            return None

        if want_format == "ogg" and magic == b"RIFF":
            logger.info("TTS returned WAV, attempting local ffmpeg conversion")
            ogg = wav_to_ogg(audio)
            if ogg:
                return ogg
            # No converter available: hand back the WAV so callers can use
            # their own fallbacks (Telegram sends it as a document).
        return audio
    except Exception as e:
        logger.warning("TTS synthesis error: %s", e)
        return None


def _legacy_tts_fallback(url: str, text: str, lang_short: str) -> Optional[bytes]:
    try:
        resp = requests.post(url, json={"text": text, "lang": lang_short}, timeout=_TTS_TIMEOUT)
        if not resp.ok or not resp.content:
            return None
        if resp.content[:4] == b"RIFF":
            return resp.content
        try:
            out = resp.json()
            audio_b64 = out.get("audio_base64") or out.get("audio")
            if isinstance(audio_b64, str):
                import base64
                return base64.b64decode(audio_b64)
        except (ValueError, KeyError, TypeError):
            pass
        return None
    except Exception:
        return None


def wav_to_ogg(wav_bytes: bytes) -> Optional[bytes]:
    """Convert WAV to OGG/Opus using local ffmpeg (if available)."""
    import subprocess

    try:
        with tempfile.NamedTemporaryFile(prefix="vaf_", suffix=".wav", delete=False) as wav_file:
            wav_file.write(wav_bytes)
            wav_path = wav_file.name
        ogg_path = wav_path.replace(".wav", ".ogg")
        try:
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", wav_path, "-c:a", "libopus", "-b:a", "64k", ogg_path],
                capture_output=True,
                timeout=30,
            )
            if result.returncode != 0:
                logger.warning("Local ffmpeg conversion failed: %s", result.stderr.decode()[:200])
                return None
            with open(ogg_path, "rb") as f:
                return f.read()
        finally:
            for path in (wav_path, ogg_path):
                try:
                    if os.path.exists(path):
                        os.unlink(path)
                except Exception:
                    pass
    except FileNotFoundError:
        logger.warning("ffmpeg not found locally")
        return None
    except Exception as e:
        logger.warning("Local WAV to OGG conversion failed: %s", e)
        return None
