# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Voice provider catalog proxy (admin-only).

Serves the Settings UI with live model/voice catalogs for cloud speech
providers. The vendor API key never leaves the server: the browser calls
these endpoints and the backend talks to the vendor with the stored key.

Currently ElevenLabs only (GET /v1/models and GET /v2/voices are documented
list endpoints). OpenAI has no API that enumerates TTS voices or tags audio
models, so its lists stay hardcoded in the UI. Responses are cached briefly
per key so opening the Settings tab does not hammer the vendor.
"""

import asyncio
import hashlib
import logging
import time
from typing import Any, Dict, List, Tuple

import httpx
from fastapi import APIRouter, HTTPException, Request

from vaf.core.config import Config
from vaf.api.config_routes import get_current_user_or_local_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/voice", tags=["voice"])

_ELEVENLABS_BASE = "https://api.elevenlabs.io"
_TIMEOUT = 15.0
_CACHE_TTL = 300.0  # seconds (successful catalog)
# Failures are remembered too, briefly: a rejected key (e.g. an exhausted quota returns
# HTTP 401) otherwise re-hits the vendor on EVERY re-render of the Settings tab, since the
# UI refetches whenever the provider or key changes.
_NEG_CACHE_TTL = 60.0
_MAX_VOICE_PAGES = 3  # 3 x page_size=100; enough for any normal account


class _Failure:
    """A remembered error response (negative cache entry)."""

    __slots__ = ("status_code", "detail")

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail

    def raise_again(self) -> None:
        raise HTTPException(status_code=self.status_code, detail=self.detail)


# {cache_key: (timestamp, payload | _Failure)}
_cache: Dict[str, Tuple[float, Any]] = {}
# {cache_key: (event_loop, asyncio.Lock)} - de-dups CONCURRENT requests for the same catalog
# (a double-render or a second tab) so only the first one talks to the vendor.
# The loop is stored with the lock on purpose: an asyncio.Lock binds to the loop it is first
# awaited on and raises if reused from another one, which happens whenever the app runs on a
# fresh loop (tests, or any second server instance in-process). Rebuilding it per loop keeps
# the de-dup within a loop and stays correct across loops.
_locks: Dict[str, Tuple[Any, asyncio.Lock]] = {}


def _lock_for(kind: str, api_key: str) -> asyncio.Lock:
    key = _cache_key(kind, api_key)
    loop = asyncio.get_running_loop()
    entry = _locks.get(key)
    if entry is None or entry[0] is not loop:
        entry = _locks[key] = (loop, asyncio.Lock())
    return entry[1]


def _require_admin(request: Request) -> None:
    user = get_current_user_or_local_admin(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")


def _elevenlabs_key() -> str:
    key = Config.get_api_key("elevenlabs") or ""
    if not key:
        raise HTTPException(status_code=400, detail="No ElevenLabs API key configured")
    return key


def _cache_key(kind: str, api_key: str) -> str:
    # Key the cache by a hash so a changed key (different account) never
    # serves the previous account's catalog; the key itself is not stored.
    digest = hashlib.sha256(api_key.encode()).hexdigest()[:12]
    return f"{kind}:{digest}"


def _cached(kind: str, api_key: str):
    """The cached payload, a remembered _Failure, or None. Failures expire faster."""
    entry = _cache.get(_cache_key(kind, api_key))
    if not entry:
        return None
    stamped, payload = entry
    ttl = _NEG_CACHE_TTL if isinstance(payload, _Failure) else _CACHE_TTL
    if (time.time() - stamped) < ttl:
        return payload
    return None


def _store(kind: str, api_key: str, payload: Any) -> None:
    _cache[_cache_key(kind, api_key)] = (time.time(), payload)


def _serve_cached(kind: str, api_key: str):
    """Return a cached payload, re-raise a remembered failure, or None to go fetch."""
    hit = _cached(kind, api_key)
    if isinstance(hit, _Failure):
        hit.raise_again()
    return hit


async def _elevenlabs_get(path: str, api_key: str, params: Dict[str, Any] | None = None) -> Any:
    """One vendor GET. ASYNC on purpose: a synchronous httpx call here blocked the whole
    uvicorn event loop (every HTTP request AND the /ws WebSocket, for every user) for as
    long as ElevenLabs took to answer. Same AsyncClient pattern as vaf/api/tts_routes.py."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{_ELEVENLABS_BASE}{path}",
                headers={"xi-api-key": api_key},
                params=params or {},
            )
    except Exception as e:
        logger.warning("ElevenLabs catalog request failed: %s", e)
        raise HTTPException(status_code=503, detail="ElevenLabs not reachable") from e
    if resp.status_code == 401:
        raise HTTPException(status_code=400, detail="ElevenLabs API key rejected")
    if resp.status_code == 403:
        # Restricted keys need the voices_read / models_read permission scopes.
        raise HTTPException(
            status_code=400,
            detail="ElevenLabs API key lacks permission (needs voices_read / models_read scope)",
        )
    if resp.status_code != 200:
        detail = ""
        try:
            detail = str((resp.json().get("detail") or {}).get("code") or "")[:100]
        except Exception:
            pass
        raise HTTPException(status_code=502, detail=f"ElevenLabs error {resp.status_code} {detail}".strip())
    return resp.json()


