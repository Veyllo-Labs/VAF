# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Regression: a web page that is not VAF's own must not act as the owner.

The measurement this was written on: ``GET /api/secrets`` with ``Origin:
http://192.168.1.50:3000`` from this machine answered 200 with the names of the stored
credentials, plus ``access-control-allow-origin`` for that origin and ``allow-credentials``.
Any page served from a localhost or RFC 1918 address, open in a browser on the VAF machine,
could read the owner's data and send requests, because

  * CORS admitted every localhost and RFC 1918 origin with credentials, and
  * a tokenless request from this machine IS the owner (in single-user mode no auth
    middleware runs at all), and the ``vaf_token`` cookie rides to every localhost port.

The same trust leaked three more ways, pinned below too: simple cross-site requests from any
page (CSRF, also through the Next.js ``/api`` door, which forwarded no browser headers), a DNS
name rebound to 127.0.0.1 (a same-origin read, invisible to CORS), and the ``/ws`` handshake
opened by a page on another localhost port with the cookie attached.

What must keep working, pinned as well: the Web UI (its WebSocket and ``/api/version`` are
cross-origin), everything same-origin, LAN browsers through the HTTPS proxy, the documented
nginx setup, OAuth callbacks (a cross-site top-level navigation), and every client that sends
no browser marks at all (CLI, sub-agent IPC, the tray, scripts).
"""
import asyncio
import re
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from vaf.auth import middleware as mw
from vaf.network import binding

REPO = Path(__file__).resolve().parents[1]
MEASURED_ORIGIN = "http://192.168.1.50:3000"
UI = "http://localhost:3000"


@pytest.fixture(autouse=True)
def _frontend_on_3000(monkeypatch):
    monkeypatch.setattr(binding, "frontend_port", lambda: 3000)


def _reason(headers: dict, *, scheme: str = "http", method: str = "GET"):
    return binding.foreign_request_reason(Headers(headers=headers), scheme=scheme, method=method)


# --------------------------------------------------------------------------- the decision

REFUSED = [
    ("the measured origin", {"host": "127.0.0.1:8001", "origin": MEASURED_ORIGIN}, "origin"),
    ("another localhost port (same SITE, so the cookie rides along)",
     {"host": "localhost:8005", "origin": "http://localhost:5173"}, "origin"),
    ("a public page", {"host": "127.0.0.1:8001", "origin": "https://evil.example"}, "origin"),
    ("an opaque origin (sandboxed frame, file://)", {"host": "127.0.0.1:8001", "origin": "null"}, "origin"),
    ("the UI port over https is not the UI", {"host": "127.0.0.1:8001", "origin": "https://localhost:3000"}, "origin"),
    ("an origin carrying a path", {"host": "127.0.0.1:8001", "origin": UI + "/x"}, "origin"),
    ("an origin carrying credentials", {"host": "127.0.0.1:8001", "origin": "http://u@localhost:3000"}, "origin"),
    ("DNS rebinding straight at the backend",
     {"host": "attacker.example:8001", "sec-fetch-site": "same-origin"}, "host"),
    ("rebinding with forwarding headers the page added itself",
     {"host": "attacker.example:8001", "origin": "http://attacker.example:8001",
      "x-forwarded-proto": "https", "x-forwarded-host": "localhost:8001"}, "host"),
    ("rebinding through the Next.js /api door",
     {"host": "127.0.0.1:8005", "x-forwarded-host": "attacker.example:3000",
      "sec-fetch-site": "same-origin"}, "host"),
    ("a foreign LAN page through the HTTPS proxy",
     {"host": "127.0.0.1:8005", "x-forwarded-proto": "https",
      "x-forwarded-host": "192.168.1.50:8443", "origin": "https://192.168.1.77:8443"}, "origin"),
    ("an image from another localhost port",
     {"host": "localhost:8001", "sec-fetch-site": "same-site", "sec-fetch-mode": "no-cors",
      "sec-fetch-dest": "image"}, "site"),
    ("a cross-site script", {"host": "127.0.0.1:8001", "sec-fetch-site": "cross-site",
                             "sec-fetch-mode": "no-cors", "sec-fetch-dest": "script"}, "site"),
    ("a cross-site frame navigation", {"host": "127.0.0.1:8001", "sec-fetch-site": "cross-site",
                                       "sec-fetch-mode": "navigate", "sec-fetch-dest": "iframe"}, "site"),
]


@pytest.mark.parametrize("why,headers,expected", REFUSED, ids=[r[0] for r in REFUSED])
def test_a_foreign_page_is_refused(why, headers, expected):
    assert _reason(headers) == expected


def test_a_cross_site_form_post_is_refused_even_as_a_navigation():
    """A navigation passes only as GET: a form POST from someone else's page is the CSRF."""
    nav = {"host": "127.0.0.1:8001", "sec-fetch-site": "cross-site",
           "sec-fetch-mode": "navigate", "sec-fetch-dest": "document"}
    assert _reason(nav, method="POST") == "site"


