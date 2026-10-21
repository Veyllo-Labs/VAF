# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Integrated HTTPS reverse proxy for VAF (no Nginx required).

When local_network_enabled and local_network_tls_enabled, this proxy listens on
0.0.0.0:443 (or local_network_https_port) with SSL and forwards:
  - /api/* and /ws -> http://127.0.0.1:8005 (internal HTTP channel; 8001 is HTTPS-only when TLS on)
  - everything else -> http://127.0.0.1:3000 (frontend)

Best practice: single entry point for HTTPS, TLS termination here.

The listener holds every connection, admitted or not, to the door's limits (``door.py``: a
15 s TLS handshake, a request head within 15 s and 16 KiB, a connection cap, no forwarded-header
trust of uvicorn's) and serves TLS with the one policy of every listener
(``ssl_utils.server_tls_options``). It relays each body as it arrives, both ways (a request
body at most ``door.BODY_MAX_BYTES`` and never silent for ``door.BODY_STALL_S``), and accepts a
WebSocket only once the backend accepted the relayed handshake.

The door admits what the backend admits and refuses every other client before any page, API
call or socket. With Local Network on that is what ``binding.inbound_policy()`` admits (this
machine, the local networks or with "VPN only" the VPN networks, plus the private networks an
admin added); with it off, this machine only. ``admission.ByLocalNetwork`` picks the guard per
request, the same choice the backend makes, because this proxy runs whenever TLS is on, Local
Network or not (vaf/tray.py), and outlives a switch to off until the restart stops it. A
refused browser gets a short page that says which networks VAF answers and what to do, an API
call 403 JSON, a socket 4003. The door used to relay pages to any address and leave the
refusal to the backend's API (measured: ``GET /login`` 200 for ``203.0.113.9``, its socket
accepted), and with Local Network off it still relayed pages to LAN devices. Named boundary:
the TLS handshake happens before any HTTP check, so a client that reaches the port still
receives the certificate, whose names list the LAN and VPN addresses and the host name; only a
firewall closes that.
"""

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable

from starlette.applications import Starlette
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket

from vaf.network import door
from vaf.network.admission import AdmissionGuard, ByLocalNetwork, this_machine_only
from vaf.network.ssl_utils import server_tls_options

logger = logging.getLogger(__name__)


def _access_log_path() -> Path:
    """Return path for the proxy access log (logs/https_proxy_access_<date>.log)."""
    from vaf.core.platform import Platform
    log_dir = Platform.data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"https_proxy_access_{datetime.now().strftime('%Y-%m-%d')}.log"


def _write_access(line: str) -> None:
    """Append a timestamped line to the proxy access log. One request is one line: the path
    arrives percent-decoded, and GET /page%0a<text> wrote a forged second line."""
    try:
        from vaf.core.security_events import one_line
        ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        with open(_access_log_path(), "a", encoding="utf-8") as f:
            f.write(f"{ts}  {one_line(line)}\n")
    except Exception:
        pass


class AccessLogMiddleware:
    """ASGI middleware that logs every incoming request/connection to a file."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # The client as every other site resolves it (vaf.network.binding), so a device behind
        # a relay on this machine is logged as itself and not as 127.0.0.1.
        if scope["type"] == "http":
            method = scope.get("method", "?")
            path = scope.get("path", "/")
            _write_access(f"HTTP  {_logged_client(scope)}  {method} {path}")
        elif scope["type"] == "websocket":
            path = scope.get("path", "/ws")
            _write_access(f"WS    {_logged_client(scope)}  {path}")
        elif scope["type"] == "lifespan":
            _write_access("LIFESPAN event")
        else:
            _write_access(f"OTHER type={scope['type']}")
        await self.app(scope, receive, send)


def _logged_client(scope: Scope) -> str:
    from starlette.requests import HTTPConnection

    from vaf.network.binding import connection_client_ip
    try:
        return connection_client_ip(HTTPConnection(scope))
    except Exception:
        return "?"

FRONTEND_ORIGIN = "http://127.0.0.1:3000"

# Headers every PAGE relayed from the frontend carries, each added only when the frontend did
# not set it (a page that sets its own keeps it). The backend's API responses have their own
# table (_SecurityHeadersMiddleware in vaf/core/web_server.py); pages had none at all, so the
# login and the app could be framed by any site and carried no HSTS. Deliberately different
# from the backend's table:
# - no Permissions-Policy: the backend's microphone=() would switch off the web UI's own
#   voice input, which runs in these pages;
# - frame-ancestors 'none' on pages only: the interactive browser's viewer
#   (/api/browser-vnc/, a backend response with X-Frame-Options: SAMEORIGIN) is framed by the
#   UI, and a CSP frame-ancestors would override that. No page of the UI is framed by the UI
#   (every iframe is srcDoc, about:blank or that viewer).
# HSTS is scoped by the dialled host (binding.hsts_for_host), on pages and backend answers
# alike: a year for a DNS name set up for VAF, max-age=0 for localhost and this machine's own
# names, nothing for an address.
PAGE_SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Content-Security-Policy", "frame-ancestors 'none'"),
    ("Referrer-Policy", "strict-origin-when-cross-origin"),
)


def _add_missing_headers(raw_headers: list, target_origin: str, dialled_host: str = "") -> None:
    """Add the page set (frontend) and the scoped HSTS (both) to a raw header list, never over
    a value the upstream set."""
    from vaf.network.binding import hsts_for_host
    present = {name.lower() for name, _value in raw_headers}
    wanted = list(PAGE_SECURITY_HEADERS) if target_origin == FRONTEND_ORIGIN else []
    hsts = hsts_for_host(dialled_host)
    if hsts:
        wanted.append(("Strict-Transport-Security", hsts))
    for name, value in wanted:
        key = name.lower().encode("latin-1")
        if key not in present:
            raw_headers.append((key, value.encode("latin-1")))


# Internal HTTP channel (8005) is always running when this proxy runs (TLS on); 8001 is HTTPS-only
BACKEND_ORIGIN = "http://127.0.0.1:8005"

# Shared httpx clients for connection pooling: reuse TCP connections across requests instead of
# opening a new connection for every single resource (JS chunks, CSS, images, etc.).
_frontend_client: "httpx.AsyncClient | None" = None  # noqa: F821
_backend_client: "httpx.AsyncClient | None" = None  # noqa: F821

# The live proxy server, so LAN-disable / restart can stop it. The proxy runs in a daemon thread with no
# other handle; without this it keeps listening on the LAN port after hosting is turned off.
_running_server = None


def stop_https_proxy() -> None:
    """Signal the running HTTPS proxy to exit (LAN/TLS disabled, or a restart) and clear the runtime
    status so the UI stops advertising a port nothing serves."""
    global _running_server
    srv = _running_server
    _running_server = None
    if srv is not None:
        try:
            srv.should_exit = True
        except Exception:
            pass
    try:
        from vaf.network import runtime_status
        runtime_status.reset()
    except Exception:
        pass


def _new_upstream_client() -> "httpx.AsyncClient":  # noqa: F821
    """A pooled client for one upstream. The pool is as large as the door's connection cap:
    a streamed response holds its upstream connection while the device reads it, so with a
    smaller pool a few slow readers would starve every other request into the pool timeout,
    and with an equal one the door's cap is the only bound. The 60 s timeout is also how long
    a response may send nothing between two parts."""
    import httpx
    return httpx.AsyncClient(
        timeout=60.0,
        limits=httpx.Limits(max_connections=door.connection_cap(), max_keepalive_connections=20,
                            keepalive_expiry=30),
    )


async def _get_client(target_origin: str) -> "httpx.AsyncClient":  # noqa: F821
    """Return a shared httpx.AsyncClient for the target, with connection pooling."""
    global _frontend_client, _backend_client
    if target_origin == FRONTEND_ORIGIN:
        if _frontend_client is None or _frontend_client.is_closed:
            _frontend_client = _new_upstream_client()
        return _frontend_client
    else:
        if _backend_client is None or _backend_client.is_closed:
            _backend_client = _new_upstream_client()
        return _backend_client


def _normalize_headers_for_upstream(headers: dict, target_origin: str, original_host: str) -> None:
    """Drop hop-by-hop, set forward headers, set single Host to target (remove any existing host)."""
    for h in ("connection", "transfer-encoding", "keep-alive", "te", "trailer", "upgrade", "proxy-authorization"):
        headers.pop(h, None)
    # Drop every CLIENT-supplied copy of the headers this proxy authors below. Starlette hands us
    # lowercased header names, so writing "X-Forwarded-For" would ADD a second header rather than
    # replace the client's - and the backend's Headers.get() returns the FIRST match, i.e. the
    # forged one. A LAN device could then claim "X-Forwarded-For: 127.0.0.1" and be treated as a
    # local client. Strip first, set after: the value the backend sees is always ours.
    for k in list(headers):
        if k.lower() in ("host", "x-forwarded-for", "x-forwarded-proto", "x-forwarded-host"):
            del headers[k]
    from urllib.parse import urlparse
    parsed = urlparse(target_origin)
    headers["Host"] = parsed.netloc or "127.0.0.1:3000"
    headers["X-Forwarded-Proto"] = "https"
    headers["X-Forwarded-Host"] = original_host or "127.0.0.1"


# Response headers that describe one connection, not the content: the door frames its own
# answer. date and server too, which uvicorn already writes for the door (server only where it
# is on), and x-powered-by, which names the software: relayed, each response carried two dates
# and two servers.
_UPSTREAM_ONLY_HEADERS = frozenset((
    b"connection", b"keep-alive", b"transfer-encoding", b"te", b"trailer", b"upgrade",
    b"date", b"server", b"x-powered-by",
))


class _BodyRefused(Exception):
    """A request body the door stops: too large (413) or stalled (408)."""

    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


def _refusal(status: int) -> Response:
    """A body the relay stopped (413, 408, or 400 for a device that left), with the connection
    closed: the rest of the body is never read."""
    return Response(content=b"", status_code=status, headers={"Connection": "close"})


async def _client_body(request: Request):
    """The client's body as it arrives, held to door.BODY_MAX_BYTES in all and to
    door.BODY_STALL_S per part. A refusal raises _BodyRefused inside the upstream send, which
    aborts the upstream request."""
    received = 0
    parts = request.stream().__aiter__()
    while True:
        try:
            part = await asyncio.wait_for(parts.__anext__(), timeout=door.BODY_STALL_S)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError:
            raise _BodyRefused(408) from None
        received += len(part)
        if received > door.BODY_MAX_BYTES:
            raise _BodyRefused(413)
        if part:
            yield part


class _RelayedResponse(StreamingResponse):
    """An upstream answer relayed as it arrives, its body untouched (a compressed body stays
    compressed), under the header list the relay built.

    The upstream response is closed however the answer ends: done, device gone, or an error.
    Closing it in the body iterator alone missed a device that left before the first part was
    read (an iterator that never started runs no cleanup), and the pooled connection stayed
    taken. An upstream that fails after its headers ends the stream there, so the device sees
    a cut-off answer, never a clean end."""

    def __init__(self, upstream, raw_headers: list) -> None:
        super().__init__(upstream.aiter_raw(), status_code=upstream.status_code)
        self.raw_headers = raw_headers
        self._upstream = upstream

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        import anyio
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self._upstream.aclose()


async def _forward_http(request: Request, target_origin: str) -> Response:
    """Relay one HTTP request to the target, streaming in both directions.

    It used to read the whole request body into memory before sending it (a 150 MB body was
    taken, measured) and the whole response before answering: a live stream (the mail and
    WhatsApp drafts, the memory answer) arrived in one piece at its end, measured 3.0 s for a
    stream whose first event was ready at once, and it decoded the body and appended a
    Content-Length of its own to the upstream's (HEAD: 17 and 0; a compressed answer: two
    different values, and the body was lost)."""
    path = (request.scope.get("path") or request.url.path or "/").strip()
    if not path.startswith("/"):
        path = "/" + path
    url = target_origin.rstrip("/") + (path or "/")
    if request.url.query:
        url += "?" + request.url.query
    headers = dict(request.headers)
    original_host = request.headers.get("host") or request.headers.get("Host") or "127.0.0.1"
    _normalize_headers_for_upstream(headers, target_origin, original_host)
    # Set unconditionally: "no X-Forwarded-For" must reliably mean "not relayed by this proxy",
    # otherwise a relayed request without client info would be indistinguishable from genuine
    # internal loopback traffic. "unknown" is not a valid IP, so the backend fails closed.
    # The value is the resolver's (vaf.network.binding.connection_client_ip), the one reader
    # of a connection's client: a tunnel or reverse proxy on this machine connects from
    # loopback, and its visitor is the client it named, not this machine. The door switches
    # uvicorn's own forwarded-header handling off (door.door_options), so the resolver is the
    # only rule here. A non-loopback peer is written as itself; a loopback one as the client
    # the relay in front of it named, else 127.0.0.1. The resolver reads the request as it
    # arrived, before the strip above.
    from vaf.network.binding import connection_client_ip
    headers["X-Forwarded-For"] = connection_client_ip(request)
    # A client that asked for no encoding gets none: httpx would ask the upstream for gzip,
    # deflate, br and zstd on its behalf, and the body now passes through undecoded.
    if "accept-encoding" not in headers:
        headers["accept-encoding"] = "identity"

    # A body travels only when the client declared one, so a GET never becomes chunked. Its
    # Content-Length goes along with the stream (httpx then sends no Transfer-Encoding); a
    # chunked body is sent chunked. A declared length above the cap is refused before one
    # byte is read, and the upstream never sees the request.
    declared = request.headers.get("content-length")
    try:
        length = int(declared) if declared is not None else None
    except ValueError:
        length = None
    if length is not None and length > door.BODY_MAX_BYTES:
        return _refusal(413)
    has_body = (length or 0) > 0 or "transfer-encoding" in request.headers
    try:
        client = await _get_client(target_origin)
        upstream = client.build_request(request.method, url, headers=headers,
                                        content=_client_body(request) if has_body else None)
        resp = await client.send(upstream, stream=True)
    except _BodyRefused as refused:
        return _refusal(refused.status)
    except ClientDisconnect:
        # The device left during its upload; the upstream request was aborted with it, and
        # this answer reaches nobody.
        logger.debug("HTTPS proxy: client left during the body of %s %s", request.method, path)
        return _refusal(400)
    except Exception as e:
        logger.warning("HTTPS proxy forward failed %s %s -> %s: %s", request.method, path, target_origin, e)
        return Response(content=b"Bad Gateway", status_code=502)
    # Repeated headers stay repeated (two Set-Cookie are two lines; a dict joined them into
    # one), and Content-Encoding and Content-Length pass through as the upstream sent them.
    try:
        raw_headers = [(name.lower(), value) for name, value in resp.headers.raw
                       if name.lower() not in _UPSTREAM_ONLY_HEADERS]
        _add_missing_headers(raw_headers, target_origin, original_host)
    except BaseException:
        await resp.aclose()
        raise
    return _RelayedResponse(resp, raw_headers)


def _get_cookie_header(scope: dict) -> str | None:
    """Extract Cookie header from ASGI scope so we can forward it to the backend."""
    for (name, value) in scope.get("headers") or []:
        if name.lower() == b"cookie" and value:
            return value.decode("latin-1")
    return None


# Which websocket paths this proxy will relay, and to exactly which backend path.
# An ALLOWLIST rather than a wildcard: a pattern like /ws/{rest:path} would make every
# future websocket endpoint reachable from the LAN the moment somebody adds it, and
# nobody would ever make that decision on purpose. The room id shape is the store's own
# (_SAFE_COMPONENT in vaf/core/a2a/store.py), so a path that would be refused as a
# directory name never reaches the backend either.
_WS_ALLOWED = re.compile(
    r"^/ws$"
    r"|^/ws/a2a/[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"
    # The interactive browser's VNC stream. The ticket is the credential and is
    # checked against the live lease by the backend route, so the shape is all this
    # pattern has to pin: token_urlsafe output, nothing that could walk a path.
    r"|^/api/browser-vnc/t/[A-Za-z0-9_-]{16,64}/websockify$"
)


async def _forward_websocket(websocket: WebSocket) -> None:
    """Relay a client WS to the SAME path on the backend (ws://127.0.0.1:8005), which is
    asked first: the client is accepted only once the backend accepted the relayed
    handshake.

    The path used to be hardcoded to /ws, which meant a second websocket route would
    have relayed to the WebUI handler: the socket opens, frames flow, the wrong handler
    answers. That is the worst failure shape there is, because everything looks healthy,
    so the path is carried through and checked against an allowlist instead.

    The client used to be accepted before the backend was dialled, so a handshake the
    backend refused (no session, an ended one, a foreign origin: every /ws refusal comes
    before accept) opened on the device and closed with 1000 (measured), and a socket
    without a session could send frames into the door until the refusal arrived. Now a
    refused or unreachable backend closes the client before accept, which a browser sees
    as a failed handshake (HTTP 403), as the desktop does on 8005: the web UI then checks
    its session and goes to the sign-in page instead of reconnecting again and again.
    """
    import websockets
    path = websocket.scope.get("path") or "/ws"
    if not _WS_ALLOWED.match(path):
        # Refused BEFORE accept, so the client sees a handshake rejection rather than a
        # socket that opens and then closes for no stated reason.
        await websocket.close(code=4004)
        return
    # Mirror the client's offered subprotocol. The VNC client offers "binary" and
    # checks what came back; accepting without one leaves it holding a socket it
    # will not speak on.
    _offered = websocket.scope.get("subprotocols") or []
    # Derived from BACKEND_ORIGIN, not a second hardcoded 8005: the HTTP half already
    # reads that constant, and two literals for one backend is one of them going stale.
    backend_uri = BACKEND_ORIGIN.replace("http://", "ws://").replace("https://", "wss://") + path
    if websocket.url.query:
        backend_uri += "?" + websocket.url.query
    extra_headers = []
    cookie = _get_cookie_header(websocket.scope)
    if cookie:
        extra_headers.append(("Cookie", cookie))
    # Forward the REAL client on EVERY handshake, as the HTTP half does: the backend reads
    # X-Forwarded-For for the socket's admission, its token lane and the network map. It used
    # to be sent only when a peer was known, and a relayed handshake without the header reads
    # as the machine itself; "unknown" (no client) fails closed instead. The resolver's value,
    # so a relay on this machine is not written as 127.0.0.1.
    from vaf.network.binding import connection_client_ip
    extra_headers.append(("X-Forwarded-For", connection_client_ip(websocket)))
    # What the backend's origin guard judges a browser handshake by (ForeignOriginGuard in
    # vaf/auth/middleware.py): the page's Origin, the host it dialled and its fetch metadata.
    # This relay used to carry only the cookie, so a relayed socket looked like a script and
    # passed unjudged - and a page on another port of the same host is same-site, so the cookie
    # rode along and opened the owner's socket through this door. The HTTP half needs nothing:
    # it forwards every client header and authors X-Forwarded-Host/-Proto itself.
    from starlette.datastructures import Headers
    _client_headers = Headers(scope=websocket.scope)
    _origin = _client_headers.get("origin")
    _dialled = _client_headers.get("host")
    if _dialled:
        extra_headers.append(("X-Forwarded-Host", _dialled))
    extra_headers.append(("X-Forwarded-Proto", "https"))
    _site = _client_headers.get("sec-fetch-site")
    if _site:
        extra_headers.append(("Sec-Fetch-Site", _site))
    # What the client is told when this relay ends. A relayed socket used to end with a
    # bare `close()`, so every application close code the backend sent (a refused
    # credential, a room that does not exist, a peer that is already writing) reached
    # the client as a plain 1000 with no reason. Measured against the room socket: four
    # different refusals were indistinguishable from outside, and a foreign agent that
    # hit one had to guess which had happened.
    closing = {"code": 1000, "reason": ""}
    try:
        # Relay large frames untouched. The backend's history_update can embed inline base64 images,
        # so a single frame easily exceeds the websockets client default max_size (1 MB). The default
        # would raise PayloadTooBig on that frame, tear the relay down, and the WebUI would reconnect
        # into an endless flap (auto-loading the same heavy session each time → "connection lost").
        # The backend leg is trusted and bounded by the 8005 channel's ws_max_size
        # (WS_MAX_SIZE_BYTES - the tray passes it explicitly; for a long time it did
        # NOT, so this comment claimed a bound that was uvicorn's 16 MB default),
        # so disable the intermediate limit here.
        _sub = [_offered[0]] if _offered else None
        # ping_interval=None for the same reason the stream lane needs it: the client
        # library's 20s ping plus 20s pong timeout closes any relayed socket whose far
        # end does not answer protocol pings after exactly 40 seconds of quiet. The
        # WebUI's own socket answers, a relayed VNC stream does not.
        async with websockets.connect(backend_uri, additional_headers=extra_headers or None,
                                      origin=_origin, subprotocols=_sub, max_size=None,
                                      ping_interval=None) as backend_ws:
            await websocket.accept(subprotocol=_offered[0] if _offered else None)

            async def from_backend():
                try:
                    async for msg in backend_ws:
                        if isinstance(msg, str):
                            await websocket.send_text(msg)
                        else:
                            await websocket.send_bytes(msg)
                except Exception:
                    pass
                finally:
                    # Carried out to the client below. Read here rather than after the
                    # wait, because this is the moment the library has it.
                    closing["code"] = getattr(backend_ws, "close_code", None) or 1000
                    closing["reason"] = getattr(backend_ws, "close_reason", None) or ""
            async def from_client():
                # receive(), not receive_text(): this relay used to be text-only, which
                # was invisible while the only client was the WebUI's JSON socket and
                # fatal for the first BINARY one (the interactive browser's VNC stream
                # is binary end to end - receive_text() raises on the first frame and
                # this half of the relay dies without a word).
                try:
                    while True:
                        message = await websocket.receive()
                        if message.get("type") == "websocket.disconnect":
                            return
                        if message.get("bytes") is not None:
                            await backend_ws.send(message["bytes"])
                        elif message.get("text") is not None:
                            await backend_ws.send(message["text"])
                except Exception:
                    pass
            # FIRST_COMPLETED, not gather. A relay has two halves and either one
            # ending means the relay is over; gather waits for BOTH, so a backend that
            # accepted and then refused left the client half still waiting on a client
            # that was itself waiting for a welcome. Neither spoke, and the socket hung
            # until the client's own timeout - a refusal that used to be instant became
            # ten silent seconds that said less than before.
            halves = {asyncio.create_task(from_backend()),
                      asyncio.create_task(from_client())}
            _done, pending = await asyncio.wait(halves, return_when=asyncio.FIRST_COMPLETED)
            for half in pending:
                half.cancel()
    except Exception as e:
        # websockets' InvalidStatus carries the backend's answer to the handshake.
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status is not None:
            logger.warning("HTTPS proxy: backend refused the WebSocket handshake (HTTP %s)", status)
        else:
            logger.warning("HTTPS proxy WebSocket backend connect failed: %s", e)
    finally:
        # Before accept this refuses the handshake; after it, it carries the backend's close.
        try:
            await websocket.close(code=closing["code"], reason=closing["reason"])
        except Exception:
            pass


async def _api_route(request: Request) -> Response:
    """All /api and /api/* requests go directly to backend (8005). No path detection."""
    logger.info("HTTPS proxy: %s %s -> backend %s", request.method, request.url.path, BACKEND_ORIGIN)
    resp = await _forward_http(request, BACKEND_ORIGIN)
    if resp.status_code >= 400:
        logger.warning("HTTPS proxy: backend responded %s for %s %s", resp.status_code, request.method, request.url.path)
    return resp


async def _proxy_handler(request: Request) -> Response:
    # Everything else (non-/api, non-/ws) goes to frontend
    return await _forward_http(request, FRONTEND_ORIGIN)


async def _ws_handler(websocket: WebSocket) -> None:
    """WebSocket /ws: relay client connection to backend ws://127.0.0.1:8005/ws."""
    await _forward_websocket(websocket)


# All HTTP methods must be allowed for /api so POST login, etc. work (Starlette defaults to GET only).
_API_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


async def _shutdown_clients() -> None:
    """Close shared httpx clients on app shutdown."""
    global _frontend_client, _backend_client
    if _frontend_client and not _frontend_client.is_closed:
        await _frontend_client.aclose()
        _frontend_client = None
    if _backend_client and not _backend_client.is_closed:
        await _backend_client.aclose()
        _backend_client = None


@asynccontextmanager
async def _lifespan(app):
    """Starlette 1.0 removed the on_startup/on_shutdown __init__ args in favor of a lifespan context
    manager. Without this the proxy app raised `TypeError: ... unexpected keyword argument 'on_shutdown'`
    at create time, so the proxy never bound the LAN port. Close the shared httpx clients on shutdown so
    pooled connections don't leak across restarts."""
    yield
    await _shutdown_clients()


def create_proxy_app() -> ASGIApp:
    """Create the ASGI proxy application. WebSocket /ws must use WebSocketRoute so upgrades work.

    The admission check is the outermost layer: a refused client is answered before anything is
    relayed, and it writes nothing to the access log, which keeps one line per request and has no
    throttle; the refusal is the security event ``ip_blocked``, one per address per 5 seconds.
    Which guard answers follows Local Network per request, like the backend's
    (``NetworkModeLayers``): the admitted networks while it is on, this machine while it is off.
    """
    routes = [
        WebSocketRoute("/ws", endpoint=_ws_handler),
        # Agent rooms. Registered explicitly: Starlette's catch-all Route matches HTTP
        # scopes only, so without this line a websocket to /ws/a2a/... is answered with
        # HTTP 403 at the handshake and never reaches the backend at all - while working
        # perfectly on the desktop, which is handed ws://127.0.0.1:8005 directly and
        # never passes through here.
        WebSocketRoute("/ws/a2a/{room_id}", endpoint=_ws_handler),
        # The interactive browser's VNC stream. Same reason as the line above, and the
        # trap is sharper here: the path starts with /api, so the catch-all API Route
        # below LOOKS like it covers it - it does not, because that Route matches HTTP
        # scopes only, and the upgrade came back as HTTP 403 (measured) with the backend
        # route never reached. _WS_ALLOWED is the second gate; this is the first.
        WebSocketRoute("/api/browser-vnc/t/{ticket}/websockify", endpoint=_ws_handler),
        Route("/api", endpoint=_api_route, methods=_API_METHODS),
        Route("/api/{rest:path}", endpoint=_api_route, methods=_API_METHODS),
        Route("/sounds/{filename:path}", endpoint=_api_route, methods=["GET", "HEAD"]),
        Route("/{path:path}", endpoint=_proxy_handler, methods=_API_METHODS),
    ]
    logged = AccessLogMiddleware(Starlette(routes=routes, lifespan=_lifespan))
    return ByLocalNetwork(network=AdmissionGuard(logged), this_machine=this_machine_only(logged))


def run_https_proxy(
    host: str,
    port: int,
    ssl_certfile: str,
    ssl_keyfile: str,
    log_callback: Callable[[str, str], None] | None = None,
) -> None:
    """Run the HTTPS proxy (blocking). Call in a daemon thread."""
    def _log(msg: str, style: str = "info"):
        logger.info("[HTTPS Proxy] %s", msg)
        if log_callback:
            log_callback(msg, style)
    from vaf.network import runtime_status
    try:
        import uvicorn
        from vaf.network.binding import pick_bindable_port
        # Pick a bindable port: try the configured one, fall back to 8443 when it is privileged/taken.
        # 443 needs root on Linux/macOS — the old code only fell back on Windows, so on Linux the proxy
        # silently died and nothing listened on the LAN. The effective port is recorded so the UI shows
        # the REAL URL instead of a value merely computed from config.
        effective = pick_bindable_port(host, port, 8443)
        if effective is None:
            msg = (f"could not bind HTTPS port {port} or fallback 8443 — a privileged port (<1024) needs "
                   f"admin/root; pick a high port or open it during setup")
            _log(msg, "error")
            runtime_status.set_proxy_failed(port, msg)
            return
        if effective != port:
            _log(f"HTTPS port {port} not bindable (privileged?); using {effective} instead", "warning")
        # The machine's names for the HSTS scope (binding.own_name_labels), looked up here
        # before the first answer: with TLS on and the LAN switched off the proxy still runs,
        # and no certificate check at the start has done it.
        from vaf.network.binding import own_name_labels
        own_name_labels()
        app = create_proxy_app()
        from vaf.core.log_helper import WS_MAX_SIZE_BYTES, redacted_uvicorn_log_config
        # The uvicorn loggers are process-wide: a Config without the redacting log_config
        # re-runs dictConfig with uvicorn's defaults and strips the token filter from EVERY
        # server in the tray (8001 and 8005 included) - measured: full /ws?token=<jwt>
        # lines in tray_debug.log. TLS: the one policy of every listener (ssl_utils); the
        # head, handshake and connection limits and no forwarded-header trust: door.py.
        config = uvicorn.Config(
            app,
            host=host,
            port=effective,
            **server_tls_options(ssl_certfile, ssl_keyfile),
            **door.door_options(),
            log_level="info",
            use_colors=False,
            log_config=redacted_uvicorn_log_config(),
            # The FRONT door of the LAN path: without this the proxy re-capped
            # uploads at uvicorn's 16 MB default no matter what the backend allowed.
            ws_max_size=WS_MAX_SIZE_BYTES,
        )
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None
        global _running_server
        _running_server = server  # so stop_https_proxy() (LAN-disable / restart) can shut it down
        _log(f"HTTPS proxy listening on https://{host}:{effective} (-> 3000, 8005)")
        _write_access(f"PROXY STARTED  host={host} port={effective} cert={ssl_certfile}")
        runtime_status.set_proxy_bound(effective, port)
        loop = door.door_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.serve())
        _running_server = None
    except Exception as e:
        _log(f"HTTPS proxy failed: {e}", "error")
        logger.exception("HTTPS proxy error")
        try:
            runtime_status.set_proxy_failed(port, str(e))
        except Exception:
            pass
