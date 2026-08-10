# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
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
    # No .json suffix on the temp file: the session, archive and migration globs
    # match "*.json", and a crashed write would otherwise leave something they
    # try to parse as a record.
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".part")
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

class SecureStoreUnreadable(RuntimeError):
    """A payload exists on disk and could not be decrypted.

    Distinct from "there is nothing stored", which is not an error. The distinction only
    matters to callers that would otherwise fall back to a weaker copy - see `load_strict`.
    """


def _derive_kek_scrypt(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    kdf = Scrypt(salt=salt, length=_KEY_SIZE, n=n, r=r, p=p)
    return kdf.derive(passphrase.encode("utf-8"))


def _kek_file_path() -> Path:
    return Path(Config.APP_DIR) / "secure_store.kek"


def _kek_marker_path() -> Path:
    return Path(Config.APP_DIR) / "secure_store.kek.where"


PRE_MIGRATION_BACKUP = "config.json.pre-keyring.bak"


def ensure_pre_migration_backup() -> None:
    """One config.json snapshot BEFORE the first secret is blanked out of it.

    Rolling back to an older VAF after the keys moved out of config.json would
    find them missing and MINT replacements - which orphans every encrypted
    memory row and mail body. This backup is the documented way back: restore
    it and the old release reads its keys where it always did. `vaf memory
    rekey` reads exactly this kind of file. Written once, owner-only, bytes
    verbatim (never text mode: CRLF configs must survive byte-identical).

    IT IS A PLAINTEXT COPY OF EVERY KEY, sitting next to the data those keys
    open, so it cancels the protection for as long as it exists. That is a
    deliberate trade for the migration window only: `drop_pre_migration_backup`
    removes it once every key has moved, and `vaf secure status` names it while
    it is still there.
    """
    try:
        src = Path(Config.CONFIG_FILE)
        dst = Path(Config.APP_DIR) / PRE_MIGRATION_BACKUP
        if dst.exists() or not src.exists():
            return
        _atomic_write_bytes(dst, src.read_bytes())
        harden_path(dst)
        logger.info("Wrote pre-keyring config backup: %s", dst)
    except Exception as e:
        logger.warning("Could not write pre-keyring config backup: %s", e)


def pre_migration_backup_path() -> Path:
    return Path(Config.APP_DIR) / PRE_MIGRATION_BACKUP


def drop_pre_migration_backup() -> bool:
    """Delete the plaintext rollback copy once it has nothing left to roll back to.

    Only when config.json genuinely carries no key material any more - while a
    single key is still mid-migration the backup is the way back and must stay.
    """
    path = pre_migration_backup_path()
    if not path.exists():
        return False
    leftovers = [k for k in ("secure_store_kek", "memory_encryption_key",
                             "mail_store_encryption_key", "github_credentials_key",
                             "local_network_jwt_secret")
                 if str(Config.get(k, "") or "").strip()]
    if leftovers:
        return False
    # An empty config.json is not proof that the move SUCCEEDED - during the
    # first real restart it was briefly true while the keyring could not be
    # written at all, and deleting the rollback copy in that window would have
    # left no way back. The ring has to open and actually hold the keys.
    try:
        from vaf.core.data_keyring import _ring
        stored = _ring().load_strict()
    except Exception:
        return False
    if not {"memory_encryption_key", "mail_store_encryption_key"} & set(stored):
        return False
    try:
        path.unlink()
        logger.info("Removed the plaintext pre-keyring config backup; every key has moved.")
        return True
    except OSError:
        return False


def _read_kek_marker() -> str:
    try:
        return _kek_marker_path().read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _preferred_kek_backend() -> str:
    """Where a NEW KEK goes. File by default, and that is a correction.

    The OS keyring is the stronger place - it is encrypted with the user's login
    password, which is what protects a powered-off stolen machine. It is also
    unreachable from the process that actually runs VAF on a normal install: the
    tray is started by a supervisor script with no session bus, so a KEK written
    during an interactive command could not be read by the service afterwards.
    Measured on a real restart: 295 failed key resolutions and a locked keyring,
    with the app only recovering once the file backend took over.

    So the default is the file (0600 under APP_DIR, protected by whatever disk
    encryption sits beneath it), and the keyring is opt-in for installs that
    start VAF from inside the desktop session. Reading still finds a KEK
    wherever an earlier version put it.
    """
    try:
        choice = str(Config.get("secure_store_kek_backend", "file") or "file").strip().lower()
    except Exception:
        choice = "file"
    return choice if choice in ("file", "keyring") else "file"


def _write_kek(k: bytes) -> str:
    """Persist the KEK into the configured backend; return which one was used."""
    encoded = base64.b64encode(k).decode()
    backend = "file"
    if _preferred_kek_backend() == "keyring" and keyring_available():
        try:
            import keyring as _kr
            _kr.set_password("vaf", _CONFIG_KEK_NAME, encoded)
            backend = "keyring"
        except Exception as e:
            logger.warning("Could not store KEK in OS keyring, using file: %s", e)
    if backend == "file":
        _atomic_write_bytes(_kek_file_path(), encoded.encode("utf-8"))
        harden_path(_kek_file_path())
    _atomic_write_bytes(_kek_marker_path(), backend.encode("utf-8"))
    harden_path(_kek_marker_path())
    return backend


def _kek_from_keyring() -> Optional[bytes]:
    try:
        import keyring as _kr
        encoded = _kr.get_password("vaf", _CONFIG_KEK_NAME) or ""
        k = base64.b64decode(encoded) if encoded else b""
        return k if len(k) == _KEY_SIZE else None
    except Exception:
        return None


def _kek_from_file() -> Optional[bytes]:
    try:
        encoded = _kek_file_path().read_text(encoding="utf-8").strip()
        k = base64.b64decode(encoded) if encoded else b""
        return k if len(k) == _KEY_SIZE else None
    except Exception:
        return None


def kek_backend() -> str:
    """Where the machine KEK lives: 'keyring', 'file', 'config' (legacy) or 'none'."""
    marker = _read_kek_marker()
    if marker in ("keyring", "file"):
        return marker
    if _kek_file_path().exists():
        return "file"
    if Config.get(_CONFIG_KEK_NAME, ""):
        return "config"
    return "none"


def _legacy_config_kek() -> Optional[bytes]:
    """The KEK still in config.json, read STRICTLY. None = genuinely absent.

    `Config.load()` degrades an unreadable config.json to DEFAULTS, so the
    ordinary read answers "" for a key that IS on disk. Minting on that answer
    would shadow every DEK ever wrapped under the real one, so an unparseable
    file raises rather than looking empty - the same refusal the memory key has
    carried since the incident that taught it.
    """
    encoded = str(Config.get(_CONFIG_KEK_NAME, "") or "").strip()
    if not encoded:
        cfg_path = Path(Config.CONFIG_FILE)
        if cfg_path.exists():
            try:
                raw = json.loads(cfg_path.read_bytes().decode("utf-8"))
            except Exception as e:
                raise SecureStoreUnreadable(
                    "config.json exists but could not be parsed while resolving the "
                    "secure-store KEK. Refusing to mint a replacement - that would "
                    "orphan every encrypted store. Repair config.json, or restore "
                    "config.json.pre-keyring.bak."
                ) from e
            if isinstance(raw, dict):
                encoded = str(raw.get(_CONFIG_KEK_NAME) or "").strip()
    if not encoded:
        return None
    try:
        k = base64.b64decode(encoded)
    except Exception as e:
        raise SecureStoreUnreadable(
            f"{_CONFIG_KEK_NAME} is set but is not valid base64. Refusing to mint a "
            f"replacement, which would orphan every encrypted store."
        ) from e
    if len(k) != _KEY_SIZE:
        raise SecureStoreUnreadable(
            f"{_CONFIG_KEK_NAME} decodes to {len(k)} bytes, expected {_KEY_SIZE}. "
            f"Refusing to mint a replacement."
        )
    return k


def _machine_kek(create: bool = True) -> Optional[bytes]:
    """The machine KEK: OS keyring or 0600 file - no longer config.json.

    THE RULE, and every branch below exists to keep it: never mint while key
    material for this installation might still exist. A fresh KEK does not fail
    loudly, it silently shadows every wrapped DEK - credentials, mail, memory
    and now the data keyring itself - so the resolution order is strict and the
    default answer to "I cannot tell" is None, not a new key.

    Order:
      1. the backend the marker names (missing there = hard refusal, not a mint);
      2. no marker: file, then keyring, then the legacy config.json value;
      3. only with nothing found anywhere AND nothing to shadow: mint.

    The legacy value is adopted byte-identically - the existing .key.json wraps
    have to keep opening - and the config copy is blanked only after the
    one-time backup.
    """
    marker = _read_kek_marker()

    if marker == "keyring":
        k = _kek_from_keyring()
        if k is not None:
            return k
        k = _kek_from_file()
        if k is not None:
            logger.warning(
                "The OS keyring is not reachable from this process; using the "
                "KEK file copy instead. Set secure_store_kek_backend to 'file' "
                "if this machine starts VAF outside the desktop session."
            )
            return k
        logger.error(
            "The secure-store KEK lives in the OS keyring but the keyring is not "
            "reachable from this process. Credentials and encrypted stores stay "
            "locked until VAF runs inside the desktop session again (or "
            "%s is set). Refusing to mint a replacement key.", PASSPHRASE_ENV,
        )
        return None

    if marker == "file":
        k = _kek_from_file()
        if k is not None:
            return k
        logger.error(
            "The secure-store KEK file (%s) is missing or unreadable. Every "
            "encrypted store stays locked until it is restored. Refusing to mint "
            "a replacement key.", _kek_file_path(),
        )
        return None

    # No marker yet: this is a fresh install or one that has not migrated. Look
    # in every place a KEK could be, FILE FIRST - a keyring entry from another
    # VAF installation (or a stale probe) must never win over key material that
    # belongs to this one.
    k = _kek_from_file()
    if k is not None:
        _atomic_write_bytes(_kek_marker_path(), b"file")
        harden_path(_kek_marker_path())
        return k

    # The legacy config value outranks the OS keyring for the same reason: the
    # wrapped DEKs on this disk were made with it.
    legacy = _legacy_config_kek()
    if legacy is not None:
        ensure_pre_migration_backup()
        _write_kek(legacy)
        try:
            Config.set(_CONFIG_KEK_NAME, "")
        except Exception:
            pass
        logger.info("Moved secure-store KEK out of config.json (backend: %s)", kek_backend())
        return legacy

    # The OS keyring is consulted last and only when it is the configured
    # backend: a keyring entry left by another installation must never outrank
    # the key material that belongs to THIS one.
    if _preferred_kek_backend() == "keyring" and keyring_available():
        k = _kek_from_keyring()
        if k is not None:
            _atomic_write_bytes(_kek_marker_path(), b"keyring")
            harden_path(_kek_marker_path())
            return k

    if not create:
        return None
    k = secrets.token_bytes(_KEY_SIZE)
    backend = _write_kek(k)
    logger.info("Minted machine KEK (backend: %s)", backend)
    return k


def _config_kek(create: bool = True) -> Optional[bytes]:
    """Backward-compatible alias; the KEK moved out of config.json."""
    return _machine_kek(create=create)


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
        self._wrap_stamp = None  # identity of the wrap file the cached DEK came from

    # -- public API ---------------------------------------------------------

    def load(self) -> Dict[str, str]:
        """Return the decrypted blob (empty dict if missing or undecryptable)."""
        with self._tlock, self._file_lock():
            return self._load_locked()

    def load_strict(self) -> Dict[str, str]:
        """Like load(), but RAISE when a payload exists and cannot be read.

        `load()` answers "missing" and "present but undecryptable" with the same empty
        dict, and from the outside those are indistinguishable - a caller that falls back
        to a weaker source on an empty result therefore falls back on a CORRUPT store just
        as readily as on an absent one. For credentials that is the difference between a
        migration and a silent downgrade: the weaker copy still exists on disk, so the
        fallback succeeds and nobody learns the encrypted one broke.

        This is additive. `load()` keeps its old contract, and its two existing callers
        (mail and cloud credentials) are unchanged - they swallow the same way they always
        have, which is its own finding and not this one's to fix.
        """
        with self._tlock, self._file_lock():
            if not self.enc_path.exists():
                return {}
            return self._load_locked(strict=True)

    def update(self, mutator: Callable[[Dict[str, str]], None], *, strict: bool = False) -> None:
        """Atomically load -> mutate -> save under both locks (no lost updates).

        `strict=True` refuses when a payload EXISTS but cannot be decrypted.
        Without it the load answers {} for that case and the save then replaces
        the whole store with whatever the mutator put in - so one write into an
        unreadable keyring would discard every other key in it. The check runs
        inside the same lock as the save, so there is no window between them.
        """
        with self._tlock, self._file_lock():
            data = self._load_locked(strict=strict)
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

    def _load_locked(self, strict: bool = False) -> Dict[str, str]:
        if not self.enc_path.exists():
            return {}
        try:
            raw = self.enc_path.read_bytes()
            if len(raw) < _NONCE_SIZE:
                if strict:
                    raise SecureStoreUnreadable(f"{self.name}: payload is truncated")
                return {}
            nonce, ciphertext = raw[:_NONCE_SIZE], raw[_NONCE_SIZE:]
            dek = self._get_dek(create=False)
            if dek is None:
                if strict:
                    raise SecureStoreUnreadable(f"{self.name}: key material is unavailable")
                return {}
            decrypted = AESGCM(dek).decrypt(nonce, ciphertext, None).decode("utf-8")
            return json.loads(decrypted)
        except SecureStoreUnreadable:
            raise
        except Exception as e:
            logger.warning("Failed to load secure store %s: %s", self.name, e)
            if strict:
                raise SecureStoreUnreadable(f"{self.name}: payload could not be decrypted") from e
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

    def _wrap_stamp_now(self):
        """Identity of the wrapped-DEK file, or None when it is absent."""
        try:
            st = self.wrap_path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _get_dek(self, create: bool) -> Optional[bytes]:
        """The DEK, with the cache validated against the wrap file every time.

        The cache used to be trusted for the lifetime of the process, and that
        is wrong across processes: another one re-wrapping the store gives it a
        DIFFERENT DEK, and this one then keeps encrypting its writes with the
        old one. The write succeeds, the lock is held, nothing looks wrong - and
        the payload is now sealed with a key the wrap file no longer names, so
        the whole store is unreadable to everybody including its author.

        Reproduced standalone before this fix: two stores over one file, one
        re-wraps, the other writes, result `SecureStoreUnreadable`. On the live
        machine it showed up as 34 successful key writes that left nothing
        behind, because each one bricked the store the next read gave up on.

        A stat of the wrap file is enough to notice, and it happens under the
        same lock as the read-modify-write that follows.
        """
        stamp = self._wrap_stamp_now()
        if self._dek_cache is not None:
            if stamp == self._wrap_stamp:
                return self._dek_cache
            logger.info("Wrapped DEK for %s changed on disk; re-reading it", self.name)
            self._dek_cache = None
        dek = self._resolve_dek(create=create)
        if dek is not None:
            self._dek_cache = dek
            self._wrap_stamp = self._wrap_stamp_now()
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
            if kek is None:
                # Without this the None reached AESGCM as a TypeError, the write
                # failed, the store stayed absent - and the very next call minted
                # ANOTHER key and tried again. A real restart produced 295 of
                # those; had one write landed in between, everything encrypted
                # under the previous key would have been unreadable.
                raise SecureStoreUnreadable(
                    "The machine key is not reachable, so the key store cannot be "
                    "written. Refusing to continue rather than minting a second key."
                )
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
        self._dek_cache = dek
        self._wrap_stamp = self._wrap_stamp_now()

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
