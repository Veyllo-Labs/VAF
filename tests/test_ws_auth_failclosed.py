# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Gap B regression: in network-enabled mode the /ws auth must FAIL CLOSED for non-localhost clients.

Before the fix, an auth-phase exception (e.g. get_jwt_secret() raising, an import error, or a non-PyJWT
error) fell through to manager.connect() with user_context=None — an UNSCOPED connection, which
session.list()/rag treat as global/admin. The fix rejects any non-localhost client on an auth error,
while localhost still falls through (so a transient error never bricks the desktop)."""
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from vaf.core.web_server import app, manager

LAN = ("192.168.1.50", 40000)      # RFC1918, not localhost
LOCALHOST = ("127.0.0.1", 40000)


def _enable_network(monkeypatch):
    """Force network mode at call time (the endpoint reads Config.get live)."""
    from vaf.core.config import Config
    real_get = Config.get
    def fake(key, default=None):
        if key == "local_network_enabled":
            return True
        return real_get(key, default)
    monkeypatch.setattr(Config, "get", staticmethod(fake))


def _spy_connect(monkeypatch):
    """Record manager.connect calls; abort right after so we never enter the receive loop."""
    calls = []
    async def fake_connect(ws):
        calls.append(ws)
        try:
            await ws.accept()
        except Exception:
            pass
        raise RuntimeError("stop-after-connect")  # break out before `while True: receive`
    monkeypatch.setattr(manager, "connect", fake_connect)
    return calls


def _raise(*a, **k):
    raise RuntimeError("boom")


def test_ws_lan_no_token_rejected(monkeypatch):
    _enable_network(monkeypatch)
    calls = _spy_connect(monkeypatch)
    c = TestClient(app, client=LAN)
    with pytest.raises(WebSocketDisconnect) as ei:
        with c.websocket_connect("/ws"):
            pass
    assert ei.value.code == 4001
    assert calls == []


def test_ws_lan_invalid_token_rejected(monkeypatch):
    _enable_network(monkeypatch)
    calls = _spy_connect(monkeypatch)
    c = TestClient(app, client=LAN)
    with pytest.raises(WebSocketDisconnect) as ei:
        with c.websocket_connect("/ws?token=garbage"):
            pass
    assert ei.value.code == 4001          # caught by the inner InvalidTokenError handler
    assert calls == []


def test_ws_lan_auth_exception_fails_closed(monkeypatch):
    """THE regression: an auth-phase exception (get_jwt_secret raising) for a LAN client must CLOSE,
    not fall through to an unscoped connection. Pre-fix: manager.connect was called with no scope."""
    _enable_network(monkeypatch)
    import vaf.auth.crypto as crypto
    monkeypatch.setattr(crypto, "get_jwt_secret", _raise)
    calls = _spy_connect(monkeypatch)
    c = TestClient(app, client=LAN)
    with pytest.raises(WebSocketDisconnect) as ei:
        with c.websocket_connect("/ws?token=x"):
            pass
    assert ei.value.code == 4003
    assert calls == []                    # never reached connect


def test_ws_localhost_exception_falls_through(monkeypatch):
    """Desktop-not-bricked: a localhost client whose auth phase errors still reaches connect (localhost
    trust preserved). This pins the deliberate trade-off so a future 'reject localhost too' is caught."""
    _enable_network(monkeypatch)
    import vaf.auth.crypto as crypto
    monkeypatch.setattr(crypto, "get_jwt_secret", _raise)
    calls = _spy_connect(monkeypatch)
    c = TestClient(app, client=LOCALHOST)
    try:
        with c.websocket_connect("/ws?token=x"):
            pass
    except Exception:
        pass  # the spy aborts after connect; we only care that connect was reached
    assert calls, "localhost must still reach manager.connect despite the auth error"


def _signed_in_socket_world(monkeypatch):
    """The real /ws handler over the sign-in store of tests/auth_harness.py, one account
    signed in from the LAN. Returns (harness, token, recorded identities)."""
    from auth_harness import JWT_SECRET, Harness

    import vaf.auth.crypto as crypto
    h = Harness(monkeypatch)
    monkeypatch.setattr(crypto, "get_jwt_secret", lambda: JWT_SECRET)
    h.add_user("alice", "alice-password-1", no_second_factor=True)
    token = h.sign_in(h.client("192.168.61.1"), "alice", "alice-password-1", None)
    _enable_network(monkeypatch)
    seen = []

    async def fake_connect(ws):
        await ws.accept()

    def fake_set_user(ws, scope, username=None, role=None):
        seen.append((str(scope), username))
        raise RuntimeError("stop-after-identity")

    monkeypatch.setattr(manager, "connect", fake_connect)
    monkeypatch.setattr(manager, "set_connection_user", fake_set_user)
    return h, token, seen


def test_the_cookie_alone_opens_the_socket(monkeypatch):
    """The web UI dials /ws with no ?token=: the HttpOnly cookie is its credential, and the
    socket is the account's. An ended session's cookie is refused with 4001.
    MUTATION: ignore websocket.cookies - red."""
    h, token, seen = _signed_in_socket_world(monkeypatch)
    c = TestClient(app, client=LAN)
    c.cookies.set("vaf_token", token)
    try:
        with c.websocket_connect("/ws"):
            pass
    except Exception:
        pass
    assert seen == [(str(h.user("alice").user_scope_id), "alice")]

    h.client("192.168.61.1").post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
    ended = TestClient(app, client=LAN)
    ended.cookies.set("vaf_token", token)
    with pytest.raises(WebSocketDisconnect) as ei:
        with ended.websocket_connect("/ws"):
            pass
    assert ei.value.code == 4001
    assert len(seen) == 1


def test_a_program_token_opens_the_socket_by_its_address(monkeypatch):
    """A program has no cookie: its token for programs goes in the address (`?token=`), which
    stays for exactly that. MUTATION: stop reading the token query parameter - red."""
    import asyncio

    from vaf.auth import sessions
    h, _token, seen = _signed_in_socket_world(monkeypatch)

    async def _mint(db):
        return await sessions.mint_program_token(db, h.user("alice"), name="sync", days=7)

    program, _id = h.run_db(_mint)
    c = TestClient(app, client=LAN)
    try:
        with c.websocket_connect(f"/ws?token={program}"):
            pass
    except Exception:
        pass
    assert seen == [(str(h.user("alice").user_scope_id), "alice")]
    assert asyncio.run(sessions.session_standing_async(program)) == "live"


def test_a_session_of_this_machine_opens_no_socket_elsewhere(monkeypatch):
    """The desktop window's session stands only for this machine: its cookie copied to another
    device opens no socket there. MUTATION: drop client_ip from the network lane's check - red."""
    from auth_harness import JWT_SECRET, Harness

    import vaf.auth.crypto as crypto
    h = Harness(monkeypatch)
    monkeypatch.setattr(crypto, "get_jwt_secret", lambda: JWT_SECRET)
    h.add_user("alice", "alice-password-1", no_second_factor=True)
    desk = h.sign_in(h.client(None), "alice", "alice-password-1", None)
    _enable_network(monkeypatch)
    calls = _spy_connect(monkeypatch)
    c = TestClient(app, client=LAN)
    c.cookies.set("vaf_token", desk)
    with pytest.raises(WebSocketDisconnect) as ei:
        with c.websocket_connect("/ws"):
            pass
    assert ei.value.code == 4001 and calls == []