ALLOWED = [
    ("the Web UI's socket and /api/version", {"host": "localhost:8005", "origin": UI}, "http"),
    ("the UI on 127.0.0.1", {"host": "127.0.0.1:8005", "origin": "http://127.0.0.1:3000"}, "ws"),
    ("the UI on ::1", {"host": "[::1]:8005", "origin": "http://[::1]:3000"}, "http"),
    ("same origin, e.g. /docs", {"host": "127.0.0.1:8001", "origin": "http://127.0.0.1:8001"}, "http"),
    ("same origin with TLS on the backend port",
     {"host": "127.0.0.1:8001", "origin": "https://127.0.0.1:8001"}, "https"),
    ("same origin over IPv6", {"host": "[::1]:8001", "origin": "http://[::1]:8001"}, "http"),
    ("a LAN browser through the HTTPS proxy",
     {"host": "127.0.0.1:8005", "x-forwarded-proto": "https",
      "x-forwarded-host": "192.168.1.50:8443", "origin": "https://192.168.1.50:8443"}, "http"),
    ("the proxy on 443 (no port on either side)",
     {"host": "127.0.0.1:8005", "x-forwarded-proto": "https",
      "x-forwarded-host": "192.168.1.50", "origin": "https://192.168.1.50"}, "http"),
    ("a name through the TLS proxy (cannot be rebound: the certificate would not match)",
     {"host": "127.0.0.1:8005", "x-forwarded-proto": "https",
      "x-forwarded-host": "mypc.local:8443", "origin": "https://mypc.local:8443"}, "http"),
    ("the documented nginx setup (browser IP as Host)",
     {"host": "192.168.2.114", "x-forwarded-proto": "https", "origin": "https://192.168.2.114"}, "http"),
    ("the UI through the Next.js /api door",
     {"host": "127.0.0.1:8005", "x-forwarded-host": "localhost:3000", "origin": UI,
      "sec-fetch-site": "same-origin"}, "http"),
    ("a same-origin GET (no Origin header)",
     {"host": "127.0.0.1:8005", "x-forwarded-host": "localhost:3000", "sec-fetch-site": "same-origin"}, "http"),
    ("a typed or bookmarked address", {"host": "localhost:8001", "sec-fetch-site": "none"}, "http"),
    ("an OAuth callback (cross-site top-level navigation)",
     {"host": "localhost:8001", "sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate",
      "sec-fetch-dest": "document"}, "http"),
    ("no browser marks: CLI, sub-agent IPC, tray, a script", {"host": "127.0.0.1:8005"}, "http"),
    ("no browser marks under any Host (the test client's)", {"host": "testserver"}, "http"),
]


@pytest.mark.parametrize("why,headers,scheme", ALLOWED, ids=[a[0] for a in ALLOWED])
def test_own_pages_and_non_browser_clients_pass(why, headers, scheme):
    assert _reason(headers, scheme=scheme) is None


