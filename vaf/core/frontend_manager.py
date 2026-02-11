import os
import sys
import time
import socket
import shutil
import subprocess
import threading
import platform
import ctypes
from datetime import datetime
from pathlib import Path
from vaf.core.config import Config
from vaf.core.log_helper import is_debug_logging_enabled

class FrontendManager:
    """Manages the lifecycle of the Next.js Frontend."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(FrontendManager, cls).__new__(cls)
            cls._instance.process = None
            cls._instance.port = None
            cls._instance._job_handle = None  # Windows Job Object for process tree management
        return cls._instance

    def _create_job_object(self):
        """Create a Windows Job Object that kills all child processes when closed."""
        if platform.system() != "Windows":
            return None

        try:
            kernel32 = ctypes.windll.kernel32

            # Create Job Object
            job_name = f"VAFFrontendJob_{os.getpid()}"
            job_handle = kernel32.CreateJobObjectW(None, job_name)
            if not job_handle:
                return None

            # Configure to kill all processes when job is closed
            # JOBOBJECT_EXTENDED_LIMIT_INFORMATION structure
            class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_uint64),
                    ("PerJobUserTimeLimit", ctypes.c_uint64),
                    ("LimitFlags", ctypes.c_uint32),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", ctypes.c_uint32),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", ctypes.c_uint32),
                    ("SchedulingClass", ctypes.c_uint32),
                ]

            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("ReadOperationCount", ctypes.c_uint64),
                    ("WriteOperationCount", ctypes.c_uint64),
                    ("OtherOperationCount", ctypes.c_uint64),
                    ("ReadTransferCount", ctypes.c_uint64),
                    ("WriteTransferCount", ctypes.c_uint64),
                    ("OtherTransferCount", ctypes.c_uint64),
                ]

            class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = 0x2000

            # JobObjectExtendedLimitInformation = 9
            result = kernel32.SetInformationJobObject(
                job_handle, 9, ctypes.byref(info), ctypes.sizeof(info)
            )

            if result:
                return job_handle
            else:
                kernel32.CloseHandle(job_handle)
                return None

        except Exception:
            return None

    def _assign_process_to_job(self, process):
        """Assign a process to the Job Object so all its children are tracked."""
        if platform.system() != "Windows" or not self._job_handle:
            return False

        try:
            kernel32 = ctypes.windll.kernel32
            # Get process handle with PROCESS_SET_QUOTA | PROCESS_TERMINATE
            handle = kernel32.OpenProcess(0x0100 | 0x0001, False, process.pid)
            if handle:
                result = kernel32.AssignProcessToJobObject(self._job_handle, handle)
                kernel32.CloseHandle(handle)
                return bool(result)
        except Exception:
            pass
        return False

    def __del__(self):
        """Cleanup on object destruction - ensure no zombie processes."""
        try:
            self.stop_frontend()
        except Exception:
            pass

    def _log(self, message, style="dim", callback=None):
        if callback:
            callback(message, style)
        else:
            print(f"[Frontend] {message}")

    def get_web_dir(self):
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return os.path.join(base_dir, "web")

    def get_port_file(self):
        return os.path.join(os.path.expanduser("~"), ".vaf", "web_port")

    def is_port_in_use(self, port):
        for host in ['127.0.0.1', 'localhost']:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(0.1)
                    if s.connect_ex((host, port)) == 0:
                        return True
            except:
                continue
        return False

    def get_active_port(self):
        try:
            port_file = self.get_port_file()
            if os.path.exists(port_file):
                with open(port_file, "r") as f:
                    content = f.read().strip()
                    if content.isdigit():
                        return int(content)
        except:
            pass
        return None

    def _kill_process_on_port(self, port):
        """Find and kill process using a specific port on Windows."""
        if platform.system() != "Windows": return
        try:
            cmd = f"netstat -ano | findstr :{port}"
            # Fix: Handle encoding issues on Windows (e.g. German locale)
            output = subprocess.check_output(cmd, shell=True).decode(errors='ignore')
            for line in output.splitlines():
                parts = line.strip().split()
                # Line format: TCP  LocalIP:Port  RemoteIP:Port  State  PID
                # Check for enough parts and that the port matches (to avoid false positives)
                if len(parts) > 4 and str(port) in parts[1]:
                    pid = parts[-1]
                    # Only kill if PID > 0
                    if pid.isdigit() and int(pid) > 0:
                        # print(f"[Frontend] Killing PID {pid} on port {port}...")
                        subprocess.run(["taskkill", "/F", "/T", "/PID", pid],
                                     capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception as e:
            pass

    def start_frontend(self, log_callback=None, host=None, force_restart=False):
        """Start the Next.js frontend if not already reachable.

        Args:
            log_callback: Optional callback for logging messages
            host: Host to bind to. If None, uses local_network_enabled config.
                  "0.0.0.0" = accessible from network, "127.0.0.1" = localhost only
            force_restart: If True, skip the "already running" check and start fresh
        """
        # Determine host binding based on config if not explicitly provided
        if host is None:
            local_network_enabled = Config.get("local_network_enabled", False)
            host = "0.0.0.0" if local_network_enabled else "127.0.0.1"

        web_dir = self.get_web_dir()
        pkg_file = os.path.join(web_dir, "package.json")

        if not os.path.exists(web_dir) or not os.path.exists(pkg_file):
            self._log("Web directory not found.", "error", log_callback)
            return None

        # Check for existing port (skip if force_restart)
        if not force_restart:
            active_port = self.get_active_port()
            if active_port and self.is_port_in_use(active_port):
                self._log(f"Frontend active on port {active_port}", "success", log_callback)
                return active_port

        # Not running, need to start
        try:
            npm_path = shutil.which("npm")
            if not npm_path:
                raise FileNotFoundError("npm not found in PATH")

            # Install deps if needed
            if not os.path.exists(os.path.join(web_dir, "node_modules")):
                self._log("Installing npm dependencies...", "warning", log_callback)
                install_kwargs = {"cwd": web_dir, "capture_output": True}
                if platform.system() == "Windows":
                    install_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                subprocess.run([npm_path, "install"], **install_kwargs)

            # Determine starting port from config
            base_port = Config.get("local_network_port_frontend", 3000)
            
            # Find free port starting at base_port
            port = base_port
            while self.is_port_in_use(port):
                # If port is in use but no VAF process owns it, try to clean it
                self._kill_process_on_port(port)
                if self.is_port_in_use(port):
                    port += 1
                else:
                    break

            self._log(f"Launching Dashboard on Port {port}...", "dim", log_callback)

            # Save port file
            try:
                os.makedirs(os.path.dirname(self.get_port_file()), exist_ok=True)
                with open(self.get_port_file(), "w") as f:
                    f.write(str(port))
            except: pass

            # Log file (only when Debug Logs enabled)
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            log_dir = os.path.join(base_dir, "logs")
            os.makedirs(log_dir, exist_ok=True)
            log_file = os.path.join(log_dir, "web_debug.log")
            use_debug_log = is_debug_logging_enabled()

            # Start Process - avoid shell=True to have direct control over process tree
            # -H specifies the hostname/interface to bind to
            cmd = [npm_path, "run", "dev", "--", "-p", str(port), "-H", host]
            
            self._log(f"Starting Frontend with command: {' '.join(cmd)}", "info", log_callback)
            self._log(f"Binding to host: {host} (Local Network: {Config.get('local_network_enabled', False)})", "info", log_callback)
            if use_debug_log:
                self._log(f"Logging stdout/stderr to: {log_file}", "info", log_callback)

            creationflags = 0
            if platform.system() == "Windows":
                creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
                self._job_handle = self._create_job_object()

            if use_debug_log:
                out_err = open(log_file, "a")
                self.process = subprocess.Popen(
                    cmd,
                    cwd=web_dir,
                    stdout=out_err,
                    stderr=subprocess.STDOUT,
                    creationflags=creationflags,
                    shell=False
                )
            else:
                self.process = subprocess.Popen(
                    cmd,
                    cwd=web_dir,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=creationflags,
                    shell=False
                )

            # Assign process to Job Object (all children will be tracked)
            if self._job_handle:
                self._assign_process_to_job(self.process)

            # Wait until the frontend is actually listening (critical after reboot: npm can take 20–60s)
            wait_timeout = 90
            wait_interval = 1.5
            elapsed = 0
            while elapsed < wait_timeout:
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.settimeout(1)
                        if s.connect_ex((host if host != "0.0.0.0" else "127.0.0.1", port)) == 0:
                            self._log(f"Frontend ready on port {port} after {elapsed:.0f}s", "dim", log_callback)
                            break
                except Exception:
                    pass
                time.sleep(wait_interval)
                elapsed += wait_interval
            if elapsed >= wait_timeout:
                self._log(f"Frontend port {port} not ready after {wait_timeout}s (browser may open to loading page)", "warning", log_callback)

            self.port = port
            return port

        except Exception as e:
            self._log(f"Frontend startup failed: {e}", "error", log_callback)
            return None

    def stop_frontend(self, wait_for_exit=True):
        """Stop the subprocess and its entire tree if we own it.

        Args:
            wait_for_exit: If True, wait for the port to be released before returning
        """
        stopped_port = self.port  # Save for port cleanup

        if platform.system() == "Windows":
            # Method 1: Close Job Object (kills ALL child processes automatically)
            if self._job_handle:
                try:
                    ctypes.windll.kernel32.CloseHandle(self._job_handle)
                    self._job_handle = None
                except Exception:
                    pass

            # Method 2: Fallback - taskkill with /T (tree kill)
            if self.process:
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(self.process.pid)],
                        capture_output=True,
                        creationflags=subprocess.CREATE_NO_WINDOW
                    )
                except Exception:
                    pass

            # Method 3: Kill any remaining process on the port (Robust Loop)
            if stopped_port:
                # Retry loop: Try to kill up to 5 times
                for attempt in range(5):
                    if not self.is_port_in_use(stopped_port):
                        break
                    
                    self._kill_process_on_port(stopped_port)
                    time.sleep(0.5)

            # Wait for port to be released if requested
            if wait_for_exit and stopped_port:
                for _ in range(50):  # Wait up to 5 seconds
                    if not self.is_port_in_use(stopped_port):
                        break
                    time.sleep(0.1)

        else:
            # Unix/Mac: terminate process group with escalating signals
            if self.process:
                try:
                    import signal
                    # Try SIGTERM first (graceful)
                    try:
                        os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
                    except Exception:
                        pass
                    
                    # Wait briefly for graceful shutdown
                    time.sleep(0.5)
                    
                    # If process still alive, use SIGKILL
                    try:
                        if self.process.poll() is None:
                            os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                    except Exception:
                        pass
                    
                    # Also terminate the process directly as fallback
                    try:
                        self.process.terminate()
                        time.sleep(0.2)
                        if self.process.poll() is None:
                            self.process.kill()
                    except Exception:
                        pass
                except Exception:
                    pass
            
            # Mac-specific: Kill process on port using lsof + kill
            if stopped_port:
                try:
                    # Find process using the port
                    result = subprocess.run(
                        ["lsof", "-ti", f":{stopped_port}"],
                        capture_output=True,
                        text=True,
                        timeout=2
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        pids = result.stdout.strip().split('\n')
                        for pid in pids:
                            if pid.strip():
                                try:
                                    # Kill process group
                                    subprocess.run(["kill", "-9", pid], timeout=1)
                                except Exception:
                                    pass
                except Exception:
                    pass

            # Wait for port to be released if requested (Unix)
            if wait_for_exit and stopped_port:
                for _ in range(30):  # Wait up to 3 seconds
                    if not self.is_port_in_use(stopped_port):
                        break
                    time.sleep(0.1)

        self.process = None
        self.port = None

        # Cleanup port file
        try:
            port_file = self.get_port_file()
            if os.path.exists(port_file):
                os.remove(port_file)
        except Exception:
            pass
