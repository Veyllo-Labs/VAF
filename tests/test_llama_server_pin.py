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
    m, payload = linux_install
    Path(m.bin_dir).mkdir(parents=True); _stub_server(m.server_path, 10021)
    _fake_get(monkeypatch, payload + b"tampered")
    assert m.ensure_server_exists() is False
    assert m.installed_build() == 10021 and not list(Path(m.bin_dir).glob("*.part")) and not list(Path(m.bin_dir).glob("*.tar.gz"))
    assert not Path(m.bin_dir + ".staging").exists(), "a refused download leaves no staging directory behind"


def test_the_launcher_never_asks_github_which_build_is_latest():
    src = (REPO / "vaf" / "core" / "backend.py").read_text(encoding="utf-8")
    assert "api.github.com" not in src and "releases/latest" not in src
    assert "download_verified(" in src, "every backend download goes through the verified primitive"


# ── bin/llama-cpp: the install's own folder ──────────────────────────────────────

def _archive(build: int, files=(), links=()) -> bytes:
    """A release-shaped tar.gz: build/bin/llama-server (a stand-in, see _stub_server),
    plus extra files and symbolic links in the same folder."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in ((("llama-server", _VERSION_LINE.format(build=build).encode()),) + tuple(files)):
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
    assert any("did not stop" in e for e in errors), errors


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
    assert any("data filter" in e for e in errors), errors


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
