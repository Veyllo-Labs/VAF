# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Every speech-stack download is verified by a pinned digest, and Piper is unpacked safely.

The archive helpers hardened for llama.cpp (data filter, link cap, link containment
after the lift, removal that never follows a link) live in ONE place,
vaf/core/verified_download.py, and the Piper install, the Piper voices, the speaker
recognition models and the turn-end model all go through it with the digests in
vaf/core/speech_assets_pin.json. All offline: fixture archives, a fake network
(vaf.core.verified_download.requests.get), a fixture manifest (speech_pin.PIN_PATH).
"""
from __future__ import annotations

import hashlib
import io
import os
import random
import re
import tarfile
from pathlib import Path

import pytest

from vaf.core import verified_download
from vaf.core.verified_download import extract_archive

REPO = Path(__file__).resolve().parents[1]
NO_LINKS = pytest.mark.skipif(os.name == "nt", reason="tar links are not extracted as links without the symlink privilege")


def _tar(members) -> bytes:
    """A tar.gz of (name, bytes) files and (name, None, target) symbolic links."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for m in members:
            info = tarfile.TarInfo(name=m[0])
            if len(m) == 3:
                info.type, info.linkname = tarfile.SYMTYPE, m[2]
                tar.addfile(info)
            else:
                info.size = len(m[1])
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(m[1]))
    return buf.getvalue()


def _snapshot(root: Path, skip: Path | None = None) -> dict:
    """Every entry below `root` (links not followed): its kind and bytes or target."""
    out = {}
    for here, dirs, files in os.walk(root):
        here_p = Path(here)
        if skip is not None and (here_p == skip or skip in here_p.parents):
            dirs[:] = []
            continue
        for name in dirs + files:
            p = here_p / name
            if skip is not None and p == skip:
                continue
            rel = p.relative_to(root).as_posix()
            if p.is_symlink():
                out[rel] = ("link", os.readlink(p))
            elif p.is_dir():
                out[rel] = ("dir",)
            else:
                out[rel] = ("file", p.read_bytes())
    return out


# ── the helpers live in one place ─────────────────────────────────────────────────

def test_backend_keeps_no_archive_helper_of_its_own():
    """The llama.cpp launcher uses the shared helpers and keeps no copy or alias.
    MUTATION: put a private _extract_archive back into backend.py and this goes red."""
    src = (REPO / "vaf" / "core" / "backend.py").read_text(encoding="utf-8")
    for pattern in (r"def _?extract_archive\b", r"def _?refuse_escaping_links\b", r"def _?remove_tree_or_link\b",
                    r"def _?link_leaves_folder\b", r"^MAX_ARCHIVE_LINKS\s*=", r"_extract_archive\s*=",
                    r"_remove_tree_or_link\s*=", r"\.extractall\("):
        assert not re.search(pattern, src, re.MULTILINE), pattern
    assert "verified_download.extract_archive(" in src
    from vaf.core import backend
    for name in ("_extract_archive", "_refuse_escaping_links", "_remove_tree_or_link"):
        assert not hasattr(backend.ServerManager, name), name
    assert not hasattr(backend, "MAX_ARCHIVE_LINKS") and not hasattr(backend, "_link_leaves_folder")


# ── the lift ──────────────────────────────────────────────────────────────────────

@NO_LINKS
def test_a_folder_named_like_its_program_is_lifted_whole(tmp_path):
    """Piper keeps its program in a folder of the same name (piper/piper). Moving that
    entry to the top used to remove the folder being lifted, program and all (measured:
    FileNotFoundError, libraries left, no program). MUTATION: drop the move-aside step and
    move the entries straight out of piper/ again: red."""
    into = tmp_path / "piper.staging"
    into.mkdir()
    archive = into / "piper_linux_x86_64.tar.gz"
    archive.write_bytes(_tar([
        ("piper/piper", b"\x7fELF program"),
        ("piper/libespeak-ng.so.1.52.0.1", b"lib"),
        ("piper/libespeak-ng.so.1", None, "libespeak-ng.so.1.52.0.1"),
        ("piper/libespeak-ng.so", None, "libespeak-ng.so.1"),
        ("piper/libonnxruntime.so.1.14.1", b"ort"),
        ("piper/libonnxruntime.so", None, "libonnxruntime.so.1.14.1"),
        ("piper/espeak-ng-data/phontab", b"phonemes"),
        ("piper/espeak-ng-data/voices/de", b"voice"),
    ]))
    extract_archive(str(archive), str(into), program="piper", what="Piper")
    program = into / "piper"
    assert program.is_file() and not program.is_symlink() and program.read_bytes() == b"\x7fELF program"
    assert os.readlink(into / "libespeak-ng.so.1") == "libespeak-ng.so.1.52.0.1"
    assert os.readlink(into / "libespeak-ng.so") == "libespeak-ng.so.1"
    assert (into / "libespeak-ng.so").read_bytes() == b"lib", "the chain resolves inside"
    assert os.readlink(into / "libonnxruntime.so") == "libonnxruntime.so.1.14.1"
    assert (into / "espeak-ng-data" / "phontab").read_bytes() == b"phonemes"
    assert (into / "espeak-ng-data" / "voices" / "de").read_bytes() == b"voice"
    assert not archive.exists(), "the archive is removed"
    assert not [p for p in into.iterdir() if p.name.startswith(".lift-")]


