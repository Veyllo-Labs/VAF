"""
Secure storage for cloud-provider credentials (OAuth tokens, WebDAV passwords).

Mirrors vaf/core/credential_store.py with a separate keyring namespace (vaf-cloud)
and encrypted fallback file (cloud_credentials.enc) to avoid interfering with email
credentials.
"""

import base64
import json
import logging
import secrets
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from vaf.core.config import Config
from vaf.core.platform import Platform

logger = logging.getLogger("vaf.cloud.credentials")

SERVICE_NAME = "vaf-cloud"
_KEYRING_AVAILABLE: Optional[bool] = None
_KEYRING_LOCK = threading.Lock()
_FALLBACK_PATH: Optional[Path] = None
_CREDENTIALS_KEY = "cloud_credentials_key"
_KEY_SIZE = 32
_NONCE_SIZE = 12


# ---------------------------------------------------------------------------
#  Keyring
# ---------------------------------------------------------------------------

def _keyring_available() -> bool:
    global _KEYRING_AVAILABLE
    with _KEYRING_LOCK:
        if _KEYRING_AVAILABLE is None:
            try:
                import keyring
                keyring.get_keyring()
                keyring.set_password(SERVICE_NAME, "__vaf_probe__", "x")
                keyring.get_password(SERVICE_NAME, "__vaf_probe__")
                keyring.delete_password(SERVICE_NAME, "__vaf_probe__")
                _KEYRING_AVAILABLE = True
            except Exception as e:
                logger.info("Keyring unavailable for cloud, using encrypted file: %s", e)
                _KEYRING_AVAILABLE = False
        return _KEYRING_AVAILABLE


# ---------------------------------------------------------------------------
#  Key helpers
# ---------------------------------------------------------------------------

def _credential_key(account_id: str, provider: str, username: Optional[str] = None) -> str:
    safe_id = (account_id or "").strip().lower().replace(" ", "_")
    if username and str(username).strip():
        safe_user = str(username).strip().lower().replace(" ", "_")
        return f"cloud:{provider}:{safe_user}:{safe_id}"
    return f"cloud:{provider}:{safe_id}"


def _cred_key_username(username: Optional[str]) -> Optional[str]:
    """Normalize username for credential key lookup: None for local admin (matches storage)."""
    if not username or not str(username).strip():
        return None
    local_admin = (Config.get("local_admin_username") or "admin").strip().lower()
    if str(username).strip().lower() == local_admin:
        return None
    return str(username).strip()


# ---------------------------------------------------------------------------
#  Encrypted fallback file
# ---------------------------------------------------------------------------

def _get_fallback_path() -> Path:
    global _FALLBACK_PATH
    if _FALLBACK_PATH is None:
        _FALLBACK_PATH = Platform.data_dir() / "cloud_credentials.enc"
    return _FALLBACK_PATH


def _get_or_create_encryption_key() -> bytes:
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
        logger.warning("Failed to load cloud credential fallback: %s", e)
        return {}


def _save_fallback_data(data: Dict[str, str]) -> None:
    path = _get_fallback_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = _get_or_create_encryption_key()
    nonce = secrets.token_bytes(_NONCE_SIZE)
    aes = AESGCM(key)
    payload = json.dumps(data).encode("utf-8")
    ciphertext = aes.encrypt(nonce, payload, None)
    path.write_bytes(nonce + ciphertext)


# ---------------------------------------------------------------------------
#  Public API
# ---------------------------------------------------------------------------

def get_cloud_credentials(account_id: str, provider: str, username: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Retrieve stored credentials for a cloud account."""
    key_username = _cred_key_username(username)
    key = _credential_key(account_id, provider, key_username)
    if _keyring_available():
        try:
            import keyring
            value = keyring.get_password(SERVICE_NAME, key)
            if not value:
                return None
            return json.loads(value)
        except Exception as e:
            logger.debug("Keyring get failed for cloud %s: %s", _mask(key), e)
            return None
    data = _load_fallback_data()
    raw = data.get(key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def set_cloud_oauth_tokens(
    account_id: str,
    provider: str,
    access_token: str,
    refresh_token: str,
    expires_at: Optional[float] = None,
    username: Optional[str] = None,
) -> None:
    """Store OAuth tokens for a cloud account."""
    key = _credential_key(account_id, provider, username)
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
            logger.warning("Keyring set failed for cloud, using fallback: %s", e)
    data = _load_fallback_data()
    data[key] = value
    _save_fallback_data(data)


def set_cloud_webdav_credentials(
    account_id: str,
    url: str,
    webdav_username: str,
    password: str,
    username: Optional[str] = None,
) -> None:
    """Store WebDAV credentials (Nextcloud app password)."""
    key = _credential_key(account_id, "nextcloud", username)
    value = json.dumps({
        "url": url,
        "webdav_username": webdav_username,
        "password": password,
        "type": "webdav",
    })
    if _keyring_available():
        try:
            import keyring
            keyring.set_password(SERVICE_NAME, key, value)
            return
        except Exception as e:
            logger.warning("Keyring set failed for cloud, using fallback: %s", e)
    data = _load_fallback_data()
    data[key] = value
    _save_fallback_data(data)


def delete_cloud_credentials(account_id: str, provider: str, username: Optional[str] = None) -> None:
    """Remove stored credentials for a cloud account."""
    key = _credential_key(account_id, provider, username)
    if _keyring_available():
        try:
            import keyring
            keyring.delete_password(SERVICE_NAME, key)
            return
        except Exception:
            pass
    data = _load_fallback_data()
    data.pop(key, None)
    _save_fallback_data(data)


def _mask(s: str) -> str:
    if len(s) <= 12:
        return "***"
    return s[:8] + "***"
