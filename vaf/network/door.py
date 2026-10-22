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

- ``door_event_loop()``: the TLS handshake has ``TLS_HANDSHAKE_TIMEOUT_S``; asyncio's own limit
  for a TLS close (Python 3.11+) is set out of the way, since it counts the whole close.
- ``BoundedHttpToolsProtocol``: the request head has ``HEAD_TIMEOUT_S`` from the moment the
  connection is ready, or from the first byte that arrives while no request is being
  answered (a follow-up request, a stray empty line), and at most ``HEAD_MAX_BYTES``
  together with the trailers after a chunked body; above it the answer is 431. A connection
  may send at most ``PIPELINE_MAX`` requests ahead of their answers. A silent head and a
  pipeline past the limit drop the connection at once (an abort: a close would wait for the
  client), and the requests still being answered are told.
- Every close, whoever closes, on every Python version (``_install_close_hook``): an answer
  given while its request's body still arrives first reads and discards the rest, at most
  ``LINGER_TIME_S`` in all and ``LINGER_TIMEOUT_S`` between reads (modelled on nginx's
  lingering_close), and the connection closes after it. Then the close watch: while unsent
  bytes remain, the connection is dropped only when they have not shrunk for
  ``SEND_TIMEOUT_S`` (modelled on nginx's send_timeout, no limit on the whole answer); once
  all is out, it waits ``CLOSE_TIMEOUT_S`` for the client to end it. The client's own end (a
  FIN or a TLS close_notify) goes through the same hook (``eof_received``), and so does the
  close asyncio's TLS layer makes on the TCP transport under it, a WebSocket's included.
- ``door_options()``: those plus at most ``connection_cap()`` connections (503 above it,
  for a WebSocket upgrade as for a request), an explicit keep-alive, no ``Server`` header,
  and no forwarded-header trust of uvicorn's: ``binding.connection_client_ip`` is the only
  reader of ``X-Forwarded-For`` on a door.
- ``raise_open_file_limit()``: the process raises its own soft open-file limit.

The request body has its limits where it is read: the proxy streams it and holds it to
``BODY_MAX_BYTES`` and ``BODY_STALL_S`` (``https_proxy._forward_http``). A refused client keeps
no connection: the admission check's 403 says ``Connection: close``
(``admission.AdmissionGuard``), and a body still arriving is lingered for.

Named boundaries:
- The TLS handshake happens before any limit but its own can apply, and a connection in
  its handshake counts toward no cap (measured: 60 connections that never started TLS, 60
  more descriptors, released at 15 s). The cap turns requests away, not connections, so a
  connection it turns away holds a descriptor for at most about 70 s: 15 s for its
  handshake, 15 s for its head, ``LINGER_TIME_S`` when its body still arrives after the 503
  (the 503 is uvicorn's own answer, which lingers like any other; telling it apart would
  repeat uvicorn's cap check), and ``CLOSE_TIMEOUT_S`` for the close (the 503 is small, so
  it is out at once). The spare descriptors divided by 70 s is the rate of new connections
  that takes them all. With the soft limit at 65536: 65536 less 211 at rest (measured) and
  three for each of the cap's 512 connections, 63,789, so about 900 a second, sustained.
  Where it stays at 1024 (cap 256): 1024 less 211 at rest and 256 to 768 for the cap's
  connections (one descriptor each when idle, three when relayed), 45 to 557, so between
  about 0.6 and 8 a second. Only a firewall rate limit in front of the port closes that.
