import os
import sys
import platform
import shutil
import subprocess
import zipfile
import time
import requests
from vaf.cli.ui import UI
from vaf.core.config import Config

class ServerManager:
    """
    Manages the lifecycle of the standalone llama-server executable.
    This bypasses python bindings for robust GPU support.
    """
    
    # We pin a stable version to ensure predictable asset names
    # Using b4320 as a recent stable reference or we could try to resolve "latest"
    # For reliability, let's use a specific build tag that we know exists
    LLAMA_TAG = "b4320" 
    
    def __init__(self):
        self.base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.bin_dir = os.path.join(self.base_dir, "bin")
        self.process = None
        self._log_file = None  # Server log file handle
        
        # PID file for tracking server process (survives crashes)
        self.pid_file = os.path.join(os.path.expanduser("~"), ".vaf", "server.pid")
        
        # Determine platform specifics
        self.system = platform.system()
        self.machine = platform.machine().lower()
        
        self.server_exe = "llama-server"
        if self.system == "Windows":
             self.server_exe += ".exe"
             
        self.server_path = os.path.join(self.bin_dir, self.server_exe)
        
        # Cleanup any orphaned server from previous crash
        self._cleanup_orphan_server()

    def _cleanup_orphan_server(self):
        """Kill any orphaned server process from a previous crash."""
        if not os.path.exists(self.pid_file):
            return
        
        try:
            with open(self.pid_file, 'r') as f:
                old_pid = int(f.read().strip())
            
            # Check if process is still running
            if self._is_process_running(old_pid):
                UI.event("System", f"Found orphaned server (PID {old_pid}), cleaning up...", style="yellow")
                self._kill_process(old_pid)
                time.sleep(0.5)
            
            # Remove stale PID file
            os.remove(self.pid_file)
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

            if self.system == "Windows":
                 main_url, main_name = find_asset(["bin-win-cuda", "x64.zip"], exclude=["cudart"])
                 dep_url, dep_name = find_asset(["cudart-llama", "bin-win-cuda", "x64.zip"])

            elif self.system == "Darwin":
                 keyword = "bin-macos-arm64.zip" if ("arm64" in self.machine or "aarch64" in self.machine) else "bin-macos-x64.zip"
                 main_url, main_name = find_asset([keyword])

            elif self.system == "Linux":
                 main_url, main_name = find_asset(["bin-linux-x64.zip"])

        # 2. Check if we found it. If NOT, Fallback.
        if main_url:
            return main_url, main_name, dep_url, dep_name
            
        # FALLBACK LOGIC (Offline / Rate Limit / Parse Failure)
        tag = "b4320" # Known stable
        self.LLAMA_TAG = tag
        base_url = f"https://github.com/ggerganov/llama.cpp/releases/download/{tag}"
        
        if self.system == "Windows":
             main_name = "llama-b4320-bin-win-cuda-cu12.2.0-x64.zip" 
             main_url = f"{base_url}/{main_name}"
             dep_name = "cudart-llama-bin-win-cuda-cu12.2.0-x64.zip" 
             dep_url = f"{base_url}/{dep_name}"
             return main_url, main_name, dep_url, dep_name
             
        elif self.system == "Darwin":
             is_arm = "arm64" in self.machine or "aarch64" in self.machine
             main_name = f"llama-{tag}-bin-macos-{'arm64' if is_arm else 'x64'}.zip"
             main_url = f"{base_url}/{main_name}"
             return main_url, main_name, None, None
             
        elif self.system == "Linux":
             main_name = f"llama-{tag}-bin-linux-x64.zip"
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
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(self.bin_dir)
            
            os.remove(zip_path)
            
            if self.system != "Windows":
                 os.chmod(self.server_path, 0o755)
                 
            UI.event("System", "Backend installed successfully.", style="success")
            return True
            
        except Exception as e:
            UI.error(f"Backend download failed: {e}")
            return False

    def start_server(self, model_path, n_gpu_layers=99, n_ctx=8192, port=8080):
        if not self.ensure_server_exists():
            return False
            
        # Stop existing if any (naive check)
        self.stop_server()
        
        cmd = [
            self.server_path,
            "-m", model_path,
            "-ngl", str(n_gpu_layers),
            "-c", str(n_ctx),
            "--port", str(port),
            "--host", "127.0.0.1",
            "--ctx-size", str(n_ctx),
            "--n-gpu-layers", str(n_gpu_layers),
            "--verbose" # Helpful for debug output in console
        ]
        
        UI.event("Server", f"Starting background process on :{port}...", style="dim")
        
        # Start detached process
        # On Windows, we might want CREATE_NO_WINDOW if we want it silent, 
        # but for now let's keep it visible or standard piping so user sees it working
        
        creationflags = 0
        if self.system == "Windows":
             creationflags = subprocess.CREATE_NO_WINDOW
        
        try:
            # Create log file for server output (helps with debugging)
            log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "logs")
            os.makedirs(log_dir, exist_ok=True)
            log_file = os.path.join(log_dir, "server.log")
            
            # Open log file with UTF-8 encoding to avoid Windows cp1252 issues
            self._log_file = open(log_file, 'w', encoding='utf-8', errors='replace')
            
            self.process = subprocess.Popen(
                cmd,
                stdout=self._log_file,      # Write to log file
                stderr=self._log_file,      # Write errors to same log
                creationflags=creationflags
            )
            
            # Wait for startup (up to 60s for large models/slow disks)
            for _ in range(120):
                if self.process.poll() is not None:
                    # It died - read from log file
                    self._log_file.flush()
                    try:
                        with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
                            log_content = f.read()[-500:]  # Last 500 chars
                        UI.error(f"Server failed to start. Check {log_file}\n{log_content}")
                    except:
                        UI.error(f"Server failed to start. Check {log_file}")
                    return False
                
                # Check if port is live
                try:
                    requests.get(f"http://127.0.0.1:{port}/health", timeout=0.5)
                    # Save PID for crash recovery
                    self._save_pid(self.process.pid)
                    # Return True - caller will show success message after spinner ends
                    return True
                except:
                    time.sleep(0.5)
                    
            UI.error("Server startup timed out.")
            return False
            
        except Exception as e:
            UI.error(f"Failed to launch server: {e}")
            return False

    def stop_server(self):
        if self.process:
            pid = self.process.pid
            
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
            
            # Windows: Double-check with taskkill (most reliable)
            if self.system == "Windows" and pid:
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/PID", str(pid)],
                        capture_output=True, encoding='utf-8', errors='replace',
                        creationflags=subprocess.CREATE_NO_WINDOW,
                        timeout=2
                    )
                except:
                    pass
            
            self.process = None
        
        # Close log file if open
        if hasattr(self, '_log_file') and self._log_file:
            try:
                self._log_file.close()
            except:
                pass
            self._log_file = None
        
        # Always remove PID file on clean stop
        self._remove_pid()
