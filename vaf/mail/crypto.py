# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""At-rest encryption for mail store blobs (decision E4, EMAIL_CLIENT.md).

Raw RFC 822 bodies in mail.db are AES-256-GCM encrypted with a per-install key
(config: mail_store_encryption_key, auto-generated, PROTECTED + redacted).
Known limitation, documented on purpose: the key is shared across users of the
install (same model as the memory-RAG chunk encryption); per-user keys are a
future hardening. Envelope/snippet columns and the FTS index remain plaintext -
an FTS index inherently contains the indexed vocabulary.
"""
import base64
import os
import threading
from typing import Optional

_MAGIC = b"VMC1"  # VAF mail crypto v1: MAGIC + 12-byte nonce + GCM ciphertext
_lock = threading.Lock()
_cached_key: Optional[bytes] = None


def _load_or_create_key() -> bytes:
    """The 32-byte store key, from the data keyring.

    A legacy config.json value is adopted byte-identically (existing VMC1 blobs
    must keep decrypting), then the config copy is blanked. This also closes a
    gap the config path had: an unreadable config.json used to degrade to
    DEFAULTS and silently mint a NEW key over a store full of the old one; the
    keyring refuses instead. The cross-process race is handled inside the ring
    (setdefault under the store's file lock).
    """
    global _cached_key
    with _lock:
        if _cached_key is not None:
            return _cached_key
        from vaf.core.data_keyring import get_data_key
        _cached_key = get_data_key(
            "mail_store_encryption_key", legacy_config_key="mail_store_encryption_key"
        )
        return _cached_key


def encrypt_blob(data: bytes) -> bytes:
    """AES-256-GCM encrypt. Output: MAGIC + nonce + ciphertext(+tag)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = _load_or_create_key()
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, data, _MAGIC)
    return _MAGIC + nonce + ct


def decrypt_blob(blob: bytes) -> bytes:
    """Inverse of encrypt_blob. Raises ValueError on wrong format/key."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not blob.startswith(_MAGIC):
        raise ValueError("not a VAF mail crypto blob")
    key = _load_or_create_key()
    nonce, ct = blob[4:16], blob[16:]
    try:
        return AESGCM(key).decrypt(nonce, ct, _MAGIC)
    except Exception as e:
        raise ValueError(f"mail blob decryption failed: {e}") from e
