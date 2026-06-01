import os
import sys
import platform
import shutil
import subprocess
import zipfile
import tarfile
import time
import requests
from pathlib import Path
from vaf.cli.ui import UI
from vaf.core.config import Config
from vaf.core.gpu_detection import get_primary_gpu
from vaf.core.log_helper import get_app_log_dir, get_dated_log_path, is_debug_logging_enabled
from vaf.core.platform import Platform

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

    def start_server(self, model_path, n_gpu_layers=99, n_ctx=32768, port=8080):
        """
        Start llama-server only if provider is 'local' and auto-start is enabled.
        
        Best Practice: Skip server startup when using API providers to save resources.
        """
        from vaf.core.config import Config
        
        provider = Config.get("provider", "local")
        auto_start = Config.get("auto_start_local_server", True)
        
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

        # If a server is already listening, reuse it instead of spawning another.
        try:
            response = requests.get(f"http://127.0.0.1:{port}/health", timeout=1)
            if response.status_code == 200:
                self.process = None  # Reuse existing external process
                UI.event("Server", f"Reusing existing server on :{port}...", style="dim")
                return True
            # If it's still loading, wait briefly before deciding to restart.
            if response.status_code == 503:
                wait_start = time.time()
                while time.time() - wait_start < 30:
                    try:
                        response = requests.get(f"http://127.0.0.1:{port}/health", timeout=1)
                        if response.status_code == 200:
                            self.process = None
                            UI.event("Server", f"Reusing existing server on :{port}...", style="dim")
                            return True
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
                    # Check if server is responding
                    try:
                        response = requests.get(f"http://127.0.0.1:{port}/health", timeout=1)
                        if response.status_code == 200:
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
        self.stop_server(force_external=True)
        
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
        
        # 2. Model size in GB (primary driver for n_parallel)
        try:
            model_file_size = os.path.getsize(model_path)
            model_gb = model_file_size / (1024**3)
            # Runtime overhead: scratch buffers, compute graphs
            est_model_gb = model_gb + 1.0
        except Exception:
            est_model_gb = 6.5
        
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
            UI.event("System", f"VRAM: {vram_gb:.1f}GB | Est. 2-Slot Need: {cost_2_slots_vram:.1f}GB", style="dim")
        else:
            UI.event("System", f"RAM: {total_ram_gb:.1f}GB | Est. 2-Slot Need: {cost_2_slots_ram:.1f}GB", style="dim")
        
        # llama.cpp server (official README): -np/--parallel N (env: LLAMA_ARG_N_PARALLEL), -c/--ctx-size N.
        # CLI takes precedence over env. We pass both so the value is respected either way.
        # -kvu (disable kv_unified) is build-specific; some builds force n_parallel=4 unless -kvu.
        cmd = [
            self.server_path,
            "-m", model_path,
            "-ngl", str(n_gpu_layers),
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
            "-ctk", "q8_0",
            "-ctv", "q4_0",
            # Enable jinja so the tools/tool_choice API works.
            # VQ-1's native template uses <tool_call> XML format which llama-server
            # parses and converts to OpenAI tool_calls objects automatically.
            # Do NOT override with --chat-template: the native template has proper
            # tool-call support and overriding it breaks function calling.
            # Verified: b9058+ Vulkan binary handles the native template without SIGABRT.
            "--jinja",
        ]

        # Server log verbosity: 2=warning (small logs) or 3=info (detailed logs for debugging)
        # Only use verbose logging when Debug Logs is enabled in settings
        if is_debug_logging_enabled():
            cmd.extend(["--log-verbosity", "3"])  # Info level - logs requests/responses
        else:
            cmd.extend(["--log-verbosity", "2"])  # Warning level - minimal output
        
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
            if debug_logs:
                log_dir = get_app_log_dir()
                log_dir.mkdir(parents=True, exist_ok=True)
                log_file = get_dated_log_path("server", "log")
                self._log_file = open(log_file, 'w', encoding='utf-8', errors='replace')
                stdout_err = self._log_file
                stderr_err = self._log_file
            else:
                self._log_file = None
                stdout_err = subprocess.DEVNULL
                stderr_err = subprocess.DEVNULL

            run_env = os.environ.copy()
            run_env["LLAMA_ARG_N_PARALLEL"] = str(final_parallel)

            if self.system == "Windows":
                creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP

            self.process = subprocess.Popen(
                cmd,
                stdout=stdout_err,
                stderr=stderr_err,
                creationflags=creationflags,
                env=run_env,
            )
            
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
            
            # Wait for startup (up to 60s for large models/slow disks)
            for _ in range(120):
                if self.process.poll() is not None:
                    if self._log_file:
                        self._log_file.flush()
                        try:
                            with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
                                log_content = f.read()[-500:]
                            UI.error(f"Server failed to start. Check {log_file}\n{log_content}")
                        except Exception:
                            UI.error(f"Server failed to start. Check {log_file}")
                    else:
                        UI.error("Server failed to start. Enable Debug Logs in Advanced settings for server.log.")
                    return False
                
                # Check if port is live
                try:
                    requests.get(f"http://127.0.0.1:{port}/health", timeout=0.5)
                    # Save PID for crash recovery
                    self._save_pid(self.process.pid)
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
                    time.sleep(0.5)
                    
            UI.error("Server startup timed out.")
            return False
            
        except Exception as e:
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