@router.get("/elevenlabs/models")
async def elevenlabs_models(request: Request) -> Dict[str, Any]:
    """TTS-capable ElevenLabs models for the Settings picker."""
    _require_admin(request)
    api_key = _elevenlabs_key()
    cached = _serve_cached("models", api_key)
    if cached is not None:
        return cached

    async with _lock_for("models", api_key):
        # Re-check inside the lock: a concurrent request may have filled the cache while
        # this one waited, so only the first caller talks to the vendor.
        cached = _serve_cached("models", api_key)
        if cached is not None:
            return cached
        try:
            raw = await _elevenlabs_get("/v1/models", api_key)
        except HTTPException as e:
            _store("models", api_key, _Failure(e.status_code, str(e.detail)))
            raise
        models: List[Dict[str, Any]] = []
        for item in raw if isinstance(raw, list) else []:
            if not item.get("can_do_text_to_speech"):
                continue
            models.append({
                "model_id": item.get("model_id"),
                "name": item.get("name") or item.get("model_id"),
                "languages": len(item.get("languages") or []),
                "max_characters": item.get("maximum_text_length_per_request"),
            })
        payload = {"models": models}
        _store("models", api_key, payload)
        return payload


@router.get("/elevenlabs/voices")
async def elevenlabs_voices(request: Request) -> Dict[str, Any]:
    """The account's voice catalog (premade + cloned) for the Settings picker."""
    _require_admin(request)
    api_key = _elevenlabs_key()
    cached = _serve_cached("voices", api_key)
    if cached is not None:
        return cached

    async with _lock_for("voices", api_key):
        cached = _serve_cached("voices", api_key)
        if cached is not None:
            return cached
        voices: List[Dict[str, Any]] = []
        next_page_token = None
        try:
            for _ in range(_MAX_VOICE_PAGES):
                params: Dict[str, Any] = {"page_size": 100}
                if next_page_token:
                    params["next_page_token"] = next_page_token
                data = await _elevenlabs_get("/v2/voices", api_key, params)
                for v in data.get("voices") or []:
                    if not v.get("voice_id"):
                        continue
                    voices.append({
                        "voice_id": v.get("voice_id"),
                        "name": v.get("name") or v.get("voice_id"),
                        "category": v.get("category") or "",
                        "preview_url": v.get("preview_url") or "",
                    })
                next_page_token = data.get("next_page_token")
                if not data.get("has_more") or not next_page_token:
                    break
        except HTTPException as e:
            _store("voices", api_key, _Failure(e.status_code, str(e.detail)))
            raise

        payload = {"voices": voices}
        _store("voices", api_key, payload)
        return payload
