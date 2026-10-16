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
import json
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

    import re

    from pyrage import x25519

    assert not hasattr(recovery_kit, "_phrase_from_secret")
    body = text.split("## Your recovery key")[1].split("##")[0]
    # Exactly one indented secret line, and it is the age identity the wrap answers to.
    candidates = [ln.strip() for ln in body.splitlines() if ln.startswith("    ") and ln.strip()]
    assert len(candidates) == 1, f"one encoding, not {len(candidates)}"
    identity = x25519.Identity.from_str(candidates[0])
    assert str(identity.to_public()) == recovery_kit.recipient()
    assert not re.search(r"\b\d+ words\b", text), "no word-count claim survives"


def test_a_failure_to_write_the_kit_never_blocks_a_key(monkeypatch, _desktop):
    """A missing note is bad; a keyring that refuses to mint is worse."""
    def _explode():
        raise OSError("read-only filesystem")

    monkeypatch.setattr(recovery_kit, "kit_path", _explode)

    key = get_data_key("file_store_encryption_key")

    assert len(key) == 32
    assert peek_data_secret("file_store_encryption_key")


# ── version 2: the machine key travels too ─────────────────────────────────────────

def _identity_from_note(desktop):
    body = (desktop / recovery_kit.KIT_FILENAME).read_text(encoding="utf-8")
    body = body.split("## Your recovery key")[1].split("##")[0]
    return next(ln.strip() for ln in body.splitlines() if ln.startswith("    ") and ln.strip())


def _lose_the_machine():
    """No machine key, no marker, no cached keys: a reinstall."""
    import vaf.core.secure_store as ss
    _ring().wrap_path.unlink()
    _ring()._dek_cache = None
    ss._kek_file_path().unlink(missing_ok=True)
    ss._kek_marker_path().unlink(missing_ok=True)
    data_files.reset_key_cache()


def test_the_recovery_key_brings_back_the_password_stores_too(_desktop):
    """Measured on the first kit: it wrapped the key store's key alone, so a recovery
    brought the chats back and lost every API key, mail password and stored credential -
    each sealed under the machine key in a store of its own. MUTATION: leave the machine
    key out of the wrap, or drop the re-wrap from `vaf secure recover` - red."""
    from typer.testing import CliRunner

    from vaf.cli.cmd import secure
    from vaf.core.platform import Platform
    from vaf.core.secure_store import SecureBlobStore

    store = SecureBlobStore("provider_probe", Platform.data_dir() / "provider_probe.enc")
    store.update(lambda d: d.update({"openai": "sk-the-real-one"}))
    get_data_key("file_store_encryption_key")
    identity = _identity_from_note(_desktop)

    _lose_the_machine()
    with pytest.raises(Exception):
        SecureBlobStore("provider_probe", Platform.data_dir() / "provider_probe.enc").load_strict()

    result = CliRunner().invoke(secure.app, ["recover", "--key", identity])
    assert result.exit_code == 0, result.output
    assert "provider_probe" in result.output
    reopened = SecureBlobStore("provider_probe", Platform.data_dir() / "provider_probe.enc")
    assert reopened.load_strict() == {"openai": "sk-the-real-one"}


def test_a_version_1_kit_still_opens_the_key_store_and_says_what_it_does_not(_desktop):
    """An installation keeps working with the kit it has. MUTATION: drop the version 1
    branch of unwrap_keys - red."""
    import base64 as _b64
    import json as _json

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    dek = _ring()._get_dek(create=True)
    secret = _b64.b64encode(secrets.token_bytes(32)).decode()
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    key = recovery_kit._derive(secret, salt)
    recovery_kit.persist_recovery_wrap({
        "v": 1, "kdf": "scrypt", "salt": _b64.b64encode(salt).decode(),
        "n": recovery_kit._SCRYPT_N, "r": recovery_kit._SCRYPT_R, "p": recovery_kit._SCRYPT_P,
        "nonce": _b64.b64encode(nonce).decode(),
        "wrapped": _b64.b64encode(AESGCM(key).encrypt(nonce, dek, None)).decode()})

    keys = recovery_kit.unwrap_keys(secret)
    assert keys is not None and keys.dek == dek and keys.kek is None and keys.version == 1
    assert recovery_kit.kit_version() == 1 and recovery_kit.recipient() is None
    from vaf.core.security_misconfig import collect_security_findings
    assert "recovery_key_partial" in {f["code"] for f in collect_security_findings({})}
    assert _json.loads(recovery_kit.recovery_wrap_path().read_text())["v"] == 1


