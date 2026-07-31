# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Secure storage for email credentials (OAuth tokens and IMAP passwords).

Uses OS keyring (Windows Credential Manager, macOS Keychain, Linux Secret Service)
with fallback to an AES-256-GCM encrypted file under Platform.data_dir() when
keyring is unavailable. No credentials are stored in config.json.
"""

import json
import logging
import threading
from typing import Any, Dict, Optional

from vaf.core.config import get_local_admin_scope_id, get_local_admin_username
from vaf.core.platform import Platform
from vaf.core.log_helper import append_domain_log_always
from vaf.core.secure_store import SecureBlobStore, keyring_available

logger = logging.getLogger("vaf.core.credential_store")

SERVICE_NAME = "vaf-email"
_CREDENTIALS_KEY = "email_credentials_key"  # legacy config key; migrated to a wrapped DEK by secure_store

_store_singleton: Optional[SecureBlobStore] = None
_store_lock = threading.Lock()


def _store() -> SecureBlobStore:
    """Lazily-created encrypted fallback store (path resolved on first use)."""
    global _store_singleton
    if _store_singleton is None:
        with _store_lock:
            if _store_singleton is None:
                _store_singleton = SecureBlobStore(
                    "email", Platform.data_dir() / "email_credentials.enc", _CREDENTIALS_KEY
                )
    return _store_singleton


def _local_admin_scope_id() -> str:
    return get_local_admin_scope_id()


# ── One key builder for every lane ───────────────────────────────────────────────────────────────
# There used to be THREE `_credential_key` functions - here, in vaf/github/credential_github.py and
# in vaf/cloud/credential_cloud.py - written independently, none importing another. Two knew about
# `user_scope_id`; the cloud one had no such parameter at all, which is why the cloud lane keyed per
# NAME while these two keyed per SCOPE, and why a tenant reached the owner's cloud accounts. The one
# copy nobody touched is the one that fell behind, which is the usual direction for a copy.
#
# The lanes differ on FOUR axes, and every one of them was measured from the old functions rather
# than derived by reading. Two would not be guessed: this namespace is literally `email:` even for a
# google_drive provider, and github writes an extra literal segment before a name. They travel as
# DATA below, not as branches - a fourth special case would mean the design is wrong.
#
# The exact strings are frozen in tests/test_credential_key_baseline.py, measured BEFORE this merge.
# A key is not an implementation detail: it is the address of a secret already on disk and in the
# keyring. One character different and the owner's credentials are not migrated, they are gone.
_KEY_PROFILES = {
    #             provider segment   admin form        name prefix
    "email":     (True,              None,             None),
    "github":    (False,             "default",        "user"),
    "cloud":     (True,              None,             None),
}


def build_credential_key(
    account_id: str,
    *,
    namespace: str,
    provider: Optional[str] = None,
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> str:
    """Address a stored credential for one account, for one identity.

    IDENTITY IS KEYWORD-ONLY, deliberately. The old cloud signature was
    `(account_id, provider, username)`, so anything identity-shaped could be handed in
    positionally and land in the wrong slot without a word from Python. Making it keyword-only
    deletes that failure mode instead of testing for it - the same lesson as assigning identity
    rather than passing it through.

    Precedence: a scope wins over a name, and the configured local admin's scope collapses to the
    form with no identity segment, because the machine owner's credentials predate scoping and are
    stored unscoped.
    """
    try:
        has_provider, admin_form, name_prefix = _KEY_PROFILES[namespace]
    except KeyError:
        raise ValueError(f"unknown credential namespace {namespace!r}") from None

    safe_id = (account_id or "").strip().lower().replace(" ", "_")
    head = [namespace]
    if has_provider:
        head.append(provider or "")

    if user_scope_id and str(user_scope_id).strip():
        scope_str = str(user_scope_id).strip()
        if scope_str != _local_admin_scope_id():
            return ":".join([*head, scope_str, safe_id])
        return ":".join([*head, admin_form, safe_id]) if admin_form else ":".join([*head, safe_id])

    if username and str(username).strip():
        safe_user = str(username).strip().lower().replace(" ", "_")
        parts = [*head, *( [name_prefix] if name_prefix else [] ), safe_user, safe_id]
        return ":".join(parts)

    return ":".join([*head, admin_form, safe_id]) if admin_form else ":".join([*head, safe_id])


def _credential_key(
    account_id: str,
    provider: str = "email",
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> str:
    """Mail/IMAP key. A thin adapter over the shared builder, kept because this lane's callers
    pass `provider` positionally in four places and the signature is not worth churning; the
    KEY LOGIC lives in build_credential_key and exists once.

    IT MUST STAY LOGIC-FREE. An adapter that survives is an invitation to put "just this one
    special case" inside it, and at that moment there are two truths about how a credential is
    addressed again - only disguised, because the file still looks consolidated. If this lane
    ever needs a shape the builder cannot produce, that shape belongs in the builder's profile
    table with the others, or the design is wrong."""
    return build_credential_key(account_id, namespace="email", provider=provider,
                                username=username, user_scope_id=user_scope_id)


def _cred_key_username(username: Optional[str]) -> Optional[str]:
    """Normalize username for credential key lookup: None for local admin (matches storage)."""
    if not username or not str(username).strip():
        return None
    local_admin = get_local_admin_username().lower()
    if str(username).strip().lower() == local_admin:
        return None
    return str(username).strip()


def _cred_key_scope(user_scope_id: Optional[str]) -> Optional[str]:
    """Normalize user_scope_id for credential key: None for local admin scope (legacy key)."""
    if not user_scope_id or not str(user_scope_id).strip():
        return None
    if str(user_scope_id).strip() == _local_admin_scope_id():
        return None
    return str(user_scope_id).strip()


def get_email_credentials(
    account_id: str,
    provider: str = "email",
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Retrieve stored credentials for an email account.
    Returns dict with either OAuth fields (access_token, refresh_token, expires_at)
    or IMAP field (password). Returns None if not found or on error.
    When username or user_scope_id is set (multi-user mode), credentials are scoped to that user.
    """
    u = _cred_key_username(username)
    s = _cred_key_scope(user_scope_id)
    
    # Provider candidates: some parts of the system use 'gmail'/'microsoft', others use 'email'
    providers_to_try = [provider]
    if provider in ("gmail", "microsoft", "outlook") and "email" not in providers_to_try:
        providers_to_try.append("email")
    elif provider == "email":
        # If we only have 'email', we might also have it under 'gmail' or 'microsoft'
        # but account_id usually tells us. For now, just add them as fallback if needed.
        pass

    # Strict isolation: non-admin scopes must never fall back to admin/legacy keys.
    # Only local-admin context may probe legacy key formats for backward compatibility.
    is_local_admin_context = (s is None and u is None)

    keys_to_try = []
    for p in providers_to_try:
        keys_to_try.append(_credential_key(account_id, p, u, user_scope_id=s))
        if is_local_admin_context:
            safe_id = (account_id or "").strip().lower().replace(" ", "_")
            legacy_admin_key = f"email:{p}:admin:{safe_id}"
            if legacy_admin_key not in keys_to_try:
                keys_to_try.append(legacy_admin_key)
            unscoped_key = f"email:{p}:{safe_id}"
            if unscoped_key not in keys_to_try:
                keys_to_try.append(unscoped_key)

    # Log candidates for debugging
    # append_domain_log_always("backend", f"CRED_LOOKUP account={account_id} candidates={','.join(keys_to_try)}")

    if keyring_available():
        import keyring
        for key in keys_to_try:
            try:
                value = keyring.get_password(SERVICE_NAME, key)
                if value:
                    append_domain_log_always("backend", f"CRED_FOUND_KEYRING key={_mask(key)}")
                    return json.loads(value)
            except Exception as e:
                logger.debug("Keyring get failed for %s: %s", _mask(key), e)
        return None

    data = _store().load()
    for key in keys_to_try:
        raw = data.get(key)
        if raw:
            try:
                append_domain_log_always("backend", f"CRED_FOUND_FALLBACK key={_mask(key)}")
                return json.loads(raw)
            except Exception:
                continue
    return None


