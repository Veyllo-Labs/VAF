# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Gap A regression: the HTTP AuthMiddleware must not let an `Upgrade: websocket` header (or any
non-localhost request) bypass JWT auth. Real WebSocket handshakes are websocket-scope and never reach
this HTTP middleware, so an `Upgrade: websocket` header on an HTTP request is only ever a bypass attempt.

Hermetic: a tiny Starlette app + AuthMiddleware only (no need to import the full web_server graph; its
middleware is attached at import time only when local_network_enabled)."""
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from vaf.auth.middleware import AuthMiddleware

LAN = ("192.168.1.50", 40000)      # RFC1918, is_localhost() False -> must auth
LOCALHOST = ("127.0.0.1", 40000)   # trusted -> bypass


async def _ok(request):
    return JSONResponse({"ok": True})


def _app():
    app = Starlette(routes=[
        Route("/api/supervisor/status", _ok),
        Route("/api/auth/login", _ok, methods=["GET", "POST"]),  # an exempt path
    ])
    app.add_middleware(AuthMiddleware)
    return app


def test_upgrade_header_does_not_bypass_auth():
    """THE regression: a LAN client adding `Upgrade: websocket` to a normal HTTP request must still be
    challenged for a token. Pre-fix this returned 200 (auth skipped)."""
    c = TestClient(_app(), client=LAN)
    r = c.get("/api/supervisor/status", headers={"Upgrade": "websocket"})
    assert r.status_code == 401


def test_lan_without_token_is_401():
    c = TestClient(_app(), client=LAN)
    assert c.get("/api/supervisor/status").status_code == 401


def test_localhost_bypasses_auth():
    """Localhost trust (desktop/CLI) is preserved — no token required."""
    c = TestClient(_app(), client=LOCALHOST)
    assert c.get("/api/supervisor/status").status_code == 200


def test_localhost_with_upgrade_header_still_bypasses():
    c = TestClient(_app(), client=LOCALHOST)
    assert c.get("/api/supervisor/status", headers={"Upgrade": "websocket"}).status_code == 200


def test_exempt_path_passes_without_token():
    c = TestClient(_app(), client=LAN)
    assert c.get("/api/auth/login").status_code == 200


def test_valid_access_token_allows(monkeypatch):
    import vaf.auth.crypto as crypto
    monkeypatch.setattr(crypto, "decode_token",
                        lambda tok: {"type": "access", "sub": "u1", "username": "alice",
                                     "role": "user", "user_scope_id": "s1"})
    c = TestClient(_app(), client=LAN)
    r = c.get("/api/supervisor/status", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200


def test_non_access_token_is_401(monkeypatch):
    import vaf.auth.crypto as crypto
    monkeypatch.setattr(crypto, "decode_token", lambda tok: {"type": "refresh"})
    c = TestClient(_app(), client=LAN)
    r = c.get("/api/supervisor/status", headers={"Authorization": "Bearer x"})
    assert r.status_code == 401
