# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The integrated HTTPS proxy streams in both directions and frames each response once.

Measured before (scratch probe, the real proxy in front of a mock upstream): a live stream of
six events over 3 s arrived through the proxy in one piece after 3.01 s (its first event was
ready at once); every response carried two Content-Length, two Date and two Server headers,
HEAD with Content-Length 17 and 0, a compressed answer 40 and 5000 and no body at all; two
Set-Cookie arrived joined into one; a 150 MB body was read whole into memory before the
upstream saw a byte; and a WebSocket the backend refused opened on the client and closed
with 1000.

Every test runs the real create_proxy_app() on a door listener (door_options() and
door_event_loop()) on a free loopback port, with both origins pointed at a mock upstream on
another free port (tests/door_harness.py); nothing dials 3000, 8005 or 8443.
"""
from __future__ import annotations

import asyncio
import gzip
import socket
import time

import pytest
import uvicorn

from door_harness import http_exchange, serving
from vaf.network import door
import vaf.network.https_proxy as proxy

SMALL = b"hello from server"          # 17 bytes
PACKED = gzip.compress(b"x" * 5000)


class _Upstream:
    """The mock upstream: answers by path and records every request it saw."""

    def __init__(self):
        self.requests = []

    async def __call__(self, scope, receive, send):
        path = scope["path"]
        if scope["type"] == "websocket":
            if path == "/ws":
                await send({"type": "websocket.close", "code": 4001})   # refused before accept
                return
            await receive()
            await send({"type": "websocket.accept"})
            message = await receive()
            await send({"type": "websocket.send", "text": "echo:" + (message.get("text") or "")})
            await send({"type": "websocket.close", "code": 1000})
            return
        if scope["type"] != "http":
            return
        record = {"path": path, "method": scope["method"],
                  "headers": [(k.decode(), v.decode("latin-1")) for k, v in scope["headers"]],
                  "chunks": [], "ended": False, "disconnected": False}
        self.requests.append(record)

        async def reply(status, body=b"", headers=()):
            await send({"type": "http.response.start", "status": status, "headers": list(headers)})
            await send({"type": "http.response.body", "body": body})

        if path == "/sse":
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/event-stream")]})
            for i in range(5):
                await send({"type": "http.response.body", "body": b"data: %d\n\n" % i,
                            "more_body": True})
                await asyncio.sleep(0.5)
            await send({"type": "http.response.body", "body": b""})
            return
        if path == "/small":
            return await reply(200, SMALL, [(b"content-length", b"17"),
                                            (b"x-powered-by", b"Next.js")])
        if path == "/cookies":
            return await reply(200, b"ok", [(b"set-cookie", b"a=1"), (b"set-cookie", b"b=2"),
                                            (b"content-length", b"2")])
        if path == "/packed":
            if "gzip" in dict(record["headers"]).get("accept-encoding", ""):
                return await reply(200, PACKED, [(b"content-encoding", b"gzip"),
                                                 (b"content-length", str(len(PACKED)).encode())])
            return await reply(200, b"x" * 5000, [(b"content-length", b"5000")])
        # Anything else reads the whole body, recording when each part arrived.
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                record["disconnected"] = True
                return
            if message.get("body"):
                record["chunks"].append((time.time(), len(message["body"])))
            if not message.get("more_body"):
                break
        record["ended"] = True
        await reply(200, b"done", [(b"content-length", b"4")])

    def body_bytes(self, record, before=None):
        return sum(n for t, n in record["chunks"] if before is None or t < before)


@pytest.fixture
def relay(monkeypatch):
    """(proxy port, upstream): the real proxy app on a door listener, both origins pointed at
    the mock upstream."""
    upstream = _Upstream()
    monkeypatch.setattr(proxy, "_frontend_client", None)
    monkeypatch.setattr(proxy, "_backend_client", None)
    with serving(uvicorn.Config(upstream, lifespan="off", log_config=None)) as (up_port, _s):
        origin = f"http://127.0.0.1:{up_port}"
        monkeypatch.setattr(proxy, "FRONTEND_ORIGIN", origin)
        monkeypatch.setattr(proxy, "BACKEND_ORIGIN", origin)
        config = uvicorn.Config(proxy.create_proxy_app(), lifespan="on", log_config=None,
                                **door.door_options())
        with serving(config, door.door_event_loop) as (port, _server):
            yield port, upstream


def _connect(port):
    return socket.create_connection(("127.0.0.1", port), timeout=5)


def _response(sock, timeout=5.0):
    """Read one whole response (the request said Connection: close)."""
    sock.settimeout(timeout)
    data = b""
    while True:
        part = sock.recv(65536)
        if not part:
            break
        data += part
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    headers = [tuple(line.split(": ", 1)) for line in lines[1:] if ": " in line]
    return lines[0], [(k.lower(), v) for k, v in headers], body


def _request(port, head: bytes, body: bytes = b""):
    with _connect(port) as s:
        s.sendall(head + body)
        return _response(s)


def _values(headers, name):
    return [v for k, v in headers if k == name]


# --------------------------------------------------------------------------- responses

def test_a_live_stream_arrives_live(relay):
    """AC 10: the first event of a stream reaches the device at once, not when the stream
    ends. MUTATION: read the whole upstream response before answering - red."""
    port, _upstream = relay
    with _connect(port) as s:
        started = time.monotonic()
        s.sendall(b"GET /sse HTTP/1.1\r\nHost: door\r\nConnection: close\r\n\r\n")
        data = b""
        while b"data: 0" not in data:
            part = s.recv(4096)
            assert part, "the stream ended before its first event"
            data += part
        assert time.monotonic() - started < 0.4


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_one_framing_per_response(relay, method):
    """AC 11: one Content-Length (the upstream's, also for HEAD), one Date, no Server, no
    X-Powered-By. MUTATION: append a recounted Content-Length, or keep the upstream's Date
    and Server - red."""
    port, _upstream = relay
    status, headers, body = _request(
        port, f"{method} /small HTTP/1.1\r\nHost: door\r\nConnection: close\r\n\r\n".encode())
    assert status.startswith("HTTP/1.1 200")
    assert _values(headers, "content-length") == ["17"]
    assert len(_values(headers, "date")) == 1
    assert _values(headers, "server") == [] and _values(headers, "x-powered-by") == []
    assert body == (SMALL if method == "GET" else b"")


def test_encoding_passes_through_and_is_never_unasked(relay):
    """AC 12: a client that asked for no encoding reaches the upstream with identity (httpx
    asked for gzip, deflate, br and zstd on its behalf); a client that asked for gzip gets
    the upstream's compressed bytes with its Content-Encoding and Content-Length. MUTATION:
    relay decoded bytes, or drop the identity default - red."""
    port, upstream = relay
    _request(port, b"GET /packed HTTP/1.1\r\nHost: door\r\nConnection: close\r\n\r\n")
    assert dict(upstream.requests[-1]["headers"])["accept-encoding"] == "identity"
    status, headers, body = _request(
        port, b"GET /packed HTTP/1.1\r\nHost: door\r\nAccept-Encoding: gzip\r\n"
              b"Connection: close\r\n\r\n")
    assert status.startswith("HTTP/1.1 200")
    assert _values(headers, "content-encoding") == ["gzip"]
    assert _values(headers, "content-length") == [str(len(PACKED))]
    assert body == PACKED


def test_repeated_headers_stay_repeated(relay):
    """AC 13: two Set-Cookie arrive as two. MUTATION: a dict of the upstream headers - red."""
    port, _upstream = relay
    _status, headers, _body = _request(
        port, b"GET /cookies HTTP/1.1\r\nHost: door\r\nConnection: close\r\n\r\n")
    assert _values(headers, "set-cookie") == ["a=1", "b=2"]


def test_the_upstream_is_released_when_the_device_leaves_first():
    """A device that leaves before the first part of the answer is read: the upstream
    response is still closed, so its pooled connection is free again. Closing it in the body
    iterator alone missed this (an iterator that never started runs no cleanup). MUTATION:
    close the upstream only in the body iterator - red."""
    closed = []

    class _Upstream:
        status_code = 200

        async def aiter_raw(self):
            yield b"never read"

        async def aclose(self):
            closed.append(True)

    async def _receive():
        return {"type": "http.disconnect"}

    async def _send(_message):
        await asyncio.sleep(0)

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}}
    asyncio.run(proxy._RelayedResponse(_Upstream(), [])(scope, _receive, _send))
    assert closed and all(closed)


# --------------------------------------------------------------------------- request bodies

def test_the_body_streams_to_the_upstream(relay):
    """AC 14: a 3 MiB body with Content-Length reaches the upstream with that header, no
    Transfer-Encoding and every byte, and its first half arrives before the device sends
    the second. MUTATION: read the whole body before sending it upstream - red."""
    port, upstream = relay
    half = b"a" * (3 * 1024 * 1024 // 2)
    with _connect(port) as s:
        s.sendall(b"POST /upload HTTP/1.1\r\nHost: door\r\nConnection: close\r\n"
                  b"Content-Length: %d\r\n\r\n" % (2 * len(half)) + half)
        time.sleep(1.0)
        second_sent = time.time()
        s.sendall(half)
        status, _headers, _body = _response(s)
    assert status.startswith("HTTP/1.1 200")
    record = upstream.requests[-1]
    headers = dict(record["headers"])
    assert headers["content-length"] == str(2 * len(half)) and "transfer-encoding" not in headers
    assert upstream.body_bytes(record) == 2 * len(half) and record["ended"]
    assert upstream.body_bytes(record, before=second_sent) == len(half)


def test_a_get_carries_no_body(relay):
    """AC 14: a GET reaches the upstream with no Transfer-Encoding and no body. MUTATION:
    always pass the client's stream - red (httpx sends it chunked)."""
    port, upstream = relay
    _request(port, b"GET /upload HTTP/1.1\r\nHost: door\r\nConnection: close\r\n\r\n")
    record = upstream.requests[-1]
    assert "transfer-encoding" not in dict(record["headers"])
    assert upstream.body_bytes(record) == 0


def test_a_body_over_the_cap_gets_413(relay, monkeypatch):
    """AC 15: a declared length over BODY_MAX_BYTES is refused before a byte is read, and the
    upstream never sees the request; a chunked body is counted and refused as it passes the
    cap. MUTATION: drop the Content-Length check (the upstream sees a request), or the byte
    count (the chunked body passes) - red."""
    monkeypatch.setattr(door, "BODY_MAX_BYTES", 1024 * 1024)
    monkeypatch.setattr(door, "BODY_STALL_S", 3.0)
    port, upstream = relay
    with _connect(port) as s:
        answer = http_exchange(s, b"POST /upload HTTP/1.1\r\nHost: door\r\n"
                                  b"Content-Length: %d\r\n\r\n" % (2 * 1024 * 1024))
    assert answer.startswith(b"HTTP/1.1 413"), answer[:80]
    assert b"connection: close" in answer.lower()
    assert upstream.requests == []

    # Exactly the cap first, relayed in full; then one byte more. Bytes the door has not read
    # when it closes turn its close into a reset that hides the answer, so the byte that
    # crosses the cap comes alone.
    part = b"b" * 65536
    with _connect(port) as s:
        s.sendall(b"POST /upload HTTP/1.1\r\nHost: door\r\nTransfer-Encoding: chunked\r\n\r\n"
                  + (b"%x\r\n" % len(part) + part + b"\r\n") * 16)
        deadline = time.monotonic() + 5
        while (not upstream.requests or upstream.body_bytes(upstream.requests[-1]) < 1024 * 1024)\
                and time.monotonic() < deadline:
            time.sleep(0.02)
        assert upstream.body_bytes(upstream.requests[-1]) == 1024 * 1024
        s.sendall(b"1\r\nb\r\n")
        status, headers, _body = _response(s)
    assert status.startswith("HTTP/1.1 413"), status
    assert _values(headers, "connection") == ["close"]


def test_a_stalled_body_gets_408(relay, monkeypatch):
    """AC 16: a body that sends nothing for BODY_STALL_S gets 408 and the upstream request
    ends. MUTATION: no wait limit per part - red (the read times out instead)."""
    monkeypatch.setattr(door, "BODY_STALL_S", 0.5)
    port, upstream = relay
    with _connect(port) as s:
        started = time.monotonic()
        s.sendall(b"POST /upload HTTP/1.1\r\nHost: door\r\nContent-Length: 10\r\n\r\nabcde")
        status, headers, _body = _response(s, timeout=5.0)
        assert time.monotonic() - started < 2.0
    assert status.startswith("HTTP/1.1 408"), status
    assert _values(headers, "connection") == ["close"]
    deadline = time.monotonic() + 2
    while not upstream.requests[-1]["disconnected"] and time.monotonic() < deadline:
        time.sleep(0.02)
    assert upstream.requests[-1]["disconnected"] and not upstream.requests[-1]["ended"]


def test_the_pools_match_the_door(monkeypatch):
    """AC 17: both pooled clients hold as many connections as the door serves: a streamed
    response holds its upstream connection while the device reads, and a smaller pool let a
    few slow readers starve every other request. MUTATION: max_connections=50 - red."""
    import httpx
    made = []

    class _Recording(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            made.append(kwargs.get("limits"))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _Recording)
    monkeypatch.setattr(door, "MAX_CONNECTIONS", 77)
    monkeypatch.setattr(proxy, "_frontend_client", None)
    monkeypatch.setattr(proxy, "_backend_client", None)

    async def _both():
        clients = [await proxy._get_client(proxy.FRONTEND_ORIGIN),
                   await proxy._get_client(proxy.BACKEND_ORIGIN)]
        for client in clients:
            await client.aclose()
        return clients

    clients = asyncio.run(_both())
    assert clients[0] is not clients[1]
    assert [limits.max_connections for limits in made] == [door.connection_cap()] * 2 == [77, 77]
