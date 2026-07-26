# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Regression: a LAN device relayed by the integrated HTTPS proxy must not be treated as local.

The incident: the proxy terminates TLS on 0.0.0.0 and forwards to the backend over loopback, so
``request.client.host`` was 127.0.0.1 for every remote user. The auth middleware read only that
peer address, so any LAN device reached every non-exempt route with NO token at all - and the
route-level local-admin floors then promoted it to admin (user management, log viewer, the whole
security dashboard). Reproduced live against the running app before the fix: a tokenless
``GET /api/users`` through the proxy returned 200 with the full user list, and an INVALID token
returned 200 as well.

Two halves of the same fix, which is why they are tested together:
  1. ``binding.effective_client_ip`` resolves the real client, honouring X-Forwarded-For only when
     the peer is loopback (i.e. our proxy relayed it). A hop can only be ADDED, never removed, so a
     client can make itself look more remote but never more local.
  2. The proxy STRIPS any client-supplied forwarding header before setting its own. Without this,
     half 1 is bypassable: Starlette lowercases incoming header names, so writing
     "X-Forwarded-For" used to ADD a second header and the backend's Headers.get() returned the
     client's forged copy.

What must keep working, and is pinned below: internal loopback IPC (no token, no forwarding
header), the desktop via the Next.js /api route (same shape), and the same-host OAuth callback
relayed by the proxy with a loopback hop.
"""
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from vaf.auth.middleware import AuthMiddleware
from vaf.network.binding import effective_client_ip

# The peer the backend sees for ANYTHING relayed by the integrated HTTPS proxy.
PROXY_PEER = ("127.0.0.1", 40000)
LAN_DEVICE = "192.168.1.77"


async def _ok(request):
    return JSONResponse({"ok": True})


def _app():
    app = Starlette(routes=[
        # A non-exempt route guarded by a local-admin floor in production.
        Route("/api/users", _ok),
        Route("/api/auth/login", _ok, methods=["GET", "POST"]),  # exempt
        Route("/api/auth/test-veyllo-key", _ok, methods=["GET", "POST"]),  # exempt (first-run)
    ])
    app.add_middleware(AuthMiddleware)
    return app


# --------------------------------------------------------------------------- the vulnerability

def test_proxied_lan_client_without_token_is_rejected():
    """THE regression. Peer is the proxy (127.0.0.1) but the hop names a LAN device -> 401.

    Pre-fix this returned 200 and the route floors handed out the local admin identity.
    """
    c = TestClient(_app(), client=PROXY_PEER)
    r = c.get("/api/users", headers={"X-Forwarded-For": LAN_DEVICE})
    assert r.status_code == 401


def test_proxied_lan_client_cannot_forge_loopback():
    """A forged hop must not buy trust. Even if the header claims 127.0.0.1, the value the backend
    reads is the one OUR proxy set (see the proxy-strip test below); a client-controlled hop is
    only ever honoured when it makes the client look more remote."""
    c = TestClient(_app(), client=PROXY_PEER)
    # Two hops: the forged one first, ours last - the resolver takes the first, so a client that
    # prepends its own value is exactly the case that must NOT be trusted more than a plain LAN hop.
    r = c.get("/api/users", headers={"X-Forwarded-For": f"{LAN_DEVICE}, 127.0.0.1"})
    assert r.status_code == 401


# --------------------------------------------------------------------------- must keep working

def test_internal_loopback_ipc_still_passes_without_token():
    """Internal callers (/api/subagent/stream, /api/workflow/update, /api/heartbeat) hold no token
    and set no forwarding header. Breaking them would break sub-agent result delivery (invariant:
    sub-agent results are delivered exactly once by the runner drain)."""
    c = TestClient(_app(), client=PROXY_PEER)
    assert c.get("/api/users").status_code == 200


def test_same_host_browser_through_proxy_still_passes():
    """The desktop OAuth callback opens in the system browser on the host; the proxy relays it with
    a loopback hop, so it stays genuinely local."""
    c = TestClient(_app(), client=PROXY_PEER)
    assert c.get("/api/users", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 200


def test_first_run_endpoints_stay_reachable_for_a_lan_browser():
    """A headless/LAN first run has no token by definition. /bootstrap and /login were already
    exempt; the onboarding key test must be too, or setup dead-ends at the Veyllo-key step."""
    c = TestClient(_app(), client=PROXY_PEER)
    hop = {"X-Forwarded-For": LAN_DEVICE}
    assert c.post("/api/auth/login", headers=hop).status_code == 200
    assert c.post("/api/auth/test-veyllo-key", headers=hop).status_code == 200


# --------------------------------------------------------------------------- the resolver itself

def test_effective_client_ip_polarity():
    """A hop REMOVES trust, never grants it."""
    # Relayed by our proxy -> the hop is the truth.
    assert effective_client_ip("127.0.0.1", LAN_DEVICE) == LAN_DEVICE
    # Direct non-loopback peer -> the socket wins, a claimed hop is ignored.
    assert effective_client_ip(LAN_DEVICE, "127.0.0.1") == LAN_DEVICE
    # No hop -> the peer, unchanged (internal IPC, desktop via the Next.js route).
    assert effective_client_ip("127.0.0.1", None) == "127.0.0.1"
    assert effective_client_ip("127.0.0.1", "") == "127.0.0.1"
    # Chains: the first entry is the originating client.
    assert effective_client_ip("127.0.0.1", f"{LAN_DEVICE}, 10.0.0.1") == LAN_DEVICE
    # Missing client info fails closed: "unknown" is not a valid IP, so is_localhost() is False.
    assert effective_client_ip(None, None) == "unknown"


def test_proxy_strips_client_supplied_forwarding_headers():
    """Half 2 of the fix, pinned at the source.

    Starlette hands the proxy lowercased header names. Writing "X-Forwarded-For" without stripping
    first left BOTH keys in the outgoing dict, and the backend's Headers.get() returns the first
    match - the client's forged value. This test walks the proxy's own normaliser to prove the
    client's copy is gone before ours is set.
    """
    from vaf.network.https_proxy import _normalize_headers_for_upstream

    headers = {
        "x-forwarded-for": "127.0.0.1",      # forged by a LAN client
        "x-forwarded-proto": "https",
        "x-forwarded-host": "evil.example",
        "host": "vaf.local",
        "authorization": "Bearer keep-me",   # must survive: real users authenticate through it
    }
    _normalize_headers_for_upstream(headers, "http://127.0.0.1:8005", "vaf.local")

    assert not any(k.lower() == "x-forwarded-for" for k in headers), (
        "a client-supplied X-Forwarded-For must be stripped, or the backend reads the forged value"
    )
    assert headers["authorization"] == "Bearer keep-me"
    assert headers["Host"] == "127.0.0.1:8005"
