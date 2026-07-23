# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The scanner's content-hashing facility (SHA-2 / SHA-3): strong-only algorithm
allow-list, standard-vector correctness, and a deterministic, tamper-evident
skill-folder fingerprint for later integrity use."""
import hashlib

import pytest

from vaf.skills import scanner as s


# -- algorithm allow-list ------------------------------------------------------

def test_only_strong_algos_supported():
    assert s.SUPPORTED_HASH_ALGOS == {"sha256", "sha512", "sha3_256", "sha3_512"}
    assert s.DEFAULT_HASH_ALGO == "sha256"


@pytest.mark.parametrize("spelling,canon", [
    ("sha256", "sha256"), ("SHA-256", "sha256"), ("sha2", "sha256"),
    ("sha512", "sha512"), ("sha-512", "sha512"),
    ("sha3", "sha3_256"), ("sha3-256", "sha3_256"), ("SHA3_256", "sha3_256"),
    ("sha3-512", "sha3_512"),
])
def test_algo_aliases_resolve(spelling, canon):
    assert s.resolve_hash_algo(spelling) == canon


@pytest.mark.parametrize("bad", ["md5", "sha1", "crc32", "blake2b", "", "  ", None])
def test_weak_or_unknown_algos_rejected(bad):
    with pytest.raises(ValueError):
        s.resolve_hash_algo(bad)


# -- primitives vs. published standard vectors ---------------------------------

def test_sha2_matches_known_vectors():
    assert s.hash_text("abc", "sha256") == \
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert s.hash_bytes(b"abc", "sha512") == hashlib.sha512(b"abc").hexdigest()
    assert s.hash_text("abc") == s.hash_text("abc", "sha256")  # default is sha256


def test_sha3_matches_known_vector():
    assert s.hash_text("abc", "sha3-256") == \
        "3a985da74fe225b2045c172d6bd390bd855f086e3e9d525b46bfe24511431532"


# -- skill-folder fingerprint --------------------------------------------------

def _skill(root, files, order):
    root.mkdir(parents=True, exist_ok=True)
    for name in order:
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        data = files[name]
        p.write_bytes(data if isinstance(data, bytes) else data.encode())
    return root


_FILES = {
    "SKILL.md": "---\nname: x\n---\nBody\n",
    "sub/helper.py": "print(1)\n",
    "logo.png": bytes(range(256)),   # binary is covered too, unlike the text scan
}


def test_fingerprint_is_deterministic_across_creation_order(tmp_path):
    a = _skill(tmp_path / "a", _FILES, ["SKILL.md", "sub/helper.py", "logo.png"])
    b = _skill(tmp_path / "b", _FILES, ["logo.png", "SKILL.md", "sub/helper.py"])
    assert s.hash_skill_folder(a) == s.hash_skill_folder(b)


def test_fingerprint_detects_binary_tamper(tmp_path):
    a = _skill(tmp_path / "a", _FILES, list(_FILES))
    before = s.hash_skill_folder(a)
    (a / "logo.png").write_bytes(bytes(range(256))[:-1] + b"\x00")
    assert s.hash_skill_folder(a) != before


def test_fingerprint_detects_rename_same_content(tmp_path):
    a = _skill(tmp_path / "a", _FILES, list(_FILES))
    before = s.hash_skill_folder(a)
    (a / "sub" / "helper.py").rename(a / "sub" / "renamed.py")
    assert s.hash_skill_folder(a) != before


def test_fingerprint_algo_selectable_and_sized(tmp_path):
    a = _skill(tmp_path / "a", _FILES, list(_FILES))
    assert s.hash_skill_folder(a, "sha256") != s.hash_skill_folder(a, "sha3-256")
    assert len(s.hash_skill_folder(a, "sha256")) == 64
    assert len(s.hash_skill_folder(a, "sha512")) == 128


def test_fingerprint_skips_symlink_escape(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    a = _skill(tmp_path / "a", {"SKILL.md": "x"}, ["SKILL.md"])
    try:
        (a / "leak").symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    # the escaping symlink is skipped, so the secret never enters the digest
    assert s.hash_skill_folder(a) == s.hash_skill_folder(_skill(tmp_path / "b", {"SKILL.md": "x"}, ["SKILL.md"]))
