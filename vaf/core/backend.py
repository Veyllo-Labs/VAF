# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
import os
import sys
import platform
import shutil
import subprocess
import zipfile
import tarfile
import threading
import time
import requests
from pathlib import Path
from vaf.cli.ui import UI
from vaf.core.config import Config
from vaf.core.gpu_detection import get_primary_gpu
from vaf.core.log_helper import get_app_log_dir, get_dated_log_path, is_debug_logging_enabled
from vaf.core.platform import Platform


# Bare GGUF filenames of the built-in default models map back to their HuggingFace repo, so a config
# value without a repo prefix (e.g. "Qwen3.5-4B-UD-Q8_K_XL.gguf") can still be downloaded. Keyed by a
# lowercase filename prefix.
_KNOWN_MODEL_REPOS = {
    "qwen3.5-4b": "unsloth/Qwen3.5-4B-GGUF",
    "qwen3.5-9b": "unsloth/Qwen3.5-9B-GGUF",
    "deepseek-r1-0528-qwen3-8b": "unsloth/DeepSeek-R1-0528-Qwen3-8B-GGUF",
}


def _resolve_model_ref(name: str):
    """Resolve a config model value to (repo_id, filename) for a HuggingFace download.

        "owner/repo/file.gguf" (>= 2 slashes) -> ("owner/repo", "file.gguf")
        "owner/repo"           (1 slash)      -> ("owner/repo", "<repo>.gguf")
        "file.gguf"            (bare)         -> (known repo by prefix or None, "file.gguf")

    repo_id is None for a bare filename whose repo is unknown -- it cannot be downloaded, and the
    caller should fall back to the VRAM-adaptive default."""
    name = (name or "").strip()
    repo_id = None
    if name.count("/") >= 2:
        repo_id, filename = name.rsplit("/", 1)
    elif "/" in name:
        repo_id, filename = name, name.split("/")[-1] + ".gguf"
    else:
        filename = name
        low = filename.lower()
        for prefix, repo in _KNOWN_MODEL_REPOS.items():
            if low.startswith(prefix):
                repo_id = repo
                break
    if filename and not filename.lower().endswith(".gguf"):
        filename += ".gguf"
    return repo_id, filename


def get_loaded_model_id(port: int = 8080, timeout: float = 2.0):
    """Basename of the GGUF the llama server currently holds, or None.

    llama-server reports the -m path (or alias) as the model id on
    /v1/models. Needed since the voice lane can hold a DIFFERENT model on
    the ONE server (model swap): a healthy port no longer implies the
    right model.
    """
    try:
        r = requests.get(f"http://127.0.0.1:{port}/v1/models", timeout=timeout)
        if r.status_code != 200:
            return None
        data = (r.json().get("data") or [{}])[0]
        model_id = str(data.get("id") or "").strip()
        return os.path.basename(model_id) if model_id else None
    except Exception:
        return None


def _loaded_model_matches(model_path: str, port: int = 8080) -> bool:
    """True when the server on `port` holds exactly `model_path`'s file.
    Unknown (probe failed / no id) counts as MATCH so legacy behavior
    (reuse any healthy server) is preserved when the id is unreadable."""
    loaded = get_loaded_model_id(port)
    if not loaded:
        return True
    try:
        return loaded.lower() == os.path.basename(model_path or "").lower()
    except Exception:
        return True


_ENSURE_LOCAL_LOCK = threading.Lock()


def ensure_local_model(model_path: str, reason: str = "", skip_provider_gate: bool = False) -> bool:
    """Make the ONE llama server hold exactly `model_path` (blocking).

    The single-server invariant stays: this never spawns a second server -
    a mismatching healthy server is stopped and restarted with the wanted
    GGUF (start_server is model-aware). Used by the voice lane (dedicated
    voice model) and by the main lane's swap-back check. Serialized by a
    module lock so voice and main cannot fight over the swap.
    """
    if not model_path or not os.path.exists(model_path):
        return False
    with _ENSURE_LOCAL_LOCK:
        try:
            if _loaded_model_matches(model_path) and get_loaded_model_id() is not None:
                return True
            UI.event("Server", f"Swapping local model -> {os.path.basename(model_path)}"
                     + (f" ({reason})" if reason else ""), style="dim")
            mgr = ServerManager(skip_cleanup=True)
            return bool(mgr.start_server(model_path, skip_provider_gate=skip_provider_gate))
        except Exception as e:
            UI.event("Server", f"Local model swap failed: {e}", style="yellow")
            return False


def ensure_model_available(model_name, models_dir) -> str:
    """Resolve a model ref (auto / repo/file / known bare filename) and make sure its GGUF is on disk,
    downloading it from HuggingFace when missing -- then return the local path. THE single model-download
    entry point (the tray, the agent/headless worker and the CLI all go through here).

    - Concurrency: the download is wrapped in a cross-thread/process ``filelock`` so the tray, the web
      worker and a `vaf run` never fetch the same file at once or read a half-written file; a second
      caller blocks, then finds the finished file.
    - Progress: byte progress is mirrored into ``model_download_state.MODEL_DOWNLOAD`` so other threads
      can show a status / wait instead of racing the download.
    - Self-heal: if the configured model has no resolvable repo (an unrecognised bare filename) or its
      download fails, fall back to ``recommended_default_model()`` (the VRAM-adaptive default) and
      download THAT -- so "no model at all" recovers to a model that fits the GPU, never a dead start.

    Never ``sys.exit()``s. If even the VRAM default cannot be obtained (e.g. offline) the returned path
    may still be missing -- callers must check ``os.path.exists`` and surface a clear error."""
    from vaf.core.gpu_detection import recommended_default_model
    models_dir = Path(models_dir)

    def _present(fname: str) -> bool:
        return bool(fname) and (models_dir / fname).is_file()

    def _download(repo_id: str, fname: str) -> str:
        local = models_dir / fname
        models_dir.mkdir(parents=True, exist_ok=True)
        from filelock import FileLock
        # Serialize downloads across threads AND processes (filelock is already used in secure_store.py).
        with FileLock(str(models_dir / ".download.lock")):
            if local.is_file():                       # another caller finished while we waited on the lock
                return str(local)
            from vaf.core.model_download_state import MODEL_DOWNLOAD, make_state_tqdm
            from huggingface_hub import hf_hub_download
            UI.event("System", f"Downloading {fname} from {repo_id}...", style="warning")
            MODEL_DOWNLOAD.start(repo_id, fname)
            # Stream progress to the WebUI download banner (same channel a WebUI-initiated download uses),
            # so tray/auto (first-run) downloads are visible too -- not only WebUI-started ones.
            _stop_broadcast = None
            try:
                from vaf.core.web_interface import start_model_download_broadcast
                _stop_broadcast = start_model_download_broadcast()
            except Exception:
                _stop_broadcast = None
            try:
                hf_hub_download(repo_id=repo_id, filename=fname, local_dir=str(models_dir),
                                tqdm_class=make_state_tqdm())
            finally:
                MODEL_DOWNLOAD.finish()
                if _stop_broadcast:
                    try:
                        _stop_broadcast()
                    except Exception:
                        pass
                # Tell the WebUI the download finished: clears the banner and refreshes the model list.
                try:
                    from vaf.core.web_interface import get_web_interface
                    get_web_interface().push_update({
                        "type": "model_download_done",
                        "success": local.is_file(),
                        "models": [p.name for p in models_dir.glob("*.gguf")],
                    })
                except Exception:
                    pass
            UI.event("System", "Download complete", style="success")
            return str(local)

    name = (model_name or Config.get("model") or "").strip()
    if name.lower() == "auto":
        name = recommended_default_model()

    # 1) the configured model -- use it if present, else download it when its repo is known
    repo_id, filename = _resolve_model_ref(name)
    if _present(filename):
        return str(models_dir / filename)
    if repo_id and filename:
        try:
            return _download(repo_id, filename)
        except Exception as e:
            UI.error(f"Download of {filename} failed: {e}")

    # 2) self-heal -- nothing usable was configured/downloadable: pick a model that fits this VRAM
    auto_repo, auto_file = _resolve_model_ref(recommended_default_model())
    if _present(auto_file):
        return str(models_dir / auto_file)
    if auto_repo and auto_file and auto_file != filename:
        UI.event("System", f"No usable model for '{name or 'config'}' -- falling back to the VRAM-adaptive default ({auto_file}).", style="warning")
        try:
            return _download(auto_repo, auto_file)
        except Exception as e:
            UI.error(f"Fallback download failed: {e}")

    # 3) last resort -- return the resolved path so the caller surfaces a clear missing-file error
    return str(models_dir / (filename or auto_file or "model.gguf"))


