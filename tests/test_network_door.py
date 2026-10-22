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


@pytest.fixture
def quiet_refusals(monkeypatch):
    """The admission check records each refusal in the security log; not from these tests."""
    import vaf.core.security_events as security_events
    monkeypatch.setattr(security_events, "log_security_event", lambda *_a, **_k: None)


def test_a_slow_head_is_closed(cert, quick_head):
    """AC 4: no complete head within HEAD_TIMEOUT_S closes the connection, while a complete
    request on the same listener answers. MUTATION: no arm in connection_made (the idle
    connection stays open) - red. The kept-alive dribble is held twice over, by the arm on
    bytes that arrive after an answer and by the one in on_message_begin; each has its own
    test below (an empty line after an answer, a pipelined head)."""
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


def _read_until_closed(sock: socket.socket, seconds: float = 3.0) -> bytes:
    """Everything the server sends until it closes (or a reset, or the time is up)."""
    deadline = time.monotonic() + seconds
    data = b""
    while time.monotonic() < deadline:
        sock.settimeout(max(0.01, deadline - time.monotonic()))
        try:
            part = sock.recv(65536)
        except (socket.timeout, OSError):
            break
        if not part:
            break
        data += part
    return data


class _Early:
    """Answers 401 at once without reading the body and keeps the connection alive: the shape
    of a backend route that refuses a request before its body (``vaf run --web``)."""

    def __init__(self):
        self.calls = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        self.calls += 1
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-length", b"2")]})
        await send({"type": "http.response.body", "body": b"no"})


class _Reader:
    """Reads the whole body before it answers 200 with the byte count, like the proxy's relay;
    records a client that left first."""

    def __init__(self):
        self.received = []
        self.disconnected = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                self.disconnected += 1
                return
            total += len(message.get("body", b""))
            if not message.get("more_body"):
                break
        self.received.append(total)
        body = b"%d" % total
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-length", b"%d" % len(body))]})
        await send({"type": "http.response.body", "body": body})


def test_an_empty_line_after_an_answer_starts_the_head_timer(quick_head):
    """uvicorn's data_received cancels its keep-alive timer on any byte and re-arms it only
    after the next answer, and llhttp skips an empty line between requests without
    on_message_begin: one CRLF after an answer held the connection forever (measured: open
    after 45 s; three such connections at a cap of 4 turned every new request away with
    503). MUTATION: no arm on bytes that arrive while nothing is being answered - red."""
    with serving(_door_config(_App()), door.door_event_loop) as (port, _server):
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            assert http_exchange(s, GET).startswith(b"HTTP/1.1 200")
            s.sendall(b"\r\n")
            assert closed_within(s, 2.0), "an empty line after the answer held the connection"


@pytest.mark.parametrize("accept", [b"text/html", b"application/json"], ids=["page", "json"])
def test_a_refused_client_is_closed_at_once(monkeypatch, quiet_refusals, accept):
    """The admission check's 403 closes the connection itself, long before any timer: a
    refused client cannot keep a slot of the cap. MUTATION: drop Connection: close from the
    403 - red (uvicorn keeps the connection alive for 5 s)."""
    from vaf.network.admission import AdmissionGuard
    monkeypatch.setattr(door, "HEAD_TIMEOUT_S", 30.0)
    app = _App()
    with serving(_door_config(AdmissionGuard(app, admits=lambda ip: False)),
                 door.door_event_loop) as (port, _server):
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            answer = http_exchange(s, b"GET / HTTP/1.1\r\nHost: door\r\nAccept: " + accept
                                   + b"\r\n\r\n")
            assert answer.startswith(b"HTTP/1.1 403"), answer[:80]
            assert b"\r\nconnection: close\r\n" in answer
            assert closed_within(s, 1.0), "the refused connection was kept alive"
    assert app.calls == 0


def _refused():
    from vaf.network.admission import AdmissionGuard
    return AdmissionGuard(_App(), admits=lambda ip: False)


def _trickle(sock: socket.socket, seconds: float, piece: bytes = b"x" * 100) -> bool:
    """Send a piece every 0.1 s for at most `seconds`; return whether the server closed."""
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        try:
            sock.sendall(piece)
        except OSError:
            return True
        if closed_within(sock, 0.1):
            return True
    return False


