# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
The admission check as an ASGI door: refuse every client that is not admitted.

``AdmissionGuard(app)`` judges the REAL client of every HTTP request and every WebSocket
handshake (``binding.connection_client_ip``, so a client relayed over loopback is judged as
the device the relay named) against ``binding.is_allowed_ip``, the one answer to "who may
connect" (``inbound_policy()``: this machine, the local networks or with "VPN only" the VPN
networks, plus the private networks an admin added; never a public address). A client that
is not admitted gets HTTP 403, or a WebSocket close 4003 before accept, which a browser sees
as a failed handshake; the app behind it is never called.

Why a pure ASGI middleware and both scope types: Starlette's ``BaseHTTPMiddleware`` passes
every scope that is not ``http`` through unjudged, so an address check written as one left
every WebSocket route open (measured: HTTP 403, while the socket behind the same check
opened and answered).

Each refusal is the security event ``ip_blocked`` with the path in ``detail``: the event
writer throttles per source and kind, and ``path`` is part of its key, so a scanner walking
fifty paths would write fifty lines; in ``detail`` it writes one per address per 5 seconds.

Named boundaries: the TLS handshake happens before any HTTP check, so a client that reaches
the access port still receives the certificate (whose names list the LAN and VPN addresses
and the host name); only a firewall, or a bind to the admitted interfaces only, closes that.
A per-interface bind is not built: a VPN that comes up later would need a rebind.

Not imported by ``vaf.network``: starlette belongs to the server extra, and ``import
vaf.network`` must keep working on a slim install.
"""

import logging
from typing import Callable, Optional

from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.websockets import WebSocketClose

from vaf.network import binding

logger = logging.getLogger(__name__)

REFUSED_DETAIL = "Access denied: only local network clients are allowed"
REFUSED_SOCKET_REASON = "Local network only"


class AdmissionGuard:
    """Refuse every HTTP request and WebSocket handshake from a client that is not admitted.

    ``admits`` decides over the resolved client address; it defaults to
    ``binding.is_allowed_ip``, read at call time so a change of the admitted networks applies
    to the next request. ``reason`` replaces the refusal's wording (the HTTP ``detail`` and the
    WebSocket close reason) when a door admits by another rule.
    """

    def __init__(self, app, *, admits: Optional[Callable[[str], bool]] = None,
                 reason: Optional[str] = None) -> None:
        self.app = app
        self._admits = admits
        self._reason = reason

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        ip = binding.connection_client_ip(HTTPConnection(scope))
        admits = self._admits or binding.is_allowed_ip
        if admits(ip):
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or ""
        logger.debug("Refused %s from %s: not admitted (%s)", scope["type"], ip, path)
        _record_refusal(ip, path)
        if scope["type"] == "websocket":
            await WebSocketClose(code=4003, reason=self._reason or REFUSED_SOCKET_REASON)(
                scope, receive, send)
            return
        await JSONResponse(status_code=403, content={"detail": self._reason or REFUSED_DETAIL})(
            scope, receive, send)


def _record_refusal(ip: str, path: str) -> None:
    """The refusal in the security log. Never raises: auditing must not break the door."""
    try:
        from vaf.core.security_events import log_security_event
        log_security_event("ip_blocked", ip=ip, detail=path[:200])
    except Exception:
        pass
