"""
TTS (Text-to-Speech) API Routes for VAF.

Provides proxy endpoints to the TTS Docker container and manages TTS configuration.
Supports multi-language TTS with automatic language detection.
"""

import logging
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import httpx
from vaf.core.config import Config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tts", tags=["tts"])

# Default TTS Docker URL
DEFAULT_TTS_URL = "http://localhost:5002"


def get_tts_base_url() -> str:
    """Get the TTS Docker container base URL from config."""
    return Config.get("speech_tts_docker_url", DEFAULT_TTS_URL)


# ============================================================================
# Pydantic Models
# ============================================================================

class SynthesizeRequest(BaseModel):
    """Request model for speech synthesis."""
    text: str
    language: Optional[str] = None  # Auto-detect if not provided


class InstallLanguageRequest(BaseModel):
    """Request model for installing a language."""
    language: str


class UpdateConfigRequest(BaseModel):
    """Request model for updating TTS configuration."""
    language_priority: Optional[List[str]] = None
    auto_detect: Optional[bool] = None
    default_language: Optional[str] = None


class LanguageInfo(BaseModel):
    """Language information model."""
    code: str
    name: str
    voice: str
    quality: str
    installed: bool
    priority: int
    download_status: Optional[Dict[str, Any]] = None


# ============================================================================
# API Endpoints
# ============================================================================

@router.get("/health")
async def tts_health():
    """Check TTS Docker container health."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{get_tts_base_url()}/health")
            if response.status_code == 200:
                return response.json()
            return {"status": "unhealthy", "error": f"Status {response.status_code}"}
    except httpx.RequestError as e:
        return {"status": "unavailable", "error": str(e)}


@router.get("/languages")
async def list_languages():
    """List all available languages and their installation status."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{get_tts_base_url()}/languages")
            response.raise_for_status()
            return response.json()
    except httpx.RequestError as e:
        logger.error(f"Failed to fetch languages: {e}")
        raise HTTPException(status_code=503, detail=f"TTS service unavailable: {e}")


@router.post("/install")
async def install_language(request: InstallLanguageRequest):
    """Install a language model."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{get_tts_base_url()}/install",
                json={"language": request.language}
            )
            response.raise_for_status()
            return response.json()
    except httpx.RequestError as e:
        logger.error(f"Failed to install language: {e}")
        raise HTTPException(status_code=503, detail=f"TTS service unavailable: {e}")


@router.get("/install/status/{lang}")
async def install_status(lang: str):
    """Get installation status for a language."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{get_tts_base_url()}/install/status/{lang}")
            response.raise_for_status()
            return response.json()
    except httpx.RequestError as e:
        logger.error(f"Failed to get install status: {e}")
        raise HTTPException(status_code=503, detail=f"TTS service unavailable: {e}")


@router.post("/uninstall")
async def uninstall_language(request: InstallLanguageRequest):
    """Remove a language model."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{get_tts_base_url()}/uninstall",
                json={"language": request.language}
            )
            response.raise_for_status()
            return response.json()
    except httpx.RequestError as e:
        logger.error(f"Failed to uninstall language: {e}")
        raise HTTPException(status_code=503, detail=f"TTS service unavailable: {e}")


@router.get("/config")
async def get_tts_config():
    """Get TTS configuration."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{get_tts_base_url()}/config")
            response.raise_for_status()
            return response.json()
    except httpx.RequestError as e:
        logger.error(f"Failed to get config: {e}")
        raise HTTPException(status_code=503, detail=f"TTS service unavailable: {e}")


@router.post("/config")
async def update_tts_config(request: UpdateConfigRequest):
    """Update TTS configuration (language priority, auto-detect, etc.)."""
    try:
        data = request.model_dump(exclude_none=True)
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                f"{get_tts_base_url()}/config",
                json=data
            )
            response.raise_for_status()
            return response.json()
    except httpx.RequestError as e:
        logger.error(f"Failed to update config: {e}")
        raise HTTPException(status_code=503, detail=f"TTS service unavailable: {e}")


@router.post("/synthesize")
async def synthesize_speech(request: SynthesizeRequest):
    """
    Synthesize speech from text.

    Returns audio/wav stream.
    """
    if not request.text or not request.text.strip():
        raise HTTPException(status_code=400, detail="Text cannot be empty")

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{get_tts_base_url()}/synthesize",
                json={"text": request.text, "language": request.language}
            )

            if response.status_code != 200:
                error_detail = response.text
                try:
                    error_detail = response.json().get("error", response.text)
                except Exception:
                    pass
                raise HTTPException(status_code=response.status_code, detail=error_detail)

            # Return audio stream
            return Response(
                content=response.content,
                media_type="audio/wav",
                headers={
                    "Content-Disposition": "inline; filename=speech.wav",
                    "X-TTS-Language": response.headers.get("X-TTS-Language", "unknown")
                }
            )
    except httpx.RequestError as e:
        logger.error(f"TTS synthesis failed: {e}")
        raise HTTPException(status_code=503, detail=f"TTS service unavailable: {e}")


@router.get("/synthesize")
async def synthesize_speech_get(text: str, language: Optional[str] = None):
    """
    Synthesize speech from text (GET variant for simple use).

    Query params:
        text: Text to synthesize
        language: Optional language code (auto-detected if not provided)

    Returns audio/wav stream.
    """
    if not text or not text.strip():
        raise HTTPException(status_code=400, detail="Text cannot be empty")

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            params = {"text": text}
            if language:
                params["language"] = language

            response = await client.post(
                f"{get_tts_base_url()}/synthesize",
                json=params
            )

            if response.status_code != 200:
                error_detail = response.text
                try:
                    error_detail = response.json().get("error", response.text)
                except Exception:
                    pass
                raise HTTPException(status_code=response.status_code, detail=error_detail)

            return Response(
                content=response.content,
                media_type="audio/wav",
                headers={
                    "Content-Disposition": "inline; filename=speech.wav",
                    "X-TTS-Language": response.headers.get("X-TTS-Language", "unknown")
                }
            )
    except httpx.RequestError as e:
        logger.error(f"TTS synthesis failed: {e}")
        raise HTTPException(status_code=503, detail=f"TTS service unavailable: {e}")
