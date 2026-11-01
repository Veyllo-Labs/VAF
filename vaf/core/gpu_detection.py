# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
GPU Detection and Support Management for VAF.

Supports:
- NVIDIA (CUDA)
- AMD (ROCm)
- Intel Arc (SYCL/OpenCL)
- Apple Silicon (Metal)

The GPU hide (``hide_gpu_from_inprocess_torch``) and its undo (``gpu_child_env``): VAF's own
entry points (the tray, the headless runner) hide the GPU from PyTorch in their process,
because in-process PyTorch users (embeddings, speech) pre-allocate GPU memory even for a
CPU-only model. The GPU engines VAF starts as children (the vLLM install and its probe, the
vLLM server, the llama server) must not inherit that hide: under the tray the vLLM probe once
reported "no CUDA GPU" on a machine whose GPU was fine, because the child got
``CUDA_VISIBLE_DEVICES=""``. So the hide records which variables IT set, and
``gpu_child_env`` takes exactly those out again; a value the user set is never touched.
Stdlib-only: the tray imports this module before anything that could load torch.
"""
import os
import re
import sys
import platform
import shutil
import subprocess
from typing import Optional, Dict, List, Mapping, Tuple
from pathlib import Path


# The names of the variables hide_gpu_from_inprocess_torch set itself (comma-separated). It is
# inherited by VAF's own children (sub-agents, the headless runner subprocess), so a GPU engine
# they start undoes the hide correctly too.
HIDE_RECORD = "VAF_GPU_HIDDEN_FOR_TORCH"
_HIDE = (("CUDA_VISIBLE_DEVICES", ""), ("PYTORCH_CUDA_ALLOC_CONF", "max_split_size_mb:32"))


def hide_gpu_from_inprocess_torch() -> None:
    """Hide the GPU from PyTorch in this process, before anything imports torch. Sets each
    variable of the hide that is not in the environment yet and records its name in
    ``HIDE_RECORD``; a variable the user set (also an empty one) is left as it is and not
    recorded. Idempotent: a second call (the headless runner imported into the tray) finds
    both set and the record in place."""
    recorded = [n for n in os.environ.get(HIDE_RECORD, "").split(",") if n]
    for name, value in _HIDE:
        if name in os.environ:
            continue
        os.environ[name] = value
        if name not in recorded:
            recorded.append(name)
    if recorded:
        os.environ[HIDE_RECORD] = ",".join(recorded)


def gpu_child_env(base: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """The environment for a GPU engine VAF starts as a child: a copy of ``base`` (default
    ``os.environ``) without every variable the hide set and without the record itself. A
    ``CUDA_VISIBLE_DEVICES`` the user set (no record entry) passes through unchanged, empty or
    not: choosing or hiding a GPU for the engines is the user's decision."""
    source = os.environ if base is None else base
    hidden = {n for n in str(source.get(HIDE_RECORD, "") or "").split(",") if n}
    return {k: v for k, v in source.items() if k != HIDE_RECORD and k not in hidden}


def _get_subprocess_kwargs() -> dict:
    """Get platform-specific kwargs for headless subprocess execution."""
    kwargs = {}
    if platform.system() == "Windows":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


class GPUInfo:
    """Information about a detected GPU."""
    def __init__(self, vendor: str, model: str = "", vram_mb: int = 0, 
                 driver_available: bool = False, compute_available: bool = False,
                 compute_capability: str = "", driver_version: str = "", gfx_arch: str = ""):
        self.vendor = vendor  # "nvidia", "amd", "intel", "apple"
        self.model = model
        self.vram_mb = vram_mb
        self.driver_available = driver_available
        self.compute_available = compute_available  # CUDA/ROCm/SYCL available
        # NVIDIA compute capability as nvidia-smi prints it ("8.6"); "" when unknown or
        # not an NVIDIA GPU. vLLM's CUDA build needs 7.5 or newer (vaf/core/local_engine.py).
        self.compute_capability = compute_capability
        # NVIDIA driver version as nvidia-smi prints it ("580.65.06"); "" when unknown. vLLM's
        # CUDA 13.0 build needs driver 580 or newer (vaf/core/local_engine.py).
        self.driver_version = driver_version
        # AMD GPU architecture as the amdgpu driver's KFD topology names it ("gfx1100"); ""
        # when unknown or not an AMD GPU. vLLM's ROCm build has kernels for a fixed list
        # (vaf/core/local_engine.py ROCM_GFX).
        self.gfx_arch = gfx_arch

    def __repr__(self):
        return f"GPUInfo(vendor={self.vendor}, model={self.model}, vram={self.vram_mb}MB, compute={self.compute_available})"


