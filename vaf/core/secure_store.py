"""
Encrypted fallback storage shared by the credential stores.

Used when the OS keyring is unavailable (headless servers, CI, minimal installs).
Provides envelope encryption:

  - A random per-file Data Encryption Key (DEK, 32 bytes) encrypts the payload
    with AES-256-GCM (same scheme as before).
  - The DEK is wrapped by a Key Encryption Key (KEK):
      * with a master passphrase (VAF_MASTER_PASSPHRASE env or set_passphrase()):
        KEK = scrypt(passphrase, salt) -> config.json holds no secret;
      * without a passphrase (default / headless): KEK is a random key persisted
        in config.json (secure_store_kek). config.json is chmod 0600, so this is
        equivalent to chmod-only protection, but allows a seamless upgrade to a
        passphrase later (only the small wrapped-DEK file is re-encrypted).

All on-disk artifacts (payload .enc, wrapped-DEK .key.json, config.json) are chmod
0600. Read-modify-write is serialized with a process-local threading.Lock plus a
cross-process filelock.FileLock to prevent lost updates between separate processes
(e.g. backend and CLI).
"""

import base64
import contextlib
import json
import logging
import os
import secrets
import tempfile
import threading
from pathlib import Path
from typing import Callable, Dict, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from vaf.core.config import Config

logger = logging.getLogger("vaf.core.secure_store")

PASSPHRASE_ENV = "VAF_MASTER_PASSPHRASE"
_CONFIG_KEK_NAME = "secure_store_kek"

_KEY_SIZE = 32
_NONCE_SIZE = 12
_SALT_SIZE = 16
# scrypt cost parameters (~32 MiB, well under cryptography's default memory limit)
_SCRYPT_N = 2 ** 15
_SCRYPT_R = 8
_SCRYPT_P = 1

_session_passphrase: Optional[str] = None
_pp_lock = threading.Lock()

# Lazily probed cross-process lock implementation.
_FILELOCK_CLS = None
_FILELOCK_PROBED = False

# Cached keyring-availability flag (availability is global, not per service).
_KEYRING_AVAILABLE: Optional[bool] = None
_KEYRING_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
#  Passphrase
# ---------------------------------------------------------------------------

def set_passphrase(passphrase: Optional[str]) -> None:
    """Set the master passphrase for this process (e.g. from the setup wizard).

    Pass None to clear it. After changing the passphrase, call SecureBlobStore.rewrap()
    on each store to re-wrap its DEK under the new key.
    """
    global _session_passphrase
    with _pp_lock:
        _session_passphrase = passphrase or None


def _get_passphrase() -> Optional[str]:
    with _pp_lock:
        if _session_passphrase:
            return _session_passphrase
    env = os.environ.get(PASSPHRASE_ENV)
    return env or None


# ---------------------------------------------------------------------------
#  Filesystem hardening
# ---------------------------------------------------------------------------

def harden_path(path) -> None:
    """Restrict a file to owner-only (0600). No-op on platforms without chmod."""
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass  # Windows may not support chmod


def harden_dir(path) -> None:
    """Restrict a directory to owner-only (0700). No-op where unsupported."""
    try:
        os.chmod(str(path), 0o700)
    except OSError:
        pass


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write bytes atomically: temp file (mode 0600 via mkstemp) + fsync + os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)  # atomic on POSIX and Windows
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
#  Keyring availability probe (shared by both credential modules)
# ---------------------------------------------------------------------------

def keyring_available() -> bool:
    """True if an OS keyring backend can be used (probed once, thread-safe)."""
    global _KEYRING_AVAILABLE
    with _KEYRING_LOCK:
        if _KEYRING_AVAILABLE is None:
            try:
                import keyring
                keyring.get_keyring()
                keyring.set_password("vaf", "__vaf_probe__", "x")
                keyring.get_password("vaf", "__vaf_probe__")
                keyring.delete_password("vaf", "__vaf_probe__")
                _KEYRING_AVAILABLE = True
            except Exception as e:
                logger.info("Keyring unavailable, using encrypted file fallback: %s", e)
                _KEYRING_AVAILABLE = False
        return _KEYRING_AVAILABLE


# ---------------------------------------------------------------------------
#  Cross-process lock
# ---------------------------------------------------------------------------

def _get_filelock_cls():
    global _FILELOCK_CLS, _FILELOCK_PROBED
    if not _FILELOCK_PROBED:
        _FILELOCK_PROBED = True
        try:
            from filelock import FileLock
            _FILELOCK_CLS = FileLock
        except Exception as e:  # pragma: no cover - only when dependency missing
            logger.warning("filelock unavailable; cross-process locking disabled: %s", e)
            _FILELOCK_CLS = None
    return _FILELOCK_CLS


