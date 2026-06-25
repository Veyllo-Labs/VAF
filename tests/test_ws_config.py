# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""ws-config must steer each caller to a WS transport it can actually open.

The desktop window loads the frontend on plain http://127.0.0.1:3000 and cannot use wss:// to the
integrated proxy: QtWebEngine rejects the self-signed cert (ERR_CERT_AUTHORITY_INVALID) and the socket
dies, leaving the desktop UI unable to connect. So:
  - TLS off                      -> plain ws to the backend (8001)
  - TLS on, no proxy header      -> local DESKTOP -> plain ws to the internal 8005 channel
  - TLS on, X-Forwarded-Proto    -> LAN client behind the proxy -> wss on the effective proxy port
The proxy stamps X-Forwarded-Proto: https; the Next.js /api proxy (desktop path) forwards no such header,
so the header is a reliable LAN-vs-desktop discriminator.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

import vaf.api.network_routes as nr


def _client(tls: bool, monkeypatch, https_port=443):
    cfg = {
        "local_network_tls_enabled": tls,
        "local_network_port": 8001,
        "local_network_https_port": https_port,
    }
    monkeypatch.setattr(nr.Config, "get", lambda key, default=None: cfg.get(key, default))
    app = FastAPI()
    app.include_router(nr.router)
    return TestClient(app)


def test_tls_off_returns_plain_backend(monkeypatch):
    c = _client(False, monkeypatch)
    assert c.get("/api/network/ws-config").json() == {"useWss": False, "port": 8001}


def test_tls_on_desktop_gets_plain_internal_channel(monkeypatch):
    # No X-Forwarded-Proto → local desktop → plain ws to 8005 (never wss to the self-signed proxy).
    c = _client(True, monkeypatch)
    assert c.get("/api/network/ws-config").json() == {"useWss": False, "port": 8005}


def test_tls_on_lan_via_proxy_gets_wss(monkeypatch):
    # Simulate the HTTPS proxy forwarding: X-Forwarded-Proto: https → LAN client → wss on effective port.
    from vaf.network import runtime_status
    runtime_status.set_proxy_bound(8443, 443)
    try:
        c = _client(True, monkeypatch)
        body = c.get("/api/network/ws-config", headers={"X-Forwarded-Proto": "https"}).json()
        assert body == {"useWss": True, "port": 8443}
    finally:
        runtime_status.reset()
