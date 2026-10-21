# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
The door: what a listener other devices reach holds to, before any HTTP is judged.

VAF serves other devices through two listeners: the integrated HTTPS proxy
(``vaf.network.https_proxy``, the access port) and ``vaf run --web``, whose backend binds
``0.0.0.0:8001`` in network mode and has no proxy in front. Both are uvicorn servers, and
uvicorn on its own holds a slow client open for as long as it likes. Measured on the proxy
with the settings it used to run with:

- An idle connection after the TLS handshake, a request head sent one byte every few
  seconds and a TCP connection that never starts TLS were all still open after 25 s
  (uvicorn arms no timer before the first request; asyncio's own TLS handshake limit is
  60 s). A systemd service starts with 1024 open files, so about 800 such connections
  starved the whole process: the proxy, the backend and its database pool.
- A request head of 1 MB, or 2000 headers, was answered 200 and relayed (httptools has no
  head limit; uvicorn has one for h11 only).
- No connection limit.
- uvicorn's forwarded-header handling trusted the addresses in ``FORWARDED_ALLOW_IPS``:
  with ``*`` set, a device on the network that sent ``X-Forwarded-For: 127.0.0.1`` reached
  the backend as this machine, the tokenless owner.

What a door gets here, all in the protocol and the event loop, so it applies to every
connection, admitted or not, before the admission check reads a single header:

- ``door_event_loop()``: the TLS handshake has ``TLS_HANDSHAKE_TIMEOUT_S``.
- ``BoundedHttpToolsProtocol``: the request head has ``HEAD_TIMEOUT_S`` from the moment the
  connection is ready, or from the first byte that arrives while no request is being
  answered (a follow-up request, a stray empty line, the rest of a body after an early
  answer), and at most ``HEAD_MAX_BYTES`` together with the trailers after a chunked body;
  above it the answer is 431.
- ``door_options()``: those plus at most ``connection_cap()`` connections (503 above it,
  for a WebSocket upgrade as for a request), an explicit keep-alive, no ``Server`` header,
  and no forwarded-header trust of uvicorn's: ``binding.connection_client_ip`` is the only
  reader of ``X-Forwarded-For`` on a door.
- ``raise_open_file_limit()``: the process raises its own soft open-file limit.

The request body has its limits where it is read: the proxy streams it and holds it to
``BODY_MAX_BYTES`` and ``BODY_STALL_S`` (``https_proxy._forward_http``). A refused client
keeps no connection: the admission check's 403 says ``Connection: close``
(``admission.AdmissionGuard``).

Named boundaries:
- The TLS handshake happens before any limit but its own can apply, and a connection in
  its handshake counts toward no cap: it holds one descriptor for up to
  ``TLS_HANDSHAKE_TIMEOUT_S`` (measured: 60 connections that never started TLS, 60 more
  descriptors, released at 15 s). The spare descriptors divided by 15 s is the rate of new
  connections that takes them all: with the soft limit at 65536, about 4,200 a second,
  sustained; where it stays at 1024 (cap 256, about 200 descriptors at rest), between about
  3 and 37 a second, by how many of the door's connections are relayed. Only a firewall
  rate limit in front of the port closes that.
- One address can fill the connection cap; a per-address count would need a second
  reader of the peer (``tests/test_client_address_has_one_reader.py``) and bookkeeping
  across the WebSocket upgrade, and no incident asks for it.
- A client that reads a response slowly, or never, holds one door connection (and on the
  proxy one upstream connection) for as long as it keeps the connection open: uvicorn has
  no write timeout (measured: five clients that never read a streamed answer still held
  both at 70 s). A body sent a little at a time, each part inside ``BODY_STALL_S``, does the
  same while it lasts. The cap bounds how many.
- ``vaf run --web`` serves the backend itself: there the head, handshake and connection
  limits apply to every client, the owner's own web UI included, which reaches it through
  Next.js; a flood that fills the cap turns that UI away too. A body is held only by the
  route that reads it.
- Refusals here (431, a closed head, 503) never reach the access log or the security
  log, because a flood must not become a write flood. uvicorn still logs its own
  one-line warnings, and the door logs uvicorn's line for an upgrade it turns away.
- The protocol subclasses uvicorn's ``HttpToolsProtocol`` and overrides only asyncio's
  protocol methods, httptools' parser callbacks and ``handle_websocket_upgrade``; it reads
  ``cycle``, ``connections``, ``tasks`` and ``limit_concurrency``. All of them are the same
  from uvicorn 0.27 to 0.54, and httptools 0.8 and 0.9 call the chunk callbacks the trailer
  count needs; ``tests/test_network_door.py`` pins them, and CI runs against the newest
  uvicorn ``requirements.txt`` resolves.

The limits are constants, not settings: they are the floor of the door, not a preference,
and a config key's name is a security decision of its own.