def test_a_refusal_lingers_for_its_body(monkeypatch, quiet_refusals):
    """An answer that ends its connection while the request's body still arrives (here the
    admission check's 403 with Connection: close) reads and discards the rest before it
    closes, so the answer stays readable: closed at once, the connection was reset under the
    arriving body and the 403 lost (measured: an 8 MB POST got ConnectionResetError). It
    closes once the body has stopped for LINGER_TIMEOUT_S. MUTATION: close without lingering
    - red (reset)."""
    monkeypatch.setattr(door, "LINGER_TIMEOUT_S", 0.5)
    size = 8 * 1000 * 1000
    with serving(_door_config(_refused()), door.door_event_loop) as (port, _server):
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(b"POST /api/auth/login HTTP/1.1\r\nHost: door\r\n"
                      b"Content-Type: application/json\r\nContent-Length: %d\r\n\r\n" % size
                      + b"x" * size)
            answer = _one_answer(s)
            assert answer.startswith(b"HTTP/1.1 403"), answer[:80]
            assert b"\r\nconnection: close\r\n" in answer.lower()
            assert closed_within(s, 2.0), "the refused upload kept its connection"


def test_a_repeat_refused_client_is_closed(quiet_refusals):
    """A refused client that sent a request with a small body every few seconds kept one
    connection for good (measured: 10 refused answers over 40 s on one connection). Every 403
    ends its connection now. MUTATION: keep the 403 alive - red (a second answer)."""
    request = b"POST /api/x HTTP/1.1\r\nHost: door\r\nContent-Length: 2\r\n\r\nab"
    with serving(_door_config(_refused()), door.door_event_loop) as (port, _server):
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(request)
            assert _one_answer(s).startswith(b"HTTP/1.1 403")
            time.sleep(0.3)
            try:
                s.sendall(request)
            except OSError:
                pass
            rest = _read_until_closed(s, 3.0)
            assert b"HTTP/1.1 403" not in rest, "a second refused request was answered"
            assert closed_within(s, 0.5)


def test_lingering_ends_when_the_body_stops(monkeypatch, quiet_refusals):
    """Lingering waits at most LINGER_TIMEOUT_S between two reads: a client that sent part of
    its body and then nothing is closed then. MUTATION: no wait limit between reads - red."""
    monkeypatch.setattr(door, "LINGER_TIMEOUT_S", 0.5)
    with serving(_door_config(_refused()), door.door_event_loop) as (port, _server):
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(b"POST / HTTP/1.1\r\nHost: door\r\nContent-Length: 100000\r\n\r\n"
                      + b"x" * 10)
            assert _one_answer(s).startswith(b"HTTP/1.1 403")
            assert closed_within(s, 2.0), "lingering outlived a body that stopped"


def test_lingering_has_a_total_time(monkeypatch, quiet_refusals):
    """Lingering lasts at most LINGER_TIME_S in all, however steadily the rest of the body
    arrives. MUTATION: no total limit - red."""
    monkeypatch.setattr(door, "LINGER_TIMEOUT_S", 0.5)
    monkeypatch.setattr(door, "LINGER_TIME_S", 1.0)
    with serving(_door_config(_refused()), door.door_event_loop) as (port, _server):
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(b"POST / HTTP/1.1\r\nHost: door\r\nContent-Length: 10000000\r\n\r\n"
                      + b"x" * 10)
            assert _one_answer(s).startswith(b"HTTP/1.1 403")
            assert _trickle(s, 3.0), "a body trickled after the answer kept the connection"


def test_an_early_answer_kept_alive_lingers_too(monkeypatch):
    """An app that answers before reading the body and keeps the connection alive (a backend
    route's 401 under vaf run --web) gets the same lingering: the rest of the body is read and
    discarded, never parsed, and the connection closes after it. MUTATION: no lingering after
    a kept-alive early answer - red (the trickle keeps the connection)."""
    monkeypatch.setattr(door, "HEAD_TIMEOUT_S", 30.0)
    monkeypatch.setattr(door, "LINGER_TIMEOUT_S", 0.5)
    monkeypatch.setattr(door, "LINGER_TIME_S", 1.0)
    app = _Early()
    with serving(_door_config(app), door.door_event_loop) as (port, _server):
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(b"POST / HTTP/1.1\r\nHost: door\r\nContent-Length: 10000000\r\n\r\n")
            answer = _one_answer(s)
            assert answer.startswith(b"HTTP/1.1 401") and b"connection: close" not in answer
            assert _trickle(s, 3.0), "a body trickled after an early answer kept the connection"
    assert app.calls == 1


