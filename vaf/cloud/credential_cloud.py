# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Secure storage for cloud-provider credentials (OAuth tokens, WebDAV passwords).

Mirrors vaf/core/credential_store.py with a separate keyring namespace (vaf-cloud)
and encrypted fallback file (cloud_credentials.enc) to avoid interfering with email
credentials.
"""

import json
import logging
import threading
from typing import Any, Dict, Optional

from vaf.core.config import Config
from vaf.core.platform import Platform
from vaf.core.secure_store import SecureBlobStore, keyring_available

logger = logging.getLogger("vaf.cloud.credentials")

SERVICE_NAME = "vaf-cloud"
_CREDENTIALS_KEY = "cloud_credentials_key"  # legacy config key; migrated to a wrapped DEK by secure_store

_store_singleton: Optional[SecureBlobStore] = None
_store_lock = threading.Lock()


def _store() -> SecureBlobStore:
    """Lazily-created encrypted fallback store (path resolved on first use)."""
    global _store_singleton
    if _store_singleton is None:
        with _store_lock:
            if _store_singleton is None:
                _store_singleton = SecureBlobStore(
                    "cloud", Platform.data_dir() / "cloud_credentials.enc", _CREDENTIALS_KEY
                )
    return _store_singleton


# ── Credential keys: SHARED BUILDER, STILL WITHOUT A SCOPE (part 1 of 2) ────────────────────────
# This lane calls the same builder as mail and github, which makes the three key formats one
# decision instead of three copies. It does NOT pass `user_scope_id` yet.
#
# CORRECTED 2026-07-31, because this paragraph and the changelog said opposite things and a reader
# had no way to tell which was stale. It used to read "without a scope every caller resolves to the
# same key, so a tenant reaches the machine owner's cloud accounts". That premise no longer holds,
# and it held for a reason that was never in this file: `cloud_storage` resolved its caller from an
# environment variable nobody sets, so it handed this builder the OWNER's name every time. With the
# tool taking its identity from the caller, the keys already diverge - `cloud:<provider>:<acct>` for
# the owner, `cloud:<provider>:<name>:<acct>` for a named tenant, `cloud:<provider>:scope_<hex>:
# <acct>` for one whose name is not known.
#
# WHAT REMAINS, stated narrowly so it is not read as done: the key is keyed on a NAME, and a name is
# resolved per lane. A lane that supplies no name still collapses to the owner's key, and the
# librarian's dispatch path is exactly such a lane today. A scope would remove the question instead
# of answering it per caller.
#
# PART 2 adds it, and it is a bigger change than swapping this call: the four public functions below
# take `username` and no scope, and the readers reach them through the provider classes -
# `BaseProvider(username, account_id)` plus five subclasses and three `_create_provider` copies. The
# ORDER matters and was measured: the hole is a READ, so the providers must carry a scope BEFORE the
# key changes; changing the write key first would leave every reader on the old form. It also needs
# the lock mail already has - once a scope format exists, a scoped user must never fall back to the
# OWNERLESS form - plus a single migration probe on the caller's own non-empty name, which re-keys
# and deletes rather than becoming a permanent second lookup.
from vaf.core.credential_store import build_credential_key

def _cred_key_username(username: Optional[str]) -> Optional[str]:
    """Normalize username for credential key lookup: None for local admin (matches storage)."""
    if not username or not str(username).strip():
        return None
    local_admin = (Config.get("local_admin_username") or "admin").strip().lower()
    if str(username).strip().lower() == local_admin:
        return None
    return str(username).strip()


# ---------------------------------------------------------------------------
#  Public API
# ---------------------------------------------------------------------------

def _normalize_google_email(email: str) -> list:
    """Return [email, alt] for lookup: @googlemail.com <-> @gmail.com are equivalent."""
    e = (email or "").strip().lower()
    if not e or "@" not in e:
        return [e]
    if e.endswith("@googlemail.com"):
        return [e, e.replace("@googlemail.com", "@gmail.com")]
    if e.endswith("@gmail.com"):
        return [e, e.replace("@gmail.com", "@googlemail.com")]
    return [e]


def get_cloud_credentials(account_id: str, provider: str, username: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Retrieve stored credentials for a cloud account."""
    key_username = _cred_key_username(username)
    account_ids_to_try = _normalize_google_email(account_id) if provider == "google_drive" else [account_id]
    for aid in account_ids_to_try:
        key = build_credential_key(aid, namespace="cloud", provider=provider,
                                   username=key_username)  # no scope yet - part 2
        raw = _get_credential_raw(key)
        if raw:
            try:
                return json.loads(raw)
            except Exception:
                continue
    return None


def _get_credential_raw(key: str) -> Optional[str]:
    """Get raw credential JSON string by key from keyring or fallback file."""
    if keyring_available():
        try:
            import keyring
            value = keyring.get_password(SERVICE_NAME, key)
            if value:
                return value
        except Exception as e:
            logger.debug("Keyring get failed for cloud %s: %s", _mask(key), e)
    return _store().load().get(key)


def set_cloud_oauth_tokens(
    account_id: str,
    provider: str,
    access_token: str,
    refresh_token: str,
    expires_at: Optional[float] = None,
    username: Optional[str] = None,
) -> None:
    """Store OAuth tokens for a cloud account."""
    # Use canonical @gmail.com for Google (googlemail.com equivalent)
    store_id = account_id
    if provider == "google_drive" and isinstance(account_id, str):
        store_id = (account_id or "").strip().lower().replace("@googlemail.com", "@gmail.com")
    key = build_credential_key(store_id, namespace="cloud", provider=provider,
                               username=_cred_key_username(username))  # no scope yet - part 2
    value = json.dumps({
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at,
        "type": "oauth",
    })
    # Always write to fallback file (reliable across processes/Keyring backends on Windows)
    _store().update(lambda d: d.__setitem__(key, value))
    # Also try keyring for systems where it works
    if keyring_available():
        try:
            import keyring
            keyring.set_password(SERVICE_NAME, key, value)
        except Exception as e:
            logger.debug("Keyring set failed for cloud (fallback file used): %s", e)


def set_cloud_webdav_credentials(
    account_id: str,
    url: str,
    webdav_username: str,
    password: str,
    username: Optional[str] = None,
) -> None:
    """Store WebDAV credentials (Nextcloud app password)."""
    key = build_credential_key(account_id, namespace="cloud", provider="nextcloud",
                               username=_cred_key_username(username))  # no scope yet - part 2
    value = json.dumps({
        "url": url,
        "webdav_username": webdav_username,
        "password": password,
        "type": "webdav",
    })
    if keyring_available():
        try:
            import keyring
            keyring.set_password(SERVICE_NAME, key, value)
            return
        except Exception as e:
            logger.warning("Keyring set failed for cloud, using fallback: %s", e)
    _store().update(lambda d: d.__setitem__(key, value))


def delete_cloud_credentials(account_id: str, provider: str, username: Optional[str] = None) -> None:
    """Remove stored credentials for a cloud account."""
    key = build_credential_key(account_id, namespace="cloud", provider=provider,
                               username=_cred_key_username(username))  # no scope yet - part 2
    if keyring_available():
        try:
            import keyring
            keyring.delete_password(SERVICE_NAME, key)
            return
        except Exception:
            pass
    _store().update(lambda d: d.pop(key, None))


def _mask(s: str) -> str:
    if len(s) <= 12:
        return "***"
    return s[:8] + "***"
