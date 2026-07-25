# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""MailService sanitizer tests: the HTML trust boundary (nh3) must strip
scripts/handlers/dangerous schemes, block remote images (tracking protection)
while keeping cid: inline images via the authenticated endpoint, and neutralize
style-based exfiltration. Also covers attachment retrieval and fail-closed
scoping. Isolated: tmp store + pinned crypto key."""
import os

import pytest

import vaf.mail.crypto as mail_crypto
from vaf.mail.parser import ParsedMessage
from vaf.mail.service import MailService
from vaf.mail.store import MailStore

_SCOPE = "12345678-1234-1234-1234-123456789abc"


@pytest.fixture(autouse=True)
def _pinned_crypto_key():
    old = mail_crypto._cached_key
    mail_crypto._cached_key = os.urandom(32)
    yield
    mail_crypto._cached_key = old


@pytest.fixture()
def svc(tmp_path, monkeypatch):
    store = MailStore(_SCOPE, base_dir=tmp_path)
    service = MailService.__new__(MailService)  # bypass ctor to inject tmp store
    service.user_scope_id = _SCOPE
    service.store = store
    yield service
    store.close()


def _ingest_html(svc, html_body: str, uid: int = 1) -> int:
    raw = (
        f"Message-ID: <h{uid}@example.com>\r\nSubject: T\r\nMIME-Version: 1.0\r\n"
        f"Content-Type: multipart/related; boundary=B\r\n\r\n"
        f"--B\r\nContent-Type: text/html; charset=utf-8\r\n\r\n{html_body}\r\n"
        f"--B\r\nContent-Type: image/png\r\nContent-ID: <logo1>\r\n"
        f"Content-Transfer-Encoding: base64\r\n\r\niVBORw0KGgo=\r\n--B--\r\n"
    ).encode()
    apk = svc.store.upsert_account("bob@example.com", "imap", "bob@example.com")
    fpk = svc.store.upsert_folder(apk, "INBOX")
    return svc.store.ingest_message(
        apk, fpk, uid,
        ParsedMessage(message_id=f"<h{uid}@example.com>", subject="T", body_html=html_body),
        raw=raw)


def test_fail_closed_scope():
    with pytest.raises(ValueError):
        MailService("")


def test_scripts_and_handlers_stripped(svc):
    pk = _ingest_html(svc, '<p onclick="steal()">Hi</p><script>evil()</script>'
                           '<a href="javascript:evil()">x</a>')
    body = svc.get_body(pk)
    assert body["html"] is not None
    assert "script" not in body["html"].lower()
    assert "onclick" not in body["html"].lower()
    assert "javascript:" not in body["html"].lower()
    assert "Hi" in body["html"]


def test_remote_images_blocked_and_counted(svc):
    pk = _ingest_html(svc, '<img src="https://tracker.example.com/pixel.gif">'
                           '<img src="//cdn.example.com/x.png"><p>Text</p>')
    body = svc.get_body(pk)
    assert body["blocked_remote"] == 2
    assert "tracker.example.com" not in body["html"]
    assert "cdn.example.com" not in body["html"]


def test_cid_inline_image_rewritten_to_endpoint(svc):
    pk = _ingest_html(svc, '<img src="cid:logo1"><p>Logo above</p>')
    body = svc.get_body(pk)
    assert f"/api/mail/messages/{pk}/parts/logo1" in body["html"]
    assert body["blocked_remote"] == 0


def test_data_image_kept_data_html_dropped(svc):
    pk = _ingest_html(svc, '<img src="data:image/png;base64,iVBORw0KGgo=">'
                           '<img src="data:text/html;base64,PHNjcmlwdD4=">')
    body = svc.get_body(pk)
    assert "data:image/png" in body["html"]
    assert "data:text/html" not in body["html"]
    assert body["blocked_remote"] == 1


def test_style_url_exfiltration_neutralized(svc):
    pk = _ingest_html(svc, '<div style="background:url(https://t.example.com/x)">A</div>'
                           '<p style="color:#333">B</p>')
    body = svc.get_body(pk)
    assert "t.example.com" not in body["html"]
    assert 'style="color:#333"' in body["html"].replace("'", '"')


@pytest.mark.parametrize("style", [
    # A CSS escape renders as url(: "u\72 l(" IS "url(". The filter's backslash
    # alternative used to be \\\\ (two literal backslashes), so a single-backslash
    # escape - the only kind CSS actually uses - went straight through.
    r"background:u\72 l(https://t.example.com/x.gif)",
    # These fetch a URL with no url( token at all, so a filter that only looks for
    # url( never sees them.
    "background-image:image-set('https://t.example.com/x.gif' 1x)",
    "background:-webkit-image-set(url('https://t.example.com/x.gif') 1x)",
    "src:src('https://t.example.com/f.woff2')",
])
def test_style_remote_refs_that_dodge_a_naive_url_match(svc, style):
    """Every one of these reached the browser with the third-party URL intact AND
    left blocked_remote at 0, so no banner warned the reader. The iframe CSP
    refused the fetch, but the sanitizer is the trust boundary - a second consumer
    of it (agent tool, export, a future mobile view) has no CSP behind it."""
    pk = _ingest_html(svc, f'<div style="{style}">A</div>')
    body = svc.get_body(pk)
    assert "t.example.com" not in body["html"]
    assert body["blocked_remote"] == 1, "a dropped remote style must be counted"


def test_blocked_style_is_counted_so_the_reader_is_told(svc):
    """A mail whose only tracker sits in CSS must not report 'nothing blocked'."""
    pk = _ingest_html(svc, '<div style="background:url(https://t.example.com/p.gif)">A</div>'
                           '<p style="color:#333">plain styles stay uncounted</p>')
    body = svc.get_body(pk)
    assert body["blocked_remote"] == 1
    assert 'style="color:#333"' in body["html"].replace("'", '"')


def test_attachment_by_cid_and_uncached_body(svc):
    pk = _ingest_html(svc, "<p>x</p>")
    att = svc.get_attachment(pk, "logo1")
    assert att is not None
    filename, ctype, payload = att
    assert ctype == "image/png" and payload.startswith(b"\x89PNG")
    # message without cached raw: body falls back to snippet, no html
    apk = svc.store.account_pk("bob@example.com")
    fpk = svc.store.upsert_folder(apk, "INBOX")
    pk2 = svc.store.ingest_message(
        apk, fpk, 99, ParsedMessage(message_id="<nc@example.com>", subject="NC",
                                    body_text="only headers synced"))
    body2 = svc.get_body(pk2)
    assert body2["cached"] is False and body2["html"] is None
    assert svc.get_attachment(pk2, "1") is None
