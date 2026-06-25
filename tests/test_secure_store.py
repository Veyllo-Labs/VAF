# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Tests for vaf.core.secure_store (envelope encryption, locking, migration)."""

import base64
import json
import os
import threading

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import vaf.core.secure_store as ss
from vaf.core.config import Config


WINDOWS = os.name == "nt"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Redirect Config to a tmp file and give each test isolated store paths."""
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(Config, "APP_DIR", tmp_path, raising=False)
    monkeypatch.setattr(Config, "CONFIG_FILE", cfg, raising=False)
    ss.set_passphrase(None)
    yield tmp_path
    ss.set_passphrase(None)


def _make_store(tmp_path, legacy=None):
    return ss.SecureBlobStore("test", tmp_path / "test_credentials.enc", legacy)


# ── roundtrip ────────────────────────────────────────────────────────────────

def test_roundtrip(env):
    store = _make_store(env)
    store.update(lambda d: d.__setitem__("acct", json.dumps({"password": "s3cret"})))
    assert json.loads(store.load()["acct"]) == {"password": "s3cret"}
    # A fresh instance (cold cache) reads the same data back.
    assert _make_store(env).load()["acct"]


def test_missing_file_returns_empty(env):
    assert _make_store(env).load() == {}


# ── concurrency: no lost updates ───────────────────────────────────────────────

def test_concurrent_updates_no_lost_writes(env):
    store = _make_store(env)
    n = 25

    def writer(i):
        store.update(lambda d, k=f"acct{i}": d.__setitem__(k, str(k)))

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    data = store.load()
    assert len(data) == n
    assert all(f"acct{i}" in data for i in range(n))


# ── permissions ────────────────────────────────────────────────────────────────

@pytest.mark.skipif(WINDOWS, reason="chmod not enforced on Windows")
def test_files_are_owner_only(env):
    store = _make_store(env)
    store.update(lambda d: d.__setitem__("k", "v"))
    for p in (store.enc_path, store.wrap_path):
        assert p.exists()
        assert oct(os.stat(p).st_mode & 0o777) == oct(0o600)


# ── passphrase mode ─────────────────────────────────────────────────────────────

def test_passphrase_keeps_config_secret_free(env):
    ss.set_passphrase("correct horse battery staple")
    store = _make_store(env)
    store.update(lambda d: d.__setitem__("k", "v"))

    wrap = json.loads(store.wrap_path.read_text("utf-8"))
    assert wrap["kdf"] == "scrypt"
    # No raw KEK is ever written to config in passphrase mode.
    assert (Config.get(ss._CONFIG_KEK_NAME, "") or "") == ""
    assert store.load()["k"] == "v"


def test_wrong_passphrase_fails_gracefully(env):
    ss.set_passphrase("right-pass")
    store = _make_store(env)
    store.update(lambda d: d.__setitem__("k", "v"))

    ss.set_passphrase("wrong-pass")
    other = _make_store(env)  # cold cache -> forced to unwrap with wrong KEK
    assert other.load() == {}  # no crash, just empty

    ss.set_passphrase("right-pass")
    assert _make_store(env).load()["k"] == "v"


def test_env_var_passphrase(env, monkeypatch):
    ss.set_passphrase(None)
    monkeypatch.setenv(ss.PASSPHRASE_ENV, "from-env")
    store = _make_store(env)
    store.update(lambda d: d.__setitem__("k", "v"))
    assert json.loads(store.wrap_path.read_text("utf-8"))["kdf"] == "scrypt"
    assert store.load()["k"] == "v"


# ── passphrase upgrade + rotation ───────────────────────────────────────────────

def test_raw_upgrades_to_passphrase(env):
    # Write without a passphrase (raw KEK in config).
    store = _make_store(env)
    store.update(lambda d: d.__setitem__("k", "v"))
    assert json.loads(store.wrap_path.read_text("utf-8"))["kdf"] == "raw"

    # Now a passphrase appears -> next load transparently upgrades to scrypt.
    ss.set_passphrase("new-pass")
    upgraded = _make_store(env)
    assert upgraded.load()["k"] == "v"
    assert json.loads(upgraded.wrap_path.read_text("utf-8"))["kdf"] == "scrypt"


def test_rewrap_after_passphrase_change(env):
    ss.set_passphrase("pass-one")
    store = _make_store(env)
    store.update(lambda d: d.__setitem__("k", "v"))
    salt_before = json.loads(store.wrap_path.read_text("utf-8"))["salt"]

    ss.set_passphrase("pass-two")
    assert store.rewrap() is True
    doc = json.loads(store.wrap_path.read_text("utf-8"))
    assert doc["kdf"] == "scrypt"
    assert doc["salt"] != salt_before  # re-wrapped with a fresh salt
    # Old passphrase can no longer open it; new one can.
    ss.set_passphrase("pass-two")
    assert _make_store(env).load()["k"] == "v"


# ── legacy migration ─────────────────────────────────────────────────────────────

def test_legacy_key_migration(env):
    # Seed a legacy plaintext key + a payload encrypted with it (old scheme).
    legacy_key = os.urandom(32)
    Config.set("email_credentials_key", base64.b64encode(legacy_key).decode())

    enc_path = env / "test_credentials.enc"
    nonce = os.urandom(12)
    payload = json.dumps({"acct": json.dumps({"password": "old"})}).encode()
    ct = AESGCM(legacy_key).encrypt(nonce, payload, None)
    enc_path.write_bytes(nonce + ct)

    store = _make_store(env, legacy="email_credentials_key")
    # Old data is still readable after migration.
    assert json.loads(store.load()["acct"]) == {"password": "old"}
    # Wrap file now exists and the plaintext legacy key is gone from config.
    assert store.wrap_path.exists()
    assert (Config.get("email_credentials_key", "") or "") == ""
