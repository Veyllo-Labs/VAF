# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The way back in after a reinstall.

Machine-held keys make one failure absolute: lose the machine, lose the data,
with nobody able to help. The recovery key is the second door, so the test
that matters is the whole journey - encrypt something, throw the machine key
away, and open the data again with nothing but that key and the two key files.
Anything less than that end-to-end run does not prove a recovery path exists.
"""
import base64
import secrets

import pytest

from vaf.core import data_files, recovery_kit
from vaf.core.data_keyring import _ring, get_data_key, peek_data_secret

SECRET_CHAT = "die Seed Phrase liegt im Safe"


@pytest.fixture(autouse=True)
def _desktop(tmp_path, monkeypatch):
    """A throwaway Desktop: the kit must never land on the developer's real one."""
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    monkeypatch.setattr(recovery_kit, "kit_path", lambda: desktop / recovery_kit.KIT_FILENAME)
    data_files.reset_key_cache()
    yield desktop
    data_files.reset_key_cache()


def test_the_kit_appears_when_the_first_key_is_created(_desktop):
    get_data_key("file_store_encryption_key")

    note = _desktop / recovery_kit.KIT_FILENAME
    assert note.exists(), "a machine-held key without a way back is the whole problem"
    assert recovery_kit.recovery_wrap_path().exists()


def test_the_note_says_what_it_is_before_it_says_anything_else(_desktop):
    get_data_key("file_store_encryption_key")
    text = (_desktop / recovery_kit.KIT_FILENAME).read_text(encoding="utf-8")

    head = text[:400]
    assert "is a key" in head and "Move it off" in head
    # And it must not promise more than it delivers.
    assert "alone is not enough" in text
    assert "vaf secure recover" in text


def test_the_recovery_key_opens_the_data_after_the_machine_key_is_gone(_desktop, tmp_path):
    """The journey: encrypt, lose the machine key, come back with the recovery key."""
    import vaf.core.secure_store as ss

    target = tmp_path / "chat.json"
    data_files.write_json_atomic(target, {"content": SECRET_CHAT})
    assert SECRET_CHAT.encode() not in target.read_bytes()

    secret = recovery_kit.create_recovery_wrap(_ring()._get_dek(create=True))

    # The machine is gone: no KEK, no cached DEK, no wrapped-DEK file.
    _ring().wrap_path.unlink()
    _ring()._dek_cache = None
    ss._kek_file_path().unlink(missing_ok=True)
    ss._kek_marker_path().unlink(missing_ok=True)
    with pytest.raises(Exception):
        _ring().load_strict()

    dek = recovery_kit.unwrap_with_secret(secret)
    assert dek is not None

    _ring()._wrap_and_store_dek(dek)
    _ring()._dek_cache = dek
    data_files.reset_key_cache()

    assert data_files.read_json(target)["content"] == SECRET_CHAT


def test_a_wrong_recovery_key_recovers_nothing(_desktop):
    recovery_kit.create_recovery_wrap(_ring()._get_dek(create=True))

    wrong = base64.b64encode(secrets.token_bytes(32)).decode()

    assert recovery_kit.unwrap_with_secret(wrong) is None


def test_the_kit_is_written_once_and_not_rotated_behind_the_users_back(_desktop):
    """A second key would silently invalidate the copy the user filed away."""
    get_data_key("file_store_encryption_key")
    first = (_desktop / recovery_kit.KIT_FILENAME).read_text(encoding="utf-8")

    get_data_key("mail_store_encryption_key")

    assert (_desktop / recovery_kit.KIT_FILENAME).read_text(encoding="utf-8") == first


def test_the_note_ships_exactly_one_encoding_of_the_secret(_desktop):
    """It used to print 24 words AND base64 and call the words 256 bits.

    Six bits per word over a 64-word list is 144, there was no checksum, and no
    comparable product ships two encodings of one secret. Now: one string.
    """
    get_data_key("file_store_encryption_key")
    text = (_desktop / recovery_kit.KIT_FILENAME).read_text(encoding="utf-8")

    import base64 as _b64
    import re

    assert not hasattr(recovery_kit, "_phrase_from_secret")
    body = text.split("## Your recovery key")[1].split("##")[0]
    # Exactly one indented secret line, and it decodes to the 32 bytes claimed.
    candidates = [ln.strip() for ln in body.splitlines() if ln.startswith("    ") and ln.strip()]
    assert len(candidates) == 1, f"one encoding, not {len(candidates)}"
    assert len(_b64.b64decode(candidates[0])) == 32
    assert not re.search(r"\b\d+ words\b", text), "no word-count claim survives"


def test_a_failure_to_write_the_kit_never_blocks_a_key(monkeypatch, _desktop):
    """A missing note is bad; a keyring that refuses to mint is worse."""
    def _explode():
        raise OSError("read-only filesystem")

    monkeypatch.setattr(recovery_kit, "kit_path", _explode)

    key = get_data_key("file_store_encryption_key")

    assert len(key) == 32
    assert peek_data_secret("file_store_encryption_key")
