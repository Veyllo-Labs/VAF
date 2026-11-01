# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""AMD GPU facts for vLLM's ROCm build (vaf/core/gpu_detection.py): the KFD topology reader,
the VRAM of the amdgpu driver, the HIP runtime and ROCm's version.

Everything reads a fake sysfs or filesystem tree under tmp_path; rocm-smi is faked. Nothing
needs an AMD GPU.

MUTATIONS, each turns the named test red:
- decode the gfx minor in decimal (gfx90a becomes gfx9010), or drop the simd_count or the
  vendor filter -> test_kfd_reader_decodes_and_filters;
- pick the first node instead of the one with the most VRAM ->
  test_detect_amd_prefers_the_largest_vram;
- return (used, total) or total twice -> test_amd_vram_free_is_total_minus_used;
- read .info/version unparsed, look only in /opt/rocm, or name a distribution directory for
  LD_LIBRARY_PATH -> test_rocm_runtime_finds_lib_and_version;
- take a relative $ROCM_PATH as it is -> test_a_relative_rocm_path_is_named_absolute.
"""
import subprocess
from pathlib import Path

from vaf.core import gpu_detection as gd

MIB = 1024 * 1024


def _node(sys_root: Path, index: int, *, gfx: int, simd: int = 96, vendor: int = 4098,
          minor: int = 128, total_mb: int = 0, used_mb: int = 0) -> None:
    node = sys_root / "class" / "kfd" / "kfd" / "topology" / "nodes" / str(index)
    node.mkdir(parents=True)
    (node / "properties").write_bytes(
        (f"cpu_cores_count 0\nsimd_count {simd}\ngfx_target_version {gfx}\n"
         f"vendor_id {vendor}\ndevice_id 29772\ndrm_render_minor {minor}\n").encode())
    device = sys_root / "class" / "drm" / f"renderD{minor}" / "device"
    device.mkdir(parents=True, exist_ok=True)
    if total_mb:
        (device / "mem_info_vram_total").write_bytes(f"{total_mb * MIB}\n".encode())
        (device / "mem_info_vram_used").write_bytes(f"{used_mb * MIB}\n".encode())


def test_kfd_reader_decodes_and_filters(tmp_path):
    """gfx_target_version decodes to the gfx name (minor and stepping are hex digits); a CPU
    node (simd_count 0) and another vendor's node are skipped; VRAM is read in MiB."""
    _node(tmp_path, 0, gfx=0, simd=0, minor=-1)                     # the CPU
    for i, gfx in enumerate((110000, 90010, 110501, 120001, 90402), start=1):
        _node(tmp_path, i, gfx=gfx, minor=127 + i, total_mb=1000 * i, used_mb=10 * i)
    _node(tmp_path, 6, gfx=110000, vendor=4318, minor=140, total_mb=8000)  # not AMD
    nodes = gd.amd_kfd_gpus(str(tmp_path))
    assert [n["gfx"] for n in nodes] == ["gfx1100", "gfx90a", "gfx1151", "gfx1201", "gfx942"]
    assert nodes[0] == {"gfx": "gfx1100", "render_minor": 128, "vram_total_mb": 1000,
                        "vram_used_mb": 10}
    assert gd.amd_kfd_gpus(str(tmp_path / "nothing")) == []


def test_kfd_node_without_memory_files_reads_zero(tmp_path):
    _node(tmp_path, 1, gfx=110000)
    assert gd.amd_kfd_gpus(str(tmp_path))[0]["vram_total_mb"] == 0
    assert gd.amd_vram_mb(str(tmp_path)) == (0, 0)


def _fake_rocm_smi(monkeypatch, stdout: str, tools=("rocm-smi",)):
    import shutil
    monkeypatch.setattr(gd.platform, "system", lambda: "Linux")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}" if name in tools else None)
    monkeypatch.setattr(gd, "_check_rocm_available", lambda: True)
    monkeypatch.setattr(gd.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 0, stdout=stdout, stderr=""))


_SMI_OUT = ("GPU[0]\t\t: Card series: \t\tAMD Radeon RX 7900 XTX\n"
            "GPU[0]\t\t: VRAM Total Memory (B): 25753026560\n")


def test_detect_amd_prefers_the_largest_vram(monkeypatch, tmp_path):
    """An APU's carve-out before a discrete GPU in node order: the dGPU is the one reported,
    its gfx and its VRAM from KFD (rocm-smi prints bytes, its parser reads none)."""
    _node(tmp_path, 1, gfx=110300, minor=128, total_mb=512)
    _node(tmp_path, 2, gfx=110000, minor=129, total_mb=24560, used_mb=560)
    _fake_rocm_smi(monkeypatch, _SMI_OUT)
    gpu = gd.detect_amd_gpu(sys_root=str(tmp_path))
    assert gpu is not None and gpu.vendor == "amd"
    assert (gpu.model, gpu.gfx_arch, gpu.vram_mb) == ("AMD Radeon RX 7900 XTX", "gfx1100", 24560)


