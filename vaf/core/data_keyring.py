# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The one place VAF's data-at-rest keys live.

Before this module, every at-rest key sat in plaintext in config.json and every
consumer minted and read its own: the memory key, the mail key, the GitHub key,
the JWT secret, the secure-store KEK - six hand-rolled special cases, and a
config backup carried all of them next to the data they protect.

Now they live in ONE envelope-encrypted blob (`data_keys.enc`, the same
SecureBlobStore machinery the credential stores already use), whose KEK sits in a
0600 file beside the config - on every platform, by default - or in the OS keyring
when `secure_store_kek_backend` is set to "keyring". The keyring is the stronger
place, because it is encrypted with the user's LOGIN password, and it is opt-in
because it is unreachable from a VAF started outside the desktop session
(`secure_store._preferred_kek_backend` carries the measurement). config.json ends
up holding no key material at all.

Three rules, each guarding real data:

- **A legacy value is ADOPTED byte-identically, never re-minted.** Minting a
  fresh memory key while encrypted rows exist orphans every one of them. The
  same applies to the JWT secret: TOTP rows are encrypted under a key derived
  from it, so its VALUE must survive the move.
- **An unreadable ring RAISES; it never mints.** `load()` answers "missing" and
  "present but undecryptable" identically, and minting on the second one is the
  exact catastrophe the first rule prevents. All reads here are strict.
- **Before the first config copy is blanked, a one-time backup of config.json
  is written** (`config.json.pre-keyring.bak`, 0600). That file is the
  documented downgrade path: an older VAF release looks for its keys in
  config.json, and restoring the backup puts them back where it looks.

