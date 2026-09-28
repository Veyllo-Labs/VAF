# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Where a finished GitHub or cloud sign-in sends the browser back to.

The start of the sign-in took ``redirect_base`` as any string, stored it in the OAuth state,
and the callback redirected there (an open redirect) or, on its error page, put it into an
``href`` unescaped. Any logged-in account could start a sign-in with
``redirect_base=http://x"><script>...`` and send another account the callback link: markup of
its choosing on the app's origin. The value is now kept only when it is an origin of VAF's own
for the request that started the sign-in (the origin guard's rule), and the link is escaped.
"""
from pathlib import Path

import pytest
from starlette.requests import Request

from vaf.network import binding
from vaf.network.oauth_redirect import own_redirect_base

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _frontend_on_3000(monkeypatch):
    monkeypatch.setattr(binding, "frontend_port", lambda: 3000)


def _start_request(headers: dict, scheme: str = "http") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "method": "GET", "path": "/api/github/oauth/start",
                    "scheme": scheme, "headers": raw, "query_string": b""})


DESKTOP = {"host": "127.0.0.1:8005", "x-forwarded-host": "localhost:3000"}   # the Next.js door
LAN = {"host": "127.0.0.1:8005", "x-forwarded-proto": "https", "x-forwarded-host": "192.168.1.50:8443"}


@pytest.mark.parametrize("headers,base", [
    (DESKTOP, "http://localhost:3000"),
    (DESKTOP, "http://127.0.0.1:3000/"),
    (LAN, "https://192.168.1.50:8443"),
])
def test_the_page_the_sign_in_started_from_is_kept(headers, base):
    assert own_redirect_base(base, _start_request(headers)) == base.rstrip("/")


@pytest.mark.parametrize("headers,base,kept", [
    (DESKTOP, "http://localhost:3000?", "http://localhost:3000"),
    (DESKTOP, "http://localhost:3000#", "http://localhost:3000"),
    (DESKTOP, "HTTP://LocalHost:3000", "http://localhost:3000"),
    (DESKTOP, "http://[::1]:3000", "http://[::1]:3000"),
    (LAN, "https://192.168.1.50:8443/", "https://192.168.1.50:8443"),
])
def test_what_is_kept_is_the_canonical_origin(headers, base, kept):
    """The callback appends /settings?... to it: an empty "?" or "#" that the origin check lets
    through would turn that path into a query, so the parsed parts are handed on, not the input."""
    assert own_redirect_base(base, _start_request(headers)) == kept


@pytest.mark.parametrize("headers,base", [
    (DESKTOP, 'http://x"><script>alert(1)</script>'),
    (DESKTOP, "https://evil.example"),
    (DESKTOP, "http://localhost:5173"),
    (LAN, "https://192.168.1.77:8443"),
    (DESKTOP, "javascript:alert(1)"),
])
def test_anything_else_is_dropped(headers, base):
    assert own_redirect_base(base, _start_request(headers)) is None


@pytest.mark.parametrize("module", ["vaf.api.github_routes", "vaf.api.cloud_routes"])
def test_the_error_page_escapes_its_link(module):
    """Defence in depth: the stored value is validated at the start, and the page escapes it
    anyway - an entry stored before this change still reaches the page."""
    import importlib
    mod = importlib.import_module(module)
    page = mod._redirect_error("failed", 'http://x"><script>alert(1)</script>').body.decode()
    assert "<script>alert(1)</script>" not in page
    assert "&quot;&gt;&lt;script&gt;" in page


@pytest.mark.parametrize("path", ["vaf/api/github_routes.py", "vaf/api/cloud_routes.py"])
def test_both_starts_validate_before_storing(path):
    src = (REPO / path).read_text(encoding="utf-8")
    start = src[src.index('@router.get("/oauth/start")'):]
    start = start[:start.index("\n@router.", 1)]
    assert "redirect_base=own_redirect_base(redirect_base, request)" in start
