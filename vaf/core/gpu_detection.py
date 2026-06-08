"""
GPU Detection and Support Management for VAF.

Supports:
- NVIDIA (CUDA)
- AMD (ROCm)
- Intel Arc (SYCL/OpenCL)
- Apple Silicon (Metal)
"""
import os
import sys
import platform
import shutil
import subprocess
from typing import Optional, Dict, List, Tuple
from pathlib import Path


def _get_subprocess_kwargs() -> dict:
    """Get platform-specific kwargs for headless subprocess execution."""
    kwargs = {}
    if platform.system() == "Windows":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


class GPUInfo:
    """Information about a detected GPU."""
    def __init__(self, vendor: str, model: str = "", vram_mb: int = 0, 
                 driver_available: bool = False, compute_available: bool = False):
        self.vendor = vendor  # "nvidia", "amd", "intel", "apple"
        self.model = model
        self.vram_mb = vram_mb
        self.driver_available = driver_available
        self.compute_available = compute_available  # CUDA/ROCm/SYCL available
    
    def __repr__(self):
        return f"GPUInfo(vendor={self.vendor}, model={self.model}, vram={self.vram_mb}MB, compute={self.compute_available})"


def detect_nvidia_gpu() -> Optional[GPUInfo]:
    """Detect NVIDIA GPU via nvidia-smi."""
    if not shutil.which("nvidia-smi"):
        return None
    
    try:
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
            
            # Check if CUDA is actually available (not just driver)
            cuda_available = _check_cuda_available()
            
            return GPUInfo(
                vendor="nvidia",
                model=model,
                vram_mb=vram_mb,
                driver_available=True,
                compute_available=cuda_available
            )
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        pass
    
    return None


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
    # If someone needs torch, the embedding model will handle CUDA_VISIBLE_DEVICES.

    return False


def detect_amd_gpu() -> Optional[GPUInfo]:
    """Detect AMD GPU via rocm-smi or Windows WMI/DXGI."""
    system = platform.system()
    
    # Linux: Check rocm-smi
    if system == "Linux" and shutil.which("rocm-smi"):
        try:
            result = subprocess.run(
                ["rocm-smi", "--showid", "--showproductname", "--showmeminfo", "vram"],
                capture_output=True,
                text=True,
                timeout=5,
                **_get_subprocess_kwargs()
            )
            
            if result.returncode == 0:
                # Parse output
                model = "AMD GPU"
                vram_mb = 0
                
                for line in result.stdout.splitlines():
                    if "Card series" in line or "Card model" in line:
                        model = line.split(":")[-1].strip()
                    elif "vram" in line.lower() and "total" in line.lower():
                        # Extract memory value
                        parts = line.split()
                        for i, part in enumerate(parts):
                            if "MiB" in part or "MB" in part:
                                try:
                                    vram_mb = int(parts[i-1])
                                except (ValueError, IndexError):
                                    pass
                
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
            # Metal is always available on Apple Silicon
            return GPUInfo(
                vendor="apple",
                model="Apple Silicon",
                vram_mb=0,  # Unified memory, not easily queryable
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


# ─── Default local model (VRAM-aware) ─────────────────────────────────────────
# Original Gemma GGUF (Q8_0) from the llama.cpp org (ggml-org). E4B is the larger variant.
GEMMA_E2B_Q8 = "ggml-org/gemma-4-E2B-it-GGUF/gemma-4-E2B-it-Q8_0.gguf"
GEMMA_E4B_Q8 = "ggml-org/gemma-4-E4B-it-GGUF/gemma-4-E4B-it-Q8_0.gguf"


def recommended_default_model() -> str:
    """VRAM-aware default local model: gemma-4 E4B (Q8 GGUF) when the primary GPU has more than
    10 GB of VRAM, otherwise the smaller E2B. Falls back to E2B when VRAM is unknown/CPU-only."""
    try:
        gpu = get_primary_gpu()
        vram_mb = gpu.vram_mb if (gpu and gpu.vram_mb) else 0
    except Exception:
        vram_mb = 0
    return GEMMA_E4B_Q8 if vram_mb > 10 * 1024 else GEMMA_E2B_Q8

