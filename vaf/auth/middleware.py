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

        # Skip WebSocket upgrade - the WS handler does its own auth
        if request.headers.get("upgrade", "").lower() == "websocket":
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"

        # Localhost is always allowed without token
        try:
            from vaf.network.binding import is_localhost
        except ImportError:
            def is_localhost(ip: str) -> bool:
                return ip in ("127.0.0.1", "::1", "localhost")

        if is_localhost(client_ip):
            return await call_next(request)

        # --- Network client: require valid JWT ---
        token = _extract_token(request)
        if not token:
            return JSONResponse(
                status_code=401,
                content={"detail": "Authentication required"},
            )

        try:
            from vaf.auth.crypto import decode_token
            payload = decode_token(token)
            if not payload or payload.get("type") != "access":
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Invalid or expired token"},
                )

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

        except Exception as e:
            logger.warning("Auth middleware error for %s: %s", client_ip, e)
            return JSONResponse(
                status_code=401,
                content={"detail": "Authentication failed"},
            )

        return await call_next(request)


def _extract_token(request: Request) -> str | None:
    """Extract JWT from Authorization header or cookie."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:].strip()

    return request.cookies.get(AuthMiddleware.COOKIE_NAME)
