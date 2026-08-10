# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""What a stolen disk gets: the file stores, and whether the words are in them.

The whole point of the shield is that the BYTES on disk do not contain the
conversation. So the central assertions here are deliberately crude - write a
chat, then grep the raw file for a sentence from it. If that sentence is found,
the feature does not work, whatever the unit tests of the crypto helpers say.

The other half is tolerance, because the shield must not cost anyone their
history: a plaintext file written before this existed still loads, and turning
`file_encryption_enabled` off writes plaintext again without stranding what is
already encrypted.
"""
import json

import pytest

from vaf.core import data_files
from vaf.core.session import Session, SessionManager

SECRET = "mein Wallet Seed lautet apfel banane kirsche"
SCOPE = "ab12cd34-owner"


@pytest.fixture(autouse=True)
def _fresh_key():
    data_files.reset_key_cache()
    yield
    data_files.reset_key_cache()


# ── the disk does not carry the words ───────────────────────────────────────────

def test_a_saved_chat_is_not_readable_on_disk(tmp_path):
    """MUTATION: write the plaintext bytes instead, and this goes red."""
    manager = SessionManager(storage_dir=str(tmp_path))
    session = Session(id="chat_a", name="Wallet")
    session.add_message("user", SECRET)
    path = manager.save(session)

    raw = path.read_bytes()

    assert raw.startswith(b"VAFENC1:")
    assert SECRET.encode() not in raw
    assert b"Wallet" not in raw          # not even the chat's title
    assert manager.load("chat_a").messages[0].content == SECRET


def test_the_file_mode_is_owner_only(tmp_path):
    import os
    import sys

    manager = SessionManager(storage_dir=str(tmp_path))
    path = manager.save(Session(id="chat_a"))

    if sys.platform != "win32":
        assert oct(os.stat(path).st_mode)[-3:] == "600"


def test_every_container_store_hides_its_content(tmp_path):
    """Same proof for the four stores that are not the chat file itself."""
    for name in ("archive.json", "bundle.json", "queue.json", "working_memory.json"):
        target = tmp_path / name
        data_files.write_json_atomic(target, {"history": [{"content": SECRET}]})
        assert SECRET.encode() not in target.read_bytes()
        assert data_files.read_json(target)["history"][0]["content"] == SECRET


# ── nobody loses their history ──────────────────────────────────────────────────

def test_a_plaintext_chat_from_before_the_change_still_loads(tmp_path):
    manager = SessionManager(storage_dir=str(tmp_path))
    legacy = {
        "id": "old_chat",
        "name": "Alt",
        "messages": [{"role": "user", "content": SECRET}],
        "metadata": {"user_scope_id": SCOPE},
    }
    (tmp_path / "old_chat.json").write_text(json.dumps(legacy), encoding="utf-8")

    assert manager.load("old_chat").messages[0].content == SECRET
    assert [r["id"] for r in manager.list()] == ["old_chat"]
    assert [d.get("id") for _p, d in manager.iter_owned_sessions(SCOPE)] == ["old_chat"]


def test_resaving_a_legacy_chat_encrypts_it(tmp_path):
    manager = SessionManager(storage_dir=str(tmp_path))
    (tmp_path / "old_chat.json").write_text(
        json.dumps({"id": "old_chat", "messages": [{"role": "user", "content": SECRET}]}),
        encoding="utf-8",
    )

    manager.save(manager.load("old_chat"))

    assert (tmp_path / "old_chat.json").read_bytes().startswith(b"VAFENC1:")


def test_a_wrong_key_refuses_rather_than_answering_empty(tmp_path, monkeypatch):
    """Silently returning "no messages" would look like data loss and invite a
    save that overwrites still-recoverable ciphertext."""
    import secrets

    target = tmp_path / "chat.json"
    data_files.write_json_atomic(target, {"content": SECRET})
    data_files.reset_key_cache()
    monkeypatch.setattr(data_files, "_key", lambda: secrets.token_bytes(32))

    with pytest.raises(ValueError, match="locked"):
        data_files.read_bytes(target)


# ── the switch ──────────────────────────────────────────────────────────────────

def test_turning_encryption_off_writes_plaintext_again(tmp_path, monkeypatch):
    """Embedders get both modes; the product default is on."""
    from vaf.core.config import Config

    real_get = Config.get
    monkeypatch.setattr(Config, "get", classmethod(
        lambda cls, k, d=None: False if k == "file_encryption_enabled" else real_get(k, d)))

    manager = SessionManager(storage_dir=str(tmp_path))
    session = Session(id="plain")
    session.add_message("user", SECRET)
    path = manager.save(session)

    assert SECRET.encode() in path.read_bytes()
    assert manager.load("plain").messages[0].content == SECRET


def test_files_already_encrypted_stay_readable_after_switching_off(tmp_path, monkeypatch):
    from vaf.core.config import Config

    manager = SessionManager(storage_dir=str(tmp_path))
    session = Session(id="chat_a")
    session.add_message("user", SECRET)
    manager.save(session)

    real_get = Config.get
    monkeypatch.setattr(Config, "get", classmethod(
        lambda cls, k, d=None: False if k == "file_encryption_enabled" else real_get(k, d)))

    assert manager.load("chat_a").messages[0].content == SECRET


def test_the_default_is_on():
    from vaf.core.config import Config
    assert Config.DEFAULTS["file_encryption_enabled"] is True


# ── the stores really call the primitive ────────────────────────────────────────

def test_no_store_writes_its_own_plaintext_file_any_more():
    """Mutation guard for the five converted stores.

    A store that goes back to `open(..., 'w')` writes the user's words to disk in
    the clear again, and no behavioural test elsewhere would notice.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for rel, forbidden in (
        ("vaf/core/session.py", "open(tmp_filepath, 'w'"),
        ("vaf/core/handoff_bundle.py", 'open(tmp, "w"'),
        ("vaf/core/main_persistence.py", "open(path, 'w'"),
        ("vaf/core/subagent_ipc.py", "open(file_path, 'w'"),
        ("vaf/core/context.py", "open(archive_file, 'w'"),
    ):
        src = (root / rel).read_text(encoding="utf-8")
        assert forbidden not in src, f"{rel} writes plaintext again"
        assert "data_files" in src, f"{rel} no longer uses the at-rest primitive"