This module is engine-internal on purpose. It is not exported on the facade and
EMBEDDING.md says so: embedders get the same behaviour they always had - keys
mint themselves on first use - just no longer into a plaintext file.
"""
from __future__ import annotations

import base64
import json
import logging
import secrets
import threading
from pathlib import Path
from typing import Callable, Optional

from vaf.core.config import Config
from vaf.core.secure_store import (
    SecureBlobStore,
    SecureStoreUnreadable,
    ensure_pre_migration_backup,
)

logger = logging.getLogger("vaf.core.data_keyring")

_store: Optional[SecureBlobStore] = None
_store_lock = threading.Lock()


def _ring() -> SecureBlobStore:
    global _store
    with _store_lock:
        if _store is None:
            from vaf.core.platform import Platform
            _store = SecureBlobStore("data_keys", Platform.data_dir() / "data_keys.enc")
        return _store


def reset_ring() -> None:
    """Testing hook: drop the cached store so a patched data_dir takes effect."""
    global _store
    with _store_lock:
        _store = None


def _established_marker() -> Path:
    """Records that THIS installation already has a keyring.

    Deliberately beside the config, not beside the ring: losing the data
    directory alone must still trip it.
    """
    return Path(Config.APP_DIR) / "data_keys.established"


def _mark_established() -> None:
    try:
        from vaf.core.secure_store import _atomic_write_bytes, harden_path
        marker = _established_marker()
        if not marker.exists():
            _atomic_write_bytes(marker, b"1")
            harden_path(marker)
    except Exception:
        pass


def _refuse_if_the_ring_vanished() -> None:
    """A MISSING ring is only a fresh install before the first key exists.

    This is the hole the first live restart fell through, and it is the worst
    one in the design: the refuse-to-mint guard only covered a ring that exists
    and cannot be opened. A ring file that is absent read as "first run", and
    since the legacy values in config.json had already been blanked by then,
    there was nothing left to adopt - so a new memory key was minted while every
    encrypted row on disk still needed the old one. Logged twice on a real
    machine before the marker existed.

    After the first key, an absent ring is an emergency, and the answer is the
    recovery key, never a fresh key.
    """
    if not _established_marker().exists():
        return
    if _ring().enc_path.exists():
        return
    raise RuntimeError(
        "The key store is gone but this installation had one "
        f"({_ring().enc_path}). Encrypted data cannot be read without it and a "
        "new key would make the loss permanent, so nothing is being minted. "
        "Restore data_keys.enc and data_keys.key.json from your backup, or use "
        "the recovery key from VAF-BackThisUp.md: vaf secure recover"
    )


def _refuse_on_unparseable_config(legacy_config_key: str) -> None:
    """The guard the memory key always had, generalised.

    `Config.load()` degrades an unreadable config.json to DEFAULTS, so a plain
    `Config.get` returns "" for a key that IS on disk - and minting then would
    orphan everything encrypted under the real one. A config file that exists
    but cannot be parsed therefore refuses key resolution outright.
    """
    cfg_path = Path(Config.CONFIG_FILE)
    if not cfg_path.exists():
        return
    try:
        json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise RuntimeError(
            f"config.json exists but could not be parsed while resolving "
            f"{legacy_config_key}. Refusing to mint a replacement key - repair or "
            f"restore config.json (or config.json.pre-keyring.bak) first."
        ) from e


def _legacy_config_value(legacy_config_key: str) -> str:
    value = str(Config.get(legacy_config_key, "") or "").strip()
    if value:
        return value
    # Config.get can answer from DEFAULTS after a failed load; re-read raw.
    try:
        raw = json.loads(Path(Config.CONFIG_FILE).read_text(encoding="utf-8"))
        return str(raw.get(legacy_config_key) or "").strip()
    except Exception:
        return ""


def _resolve(
    name: str,
    *,
    legacy_config_key: Optional[str],
    mint: Callable[[], str],
    validate: Callable[[str], bool],
) -> str:
    """Ring -> legacy config (adopt + blank) -> mint. Strict at every step."""
    _refuse_if_the_ring_vanished()
    ring = _ring()
    data = ring.load_strict()  # raises SecureStoreUnreadable instead of minting
    stored = data.get(name, "")
    if stored:
        return stored

    adopted: Optional[str] = None
    if legacy_config_key:
        _refuse_on_unparseable_config(legacy_config_key)
        legacy = _legacy_config_value(legacy_config_key)
        if legacy:
            if not validate(legacy):
                # PRESENT but malformed is never "absent". Minting here would
                # overwrite the only key that opens the existing ciphertext and
                # permanently orphan it - the incident this guard was written for.
                raise RuntimeError(
                    f"{legacy_config_key} is set but malformed. Refusing to mint a "
                    f"replacement because that would permanently orphan everything "
                    f"already encrypted under it. Restore the correct value (see "
                    f"config.json.pre-keyring.bak), or clear the config entry "
                    f"explicitly to start with a fresh store."
                )
            adopted = legacy

    minted = adopted is None
    if minted:
        adopted = mint()
    else:
        ensure_pre_migration_backup()

    def _put(payload):
        # Another process may have won the race; keep the first value.
        payload.setdefault(name, adopted)

    # A failed write raises out of here; nothing below runs, and the caller sees
    # the failure instead of a key that exists only in this process's memory.
    ring.update(_put, strict=True)
    _mark_established()
    final = ring.load_strict().get(name)
    if not final:
        # THE bug behind every "encrypted with a key nobody has" file: this used
        # to fall back to the in-memory value when the read-back did not show it.
        # The caller then encrypted real data with a key that lives only in this
        # process and dies with it, and the next start found ciphertext it could
        # not open. A key that is not in the store does not exist.
        raise RuntimeError(
            f"Key {name!r} was written but is not in the key store on read-back. "
            f"Refusing to hand out a key that is not persisted - data encrypted "
            f"with it would be unreadable after this process exits."
        )
    if minted:
        # Logged only now, because it is only true now: the earlier version
        # announced the mint before the write and printed it 295 times in a row
        # while nothing was ever stored.
        logger.warning(
            "Minted new data key %r into the keyring. If encrypted data written "
            "under an older key exists, restore config.json.pre-keyring.bak and "
            "delete data_keys.enc to re-adopt the original.", name,
        )
    ensure_recovery_kit()

    if adopted is not None and legacy_config_key and final == adopted and _legacy_config_value(legacy_config_key):
        try:
            Config.set(legacy_config_key, "")
            logger.info("Moved %s out of config.json into the keyring", legacy_config_key)
        except Exception:
            pass
    return final


def get_data_key(name: str, *, legacy_config_key: Optional[str] = None) -> bytes:
    """A 32-byte AES key by name; adopted from config.json once, then ring-only.

    Raises RuntimeError when the ring exists but cannot be opened, or when the
    stored value is malformed - never mints over unreadable state.
    """
    def _valid(value: str) -> bool:
        try:
            return len(base64.b64decode(value)) == 32
        except Exception:
            return False

    try:
        encoded = _resolve(
            name,
            legacy_config_key=legacy_config_key,
            mint=lambda: base64.b64encode(secrets.token_bytes(32)).decode(),
            validate=_valid,
        )
    except SecureStoreUnreadable as e:
        raise RuntimeError(
            f"The data keyring exists but cannot be opened while resolving {name!r} "
            f"(KEK unreachable or store corrupt). Encrypted data stays locked; "
            f"refusing to mint a replacement key."
        ) from e
    key = base64.b64decode(encoded)
    if len(key) != 32:
        raise RuntimeError(f"Keyring entry {name!r} is malformed (not a 32-byte key)")
    return key


def get_data_secret(
    name: str,
    *,
    legacy_config_key: Optional[str] = None,
    min_length: int = 32,
) -> str:
    """A string secret by name (e.g. the JWT signing secret). Same rules as keys."""
    try:
        return _resolve(
            name,
            legacy_config_key=legacy_config_key,
            mint=lambda: secrets.token_urlsafe(32),
            validate=lambda value: len(value) >= min_length,
        )
    except SecureStoreUnreadable as e:
        raise RuntimeError(
            f"The data keyring exists but cannot be opened while resolving {name!r}."
        ) from e


def set_data_secret(name: str, value: str) -> None:
    """Store/overwrite a named secret (e.g. the admin password-hash copy).

    The vanished-ring guard runs here too, and it has to. This is the only
    entry point that WRITES without going through _resolve, and on a machine
    whose key store is gone `update` would happily create a fresh one: a ring
    that looks healthy, opens nothing, and silences the guard for every later
    read. The realistic path is not exotic - restore a backup that missed the
    data directory, then set a password in the web UI, and the hash mirror
    lands here first.
    """
    _refuse_if_the_ring_vanished()

    def _put(payload):
        payload[name] = value

    # strict: writing one entry must never be able to discard the others.
    _ring().update(_put, strict=True)


def peek_data_secret(name: str) -> str:
    """Read a named secret without minting; '' when absent. Strict on corruption."""
    try:
        return _ring().load_strict().get(name, "")
    except SecureStoreUnreadable:
        raise RuntimeError(f"The data keyring cannot be opened while reading {name!r}.")


def ensure_recovery_kit() -> None:
    """Create the recovery key the first time the keyring has a DEK.

    Machine-held keys mean a reinstall or a lost OS keyring is unrecoverable, so
    the way back has to be created at the same moment the keys are - not offered
    in a settings page the user visits after the disk dies.
    """
    try:
        ring = _ring()
        dek = ring._get_dek(create=False)
        if dek is None:
            return
        from vaf.core.recovery_kit import ensure_recovery_kit as _ensure
        _ensure(dek)
    except Exception as e:  # noqa: BLE001 - a missing kit must never block a key
        logger.warning("Could not create the recovery kit: %s", e)


def ring_exists() -> bool:
    """Is there a keyring payload on disk at all? (Absent = first run.)"""
    try:
        return _ring().enc_path.exists()
    except Exception:
        return False


def ring_status() -> dict:
    """For `vaf secure status`: what is in the ring, what is still legacy."""
    from vaf.core.secure_store import kek_backend

    names = []
    unreadable = False
    try:
        names = sorted(_ring().load_strict().keys())
    except SecureStoreUnreadable:
        unreadable = True
    legacy = {
        key: bool(Config.get(key, ""))
        for key in (
            "memory_encryption_key",
            "mail_store_encryption_key",
            "github_credentials_key",
            "local_network_jwt_secret",
            "secure_store_kek",
        )
    }
    return {
        "kek_backend": kek_backend(),
        "entries": names,
        "unreadable": unreadable,
        "legacy_in_config": [key for key, present in legacy.items() if present],
        "store_path": str(_ring().enc_path),
    }