def test_a_zip_with_its_program_in_a_folder_of_the_same_name_is_lifted(tmp_path):
    """Windows' archive: piper/piper.exe in a zip. The same lift, no links."""
    import zipfile
    into = tmp_path / "staging"
    into.mkdir()
    archive = into / "piper_windows_amd64.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("piper/piper.exe", b"MZ program")
        z.writestr("piper/onnxruntime.dll", b"dll")
        z.writestr("piper/piper.exe.d/x", b"x")
    extract_archive(str(archive), str(into), program="piper.exe", what="Piper")
    assert (into / "piper.exe").read_bytes() == b"MZ program"
    assert (into / "onnxruntime.dll").read_bytes() == b"dll"
    assert (into / "piper.exe.d" / "x").read_bytes() == b"x"
    assert sorted(p.name for p in into.iterdir()) == ["onnxruntime.dll", "piper.exe", "piper.exe.d"]


@NO_LINKS
@pytest.mark.parametrize("member", [("piper/evil", None, "/etc/passwd"), ("piper/evil", None, "../../x")],
                         ids=["absolute", "climbs-after-lift"])
def test_the_word_in_the_message_names_the_folder(tmp_path, member):
    into = tmp_path / "staging"
    into.mkdir()
    archive = into / "a.tar.gz"
    archive.write_bytes(_tar([("piper/piper", b"p"), member]))
    with pytest.raises((RuntimeError, tarfile.TarError)) as e:
        extract_archive(str(archive), str(into), program="piper", what="Piper")
    if isinstance(e.value, RuntimeError):
        assert "Piper folder" in str(e.value) and "backend" not in str(e.value)


# ── the lift, fuzzed ──────────────────────────────────────────────────────────────

_ALPHABET = ("piper", "bin", "lib.so", "lib.so.1", "d", "x")


def _draw_archive(rng: random.Random):
    """A random archive: 1 to 12 members, depth 0 to 3, files and relative links whose
    targets are in-archive names with 0 to 3 leading "..", exactly one program member
    (a regular file named piper). Folders may be called piper too. No path is both a
    file and a folder, so the tree is consistent and the oracle order-independent."""
    entries: dict[str, tuple] = {}

    def free(path: str) -> bool:
        parts = path.split("/")
        if path in entries:
            return False
        for i in range(1, len(parts)):                  # no file or link above it
            if "/".join(parts[:i]) in entries:
                return False
        prefix = path + "/"
        return not any(k.startswith(prefix) for k in entries)   # nothing below it

    def draw_dirs() -> list[str]:
        return [rng.choice(_ALPHABET) for _ in range(rng.randint(0, 3))]

    while True:
        program = "/".join(draw_dirs() + ["piper"])
        if program != "piper" or rng.random() < 0.2:
            break
    entries[program] = ("file", rng.randbytes(rng.randint(1, 16)))
    for _ in range(rng.randint(0, 11)):
        name = rng.choice([n for n in _ALPHABET if n != "piper"])
        path = "/".join(draw_dirs() + [name])
        if not free(path):
            continue
        if rng.random() < 0.4:
            existing = list(entries)
            target = "../" * rng.randint(0, 3) + (rng.choice(existing).split("/")[-1] if rng.random() < 0.5
                                                   else rng.choice(existing))
            entries[path] = ("link", target)
        else:
            entries[path] = ("file", rng.randbytes(rng.randint(0, 16)))
    members = []
    for path, kind in entries.items():
        members.append((path, kind[1]) if kind[0] == "file" else (path, None, kind[1]))
    rng.shuffle(members)
    return program, entries, members


def _check_draw(tmp_path: Path, draw: int, program: str, entries: dict, members) -> None:
    base = tmp_path / f"d{draw}"
    into = base / "staging"
    sentinel = base / "sentinel"
    into.mkdir(parents=True)
    sentinel.mkdir()
    (sentinel / "keep").write_bytes(b"keep")
    (base / "lib.so").write_bytes(b"outside")
    archive = into / "piper.tar.gz"
    archive.write_bytes(_tar(members))
    before = _snapshot(base, skip=into)
    try:
        extract_archive(str(archive), str(into), program="piper", what="Piper")
    except (RuntimeError, tarfile.TarError) as e:
        assert _snapshot(base, skip=into) == before, (draw, members, e)
        return
    assert _snapshot(base, skip=into) == before, (draw, members)
    top = into / "piper"
    assert top.is_file() and not top.is_symlink(), (draw, members)
    assert top.read_bytes() == entries[program][1], (draw, members)
    folder = program.rsplit("/", 1)[0] if "/" in program else ""
    if folder:
        for path, kind in entries.items():
            if kind[0] == "file" and "/" in path and path.rsplit("/", 1)[0] == folder:
                name = path.rsplit("/", 1)[1]
                assert (into / name).read_bytes() == kind[1], (draw, path, members)
    real_into = os.path.realpath(into)
    for here, dirs, files in os.walk(into):
        for name in dirs + files:
            p = os.path.join(here, name)
            assert not name.startswith(".lift-"), (draw, members)
            if os.path.islink(p):
                real = os.path.realpath(p)
                assert os.path.commonpath([real, real_into]) == real_into, (draw, p, members)


