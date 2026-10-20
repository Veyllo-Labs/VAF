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


def _get(monkeypatch, path, upstream_headers):
    upstream = _Upstream(upstream_headers)

    async def _client(_origin):
        return upstream

    monkeypatch.setattr(proxy, "_get_client", _client)
    c = TestClient(proxy.create_proxy_app(), client=("192.168.1.20", 40000))
    return c.get(path)


def test_pages_through_the_proxy_carry_the_headers(monkeypatch):
    """MUTATION: drop the page header step - red."""
    r = _get(monkeypatch, "/login", {"content-type": "text/html"})
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert r.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert r.headers["strict-transport-security"].startswith("max-age=31536000")
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
    assert r.headers["strict-transport-security"].startswith("max-age=31536000")
