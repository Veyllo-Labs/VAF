"""
Secure storage for GitHub OAuth tokens.

Uses OS keyring (Windows Credential Manager, macOS Keychain, Linux Secret Service)
with fallback to an AES-256-GCM encrypted file under Platform.data_dir().
No tokens are stored in config.json. Per-user isolation via user_scope_id/username.
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

logger = logging.getLogger("vaf.github.credential")

SERVICE_NAME = "vaf-github"
_KEYRING_AVAILABLE: Optional[bool] = None
_KEYRING_LOCK = threading.Lock()
_FALLBACK_PATH: Optional[Path] = None
_CREDENTIALS_KEY = "github_credentials_key"
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
                keyring.set_password(SERVICE_NAME, "__vaf_probe__", "x")
                keyring.get_password(SERVICE_NAME, "__vaf_probe__")
                keyring.delete_password(SERVICE_NAME, "__vaf_probe__")
                _KEYRING_AVAILABLE = True
            except Exception as e:
                logger.info("Keyring unavailable for GitHub, using encrypted file: %s", e)
                _KEYRING_AVAILABLE = False
        return _KEYRING_AVAILABLE


def _local_admin_scope_id() -> str:
    return get_local_admin_scope_id()


def _credential_key(
    account_id: str,
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> str:
    """Build keyring/fallback key for a GitHub account. Scoped by user_scope_id or username."""
    safe_id = (account_id or "").strip().lower().replace(" ", "_")
    if user_scope_id and str(user_scope_id).strip():
        scope_str = str(user_scope_id).strip()
        if scope_str == _local_admin_scope_id():
            return f"github:default:{safe_id}"
        return f"github:{scope_str}:{safe_id}"
    if username and str(username).strip():
        safe_user = str(username).strip().lower().replace(" ", "_")
        return f"github:user:{safe_user}:{safe_id}"
    return f"github:default:{safe_id}"


def _cred_key_username(username: Optional[str]) -> Optional[str]:
    """Normalize username for credential key: None for local admin."""
    if not username or not str(username).strip():
        return None
    local_admin = get_local_admin_username().lower()
    if str(username).strip().lower() == local_admin:
        return None
    return str(username).strip()


def _cred_key_scope(user_scope_id: Optional[str]) -> Optional[str]:
    """Normalize user_scope_id for credential key: None for local admin scope."""
    if not user_scope_id or not str(user_scope_id).strip():
        return None
    if str(user_scope_id).strip() == _local_admin_scope_id():
        return None
    return str(user_scope_id).strip()


def _get_fallback_path() -> Path:
    global _FALLBACK_PATH
    if _FALLBACK_PATH is None:
        _FALLBACK_PATH = Platform.data_dir() / "github_credentials.enc"
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
    """Load and decrypt fallback file."""
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
        logger.warning("Failed to load GitHub credential fallback: %s", e)
        return {}


def _save_fallback_data(data: Dict[str, str]) -> None:
    """Encrypt and write fallback file."""
    path = _get_fallback_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = _get_or_create_encryption_key()
    nonce = secrets.token_bytes(_NONCE_SIZE)
    aes = AESGCM(key)
    payload = json.dumps(data).encode("utf-8")
    ciphertext = aes.encrypt(nonce, payload, None)
    path.write_bytes(nonce + ciphertext)


def get_github_oauth_token(
    account_id: str,
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> Optional[str]:
    """
    Retrieve GitHub access token for an account.
    Returns None if not found or on error.

    Strict lookup:
    - Non-admin context: scoped key only (no cross-user fallback).
    - Local admin context: scoped/default key with legacy compatibility.
    """
    primary_key = _credential_key(
        account_id,
        _cred_key_username(username),
        user_scope_id=_cred_key_scope(user_scope_id) or user_scope_id,
    )
    keys_to_try = [primary_key]
    is_local_admin_context = (_cred_key_username(username) is None and _cred_key_scope(user_scope_id) is None)
    if is_local_admin_context:
        safe_id = (account_id or "").strip().lower().replace(" ", "_")
        fallback_key = f"github:default:{safe_id}"
        if fallback_key != primary_key:
            keys_to_try.append(fallback_key)

    if _keyring_available():
        import keyring
        for key in keys_to_try:
            try:
                value = keyring.get_password(SERVICE_NAME, key)
                if not value:
                    continue
                data = json.loads(value)
                if data.get("type") == "oauth" and data.get("access_token"):
                    return data["access_token"]
            except Exception as e:
                logger.debug("Keyring get failed for GitHub %s: %s", key[:20] + "***", e)
        return None

    data = _load_fallback_data()
    for key in keys_to_try:
        raw = data.get(key)
        if not raw:
            continue
        try:
            obj = json.loads(raw)
            if obj.get("type") == "oauth" and obj.get("access_token"):
                return obj["access_token"]
        except Exception:
            pass
    return None


def get_github_credentials(
    account_id: str,
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Retrieve full stored credentials for a GitHub account.
    Returns dict with access_token, optional refresh_token, type; or None.
    """
    key = _credential_key(
        account_id,
        _cred_key_username(username),
        user_scope_id=_cred_key_scope(user_scope_id) or user_scope_id,
    )
    if _keyring_available():
        try:
            import keyring
            value = keyring.get_password(SERVICE_NAME, key)
            if not value:
                return None
            return json.loads(value)
        except Exception as e:
            logger.debug("Keyring get failed for GitHub: %s", e)
            return None
    data = _load_fallback_data()
    raw = data.get(key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def set_github_oauth_tokens(
    account_id: str,
    access_token: str,
    refresh_token: Optional[str] = None,
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> None:
    """Store GitHub OAuth tokens. GitHub tokens are long-lived; refresh_token optional."""
    key = _credential_key(
        account_id,
        _cred_key_username(username),
        user_scope_id=_cred_key_scope(user_scope_id) or user_scope_id,
    )
    value = json.dumps({
        "access_token": access_token,
        "refresh_token": refresh_token or "",
        "type": "oauth",
    })
    if _keyring_available():
        try:
            import keyring
            keyring.set_password(SERVICE_NAME, key, value)
            return
        except Exception as e:
            logger.warning("Keyring set failed for GitHub, using fallback: %s", e)
    data = _load_fallback_data()
    data[key] = value
    _save_fallback_data(data)


def delete_github_credentials(
    account_id: str,
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> None:
    """Remove stored credentials for a GitHub account."""
    key = _credential_key(
        account_id,
        _cred_key_username(username),
        user_scope_id=_cred_key_scope(user_scope_id) or user_scope_id,
    )
    if _keyring_available():
        try:
            import keyring
            keyring.delete_password(SERVICE_NAME, key)
            return
        except Exception as e:
            logger.warning("Keyring delete failed for GitHub: %s", e)
    data = _load_fallback_data()
    data.pop(key, None)
    _save_fallback_data(data)