@NO_LINKS
def test_a_link_through_a_file_is_refused_as_an_archive_error(tmp_path):
    """Found by the fuzz below (seed 404, draw 69): a link whose target runs through a
    regular file (x/l -> ../sub/file/x). The data filter resolves it strictly and raised a
    bare NotADirectoryError on Python 3.13, an OSError instead of the archive error the
    unpack promises. MUTATION: let the OSError through again and this goes red."""
    into = tmp_path / "staging"
    into.mkdir()
    archive = into / "a.tar.gz"
    archive.write_bytes(_tar([("piper/piper", b"p"), ("piper/sub/file", b"f"),
                              ("piper/a/b/l", None, "../../sub/file/x")]))
    try:
        extract_archive(str(archive), str(into), program="piper", what="Piper")
    except (RuntimeError, tarfile.TarError):
        pass                              # refused, as an archive error
    else:                                 # a Python whose filter accepts the dangling link
        assert os.path.islink(into / "a" / "b" / "l")


@NO_LINKS
def test_the_lift_holds_its_invariants_on_random_archives(tmp_path):
    """Seeded fuzz of extract_archive (a path transform: lift plus link containment).
    Either the archive is refused and nothing beside the unpack folder changed, or the
    program is a regular file at the top with its bytes, every regular sibling of it is at
    the top with its bytes, every link resolves inside the folder, and no .lift- folder is
    left. VAF_FUZZ_DRAWS raises the draw count outside the suite.
    MUTATION: drop the move-aside step (red: piper/piper loses the program) or the check
    after the lift (red: a link climbs out)."""
    rng = random.Random(404)
    draws = int(os.environ.get("VAF_FUZZ_DRAWS", "200"))
    for draw in range(draws):
        program, entries, members = _draw_archive(rng)
        _check_draw(tmp_path, draw, program, entries, members)


# ── verified files ────────────────────────────────────────────────────────────────

def _fake_get(monkeypatch, payload: bytes, calls: list | None = None):
    def fake_get(url, **kwargs):
        if calls is not None:
            calls.append(url)

        class R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def raise_for_status(self): pass
            def iter_content(self, chunk_size):
                for i in range(0, len(payload), chunk_size):
                    yield payload[i:i + chunk_size]
        return R()
    monkeypatch.setattr("vaf.core.verified_download.requests.get", fake_get)


def _no_download(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("no download")
    monkeypatch.setattr("vaf.core.verified_download.requests.get", refuse)


def test_a_verified_file_is_hashed_once_per_process(monkeypatch, tmp_path):
    """A 114 MB voice is read once per process, not at every reply. MUTATION: drop the
    memo and the second call hashes again."""
    monkeypatch.setattr(verified_download, "_verified_files", set())
    data = b"voice" * 1000
    dest = tmp_path / "v.onnx"
    dest.write_bytes(data)
    hashed = []
    real = verified_download.file_sha256
    monkeypatch.setattr(verified_download, "file_sha256", lambda p, *a: hashed.append(p) or real(p, *a))
    _no_download(monkeypatch)
    digest = hashlib.sha256(data).hexdigest()
    for _ in range(3):
        assert verified_download.fetch_verified("https://example.invalid/v", digest, dest, size=len(data)) == dest
    assert len(hashed) == 1
    dest.write_bytes(data[:-1] + b"X")          # same size, other bytes, new mtime
    os.utime(dest, ns=(1, 1))
    assert not verified_download.has_pinned_digest(dest, digest, len(data))


def test_fetch_verified_replaces_a_truncated_file_and_never_a_good_one(monkeypatch, tmp_path):
    data = b"model bytes" * 100
    digest = hashlib.sha256(data).hexdigest()
    dest = tmp_path / "m.onnx"
    dest.write_bytes(data[:50])                 # an interrupted transfer of an older VAF
    calls = []
    _fake_get(monkeypatch, data, calls)
    verified_download.fetch_verified("https://example.invalid/m", digest, dest, size=len(data))
    assert dest.read_bytes() == data and len(calls) == 1
    _fake_get(monkeypatch, b"other", calls)
    verified_download.fetch_verified("https://example.invalid/m", digest, dest, size=len(data))
    assert dest.read_bytes() == data and len(calls) == 1, "a file with the pinned digest is kept"


@pytest.mark.skipif(os.name == "nt", reason="symlink privilege")
def test_a_link_never_counts_as_a_verified_file(monkeypatch, tmp_path):
    data = b"model"
    real = tmp_path / "real"
    real.write_bytes(data)
    os.symlink(real, tmp_path / "link")
    assert not verified_download.has_pinned_digest(tmp_path / "link", hashlib.sha256(data).hexdigest())