# ---------------------------------------------------------------------------
#  KEK helpers
# ---------------------------------------------------------------------------

def _derive_kek_scrypt(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    kdf = Scrypt(salt=salt, length=_KEY_SIZE, n=n, r=r, p=p)
    return kdf.derive(passphrase.encode("utf-8"))


def _config_kek(create: bool = True) -> Optional[bytes]:
    """Random KEK persisted in config.json (used when no passphrase is set)."""
    encoded = Config.get(_CONFIG_KEK_NAME, "") or ""
    if encoded:
        try:
            k = base64.b64decode(encoded)
            if len(k) == _KEY_SIZE:
                return k
        except Exception:
            pass
    if not create:
        return None
    k = secrets.token_bytes(_KEY_SIZE)
    Config.set(_CONFIG_KEK_NAME, base64.b64encode(k).decode())
    return k


# ---------------------------------------------------------------------------
#  Secure blob store
# ---------------------------------------------------------------------------

class SecureBlobStore:
    """A single encrypted JSON blob on disk with envelope encryption and locking.

    The payload is a flat dict[str, str]. Mutate it through update() so the
    read-modify-write happens atomically under both the process-local and the
    cross-process lock.
    """

    def __init__(self, name: str, enc_path, legacy_key_config_name: Optional[str] = None):
        self.name = name
        self.enc_path = Path(enc_path)
        # email_credentials.enc -> email_credentials.key.json
        self.wrap_path = self.enc_path.with_name(self.enc_path.stem + ".key.json")
        self.lock_path = self.enc_path.with_name(self.enc_path.name + ".lock")
        self.legacy_key_config_name = legacy_key_config_name
        self._tlock = threading.Lock()
        self._dek_cache: Optional[bytes] = None

    # -- public API ---------------------------------------------------------

    def load(self) -> Dict[str, str]:
        """Return the decrypted blob (empty dict if missing or undecryptable)."""
        with self._tlock, self._file_lock():
            return self._load_locked()

    def update(self, mutator: Callable[[Dict[str, str]], None]) -> None:
        """Atomically load -> mutate -> save under both locks (no lost updates)."""
        with self._tlock, self._file_lock():
            data = self._load_locked()
            mutator(data)
            self._save_locked(data)

    def rewrap(self) -> bool:
        """Re-wrap the DEK under the current KEK (after a passphrase change).

        The store must already be unlocked under the previous key: load or write
        once with the old passphrase, then set_passphrase(new) and call rewrap()
        on the same instance. Uses the in-memory DEK so the old passphrase need
        not be supplied again. Returns False if the store cannot be opened.
        """
        with self._tlock, self._file_lock():
            dek = self._dek_cache
            if dek is None:
                if not self.wrap_path.exists():
                    return False
                dek = self._unwrap_dek()
            if dek is None:
                return False
            self._wrap_and_store_dek(dek)
            self._dek_cache = dek
            return True

    # -- locking ------------------------------------------------------------

    def _file_lock(self):
        cls = _get_filelock_cls()
        if cls is None:
            return contextlib.nullcontext()
        self.enc_path.parent.mkdir(parents=True, exist_ok=True)
        return cls(str(self.lock_path), timeout=15)

    # -- payload I/O (must be called while holding the locks) ---------------

    def _load_locked(self) -> Dict[str, str]:
        if not self.enc_path.exists():
            return {}
        try:
            raw = self.enc_path.read_bytes()
            if len(raw) < _NONCE_SIZE:
                return {}
            nonce, ciphertext = raw[:_NONCE_SIZE], raw[_NONCE_SIZE:]
            dek = self._get_dek(create=False)
            if dek is None:
                return {}
            decrypted = AESGCM(dek).decrypt(nonce, ciphertext, None).decode("utf-8")
            return json.loads(decrypted)
        except Exception as e:
            logger.warning("Failed to load secure store %s: %s", self.name, e)
            return {}

    def _save_locked(self, data: Dict[str, str]) -> None:
        self.enc_path.parent.mkdir(parents=True, exist_ok=True)
        harden_dir(self.enc_path.parent)
        dek = self._get_dek(create=True)
        nonce = secrets.token_bytes(_NONCE_SIZE)
        ciphertext = AESGCM(dek).encrypt(nonce, json.dumps(data).encode("utf-8"), None)
        _atomic_write_bytes(self.enc_path, nonce + ciphertext)
        harden_path(self.enc_path)

    # -- DEK / envelope -----------------------------------------------------

    def _get_dek(self, create: bool) -> Optional[bytes]:
        if self._dek_cache is not None:
            return self._dek_cache
        dek = self._resolve_dek(create=create)
        if dek is not None:
            self._dek_cache = dek
        return dek

    def _resolve_dek(self, create: bool) -> Optional[bytes]:
        # 1) Wrapped DEK file already exists.
        if self.wrap_path.exists():
            dek = self._unwrap_dek()
            if dek is not None:
                self._maybe_upgrade_wrap()
            return dek
        # 2) Legacy plaintext key in config.json -> adopt as DEK, then wrap it.
        if self.legacy_key_config_name:
            legacy = Config.get(self.legacy_key_config_name, "") or ""
            if legacy:
                try:
                    dek = base64.b64decode(legacy)
                except Exception:
                    dek = b""
                if len(dek) == _KEY_SIZE:
                    self._wrap_and_store_dek(dek)
                    try:
                        Config.set(self.legacy_key_config_name, "")
                    except Exception:
                        pass
                    logger.info("Migrated legacy %s to wrapped DEK", self.legacy_key_config_name)
                    return dek
        # 3) Fresh install.
        if create:
            dek = secrets.token_bytes(_KEY_SIZE)
            self._wrap_and_store_dek(dek)
            return dek
        return None

    def _wrap_and_store_dek(self, dek: bytes) -> None:
        passphrase = _get_passphrase()
        if passphrase:
            salt = secrets.token_bytes(_SALT_SIZE)
            kek = _derive_kek_scrypt(passphrase, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
            meta = {
                "kdf": "scrypt",
                "salt": base64.b64encode(salt).decode(),
                "n": _SCRYPT_N, "r": _SCRYPT_R, "p": _SCRYPT_P,
            }
        else:
            kek = _config_kek(create=True)
            meta = {"kdf": "raw"}
        nonce = secrets.token_bytes(_NONCE_SIZE)
        wrapped = AESGCM(kek).encrypt(nonce, dek, None)
        doc = {
            "v": 1,
            **meta,
            "nonce": base64.b64encode(nonce).decode(),
            "wrapped": base64.b64encode(wrapped).decode(),
        }
        self.wrap_path.parent.mkdir(parents=True, exist_ok=True)
        harden_dir(self.wrap_path.parent)
        _atomic_write_bytes(self.wrap_path, json.dumps(doc).encode("utf-8"))
        harden_path(self.wrap_path)

    def _unwrap_dek(self) -> Optional[bytes]:
        try:
            doc = json.loads(self.wrap_path.read_text("utf-8"))
        except Exception as e:
            logger.warning("Cannot read wrap file for %s: %s", self.name, e)
            return None
        try:
            kdf = doc.get("kdf")
            if kdf == "scrypt":
                passphrase = _get_passphrase()
                if not passphrase:
                    logger.warning(
                        "Secure store %s is wrapped with a passphrase, but none is set "
                        "(%s)", self.name, PASSPHRASE_ENV,
                    )
                    return None
                salt = base64.b64decode(doc["salt"])
                kek = _derive_kek_scrypt(
                    passphrase, salt,
                    int(doc.get("n", _SCRYPT_N)), int(doc.get("r", _SCRYPT_R)),
                    int(doc.get("p", _SCRYPT_P)),
                )
            elif kdf == "raw":
                kek = _config_kek(create=False)
                if kek is None:
                    logger.warning("Secure store %s: raw KEK missing from config", self.name)
                    return None
            else:
                logger.warning("Secure store %s: unknown kdf %r", self.name, kdf)
                return None
            nonce = base64.b64decode(doc["nonce"])
            wrapped = base64.b64decode(doc["wrapped"])
            return AESGCM(kek).decrypt(nonce, wrapped, None)
        except Exception as e:
            logger.warning(
                "Failed to unwrap DEK for %s (wrong passphrase or corrupt file?): %s",
                self.name, e,
            )
            return None

    def _maybe_upgrade_wrap(self) -> None:
        """If the DEK is wrapped with the config KEK but a passphrase is now set,
        upgrade transparently to passphrase-derived wrapping."""
        try:
            doc = json.loads(self.wrap_path.read_text("utf-8"))
        except Exception:
            return
        if doc.get("kdf") == "raw" and _get_passphrase():
            dek = self._unwrap_dek()
            if dek is not None:
                self._wrap_and_store_dek(dek)
                logger.info("Upgraded secure store %s to passphrase-derived KEK", self.name)
