# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Real uvicorn listeners for the tests of the HTTPS door (vaf/network/door.py,
vaf/network/https_proxy.py).

`serving(config, loop_factory)` runs a uvicorn server in a thread of its own, on an event loop
from `loop_factory` (the door's loop, or a plain one for a control), on a socket bound to a
free port on 127.0.0.1 before the server starts (no port race), and yields the port and the
server. Nothing here dials 3000, 8001, 8005 or 8443, where a running VAF may listen.

`tls_client()` is a client context that offers what a test asks for: a cipher list (with
`@SECLEVEL=0`, so OpenSSL lets it offer the old suites a door must refuse) and a version
range. It verifies nothing; the tests are about what the server accepts.
"""
from __future__ import annotations

import asyncio
import contextlib
import socket
import ssl
import threading
import time
from typing import Callable, Iterator, Optional, Tuple


@contextlib.contextmanager
def serving(config, loop_factory: Callable[[], asyncio.AbstractEventLoop] = asyncio.new_event_loop
            ) -> Iterator[Tuple[int, object]]:
    import uvicorn
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(config)
    loop = loop_factory()
    failure = []

    def _run():
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(server.serve(sockets=[sock]))
        except BaseException as exc:  # surfaced below, the thread must not die silently
            failure.append(exc)
        finally:
            loop.close()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and not failure and time.monotonic() < deadline:
        time.sleep(0.02)
    if failure:
        raise failure[0]
    assert server.started, "the test server did not start"
    try:
        yield port, server
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()


def tls_client(ciphers: Optional[str] = None, *, minimum=None, maximum=None) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    if ciphers:
        ctx.set_ciphers(ciphers + ":@SECLEVEL=0")
    if minimum is not None:
        ctx.minimum_version = minimum
    if maximum is not None:
        ctx.maximum_version = maximum
    return ctx


def tls_connect(port: int, ctx: Optional[ssl.SSLContext] = None, timeout: float = 5.0) -> ssl.SSLSocket:
    raw = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        return (ctx or tls_client()).wrap_socket(raw)
    except BaseException:
        raw.close()
        raise


def closed_within(sock: socket.socket, seconds: float) -> bool:
    """Whether the server closes `sock` within `seconds` (reads until EOF, a reset, or the
    time is up; data the server sends before closing is read and ignored)."""
    deadline = time.monotonic() + seconds
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return False
        sock.settimeout(left)
        try:
            if sock.recv(4096) == b"":
                return True
        except socket.timeout:
            return False
        except (ConnectionError, ssl.SSLError, OSError):
            return True


def http_exchange(sock: socket.socket, head: bytes, timeout: float = 5.0) -> bytes:
    """Send a request head on `sock` and read one response head (up to the blank line)."""
    sock.sendall(head)
    sock.settimeout(timeout)
    data = b""
    while b"\r\n\r\n" not in data:
        part = sock.recv(65536)
        if not part:
            break
        data += part
    return data