def resolve_mmproj_for(model_path: str) -> str:
    """Local vision: path of the mmproj (multimodal projector) to launch the
    llama server with, or "" when local vision is off / unresolvable.

    Enabled by vision_provider == "local". The mmproj ref comes from
    `vision_local_mmproj` ("owner/repo/file.gguf"), else it is derived from
    the model's known repo (e.g. qwen3.5-4b -> unsloth/Qwen3.5-4B-GGUF +
    mmproj-F16.gguf). Download goes DIRECTLY through hf_hub (never through
    ensure_model_available: its self-heal would silently substitute a CHAT
    model for a failed mmproj fetch - fatal as a --mmproj argument).
    Fail-open to "": a missing mmproj must never block the text server.
    """
    from vaf.core.config import Config
    try:
        if (Config.get("vision_provider", "") or "").strip().lower() != "local":
            return ""
        models_dir = os.path.dirname(os.path.abspath(model_path))
        ref = (Config.get("vision_local_mmproj", "") or "").strip()
        if ref:
            repo_id, filename = _resolve_model_ref(ref)
        else:
            base = os.path.basename(model_path).lower()
            repo_id = None
            for prefix, repo in _KNOWN_MODEL_REPOS.items():
                if base.startswith(prefix):
                    repo_id = repo
                    break
            filename = "mmproj-F16.gguf"
        if not filename:
            return ""
        local = os.path.join(models_dir, filename)
        if os.path.exists(local):
            return local
        if not repo_id:
            return ""
        from huggingface_hub import hf_hub_download
        UI.event("System", f"Downloading vision projector {filename} ({repo_id})...", style="dim")
        got = hf_hub_download(repo_id=repo_id, filename=filename, local_dir=models_dir)
        return got if got and os.path.exists(got) else ""
    except Exception as e:
        UI.event("System", f"Vision projector unavailable: {e}", style="yellow")
        return ""


def local_server_multimodal(port: int = 8080) -> bool:
    """True when the running llama server reports image support. Defensive:
    any unreadable/absent capability info counts as False."""
    try:
        r = requests.get(f"http://127.0.0.1:{port}/v1/models", timeout=2)
        if r.status_code != 200:
            return False
        return "multimodal" in r.text.lower() or "image" in r.text.lower()
    except Exception:
        return False


def _server_ready_budget() -> float:
    """Seconds to wait for llama-server to reach /health == 200 (weights loaded, context
    allocated). A cold multi-GB GGUF on a slow disk or CPU-only box can need minutes, so
    the default is generous and configurable (server_ready_timeout); never below 60s."""
    try:
        return max(60.0, float(Config.get("server_ready_timeout", 600)))
    except (TypeError, ValueError):
        return 600.0


