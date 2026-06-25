# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Rate limiting middleware for login endpoints.

Tracks failed login attempts per IP address and blocks IPs that exceed
the configured threshold within a sliding time window.

Config keys:
  local_network_rate_limit_attempts       - max attempts (default 5)
  local_network_rate_limit_window_minutes  - window in minutes (default 15)
"""

import logging
import time
from collections import defaultdict
from threading import Lock
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from vaf.core.config import Config

logger = logging.getLogger(__name__)

# Paths subject to rate limiting
_RATE_LIMITED_PATHS: set[str] = {
    "/api/auth/login",
    "/api/auth/bootstrap",
    "/api/auth/verify-2fa",
}


class _AttemptTracker:
    """Thread-safe tracker of failed login attempts per IP."""

    def __init__(self):
        self._lock = Lock()
        # ip -> list of timestamps of failed attempts
        self._attempts: dict[str, list[float]] = defaultdict(list)

    def record_failure(self, ip: str) -> None:
        with self._lock:
            self._attempts[ip].append(time.monotonic())

    def is_blocked(self, ip: str, max_attempts: int, window_seconds: float) -> bool:
        with self._lock:
            now = time.monotonic()
            cutoff = now - window_seconds
            # Prune old entries
            self._attempts[ip] = [t for t in self._attempts[ip] if t > cutoff]
            return len(self._attempts[ip]) >= max_attempts

    def clear(self, ip: str) -> None:
        with self._lock:
            self._attempts.pop(ip, None)


_tracker = _AttemptTracker()


def record_login_failure(ip: str) -> None:
    """Record a failed login attempt (called from auth_routes on 401)."""
    _tracker.record_failure(ip)
    logger.info("Rate-limit: recorded failed attempt for %s", ip)


def clear_login_failures(ip: str) -> None:
    """Clear failure count after successful login."""
    _tracker.clear(ip)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Block login requests from IPs that have exceeded the failure threshold.

    Only applies to login-related endpoints.  Other paths pass through
    unaffected.
    """

    async def dispatch(self, request: Request, call_next: Callable):
        path = request.url.path

        if path not in _RATE_LIMITED_PATHS:
            return await call_next(request)

        # Only rate-limit POST requests (the actual login attempts)
        if request.method != "POST":
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"

        max_attempts = int(Config.get("local_network_rate_limit_attempts", 5))
        window_minutes = int(Config.get("local_network_rate_limit_window_minutes", 15))
        window_seconds = window_minutes * 60

        if _tracker.is_blocked(client_ip, max_attempts, window_seconds):
            logger.warning(
                "Rate-limit: blocked %s on %s (%d attempts in %d min)",
                client_ip, path, max_attempts, window_minutes,
            )
            return JSONResponse(
                status_code=429,
                content={
                    "detail": f"Too many failed attempts. Try again in {window_minutes} minutes.",
                },
                headers={"Retry-After": str(int(window_seconds))},
            )

        response = await call_next(request)

        # Record failure if the response was 401 (bad credentials)
        if response.status_code == 401:
            _tracker.record_failure(client_ip)

        # Clear failures on successful login
        if response.status_code == 200 and path == "/api/auth/login":
            _tracker.clear(client_ip)

        return response
