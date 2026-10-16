# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The way back in: a recovery key, and the note that explains it.

Machine-held keys have one honest drawback, and it is severe: reinstall the
system, replace the disk, or lose the OS keyring, and the encrypted chats are
gone. No vendor can help, because nobody else has the key. So the moment the
keyring is created, VAF writes a recovery kit the user can put somewhere else.

## What the recovery key actually is

An `age` identity (`AGE-SECRET-KEY-1...`, the X25519 key of the age file
encryption format), and `data_keys.recovery.json` is a small file encrypted TO it
that holds two keys: the keyring's DEK and the machine key (KEK) that wraps every
other encrypted store. Both, because measured: the first version wrapped the DEK
alone, so it brought back the chats and memories while the API keys, the mail
passwords and the people's stored credentials - each in a store of its own,
wrapped under the KEK - stayed lost with the machine.

Why age rather than a key of our own: it is a published format with a bech32
checksum (a mistyped character is reported, not taken for the wrong file), a
reference implementation (pyrage binds it) and a standalone CLI, and the machine
needs only the PUBLIC half (`recipient`) to encrypt to it - which is what lets a
scheduled backup be addressed to a key that is not on the machine.

Two consequences worth stating plainly:

- **The key plus the key folder is enough.** No OS keyring, no original
  machine, no password. That is the point.
- **The key alone is not**, and neither are the files alone. The kit says so,
  because a recovery instruction that overstates what it covers is worse than
  none.

Version 1 kits (a base64 secret, scrypt, the DEK only) stay readable: an
installation keeps working with the kit it has, `vaf secure status` says what it
does not cover, and `vaf secure renew-recovery-key` replaces it.

It is ONE string. An earlier version also printed 24 words from a 64-word list
and called them 256 bits; six bits per word makes 144, and there was no
checksum. Two encodings of one secret is also twice the transcription and leak
surface, and no comparable product ships both.

## Where the note goes

