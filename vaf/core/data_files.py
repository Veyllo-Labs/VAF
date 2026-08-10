# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Encrypted-at-rest JSON and text files: one writer, one reader, one key.

Five stores held the user's conversations in plaintext on disk - chat sessions,
context archives, handoff bundles, the sub-agent queue and the per-session
working memory - and each had hand-rolled its own write path (three different
temp-and-rename dances, one that was not atomic at all, none of them setting a
file mode). This module is the single seam they now share.

## What it protects, stated honestly

The key is machine-held (see `vaf.core.data_keyring`), because the agent has to
keep working after a reboot without anyone typing a password. So this is not a
defence against someone who is already running code as you. It IS a defence
against everything that moves the FILES without the key: a stolen or discarded
disk whose keyring sits elsewhere, a backup or a cloud sync, a support archive,
a copied directory, another local account.

## Optional, and readable both ways

`file_encryption_enabled` (default true) decides what NEW writes look like.
Reading never depends on it: a file without the magic prefix is plaintext and
is returned as-is. That gives three properties the product and embedders need:

- chats written before this existed keep opening, forever;
- turning the setting off writes plaintext again, and the encrypted files
  already on disk still open (the key stays in the ring);
- an embedder chooses per deployment - encrypt the end user's chats, or don't,
  because their own storage layer already does it.

Format: `VAFENC1:` ‖ 12-byte nonce ‖ AES-256-GCM ciphertext. The container is
the one `vaf/memory/crypto.py` already ships for the profile cache, deliberately
reused rather than re-invented; the key differs, so the two lanes stay separable.
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
from pathlib import Path
from typing import Any, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from vaf.core.config import Config
from vaf.core.secure_store import _atomic_write_bytes, harden_dir, harden_path

logger = logging.getLogger("vaf.core.data_files")

# Same container as the memory profile cache. Pinned in tests/test_persisted_format_tags.py.
FILE_MAGIC = b"VAFENC1:"
_NONCE_SIZE = 12
_KEY_NAME = "file_store_encryption_key"

_key_cache: Optional[bytes] = None
_key_lock = threading.Lock()


def _key() -> bytes:
    """The file-store key. Refuses to mint one while ciphertext exists.

    A missing ring entry means "fresh install" only if there is nothing on disk
    that was written under an older key. If encrypted files ARE there and the
    entry is gone (a restored backup without its keyring, a deleted
    data_keys.enc), minting would answer every read with garbage and every write
    would then overwrite the still-recoverable original. So that combination is
    a hard error, not a warning in a log nobody reads.
    """
    global _key_cache
    with _key_lock:
        if _key_cache is None:
            from vaf.core.data_keyring import get_data_key, peek_data_secret, ring_exists
            if ring_exists() and not peek_data_secret(_KEY_NAME) and _encrypted_files_exist():
                # A ring that holds other keys but lost THIS one is corruption, not
                # a fresh install: minting here would answer every existing file
                # with garbage and the next write would overwrite the original.
                # (A ring that is missing entirely cannot be told apart from a
                # first run; that case is caught on READ, where decrypt_bytes
                # raises "intact but locked" instead of returning an empty chat.)
                raise RuntimeError(
                    "Encrypted files exist but the file-store key is missing from the "
                    "keyring. Restore data_keys.enc, its .key.json sibling and the "
                    "machine KEK together - minting a new key here would make the "
                    "existing data permanently unreadable."
                )
            _key_cache = get_data_key(_KEY_NAME)
        return _key_cache


def _encrypted_files_exist() -> bool:
    """Cheap probe: does any store already hold a VAFENC1 file?"""
    try:
        from vaf.core.platform import Platform
        root = Path(Platform.vaf_dir())
    except Exception:
        return False
    for pattern in ("sessions/*.json", "context_archive/*.json",
                    "handoff_bundles/*/*.json", "subagent_queue/*.json"):
        for path in root.glob(pattern):
            try:
                with open(path, "rb") as handle:
                    if handle.read(len(FILE_MAGIC)) == FILE_MAGIC:
                        return True
            except OSError:
                continue
    return False


def reset_key_cache() -> None:
    """Testing hook: forget the cached key so a patched keyring takes effect."""
    global _key_cache
    with _key_lock:
        _key_cache = None


