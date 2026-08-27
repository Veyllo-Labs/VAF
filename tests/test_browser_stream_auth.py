# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The stream port's credential must reach BOTH proxy legs.

The hole this closes, measured live before and after: KasmVNC on 6901 required
an Origin header to be merely PRESENT and accepted any value, while CDP answers
403 to any request carrying one. So a page in the user's ordinary browser could
open `ws://127.0.0.1:6901/websockify` and get a bidirectional RFB channel -
framebuffer out, key and pointer events in. With basic auth the same handshake
answers 401, because a page's WebSocket constructor cannot set an Authorization
header and credentials in a URL are not converted into one.

These are WIRING tests, deliberately not source greps: the failure mode is a
dropped header, which no unit test notices and which shows up only as a blank
panel in a browser the suite never opens. Each one goes red if the credential
is removed from its leg.
"""
import base64
import types

import pytest

import vaf.core.browser_interactive as bi


SECRET = "test-stream-secret"
EXPECTED = "Basic " + base64.b64encode(f"vaf:{SECRET}".encode()).decode()


class _FakeManager:
    def vnc_base(self):
        return "http://127.0.0.1:16901"

    def validate_ticket(self, ticket):
        return object()          # not an AgentStream: not watch-only

    def stream_connected(self, ticket):
        return True

    def stream_disconnected(self, ticket):
        return None


@pytest.fixture
def with_secret(monkeypatch):
    """A known credential, without touching the machine's real keyring."""
    monkeypatch.setattr(bi, "_VNC_SECRET_CACHE", "", raising=False)
    monkeypatch.setattr(bi, "browser_vnc_secret", lambda: SECRET)
    monkeypatch.setattr(bi, "get_manager_by_ticket", lambda ticket: _FakeManager())
    return SECRET


def test_the_asset_leg_sends_the_credential(with_secret, monkeypatch):
    """MUTATION: drop `headers=vnc_auth_headers()` from the httpx client and
    this goes red. Without it the container answers 401 and the viewer's own
    files never arrive."""
    seen = {}

    class _FakeClient:
        def __init__(self, *a, **kw):
            seen.update(kw)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            seen["url"] = url
            return types.SimpleNamespace(status_code=200, content=b"ok",
                                         headers={"content-type": "text/plain"})

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    from fastapi.testclient import TestClient
    from vaf.core.web_server import app
    with TestClient(app) as client:
        r = client.get("/api/browser-vnc/t/tkt/index.html")

    assert r.status_code == 200
    assert seen.get("headers", {}).get("Authorization") == EXPECTED, (
        "the asset leg fetched the viewer without the stream credential")


def test_the_stream_leg_sends_the_credential(with_secret, monkeypatch):
    """MUTATION: drop `additional_headers=...` from the websocket connect and
    this goes red. That leg is the one a person actually watches through."""
    seen = {}

    class _FakeUpstream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def recv(self):
            raise RuntimeError("upstream closed")

        async def send(self, data):
            return None

        async def close(self):
            return None

    def _fake_connect(uri, **kw):
        seen.update(kw)
        seen["uri"] = uri
        return _FakeUpstream()

    import websockets.asyncio.client as wsc
    monkeypatch.setattr(wsc, "connect", _fake_connect)

    from fastapi.testclient import TestClient
    from vaf.core.web_server import app
    with TestClient(app) as client:
        try:
            with client.websocket_connect("/api/browser-vnc/t/tkt/websockify"):
                pass
        except Exception:
            # The fake upstream dies on the first recv; the handshake is what
            # this test is about, and it has already happened by then.
            pass

    assert seen.get("additional_headers", {}).get("Authorization") == EXPECTED, (
        "the stream leg opened the socket without the credential")


def test_an_unreadable_keyring_yields_no_header_rather_than_a_guess(monkeypatch):
    """Fail-closed by construction: with no secret the header is EMPTY, the
    container answers 401 and the stream fails loudly. It must never invent a
    default credential, and it must never degrade to an unauthenticated
    stream - the container has no unauthenticated mode any more."""
    monkeypatch.setattr(bi, "_VNC_SECRET_CACHE", "", raising=False)
    monkeypatch.setattr(bi, "browser_vnc_secret", lambda: "")
    assert bi.vnc_auth_headers() == {}


def test_the_credential_is_stable_across_calls(monkeypatch):
    """A rotating value would 401 every ADOPTED container: an instance keeps
    the environment it was created with for life, and the pool adopts
    containers from earlier VAF processes."""
    calls = []
    monkeypatch.setattr(bi, "_VNC_SECRET_CACHE", "", raising=False)

    def _mint(name, **kw):
        calls.append(name)
        return SECRET

    import vaf.core.data_keyring as dk
    monkeypatch.setattr(dk, "get_data_secret", _mint)
    assert bi.browser_vnc_secret() == SECRET
    assert bi.browser_vnc_secret() == SECRET
    assert len(calls) == 1, "the credential is re-read on every call (keyring decrypt per asset)"
