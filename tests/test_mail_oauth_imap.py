# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""C8/T8+T19: the Microsoft IMAP OAuth lane must resolve the account address from
the id_token claims (Graph /me is unreachable with the outlook.office.com token)
and must find an env-configured Microsoft client id under the shared env keys."""
import base64
import json

from vaf.core.oauth_pkce import (
    _email_from_id_token,
    _get_account_id_from_tokens,
    _get_oauth_client_credential,
)


def _id_token(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"eyJhbGciOiJub25lIn0.{payload}.sig"  # header.payload.sig (header/sig ignored)


def test_email_from_id_token_claim_precedence():
    assert _email_from_id_token(_id_token({"email": "a@b.com"})) == "a@b.com"
    assert _email_from_id_token(_id_token({"preferred_username": "U@B.COM"})) == "u@b.com"
    assert _email_from_id_token(_id_token({"upn": "x@y.de"})) == "x@y.de"
    assert _email_from_id_token(_id_token({"sub": "no-email-here"})) == ""
    assert _email_from_id_token(None) == ""
    assert _email_from_id_token("not-a-jwt") == ""


def test_microsoft_imap_account_id_comes_from_id_token_not_unknown():
    got = _get_account_id_from_tokens(
        "microsoft_imap", "access-token-unused",
        {"id_token": _id_token({"email": "user@contoso.com"})})
    assert got == "user@contoso.com"          # NOT "unknown_<hex>"


def test_microsoft_imap_env_client_id_uses_microsoft_env(monkeypatch):
    from vaf.core.config import Config
    monkeypatch.setattr(Config, "get", staticmethod(lambda key, default=None: ""))
    monkeypatch.setenv("VAF_EMAIL_OAUTH_MICROSOFT_CLIENT_ID", "the-client-id")
    assert _get_oauth_client_credential("microsoft_imap", "client_id") == "the-client-id"
