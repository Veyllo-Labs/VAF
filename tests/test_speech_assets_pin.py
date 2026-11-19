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
import shutil
import stat
import tarfile
import threading
from pathlib import Path
from types import SimpleNamespace

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
    """The llama.cpp launcher uses the shared helpers and keeps no copy or alias, and the
    staging folder of a failed install is removed through the shared removal (a raw rmtree
    skips a folder without its owner's rights). MUTATIONS: put a private _extract_archive
    back into backend.py, or one raw rmtree of the staging folder: each goes red."""
    src = (REPO / "vaf" / "core" / "backend.py").read_text(encoding="utf-8")
    for pattern in (r"def _?extract_archive\b", r"def _?refuse_escaping_links\b", r"def _?remove_tree_or_link\b",
                    r"def _?link_leaves_folder\b", r"^MAX_ARCHIVE_LINKS\s*=", r"_extract_archive\s*=",
                    r"_remove_tree_or_link\s*=", r"\.extractall\("):
        assert not re.search(pattern, src, re.MULTILINE), pattern
    assert "verified_download.extract_archive(" in src
    assert "shutil.rmtree(" not in src, "the install removes its folders through verified_download"
    assert "verified_download.remove_quietly(staging)" in src
    speech_src = (REPO / "vaf" / "core" / "speech.py").read_text(encoding="utf-8")
    assert "def _remove_quietly" not in speech_src and "verified_download.remove_quietly(" in speech_src
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


# ── the removal of a leftover ────────────────────────────────────────────────────

POSIX_MODES = pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                                 reason="folder modes are POSIX, and root ignores them")


def _mode(p) -> int:
    return stat.S_IMODE(os.lstat(p).st_mode)


def _remove_bounded(path, seconds: float = 10.0):
    """remove_tree_or_link in a thread, so a walk that never ends fails the test instead of
    hanging the suite. Returns the exception it raised, or None."""
    outcome = {}

    def run():
        try:
            verified_download.remove_tree_or_link(str(path))
            outcome["error"] = None
        except BaseException as e:        # handed to the test, which decides
            outcome["error"] = e

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), f"the removal of {path} did not return within {seconds} s"
    return outcome["error"]


def _outside(p: Path) -> tuple:
    st = os.lstat(p)
    return stat.S_IMODE(st.st_mode), st.st_ino, (p.read_bytes() if stat.S_ISREG(st.st_mode) else None)


@POSIX_MODES
def test_a_folder_without_its_owner_bits_is_removed(tmp_path):
    """A hard link member over an in-archive folder link makes tarfile chmod that folder to
    the member's 0644; rmtree(ignore_errors) then left it behind and every later install
    failed on the leftover. Real folders get their owner's rwx back, links are not followed.
    MUTATIONS: drop the second pass (red: OSError); lstat replaced by stat in the walk (red:
    the outside folder becomes 0o700)."""
    parent = tmp_path / "bin"
    parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_bytes(b"keep")
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(b"sentinel")
    top = parent / "staging"
    (top / "a" / "b").mkdir(parents=True)
    (top / "a" / "b" / "f").write_bytes(b"f")
    (top / "zero").mkdir()
    (top / "zero" / "f").write_bytes(b"")
    (top / "ro").mkdir()
    (top / "ro" / "f").write_bytes(b"")
    os.symlink(outside, top / "a" / "out")
    os.link(sentinel, top / "ro" / "hard")
    os.chmod(sentinel, 0o444)
    os.chmod(outside, 0o500)
    os.chmod(parent, 0o750)
    before = {p: _outside(p) for p in (outside, outside / "keep", sentinel, parent)}
    for p, mode in ((top / "a" / "b", 0o600), (top / "a", 0o644), (top / "zero", 0o000),
                    (top / "ro", 0o555), (top, 0o600)):
        os.chmod(p, mode)
    try:
        assert _remove_bounded(top) is None
        assert not os.path.lexists(top)
        assert {p: _outside(p) for p in before} == before, "nothing outside the folder changed"
    finally:
        os.chmod(outside, 0o700)


@POSIX_MODES
def test_what_cannot_be_removed_still_raises_and_the_parent_keeps_its_mode(tmp_path):
    """The parent of the folder is never changed, so a parent without write still refuses,
    and the caller hears about it. MUTATION: start the walk at the parent (red)."""
    parent = tmp_path / "bin"
    (parent / "staging" / "x").mkdir(parents=True)
    os.chmod(parent / "staging" / "x", 0o000)
    os.chmod(parent, 0o555)
    try:
        error = _remove_bounded(parent / "staging")
        assert isinstance(error, OSError) and "could not be removed completely" in str(error)
        assert _mode(parent) == 0o555
    finally:
        os.chmod(parent, 0o755)


@POSIX_MODES
def test_a_link_cycle_does_not_hold_up_the_removal(tmp_path):
    """MUTATION: follow links in the walk (stat instead of lstat): two links back to the top
    double the walk at every level and it never returns (red on the 10 s bound)."""
    top = tmp_path / "staging"
    (top / "a").mkdir(parents=True)
    os.symlink("..", top / "a" / "l1")
    os.symlink("..", top / "a" / "l2")
    (top / "a" / "f").write_bytes(b"f")
    os.chmod(top / "a", 0o600)
    assert _remove_bounded(top) is None
    assert not os.path.lexists(top)


@pytest.fixture
def windows_unlink(monkeypatch):
    """Windows' rule on Linux: a file whose read-only attribute is set (no owner write in its
    mode) cannot be unlinked. shutil is put on its path-based walk, which calls os.unlink."""
    real_unlink = os.unlink

    def unlink(path, *a, **k):
        st = os.lstat(path, *a, **k)
        if stat.S_ISREG(st.st_mode) and not st.st_mode & stat.S_IWUSR:
            raise PermissionError(13, "Access is denied", str(path))
        return real_unlink(path, *a, **k)

    monkeypatch.setattr(shutil, "_use_fd_functions", False)
    monkeypatch.setattr(verified_download, "_WINDOWS", True)
    monkeypatch.setattr(os, "unlink", unlink)


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links")
def test_on_windows_a_read_only_file_is_made_removable(tmp_path, windows_unlink):
    """Windows refuses to delete a read-only file, so the install's leftover stayed. Only a
    file no other name links to is changed: a file's mode is shared by all its links.
    MUTATIONS: skip regular files in the walk (red: the first case raises); drop the
    st_nlink == 1 guard (red: the outside link gains write)."""
    top = tmp_path / "staging"
    top.mkdir()
    (top / "ro.dll").write_bytes(b"read-only")
    os.chmod(top / "ro.dll", 0o444)
    assert _remove_bounded(top) is None
    assert not os.path.lexists(top)

    outside = tmp_path / "outside.dll"
    outside.write_bytes(b"shared")
    top.mkdir()
    os.link(outside, top / "linked.dll")
    os.chmod(outside, 0o444)
    error = _remove_bounded(top)
    assert isinstance(error, OSError)
    assert _mode(outside) == 0o444, "the other name of the file kept its read-only bit"


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links")
def test_on_windows_a_read_only_file_at_the_root_is_removed(tmp_path, windows_unlink):
    """The swap moves a plain file at bin/llama-cpp aside, and a later step removes it as
    .previous or .staging; that removal unlinked it directly, which Windows refuses for a
    read-only file, so the leftover blocked the next install. A file with a second name and
    a link are never changed. MUTATIONS: drop the retry (red: the single-link file stays);
    make the file writable whatever its link count (red: the other name gains write)."""
    single = tmp_path / "llama-cpp.staging"
    single.write_bytes(b"a read-only file where the folder belongs")
    os.chmod(single, 0o444)
    assert _remove_bounded(single) is None
    assert not os.path.lexists(single)

    outside = tmp_path / "elsewhere"
    outside.write_bytes(b"shared")
    shared = tmp_path / "llama-cpp.previous"
    os.link(outside, shared)
    os.chmod(outside, 0o444)
    error = _remove_bounded(shared)
    assert isinstance(error, OSError)
    assert _mode(outside) == 0o444 and os.path.lexists(shared), "a file with another name is left alone"

    if hasattr(os, "symlink"):
        link = tmp_path / "linked"
        os.symlink(outside, link)
        assert _remove_bounded(link) is None
        assert not os.path.lexists(link) and _mode(outside) == 0o444, "a link is unlinked, never followed"


@POSIX_MODES
def test_on_windows_a_junction_is_not_entered(tmp_path, monkeypatch):
    """A directory junction is a reparse point lstat reports as a folder; it is neither
    changed nor entered, like a link. Played here by a real folder whose lstat carries the
    reparse attribute. MUTATION: drop the reparse check (red: the folder behind it is
    changed and removed)."""
    top = tmp_path / "staging"
    behind = top / "junction" / "inner"
    behind.mkdir(parents=True)
    (behind / "f").write_bytes(b"f")
    os.chmod(behind, 0o500)
    real_lstat = os.lstat

    def lstat(path, *a, **k):
        st = real_lstat(path, *a, **k)
        if not a and not k and os.fspath(path) == str(top / "junction"):
            return SimpleNamespace(st_mode=st.st_mode, st_nlink=st.st_nlink, st_ino=st.st_ino,
                                   st_file_attributes=0x400)
        return st

    monkeypatch.setattr(shutil, "_use_fd_functions", False)
    monkeypatch.setattr(verified_download, "_WINDOWS", True)
    monkeypatch.setattr(os, "lstat", lstat)
    try:
        error = _remove_bounded(top)
        monkeypatch.setattr(os, "lstat", real_lstat)
        assert isinstance(error, OSError)
        assert _mode(behind) == 0o500, "nothing behind the junction was changed"
    finally:
        os.chmod(behind, 0o700)