- A close while an answer is still being sent has no limit as long as the answer moves, and
  a client that takes nothing for ``SEND_TIMEOUT_S`` is dropped, with what is still unsent
  lost. Progress counts when the kernel takes more of the answer, and Linux takes more only
  once a large share of its send buffer has drained, so a client must take about that share
  within each ``SEND_TIMEOUT_S``. Measured with the real limits on loopback, where that
  buffer grows to megabytes: a 4 MB one-piece answer read at 200 KB/s arrives whole, plain
  and TLS, kept alive or with ``Connection: close``, on Python 3.13 and 3.10; readers at 5
  and 10 KB/s were dropped after 60 s, having read 0.3 and 0.6 MB (65 % of the answer
  reached the 5 KB/s reader in all, from the kernel's buffers); a client that stops reading
  is dropped after 60 s.
- On Windows, asyncio's Proactor transport counts a whole pending send until it completes
  (``proactor_events``), so the watch sees no progress inside one large send: a one-piece
  answer its client needs longer than ``SEND_TIMEOUT_S`` to take is dropped there. Read in
  asyncio's code, not measured on Windows.
- One address can fill the connection cap; a per-address count would need a second
  reader of the peer (``tests/test_client_address_has_one_reader.py``) and bookkeeping
  across the WebSocket upgrade, and no incident asks for it.
- A client that reads a response slowly, or never, holds one door connection (and on the
  proxy one upstream connection) for as long as the answer goes on and it keeps the
  connection open: uvicorn has no write timeout (measured: five clients that never read a
  streamed answer still held both at 70 s). Once the door closes the connection, the close
  watch ends it. A body sent a little at a time, each part inside ``BODY_STALL_S``, does the
  same while it lasts. The cap bounds how many.
- A client that ends its own side early (a half-close) gets what is already written when its
  end arrives, as long as it keeps reading. An answer still being produced then is cut there
  (measured: none of a 1 MB answer written 0.5 s later, or streamed, reached such a client,
  plain and TLS, with or without the door's close hook): asyncio ends a connection at the
  client's end of file, and its TLS transport cannot stay half open. Browsers do not
  half-close.
- ``vaf run --web`` serves the backend itself: there the head, handshake and connection
  limits apply to every client, the owner's own web UI included, which reaches it through
  Next.js; a flood that fills the cap turns that UI away too. A body is held only by the
  route that reads it.
- Refusals here (431, a closed head, 503) never reach the access log or the security
  log, because a flood must not become a write flood. uvicorn logs one line of its own for
  each 503 it answers, and the door logs the same line for an upgrade it turns away.
- The protocol subclasses uvicorn's ``HttpToolsProtocol`` and overrides only asyncio's
  protocol methods, httptools' parser callbacks, ``handle_websocket_upgrade`` and
  ``on_response_complete``; it reads ``cycle``, ``pipeline``, ``connections``, ``tasks``,
  ``limit_concurrency``, ``flow`` and an answer cycle's ``response_started``,
  ``response_complete``, ``disconnected`` and ``message_event``. All of them are the same from
  uvicorn 0.27 to 0.54, and httptools 0.8 and 0.9 call the chunk callbacks the trailer count
  needs; ``tests/test_network_door.py`` pins them, and CI runs against the newest uvicorn
  ``requirements.txt`` resolves. The door reaches the TCP transport under a TLS one through
  asyncio's private ``_ssl_protocol._transport``, to read its progress (Python 3.11+) and to
  hook its close (every version); the pin test and the TLS WebSocket test fail if it moves.

The limits are constants, not settings: they are the floor of the door, not a preference,
and a config key's name is a security decision of its own.

Needs the server extra (uvicorn with httptools); ``vaf.network`` does not import it.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import weakref
from typing import Optional

from uvicorn.protocols.http.httptools_impl import HttpToolsProtocol

logger = logging.getLogger(__name__)

# asyncio's own default is 60 s, and uvicorn passes none.
TLS_HANDSHAKE_TIMEOUT_S = 15.0
# A closing connection that still has an answer to send keeps sending it as long as it
# moves: it is dropped only when its unsent bytes have not shrunk for this long. Timed between
# two writes, never for the whole answer (modelled on nginx's send_timeout, whose default this
# is). uvicorn closes as soon as an answer is handed to the transport, not when the client has
# it: a one-piece answer of 4 MB read at 200 KB/s is still being sent 20 s after the close.
SEND_TIMEOUT_S = 60.0
# Once everything is sent, how long a closing connection waits for the client to end it (its
# TLS close_notify) before it is dropped. Until it ends it holds its descriptor and its place
# under the cap: asyncio's own limit is 30 s on Python 3.11+ and none on 3.10.
CLOSE_TIMEOUT_S = 10.0
# An answer given while its request's body is still arriving ends the connection after the
# body: the rest is read and discarded for at most LINGER_TIME_S in all and LINGER_TIMEOUT_S
# between two reads, then the connection closes (modelled on nginx's lingering_close, whose
# defaults these are). Closed at once, a connection whose body still arrived was reset and its
# answer lost (measured: a refused 8 MB POST got a reset, not the 403).
LINGER_TIME_S = 30.0
LINGER_TIMEOUT_S = 5.0
# asyncio's own limit for a TLS close (Python 3.11+) counts the whole close, the sending of the
# rest of the answer included, so a short one cut an answer a slow client was still reading
# (measured: 73 % of a 4 MB answer at 200 KB/s). A day keeps it out of the way; the close watch
# bounds the close by its progress instead.
_ASYNCIO_TLS_CLOSE_S = 24 * 3600.0
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
# Requests one connection may have sent ahead of their answers (HTTP/1.1 pipelining, which no
# browser uses). uvicorn queues every request of a read before it pauses reading: one send
# of 256 KiB of minimal requests whose answers were never read queued 9,362 of them (34 MB)
# and relayed them upstream one by one for minutes.
PIPELINE_MAX = 16
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
    """uvicorn's httptools protocol with a time and a size limit on every request head, a
    WebSocket upgrade held to the connection cap like any request, and lingering for the rest
    of a body whose request is answered.

    Overrides only asyncio protocol methods, httptools parser callbacks and uvicorn's
    ``handle_websocket_upgrade`` and ``on_response_complete``, always hands on to uvicorn's
    own, and never reads the client address.

    The time: the head timer runs from the ready connection, and from the first byte that
    arrives while no request is being answered: the next request on a kept-alive connection,
    or an empty line (llhttp skips one between requests without ``on_message_begin``). The
    rest of a body after its request's answer is lingered for instead. uvicorn's own
    ``data_received`` cancels its keep-alive timer on any byte and re-arms it only after the
    next answer, so one empty line used to hold a kept-alive connection forever (measured:
    open after 45 s, after a 200 or a refused request alike).

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
        # The answer cycle of the request being parsed (None for an upgrade), and the cycles
        # not yet answered, which a drop tells that the connection is gone.
        self._message_cycle = None
        self._cycles: list = []
        # Lingering: the rest of an answered request's body is read and discarded.
        self._lingering = False
        self._linger_close = None
        self._linger_end: Optional[asyncio.TimerHandle] = None
        self._linger_wait: Optional[asyncio.TimerHandle] = None

    # -- asyncio protocol

    def connection_made(self, transport) -> None:  # type: ignore[override]
        _install_close_hook(transport, self)
        # Over TLS, asyncio's TLS layer closes the TCP transport under it by itself (after the
        # client's close_notify, or once its own shutdown is done), and that close waited for
        # a client that reads nothing without any limit: a WebSocket whose client stopped
        # reading and sent close_notify was held past 150 s on Python 3.13 and 3.10.15. The
        # same hook on the TCP transport bounds it; lingering is decided above it.
        inner = getattr(getattr(transport, "_ssl_protocol", None), "_transport", None)
        if inner is not None:
            _install_close_hook(inner, None)
        super().connection_made(transport)
        self._door_transport = transport
        self._begin_head()
        self._arm_head_timer()

    def connection_lost(self, exc) -> None:
        self._cancel_head_timer()
        self._cancel_linger()
        super().connection_lost(exc)

    def eof_received(self):
        # The client has sent all it will: nothing is left to linger for.
        if self._lingering:
            self._end_linger()
        result = super().eof_received()
        # The client's own end goes through the close hook too, so the close watch bounds it.
        # Over TLS on Python 3.11+, asyncio ends the TLS layer by itself after a FIN or a
        # close_notify, uvicorn then sees the transport closing and never closes it, and the
        # TCP transport waits for a client that reads nothing without any limit (measured:
        # still held after 130 s, both ways). What is already written when the client's end
        # arrives still reaches a client that keeps reading: the watch drops only a close that
        # stops moving. An answer still being produced then is cut there, as before: asyncio
        # ends a connection at the client's end of file, and its TLS transport cannot be kept
        # half open.
        transport = self._door_transport
        if transport is not None and not transport.is_closing():
            transport.close()
        return result

    def data_received(self, data: bytes) -> None:
        if self._lingering:
            # The rest of the body of a request that is answered: discarded unparsed.
            if self._linger_wait is not None:
                self._linger_wait.cancel()
            self._linger_wait = asyncio.get_running_loop().call_later(LINGER_TIMEOUT_S,
                                                                      self._end_linger)
            return
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

    def on_response_complete(self) -> None:
        super().on_response_complete()
        if not self._door_closed and not self.transport.is_closing() and self._answered_early():
            # Answered while its body still arrives, the answer kept alive: the body is read
            # and discarded, then the connection closes, as after an answer that closes it.
            self.transport.close()

    # -- httptools parser callbacks

    def on_message_begin(self) -> None:
        if self._door_closed:
            return
        if len(self.pipeline) >= PIPELINE_MAX:
            # Too many requests sent ahead of their answers: the connection is dropped. No
            # 431 or other answer: one for an earlier request is still due on it.
            self._drop()
            return
        self._messages += 1
        self._message_open = True
        self._message_cycle = None
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
        before = self.cycle
        super().on_headers_complete()
        if self.cycle is not before:
            self._message_cycle = self.cycle
            self._cycles = [c for c in self._cycles if not c.response_complete] + [self.cycle]

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
        """No complete head in time: the connection is dropped without an answer."""
        self._head_timer = None
        self._drop()

    def _drop(self) -> None:
        """Drop the connection at once: nothing is left to send on it. An abort, not a close,
        because a close keeps the connection and its place under the cap until the client
        answers it or the close watch runs out.

        Every request still being answered is told the connection is gone first. uvicorn tells
        only the last request it parsed, so a request in flight ahead of a dropped pipeline kept
        running, and its next write into the dropped connection logged a traceback per
        connection (measured: Python 3.13 and 3.10.15)."""
        self._cancel_head_timer()
        self._cancel_linger()
        self._door_closed = True
        for cycle in self._cycles:
            if not cycle.response_complete:
                cycle.disconnected = True
                cycle.message_event.set()
        transport = self._door_transport
        if transport is not None:
            transport.abort()

    # -- lingering

    def _answered_early(self) -> bool:
        """Whether the request being parsed has its answer while its body still arrives."""
        cycle = self._message_cycle
        return self._message_open and cycle is not None and cycle.response_complete

    def _linger(self, close_now) -> bool:
        """Called by the close hook: linger instead of closing now when the request has its
        answer while its body still arrives. ``close_now`` closes after the lingering."""
        if self._lingering:
            return True
        if self._door_closed or not self._answered_early():
            return False
        self._lingering = True
        self._linger_close = close_now
        self._cancel_head_timer()
        loop = asyncio.get_running_loop()
        self._linger_end = loop.call_later(LINGER_TIME_S, self._end_linger)
        self._linger_wait = loop.call_later(LINGER_TIMEOUT_S, self._end_linger)
        self.flow.resume_reading()
        return True

    def _end_linger(self) -> None:
        if not self._lingering:
            return
        self._cancel_linger()
        self._lingering = False
        self._door_closed = True
        close_now, self._linger_close = self._linger_close, None
        if close_now is not None:
            close_now()

    def _cancel_linger(self) -> None:
        for handle in (self._linger_end, self._linger_wait):
            if handle is not None:
                handle.cancel()
        self._linger_end = self._linger_wait = None

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


def _install_close_hook(transport, protocol) -> None:
    """Hook ``close()`` on the transport itself, so that every close goes through the door:
    uvicorn's (an answer with ``Connection: close``, the keep-alive expiry, a 400), the door's
    (a 431, a 503 for an upgrade at the cap), websockets' after an upgrade, and asyncio's own
    when a plain connection reaches its end. Over TLS the TCP transport under the TLS one gets
    the hook as well (``protocol`` None: it never lingers), for the closes asyncio's TLS layer
    makes there by itself. The transport keeps its identity and type, so nothing that reads it
    changes.

    A close first asks the protocol whether to linger (an answered request whose body still
    arrives, ``BoundedHttpToolsProtocol._linger``); then it closes and starts the close watch
    (``_watch_close``). Only the first ``close()`` is passed on, and none to a transport
    something already closed: on Python 3.11+ a second one drops the TLS transport's protocol,
    and ``abort()`` then does nothing (measured: closed twice, aborted after 1 s, still open
    after 20 s). A transport that takes no attributes (uvloop's, which the door's own loop
    never uses) keeps its loop's limits.
    """
    ref = weakref.ref(transport)
    owner = weakref.ref(protocol) if protocol is not None else (lambda: None)
    close = type(transport).close
    loop = asyncio.get_running_loop()
    closing = False

    def close_now() -> None:
        target = ref()
        if target is None:
            return
        # A transport something already closed is not closed again: asyncio's TLS layer closes
        # it by itself after the client's close_notify, and on Python 3.11+ a second close
        # drops the TLS transport's protocol, after which the watch and abort() do nothing
        # (measured: a WebSocket closed again by websockets after a close_notify, held past
        # 150 s). The watch still starts.
        if not target.is_closing():
            close(target)
        # A close from a finalizer can come after the loop stopped; nothing is left to hold.
        if not loop.is_closed():
            _watch_close(loop, ref)

    def _close() -> None:
        nonlocal closing
        if closing:
            return
        closing = True
        door = owner()
        if door is not None and door._linger(close_now):
            return
        close_now()

    try:
        transport.close = _close
    except (AttributeError, TypeError):
        pass


def _watch_close(loop, ref) -> None:
    """Bound a close by its progress. While unsent bytes remain, the connection is dropped only
    when they have not shrunk for ``SEND_TIMEOUT_S``; once they are all out, it waits
    ``CLOSE_TIMEOUT_S`` for the client to end it (its TLS close_notify). No limit on the whole
    close: a slow client that keeps reading gets the whole answer.

    Before this, a close ended a fixed time after it began, and an answer a client was still
    reading was cut (measured: 71 to 92 % of a 4 MB one-piece answer read at 200 KB/s, plain
    and TLS, kept alive or closed, on Python 3.13 and 3.10.15). Without any limit, a TLS close
    waited for close_notify for asyncio's 30 s (Python 3.11+) or for ever (3.10: still open
    after 40 s), and a plain close for a client that read nothing for ever, counted toward the
    cap all the while."""
    state = {"unsent": None, "moved": loop.time(), "drained": None}

    def tick() -> None:
        transport = ref()
        if transport is None or _gone(transport):
            return
        now = loop.time()
        unsent = _unsent_bytes(transport)
        if unsent:
            if state["unsent"] is None or unsent < state["unsent"]:
                state["moved"] = now
            state["unsent"] = unsent
            state["drained"] = None
            if now - state["moved"] >= SEND_TIMEOUT_S:
                transport.abort()
                return
        elif state["drained"] is None:
            state["drained"] = now
        elif now - state["drained"] >= CLOSE_TIMEOUT_S:
            transport.abort()
            return
        loop.call_later(min(1.0, SEND_TIMEOUT_S / 4, CLOSE_TIMEOUT_S / 4), tick)

    tick()


def _unsent_bytes(transport) -> int:
    """The bytes written to the transport that the kernel has not taken yet. On Python 3.11+ a
    TLS transport counts only what it has not encrypted; the TCP transport under it holds the
    rest (measured: 0 against 1.6 MB right after a 4 MB write). On 3.10 the TLS transport
    reports the TCP transport's count itself."""
    try:
        size = transport.get_write_buffer_size()
        if sys.version_info >= (3, 11):
            inner = getattr(getattr(transport, "_ssl_protocol", None), "_transport", None)
            if inner is not None:
                size += inner.get_write_buffer_size()
        return size
    except AttributeError:
        return 0


def _gone(transport) -> bool:
    """Whether the connection has ended: a TLS transport then has no socket any more, a plain
    one a closed socket."""
    try:
        sock = transport.get_extra_info("socket")
    except AttributeError:
        return True
    return sock is None or sock.fileno() == -1


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
    listener ``TLS_HANDSHAKE_TIMEOUT_S`` for each handshake and, on Python 3.11 and later,
    sets asyncio's own TLS close limit out of the way.

    uvicorn calls ``loop.create_server(..., ssl=context)`` with neither, so asyncio's 60 s
    applied to the handshake (measured: a TCP connection that never started TLS was still
    open after 25 s). asyncio's TLS close limit counts the whole close, the sending of the
    rest of an answer included: at its 30 s a slow reader of a large answer lost the rest,
    and at a few seconds most readers did (measured: 73 % of a 4 MB answer at 200 KB/s). The
    door's close watch (``_watch_close``) bounds every close by its progress instead, on every
    Python version (3.10 has no such limit at all). Both are asyncio's public parameters; a
    caller that passes its own keeps it."""
    loop = asyncio.new_event_loop()
    create_server = loop.create_server

    async def _create_server(*args, **kwargs):
        if kwargs.get("ssl") is not None:
            if kwargs.get("ssl_handshake_timeout") is None:
                kwargs["ssl_handshake_timeout"] = TLS_HANDSHAKE_TIMEOUT_S
            if sys.version_info >= (3, 11) and kwargs.get("ssl_shutdown_timeout") is None:
                kwargs["ssl_shutdown_timeout"] = _ASYNCIO_TLS_CLOSE_S
        return await create_server(*args, **kwargs)

    loop.create_server = _create_server  # type: ignore[method-assign]
    return loop