def test_detect_amd_with_amd_smi_only(monkeypatch, tmp_path):
    """ROCm 7 ships amd-smi as rocm-smi's successor: either one is the sign ROCm is there."""
    _node(tmp_path, 1, gfx=120001, total_mb=16304)
    _fake_rocm_smi(monkeypatch, "", tools=("amd-smi",))
    gpu = gd.detect_amd_gpu(sys_root=str(tmp_path))
    assert gpu is not None and (gpu.gfx_arch, gpu.vram_mb) == ("gfx1201", 16304)
    _fake_rocm_smi(monkeypatch, "", tools=())
    assert gd.detect_amd_gpu(sys_root=str(tmp_path)) is None, "no ROCm tooling, no AMD GPU"


def test_amd_vram_free_is_total_minus_used(tmp_path):
    _node(tmp_path, 1, gfx=110300, minor=128, total_mb=512, used_mb=100)
    _node(tmp_path, 2, gfx=110000, minor=129, total_mb=24560, used_mb=1560)
    assert gd.amd_vram_mb(str(tmp_path)) == (23000, 24560)
    assert gd.amd_vram_mb(str(tmp_path / "none")) == (0, 0)


def test_rocm_runtime_finds_lib_and_version(tmp_path):
    """libamdhip64.so.7 under $ROCM_PATH, /opt/rocm[-x.y.z] or a distribution's library
    directory; the version from that root's .info/version ("" for a distribution install)."""
    root = tmp_path / "root"
    assert gd.rocm_runtime(env={}, root=str(root)) == (False, "")
    custom = tmp_path / "custom-rocm"
    (custom / "lib").mkdir(parents=True)
    (custom / "lib" / gd.HIP_RUNTIME).write_bytes(b"")
    (custom / ".info").mkdir()
    (custom / ".info" / "version").write_bytes(b"7.2.3-123\n")
    assert gd.rocm_runtime(env={"ROCM_PATH": str(custom)}, root=str(root)) == (True, "7.2.3")
    versioned = root / "opt" / "rocm-7.2.1"
    (versioned / "lib").mkdir(parents=True)
    (versioned / "lib" / gd.HIP_RUNTIME).write_bytes(b"")
    (versioned / ".info").mkdir()
    (versioned / ".info" / "version").write_bytes(b"7.2.1-40\n")
    assert gd.rocm_runtime(env={}, root=str(root)) == (True, "7.2.1")
    distro = tmp_path / "distro"
    (distro / "usr" / "lib64").mkdir(parents=True)
    (distro / "usr" / "lib64" / gd.HIP_RUNTIME).write_bytes(b"")
    assert gd.rocm_runtime(env={}, root=str(distro)) == (True, "")
    # Only a ROCm the linker does not find by itself is named for LD_LIBRARY_PATH.
    assert gd.rocm_library_dir(env={}, root=str(distro)) == ""
    assert gd.rocm_library_dir(env={}, root=str(root)) == str(versioned / "lib")
    assert gd.rocm_library_dir(env={"ROCM_PATH": str(custom)}, root=str(root)) == str(custom / "lib")
    assert gd.rocm_library_dir(env={}, root=str(tmp_path / "none")) == ""
    # The unversioned name of ROCm 6 is not the runtime the ROCm build links against.
    old = tmp_path / "old"
    (old / "opt" / "rocm" / "lib").mkdir(parents=True)
    (old / "opt" / "rocm" / "lib" / "libamdhip64.so.6").write_bytes(b"")
    assert gd.rocm_runtime(env={}, root=str(old)) == (False, "")


def test_a_relative_rocm_path_is_named_absolute(tmp_path, monkeypatch):
    """A relative $ROCM_PATH is named as an absolute directory: LD_LIBRARY_PATH of the server
    would read a relative entry against the server's own working directory."""
    custom = tmp_path / "rel-rocm"
    (custom / "lib").mkdir(parents=True)
    (custom / "lib" / gd.HIP_RUNTIME).write_bytes(b"")
    monkeypatch.chdir(tmp_path)
    found = gd.rocm_library_dir(env={"ROCM_PATH": "rel-rocm"}, root=str(tmp_path / "none"))
    assert Path(found).is_absolute(), found
    assert Path(found) == (custom / "lib").resolve() or Path(found) == custom / "lib"
