# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The local backend runs the llama.cpp build pinned with the VAF release, and nothing else.

VAF never updates llama.cpp on its own: `vaf/core/llama_server_pin.json` (written by
scripts/pin_llama_cpp.py) names the build and the SHA-256 of every release asset the
launcher may pick, the launcher installs that build, and only bytes that hash to the
recorded digest reach `bin/`. It used to ask GitHub for the "latest" release on every
fresh install; when llama.cpp made that a semver tag without binaries, every fresh
install silently got a 2024 build, and a replaced asset would have run unverified.

MUTATION: let `download_verified` skip the digest comparison and the integrity test goes
red; let `ensure_server_exists` return True on any existing binary and the re-install test
goes red.

llama.cpp lives in its own folder, bin/llama-cpp: the install swaps only that folder, so
the vLLM environment (bin/vllm) and Piper (bin/piper) survive every update, and a build an
earlier VAF put straight into bin/ is moved there and its top-level files cleaned up only
after the new build is verified. A llama server that still runs is stopped after the
verified download and before the first rename.
"""
import hashlib
import io
import os
import re
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from vaf.core import backend
from vaf.core.verified_download import DownloadIntegrityError, download_verified

REPO = Path(__file__).resolve().parents[1]


def _manager(system: str, machine: str = "x86_64", tmp_path: Path | None = None):
    """A ServerManager without __init__ (it cleans up orphan servers and creates Windows
    job objects); only the fields the pin logic reads."""
    m = backend.ServerManager.__new__(backend.ServerManager)
    m.system, m.machine = system, machine
    m.server_exe = "llama-server.exe" if system == "Windows" else "llama-server"
    if tmp_path is not None:
        m._set_install_paths(str(tmp_path))
        m.pid_file = str(tmp_path / "server.pid")
    return m


def _fake_get(monkeypatch, payload: bytes):
    def fake_get(url, **kwargs):
        class R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def raise_for_status(self): pass
            def iter_content(self, chunk_size):
                for i in range(0, len(payload), chunk_size):
                    yield payload[i:i + chunk_size]
        return R()
    monkeypatch.setattr("vaf.core.verified_download.requests.get", fake_get)


# ── the manifest ──────────────────────────────────────────────────────────────────

def test_the_manifest_is_a_pin_the_launcher_can_act_on():
    pin = backend.load_llama_pin()
    assert re.fullmatch(r"b\d+", pin["tag"]) and pin["repository"] == backend.LLAMA_REPO
    assert backend.ServerManager.pinned_build(pin) >= 9857, "the log verbosity semantics and the voice model need a 2026 build"
    assert len(pin["assets"]) >= 12
    for name, meta in pin["assets"].items():
        assert backend.LLAMA_ASSET_PATTERN.fullmatch(name), name
        assert re.fullmatch(r"[0-9a-f]{64}", meta["sha256"]) and int(meta["size"]) > 0, name
    # The builder the pin script found: llama.cpp's release workflow, or since the
    # build/publish split its publish workflow; named exactly, with the repository id.
    assert re.search(r"\.github/workflows/release(-publish)?\.yml \(repository id 612354784\)", pin.get("provenance", "")), \
        pin.get("provenance")
    for name in pin["assets"]:
        if name.startswith("llama-"):
            assert name.startswith(f"llama-{pin['tag']}-bin-"), f"{name} is not an asset of the pinned {pin['tag']}"


def test_the_asset_pattern_takes_what_the_launcher_picks_and_nothing_else():
    ok = ("llama-b10955-bin-macos-arm64.tar.gz", "llama-b10955-bin-ubuntu-vulkan-arm64.tar.gz",
          "llama-b10955-bin-win-cuda-13.3-x64.zip", "llama-b10955-bin-win-rocm-10.0-x64.zip",
          "cudart-llama-bin-win-cuda-12.4-x64.zip", "llama-b10955-bin-win-cpu-x64.zip")
    ok += ("llama-b12359-bin-win-cuda-13.5-x64.zip", "cudart-llama-bin-win-cuda-13.5-x64.zip")
    no = ("llama-b10955-ui.tar.gz", "llama-b10955-xcframework.zip", "llama-b10955-bin-android-arm64.tar.gz",
          "llama-b10955-bin-ubuntu-sycl-fp16-x64.tar.gz", "llama-b10955-bin-win-cuda-13.4-arm64.zip",
          "nightly-tag.txt", "llama-b10955-bin-ubuntu-openvino-2026.3.1-x64.tar.gz",
          # New in b12359: Linux CUDA (VAF stays on Vulkan there), Windows arm64, Snapdragon, s390x.
          "llama-b12359-bin-ubuntu-cuda-12.8-x64.tar.gz", "llama-b12359-bin-ubuntu-cuda-13.5-x64.tar.gz",
          "llama-b12359-bin-ubuntu-cuda-13.5-arm64.tar.gz", "cudart-llama-b12359-bin-ubuntu-cuda-12.8-x64.tar.gz",
          "cudart-llama-b12359-bin-ubuntu-cuda-13.5-x64.tar.gz", "llama-b12359-bin-win-cpu-arm64.zip",
          "llama-b12359-bin-win-vulkan-arm64.zip", "llama-b12359-bin-win-cuda-13.5-arm64.zip",
          "llama-b12359-bin-linux-arm64-snapdragon.tar.gz", "llama-b12359-bin-ubuntu-26.04-s390x.tar.gz",
          "llama-b12359-bin-ubuntu-zdnn-s390x.tar.gz")
    assert all(backend.LLAMA_ASSET_PATTERN.fullmatch(n) for n in ok)
    assert not any(backend.LLAMA_ASSET_PATTERN.fullmatch(n) for n in no)


def test_a_missing_or_malformed_manifest_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(backend, "PIN_PATH", tmp_path / "nope.json")
    with pytest.raises(RuntimeError):
        backend.load_llama_pin()
    bad = tmp_path / "bad.json"
    bad.write_text('{"tag": "b10955", "assets": {"llama-b10955-bin-ubuntu-x64.tar.gz": {"sha256": "short"}}}', encoding="utf-8")
    monkeypatch.setattr(backend, "PIN_PATH", bad)
    with pytest.raises(RuntimeError):
        backend.load_llama_pin()


# ── picking ───────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("system,machine,vendor,expect", [
    ("Windows", "amd64", "nvidia", "bin-win-cuda-13"), ("Windows", "amd64", "amd", "bin-win-rocm"),
    ("Windows", "amd64", "intel", "bin-win-sycl"), ("Windows", "amd64", None, "bin-win-vulkan"),
    ("Darwin", "arm64", None, "bin-macos-arm64"), ("Darwin", "x86_64", None, "bin-macos-x64"),
    ("Linux", "x86_64", "nvidia", "bin-ubuntu-vulkan-x64"), ("Linux", "x86_64", "amd", "bin-ubuntu-vulkan-x64"),
    ("Linux", "x86_64", None, "bin-ubuntu-x64"), ("Linux", "aarch64", "nvidia", "bin-ubuntu-vulkan-arm64"),
    ("Linux", "aarch64", None, "bin-ubuntu-arm64"),
])
def test_every_platform_picks_a_pinned_asset(monkeypatch, system, machine, vendor, expect):
    monkeypatch.setattr(backend, "get_primary_gpu", lambda: SimpleNamespace(vendor=vendor) if vendor else None)
    pin = backend.load_llama_pin()
    chosen = _manager(system, machine).pinned_assets(pin)
    assert chosen and expect in chosen["main"]["name"], (system, machine, vendor, chosen)
    assert chosen["main"]["sha256"] == pin["assets"][chosen["main"]["name"]]["sha256"]
    assert chosen["main"]["url"].startswith(f"https://github.com/{backend.LLAMA_REPO}/releases/download/{pin['tag']}/")
    if vendor == "nvidia" and system == "Windows":
        ver = re.search(r"cuda-(\d+\.\d+)", chosen["main"]["name"]).group(1)
        assert chosen["dep"] and f"cuda-{ver}" in chosen["dep"]["name"], "the runtime archive of the binary's own CUDA version"
    else:
        assert chosen["dep"] is None


def test_a_platform_without_a_pinned_asset_gets_nothing(monkeypatch):
    monkeypatch.setattr(backend, "get_primary_gpu", lambda: None)
    pin = {"tag": "b10955", "assets": {"llama-b10955-bin-macos-arm64.tar.gz": {"sha256": "0" * 64, "size": 1}}}
    assert _manager("Linux").pinned_assets(pin) is None
    assert _manager("Windows", "amd64").pinned_assets(pin) is None


# ── the verified download ────────────────────────────────────────────────────────

def test_matching_bytes_land_and_mismatching_bytes_never_touch_the_target(monkeypatch, tmp_path):
    payload = b"llama" * 1000
    _fake_get(monkeypatch, payload)
    dest = tmp_path / "asset.tar.gz"
    assert download_verified("https://example.invalid/x", hashlib.sha256(payload).hexdigest(), dest) == dest
    assert dest.read_bytes() == payload and not dest.with_name("asset.tar.gz.part").exists()
    with pytest.raises(DownloadIntegrityError):
        download_verified("https://example.invalid/x", "ab" * 32, tmp_path / "other.tar.gz")
    assert not (tmp_path / "other.tar.gz").exists() and not (tmp_path / "other.tar.gz.part").exists()
    with pytest.raises(ValueError):
        download_verified("https://example.invalid/x", "", tmp_path / "third.tar.gz")


# ── the install ───────────────────────────────────────────────────────────────────

_VERSION_LINE = "version: {build} (deadbeef)\nbuilt with GNU 11.4.0 for Linux x86_64\n"


def _stub_server(path, build: int) -> None:
    """A stand-in for the binary whose BYTES are the version line the real one prints.
    The fixture's probe reads it, so nothing here has to be a program: a shell stub is
    one on Linux and macOS and is not one on Windows."""
    Path(path).write_text(_VERSION_LINE.format(build=build), encoding="utf-8")


def _archive_with_server(build: int) -> bytes:
    """A tar.gz shaped like a release archive: a top-level folder holding a llama-server
    that answers --version with the given build (a stand-in, see _stub_server)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        script = _VERSION_LINE.format(build=build).encode()
        info = tarfile.TarInfo(name="build/bin/llama-server")
        info.size = len(script)
        tar.addfile(info, io.BytesIO(script))
    return buf.getvalue()