_FOLDER_MODES = (0o000, 0o100, 0o300, 0o400, 0o444, 0o500, 0o555, 0o600, 0o644, 0o700, 0o755)


def _draw_tree(rng: random.Random, top: Path, out: Path, sentinel: Path, outfile: Path) -> list:
    """A random leftover at `top`: 1 to 40 entries, at most 5 folders deep, of folders,
    files (0o000, 0o444, 0o644), hard links to the outside sentinel, and symbolic links to
    the outside folder, the outside file, a folder of the tree (cycles included) or nothing.
    Every folder, the top included, then gets a drawn mode, bottom-up. Returns the draw."""
    top.mkdir()
    folders = [(top, 0)]
    drawn = []
    for i in range(rng.randint(1, 40)):
        here, depth = rng.choice([f for f in folders if f[1] < 5])
        p = here / f"e{i}"
        kind = rng.choice(("folder", "file", "hard", "out-folder", "out-file", "inside", "missing"))
        if kind == "folder":
            p.mkdir()
            folders.append((p, depth + 1))
        elif kind == "file":
            p.write_bytes(rng.randbytes(rng.randint(0, 8)))
            os.chmod(p, rng.choice((0o000, 0o444, 0o644)))
        elif kind == "hard":
            os.link(sentinel, p)
        elif kind == "out-folder":
            os.symlink(out, p)
        elif kind == "out-file":
            os.symlink(outfile, p)
        elif kind == "inside":
            target = rng.choice(folders)[0]
            os.symlink(os.path.relpath(target, here) if rng.random() < 0.5 else target, p)
        else:
            os.symlink(here / "nowhere", p)
        drawn.append((p.relative_to(top).as_posix(), kind))
    for folder, _ in sorted(folders, key=lambda f: -f[1]):
        mode = rng.choice(_FOLDER_MODES)
        os.chmod(folder, mode)
        drawn.append((folder.relative_to(top.parent).as_posix(), oct(mode)))
    return drawn


@POSIX_MODES
@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links")
def test_removal_holds_its_invariants_on_random_trees(tmp_path):
    """Seeded fuzz of remove_tree_or_link (a removal that must stay inside its folder). For
    every drawn tree the call returns, the folder is gone, and the outside folder, its file,
    the outside file, the hard-linked sentinel and the parent keep mode, inode and bytes.
    VAF_FUZZ_DRAWS raises the draw count outside the suite. MUTATIONS: drop the second pass
    (red: a folder without x stays); follow links in the walk (red: the outside folder
    gains write, or a cycle never returns)."""
    rng = random.Random(405)
    draws = int(os.environ.get("VAF_FUZZ_DRAWS", "200"))
    for draw in range(draws):
        base = tmp_path / f"r{draw}"
        parent = base / "parent"
        out = base / "out"
        parent.mkdir(parents=True)
        out.mkdir()
        (out / "keep").write_bytes(b"keep")
        sentinel = base / "sentinel"
        sentinel.write_bytes(b"sentinel")
        outfile = base / "outfile"
        outfile.write_bytes(b"outfile")
        os.chmod(outfile, 0o444)
        top = parent / "staging"
        drawn = _draw_tree(rng, top, out, sentinel, outfile)
        os.chmod(sentinel, 0o444)
        os.chmod(out, 0o500)
        os.chmod(parent, 0o750)
        watched = (out, out / "keep", sentinel, outfile, parent)
        before = {p: _outside(p) for p in watched}
        try:
            error = _remove_bounded(top)
            assert error is None, (draw, error, drawn)
            assert not os.path.lexists(top), (draw, drawn)
            assert {p: _outside(p) for p in watched} == before, (draw, drawn)
        finally:
            os.chmod(out, 0o700)


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


