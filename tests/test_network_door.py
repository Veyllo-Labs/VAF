# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The door other devices reach holds against a client on the same network.

Measured on the HTTPS proxy before (scratch probe, the exact uvicorn settings it ran with):
TLS 1.2 handshakes with static-RSA and CBC/SHA-1 suites accepted on the proxy and on a listener
shaped like the desktop's TLS port; an idle connection after TLS, a request head sent a byte
at a time and a TCP connection that never started TLS all still open after 25 s; heads of
20 KiB, 64 KiB, 1 MB and 2000 headers answered 200; 520 idle connections and a request still
answered; and with FORWARDED_ALLOW_IPS=* a LAN peer that wrote X-Forwarded-For: 127.0.0.1
reached the backend as 127.0.0.1. After: the old suites refused, each slow connection closed at
15.0 s, the large heads 431, the request at 520 open connections 503, the LAN peer written as
itself.

Every live test here binds 127.0.0.1 on a free port (tests/door_harness.py).
"""
from __future__ import annotations

import ast
import asyncio
import logging
import socket
import ssl
import sys
import time
from pathlib import Path

import pytest
import uvicorn

from door_harness import closed_within, http_exchange, serving, tls_client, tls_connect
from ftp_stub import make_cert
from vaf.network import door
from vaf.network.ssl_utils import SERVER_TLS12_CIPHERS, server_tls_options

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def cert(tmp_path_factory):
    """An RSA certificate: the static-RSA suites the door refuses need one to be offered."""
    path = make_cert(tmp_path_factory.mktemp("door-cert"), "door", rsa=True)
    return str(path)


class _App:
    """A tiny ASGI app that answers 200 and counts the requests that reached it."""

    def __init__(self):
        self.calls = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        self.calls += 1
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-length", b"2")]})
        await send({"type": "http.response.body", "body": b"ok"})


def _door_config(app, cert=None, **extra):
    tls = server_tls_options(cert, cert) if cert else {}
    return uvicorn.Config(app, lifespan="off", log_config=None, **tls, **door.door_options(),
                          **extra)


GET = b"GET / HTTP/1.1\r\nHost: door\r\n\r\n"


# --------------------------------------------------------------------------- the TLS policy

def test_the_tls_policy_is_forward_secret_aead_only(cert):
    """AC 1. MUTATION: ssl_ciphers="DEFAULT" in the helper, or drop ssl_ciphers - red."""
    assert server_tls_options("c", "k") == {
        "ssl_certfile": "c", "ssl_keyfile": "k", "ssl_version": ssl.PROTOCOL_TLS_SERVER,
        "ssl_ciphers": SERVER_TLS12_CIPHERS}
    assert SERVER_TLS12_CIPHERS.split(":") == [
        "ECDHE-ECDSA-AES128-GCM-SHA256", "ECDHE-RSA-AES128-GCM-SHA256",
        "ECDHE-ECDSA-AES256-GCM-SHA384", "ECDHE-RSA-AES256-GCM-SHA384",
        "ECDHE-ECDSA-CHACHA20-POLY1305", "ECDHE-RSA-CHACHA20-POLY1305"]
    config = uvicorn.Config(_App(), log_config=None, **server_tls_options(cert, cert))
    config.load()
    tls12 = [c for c in config.ssl.get_ciphers() if c["protocol"] != "TLSv1.3"]
    assert tls12, "no TLS 1.2 suite left at all"
    for suite in tls12:
        assert suite["kea"] == "kx-ecdhe" and suite["aead"], suite["name"]
    # The floor is Python's own; pinned here, not set by a line that could not fail.
    assert config.ssl.minimum_version >= ssl.TLSVersion.TLSv1_2


_TLS_KEYWORDS = {"ssl_certfile", "ssl_keyfile", "ssl_ciphers", "ssl_version"}


def test_every_tls_listener_takes_the_policy():
    """AC 2: the proxy, the desktop's TLS port and run_server spread the one helper, and no
    file in vaf/ but ssl_utils.py writes a TLS keyword of uvicorn's by hand. MUTATION: write
    ssl_certfile=ssl_cert, ssl_keyfile=ssl_key in tray.py again - red."""
    offenders = []
    for path in sorted((ROOT / "vaf").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel == "vaf/network/ssl_utils.py" or rel.startswith("vaf/vendor/"):
            continue
        for node in ast.walk(ast.parse(path.read_bytes().decode("utf-8"))):
            if isinstance(node, ast.keyword) and node.arg in _TLS_KEYWORDS:
                offenders.append(f"{rel}:{node.value.lineno} {node.arg}=")
    assert not offenders, offenders
    for rel, call in (("vaf/network/https_proxy.py", "**server_tls_options(ssl_certfile, ssl_keyfile)"),
                      ("vaf/tray.py", "**server_tls_options(ssl_cert, ssl_key)"),
                      ("vaf/core/web_server.py", "**server_tls_options(ssl_cert, ssl_key)")):
        assert call in (ROOT / rel).read_bytes().decode("utf-8"), rel


def _handshake(port, ctx):
    try:
        with tls_connect(port, ctx) as s:
            return s.version(), s.cipher()[0]
    except (ssl.SSLError, ConnectionError, OSError):
        return None


@pytest.mark.parametrize("suite", ["AES128-SHA", "AES128-GCM-SHA256", "ECDHE-RSA-AES128-SHA"])
def test_old_suites_fail_the_handshake(cert, suite):
    """AC 3, live: a TLS 1.2 client that offers only a static-RSA or a CBC suite gets no
    connection; the same client gets one from a listener with OpenSSL's DEFAULT (the control
    proves it can offer the suite). MUTATION: the helper back to "DEFAULT" - red."""
    client = tls_client(suite, maximum=ssl.TLSVersion.TLSv1_2)
    control = uvicorn.Config(_App(), lifespan="off", log_config=None, ssl_certfile=cert,
                             ssl_keyfile=cert, ssl_ciphers="DEFAULT")
    with serving(control) as (port, _server):
        if _handshake(port, client) is None:
            pytest.skip(f"this platform's OpenSSL cannot offer {suite} at all")
    with serving(_door_config(_App(), cert), door.door_event_loop) as (port, _server):
        assert _handshake(port, client) is None


def test_modern_clients_still_connect(cert):
    """AC 3: an ECDHE + AES-GCM TLS 1.2 client connects, a default client gets TLS 1.3, and a
    client that speaks TLS 1.1 at most gets nothing."""
    with serving(_door_config(_App(), cert), door.door_event_loop) as (port, _server):
        assert _handshake(port, tls_client("ECDHE-RSA-AES128-GCM-SHA256",
                                           maximum=ssl.TLSVersion.TLSv1_2)) == (
            "TLSv1.2", "ECDHE-RSA-AES128-GCM-SHA256")
        assert _handshake(port, tls_client())[0] == "TLSv1.3"
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            old = tls_client("DEFAULT", minimum=ssl.TLSVersion.TLSv1_1,
                             maximum=ssl.TLSVersion.TLSv1_1)
        assert _handshake(port, old) is None


# --------------------------------------------------------------------------- the head

@pytest.fixture
def quick_head(monkeypatch):
    monkeypatch.setattr(door, "HEAD_TIMEOUT_S", 0.5)


def test_a_slow_head_is_closed(cert, quick_head):
    """AC 4: no complete head within HEAD_TIMEOUT_S closes the connection, while a complete
    request on the same listener answers. MUTATION: no arm in connection_made (the idle
    connection stays open), or none in on_message_begin (the kept-alive dribble stays open)
    - red."""
    app = _App()
    with serving(_door_config(app, cert), door.door_event_loop) as (port, _server):
        with tls_connect(port) as ok:
            assert http_exchange(ok, GET).startswith(b"HTTP/1.1 200")

        with tls_connect(port) as idle:
            assert closed_within(idle, 2.0), "a connection that sends nothing stayed open"

        with tls_connect(port) as slow:
            slow.sendall(b"GET / HTTP/1.1\r\nHost: door\r\nX-Slow: ")
            started = time.monotonic()
            closed = False
            while time.monotonic() - started < 2.0:
                try:
                    slow.sendall(b"a")
                except OSError:
                    closed = True
                    break
                if closed_within(slow, 0.2):
                    closed = True
                    break
            assert closed, "a head sent a byte at a time stayed open"

        with tls_connect(port) as kept:
            assert http_exchange(kept, GET).startswith(b"HTTP/1.1 200")
            kept.sendall(b"GET / HTTP/1.1\r\n")
            assert closed_within(kept, 2.0), "a dribbled second request stayed open"
    assert app.calls == 2


@pytest.mark.parametrize("head", [
    b"GET / HTTP/1.1\r\nHost: door\r\nX-Big: " + b"a" * (20 * 1024) + b"\r\n\r\n",
    b"GET /" + b"a" * (20 * 1024) + b" HTTP/1.1\r\nHost: door\r\n\r\n",
    b"GET / HTTP/1.1\r\nHost: door\r\n" + b"".join(b"X-H%04d: v\r\n" % i for i in range(2000))
    + b"\r\n",
    # An upgrade in an oversized head must not start a WebSocket on the closed connection.
    b"GET /ws HTTP/1.1\r\nHost: door\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n"
    b"Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nX-Big: "
    + b"a" * (20 * 1024) + b"\r\n\r\n",
], ids=["one header", "url", "2000 headers", "upgrade"])
def test_a_large_head_gets_431(cert, head, caplog):
    """AC 5: a head above HEAD_MAX_BYTES gets 431, the connection closes, and the app is never
    called. MUTATION: count only header values (the 2000-header row passes), skip the count in
    on_url (the URL row passes), or start the app on a refused head - red."""
    app = _App()
    with caplog.at_level(logging.WARNING):
        with serving(_door_config(app, cert), door.door_event_loop) as (port, _server):
            with tls_connect(port) as s:
                answer = http_exchange(s, head)
                assert answer.startswith(b"HTTP/1.1 431"), answer[:80]
                assert closed_within(s, 2.0)
    assert app.calls == 0
    assert not [r for r in caplog.records if r.exc_info], "a refused head raised inside uvicorn"


def test_a_head_under_the_limit_is_served(cert):
    """AC 5, the control: 15 KiB of head answers 200."""
    app = _App()
    with serving(_door_config(app, cert), door.door_event_loop) as (port, _server):
        with tls_connect(port) as s:
            head = b"GET / HTTP/1.1\r\nHost: door\r\nX-Big: " + b"a" * (15 * 1024) + b"\r\n\r\n"
            assert http_exchange(s, head).startswith(b"HTTP/1.1 200")
    assert app.calls == 1


def test_a_head_line_that_never_ends_is_refused(monkeypatch):
    """httptools gathers a header line until it ends before the callback sees it, so a line
    that never ends grew without a bound: the raw bytes of an unfinished head count too.
    MUTATION: drop the raw count in data_received - red (the line keeps growing)."""
    monkeypatch.setattr(door, "HEAD_TIMEOUT_S", 30.0)
    app = _App()
    with serving(_door_config(app), door.door_event_loop) as (port, _server):
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(b"GET / HTTP/1.1\r\nHost: door\r\nX-Endless: ")
            refused = False
            for _ in range(8):
                try:
                    s.sendall(b"a" * 4096)
                except OSError:
                    refused = True
                    break
                time.sleep(0.05)
            answer = b""
            s.settimeout(5)
            try:
                answer = s.recv(4096)
            except OSError:
                refused = True
            assert answer.startswith(b"HTTP/1.1 431") or refused, answer[:80]
            assert closed_within(s, 2.0)
    assert app.calls == 0


# --------------------------------------------------------------------------- handshake, cap

def test_the_handshake_has_a_limit(cert, monkeypatch):
    """AC 6: a TCP connection that never starts TLS is closed by the door's loop; the same
    listener on a plain loop keeps it open (asyncio's default is 60 s). MUTATION: return a
    plain loop from door_event_loop - red."""
    monkeypatch.setattr(door, "TLS_HANDSHAKE_TIMEOUT_S", 0.5)
    with serving(_door_config(_App(), cert), door.door_event_loop) as (port, _server):
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            assert closed_within(s, 2.0)
    with serving(_door_config(_App(), cert), asyncio.new_event_loop) as (port, _server):
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            assert not closed_within(s, 2.0)


def test_the_door_answers_503_at_the_cap(monkeypatch):
    """AC 7: at the connection cap a request gets 503 with the connection closed. MUTATION:
    drop limit_concurrency from door_options - red."""
    monkeypatch.setattr(door, "MAX_CONNECTIONS", 2)
    app = _App()
    with serving(_door_config(app), door.door_event_loop) as (port, server):
        with socket.create_connection(("127.0.0.1", port), timeout=5) as alone:
            assert http_exchange(alone, b"GET / HTTP/1.1\r\nHost: door\r\nConnection: close"
                                        b"\r\n\r\n").startswith(b"HTTP/1.1 200")
        idle = [socket.create_connection(("127.0.0.1", port), timeout=5) for _ in range(2)]
        try:
            deadline = time.monotonic() + 5
            while len(server.server_state.connections) < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
            with socket.create_connection(("127.0.0.1", port), timeout=5) as third:
                answer = http_exchange(third, GET)
                assert answer.startswith(b"HTTP/1.1 503"), answer[:80]
                assert b"connection: close" in answer.lower()
        finally:
            for s in idle:
                s.close()
    assert app.calls == 1


# --------------------------------------------------------------------------- open files

class _FakeResource:
    RLIMIT_NOFILE = 7
    RLIM_INFINITY = -1

    def __init__(self, soft, hard, refuse=()):
        self.limits = (soft, hard)
        self.refuse = set(refuse)
        self.calls = []

    def getrlimit(self, which):
        assert which == self.RLIMIT_NOFILE
        return self.limits

    def setrlimit(self, which, limits):
        self.calls.append(limits)
        if limits[0] in self.refuse:
            raise ValueError("not allowed")
        self.limits = limits


@pytest.fixture
def fake_resource(monkeypatch):
    monkeypatch.setattr(door, "_limit_logged", False)
    monkeypatch.setattr(door, "_cap_warned", False)

    def _install(module):
        monkeypatch.setitem(sys.modules, "resource", module)
        return module
    return _install


def test_the_open_file_limit_is_raised(fake_resource):
    """AC 8: a systemd service's (1024, 524288) becomes (65536, 524288); a refusal is retried
    with macOS's OPEN_MAX; a second call changes nothing. MUTATION: no setrlimit, or no retry
    with 10240 - red."""
    res = fake_resource(_FakeResource(1024, 524288))
    assert door.raise_open_file_limit() == 65536
    assert res.calls == [(65536, 524288)]
    assert door.raise_open_file_limit() == 65536 and len(res.calls) == 1
    assert door.connection_cap() == door.MAX_CONNECTIONS

    res = fake_resource(_FakeResource(256, -1, refuse={65536}))
    assert door.raise_open_file_limit() == 10240
    assert res.calls == [(65536, -1), (10240, -1)]

    res = fake_resource(_FakeResource(100000, 524288))
    assert door.raise_open_file_limit() == 100000 and res.calls == []   # never lowered


def test_a_low_limit_lowers_the_cap(fake_resource, caplog):
    """AC 8: where the limit cannot be raised (soft = hard = 1024) the cap is a quarter of it,
    said once; without a resource module (Windows) there is no limit to read. MUTATION: a cap
    not derived from the soft limit - red."""
    res = fake_resource(_FakeResource(1024, 1024))
    with caplog.at_level(logging.WARNING, logger="vaf.network.door"):
        assert door.raise_open_file_limit() == 1024
        assert door.connection_cap() == 256
        assert door.door_options()["limit_concurrency"] == 256
    assert res.calls == []
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1 and "1024" in warnings[0].getMessage()

    fake_resource(None)          # `import resource` raises ImportError
    assert door.raise_open_file_limit() is None
    assert door.connection_cap() == door.MAX_CONNECTIONS


# --------------------------------------------------------------------------- the options

def _app_chain(app):
    chain = []
    while app is not None and len(chain) < 20:
        chain.append(type(app).__name__)
        app = getattr(app, "app", None)
    return chain


def test_door_options_switch_off_forwarded_trust(monkeypatch):
    """AC 9: with FORWARDED_ALLOW_IPS=* in the environment, a door's app has no uvicorn
    proxy-header middleware (it turned a LAN peer's X-Forwarded-For: 127.0.0.1 into this
    machine); a listener without the options does. MUTATION: leave proxy_headers at its
    default - red."""
    options = door.door_options()
    assert options == {"http": door.BoundedHttpToolsProtocol,
                       "limit_concurrency": door.connection_cap(),
                       "timeout_keep_alive": 5, "server_header": False, "proxy_headers": False}
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    plain = uvicorn.Config(_App(), log_config=None)
    plain.load()
    assert "ProxyHeadersMiddleware" in _app_chain(plain.loaded_app)
    config = uvicorn.Config(_App(), log_config=None, **options)
    config.load()
    assert "ProxyHeadersMiddleware" not in _app_chain(config.loaded_app)


def _capture_serve(monkeypatch, seen):
    async def _serve(self, sockets=None):
        seen["config"] = self.config
        seen["loop"] = asyncio.get_running_loop()
    monkeypatch.setattr(uvicorn.Server, "serve", _serve)
    real = door.door_event_loop

    def _door_loop():
        loop = real()
        seen.setdefault("door_loops", []).append(loop)
        return loop
    monkeypatch.setattr(door, "door_event_loop", _door_loop)
    import vaf.core.log_helper as log_helper
    monkeypatch.setattr(log_helper, "redacted_uvicorn_log_config", lambda: None)


@pytest.fixture
def door_loops_closed():
    """run_https_proxy and run_server set their loop as this thread's; the test closes them
    and leaves no loop behind for the next test."""
    seen = {}
    yield seen
    for loop in seen.get("door_loops", []):
        loop.close()
    asyncio.set_event_loop(None)


def _assert_a_door(seen):
    config = seen["config"]
    assert config.http is door.BoundedHttpToolsProtocol
    assert config.limit_concurrency == door.connection_cap()
    assert config.timeout_keep_alive == door.KEEP_ALIVE_S
    assert config.server_header is False and config.proxy_headers is False
    assert seen["loop"] in seen.get("door_loops", []), "not served on the door's loop"


def test_both_doors_use_the_options_and_the_loop(monkeypatch, tmp_path, door_loops_closed):
    """AC 9: run_https_proxy and run_server build their Config with the door's options and the
    TLS policy, and serve it on the door's loop. MUTATION: run_server without door_options(),
    or run_https_proxy on asyncio.new_event_loop() - red."""
    import vaf.network.binding as binding
    import vaf.network.https_proxy as proxy
    from vaf.network import runtime_status
    seen = door_loops_closed
    _capture_serve(monkeypatch, seen)
    monkeypatch.setattr(binding, "pick_bindable_port", lambda _host, port, _fallback: port)
    monkeypatch.setattr(runtime_status, "set_proxy_bound", lambda *_a: None)
    monkeypatch.setattr(runtime_status, "set_proxy_failed", lambda *_a: None)
    proxy.run_https_proxy("127.0.0.1", 18443, "cert.pem", "key.pem")
    _assert_a_door(seen)
    assert seen["config"].ssl_ciphers == SERVER_TLS12_CIPHERS
    assert seen["config"].ssl_certfile == "cert.pem"

    import vaf.core.web_server as web_server
    from vaf.core.config import Config
    monkeypatch.setenv("VAF_WEBUI_ACTIVE", "0")      # run_server sets it; restored after
    monkeypatch.setattr(web_server.manager, "set_server_loop", lambda _loop: None)
    for tls in (False, True):
        seen.pop("config", None)
        seen.pop("loop", None)
        files = {}
        if tls:
            for name in ("cert", "key"):
                files[name] = tmp_path / f"{name}.pem"
                files[name].write_text("unused: Config.load is never reached")
        values = {"local_network_tls_enabled": tls,
                  "local_network_ssl_cert": str(files.get("cert", "")),
                  "local_network_ssl_key": str(files.get("key", ""))}
        monkeypatch.setattr(Config, "get", staticmethod(
            lambda key, default=None, _v=values: _v.get(key, default)))
        web_server.run_server("127.0.0.1", 18001)
        _assert_a_door(seen)
        if tls:
            assert seen["config"].ssl_ciphers == SERVER_TLS12_CIPHERS


# --------------------------------------------------------------------------- the pin

def test_the_uvicorn_hooks_exist():
    """AC 18: the protocol overrides asyncio's protocol methods and httptools' parser
    callbacks of uvicorn's HttpToolsProtocol, which must still exist under these names, and
    uvicorn must still take a protocol class. A refused head drops the gathered headers so
    uvicorn's upgrade check (which reads self.headers) cannot start a WebSocket. MUTATION:
    rename one override (on_headers instead of on_header) - red here and in the 431 test."""
    import inspect

    from uvicorn.protocols.http.httptools_impl import HttpToolsProtocol
    hooks = ("connection_made", "connection_lost", "data_received", "on_message_begin",
             "on_url", "on_header", "on_headers_complete", "on_body", "on_message_complete")
    for name in hooks:
        assert name in vars(HttpToolsProtocol), f"uvicorn no longer defines {name}"
        assert name in vars(door.BoundedHttpToolsProtocol), f"the door does not override {name}"
    overrides = {n for n, v in vars(door.BoundedHttpToolsProtocol).items()
                 if callable(v) and not n.startswith("_")}
    assert overrides <= set(hooks), overrides - set(hooks)
    assert "self.headers" in inspect.getsource(HttpToolsProtocol._get_upgrade)
    config = uvicorn.Config(_App(), http=door.BoundedHttpToolsProtocol, log_config=None)
    config.load()
    assert config.http_protocol_class is door.BoundedHttpToolsProtocol


def test_the_door_reads_no_client_address():
    """The protocol never reads the peer: binding.connection_client_ip is the only reader
    (tests/test_client_address_has_one_reader.py walks vaf/ for the shapes; this pins the
    attribute uvicorn keeps the peer in)."""
    source = (ROOT / "vaf" / "network" / "door.py").read_bytes().decode("utf-8")
    assert "self.client" not in source and "peername" not in source
