# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Authentication, IP validation and origin middleware for the web server.

Middleware stack as ``vaf/core/web_server.py`` registers it (outermost -> innermost; Starlette
puts the LAST ``add_middleware`` call outermost):
  SecurityHeaders -> ForeignOriginGuard -> AuthMiddleware -> IPValidationMiddleware
    -> RateLimitMiddleware -> OwnOriginCORSMiddleware -> route handler
The three in the middle exist only in network mode; the guard and CORS always.

ForeignOriginGuard refuses what a browser marks as coming from another web page (HTTP and
WebSocket alike), before any identity is looked at.
IPValidationMiddleware rejects any client IP that is not RFC 1918 or localhost.
AuthMiddleware enforces JWT authentication for non-localhost clients.
Public paths (login, bootstrap, needs-setup, static assets) are exempt from auth.
"""

import logging
from typing import Callable

from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.websockets import WebSocketClose

from vaf.core.config import Config

logger = logging.getLogger(__name__)

# Paths that do NOT require authentication (login flow, health check, static)
AUTH_EXEMPT_PATHS: set[str] = {
    "/api/auth/needs-setup",
    "/api/auth/bootstrap",
    "/api/auth/login",
    "/api/auth/verify-2fa",
    "/api/auth/refresh",
    "/api/auth/setup-2fa",
    # First-run only: the onboarding wizard live-tests a Veyllo key BEFORE /bootstrap, so no token
    # can exist yet. The endpoint gates itself (403 once an admin exists) and is rate-limited, so
    # exempting it opens nothing after setup. Needed for a headless/LAN first run, where the
    # tokenless-localhost path no longer covers a browser on another device.
    "/api/auth/test-veyllo-key",
    "/api/network/ws-config",  # So frontend can build wss:// URL when TLS is on
    # The A2A guest lane: a foreign harness holds a join ticket, not an account,
    # so it cannot authenticate here - and must not need to. Both files are
    # public by design (the CA certificate's private key never leaves this
    # machine; the client file is public repository content), and the invitation
    # carries their checksums by another route, which is what makes fetching
    # them over an unverified channel safe for the guest.
    "/api/a2a/client.py",
    "/api/a2a/ca.pem",
    "/docs",
    "/openapi.json",
}

AUTH_EXEMPT_PREFIXES: tuple[str, ...] = (
    "/_next/",
    "/static/",
    "/favicon",
    # The room workspace lane for remote SEAT holders (list/fetch/push). A seat
    # is a room credential, not an account, so these routes cannot pass the JWT
    # gate - they authenticate every request themselves against the room's own
    # seat record (web_server._a2a_workspace_for_seat) and refuse without one.
    "/api/a2a/rooms/",
    # The interactive-browser stream lane. The KasmVNC client is loaded in a
    # cross-origin iframe, so the SameSite JWT cookie cannot ride along on its
    # asset and socket requests - instead the lease TICKET in the path is the
    # credential, validated on every request against the current lease
    # (vaf/core/browser_interactive.py). No ticket, no bytes.
    "/api/browser-vnc/t/",
)


def _is_auth_exempt(path: str) -> bool:
    """Check if a request path is exempt from authentication."""
    if path in AUTH_EXEMPT_PATHS:
        return True
    return path.startswith(AUTH_EXEMPT_PREFIXES)


# ---------------------------------------------------------------------------
# Origin guard (always on, every mode)
# ---------------------------------------------------------------------------

class ForeignOriginGuard:
    """Refuse a request a browser marks as coming from a page that is not VAF's own.

    Why this is a door and not a route check: the trust it protects is handed out in many
    places. A tokenless request from this machine is the owner (the local-admin fallback lives
    in route helpers all over ``vaf/api``, and in single-user mode AuthMiddleware is not even
    registered), and the ``vaf_token`` cookie rides along to every localhost port. Neither can
    tell the desktop from another page open in the same browser; only the browser's own marks
    can (``Origin``, ``Sec-Fetch-Site``, the ``Host`` it dialled), and those are read ONCE here,
    by ``vaf.network.binding.foreign_request_reason``.

    Pure ASGI rather than BaseHTTPMiddleware because the WebSocket handshake is the same door:
    ``/ws`` accepts the cookie, so a page on another localhost port could open it with the
    owner's session. A refused socket is closed before accept (4003), which the browser sees as
    a failed handshake. A client that sends no browser marks (CLI, sub-agent IPC, the tray, a
    script) is not judged and passes unchanged.

    Named boundary: the guard is plain ASGI and depends on nothing in the web server, so a
    second front door (an embedder serving an agent over HTTP) could mount it as it is. There is
    one door today, so it is not on the framework surface.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        from vaf.network.binding import effective_client_ip, foreign_request_reason

        headers = Headers(scope=scope)
        reason = foreign_request_reason(headers, scheme=scope.get("scheme") or "http",
                                        method=scope.get("method") or "GET")
        if reason is None:
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        ip = effective_client_ip(client[0] if client else None, headers.get("x-forwarded-for"))
        path = scope.get("path") or ""
        shown = headers.get("origin") or headers.get("x-forwarded-host") or headers.get("host") or ""
        logger.warning("Refused %s from another origin (%s: %s) %s", scope["type"], reason, shown, path)
        _emit_security_event("foreign_origin_blocked", ip=ip, path=path, detail=f"{reason}: {shown[:120]}")
        if scope["type"] == "websocket":
            await WebSocketClose(code=4003, reason="Request from another web origin refused")(
                scope, receive, send)
            return
        await JSONResponse(status_code=403,
                           content={"detail": "Request from another web origin refused"})(
            scope, receive, send)