def test_the_ui_origin_follows_the_port_the_frontend_really_runs_on(monkeypatch):
    """When 3000 was taken the UI runs on 3001, and whatever answers on 3000 is not VAF."""
    monkeypatch.setattr(binding, "frontend_port", lambda: 3001)
    assert _reason({"host": "localhost:8005", "origin": "http://localhost:3001"}) is None
    assert _reason({"host": "localhost:8005", "origin": UI}) == "origin"


def test_the_cors_allow_list_is_the_web_ui_alone():
    cors = mw.OwnOriginCORSMiddleware(app=None)
    for origin in (UI, "http://127.0.0.1:3000", "http://[::1]:3000"):
        assert cors.is_allowed_origin(origin) is True, origin
    for origin in (MEASURED_ORIGIN, "http://localhost:5173", "https://evil.example", "null",
                   "http://10.0.0.5:3000", "https://localhost:3000"):
        assert cors.is_allowed_origin(origin) is False, origin


# --------------------------------------------------------------------------- the door itself

@pytest.fixture
def events(monkeypatch):
    seen = []
    monkeypatch.setattr(mw, "_emit_security_event", lambda kind, **f: seen.append((kind, f)))
    return seen


def _guarded_app():
    async def ok(request):
        return JSONResponse({"ok": True})

    async def socket(websocket):
        await websocket.accept()
        await websocket.send_text("hello")
        await websocket.close()

    app = Starlette(routes=[Route("/api/secrets", ok, methods=["GET", "POST"]),
                            WebSocketRoute("/ws", socket)])
    app.add_middleware(mw.ForeignOriginGuard)
    return app


def test_the_door_refuses_http_and_records_it(events):
    client = TestClient(_guarded_app(), base_url="http://127.0.0.1:8001")
    r = client.post("/api/secrets", headers={"Origin": MEASURED_ORIGIN}, content="x")
    assert r.status_code == 403
    assert events and events[0][0] == "foreign_origin_blocked"
    assert events[0][1]["path"] == "/api/secrets"
    assert MEASURED_ORIGIN in events[0][1]["detail"]


def test_the_door_lets_own_and_scripted_requests_through(events):
    client = TestClient(_guarded_app(), base_url="http://127.0.0.1:8001")
    assert client.get("/api/secrets", headers={"Origin": UI}).status_code == 200
    assert client.get("/api/secrets").status_code == 200
    assert events == []


# The test client sends Host "testserver" on a socket unless the URL is absolute, and a Host
# that is a DNS name is refused for its own reason; the URLs below name the host the browser
# dials, so the verdict is about the ORIGIN, which the event detail confirms.
WS_URL = "ws://localhost:8005/ws"


def test_the_door_closes_a_foreign_websocket_before_accept(events):
    client = TestClient(_guarded_app())
    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect(WS_URL, headers={"Origin": "http://localhost:5173"}):
            pass
    assert refused.value.code == 4003
    assert events and events[0][0] == "foreign_origin_blocked"
    assert events[0][1]["detail"].startswith("origin:")
    with client.websocket_connect(WS_URL, headers={"Origin": UI}) as ws:
        assert ws.receive_text() == "hello"


# --------------------------------------------------------------------------- the real app

@pytest.fixture(scope="module")
def real_app():
    from vaf.core.web_server import app
    return app


def test_the_guard_is_registered_in_every_mode_and_outside_cors(real_app):
    """Registered at module level, not inside the local_network_enabled branch: single-user
    mode (no AuthMiddleware at all) is where the measured leak was widest. Starlette's
    user_middleware lists the outermost layer first."""
    order = [m.cls for m in real_app.user_middleware]
    assert mw.ForeignOriginGuard in order
    assert mw.OwnOriginCORSMiddleware in order
    assert order.index(mw.ForeignOriginGuard) < order.index(mw.OwnOriginCORSMiddleware)
    from starlette.middleware.cors import CORSMiddleware
    assert all(not (c is CORSMiddleware) for c in order), "the regex CORS layer is back"


