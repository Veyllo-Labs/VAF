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


def test_an_https_page_dials_its_own_origin():
    """The client half. An HTTPS page came through a TLS proxy that relays /ws (the Next server
    never speaks TLS), so it dials its own origin and does not ask this route: the port the route
    names is the one the proxy BOUND, and a port forward or a NAT changes the one the browser
    dialled (measured: a page on a forwarded :18443 dialled :8443 and never connected). The proxy's
    port written into the same-origin test was that bug."""
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    same_origin = re.search(r"if \(([^{]*)\) \{\s*base = '';", src)
    assert same_origin, "the WebSocket base resolution in web/app/page.tsx moved; re-point this guard"
    condition = same_origin.group(1)
    assert "window.location.protocol === 'https:'" in condition
    assert "8443" not in condition


def test_the_docs_name_the_same_three_same_origin_cases():
    """The client dials its own origin from any HTTPS page AND from an HTTP page on port 80 or
    with no explicit port (`window.location.port` is '' then). The two docs and the route's
    docstring each said less, or something vaguer ("the default port"), than the condition
    does. MUTATION: drop "port 80" from NETWORK_FEATURES.md, or `port === ''` from page.tsx,
    and this goes red."""
    import inspect
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    src = (root / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    condition = re.search(r"if \(([^{]*)\) \{\s*base = '';", src).group(1)
    parts = {p.strip() for p in condition.split("||")}
    assert parts == {"window.location.protocol === 'https:'", "port === '80'", "port === ''"}, parts

    def flat(text):
        return " ".join(text.split())

    for name, text in (
        ("WEBUI_WEBSOCKET_FLOW.md", (root / "docs" / "web-ui" / "WEBUI_WEBSOCKET_FLOW.md").read_text(encoding="utf-8")),
        ("NETWORK_FEATURES.md", (root / "docs" / "setup" / "NETWORK_FEATURES.md").read_text(encoding="utf-8")),
        ("get_ws_config", inspect.getdoc(nr.get_ws_config)),
    ):
        text = flat(text)
        assert "HTTPS page" in text, name
        assert "HTTP page on port 80 or with no explicit port" in text, name
        assert "the default port" not in text, name