Needs the server extra (uvicorn with httptools); ``vaf.network`` does not import it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from uvicorn.protocols.http.httptools_impl import HttpToolsProtocol

logger = logging.getLogger(__name__)

# asyncio's own default is 60 s, and uvicorn passes none.
TLS_HANDSHAKE_TIMEOUT_S = 15.0
# From the connection being ready (after the TLS handshake), or from the first byte of a
# follow-up request on a kept-alive connection, until the head is complete.
HEAD_TIMEOUT_S = 15.0
# Node's default (http.maxHeaderSize), which already refuses larger heads for every page
# the proxy relays from the frontend.
HEAD_MAX_BYTES = 16 * 1024
# uvicorn's default, written down so it cannot drift.
KEEP_ALIVE_S = 5
# A household behind one VPN with every tab, socket and live stream fits many times over.
MAX_CONNECTIONS = 512
# More than twice the largest body VAF accepts over HTTP: a Front Office document of 40 MiB
# sent as base64 JSON, about 54 MB.
BODY_MAX_BYTES = 128 * 1024 * 1024
# The proxy's upstream timeout: a request body that sends nothing for this long ends.
BODY_STALL_S = 60.0
# Where a hard limit is unlimited or high, the soft limit is raised to this.
OPEN_FILES_TARGET = 65536
# macOS refuses a soft limit above OPEN_MAX (10240) when the hard limit is unlimited.
OPEN_FILES_FALLBACK = 10240


def _plain_answer(status_line: bytes, text: bytes) -> bytes:
    """A whole answer written by the protocol itself, with a short sentence a person can read
    and the connection closed."""
    return (status_line + b"content-type: text/plain; charset=utf-8\r\n"
            + b"content-length: %d\r\nconnection: close\r\n\r\n" % len(text) + text)


_HEADER_TOO_LARGE = _plain_answer(b"HTTP/1.1 431 Request Header Fields Too Large\r\n",
                                  b"Request header too large")
# The same answer uvicorn gives a request at the cap (flow_control.service_unavailable).
_SERVICE_UNAVAILABLE = _plain_answer(b"HTTP/1.1 503 Service Unavailable\r\n",
                                     b"Service Unavailable")

_limit_logged = False
_cap_warned = False


