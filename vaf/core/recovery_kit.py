# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The way back in: a recovery key, and the note that explains it.

Machine-held keys have one honest drawback, and it is severe: reinstall the
system, replace the disk, or lose the OS keyring, and the encrypted chats are
gone. No vendor can help, because nobody else has the key. So the moment the
keyring is created, VAF writes a recovery kit the user can put somewhere else.

## What the recovery key actually is

A SECOND wrapping of the same data-encryption key. The keyring's DEK is already
wrapped once under the machine KEK (OS keyring or a 0600 file); the recovery
wrap stores it a second time under a key derived from that secret with scrypt.
Two consequences worth stating plainly:

- **The key plus `data_keys.enc` and `data_keys.recovery.json` is enough.**
  No OS keyring, no original machine, no password. That is the point.
- **The key alone is not**, and neither are the files alone. The kit says so,
  because a recovery instruction that overstates what it covers is worse than
  none.

It is written as ONE base64 string. An earlier version also printed 24 words
from a 64-word list and called them 256 bits; six bits per word makes 144, and
there was no checksum, so a mistyped word was indistinguishable from the wrong
backup file. Two encodings of one secret is also twice the transcription and
leak surface, and no comparable product ships both. If hand-transcription ever
becomes the real scenario, the answer is BIP-39 - 2048 words with a checksum -
not a home-made list.

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
import secrets
from pathlib import Path
from typing import Optional

logger = logging.getLogger("vaf.core.recovery_kit")

KIT_FILENAME = "VAF-BackThisUp.md"
RECOVERY_WRAP_NAME = "data_keys.recovery.json"
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


def create_recovery_wrap(dek: bytes) -> str:
    """Wrap `dek` a second time under a fresh recovery secret. Returns it (base64)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from vaf.core.secure_store import _atomic_write_bytes, harden_path

    secret = secrets.token_bytes(32)
    secret_b64 = base64.b64encode(secret).decode()
    salt = secrets.token_bytes(16)
    key = _derive(secret_b64, salt)
    nonce = secrets.token_bytes(12)
    doc = {
        "v": 1,
        "kdf": "scrypt",
        "salt": base64.b64encode(salt).decode(),
        "n": _SCRYPT_N, "r": _SCRYPT_R, "p": _SCRYPT_P,
        "nonce": base64.b64encode(nonce).decode(),
        "wrapped": base64.b64encode(AESGCM(key).encrypt(nonce, dek, None)).decode(),
    }
    path = recovery_wrap_path()
    _atomic_write_bytes(path, json.dumps(doc).encode("utf-8"))
    harden_path(path)
    return secret_b64


def unwrap_with_secret(secret_b64: str) -> Optional[bytes]:
    """The DEK, from the recovery wrap and the secret. None when it does not fit."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        doc = json.loads(recovery_wrap_path().read_text(encoding="utf-8"))
        key = _derive(
            secret_b64.strip(),
            base64.b64decode(doc["salt"]),
        )
        return AESGCM(key).decrypt(
            base64.b64decode(doc["nonce"]), base64.b64decode(doc["wrapped"]), None
        )
    except Exception as e:
        logger.warning("Recovery unwrap failed: %s", e)
        return None


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

Copy it exactly, including the trailing characters. It is 256 bits of
randomness and there is no second copy anywhere.

## What you need to recover

Three things, and the key alone is not enough:

1. this recovery key,
2. a backup of `{ring.name}`,
3. a backup of `{ring.with_name('data_keys.enc').name}`.

Both files live in:

    {ring.parent}

Back up that whole folder. It does not contain your chats, only the keys that
open them, so it is small.

## How to recover

On the new machine, after installing VAF, put the two files back into the folder
above, restore your data, then run:

    vaf secure recover

It asks for the key and puts it back where VAF looks for it. Verify with:

    vaf secure status

## If you switch encryption off

`file_encryption_enabled = false` writes new chats in plain text. Files already
encrypted stay readable, and this recovery key stays the way back to them.
"""
        path = kit_path()
        path.write_text(text, encoding="utf-8")
        harden_path(path)
        logger.info("Wrote the recovery kit to %s", path)
        return path
    except Exception as e:
        logger.warning("Could not write the recovery kit: %s", e)
        return None


def ensure_recovery_kit(dek: bytes) -> Optional[Path]:
    """Create the recovery wrap and the note, once. Returns the note's path."""
    if recovery_wrap_path().exists():
        return None
    secret = create_recovery_wrap(dek)
    return write_kit(secret)
