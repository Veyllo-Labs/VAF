# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The Veyllo App routes: the pairing window opens with the caller's own second factor, no
route pairs for another account, answers are not cached, and the direct WebSocket gives
nothing without the inner TLS, to this machine no more than to the LAN."""
import contextlib
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tests.app_link_client import paired_device
from tests.app_link_env import SCOPE_A, SCOPE_B, app_link_home  # noqa: F401  (fixture)
from vaf.api import app_link_routes
from vaf.core.app_link import bridge, devices, pairing


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    bridge.reset()
    pairing.reset()
    monkeypatch.setattr(pairing, "direct_addresses", lambda: ["wss://10.0.0.1:8443/api/app-link"])
    from vaf.auth import rate_limit
    monkeypatch.setattr(rate_limit, "_tracker", rate_limit._AttemptTracker())
    yield
    pairing.reset()
    bridge.reset()


ACCOUNTS = {
    SCOPE_A: SimpleNamespace(username="alice", user_scope_id=SCOPE_A, totp_secret="s", totp_nonce="n",
                             requires_2fa_setup=False, id="a"),
    SCOPE_B: SimpleNamespace(username="bob", user_scope_id=SCOPE_B, totp_secret="s", totp_nonce="n",
                             requires_2fa_setup=False, id="b"),
}


def _app(monkeypatch, *, as_scope=SCOPE_A, role="user", codes=None):
    """The router behind a stand-in for the auth middleware; the account behind the caller
    is ACCOUNTS[as_scope], and `codes` is the set of 2FA codes that are right."""
    from vaf.api import auth_routes
    from vaf.auth import second_factor

    @contextlib.asynccontextmanager
    async def _db():
        yield None

    import vaf.auth.database as database
    monkeypatch.setattr(database, "get_auth_db", _db)
    monkeypatch.setattr(auth_routes, "get_auth_db", _db)

    async def _account(db, request):
        return ACCOUNTS[request.state.user["user_scope_id"]]
    monkeypatch.setattr(auth_routes, "_caller_account", _account)
    right = set(codes or {"123456"})
    monkeypatch.setattr(second_factor, "check_second_factor",
                        lambda user, code, ip=None, where="": None if code in right else "wrong")

    class _Who(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.user = {"username": ACCOUNTS[as_scope].username, "role": role,
                                  "user_scope_id": as_scope}
            return await call_next(request)

    app = FastAPI()
    app.include_router(app_link_routes.router)
    from vaf.auth.rate_limit import RateLimitMiddleware
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(_Who)
    return TestClient(app)


def test_window_needs_own_2fa(app_link_home, monkeypatch):
    client = _app(monkeypatch)
    r = client.post("/api/app-link/pairings", json={"mode": "local"})
    assert r.status_code == 409 and r.json()["code"] == "window_closed"
    r = client.post("/api/app-link/pairing-window", json={"code": "000000"})
    assert r.status_code == 401 and r.json()["code"] == "wrong_code"
    assert client.post("/api/app-link/pairings", json={"mode": "relay"}).status_code == 409
    r = client.post("/api/app-link/pairing-window", json={"code": "123456"})
    assert r.status_code == 200 and r.json()["open_until"] > 0
    for mode in ("relay", "local"):
        r = client.post("/api/app-link/pairings", json={"mode": mode})
        assert r.status_code == 200 and r.json()["qr"].startswith("https://veyllo.app/link#")


def test_a_wrong_code_is_counted(app_link_home, monkeypatch):
    from vaf.auth.rate_limit import _RATE_LIMITED_PATHS
    assert "/api/app-link/pairing-window" in _RATE_LIMITED_PATHS
    client = _app(monkeypatch)
    codes = [client.post("/api/app-link/pairing-window", json={"code": "000000"}).status_code
             for _ in range(8)]
    assert 429 in codes


def test_no_store(app_link_home, monkeypatch):
    client = _app(monkeypatch)
    client.post("/api/app-link/pairing-window", json={"code": "123456"})
    for response in (client.post("/api/app-link/pairings", json={"mode": "local"}),
                     client.get("/api/app-link/pairings/current"),
                     client.post("/api/app-link/pairings/current/confirm", json={"accept": False}),
                     client.delete("/api/app-link/pairings/current"),
                     client.get("/api/app-link/status"),
                     client.get("/api/app-link/devices")):
        assert response.headers.get("cache-control") == "no-store", response.request.url


def test_no_foreign_account(app_link_home, monkeypatch):
    pairing.open_window(SCOPE_B, "bob")
    client = _app(monkeypatch, as_scope=SCOPE_A)
    r = client.post("/api/app-link/pairings", json={"mode": "local", "user_scope_id": SCOPE_B,
                                                    "username": "bob"})
    assert r.status_code == 409 and r.json()["code"] == "window_closed"
    client.post("/api/app-link/pairing-window", json={"code": "123456", "user_scope_id": SCOPE_B})
    r = client.post("/api/app-link/pairings", json={"mode": "local", "user_scope_id": SCOPE_B})
    assert r.status_code == 200
    assert pairing._issued[SCOPE_A].scope == SCOPE_A and SCOPE_B not in pairing._issued


def test_devices_own_and_all(app_link_home, monkeypatch):
    mine = paired_device(SCOPE_A)[0]
    theirs = paired_device(SCOPE_B, username="bob")[0]
    client = _app(monkeypatch, as_scope=SCOPE_A)
    listed = client.get("/api/app-link/devices").json()["devices"]
    assert [d["id"] for d in listed] == [mine["device_id"]]
    assert "spki_sha256" not in listed[0] and "cert_serial" not in listed[0]
    assert client.get("/api/app-link/devices?all=1").status_code == 403
    assert client.delete(f"/api/app-link/devices/{theirs['device_id']}").status_code == 404
    assert devices.get(theirs["device_id"]) is not None
    assert client.delete(f"/api/app-link/devices/{mine['device_id']}").json() == {"removed": True}
    admin = _app(monkeypatch, as_scope=SCOPE_A, role="admin")
    assert [d["username"] for d in admin.get("/api/app-link/devices?all=1").json()["devices"]] == ["bob"]
    assert admin.delete(f"/api/app-link/devices/{theirs['device_id']}").json() == {"removed": True}


def test_end_others(app_link_home, monkeypatch):
    import asyncio

    from vaf.api import auth_routes
    paired_device(SCOPE_A)
    paired_device(SCOPE_B, username="bob")
    _app(monkeypatch, as_scope=SCOPE_A)

    async def _ended(db, user_id, keep_token=None):
        return 2
    monkeypatch.setattr(auth_routes, "end_account_sessions", _ended)
    monkeypatch.setattr(auth_routes, "close_account_sockets", lambda scope: None)
    monkeypatch.setattr(auth_routes, "_presented_token", lambda request: None)
    request = SimpleNamespace(state=SimpleNamespace(user={"user_scope_id": SCOPE_A}))
    out = asyncio.run(auth_routes.end_other_sessions(request))
    assert out == {"ended": 2}
    assert devices.for_scope(SCOPE_A) == [] and len(devices.for_scope(SCOPE_B)) == 1


# ── the direct WebSocket ─────────────────────────────────────────────────────

PROXY_PEER = ("127.0.0.1", 40000)


@pytest.fixture
def network_on(monkeypatch):
    from vaf.core.config import Config
    real_get = Config.get
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: True if key == "local_network_enabled" else real_get(key, default)))


def _door():
    from starlette.applications import Starlette
    from starlette.routing import WebSocketRoute

    from vaf.auth import middleware as mw
    app = Starlette(routes=[WebSocketRoute("/api/app-link", app_link_routes.app_link_socket)])
    app.add_middleware(mw.NetworkModeLayers)
    return app


def _client(ip):
    c = TestClient(_door(), client=PROXY_PEER)
    if ip:
        c.headers["X-Forwarded-For"] = ip
    return c


def test_direct_ws_admission(app_link_home, network_on, monkeypatch):
    import vaf.core.security_events as se
    seen = []
    monkeypatch.setattr(se, "log_security_event", lambda kind, **f: seen.append(kind))
    from vaf.auth import middleware as mw
    monkeypatch.setattr(mw, "_emit_security_event", lambda kind, **f: seen.append(kind))
    with pytest.raises(WebSocketDisconnect) as refused:
        with _client("203.0.113.9").websocket_connect("/api/app-link"):
            pass
    assert refused.value.code == 4003 and "ip_blocked" in seen
    from vaf.network.https_proxy import _WS_ALLOWED
    assert _WS_ALLOWED.match("/api/app-link")
    assert not _WS_ALLOWED.match("/api/app-link/x") and not _WS_ALLOWED.match("/api/app-links")


def _answer(ip):
    """What the door says to a client that sends a plain message instead of TLS."""
    got = []
    with _client(ip).websocket_connect("/api/app-link") as ws:
        ws.send_text('{"t":"hello"}')
        ws.send_bytes(b"\x01\x00\x00\x00\x05hello")
        try:
            while True:
                message = ws.receive()
                if message.get("type") == "websocket.close":
                    break
                got.append(message.get("bytes") or message.get("text"))
        except WebSocketDisconnect:
            pass
    return got


def test_loopback_no_rights(app_link_home, network_on):
    lan = _answer("192.168.1.20")
    loopback = _answer(None)
    assert lan == loopback
    assert all(isinstance(b, (bytes, type(None))) for b in lan)
    assert not any(b and b"welcome" in b for b in lan)
    assert bridge._sessions == {}