The Desktop, deliberately visible, named so it reads as an instruction rather
than as clutter. It is a PLAINTEXT secret while it sits there - the file says
that in its first line and tells the user to move it off the machine. A recovery
kit on the same disk as the data protects against a reinstall, not against
theft; those are different failures and the note does not blur them.
"""
from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("vaf.core.recovery_kit")

KIT_FILENAME = "VAF-BackThisUp.md"
RECOVERY_WRAP_NAME = "data_keys.recovery.json"
# The format tag of a version 2 wrap. Chosen once; pinned in
# tests/test_persisted_format_tags.py. A wrap without it is version 1.
RECOVERY_FORMAT = "recovery-2-3d8a51"
_AGE_IDENTITY_PREFIX = "AGE-SECRET-KEY-1"
_SCRYPT_N = 2 ** 15
_SCRYPT_R = 8
_SCRYPT_P = 1



def _derive(secret_b64: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    kdf = Scrypt(salt=salt, length=32, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return kdf.derive(secret_b64.encode("utf-8"))


def recovery_wrap_path() -> Path:
    from vaf.core.data_keyring import _ring
    return _ring().enc_path.with_name(RECOVERY_WRAP_NAME)


def kit_path() -> Path:
    """Where the note goes: the Desktop, else next to the keys.

    The fallback is the KEY DIRECTORY, not the bare home. A home directory is
    somebody's working folder and the suite's own isolation guard treats
    anything landing there as an escape - correctly, because a file that shows
    up unannounced in `~` is indistinguishable from pollution. Beside the keys
    it is at least where the thing it unlocks lives.
    """
    home = Path.home()
    for candidate in (home / "Desktop", home / "Schreibtisch"):
        if candidate.is_dir():
            return candidate / KIT_FILENAME
    return recovery_wrap_path().with_name(KIT_FILENAME)


class RecoveredKeys:
    """What a recovery key opens: the keyring's DEK and, from version 2 on, the machine
    key (None for a version 1 kit, or for an installation whose stores are wrapped under
    a passphrase instead of a machine key)."""

    def __init__(self, dek: bytes, kek: Optional[bytes], version: int):
        self.dek = dek
        self.kek = kek
        self.version = version


def _machine_kek_for_kit() -> Optional[bytes]:
    """The machine key the stores are wrapped under, or None when they are wrapped under
    a passphrase (`VAF_MASTER_PASSPHRASE`): the person holds that one, not the machine."""
    from vaf.core.secure_store import _config_kek, _get_passphrase
    if _get_passphrase():
        return None
    return _config_kek(create=False)


def build_recovery_wrap(dek: bytes, kek: Optional[bytes] = None) -> tuple:
    """A fresh recovery identity and the wrap encrypted to it, IN MEMORY. Writes nothing.

    Returns (identity string, wrap document). Split out from the write so the caller
    can order the two persistent steps. Which order matters: see `ensure_recovery_kit`.
    """
    import pyrage
    from pyrage import x25519

    identity = x25519.Identity.generate()
    recipient = identity.to_public()
    payload = json.dumps({
        "dek": base64.b64encode(dek).decode(),
        "kek": base64.b64encode(kek).decode() if kek else None,
    }).encode("utf-8")
    doc = {
        "v": 2,
        "format": RECOVERY_FORMAT,
        "recipient": str(recipient),
        "covers": ["keyring"] + (["machine_key"] if kek else []),
        "age": base64.b64encode(pyrage.encrypt(payload, [recipient])).decode(),
    }
    return str(identity), doc


def persist_recovery_wrap(doc: dict) -> Path:
    """Write the wrap document, owner-only. Raises if it cannot be written."""
    from vaf.core.secure_store import _atomic_write_bytes, harden_path

    path = recovery_wrap_path()
    _atomic_write_bytes(path, json.dumps(doc).encode("utf-8"))
    harden_path(path)
    return path


def create_recovery_wrap(dek: bytes, kek: Optional[bytes] = None) -> str:
    """Wrap `dek` (and `kek`) for a fresh recovery identity. Returns the identity."""
    identity, doc = build_recovery_wrap(dek, kek)
    persist_recovery_wrap(doc)
    return identity


def _read_wrap() -> Optional[dict]:
    try:
        return json.loads(recovery_wrap_path().read_text(encoding="utf-8"))
    except Exception:
        return None


def kit_version() -> int:
    """2 for an age wrap that carries the machine key too, 1 for the scrypt wrap of the
    DEK alone, 0 when there is no recovery wrap."""
    doc = _read_wrap()
    if not doc:
        return 0
    return 2 if doc.get("format") == RECOVERY_FORMAT else 1


def kit_covers_machine_key() -> bool:
    doc = _read_wrap() or {}
    return doc.get("format") == RECOVERY_FORMAT and "machine_key" in (doc.get("covers") or [])


def recipient() -> Optional[str]:
    """The PUBLIC half of the recovery identity (`age1...`), from a version 2 wrap: what
    a file can be encrypted to so that only the holder of the recovery key opens it."""
    doc = _read_wrap() or {}
    if doc.get("format") != RECOVERY_FORMAT:
        return None
    value = str(doc.get("recipient") or "").strip()
    return value or None


def unwrap_keys(secret: str) -> Optional[RecoveredKeys]:
    """What the recovery key opens, from the wrap beside the keyring. None when it does
    not fit. Takes either kit: an age identity for version 2, the base64 secret for 1."""
    doc = _read_wrap()
    secret = str(secret or "").strip()
    if not doc or not secret:
        return None
    try:
        if doc.get("format") == RECOVERY_FORMAT:
            if not secret.upper().startswith(_AGE_IDENTITY_PREFIX):
                return None
            import pyrage
            from pyrage import x25519
            identity = x25519.Identity.from_str(secret.upper())
            payload = json.loads(pyrage.decrypt(base64.b64decode(doc["age"]), [identity]))
            kek = payload.get("kek")
            return RecoveredKeys(base64.b64decode(payload["dek"]),
                                 base64.b64decode(kek) if kek else None, 2)
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        key = _derive(secret, base64.b64decode(doc["salt"]))
        dek = AESGCM(key).decrypt(
            base64.b64decode(doc["nonce"]), base64.b64decode(doc["wrapped"]), None)
        return RecoveredKeys(dek, None, 1)
    except Exception as e:
        logger.warning("Recovery unwrap failed: %s", e)
        return None


def unwrap_with_secret(secret: str) -> Optional[bytes]:
    """The keyring's DEK, from the recovery wrap and the key. None when it does not fit."""
    keys = unwrap_keys(secret)
    return keys.dek if keys else None