def test_two_downloads_of_one_file_at_once_cannot_mix_their_bytes(monkeypatch, tmp_path):
    """Two processes fetch the same voice at once: A gets the pinned bytes, B other ones.
    Both used to write the one shared <name>.part, so B's bytes ended up in the file A
    had renamed into place. Now each call has its own partial file: the destination only
    ever holds the pinned bytes, and B's failure does not touch A's result.
    MUTATION: a shared partial name (target + ".part") again: red."""
    import threading
    good, bad = b"G" * 4096, b"B" * 4096
    digest = _sha(good)
    dest = tmp_path / "voice.onnx"
    both_open = threading.Barrier(2, timeout=5)
    a_done = threading.Event()
    seen = []

    def fake_get(url, **kwargs):
        payload = good if url.endswith("/a") else bad

        class R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def raise_for_status(self): pass
            def iter_content(self, chunk_size):
                yield payload[:1024]
                both_open.wait()
                if payload is bad:
                    assert a_done.wait(5)
                    seen.append(dest.read_bytes() if dest.exists() else None)
                yield payload[1024:]
        return R()

    monkeypatch.setattr("vaf.core.verified_download.requests.get", fake_get)
    results = {}

    def run(name):
        try:
            results[name] = verified_download.download_verified(f"https://example.invalid/{name}", digest, dest,
                                                                size=len(good))
        except Exception as e:      # noqa: BLE001 - the outcome is what is asserted
            results[name] = e
        finally:
            if name == "a":
                a_done.set()

    threads = [threading.Thread(target=run, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert results["a"] == dest, results
    assert isinstance(results["b"], verified_download.DownloadIntegrityError), results
    assert seen == [good], "right after A returned, the destination held the pinned bytes"
    assert dest.read_bytes() == good
    assert [p.name for p in tmp_path.iterdir()] == ["voice.onnx"], "no partial file is left"


def test_a_stream_longer_than_the_pinned_size_is_cut_off(monkeypatch, tmp_path):
    """MUTATION: ignore the size and the whole stream is read."""
    pinned = b"x" * 100
    read = []

    def fake_get(url, **kwargs):
        class R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def raise_for_status(self): pass
            def iter_content(self, chunk_size):
                for _ in range(1000):
                    read.append(1)
                    yield b"y" * 50
        return R()

    monkeypatch.setattr("vaf.core.verified_download.requests.get", fake_get)
    with pytest.raises(verified_download.DownloadIntegrityError, match="more than the pinned 100 bytes"):
        verified_download.download_verified("https://example.invalid/x", _sha(pinned), tmp_path / "x", size=100)
    assert len(read) == 3 and list(tmp_path.iterdir()) == []


def test_a_replaced_file_with_the_old_time_is_hashed_again(monkeypatch, tmp_path):
    """A file replaced by other bytes of the same size, its modification time set back,
    must not ride on the memo of the verified one: the change time and the inode differ.
    MUTATION: key the memo on path, size and modification time only: red."""
    monkeypatch.setattr(verified_download, "_verified_files", set())
    good = b"a" * 64
    dest = tmp_path / "m.onnx"
    dest.write_bytes(good)
    assert verified_download.has_pinned_digest(dest, _sha(good), 64)
    st = os.stat(dest)
    other = tmp_path / "other"
    other.write_bytes(b"b" * 64)
    os.replace(other, dest)
    os.utime(dest, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert not verified_download.has_pinned_digest(dest, _sha(good), 64)


def test_a_partial_file_left_by_a_killed_download_is_swept(monkeypatch, tmp_path):
    """A download killed hard leaves its own .<name>.<random>.part (up to the pinned size).
    The next download of the same file removes the ones older than an hour and leaves a
    fresh one (a download still running) and other files alone. MUTATION: drop the
    sweep: red (the stale file stays)."""
    import time as _time
    good = b"g" * 64
    dest = tmp_path / "voice.onnx"
    stale = tmp_path / ".voice.onnx.k1ll3d.part"
    fresh = tmp_path / ".voice.onnx.runn1ng.part"
    other = tmp_path / ".other.onnx.k1ll3d.part"
    for p in (stale, fresh, other):
        p.write_bytes(b"x" * 10)
    old = _time.time() - verified_download.STALE_PARTIAL_SECONDS - 60
    os.utime(stale, (old, old))
    os.utime(other, (old, old))
    _fake_get(monkeypatch, good)
    verified_download.download_verified("https://example.invalid/v", _sha(good), dest, size=len(good))
    assert not stale.exists()
    assert fresh.exists() and other.exists()
    assert dest.read_bytes() == good


@pytest.mark.parametrize("size", [None, 0, -5, "100", True, 1.5],
                         ids=["none", "zero", "negative", "text", "bool", "float"])
def test_a_download_without_a_positive_pinned_size_is_refused(monkeypatch, tmp_path, size):
    """The size is required (no default) and must be a positive int: a missing or broken
    one would switch off the cap on the stream. Refused before anything is fetched, by
    both entry points. MUTATION: drop the check in _pinned_size: red."""
    _no_download(monkeypatch)
    digest = _sha(b"x")
    with pytest.raises(ValueError, match="positive int"):
        verified_download.download_verified("https://example.invalid/x", digest, tmp_path / "x", size=size)
    with pytest.raises(ValueError, match="positive int"):
        verified_download.fetch_verified("https://example.invalid/x", digest, tmp_path / "y", size=size)
    with pytest.raises(TypeError):
        verified_download.download_verified("https://example.invalid/x", digest, tmp_path / "z")
    assert list(tmp_path.iterdir()) == []


def _stalled_download(monkeypatch, tmp_path):
    """A download whose transfer stalls after its first chunk long enough for another
    download of the same file to find its partial file stale and sweep it."""
    import time as _time
    good = b"g" * 4096
    dest = tmp_path / "voice.onnx"

    def fake_get(url, **kwargs):
        class R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def raise_for_status(self): pass
            def iter_content(self, chunk_size):
                yield good[:1024]
                old = _time.time() - verified_download.STALE_PARTIAL_SECONDS - 60
                for p in tmp_path.glob(".voice.onnx.*.part"):
                    os.utime(p, (old, old))
                verified_download.sweep_stale_partials(dest)     # another download's sweep
                yield good[1024:]
        return R()

    monkeypatch.setattr("vaf.core.verified_download.requests.get", fake_get)
    return good, dest


def test_a_stalled_download_whose_partial_was_swept_fails_closed(monkeypatch, tmp_path):
    """Named boundary of the sweep: a transfer that wrote nothing for an hour can lose its
    partial file to another download of the same file. Where an open file can be removed
    (Linux, macOS) it must then fail, never leave unverified bytes at the destination;
    on Windows the open partial cannot be removed, so the transfer completes with the
    pinned bytes (that branch is also simulated on every host, below).
    MUTATION: tolerate a missing partial at the final rename (create it again and rename
    that): red."""
    good, dest = _stalled_download(monkeypatch, tmp_path)
    if os.name == "nt":
        assert verified_download.download_verified("https://example.invalid/v", _sha(good), dest,
                                                   size=len(good)) == dest
        assert dest.read_bytes() == good
        return
    with pytest.raises(OSError):
        verified_download.download_verified("https://example.invalid/v", _sha(good), dest, size=len(good))
    assert not dest.exists() and list(tmp_path.iterdir()) == []


def test_where_an_open_partial_cannot_be_removed_the_stalled_download_completes(monkeypatch, tmp_path):
    """Windows' branch of the boundary, run on every host: removing the partial file a
    download still holds open raises PermissionError (as Windows does for a file in use).
    The sweep must leave it, and the download complete with the pinned bytes.
    MUTATION: let the sweep's removal error escape: red."""
    good, dest = _stalled_download(monkeypatch, tmp_path)
    real_unlink = os.unlink

    def unlink(path, *a, **k):
        if str(path).endswith(".part") and os.path.basename(str(path)).startswith(".voice.onnx."):
            raise PermissionError(13, "The process cannot access the file because it is being used")
        return real_unlink(path, *a, **k)

    monkeypatch.setattr(os, "unlink", unlink)
    assert verified_download.download_verified("https://example.invalid/v", _sha(good), dest,
                                               size=len(good)) == dest
    assert dest.read_bytes() == good
    assert [p.name for p in tmp_path.iterdir()] == ["voice.onnx"]


def test_every_download_in_the_tree_passes_its_pinned_size():
    """The size cap only works where a caller passes the size. Every call of
    download_verified( and fetch_verified( outside the module itself, in vaf/ and the speech
    container, names size=. MUTATION: drop size= at any one caller: red."""
    calls = []
    for path in list((REPO / "vaf").rglob("*.py")) + list((REPO / "docker").rglob("*.py")):
        if path.name == "verified_download.py":
            continue
        src = path.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"\b(download_verified|fetch_verified)\(", src):
            if src[max(0, m.start() - 4):m.start()] == "def ":
                continue
            depth, i = 0, m.end() - 1
            while True:
                depth += {"(": 1, ")": -1}.get(src[i], 0)
                if depth == 0:
                    break
                i += 1
            call = src[m.start():i + 1]
            calls.append(path.relative_to(REPO).as_posix())
            assert re.search(r"\bsize=", call), f"{path.relative_to(REPO).as_posix()}: {call}"
    assert len(calls) >= 7, calls       # llama, Piper, two voice files, speaker models, turn end, container x2


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


# ── fixtures for the speech stack ─────────────────────────────────────────────────

from vaf.core import speech_pin  # noqa: E402
from vaf.core.speech import SpeechManager  # noqa: E402

ASSET = "piper_linux_x86_64.tar.gz"
REVISION = "0123456789abcdef0123456789abcdef01234567"
VOICE = "de/de_DE/mls/medium/de_DE-mls-medium.onnx"
HEX64 = re.compile(r"\b[0-9a-f]{64}\b")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _voice_row(path: str, **override) -> dict:
    """A voice row as the pin script writes it, for the voice whose .onnx is `path`."""
    row = {"licence": "CC0-1.0", "dataset": "https://example.invalid/dataset", "base": "scratch",
           "card": {"path": path.rsplit("/", 1)[0] + "/MODEL_CARD", "sha256": _sha(b"card"), "size": 4},
           "host_piper": True}
    row.update(override)
    return row


def _write_pin(monkeypatch, tmp_path, *, archive: bytes | None = None, program: bytes = b"",
               voices: dict | None = None, models: dict | None = None, asset: str = ASSET,
               voice_row: dict | None = None, model_licence: str = "MIT") -> None:
    files = voices or {VOICE: b"v", VOICE + ".json": b"{}"}
    pin = {"recorded": "2026-01-01",
           "piper": {"repository": "rhasspy/piper", "tag": "2023.11.14-2", "provenance": "test",
                     "assets": {asset: {"sha256": _sha(archive or b"x"), "size": len(archive or b"x"),
                                        "program_sha256": _sha(program)}}},
           "piper_voices": {"repository": "rhasspy/piper-voices", "revision": REVISION,
                            "files": {p: {"sha256": _sha(b), "size": len(b)} for p, b in files.items()},
                            "voices": {p.rsplit("/", 1)[1][:-len(".onnx")]: _voice_row(p, **(voice_row or {}))
                                       for p in files if p.endswith(".onnx")}},
           "models": {n: {"url": f"https://example.invalid/{n}", "sha256": _sha(b), "size": len(b),
                          "licence": model_licence, "licence_source": "https://example.invalid/licence"}
                      for n, b in (models or {}).items()}}
    path = tmp_path / "speech_assets_pin.json"
    import json
    path.write_text(json.dumps(pin), encoding="utf-8")
    monkeypatch.setattr(speech_pin, "PIN_PATH", path)


def _get_by_url(monkeypatch, payloads: dict, calls: list | None = None):
    """The fake network: each URL answers with its payload (a missing one with b"")."""
    def fake_get(url, **kwargs):
        if calls is not None:
            calls.append(url)
        payload = next((v for k, v in payloads.items() if url.endswith(k)), b"")

        class R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def raise_for_status(self): pass
            def iter_content(self, chunk_size):
                for i in range(0, len(payload), chunk_size):
                    yield payload[i:i + chunk_size]
        return R()
    monkeypatch.setattr("vaf.core.verified_download.requests.get", fake_get)


@pytest.fixture
def speech(monkeypatch, tmp_path):
    """A SpeechManager without __init__ (it loads the config and may open a microphone),
    with bin/ and models/ under tmp_path, the platform's archive fixed to the Linux x86_64
    tarball (a tar works on every host), and the UI captured."""
    from vaf.core import speech as speech_mod
    monkeypatch.setattr(verified_download, "_verified_files", set())
    sm = SpeechManager.__new__(SpeechManager)
    sm.base_dir = tmp_path
    sm.bin_dir = tmp_path / "bin" / "piper"
    sm.models_dir = tmp_path / "models" / "voices"
    sm.models_dir.mkdir(parents=True)
    sm._piper_checked = False
    sm._has_piper = False
    monkeypatch.setattr(SpeechManager, "_piper_asset_name", lambda self: ASSET)
    said = {"error": [], "event": [], "success": []}
    for kind in said:
        monkeypatch.setattr(speech_mod.UI, kind, lambda *a, _k=kind, **k: said[_k].append(" ".join(map(str, a))))
    sm.said = said
    return sm


def _program_name(sm) -> str:
    return sm._get_piper_binary().name


def _piper_archive(sm, program: bytes, extra=()) -> bytes:
    exe = _program_name(sm)
    return _tar([(f"piper/{exe}", program), ("piper/libespeak-ng.so.1.52.0.1", b"lib"),
                 ("piper/espeak-ng-data/phontab", b"phonemes"), *extra])


def _old_install(sm) -> dict:
    """An earlier VAF's Piper: another program, a stray file, its old archive left in
    the folder; and the other tenants of bin/."""
    sm.bin_dir.mkdir(parents=True)
    (sm.bin_dir / _program_name(sm)).write_bytes(b"older piper")
    (sm.bin_dir / "stray.so").write_bytes(b"stray")
    (sm.bin_dir / ASSET).write_bytes(b"old archive")
    bin_root = sm.bin_dir.parent
    (bin_root / "llama-cpp").mkdir()
    (bin_root / "llama-cpp" / "llama-server").write_bytes(b"llama")
    (bin_root / "vllm").mkdir()
    (bin_root / "vllm" / "x").write_bytes(b"vllm")
    (bin_root / "top-file").write_bytes(b"top")
    return _snapshot(bin_root)


# ── Piper ─────────────────────────────────────────────────────────────────────────

def test_piper_bytes_that_miss_the_pin_change_nothing(speech, monkeypatch, tmp_path):
    """The archive is downloaded only through download_verified with the pinned digest:
    other bytes leave bin/piper exactly as it was, no staging folder, no archive, and the
    line a person reads carries no raw hash. MUTATION: write the response straight to
    disk with requests.get as before: red."""
    program = b"pinned piper"
    archive = _piper_archive(speech, program)
    _write_pin(monkeypatch, tmp_path, archive=archive, program=program)
    before = _old_install(speech)
    _get_by_url(monkeypatch, {ASSET: archive + b"tampered"})
    assert speech._check_piper() is False
    bin_root = speech.bin_dir.parent
    assert _snapshot(bin_root) == before
    assert not (bin_root / "piper.staging").exists() and not (bin_root / "piper.previous").exists()
    assert any("did not match its expected checksum" in e and "github.com/Veyllo-Labs/VAF/issues" in e
               for e in speech.said["error"]), speech.said
    assert not any(HEX64.search(e) for e in speech.said["error"]), speech.said["error"]
    assert any(e.startswith("System Details:") for e in speech.said["event"]), "the details go to the dim event"


@NO_LINKS
@pytest.mark.parametrize("link", [("piper/evil", None, "/etc/passwd"), ("piper/evil", None, "../outside")],
                         ids=["absolute", "climbs-after-lift"])
def test_a_piper_link_that_escapes_is_refused(speech, monkeypatch, tmp_path, link):
    """An absolute link is refused by the data filter; ../outside stays inside the staging
    folder where it is unpacked and leaves bin/piper once the lift moved it up, so it is
    refused by the check after the lift. MUTATION: drop refuse_escaping_links from
    extract_archive and the second goes red."""
    program = b"pinned piper"
    archive = _piper_archive(speech, program, extra=(link,))
    _write_pin(monkeypatch, tmp_path, archive=archive, program=program)
    before = _old_install(speech)
    _get_by_url(monkeypatch, {ASSET: archive})
    assert speech._check_piper() is False
    assert _snapshot(speech.bin_dir.parent) == before


@NO_LINKS
def test_too_many_links_are_refused_before_unpacking(speech, monkeypatch, tmp_path):
    """MUTATION: drop the link cap and the archive (harmless links, all inside) installs."""
    program = b"pinned piper"
    links = tuple((f"piper/l{i}", None, "libespeak-ng.so.1.52.0.1")
                  for i in range(verified_download.MAX_ARCHIVE_LINKS + 1))
    archive = _piper_archive(speech, program, extra=links)
    _write_pin(monkeypatch, tmp_path, archive=archive, program=program)
    before = _old_install(speech)
    _get_by_url(monkeypatch, {ASSET: archive})
    assert speech._check_piper() is False
    assert _snapshot(speech.bin_dir.parent) == before
    assert any("links" in e for e in speech.said["event"]), speech.said["event"]


def test_no_data_filter_no_unpack(speech, monkeypatch, tmp_path):
    """A Python without tarfile's data filter gets an error, not an unfiltered unpack.
    MUTATION: fall back to extractall without the filter and Piper installs."""
    program = b"pinned piper"
    archive = _piper_archive(speech, program)
    _write_pin(monkeypatch, tmp_path, archive=archive, program=program)
    before = _old_install(speech)
    real = tarfile.TarFile.extractall

    def old_python(self, path=".", members=None, *, numeric_owner=False, **kwargs):
        if "filter" in kwargs:
            raise TypeError("extractall() got an unexpected keyword argument 'filter'")
        return real(self, path, members, numeric_owner=numeric_owner)

    monkeypatch.setattr(tarfile.TarFile, "extractall", old_python)
    _get_by_url(monkeypatch, {ASSET: archive})
    assert speech._check_piper() is False
    assert _snapshot(speech.bin_dir.parent) == before
    assert any("data filter" in e for e in speech.said["event"]), speech.said["event"]


def test_a_pinned_program_downloads_nothing(speech, monkeypatch, tmp_path):
    """The program with the pinned digest is the pinned Piper: no download. MUTATION: let
    _check_piper install whenever it asks (never trusting the digest): red."""
    program = b"pinned piper"
    _write_pin(monkeypatch, tmp_path, archive=_piper_archive(speech, program), program=program)
    speech.bin_dir.mkdir(parents=True)
    (speech.bin_dir / _program_name(speech)).write_bytes(program)
    _no_download(monkeypatch)
    monkeypatch.setattr(SpeechManager, "_install_piper", lambda self, entry: pytest.fail("install reached"))
    assert speech._check_piper() is True
    assert speech.said["event"] == [] and speech.said["error"] == []


def test_another_program_is_replaced_whole_and_tenants_stay(speech, monkeypatch, tmp_path):
    """An installed Piper whose program is not the pinned one is replaced by the whole
    folder swap: the stray file and the old archive of the earlier install are gone, no
    .previous or .staging is left, and bin/llama-cpp, bin/vllm and a file at the top of
    bin/ are untouched. MUTATIONS: _check_piper back to binary.exists() (the old program
    stays), or unpack into bin/ on top of the old folder (the stray file stays): red."""
    program = b"pinned piper"
    archive = _piper_archive(speech, program)
    _write_pin(monkeypatch, tmp_path, archive=archive, program=program)
    before = _old_install(speech)
    calls = []
    _get_by_url(monkeypatch, {ASSET: archive}, calls)
    assert speech._check_piper() is True
    assert len(calls) == 1 and calls[0].endswith(f"/releases/download/2023.11.14-2/{ASSET}"), calls
    bin_root = speech.bin_dir.parent
    after = _snapshot(bin_root)
    for tenant in ("llama-cpp", "llama-cpp/llama-server", "vllm", "vllm/x", "top-file"):
        assert after[tenant] == before[tenant], tenant
    exe = _program_name(speech)
    assert after[f"piper/{exe}"] == ("file", program)
    assert sorted(k for k in after if k.startswith("piper/") and "/" not in k[6:]) == sorted(
        [f"piper/{exe}", "piper/libespeak-ng.so.1.52.0.1", "piper/espeak-ng-data"])
    assert not (bin_root / "piper.previous").exists() and not (bin_root / "piper.staging").exists()
    if os.name != "nt":
        assert os.stat(speech.bin_dir / exe).st_mode & 0o111
    assert any("not the version this VAF release expects" in e for e in speech.said["event"]), speech.said["event"]


@pytest.mark.skipif(os.name == "nt", reason="symlink privilege")
def test_a_program_that_is_a_link_is_replaced_and_its_target_left_alone(speech, monkeypatch, tmp_path):
    """bin/piper/piper as a link to a file with the pinned bytes elsewhere is not the
    pinned program: the folder is swapped, and the link's target is neither followed by
    chmod nor removed. MUTATION: decide on os.stat (following the link) and the link
    counts as installed."""
    program = b"pinned piper"
    archive = _piper_archive(speech, program)
    _write_pin(monkeypatch, tmp_path, archive=archive, program=program)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_bytes(program)
    elsewhere.chmod(0o600)
    speech.bin_dir.mkdir(parents=True)
    os.symlink(elsewhere, speech.bin_dir / _program_name(speech))
    calls = []
    _get_by_url(monkeypatch, {ASSET: archive}, calls)
    assert speech._check_piper() is True and len(calls) == 1
    program_path = speech.bin_dir / _program_name(speech)
    assert not program_path.is_symlink() and program_path.read_bytes() == program
    assert elsewhere.read_bytes() == program and (elsewhere.stat().st_mode & 0o777) == 0o600


def test_an_archive_whose_program_is_not_the_pinned_one_is_refused(speech, monkeypatch, tmp_path):
    """The archive matches its pinned digest, but the program inside differs from the
    pinned program digest (a manifest whose two digests disagree): nothing is installed.
    MUTATION: drop the check of the staged program: red (it installs)."""
    archive = _piper_archive(speech, b"another program")
    _write_pin(monkeypatch, tmp_path, archive=archive, program=b"pinned piper")
    before = _old_install(speech)
    _get_by_url(monkeypatch, {ASSET: archive})
    assert speech._check_piper() is False
    assert _snapshot(speech.bin_dir.parent) == before
    assert any("not the pinned program" in e for e in speech.said["event"]), speech.said["event"]


@NO_LINKS
def test_an_installed_piper_with_a_link_leading_out_is_installed_again(speech, monkeypatch, tmp_path):
    """An older VAF unpacked Piper without a filter, so its folder may hold a link that
    leads outside it. The program alone matching is not enough then: the folder is
    replaced. MUTATION: decide on the program digest alone: red (no download)."""
    program = b"pinned piper"
    archive = _piper_archive(speech, program)
    _write_pin(monkeypatch, tmp_path, archive=archive, program=program)
    speech.bin_dir.mkdir(parents=True)
    (speech.bin_dir / _program_name(speech)).write_bytes(program)
    os.symlink(str(tmp_path), speech.bin_dir / "libpiper_phonemize.so.1")
    calls = []
    _get_by_url(monkeypatch, {ASSET: archive}, calls)
    assert speech._check_piper() is True and len(calls) == 1
    assert not os.path.lexists(speech.bin_dir / "libpiper_phonemize.so.1")


def test_an_unreachable_server_is_said_in_one_line(speech, monkeypatch, tmp_path):
    import requests
    program = b"pinned piper"
    _write_pin(monkeypatch, tmp_path, archive=_piper_archive(speech, program), program=program)

    def down(url, **kwargs):
        raise requests.ConnectionError("HTTPSConnectionPool(host='github.com', port=443): Max retries exceeded")
    monkeypatch.setattr("vaf.core.verified_download.requests.get", down)
    assert speech._check_piper() is False
    assert any("the download server could not be reached" in e for e in speech.said["error"]), speech.said
    assert not any("HTTPSConnectionPool" in e for e in speech.said["error"])
    assert any("HTTPSConnectionPool" in e for e in speech.said["event"])


@pytest.mark.skipif(os.name == "nt", reason="symlink privilege")
def test_a_piper_folder_that_is_a_link_is_not_the_installed_piper(speech, monkeypatch, tmp_path):
    """bin/piper itself a link to a folder elsewhere holding the pinned program: not this
    install's own folder, so it is installed again (the link is moved aside and removed,
    never followed). MUTATION: drop the link check on the folder: red (no download)."""
    program = b"pinned piper"
    archive = _piper_archive(speech, program)
    _write_pin(monkeypatch, tmp_path, archive=archive, program=program)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / _program_name(speech)).write_bytes(program)
    speech.bin_dir.parent.mkdir(parents=True)
    os.symlink(elsewhere, speech.bin_dir)
    calls = []
    _get_by_url(monkeypatch, {ASSET: archive}, calls)
    assert speech._check_piper() is True and len(calls) == 1
    assert not os.path.islink(speech.bin_dir) and speech.bin_dir.is_dir()
    assert (elsewhere / _program_name(speech)).read_bytes() == program, "the link's target is left alone"


def test_a_first_install_never_says_the_current_piper_is_unchanged(speech, monkeypatch, tmp_path):
    """With no Piper before, "Your current Piper is unchanged" is false; with one, it comes
    before the report link, which ends the line. MUTATION: always append the sentence: red."""
    program = b"pinned piper"
    archive = _piper_archive(speech, program)
    _write_pin(monkeypatch, tmp_path, archive=archive, program=program)
    _get_by_url(monkeypatch, {ASSET: archive[:-3] + b"xyz"})
    assert speech._check_piper() is False
    assert speech.said["error"] and not any("unchanged" in e for e in speech.said["error"]), speech.said["error"]

    speech.said["error"].clear()
    speech._piper_checked = False
    _old_install(speech)
    assert speech._check_piper() is False
    line = next(e for e in speech.said["error"] if "checksum" in e)
    assert "Your current Piper is unchanged. If this keeps happening" in line, line
    assert line.endswith("https://github.com/Veyllo-Labs/VAF/issues"), line


def test_a_failed_swap_puts_the_old_piper_back(speech, monkeypatch, tmp_path):
    """The second rename (staging into bin/piper) fails: the old folder is renamed back.
    MUTATION: drop the rename-back and bin/piper is gone."""
    program = b"pinned piper"
    archive = _piper_archive(speech, program)
    _write_pin(monkeypatch, tmp_path, archive=archive, program=program)
    before = _old_install(speech)
    real_rename = os.rename
    staging = str(speech.bin_dir.parent / "piper.staging")

    def rename(src, dst):
        if os.path.abspath(src) == os.path.abspath(staging):
            raise PermissionError(13, "Permission denied")
        return real_rename(src, dst)

    monkeypatch.setattr(os, "rename", rename)
    _get_by_url(monkeypatch, {ASSET: archive})
    assert speech._check_piper() is False
    assert _snapshot(speech.bin_dir.parent) == before
    assert any("could not be replaced right now" in e for e in speech.said["error"]), speech.said["error"]
    assert not any("Errno" in e for e in speech.said["error"]), "the raw error goes to the dim event"


def test_no_pinned_asset_no_download(speech, monkeypatch, tmp_path):
    """A platform the manifest holds no archive for gets no Piper and says so.
    MUTATION: fall back to the release URL without a digest: red (a download)."""
    _write_pin(monkeypatch, tmp_path, asset="piper_macos_x64.tar.gz")
    _no_download(monkeypatch)
    assert speech._check_piper() is False
    assert any("Piper has no download for this computer" in e for e in speech.said["error"])
    assert not any(".json" in e or ".tar.gz" in e for e in speech.said["error"]), "no internal file name"


def test_a_held_lock_says_it_waits(speech, monkeypatch, tmp_path):
    """Another process holds bin/.piper.lock: this one says it waits, gives up after the
    timeout and downloads nothing. MUTATION: drop the lock and it downloads at once."""
    from filelock import FileLock
    from vaf.core import speech as speech_mod
    program = b"pinned piper"
    _write_pin(monkeypatch, tmp_path, archive=_piper_archive(speech, program), program=program)
    monkeypatch.setattr(speech_mod, "PIPER_INSTALL_LOCK_TIMEOUT", 0.2)
    speech.bin_dir.parent.mkdir(parents=True)
    other = FileLock(str(speech.bin_dir.parent / ".piper.lock"))
    other.acquire()
    try:
        _no_download(monkeypatch)
        assert speech._check_piper() is False
    finally:
        other.release()
    assert any("Waiting for another VAF process that is installing Piper" in e for e in speech.said["event"])
    assert any("Another VAF process has been installing Piper" in e for e in speech.said["error"])


def test_a_waiting_process_finds_the_pinned_piper_and_downloads_nothing(speech, monkeypatch, tmp_path):
    """Once it holds the lock, the install first asks whether the other process already
    installed the pinned build."""
    program = b"pinned piper"
    entry = {"name": ASSET, "url": "https://example.invalid/" + ASSET, "sha256": "0" * 64, "size": 1,
             "program_sha256": _sha(program)}
    speech.bin_dir.mkdir(parents=True)
    (speech.bin_dir / _program_name(speech)).write_bytes(program)
    _no_download(monkeypatch)
    assert speech._install_piper(entry) is True


# ── voices ────────────────────────────────────────────────────────────────────────

ONNX, CONFIG = b"onnx voice model" * 64, b'{"audio": {"sample_rate": 22050}}'


def _voice_pin(monkeypatch, tmp_path):
    _write_pin(monkeypatch, tmp_path, voices={VOICE: ONNX, VOICE + ".json": CONFIG})


def test_voice_bytes_that_miss_the_pin_leave_nothing(speech, monkeypatch, tmp_path):
    """MUTATION: _ensure_voice_model back to requests.get straight into the final name:
    red (a file is left)."""
    _voice_pin(monkeypatch, tmp_path)
    _get_by_url(monkeypatch, {".onnx": ONNX + b"x", ".onnx.json": CONFIG})
    assert speech._ensure_voice_model("de") is None
    assert sorted(p.name for p in speech.models_dir.iterdir()) == []
    assert any("did not match its expected checksum" in e for e in speech.said["error"])
    assert not any(HEX64.search(e) for e in speech.said["error"])


def test_a_verified_voice_downloads_nothing(speech, monkeypatch, tmp_path):
    _voice_pin(monkeypatch, tmp_path)
    (speech.models_dir / "de_DE-mls-medium.onnx").write_bytes(ONNX)
    (speech.models_dir / "de_DE-mls-medium.onnx.json").write_bytes(CONFIG)
    _no_download(monkeypatch)
    assert speech._ensure_voice_model("de-DE") == speech.models_dir / "de_DE-mls-medium.onnx"


def test_a_truncated_voice_is_fetched_again(speech, monkeypatch, tmp_path):
    """An interrupted transfer of an earlier VAF left a short .onnx that counted as
    installed forever. MUTATION: let fetch_verified keep any existing file: red."""
    _voice_pin(monkeypatch, tmp_path)
    (speech.models_dir / "de_DE-mls-medium.onnx").write_bytes(ONNX[:100])
    (speech.models_dir / "de_DE-mls-medium.onnx.json").write_bytes(CONFIG)
    calls = []
    _get_by_url(monkeypatch, {".onnx": ONNX, ".onnx.json": CONFIG}, calls)
    assert speech._ensure_voice_model("de") == speech.models_dir / "de_DE-mls-medium.onnx"
    assert (speech.models_dir / "de_DE-mls-medium.onnx").read_bytes() == ONNX
    assert len(calls) == 1, "the matching .onnx.json is kept"


def test_a_voice_that_failed_its_checksum_is_not_fetched_at_every_reply(speech, monkeypatch, tmp_path):
    """A 63 to 128 MB voice whose bytes do not match is fetched once per process, not at
    every spoken reply; a connection that could not be made is retried. MUTATION: drop the
    memo of failed voices: red (a second download)."""
    import requests
    _voice_pin(monkeypatch, tmp_path)
    calls = []
    _get_by_url(monkeypatch, {".onnx": ONNX + b"x", ".onnx.json": CONFIG}, calls)
    assert speech._ensure_voice_model("de") is None
    assert speech._ensure_voice_model("de") is None
    assert len(calls) == 1

    other = SpeechManager.__new__(SpeechManager)
    other.models_dir = speech.models_dir

    def down(url, **kwargs):
        calls.append(url)
        raise requests.ConnectionError("no route")
    monkeypatch.setattr("vaf.core.verified_download.requests.get", down)
    calls.clear()
    assert other._ensure_voice_model("de") is None
    assert other._ensure_voice_model("de") is None
    assert len(calls) == 2, "a connection failure is retried at the next reply"


@pytest.mark.parametrize("status,remembered", [(404, True), (403, True), (503, False), (500, False),
                                               (408, False), (429, False)])
def test_only_a_refusal_is_remembered_never_a_server_error(speech, monkeypatch, tmp_path, status, remembered):
    """The web app runs for days: one 503 from the voice host must not silence the voice
    until a restart. A 4xx (and a checksum failure) is remembered for the process, except
    a timeout (408) or rate limit (429). MUTATION: remember every HTTP error: red for 5xx;
    remember every 4xx: red for 408 and 429."""
    import requests
    _voice_pin(monkeypatch, tmp_path)
    calls = []

    def answer(url, **kwargs):
        calls.append(url)
        r = requests.Response()
        r.status_code = status
        raise requests.HTTPError(f"{status} error", response=r)
    monkeypatch.setattr("vaf.core.verified_download.requests.get", answer)
    assert speech._ensure_voice_model("de") is None
    assert speech._ensure_voice_model("de") is None
    assert len(calls) == (1 if remembered else 2), calls
    assert any(f"({status})" in e for e in speech.said["error"]), speech.said["error"]


def test_voice_urls_name_the_pinned_revision(speech, monkeypatch, tmp_path):
    """MUTATION: the URL back to resolve/main: red."""
    _voice_pin(monkeypatch, tmp_path)
    calls = []
    _get_by_url(monkeypatch, {".onnx": ONNX, ".onnx.json": CONFIG}, calls)
    assert speech._ensure_voice_model("de") is not None
    assert calls == [f"https://huggingface.co/rhasspy/piper-voices/resolve/{REVISION}/{VOICE}",
                     f"https://huggingface.co/rhasspy/piper-voices/resolve/{REVISION}/{VOICE}.json"]


def test_the_english_fallback_is_fetched_from_the_english_folder():
    """A language without a voice speaks English; its path is read from the voice's own
    locale (it used to be built from the requested language: xx/en_US/..., a 404)."""
    assert speech_pin.voice_path(SpeechManager.PIPER_VOICES["en"]) == \
        "en/en_US/ljspeech/high/en_US-ljspeech-high.onnx"
    assert speech_pin.voice_path("pt_BR-faber-medium") == "pt/pt_BR/faber/medium/pt_BR-faber-medium.onnx"
    assert speech_pin.voice_path("simple") is None


@pytest.mark.parametrize("row", [{"licence": "CC-BY-NC-SA-4.0"}, {"base": "en_US-lessac-high"}, {"host_piper": False}],
                         ids=["non-commercial", "lessac-base", "host-cannot-read"])
def test_a_voice_outside_the_licence_list_is_never_downloaded(speech, monkeypatch, tmp_path, row):
    """A pinned voice whose licence is not on the list, which was fine-tuned from a base
    that is not clean, or which the pinned Piper cannot read, has no download: nothing is
    fetched or written, and the reason is said. MUTATIONS: drop the licence check in
    voice_files (red: non-commercial), the base check (red: lessac-base), the host_piper
    check (red: host-cannot-read)."""
    _write_pin(monkeypatch, tmp_path, voices={VOICE: ONNX, VOICE + ".json": CONFIG}, voice_row=row)
    _no_download(monkeypatch)
    assert speech_pin.voice_files("de_DE-mls-medium") is None
    assert speech._ensure_voice_model("de") is None
    assert sorted(p.name for p in speech.models_dir.iterdir()) == []
    assert any("has no download in this VAF release" in e for e in speech.said["error"]), speech.said["error"]


def test_a_voice_on_the_list_is_offered(monkeypatch, tmp_path):
    _write_pin(monkeypatch, tmp_path, voices={VOICE: ONNX, VOICE + ".json": CONFIG})
    assert speech_pin.voice_files("de_DE-mls-medium")[0]["path"] == VOICE


def _retired_folder(models: Path, outside: Path) -> dict:
    """models/voices/ as an earlier VAF left it: two retired voices (one of them only its
    .onnx), a retired name that is a link to a file outside, a folder named like a retired
    file, a current voice and files of the person's own."""
    (models / "en_US-lessac-high.onnx").write_bytes(b"x" * 2_000_000)
    (models / "en_US-lessac-high.onnx.json").write_bytes(b"{}")
    (models / "tr_TR-dfki-medium.onnx").write_bytes(b"y" * 1_000_000)
    outside.write_bytes(b"not VAF's")
    if os.name != "nt":                   # a link needs a privilege on Windows
        (models / "de_DE-thorsten-high.onnx.json").symlink_to(outside)
    (models / "fr_FR-siwis-medium.onnx").mkdir()
    (models / "fr_FR-siwis-medium.onnx" / "inside").write_bytes(b"kept")
    (models / "de_DE-mls-medium.onnx").write_bytes(b"current")
    (models / "de_DE-mls-medium.onnx.json").write_bytes(b"{}")
    (models / "notes.onnx").write_bytes(b"mine")
    (models / "en_US-lessac-high.onnx.bak").write_bytes(b"mine too")
    return {"de_DE-mls-medium.onnx", "de_DE-mls-medium.onnx.json", "notes.onnx", "en_US-lessac-high.onnx.bak",
            "fr_FR-siwis-medium.onnx"}


def test_retired_voices_are_removed_by_exact_name_once(speech, monkeypatch, tmp_path):
    """The voices this release no longer offers are deleted from models/voices/ by their
    exact names: regular files and links (never what a link points to), not a folder of
    that name, not the current voice, not any other file. Said once, with the space freed;
    a second call in the same process does nothing. MUTATIONS: remove by a pattern (*.onnx:
    red, notes.onnx and the current voice go), follow the link (red: the outside file goes),
    no once-flag (red: two calls look twice)."""
    outside = tmp_path / "outside.bin"
    keep = _retired_folder(speech.models_dir, outside)
    _write_pin(monkeypatch, tmp_path, voices={VOICE: b"current", VOICE + ".json": b"{}"})
    _no_download(monkeypatch)
    assert speech._ensure_voice_model("de") == speech.models_dir / "de_DE-mls-medium.onnx"
    assert {p.name for p in speech.models_dir.iterdir()} == keep
    assert outside.read_bytes() == b"not VAF's"
    assert (speech.models_dir / "fr_FR-siwis-medium.onnx" / "inside").read_bytes() == b"kept"
    removed = [e for e in speech.said["event"] if "no longer offers" in e]
    assert removed == ["Speech Removed 2 voices this VAF release no longer offers: their licence does not allow any use, "
                       "or Piper on this computer cannot read them (3 MB freed)."], removed

    (speech.models_dir / "tr_TR-dfki-medium.onnx").write_bytes(b"again")
    looked = []
    real_lstat = os.lstat
    monkeypatch.setattr(os, "lstat", lambda path, *a, **k: looked.append(str(path)) or real_lstat(path, *a, **k))
    speech._ensure_voice_model("de")
    assert not any("tr_TR-dfki-medium" in p for p in looked), "the second call does not look again"
    assert (speech.models_dir / "tr_TR-dfki-medium.onnx").exists()
    assert len([e for e in speech.said["event"] if "no longer offers" in e]) == 1


def test_a_retired_voice_that_cannot_be_removed_is_said_and_left(speech, monkeypatch, tmp_path):
    """A file in use (Windows) cannot be deleted: one dim line, the voice works on."""
    (speech.models_dir / "en_US-lessac-high.onnx").write_bytes(b"x")
    _write_pin(monkeypatch, tmp_path, voices={VOICE: b"current", VOICE + ".json": b"{}"})
    (speech.models_dir / "de_DE-mls-medium.onnx").write_bytes(b"current")
    (speech.models_dir / "de_DE-mls-medium.onnx.json").write_bytes(b"{}")
    real_unlink = os.unlink

    def busy(path, *a, **k):
        if str(path).endswith("en_US-lessac-high.onnx"):
            raise PermissionError(13, "in use")
        return real_unlink(path, *a, **k)
    monkeypatch.setattr(os, "unlink", busy)
    assert speech._ensure_voice_model("de") is not None
    assert (speech.models_dir / "en_US-lessac-high.onnx").exists()
    assert any("could not be removed yet" in e for e in speech.said["event"]), speech.said["event"]


def test_the_retired_list_and_the_current_voices_are_disjoint():
    """A voice cannot be offered and deleted at once; every retired name is a voice name."""
    assert not set(SpeechManager.RETIRED_VOICES) & set(SpeechManager.PIPER_VOICES.values())
    assert len(SpeechManager.RETIRED_VOICES) == len(set(SpeechManager.RETIRED_VOICES)) == 29
    assert all(speech_pin.voice_path(name) for name in SpeechManager.RETIRED_VOICES)


def test_a_language_without_a_voice_says_it_reads_in_english_once(speech, monkeypatch, tmp_path):
    """Turkish has no voice on the computer: the English voice reads it, and the person is
    told once per language, not at every reply. MUTATIONS: delete the notice (red), say it
    at every call (red)."""
    en = "en/en_US/ljspeech/high/en_US-ljspeech-high.onnx"
    _write_pin(monkeypatch, tmp_path, voices={en: ONNX, en + ".json": CONFIG})
    (speech.models_dir / "en_US-ljspeech-high.onnx").write_bytes(ONNX)
    (speech.models_dir / "en_US-ljspeech-high.onnx.json").write_bytes(CONFIG)
    _no_download(monkeypatch)
    for lang in ("tr", "tr-TR", "tr", "ja", "en", "auto"):
        assert speech._ensure_voice_model(lang) == speech.models_dir / "en_US-ljspeech-high.onnx"
    said = [e for e in speech.said["event"] if "the English voice reads it" in e]
    assert said == ["Speech There is no voice for 'tr' in this VAF release; the English voice reads it.",
                    "Speech There is no voice for 'ja' in this VAF release; the English voice reads it."], said


# ── speaker and turn-end models ───────────────────────────────────────────────────

def test_speaker_models_refuse_bad_bytes(monkeypatch, tmp_path):
    """MUTATION: the old st_size > 10000 check with a plain download: the bad bytes are
    written and _ensure_models is True."""
    from vaf.core import speaker_id
    monkeypatch.setattr(verified_download, "_verified_files", set())
    vad, emb = b"vad" * 5000, b"emb" * 5000
    _write_pin(monkeypatch, tmp_path, models={"silero_vad.onnx": vad,
                                              "3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx": emb})
    models = tmp_path / "speaker"
    monkeypatch.setattr(speaker_id, "_models_dir", lambda: models)
    _get_by_url(monkeypatch, {"silero_vad.onnx": vad + b"x", "voxceleb_16k.onnx": emb + b"x"})
    assert speaker_id._ensure_models() is False
    assert sorted(p.name for p in models.iterdir()) == []
    _get_by_url(monkeypatch, {"silero_vad.onnx": vad, "voxceleb_16k.onnx": emb})
    assert speaker_id._ensure_models() is True
    assert (models / "silero_vad.onnx").read_bytes() == vad


def test_a_model_outside_the_licence_list_is_not_fetched(monkeypatch, tmp_path):
    """A speaker model whose recorded licence is not on the list is not available: nothing
    is fetched or written. MUTATION: drop the licence check in model(): red."""
    from vaf.core import speaker_id
    monkeypatch.setattr(verified_download, "_verified_files", set())
    vad, emb = b"vad" * 5000, b"emb" * 5000
    _write_pin(monkeypatch, tmp_path, models={"silero_vad.onnx": vad,
                                              "3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx": emb},
               model_licence="CC-BY-NC-4.0")
    models = tmp_path / "speaker"
    monkeypatch.setattr(speaker_id, "_models_dir", lambda: models)
    _no_download(monkeypatch)
    assert speech_pin.model("silero_vad.onnx") is None
    assert speaker_id._ensure_models() is False
    assert not models.exists() or sorted(p.name for p in models.iterdir()) == []


def test_smart_turn_refuses_bad_bytes_and_fails_open(monkeypatch, tmp_path):
    """The turn-end model with other bytes is not written, onnxruntime is never asked to
    load anything, and the judge fails open (the speaker counts as finished).
    MUTATION: the old st_size > 1_000_000 check with a plain download: a file is written
    and the stub runtime is asked to load it."""
    import sys
    import types
    from vaf.core import speaker_id, voice_vad
    monkeypatch.setattr(verified_download, "_verified_files", set())
    model = b"turn" * 300_000
    _write_pin(monkeypatch, tmp_path, models={"smart-turn-v3.2-cpu.onnx": model})
    models = tmp_path / "speaker"
    monkeypatch.setattr(speaker_id, "_models_dir", lambda: models)
    loaded = []
    stub = types.ModuleType("onnxruntime")
    stub.SessionOptions = lambda: types.SimpleNamespace()
    stub.InferenceSession = lambda *a, **k: loaded.append(a) or (_ for _ in ()).throw(RuntimeError("stub"))
    monkeypatch.setitem(sys.modules, "onnxruntime", stub)
    _get_by_url(monkeypatch, {"smart-turn-v3.2-cpu.onnx": model + b"x"})
    assert voice_vad.SemanticTurnJudge().complete(b"\0" * 3200) is True
    assert loaded == []
    assert not models.exists() or sorted(p.name for p in models.iterdir()) == []


# ── the manifest ──────────────────────────────────────────────────────────────────

def test_every_voice_name_is_pinned():
    """Every voice VAF may fetch, on the host and in the speech container, is in the real
    manifest with both files. MUTATION: revert one name to sv_SE-talesyntese-medium
    (never published): red."""
    import json
    pin = speech_pin.load_speech_pin()
    for lang, name in SpeechManager.PIPER_VOICES.items():
        assert speech_pin.voice_files(name) is not None, (lang, name)
    table = json.loads((REPO / "docker" / "tts-multilang" / "models.json").read_bytes().decode("utf-8"))
    for lang, row in table["models"].items():
        for part in ("model", "config"):
            meta = pin["piper_voices"]["files"].get(row[part]["path"])
            assert meta == {"sha256": row[part]["sha256"], "size": row[part]["size"]}, (lang, part)
    assert set(pin["piper"]["assets"]) == set(speech_pin.PIPER_ASSETS)
    assert {"silero_vad.onnx", "3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx",
            "smart-turn-v3.2-cpu.onnx"} <= set(pin["models"])
    turn = pin["models"]["smart-turn-v3.2-cpu.onnx"]["url"]
    assert re.search(r"/resolve/[0-9a-f]{40}/", turn), "the turn-end model is pinned to a commit, not main"


# The voices of the licence audit: the computer's eleven and the container's eleven
# languages, eighteen voices in all. A voice joins only with its licence reviewed.
AUDITED_VOICES = {
    "en_US-ljspeech-high", "de_DE-mls-medium", "fr_FR-mls-medium", "es_ES-carlfm-x_low", "it_IT-riccardo-x_low",
    "is_IS-bui-medium", "sv_SE-nst-medium", "uk_UA-ukrainian_tts-medium", "kk_KZ-issai-high",
    "el_GR-rapunzelina-medium", "nl_NL-mls-medium", "de_DE-thorsten-medium", "en_US-hifi_tts_92-medium",
    "fr_FR-siwis-medium", "pl_PL-gosia-medium", "pt_BR-faber-medium", "ru_RU-dmitri-medium", "zh_CN-chaowen-medium",
}
HOST_VOICES = {"en": "en_US-ljspeech-high", "de": "de_DE-mls-medium", "fr": "fr_FR-mls-medium",
               "es": "es_ES-carlfm-x_low", "it": "it_IT-riccardo-x_low", "is": "is_IS-bui-medium",
               "sv": "sv_SE-nst-medium", "uk": "uk_UA-ukrainian_tts-medium", "kk": "kk_KZ-issai-high",
               "el": "el_GR-rapunzelina-medium", "nl": "nl_NL-mls-medium"}
CONTAINER_VOICES = {"de": "de_DE-thorsten-medium", "en": "en_US-hifi_tts_92-medium", "fr": "fr_FR-siwis-medium",
                    "es": "es_ES-carlfm-x_low", "it": "it_IT-riccardo-x_low", "nl": "nl_NL-mls-medium",
                    "pl": "pl_PL-gosia-medium", "pt": "pt_BR-faber-medium", "ru": "ru_RU-dmitri-medium",
                    "uk": "uk_UA-ukrainian_tts-medium", "zh": "zh_CN-chaowen-medium"}


def _container_table() -> dict:
    import json
    return json.loads((REPO / "docker" / "tts-multilang" / "models.json").read_bytes().decode("utf-8"))


def test_every_pinned_voice_has_an_allowed_licence_and_a_card():
    """The real manifest: every voice carries a licence on the list, a clean base whose own
    licence is on the list, an https dataset, its MODEL_CARD by digest and the host flag.
    MUTATIONS: set CC-BY-NC-SA-4.0 on one row (red), delete one card (red: the loader)."""
    pin = speech_pin.load_speech_pin()
    assert all(lic is None or lic in speech_pin.ALLOWED_WEIGHT_LICENCES for lic in speech_pin.CLEAN_BASES.values())
    for name, row in pin["piper_voices"]["voices"].items():
        assert row["licence"] in speech_pin.ALLOWED_WEIGHT_LICENCES, (name, row["licence"])
        assert row["base"] in speech_pin.CLEAN_BASES, (name, row["base"])
        assert row["dataset"].startswith("https://"), name
        assert row["card"]["path"] == speech_pin.voice_path(name).rsplit("/", 1)[0] + "/MODEL_CARD", name
        assert HEX64.fullmatch(row["card"]["sha256"]) and row["card"]["size"] > 0, name
        assert isinstance(row["host_piper"], bool), name


def test_the_pinned_voices_are_exactly_the_audited_ones():
    """MUTATION: add ja_JP-hi_fi_captain-medium back to the manifest: red."""
    pin = speech_pin.load_speech_pin()
    assert set(pin["piper_voices"]["voices"]) == AUDITED_VOICES
    assert {p.rsplit("/", 1)[1].removesuffix(".json").removesuffix(".onnx")
            for p in pin["piper_voices"]["files"]} == AUDITED_VOICES


def test_every_host_voice_is_loadable_by_the_pinned_piper():
    """The computer's table is exactly the audited one, and each of its voices is one the
    pinned Piper reads and the licence list allows. MUTATION: fr_FR-siwis-medium back as
    the computer's French voice: red."""
    pin = speech_pin.load_speech_pin()
    assert SpeechManager.PIPER_VOICES == HOST_VOICES
    for lang, name in SpeechManager.PIPER_VOICES.items():
        assert pin["piper_voices"]["voices"][name]["host_piper"] is True, (lang, name)
        assert speech_pin.voice_files(name) is not None, (lang, name)


def test_the_speech_models_carry_their_licence():
    """MUTATION: drop the licence of the Smart Turn entry: red (the loader refuses)."""
    pin = speech_pin.load_speech_pin()
    assert {n: m["licence"] for n, m in pin["models"].items()} == {
        "silero_vad.onnx": "MIT", "3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx": "Apache-2.0",
        "smart-turn-v3.2-cpu.onnx": "BSD-2-Clause"}
    for name, meta in pin["models"].items():
        assert meta["licence_source"].startswith("https://"), name
        assert speech_pin.model(name) is not None, name
    turn = pin["models"]["smart-turn-v3.2-cpu.onnx"]
    assert turn["licence_source"].split("/raw/")[1].split("/")[0] in turn["url"], "the README of the pinned commit"


@pytest.mark.parametrize("damage", [
    lambda p: p.pop("piper"),
    lambda p: p["piper"]["assets"][ASSET].update(sha256="short"),
    lambda p: p["piper"]["assets"][ASSET].update(program_sha256=None),
    lambda p: p["piper"]["assets"].update({"piper_evil.tar.gz": dict(p["piper"]["assets"][ASSET])}),
    lambda p: p["piper_voices"].update(revision="main"),
    lambda p: p["piper_voices"]["files"].update({"../x.onnx": {"sha256": "0" * 64, "size": 1}}),
    lambda p: p["piper_voices"]["files"][VOICE].update(size=0),
    lambda p: p["models"].update({"m.onnx": {"url": "http://x", "sha256": "0" * 64, "size": 1, "licence": "MIT",
                                             "licence_source": "https://x"}}),
    lambda p: p["piper_voices"].pop("voices"),
    lambda p: p["piper_voices"]["voices"].pop("de_DE-mls-medium"),
    lambda p: p["piper_voices"]["voices"].update({"de-x": p["piper_voices"]["voices"]["de_DE-mls-medium"]}),
    lambda p: p["piper_voices"]["files"].pop(VOICE + ".json"),
    lambda p: p["piper_voices"]["voices"]["de_DE-mls-medium"].update(licence=" "),
    lambda p: p["piper_voices"]["voices"]["de_DE-mls-medium"].update(host_piper="yes"),
    lambda p: p["piper_voices"]["voices"]["de_DE-mls-medium"]["card"].update(path="de/de_DE/mls/medium/README"),
    lambda p: p["piper_voices"]["voices"]["de_DE-mls-medium"]["card"].update(sha256="short"),
    lambda p: p["models"].update({"m.onnx": {"url": "https://x", "sha256": "0" * 64, "size": 1,
                                             "licence_source": "https://x"}}),
    lambda p: p["models"].update({"m.onnx": {"url": "https://x", "sha256": "0" * 64, "size": 1, "licence": "MIT",
                                             "licence_source": "http://x"}}),
], ids=["no-piper", "short-digest", "no-program-digest", "unknown-archive", "moving-revision",
        "climbing-path", "zero-size", "plain-http", "no-voices", "onnx-without-row", "bad-voice-name",
        "row-without-files", "empty-licence", "host-piper-string", "card-not-model-card", "card-digest",
        "model-without-licence", "licence-source-http"])
def test_a_malformed_manifest_is_refused(monkeypatch, tmp_path, damage):
    import json
    _write_pin(monkeypatch, tmp_path)
    pin = json.loads(speech_pin.PIN_PATH.read_text(encoding="utf-8"))
    damage(pin)
    speech_pin.PIN_PATH.write_text(json.dumps(pin), encoding="utf-8")
    with pytest.raises(RuntimeError):
        speech_pin.load_speech_pin()


def test_a_malformed_manifest_refuses_the_install(speech, monkeypatch, tmp_path):
    """A broken manifest downloads nothing anywhere. MUTATION: make the loader return {}
    on an error: red (a lookup falls through or a download starts)."""
    from vaf.core import speaker_id
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(speech_pin, "PIN_PATH", bad)
    monkeypatch.setattr(speaker_id, "_models_dir", lambda: tmp_path / "speaker")
    _no_download(monkeypatch)
    assert speech._check_piper() is False
    assert speech._ensure_voice_model("de") is None
    assert speaker_id._ensure_models() is False
    assert any("cannot be read" in e for e in speech.said["error"]), speech.said["error"]
    assert any("speech pin manifest" in e for e in speech.said["event"]), speech.said["event"]


def test_no_unfiltered_unpack_or_hand_rolled_download_in_the_speech_stack():
    """MUTATION: put zip_ref.extractall( back into speech.py: red."""
    for path in (REPO / "vaf").rglob("*.py"):
        if path.name == "verified_download.py" and path.parent.name == "core":
            continue
        assert ".extractall(" not in path.read_text(encoding="utf-8", errors="replace"), path
    for rel in ("vaf/core/speech.py", "vaf/core/speaker_id.py", "vaf/core/voice_vad.py",
                "docker/tts-multilang/app.py"):
        assert "iter_content(" not in (REPO / rel).read_text(encoding="utf-8"), rel


# ── the speech container ──────────────────────────────────────────────────────────

def test_the_container_copy_is_byte_identical():
    """The container's build context holds no vaf package, so it carries a copy of the
    verified download. MUTATION: change one byte of the copy: red."""
    assert (REPO / "docker" / "tts-multilang" / "verified_download.py").read_bytes() == \
        (REPO / "vaf" / "core" / "verified_download.py").read_bytes()


def test_container_and_host_pins_agree():
    """Both tables come from one commit of rhasspy/piper-voices and agree on every path
    they share and on each voice's licence; the container's languages are the audited
    ones, each row naming its card at the revision as the licence source, and Japanese and
    Turkish retired. MUTATIONS: change the container's revision (red), change one row's
    licence in models.json (red)."""
    pin = speech_pin.load_speech_pin()
    table = _container_table()
    revision = pin["piper_voices"]["revision"]
    assert table["revision"] == revision
    assert table["source"] == f"https://huggingface.co/{speech_pin.VOICES_REPO}"
    assert table["retired"] == ["ja", "tr"]
    voices = {lang: f"{row['locale']}-{row['voice']}-{row['quality']}" for lang, row in table["models"].items()}
    assert voices == CONTAINER_VOICES
    for lang, row in table["models"].items():
        assert row["model"]["path"] == speech_pin.voice_path(voices[lang])
        assert row["config"]["path"] == row["model"]["path"] + ".json"
        voice = pin["piper_voices"]["voices"][voices[lang]]
        assert row["licence"] == voice["licence"], lang
        assert row["licence_source"] == \
            f"https://huggingface.co/{speech_pin.VOICES_REPO}/raw/{revision}/{voice['card']['path']}", lang


def _third_party_weight_rows() -> dict:
    """First cell -> licence cell of the rows of "Container images and model weights" in
    docs/legal/THIRD_PARTY.md (the pattern of test_dependency_manifests_in_sync.py)."""
    rows, section = {}, None
    for line in (REPO / "docs" / "legal" / "THIRD_PARTY.md").read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        if section != "Container images and model weights" or not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 4 and not set(cells[0]) <= set("-: "):
            rows[cells[0]] = cells[2]
    return rows


def test_third_party_names_every_pinned_voice_and_model():
    """Every pinned voice has its own row (first cell its backticked name) whose licence
    cell starts with the manifest's licence, no row names a voice the manifest does not
    pin, and each model's row names its licence. MUTATIONS: delete the nl_NL-mls-medium
    row (red), write CC0-1.0 into a CC-BY-4.0 row (red)."""
    pin = speech_pin.load_speech_pin()
    rows = _third_party_weight_rows()
    voice_rows = {cell.strip("`"): lic for cell, lic in rows.items()
                  if re.fullmatch(r"`[a-z]{2,3}_[A-Z]{2}-[A-Za-z0-9_]+-[a-z_]+`", cell)}
    assert set(voice_rows) == set(pin["piper_voices"]["voices"])
    for name, row in pin["piper_voices"]["voices"].items():
        assert voice_rows[name].split(" ")[0] == row["licence"], (name, voice_rows[name])
    for name, meta in pin["models"].items():
        [lic] = [lic for cell, lic in rows.items() if f"`{name}`" in cell]
        assert lic.split(" ")[0] == meta["licence"], (name, lic)
