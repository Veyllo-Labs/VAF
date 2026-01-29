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
            output = subprocess.check_output(cmd, shell=True).decode()
            for line in output.splitlines():
                if "LISTENING" in line:
                    parts = line.strip().split()
                    if len(parts) > 4:
                        pid = parts[-1]
                        subprocess.run(["taskkill", "/F", "/T", "/PID", pid],
                                     capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        except:
            pass

    def start_frontend(self, log_callback=None):
        """Start the Next.js frontend if not already reachable."""
        web_dir = self.get_web_dir()
        pkg_file = os.path.join(web_dir, "package.json")

        if not os.path.exists(web_dir) or not os.path.exists(pkg_file):
            self._log("Web directory not found.", "error", log_callback)
            return None

        # Check for existing port
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
                subprocess.run([npm_path, "install"], cwd=web_dir, capture_output=True)

            # Find free port starting at 3000
            port = 3000
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

            # Log file
            log_dir = os.path.join(os.getcwd(), "logs")
            os.makedirs(log_dir, exist_ok=True)
            log_file = os.path.join(log_dir, "web_debug.log")

            # Start Process - avoid shell=True to have direct control over process tree
            cmd = [npm_path, "run", "dev", "--", "-p", str(port)]

            creationflags = 0
            if platform.system() == "Windows":
                # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP for better process management
                creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP

                # Create Job Object BEFORE starting process
                self._job_handle = self._create_job_object()

            self.process = subprocess.Popen(
                cmd,
                cwd=web_dir,
                stdout=open(log_file, "a"),
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
                shell=False  # Important: shell=False for proper process tree control
            )

            # Assign process to Job Object (all children will be tracked)
            if self._job_handle:
                self._assign_process_to_job(self.process)

            self.port = port
            return port

        except Exception as e:
            self._log(f"Frontend startup failed: {e}", "error", log_callback)
            return None

    def stop_frontend(self):
        """Stop the subprocess and its entire tree if we own it."""
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

            # Method 3: Kill any remaining process on the port
            if stopped_port:
                self._kill_process_on_port(stopped_port)

        else:
            # Unix: terminate process group
            if self.process:
                try:
                    import signal
                    os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
                except Exception:
                    pass
                try:
                    self.process.terminate()
                except Exception:
                    pass

        self.process = None
        self.port = None

        # Cleanup port file
        try:
            port_file = self.get_port_file()
            if os.path.exists(port_file):
                os.remove(port_file)
        except Exception:
            pass