def write_kit(secret_b64: str) -> Optional[Path]:
    """Write the human-readable note next to the recovery wrap. Never raises."""
    from vaf.core.secure_store import harden_path

    try:
        ring = recovery_wrap_path()
        text = f"""# VAF - back this up

**This file is a key. Anyone holding it can read your VAF chats.** Move it off
this computer - a password manager, a printout in a drawer, an encrypted USB
stick - and delete it here once you have. While it sits on this Desktop it
protects you against a reinstall, not against someone taking the machine.

## Why it exists

VAF encrypts your chats, memories and mail on this computer. The key is held by
the machine so the assistant keeps working after a reboot without you typing
anything. The flip side: reinstall the system, swap the disk, or lose your
operating-system login, and nothing on earth can read those files again. This
recovery key is the second way in.

## Your recovery key

    {secret_b64}

Copy it exactly. It is an `age` key: 256 bits of randomness with a checksum, so a
mistyped character is reported instead of silently failing. There is no second
copy anywhere.

## What it opens

The key store (chats, memories, mail) and the machine key under which every other
encrypted store is sealed: your API keys, mail passwords and the credentials
stored for commands. On a new machine all of them open again.

## What you need to recover

Two things, and the key alone is not enough:

1. this recovery key,
2. a backup of the folder

       {ring.parent}

   It holds `{ring.name}`, `{ring.with_name('data_keys.enc').name}` and the other
   encrypted stores, and also the message, mail, calendar and contact databases.
   Your chats live elsewhere (`~/.vaf`), so back that folder up as well.

## How to recover

On the new machine, after installing VAF, put the folder back, restore your data,
then run:

    vaf secure recover

It asks for the key and puts the keys back where VAF looks for them. Verify with:

    vaf secure status

## If you switch encryption off

`file_encryption_enabled = false` writes new chats in plain text. Files already
encrypted stay readable, and this recovery key stays the way back to them.
"""
        # Atomically, and created 0600 from the start. write_text() creates the
        # file with the default mode and only harden_path() narrows it, so the
        # plaintext master key sat world-readable for the width of that gap -
        # on the Desktop, the most-watched directory on the machine.
        from vaf.core.secure_store import _atomic_write_bytes

        path = kit_path()
        _atomic_write_bytes(path, text.encode("utf-8"))
        harden_path(path)
        logger.info("Wrote the recovery kit to %s", path)
        return path
    except Exception as e:
        logger.warning("Could not write the recovery kit: %s", e)
        return None


def ensure_recovery_kit(dek: bytes) -> Optional[Path]:
    """Create the recovery wrap and the note, once. Returns the note's path.

    THE NOTE IS WRITTEN FIRST, and the wrap only if that succeeded. The obvious
    order is the dangerous one: the wrap is the durable half, the note is the
    only copy of the secret that opens it, and writing the wrap first leaves a
    window in which the secret exists solely in memory. Every reason the note
    can fail is real and platform-specific - a Desktop that macOS TCC has denied
    the process, a OneDrive-redirected Desktop that is not materialised, a
    read-only or full disk - and `write_kit` swallows all of them. The result
    was a recovery file nothing on earth can open, a guard below that considers
    the job done forever because it tests the WRAP, and `vaf secure status`
    reporting "Recovery key set up".

    Reversed, a failed note leaves nothing behind and the next start tries
    again. The mirror case - note written, wrap not - is self-healing for the
    same reason: the guard still sees no wrap, so the next start rewrites both
    as a matching pair.

    The guard cannot test the note instead: the note tells the user to move it
    off the machine, so its absence is the SUCCESS case, and regenerating on
    absence would silently invalidate the copy they filed away.
    """
    if recovery_wrap_path().exists():
        return None
    secret, doc = build_recovery_wrap(dek, _machine_kek_for_kit())
    path = write_kit(secret)
    if path is None:
        # Deliberately without naming the directory: resolving it means calling
        # kit_path() again, and the reason the note failed may BE that call.
        logger.error(
            "The recovery note could not be written, so no recovery key was created - "
            "the encrypted data would be unrecoverable after a reinstall. Nothing was "
            "stored, and the next start tries again."
        )
        return None
    try:
        persist_recovery_wrap(doc)
    except Exception as e:
        logger.error("Recovery key written to %s but its wrap could not be stored (%s); "
                     "the next start will replace both.", path, e)
        return None
    return path


def renew_recovery_kit(dek: bytes) -> Path:
    """Replace the recovery key with a new one: a fresh identity, a wrap that carries the
    machine key, a new note. The previous key stops opening anything. Raises when the
    note cannot be written; the wrap is replaced only after it was.

    Same order as `ensure_recovery_kit`, for the same reason: the note is the only copy
    of the secret, so the durable half is written second."""
    secret, doc = build_recovery_wrap(dek, _machine_kek_for_kit())
    path = write_kit(secret)
    if path is None:
        raise RuntimeError("The recovery note could not be written, so the recovery key was "
                           "not replaced; the previous one still works.")
    persist_recovery_wrap(doc)
    return path
