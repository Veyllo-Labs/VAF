# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Auth crypto: password hashing (Argon2), JWT, TOTP encryption.

Uses local_network_jwt_secret from config; TOTP secrets encrypted with AES-256-GCM.
"""

import asyncio
import hashlib
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
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
    """The JWT signing secret, from the data keyring.

    The legacy config.json value is adopted UNCHANGED: TOTP secrets are
    encrypted under a key derived from this very string, so re-minting it
    would silently invalidate every stored second factor. The old path did
    exactly that whenever a degraded config read returned "" - the keyring
    refuses to mint over unreadable state instead.
    """
    from vaf.core.data_keyring import get_data_secret
    return get_data_secret(
        "local_network_jwt_secret", legacy_config_key="local_network_jwt_secret"
    )


# Public alias used by web_server.py WebSocket handler
get_jwt_secret = _get_jwt_secret


def _get_totp_key() -> bytes:
    """Derive 32-byte key for TOTP encryption from JWT secret."""
    return hashlib.sha256(_get_jwt_secret().encode()).digest()


def hash_password(password: str) -> str:
    """Hash password with Argon2id. Blocking (~25 ms, 64 MiB): from async code use
    `hash_password_async`."""
    return _ph.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Verify password against Argon2 hash. Returns True if match. Blocking: from async code
    use `verify_password_async`."""
    try:
        _ph.verify(password_hash, password)
        return True
    except VerifyMismatchError:
        return False
    except Exception:
        return False


# Argon2 off the event loop. Each check takes ~25 ms of CPU and 64 MiB, and it ran on the loop
# that also carries every chat stream and socket: 40 concurrent wrong logins delayed an
# unrelated request by about a second (measured, idle 1 ms). A pool of its own, two workers:
# memory stays bounded (2 x 64 MiB) however many logins arrive, and hashes that wait never
# occupy the default executor that other work (the room handshake) runs on.
_ARGON2_WORKERS = 2
_pool = None
_pool_lock = threading.Lock()
# What a name without an account is verified against, so it costs exactly what a known name
# costs. A fixed hash with _ph's parameters (a test pins that they match) of a random password
# nobody kept: made lazily, the first unknown name paid for a hash AND a verification.
_DUMMY_HASH = "$argon2id$v=19$m=65536,t=2,p=4$759DChOqZfhyd10WaiEu4g$6jwk2BU3gxe8fK8FXHd9a4Ge5tOV0jpEjnzZS794Nbs"


def _argon2_pool() -> ThreadPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=_ARGON2_WORKERS, thread_name_prefix="vaf-argon2")
        return _pool


def _verify_or_dummy(password_hash: Optional[str], password: str) -> bool:
    """One Argon2 verification either way: against the account's hash, or, for a name that
    has no account, against a hash made once with the same parameters (always False). An
    unknown name used to answer before any hashing (measured: 3.5 ms against 28.6 ms for a
    known one), which listed the account names to anyone who timed the answer."""
    if password_hash is None:
        verify_password(_DUMMY_HASH, password)
        return False
    return verify_password(password_hash, password)


async def verify_password_async(password_hash: Optional[str], password: str) -> bool:
    """`verify_password` on the Argon2 pool. `password_hash=None` (no such account) does the
    same work against a dummy hash and returns False."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_argon2_pool(), _verify_or_dummy, password_hash, password)


async def hash_password_async(password: str) -> str:
    """`hash_password` on the Argon2 pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_argon2_pool(), hash_password, password)


def password_fingerprint(password_hash: Optional[str]) -> str:
    """A short digest of an account's stored password hash. A token from before the second
    factor carries it (`pwv`), so the 2FA steps can refuse one minted before the password
    changed, without the hash itself in a JWT anyone can read."""
    return hashlib.sha256(str(password_hash or "").encode("utf-8")).hexdigest()[:16]


def create_access_token(
    user_id: str,
    username: str,
    role: str,
    user_scope_id: str,
    expires_hours: Optional[float] = None,
    is_2fa_verified: bool = False,
    password_version: Optional[str] = None,
) -> str:
    """Create JWT access token. `password_version` (`password_fingerprint`) is set on a token
    from before the second factor, binding it to the password it was minted with."""
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
        # Unique per mint: two sign-ins of one account in the same second minted byte-identical
        # tokens, and the second session row failed on the unique token hash (a double-clicked
        # code answered 500). It also makes every token its own session to end.
        "jti": secrets.token_hex(16),
    }
    if password_version:
        payload["pwv"] = password_version
    return jwt.encode(
        payload,
        _get_jwt_secret(),
        algorithm="HS256",
    )


def second_factor_done(payload: dict[str, Any]) -> bool:
    """Whether an ACCESS token may open anything beyond the 2FA endpoints.

    `/login` answers a correct password with a full access token minted BEFORE the
    second factor (`is_2fa_verified: False`, the `temp_token`); only `/setup-2fa` and
    `/verify-2fa` may take it, and they check it themselves. Every other reader of a
    token asks here, so a password alone never opens the API.

    There is no switch. Every account is created needing a second factor
    (vaf/auth/user_admin.py) and `/login` asks for it by the account's own state, so a
    setting that made a token from before the code count as done could only ever open
    the API to a password alone. The configuration key that did it is gone, and a value
    an older install left in config.json is ignored.
    """
    return payload.get("is_2fa_verified") is True


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