def test_a_read_with_an_earlier_body_is_not_counted_as_head(monkeypatch):
    """A read that carries the end of one request's body and the start of the next request's
    head counted whole toward HEAD_MAX_BYTES (a false 431). MUTATION: count every read while
    a head is pending - red."""
    monkeypatch.setattr(door, "HEAD_TIMEOUT_S", 10.0)
    app = _Reader()
    with serving(_door_config(app), door.door_event_loop) as (port, _server):
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            body = 40 * 1024
            s.sendall(b"POST / HTTP/1.1\r\nHost: door\r\nContent-Length: %d\r\n\r\n" % body
                      + b"b" * (body - 20 * 1024))
            time.sleep(0.1)
            # The last 20 KiB of the body and half of the next head arrive in one read.
            s.sendall(b"b" * (20 * 1024) + b"GET / HTTP/1.1\r\nHost: door\r\n")
            time.sleep(0.1)
            s.sendall(b"Connection: close\r\n\r\n")
            answers = _read_until_closed(s)
    assert answers.count(b"HTTP/1.1 200") == 2, answers[:200]
    assert b"431" not in answers and app.received == [body, 0]


def test_a_pipelined_head_has_the_time_limit_too(quick_head):
    """A head that begins while an earlier request is still being answered (pipelined) has
    the same HEAD_TIMEOUT_S. MUTATION: no arm in on_message_begin - red (the connection waits
    for the slow answer first)."""
    class _Slow:
        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                return
            await asyncio.sleep(3.0)
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-length", b"2")]})
            await send({"type": "http.response.body", "body": b"ok"})

    with serving(_door_config(_Slow()), door.door_event_loop) as (port, _server):
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(GET + b"GET / HTTP/1.1\r\n")
            assert closed_within(s, 2.0), "a pipelined head sent in part stayed open"


def test_a_connection_may_send_only_so_far_ahead():
    """uvicorn queues every request of a read before it pauses reading, so pipelined requests
    whose answers were never read piled up with no limit (measured: one 256 KiB send of
    minimal requests queued 9,362 of them, RSS 45 to 79 MB, relayed upstream one by one for
    minutes). Past PIPELINE_MAX the connection is dropped, with no answer over the one still
    due. MUTATION: no pipeline limit in on_message_begin - red."""
    app = _App()
    with serving(_door_config(app), door.door_event_loop) as (port, _server):
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(b"GET / HTTP/1.1\r\nHost: door\r\n\r\n" * 2000)
            assert closed_within(s, 2.0), "a flood of pipelined requests kept the connection"
    assert app.calls <= door.PIPELINE_MAX + 1


def test_pipelined_requests_up_to_the_limit_are_answered():
    """The control: one request in flight and PIPELINE_MAX more sent ahead of it are all
    answered, in order. MUTATION: a limit one lower - red."""
    app = _App()
    count = door.PIPELINE_MAX + 1
    with serving(_door_config(app), door.door_event_loop) as (port, _server):
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(GET * (count - 1) + b"GET / HTTP/1.1\r\nHost: door\r\n"
                      b"Connection: close\r\n\r\n")
            answers = _read_until_closed(s, 5.0)
    assert answers.count(b"HTTP/1.1 200") == count, answers[-200:]
    assert app.calls == count


def test_a_431_never_answers_another_request():
    """A head refused while the request before it is still being answered closes the
    connection without a 431, which the client would read as the earlier request's answer.
    MUTATION: write the 431 whatever is being answered - red."""
    class _Slow:
        calls = 0

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                return
            _Slow.calls += 1
            await asyncio.sleep(1.0)
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-length", b"2")]})
            await send({"type": "http.response.body", "body": b"ok"})

    with serving(_door_config(_Slow()), door.door_event_loop) as (port, _server):
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(GET + b"GET / HTTP/1.1\r\nHost: door\r\nX-Big: " + b"a" * (20 * 1024)
                      + b"\r\n\r\n")
            answer = _read_until_closed(s)
    assert b"431" not in answer, answer[:80]
    assert _Slow.calls == 1


def _one_answer(sock: socket.socket) -> bytes:
    """One whole answer: the head, then as many body bytes as its Content-Length says."""
    sock.settimeout(5)
    data = b""
    while b"\r\n\r\n" not in data:
        part = sock.recv(65536)
        if not part:
            return data
        data += part
    head, _, body = data.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value)
    while len(body) < length:
        part = sock.recv(65536)
        if not part:
            break
        body += part
    return head + b"\r\n\r\n" + body


def _send_until_the_door_acts(sock: socket.socket, piece: bytes, times: int = 64):
    """Send `piece` again and again, a read each, until the server answers or closes; stop
    sending at once then, so the client's further bytes cannot turn the close into a reset
    that hides the answer. Returns (what the server sent, whether it closed)."""
    for _ in range(times):
        try:
            sock.sendall(piece)
        except OSError:
            return b"", True
        sock.settimeout(0.05)
        try:
            part = sock.recv(65536)
        except socket.timeout:
            continue
        except OSError:
            return b"", True
        if not part:
            return b"", True
        rest = _read_until_closed(sock, 3.0)
        return part + rest, closed_within(sock, 0.5)
    return b"", False