class BoundedHttpToolsProtocol(HttpToolsProtocol):
    """uvicorn's httptools protocol with a time and a size limit on every request head, and
    a WebSocket upgrade held to the connection cap like any request.

    Overrides only asyncio protocol methods, httptools parser callbacks and uvicorn's
    ``handle_websocket_upgrade``, always hands on to uvicorn's own, and never reads the client
    address.

    The time: the head timer runs from the ready connection, and from the first byte that
    arrives while no request is being answered: the next request on a kept-alive connection,
    an empty line (llhttp skips one between requests without ``on_message_begin``), or the
    rest of a body after an early answer. uvicorn's own ``data_received`` cancels its
    keep-alive timer on any byte and re-arms it only after the next answer, so one empty line
    used to hold a kept-alive connection forever (measured: open after 45 s, after a 200 or a
    refused request alike).

    The size, per request: the head and the trailer section after a chunked body together.
    They are counted twice: in the parser callbacks (the URL and each complete header or
    trailer line, so a large head that arrives in one read is caught), and as raw bytes of
    every read that lies wholly inside an unfinished head or trailer section (httptools
    gathers a header line until it ends before the callback sees it, so a line that never
    ends grew without a bound: a trailer line after the last chunk took the process from 50 MB
    to about 1 GB in 48 s, admitted or not). A read that also carries the end of an earlier
    request (a pipelined request, the rest of a body) is not counted as raw bytes, so a body
    never turns into a 431; the line it leaves unfinished is counted from the next read on,
    which bounds what is gathered to one read plus ``HEAD_MAX_BYTES``.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._door_transport: Optional[asyncio.BaseTransport] = None
        self._door_closed = False
        self._head_timer: Optional[asyncio.TimerHandle] = None
        self._head_pending = False
        self._head_counted = 0
        self._head_received = 0
        # Where the parser is: requests begun on this connection and whether the last one is
        # still open; chunks begun, and whether the open one has delivered body bytes. The
        # last chunk has none: a read that begins and ends inside one open chunk without
        # body is trailer section.
        self._messages = 0
        self._message_open = False
        self._chunks = 0
        self._chunk_open = False
        self._chunk_body = False

    # -- asyncio protocol

    def connection_made(self, transport) -> None:  # type: ignore[override]
        super().connection_made(transport)
        self._door_transport = transport
        self._begin_head()
        self._arm_head_timer()

    def connection_lost(self, exc) -> None:
        self._cancel_head_timer()
        super().connection_lost(exc)

    def data_received(self, data: bytes) -> None:
        if self._door_closed:
            return
        if self._head_timer is None and (self.cycle is None or self.cycle.response_complete):
            # Nothing is being answered: whatever these bytes are, the head timer holds them.
            self._arm_head_timer()
        messages, chunks, between = self._messages, self._chunks, not self._message_open
        super().data_received(data)
        if self._door_closed:
            return
        if self._head_pending:
            # The whole read is head when the head began before it, or when it began in this
            # read with no earlier request open (only empty lines can precede it then).
            whole = self._messages == messages or (between and self._messages == messages + 1)
        else:
            whole = (self._chunk_open and not self._chunk_body
                     and (self._messages, self._chunks) == (messages, chunks))
        if whole:
            self._head_received += len(data)
            if self._head_received > HEAD_MAX_BYTES:
                self._refuse_head()

    # -- uvicorn

    def handle_websocket_upgrade(self) -> None:
        """At the connection cap, a WebSocket upgrade gets the 503 a request gets. uvicorn
        decides the upgrade before it checks the cap, so sockets went past it (measured: 20
        sockets at a cap of 3, while a request got 503); each holds a descriptor, three
        through the proxy, as long as it stays open."""
        if self.limit_concurrency is not None and (
                len(self.connections) >= self.limit_concurrency
                or len(self.tasks) >= self.limit_concurrency):
            self.logger.warning("Exceeded concurrency limit.")
            self._door_closed = True
            transport = self._door_transport
            if transport is not None and not transport.is_closing():
                transport.write(_SERVICE_UNAVAILABLE)
                transport.close()
            return
        super().handle_websocket_upgrade()

    # -- httptools parser callbacks

    def on_message_begin(self) -> None:
        if self._door_closed:
            return
        self._messages += 1
        self._message_open = True
        self._chunk_open = False
        self._begin_head()
        if self._head_timer is None:
            self._arm_head_timer()
        super().on_message_begin()

    def on_url(self, url: bytes) -> None:
        if self._door_closed:
            return
        self._head_counted += len(url)
        if self._head_counted > HEAD_MAX_BYTES:
            self._refuse_head()
            return
        super().on_url(url)

    def on_header(self, name: bytes, value: bytes) -> None:
        if self._door_closed:
            return
        # The separators of a header line: ": " and CRLF.
        self._head_counted += len(name) + len(value) + 4
        if self._head_counted > HEAD_MAX_BYTES:
            self._refuse_head()
            return
        super().on_header(name, value)

    def on_headers_complete(self) -> None:
        self._cancel_head_timer()
        if self._door_closed:
            return
        self._head_pending = False
        super().on_headers_complete()

    def on_body(self, body: bytes) -> None:
        self._chunk_body = True
        if self._door_closed:
            return
        super().on_body(body)

    def on_message_complete(self) -> None:
        self._message_open = False
        self._chunk_open = False
        if self._door_closed:
            return
        super().on_message_complete()

    # httptools calls these when the protocol has them; uvicorn's has none.

    def on_chunk_header(self) -> None:
        self._chunks += 1
        self._chunk_open = True
        self._chunk_body = False

    def on_chunk_complete(self) -> None:
        self._chunk_open = False

    # -- the limits

    def _begin_head(self) -> None:
        self._head_pending = True
        self._head_counted = 0
        self._head_received = 0

    def _arm_head_timer(self) -> None:
        self._head_timer = asyncio.get_running_loop().call_later(HEAD_TIMEOUT_S,
                                                                 self._head_timed_out)

    def _cancel_head_timer(self) -> None:
        if self._head_timer is not None:
            self._head_timer.cancel()
            self._head_timer = None

    def _head_timed_out(self) -> None:
        """No complete head in time: the connection closes without an answer."""
        self._head_timer = None
        self._door_closed = True
        transport = self._door_transport
        if transport is not None and not transport.is_closing():
            transport.close()

    def _refuse_head(self) -> None:
        """431 and close, for a head or a trailer section above the limit. The parser may
        still run to the end of the current read; every callback is a no-op from here, and the
        head gathered so far is dropped, so an upgrade header in it cannot start a WebSocket on
        a closed connection.

        The 431 is written only where it answers the request it refuses: never after or in
        the middle of another answer (an earlier request still being answered, or trailers
        after the request's own answer began); the connection just closes then."""
        self._cancel_head_timer()
        self._door_closed = True
        self.headers = []
        transport = self._door_transport
        if transport is not None and not transport.is_closing():
            cycle = self.cycle
            if self._head_pending:
                answers = cycle is None or cycle.response_complete
            else:
                answers = cycle is not None and not cycle.response_started
            if answers:
                transport.write(_HEADER_TOO_LARGE)
            transport.close()


def raise_open_file_limit() -> Optional[int]:
    """Raise this process's soft open-file limit; return the soft limit in force afterwards.

    A systemd service starts with a soft limit of 1024 (``DefaultLimitNOFILESoft``; the hard
    limit is 524288), and about 800 slow connections then took every descriptor of the
    process. The soft limit goes to ``min(hard, OPEN_FILES_TARGET)`` (an unlimited hard
    limit counts as large); where that is refused (macOS caps it at ``OPEN_MAX`` when the
    hard limit is unlimited) to ``OPEN_FILES_FALLBACK``; otherwise it stays. Never lowered.
    Process-wide and idempotent: a second call finds the limit raised. Returns None where
    there is no ``resource`` module (Windows, which has no such per-process limit).

    Child processes inherit the higher soft limit. A descriptor above 1023 breaks
    ``select.select``; ``vaf/`` has none, and the HTTP and mail libraries use ``poll``.
    """
    global _limit_logged
    try:
        import resource
    except ImportError:
        return None
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (ValueError, OSError):
        return None
    unlimited = getattr(resource, "RLIM_INFINITY", -1)
    ceiling = OPEN_FILES_TARGET if hard == unlimited else min(hard, OPEN_FILES_TARGET)
    if soft != unlimited and soft < ceiling:
        for target in (ceiling, min(ceiling, OPEN_FILES_FALLBACK)):
            if target <= soft:
                continue
            try:
                resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
            except (ValueError, OSError):
                continue
            if not _limit_logged:
                logger.info("Raised the open-file limit from %s to %s", soft, target)
                _limit_logged = True
            soft = target
            break
    return soft


def _soft_open_file_limit() -> Optional[int]:
    try:
        import resource
    except ImportError:
        return None
    try:
        soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (ValueError, OSError):
        return None
    if soft == getattr(resource, "RLIM_INFINITY", -1):
        return None
    return soft


def connection_cap() -> int:
    """How many connections a door serves at once: ``MAX_CONNECTIONS``, or a quarter of the
    soft open-file limit when that is lower.

    Why a quarter: each relayed request holds three descriptors of this process (the
    device's connection, the proxy's upstream connection and the backend's end of it), on
    top of what the process holds at rest (about 200 measured). A cap below
    ``MAX_CONNECTIONS`` is logged once, with the limit that set it."""
    global _cap_warned
    soft = _soft_open_file_limit()
    if soft is None:
        return MAX_CONNECTIONS
    cap = max(1, min(MAX_CONNECTIONS, soft // 4))
    if cap < MAX_CONNECTIONS and not _cap_warned:
        logger.warning("The open-file limit is %s, so the HTTPS door serves at most %s "
                       "connections instead of %s", soft, cap, MAX_CONNECTIONS)
        _cap_warned = True
    return cap


def door_options() -> dict:
    """The ``uvicorn.Config`` keyword arguments of a door (raises the open-file limit first).

    - ``http``: the bounded protocol (head time and size).
    - ``limit_concurrency``: at the cap uvicorn answers 503 with ``connection: close``;
      WebSocket connections count, and the bounded protocol gives a WebSocket upgrade at the
      cap the same 503.
    - ``timeout_keep_alive``: ``KEEP_ALIVE_S``.
    - ``server_header=False``: the software is not named; the ``date`` header stays.
    - ``proxy_headers=False``: uvicorn trusted ``FORWARDED_ALLOW_IPS`` and, with ``*``,
      turned a device that wrote ``X-Forwarded-For: 127.0.0.1`` into this machine. The
      resolver in ``binding.py`` is the only reader of that header on a door; it honours it
      from a loopback peer only.
    """
    raise_open_file_limit()
    return {
        "http": BoundedHttpToolsProtocol,
        "limit_concurrency": connection_cap(),
        "timeout_keep_alive": KEEP_ALIVE_S,
        "server_header": False,
        "proxy_headers": False,
    }


def door_event_loop() -> asyncio.AbstractEventLoop:
    """A new event loop (the platform's default kind) whose ``create_server`` gives a TLS
    listener ``TLS_HANDSHAKE_TIMEOUT_S`` for each handshake.

    uvicorn calls ``loop.create_server(..., ssl=context)`` without a handshake timeout, so
    asyncio's 60 s applied (measured: a TCP connection that never started TLS was still
    open after 25 s). ``ssl_handshake_timeout`` is asyncio's public parameter; a caller that
    passes its own keeps it."""
    loop = asyncio.new_event_loop()
    create_server = loop.create_server

    async def _create_server(*args, **kwargs):
        if kwargs.get("ssl") is not None and kwargs.get("ssl_handshake_timeout") is None:
            kwargs["ssl_handshake_timeout"] = TLS_HANDSHAKE_TIMEOUT_S
        return await create_server(*args, **kwargs)

    loop.create_server = _create_server  # type: ignore[method-assign]
    return loop
