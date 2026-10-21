# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The backend's network checks follow the Local Network setting on every request.

The measurement this was written on: `vaf.core.web_server` imported with Local Network off,
then the setting switched on in the same process, which is what the tray's restart after the
settings toggle serves (it starts a new uvicorn on the SAME app object). The sign-in check,
the address check and the rate limit had been added at import time only, so the middleware
list stayed without them, and a client relayed by the integrated HTTPS proxy
(`127.0.0.1` with `X-Forwarded-For: 203.0.113.9`) got `GET /api/config` with every key,
the admin-only remote-access route and the account list, all 200, as the local admin.

The other direction is pinned as well: with Local Network off the backend serves this machine
only. A per-request read that only looked at the setting would hand every relayed device the
local admin the moment the setting goes off, while the HTTPS proxy is still up (the tray
restarts the frontend before it stops the proxy) or for the whole run of `vaf run --web`,
whose backend stays bound on 0.0.0.0.
"""
import re
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from vaf.auth import middleware as mw
from vaf.core.config import Config

PROXY_PEER = ("127.0.0.1", 40000)     # what the backend sees for everything the proxy relays
PUBLIC = "203.0.113.9"
LAN = "192.168.1.20"


@pytest.fixture
def network(monkeypatch):
    """A switch for Local Network, read at call time like the real setting."""
    state = {"on": False}
    real_get = Config.get

    def fake_get(key, default=None):
        if key == "local_network_enabled":
            return state["on"]
        return real_get(key, default)

    monkeypatch.setattr(Config, "get", staticmethod(fake_get))
    return state


@pytest.fixture
def events(monkeypatch):
    seen = []
    import vaf.core.security_events as se
    monkeypatch.setattr(se, "log_security_event", lambda kind, **f: seen.append((kind, f)))
    monkeypatch.setattr(mw, "_emit_security_event", lambda kind, **f: seen.append((kind, f)))
    return seen


def _small_app():
    async def ok(request):
        return JSONResponse({"user": getattr(request.state, "user", None)})

    async def socket(websocket):
        await websocket.accept()
        await websocket.send_text("hello")
        await websocket.close()

    app = Starlette(routes=[Route("/api/users", ok), WebSocketRoute("/ws/a2a/r1", socket)])
    app.add_middleware(mw.NetworkModeLayers)
    return app


def _relayed(app, ip):
    c = TestClient(app, client=PROXY_PEER)
    if ip:
        c.headers["X-Forwarded-For"] = ip
    return c


# --------------------------------------------------------------------------- the layer itself

def test_the_network_layers_follow_the_setting_at_request_time(network):
    """THE regression in small: the same app object, the setting flipped between requests."""
    app = _small_app()
    desktop = _relayed(app, None)
    lan = _relayed(app, LAN)

    assert desktop.get("/api/users").status_code == 200
    network["on"] = True
    assert lan.get("/api/users").status_code == 401           # sign-in required now
    assert _relayed(app, PUBLIC).get("/api/users").status_code == 403
    assert desktop.get("/api/users").status_code == 200       # the desktop stays tokenless


def test_with_local_network_off_only_this_machine_is_served(network, events):
    """Off means this machine only: a relayed device is refused, never the local admin."""
    app = _small_app()
    assert _relayed(app, None).get("/api/users").status_code == 200
    assert _relayed(app, "127.0.0.1").get("/api/users").status_code == 200
    for ip in (LAN, PUBLIC, "unknown"):
        r = _relayed(app, ip).get("/api/users")
        assert r.status_code == 403, ip
    assert [k for k, _ in events] == ["ip_blocked"] * 3
    assert events[0][1]["ip"] == LAN


def test_switching_off_never_hands_a_device_the_local_admin(network):
    """The proxy outlives the switch by the frontend's restart, and `vaf run --web` keeps
    0.0.0.0 for its whole run: the device that needed a sign-in a moment ago is refused, not
    promoted."""
    app = _small_app()
    lan = _relayed(app, LAN)
    network["on"] = True
    assert lan.get("/api/users").status_code == 401
    network["on"] = False
    r = lan.get("/api/users")
    assert r.status_code == 403
    assert r.json().get("user") is None


def test_a_socket_from_another_device_is_refused_while_local_network_is_off(network, events):
    app = _small_app()
    with pytest.raises(WebSocketDisconnect) as refused:
        with _relayed(app, LAN).websocket_connect("/ws/a2a/r1"):
            pass
    assert refused.value.code == 4003
    assert events and events[0][0] == "ip_blocked"
    with _relayed(app, None).websocket_connect("/ws/a2a/r1") as ws:
        assert ws.receive_text() == "hello"


def test_the_network_chain_is_admission_then_auth_then_rate_limit():
    """Admission outermost, built once: the rate limiter's windows are module state, and a
    chain rebuilt per request would cost an allocation for nothing. With Local Network off,
    the door the HTTPS proxy uses then."""
    from vaf.auth.rate_limit import RateLimitMiddleware
    from vaf.network.admission import LOCAL_NETWORK_OFF, AdmissionGuard
    from vaf.network.binding import is_localhost

    async def inner(scope, receive, send):  # pragma: no cover - never called here
        pass

    layers = mw.NetworkModeLayers(inner)
    chain = layers.network
    assert isinstance(chain, AdmissionGuard) and chain._admits is None
    assert isinstance(chain.app, mw.AuthMiddleware)
    assert isinstance(chain.app.app, RateLimitMiddleware)
    assert chain.app.app.app is inner
    assert isinstance(layers.this_machine, AdmissionGuard)
    assert layers.this_machine._admits is is_localhost and layers.this_machine.app is inner
    assert layers.this_machine._reason == LOCAL_NETWORK_OFF


def test_a_client_outside_the_admitted_networks_meets_no_sign_in_check(network, events,
                                                                      monkeypatch):
    """With the sign-in check outermost a tokenless public client got 401 and was recorded as
    `unauthenticated_blocked`, which the doctor's count of public refusals never reads, and a
    public client that sent a token had it decoded and looked up in the account and session
    stores before the address check refused it. MUTATION: Auth outside the admission check -
    red."""
    import vaf.auth.crypto as crypto
    decoded = []
    monkeypatch.setattr(crypto, "decode_token", lambda token: decoded.append(token) or None)
    network["on"] = True
    app = _small_app()
    r = _relayed(app, PUBLIC).get("/api/users")
    assert r.status_code == 403
    assert [k for k, _ in events] == ["ip_blocked"]
    c = _relayed(app, PUBLIC)
    c.headers["Authorization"] = "Bearer abc.def.ghi"
    assert c.get("/api/users").status_code == 403
    assert decoded == []


# --------------------------------------------------------------------------- the real app

@pytest.fixture(scope="module")
def real_app():
    from vaf.core.web_server import app
    return app


def test_the_real_app_registers_the_layers_in_every_mode(real_app):
    """Registered at module level, whatever the setting was at import. Starlette lists the
    outermost layer first."""
    order = [m.cls for m in real_app.user_middleware]
    assert mw.NetworkModeLayers in order
    assert (order.index(mw.ForeignOriginGuard) < order.index(mw.NetworkModeLayers)
            < order.index(mw.OwnOriginCORSMiddleware))
    source = (Path(__file__).resolve().parents[1] / "vaf" / "core" / "web_server.py").read_text(
        encoding="utf-8")
    for layer in ("AuthMiddleware", "RateLimitMiddleware", "IPValidationMiddleware"):
        assert not re.search(r"add_middleware\(\s*" + layer, source), layer


def test_the_real_app_refuses_after_a_runtime_enable(real_app, network):
    """The measured request, against the real app that was imported with the setting off."""
    desktop = _relayed(real_app, None)
    assert desktop.get("/api/config").status_code == 200
    network["on"] = True
    public = _relayed(real_app, PUBLIC).get("/api/config")
    assert public.status_code == 403
    assert "local_network_enabled" not in public.text
    assert _relayed(real_app, LAN).get("/api/config").status_code == 401
    assert _relayed(real_app, LAN).get("/api/network/remote-access").status_code == 401
    assert desktop.get("/api/config").status_code == 200


def test_the_real_app_serves_only_this_machine_while_local_network_is_off(real_app, network,
                                                                        monkeypatch):
    for ip in (LAN, PUBLIC):
        assert _relayed(real_app, ip).get("/api/config").status_code == 403, ip

    import vaf.core.a2a.wire as wire
    opened = []
    monkeypatch.setattr(wire, "open_room", lambda room_id: opened.append(room_id))
    with pytest.raises(WebSocketDisconnect) as refused:
        with _relayed(real_app, LAN).websocket_connect("/ws/a2a/r1"):
            pass
    assert refused.value.code == 4003
    assert opened == []


# --------------------------------------------------------------------------- every socket route

VNC = "/api/browser-vnc/t/abcdefghijklmnop/websockify"


def test_the_backend_judges_every_socket(real_app, network, events, monkeypatch):
    """The room socket and the interactive browser's stream had no address check at all, and
    `/ws` had its own. In network mode the admission check refuses a client outside the
    admitted networks at the handshake of every socket route, before any handler runs."""
    network["on"] = True
    import vaf.core.a2a.wire as wire
    import vaf.core.browser_interactive as browser_interactive
    import vaf.core.web_server as web_server

    reached = []
    monkeypatch.setattr(web_server.manager, "connect", lambda ws: reached.append("connect"))
    monkeypatch.setattr(wire, "open_room", lambda room_id: reached.append("room"))
    monkeypatch.setattr(browser_interactive, "get_manager_by_ticket",
                        lambda ticket: reached.append("vnc"))
    for path in ("/ws", "/ws/a2a/r1", VNC):
        with pytest.raises(WebSocketDisconnect) as refused:
            with _relayed(real_app, PUBLIC).websocket_connect(path):
                pass
        assert refused.value.code == 4003, path
    assert reached == []
    assert [(k, f["detail"]) for k, f in events] == [
        ("ip_blocked", "/ws"), ("ip_blocked", "/ws/a2a/r1"), ("ip_blocked", VNC)]

    # An admitted device still meets the handler's own sign-in check.
    with pytest.raises(WebSocketDisconnect) as refused:
        with _relayed(real_app, LAN).websocket_connect("/ws"):
            pass
    assert refused.value.code == 4001
