# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Regression: the login rate limiter must count per DEVICE, not per socket peer.

The incident: the integrated HTTPS proxy relays every LAN device to the backend over loopback,
so ``request.client.host`` was 127.0.0.1 for all of them and they shared ONE sliding window.
Five failed logins by anyone locked out every device on the network for the whole window - a
one-line denial of service - and a real attacker could not be told apart from a mistyped
password.

The subtler half, and the reason ``client_key`` exists as a single function: THREE places fed
this limiter (the middleware plus two route modules that recorded failures for endpoints which
answer 200 on failure). If only one of them had been converted, failures would be recorded under
one key and looked up under another - the limiter would silently stop blocking anything at all,
which is worse than the bug it was meant to fix. The agreement between the recording path and
the checking path is what test_recording_and_checking_agree pins.
"""
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from vaf.auth import rate_limit
from vaf.auth.rate_limit import RateLimitMiddleware, client_key

PROXY_PEER = ("127.0.0.1", 40000)  # what the backend sees for anything the proxy relays
DEVICE_A = "192.168.1.10"
DEVICE_B = "192.168.1.20"


@pytest.fixture(autouse=True)
def _clean_tracker():
    """The tracker is a module-level singleton; isolate every test."""
    rate_limit._tracker._attempts.clear()
    yield
    rate_limit._tracker._attempts.clear()


async def _bad_login(request):
    return JSONResponse({"detail": "nope"}, status_code=401)


def _client():
    app = Starlette(routes=[Route("/api/auth/login", _bad_login, methods=["POST"])])
    app.add_middleware(RateLimitMiddleware)
    return TestClient(app, client=PROXY_PEER)


def _fail_login(c, device: str):
    return c.post("/api/auth/login", headers={"X-Forwarded-For": device}, json={})


def test_one_device_cannot_lock_out_the_others():
    """THE regression. Device A exhausts its window; device B must be unaffected.

    Pre-fix both were keyed 127.0.0.1, so A's failures blocked B (and the desktop).
    """
    c = _client()
    for _ in range(6):
        _fail_login(c, DEVICE_A)

    assert _fail_login(c, DEVICE_A).status_code == 429, "the offending device must be blocked"
    assert _fail_login(c, DEVICE_B).status_code == 401, (
        "a different device must still be allowed to try (401 = credentials rejected, not blocked)"
    )


def test_local_user_is_not_blocked_by_a_lan_device():
    """The person at the machine must not be locked out of their own app by a LAN device."""
    c = _client()
    for _ in range(6):
        _fail_login(c, DEVICE_A)

    # Genuine local traffic carries no forwarding header at all.
    assert c.post("/api/auth/login", json={}).status_code == 401


def test_recording_and_checking_agree():
    """The fail-open trap: every site that RECORDS a failure must use the same key the
    middleware CHECKS. Route modules record via record_login_failure(client_key(request)) for
    endpoints that answer 200 on failure; if that key were the raw peer, the middleware would
    look up a key nobody ever wrote and block nothing.
    """
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/auth/login",
        "headers": [(b"x-forwarded-for", DEVICE_A.encode())],
        "client": PROXY_PEER,
    }
    recorded_key = client_key(Request(scope))
    assert recorded_key == DEVICE_A

    # Feed the tracker exactly the way the route modules do, then let the middleware decide.
    for _ in range(6):
        rate_limit.record_login_failure(recorded_key)
    assert _fail_login(_client(), DEVICE_A).status_code == 429, (
        "failures recorded by a route module must be seen by the middleware"
    )


def test_client_key_cannot_be_forged_into_someone_elses_bucket():
    """A direct (non-relayed) client keeps its socket address, so it cannot spend another
    device's budget or dodge its own by claiming a different origin."""
    direct = {
        "type": "http",
        "method": "POST",
        "path": "/api/auth/login",
        "headers": [(b"x-forwarded-for", b"127.0.0.1")],
        "client": (DEVICE_A, 50000),
    }
    assert client_key(Request(direct)) == DEVICE_A