class _Started:
    """Starts its answer at once and keeps it open while it waits for the body."""

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"part", "more_body": True})
        while (await receive())["type"] != "http.disconnect":
            pass


@pytest.mark.parametrize("app_kind", ["reads the body", "answer started", "refused"])
def test_an_endless_trailer_is_refused(monkeypatch, quiet_refusals, app_kind):
    """After the last chunk, httptools gathers a trailer line until it ends, and the raw count
    stopped with the head: a trailer line that never ended took the process from 50 MB to
    about 1 GB in 48 s, for an admitted client (until the relay's 60 s stall) and a refused
    one (with no limit at all). It counts toward HEAD_MAX_BYTES now: 431 while the request
    has no answer yet, a plain close once its answer has begun. A refused request lingers
    instead: the trailer is discarded unparsed for LINGER_TIME_S at most. MUTATION: drop the
    trailer count - red (the first two rows stay open); no 431 for a trailer - red (reads the
    body); write the 431 after an answer began - red (answer started)."""
    monkeypatch.setattr(door, "HEAD_TIMEOUT_S", 30.0)
    monkeypatch.setattr(door, "LINGER_TIME_S", 1.0)
    app = {"reads the body": _Reader(), "answer started": _Started(),
           "refused": _refused()}[app_kind]
    with serving(_door_config(app), door.door_event_loop) as (port, _server):
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(b"POST / HTTP/1.1\r\nHost: door\r\nTransfer-Encoding: chunked\r\n\r\n"
                      b"5\r\nhello\r\n0\r\nX-Endless: ")
            if app_kind != "reads the body":
                first = _one_answer(s)
                assert first.startswith(b"HTTP/1.1 " + (b"403" if app_kind == "refused"
                                                        else b"200")), first[:80]
            if app_kind == "answer started":
                # The first part of the open answer, read before the flood, so that only the
                # door's own reaction ends the flood.
                s.settimeout(5)
                while b"part" not in first:
                    first += s.recv(4096)
            then, closed = _send_until_the_door_acts(s, b"a" * 4096)
    assert closed, "an endless trailer stayed open"
    if app_kind == "reads the body":
        assert then.startswith(b"HTTP/1.1 431"), then[:80]
        assert app.received == [] and app.disconnected == 1
    else:
        assert b"431" not in then, then[:80]


def test_a_slow_upload_is_not_a_slow_head(quick_head):
    """The head timer holds bytes only while nothing is being answered: a body the app is
    reading takes as long as it takes here (the proxy holds it to BODY_STALL_S per part).
    MUTATION: arm the head timer on any byte when none runs - red."""
    app = _Reader()
    with serving(_door_config(app), door.door_event_loop) as (port, _server):
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(b"POST / HTTP/1.1\r\nHost: door\r\nContent-Length: 10\r\n"
                      b"Connection: close\r\n\r\nabcde")
            time.sleep(0.6)
            s.sendall(b"f")
            time.sleep(0.6)
            s.sendall(b"ghij")
            answer = _read_until_closed(s)
    assert answer.startswith(b"HTTP/1.1 200") and answer.endswith(b"\r\n\r\n10"), answer
    assert app.received == [10]