def test_the_measured_request_is_refused_by_the_real_app(real_app, events):
    client = TestClient(real_app, base_url="http://127.0.0.1:8001", client=("127.0.0.1", 50000))
    r = client.get("/api/version", headers={"Origin": MEASURED_ORIGIN})
    assert r.status_code == 403
    assert "access-control-allow-origin" not in r.headers
    pre = client.options("/api/version", headers={"Origin": MEASURED_ORIGIN,
                                                  "Access-Control-Request-Method": "POST"})
    assert pre.status_code == 403


def test_the_real_app_still_serves_the_ui_and_scripts(real_app, events):
    client = TestClient(real_app, base_url="http://127.0.0.1:8001", client=("127.0.0.1", 50000))
    r = client.get("/api/version", headers={"Origin": UI})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == UI
    assert client.get("/api/version").status_code == 200
    pre = client.options("/api/version", headers={"Origin": UI, "Access-Control-Request-Method": "POST"})
    assert pre.status_code == 200 and pre.headers.get("access-control-allow-origin") == UI


def test_the_real_ws_handshake_is_judged_before_any_token(real_app, events):
    client = TestClient(real_app, client=("127.0.0.1", 50000))
    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect(WS_URL, headers={"Origin": "http://localhost:5173"}):
            pass
    assert refused.value.code == 4003
    assert events and events[0][1]["detail"].startswith("origin:")


# --------------------------------------------------------------------------- the two relays

def test_the_https_proxy_relays_what_the_guard_judges(monkeypatch):
    """The relay used to forward only the cookie, so a relayed socket looked like a script and
    passed unjudged. It now carries Origin, the dialled host and the fetch metadata, and the
    backend's verdict on what arrives is the verdict on the page."""
    import websockets as _ws
    from vaf.network import https_proxy as proxy

    captured = {}

    class _BackendWS:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def send(self, _):
            pass

    def _fake_connect(uri, **kwargs):
        captured.update(kwargs)
        return _BackendWS()

    def _client(origin):
        class _ClientWS:
            url = type("U", (), {"query": ""})()
            scope = {"path": "/ws", "headers": [(b"origin", origin.encode()),
                                                (b"host", b"192.168.1.50:8443"),
                                                (b"sec-fetch-site", b"same-origin")]}
            client = type("C", (), {"host": "192.168.1.50"})()

            async def accept(self, subprotocol=None):
                pass

            async def receive(self):
                raise RuntimeError("client gone")

            async def close(self, **_):
                pass

        return _ClientWS()

    monkeypatch.setattr(_ws, "connect", _fake_connect)

    def backend_sees(origin):
        captured.clear()
        asyncio.run(proxy._forward_websocket(_client(origin)))
        seen = {k.lower(): v for k, v in captured["additional_headers"]}
        seen["host"] = "127.0.0.1:8005"
        if captured.get("origin"):
            seen["origin"] = captured["origin"]
        return seen

    own = backend_sees("https://192.168.1.50:8443")
    assert own["origin"] == "https://192.168.1.50:8443"
    assert own["x-forwarded-host"] == "192.168.1.50:8443"
    assert own["x-forwarded-proto"] == "https"
    assert own["sec-fetch-site"] == "same-origin"
    assert _reason(own, scheme="ws") is None
    assert _reason(backend_sees("https://192.168.1.77:8443"), scheme="ws") == "origin"


def test_the_next_api_door_forwards_what_the_guard_judges():
    """The Next.js /api route used to forward five named headers, so every request through it
    reached the backend as a tokenless local client - the owner - whichever page sent it."""
    src = (REPO / "web" / "app" / "api" / "[...path]" / "route.ts").read_text(encoding="utf-8")
    forwarded = re.search(r"const toForward = \[([^\]]*)\]", src)
    assert forwarded, "the forwarded-header list moved; re-point this guard"
    names = set(re.findall(r"'([a-z-]+)'", forwarded.group(1)))
    assert {"origin", "sec-fetch-site"} <= names
    assert re.search(r"out\['x-forwarded-host'\]\s*=\s*dialled", src)
    assert re.search(r"const dialled = request\.headers\.get\('host'\)", src)
