"""
Integrated HTTPS reverse proxy for VAF (no Nginx required).

When local_network_enabled and local_network_tls_enabled, this proxy listens on
0.0.0.0:443 (or local_network_https_port) with SSL and forwards:
  - /api/* and /ws -> http://127.0.0.1:8005 (internal HTTP channel; 8001 is HTTPS-only when TLS on)
  - everything else -> http://127.0.0.1:3000 (frontend)

Best practice: single entry point for HTTPS, TLS termination here.
"""

import asyncio
import logging
import ssl
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route, WebSocketRoute
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket

logger = logging.getLogger(__name__)


def _access_log_path() -> Path:
    """Return path for the proxy access log (logs/https_proxy_access_<date>.log)."""
    from vaf.core.platform import Platform
    log_dir = Platform.data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"https_proxy_access_{datetime.now().strftime('%Y-%m-%d')}.log"


def _write_access(line: str) -> None:
    """Append a timestamped line to the proxy access log."""
    try:
        ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        with open(_access_log_path(), "a", encoding="utf-8") as f:
            f.write(f"{ts}  {line}\n")
    except Exception:
        pass


class AccessLogMiddleware:
    """ASGI middleware that logs every incoming request/connection to a file."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            client = scope.get("client") or ("?", 0)
            method = scope.get("method", "?")
            path = scope.get("path", "/")
            _write_access(f"HTTP  {client[0]}:{client[1]}  {method} {path}")
        elif scope["type"] == "websocket":
            client = scope.get("client") or ("?", 0)
            path = scope.get("path", "/ws")
            _write_access(f"WS    {client[0]}:{client[1]}  {path}")
        elif scope["type"] == "lifespan":
            _write_access("LIFESPAN event")
        else:
            client = scope.get("client") or ("?", 0)
            _write_access(f"OTHER type={scope['type']}  {client[0]}:{client[1]}")
        await self.app(scope, receive, send)

FRONTEND_ORIGIN = "http://127.0.0.1:3000"
# Internal HTTP channel (8005) is always running when this proxy runs (TLS on); 8001 is HTTPS-only
BACKEND_ORIGIN = "http://127.0.0.1:8005"

# Shared httpx clients for connection pooling — reuse TCP connections across requests
# instead of opening a new connection for every single resource (JS chunks, CSS, images, etc.)
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


async def _get_client(target_origin: str) -> "httpx.AsyncClient":  # noqa: F821
    """Return a shared httpx.AsyncClient for the target, with connection pooling."""
    import httpx
    global _frontend_client, _backend_client
    if target_origin == FRONTEND_ORIGIN:
        if _frontend_client is None or _frontend_client.is_closed:
            _frontend_client = httpx.AsyncClient(
                timeout=60.0,
                limits=httpx.Limits(max_connections=50, max_keepalive_connections=20, keepalive_expiry=30),
            )
        return _frontend_client
    else:
        if _backend_client is None or _backend_client.is_closed:
            _backend_client = httpx.AsyncClient(
                timeout=60.0,
                limits=httpx.Limits(max_connections=50, max_keepalive_connections=20, keepalive_expiry=30),
            )
        return _backend_client


def _normalize_headers_for_upstream(headers: dict, target_origin: str, original_host: str) -> None:
    """Drop hop-by-hop, set forward headers, set single Host to target (remove any existing host)."""
    for h in ("connection", "transfer-encoding", "keep-alive", "te", "trailer", "upgrade", "proxy-authorization"):
        headers.pop(h, None)
    for k in list(headers):
        if k.lower() == "host":
            del headers[k]
    from urllib.parse import urlparse
    parsed = urlparse(target_origin)
    headers["Host"] = parsed.netloc or "127.0.0.1:3000"
    headers["X-Forwarded-Proto"] = "https"
    headers["X-Forwarded-Host"] = original_host or "127.0.0.1"


async def _forward_http(request: Request, target_origin: str) -> Response:
    """Forward HTTP request to target and return response."""
    import httpx
    path = (request.scope.get("path") or request.url.path or "/").strip()
    if not path.startswith("/"):
        path = "/" + path
    url = target_origin.rstrip("/") + (path or "/")
    if request.url.query:
        url += "?" + request.url.query
    headers = dict(request.headers)
    original_host = request.headers.get("host") or request.headers.get("Host") or "127.0.0.1"
    _normalize_headers_for_upstream(headers, target_origin, original_host)
    if request.client:
        headers["X-Forwarded-For"] = request.client.host
    try:
        body = await request.body()
    except Exception:
        body = b""
    try:
        client = await _get_client(target_origin)
        resp = await client.request(
            request.method,
            url,
            headers=headers,
            content=body,
        )
    except Exception as e:
        logger.warning("HTTPS proxy forward failed %s %s -> %s: %s", request.method, path, target_origin, e)
        return Response(content=b"Bad Gateway", status_code=502)
    response_headers = dict(resp.headers)
    for h in ("connection", "transfer-encoding", "keep-alive", "te", "trailer", "upgrade"):
        response_headers.pop(h, None)
    # httpx returns decoded body; strip Content-Encoding so client does not double-decode
    for k in list(response_headers):
        if k.lower() == "content-encoding":
            del response_headers[k]
            break
    body_bytes = resp.content
    # Always set Content-Length from actual (decompressed) body to avoid mismatches
    response_headers["Content-Length"] = str(len(body_bytes))
    return Response(
        content=body_bytes,
        status_code=resp.status_code,
        headers=response_headers,
    )


def _get_cookie_header(scope: dict) -> str | None:
    """Extract Cookie header from ASGI scope so we can forward it to the backend."""
    for (name, value) in scope.get("headers") or []:
        if name.lower() == b"cookie" and value:
            return value.decode("latin-1")
    return None


async def _forward_websocket(websocket: WebSocket) -> None:
    """Accept client WS and relay to backend ws://127.0.0.1:8005/ws (internal HTTP channel)."""
    import websockets
    await websocket.accept()
    backend_uri = "ws://127.0.0.1:8005/ws"
    if websocket.url.query:
        backend_uri += "?" + websocket.url.query
    extra_headers = []
    cookie = _get_cookie_header(websocket.scope)
    if cookie:
        extra_headers.append(("Cookie", cookie))
    # Forward the REAL client IP so the backend's connection tracker (the network map) records the actual
    # LAN device instead of 127.0.0.1 (this proxy). The backend reads X-Forwarded-For for the WS too.
    if websocket.client and websocket.client.host:
        extra_headers.append(("X-Forwarded-For", websocket.client.host))
    try:
        async with websockets.connect(backend_uri, additional_headers=extra_headers or None) as backend_ws:
            async def from_backend():
                try:
                    async for msg in backend_ws:
                        if isinstance(msg, str):
                            await websocket.send_text(msg)
                        else:
                            await websocket.send_bytes(msg)
                except Exception:
                    pass
            async def from_client():
                try:
                    while True:
                        data = await websocket.receive_text()
                        await backend_ws.send(data)
                except Exception:
                    pass
            await asyncio.gather(from_backend(), from_client())
    except Exception as e:
        logger.warning("HTTPS proxy WebSocket backend connect failed: %s", e)
    finally:
        try:
            await websocket.close()
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