@pytest.mark.parametrize("chunk", [1000, 1024 * 1024], ids=["small chunks", "large chunks"])
def test_a_chunked_upload_is_not_a_trailer(chunk):
    """The control for the trailer count: several MiB of chunked body pass, also when a read
    ends right after a chunk's size line (an open chunk without body yet, the state the
    trailer section is told by). MUTATION: count a read that only ends in that state - red
    (large chunks)."""
    app = _Reader()
    total = 4 * 1024 * 1024
    with serving(_door_config(app), door.door_event_loop) as (port, _server):
        with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
            s.sendall(b"POST / HTTP/1.1\r\nHost: door\r\nTransfer-Encoding: chunked\r\n"
                      b"Connection: close\r\n\r\n")
            if chunk == 1000:
                s.sendall((b"%x\r\n" % chunk + b"c" * chunk + b"\r\n") * (total // chunk))
                rest = total - (total // chunk) * chunk
                s.sendall(b"%x\r\n" % rest + b"c" * rest + b"\r\n0\r\n\r\n")
            else:
                s.sendall(b"%x\r\n" % chunk)
                for i in range(total // chunk):
                    time.sleep(0.05)
                    tail = b"%x\r\n" % chunk if i < total // chunk - 1 else b"0\r\n\r\n"
                    s.sendall(b"c" * chunk + b"\r\n" + tail)
            answer = _read_until_closed(s, 10.0)
    assert answer.startswith(b"HTTP/1.1 200"), answer[:80]
    assert app.received == [total]


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
                assert b"content-type: text/plain" in answer
                answer += _read_until_closed(s, 2.0)
                assert answer.endswith(b"\r\n\r\nRequest header too large"), answer[-80:]
                assert closed_within(s, 0.5)
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


def _wait_for_200(port, ctx=None, seconds=8.0):
    """Ask again until a GET answers 200; return the seconds it took, or None."""
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        try:
            with tls_connect(port, ctx, timeout=2) as s:
                if http_exchange(s, b"GET / HTTP/1.1\r\nHost: door\r\nConnection: close"
                                    b"\r\n\r\n", timeout=2).startswith(b"HTTP/1.1 200"):
                    return time.monotonic() - started
        except OSError:
            pass
        time.sleep(0.2)
    return None


def _wait_for_connections(server, count, seconds=5.0):
    deadline = time.monotonic() + seconds
    while len(server.server_state.connections) < count and time.monotonic() < deadline:
        time.sleep(0.02)


def test_an_idle_tls_connection_frees_its_place_at_the_head_timer(cert, monkeypatch):
    """The head timer drops a silent connection at once: a TLS close waits for the client to
    answer it, and until then the connection kept its place under the cap (measured: four
    idle connections at a cap of 4 turned new requests away until 45 s; on Python 3.10 with
    no limit at all). MUTATION: close instead of abort in _drop - red."""
    monkeypatch.setattr(door, "MAX_CONNECTIONS", 2)
    monkeypatch.setattr(door, "HEAD_TIMEOUT_S", 0.5)
    monkeypatch.setattr(door, "CLOSE_TIMEOUT_S", 30.0)
    monkeypatch.setattr(door, "SEND_TIMEOUT_S", 30.0)
    with serving(_door_config(_App(), cert), door.door_event_loop) as (port, server):
        idle = [tls_connect(port) for _ in range(2)]
        try:
            _wait_for_connections(server, 2)
            waited = _wait_for_200(port)
            assert waited is not None, "the idle connections kept their places"
        finally:
            for s in idle:
                s.close()


class _Large:
    """Answers 200 with a body of `size` bytes, in one part, as Starlette's Response does."""

    def __init__(self, size: int):
        self.size = size

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-length", b"%d" % self.size)]})
        await send({"type": "http.response.body", "body": b"x" * self.size})


def test_a_drained_close_waits_only_for_the_goodbye(cert, monkeypatch):
    """Once everything is sent, a closing connection waits CLOSE_TIMEOUT_S for the client to
    end it and is dropped then, on every Python version. Served on a plain loop, so asyncio's
    own TLS close limit is not what ends it: measured on Python 3.10.15 with uvicorn 0.48,
    two clients that never answered the TLS close held their places past 40 s. MUTATION: no
    drop once the answer is out - red."""
    monkeypatch.setattr(door, "MAX_CONNECTIONS", 3)
    monkeypatch.setattr(door, "CLOSE_TIMEOUT_S", 0.5)
    monkeypatch.setattr(door, "SEND_TIMEOUT_S", 30.0)
    head = b"GET / HTTP/1.1\r\nHost: door\r\nConnection: close\r\n\r\n"
    with serving(_door_config(_App(), cert), asyncio.new_event_loop) as (port, _server):
        silent = [tls_connect(port) for _ in range(2)]
        try:
            for s in silent:
                assert http_exchange(s, head).startswith(b"HTTP/1.1 200")
            # Neither client answers the close or closes its socket.
            assert _wait_for_200(port) is not None, "the closed connections kept their places"
        finally:
            for s in silent:
                s.close()


def test_a_close_that_stops_moving_is_dropped(monkeypatch):
    """A closing connection whose answer stops moving (the client reads nothing) is dropped
    after SEND_TIMEOUT_S without progress; a plain close waited for it for ever. MUTATION: no
    drop while bytes are unsent - red."""
    monkeypatch.setattr(door, "SEND_TIMEOUT_S", 0.5)
    monkeypatch.setattr(door, "CLOSE_TIMEOUT_S", 30.0)
    head = b"GET / HTTP/1.1\r\nHost: door\r\nConnection: close\r\n\r\n"
    with serving(_door_config(_Large(32 * 1024 * 1024)), asyncio.new_event_loop) as (port, server):
        reader = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        reader.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        reader.settimeout(5)
        try:
            reader.connect(("127.0.0.1", port))
            reader.sendall(head)
            _wait_for_connections(server, 1)
            started = time.monotonic()
            while server.server_state.connections and time.monotonic() - started < 4.0:
                time.sleep(0.05)
            assert not server.server_state.connections, "a close waited on a client that reads nothing"
        finally:
            reader.close()


@pytest.mark.parametrize("kind,keep", [("tls", True), ("plain", False)],
                         ids=["tls kept alive", "plain closed"])
def test_a_close_waits_while_the_answer_still_moves(cert, monkeypatch, kind, keep):
    """uvicorn closes as soon as an answer is handed to the transport, not when the client has
    it, and a close that ended a fixed time after it began cut an answer the client was still
    reading (measured: 65 to 89 % of a 4 MB one-piece answer read at 200 KB/s, plain and TLS,
    kept alive or closed, on Python 3.13 and 3.10.15). A close now waits while the unsent
    bytes shrink. Served on the door's own loop, so asyncio's TLS close limit is pinned too.
    MUTATION: a close limit counted from the close, the TCP transport under TLS left
    uncounted, or asyncio's TLS close limit at CLOSE_TIMEOUT_S - red."""
    # Linux reports a socket writable only once a large share of its buffer is free, so the
    # unsent bytes shrink in steps about a second apart at this pace.
    monkeypatch.setattr(door, "SEND_TIMEOUT_S", 3.0)
    monkeypatch.setattr(door, "CLOSE_TIMEOUT_S", 0.25)
    monkeypatch.setattr(door, "KEEP_ALIVE_S", 0.5)
    size = 4 * 1000 * 1000
    with serving(_door_config(_Large(size), cert if kind == "tls" else None),
                 door.door_event_loop) as (port, _server):
        if kind == "tls":
            s = tls_connect(port)
        else:
            s = socket.create_connection(("127.0.0.1", port), timeout=5)
        with s:
            s.sendall(b"GET / HTTP/1.1\r\nHost: door\r\n"
                      + (b"" if keep else b"Connection: close\r\n") + b"\r\n")
            s.settimeout(10)
            got, head = 0, b""
            while got < size:
                try:
                    part = s.recv(50000)
                except OSError:
                    break
                if not part:
                    break
                if not head:
                    head, _, part = part.partition(b"\r\n\r\n")
                got += len(part)
                time.sleep(0.05)   # about 1 MB/s
    assert got == size, f"{100 * got // size} % of the answer arrived"


class _NoLinger:
    def _linger(self, close_now):
        return False


def test_a_second_close_does_not_disarm_the_deadline(cert, monkeypatch):
    """On Python 3.11+ a second close() of a TLS transport drops its protocol, and abort() then
    does nothing (measured: closed twice, aborted after 1 s, still open after 20 s). The hook
    passes the first close on only, and the close watch still drops the connection.
    MUTATION: pass every close on - red on Python 3.11+."""
    import ssl as ssl_module
    monkeypatch.setattr(door, "CLOSE_TIMEOUT_S", 0.5)
    monkeypatch.setattr(door, "SEND_TIMEOUT_S", 30.0)
    lost = []
    owner = _NoLinger()

    class _Closer(asyncio.Protocol):
        def connection_made(self, transport):
            door._install_close_hook(transport, owner)
            transport.close()
            transport.close()

        def connection_lost(self, exc):
            lost.append(time.monotonic())

    async def _run():
        context = ssl_module.SSLContext(ssl_module.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, cert)
        server = await asyncio.get_running_loop().create_server(_Closer, "127.0.0.1", 0,
                                                                ssl=context)
        port = server.sockets[0].getsockname()[1]
        silent = await asyncio.to_thread(tls_connect, port)
        try:
            started = time.monotonic()
            while not lost and time.monotonic() - started < 3.0:
                await asyncio.sleep(0.05)
        finally:
            silent.close()
            server.close()
        return lost and lost[0] - started

    took = asyncio.run(_run())
    assert took and took < 2.0, "the close never ended"


class _Streaming:
    """Answers /slow with a stream of parts for up to 5 s and records when it learns that the
    connection is gone (what ends a relay's upstream request); answers anything else at once."""

    def __init__(self):
        self.told_after = None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        if scope["path"] != "/slow":
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-length", b"2")]})
            await send({"type": "http.response.body", "body": b"ok"})
            return
        started = time.monotonic()

        async def listen():
            while (await receive())["type"] != "http.disconnect":
                pass
            self.told_after = time.monotonic() - started

        listening = asyncio.ensure_future(listen())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        for _ in range(100):
            if listening.done():
                return
            await send({"type": "http.response.body", "body": b"x" * 100, "more_body": True})
            await asyncio.sleep(0.05)
        listening.cancel()
        await send({"type": "http.response.body", "body": b""})


def test_a_dropped_pipeline_tells_the_request_in_flight(cert, caplog):
    """Dropping a pipeline past PIPELINE_MAX tells the request still being answered that the
    connection is gone. uvicorn tells only the last request it parsed, so the one in flight
    kept running (its upstream request too) and its next write into the dropped connection
    logged a traceback per connection (measured: Python 3.13 and 3.10.15). MUTATION: drop
    without telling the requests being answered - red."""
    app = _Streaming()
    with caplog.at_level(logging.ERROR):
        with serving(_door_config(app, cert), door.door_event_loop) as (port, _server):
            with tls_connect(port) as s:
                s.sendall(b"GET /slow HTTP/1.1\r\nHost: door\r\n\r\n" + GET * 40)
                deadline = time.monotonic() + 3.0
                while app.told_after is None and time.monotonic() < deadline:
                    time.sleep(0.05)
            time.sleep(0.5)
    assert app.told_after is not None and app.told_after < 2.0, "the request in flight ran on"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], \
        [r.getMessage() for r in caplog.records]


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
                third.sendall(GET)
                answer = _read_until_closed(third)
                assert answer.startswith(b"HTTP/1.1 503"), answer[:80]
                assert b"connection: close" in answer.lower()
                # uvicorn's own answer at the cap carries a sentence a person can read.
                assert answer.endswith(b"\r\n\r\nService Unavailable"), answer[-80:]
        finally:
            for s in idle:
                s.close()
    assert app.calls == 1


UPGRADE = (b"GET /ws HTTP/1.1\r\nHost: door\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n"
           b"Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n")


def test_an_upgrade_at_the_cap_gets_503(monkeypatch):
    """uvicorn decides a WebSocket upgrade before it checks the cap, so sockets went past it
    (measured: 20 sockets at a cap of 3 while a request got 503), each holding a descriptor,
    three through the proxy. At the cap an upgrade gets the request's 503 now; below it, it
    opens. MUTATION: hand every upgrade to uvicorn - red."""
    monkeypatch.setattr(door, "MAX_CONNECTIONS", 2)
    opened = []

    async def sockets(scope, receive, send):
        if scope["type"] != "websocket":
            return
        await receive()
        opened.append(scope["path"])
        await send({"type": "websocket.accept"})
        while (await receive())["type"] != "websocket.disconnect":
            pass

    with serving(_door_config(sockets), door.door_event_loop) as (port, server):
        with socket.create_connection(("127.0.0.1", port), timeout=5) as alone:
            assert http_exchange(alone, UPGRADE).startswith(b"HTTP/1.1 101")
        deadline = time.monotonic() + 5
        while server.server_state.connections and time.monotonic() < deadline:
            time.sleep(0.02)
        idle = [socket.create_connection(("127.0.0.1", port), timeout=5) for _ in range(2)]
        try:
            while len(server.server_state.connections) < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
            with socket.create_connection(("127.0.0.1", port), timeout=5) as third:
                third.sendall(UPGRADE)
                answer = _read_until_closed(third)
        finally:
            for s in idle:
                s.close()
    assert answer.startswith(b"HTTP/1.1 503"), answer[:80]
    assert b"connection: close" in answer and answer.endswith(b"Service Unavailable")
    assert opened == ["/ws"]


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
    from vaf.network import ssl_utils as _ssl_utils
    monkeypatch.setattr(_ssl_utils, "_LISTENERS", {})
    proxy.run_https_proxy("127.0.0.1", 18443, "cert.pem", "key.pem")
    _assert_a_door(seen)
    assert seen["config"].ssl_ciphers == SERVER_TLS12_CIPHERS
    assert seen["config"].ssl_certfile == "cert.pem"

    import vaf.core.web_server as web_server
    from vaf.core.config import Config
    monkeypatch.setenv("VAF_WEBUI_ACTIVE", "0")      # run_server sets it; restored after
    monkeypatch.setattr(web_server.manager, "set_server_loop", lambda _loop: None)
    from vaf.network import ssl_utils
    # The pair comes from ensure_ssl_certificates(); Config.load is never reached, so the
    # files are never read.
    monkeypatch.setattr(ssl_utils, "ensure_ssl_certificates",
                        lambda: (str(tmp_path / "cert.pem"), str(tmp_path / "key.pem")))
    monkeypatch.setattr(ssl_utils, "_LISTENERS", {})
    for tls in (False, True):
        seen.pop("config", None)
        seen.pop("loop", None)
        values = {"local_network_tls_enabled": tls}
        monkeypatch.setattr(Config, "get", staticmethod(
            lambda key, default=None, _v=values: _v.get(key, default)))
        web_server.run_server("127.0.0.1", 18001)
        _assert_a_door(seen)
        if tls:
            assert seen["config"].ssl_ciphers == SERVER_TLS12_CIPHERS


# --------------------------------------------------------------------------- the pin

def test_the_uvicorn_hooks_exist():
    """AC 18: the protocol overrides asyncio's protocol methods, httptools' parser callbacks
    and handle_websocket_upgrade of uvicorn's HttpToolsProtocol, which must still exist under
    these names, and reads the state uvicorn keeps under these names; uvicorn must still take a
    protocol class. The two chunk callbacks are httptools' own, which uvicorn does not define:
    if it ever does, the door's would hide them. A refused head drops the gathered headers so
    uvicorn's upgrade check (which reads self.headers) cannot start a WebSocket. MUTATION:
    rename one override (on_headers instead of on_header) - red here and in the 431 test."""
    import inspect

    from uvicorn.protocols.http.httptools_impl import HttpToolsProtocol, RequestResponseCycle
    hooks = ("connection_made", "connection_lost", "data_received", "eof_received",
             "on_message_begin", "on_url", "on_header", "on_headers_complete", "on_body",
             "on_message_complete", "handle_websocket_upgrade", "on_response_complete")
    httptools_only = ("on_chunk_header", "on_chunk_complete")
    for name in hooks:
        assert name in vars(HttpToolsProtocol), f"uvicorn no longer defines {name}"
        assert name in vars(door.BoundedHttpToolsProtocol), f"the door does not override {name}"
    for name in httptools_only:
        assert not hasattr(HttpToolsProtocol, name), f"uvicorn defines {name} now"
        assert name in vars(door.BoundedHttpToolsProtocol), f"the door does not define {name}"
    overrides = {n for n, v in vars(door.BoundedHttpToolsProtocol).items()
                 if callable(v) and not n.startswith("_")}
    assert overrides <= set(hooks + httptools_only), overrides - set(hooks + httptools_only)
    assert "self.headers" in inspect.getsource(HttpToolsProtocol._get_upgrade)
    state = inspect.getsource(HttpToolsProtocol.__init__)
    for name in ("self.cycle", "self.connections", "self.tasks", "self.limit_concurrency",
                 "self.logger", "self.pipeline", "self.flow"):
        assert name in state, f"uvicorn no longer keeps {name}"
    cycle = inspect.getsource(RequestResponseCycle.__init__)
    for name in ("self.response_started", "self.response_complete", "self.disconnected",
                 "self.message_event"):
        assert name in cycle, f"uvicorn's answer cycle no longer keeps {name}"
    config = uvicorn.Config(_App(), http=door.BoundedHttpToolsProtocol, log_config=None)
    config.load()
    assert config.http_protocol_class is door.BoundedHttpToolsProtocol


def test_httptools_reports_what_the_door_counts_by():
    """What the head timer and the trailer count rely on, read from the httptools CI
    installs: an empty line between requests reaches no callback (so nothing but the door's
    own timer notices it), the last chunk reports a chunk header with no body, and its
    trailer lines arrive as headers before the chunk completes."""
    import httptools

    class _Recorder:
        def __init__(self):
            self.events = []

        def on_message_begin(self):
            self.events.append("begin")

        def on_header(self, name, value):
            self.events.append(("header", name))

        def on_headers_complete(self):
            self.events.append("headers")

        def on_body(self, body):
            self.events.append(("body", len(body)))

        def on_message_complete(self):
            self.events.append("complete")

        def on_chunk_header(self):
            self.events.append("chunk")

        def on_chunk_complete(self):
            self.events.append("chunk done")

    rec = _Recorder()
    parser = httptools.HttpRequestParser(rec)
    parser.feed_data(GET)
    rec.events.clear()
    parser.feed_data(b"\r\n")
    assert rec.events == []
    parser.feed_data(b"POST / HTTP/1.1\r\nHost: door\r\nTransfer-Encoding: chunked\r\n\r\n"
                     b"5\r\nhello\r\n0\r\nX-T: ")
    assert rec.events ==["begin", ("header", b"Host"), ("header", b"Transfer-Encoding"),
                          "headers", "chunk", ("body", 5), "chunk done", "chunk"]
    rec.events.clear()
    parser.feed_data(b"a" * 100)
    assert rec.events == []
    parser.feed_data(b"\r\n\r\n")
    assert rec.events == [("header", b"X-T"), "chunk done", "complete"]


def test_the_door_reads_no_client_address():
    """The protocol never reads the peer: binding.connection_client_ip is the only reader
    (tests/test_client_address_has_one_reader.py walks vaf/ for the shapes; this pins the
    attribute uvicorn keeps the peer in)."""
    source = (ROOT / "vaf" / "network" / "door.py").read_bytes().decode("utf-8")
    assert "self.client" not in source and "peername" not in source
