# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The admission check as a door: HTTP and WebSocket alike, the inner app never reached.

The measurement this was written on: a Starlette app behind the old address check (a
``BaseHTTPMiddleware``) with an HTTP route and a WebSocket route, client ``203.0.113.9``:
HTTP 403, while the WebSocket opened and the route answered ``hello``. ``BaseHTTPMiddleware``
passes every scope that is not ``http`` through, so a check written that way judges no socket.

Addresses that must be PUBLIC to ``classify_address`` are not used here: the documentation
ranges (``203.0.113.0/24``) are refused as clients, which is all these rows need.
"""
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from vaf.network import admission, binding

ROOT = Path(__file__).resolve().parents[1]
PROXY_PEER = "127.0.0.1"
MESH = "100.101.102.103"


@pytest.fixture
def reached():
    return []


_REAL_POLICY = binding.inbound_policy


@pytest.fixture
def admitted(monkeypatch):
    """The admitted networks as a config dict, read the way the door reads them."""
    cfg = {"local_network_allowed_networks": [], "local_network_vpn_only": False}
    monkeypatch.setattr(binding, "inbound_policy", lambda c=None, **kw: _REAL_POLICY(cfg, **kw))
    return cfg


@pytest.fixture
def events(monkeypatch):
    seen = []
    import vaf.core.security_events as se
    monkeypatch.setattr(se, "log_security_event", lambda kind, **f: seen.append((kind, f)))
    return seen


def _app(reached):
    async def ok(request):
        reached.append(("http", request.url.path))
        return JSONResponse({"ok": True})

    async def socket(websocket):
        reached.append(("websocket", websocket.url.path))
        await websocket.accept()
        await websocket.send_text("hello")
        await websocket.close()

    app = Starlette(routes=[Route("/login", ok), WebSocketRoute("/ws/a2a/r1", socket)])
    return admission.AdmissionGuard(app)


def _client(app, peer, forwarded=None):
    c = TestClient(app, client=(peer, 40000))
    if forwarded:
        c.headers["X-Forwarded-For"] = forwarded
    return c


REFUSED = [
    ("a public peer", "203.0.113.9", None),
    ("a relay on this machine naming a public client", PROXY_PEER, "203.0.113.9"),
    ("a hop that is a name, not an address", PROXY_PEER, "localhost"),
    ("a mesh VPN address while 100.64.0.0/10 is not admitted", MESH, None),
]

PASSING = [
    ("a LAN device", "192.168.1.20", None),
    ("this machine", PROXY_PEER, None),
    ("a relay on this machine naming a LAN device", PROXY_PEER, "192.168.1.66"),
]


@pytest.mark.parametrize("why,peer,forwarded", REFUSED, ids=[r[0] for r in REFUSED])
def test_the_guard_refuses_what_the_policy_does_not_admit(why, peer, forwarded, admitted,
                                                         reached, events):
    app = _app(reached)
    r = _client(app, peer, forwarded).get("/login")
    assert r.status_code == 403
    assert r.json() == {"detail": "Access denied: only local network clients are allowed"}
    with pytest.raises(WebSocketDisconnect) as refused:
        with _client(app, peer, forwarded).websocket_connect("/ws/a2a/r1"):
            pass
    assert refused.value.code == 4003
    assert reached == []
    assert [k for k, _ in events] == ["ip_blocked", "ip_blocked"]


@pytest.mark.parametrize("why,peer,forwarded", PASSING, ids=[r[0] for r in PASSING])
def test_the_guard_admits_what_the_policy_admits(why, peer, forwarded, admitted, reached,
                                                events):
    app = _app(reached)
    assert _client(app, peer, forwarded).get("/login").status_code == 200
    with _client(app, peer, forwarded).websocket_connect("/ws/a2a/r1") as ws:
        assert ws.receive_text() == "hello"
    assert reached == [("http", "/login"), ("websocket", "/ws/a2a/r1")]
    assert events == []


def test_an_admitted_mesh_network_passes_on_the_next_request(admitted, reached, events):
    app = _app(reached)
    assert _client(app, MESH).get("/login").status_code == 403
    admitted["local_network_allowed_networks"] = ["100.64.0.0/10"]
    assert _client(app, MESH).get("/login").status_code == 200


async def _drive(app, scope):
    """One raw ASGI call; returns what the app sent."""
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return sent


def test_a_scope_without_a_client_is_refused(admitted, reached, events):
    import asyncio
    scope = {"type": "http", "method": "GET", "path": "/login", "raw_path": b"/login",
             "query_string": b"", "headers": [], "scheme": "http", "server": ("127.0.0.1", 8443),
             "http_version": "1.1", "root_path": ""}
    sent = asyncio.run(_drive(_app(reached), scope))
    assert sent[0]["status"] == 403
    assert reached == []
    assert events and events[0][1]["ip"] == "unknown"


def test_a_lifespan_scope_passes():
    import asyncio
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    asyncio.run(admission.AdmissionGuard(inner)({"type": "lifespan"}, None, None))
    assert seen == ["lifespan"]


def test_a_door_can_admit_by_its_own_rule(reached, events):
    """This machine only, the rule the backend keeps while Local Network is off."""
    app = Starlette(routes=[Route("/login", lambda r: JSONResponse({}))])
    guard = admission.AdmissionGuard(app, admits=binding.is_localhost, reason="Not here")
    assert _client(guard, PROXY_PEER).get("/login").status_code == 200
    r = _client(guard, PROXY_PEER, "192.168.1.20").get("/login")
    assert r.status_code == 403 and r.json() == {"detail": "Not here"}


# --------------------------------------------------------------------------- the record

def test_a_refusal_is_recorded_with_the_path_in_detail(admitted, reached, events):
    _client(_app(reached), PROXY_PEER, "203.0.113.9").get("/login")
    assert events == [("ip_blocked", {"ip": "203.0.113.9", "detail": "/login"})]


def test_a_scanner_walking_paths_writes_one_line(admitted, reached, tmp_path, monkeypatch):
    """The writer throttles per source and kind with ``path`` in its key: fifty paths from one
    address were fifty lines. With the path in ``detail`` they are one within 5 seconds."""
    import vaf.core.security_events as se
    monkeypatch.setenv("VAF_LOG_DIR", str(tmp_path))
    se._last_emit.clear()
    client = _client(_app(reached), "203.0.113.9")
    for i in range(50):
        assert client.get(f"/login{i}").status_code == 403
    day = datetime.now().strftime("%Y-%m-%d")
    lines = (tmp_path / f"security_events_{day}.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["kind"] == "ip_blocked"


# --------------------------------------------------------------------------- slim installs

def test_vaf_network_imports_without_starlette(tmp_path):
    """starlette is in the server extra; `import vaf.network` must work without it, and only
    the door module needs it."""
    probe = (
        "import sys\n"
        "class _Blocker:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in ('starlette', 'fastapi'):\n"
        "            raise ModuleNotFoundError(name, name=name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, _Blocker())\n"
        "import vaf.network\n"
        "assert 'vaf.network.admission' not in sys.modules\n"
        "try:\n"
        "    import vaf.network.admission\n"
        "except ModuleNotFoundError:\n"
        "    print('door needs starlette')\n"
        "print('ok')\n"
    )
    env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path),
               XDG_DATA_HOME=str(tmp_path / "d"), XDG_CONFIG_HOME=str(tmp_path / "c"),
               XDG_CACHE_HOME=str(tmp_path / "k"), XDG_STATE_HOME=str(tmp_path / "s"))
    r = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, env=env, capture_output=True,
                       text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["door", "needs", "starlette", "ok"]