# ── the enforced state ──────────────────────────────────────────────────────────

def _enforce(monkeypatch, allow: bool):
    from vaf.core.config import Config
    real = Config.get
    monkeypatch.setattr(Config, "get", classmethod(
        lambda cls, k, d=None: allow if k == "allow_plaintext_at_rest" else real(k, d)))


def test_an_enforced_store_refuses_a_plaintext_record(tmp_path, monkeypatch):
    """The downgrade path: swapping ciphertext for plaintext bypasses the AEAD
    by never presenting a ciphertext to authenticate.

    MUTATION: accept plaintext again and this goes red.
    """
    manager = SessionManager(storage_dir=str(tmp_path))
    (tmp_path / "swapped.json").write_text(
        json.dumps({"id": "swapped", "messages": [{"role": "user", "content": "injected"}]}),
        encoding="utf-8")

    _enforce(monkeypatch, False)

    with pytest.raises(ValueError, match="enforced"):
        manager.load("swapped")


def test_the_tolerant_read_is_the_default_during_migration(tmp_path):
    """Tightening must never be the state a user lands in by accident."""
    from vaf.core.config import Config

    assert Config.DEFAULTS["allow_plaintext_at_rest"] is True


def test_enforcing_does_not_break_encrypted_records(tmp_path, monkeypatch):
    manager = SessionManager(storage_dir=str(tmp_path))
    session = Session(id="chat_a")
    session.add_message("user", SECRET)
    manager.save(session)

    _enforce(monkeypatch, False)

    assert manager.load("chat_a").messages[0].content == SECRET


def test_switching_encryption_off_still_reads_what_it_writes(tmp_path, monkeypatch):
    """The two switches are not independent, and the combination is reachable.

    The sweep sets `allow_plaintext_at_rest = False` by itself after one clean
    pass, so on any migrated installation an embedder who then sets
    `file_encryption_enabled = False` writes plaintext that the reader refuses.
    The documented promise - turn it off and everything keeps working - would be
    false exactly where it is most likely to be used.

    MUTATION: drop the encryption_enabled() branch in allow_plaintext_at_rest and
    this goes red with "plaintext ... enforced".
    """
    from vaf.core.config import Config

    real = Config.get
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, k, d=None: {
        "file_encryption_enabled": False,
        "allow_plaintext_at_rest": False,      # the state the sweep leaves behind
    }.get(k, real(k, d))))

    manager = SessionManager(storage_dir=str(tmp_path))
    session = Session(id="plain")
    session.add_message("user", SECRET)
    path = manager.save(session)

    assert SECRET.encode() in path.read_bytes()
    assert manager.load("plain").messages[0].content == SECRET


def test_enforcement_still_holds_while_encryption_is_on(tmp_path, monkeypatch):
    """The escape hatch above must not become a way to disable enforcement."""
    manager = SessionManager(storage_dir=str(tmp_path))
    (tmp_path / "swapped.json").write_text(
        json.dumps({"id": "swapped", "messages": [{"role": "user", "content": "injected"}]}),
        encoding="utf-8")

    _enforce(monkeypatch, False)          # encryption stays ON, tolerance off

    with pytest.raises(ValueError, match="enforced"):
        manager.load("swapped")
