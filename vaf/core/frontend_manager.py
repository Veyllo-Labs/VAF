import os
import sys
import time
import socket
import shutil
import subprocess
import threading
import platform
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
        return cls._instance

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
            
            # Start Process
            cmd = [npm_path, "run", "dev", "--", "-p", str(port)]
            
            creationflags = 0
            use_shell = False
            if platform.system() == "Windows":
                creationflags = subprocess.CREATE_NO_WINDOW
                use_shell = True
                
            self.process = subprocess.Popen(
                cmd, 
                cwd=web_dir, 
                stdout=open(log_file, "a"),
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
                shell=use_shell
            )
            
            self.port = port
            return port

        except Exception as e:
            self._log(f"Frontend startup failed: {e}", "error", log_callback)
            return None

    def stop_frontend(self):
        """Stop the subprocess and its entire tree if we own it."""
        if self.process:
            if platform.system() == "Windows":
                try:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.process.pid)], 
                                 capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                except: pass
            else:
                self.process.terminate()
            
            self.process = None
            try:
                if os.path.exists(self.get_port_file()):
                    os.remove(self.get_port_file())
            except: pass