def encryption_enabled() -> bool:
    """Whether NEW writes are encrypted. Reading tolerates both either way."""
    try:
        return bool(Config.get("file_encryption_enabled", True))
    except Exception:
        return True


def allow_plaintext_at_rest() -> bool:
    """Whether a file WITHOUT the magic prefix is still accepted on read.

    The tolerant reader is what lets chats written before encryption keep
    opening, and it has to exist during a migration. Left on forever it is a
    downgrade path: anyone who can write into the store replaces a record with
    plaintext, and the reader takes it - which defeats the AEAD by never
    presenting a ciphertext to authenticate. A truncated file is likewise
    indistinguishable from a plaintext one.

    So this is a THIRD state, not a second: write-encrypted while still reading
    both, then read-only-ciphertext once a full pass has found nothing plain
    left. The ordered states are the ones the AWS Database Encryption SDK
    defines, and the sweep flips this automatically after a clean pass.
    """
    try:
        return bool(Config.get("allow_plaintext_at_rest", True))
    except Exception:
        return True


def is_encrypted(data: bytes) -> bool:
    return bool(data) and data.startswith(FILE_MAGIC)


def encrypt_bytes(plaintext: bytes) -> bytes:
    nonce = secrets.token_bytes(_NONCE_SIZE)
    return FILE_MAGIC + nonce + AESGCM(_key()).encrypt(nonce, plaintext, None)


def decrypt_bytes(data: bytes) -> bytes:
    """Decrypt, or pass plaintext through unchanged.

    A file without the magic prefix predates encryption (or was written with the
    setting off) and is returned as it is - that tolerance is what lets old chats
    keep opening. A file WITH the prefix that fails to decrypt raises: silently
    answering "empty chat" would look exactly like data loss to the user, and
    would invite a save that overwrites the still-recoverable ciphertext.
    """
    if not data:
        return b""
    if not is_encrypted(data):
        if not allow_plaintext_at_rest():
            raise ValueError(
                "This store is enforced: a file without the encryption header is "
                "either a downgrade attempt or a truncated write, and is refused "
                "rather than read. Set allow_plaintext_at_rest to true if you "
                "deliberately keep plaintext records."
            )
        return data
    body = data[len(FILE_MAGIC):]
    nonce, ciphertext = body[:_NONCE_SIZE], body[_NONCE_SIZE:]
    try:
        return AESGCM(_key()).decrypt(nonce, ciphertext, None)
    except Exception as e:
        raise ValueError(
            "Encrypted file could not be decrypted with the current key. The data "
            "is intact but locked - restore the keyring (data_keys.enc + its "
            "wrapped DEK + the machine KEK) rather than deleting the file."
        ) from e


# --- file helpers ----------------------------------------------------------

def write_bytes_atomic(path, data: bytes, *, encrypt: Optional[bool] = None) -> None:
    """Encrypt (unless disabled), write atomically, then restrict to 0600."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    harden_dir(path.parent)
    use = encryption_enabled() if encrypt is None else encrypt
    _atomic_write_bytes(path, encrypt_bytes(data) if use else data)
    harden_path(path)


def read_bytes(path) -> bytes:
    """Read and decrypt; b"" when the file does not exist."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return b""
    return decrypt_bytes(raw)


def write_json_atomic(path, payload: Any, *, encrypt: Optional[bool] = None, **dumps_kwargs) -> None:
    dumps_kwargs.setdefault("ensure_ascii", False)
    write_bytes_atomic(path, json.dumps(payload, **dumps_kwargs).encode("utf-8"), encrypt=encrypt)


def read_json(path, default: Any = None) -> Any:
    raw = read_bytes(path)
    if not raw:
        return default
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as e:
        logger.warning("Unreadable json at %s: %s", path, e)
        return default


def write_text_atomic(path, text: str, *, encrypt: Optional[bool] = None) -> None:
    write_bytes_atomic(path, (text or "").encode("utf-8"), encrypt=encrypt)


def read_text(path, default: str = "") -> str:
    raw = read_bytes(path)
    if not raw:
        return default
    try:
        return raw.decode("utf-8")
    except Exception:
        return default