def create_proxy_app() -> Starlette:
    """Create the ASGI proxy application. WebSocket /ws must use WebSocketRoute so upgrades work."""
    routes = [
        WebSocketRoute("/ws", endpoint=_ws_handler),
        Route("/api", endpoint=_api_route, methods=_API_METHODS),
        Route("/api/{rest:path}", endpoint=_api_route, methods=_API_METHODS),
        Route("/sounds/{filename:path}", endpoint=_api_route, methods=["GET", "HEAD"]),
        Route("/{path:path}", endpoint=_proxy_handler, methods=_API_METHODS),
    ]
    app = Starlette(routes=routes, lifespan=_lifespan)
    return AccessLogMiddleware(app)


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
        app = create_proxy_app()
        # Compatibility: ensure TLS 1.2 clients can connect.
        # Some devices fail with ERR_EMPTY_RESPONSE if only TLS 1.3 is effectively negotiated.
        config = uvicorn.Config(
            app,
            host=host,
            port=effective,
            ssl_certfile=ssl_certfile,
            ssl_keyfile=ssl_keyfile,
            ssl_version=ssl.PROTOCOL_TLS_SERVER,
            ssl_ciphers="DEFAULT",
            log_level="info",
            use_colors=False,
        )
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None
        global _running_server
        _running_server = server  # so stop_https_proxy() (LAN-disable / restart) can shut it down
        _log(f"HTTPS proxy listening on https://{host}:{effective} (-> 3000, 8005)")
        _write_access(f"PROXY STARTED  host={host} port={effective} cert={ssl_certfile}")
        runtime_status.set_proxy_bound(effective, port)
        loop = asyncio.new_event_loop()
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
