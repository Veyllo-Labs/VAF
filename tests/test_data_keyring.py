# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The data keyring: one home for every at-rest key, and the guards around it.

Every at-rest key used to sit in plaintext in config.json next to the data it
protects, which made "encrypted at rest" mean "encrypted, with the key one file
away". Moving them is only safe if two properties hold, and both are pinned here:

- a legacy value is ADOPTED byte-identically, never re-minted (re-minting
  orphans every row already encrypted under it), and
- an unreadable or malformed source RAISES rather than minting over it.

The third property is about the way back: the one-time config.json backup must
exist BEFORE the first plaintext copy is blanked, because an older VAF release
looks for its keys in config.json and would mint replacements without it.
"""
import base64
import json
import secrets

import pytest

from vaf.core import data_keyring as dk
from vaf.core.config import Config


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    """A throwaway config.json plus a recorder for every Config.set."""
    written = {}
    store = {}
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")

    def _get(cls, key, default=None):
        return store.get(key, default)

    def _set(cls, key, value):
        store[key] = value
        written[key] = value
        path.write_text(json.dumps(store), encoding="utf-8")

    monkeypatch.setattr(Config, "get", classmethod(_get))
    monkeypatch.setattr(Config, "set", classmethod(_set))
    monkeypatch.setattr(Config, "CONFIG_FILE", path)
    monkeypatch.setattr(Config, "APP_DIR", tmp_path)

    def _seed(key, value):
        store[key] = value
        path.write_text(json.dumps(store), encoding="utf-8")

    return type("Cfg", (), {"seed": staticmethod(_seed), "written": written,
                            "store": store, "path": path})


# ── adoption ────────────────────────────────────────────────────────────────────

def test_a_legacy_key_is_adopted_unchanged_then_the_config_copy_is_cleared(cfg):
    """MUTATION: mint instead of adopt -> the value changes and the assert fails."""
    original = base64.b64encode(secrets.token_bytes(32)).decode()
    cfg.seed("memory_encryption_key", original)

    key = dk.get_data_key("memory_encryption_key", legacy_config_key="memory_encryption_key")

    assert base64.b64encode(key).decode() == original
    assert dk.peek_data_secret("memory_encryption_key") == original
    assert cfg.written["memory_encryption_key"] == ""     # blanked, not rotated


def test_a_string_secret_survives_the_move_verbatim(cfg):
    """TOTP rows are encrypted under a key derived from this string."""
    original = "a-jwt-secret-that-is-long-enough-to-pass-32"
    cfg.seed("local_network_jwt_secret", original)

    assert dk.get_data_secret(
        "local_network_jwt_secret", legacy_config_key="local_network_jwt_secret"
    ) == original


def test_the_second_call_reads_the_ring_not_the_blanked_config(cfg):
    original = base64.b64encode(secrets.token_bytes(32)).decode()
    cfg.seed("memory_encryption_key", original)

    first = dk.get_data_key("memory_encryption_key", legacy_config_key="memory_encryption_key")
    second = dk.get_data_key("memory_encryption_key", legacy_config_key="memory_encryption_key")

    assert first == second


# ── refuse to mint ──────────────────────────────────────────────────────────────

def test_a_malformed_legacy_value_raises_instead_of_minting(cfg):
    """Present-but-broken is not absent. MUTATION: treat it as absent -> mints."""
    cfg.seed("memory_encryption_key", "not-base64!!!")

    with pytest.raises(RuntimeError, match="orphan"):
        dk.get_data_key("memory_encryption_key", legacy_config_key="memory_encryption_key")

    assert not dk.peek_data_secret("memory_encryption_key")
    assert "memory_encryption_key" not in cfg.written


def test_a_wrong_length_legacy_key_raises(cfg):
    cfg.seed("memory_encryption_key", base64.b64encode(b"too-short").decode())

    with pytest.raises(RuntimeError, match="orphan"):
        dk.get_data_key("memory_encryption_key", legacy_config_key="memory_encryption_key")


def test_an_unparseable_config_raises_rather_than_minting(cfg):
    cfg.path.write_text('{"provider": "loc', encoding="utf-8")  # torn write

    with pytest.raises(RuntimeError, match="Refusing to mint"):
        dk.get_data_key("memory_encryption_key", legacy_config_key="memory_encryption_key")


def test_an_unreadable_ring_raises_rather_than_minting(cfg, tmp_path, monkeypatch):
    """A corrupt data_keys.enc must not look like a fresh install."""
    dk.get_data_key("file_store_encryption_key")          # create the store
    dk._ring().enc_path.write_bytes(b"garbage-not-a-valid-payload")
    dk._ring()._dek_cache = None

    with pytest.raises(RuntimeError, match="cannot be opened"):
        dk.get_data_key("file_store_encryption_key")


# ── the way back ────────────────────────────────────────────────────────────────

def test_the_backup_is_written_before_the_first_copy_is_blanked(cfg, monkeypatch, tmp_path):
    """MUTATION: blank first, back up later -> the recorded order flips and fails."""
    import vaf.core.data_keyring as module

    events = []
    real_set = Config.set

    monkeypatch.setattr(module, "ensure_pre_migration_backup",
                        lambda: events.append("backup"))
    monkeypatch.setattr(Config, "set", classmethod(
        lambda cls, k, v: (events.append(f"blank:{k}"), real_set(k, v))[1]))

    cfg.seed("memory_encryption_key", base64.b64encode(secrets.token_bytes(32)).decode())
    dk.get_data_key("memory_encryption_key", legacy_config_key="memory_encryption_key")

    assert events.index("backup") < events.index("blank:memory_encryption_key")


def test_a_fresh_install_mints_into_the_ring_and_leaves_config_alone(cfg):
    key = dk.get_data_key("file_store_encryption_key")

    assert len(key) == 32
    assert dk.peek_data_secret("file_store_encryption_key")
    assert cfg.written == {}


# ── status surface ──────────────────────────────────────────────────────────────

def test_status_reports_the_kek_backend_and_leftover_legacy_entries(cfg):
    cfg.seed("mail_store_encryption_key", base64.b64encode(secrets.token_bytes(32)).decode())
    dk.get_data_key("memory_encryption_key")

    status = dk.ring_status()

    assert status["kek_backend"] in ("keyring", "file", "config", "none")
    assert "memory_encryption_key" in status["entries"]
    assert "mail_store_encryption_key" in status["legacy_in_config"]
    assert status["unreadable"] is False


# ── the KEK itself ──────────────────────────────────────────────────────────────

def test_the_kek_leaves_config_json_and_keeps_existing_wraps_openable(cfg, tmp_path, monkeypatch):
    """The KEK value must be adopted, not replaced: wrapped DEKs depend on it."""
    import vaf.core.secure_store as ss
    from vaf.core.secure_store import SecureBlobStore

    # Own KEK location: the session fixture's shared one already holds a minted
    # key, and a present file legitimately wins over the legacy config value.
    monkeypatch.setattr(ss, "_kek_file_path", lambda: tmp_path / "secure_store.kek")
    monkeypatch.setattr(ss, "_kek_marker_path", lambda: tmp_path / "secure_store.kek.where")

    kek = base64.b64encode(secrets.token_bytes(32)).decode()
    cfg.seed("secure_store_kek", kek)

    store = SecureBlobStore("probe", tmp_path / "probe.enc")
    store.update(lambda d: d.__setitem__("token", "s3cret"))   # wraps under the legacy KEK

    assert ss.kek_backend() in ("keyring", "file")
    assert cfg.written["secure_store_kek"] == ""

    reopened = SecureBlobStore("probe", tmp_path / "probe.enc")
    assert reopened.load_strict()["token"] == "s3cret"


def test_a_keyring_backed_kek_that_is_unreachable_fails_loudly(cfg, tmp_path, monkeypatch):
    """Silently minting here would strand every wrapped DEK on the machine."""
    import vaf.core.secure_store as ss

    (tmp_path / "secure_store.kek.where").write_text("keyring", encoding="utf-8")
    monkeypatch.setattr(ss, "_kek_marker_path", lambda: tmp_path / "secure_store.kek.where")
    monkeypatch.setattr(ss, "_kek_file_path", lambda: tmp_path / "secure_store.kek")
    monkeypatch.setattr(ss, "_kek_from_keyring", lambda: None)

    assert ss._machine_kek(create=True) is None
    assert not (tmp_path / "secure_store.kek").exists()


# ── what the first real restart taught ──────────────────────────────────────────

def test_a_new_kek_goes_to_the_file_not_the_os_keyring_by_default(cfg, tmp_path, monkeypatch):
    """The tray has no session bus, so a keyring-only key locks the product out.

    MUTATION: prefer the keyring again and this goes red - which is exactly what
    happened on the first restart: 295 failed key resolutions and an app that
    only recovered once the file backend took over.
    """
    import vaf.core.secure_store as ss

    monkeypatch.setattr(ss, "_kek_file_path", lambda: tmp_path / "secure_store.kek")
    monkeypatch.setattr(ss, "_kek_marker_path", lambda: tmp_path / "secure_store.kek.where")
    monkeypatch.setattr(ss, "keyring_available", lambda: True)
    monkeypatch.setattr(ss, "_kek_from_keyring", lambda: None)
    written = []
    monkeypatch.setitem(__import__("sys").modules, "keyring",
                        type("K", (), {"set_password": staticmethod(lambda *a: written.append(a)),
                                       "get_password": staticmethod(lambda *a: None),
                                       "delete_password": staticmethod(lambda *a: None)})())

    assert ss._machine_kek(create=True) is not None

    assert (tmp_path / "secure_store.kek").exists()
    assert (tmp_path / "secure_store.kek.where").read_text(encoding="utf-8") == "file"
    assert written == [], "the OS keyring must not be written unless it is configured"


def test_opting_in_puts_the_kek_in_the_os_keyring(cfg, tmp_path, monkeypatch):
    import vaf.core.secure_store as ss

    cfg.seed("secure_store_kek_backend", "keyring")
    monkeypatch.setattr(ss, "_kek_file_path", lambda: tmp_path / "secure_store.kek")
    monkeypatch.setattr(ss, "_kek_marker_path", lambda: tmp_path / "secure_store.kek.where")
    monkeypatch.setattr(ss, "keyring_available", lambda: True)
    stored = {}
    monkeypatch.setitem(__import__("sys").modules, "keyring",
                        type("K", (), {"set_password": staticmethod(lambda s_, k, v: stored.__setitem__(k, v)),
                                       "get_password": staticmethod(lambda s_, k: stored.get(k)),
                                       "delete_password": staticmethod(lambda *a: None)})())

    assert ss._machine_kek(create=True) is not None

    assert stored, "the opt-in must actually use the keyring"
    assert (tmp_path / "secure_store.kek.where").read_text(encoding="utf-8") == "keyring"
    assert not (tmp_path / "secure_store.kek").exists()


def test_an_unreachable_keyring_falls_back_to_the_file_copy(cfg, tmp_path, monkeypatch):
    """A machine that HAS both must not lock itself out when the bus is missing."""
    import base64 as _b64
    import secrets as _secrets

    import vaf.core.secure_store as ss

    key = _secrets.token_bytes(32)
    (tmp_path / "secure_store.kek").write_text(_b64.b64encode(key).decode(), encoding="utf-8")
    (tmp_path / "secure_store.kek.where").write_text("keyring", encoding="utf-8")
    monkeypatch.setattr(ss, "_kek_file_path", lambda: tmp_path / "secure_store.kek")
    monkeypatch.setattr(ss, "_kek_marker_path", lambda: tmp_path / "secure_store.kek.where")
    monkeypatch.setattr(ss, "_kek_from_keyring", lambda: None)

    assert ss._machine_kek(create=True) == key


def test_an_unwritable_key_store_raises_instead_of_minting_again(cfg, tmp_path, monkeypatch):
    """295 mints in one restart. Each one a different key; one successful write
    in between would have orphaned everything encrypted before it."""
    import vaf.core.secure_store as ss

    monkeypatch.setattr(ss, "_machine_kek", lambda create=True: None)

    with pytest.raises(Exception) as excinfo:
        dk.get_data_key("file_store_encryption_key")

    assert "not reachable" in str(excinfo.value) or "cannot be opened" in str(excinfo.value)
    assert not dk.peek_data_secret("file_store_encryption_key")


def test_the_rollback_copy_survives_until_the_ring_really_holds_the_keys(cfg, tmp_path, monkeypatch):
    """An empty config.json is not proof the move worked - during the first
    restart it was briefly true while nothing could be written at all."""
    import vaf.core.secure_store as ss

    backup = tmp_path / ss.PRE_MIGRATION_BACKUP
    backup.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ss, "pre_migration_backup_path", lambda: backup)

    # config is clean, but the ring holds nothing yet
    assert ss.drop_pre_migration_backup() is False
    assert backup.exists(), "deleting it here would leave no way back"

    dk.get_data_key("memory_encryption_key")
    dk.get_data_key("mail_store_encryption_key")

    assert ss.drop_pre_migration_backup() is True
    assert not backup.exists()


def test_a_vanished_ring_is_an_emergency_not_a_fresh_install(cfg, tmp_path):
    """The hole the first live restart fell through.

    Once a key exists, an ABSENT key store means the store was lost - not that
    this is a first run. Minting there is the worst possible answer: the legacy
    copies in config.json are already blanked by that point, so the new key
    silently replaces the only one that opens the encrypted rows. It happened
    on a real machine, twice in one start, before this guard existed.

    MUTATION: drop the marker check and this mints instead of raising.
    """
    original = base64.b64encode(secrets.token_bytes(32)).decode()
    cfg.seed("memory_encryption_key", original)
    assert dk.get_data_key("memory_encryption_key", legacy_config_key="memory_encryption_key")

    # The store is gone and config.json no longer carries the fallback - exactly
    # the state a real install reaches right after a successful migration.
    dk._ring().enc_path.unlink()
    dk._ring()._dek_cache = None
    assert not str(Config.get("memory_encryption_key", "") or "")

    with pytest.raises(RuntimeError, match="recover|key store is gone"):
        dk.get_data_key("memory_encryption_key", legacy_config_key="memory_encryption_key")

    assert not dk._ring().enc_path.exists(), "nothing may be minted over a lost store"


def test_a_genuine_first_run_is_still_allowed_to_mint(cfg):
    """The guard must not turn a real fresh install into a dead end."""
    assert not dk._established_marker().exists()

    key = dk.get_data_key("file_store_encryption_key")

    assert len(key) == 32
    assert dk._established_marker().exists(), "the second run must know a ring existed"


def test_a_key_that_is_not_in_the_store_is_never_handed_out(cfg, monkeypatch):
    """THE bug behind every file encrypted with a key nobody has.

    The read-back used to fall back to the in-memory value, so a caller could
    encrypt real data with a key that lived only in that process and died with
    it. The next start then found ciphertext it could not open, and the
    refuse-to-mint guard closed the store for good.

    MUTATION: restore the `.get(name, adopted)` fallback and this goes red.
    """
    from vaf.core.secure_store import SecureBlobStore

    original = SecureBlobStore.load_strict
    calls = {"n": 0}

    def losing_readback(self):
        calls["n"] += 1
        return {} if calls["n"] > 1 else original(self)

    monkeypatch.setattr(SecureBlobStore, "load_strict", losing_readback)

    with pytest.raises(RuntimeError, match="not persisted|not in the key store"):
        dk.get_data_key("file_store_encryption_key")


def test_writing_a_secret_cannot_resurrect_a_vanished_ring(tmp_path, monkeypatch):
    """The one write path that bypassed the guard, and what it cost.

    `set_data_secret` is the only entry point that writes without going through
    `_resolve`, so it never ran `_refuse_if_the_ring_vanished`. On a machine
    whose key store is gone, `update` creates a new one - and from that moment
    the ring exists again, so every later read passes the guard while the keys
    that open the actual data are gone for good. The path is ordinary: restore
    a backup that missed the data directory, then set a password in the web UI,
    and the admin-hash mirror is the first thing that lands here.

    MUTATION: drop the guard call from set_data_secret and this goes red - a
    ring file appears where there must be none.
    """
    import vaf.core.data_keyring as dk
    from vaf.core.secure_store import SecureBlobStore

    ring_path = tmp_path / "data_keys.enc"
    monkeypatch.setattr(dk, "_store", SecureBlobStore("data_keys", ring_path))
    marker = tmp_path / "data_keys.established"
    marker.write_text("1", encoding="utf-8")
    monkeypatch.setattr(dk, "_established_marker", lambda: marker)

    assert not ring_path.exists(), "precondition: the store is gone"

    with pytest.raises(RuntimeError, match="The key store is gone"):
        dk.set_data_secret("admin_password_hash", "$argon2id$fake")

    assert not ring_path.exists(), (
        "a fresh ring was created on a machine that had lost its own - every "
        "later read now passes the guard while the real keys are unrecoverable")
