# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Auth crypto: password hashing (Argon2), JWT, TOTP encryption.

Uses local_network_jwt_secret from config; TOTP secrets encrypted with AES-256-GCM.
"""

import hashlib
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional, Any

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
import jwt
import pyotp
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from vaf.core.config import Config

# Argon2id for password hashing
_ph = PasswordHasher(time_cost=2, memory_cost=65536)

# AES-GCM for TOTP secret at rest
_NONCE_SIZE = 12


def _get_jwt_secret() -> str:
    """Get or create JWT secret from config."""
    secret = Config.get("local_network_jwt_secret", "")
    if not secret or len(secret) < 32:
        secret = secrets.token_urlsafe(32)
        Config.set("local_network_jwt_secret", secret)
    return secret


# Public alias used by web_server.py WebSocket handler
get_jwt_secret = _get_jwt_secret


def _get_totp_key() -> bytes:
    """Derive 32-byte key for TOTP encryption from JWT secret."""
    return hashlib.sha256(_get_jwt_secret().encode()).digest()


def hash_password(password: str) -> str:
    """Hash password with Argon2id."""
    return _ph.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Verify password against Argon2 hash. Returns True if match."""
    try:
        _ph.verify(password_hash, password)
        return True
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def create_access_token(
    user_id: str,
    username: str,
    role: str,
    user_scope_id: str,
    expires_hours: Optional[float] = None,
    is_2fa_verified: bool = False,
) -> str:
    """Create JWT access token."""
    if expires_hours is None:
        expires_hours = float(Config.get("local_network_jwt_expiry_hours", 24))
    now = datetime.now(timezone.utc)
    exp = now + timedelta(hours=expires_hours)
    payload = {
        "sub": user_id,
        "username": username,
        "role": role,
        "user_scope_id": user_scope_id,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "type": "access",
        "is_2fa_verified": is_2fa_verified,
    }
    return jwt.encode(
        payload,
        _get_jwt_secret(),
        algorithm="HS256",
    )


def create_refresh_token(user_id: str, expires_days: int = 7) -> str:
    """Create JWT refresh token."""
    now = datetime.now(timezone.utc)
    exp = now + timedelta(days=expires_days)
    payload = {
        "sub": user_id,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "type": "refresh",
    }
    return jwt.encode(
        payload,
        _get_jwt_secret(),
        algorithm="HS256",
    )


def decode_token(token: str) -> Optional[dict[str, Any]]:
    """Decode and validate JWT; returns payload or None."""
    try:
        return jwt.decode(
            token,
            _get_jwt_secret(),
            algorithms=["HS256"],
        )
    except Exception:
        return None


def token_hash_for_db(token: str) -> str:
    """SHA-256 of JWT for session lookup (no plaintext in DB)."""
    return hashlib.sha256(token.encode()).hexdigest()


def encrypt_totp_secret(plaintext: str) -> tuple[bytes, bytes]:
    """Encrypt TOTP secret with AES-256-GCM. Returns (ciphertext, nonce)."""
    key = _get_totp_key()
    nonce = secrets.token_bytes(_NONCE_SIZE)
    aes = AESGCM(key)
    ct = aes.encrypt(nonce, plaintext.encode("utf-8"), None)
    return ct, nonce


def decrypt_totp_secret(ciphertext: bytes, nonce: bytes) -> str:
    """Decrypt TOTP secret."""
    key = _get_totp_key()
    aes = AESGCM(key)
    return aes.decrypt(nonce, ciphertext, None).decode("utf-8")


def generate_totp_secret() -> str:
    """Generate a new TOTP secret (base32)."""
    return pyotp.random_base32()


def get_totp_uri(secret: str, username: str, issuer: str = "VAF") -> str:
    """Provisioning URI for authenticator apps."""
    return pyotp.totp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)


def verify_totp(secret: str, code: str, window: int = 1) -> bool:
    """Verify 6-digit TOTP code. window=1 allows ±30s."""
    try:
        totp = pyotp.TOTP(secret)
        return totp.verify(code, valid_window=window)
    except Exception:
        return False
