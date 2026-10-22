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
                                     "role": "user", "user_scope_id": "s1",
                                     "is_2fa_verified": True})
    c = TestClient(_app(), client=LAN)
    r = c.get("/api/supervisor/status", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200


def test_non_access_token_is_401(monkeypatch):
    import vaf.auth.crypto as crypto
    monkeypatch.setattr(crypto, "decode_token", lambda tok: {"type": "refresh"})
    c = TestClient(_app(), client=LAN)
    r = c.get("/api/supervisor/status", headers={"Authorization": "Bearer x"})
    assert r.status_code == 401


def _pre_2fa(monkeypatch, *, verified):
    import vaf.auth.crypto as crypto
    monkeypatch.setattr(crypto, "decode_token",
                        lambda tok: {"type": "access", "sub": "u1", "username": "alice",
                                     "role": "admin", "user_scope_id": "s1",
                                     "is_2fa_verified": verified})


def test_a_token_from_before_the_second_factor_opens_nothing(monkeypatch):
    """/login answers a correct password with a full access token minted before the 2FA
    code (the temp_token). The check read a `requires_2fa_setup` claim no token carries,
    so that token opened every route: measured 200 on /api/users with a password alone.
    MUTATION: drop the second_factor_done check - red (200)."""
    _pre_2fa(monkeypatch, verified=False)
    c = TestClient(_app(), client=LAN)
    r = c.get("/api/supervisor/status", headers={"Authorization": "Bearer x"})
    assert r.status_code == 403 and "2FA" in r.json()["detail"]
    # The 2FA endpoints are exempt and take it (they check it themselves).
    assert c.get("/api/auth/login", headers={"Authorization": "Bearer x"}).status_code == 200


def test_the_verified_token_passes(monkeypatch):
    _pre_2fa(monkeypatch, verified=True)
    c = TestClient(_app(), client=LAN)
    assert c.get("/api/supervisor/status", headers={"Authorization": "Bearer x"}).status_code == 200


def test_two_factor_has_no_switch(monkeypatch, tmp_path):
    """A config.json an older install left with the 2FA switch off changes nothing: a token
    from before the second factor still opens no route for a network client, and the rule
    reads no configuration. MUTATION: restore the Config.get in second_factor_done - red."""
    import json

    from vaf.auth.crypto import second_factor_done
    from vaf.core.config import Config
    raw = tmp_path / "config.json"
    raw.write_text(json.dumps({"local_network_" + "require_2fa": False}), encoding="utf-8")
    monkeypatch.setattr(Config, "CONFIG_FILE", raw)
    assert Config.get("local_network_" + "require_2fa") is False   # the stale value is read
    _pre_2fa(monkeypatch, verified=False)
    c = TestClient(_app(), client=LAN)
    r = c.get("/api/supervisor/status", headers={"Authorization": "Bearer x"})
    assert r.status_code == 403 and r.json()["detail"] == "2FA verification required"
    assert second_factor_done({"is_2fa_verified": False}) is False
    assert second_factor_done({"is_2fa_verified": True}) is True


def test_the_2fa_switch_is_gone():
    """No reader, no default, no doctor finding: the key is not named in vaf/ or web/.
    MUTATION: put the key back into Config.DEFAULTS - red."""
    from pathlib import Path

    from vaf.core.config import Config
    key = "local_network_" + "require_2fa"
    assert key not in Config.DEFAULTS
    root = Path(__file__).resolve().parents[1]
    hits = []
    for top in ("vaf", "web/app", "web/components", "web/lib", "web/messages"):
        for path in (root / top).rglob("*"):
            if path.suffix in (".py", ".ts", ".tsx", ".json") and path.is_file():
                if key in path.read_bytes().decode("utf-8", "ignore"):
                    hits.append(path.relative_to(root).as_posix())
    assert hits == [], hits


def test_api_docs_need_a_sign_in(monkeypatch):
    """`vaf run --web` serves the backend itself on 0.0.0.0 in network mode, and the API
    documentation was exempt: every device on the network read the whole route table without
    an account. Now a tokenless network client gets 401, a signed-in one and the machine
    itself 200. MUTATION: put /openapi.json back into the exempt set - red."""
    from auth_harness import Harness

    from vaf.auth.middleware import AUTH_EXEMPT_PATHS
    assert "/docs" not in AUTH_EXEMPT_PATHS and "/openapi.json" not in AUTH_EXEMPT_PATHS
    h = Harness(monkeypatch)
    h.add_user("alice", "alice-password-123", no_second_factor=True)
    app = h.app()
    stranger = h.client("192.168.21.5", app=app)
    for path in ("/docs", "/openapi.json"):
        assert stranger.get(path).status_code == 401, path
    token = h.sign_in(h.client("192.168.21.6", app=app), "alice", "alice-password-123", None)
    device = h.client("192.168.21.7", app=app)
    for path in ("/docs", "/openapi.json"):
        assert device.get(path, headers={"Authorization": f"Bearer {token}"}).status_code == 200, path
        assert h.client(None, app=app).get(path).status_code == 200, path


def test_every_token_reader_asks_the_same_question():
    """The WebSocket's network lane and the A2A room door read tokens without this middleware.
    The localhost-only socket lane (local network off) is deliberately not asked: there a
    tokenless localhost request is the machine owner anyway, so a second factor guards nothing.
    MUTATION: inline the old check in either place - red."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    src = (root / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    handshake = src[src.index("async def websocket_endpoint"):]
    handshake = handshake[:handshake.index("elif type == ")]
    assert handshake.count("if not second_factor_done(payload):") == 1
    main = (root / "vaf" / "main.py").read_text(encoding="utf-8")
    verifier = main.split("def _a2a_credential_verifier")[1].split("\n\n\n")[0]
    assert "if not second_factor_done(payload):" in verifier


def test_the_middleware_asks_the_session(monkeypatch):
    """A token whose session ended (logout, a password change, a reset, a deactivation) is
    refused like an invalid one: 401 to a network client, the tokenless path for the machine
    itself. A store that cannot be asked keeps the token. A token from before the second factor
    keeps its 403: the second-factor gate is asked first (it has no session yet).
    MUTATION: drop the session half; reorder so a pre-2FA token gets 401 - red."""
    from vaf.auth import permissions, sessions
    now = {"session": sessions.ENDED}

    async def _account(_scope):
        return ("active", "admin")      # the token's own role: only the session decides here

    async def _session(_token):
        return now["session"]

    monkeypatch.setattr(permissions, "account_standing_async", _account)
    monkeypatch.setattr(sessions, "session_standing_async", _session)
    _pre_2fa(monkeypatch, verified=True)
    lan, local = TestClient(_app(), client=LAN), TestClient(_app(), client=LOCALHOST)
    bearer = {"Authorization": "Bearer x"}
    assert lan.get("/api/supervisor/status", headers=bearer).status_code == 401
    assert local.get("/api/supervisor/status", headers=bearer).status_code == 200
    now["session"] = sessions.UNKNOWN
    assert lan.get("/api/supervisor/status", headers=bearer).status_code == 200
    now["session"] = sessions.ENDED
    _pre_2fa(monkeypatch, verified=False)
    r = lan.get("/api/supervisor/status", headers=bearer)
    assert r.status_code == 403 and r.json()["detail"] == "2FA verification required"