class OwnOriginCORSMiddleware(CORSMiddleware):
    """CORS for the one origin that calls the backend cross-origin: the Web UI on this machine.

    Everything else reaches the backend same-origin (the Next.js /api door, the HTTPS proxy)
    and needs no CORS at all. This used to be a regex admitting every localhost and RFC 1918
    origin WITH credentials, which let any page from those addresses read the owner's answers.
    """

    def __init__(self, app) -> None:
        super().__init__(app, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

    def is_allowed_origin(self, origin: str) -> bool:
        from vaf.network.binding import is_own_frontend_origin
        return is_own_frontend_origin(origin)


# ---------------------------------------------------------------------------
# Layer 2: IP Validation Middleware
# ---------------------------------------------------------------------------

class IPValidationMiddleware(BaseHTTPMiddleware):
    """
    Reject requests from non-private IP addresses.

    Only RFC 1918 ranges (10.x, 172.16-31.x, 192.168.x) and localhost
    are allowed.  Everything else gets a 403.
    """

    async def dispatch(self, request: Request, call_next: Callable):
        client_ip = request.client.host if request.client else "unknown"

        try:
            from vaf.network.binding import is_allowed_ip
            if not is_allowed_ip(client_ip):
                logger.warning("Blocked non-private IP: %s %s", client_ip, request.url.path)
                _emit_security_event("ip_blocked", ip=client_ip, path=request.url.path)
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Access denied: only local network clients are allowed"},
                )
        except ImportError:
            # Fallback: only allow obvious localhost
            if client_ip not in ("127.0.0.1", "::1", "localhost"):
                logger.warning("Blocked IP (binding module unavailable): %s", client_ip)
                _emit_security_event("ip_blocked", ip=client_ip, path=request.url.path)
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Access denied"},
                )

        return await call_next(request)


# ---------------------------------------------------------------------------
# Layer 3: JWT Authentication Middleware
# ---------------------------------------------------------------------------

async def token_account_stands(payload: dict) -> bool:
    """Whether the account behind a decoded access token still stands.

    A valid signature is not enough: the account must still exist, be active and hold the
    role the token was issued with, or a deactivation would leave a day of access and a
    demotion would leave admin rights for the token's whole lifetime. Both lanes ask this
    one function - the HTTP middleware below and the WebSocket handshake
    (`vaf/core/web_server.py`). An auth store that cannot be asked keeps the token
    (`permissions.token_still_stands`: the desktop default)."""
    from vaf.auth.permissions import account_standing_async, token_still_stands
    return token_still_stands(await account_standing_async(payload.get("user_scope_id")),
                              payload.get("role"))


