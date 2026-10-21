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
is not admitted gets HTTP 403 (with ``Connection: close`` when it sends no body), or a
WebSocket close 4003 before accept, which a browser sees as a failed handshake; the app
behind it is never called.

What a refused person sees: a browser that loads a page (a GET outside ``/api`` that accepts
``text/html``) gets a short HTML page that says which networks VAF answers and what to do,
without any address or internal name; an API call gets the same sentence as JSON
(``{"detail": ...}``), a socket a short close reason. The JSON body used to be the only
answer, so a person who opened the login page from a network that is not admitted (a mesh
VPN not admitted yet, the home network under "VPN only") read raw JSON that named the local
network alone and gave no way forward.

Why a pure ASGI middleware and both scope types: Starlette's ``BaseHTTPMiddleware`` passes
every scope that is not ``http`` through unjudged, so an address check written as one left
every WebSocket route open (measured: HTTP 403, while the socket behind the same check
opened and answered).

Each refusal is the security event ``ip_blocked`` with the path in ``detail``: the event
writer throttles per source and kind, and ``path`` is part of its key, so a scanner walking
fifty paths would write fifty lines; in ``detail`` it writes one per address per 5 seconds.

``ByLocalNetwork`` picks a door per request by the Local Network setting, and
``this_machine_only`` is the door while it is off: VAF's HTTPS proxy and its backend both
follow the setting through them, so the proxy, which outlives a switch to off until the
restart stops it, refuses other devices exactly when the backend does.

Named boundaries: the TLS handshake happens before any HTTP check, so a client that reaches
the access port still receives the certificate (whose names list the LAN and VPN addresses
and the host name); only a firewall, or a bind to the admitted interfaces only, closes that.
A per-interface bind is not built: a VPN that comes up later would need a rebind. The page
is English only: it is answered before any session or language exists. It follows the
system's color scheme (`prefers-color-scheme`), not the theme chosen in VAF: that choice
lives in the browser's storage, and reading it takes a script, which the page does not run.

Not imported by ``vaf.network``: starlette belongs to the server extra, and ``import
vaf.network`` must keep working on a slim install.
"""

import html
import logging
import re
from typing import Callable, Optional

from starlette.datastructures import Headers
from starlette.requests import HTTPConnection
from starlette.responses import HTMLResponse, JSONResponse
from starlette.websockets import WebSocketClose

from vaf.network import binding

logger = logging.getLogger(__name__)

# The refusal, said in one place: the JSON detail and the page's first sentence. It names the
# VPN as well, because under "VPN only" the local network is refused too.
REFUSED_DETAIL = ("VAF answers only this machine and the networks it admits, its local network "
                  "or a VPN, and this device's address is not on one of them.")
# The page's second sentence: what to do. Never on the API answer, which a program reads.
REFUSED_NEXT_STEP = ("Connect through the VPN, or run `vaf server status` on the machine VAF "
                     "runs on: it lists the addresses to open.")
REFUSED_SOCKET_REASON = "Not an admitted network"

# The same while Local Network is off and only this machine is served; the first sentence is
# short enough to be the socket's close reason as well.
LOCAL_NETWORK_OFF = "Local Network is off: VAF answers this machine only."
LOCAL_NETWORK_OFF_NEXT_STEP = ("Whoever runs VAF can switch Local Network on in Settings, or "
                               "with `vaf server on` on that machine.")

# A WebSocket close reason is at most 123 bytes (RFC 6455 5.5: a control frame carries 125,
# two of them the code); a longer one fails the close itself.
_SOCKET_REASON_MAX_BYTES = 123

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>VAF is not reachable from here</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ margin: 0; padding: 15vh 16px; background: #ffffff; color: #1f2937;
        font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }}
main {{ max-width: 34rem; margin: 0 auto; }}
h1 {{ font-size: 1.25rem; margin: 0 0 1rem; }}
code {{ font-size: 0.95em; padding: 0 0.2em; border-radius: 3px; background: #f3f4f6;
        white-space: nowrap; }}
@media (prefers-color-scheme: dark) {{
  body {{ background: #111827; color: #e5e7eb; }}
  code {{ background: #1f2937; }}
}}
</style>
</head>
<body>
<main>
<h1>VAF is not reachable from here</h1>
{paragraphs}
</main>
</body>
</html>
"""

# The page has no script, loads nothing, and is never framed or kept.
_PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                               "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Vary": "Accept",
}

_CODE_SPAN = re.compile(r"`([^`]+)`")


def _paragraph(text: str) -> str:
    """One sentence as a paragraph: escaped, with `code` spans as <code>."""
    return "<p>" + _CODE_SPAN.sub(r"<code>\1</code>", html.escape(text)) + "</p>"


