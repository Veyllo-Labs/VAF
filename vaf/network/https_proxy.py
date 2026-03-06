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
from typing import Callable

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

logger = logging.getLogger(__name__)

FRONTEND_ORIGIN = "http://127.0.0.1:3000"
# Internal HTTP channel (8005) is always running when this proxy runs (TLS on); 8001 is HTTPS-only
BACKEND_ORIGIN = "http://127.0.0.1:8005"


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
        async with httpx.AsyncClient(timeout=60.0) as client:
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
    if not any(h.lower() == "content-length" for h in response_headers):
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
    try:
        async with websockets.connect(backend_uri, additional_headers=extra_headers or None) as backend_ws:
            async def from_backend():
                try:
                    async for msg in backend_ws:
                        if isinstance(msg, str):
                            await websocket.send_text(msg)
                        else:
                            await websocket.send_bytes(msg)
                except Exception as exc:
                    logger.debug("HTTPS proxy from_backend relay ended: %s", exc)
            async def from_client():
                try:
                    while True:
                        data = await websocket.receive_text()
                        await backend_ws.send(data)
                except Exception as exc:
                    logger.debug("HTTPS proxy from_client relay ended: %s", exc)
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


def create_proxy_app() -> Starlette:
    """Create the ASGI proxy application. WebSocket /ws must use WebSocketRoute so upgrades work."""
    routes = [
        WebSocketRoute("/ws", endpoint=_ws_handler),
        Route("/api", endpoint=_api_route, methods=_API_METHODS),
        Route("/api/{rest:path}", endpoint=_api_route, methods=_API_METHODS),
        # Static assets (/_next/*, etc.) and pages need GET/HEAD; allow all for compatibility
        Route("/{path:path}", endpoint=_proxy_handler, methods=_API_METHODS),
    ]
    return Starlette(routes=routes)


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
    try:
        import uvicorn
        app = create_proxy_app()
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            ssl_certfile=ssl_certfile,
            ssl_keyfile=ssl_keyfile,
            log_level="warning",
            use_colors=False,
        )
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None
        _log(f"HTTPS proxy listening on https://0.0.0.0:{port} (-> 3000, 8005)")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.serve())
    except Exception as e:
        _log(f"HTTPS proxy failed: {e}", "error")
        logger.exception("HTTPS proxy error")
