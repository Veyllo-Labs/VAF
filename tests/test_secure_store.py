# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Tests for vaf.core.secure_store (envelope encryption, locking, migration)."""

import base64
import json
import os
import threading

import secrets

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


def test_a_stale_dek_cache_cannot_brick_the_store(tmp_path):
    """The defect behind 34 key writes that left nothing behind.

    The DEK was cached for the lifetime of the process. Another process
    re-wrapping the store gives it a DIFFERENT DEK, and this one then kept
    encrypting with the old one: the write succeeds under the lock, nothing
    looks wrong, and the payload ends up sealed with a key the wrap file no
    longer names - unreadable to everybody, including its author.

    MUTATION: trust the cache again (drop the stamp check in _get_dek) and this
    ends in SecureStoreUnreadable.
    """
    from vaf.core.secure_store import SecureBlobStore

    path = tmp_path / "probe.enc"
    holder = SecureBlobStore("probe", path)
    holder.update(lambda d: d.__setitem__("a", "1"))
    holder.load_strict()                      # holder now caches the DEK

    other = SecureBlobStore("probe", path)    # a second process
    other.update(lambda d: d.__setitem__("b", "2"))
    other._wrap_and_store_dek(secrets.token_bytes(32))   # and it re-wraps

    holder.update(lambda d: d.__setitem__("c", "3"))

    assert SecureBlobStore("probe", path).load_strict(), "the store must stay readable"


def test_a_first_start_mints_exactly_one_machine_key(tmp_path):
    """Eight processes on a virgin home must agree on ONE master key.

    Not a theoretical race: a first start brings up the tray and five headless
    workers at once and every one of them resolves the KEK. Two that mint
    simultaneously used to both write the file, the later one winning - and any
    store the earlier one had already wrapped under its own key was then
    permanently unopenable, with no error anywhere.

    Real processes rather than threads, because the defect is cross-process and
    a threading test would pass against the broken code.

    MUTATION: replace the exclusive create in _write_kek with a truncating open,
    or drop the lock in _machine_kek, and this goes red - intermittently, which
    is exactly why it survived review. Three consecutive runs were enough to
    reproduce it while the fix was reverted.
    """
    import collections
    import os
    import subprocess
    import sys
    from pathlib import Path

    repo = str(Path(__file__).resolve().parent.parent)
    home = tmp_path / "home"
    (home / ".vaf").mkdir(parents=True)
    (home / ".vaf" / "config.json").write_text("{}", encoding="utf-8")
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home),
               XDG_DATA_HOME=str(home / ".local" / "share"),
               VAF_LOG_DIR=str(home / "logs"), PYTHONPATH=repo)
    code = ("import base64,sys;from vaf.core.secure_store import _machine_kek;"
            "k=_machine_kek(create=True);"
            "sys.stdout.write(base64.b64encode(k).decode() if k else 'NONE')")

    procs = [subprocess.Popen([sys.executable, "-c", code], env=env, cwd=repo,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
             for _ in range(8)]
    keys = [p.communicate(timeout=120)[0].strip() for p in procs]

    assert "NONE" not in keys, "a process failed to resolve the key at all"
    counts = collections.Counter(keys)
    assert len(counts) == 1, (
        f"{len(counts)} different master keys were handed out on one machine: "
        f"{[(k[:12], n) for k, n in counts.items()]} - every store wrapped under "
        f"a losing key is now unopenable")
    on_disk = (home / ".vaf" / "secure_store.kek").read_text(encoding="utf-8").strip()
    assert keys[0] == on_disk, "the key in use is not the key that survived on disk"
