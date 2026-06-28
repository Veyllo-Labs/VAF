# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Tests for the first-run-gated Veyllo key-test endpoint used by onboarding.

The onboarding Veyllo step runs PRE-AUTH (before bootstrap), so the key is validated server-side via
this endpoint. It MUST: refuse once an admin exists (no open key-probe oracle), report {ok:true} only
on a 200 from the provider, and be in the rate-limited path set. Network-free (DB gate + httpx mocked)."""
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

import vaf.api.auth_routes as ar
from vaf.auth.rate_limit import _RATE_LIMITED_PATHS


# --- fakes for the async DB gate ---------------------------------------------------
class _FakeResult:
    def __init__(self, val): self._val = val
    def scalar_one_or_none(self): return self._val

class _FakeSession:
    def __init__(self, admin): self._admin = admin
    async def execute(self, *a, **k): return _FakeResult(self._admin)

class _FakeAuthDB:
    """Async context manager mirroring get_auth_db()."""
    def __init__(self, admin): self._admin = admin
    async def __aenter__(self): return _FakeSession(self._admin)
    async def __aexit__(self, *a): return False


# --- fakes for httpx ---------------------------------------------------------------
class _FakeResp:
    def __init__(self, code): self.status_code = code

class _FakeClient:
    def __init__(self, code): self._code = code
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def get(self, *a, **k): return _FakeResp(self._code)


def _client():
    app = FastAPI()
    app.include_router(ar.router)
    return TestClient(app)


def _patch(monkeypatch, *, admin, http_code=200):
    monkeypatch.setattr(ar, "get_auth_db", lambda: _FakeAuthDB(admin))
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient(http_code))


def test_route_registered_and_rate_limited():
    assert any(getattr(r, "path", "") == "/api/auth/test-veyllo-key" for r in ar.router.routes)
    assert "/api/auth/test-veyllo-key" in _RATE_LIMITED_PATHS


def test_refuses_once_admin_exists(monkeypatch):
    _patch(monkeypatch, admin=object(), http_code=200)  # an admin row exists
    r = _client().post("/api/auth/test-veyllo-key", json={"api_key": "vaf_live_x"})
    assert r.status_code == 403


def test_first_run_valid_key(monkeypatch):
    _patch(monkeypatch, admin=None, http_code=200)  # no admin yet, provider says 200
    r = _client().post("/api/auth/test-veyllo-key", json={"api_key": "vaf_live_x"})
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_first_run_invalid_key(monkeypatch):
    _patch(monkeypatch, admin=None, http_code=401)  # provider rejects the key
    r = _client().post("/api/auth/test-veyllo-key", json={"api_key": "bad"})
    assert r.status_code == 200 and r.json().get("ok") is False


def test_first_run_empty_key(monkeypatch):
    _patch(monkeypatch, admin=None, http_code=200)
    r = _client().post("/api/auth/test-veyllo-key", json={"api_key": "   "})
    assert r.status_code == 200 and r.json().get("ok") is False
