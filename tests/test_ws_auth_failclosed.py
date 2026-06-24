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
    """Force network mode + 2FA-required at call time (the endpoint reads Config.get live)."""
    from vaf.core.config import Config
    real_get = Config.get
    def fake(key, default=None):
        if key == "local_network_enabled":
            return True
        if key == "local_network_require_2fa":
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
