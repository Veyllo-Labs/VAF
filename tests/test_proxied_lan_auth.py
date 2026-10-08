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

from vaf.auth.middleware import AuthMiddleware, IPValidationMiddleware
from vaf.network.binding import effective_client_ip

# The peer the backend sees for ANYTHING relayed by the integrated HTTPS proxy.
PROXY_PEER = ("127.0.0.1", 40000)
LAN_DEVICE = "192.168.1.77"
VPN_DEVICE = "10.8.0.2"         # a WireGuard/OpenVPN client address
PUBLIC_DEVICE = "203.0.113.7"   # TEST-NET-3, a documentation address


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


# --------------------------------------------------------------------------- the IP check (Layer 2)

def _ip_checked_app():
    app = Starlette(routes=[
        Route("/api/users", _ok),
        Route("/api/auth/bootstrap", _ok, methods=["POST"]),  # creates the first admin
    ])
    app.add_middleware(IPValidationMiddleware)
    return app


def test_ip_check_refuses_a_public_client_relayed_by_the_proxy():
    """THE Layer 2 regression. The check judged the socket peer, which is 127.0.0.1 for
    everything the proxy relays, so a public address that reached the proxy port passed on HTTP
    - the first-admin route included - while only the WebSocket refused it.

    Measured before the fix: 200 relayed, 403 only for a direct connection.
    """
    c = TestClient(_ip_checked_app(), client=PROXY_PEER)
    hop = {"X-Forwarded-For": PUBLIC_DEVICE}
    assert c.post("/api/auth/bootstrap", headers=hop).status_code == 403
    assert c.get("/api/users", headers=hop).status_code == 403


def test_ip_check_lets_lan_and_vpn_devices_through():
    """A home LAN and a VPN client (RFC 1918 addresses) are the devices this mode is for."""
    c = TestClient(_ip_checked_app(), client=PROXY_PEER)
    for device in (LAN_DEVICE, VPN_DEVICE):
        assert c.get("/api/users", headers={"X-Forwarded-For": device}).status_code == 200


def test_ip_check_keeps_internal_loopback_callers():
    """Internal IPC and the desktop through the Next.js route carry no forwarding header."""
    c = TestClient(_ip_checked_app(), client=PROXY_PEER)
    assert c.get("/api/users").status_code == 200


def test_ip_check_fails_closed_without_client_info():
    """The proxy writes "unknown" when it has no client address. That is no address at all."""
    c = TestClient(_ip_checked_app(), client=PROXY_PEER)
    assert c.get("/api/users", headers={"X-Forwarded-For": "unknown"}).status_code == 403


def test_ip_check_records_the_device_not_the_proxy(monkeypatch):
    """The security log entry must name who knocked, or every refusal reads 127.0.0.1."""
    import vaf.auth.middleware as mw

    events = []
    monkeypatch.setattr(mw, "_emit_security_event", lambda kind, **f: events.append((kind, f)))
    c = TestClient(_ip_checked_app(), client=PROXY_PEER)
    c.get("/api/users", headers={"X-Forwarded-For": PUBLIC_DEVICE})
    assert events == [("ip_blocked", {"ip": PUBLIC_DEVICE, "path": "/api/users"})]


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
