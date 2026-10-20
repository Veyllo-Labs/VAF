# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Pages relayed by the integrated HTTPS proxy carry anti-framing and transport headers.

Measured before: `/login` through the proxy carried none of X-Frame-Options, a CSP
frame-ancestors, HSTS, nosniff or Referrer-Policy, because the backend's header table covers
API responses only and the proxy copied the frontend's headers as they were. So the login
and the app could be framed by any site.

The set at the proxy is deliberately not the backend's: no Permissions-Policy (it would
switch off the UI's own voice input), and frame-ancestors on pages only (the interactive
browser's viewer is a BACKEND response framed by the UI under SAMEORIGIN).
"""
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

import vaf.network.https_proxy as proxy


class _Upstream:
    """The proxy's pooled client, answering with the headers each test hands it."""

    def __init__(self, headers):
        self._headers = headers
        self.urls = []

    async def request(self, method, url, headers=None, content=None):
        self.urls.append(url)
        return SimpleNamespace(status_code=200, headers=dict(self._headers), content=b"<html></html>")


def _get(monkeypatch, path, upstream_headers, host="vaf.lan:8443"):
    upstream = _Upstream(upstream_headers)

    async def _client(_origin):
        return upstream

    monkeypatch.setattr(proxy, "_get_client", _client)
    c = TestClient(proxy.create_proxy_app(), client=("192.168.1.20", 40000))
    return c.get(path, headers={"Host": host})


def test_pages_through_the_proxy_carry_the_headers(monkeypatch):
    """MUTATION: drop the page header step - red."""
    r = _get(monkeypatch, "/login", {"content-type": "text/html"})
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert r.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert r.headers["strict-transport-security"] == "max-age=31536000"
    # The UI's own voice input runs in these pages.
    assert "permissions-policy" not in r.headers


def test_a_page_keeps_what_the_frontend_set(monkeypatch):
    """Add if absent, never over the frontend's own value. MUTATION: overwrite - red."""
    r = _get(monkeypatch, "/", {"content-type": "text/html", "X-Frame-Options": "SAMEORIGIN"})
    assert r.headers["x-frame-options"] == "SAMEORIGIN"


@pytest.mark.parametrize("path", ["/api/browser-vnc/t/abcdefghijklmnop/vnc.html", "/api/users"])
def test_backend_responses_get_hsts_only(monkeypatch, path):
    """The viewer is framed by the UI: a CSP frame-ancestors would override its SAMEORIGIN.
    MUTATION: apply the page set to backend responses - red."""
    r = _get(monkeypatch, path, {"content-type": "text/html", "X-Frame-Options": "SAMEORIGIN"})
    assert "content-security-policy" not in r.headers
    assert r.headers["x-frame-options"] == "SAMEORIGIN"
    assert r.headers["strict-transport-security"] == "max-age=31536000"


@pytest.mark.parametrize("host", ["192.168.1.5:8443", "localhost:8443", "app.localhost:8443",
                                  "[::1]:8443", "127.0.0.1:8443"])
def test_no_hsts_for_an_address_or_localhost(monkeypatch, host):
    """HSTS holds a host name on every port for a year: on localhost it would upgrade every
    plain-HTTP app there, the Web UI's own port included; browsers ignore it for addresses.
    MUTATION: send it for every host, or with includeSubDomains - red."""
    r = _get(monkeypatch, "/login", {"content-type": "text/html"}, host=host)
    assert "strict-transport-security" not in r.headers
    assert r.headers["x-frame-options"] == "DENY"


def test_the_backend_scopes_its_hsts_the_same_way():
    """The backend's own table (web_server._SecurityHeadersMiddleware) asks the same function."""
    from vaf.network.binding import hsts_for_host
    assert hsts_for_host("vaf.lan:8443") == "max-age=31536000"
    for host in ("localhost", "192.168.1.5", "[::1]:8443", "", None, "x.localhost"):
        assert hsts_for_host(host) is None, host
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "vaf" / "core" / "web_server.py").read_text(
        encoding="utf-8")
    assert "max-age=31536000; includeSubDomains" not in src
    assert "hsts_for_host(request.headers.get(\"x-forwarded-host\")" in src


def test_the_access_log_keeps_one_request_on_one_line(monkeypatch):
    """The path arrives percent-decoded: GET /page%0a<text> wrote a forged second line into
    https_proxy_access_<date>.log. MUTATION: write the line unsanitized - red."""
    # (the router answers 404 to a path with a newline; the access log is written before it)
    _get(monkeypatch, "/page%0a2026-10-21T00:00:00.000  HTTP  10.0.0.9  GET /forged", {})
    lines = proxy._access_log_path().read_text(encoding="utf-8").splitlines()
    assert not any(ln.startswith("2026-10-21T00:00:00.000") for ln in lines)
    assert any("GET /page 2026-10-21T00:00:00.000" in ln for ln in lines)
