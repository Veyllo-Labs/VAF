# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Real IMAP client construction for the sync engine.

Security invariants carried over from the v1 transport (EMAIL_CLIENT.md):
- TLS is verified and not disableable (system trust store + hostname check).
- Every connect passes the SSRF guard; private hosts only via the admin-only
  email_allow_private_hosts flag.
- Credentials come from credential_store at connect time and never leave this
  module. Auth lanes: password/app-password (type 'imap') and XOAUTH2 (type
  'oauth' - functional once tokens carry IMAP scopes, phase 3 re-consent).
"""
import logging
import ssl
from typing import Any, Dict, Optional

logger = logging.getLogger("vaf.mail.imap_client")

IMAP_TIMEOUT_SEC = 30

_PROVIDER_IMAP_DEFAULTS = {
    "gmail": ("imap.gmail.com", 993),
    "microsoft": ("outlook.office365.com", 993),
    "imap": ("imap.gmail.com", 993),
}


class MailAuthError(Exception):
    """Login/authentication failed (distinguishable from connectivity issues)."""


def build_imap_client(account: Dict[str, Any], username: Optional[str],
                      user_scope_id: Optional[str]):
    """Connect + authenticate an IMAPClient for the account. Raises
    MailAuthError on auth failure, ValueError on SSRF-guard refusal."""
    from imapclient import IMAPClient  # lazy: "mail" extra
    from vaf.core.config import Config
    from vaf.core.credential_store import get_email_credentials
    from vaf.network.binding import assert_safe_remote_host

    provider = (account.get("provider") or "imap").lower()
    account_id = account.get("account_id") or account.get("email") or ""
    default_host, default_port = _PROVIDER_IMAP_DEFAULTS.get(provider, _PROVIDER_IMAP_DEFAULTS["imap"])
    host = (account.get("imap_host") or "").strip() or default_host
    port = int(account.get("imap_port") or default_port)

    allow_private = bool(Config.get("email_allow_private_hosts", False))
    assert_safe_remote_host(host, allow_private=allow_private)

    ctx = ssl.create_default_context()  # verified TLS, never disableable
    client = IMAPClient(host, port=port, ssl=True, ssl_context=ctx, timeout=IMAP_TIMEOUT_SEC)

    creds = get_email_credentials(account_id, provider if provider != "imap" else "imap",
                                  username, user_scope_id=user_scope_id)
    try:
        if creds and creds.get("type") == "imap" and creds.get("password"):
            client.login(account.get("email") or account_id, creds["password"])
        elif creds and creds.get("type") == "oauth":
            from vaf.core.oauth_pkce import get_valid_access_token
            # Microsoft mail tokens live under their own record (outlook.office.com
            # resource); Google uses the union token (EMAIL_CLIENT.md, E1).
            token_provider = "microsoft_imap" if provider == "microsoft" else provider
            token = get_valid_access_token(account_id, token_provider, username,
                                           user_scope_id=user_scope_id)
            if not token:
                raise MailAuthError("no valid OAuth token for IMAP (re-consent required?)")
            client.oauth2_login(account.get("email") or account_id, token)
        else:
            raise MailAuthError("no stored credentials for this account")
    except MailAuthError:
        _safe_logout(client)
        raise
    except Exception as e:
        _safe_logout(client)
        raise MailAuthError(str(e)) from e
    return client


def _safe_logout(client) -> None:
    try:
        client.logout()
    except Exception:
        try:
            client.shutdown()
        except Exception:
            pass