@pytest.fixture
def linux_install(monkeypatch, tmp_path):
    payload = _archive_with_server(10955)
    pin = {"tag": "b10955", "repository": backend.LLAMA_REPO, "provenance": "test",
           "assets": {"llama-b10955-bin-ubuntu-x64.tar.gz": {"sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}}}
    monkeypatch.setattr(backend, "load_llama_pin", lambda: pin)
    monkeypatch.setattr(backend, "get_primary_gpu", lambda: None)
    monkeypatch.setattr(backend.UI, "event", lambda *a, **k: None)
    monkeypatch.setattr(backend.UI, "error", lambda *a, **k: None)
    # `llama-server --version` without starting a program: the stand-in binary is not
    # executable on every host, and what these tests are about is the decision the build
    # number drives. test_the_version_probe_really_runs_the_binary covers the exec where
    # a host can perform it.
    monkeypatch.setattr(backend.ServerManager, "_version_output",
                        lambda self, path=None: Path(path or self.server_path).read_text(encoding="utf-8", errors="replace"))
    # No llama server of this installation runs unless a test says so.
    monkeypatch.setattr(backend.ServerManager, "_llama_pid", lambda self: None)
    return _manager("Linux", tmp_path=tmp_path), payload


def test_the_pinned_build_on_disk_downloads_nothing(linux_install, monkeypatch):
    m, _ = linux_install
    Path(m.bin_dir).mkdir(parents=True); _stub_server(m.server_path, 10955)
    monkeypatch.setattr("vaf.core.verified_download.requests.get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no download")))
    assert m.ensure_server_exists() is True


def test_an_older_build_on_disk_is_replaced_whole_by_the_pinned_one(linux_install, monkeypatch):
    m, payload = linux_install
    Path(m.bin_dir).mkdir(parents=True); _stub_server(m.server_path, 10021)
    (Path(m.bin_dir) / "libggml-base.so.0.16.0").write_bytes(b"old library")
    assert m.installed_build() == 10021
    _fake_get(monkeypatch, payload)
    assert m.ensure_server_exists() is True
    assert m.installed_build() == 10955, "the pinned build replaced the old one"
    assert not (Path(m.bin_dir) / "libggml-base.so.0.16.0").exists(), "nothing of the old build stays next to the new one"
    assert not list(Path(m.bin_dir).glob("*.tar.gz")), "the archive is gone after extraction"
    assert not Path(m.bin_dir + ".staging").exists() and not Path(m.bin_dir + ".previous").exists()


def test_a_fresh_install_has_no_bin_dir_yet(linux_install, monkeypatch):
    m, payload = linux_install
    assert not Path(m.bin_dir).exists()
    _fake_get(monkeypatch, payload)
    assert m.ensure_server_exists() is True and m.installed_build() == 10955


@pytest.mark.skipif(os.name == "nt", reason="a shell stub is not a program on Windows")
def test_the_version_probe_really_runs_the_binary(tmp_path):
    """The one step the install tests above stand in for, exercised on a host that can
    perform it: _version_output starts the binary and installed_build reads its number.
    On Windows the stub cannot be started, which is why the other tests do not rely on
    it and why installed_build answers None there rather than raising
    (test_a_binary_that_cannot_be_started_reads_as_no_build)."""
    m = _manager("Linux", tmp_path=tmp_path)
    Path(m.bin_dir).mkdir(parents=True)
    Path(m.server_path).write_text("#!/bin/sh\necho 'version: 0.4.0-dev (build 10955, commit abc)'\n", encoding="utf-8")
    Path(m.server_path).chmod(0o755)
    assert "build 10955" in m._version_output()
    assert m.installed_build() == 10955


def test_a_binary_that_cannot_be_started_reads_as_no_build(linux_install, monkeypatch):
    """A binary that will not start answers no build: what Windows does with a shell stub,
    and what a truncated binary or one missing its CUDA runtime does anywhere. The number
    is unknown rather than wrong, so the pinned build is installed over it, and a launcher
    that still cannot read a number afterwards reports failure instead of starting
    something it cannot identify."""
    m, payload = linux_install
    Path(m.bin_dir).mkdir(parents=True); _stub_server(m.server_path, 10021)
    monkeypatch.setattr(backend.ServerManager, "_version_output",
                        lambda self, path=None: (_ for _ in ()).throw(OSError("Exec format error")))
    assert m.installed_build() is None
    _fake_get(monkeypatch, payload)
    assert m.ensure_server_exists() is False, "an unidentifiable binary is not a green light"


def test_both_shapes_of_the_version_line_yield_the_build_number():
    parse = backend.ServerManager.parse_build_number
    assert parse("version: 10021 (33a75f41c)\nbuilt with GNU 11.4.0 for Linux x86_64\n") == 10021
    assert parse("version: 0.4.0-dev (build 10955, commit 2f539596c)\nbuilt with ...") == 10955, \
        "the semver era prints the build in parentheses; the live install went red on this"
    assert parse("version: 1.0.0 (build 12359, commit b1b770d90)\nbuilt with ...") == 12359, "llama.cpp v1.0.0"
    assert parse("garbage") is None and parse("") is None


def test_bytes_that_do_not_hash_to_the_pin_leave_the_old_build_alone(linux_install, monkeypatch):
    """The person reads that nothing changed and what to do, not two 64-digit hashes
    (those follow as a detail line). MUTATION: put the exception text back as the error
    and this goes red."""
    m, payload = linux_install
    Path(m.bin_dir).mkdir(parents=True); _stub_server(m.server_path, 10021)
    errors = []
    monkeypatch.setattr(backend.UI, "error", lambda *a, **k: errors.append(" ".join(map(str, a))))
    _fake_get(monkeypatch, payload + b"tampered")
    assert m.ensure_server_exists() is False
    assert len(errors) == 1 and "nothing was changed" in errors[0] and "Try again later" in errors[0], errors
    assert not re.search(r"[0-9a-f]{64}", errors[0]), "no raw hash in the line a person reads"
    assert m.installed_build() == 10021 and not list(Path(m.bin_dir).glob("*.part")) and not list(Path(m.bin_dir).glob("*.tar.gz"))
    assert not Path(m.bin_dir + ".staging").exists(), "a refused download leaves no staging directory behind"


def test_the_launcher_never_asks_github_which_build_is_latest():
    src = (REPO / "vaf" / "core" / "backend.py").read_text(encoding="utf-8")
    assert "api.github.com" not in src and "releases/latest" not in src
    assert "download_verified(" in src, "every backend download goes through the verified primitive"


# ── bin/llama-cpp: the install's own folder ──────────────────────────────────────

def _archive(build: int, files=(), links=(), server: bool = True) -> bytes:
    """A release-shaped tar.gz: build/bin/llama-server (a stand-in, see _stub_server;
    left out with server=False), plus extra files and symbolic links in the same folder."""
    buf = io.BytesIO()
    head = (("llama-server", _VERSION_LINE.format(build=build).encode()),) if server else ()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in head + tuple(files):
            info = tarfile.TarInfo(name=f"build/bin/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for name, target in links:
            info = tarfile.TarInfo(name=f"build/bin/{name}")
            info.type, info.linkname = tarfile.SYMTYPE, target
            tar.addfile(info)
    return buf.getvalue()


def _pin(monkeypatch, payload: bytes) -> None:
    pin = {"tag": "b10955", "repository": backend.LLAMA_REPO, "provenance": "test",
           "assets": {"llama-b10955-bin-ubuntu-x64.tar.gz": {"sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}}}
    monkeypatch.setattr(backend, "load_llama_pin", lambda: pin)


def _tree(root: Path) -> dict:
    """Every entry below `root`: a file's bytes, a link's target, or a folder."""
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_symlink():
            out[rel] = ("link", os.readlink(p))
        elif p.is_file():
            out[rel] = ("file", p.read_bytes())
        else:
            out[rel] = ("dir",)
    return out


def _other_tenants(bin_root: Path) -> None:
    """What else lives in bin/: the vLLM environment and Piper."""
    (bin_root / "vllm" / "venv" / "lib").mkdir(parents=True)
    (bin_root / "vllm" / "venv" / "marker").write_bytes(b"a hash-locked vLLM environment")
    (bin_root / "vllm" / "venv" / "lib" / "torch.so").write_bytes(b"\x7fELF torch")
    (bin_root / "piper").mkdir()
    (bin_root / "piper" / "piper").write_bytes(b"\x7fELF piper")


def _snapshot(bin_root: Path) -> dict:
    return {d: _tree(bin_root / d) for d in ("vllm", "piper")}


@pytest.mark.parametrize("old_build", [None, 10021], ids=["install", "reinstall"])
def test_a_reinstall_keeps_the_other_tenants_of_bin(linux_install, monkeypatch, old_build):
    """The install swapped the whole of bin/ and rmtree'd the old one, the vLLM environment
    and Piper with it (measured: a bin/ of llama-server, piper, vllm held only llama-server
    afterwards). MUTATION: point bin_dir back at bin/ in _set_install_paths and this goes red."""
    m, payload = linux_install
    bin_root = Path(m.bin_root)
    bin_root.mkdir()
    _other_tenants(bin_root)
    if old_build:
        Path(m.bin_dir).mkdir(); _stub_server(m.server_path, old_build)
    before = _snapshot(bin_root)
    _fake_get(monkeypatch, payload)
    assert m.ensure_server_exists() is True
    assert Path(m.server_path) == bin_root / "llama-cpp" / "llama-server"
    assert m.installed_build() == 10955
    assert _snapshot(bin_root) == before, "bin/vllm and bin/piper are byte-identical after the install"
    assert sorted(p.name for p in bin_root.iterdir()) == ["llama-cpp", "piper", "vllm"]


def _legacy_layout(m) -> Path:
    """bin/ as an earlier VAF left it: the llama.cpp files at the top, next to the folders
    of the other tenants."""
    bin_root = Path(m.bin_root)
    bin_root.mkdir()
    _other_tenants(bin_root)
    _stub_server(m.legacy_server_path, 10021)
    (bin_root / "LICENSE").write_bytes(b"MIT License")
    (bin_root / "libllama.so.0.0.1").write_bytes(b"\x7fELF old libllama")
    try:
        os.symlink("libllama.so.0.0.1", bin_root / "libllama.so.0")
    except (OSError, NotImplementedError):
        pass        # Windows without the symlink privilege: the files alone still count
    return bin_root


def test_a_legacy_layout_moves_and_cleans_up(linux_install, monkeypatch):
    """MUTATION: skip _remove_legacy_layout and this goes red."""
    m, payload = linux_install
    bin_root = _legacy_layout(m)
    events = []
    monkeypatch.setattr(backend.UI, "event", lambda *a, **k: events.append(" ".join(map(str, a))))
    assert m.installed_build() is None, "a legacy binary never counts as installed"
    before = _snapshot(bin_root)
    _fake_get(monkeypatch, payload)
    assert m.ensure_server_exists() is True
    assert m.installed_build() == 10955
    assert sorted(p.name for p in bin_root.iterdir()) == ["llama-cpp", "piper", "vllm"], \
        "the top-level files and links of the old layout are gone, the folders stay"
    assert _snapshot(bin_root) == before
    assert any("Moving the backend into bin/llama-cpp" in e and "b10955" in e for e in events), events


def test_a_refused_download_leaves_a_legacy_layout_as_it_was(linux_install, monkeypatch):
    """The cleanup runs only after the new build is installed and verified. MUTATION: run
    it before the download and this goes red."""
    m, payload = linux_install
    bin_root = _legacy_layout(m)
    before = _tree(bin_root)
    _fake_get(monkeypatch, payload + b"tampered")
    assert m.ensure_server_exists() is False
    assert _tree(bin_root) == before, "nothing in bin/ changed"


class _Swap:
    """Records the stop and the renames of bin/llama-cpp and its staging folder, and plays
    Windows: a folder whose program runs cannot be renamed."""

    def __init__(self, m, monkeypatch, *, stop_reason=""):
        self.m, self.order, self.running = m, [], True
        self.stop_reason = stop_reason
        real_rename = os.rename
        swap = self

        def rename(src, dst):
            if os.path.abspath(src) in (os.path.abspath(m.bin_dir), os.path.abspath(m.bin_dir + ".staging")):
                if swap.running and os.path.abspath(src) == os.path.abspath(m.bin_dir):
                    raise PermissionError(32, "The process cannot access the file because it is being used by another process")
                swap.order.append(("rename", os.path.basename(src)))
            return real_rename(src, dst)

        def hand_over(self_, port=8080, wait=15.0, reason="the local engine is vLLM"):
            swap.order.append(("stop", reason))
            swap.port = port
            if swap.stop_reason:
                return swap.stop_reason
            swap.running = False
            return ""

        monkeypatch.setattr(backend.os, "rename", rename)
        monkeypatch.setattr(backend.ServerManager, "_llama_pid", lambda self_: 4242 if swap.running else None)
        monkeypatch.setattr(backend.ServerManager, "hand_over_from_llama", hand_over)


def test_a_running_old_server_is_stopped_before_the_swap(linux_install, monkeypatch):
    """MUTATION: drop the stop and the order assertion and the Windows rename both fail."""
    m, payload = linux_install
    Path(m.bin_dir).mkdir(parents=True); _stub_server(m.server_path, 10021)
    swap = _Swap(m, monkeypatch)
    _fake_get(monkeypatch, payload)
    assert m.ensure_server_exists() is True, "the stop frees the folder Windows would not rename"
    assert swap.order and swap.order[0][0] == "stop", swap.order
    assert "replaced by the pinned build" in swap.order[0][1]
    assert [s for s, _ in swap.order[1:]] == ["rename", "rename"], swap.order
    assert m.installed_build() == 10955


def test_a_server_that_will_not_stop_aborts_the_swap(linux_install, monkeypatch):
    """MUTATION: ignore hand_over_from_llama's answer and this goes red."""
    m, payload = linux_install
    Path(m.bin_dir).mkdir(parents=True); _stub_server(m.server_path, 10021)
    (Path(m.bin_dir) / "libggml-base.so.0.16.0").write_bytes(b"old library")
    before = _tree(Path(m.bin_dir))
    errors = []
    monkeypatch.setattr(backend.UI, "error", lambda *a, **k: errors.append(" ".join(map(str, a))))
    swap = _Swap(m, monkeypatch, stop_reason="llama-server (PID 4242) did not stop.")
    _fake_get(monkeypatch, payload)
    assert m.ensure_server_exists() is False
    assert [s for s, _ in swap.order] == ["stop"], "nothing is renamed"
    assert _tree(Path(m.bin_dir)) == before, "the old install stays exactly as it was"
    assert not Path(m.bin_dir + ".staging").exists()
    assert any("did not stop" in e and "was not installed" in e and "unchanged" in e and "start VAF again" in e
               for e in errors), errors


def test_an_up_to_date_build_stops_nothing(linux_install, monkeypatch):
    """MUTATION: stop the server before the version check and this goes red."""
    m, _ = linux_install
    Path(m.bin_dir).mkdir(parents=True); _stub_server(m.server_path, 10955)
    swap = _Swap(m, monkeypatch)
    monkeypatch.setattr("vaf.core.verified_download.requests.get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no download")))
    assert m.ensure_server_exists() is True
    assert swap.order == []


# ── the extraction (tarfile's data filter is required) ───────────────────────────

def test_extraction_without_the_data_filter_is_refused(linux_install, monkeypatch):
    """A Python whose tarfile has no `filter` argument (3.10.0 to 3.10.11) used to fall back
    to an unfiltered extractall. MUTATION: restore that fallback and this goes red."""
    m, payload = linux_install
    real = tarfile.TarFile.extractall

    def old_python(self, path=".", members=None, *, numeric_owner=False, **kwargs):
        if "filter" in kwargs:
            raise TypeError("extractall() got an unexpected keyword argument 'filter'")
        return real(self, path, members, numeric_owner=numeric_owner, filter="fully_trusted")

    monkeypatch.setattr(tarfile.TarFile, "extractall", old_python)
    errors = []
    monkeypatch.setattr(backend.UI, "error", lambda *a, **k: errors.append(" ".join(map(str, a))))
    _fake_get(monkeypatch, payload)
    assert m.ensure_server_exists() is False
    assert not Path(m.bin_dir).exists() and not Path(m.bin_dir + ".staging").exists(), "nothing is installed"
    assert any("data filter" in e and "3.11.4" in e for e in errors), errors


@pytest.mark.parametrize("target", ["../../../../etc/passwd", "/etc/passwd"], ids=["climbing", "absolute"])
def test_an_escaping_link_is_refused(linux_install, monkeypatch, target):
    """MUTATION: extract with filter="fully_trusted" and this goes red."""
    m, _ = linux_install
    payload = _archive(10955, links=[("libevil.so", target)])
    _pin(monkeypatch, payload)
    _fake_get(monkeypatch, payload)
    assert m.ensure_server_exists() is False
    assert not Path(m.bin_dir).exists() and not Path(m.bin_dir + ".staging").exists()


@pytest.mark.skipif(os.name == "nt", reason="a release's library links are a Linux and macOS shape")
def test_library_links_survive(linux_install, monkeypatch):
    """The SONAME chain of a release (libllama.so -> libllama.so.1 -> libllama.so.1.0.0)
    stays links next to llama-server. MUTATION: lift with a copy that follows links and
    this goes red."""
    m, _ = linux_install
    payload = _archive(10955, files=[("libllama.so.1.0.0", b"\x7fELF libllama")],
                       links=[("libllama.so.1", "libllama.so.1.0.0"), ("libllama.so", "libllama.so.1")])
    _pin(monkeypatch, payload)
    _fake_get(monkeypatch, payload)
    assert m.ensure_server_exists() is True
    lib = Path(m.bin_dir)
    assert (lib / "libllama.so").is_symlink() and (lib / "libllama.so.1").is_symlink(), "the links stay links"
    assert os.readlink(lib / "libllama.so") == "libllama.so.1"
    assert os.readlink(lib / "libllama.so.1") == "libllama.so.1.0.0"
    assert not (lib / "libllama.so.1.0.0").is_symlink()
    assert (lib / "libllama.so").read_bytes() == b"\x7fELF libllama"


# ── vaf info ──────────────────────────────────────────────────────────────────────

def test_vaf_info_names_a_legacy_install(linux_install):
    """A legacy binary is not "not installed": `vaf info` names its build, the old place
    and the move. MUTATION: print the plain line and this goes red."""
    from vaf.cli.cmd.info import llama_server_line
    m, _ = linux_install
    Path(m.bin_root).mkdir()
    _stub_server(m.legacy_server_path, 10955)
    line = llama_server_line(m, 12359)
    assert "not installed" not in line
    assert "b10955 in the old location" in line and "bin/llama-cpp" in line and "b12359" in line, line
    Path(m.bin_dir).mkdir(); _stub_server(m.server_path, 12359)
    assert "b12359 (release pins b12359, matches the pin)" in llama_server_line(m, 12359)


def test_vaf_info_names_an_unreadable_binary_as_present(linux_install, monkeypatch):
    """A binary in bin/llama-cpp whose version line cannot be read is there, not missing,
    and a legacy binary that is still in bin/ does not hide it. MUTATIONS: drop that
    branch (the line says "not installed" again), or check the legacy binary first (the
    current one is hidden): each goes red."""
    from vaf.cli.cmd.info import llama_server_line
    m, _ = linux_install
    Path(m.bin_dir).mkdir(parents=True); _stub_server(m.server_path, 12359)

    def version_output(self, path=None):
        if os.path.abspath(path or self.server_path) == os.path.abspath(m.server_path):
            raise OSError("Exec format error")
        return Path(path).read_text(encoding="utf-8")

    monkeypatch.setattr(backend.ServerManager, "_version_output", version_output)
    line = llama_server_line(m, 12359)
    assert "not installed" not in line and "version unreadable" in line and "bin/llama-cpp" in line, line
    _stub_server(m.legacy_server_path, 10955)     # a legacy binary the cleanup could not remove
    line = llama_server_line(m, 12359)
    assert "version unreadable" in line and "old location" not in line, line


# ── links after the lift, linked leftovers and tenants ───────────────────────────

def _can_symlink(tmp_path: Path) -> bool:
    try:
        os.symlink("x", tmp_path / ".probe-link")
    except (OSError, NotImplementedError):
        return False
    os.unlink(tmp_path / ".probe-link")
    return True


@pytest.mark.skipif(os.name == "nt", reason="tar links are not extracted as links without the symlink privilege")
@pytest.mark.parametrize("shape", [
    dict(links=[("d", "../x")]),                        # climbs one level: staging/build/x, then bin/x
    dict(links=[("lib-dir", "..")]),                    # names bin/llama-cpp's parent after the lift
    dict(links=[("llama-server", "../../victim")], server=False),   # the binary itself leaves
    # Names a check on edited text would read as another (harmless) name: trailing and lone
    # whitespace, a backslash, a newline, a tab, and one in a subfolder.
    dict(links=[("d ", "../../victim")]),
    dict(links=[(" ", "../../victim")]),
    dict(links=[("a\\b", "../../victim")]),
    dict(links=[("q\n", "../..")]),
    dict(links=[("d\t", "../../victim")]),
    dict(links=[("sub/e ", "../../../victim")]),
    # Out and back in by the folder's temporary name: inside while it is called
    # llama-cpp.staging, outside once it is renamed to llama-cpp.
    dict(links=[("d", "../llama-cpp.staging/x")]),
], ids=["file-link", "folder-link", "server-link", "trailing-space", "lone-space", "backslash",
        "newline", "tab", "nested-trailing-space", "staging-name"])
def test_a_link_that_leaves_the_folder_after_the_lift_is_refused(linux_install, monkeypatch, tmp_path, shape):
    """The data filter judges a link where it is unpacked (staging/build/bin), and those
    links stay inside staging there. The lift moves build/bin up two levels without
    rewriting them, and then they point out of bin/llama-cpp. MUTATIONS: drop the check
    after the lift (_refuse_escaping_links), decide a link on its name passed through
    contained_path (which strips whitespace and reads a backslash as a separator), or drop
    the check of the target as written (the staging-name shape): each goes red."""
    m, _ = linux_install
    victim = tmp_path / "victim"
    victim.write_bytes(b"not llama")
    victim.chmod(0o600)
    payload = _archive(10955, **shape)
    _pin(monkeypatch, payload)
    _fake_get(monkeypatch, payload)
    assert m.ensure_server_exists() is False
    assert not os.path.lexists(m.bin_dir) and not os.path.lexists(m.bin_dir + ".staging"), "nothing is installed"
    assert (victim.stat().st_mode & 0o777) == 0o600, "chmod never followed a link out of the folder"


def test_a_link_through_a_link_and_back_up_is_refused_whatever_the_folder_is_called(tmp_path):
    """d -> . makes d/.. the folder's PARENT, so t -> d/../llama-cpp.staging/x resolves
    inside while the folder is called llama-cpp.staging and outside once it is renamed. A
    patched data filter rewrites such a target while extracting; tarfile before the 2025
    fix leaves it as written, so the shape is built on disk. The release's library links
    and a leading ".." from a subfolder still pass, and stay inside after the rename.
    MUTATION: allow a ".." after a named part and the shape is accepted."""
    if not _can_symlink(tmp_path):
        pytest.skip("no symlink privilege")
    bin_root = tmp_path / "bin"
    root = bin_root / "llama-cpp.staging"
    (root / "sub").mkdir(parents=True)
    os.symlink(".", root / "d")
    os.symlink(os.path.join("d", "..", "llama-cpp.staging", "x"), root / "t")
    with pytest.raises(RuntimeError, match="after a named part"):
        backend.ServerManager._refuse_escaping_links(str(root))

    os.unlink(root / "t"); os.unlink(root / "d")
    (root / "libllama.so.1.0.0").write_bytes(b"lib")
    os.symlink("libllama.so.1.0.0", root / "libllama.so.1")
    os.symlink("libllama.so.1", root / "libllama.so")
    os.symlink(os.path.join("..", "libllama.so"), root / "sub" / "l")
    backend.ServerManager._refuse_escaping_links(str(root))
    final = bin_root / "llama-cpp"
    os.rename(root, final)
    for link in (final / "libllama.so", final / "libllama.so.1", final / "sub" / "l"):
        assert os.path.commonpath([os.path.realpath(link), os.path.realpath(final)]) == os.path.realpath(final)


def test_the_resolution_step_refuses_what_resolves_outside(tmp_path):
    """The last step decides a link on where it resolves. With the rules on the target as
    written, a link in a folder of the walk cannot reach it any more (a ".." after a named
    part is refused first, and deep/t -> s/.. with deep/s -> .. is now refused there); it
    stays as the guard of those rules. Exercised on the helper directly: a link whose
    target is harmless as written, asked about a root it does not lie in. MUTATION: drop
    the realpath containment and this goes red."""
    if not _can_symlink(tmp_path):
        pytest.skip("no symlink privilege")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "x").write_bytes(b"x")
    os.symlink("x", elsewhere / "l")
    root = tmp_path / "bin" / "llama-cpp.staging"
    root.mkdir(parents=True)
    reason = backend._link_leaves_folder(str(elsewhere / "l"), 0, os.path.realpath(root))
    assert reason.startswith("resolves outside"), reason
    assert backend._link_leaves_folder(str(elsewhere / "l"), 0, os.path.realpath(elsewhere)) == ""

    (root / "deep").mkdir()
    os.symlink("..", root / "deep" / "s")
    os.symlink(os.path.join("s", ".."), root / "deep" / "t")
    with pytest.raises(RuntimeError, match="after a named part"):
        backend.ServerManager._refuse_escaping_links(str(root))


@pytest.mark.skipif(os.name == "nt", reason="tar links are not extracted as links without the symlink privilege")
def test_a_chain_past_the_kernels_hop_limit_is_refused(linux_install, monkeypatch):
    """A chain a0 -> a1 -> ... -> a200 -> llama-server stays inside the folder (and under
    the archive's link cap), but resolving every link down the rest of the chain costs the
    square of its length. The kernel follows at most 40 hops; a chain it will not follow
    is refused, and the check stays linear. MUTATION: drop the os.stat step and the chain
    is accepted."""
    import time as _time
    m, _ = linux_install
    n = 200
    assert n <= backend.MAX_ARCHIVE_LINKS
    payload = _archive(10955, links=[(f"a{i}", f"a{i + 1}" if i + 1 < n else "llama-server") for i in range(n)])
    _pin(monkeypatch, payload)
    _fake_get(monkeypatch, payload)
    started = _time.monotonic()
    assert m.ensure_server_exists() is False
    assert _time.monotonic() - started < 10.0
    assert not os.path.lexists(m.bin_dir)


def test_an_archive_with_too_many_links_is_refused_before_unpacking(linux_install, monkeypatch):
    """A chain of 3000 links stored in reverse order: the data filter resolves every link
    member down the part of the chain already unpacked, the square of the length (26 s at
    4000). The link count is read first, and the archive is refused before anything is
    unpacked. MUTATION: drop the cap and the reason and the time bound both fail."""
    import time as _time
    m, _ = linux_install
    n = 3000
    chain = [(f"a{i}", f"a{i + 1}" if i + 1 < n else "llama-server") for i in range(n)]
    payload = _archive(10955, links=list(reversed(chain)))
    _pin(monkeypatch, payload)
    _fake_get(monkeypatch, payload)
    errors = []
    monkeypatch.setattr(backend.UI, "error", lambda *a, **k: errors.append(" ".join(map(str, a))))
    started = _time.monotonic()
    assert m.ensure_server_exists() is False
    assert _time.monotonic() - started < 3.0
    assert any(f"{n} links" in e for e in errors), errors
    assert not os.path.lexists(m.bin_dir) and not os.path.lexists(m.bin_dir + ".staging")


@pytest.mark.skipif(os.name == "nt", reason="tar links are not extracted as links without the symlink privilege")
def test_a_server_binary_that_is_a_link_is_refused(linux_install, monkeypatch):
    """Even a link that stays inside: chmod and the start would follow it, and a release's
    llama-server is a regular file. MUTATION: drop the lstat check and this goes red."""
    m, _ = linux_install
    payload = _archive(10955, server=False, files=[("llama-server-real", _VERSION_LINE.format(build=10955).encode())],
                       links=[("llama-server", "llama-server-real")])
    _pin(monkeypatch, payload)
    _fake_get(monkeypatch, payload)
    assert m.ensure_server_exists() is False
    assert not os.path.lexists(m.bin_dir)


def test_a_linked_install_folder_and_leftover_are_unlinked_not_followed(linux_install, monkeypatch, tmp_path):
    """bin/llama-cpp as a link (to a folder elsewhere) is swapped out as the link; rmtree
    refuses a link, so the old code left bin/llama-cpp.previous behind, and every later
    re-install died on it ("Not a directory"). MUTATION: remove leftovers with isdir +
    rmtree again and this goes red."""
    if not _can_symlink(tmp_path):
        pytest.skip("no symlink privilege")
    m, payload = linux_install
    elsewhere = tmp_path / "elsewhere" / "llama-cpp"
    elsewhere.mkdir(parents=True)
    _stub_server(elsewhere / "llama-server", 10021)
    Path(m.bin_root).mkdir()
    os.symlink(elsewhere, m.bin_dir)
    _fake_get(monkeypatch, payload)
    assert m.ensure_server_exists() is True
    assert not os.path.islink(m.bin_dir) and m.installed_build() == 10955
    assert not os.path.lexists(m.bin_dir + ".previous"), "the swapped-out link is gone"
    assert (elsewhere / "llama-server").read_text(encoding="utf-8") == _VERSION_LINE.format(build=10021), \
        "what the link pointed at is untouched"
    # A linked leftover from an earlier crash does not block the next install either.
    os.symlink(elsewhere, m.bin_dir + ".previous")
    _stub_server(m.server_path, 10021)
    assert m.ensure_server_exists() is True and m.installed_build() == 10955
    assert not os.path.lexists(m.bin_dir + ".previous") and (elsewhere / "llama-server").exists()


def test_a_linked_tenant_folder_survives_the_legacy_cleanup(linux_install, monkeypatch, tmp_path):
    """bin/vllm as a link to an environment on another disk is a tenant, not an archive
    file. MUTATION: decide with is_dir(follow_symlinks=False) again and this goes red."""
    if not _can_symlink(tmp_path):
        pytest.skip("no symlink privilege")
    m, payload = linux_install
    bin_root = Path(m.bin_root)
    bin_root.mkdir()
    other_disk = tmp_path / "other-disk" / "vllm"
    (other_disk / "venv").mkdir(parents=True)
    (other_disk / "venv" / "marker").write_bytes(b"vLLM")
    os.symlink(other_disk, bin_root / "vllm")
    _stub_server(m.legacy_server_path, 10021)
    _fake_get(monkeypatch, payload)
    assert m.ensure_server_exists() is True
    assert os.path.islink(bin_root / "vllm") and (bin_root / "vllm" / "venv" / "marker").read_bytes() == b"vLLM"
    assert sorted(p.name for p in bin_root.iterdir()) == ["llama-cpp", "vllm"]


def test_a_cleanup_that_could_not_finish_is_retried_on_the_next_start(linux_install, monkeypatch):
    """A locked file (Windows) stops the legacy cleanup: the person is told which file, the
    server binary stays as the key of the retry, and the next start (already up to date,
    no download) finishes it. MUTATIONS: drop the retry on the up-to-date path, drop the
    warning, unlink the server binary even after a failure, or drop the sort that puts it
    last: each goes red."""
    m, payload = linux_install
    bin_root = _legacy_layout(m)
    events = []
    monkeypatch.setattr(backend.UI, "event", lambda *a, **k: events.append(" ".join(map(str, a))))
    locked = {"name": "libllama.so.0.0.1"}
    real_unlink = os.unlink

    def unlink(path, *a, **k):
        if locked["name"] and os.path.basename(path) == locked["name"]:
            raise PermissionError(32, "The process cannot access the file because it is being used by another process")
        return real_unlink(path, *a, **k)

    monkeypatch.setattr(backend.os, "unlink", unlink)

    # bin/ is listed with the server binary FIRST, whatever the filesystem's own order: only
    # the cleanup's sort then keeps it for last.
    real_scandir = os.scandir

    class _Listing:
        def __init__(self, entries): self._it = iter(entries)
        def __iter__(self): return self
        def __next__(self): return next(self._it)
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def close(self): pass

    def scandir(path="."):
        if os.path.abspath(path) != os.path.abspath(m.bin_root):
            return real_scandir(path)
        return _Listing(sorted(real_scandir(path), key=lambda e: e.name != m.server_exe))

    monkeypatch.setattr(backend.os, "scandir", scandir)
    _fake_get(monkeypatch, payload)
    assert m.ensure_server_exists() is True, "the start does not fail on a leftover"
    assert any("Could not remove the old backend file bin/libllama.so.0.0.1" in e for e in events), events
    assert os.path.exists(m.legacy_server_path), "the server binary stays as the key of the retry"
    assert not (bin_root / "LICENSE").exists()

    locked["name"] = None
    monkeypatch.setattr("vaf.core.verified_download.requests.get",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no download")))
    assert m.ensure_server_exists() is True
    assert sorted(p.name for p in bin_root.iterdir()) == ["llama-cpp", "piper", "vllm"]


# ── a server that does not stop stays tracked ────────────────────────────────────

_REAL_LLAMA_PID = backend.ServerManager._llama_pid


def test_a_server_that_did_not_stop_is_stopped_on_the_retry(linux_install, monkeypatch):
    """Through the real hand_over_from_llama and stop_server, with the process faked:
    stop_server removes the pid file and the vision record before it knows whether the
    server died. When it did not, both were lost, the retry saw no server to stop, and on
    Windows the live server kept its folder locked for good. MUTATION: drop
    _keep_tracking and the retry goes red."""
    m, payload = linux_install
    m.process, m._log_file, m._vllm_started = None, None, False
    Path(m.bin_dir).mkdir(parents=True); _stub_server(m.server_path, 10021)
    Path(m.pid_file).write_text("4242", encoding="utf-8")
    Path(m._vision_state_file).write_text("/models/proj.gguf", encoding="utf-8")
    proc = {"alive": True, "wedged": True, "kills": 0}

    def kill(pid, sig):
        assert pid == 4242, "only the fake server is ever signalled"
        proc["kills"] += 1
        if not proc["wedged"]:
            proc["alive"] = False

    real_rename = os.rename

    def rename(src, dst):
        if proc["alive"] and os.path.abspath(src) == os.path.abspath(m.bin_dir):
            raise PermissionError(32, "The process cannot access the file because it is being used by another process")
        return real_rename(src, dst)

    clock = {"t": 0.0}
    monkeypatch.setattr(backend, "time", SimpleNamespace(
        monotonic=lambda: clock["t"], time=lambda: clock["t"],
        sleep=lambda s: clock.__setitem__("t", clock["t"] + s)))
    monkeypatch.setattr(backend.ServerManager, "_llama_pid", _REAL_LLAMA_PID)
    monkeypatch.setattr(backend.ServerManager, "_looks_like_llama_server", lambda self, pid: pid == 4242 and proc["alive"])
    monkeypatch.setattr(backend.ServerManager, "_is_process_running", lambda self, pid: pid == 4242 and proc["alive"])
    monkeypatch.setattr(backend, "_settle_after_stop", lambda port: None)
    monkeypatch.setattr(backend.os, "kill", kill)
    monkeypatch.setattr(backend.os, "rename", rename)
    _fake_get(monkeypatch, payload)

    assert m.ensure_server_exists() is False
    assert proc["kills"] == 1 and proc["alive"]
    assert Path(m.pid_file).read_text(encoding="utf-8").strip() == "4242", "the live server stays tracked"
    assert m._running_vision_state() == "/models/proj.gguf"
    assert m._llama_pid() == 4242

    proc["wedged"] = False
    assert m.ensure_server_exists() is True, "the retry stops the server and installs"
    assert proc["kills"] == 2 and not proc["alive"]
    assert m.installed_build() == 10955 and not Path(m.pid_file).exists()


def test_a_leftover_that_cannot_be_removed_ends_the_install_cleanly(linux_install, monkeypatch, tmp_path):
    """A linked bin/llama-cpp.staging that cannot be unlinked: the start gets False and a
    message, not an exception, nothing is downloaded, and the link's target is untouched.
    MUTATION: prepare the staging folder outside the handler again and this goes red."""
    if not _can_symlink(tmp_path):
        pytest.skip("no symlink privilege")
    m, _ = linux_install
    Path(m.bin_dir).mkdir(parents=True); _stub_server(m.server_path, 10021)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep").write_bytes(b"keep")
    staging = m.bin_dir + ".staging"
    os.symlink(elsewhere, staging)
    real_unlink = os.unlink

    def unlink(path, *a, **k):
        if os.path.abspath(path) == os.path.abspath(staging):
            raise PermissionError(13, "Permission denied")
        return real_unlink(path, *a, **k)

    errors = []
    monkeypatch.setattr(backend.os, "unlink", unlink)
    monkeypatch.setattr(backend.UI, "error", lambda *a, **k: errors.append(" ".join(map(str, a))))
    monkeypatch.setattr("vaf.core.verified_download.requests.get",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no download")))
    assert m.ensure_server_exists() is False
    assert any("leftover" in e and "start VAF again" in e for e in errors), errors
    assert os.path.islink(staging) and (elsewhere / "keep").read_bytes() == b"keep"
    assert m.installed_build() == 10021


def test_a_replaced_build_that_cannot_be_removed_does_not_fail_the_install(linux_install, monkeypatch):
    """The swap went through; only removing the replaced build failed. The new build runs,
    the person is told, the next install retries. MUTATION: let that failure reach the
    install's handler again and the install reports False."""
    m, payload = linux_install
    Path(m.bin_dir).mkdir(parents=True); _stub_server(m.server_path, 10021)
    events = []
    monkeypatch.setattr(backend.UI, "event", lambda *a, **k: events.append(" ".join(map(str, a))))
    real = backend.ServerManager._remove_tree_or_link

    def remove(path):
        if path.endswith(".previous") and os.path.lexists(path):
            raise PermissionError(5, "Access is denied")
        return real(path)

    monkeypatch.setattr(backend.ServerManager, "_remove_tree_or_link", staticmethod(remove))
    _fake_get(monkeypatch, payload)
    assert m.ensure_server_exists() is True
    assert m.installed_build() == 10955
    assert any("could not be removed" in e and "by hand" in e for e in events), events


def test_debris_a_leftover_removal_could_not_clear_stops_the_install(linux_install, monkeypatch):
    """rmtree(ignore_errors=True) skips what it cannot remove; the install must not then
    unpack on top of what is left. MUTATION: drop the check that the leftover is gone and
    the install goes ahead on top of the debris."""
    m, payload = linux_install
    Path(m.bin_dir).mkdir(parents=True); _stub_server(m.server_path, 10021)
    staging = Path(m.bin_dir + ".staging")
    (staging / "stuck").mkdir(parents=True)
    (staging / "stuck" / "debris").write_bytes(b"left by a refused archive")
    monkeypatch.setattr(backend.shutil, "rmtree", lambda path, ignore_errors=False, **k: None)
    errors = []
    monkeypatch.setattr(backend.UI, "error", lambda *a, **k: errors.append(" ".join(map(str, a))))
    _fake_get(monkeypatch, payload)
    assert m.ensure_server_exists() is False
    assert any("leftover" in e and "start VAF again" in e for e in errors), errors
    assert (staging / "stuck" / "debris").exists() and m.installed_build() == 10021


# ── the new build proves itself before the old one goes ──────────────────────────

def _failing_probe(monkeypatch, fails):
    """`llama-server --version` read from the stub's bytes, except where `fails(path)`."""
    def version_output(self, path=None):
        target = path or self.server_path
        if fails(target):
            raise OSError("error while loading shared libraries: libvulkan.so.1")
        return Path(target).read_text(encoding="utf-8", errors="replace")
    monkeypatch.setattr(backend.ServerManager, "_version_output", version_output)


def test_a_staged_build_that_does_not_start_leaves_the_old_server_and_install(linux_install, monkeypatch):
    """The checksum proves the bytes, not that they run here. The staged binary is asked
    for its build before the running server is stopped. MUTATIONS: drop that probe, or
    run it after the stop: each goes red."""
    m, payload = linux_install
    Path(m.bin_dir).mkdir(parents=True); _stub_server(m.server_path, 10021)
    before = _tree(Path(m.bin_dir))
    swap = _Swap(m, monkeypatch)
    _failing_probe(monkeypatch, lambda path: ".staging" in path)
    errors = []
    monkeypatch.setattr(backend.UI, "error", lambda *a, **k: errors.append(" ".join(map(str, a))))
    _fake_get(monkeypatch, payload)
    assert m.ensure_server_exists() is False
    assert swap.order == [], "the running server was not stopped and nothing was renamed"
    assert swap.running
    assert _tree(Path(m.bin_dir)) == before and not os.path.lexists(m.bin_dir + ".staging")
    assert any("does not start" in e and "unchanged" in e for e in errors), errors


def test_a_build_that_fails_at_its_final_place_puts_the_previous_one_back(linux_install, monkeypatch):
    """The replaced build stays until the new one also reports the pinned number where it
    will run. MUTATIONS: drop the roll-back, or remove .previous before that probe: each
    goes red."""
    m, payload = linux_install
    Path(m.bin_dir).mkdir(parents=True); _stub_server(m.server_path, 10021)
    before = _tree(Path(m.bin_dir))
    # The new build answers in staging and not once it sits in bin/llama-cpp.
    _failing_probe(monkeypatch, lambda path: os.path.abspath(path) == os.path.abspath(m.server_path)
                   and "10955" in Path(path).read_text(encoding="utf-8"))
    errors = []
    monkeypatch.setattr(backend.UI, "error", lambda *a, **k: errors.append(" ".join(map(str, a))))
    _fake_get(monkeypatch, payload)
    assert m.ensure_server_exists() is False
    assert _tree(Path(m.bin_dir)) == before, "the previous build is back in bin/llama-cpp"
    assert not os.path.lexists(m.bin_dir + ".previous")
    assert any("put back" in e for e in errors), errors


def _rename_fails(monkeypatch, m, *, also_restore: bool):
    real = os.rename
    state = {"armed": True}

    def rename(src, dst):
        if state["armed"] and os.path.abspath(dst) == os.path.abspath(m.bin_dir):
            if os.path.abspath(src) == os.path.abspath(m.bin_dir + ".staging"):
                raise PermissionError(5, "Access is denied")
            if also_restore and os.path.abspath(src) == os.path.abspath(m.bin_dir + ".previous"):
                raise PermissionError(5, "Access is denied")
        return real(src, dst)

    monkeypatch.setattr(backend.os, "rename", rename)
    return state


def test_a_failed_second_rename_puts_the_previous_build_back(linux_install, monkeypatch):
    """bin/llama-cpp was moved aside and staging could not take its place: the old build
    goes back instead of leaving no backend at all. MUTATION: drop the restore and this
    goes red."""
    m, payload = linux_install
    Path(m.bin_dir).mkdir(parents=True); _stub_server(m.server_path, 10021)
    before = _tree(Path(m.bin_dir))
    _rename_fails(monkeypatch, m, also_restore=False)
    _fake_get(monkeypatch, payload)
    assert m.ensure_server_exists() is False
    assert _tree(Path(m.bin_dir)) == before
    assert not os.path.lexists(m.bin_dir + ".previous") and not os.path.lexists(m.bin_dir + ".staging")


def test_a_previous_build_that_could_not_go_back_is_recovered_next_time(linux_install, monkeypatch):
    """The restore failed too: .previous is the only copy of the old build. The next
    attempt moves it back before anything treats it as a leftover; with a refused download
    that attempt then leaves the old build installed. MUTATION: drop the recovery at the
    start and .previous is deleted as a leftover, leaving no backend."""
    m, payload = linux_install
    Path(m.bin_dir).mkdir(parents=True); _stub_server(m.server_path, 10021)
    state = _rename_fails(monkeypatch, m, also_restore=True)
    _fake_get(monkeypatch, payload)
    assert m.ensure_server_exists() is False
    assert not os.path.lexists(m.bin_dir) and os.path.isdir(m.bin_dir + ".previous")

    state["armed"] = False
    _fake_get(monkeypatch, payload + b"tampered")
    assert m.ensure_server_exists() is False
    assert m.installed_build() == 10021, "the old build is back in bin/llama-cpp"
    assert not os.path.lexists(m.bin_dir + ".previous")


def test_the_install_stops_the_server_on_the_port_the_start_serves(linux_install, monkeypatch):
    """MUTATION: hand over on the default port again and this goes red."""
    m, payload = linux_install
    Path(m.bin_dir).mkdir(parents=True); _stub_server(m.server_path, 10021)
    swap = _Swap(m, monkeypatch)
    _fake_get(monkeypatch, payload)
    assert m.ensure_server_exists(port=8090) is True
    assert swap.port == 8090


def test_the_legacy_cleanup_removes_llama_cpp_files_only(linux_install, monkeypatch, tmp_path):
    """A tenant link whose disk is not mounted (is_dir() is False) and a file of another
    name stay; the llama.cpp files go. MUTATION: remove every non-folder entry again (no
    name check) and this goes red."""
    m, payload = linux_install
    bin_root = _legacy_layout(m)
    (bin_root / "notes.txt").write_bytes(b"mine")
    offline = None
    if _can_symlink(tmp_path):
        shutil_target = tmp_path / "unmounted-disk" / "vllm-env"
        os.symlink(shutil_target, bin_root / "vllm-offline")
        offline = bin_root / "vllm-offline"
    _fake_get(monkeypatch, payload)
    assert m.ensure_server_exists() is True
    assert (bin_root / "notes.txt").read_bytes() == b"mine"
    if offline is not None:
        assert os.path.islink(offline) and not offline.exists(), "the link to the offline disk is kept"
    assert not (bin_root / "LICENSE").exists() and not os.path.lexists(m.legacy_server_path)
    assert not os.path.lexists(bin_root / "libllama.so.0.0.1")


def test_a_plain_file_leftover_is_removed(linux_install, monkeypatch):
    """bin/llama-cpp.staging as a plain file (not a folder, not a link) is removed like
    any leftover. MUTATION: drop the plain-file branch and the install stops on it."""
    m, payload = linux_install
    Path(m.bin_root).mkdir()
    Path(m.bin_dir + ".staging").write_bytes(b"not a folder")
    _fake_get(monkeypatch, payload)
    assert m.ensure_server_exists() is True and m.installed_build() == 10955


# ── .previous is never lost while it is the only working build ───────────────────

def test_a_roll_back_whose_cleanup_fails_still_keeps_the_previous_build(linux_install, monkeypatch):
    """The new build fails at its final place and nothing can be removed (a locked file):
    the roll-back moves the folders by renames, so the previous build is back in
    bin/llama-cpp at once, and a later attempt cannot mistake it for a leftover. Removing
    bin/llama-cpp first left debris there, and the next start deleted .previous, the only
    working build. MUTATION: roll back by removing bin/llama-cpp first and this goes red."""
    m, payload = linux_install
    Path(m.bin_dir).mkdir(parents=True); _stub_server(m.server_path, 10021)
    before = _tree(Path(m.bin_dir))
    _failing_probe(monkeypatch, lambda path: os.path.abspath(path) == os.path.abspath(m.server_path)
                   and "10955" in Path(path).read_text(encoding="utf-8"))
    real_remove = backend.ServerManager._remove_tree_or_link

    def remove(path):
        if os.path.abspath(path) in (os.path.abspath(m.bin_dir), os.path.abspath(m.bin_dir + ".staging")) \
                and os.path.isdir(path):
            raise PermissionError(13, "Permission denied")       # leaves everything in place
        return real_remove(path)

    monkeypatch.setattr(backend.ServerManager, "_remove_tree_or_link", staticmethod(remove))
    errors = []
    monkeypatch.setattr(backend.UI, "error", lambda *a, **k: errors.append(" ".join(map(str, a))))
    _fake_get(monkeypatch, payload)
    assert m.ensure_server_exists() is False
    assert _tree(Path(m.bin_dir)) == before, "the previous build is back in bin/llama-cpp at once"
    assert any("was put back" in e for e in errors), errors

    _fake_get(monkeypatch, payload + b"tampered")
    m.ensure_server_exists()
    assert m.installed_build() == 10021, "a later attempt keeps the old build"


def test_a_crash_after_the_swap_with_a_build_that_does_not_start_recovers_the_previous_one(linux_install, monkeypatch):
    """The process died after the new folder took its place and before its probe there:
    bin/llama-cpp holds a build that does not start, .previous the old one that does. The
    next start puts .previous back before the leftover step could remove it. MUTATION:
    recover only when bin/llama-cpp is missing and .previous is deleted as a leftover."""
    m, payload = linux_install
    Path(m.bin_dir).mkdir(parents=True); _stub_server(m.server_path, 10955)
    Path(m.bin_dir + ".previous").mkdir(); _stub_server(Path(m.bin_dir + ".previous") / m.server_exe, 10021)
    _failing_probe(monkeypatch, lambda path: os.path.abspath(path) == os.path.abspath(m.server_path)
                   and "10955" in Path(path).read_text(encoding="utf-8"))
    events = []
    monkeypatch.setattr(backend.UI, "event", lambda *a, **k: events.append(" ".join(map(str, a))))
    _fake_get(monkeypatch, payload + b"tampered")
    assert m.ensure_server_exists() is False
    assert m.installed_build() == 10021, "the working build is back"
    assert not os.path.lexists(m.bin_dir + ".previous")
    assert any("Recovered the previous backend" in e for e in events), events


def test_a_previous_build_is_left_alone_while_the_current_one_works(linux_install, monkeypatch):
    """The recovery applies only when bin/llama-cpp reports no build. A working current
    build stays, and a .previous next to it is the leftover it looks like (here with a
    refused download, so the current build is what remains). MUTATION: recover whenever
    .previous reports a build and the older b9999 replaces the working b10021."""
    m, payload = linux_install
    Path(m.bin_dir).mkdir(parents=True); _stub_server(m.server_path, 10021)
    Path(m.bin_dir + ".previous").mkdir(); _stub_server(Path(m.bin_dir + ".previous") / m.server_exe, 9999)
    _fake_get(monkeypatch, payload + b"tampered")
    assert m.ensure_server_exists() is False
    assert m.installed_build() == 10021
    assert not os.path.lexists(m.bin_dir + ".previous")


def test_the_legacy_cleanup_leaves_the_installs_own_names_alone(linux_install, monkeypatch, tmp_path):
    """On the up-to-date path the legacy cleanup runs and the install's own recovery and
    leftover steps do not; a .previous link whose disk is offline (is_dir() False, a name
    the llama.cpp pattern matches) waits for them. MUTATION: drop the exclusion of the
    install's own names and this goes red."""
    if not _can_symlink(tmp_path):
        pytest.skip("no symlink privilege")
    m, _ = linux_install
    Path(m.bin_dir).mkdir(parents=True); _stub_server(m.server_path, 10955)
    _stub_server(m.legacy_server_path, 10021)
    os.symlink(tmp_path / "unmounted" / "llama-cpp", m.bin_dir + ".previous")
    monkeypatch.setattr("vaf.core.verified_download.requests.get",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no download")))
    assert m.ensure_server_exists() is True
    assert not os.path.lexists(m.legacy_server_path)
    assert os.path.islink(m.bin_dir + ".previous")


def test_a_partly_removed_new_build_next_to_previous_completes_the_roll_back(linux_install, monkeypatch):
    """The state a roll-back by removal left when the removal stopped partway: part of the
    new build in bin/llama-cpp (its llama-server already gone, a library left), the old
    build whole in .previous. The next start completes the roll-back before the leftover
    step, so a refused download still leaves the old build installed. MUTATION: recover
    only when bin/llama-cpp is missing and .previous is deleted as a leftover."""
    m, payload = linux_install
    Path(m.bin_dir).mkdir(parents=True)
    (Path(m.bin_dir) / "libggml-base.so.1").write_bytes(b"part of the new build")
    Path(m.bin_dir + ".previous").mkdir(); _stub_server(Path(m.bin_dir + ".previous") / m.server_exe, 10021)
    _fake_get(monkeypatch, payload + b"tampered")
    assert m.ensure_server_exists() is False
    assert m.installed_build() == 10021
    assert not (Path(m.bin_dir) / "libggml-base.so.1").exists(), "the debris went with the roll-back"


def test_a_verified_pinned_install_next_to_previous_stays_unchanged(linux_install, monkeypatch):
    """bin/llama-cpp already reports the pinned build: a .previous beside it (its removal
    failed after a good install) is not swapped back, and nothing is downloaded. MUTATION:
    recover whenever .previous reports a build and the pinned install is replaced."""
    m, _ = linux_install
    Path(m.bin_dir).mkdir(parents=True); _stub_server(m.server_path, 10955)
    before = _tree(Path(m.bin_dir))
    Path(m.bin_dir + ".previous").mkdir(); _stub_server(Path(m.bin_dir + ".previous") / m.server_exe, 10021)
    monkeypatch.setattr("vaf.core.verified_download.requests.get",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no download")))
    assert m.ensure_server_exists() is True
    assert _tree(Path(m.bin_dir)) == before
