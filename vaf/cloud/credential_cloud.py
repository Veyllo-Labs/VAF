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


# ── Credential keys: SHARED BUILDER, NOW WITH THE SCOPE (part 2 of 2, 2026-08-01) ───────────────
# The same builder as mail and github, so the three key formats are one decision rather than three
# copies - and now with the identity that decides ownership.
#
# WHY THE SCOPE AND NOT THE NAME. A name is resolved per lane, so a lane that supplies none
# collapses to the owner's key; the scope is the only thing that can answer "am I the owner" for a
# caller who has no name. Part 1 (`579431b0`) fixed the TOOL's resolution and left this note saying
# the question was answered per caller rather than removed. It is removed here.
#
# THE ORDER WAS MEASURED AND IT DECIDED THE ROUND: the hole is a READ. Nine callers reach these
# functions, and all but three route through `get_valid_access_token(account_id, provider, ...)` in
# the providers, which had no scope. Changing the WRITE key first would have left every reader on
# the old form - the credentials would have gone missing for the very user who just connected,
# while a tenant kept reading the owner's. So the providers learned the scope first (`base.py` plus
# five subclasses plus ONE factory that used to be three copies), and only then this file.
#
# THE LOCK, and its assurance points at the OWNERLESS form rather than at "fallbacks in general":
# once a scoped key format exists, a scoped caller must never fall back to `cloud:<provider>:<acct>`
# - that form IS the hole. Exactly one legacy probe is permitted, the caller's OWN non-empty name,
# and on a hit the entry is re-keyed and the old one DELETED, so the branch drains instead of
# becoming a second permanent truth. An empty name yields no probe at all: `_cred_key_username`
# normalizes empty to None, and the name key would then collapse onto the ownerless form.
from vaf.core.credential_store import build_credential_key


def _scope_for_key(user_scope_id: Optional[str]) -> Optional[str]:
    """Normalize a scope for the key builder: blank is no scope."""
    s = (str(user_scope_id).strip() if user_scope_id else "")
    return s or None

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


def get_cloud_credentials(
    account_id: str,
    provider: str,
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Retrieve stored credentials for a cloud account, for one identity.

    STRICT for a scoped caller: their key, then AT MOST their own named key, and never the
    ownerless form. A found legacy entry is re-keyed and removed, so the probe drains.
    """
    key_username = _cred_key_username(username)
    scope = _scope_for_key(user_scope_id)
    account_ids_to_try = _normalize_google_email(account_id) if provider == "google_drive" else [account_id]

    for aid in account_ids_to_try:
        key = build_credential_key(aid, namespace="cloud", provider=provider,
                                   username=key_username, user_scope_id=scope)
        raw = _get_credential_raw(key)
        if raw:
            try:
                return json.loads(raw)
            except Exception:
                continue

        # THE ONE PERMITTED LEGACY PROBE. Only for a caller that HAS a scope (an unscoped
        # caller's key already IS the ownerless form, so there is nothing to migrate) and
        # only on their own non-empty name. Never the ownerless form: that is the hole this
        # change closes, and reaching it from a scoped caller would reopen it under the name
        # of compatibility.
        if not scope or not key_username:
            continue
        legacy = build_credential_key(aid, namespace="cloud", provider=provider,
                                      username=key_username, user_scope_id=None)
        if legacy == key:
            continue
        raw = _get_credential_raw(legacy)
        if not raw:
            continue
        try:
            creds = json.loads(raw)
        except Exception:
            continue
        _rekey_credential(legacy, key, raw)
        return creds
    return None


def _rekey_credential(old_key: str, new_key: str, raw: str) -> None:
    """Move one credential onto its scoped key and DELETE the old one. Never raises.

    Deleting is what makes the probe above temporary. Leaving the old entry would turn a
    migration into a second permanent lookup - the shape recorded in `api_keys` as the
    estate branch that has to end, one lane over. Soft on failure for the same reason the
    key migration is: the credential was found and is usable, and a read-only data
    directory says nothing about whether it is valid.
    """
    try:
        _store().update(lambda d: (d.__setitem__(new_key, raw), d.pop(old_key, None))[0])
    except Exception as e:                                  # noqa: BLE001 - see docstring
        logger.debug("Cloud credential re-key failed for %s: %s", _mask(old_key), e)
        return
    if keyring_available():
        try:
            import keyring
            keyring.set_password(SERVICE_NAME, new_key, raw)
            keyring.delete_password(SERVICE_NAME, old_key)
        except Exception as e:
            logger.debug("Keyring re-key failed for cloud %s: %s", _mask(old_key), e)


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
    user_scope_id: Optional[str] = None,
) -> None:
    """Store OAuth tokens for a cloud account, under the caller's identity."""
    # Use canonical @gmail.com for Google (googlemail.com equivalent)
    store_id = account_id
    if provider == "google_drive" and isinstance(account_id, str):
        store_id = (account_id or "").strip().lower().replace("@googlemail.com", "@gmail.com")
    key = build_credential_key(store_id, namespace="cloud", provider=provider,
                               username=_cred_key_username(username),
                               user_scope_id=_scope_for_key(user_scope_id))
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
    user_scope_id: Optional[str] = None,
) -> None:
    """Store WebDAV credentials (Nextcloud app password)."""
    key = build_credential_key(account_id, namespace="cloud", provider="nextcloud",
                               username=_cred_key_username(username),
                               user_scope_id=_scope_for_key(user_scope_id))
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


def delete_cloud_credentials(
    account_id: str,
    provider: str,
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> None:
    """Remove stored credentials for a cloud account."""
    key = build_credential_key(account_id, namespace="cloud", provider=provider,
                               username=_cred_key_username(username),
                               user_scope_id=_scope_for_key(user_scope_id))
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