class ServerManager:
    """
    Manages the lifecycle of the standalone llama-server executable.
    This bypasses python bindings for robust GPU support.
    """
    
    # We pin a stable version to ensure predictable asset names
    # Using b4320 as a recent stable reference or we could try to resolve "latest"
    # For reliability, let's use a specific build tag that we know exists
    LLAMA_TAG = "b4320" 
    
    def __init__(self, skip_cleanup: bool = False):
        self.base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.bin_dir = os.path.join(self.base_dir, "bin")
        self.process = None
        self._log_file = None  # Server log file handle
        self._job_handle = None  # Windows Job Object for process group management
        # Memo: this backend/model combination died on quantized V cache (needs Flash
        # Attention). Skips the doomed first attempt on later restarts (idle unload /
        # config change) so each reload does not re-pay a full model load + crash.
        self._kv_vquant_unsupported = False
        
        # PID file for tracking server process (survives crashes)
        self.pid_file = os.path.join(os.path.expanduser("~"), ".vaf", "server.pid")
        
        # Determine platform specifics
        self.system = platform.system()
        self.machine = platform.machine().lower()
        
        self.server_exe = "llama-server"
        if self.system == "Windows":
             self.server_exe += ".exe"
             
        self.server_path = os.path.join(self.bin_dir, self.server_exe)
        
        # Cleanup any orphaned server from previous crash (unless skipped)
        if not skip_cleanup:
            self._cleanup_orphan_server()
        
        # Windows: Create Job Object to ensure child process terminates with parent
        if self.system == "Windows":
            self._create_job_object()

    def get_model_path(self, model_name: str | None = None) -> str:
        """
        Resolve the local model file path from config or input name.
        Handles repo-style names and ensures .gguf extension.
        """
        from pathlib import Path
        from vaf.core.config import Config

        name = (model_name or Config.get("model") or "").strip()
        # "auto" -> VRAM-aware default (gemma-4 E4B/E2B Q8). The server/tray path builds the model path
        # independently of the Agent, so it must expand the sentinel here too (else it looks for auto.gguf).
        if name.lower() == "auto":
            from vaf.core.gpu_detection import recommended_default_model
            name = recommended_default_model()
        if not name:
            return str(Path(self.base_dir) / "models" / "VQ-1_Instruct-q4_k_m.gguf")

        candidate = Path(name)
        if candidate.is_file():
            return str(candidate.resolve())

        # If a repo/path was supplied, use the filename portion.
        filename = name.rsplit("/", 1)[-1]
        if not filename.lower().endswith(".gguf"):
            filename += ".gguf"

        return str(Path(self.base_dir) / "models" / filename)

    def ensure_model_present(self, model_name: str | None = None) -> str:
        """Resolve + download (locked, self-healing) the configured model and return its local path.
        The tray/server auto-start uses this instead of get_model_path(), which only builds a path and
        would otherwise launch llama-server against a non-existent file. Thin wrapper around the shared
        module-level ``ensure_model_available`` so every load path uses one implementation."""
        return ensure_model_available(model_name, Path(self.base_dir) / "models")

    def __del__(self):
        """Destructor: Clean up job object handle on Windows."""
        if hasattr(self, 'system') and self.system == "Windows" and hasattr(self, '_job_handle') and self._job_handle:
            try:
                import ctypes
                ctypes.windll.kernel32.CloseHandle(self._job_handle)
            except Exception:
                pass
    
    def _create_job_object(self):
        """
        Windows-specific: Create a Job Object to ensure child processes terminate with parent.
        This prevents orphaned llama-server processes when the terminal is closed.
        """
        try:
            import ctypes
            from ctypes import wintypes
            
            # Windows API constants
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
            
            # Create Job Object
            job_name = None  # Anonymous job
            self._job_handle = ctypes.windll.kernel32.CreateJobObjectW(None, job_name)
            
            if not self._job_handle:
                return  # Failed to create job, continue anyway
            
            # Configure job to kill all processes when the job handle is closed
            class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", ctypes.c_byte * 64),  # JOBOBJECT_BASIC_LIMIT_INFORMATION
                    ("IoInfo", ctypes.c_byte * 32),  # IO_COUNTERS
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]
            
            job_info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            # Set the flag in BasicLimitInformation.LimitFlags (offset 16 bytes, DWORD)
            limit_flags_offset = 16
            ctypes.memmove(
                ctypes.addressof(job_info) + limit_flags_offset,
                ctypes.byref(wintypes.DWORD(JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)),
                ctypes.sizeof(wintypes.DWORD)
            )
            
            # Set job information
            JobObjectExtendedLimitInformation = 9
            ctypes.windll.kernel32.SetInformationJobObject(
                self._job_handle,
                JobObjectExtendedLimitInformation,
                ctypes.byref(job_info),
                ctypes.sizeof(job_info)
            )
            
        except Exception:
            # If job object creation fails, continue without it
            # The process will still be managed via PID tracking
            self._job_handle = None
    
    def _cleanup_orphan_server(self):
        """Kill any orphaned server process from a previous crash."""
        if not os.path.exists(self.pid_file):
            return
        
        try:
            with open(self.pid_file, 'r') as f:
                old_pid = int(f.read().strip())
            
            # Check if process is still running
            if self._is_process_running(old_pid):
                # WICHTIG: Prüfe zuerst, ob der Server noch antwortet
                # Wenn er antwortet, ist er NICHT orphaned, sondern aktiv!
                try:
                    response = requests.get("http://127.0.0.1:8080/health", timeout=2)
                    if response.status_code == 200:
                        # Server läuft und antwortet - NICHT orphaned, sondern aktiv!
                        # Lass ihn laufen, entferne nur die PID-Datei nicht
                        return
                except:
                    # Server läuft, aber antwortet nicht - wirklich orphaned
                    pass
                
                # Server läuft, aber antwortet nicht - wirklich orphaned
                UI.event("System", f"Found orphaned server (PID {old_pid}), cleaning up...", style="yellow")
                self._kill_process(old_pid)
                time.sleep(0.5)
            
            # Remove stale PID file (nur wenn Server nicht mehr läuft oder nicht antwortet)
            try:
                os.remove(self.pid_file)
            except:
                pass
        except (ValueError, FileNotFoundError, PermissionError):
            # PID file corrupt or inaccessible, try to remove it
            try:
                os.remove(self.pid_file)
            except:
                pass
    
    def _is_process_running(self, pid: int) -> bool:
        """Check if a process with given PID is running (cross-platform)."""
        if self.system == "Windows":
            try:
                # Use tasklist to check if process exists
                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                    capture_output=True, text=True, encoding='utf-8', errors='replace',
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
                return str(pid) in result.stdout
            except:
                return False
        else:
            # Unix: send signal 0 to check if process exists
            try:
                os.kill(pid, 0)
                return True
            except (OSError, ProcessLookupError):
                return False
    
    def _kill_process(self, pid: int):
        """Kill a process by PID (cross-platform)."""
        try:
            if self.system == "Windows":
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    capture_output=True, encoding='utf-8', errors='replace',
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
            else:
                os.kill(pid, 9)  # SIGKILL
        except:
            pass
    
    def _save_pid(self, pid: int):
        """Save server PID to file for crash recovery."""
        try:
            os.makedirs(os.path.dirname(self.pid_file), exist_ok=True)
            with open(self.pid_file, 'w') as f:
                f.write(str(pid))
        except:
            pass
    
    def _remove_pid(self):
        """Remove PID file on clean shutdown."""
        try:
            if os.path.exists(self.pid_file):
                os.remove(self.pid_file)
        except:
            pass

    def resolve_latest_release(self):
        """Fetches the latest release data from GitHub API to avoid 404s."""
        try:
            # Simple timeout to prevent hanging
            resp = requests.get("https://api.github.com/repos/ggerganov/llama.cpp/releases/latest", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                tag = data.get("tag_name", "b4400") 
                assets = data.get("assets", [])
                return tag, assets
        except:
            pass
        return None, []

    def get_asset_url(self):
        """Returns main binary asset and optional dependency asset URLs dynamically."""
        
        # 1. Try Dynamic Resolution
        tag, assets = self.resolve_latest_release()
        
        main_url = None
        main_name = None
        dep_url = None
        dep_name = None

        if tag and assets:
            self.LLAMA_TAG = tag
            
            # Helper to find asset by partial name
            def find_asset(keywords, exclude=None):
                for a in assets:
                    name = a["name"]
                    if exclude and any(e in name for e in exclude): continue
                    if all(k in name for k in keywords):
                        return a["browser_download_url"], name
                return None, None

            # Detect GPU to choose appropriate binary
            primary_gpu = get_primary_gpu()
            gpu_type = primary_gpu.vendor if primary_gpu else None
            
            if self.system == "Windows":
                # Try GPU-specific binaries first
                if gpu_type == "amd":
                    # AMD - HIP/Radeon binary
                    main_url, main_name = find_asset(["bin-win-hip-radeon", "x64.zip"])
                    if not main_url:
                        # Fallback to Vulkan (works with AMD too)
                        main_url, main_name = find_asset(["bin-win-vulkan", "x64.zip"])
                elif gpu_type == "intel":
                    # Intel - SYCL binary
                    main_url, main_name = find_asset(["bin-win-sycl", "x64.zip"])
                    if not main_url:
                        # Fallback to Vulkan
                        main_url, main_name = find_asset(["bin-win-vulkan", "x64.zip"])
                elif gpu_type == "nvidia":
                    # NVIDIA - CUDA binary (prefer CUDA 13, fallback to CUDA 12)
                    main_url, main_name = find_asset(["bin-win-cuda-13", "x64.zip"], exclude=["cudart"])
                    if not main_url:
                        main_url, main_name = find_asset(["bin-win-cuda-12", "x64.zip"], exclude=["cudart"])
                    if main_url:
                        dep_url, dep_name = find_asset(["cudart-llama", "bin-win-cuda", "x64.zip"])
                else:
                    # No GPU or unknown - try Vulkan (universal), then CPU
                    main_url, main_name = find_asset(["bin-win-vulkan", "x64.zip"])
                    if not main_url:
                        main_url, main_name = find_asset(["bin-win-cpu", "x64.zip"])

            elif self.system == "Darwin":
                 # macOS binaries are .tar.gz, not .zip
                 keyword = "bin-macos-arm64.tar.gz" if ("arm64" in self.machine or "aarch64" in self.machine) else "bin-macos-x64.tar.gz"
                 main_url, main_name = find_asset([keyword])

            elif self.system == "Linux":
                # Linux: Use Vulkan for GPU (NVIDIA + AMD both support it, no CUDA toolkit needed).
                # CUDA binary requires libcudart (CUDA toolkit), which is often not installed even
                # when the NVIDIA driver is present. Vulkan only needs libvulkan (always available
                # with any modern NVIDIA/AMD driver).
                if gpu_type in ("nvidia", "amd", "intel"):
                    main_url, main_name = find_asset(["bin-ubuntu-vulkan", "x64.tar.gz"])

                # Fallback to CPU if GPU-specific not found
                if not main_url:
                    main_url, main_name = find_asset(["bin-ubuntu-x64.tar.gz"])

        # 2. Check if we found it. If NOT, Fallback.
        if main_url:
            return main_url, main_name, dep_url, dep_name
            
        # FALLBACK LOGIC (Offline / Rate Limit / Parse Failure)
        tag = "b4320" # Known stable
        self.LLAMA_TAG = tag
        base_url = f"https://github.com/ggerganov/llama.cpp/releases/download/{tag}"
        
        if self.system == "Windows":
            # Fallback: Use GPU detection to choose binary
            primary_gpu = get_primary_gpu()
            gpu_type = primary_gpu.vendor if primary_gpu else None
            
            if gpu_type == "amd":
                # AMD - HIP/Radeon
                main_name = f"llama-{tag}-bin-win-hip-radeon-x64.zip"
            elif gpu_type == "intel":
                # Intel - SYCL
                main_name = f"llama-{tag}-bin-win-sycl-x64.zip"
            elif gpu_type == "nvidia":
                # NVIDIA - CUDA 12.4 (most common)
                main_name = f"llama-{tag}-bin-win-cuda-12.4-x64.zip"
                dep_name = f"cudart-llama-bin-win-cuda-12.4-x64.zip"
                dep_url = f"{base_url}/{dep_name}"
            else:
                # No GPU or unknown - Vulkan (universal)
                main_name = f"llama-{tag}-bin-win-vulkan-x64.zip"
            
            main_url = f"{base_url}/{main_name}"
            return main_url, main_name, dep_url if gpu_type == "nvidia" else None, dep_name if gpu_type == "nvidia" else None
             
        elif self.system == "Darwin":
             is_arm = "arm64" in self.machine or "aarch64" in self.machine
             # macOS binaries are .tar.gz, not .zip
             main_name = f"llama-{tag}-bin-macos-{'arm64' if is_arm else 'x64'}.tar.gz"
             main_url = f"{base_url}/{main_name}"
             return main_url, main_name, None, None
             
        elif self.system == "Linux":
             # Prefer Vulkan for GPU systems; CPU fallback for headless/no-GPU
             primary_gpu = get_primary_gpu()
             if primary_gpu and primary_gpu.vendor in ("nvidia", "amd", "intel"):
                 main_name = f"llama-{tag}-bin-ubuntu-vulkan-x64.tar.gz"
             else:
                 main_name = f"llama-{tag}-bin-ubuntu-x64.tar.gz"
             main_url = f"{base_url}/{main_name}"
             return main_url, main_name, None, None
             
        return None, None, None, None

    def ensure_server_exists(self):
        if not os.path.exists(self.bin_dir):
            os.makedirs(self.bin_dir)

        # 0. Check if already installed (Fast Path / Offline Support)
        if os.path.exists(self.server_path):
             return True
            
        url, filename, dep_url, dep_filename = self.get_asset_url()
        
        # Verify if we actually need to download
        if not url:
             UI.error("Could not resolve backend URLs.")
             return False
            
        zip_path = os.path.join(self.bin_dir, filename)
        
        UI.event("System", f"Downloading Backend ({filename})...", style="warning")
        try:
            with requests.get(url, stream=True) as r:
                r.raise_for_status()
                with open(zip_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
                        
            UI.event("System", "Extracting backend...", style="dim")
            # Handle both .zip and .tar.gz files
            if filename.endswith('.tar.gz') or filename.endswith('.tgz'):
                with tarfile.open(zip_path, 'r:gz') as tar_ref:
                    tar_ref.extractall(self.bin_dir)
            else:
                # Assume .zip
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(self.bin_dir)
            
            # Cleanup
            os.remove(zip_path)
            
            # Post-Extraction: Check for nested directory (common in GitHub releases)
            # If server_path doesn't exist, search for it in subfolders
            if not os.path.exists(self.server_path):
                found_path = None
                for root, dirs, files in os.walk(self.bin_dir):
                    if self.server_exe in files:
                        found_path = os.path.join(root, self.server_exe)
                        break
                
                if found_path and found_path != self.server_path:
                    UI.event("System", f"Found backend in subfolder, moving files...", style="dim")
                    parent_dir = os.path.dirname(found_path)
                    
                    # Move all files from subfolder to bin_dir
                    for item in os.listdir(parent_dir):
                        src = os.path.join(parent_dir, item)
                        dst = os.path.join(self.bin_dir, item)
                        if os.path.exists(dst):
                            if os.path.isdir(dst): shutil.rmtree(dst)
                            else: os.remove(dst)
                        shutil.move(src, dst)
                    
                    # Remove the now empty subfolder
                    try:
                        os.rmdir(parent_dir)
                    except:
                        pass # Might not be empty if hidden files exist
            
            if self.system != "Windows":
                 os.chmod(self.server_path, 0o755)
                 
            UI.event("System", "Backend installed successfully.", style="success")
            return True
            
        except Exception as e:
            UI.error(f"Backend download failed: {e}")
            return False

    def start_server(self, model_path, n_gpu_layers=99, n_ctx=32768, port=8080,
                     skip_provider_gate=False):
        """
        Start llama-server only if provider is 'local' and auto-start is enabled.

        Best Practice: Skip server startup when using API providers to save resources.

        skip_provider_gate: the voice lane can run a dedicated LOCAL voice
        model while the MAIN provider is an API - it explicitly asks for a
        local server, so the provider gate must not veto it.

        Reuse is MODEL-AWARE: since the voice lane can swap the ONE server
        to a different GGUF, a healthy port no longer implies the wanted
        model - a mismatching server is stopped and respawned with
        `model_path` instead of being silently reused (a main-agent turn
        must never run on the voice model or vice versa).
        """
        from vaf.core.config import Config

        provider = Config.get("provider", "local")
        auto_start = Config.get("auto_start_local_server", True)

        if not skip_provider_gate:
            # Skip server start if using API provider
            if provider != "local":
                UI.event("Backend", f"Using API provider: {provider}, skipping local server", style="dim")
                return True

            # Skip if auto-start is disabled (user wants manual control)
            if not auto_start:
                UI.event("Backend", "Local server auto-start disabled in settings", style="dim")
                return True

        if not self.ensure_server_exists():
            return False

        # If a server is already listening WITH THE WANTED MODEL, reuse it.
        try:
            response = requests.get(f"http://127.0.0.1:{port}/health", timeout=1)
            if response.status_code == 200:
                if _loaded_model_matches(model_path, port):
                    self.process = None  # Reuse existing external process
                    UI.event("Server", f"Reusing existing server on :{port}...", style="dim")
                    return True
                UI.event("Server",
                         f"Server on :{port} holds a different model - restarting with "
                         f"{os.path.basename(model_path)}", style="dim")
                # fall through to the restart path below
            # If it's still loading (503), wait for it instead of killing and respawning:
            # a re-entered start (tray retry) must not restart a server that is making
            # progress. Same budget as the readiness wait below - a cold multi-GB GGUF
            # on a slow disk can legitimately need minutes.
            elif response.status_code == 503:
                wait_start = time.time()
                while time.time() - wait_start < _server_ready_budget():
                    try:
                        response = requests.get(f"http://127.0.0.1:{port}/health", timeout=1)
                        if response.status_code == 200:
                            if _loaded_model_matches(model_path, port):
                                self.process = None
                                UI.event("Server", f"Reusing existing server on :{port}...", style="dim")
                                return True
                            break  # loaded, but the wrong model -> restart below
                    except Exception:
                        pass
                    time.sleep(0.5)
        except Exception:
            pass
        
        # PRÜFE ZUERST, OB SERVER BEREITS LÄUFT
        # Check if server is already running by checking PID file and health endpoint
        if os.path.exists(self.pid_file):
            try:
                with open(self.pid_file, 'r', encoding='utf-8') as f:
                    existing_pid = int(f.read().strip())
                
                # Check if process is still running
                if self._is_process_running(existing_pid):
                    # Check if server is responding (with the wanted model)
                    try:
                        response = requests.get(f"http://127.0.0.1:{port}/health", timeout=1)
                        if response.status_code == 200 and _loaded_model_matches(model_path, port):
                            # Server läuft bereits, verwende ihn
                            self.process = None  # Wir verwalten diesen Prozess nicht direkt
                            UI.event("Server", f"Reusing existing server on :{port}...", style="dim")
                            return True
                    except:
                        # Server läuft, aber antwortet nicht - kill und neu starten
                        pass
            except (ValueError, FileNotFoundError):
                pass
        
        # Server läuft nicht oder antwortet nicht - starte neu
        # Stop existing if any (nur wenn wirklich nötig - erzwinge Kill)
        _had_server = False
        try:
            _had_server = requests.get(f"http://127.0.0.1:{port}/health", timeout=0.5).status_code in (200, 503)
        except Exception:
            pass
        self.stop_server(force_external=True)
        if _had_server:
            # Model swap: CUDA frees the old model's VRAM asynchronously after
            # the kill. Without settling, the free-VRAM probe below still sees
            # the OLD model resident and panics the n_ctx cap into the
            # KV-to-CPU fallback (live incident: "only ~4096 ctx fits" right
            # after a voice/main swap). Wait briefly until the port is dead
            # and the driver has reclaimed the memory.
            for _ in range(10):
                try:
                    requests.get(f"http://127.0.0.1:{port}/health", timeout=0.5)
                    time.sleep(0.5)
                    continue
                except Exception:
                    break
            time.sleep(1.5)
        
        # ═══════════════════════════════════════════════════════════════════
        # N_PARALLEL: DRIVEN BY MODEL SIZE (GB), NOT BY TOKENS
        # ═══════════════════════════════════════════════════════════════════
        # Goal: Avoid RAM/VRAM explosion. n_parallel is decided by MODEL SIZE IN GB
        # and available VRAM/RAM; context (tokens) is only used to check if 2 slots fit.
        # Strategy:
        # - Priority 1: Model size in GB vs VRAM — if model alone uses too much, 1 slot.
        # - Priority 2: Model + 2× context + overhead must fit in VRAM for 2 slots.
        # - Priority 3: VRAM < 12GB or model > ~6GB → always 1 slot to be safe.
        
        import psutil
        from vaf.core.gpu_detection import get_primary_gpu
        
        # 1. Get System Stats
        total_ram_gb = psutil.virtual_memory().total / (1024**3)
        gpu = get_primary_gpu()
        
        # Local vision (vision_provider=local): resolve the mmproj BEFORE the
        # VRAM math - its weights (~0.6-1 GB) are resident once --mmproj is
        # passed, and a size estimate that ignores them re-creates the exact
        # over-allocation SIGSEGV the n_ctx cap exists to prevent.
        mmproj_path = resolve_mmproj_for(model_path)

        # 2. Model size in GB (primary driver for n_parallel)
        try:
            model_file_size = os.path.getsize(model_path)
            if mmproj_path:
                model_file_size += os.path.getsize(mmproj_path)
            model_gb = model_file_size / (1024**3)
            # Runtime overhead: scratch buffers, compute graphs
            est_model_gb = model_gb + 1.0
        except Exception:
            est_model_gb = 6.5

        # VRAM-adaptive n_ctx cap: size the context so the model weights + KV cache + compute buffers fit
        # in the GPU's actually-FREE memory (total minus the desktop/compositor), NOT total. With -ngl
        # forcing all layers, llama.cpp ABORTS when the projected usage exceeds free memory (observed:
        # "projected to use 8216 MiB vs. 7952 MiB free device memory" at n_ctx 24576 → SIGSEGV). Reading
        # free VRAM (e.g. ~8 GB of a 10 GB card after a ~1.8 GB desktop) keeps the launch inside what is
        # really available. KV + per-token compute ≈ 400 MiB per 8k tokens (q8_0 keys + q4_0 values, 8B).
        if gpu and gpu.vram_mb > 0:
            _free_mb = 0
            if getattr(gpu, "vendor", "") == "nvidia":
                try:
                    _r = subprocess.run(
                        ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                        capture_output=True, text=True, timeout=5,
                    )
                    if _r.returncode == 0:
                        _free_mb = int(_r.stdout.strip().splitlines()[0])
                except Exception:
                    _free_mb = 0
            if _free_mb <= 0:
                # Non-NVIDIA (or probe failed): use the reported figure directly. For
                # Apple Silicon vram_mb is already a conservative unified-memory
                # working-set BUDGET (not free VRAM) - see detect_apple_silicon().
                _free_mb = gpu.vram_mb
            _weights_mb = max(0.0, est_model_gb - 1.0) * 1024       # est_model_gb has +1 GB scratch; weights only here
            _budget_mb = _free_mb - _weights_mb - 400               # 400 MiB headroom for desktop fluctuation + safety
            _safe_ctx = max(4096, (int((max(0.0, _budget_mb) / 400.0) * 8192) // 2048) * 2048)
            # VAF's own request (system prompt + routed tools) is ~9-10k tokens, so a context below this
            # MINIMUM starves VAF itself ("request exceeds the available context size" → endless compress
            # /retry → dead). Only cap when an on-GPU context this large actually fits. If it does not
            # (the model fills VRAM, e.g. an 8B-Q6 on a 10GB card), DON'T cap to a tiny value -- keep the
            # configured n_ctx so the KV offloads to CPU (slow but FUNCTIONAL) and tell the user the real
            # fix is a smaller quant.
            _MIN_USABLE_CTX = 16384
            if _safe_ctx < n_ctx:
                if _safe_ctx >= _MIN_USABLE_CTX:
                    UI.event("System", f"n_ctx capped {n_ctx} → {_safe_ctx} (model ~{_weights_mb:.0f} MiB + KV fit ~{_free_mb} MiB FREE VRAM → on-GPU, fast)", style="warning")
                    n_ctx = _safe_ctx
                else:
                    UI.event("System", f"Model fills VRAM: only ~{_safe_ctx} ctx fits on-GPU (< {_MIN_USABLE_CTX} that VAF needs). Keeping n_ctx={n_ctx} (KV → CPU, slow but working). Use a smaller quant (e.g. Q4_K_M) for on-GPU speed.", style="warning")

        # Context (KV) in GB — only to check if 2 slots fit; not the main driver
        est_ctx_gb_per_8k = 2.5
        os_overhead_gb = 2.0
        req_ctx_gb_per_slot = (n_ctx / 8192) * est_ctx_gb_per_8k
        cost_2_slots_vram = est_model_gb + (req_ctx_gb_per_slot * 2) + os_overhead_gb
        cost_2_slots_ram = est_model_gb + (req_ctx_gb_per_slot * 2) + 1.0

        # 3. Decision: start with 1 slot; only allow 2 if model size (GB) + context clearly fit
        final_parallel = 1
        final_total_ctx = n_ctx
        mode_msg = "Sequential (1 Slot)"
        vram_gb = (gpu.vram_mb / 1024) if (gpu and gpu.vram_mb > 0) else 0.0

        if gpu and gpu.vram_mb > 0:
            # Model-size rule: if model alone uses > 50% of VRAM, no room for 2 slots
            if est_model_gb > vram_gb * 0.5:
                mode_msg = "GPU Sequential (1 Slot - Model Size vs VRAM)"
            elif cost_2_slots_vram <= vram_gb:
                final_parallel = 2
                final_total_ctx = n_ctx * 2
                mode_msg = "GPU Parallel (2 Slots - VRAM)"
            else:
                mode_msg = "GPU Sequential (1 Slot - VRAM Optimized)"
        else:
            ram_budget_gb = total_ram_gb * 0.55
            if cost_2_slots_ram <= ram_budget_gb:
                final_parallel = 2
                final_total_ctx = n_ctx * 2
                mode_msg = "CPU Parallel (2 Slots - RAM)"
            else:
                mode_msg = "CPU Sequential (1 Slot - RAM Limited)"

        # SAFETY: Small VRAM or large model (GB) → always 1 slot
        # Threshold lowered to 8GB: RTX 3080 (10GB) can handle 2 slots with q4_0 KV cache.
        if gpu and gpu.vram_mb > 0:
            if gpu.vram_mb < 8000 or est_model_gb > 6.0:
                if final_parallel > 1:
                    final_parallel = 1
                    final_total_ctx = n_ctx
                    mode_msg = "GPU Sequential (1 Slot - VRAM/Model Safety)"

        # USER CONFIG OVERRIDE
        # Allow user to explicitly set n_parallel in config (e.g. to force 1 or try 4)
        config_parallel = Config.get("n_parallel")
        if config_parallel:
            try:
                p_val = int(config_parallel)
                if p_val > 0:
                    final_parallel = p_val
                    final_total_ctx = n_ctx * final_parallel
                    mode_msg = f"User Configured ({final_parallel} Slots)"
            except ValueError:
                pass

        UI.event("System", f"Config: {mode_msg}", style="dim")
        if gpu and gpu.vram_mb > 0:
            _vram_label = "GPU budget (unified memory)" if getattr(gpu, "vendor", "") == "apple" else "VRAM"
            UI.event("System", f"{_vram_label}: {vram_gb:.1f}GB | Est. 2-Slot Need: {cost_2_slots_vram:.1f}GB", style="dim")
        else:
            UI.event("System", f"RAM: {total_ram_gb:.1f}GB | Est. 2-Slot Need: {cost_2_slots_ram:.1f}GB", style="dim")
        
        # llama.cpp server (official README): -np/--parallel N (env: LLAMA_ARG_N_PARALLEL), -c/--ctx-size N.
        # CLI takes precedence over env. We pass both so the value is respected either way.
        # -kvu (disable kv_unified) is build-specific; some builds force n_parallel=4 unless -kvu.
        cmd = [
            self.server_path,
            "-m", model_path,
            # -ngl: pin a fixed GPU layer count ONLY when explicitly requested (>= 0). For "auto" (< 0)
            # we OMIT -ngl so llama.cpp's common_fit_params can AUTO-FIT -- it loads as many layers as
            # fit and offloads the rest to CPU, instead of ABORTING ("n_gpu_layers already set by user
            # to 99, abort" → SIGSEGV) when model + KV exceed VRAM. Forcing -ngl 99 disabled that safety.
            *(["-ngl", str(n_gpu_layers)] if (n_gpu_layers is not None and int(n_gpu_layers) >= 0) else []),
            "-c", str(final_total_ctx),  # --ctx-size: total context (env: LLAMA_ARG_CTX_SIZE)
            "--port", str(port),
            "--host", "127.0.0.1",
            "--parallel", str(final_parallel),
            "-np", str(final_parallel),  # number of parallel slots (env: LLAMA_ARG_N_PARALLEL)
            "-kvu",  # disable kv_unified on builds that support it (avoids forced n_parallel=4)
            # Disable integrated web UI to save resources and keep port 8080 clean
            "--no-webui",
            # KV-Cache Quantization: q8_0 keys (precision matters more) + q4_0 values.
            # Values tolerate more compression; this cuts KV-cache VRAM by ~62.5% vs f16
            # with negligible quality loss — the closest llama.cpp analog to TurboQuant.
            # WARNING: Quantized V cache REQUIRES Flash Attention. llama.cpp silently disables
            # FA when the backend has no kernel for the model (e.g. qwen35 head size 256
            # on Metal) → the server dies at context init ("quantized V cache was
            # requested, but this requires Flash Attention"). The launch loop below
            # retries once without -ctv (V=f16, K stays q8_0 — K-quant needs no FA).
            "-ctk", "q8_0",
            "-ctv", "q4_0",
            # Enable jinja so the tools/tool_choice API works: llama-server uses the GGUF's embedded
            # chat template to parse the model's tool calls and convert them to OpenAI tool_calls.
            # Do NOT override with --chat-template: the model's native template has the correct
            # tool-call format and overriding it breaks function calling.
            # Verified: b9058+ Vulkan binary handles the native template without SIGABRT.
            "--jinja",
        ]

        if mmproj_path:
            # Vision lane: load the projector and cap the per-image token
            # cost (a high-res photo can otherwise eat ~2k ctx tokens).
            # Note: llama-server force-disables ctx_shift/cache_reuse with
            # an mmproj loaded; VAF manages context itself, so that is
            # acceptable - documented in PROVIDER_MODES.md.
            cmd.extend(["--mmproj", mmproj_path, "--image-max-tokens", "1024"])

        # Server log verbosity is a THRESHOLD (higher = more output), not a syslog level.
        # Debug Logs on: 3 = info incl. request/response details. Off: 0 = the default,
        # which still prints startup and error lines (the FA-fallback diagnosis needs
        # those) but no per-request dumps into the rolling server_last.log.
        if is_debug_logging_enabled():
            cmd.extend(["--log-verbosity", "3"])
        else:
            cmd.extend(["--log-verbosity", "0"])
        
        # Prompt cache RAM (MB): configurable to avoid OOM on limited systems
        cache_ram_mb = Config.get("llama_cache_ram", 4096)
        if cache_ram_mb == -1:
            free_mb = psutil.virtual_memory().available / (1024 * 1024)
            cache_ram_mb = min(8192, int(free_mb * 0.4))
        try:
            cache_ram_mb = max(0, int(cache_ram_mb))
        except (TypeError, ValueError):
            cache_ram_mb = 4096
        cmd.extend(["--cache-ram", str(cache_ram_mb)])
        UI.event("Server", f"Prompt cache: {cache_ram_mb} MB", style="dim")
        
        # Log the command and server output only when Debug Logs is enabled (server_cmd_YYYY-MM-DD.log, server_YYYY-MM-DD.log)
        debug_logs = is_debug_logging_enabled()
        if debug_logs:
            try:
                log_dir = get_app_log_dir()
                log_dir.mkdir(parents=True, exist_ok=True)
                cmd_log = get_dated_log_path("server_cmd", "log")
                cmd_log.write_text(
                    f"# n_parallel (slots) = {final_parallel}\n" + " ".join(cmd),
                    encoding="utf-8",
                )
            except Exception:
                pass
        
        UI.event("Server", f"Starting background process on :{port} (ctx={final_total_ctx}, parallel={final_parallel})...", style="dim")
        
        creationflags = 0
        if self.system == "Windows":
             creationflags = subprocess.CREATE_NO_WINDOW
        
        try:
            # ALWAYS capture the server's output to a file. With DEVNULL (the old
            # non-debug path) a dying llama-server left ZERO diagnostics — the
            # qwen35 "quantized V cache requires Flash Attention" failure on
            # macOS/Metal was invisible. Debug Logs keeps its dated file; the
            # non-debug file rolls: the previous start's output survives one
            # generation as server_last.prev.log (a crashing server is usually
            # auto-restarted within seconds — truncating would erase the post-mortem).
            # Logging is best-effort: failing to open a log must never block the start.
            log_file = None
            try:
                log_dir = get_app_log_dir()
                log_dir.mkdir(parents=True, exist_ok=True)
                if debug_logs:
                    log_file = get_dated_log_path("server", "log")
                else:
                    log_file = log_dir / "server_last.log"
                    if log_file.exists():
                        os.replace(log_file, log_file.with_name("server_last.prev.log"))
            except Exception:
                log_file = None

            run_env = os.environ.copy()
            run_env["LLAMA_ARG_N_PARALLEL"] = str(final_parallel)

            if self.system == "Windows":
                creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP

            # KV-quant fallback: attempt 1 = cmd as built (K q8_0 + V q4_0);
            # if the server dies because the backend can't do Flash Attention
            # for this model (V-quant needs FA), attempt 2 retries without
            # -ctv (V cache f16). Costs RAM (~2 GiB at 45k ctx on a 4B model)
            # but starts on every backend/model combination.
            attempt_cmds = [cmd]
            if "-ctv" in cmd:
                no_vquant = list(cmd)
                _i = no_vquant.index("-ctv")
                del no_vquant[_i:_i + 2]
                if self._kv_vquant_unsupported:
                    # This combination already died on V-quant in this process — do not
                    # re-pay a full model load + deterministic crash to rediscover it.
                    attempt_cmds = [no_vquant]
                else:
                    attempt_cmds.append(no_vquant)

            for attempt, run_cmd in enumerate(attempt_cmds, start=1):
                self._log_file = None
                if log_file is not None:
                    try:
                        # Append on retry: attempt 1's output holds the root cause the
                        # retry is reacting to — truncating would erase it.
                        self._log_file = open(log_file, 'w' if attempt == 1 else 'a', encoding='utf-8', errors='replace')
                        if attempt > 1:
                            self._log_file.write(
                                "\n===== retry without -ctv (V cache f16) =====\n# "
                                + " ".join(run_cmd) + "\n"
                            )
                            self._log_file.flush()
                    except Exception:
                        self._log_file = None
                _out = self._log_file if self._log_file is not None else subprocess.DEVNULL
                self.process = subprocess.Popen(
                    run_cmd,
                    stdout=_out,
                    stderr=_out,
                    creationflags=creationflags,
                    env=run_env,
                )
                # Save the PID immediately: crash/orphan cleanup and idle-unload must be
                # able to find the server during the WHOLE load window (minutes on slow
                # setups), not only once it is ready.
                self._save_pid(self.process.pid)

                # Windows: Add process to job object for automatic cleanup
                if self.system == "Windows" and self._job_handle:
                    try:
                        import ctypes
                        process_handle = int(self.process._handle)
                        ctypes.windll.kernel32.AssignProcessToJobObject(
                            self._job_handle,
                            process_handle
                        )
                    except Exception:
                        # Failed to assign to job, but process is still running
                        # Fall back to manual cleanup
                        pass

                # Wait until the server is READY: /health == 200 means weights loaded +
                # context allocated. llama-server binds the port within ~1s and serves
                # 503 while the model is still loading (and possibly about to die at
                # context init). Accepting any TCP response here returned True for
                # servers that crashed 2s later → the agent's call failed → endless
                # relaunch loop and orphaned processes. Only HTTP 200 counts as started.
                # A live process answering 503 is PROGRESS and gets the full
                # configurable budget — a flat ~60s deadline here would turn
                # legitimately slow cold loads (big GGUF, HDD, CPU-only) into a
                # kill/reload loop. Only "no HTTP answer at all" (bind phase, hung
                # server) runs on a short grace that refreshes with every response.
                ready_deadline = time.monotonic() + _server_ready_budget()
                no_response_deadline = time.monotonic() + 60.0
                died = False
                timed_out = False
                while True:
                    if self.process.poll() is not None:
                        died = True
                        break
                    if time.monotonic() >= ready_deadline:
                        timed_out = True
                        break
                    try:
                        _health = requests.get(f"http://127.0.0.1:{port}/health", timeout=0.5)
                        no_response_deadline = time.monotonic() + 60.0
                        if _health.status_code != 200:
                            time.sleep(0.5)
                            continue
                        # Verify server actually applied our n_parallel (GET /props returns total_slots)
                        try:
                            r = requests.get(f"http://127.0.0.1:{port}/props", timeout=2)
                            if r.status_code == 200:
                                data = r.json()
                                slots = data.get("total_slots")
                                if slots is not None and int(slots) != final_parallel:
                                    UI.event("Server", f"Warn: server reports total_slots={slots} (expected {final_parallel})", style="yellow")
                                elif slots is not None:
                                    UI.event("System", f"Server confirmed: total_slots={slots}", style="dim")
                        except Exception:
                            pass  # /props optional; older builds may not have it
                        return True
                    except Exception:
                        if time.monotonic() >= no_response_deadline:
                            timed_out = True
                            break
                        time.sleep(0.5)

                # Startup failed — close/flush the log and read its tail for diagnosis.
                try:
                    if self._log_file is not None:
                        self._log_file.flush()
                        self._log_file.close()
                except Exception:
                    pass
                self._log_file = None
                log_tail = ""
                if log_file is not None:
                    try:
                        with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
                            log_tail = f.read()[-800:]
                    except Exception:
                        pass

                if timed_out:
                    # Still alive but not ready within the budget: stop it, so no
                    # untracked half-loaded llama-server is left behind for the next
                    # start to blindly kill/respawn.
                    UI.error(
                        f"Server not ready after {int(_server_ready_budget())}s - stopping it. "
                        "For very slow disks/models raise server_ready_timeout in the config."
                    )
                    try:
                        self.process.terminate()
                        try:
                            self.process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            self.process.kill()
                    except Exception:
                        pass
                    self._remove_pid()
                    return False

                if attempt < len(attempt_cmds) and "requires Flash Attention" in log_tail:
                    self._kv_vquant_unsupported = True
                    if debug_logs:
                        try:
                            with open(get_dated_log_path("server_cmd", "log"), "a", encoding="utf-8") as _f:
                                _f.write("\n# retry without -ctv (V cache f16)\n" + " ".join(attempt_cmds[attempt]))
                        except Exception:
                            pass
                    UI.event(
                        "Server",
                        "V-cache quant needs Flash Attention (no backend support for this model) - retrying with f16 V cache...",
                        style="yellow",
                    )
                    continue

                self._remove_pid()  # process is dead; do not leave a stale pid file
                UI.error(f"Server failed to start. Check {log_file}\n{log_tail[-500:]}")
                return False

            return False

        except Exception as e:
            try:
                if self._log_file is not None:
                    self._log_file.close()
            except Exception:
                pass
            self._log_file = None
            UI.error(f"Failed to launch server: {e}")
            return False

    def stop_server(self, force_external=False):
        # WICHTIG: Prüfe zuerst PID-Datei, falls self.process nicht gesetzt ist
        # (z.B. wenn Server von anderem Prozess gestartet wurde)
        pid_to_kill = None
        
        if self.process:
            pid_to_kill = self.process.pid
            
            # Try graceful termination first
            self.process.terminate()
            try:
                self.process.wait(timeout=0.5)
            except:
                # Force kill if still running
                self.process.kill()
                try:
                    self.process.wait(timeout=0.5)
                except:
                    pass
            
            self.process = None
        elif os.path.exists(self.pid_file) and force_external:
            # Server wurde von anderem Prozess gestartet, lese PID aus Datei
            # Nur stoppen, wenn force_external=True (z.B. bei Neustart oder Tray Exit)
            try:
                with open(self.pid_file, 'r', encoding='utf-8') as f:
                    pid_to_kill = int(f.read().strip())
            except (ValueError, FileNotFoundError):
                pass
        
        # Kill process by PID (wenn noch nicht beendet)
        if pid_to_kill and self._is_process_running(pid_to_kill):
            # Windows: Use taskkill (most reliable)
            if self.system == "Windows":
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/PID", str(pid_to_kill)],
                        capture_output=True, encoding='utf-8', errors='replace',
                        creationflags=subprocess.CREATE_NO_WINDOW,
                        timeout=2
                    )
                except:
                    pass
            else:
                # Unix/Linux/macOS: Use kill
                try:
                    os.kill(pid_to_kill, 9)  # SIGKILL
                except:
                    pass
        
        # Close log file if open
        if hasattr(self, '_log_file') and self._log_file:
            try:
                self._log_file.close()
            except:
                pass
            self._log_file = None
        
        # Windows: Close job object handle (this will automatically terminate all processes in the job)
        if self.system == "Windows" and self._job_handle:
            try:
                import ctypes
                ctypes.windll.kernel32.CloseHandle(self._job_handle)
                self._job_handle = None
            except Exception:
                pass
        
        # Always remove PID file on clean stop
        self._remove_pid()
