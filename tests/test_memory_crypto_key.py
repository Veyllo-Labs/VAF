# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Memory encryption key handling (vaf/memory/crypto.py).

Pins the landmine fix: a PRESENT but corrupt/wrong-length key must be a hard
error, never a silent regenerate (the old behavior overwrote the key and
permanently orphaned every already-encrypted memory). Only a genuinely
missing key generates a fresh one.
"""
import base64
import secrets

import pytest

from vaf.memory.crypto import MemoryCrypto


def _with_key(monkeypatch, value):
    from vaf.core.config import Config
    saved = {}
    monkeypatch.setattr(Config, "get", classmethod(
        lambda cls, k, d=None: value if k == "memory_encryption_key" else d))
    monkeypatch.setattr(Config, "set", classmethod(
        lambda cls, k, v: saved.__setitem__(k, v)))
    return saved


def test_valid_key_roundtrip(monkeypatch):
    key = secrets.token_bytes(32)
    _with_key(monkeypatch, base64.b64encode(key).decode())
    c = MemoryCrypto()
    ct, nonce = c.encrypt("geheimer fakt")
    assert c.decrypt(ct, nonce) == "geheimer fakt"


def test_corrupt_base64_key_refuses_to_regenerate(monkeypatch):
    saved = _with_key(monkeypatch, "not-valid-base64!!!")
    with pytest.raises(RuntimeError, match="orphan"):
        MemoryCrypto()
    assert "memory_encryption_key" not in saved  # never overwritten


def test_wrong_length_key_refuses_to_regenerate(monkeypatch):
    saved = _with_key(monkeypatch, base64.b64encode(b"short").decode())
    with pytest.raises(RuntimeError, match="16|32|bytes"):
        MemoryCrypto()
    assert "memory_encryption_key" not in saved


def test_missing_key_generates_once(monkeypatch):
    saved = _with_key(monkeypatch, "")
    c = MemoryCrypto()
    assert "memory_encryption_key" in saved  # first-run generation persists
    ct, nonce = c.encrypt("x")
    assert c.decrypt(ct, nonce) == "x"


# ---------------------------------------------------------------------------
# Field/file helpers (chunk-text-at-rest + profile-cache encryption)
# ---------------------------------------------------------------------------

def test_field_roundtrip_and_legacy_passthrough(monkeypatch):
    import base64 as _b64
    import secrets as _secrets
    _with_key(monkeypatch, _b64.b64encode(_secrets.token_bytes(32)).decode())
    from vaf.memory import crypto as mc
    mc.reset_crypto()
    enc = mc.encrypt_field("Mert owns patent US12375457B2.")
    assert enc.startswith(mc.FIELD_PREFIX)
    assert "patent" not in enc
    assert mc.decrypt_field(enc) == "Mert owns patent US12375457B2."
    # Legacy plaintext rows pass through untouched (pre-migration tolerance)
    assert mc.decrypt_field("plain old chunk text") == "plain old chunk text"
    assert mc.encrypt_field("") == ""
    # Tampered ciphertext degrades to the sentinel, never raises
    assert mc.decrypt_field(mc.FIELD_PREFIX + "AAAA:BBBB") == "[Decryption failed]"
    mc.reset_crypto()


def test_file_roundtrip_and_legacy_passthrough(monkeypatch):
    import base64 as _b64
    import secrets as _secrets
    _with_key(monkeypatch, _b64.b64encode(_secrets.token_bytes(32)).decode())
    from vaf.memory import crypto as mc
    mc.reset_crypto()
    blob = mc.encrypt_file_bytes("known facts: user prefers dark mode")
    assert blob.startswith(b"VAFENC1:")
    assert b"dark mode" not in blob
    assert mc.decrypt_file_bytes(blob) == "known facts: user prefers dark mode"
    # Legacy plaintext cache files keep working until rewritten
    assert mc.decrypt_file_bytes(b"legacy plaintext cache") == "legacy plaintext cache"
    assert mc.decrypt_file_bytes(b"") == ""
    mc.reset_crypto()