def test_renewing_needs_a_person_and_retires_the_old_key(_desktop, monkeypatch):
    """It writes a new secret in plain text: never from the agent's shell or a script, and
    only with the admin password. MUTATION: drop any of the three checks - red."""
    from typer.testing import CliRunner

    import vaf.cli.gate as gate
    from vaf.cli.cmd import secure
    from vaf.core.processes import AGENT_COMMAND_ENV

    get_data_key("file_store_encryption_key")
    old = _identity_from_note(_desktop)
    runner = CliRunner()

    monkeypatch.setattr(gate, "is_interactive", lambda: True)
    monkeypatch.setattr(gate, "require_admin_password", lambda force=False: True)
    monkeypatch.setenv(AGENT_COMMAND_ENV, "1")
    assert runner.invoke(secure.app, ["renew-recovery-key", "--yes"]).exit_code == 1
    monkeypatch.delenv(AGENT_COMMAND_ENV)
    monkeypatch.setattr(gate, "is_interactive", lambda: False)
    assert runner.invoke(secure.app, ["renew-recovery-key", "--yes"]).exit_code == 1
    monkeypatch.setattr(gate, "is_interactive", lambda: True)
    monkeypatch.setattr(gate, "require_admin_password", lambda force=False: False)
    assert runner.invoke(secure.app, ["renew-recovery-key", "--yes"]).exit_code == 1
    assert _identity_from_note(_desktop) == old

    monkeypatch.setattr(gate, "require_admin_password", lambda force=False: True)
    result = runner.invoke(secure.app, ["renew-recovery-key", "--yes"])
    assert result.exit_code == 0, result.output
    new = _identity_from_note(_desktop)
    assert new != old
    assert recovery_kit.unwrap_keys(old) is None
    assert recovery_kit.unwrap_keys(new).kek is not None


def test_a_renewal_that_cannot_store_its_wrap_puts_the_old_note_back(_desktop, monkeypatch):
    """The note may be the only copy of the CURRENT key: a failed renewal must not leave a
    note naming a key that opens nothing. MUTATION: skip the restore of the old note - red."""
    get_data_key("file_store_encryption_key")
    old_note = (_desktop / recovery_kit.KIT_FILENAME).read_bytes()
    old = _identity_from_note(_desktop)

    def _no_disk(doc):
        raise OSError("disk full")

    monkeypatch.setattr(recovery_kit, "persist_recovery_wrap", _no_disk)
    with pytest.raises(RuntimeError) as err:
        recovery_kit.renew_recovery_kit(_ring()._get_dek(create=False))
    assert "previous one still works" in str(err.value)
    assert (_desktop / recovery_kit.KIT_FILENAME).read_bytes() == old_note
    assert recovery_kit.unwrap_keys(old) is not None


def test_refreshing_the_wrap_puts_this_machines_key_in_for_the_same_recovery_key(_desktop,
                                                                               monkeypatch):
    """The wrap is rebuilt from its public half alone: the person's key keeps opening it,
    and what it opens is this machine's key. A version 1 wrap carries no machine key and is
    left alone. MUTATION: keep the old machine key in the refresh - red."""
    dek, old_kek, new_kek = (secrets.token_bytes(32) for _ in range(3))
    identity = recovery_kit.create_recovery_wrap(dek, old_kek)
    before = recovery_kit.recipient()
    monkeypatch.setattr(recovery_kit, "_machine_kek_for_kit", lambda: new_kek)

    assert recovery_kit.refresh_recovery_wrap(dek) is True

    keys = recovery_kit.unwrap_keys(identity)
    assert keys.dek == dek and keys.kek == new_kek
    assert recovery_kit.recipient() == before and recovery_kit.kit_covers_machine_key()

    recovery_kit.persist_recovery_wrap({"v": 1, "kdf": "scrypt"})
    assert recovery_kit.refresh_recovery_wrap(dek) is False
    assert json.loads(recovery_kit.recovery_wrap_path().read_text()) == {"v": 1, "kdf": "scrypt"}


def test_the_recovery_key_survives_a_second_machine_loss(_desktop):
    """After a recovery the stores are sealed under the new machine's key; the wrap still
    carried the first machine's, so the next loss brought back the key store and skipped
    every password store, while the command said it had worked. MUTATION: drop the refresh
    from `vaf secure recover` - red."""
    from typer.testing import CliRunner

    from vaf.cli.cmd import secure
    from vaf.core.platform import Platform
    from vaf.core.secure_store import SecureBlobStore

    path = Platform.data_dir() / "provider_probe.enc"
    SecureBlobStore("provider_probe", path).update(lambda d: d.update({"openai": "sk-kept"}))
    get_data_key("file_store_encryption_key")
    identity = _identity_from_note(_desktop)

    for loss in (1, 2):
        _lose_the_machine()
        result = CliRunner().invoke(secure.app, ["recover", "--key", identity])
        assert result.exit_code == 0, result.output
        assert SecureBlobStore("provider_probe", path).load_strict() == {"openai": "sk-kept"}, loss


def test_a_recovery_whose_wrap_could_not_be_refreshed_says_so(_desktop, monkeypatch):
    """A refresh that finds nothing to refresh (the file gone, or no longer version 2) left
    the old machine's key in place without a word. MUTATION: ignore the False - red."""
    from typer.testing import CliRunner

    from vaf.cli.cmd import secure
    get_data_key("file_store_encryption_key")
    identity = _identity_from_note(_desktop)
    _lose_the_machine()
    monkeypatch.setattr(recovery_kit, "refresh_recovery_wrap", lambda dek: False)
    result = CliRunner().invoke(secure.app, ["recover", "--key", identity])
    assert result.exit_code == 0, result.output
    assert "renew-recovery-key" in result.output