def set_email_oauth_tokens(
    account_id: str,
    provider: str,
    access_token: str,
    refresh_token: str,
    expires_at: Optional[float] = None,
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> None:
    """Store OAuth tokens for an account. Prefer keyring; fallback to encrypted file. Optional username/user_scope_id for multi-user scope."""
    key = _credential_key(account_id, provider, _cred_key_username(username), user_scope_id=_cred_key_scope(user_scope_id))
    value = json.dumps({
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at,
        "type": "oauth",
    })
    if keyring_available():
        try:
            import keyring
            keyring.set_password(SERVICE_NAME, key, value)
            return
        except Exception as e:
            logger.warning("Keyring set failed, using fallback: %s", e)
    _store().update(lambda d: d.__setitem__(key, value))


def set_email_imap_password(
    account_id: str,
    password: str,
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> None:
    """Store IMAP/SMTP password for an account. Prefer keyring; fallback to encrypted file. Optional username/user_scope_id for multi-user scope."""
    key = _credential_key(account_id, "imap", _cred_key_username(username), user_scope_id=_cred_key_scope(user_scope_id))
    value = json.dumps({"password": password, "type": "imap"})
    if keyring_available():
        try:
            import keyring
            keyring.set_password(SERVICE_NAME, key, value)
            return
        except Exception as e:
            logger.warning("Keyring set failed, using fallback: %s", e)
    _store().update(lambda d: d.__setitem__(key, value))


def delete_email_credentials(
    account_id: str,
    provider: Optional[str] = None,
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> None:
    """
    Remove stored credentials for an account.
    If provider is None, deletes both email:* and email:imap:* keys for this account_id.
    Optional username/user_scope_id for multi-user scope.
    """
    safe_username = _cred_key_username(username)
    safe_scope = _cred_key_scope(user_scope_id)
    
    # Collect all possible keys to delete (primary + legacy admin)
    base_keys = []
    if provider:
        base_keys.append((account_id, provider, safe_username, safe_scope))
        if safe_username is None: # Add legacy admin fallback for deletion
            base_keys.append((account_id, provider, "admin", safe_scope))
    else:
        # Default providers to check. microsoft_imap is the token lane an OAuth
        # Microsoft account gets after the IMAP re-consent/upgrade (imap_client uses
        # it for XOAUTH2); it MUST be in this set or a deleted account leaves an
        # orphaned IMAP token behind (fail-closed lifecycle, EMAIL_CLIENT.md).
        for p in ("email", "imap", "gmail", "microsoft", "microsoft_imap", "outlook", "apple", "icloud"):
            base_keys.append((account_id, p, safe_username, safe_scope))
            if safe_username is None:
                base_keys.append((account_id, p, "admin", safe_scope))

    keys_to_delete = []
    for aid, p, u, s in base_keys:
        keys_to_delete.append(_credential_key(aid, p, u, user_scope_id=s))

    if keyring_available():
        try:
            import keyring
            for k in keys_to_delete:
                try:
                    keyring.delete_password(SERVICE_NAME, k)
                except Exception:
                    pass
            return
        except Exception as e:
            logger.warning("Keyring delete failed, cleaning fallback: %s", e)

    def _drop(d):
        for k in keys_to_delete:
            d.pop(k, None)
    _store().update(_drop)


def _mask(s: str) -> str:
    """Mask credential key for logging (show only prefix)."""
    if len(s) <= 12:
        return "***"
    return s[:8] + "***"


def is_keyring_used() -> bool:
    """Return True if keyring backend is in use (for UI hint)."""
    return keyring_available()
