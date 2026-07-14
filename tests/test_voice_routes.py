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


def _patch(monkeypatch, *, role="admin", api_key="el-key", responses=None):
    vr._cache.clear()
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
        def fake_get(url, headers=None, params=None, timeout=None, **kw):
            calls.append((url, dict(params or {})))
            return responses[min(len(calls), len(responses)) - 1]
        monkeypatch.setattr(vr.httpx, "get", fake_get)
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