def refusal_page(detail: str, next_step: Optional[str] = None) -> str:
    """The HTML a browser shows for a refused page load."""
    paragraphs = [_paragraph(detail)] + ([_paragraph(next_step)] if next_step else [])
    return _PAGE.format(paragraphs="\n".join(paragraphs))


def _wants_page(scope) -> bool:
    """A browser loading a page: a GET or HEAD outside /api whose Accept names text/html.
    An API call, a fetch, an image or a sound keeps the JSON answer."""
    if scope.get("type") != "http" or scope.get("method") not in ("GET", "HEAD"):
        return False
    path = scope.get("path") or ""
    if path == "/api" or path.startswith("/api/"):
        return False
    return "text/html" in Headers(scope=scope).get("accept", "").lower()


def _sends_body(scope) -> bool:
    """Whether the request declared a body (a Content-Length above 0, or a Transfer-Encoding),
    the rule the HTTPS proxy's relay uses too."""
    headers = Headers(scope=scope)
    if "transfer-encoding" in headers:
        return True
    try:
        return int(headers.get("content-length") or 0) > 0
    except ValueError:
        return False


def _socket_reason(text: str) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= _SOCKET_REASON_MAX_BYTES:
        return text
    return raw[:_SOCKET_REASON_MAX_BYTES].decode("utf-8", "ignore")


class AdmissionGuard:
    """Refuse every HTTP request and WebSocket handshake from a client that is not admitted.

    ``admits`` decides over the resolved client address; it defaults to
    ``binding.is_allowed_ip``, read at call time so a change of the admitted networks applies
    to the next request. ``reason`` replaces the refusal's sentence (the HTTP ``detail``, the
    page's first sentence and the WebSocket close reason, cut to 123 bytes there) when a door
    admits by another rule; ``next_step`` is the page's second sentence, shown with a custom
    ``reason`` only when given.
    """

    def __init__(self, app, *, admits: Optional[Callable[[str], bool]] = None,
                 reason: Optional[str] = None, next_step: Optional[str] = None) -> None:
        self.app = app
        self._admits = admits
        self._reason = reason
        self._next_step = next_step if reason else (next_step or REFUSED_NEXT_STEP)

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
            reason = _socket_reason(self._reason) if self._reason else REFUSED_SOCKET_REASON
            await WebSocketClose(code=4003, reason=reason)(scope, receive, send)
            return
        detail = self._reason or REFUSED_DETAIL
        # A refused request without a body closes its connection: kept alive, a single empty
        # line after the 403 cancelled uvicorn's keep-alive timer and held a slot of the door's
        # cap (measured: three such connections at a cap of 4 turned every other device away
        # with 503). One with a body stays open while the body passes and is discarded, held
        # by the door's head timer: closed while the body still arrived, the connection was
        # reset and the 403 lost (measured: a refused 8 MB POST got a reset, not the answer).
        closing = {} if _sends_body(scope) else {"Connection": "close"}
        if _wants_page(scope):
            response = HTMLResponse(refusal_page(detail, self._next_step), status_code=403,
                                    headers={**_PAGE_HEADERS, **closing})
        else:
            response = JSONResponse(status_code=403, content={"detail": detail},
                                    headers={"Vary": "Accept", **closing})
        await response(scope, receive, send)


def this_machine_only(app) -> AdmissionGuard:
    """The door while Local Network is off: this machine only, every other client refused
    with the sentence that says Local Network is off."""
    return AdmissionGuard(app, admits=binding.is_localhost, reason=LOCAL_NETWORK_OFF,
                          next_step=LOCAL_NETWORK_OFF_NEXT_STEP)


class ByLocalNetwork:
    """One of two doors per request, by the Local Network setting: ``network`` while it is on,
    ``this_machine`` while it is off. One config read per request (about 0.15 ms measured with
    every key stored) decides the whole request, so a toggle applies to the next request and a
    request never passes one door's check and then another door's chain.

    Scopes other than http and websocket (lifespan) go to ``this_machine``, whose guard hands
    them to its app unjudged; both doors are built around the same app, so it sees each
    lifespan once.
    """

    def __init__(self, *, network, this_machine) -> None:
        self.network = network
        self.this_machine = this_machine

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") in ("http", "websocket"):
            from vaf.core.config import Config

            if Config.get_bool("local_network_enabled", False):
                await self.network(scope, receive, send)
                return
        await self.this_machine(scope, receive, send)


def _record_refusal(ip: str, path: str) -> None:
    """The refusal in the security log. Never raises: auditing must not break the door."""
    try:
        from vaf.core.security_events import log_security_event
        log_security_event("ip_blocked", ip=ip, detail=path[:200])
    except Exception:
        pass
