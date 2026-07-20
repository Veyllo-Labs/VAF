# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Voice catalog proxy (/api/voice/elevenlabs/*) contract tests.

Pins: admin-only access, no-key -> 400, key never appears in the response,
TTS-capability filtering for models, voice pagination, per-key caching, and
vendor-error mapping (401 -> 400, other -> 502). httpx mocked; no network.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

import vaf.api.voice_routes as vr


class _Resp:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


def _client():
    app = FastAPI()
    app.include_router(vr.router)
    return TestClient(app)


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient: the catalog proxy must never block the event loop,
    so it talks to the vendor through an async client context manager."""

    def __init__(self, calls, responses):
        self._calls = calls
        self._responses = responses

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None, params=None, **kw):
        self._calls.append((url, dict(params or {})))
        return self._responses[min(len(self._calls), len(self._responses)) - 1]


def _patch(monkeypatch, *, role="admin", api_key="el-key", responses=None):
    vr._cache.clear()
    vr._locks.clear()  # locks are per event loop; TestClient gives each request a fresh one
    monkeypatch.setattr(
        vr, "get_current_user_or_local_admin",
        lambda request: {"username": "u", "role": role, "user_scope_id": "s"},
    )
    monkeypatch.setattr(
        vr.Config, "get_api_key",
        classmethod(lambda cls, provider: api_key if provider == "elevenlabs" else ""),
    )
    calls = []
    if responses is not None:
        monkeypatch.setattr(
            vr.httpx, "AsyncClient",
            lambda *a, **kw: _FakeAsyncClient(calls, responses),
        )
    return calls


def test_non_admin_gets_403(monkeypatch):
    _patch(monkeypatch, role="user")
    assert _client().get("/api/voice/elevenlabs/models").status_code == 403
    assert _client().get("/api/voice/elevenlabs/voices").status_code == 403


def test_missing_key_gets_400(monkeypatch):
    _patch(monkeypatch, api_key="")
    r = _client().get("/api/voice/elevenlabs/models")
    assert r.status_code == 400
    assert "key" in r.json()["detail"].lower()


def test_models_filters_tts_capable(monkeypatch):
    _patch(monkeypatch, responses=[_Resp(200, [
        {"model_id": "eleven_flash_v2_5", "name": "Flash v2.5", "can_do_text_to_speech": True,
         "languages": [{"language_id": "de"}, {"language_id": "en"}],
         "maximum_text_length_per_request": 40000},
        {"model_id": "eleven_english_sts_v2", "name": "STS", "can_do_text_to_speech": False},
    ])])
    r = _client().get("/api/voice/elevenlabs/models")
    assert r.status_code == 200
    models = r.json()["models"]
    assert [m["model_id"] for m in models] == ["eleven_flash_v2_5"]
    assert models[0]["languages"] == 2
    assert "el-key" not in r.text


def test_voices_paginates_and_slims(monkeypatch):
    page1 = _Resp(200, {"voices": [{"voice_id": "v1", "name": "Rachel", "category": "premade",
                                    "preview_url": "https://x/1.mp3", "settings": {"stability": 1}}],
                        "has_more": True, "next_page_token": "tok"})
    page2 = _Resp(200, {"voices": [{"voice_id": "v2", "name": "Custom"}], "has_more": False})
    calls = _patch(monkeypatch, responses=[page1, page2])
    r = _client().get("/api/voice/elevenlabs/voices")
    assert r.status_code == 200
    voices = r.json()["voices"]
    assert [v["voice_id"] for v in voices] == ["v1", "v2"]
    assert "settings" not in voices[0]  # slimmed payload
    assert calls[1][1].get("next_page_token") == "tok"


def test_cache_serves_second_request(monkeypatch):
    calls = _patch(monkeypatch, responses=[_Resp(200, [])])
    c = _client()
    assert c.get("/api/voice/elevenlabs/models").status_code == 200
    assert c.get("/api/voice/elevenlabs/models").status_code == 200
    assert len(calls) == 1  # second answer came from the cache


def test_vendor_401_maps_to_400(monkeypatch):
    _patch(monkeypatch, responses=[_Resp(401, {"detail": {"code": "invalid_api_key"}})])
    r = _client().get("/api/voice/elevenlabs/voices")
    assert r.status_code == 400
    assert "rejected" in r.json()["detail"].lower()


def test_vendor_403_maps_to_400_with_scope_hint(monkeypatch):
    _patch(monkeypatch, responses=[_Resp(403, {"detail": {"code": "missing_permissions"}})])
    r = _client().get("/api/voice/elevenlabs/voices")
    assert r.status_code == 400
    assert "voices_read" in r.json()["detail"]


def test_vendor_5xx_maps_to_502(monkeypatch):
    _patch(monkeypatch, responses=[_Resp(500, {"detail": {"code": "internal"}})])
    assert _client().get("/api/voice/elevenlabs/models").status_code == 502


def test_endpoints_and_vendor_call_are_async():
    """Contract pin: a SYNCHRONOUS httpx call here blocked the whole uvicorn event loop
    (every HTTP request AND the /ws WebSocket, for every user) for as long as the vendor
    took. The route handlers and the vendor helper must stay coroutines."""
    import inspect
    assert inspect.iscoroutinefunction(vr.elevenlabs_models)
    assert inspect.iscoroutinefunction(vr.elevenlabs_voices)
    assert inspect.iscoroutinefunction(vr._elevenlabs_get)


def test_failure_is_remembered_so_a_dead_key_stops_hammering(monkeypatch):
    """A rejected key (an exhausted quota answers 401) must be negative-cached: the Settings
    tab refetches on every provider/key change, and without this each render re-hit the
    vendor. The error is still reported to the caller, just not re-fetched."""
    calls = _patch(monkeypatch, responses=[_Resp(401, {"detail": {"code": "quota_exceeded"}})])
    c = _client()
    first = c.get("/api/voice/elevenlabs/models")
    second = c.get("/api/voice/elevenlabs/models")
    assert first.status_code == 400 and second.status_code == 400
    assert "rejected" in second.json()["detail"].lower()   # same error, still surfaced
    assert len(calls) == 1                                  # second one never hit the vendor


def test_negative_cache_expires_sooner_than_a_successful_one():
    """A transient outage must not be remembered as long as a real catalog."""
    assert vr._NEG_CACHE_TTL < vr._CACHE_TTL


def test_models_and_voices_are_cached_independently(monkeypatch):
    """Distinct catalogs use distinct cache keys, so one failing must not poison the other."""
    calls = _patch(monkeypatch, responses=[_Resp(200, []), _Resp(200, {"voices": []})])
    c = _client()
    assert c.get("/api/voice/elevenlabs/models").status_code == 200
    assert c.get("/api/voice/elevenlabs/voices").status_code == 200
    assert len(calls) == 2  # both fetched; neither served the other's cache entry
