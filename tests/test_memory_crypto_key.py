# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Memory encryption key handling (vaf/memory/crypto.py + vaf/core/data_keyring.py).

Pins the landmine fix: a PRESENT but corrupt/wrong-length key must be a hard
error, never a silent regenerate (the old behavior overwrote the key and
permanently orphaned every already-encrypted memory). Only a genuinely
missing key generates a fresh one.

The key moved OUT of config.json into the data keyring, so these now also pin
the adoption contract: a legacy config value is taken over BYTE-IDENTICALLY
(otherwise every encrypted row dies at the moment of the upgrade) and the
config copy is blanked only afterwards. The refuse-to-mint guards moved with
it, unchanged in meaning.
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
    with pytest.raises(RuntimeError, match="orphan|bytes"):
        MemoryCrypto()
    assert "memory_encryption_key" not in saved


def test_a_legacy_config_key_is_adopted_byte_identically(monkeypatch, tmp_path):
    """The upgrade must not change the key - existing rows have to keep opening.

    MUTATION: mint instead of adopting, and the ciphertext written before the
    move stops decrypting after it.
    """
    import json as _json
    from vaf.core.config import Config
    from vaf.core.data_keyring import peek_data_secret

    real = secrets.token_bytes(32)
    encoded = base64.b64encode(real).decode()
    saved = _with_key(monkeypatch, encoded)
    p = tmp_path / "config.json"
    p.write_text(_json.dumps({"memory_encryption_key": encoded}), encoding="utf-8")
    monkeypatch.setattr(Config, "CONFIG_FILE", p)

    before = MemoryCrypto(key=real)
    ct, nonce = before.encrypt("ein alter verschluesselter satz")

    assert MemoryCrypto()._key == real
    assert MemoryCrypto().decrypt(ct, nonce) == "ein alter verschluesselter satz"
    assert peek_data_secret("memory_encryption_key") == encoded
    # ...and only THEN is the plaintext copy in config.json cleared.
    assert saved.get("memory_encryption_key") == ""


def test_missing_key_with_no_config_file_mints_once_into_the_ring(monkeypatch, tmp_path, caplog):
    import logging
    from vaf.core.config import Config
    from vaf.core.data_keyring import peek_data_secret

    saved = _with_key(monkeypatch, "")
    monkeypatch.setattr(Config, "CONFIG_FILE", tmp_path / "config.json")
    with caplog.at_level(logging.WARNING, logger="vaf.core.data_keyring"):
        c = MemoryCrypto()

    assert peek_data_secret("memory_encryption_key")      # persisted in the ring
    assert "memory_encryption_key" not in saved           # NOT in config.json any more
    assert any("Minted" in r.message for r in caplog.records), "the mint must be loud"
    ct, nonce = c.encrypt("x")
    assert c.decrypt(ct, nonce) == "x"


def test_missing_key_with_parseable_config_mints(monkeypatch, tmp_path):
    """A cleanly-parsed config that genuinely has no key is the documented
    deliberate reset - minting is allowed."""
    from vaf.core.config import Config
    from vaf.core.data_keyring import peek_data_secret

    _with_key(monkeypatch, "")
    p = tmp_path / "config.json"
    p.write_text('{"provider": "local"}', encoding="utf-8")
    monkeypatch.setattr(Config, "CONFIG_FILE", p)
    MemoryCrypto()
    assert peek_data_secret("memory_encryption_key")


def test_unparseable_config_refuses_to_mint(monkeypatch, tmp_path):
    """THE incident pin: a torn config read must never mint a replacement key
    (a truncated json made Config.load fall back to DEFAULTS, the old code saw
    "no key" and rotated - orphaning every encrypted row)."""
    from vaf.core.config import Config
    from vaf.core.data_keyring import peek_data_secret

    saved = _with_key(monkeypatch, "")
    p = tmp_path / "config.json"
    p.write_text('{"provider": "loc', encoding="utf-8")  # cut mid-write
    monkeypatch.setattr(Config, "CONFIG_FILE", p)
    with pytest.raises(RuntimeError, match="Refusing to mint"):
        MemoryCrypto()
    assert "memory_encryption_key" not in saved
    assert not peek_data_secret("memory_encryption_key")


def test_defaults_fallback_read_recovers_the_real_key(monkeypatch, tmp_path):
    """When the normal read degrades to DEFAULTS ("") but the file actually
    carries a key, the strict raw read must return THAT key - never mint."""
    import json as _json
    from vaf.core.config import Config
    saved = _with_key(monkeypatch, "")
    real = secrets.token_bytes(32)
    p = tmp_path / "config.json"
    p.write_text(_json.dumps({"memory_encryption_key": base64.b64encode(real).decode()}),
                 encoding="utf-8")
    monkeypatch.setattr(Config, "CONFIG_FILE", p)
    c = MemoryCrypto()
    assert c._key == real, "the real key on disk must win over the degraded read"
    # The only write back to config.json is the blanking that follows adoption -
    # never a freshly minted key.
    assert saved.get("memory_encryption_key", "") == ""
    ct, nonce = c.encrypt("fact")
    assert c.decrypt(ct, nonce) == "fact"


def test_protected_keys_carry_the_memory_encryption_key():
    from vaf.core.config import Config
    assert "memory_encryption_key" in Config.PROTECTED_KEYS


# ---------------------------------------------------------------------------
# Field/file helpers (chunk-text-at-rest + profile-cache encryption)
# ---------------------------------------------------------------------------

def test_field_roundtrip_and_legacy_passthrough(monkeypatch):
    import base64 as _b64
    import secrets as _secrets
    _with_key(monkeypatch, _b64.b64encode(_secrets.token_bytes(32)).decode())
    from vaf.memory import crypto as mc
    mc.reset_crypto()
    enc = mc.encrypt_field("Alice owns patent US12375457B2.")
    assert enc.startswith(mc.FIELD_PREFIX)
    assert "patent" not in enc
    assert mc.decrypt_field(enc) == "Alice owns patent US12375457B2."
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