def detect_nvidia_gpu() -> Optional[GPUInfo]:
    """Detect NVIDIA GPU via nvidia-smi."""
    if not shutil.which("nvidia-smi"):
        return None
    
    try:
        # compute_cap and driver_version are asked for too (vLLM's hardware verdict needs
        # them). A driver too old to know compute_cap refuses the whole query, so the two-field
        # query it always answered runs again without them: detection must never lose the GPU
        # over the extra columns.
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,compute_cap,driver_version",
             "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
            **_get_subprocess_kwargs()
        )
        if result.returncode != 0:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=5,
                **_get_subprocess_kwargs()
            )
        
        if result.returncode != 0:
            return None
        
        lines = result.stdout.strip().splitlines()
        if not lines:
            return None
        
        # Parse first GPU
        line = lines[0].strip()
        parts = line.split(",")
        if len(parts) >= 2:
            model = parts[0].strip()
            vram_str = parts[1].strip()
            try:
                vram_mb = int(vram_str)
            except ValueError:
                vram_mb = 0
            
            compute_capability = parts[2].strip() if len(parts) >= 3 else ""
            if not re.fullmatch(r"\d+\.\d+", compute_capability):
                compute_capability = ""     # "[N/A]" or anything else that is no version
            driver_version = parts[3].strip() if len(parts) >= 4 else ""
            if not re.fullmatch(r"\d+(\.\d+)+", driver_version):
                driver_version = ""
            
            # Check if CUDA is actually available (not just driver)
            cuda_available = _check_cuda_available()
            
            return GPUInfo(
                vendor="nvidia",
                model=model,
                vram_mb=vram_mb,
                driver_available=True,
                compute_available=cuda_available,
                compute_capability=compute_capability,
                driver_version=driver_version,
            )
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        pass
    
    return None



def nvidia_free_vram_mb() -> int:
    """Free memory of the first NVIDIA GPU in MiB, from nvidia-smi; 0 when it cannot be read.

    The NVIDIA reader of the free figure: the llama server sizes its context with it, the vLLM
    engine its memory share and its fit check (``amd_vram_mb`` is the AMD reader)."""
    try:
        _r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if _r.returncode == 0:
            return int(_r.stdout.strip().splitlines()[0])
    except Exception:
        pass
    return 0

def _check_cuda_available() -> bool:
    """Check if CUDA runtime is actually available (not just driver)."""
    system = platform.system()
    
    if system == "Windows":
        # Check for CUDA DLLs in common locations
        cuda_paths = [
            os.path.join(os.environ.get("CUDA_PATH", ""), "bin", "cudart64_*.dll"),
            "C:\\Program Files\\NVIDIA GPU Computing Toolkit\\CUDA\\*\\bin\\cudart64_*.dll",
        ]
        # Also check if ggml-cuda.dll exists (indicates CUDA backend available)
        from vaf.core.backend import ServerManager
        sm = ServerManager()
        cuda_dll = os.path.join(sm.bin_dir, "ggml-cuda.dll")
        if os.path.exists(cuda_dll):
            return True
    elif system == "Linux":
        # Check for libcudart.so
        if shutil.which("nvcc"):
            return True
        # Check common library paths
        lib_paths = [
            "/usr/local/cuda/lib64/libcudart.so",
            "/usr/lib/x86_64-linux-gnu/libcudart.so",
        ]
        for path in lib_paths:
            if os.path.exists(path):
                return True
    
    # REMOVED: PyTorch fallback check - importing torch causes 1GB+ RAM explosion!
    # The DLL/library checks above are sufficient for CUDA detection.
    # If someone needs torch, the GPU hide above (hide_gpu_from_inprocess_torch) keeps it on the CPU.

    return False


# The vendor id of AMD in the KFD topology (PCI 0x1002).
_AMD_VENDOR_ID = 4098


