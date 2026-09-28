# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Image bytes whose type someone else chose are served as a raster image or not at all.

The mail image proxy (the sender's server), a mail's inline parts (the sender) and WhatsApp
profile pictures (the CDN) passed the foreign Content-Type on after checking "starts with
image/ and is not exactly image/svg+xml". Two headers arrive joined by a comma
(``image/png, text/html``, measured with urllib3), which passes that check, and browsers take
the last type of such a list: HTML on the app's origin.
"""
import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from vaf.core.safe_media import raster_image_type

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("given,served", [
    ("image/png", "image/png"),
    ("IMAGE/PNG; charset=binary", "image/png"),
    ("image/jpg", "image/jpeg"),
    ("image/vnd.microsoft.icon", "image/x-icon"),
    ("image/png, text/html", None),
    # a second type hidden behind a parameter: refused before the parameter is cut off, or it
    # would lose its second type with it and pass as a plain PNG
    ("image/png; q=1, text/html", None),
    ("image/svg+xml", None),
    ("image/svg+xml; charset=utf-8", None),
    ("text/html", None),
    ("image/x-unknown", None),
    ("", None),
    (None, None),
])
def test_only_a_single_raster_type_is_served_and_only_canonically(given, served):
    assert raster_image_type(given) == served


def test_every_route_that_serves_foreign_image_bytes_asks_the_allowlist():
    mail = (REPO / "vaf/api/mail_routes.py").read_text(encoding="utf-8")
    proxy = mail[mail.index('def _fetch()'):]
    proxy = proxy[:proxy.index("result = await asyncio.to_thread(_fetch)")]
    assert 'ctype = raster_image_type(r.headers.get("Content-Type"))' in proxy
    parts = mail[mail.index('async def message_part('):]
    parts = parts[:parts.index("\n@router.")]
    assert 'serve_type = raster_image_type(ctype) or "application/octet-stream"' in parts
    assert 'startswith("image/") and' not in mail and 'ctype == "image/svg+xml"' not in mail


def _avatar(monkeypatch, data, mime):
    import vaf.api.whatsapp_bridge as bridge
    from vaf.api import whatsapp_routes as wr
    monkeypatch.setattr(bridge, "get_avatar", lambda username, jid, timeout: (data, mime))
    monkeypatch.setattr(bridge, "is_bridge_running", lambda: False)
    req = Request({"type": "http", "method": "GET", "path": "/api/whatsapp/avatar", "headers": []})
    return asyncio.run(wr.get_whatsapp_avatar(req, "491701234567"))


def test_a_profile_picture_is_served_as_its_raster_type(monkeypatch):
    resp = _avatar(monkeypatch, b"\xff\xd8\xff", None)
    assert resp.media_type == "image/jpeg"
    assert resp.headers.get("x-content-type-options") == "nosniff"


@pytest.mark.parametrize("mime", ["image/svg+xml", "image/jpeg, text/html", "text/html"])
def test_a_profile_picture_of_another_type_is_not_served(monkeypatch, mime):
    with pytest.raises(HTTPException) as refused:
        _avatar(monkeypatch, b"<svg onload=alert(1)>", mime)
    assert refused.value.status_code == 404
