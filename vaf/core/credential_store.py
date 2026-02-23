"""
Secure storage for email credentials (OAuth tokens and IMAP passwords).

Uses OS keyring (Windows Credential Manager, macOS Keychain, Linux Secret Service)
with fallback to an AES-256-GCM encrypted file under Platform.data_dir() when
keyring is unavailable. No credentials are stored in config.json.
"""

import base64
import json
import logging
import secrets
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from vaf.core.config import Config, get_local_admin_scope_id, get_local_admin_username
from vaf.core.platform import Platform

logger = logging.getLogger("vaf.core.credential_store")

SERVICE_NAME = "vaf-email"
_KEYRING_AVAILABLE: Optional[bool] = None
_KEYRING_LOCK = threading.Lock()
_FALLBACK_PATH: Optional[Path] = None
_CREDENTIALS_KEY = "email_credentials_key"
_KEY_SIZE = 32
_NONCE_SIZE = 12


def _keyring_available() -> bool:
    """Check if keyring backend is available (thread-safe)."""
    global _KEYRING_AVAILABLE
    with _KEYRING_LOCK:
        if _KEYRING_AVAILABLE is None:
            try:
                import keyring
                keyring.get_keyring()
                # Probe: set and get a test value
                keyring.set_password(SERVICE_NAME, "__vaf_probe__", "x")
                keyring.get_password(SERVICE_NAME, "__vaf_probe__")
                keyring.delete_password(SERVICE_NAME, "__vaf_probe__")
                _KEYRING_AVAILABLE = True
            except Exception as e:
                logger.info("Keyring unavailable, using encrypted file fallback: %s", e)
                _KEYRING_AVAILABLE = False
        return _KEYRING_AVAILABLE


def _local_admin_scope_id() -> str:
    return get_local_admin_scope_id()


def _credential_key(
    account_id: str,
    provider: str = "email",
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> str:
    """Build keyring/fallback key for an account. When user_scope_id is set use scope key; else when username set scope by user; else legacy."""
    safe_id = (account_id or "").strip().lower().replace(" ", "_")
    if user_scope_id and str(user_scope_id).strip():
        scope_str = str(user_scope_id).strip()
        if scope_str == _local_admin_scope_id():
            return f"email:{provider}:{safe_id}"
        return f"email:{provider}:{scope_str}:{safe_id}"
    if username and str(username).strip():
        safe_user = str(username).strip().lower().replace(" ", "_")
        return f"email:{provider}:{safe_user}:{safe_id}"
    return f"email:{provider}:{safe_id}"


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


def _get_fallback_path() -> Path:
    global _FALLBACK_PATH
    if _FALLBACK_PATH is None:
        _FALLBACK_PATH = Platform.data_dir() / "email_credentials.enc"
    return _FALLBACK_PATH


def _get_or_create_encryption_key() -> bytes:
    """Get or create 32-byte key for fallback file. Stored in config (base64)."""
    encoded = Config.get(_CREDENTIALS_KEY, "")
    if encoded:
        try:
            return base64.b64decode(encoded)
        except Exception:
            pass
    new_key = secrets.token_bytes(_KEY_SIZE)
    Config.set(_CREDENTIALS_KEY, base64.b64encode(new_key).decode())
    return new_key


def _load_fallback_data() -> Dict[str, str]:
    """Load and decrypt fallback file. Returns dict mapping credential_key -> json string."""
    path = _get_fallback_path()
    if not path.exists():
        return {}
    try:
        raw = path.read_bytes()
        if len(raw) < _NONCE_SIZE:
            return {}
        nonce = raw[:_NONCE_SIZE]
        ciphertext = raw[_NONCE_SIZE:]
        key = _get_or_create_encryption_key()
        aes = AESGCM(key)
        decrypted = aes.decrypt(nonce, ciphertext, None).decode("utf-8")
        return json.loads(decrypted)
    except Exception as e:
        logger.warning("Failed to load credential fallback file: %s", e)
        return {}


def _save_fallback_data(data: Dict[str, str]) -> None:
    """Encrypt and write fallback file. Key from config only."""
    path = _get_fallback_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = _get_or_create_encryption_key()
    nonce = secrets.token_bytes(_NONCE_SIZE)
    aes = AESGCM(key)
    payload = json.dumps(data).encode("utf-8")
    ciphertext = aes.encrypt(nonce, payload, None)
    path.write_bytes(nonce + ciphertext)


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
    key = _credential_key(account_id, provider, _cred_key_username(username), user_scope_id=user_scope_id)
    if _keyring_available():
        try:
            import keyring
            value = keyring.get_password(SERVICE_NAME, key)
            if not value:
                return None
            return json.loads(value)
        except Exception as e:
            logger.debug("Keyring get failed for %s: %s", _mask(key), e)
            return None
    data = _load_fallback_data()
    raw = data.get(key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
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
    if _keyring_available():
        try:
            import keyring
            keyring.set_password(SERVICE_NAME, key, value)
            return
        except Exception as e:
            logger.warning("Keyring set failed, using fallback: %s", e)
    data = _load_fallback_data()
    data[key] = value
    _save_fallback_data(data)


def set_email_imap_password(
    account_id: str,
    password: str,
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> None:
    """Store IMAP/SMTP password for an account. Prefer keyring; fallback to encrypted file. Optional username/user_scope_id for multi-user scope."""
    key = _credential_key(account_id, "imap", _cred_key_username(username), user_scope_id=_cred_key_scope(user_scope_id))
    value = json.dumps({"password": password, "type": "imap"})
    if _keyring_available():
        try:
            import keyring
            keyring.set_password(SERVICE_NAME, key, value)
            return
        except Exception as e:
            logger.warning("Keyring set failed, using fallback: %s", e)
    data = _load_fallback_data()
    data[key] = value
    _save_fallback_data(data)


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
    keys_to_delete = []
    if provider:
        keys_to_delete.append(_credential_key(account_id, provider, safe_username, user_scope_id=safe_scope))
    else:
        keys_to_delete.append(_credential_key(account_id, "email", safe_username, user_scope_id=safe_scope))
        keys_to_delete.append(_credential_key(account_id, "imap", safe_username, user_scope_id=safe_scope))
        for p in ("gmail", "microsoft", "outlook", "apple", "icloud"):
            keys_to_delete.append(_credential_key(account_id, p, safe_username, user_scope_id=safe_scope))

    if _keyring_available():
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
    data = _load_fallback_data()
    for k in keys_to_delete:
        data.pop(k, None)
    _save_fallback_data(data)


def _mask(s: str) -> str:
    """Mask credential key for logging (show only prefix)."""
    if len(s) <= 12:
        return "***"
    return s[:8] + "***"


def is_keyring_used() -> bool:
    """Return True if keyring backend is in use (for UI hint)."""
    return _keyring_available()
