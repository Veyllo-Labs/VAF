import os
import sys
import time
import socket
import shutil
import subprocess
import threading
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
            # Fallback for headless/tray
            print(f"[Frontend] {message}")

    def get_web_dir(self):
        # Locate 'web' folder relative to this file
        # vaf/core/frontend_manager.py -> vaf/core -> vaf -> VAF -> VAF/web
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return os.path.join(base_dir, "web")

    def get_port_file(self):
        return os.path.join(os.path.expanduser("~"), ".vaf", "web_port")

    def is_port_in_use(self, port):
        # Check both 127.0.0.1 and localhost to be sure (Windows IPv4/IPv6 mix)
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
        """Read the last known port from file."""
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

    def start_frontend(self, log_callback=None):
        """Start the Next.js frontend if not already reachable."""
        web_dir = self.get_web_dir()
        pkg_file = os.path.join(web_dir, "package.json")
        
        if not os.path.exists(web_dir) or not os.path.exists(pkg_file):
            self._log("Web directory not found.", "error", log_callback)
            return None

        # Check if already running (check stored port)
        active_port = self.get_active_port()
        if active_port and self.is_port_in_use(active_port):
            # Verify it's actually responding (light check)
            # Actually, is_port_in_use just checks socket. 
            # If 'vaf run' is running, this should be true.
            self._log(f"Frontend already active on port {active_port}", "success", log_callback)
            return active_port

        # Not running, need to start
        try:
            npm_path = shutil.which("npm")
            if not npm_path:
                raise FileNotFoundError("npm not found in PATH")

            # Install deps if needed
            if not os.path.exists(os.path.join(web_dir, "node_modules")):
                self._log("Installing npm dependencies...", "warning", log_callback)
                subprocess.run([npm_path, "install"], cwd=web_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            # Find free port
            port = 3000
            while self.is_port_in_use(port):
                port += 1
            
            self._log(f"Launching Dashboard on Port {port}...", "dim", log_callback)
            
            # Save port file
            try:
                os.makedirs(os.path.dirname(self.get_port_file()), exist_ok=True)
                with open(self.get_port_file(), "w") as f:
                    f.write(str(port))
            except Exception as e:
                self._log(f"Failed to save port file: {e}", "warning", log_callback)

            # Log file
            log_dir = os.path.join(os.getcwd(), "logs")
            os.makedirs(log_dir, exist_ok=True)
            log_file = os.path.join(log_dir, "web_debug.log")
            
            # Start Process
            cmd = [npm_path, "run", "dev", "--", "-p", str(port)]
            self.process = subprocess.Popen(
                cmd, 
                cwd=web_dir, 
                stdout=open(log_file, "a"),
                stderr=subprocess.STDOUT
            )
            
            # Wait for ready
            dashboard_url = f"http://localhost:{port}"
            # Simple spin wait
            # We won't block indefinitely here in Tray, but for CLI we might want to.
            # Allowing caller to handle wait or simple delay.
            # We'll return the port immediately.
            self.port = port
            return port

        except Exception as e:
            self._log(f"Frontend startup failed: {e}", "error", log_callback)
            return None

    def stop_frontend(self):
        """Stop the subprocess if we own it."""
        if self.process:
            self.process.terminate()
            self.process = None
            try:
                if os.path.exists(self.get_port_file()):
                    os.remove(self.get_port_file())
            except: pass
