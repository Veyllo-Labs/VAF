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


# ── fixtures for the speech stack ─────────────────────────────────────────────────

from vaf.core import speech_pin  # noqa: E402
from vaf.core.speech import SpeechManager  # noqa: E402

ASSET = "piper_linux_x86_64.tar.gz"
REVISION = "0123456789abcdef0123456789abcdef01234567"
VOICE = "de/de_DE/thorsten/high/de_DE-thorsten-high.onnx"
HEX64 = re.compile(r"\b[0-9a-f]{64}\b")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_pin(monkeypatch, tmp_path, *, archive: bytes | None = None, program: bytes = b"",
               voices: dict | None = None, models: dict | None = None, asset: str = ASSET) -> None:
    pin = {"recorded": "2026-01-01",
           "piper": {"repository": "rhasspy/piper", "tag": "2023.11.14-2", "provenance": "test",
                     "assets": {asset: {"sha256": _sha(archive or b"x"), "size": len(archive or b"x"),
                                        "program_sha256": _sha(program)}}},
           "piper_voices": {"repository": "rhasspy/piper-voices", "revision": REVISION,
                            "files": {p: {"sha256": _sha(b), "size": len(b)}
                                      for p, b in (voices or {VOICE: b"v", VOICE + ".json": b"{}"}).items()}},
           "models": {n: {"url": f"https://example.invalid/{n}", "sha256": _sha(b), "size": len(b)}
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
    assert any("did not match its pinned checksum" in e for e in speech.said["error"]), speech.said
    assert not any(HEX64.search(e) for e in speech.said["error"]), speech.said["error"]


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
    assert any("links" in e for e in speech.said["error"]), speech.said["error"]


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
    assert any("data filter" in e for e in speech.said["error"]), speech.said["error"]


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
    assert any("differs from the pinned build" in e for e in speech.said["event"]), speech.said["event"]


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


def test_no_pinned_asset_no_download(speech, monkeypatch, tmp_path):
    """A platform the manifest holds no archive for gets no Piper and says so.
    MUTATION: fall back to the release URL without a digest: red (a download)."""
    _write_pin(monkeypatch, tmp_path, asset="piper_macos_x64.tar.gz")
    _no_download(monkeypatch)
    assert speech._check_piper() is False
    assert any("No pinned Piper build for piper_linux_x86_64.tar.gz" in e for e in speech.said["error"])


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
    assert any("did not match its pinned checksum" in e for e in speech.said["error"])
    assert not any(HEX64.search(e) for e in speech.said["error"])


def test_a_verified_voice_downloads_nothing(speech, monkeypatch, tmp_path):
    _voice_pin(monkeypatch, tmp_path)
    (speech.models_dir / "de_DE-thorsten-high.onnx").write_bytes(ONNX)
    (speech.models_dir / "de_DE-thorsten-high.onnx.json").write_bytes(CONFIG)
    _no_download(monkeypatch)
    assert speech._ensure_voice_model("de-DE") == speech.models_dir / "de_DE-thorsten-high.onnx"


def test_a_truncated_voice_is_fetched_again(speech, monkeypatch, tmp_path):
    """An interrupted transfer of an earlier VAF left a short .onnx that counted as
    installed forever. MUTATION: let fetch_verified keep any existing file: red."""
    _voice_pin(monkeypatch, tmp_path)
    (speech.models_dir / "de_DE-thorsten-high.onnx").write_bytes(ONNX[:100])
    (speech.models_dir / "de_DE-thorsten-high.onnx.json").write_bytes(CONFIG)
    calls = []
    _get_by_url(monkeypatch, {".onnx": ONNX, ".onnx.json": CONFIG}, calls)
    assert speech._ensure_voice_model("de") == speech.models_dir / "de_DE-thorsten-high.onnx"
    assert (speech.models_dir / "de_DE-thorsten-high.onnx").read_bytes() == ONNX
    assert len(calls) == 1, "the matching .onnx.json is kept"


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
        "en/en_US/lessac/high/en_US-lessac-high.onnx"
    assert speech_pin.voice_path("pt_BR-faber-medium") == "pt/pt_BR/faber/medium/pt_BR-faber-medium.onnx"
    assert speech_pin.voice_path("simple") is None


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


@pytest.mark.parametrize("damage", [
    lambda p: p.pop("piper"),
    lambda p: p["piper"]["assets"][ASSET].update(sha256="short"),
    lambda p: p["piper"]["assets"][ASSET].update(program_sha256=None),
    lambda p: p["piper"]["assets"].update({"piper_evil.tar.gz": dict(p["piper"]["assets"][ASSET])}),
    lambda p: p["piper_voices"].update(revision="main"),
    lambda p: p["piper_voices"]["files"].update({"../x.onnx": {"sha256": "0" * 64, "size": 1}}),
    lambda p: p["piper_voices"]["files"][VOICE].update(size=0),
    lambda p: p["models"].update({"m.onnx": {"url": "http://x", "sha256": "0" * 64, "size": 1}}),
], ids=["no-piper", "short-digest", "no-program-digest", "unknown-archive", "moving-revision",
        "climbing-path", "zero-size", "plain-http"])
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
    assert any("speech pin manifest" in e for e in speech.said["error"]), speech.said["error"]


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
    they share. MUTATION: change the container's revision: red."""
    import json
    pin = speech_pin.load_speech_pin()
    table = json.loads((REPO / "docker" / "tts-multilang" / "models.json").read_bytes().decode("utf-8"))
    assert table["revision"] == pin["piper_voices"]["revision"]
    assert table["source"] == f"https://huggingface.co/{speech_pin.VOICES_REPO}"
    for lang, row in table["models"].items():
        assert row["model"]["path"] == speech_pin.voice_path(f"{row['locale']}-{row['voice']}-{row['quality']}")
        assert row["config"]["path"] == row["model"]["path"] + ".json"
