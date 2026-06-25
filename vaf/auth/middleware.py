# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Authentication & IP validation middleware for local network mode.

Middleware stack (outermost -> innermost):
  RateLimitMiddleware  ->  IPValidationMiddleware  ->  AuthMiddleware  ->  route handler

IPValidationMiddleware rejects any client IP that is not RFC 1918 or localhost.
AuthMiddleware enforces JWT authentication for non-localhost clients.
Public paths (login, bootstrap, needs-setup, static assets) are exempt from auth.
"""

import logging
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

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
    "/api/network/ws-config",  # So frontend can build wss:// URL when TLS is on
    "/docs",
    "/openapi.json",
}

AUTH_EXEMPT_PREFIXES: tuple[str, ...] = (
    "/_next/",
    "/static/",
    "/favicon",
)


def _is_auth_exempt(path: str) -> bool:
    """Check if a request path is exempt from authentication."""
    if path in AUTH_EXEMPT_PATHS:
        return True
    return path.startswith(AUTH_EXEMPT_PREFIXES)


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
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Access denied: only local network clients are allowed"},
                )
        except ImportError:
            # Fallback: only allow obvious localhost
            if client_ip not in ("127.0.0.1", "::1", "localhost"):
                logger.warning("Blocked IP (binding module unavailable): %s", client_ip)
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Access denied"},
                )

        return await call_next(request)


# ---------------------------------------------------------------------------
# Layer 3: JWT Authentication Middleware
# ---------------------------------------------------------------------------

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
        client_ip = request.client.host if request.client else "unknown"

        try:
            from vaf.network.binding import is_localhost
        except ImportError:
            def is_localhost(ip: str) -> bool:
                return ip in ("127.0.0.1", "::1", "localhost")

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
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Invalid or expired token"},
                )

        # No valid identity established.
        if is_localhost(client_ip):
            return await call_next(request)
        return JSONResponse(
            status_code=401,
            content={"detail": "Authentication required"},
        )


def _extract_token(request: Request) -> str | None:
    """Extract JWT from Authorization header or cookie."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:].strip()

    return request.cookies.get(AuthMiddleware.COOKIE_NAME)