def _read_text(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _read_int(path: Path) -> int:
    try:
        return int(_read_text(path).strip() or 0)
    except ValueError:
        return 0


def _gfx_name(target_version: int) -> str:
    """KFD's ``gfx_target_version`` as the gfx name: 110000 -> gfx1100, 90010 -> gfx90a,
    110501 -> gfx1151. The minor and the stepping are hex digits of the name."""
    if target_version <= 0:
        return ""
    major, minor, step = target_version // 10000, (target_version // 100) % 100, target_version % 100
    return f"gfx{major}{minor:x}{step:x}"


def amd_kfd_gpus(sys_root: str = "/sys") -> List[dict]:
    """Every AMD GPU the amdgpu driver's KFD topology lists, in node order:
    ``{"gfx", "render_minor", "vram_total_mb", "vram_used_mb"}``. Read from world-readable
    sysfs files, no subprocess and no root: ``class/kfd/kfd/topology/nodes/*/properties``
    (CPU nodes have ``simd_count 0``) and the render node's ``mem_info_vram_total`` and
    ``mem_info_vram_used`` in bytes (0 when unreadable). ``[]`` without KFD (no amdgpu driver,
    or not Linux). Never raises."""
    out: List[dict] = []
    try:
        nodes = Path(sys_root) / "class" / "kfd" / "kfd" / "topology" / "nodes"
        if not nodes.is_dir():
            return out
        entries = sorted((p for p in nodes.iterdir() if p.name.isdigit()), key=lambda p: int(p.name))
        for node in entries:
            props: Dict[str, int] = {}
            for line in _read_text(node / "properties").splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1].lstrip("-").isdigit():
                    props[parts[0]] = int(parts[1])
            if props.get("simd_count", 0) <= 0 or props.get("vendor_id") != _AMD_VENDOR_ID:
                continue
            minor = props.get("drm_render_minor", -1)
            device = Path(sys_root) / "class" / "drm" / f"renderD{minor}" / "device"
            out.append({"gfx": _gfx_name(props.get("gfx_target_version", 0)),
                        "render_minor": minor,
                        "vram_total_mb": _read_int(device / "mem_info_vram_total") // (1024 * 1024),
                        "vram_used_mb": _read_int(device / "mem_info_vram_used") // (1024 * 1024)})
    except Exception:
        return out
    return out


def _largest_amd_gpu(sys_root: str = "/sys") -> Optional[dict]:
    """The KFD node with the most VRAM: a discrete GPU before an APU's carve-out."""
    nodes = amd_kfd_gpus(sys_root)
    return max(nodes, key=lambda n: n["vram_total_mb"]) if nodes else None


def amd_vram_mb(sys_root: str = "/sys") -> Tuple[int, int]:
    """(free, total) MiB of the AMD GPU ``detect_amd_gpu`` reports (the KFD node with the most
    VRAM), from the amdgpu driver; ``(0, 0)`` when unknown. The AMD counterpart of
    ``nvidia_free_vram_mb``: the vLLM engine's memory share and its fit check read it."""
    node = _largest_amd_gpu(sys_root)
    if not node or node["vram_total_mb"] <= 0:
        return 0, 0
    total = node["vram_total_mb"]
    return max(0, total - node["vram_used_mb"]), total


# Where a distribution's ROCm packages put the HIP runtime: the linker finds it there without
# a RUNPATH. ROCm's own installer uses /opt/rocm[-<version>].
_DISTRO_LIB_DIRS = ("usr/lib64", "usr/lib/x86_64-linux-gnu", "usr/lib")
# The HIP runtime vLLM's ROCm build links against (torch's libc10_hip.so and vLLM's
# _rocm_C.abi3.so NEED it; the wheels do not bundle it).
HIP_RUNTIME = "libamdhip64.so.7"


def _rocm_version(rocm_root: Path) -> str:
    """"7.2.3" from ``<rocm>/.info/version`` ("7.2.3-123"); "" when unreadable."""
    m = re.match(r"\s*(\d+\.\d+(?:\.\d+)?)", _read_text(rocm_root / ".info" / "version"))
    return m.group(1) if m else ""


def rocm_runtime(env: Optional[Mapping[str, str]] = None, root: str = "/") -> Tuple[bool, str]:
    """(the HIP runtime ``libamdhip64.so.7`` is installed, ROCm's version or ""). Looked for in
    ``$ROCM_PATH/lib``, ``/opt/rocm/lib``, ``/opt/rocm-*/lib`` (newest first) and the
    distribution library directories; the version is read from that ROCm root's
    ``.info/version`` ("" for a distribution install, which has none). ``root`` prefixes the
    fixed paths (tests). No subprocess."""
    source = os.environ if env is None else env
    base = Path(root)
    roots: List[Path] = []
    if source.get("ROCM_PATH"):
        roots.append(Path(str(source["ROCM_PATH"])))
    roots.append(base / "opt" / "rocm")
    try:
        roots += sorted((p for p in (base / "opt").glob("rocm-*") if p.is_dir()),
                        key=lambda p: p.name, reverse=True)
    except OSError:
        pass
    for rocm in roots:
        if (rocm / "lib" / HIP_RUNTIME).exists():
            return True, _rocm_version(rocm)
    for lib in _DISTRO_LIB_DIRS:
        if (base / lib / HIP_RUNTIME).exists():
            return True, ""
    return False, ""


def _amd_smi_on_path() -> bool:
    """rocm-smi or its successor amd-smi (ROCm 7) is installed: the sign that ROCm is."""
    return bool(shutil.which("rocm-smi") or shutil.which("amd-smi"))


def detect_amd_gpu(sys_root: str = "/sys") -> Optional[GPUInfo]:
    """Detect AMD GPU via rocm-smi/amd-smi plus the KFD topology, or Windows WMI/DXGI.

    On Linux the gate is rocm-smi or amd-smi on PATH (ROCm is installed). The product name
    comes from rocm-smi when it is there; the architecture and the VRAM come from the KFD node
    with the most VRAM (``amd_kfd_gpus``), and rocm-smi's VRAM line is only the fallback when
    KFD gives none (rocm-smi prints bytes, which its parser does not read)."""
    system = platform.system()

    if system == "Linux" and _amd_smi_on_path():
        try:
            model = "AMD GPU"
            vram_mb = 0
            smi_ok = False
            if shutil.which("rocm-smi"):
                result = subprocess.run(
                    ["rocm-smi", "--showid", "--showproductname", "--showmeminfo", "vram"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    **_get_subprocess_kwargs()
                )
                if result.returncode == 0:
                    smi_ok = True
                    for line in result.stdout.splitlines():
                        if "Card series" in line or "Card model" in line:
                            model = line.split(":")[-1].strip()
                        elif "vram" in line.lower() and "total" in line.lower():
                            parts = line.split()
                            for i, part in enumerate(parts):
                                if "MiB" in part or "MB" in part:
                                    try:
                                        vram_mb = int(parts[i-1])
                                    except (ValueError, IndexError):
                                        pass
            node = _largest_amd_gpu(sys_root)
            if smi_ok or node is not None:
                if node is not None and node["vram_total_mb"] > 0:
                    vram_mb = node["vram_total_mb"]
                return GPUInfo(
                    vendor="amd",
                    model=model,
                    vram_mb=vram_mb,
                    driver_available=True,
                    compute_available=_check_rocm_available(),
                    gfx_arch=node["gfx"] if node is not None else "",
                )
        except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
            pass
    
    # Windows: Check via DirectX/DXGI (requires wmic or PowerShell)
    if system == "Windows":
        try:
            # Try PowerShell to query GPU
            ps_cmd = [
                "powershell", "-Command",
                "Get-WmiObject Win32_VideoController | Where-Object {$_.Name -like '*AMD*' -or $_.Name -like '*Radeon*'} | Select-Object -First 1 Name, AdapterRAM"
            ]
            result = subprocess.run(
                ps_cmd,
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            
            if result.returncode == 0 and result.stdout.strip():
                # Parse PowerShell output
                lines = result.stdout.strip().splitlines()
                model = "AMD GPU"
                vram_mb = 0
                
                for line in lines:
                    if "Name" in line:
                        parts = line.split(":")
                        if len(parts) > 1:
                            model = parts[1].strip()
                    elif "AdapterRAM" in line:
                        parts = line.split(":")
                        if len(parts) > 1:
                            try:
                                vram_bytes = int(parts[1].strip())
                                vram_mb = vram_bytes // (1024 * 1024)
                            except ValueError:
                                pass
                
                if model and "AMD" in model.upper() or "Radeon" in model.upper():
                    # ROCm not typically available on Windows, but check anyway
                    rocm_available = _check_rocm_available()
                    
                    return GPUInfo(
                        vendor="amd",
                        model=model,
                        vram_mb=vram_mb,
                        driver_available=True,
                        compute_available=rocm_available
                    )
        except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
            pass
    
    return None


def detect_intel_gpu() -> Optional[GPUInfo]:
    """Detect Intel Arc GPU via Windows WMI or Linux lspci."""
    system = platform.system()
    
    # Windows: Check via WMI
    if system == "Windows":
        try:
            ps_cmd = [
                "powershell", "-Command",
                "Get-WmiObject Win32_VideoController | Where-Object {$_.Name -like '*Intel*' -and ($_.Name -like '*Arc*' -or $_.Name -like '*Iris*' -or $_.Name -like '*UHD*')} | Select-Object -First 1 Name, AdapterRAM"
            ]
            result = subprocess.run(
                ps_cmd,
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            
            if result.returncode == 0 and result.stdout.strip():
                lines = result.stdout.strip().splitlines()
                model = "Intel GPU"
                vram_mb = 0
                
                for line in lines:
                    if "Name" in line:
                        parts = line.split(":")
                        if len(parts) > 1:
                            model = parts[1].strip()
                    elif "AdapterRAM" in line:
                        parts = line.split(":")
                        if len(parts) > 1:
                            try:
                                vram_bytes = int(parts[1].strip())
                                vram_mb = vram_bytes // (1024 * 1024)
                            except ValueError:
                                pass
                
                if model and ("Arc" in model or "Iris" in model or "UHD" in model):
                    sycl_available = _check_sycl_available()
                    
                    return GPUInfo(
                        vendor="intel",
                        model=model,
                        vram_mb=vram_mb,
                        driver_available=True,
                        compute_available=sycl_available
                    )
        except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
            pass
    
    # Linux: Check via lspci
    elif system == "Linux":
        if shutil.which("lspci"):
            try:
                result = subprocess.run(
                    ["lspci", "-v"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    **_get_subprocess_kwargs()
                )
                
                if result.returncode == 0:
                    for line in result.stdout.splitlines():
                        if "VGA" in line or "3D" in line:
                            if "Intel" in line and ("Arc" in line or "Iris" in line):
                                sycl_available = _check_sycl_available()
                                
                                return GPUInfo(
                                    vendor="intel",
                                    model=line.strip(),
                                    vram_mb=0,  # lspci doesn't show VRAM easily
                                    driver_available=True,
                                    compute_available=sycl_available
                                )
            except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
                pass
    
    return None


def detect_apple_silicon() -> Optional[GPUInfo]:
    """Detect Apple Silicon GPU (Metal)."""
    if platform.system() != "Darwin":
        return None
    
    try:
        # Check if it's Apple Silicon (ARM64)
        machine = platform.machine().lower()
        if machine in ("arm64", "aarch64"):
            # Unified memory: the GPU can address most of system RAM. Report a
            # conservative budget: 65% of hw.memsize (Metal's
            # recommendedMaxWorkingSetSize is ~65-75% of RAM), capped at RAM
            # minus 6 GB — macOS + the Colima/memory-db VM + the VAF backend +
            # the tray need that much to live, and on unified memory their RAM
            # and the "VRAM" are the SAME bytes. Without the reserve a 16 GB
            # Mac (the most common config) reported 10.4 GB, crossed the
            # 4B->9B model threshold by 0.4 GB and over-committed itself into
            # swap churn; with it, 16 GB reports 10.0 GB -> 4B tier, while
            # 18 GB and up are unaffected (the 65% cap binds there).
            # Previously this was hardcoded to 0 → _detect_vram_gb() treated
            # every Apple Silicon Mac as "no GPU" and `model: "auto"` always
            # picked the smallest 4B/Q4 model, regardless of 16/32/128 GB.
            vram_mb = 0
            try:
                _memsize = int(subprocess.check_output(
                    ["sysctl", "-n", "hw.memsize"], timeout=2,
                ).strip())
                _budget = min(_memsize * 0.65, _memsize - 6 * 1024**3)
                vram_mb = max(0, int(_budget / (1024 * 1024)))
            except Exception:
                pass  # vram_mb stays 0 → callers fall back to the RAM path
            return GPUInfo(
                vendor="apple",
                model="Apple Silicon",
                vram_mb=vram_mb,  # unified-memory GPU working-set budget (see above)
                driver_available=True,
                compute_available=True  # Metal is built-in
            )
    except Exception:
        pass
    
    return None


def _check_rocm_available() -> bool:
    """Check if ROCm is available."""
    if platform.system() != "Linux":
        return False  # ROCm is Linux-only
    
    # Check for ROCm libraries
    rocm_paths = [
        "/opt/rocm/lib/libhip_hcc.so",
        "/opt/rocm/lib/libamdhip64.so",
        "/opt/rocm/lib/" + HIP_RUNTIME,
    ]
    
    for path in rocm_paths:
        if os.path.exists(path):
            return True
    
    # Check if rocm-smi works
    if shutil.which("rocm-smi"):
        try:
            result = subprocess.run(
                ["rocm-smi", "--version"],
                capture_output=True,
                timeout=2,
                **_get_subprocess_kwargs()
            )
            if result.returncode == 0:
                return True
        except:
            pass
    
    return False


def _check_sycl_available() -> bool:
    """Check if Intel SYCL/oneAPI is available."""
    # Check for oneAPI installation
    oneapi_paths = [
        os.path.join(os.environ.get("ONEAPI_ROOT", ""), "compiler", "latest"),
        "C:\\Program Files (x86)\\Intel\\oneAPI",
    ]
    
    for base_path in oneapi_paths:
        if os.path.exists(base_path):
            return True
    
    # Check environment variables
    if "ONEAPI_ROOT" in os.environ:
        return True
    
    return False


def detect_all_gpus() -> List[GPUInfo]:
    """Detect all available GPUs in the system."""
    gpus = []
    
    # Try each vendor
    nvidia = detect_nvidia_gpu()
    if nvidia:
        gpus.append(nvidia)
    
    amd = detect_amd_gpu()
    if amd:
        gpus.append(amd)
    
    intel = detect_intel_gpu()
    if intel:
        gpus.append(intel)
    
    apple = detect_apple_silicon()
    if apple:
        gpus.append(apple)
    
    return gpus


def get_primary_gpu() -> Optional[GPUInfo]:
    """Get the primary GPU (prefer NVIDIA > AMD > Intel > Apple)."""
    gpus = detect_all_gpus()
    
    if not gpus:
        return None
    
    # Priority order
    priority = {"nvidia": 0, "amd": 1, "intel": 2, "apple": 3}
    
    # Sort by priority
    gpus.sort(key=lambda g: priority.get(g.vendor, 99))
    
    return gpus[0]


def get_gpu_support_info() -> Dict[str, any]:
    """Get comprehensive GPU support information."""
    primary = get_primary_gpu()
    all_gpus = detect_all_gpus()
    
    return {
        "primary": primary.__dict__ if primary else None,
        "all": [gpu.__dict__ for gpu in all_gpus],
        "count": len(all_gpus),
        "recommended_backend": _get_recommended_backend(primary) if primary else "cpu"
    }


def _get_recommended_backend(gpu: GPUInfo) -> str:
    """Get recommended backend type for GPU."""
    if not gpu:
        return "cpu"
    
    if gpu.vendor == "nvidia" and gpu.compute_available:
        return "cuda"
    elif gpu.vendor == "amd" and gpu.compute_available:
        return "rocm"
    elif gpu.vendor == "intel" and gpu.compute_available:
        return "sycl"
    elif gpu.vendor == "apple":
        return "metal"
    else:
        return "cpu"  # Fallback to CPU if compute not available


# ─── Default local model ──────────────────────────────────────────────────────
# The default (model: "auto") is VRAM-adaptive (recommended_default_model): a Qwen3.5-4B on small cards
# (<= 10 GB) and a Qwen3.5-9B on larger cards (> 10 GB), unsloth GGUF. The quant is picked so the
# weights leave room for the desktop (~1.5-2 GB) and the KV cache -- not just for the weights alone.
#   https://huggingface.co/unsloth/Qwen3.5-4B-GGUF   https://huggingface.co/unsloth/Qwen3.5-9B-GGUF
_QWEN35_4B = "unsloth/Qwen3.5-4B-GGUF/Qwen3.5-4B-{quant}.gguf"
_QWEN35_9B = "unsloth/Qwen3.5-9B-GGUF/Qwen3.5-9B-{quant}.gguf"

# Alternative explicit pins (reference only; not the auto default).
_DEEPSEEK_R1_QWEN3_8B = "unsloth/DeepSeek-R1-0528-Qwen3-8B-GGUF/DeepSeek-R1-0528-Qwen3-8B-{quant}.gguf"
QWEN_4B_Q8 = "unsloth/Qwen3.5-4B-GGUF/Qwen3.5-4B-UD-Q8_K_XL.gguf"


def _detect_vram_gb() -> float:
    """Best-effort GPU memory budget of the primary GPU in GB (0.0 if no GPU / undetectable).

    Includes Apple Silicon: detect_apple_silicon() reports ~65% of unified
    memory as the GPU working-set budget. Without it every Mac read as 0.0
    and `model: "auto"` always resolved to the smallest 4B/Q4 model.
    """
    try:
        info = detect_nvidia_gpu() or detect_amd_gpu() or detect_apple_silicon()
        if info and getattr(info, "vram_mb", 0):
            return float(info.vram_mb) / 1024.0
    except Exception:
        pass
    return 0.0


def recommended_default_model(vram_gb: Optional[float] = None) -> str:
    """Default local model for `model: "auto"`: a VRAM-adaptive Qwen3.5 (unsloth GGUF). Small cards run
    a 4B model, larger cards a 9B model -- and the quant is chosen so the weights leave room for the
    desktop/compositor (~1.5-2 GB) and a usable context (the KV cache), not just for the weights alone.
    Pin a different model with an explicit "repo/file.gguf" in config to bypass this.

        9 - 10 GB   -> Qwen3.5-4B  UD-Q8_K_XL  (8-bit,   5.95 GB)   e.g. a 10 GB card + desktop
        8 GB        -> Qwen3.5-4B  Q6_K        (6-bit,   3.53 GB)   (8-bit leaves no room for context)
        < 8 GB      -> Qwen3.5-4B  Q4_K_M      (4-bit,   2.74 GB)
        > 10 GB: a 9B model, quant scaled by VRAM --
        11 - <12 GB -> Qwen3.5-9B  Q5_K_M      (5-bit,   6.58 GB)
        12 - <16 GB -> Qwen3.5-9B  Q6_K        (6-bit,   7.46 GB)
        16 - <20 GB -> Qwen3.5-9B  Q8_0        (8-bit,   9.53 GB)
        20 - <24 GB -> Qwen3.5-9B  UD-Q8_K_XL  (8-bit,  12.97 GB)
        >= 24 GB    -> Qwen3.5-9B  BF16        (16-bit, 17.92 GB)
    """
    if vram_gb is None:
        vram_gb = _detect_vram_gb()
    if vram_gb <= 10:
        # small cards: a 4B model, quant scaled so weights + KV cache fit alongside the ~1.8 GB desktop
        if vram_gb >= 9:
            q4 = "UD-Q8_K_XL"   # 8-bit (5.95 GB) -- 9-10 GB
        elif vram_gb >= 8:
            q4 = "Q6_K"         # 6-bit (3.53 GB) -- 8 GB (8-bit would leave no room for context)
        else:
            q4 = "Q4_K_M"       # 4-bit (2.74 GB) -- < 8 GB
        return _QWEN35_4B.format(quant=q4)
    # > 10 GB: a 9B model, quant scaled by VRAM (leave headroom for desktop ~1.8 GB + context KV)
    if vram_gb >= 24:
        quant = "BF16"
    elif vram_gb >= 20:
        quant = "UD-Q8_K_XL"
    elif vram_gb >= 16:
        quant = "Q8_0"
    elif vram_gb >= 12:
        quant = "Q6_K"
    else:  # 11 - <12 GB
        quant = "Q5_K_M"
    return _QWEN35_9B.format(quant=quant)

