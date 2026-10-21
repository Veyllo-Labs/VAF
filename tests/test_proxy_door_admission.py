# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The HTTPS door refuses a client that is not admitted before it relays anything.

The measurement this was written on: ``TestClient(create_proxy_app(), client=("203.0.113.9",
40000))`` with the upstream faked: ``GET /login`` 200 (the login page relayed), ``GET
/api/users`` relayed upstream, and the WebSocket ``/ws/a2a/room1`` accepted by the proxy. The
door is the only listener on a network interface, and only the backend's API behind it said
no. The same for ``192.168.1.20``, which is admitted and must stay so while Local Network is
on; while it is off the door serves this machine only, as the backend does.
"""
import contextlib
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import vaf.network.https_proxy as proxy
from vaf.network import binding

PUBLIC = "203.0.113.9"
LAN = "192.168.1.20"
_REAL_POLICY = binding.inbound_policy


def fake_upstream(monkeypatch, *, local_network=True):
    """The proxy's two upstream legs, faked in one place: the pooled HTTP client and the
    backend socket dial. Returns what reached each; ``seen.network`` switches Local Network,
    read on every request like the real setting."""
    seen = SimpleNamespace(requests=[], sockets=[], access=[], network={"on": local_network})
    from vaf.core.config import Config
    real_get_bool = Config.get_bool.__func__
    monkeypatch.setattr(Config, "get_bool", classmethod(
        lambda cls, key, default=False: seen.network["on"] if key == "local_network_enabled"
        else real_get_bool(cls, key, default)))

    class _Upstream:
        async def request(self, method, url, headers=None, content=None):
            seen.requests.append((method, url))
            return SimpleNamespace(status_code=200, headers={"content-type": "text/html"},
                                   content=b"<html>login</html>")

    async def _client(_origin):
        return _Upstream()

    class _Backend:
        close_code = 1000
        close_reason = ""

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def send(self, _msg):
            pass

    @contextlib.asynccontextmanager
    async def _connect(uri, **_kw):
        seen.sockets.append(uri)
        yield _Backend()

    import websockets
    monkeypatch.setattr(proxy, "_get_client", _client)
    monkeypatch.setattr(websockets, "connect", _connect)
    monkeypatch.setattr(proxy, "_write_access", lambda line: seen.access.append(line))
    # The default admission (this machine and the RFC 1918 networks), whatever the session's
    # configuration says.
    monkeypatch.setattr(binding, "inbound_policy", lambda c=None, **kw: _REAL_POLICY({}, **kw))
    return seen


@pytest.fixture
def events(monkeypatch):
    found = []
    import vaf.core.security_events as se
    monkeypatch.setattr(se, "log_security_event", lambda kind, **f: found.append((kind, f)))
    return found


def _client(peer, forwarded=None):
    c = TestClient(proxy.create_proxy_app(), client=(peer, 40000))
    if forwarded:
        c.headers["X-Forwarded-For"] = forwarded
    return c


@pytest.mark.parametrize("peer,forwarded", [(PUBLIC, None), ("127.0.0.1", PUBLIC)],
                         ids=["a public device", "a relay on this machine naming one"])
def test_the_door_refuses_before_any_page(monkeypatch, events, peer, forwarded):
    seen = fake_upstream(monkeypatch)
    c = _client(peer, forwarded)
    for path in ("/login", "/api/users"):
        r = c.get(path)
        assert r.status_code == 403, path
        assert "login" not in r.text
    for path in ("/ws", "/ws/a2a/r1"):
        with pytest.raises(WebSocketDisconnect) as refused:
            with c.websocket_connect(path):
                pass
        assert refused.value.code == 4003, path
    assert seen.requests == [] and seen.sockets == []
    assert seen.access == [], "a refused request must not reach the unthrottled access log"
    assert [k for k, _ in events] == ["ip_blocked"] * 4
    assert {f["ip"] for _, f in events} == {PUBLIC}


def test_a_refused_browser_gets_the_page_not_the_login(monkeypatch, events):
    seen = fake_upstream(monkeypatch)
    r = _client(PUBLIC).get("/login", headers={"Accept": "text/html"})
    assert r.status_code == 403 and r.headers["content-type"].startswith("text/html")
    assert "vaf server status" in r.text and "login" not in r.text and PUBLIC not in r.text
    assert seen.requests == [] and seen.access == []


def test_with_local_network_off_the_door_serves_this_machine_only(monkeypatch, events):
    """The proxy runs whenever TLS is on, Local Network or not, and outlives a switch to off
    until the restart stops it; it relayed pages to LAN devices then while the backend refused
    them. MUTATION: wrap the app in the network guard alone - red."""
    seen = fake_upstream(monkeypatch, local_network=False)
    r = _client(LAN).get("/login", headers={"Accept": "text/html"})
    assert r.status_code == 403 and "Local Network is off" in r.text
    with pytest.raises(WebSocketDisconnect) as refused:
        with _client(LAN).websocket_connect("/ws"):
            pass
    assert refused.value.code == 4003
    assert seen.requests == [] and seen.sockets == [] and seen.access == []
    assert _client("127.0.0.1").get("/login").status_code == 200
    seen.network["on"] = True
    assert _client(LAN).get("/login").status_code == 200


def test_an_admitted_device_is_relayed(monkeypatch, events):
    seen = fake_upstream(monkeypatch)
    c = _client(LAN)
    r = c.get("/login")
    assert r.status_code == 200 and "login" in r.text
    with contextlib.suppress(Exception):
        with c.websocket_connect("/ws") as ws:
            ws.receive()
    assert [m for m, _ in seen.requests] == ["GET"]
    assert len(seen.sockets) == 1 and seen.sockets[0].endswith("/ws")
    assert len(seen.access) == 2
    assert events == []