class AuthMiddleware(BaseHTTPMiddleware):
    """
    Enforce JWT authentication for non-localhost network clients.

    Localhost clients are allowed without a token (backward-compatible with
    single-user desktop mode).  Network clients must present a valid JWT
    either as a Bearer token or a ``vaf_token`` cookie.
    """

    COOKIE_NAME = "vaf_token"

    async def dispatch(self, request: Request, call_next: Callable):
        # Skip auth for exempt paths (login, static, etc.)
        if _is_auth_exempt(request.url.path):
            return await call_next(request)

        # NOTE: real WebSocket handshakes are scope=="websocket" and never reach this
        # HTTP middleware (BaseHTTPMiddleware skips non-http scopes); the /ws route
        # self-authenticates. An "Upgrade: websocket" header on an HTTP-scope request
        # is therefore only ever an auth-bypass attempt — it must NOT skip auth.
        peer_ip = request.client.host if request.client else "unknown"

        try:
            from vaf.network.binding import effective_client_ip, is_localhost
        except ImportError:
            def is_localhost(ip: str) -> bool:
                return ip in ("127.0.0.1", "::1", "localhost")

            def effective_client_ip(peer: str, forwarded_for: str | None) -> str:
                if not is_localhost(peer):
                    return peer
                return ((forwarded_for or "").split(",")[0] or "").strip() or peer

        # A 127.0.0.1 peer is NOT proof of a local client: the integrated HTTPS proxy terminates TLS
        # on 0.0.0.0 and relays every LAN device to the backend over loopback, so request.client.host
        # is 127.0.0.1 for remote users too — which made every tokenless LAN request pass the checks
        # below and reach the route-level local-admin floors. Resolve the REAL client from the
        # proxy-authored X-Forwarded-For instead (the proxy strips any client-supplied copy first, so
        # a hop can only be ADDED, never removed). Still local, still tokenless, still working:
        # internal loopback IPC (/api/subagent/stream, /api/workflow/update, /api/heartbeat), the
        # desktop via the Next.js /api route (sets no forwarding header) and the same-host OAuth
        # callback (relayed hop is 127.0.0.1). A LAN client without a valid token now gets 401.
        client_ip = effective_client_ip(peer_ip, request.headers.get("x-forwarded-for"))

        token = _extract_token(request)

        # Honor a presented JWT regardless of the peer IP. The integrated HTTPS proxy forwards LAN
        # clients to the backend over loopback (the backend binds 127.0.0.1), so a "localhost" peer
        # may actually be a remote user. Previously a localhost peer returned here BEFORE the token
        # was read, so an authenticated LAN user's token was ignored and downstream fell back to the
        # local admin scope — that is the cross-user data leak (one user seeing another's RAG/sessions).
        # Now: a valid token always establishes the real identity. request.state.user is left unset for
        # a tokenless localhost request, so internal loopback IPC and the single-user desktop keep
        # working without a token (those non-user-data paths do not rely on an identity).
        if token:
            payload = None
            try:
                from vaf.auth.crypto import decode_token
                payload = decode_token(token)
            except Exception as e:
                logger.warning("Auth middleware token decode error for %s: %s", client_ip, e)
                payload = None

            if payload and payload.get("type") == "access" and not await token_account_stands(payload):
                payload = None

            if payload and payload.get("type") == "access":
                # Optional: enforce 2FA verification
                require_2fa = Config.get("local_network_require_2fa", True)
                if require_2fa and payload.get("requires_2fa_setup"):
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "2FA setup required before accessing resources"},
                    )

                # Attach user info to request state for downstream handlers
                request.state.user_id = payload.get("sub")
                request.state.username = payload.get("username")
                request.state.role = payload.get("role")
                request.state.user_scope_id = payload.get("user_scope_id")

                # Consolidated dict for API route handlers (they read request.state.user)
                request.state.user = {
                    "user_id": payload.get("sub"),
                    "username": payload.get("username"),
                    "role": payload.get("role"),
                    "user_scope_id": payload.get("user_scope_id"),
                }
                return await call_next(request)

            # Token present but invalid/expired: a network client is rejected; a localhost client
            # (local desktop with a stale cookie) is not locked out — it falls through to the
            # tokenless localhost path below rather than getting a hard 401.
            if not is_localhost(client_ip):
                _emit_security_event("token_rejected", ip=client_ip, path=request.url.path)
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Invalid or expired token"},
                )

        # No valid identity established.
        if is_localhost(client_ip):
            return await call_next(request)
        _emit_security_event("unauthenticated_blocked", ip=client_ip, path=request.url.path)
        return JSONResponse(
            status_code=401,
            content={"detail": "Authentication required"},
        )


def _emit_security_event(kind: str, **fields) -> None:
    """Record a rejected access attempt in the security event log (dashboard +
    security_<date>.log). Lazy import + swallow-all: auditing must never be able
    to break or slow the request path. The writer itself throttles floods."""
    try:
        from vaf.core.security_events import log_security_event
        log_security_event(kind, **fields)
    except Exception:
        pass


def _extract_token(request: Request) -> str | None:
    """Extract JWT from Authorization header or cookie."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:].strip()

    return request.cookies.get(AuthMiddleware.COOKIE_NAME)
