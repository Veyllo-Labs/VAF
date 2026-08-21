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
