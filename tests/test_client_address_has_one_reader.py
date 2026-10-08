# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The socket peer is read in ONE place: ``vaf.network.binding.connection_client_ip``.

Behind the integrated HTTPS proxy the peer is 127.0.0.1 for every device, so a decision or a
record that reads it directly applies to the whole network. That mistake was fixed one site at
a time - the auth middleware, the rate limiter, the login routes, the WebSocket handshake, the
OAuth callback - and the next site was always one nobody had looked at yet: the IP check
(Layer 2) still judged the peer and let a public address through on HTTP, and three lanes (the
workspace upload, the room upload, the room workspace door) wrote the proxy into the security
log instead of the device.

So the rule is structural: outside the resolver, and outside the proxy itself (the hop that
writes X-Forwarded-For FROM the peer it sees), nothing in vaf/ touches a connection's client
address. Every site asks ``connection_client_ip(conn)``; a pure ASGI middleware wraps its scope
in ``HTTPConnection(scope)`` first.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The resolver, and the proxy that authors the forwarding header from its own peer.
ALLOWED = {"vaf/network/binding.py", "vaf/network/https_proxy.py"}

# The names Starlette connections go by in this tree (Request, WebSocket, HTTPConnection).
# Named boundary: the detector recognises a connection by these names, as a bare name or as
# an attribute (``self.request``), and by a constructor call; one bound to another name
# (``r.client.host``) is not seen.
_CONNECTION_NAMES = {"request", "websocket", "ws", "conn", "connection", "req"}
_CONNECTION_TYPES = {"HTTPConnection", "Request", "WebSocket"}


def _is_connection(node: ast.AST) -> bool:
    """``request``, ``self.request`` or ``HTTPConnection(scope)``."""
    if isinstance(node, ast.Name):
        return node.id in _CONNECTION_NAMES
    if isinstance(node, ast.Attribute):
        return node.attr in _CONNECTION_NAMES
    if isinstance(node, ast.Call):
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        return name in _CONNECTION_TYPES
    return False


def _is_scope(node: ast.AST) -> bool:
    """An ASGI scope: ``scope``, ``request.scope``, ``self.scope``."""
    if isinstance(node, ast.Name):
        return node.id == "scope"
    return isinstance(node, ast.Attribute) and node.attr == "scope"


def _raw_reads(source: str) -> list[tuple[int, str]]:
    """Every place in ``source`` that reads a connection's client address directly."""
    hits = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr == "client" and _is_connection(node.value):
            hits.append((node.lineno, f"{ast.unparse(node.value)}.client"))
        elif (isinstance(node, ast.Subscript) and _is_scope(node.value)
                and isinstance(node.slice, ast.Constant) and node.slice.value == "client"):
            hits.append((node.lineno, f'{ast.unparse(node.value)}["client"]'))
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get" and _is_scope(node.func.value) and node.args
                and isinstance(node.args[0], ast.Constant) and node.args[0].value == "client"):
            hits.append((node.lineno, f'{ast.unparse(node.func.value)}.get("client")'))
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "getattr" and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant) and node.args[1].value == "client"):
            hits.append((node.lineno, 'getattr(..., "client")'))
    return hits


def test_nothing_outside_the_resolver_reads_the_socket_peer():
    offenders = []
    for path in sorted((ROOT / "vaf").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWED or rel.startswith("vaf/vendor/"):
            continue
        for line, what in _raw_reads(path.read_bytes().decode("utf-8")):
            offenders.append(f"{rel}:{line} reads {what}")
    assert not offenders, (
        "Read the client through vaf.network.binding.connection_client_ip(conn), never the "
        "socket peer: behind the integrated proxy the peer is 127.0.0.1 for every device.\n"
        + "\n".join(offenders)
    )


def test_the_detector_sees_every_shape_the_fixed_sites_used():
    """The shapes this tree really used (the IP check, the WebSocket handshake, the room
    workspace door, the origin guard) plus their relatives through an attribute or a freshly
    built connection, so the guard above is known to catch them rather than assumed to."""
    shapes = [
        'ip = request.client.host if request.client else "unknown"',
        "peer = websocket.client.host",
        'ip = getattr(getattr(request, "client", None), "host", "") or ""',
        'client = scope.get("client")',
        'client = scope["client"]',
        "host = request.client[0]",
        # Through an attribute, or a connection's own scope.
        "peer = self.request.client.host",
        'client = request.scope["client"]',
        'client = websocket.scope.get("client")',
        'client = self.scope["client"]',
        # A connection built on the spot, the way a pure ASGI middleware wraps its scope.
        "peer = HTTPConnection(scope).client.host",
        "peer = Request(scope).client",
        "peer = starlette.requests.HTTPConnection(scope).client",
    ]
    for shape in shapes:
        assert _raw_reads(shape), f"the guard does not see: {shape}"
    assert not _raw_reads("ip = connection_client_ip(request)")
    assert not _raw_reads("ip = connection_client_ip(HTTPConnection(scope))")
    # An HTTP client object, or a "client" key of some other mapping, is not an address.
    for unrelated in ("resp = self.client.get(url)", "c = self.http.client",
                      'cid = config["client"]', 'info = manifest.get("client")'):
        assert not _raw_reads(unrelated), f"the guard flags an unrelated read: {unrelated}"
