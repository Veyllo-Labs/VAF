"""
VAF System Tray – persistent background service with platform-specific implementations.

Platform split:
  - macOS (Darwin): Uses rumps for native Cocoa menu bar. Requires main-thread run loop.
  - Windows/Linux: Uses pystray for system tray. Icon must be shown only after event loop is ready.

Key platform considerations (see docs/SYSTEM_TRAY.md):
  - Windows: Icon size 32x32; CREATE_NO_WINDOW for subprocesses; os.startfile() for URLs.
  - macOS: rumps.VafTrayApp; signal handlers for Cmd+Q; delayed_init for RunLoop readiness.
"""
import os
import sys

# CRITICAL: Disable CUDA for PyTorch BEFORE any torch import to prevent memory explosion
# PyTorch pre-allocates GPU memory even when using CPU-only models!
# This MUST happen at the very beginning of the process.
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")  # Hide GPU from PyTorch
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "max_split_size_mb:32")

import socket
import subprocess
from pathlib import Path

# CRITICAL FIX: Patch stdout/stderr/stdin IMMEDIATELY for pythonw (no console)
# This prevents crashes in logging/uvicorn which assume sys.stdout exists.
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w')
if sys.stdin is None:
    sys.stdin = open(os.devnull, 'r')

# Force a stable log location inside the repo unless explicitly overridden.
try:
    repo_log_dir = Path(__file__).resolve().parents[1] / "logs"
    repo_log_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("VAF_LOG_DIR", str(repo_log_dir))
except Exception:
    pass


def _tray_startup_log(msg: str, error: str = ""):
    """Always write to tray_startup_YYYY-MM-DD.txt for diagnostics (even when Debug Logs is off)."""
    try:
        fpath = get_dated_log_path("tray_startup", "txt")
        fpath.parent.mkdir(parents=True, exist_ok=True)
        ts = __import__("datetime").datetime.now().isoformat()
        with open(fpath, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}")
            if error:
                f.write(f" | ERROR: {error}")
            f.write("\n")
    except Exception:
        pass

import atexit
import time
import threading
import signal
import platform
import webbrowser
from vaf.core.config import Config
from vaf.core.log_helper import get_dated_log_path
from vaf.core.backend import ServerManager
from vaf.core.web_server import app
from vaf.core.tray_context import TrayContext
import uvicorn
from vaf.startup_logger import log, clear_log
try:
    from PIL import Image, ImageDraw
except ImportError:
    Image = None


import logging

# Configure Logging (only write tray_debug_YYYY-MM-DD.log when Debug Logs is enabled)
logger = logging.getLogger("VAF_Tray")
logger.setLevel(logging.DEBUG)
if Config.get("debug_logs_enabled", False):
    try:
        log_file = str(get_dated_log_path("tray_debug", "log"))
        fh = logging.FileHandler(log_file)
        fh.setLevel(logging.DEBUG)
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        fh.setFormatter(formatter)
        logger.addHandler(fh)
        logging.getLogger().addHandler(fh)
    except Exception:
        pass

# Global state
server_mgr = ServerManager()
tray_context = TrayContext()
server_thread = None
uvicorn_server = None  # Global reference for restart capability
uvicorn_internal_server = None  # Internal API server (port 8005) — must also be stopped on restart
uvicorn_loop = None    # Event loop for the uvicorn server


def _atexit_stop_server():
    """Ensure llama-server is stopped when the process exits (e.g. killed from outside)."""
    try:
        server_mgr.stop_server(force_external=True)
    except Exception:
        pass


atexit.register(_atexit_stop_server)


# Signal Handler for Clean Shutdown (Cmd+Q, Dock Quit, Terminal Ctrl+C)
def _signal_handler(sig, frame):
    """Handle termination signals and ensure clean shutdown."""
    print(f"\n[Signal] Received signal {sig}, initiating clean shutdown...")
    logger.info(f"[Signal] Received signal {sig}, initiating clean shutdown...")
    quit_app(None)

# Register signal handlers for ALL termination scenarios
signal.signal(signal.SIGTERM, _signal_handler)  # Dock Quit, kill command
signal.signal(signal.SIGINT, _signal_handler)   # Terminal Ctrl+C
if platform.system() == "Darwin":
    # macOS-specific: Handle Cmd+Q
    try:
        signal.signal(signal.SIGHUP, _signal_handler)
    except:
        pass

def check_singleton():
    """Ensure only one instance runs. If another instance is running, notify it to open browser."""
    log("Tray", "Checking singleton status...")
    import socket
    try:
        # Try to bind to port 8002 to ensure singleton
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        
        # STRICT SINGLETON: Do NOT use SO_REUSEADDR. We want bind to FAIL if active.
        # if platform.system() != "Windows":
        #    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            
        s.bind(("127.0.0.1", 8002))
        s.listen(5)
        log("Tray", "Singleton check passed (Port 8002 bound)")
        return s
    except socket.error as e:
        log("Tray", f"Singleton check failed: {e}")
        logger.warning(f"[Tray] Singleton check failed: {e}")
        # Port is busy, another instance is running.
        # Try to notify it.
        try:
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client.connect(("127.0.0.1", 8002))
            client.sendall(b"ACTIVATE")
            client.close()
            logger.info("[Tray] Sent ACTIVATE signal to existing instance.")
            log("Tray", "Sent ACTIVATE signal to existing instance")
        except Exception as e:
            logger.error(f"[Tray] Failed to notify existing instance: {e}")
            log("Tray", f"Failed to notify existing instance: {e}")
        
        # Fallback: Just open the browser directly since we know VAF is running
        try:
            import webbrowser
            from vaf.core.config import Config
            if Config.get("local_network_enabled", False) and Config.get("local_network_tls_enabled", False):
                p = _effective_https_port()
                url = f"https://127.0.0.1:{p}" if p != 443 else "https://127.0.0.1"
            else:
                url = "http://127.0.0.1:3000"
            webbrowser.open(url)
            logger.info("[Tray] Opened Web UI directly via fallback.")
        except Exception:
            pass

        print("VAF is already running. Notifying existing instance...")
        return None


def _is_docker_daemon_running():
    """Return True if Docker daemon is reachable (docker info succeeds)."""
    try:
        kwargs = {"check": True, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if platform.system() == "Windows" and getattr(subprocess, "CREATE_NO_WINDOW", None) is not None:
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        subprocess.run(["docker", "info"], **kwargs, timeout=5)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _attempt_docker_daemon_start():
    """Try to start Docker Desktop (Windows/macOS) so the daemon becomes available. Fails silently."""
    try:
        if platform.system() == "Darwin":
            log("Tray", "Starting Docker Desktop (macOS)...")
            subprocess.run(["open", "-a", "Docker"], check=True)
        elif platform.system() == "Windows":
            log("Tray", "Starting Docker Desktop (Windows)...")
            docker_path = r"C:\Program Files\Docker\Docker\Docker Desktop.exe"
            if os.path.exists(docker_path):
                subprocess.Popen(
                    [docker_path],
                    start_new_session=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                subprocess.Popen(
                    ["Docker Desktop.exe"],
                    shell=True,
                    start_new_session=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
        else:
            subprocess.run(["docker", "ps"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    except Exception as e:
        log("Tray", f"Docker Desktop start attempt failed: {e}")


def ensure_memory_stack_up():
    """Start Docker memory stack (Postgres, Redis, Sandbox, TTS, STT). If Docker daemon is not running, try to start Docker Desktop and wait for it."""
    try:
        # If Docker is not running, try to start Docker Desktop and wait for daemon (RAG needs DB)
        if not _is_docker_daemon_running():
            _attempt_docker_daemon_start()
            log("Tray", "Waiting for Docker daemon (max 60s)...")
            deadline = time.time() + 60
            while time.time() < deadline:
                if _is_docker_daemon_running():
                    log("Tray", "Docker daemon is ready")
                    break
                time.sleep(2)
            else:
                log("Tray", "Docker daemon did not become ready; memory stack (RAG DB) may be unavailable")
                return

        # Repo root: compose file in cwd or next to vaf/ (parents[1] from vaf/tray.py)
        cwd_file = Path.cwd() / "docker-compose.memory.yml"
        parent_file = Path(__file__).resolve().parents[1] / "docker-compose.memory.yml"
        if cwd_file.exists():
            project_root = Path.cwd()
        elif parent_file.exists():
            project_root = Path(__file__).resolve().parents[1]
        else:
            return
        compose_file = project_root / "docker-compose.memory.yml"
        # TTS and STT are default services in docker-compose.memory.yml (no profile)
        for cmd in (
            ["docker", "compose", "-f", "docker-compose.memory.yml", "up", "-d"],
            ["docker-compose", "-f", "docker-compose.memory.yml", "up", "-d"],
        ):
            try:
                kwargs = {"cwd": str(project_root), "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
                if platform.system() == "Windows" and getattr(subprocess, "CREATE_NO_WINDOW", None) is not None:
                    kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                subprocess.Popen(cmd, **kwargs)
                log("Tray", "Memory stack (DB/Redis/Sandbox/TTS/STT) start requested via Docker")
                return
            except FileNotFoundError:
                continue
    except Exception as e:
        logger.debug("[Tray] Memory stack auto-start skipped: %s", e)


def stop_memory_stack():
    """Stop Docker memory stack (Postgres, Redis, Sandbox, TTS, STT). Uses 'stop' to preserve containers and data."""
    try:
        cwd_file = Path.cwd() / "docker-compose.memory.yml"
        parent_file = Path(__file__).resolve().parents[1] / "docker-compose.memory.yml"
        if cwd_file.exists():
            project_root = Path.cwd()
        elif parent_file.exists():
            project_root = Path(__file__).resolve().parents[1]
        else:
            log("Tray", "docker-compose.memory.yml not found, skipping Docker stop")
            return
        
        compose_file = project_root / "docker-compose.memory.yml"
        log("Tray", f"Stopping Docker stack at {project_root}")
        
        # Use 'stop' instead of 'down' to preserve containers (faster restart, keeps data)
        success = False
        for cmd in (
            ["docker", "compose", "-f", "docker-compose.memory.yml", "stop"],
            ["docker-compose", "-f", "docker-compose.memory.yml", "stop"],
        ):
            try:
                # Add timeout to prevent hanging indefinitely
                kwargs = {"cwd": str(project_root), "capture_output": True, "text": True}
                if platform.system() == "Windows" and getattr(subprocess, "CREATE_NO_WINDOW", None) is not None:
                    kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                
                result = subprocess.run(cmd, **kwargs, timeout=60)
                
                if result.returncode == 0:
                    log("Tray", "Memory stack (DB/Redis/Sandbox/TTS/STT) stopped successfully")
                    success = True
                    break
                else:
                    log("Tray", f"Docker stop command failed with code {result.returncode}: {result.stderr}")
            except FileNotFoundError:
                continue
            except subprocess.TimeoutExpired:
                log("Tray", "Docker stop timed out after 60s")
                break
            except Exception as e:
                log("Tray", f"Docker stop error: {e}")
                break
        
        if not success:
            log("Tray", "Warning: Docker stack may not have stopped properly")
            
    except Exception as e:
        log("Tray", f"Memory stack stop failed: {e}")
        try:
            logger.debug("[Tray] Memory stack stop failed: %s", e)
        except:
            pass


def command_listener(lock_socket):
    """Listens for 'ACTIVATE' signals from other instances."""
    log("Tray", "Starting command listener thread")
    while not tray_context.should_exit:
        try:
            lock_socket.settimeout(1.0)
            conn, addr = lock_socket.accept()
            with conn:
                data = conn.recv(1024)
                if b"ACTIVATE" in data:
                    logger.info("[Tray] Received ACTIVATE signal. Opening Web UI.")
                    log("Tray", "Received ACTIVATE signal")
                    # Run opening in a thread to not block the listener
                    threading.Thread(target=open_webui, args=(None,), daemon=True).start()
        except socket.timeout:
            continue
        except Exception as e:
            if not tray_context.should_exit:
                logger.error(f"[Tray] Command listener error: {e}")
                log("Tray", f"Command listener error: {e}")
            break

def start_uvicorn():
    """Start uvicorn server in a separate thread."""
    global uvicorn_server, uvicorn_internal_server, uvicorn_loop
    log("Tray", "start_uvicorn thread started")
    try:
        import asyncio
        import sys
        import os

        # Ensure UTF-8 output for background threads (prevents Unicode log crashes)
        os.environ.setdefault("PYTHONIOENCODING", "utf-8")

        # CRITICAL FIX: Patch stdout/stderr/stdin if running in pythonw (no console)
        # Uvicorn crashes if sys.stdout is None because it checks .isatty()
        if sys.stdout is None:
            sys.stdout = open(os.devnull, 'w')
        if sys.stderr is None:
            sys.stderr = open(os.devnull, 'w')
        if sys.stdin is None:
            sys.stdin = open(os.devnull, 'r')

        # Windows-specific: Initialize COM for this thread
        if platform.system() == "Windows":
            try:
                import pythoncom
                pythoncom.CoInitialize()
            except ImportError:
                pass  # pythoncom not available, skip COM init

        # Create a new event loop for this thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        uvicorn_loop = loop
        log("Tray", "Event loop created for Uvicorn")

        # --- ENSURE SSL CERTS EXIST BEFORE STARTING ---
        # If TLS enabled but no certs, generate them NOW so they are in config before uvicorn reads it
        if Config.get("local_network_enabled", False) and Config.get("local_network_tls_enabled", False):
            try:
                from vaf.network.ssl_utils import ensure_ssl_certificates
                cert_p, key_p = ensure_ssl_certificates()
                if cert_p and key_p:
                    log("Tray", "SSL certificates verified/generated successfully")
            except Exception as ssl_err:
                log("Tray", f"Pre-startup SSL setup failed: {ssl_err}")

        # Check if port 8001 is available
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        result = sock.connect_ex(('127.0.0.1', 8001))
        sock.close()
        if result == 0:
            log("Tray", "CRITICAL WARNING: Port 8001 is ALREADY IN USE! Server will likely fail.")
        else:
            log("Tray", "Port 8001 is free.")

        # When network is on, bind to 127.0.0.1; access via integrated HTTPS proxy (0.0.0.0:port).
        local_network_enabled = Config.get("local_network_enabled", False)
        host = "127.0.0.1" if local_network_enabled else "127.0.0.1"
        tls_enabled = Config.get("local_network_tls_enabled", False)
        ssl_cert = (Config.get("local_network_ssl_cert") or "").strip()
        ssl_key = (Config.get("local_network_ssl_key") or "").strip()
        if tls_enabled and ssl_cert and ssl_key:
            import os
            if os.path.isfile(ssl_cert) and os.path.isfile(ssl_key):
                print(f"[Tray] Starting Uvicorn with TLS (HTTPS/WSS) on port 8001 ({host})...")
                log("Tray", f"Initializing Uvicorn Config with TLS ({host}:8001)...")
                config = uvicorn.Config(
                    app, host=host, port=8001, log_level="info", use_colors=False,
                    ssl_certfile=ssl_cert, ssl_keyfile=ssl_key
                )
            else:
                log("Tray", "TLS enabled but cert/key files missing or invalid; starting without TLS")
                config = uvicorn.Config(app, host=host, port=8001, log_level="info", use_colors=False)
        else:
            print(f"[Tray] Starting Uvicorn thread on port 8001 ({host})...")
            log("Tray", f"Initializing Uvicorn Config ({host}:8001)...")
            config = uvicorn.Config(app, host=host, port=8001, log_level="info", use_colors=False)
        server = uvicorn.Server(config)
        uvicorn_server = server

        # --- START INTEGRATED HTTPS PROXY (single entry point for HTTPS, no Nginx required) ---
        # When TLS is on, proxy listens on 0.0.0.0:local_network_https_port and forwards to 127.0.0.1:3000/8001.
        # On Windows, port 443 often requires admin → use 8443 so it works without elevation.
        # NOTE: On server restart, the old proxy daemon is still alive and keeps working
        # (each request opens a fresh connection to the restarted backend).  Attempting to
        # start a second proxy would fail with EADDRINUSE, so we skip if the port is taken.
        if tls_enabled and ssl_cert and ssl_key and os.path.isfile(ssl_cert) and os.path.isfile(ssl_key):
            try:
                from vaf.network.https_proxy import run_https_proxy
                https_port = Config.get("local_network_https_port", 443)
                if platform.system() == "Windows" and https_port == 443:
                    https_port = 8443
                    log("Tray", "Using HTTPS port 8443 on Windows (443 would require admin).")
                # Check if proxy port is already in use (e.g. from previous start before restart)
                import socket as _sock
                _probe = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
                _port_free = _probe.connect_ex(('127.0.0.1', https_port)) != 0
                _probe.close()
                if _port_free:
                    def _run_https_proxy():
                        def _log(msg: str, _style: str = "info"):
                            log("HTTPS-Proxy", msg)
                        run_https_proxy("0.0.0.0", https_port, ssl_cert, ssl_key, log_callback=_log)
                    threading.Thread(target=_run_https_proxy, daemon=True).start()
                    log("Tray", f"Integrated HTTPS proxy started on 0.0.0.0:{https_port}")
                else:
                    log("Tray", f"HTTPS proxy port {https_port} already in use (existing proxy still running), skipping")
            except Exception as px:
                log("Tray", f"HTTPS proxy failed to start: {px}")

        # --- INTERNAL NON-SSL API CHANNEL (Port 8005) only when TLS is on ---
        # Next.js proxies to 8005 to avoid SSL trust issues. When TLS is off, Next.js proxies to 8001 directly.
        # IMPORTANT: Port 8005 MUST share the same event loop as port 8001! Otherwise WebSocket
        # connections via the HTTPS proxy (8443 → 8005) live on a different loop than
        # broadcast_to_session() which runs on port 8001's loop → streaming events silently fail.
        internal_srv = None
        uvicorn_internal_server = None  # Reset (TLS may be off on this restart)
        if tls_enabled:
            log("Tray", "Preparing internal API channel on port 8005 (shared event loop with 8001)...")
            internal_cfg = uvicorn.Config(app, host="127.0.0.1", port=8005, log_level="warning", use_colors=False)
            internal_srv = uvicorn.Server(internal_cfg)
            internal_srv.install_signal_handlers = lambda: None
            uvicorn_internal_server = internal_srv  # Store globally so restart can stop it

        # Manually disable signal handlers to prevent main thread interference
        server.install_signal_handlers = lambda: None

        # Force reset exit flag in case it was set by signal handlers during init
        server.should_exit = False

        log("Tray", "Running Uvicorn server (serve)...")
        try:
            if internal_srv:
                # Run both servers on the SAME event loop so all WebSocket connections
                # share one loop and broadcast_to_session works for proxy connections.
                async def _serve_all():
                    results = await asyncio.gather(
                        server.serve(), internal_srv.serve(),
                        return_exceptions=True
                    )
                    # If internal server failed but main survived, log it
                    for i, r in enumerate(results):
                        if isinstance(r, Exception):
                            name = "main server (8001)" if i == 0 else "internal API (8005)"
                            log("Tray", f"{name} stopped with error: {r}")
                loop.run_until_complete(_serve_all())
            else:
                loop.run_until_complete(server.serve())
        except Exception as serve_error:
            log("Tray", f"Uvicorn serve() failed: {serve_error}")
            raise serve_error

        log("Tray", "Uvicorn server stopped gracefully (loop ended)")
        log("Tray", "Uvicorn server stopped gracefully")

    except Exception as e:
        print(f"Web server thread failed: {e}")
        logger.error(f"Web server failed: {e}")
        log("Tray", f"CRITICAL: Web server thread crashed: {e}")
        import traceback
        log("Tray", traceback.format_exc())


def restart_backend_server():
    """Restart the backend uvicorn server with new network binding settings."""
    global uvicorn_server, uvicorn_internal_server, uvicorn_loop, server_thread
    log("Tray", "Backend server restart requested")

    try:
        # Stop BOTH servers (main + internal API).
        # CRITICAL: internal_srv (port 8005) MUST also be stopped!
        # Without this, asyncio.gather(server.serve(), internal_srv.serve()) never returns,
        # the old thread keeps running, old WebSocket connections stay alive on the old loop,
        # and broadcast_to_session silently fails (cross-loop send).
        if uvicorn_internal_server:
            log("Tray", "Stopping internal API server (port 8005)...")
            uvicorn_internal_server.should_exit = True
        if uvicorn_server:
            log("Tray", "Stopping current uvicorn server...")
            uvicorn_server.should_exit = True
            # Give both servers a moment to shut down
            time.sleep(1)

        # Wait for old thread to finish (now it WILL finish since both servers are stopped)
        if server_thread and server_thread.is_alive():
            log("Tray", "Waiting for old server thread to finish...")
            server_thread.join(timeout=5)
            if server_thread.is_alive():
                log("Tray", "WARNING: Old server thread did not finish within timeout")

        # Close old event loop so any stale _server_loop references fail immediately
        # (is_closed() returns True → _push_session_update drops instead of silently failing)
        # After join(), loop.run_until_complete() has returned → loop is stopped but not closed.
        # We can safely call close() on a stopped loop directly.
        old_loop = uvicorn_loop
        if old_loop and not old_loop.is_closed():
            try:
                old_loop.close()
                log("Tray", "Old event loop closed")
            except Exception as loop_err:
                log("Tray", f"Old event loop close warning: {loop_err}")

        # Clear stale WebSocket connections from the old server
        # These connections are dead (old server stopped) and would cause cross-loop errors
        try:
            from vaf.core.web_interface import get_web_interface
            wi = get_web_interface()
            stale_count = len(wi.active_connections)
            if stale_count > 0:
                log("Tray", f"Clearing {stale_count} stale WebSocket connection(s) from old server")
                wi.active_connections.clear()
                wi.connection_sessions.clear()
                wi.connection_users.clear()
                wi.connection_usernames.clear()
                wi.connection_roles.clear()
        except Exception as clear_err:
            log("Tray", f"Stale connection cleanup warning: {clear_err}")

        # Start new server thread
        log("Tray", "Starting new server thread with updated settings...")
        server_thread = threading.Thread(target=start_uvicorn, daemon=True)
        server_thread.start()
        log("Tray", "Backend server restart completed")
        return True
    except Exception as e:
        log("Tray", f"Backend server restart failed: {e}")
        logger.error(f"Backend server restart failed: {e}")
        return False

def get_icon_path(status):
    """Generate and return path to an icon for the given status using the VAF logo."""
    if not Image: return None
    
    # Define colors
    colors = {
        "active": (46, 204, 113),  # Green
        "idle": (241, 196, 15),    # Yellow
        "persistent": (231, 76, 60) # Red
    }
    color = colors.get(status, (128, 128, 128))
    
    # Ensure dir exists
    vaf_dir = Path(Config.load().get("vaf_dir", os.path.expanduser("~/.vaf")))
    icon_dir = vaf_dir / "icons"
    icon_dir.mkdir(parents=True, exist_ok=True)
    
    filename = icon_dir / f"tray_v2_{status}.png"
    
    # Base logo path
    base_dir = Path(__file__).parent.parent
    logo_path = base_dir / "vaf" / "media" / "logo_original.png"

    if not filename.exists() or True: # Force recreate for now to see changes
        try:
            if not logo_path.exists():
                logger.warning(f"[Tray] Logo not found at {logo_path}, using default icon behavior.")
                return None

            if logo_path.exists():
                # Load logo
                img_src = Image.open(logo_path).convert("RGBA")
                
                # Autocrop: Remove transparent borders
                # We use a small threshold to catch nearly-transparent pixels
                bbox = img_src.getbbox()
                if bbox:
                    img_src = img_src.crop(bbox)
                
                # Resize logo to fit standard Menu Bar height (22px) -> use 22x22 (or 44x44 for Retina)
                # We use 44x44 to look good on Retina, macOS will downscale if needed
                target_size = 44 
                img = Image.new('RGBA', (target_size, target_size), (0, 0, 0, 0))
                
                img_src.thumbnail((target_size, target_size), Image.Resampling.LANCZOS)
                
                # Center the logo
                offset = ((target_size - img_src.width) // 2, (target_size - img_src.height) // 2)
                img.paste(img_src, offset, img_src)
            else:
                # Fallback to High Contrast Circle (White)
                # This ensures visibility in Dark Mode (and usually Light Mode too)
                target_size = 44
                img = Image.new('RGBA', (target_size, target_size), (0, 0, 0, 0))
                draw = ImageDraw.Draw(img)
                # Draw white background circle (visible on dark menu bar)
                draw.ellipse((4, 4, 40, 40), fill=(200, 200, 200, 255))
            
            draw = ImageDraw.Draw(img)
            
            # Draw status indicator (Bottom Right)
            # Coordinates for 44x44
            draw.ellipse((28, 28, 42, 42), fill=(255, 255, 255, 255)) 
            draw.ellipse((30, 30, 40, 40), fill=color)
            
            img.save(filename)
        except Exception as e:
            print(f"Failed to create icon: {e}")
            return None
        
    return str(filename)

def check_activity_loop(update_icon_callback):
    """Monitor activity and manage model state."""
    last_loaded = None
    last_persistent = None
    last_log_ts = 0.0
    loading_lock = threading.Lock()
    loading_in_progress = False

    def start_model_async(reason: str):
        nonlocal loading_in_progress
        with loading_lock:
            if loading_in_progress or tray_context.model_loaded:
                return
            loading_in_progress = True

        def _runner():
            nonlocal loading_in_progress
            try:
                model = Config.get("model")
                # Fix: Retrieve context window and gpu layers from config
                n_ctx = Config.get("n_ctx", 8192)
                gpu_layers = Config.get("gpu_layers", 99)
                if gpu_layers == -1: gpu_layers = 99
                
                started = server_mgr.start_server(
                    model_path=server_mgr.get_model_path(model),
                    port=8080,
                    n_ctx=n_ctx,
                    n_gpu_layers=gpu_layers
                )
                log("Tray", f"{reason} start_server result: {started}")
                if started:
                    tray_context.set_model_loaded(True)
                    log("Tray", f"Model loaded ({reason}).")
                else:
                    log("Tray", f"Failed to start server ({reason}).")
            finally:
                with loading_lock:
                    loading_in_progress = False
        threading.Thread(target=_runner, daemon=True).start()

    def emit_model_state():
        try:
            from vaf.core.web_interface import get_web_interface
            provider = Config.get("provider", "local")
            loaded_for_ui = tray_context.model_loaded if provider == "local" else True
            get_web_interface().push_update({
                "type": "model_state",
                "loaded": loaded_for_ui,
                "persistent": tray_context.is_persistent(),
                "provider": provider
            })
        except Exception:
            pass

    while not tray_context.should_exit:
        is_loaded = tray_context.model_loaded
        is_persistent = tray_context.is_persistent()
        is_active = tray_context.is_active()
        time_since_last = time.time() - tray_context.last_heartbeat
        time_since_disconnect = time.time() - tray_context.last_websocket_disconnect
        time_since_ws_activity = time.time() - tray_context.last_websocket_activity
        has_websocket = tray_context.active_websockets > 0
        telegram_grace = tray_context.has_recent_telegram_activity()
        
        # Check provider type - cloud providers don't need local model loading
        provider = Config.get("provider", "local")
        is_cloud_provider = provider in ("openai", "anthropic", "google", "openrouter", "mistral", "groq", "deepseek")
        
        # For cloud providers, "ready" means we have a WebSocket connection
        # For local providers, "ready" means model is loaded
        is_ready = has_websocket if is_cloud_provider else is_loaded
        
        if is_persistent:
            if not is_loaded and not is_cloud_provider:
                print("Persistent mode enabled. Loading model...")
                log("Tray", "Persistent mode enabled. Loading model...")
                start_model_async("Persistent")
            update_icon_callback("persistent")
        elif is_active or telegram_grace:
            # Load model for Web/CLI activity or recent Telegram prompt (keeps model for telegram_idle_timeout)
            if not is_loaded and not is_cloud_provider:
                print("Activity detected. Loading model...")
                log("Tray", "Activity detected. Loading model...")
                start_model_async("Activity")
            update_icon_callback("active")
        else:
            # When no active web connection: unload after idle_timeout from last ws activity/disconnect.
            # If we have never had a websocket, use last_heartbeat so we do not unload immediately.
            # Do not unload while within telegram_idle_timeout of last Telegram prompt (e.g. 2 min).
            no_web = tray_context.active_websockets == 0
            had_web = tray_context.last_websocket_disconnect > 0 or tray_context.last_websocket_activity > 0
            idle_long_enough = (
                (had_web and time_since_ws_activity > tray_context.idle_timeout and time_since_disconnect > tray_context.idle_timeout)
                or (not had_web and time_since_last > tray_context.idle_timeout)
            )
            if (
                is_loaded and
                not is_cloud_provider and
                no_web and
                idle_long_enough and
                not telegram_grace
            ):
                print(f"Idle timeout ({tray_context.idle_timeout}s) reached. Unloading model...")
                log("Tray", f"Idle timeout reached. Unloading model (loaded={is_loaded}).")
                server_mgr.stop_server(force_external=True) # We own it effectively here
                tray_context.set_model_loaded(False)
                log("Tray", "Model unloaded.")
                update_icon_callback("idle")
            else:
                # Show "active" if ready (cloud: websocket connected, local: model loaded)
                update_icon_callback("active" if is_ready else "idle")

        if time.time() - last_log_ts >= 60:
            last_log_ts = time.time()
            state_line = (
                f"IdleCheck state: loaded={tray_context.model_loaded} "
                f"persistent={is_persistent} ws={tray_context.active_websockets} "
                f"lastHeartbeat={time_since_last:.1f}s lastWs={time_since_ws_activity:.1f}s "
                f"lastDisconnect={time_since_disconnect:.1f}s telegramGrace={telegram_grace}"
            )
            log("Tray", state_line)
            try:
                logger.info(state_line)
            except Exception:
                pass

        if last_loaded is None or last_loaded != tray_context.model_loaded or last_persistent != is_persistent:
            emit_model_state()
            last_loaded = tray_context.model_loaded
            last_persistent = is_persistent
                
        time.sleep(1)


def _effective_https_port() -> int:
    """Port for integrated HTTPS proxy. On Windows, 443 often needs admin → use 8443."""
    p = Config.get("local_network_https_port", 443)
    if platform.system() == "Windows" and p == 443:
        return 8443
    return p


def open_webui(icon_or_item=None, item=None):
    logger.info("[Tray] open_webui called")
    from vaf.core.frontend_manager import FrontendManager
    fm = FrontendManager()
    
    # Check/Start frontend
    port = fm.start_frontend()
    if not port:
        logger.error("[Tray] open_webui: Failed to start/find Web UI port")
        return

    # With network + TLS: access via integrated HTTPS proxy. Otherwise direct frontend.
    local_network_enabled = Config.get("local_network_enabled", False)
    tls_enabled = Config.get("local_network_tls_enabled", False)
    if local_network_enabled and tls_enabled:
        https_port = _effective_https_port()
        url = f"https://127.0.0.1:{https_port}" if https_port != 443 else "https://127.0.0.1"
    else:
        url = f"http://127.0.0.1:{port}"
    logger.info(f"[Tray] open_webui: Target URL is {url}")
    
    if platform.system() == "Windows":
        try:
            import win32gui
            import win32con
            import win32process
            import ctypes
            
            def window_enum_handler(hwnd, ctx):
                title = win32gui.GetWindowText(hwnd)
                # Check for "VAF" or "localhost:3000" in browser windows
                browser_keywords = ["Google Chrome", "Firefox", "Edge", "Browser", "Chromium", "Opera", "Brave"]
                is_browser = any(kw in title for kw in browser_keywords)
                has_vaf = "VAF" in title or "localhost" in title or "localhost:3000" in title or "127.0.0.1:3000" in title
                
                if is_browser and has_vaf and win32gui.IsWindowVisible(hwnd):
                    try:
                        # Minimize first then restore - workaround for Windows focus issue
                        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
                        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                        
                        # Force foreground window
                        try:
                            # Allow this process to set foreground window
                            ctypes.windll.user32.AllowSetForegroundWindow(-1)
                        except:
                            pass
                        
                        win32gui.SetForegroundWindow(hwnd)
                        ctx['found'] = True
                        ctx['hwnd'] = hwnd
                    except Exception as e:
                        logger.error(f"[Tray] open_webui: Focus window failed: {e}")

            context = {'found': False, 'hwnd': None}
            win32gui.EnumWindows(window_enum_handler, context)
            
            if context['found']:
                logger.info(f"[Tray] open_webui: Existing window found and focused (hwnd={context['hwnd']})")
                return
        except Exception as e:
            logger.error(f"[Tray] open_webui: Windows focus check failed: {e}")

    if platform.system() == "Darwin":
        # ... (macOS logic stays same) ...
        pass

    # Fallback: Open new tab using the most reliable method
    logger.info("[Tray] open_webui: Opening new browser tab...")
    if platform.system() == "Windows":
        # On Windows, use shell start command - most reliable for background processes
        try:
            # Use os.startfile which is the most reliable for URLs on Windows
            import os
            os.startfile(url)
            logger.info("[Tray] open_webui: Opened via os.startfile")
            return
        except Exception as e:
            logger.error(f"[Tray] open_webui: os.startfile failed: {e}")
        
        try:
            # Fallback to start command
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.Popen(["cmd", "/c", "start", "", url], shell=False, creationflags=creationflags)
            logger.info("[Tray] open_webui: Opened via cmd /c start")
            return
        except Exception as e2:
            logger.error(f"[Tray] open_webui: cmd start failed: {e2}")
    
    # Final fallback: webbrowser module
    try:
        import webbrowser
        webbrowser.open(url)
        logger.info("[Tray] open_webui: Opened via webbrowser.open")
    except Exception as e:
        logger.error(f"[Tray] open_webui: webbrowser.open failed: {e}")

def toggle_persistence(icon=None, item=None):
    """Pystray passes (icon, item); Rumps passes (sender). Accept both."""
    new_state = not tray_context.is_persistent()
    tray_context.set_persistent(new_state)
    if item is not None and hasattr(item, "state"):
        item.state = new_state

def quit_app(icon=None, item=None):
    """Handle quit action with safety check. Pystray passes (icon, item); Rumps passes (sender)."""
    # Force exit after 5 seconds if cleanup hangs
    def force_exit():
        time.sleep(5)
        print("Force quitting after timeout...")
        os._exit(1)
    
    threading.Thread(target=force_exit, daemon=True).start()

    # Check if CLI is running (heartbeat active)
    if tray_context.active_websockets > 0 or (time.time() - tray_context.last_heartbeat < 30):
        pass

    print("Shutting down...")
    tray_context.should_exit = True

    # Stop Web UI (Next.js)
    try:
        from vaf.core.frontend_manager import FrontendManager
        FrontendManager().stop_frontend()
    except Exception as e:
        print(f"Error stopping frontend: {e}")

    # Stop uvicorn Web Server (API Backend on port 8001 + internal API on 8005)
    global uvicorn_server, uvicorn_internal_server
    if uvicorn_internal_server:
        try:
            uvicorn_internal_server.should_exit = True
        except Exception:
            pass
    if uvicorn_server:
        try:
            uvicorn_server.should_exit = True
            print("Web server (uvicorn) shutdown signal sent")
            time.sleep(0.5)  # Give it time to shutdown gracefully
        except Exception as e:
            print(f"Error stopping web server: {e}")

    # Stop Docker memory stack (Postgres, Redis, Sandbox)
    try:
        stop_memory_stack()
    except Exception as e:
        print(f"Error stopping memory stack: {e}")

    # Stop local llama-server so it does not stay running after quit
    try:
        server_mgr.stop_server(force_external=True)
    except Exception as e:
        print(f"Error stopping llama-server: {e}")

    time.sleep(0.5)  # Give taskkill / process exit a moment
    
    # AGGRESSIVE SHUTDOWN for macOS (and Linux)
    # This ensures uvicorn, npm, next-server, and any detached subprocesses are killed.
    if platform.system() != "Windows":
        try:
            print("[Quit] Killing all VAF-related processes (Node.js, Python)...")
            # Kill all Node.js processes in VAF directory
            subprocess.run(
                ["pkill", "-9", "-f", "node.*VAF"],
                stderr=subprocess.DEVNULL,
                timeout=2
            )
            # Kill all Python VAF processes
            subprocess.run(
                ["pkill", "-9", "-f", "python.*vaf"],
                stderr=subprocess.DEVNULL,
                timeout=2
            )
            print("[Quit] All processes killed")
        except Exception as e:
            print(f"[Quit] Error during aggressive cleanup: {e}")
            # Last resort: kill process group
            try:
                import signal
                os.killpg(os.getpgrp(), signal.SIGKILL)
            except Exception:
                pass
            
    os._exit(0)


def on_config_changed(key, value, old_value=None):
    """Handle dynamic config changes."""
    # Model, context size, or GPU layers changed → restart llama-server with new values
    if key in ["model", "n_ctx", "gpu_layers"]:
        def _restart_llama():
            time.sleep(1)  # let config save finish
            from vaf.core.config import Config
            provider = Config.get("provider", "local")
            if provider != "local":
                return  # cloud providers don't use llama-server
            n_ctx = Config.get("n_ctx", 8192)
            gpu_layers = Config.get("gpu_layers", 99)
            if gpu_layers == -1:
                gpu_layers = 99
            model = Config.get("model")
            msg = f"Config changed ({key}={value}). Restarting llama-server"
            if key == "model":
                msg += f" with model {model}"
            msg += f" (n_ctx={n_ctx}, gpu_layers={gpu_layers})..."
            log("Tray", msg)
            
            # Audit log
            try:
                from vaf.core.user_notifications import append_notification
                from vaf.core.config import get_local_admin_scope_id
                append_notification(
                    user_scope_id=str(get_local_admin_scope_id()),
                    kind="system",
                    title="AI Model reload triggered",
                    status="success",
                    summary=f"Configuration change: {key}={value}\nRestarting local inference engine."
                )
            except: pass

            try:
                server_mgr.stop_server(force_external=True)
                tray_context.set_model_loaded(False)
                time.sleep(1)
                model_path = server_mgr.get_model_path(model)
                started = server_mgr.start_server(model_path=model_path, port=8080, n_ctx=n_ctx, n_gpu_layers=gpu_layers)
                if started:
                    tray_context.set_model_loaded(True)
                    log("Tray", "llama-server restarted successfully with new settings.")
                else:
                    log("Tray", "llama-server restart failed.")
            except Exception as e:
                log("Tray", f"llama-server restart error: {e}")
        threading.Thread(target=_restart_llama, daemon=True).start()

    # Network binding changes → restart uvicorn + frontend
    elif key in ["local_network_enabled", "local_network_port", "local_network_port_frontend", "local_network_tls_enabled", "local_network_https_port"]:
        def _restart_job():
            # Delay slightly to allow the config save to complete and response to be sent
            time.sleep(1)
            
            # Re-read config (network on => 127.0.0.1, access via integrated proxy)
            from vaf.core.config import Config
            target_host = "127.0.0.1"
            
            msg = f"Config change detected: {key}={value}. Restarting servers with host={target_host}..."
            log("Tray", msg)
            
            # Audit log
            try:
                from vaf.core.user_notifications import append_notification
                from vaf.core.config import get_local_admin_scope_id
                append_notification(
                    user_scope_id=str(get_local_admin_scope_id()),
                    kind="system",
                    title="Network restart triggered",
                    status="success",
                    summary=f"Changed {key} to {value}.\nRestarting Backend & Frontend for network binding: {target_host}"
                )
            except: pass

            try: logger.info(f"[Tray] {msg}")
            except: pass
            
            # Restart Frontend (Next.js)
            try:
                from vaf.core.frontend_manager import FrontendManager
                fm = FrontendManager()
                
                # Define callback to capture logs
                def fe_logger(msg, style):
                    log("Tray", f"[FE] {msg}")
                    try: logger.info(f"[Tray] [FE] {msg}")
                    except: pass

                # Stop waiting for exit to ensure we don't hang if it's stubborn
                log("Tray", "Stopping frontend...")
                try: logger.info("[Tray] Stopping frontend...")
                except: pass
                
                fm.stop_frontend(wait_for_exit=True)
                
                # Restart
                log("Tray", f"Starting frontend (host={target_host})...")
                try: logger.info(f"[Tray] Starting frontend (host={target_host})...")
                except: pass
                
                fm.start_frontend(force_restart=True, host=target_host, log_callback=fe_logger)
                
                log("Tray", "Frontend restarted.")
                try: logger.info("[Tray] Frontend restarted.")
                except: pass
            except Exception as e:
                log("Tray", f"Frontend restart failed: {e}")
                try: logger.error(f"[Tray] Frontend restart failed: {e}")
                except: pass
            
            # Restart Backend (Uvicorn)
            # This is defined in tray.py, so we can call it directly
            try: logger.info("[Tray] Restarting backend...")
            except: pass
            restart_backend_server()
        
        # Run in separate thread to avoid deadlock (uvicorn thread waiting for itself)
        threading.Thread(target=_restart_job, daemon=True).start()

# Last known network config (read from file by poll thread) so CLI changes in another process are picked up
_last_network_config = {}

def _config_file_poll_loop():
    """Poll config file every 25s; if local_network_* changed (e.g. by 'vaf server on' in CLI), trigger restart."""
    defaults = {"local_network_enabled": False, "local_network_port": 8001, "local_network_port_frontend": 3000, "local_network_tls_enabled": False, "local_network_https_port": 443}
    while True:
        time.sleep(25)
        try:
            cfg = Config.load()
            for key in ["local_network_enabled", "local_network_port", "local_network_port_frontend", "local_network_tls_enabled", "local_network_https_port"]:
                new_val = cfg.get(key, defaults.get(key))
                old_val = _last_network_config.get(key)
                if new_val != old_val:
                    on_config_changed(key, new_val, old_val)
                    _last_network_config[key] = new_val
        except Exception as e:
            logger.debug("Config poll failed: %s", e)

# ==========================================
# macOS Implementation (Rumps)
# ==========================================
if platform.system() == "Darwin":
    try:
        import rumps
        logger.info("[Tray] Rumps imported successfully.")
    except ImportError as e:
        import sys
        print(f"CRITICAL ERROR: 'rumps' module not found.\nError: {e}\nPlease run: pip install rumps pyobjc-framework-Cocoa", file=sys.stderr)
        sys.exit(1)

    class VafTrayApp(rumps.App):
        def __init__(self):
            # Simple Icon Logic
            icon_path = get_icon_path("idle")
            logger.info(f"[Tray] Init with icon: {icon_path}")
            
            # Init Rumps App
            super(VafTrayApp, self).__init__("VAF", icon=icon_path, quit_button=None)

            self.menu = [
                rumps.MenuItem("Status: Idle", callback=None),
                rumps.separator,
                rumps.MenuItem("Open WebUI", callback=self.on_open_webui),
                rumps.MenuItem("Persistent Server", callback=self.on_toggle_persist, key="P"),
                rumps.separator,
                rumps.MenuItem("Quit VAF", callback=self.on_quit)
            ]
            
            # Start Timer immediately
            self.timer = rumps.Timer(self.update_loop, 1)
            self.timer.start()

            # Robust macOS Dock Reopen Handler
            self._dock_timer = rumps.Timer(self.setup_mac_handlers, 1)
            self._dock_timer.start()
            
            # CRITICAL FIX: Start backend/frontend threads AFTER rumps RunLoop is ready
            # Use threading.Timer (not rumps.Timer) because rumps timers need RunLoop active
            import threading
            self._init_thread_timer = threading.Timer(0.5, self.delayed_init)
            self._init_thread_timer.daemon = True
            self._init_thread_timer.start()
            logger.info("[Tray] Scheduled delayed_init via threading.Timer")
        
        def delayed_init(self):
            """Initialize backend/frontend threads after rumps is ready."""
            logger.info("[Tray] Starting delayed initialization (backend/frontend)...")
            
            # CRITICAL: Force icon refresh to make it visible (macOS Finder launch bug)
            try:
                icon_path = get_icon_path("idle")
                self.icon = icon_path
                logger.info(f"[Tray] Forced icon refresh: {icon_path}")
            except Exception as e:
                logger.error(f"[Tray] Icon refresh failed: {e}")
            
            # Start Memory stack
            import threading
            threading.Thread(target=ensure_memory_stack_up, daemon=True).start()

            # Start Garbage Collector
            from vaf.core.garbage_collector import GarbageCollector
            GarbageCollector.get_instance().start()

            # Start Web Server
            global server_thread
            logger.info("[Tray] Starting Web Server thread...")
            server_thread = threading.Thread(target=start_uvicorn, daemon=True)
            server_thread.start()
            
            # Start Headless Agent Loop
            from vaf.core.headless_runner import run_headless_agent
            t_agent = threading.Thread(target=run_headless_agent, daemon=True)
            t_agent.start()
            
            # Start Frontend
            def start_frontend_bg():
                logger.info("[Tray] Starting Frontend manager...")
                from vaf.core.frontend_manager import FrontendManager
                auto_open = Config.get("web_ui_enabled", True)
                port = FrontendManager().start_frontend()
                if port and auto_open:
                    logger.info("[Tray] Frontend started, opening browser...")
                    if Config.get("local_network_enabled", False) and Config.get("local_network_tls_enabled", False):
                        p = _effective_https_port()
                        url = f"https://127.0.0.1:{p}" if p != 443 else "https://127.0.0.1"
                    else:
                        url = f"http://127.0.0.1:{port}"
                    try:
                        import webbrowser
                        webbrowser.open(url)
                    except Exception:
                        pass
            t_fe = threading.Thread(target=start_frontend_bg, daemon=True)
            t_fe.start()
            
            logger.info("[Tray] Delayed initialization complete!")


        def setup_mac_handlers(self, _):
            """Setup notification observers for macOS."""
            self._dock_timer.stop()
            try:
                from AppKit import NSNotificationCenter, NSApplicationDidBecomeActiveNotification
                from Foundation import NSObject
                import objc

                # We MUST use a real NSObject subclass to handle selectors reliably
                class ActivationObserver(NSObject):
                    def onActivate_(self, notification):
                        # Debounce check
                        current_time = time.time()
                        if current_time - getattr(self.app_instance, "_last_open", 0) < 1.0:
                            return
                        
                        self.app_instance._last_open = current_time
                        logger.info("[Tray] App activation detected (Dock click/Focus).")
                        threading.Thread(target=open_webui, args=(None,), daemon=True).start()

                self._observer_obj = ActivationObserver.alloc().init()
                self._observer_obj.app_instance = self
                self._last_open = 0
                
                NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
                    self._observer_obj, 
                    objc.selector(self._observer_obj.onActivate_, signature=b"v@:@"),
                    NSApplicationDidBecomeActiveNotification, 
                    None
                )
                
                logger.info("[Tray] Registered robust macOS Activation observer.")
            except Exception as e:
                logger.error(f"[Tray] Failed to setup macOS handlers: {e}")

        def update_loop(self, _):
            """Main logic loop called by timer."""
            try:
                is_active = tray_context.is_active()
                is_persistent = tray_context.is_persistent()
                
                status_text = "Persistent" if is_persistent else ("Active" if is_active else "Idle")
                self.menu["Status: Idle"].title = f"Status: {status_text}"
                self.menu["Persistent Server"].state = 1 if is_persistent else 0

                # Icon Update Logic
                target_status = "idle"
                if is_persistent:
                    target_status = "persistent"
                elif is_active:
                    target_status = "active"
                
                new_icon = get_icon_path(target_status)
                if self.icon != new_icon and os.path.exists(new_icon):
                    self.icon = new_icon

            except Exception as e:
                logger.error(f"Error in update loop: {e}")

        def on_open_webui(self, sender):
            try:
                logger.info("[Tray] on_open_webui clicked")
                threading.Thread(target=open_webui, args=(None,), daemon=True).start()
            except Exception as e:
                logger.error(f"Failed to open Web UI: {e}")

        def on_toggle_persist(self, sender):
            toggle_persistence(None)
            sender.state = not sender.state
        
        def on_quit(self, sender):
            quit_app(None)

    def run_app():
        logger.info("[Tray] run_app (Rumps) called")
        # Register config observer
        Config.add_observer(on_config_changed)

        # Singleton Check
        lock_socket = check_singleton()
        if not lock_socket:
            logger.warning("[Tray] Singleton check failed - Instance exists")
            return

        # Start App (threads will be initialized via delayed_init timer)
        logger.info("Initializing VafTrayApp().run()...")
        
        # Start Command Listener thread
        t_cmd = threading.Thread(target=command_listener, args=(lock_socket,), daemon=True)
        t_cmd.start()
        
        VafTrayApp().run()

# ==========================================
# Cross-Platform Implementation (Pystray)
# ==========================================
# Use pystray for ALL platforms (Windows, macOS, Linux)
# This ensures consistent behavior and avoids rumps app bundle issues
import pystray
from PIL import Image, ImageDraw
logger.info(f"[Tray] Using pystray for tray icon")

def create_image(color_name):
    """Create PIL Image for pystray icon."""
    path = get_icon_path(color_name)
    if path:
        img = Image.open(path)
        # Windows: taskbar expects 16x16 or 32x32 (see docs/SYSTEM_TRAY.md)
        if platform.system() == "Windows" and (img.width > 32 or img.height > 32):
            img = img.resize((32, 32), Image.Resampling.LANCZOS)
        return img
    return Image.new('RGB', (32, 32), 'red')  # Fallback


def run_headless():
    """Run VAF backend + frontend WITHOUT tray icon (for native macOS wrapper)."""
    clear_log()
    log("Tray", "run_headless called (native wrapper mode)")
    print("[VAF] Running headless - native Swift app handles tray icon")
    
    # Register config observer
    Config.add_observer(on_config_changed)
    _last_network_config["local_network_enabled"] = Config.get("local_network_enabled", False)
    _last_network_config["local_network_port"] = Config.get("local_network_port", 8001)
    _last_network_config["local_network_port_frontend"] = Config.get("local_network_port_frontend", 3000)
    _last_network_config["local_network_tls_enabled"] = Config.get("local_network_tls_enabled", False)
    threading.Thread(target=_config_file_poll_loop, daemon=True).start()

    # Singleton Check
    lock_socket = check_singleton()
    if not lock_socket:
        print("[VAF] Singleton check failed (another instance running)")
        return
    
    # Start Memory stack
    threading.Thread(target=ensure_memory_stack_up, daemon=True).start()

    # Start Garbage Collector
    from vaf.core.garbage_collector import GarbageCollector
    GarbageCollector.get_instance().start()

    # Start Web Server
    global server_thread
    print("[VAF] Starting Web Server thread...")
    server_thread = threading.Thread(target=start_uvicorn, daemon=True)
    server_thread.start()
    
    # Start Headless Agent Loop
    from vaf.core.headless_runner import run_headless_agent
    t_agent = threading.Thread(target=run_headless_agent, daemon=True)
    t_agent.start()
    
    # Start Frontend
    def start_frontend_bg():
        print("[VAF] Starting Frontend manager...")
        from vaf.core.frontend_manager import FrontendManager
        port = FrontendManager().start_frontend()
        if port:
            print(f"[VAF] Frontend started on port {port}")
    
    threading.Thread(target=start_frontend_bg, daemon=True).start()
    
    # Start command listener
    t_cmd = threading.Thread(target=command_listener, args=(lock_socket,), daemon=True)
    t_cmd.start()
    
    # Block forever (native wrapper handles quit via SIGTERM)
    print("[VAF] Headless mode active. Waiting for termination signal...")
    try:
        import signal as sig_module
        while True:
            sig_module.pause()
    except (KeyboardInterrupt, SystemExit):
        print("[VAF] Shutting down headless mode...")
        quit_app(None)


def run_app():
    _tray_startup_log("Tray run_app started (Pystray)")
    clear_log()
    log("Tray", "run_app called (Pystray)")
    # Register config observer
    Config.add_observer(on_config_changed)
    # So CLI "vaf server on" (other process) is picked up: poll config file every 25s
    _last_network_config["local_network_enabled"] = Config.get("local_network_enabled", False)
    _last_network_config["local_network_port"] = Config.get("local_network_port", 8001)
    _last_network_config["local_network_port_frontend"] = Config.get("local_network_port_frontend", 3000)
    _last_network_config["local_network_tls_enabled"] = Config.get("local_network_tls_enabled", False)
    threading.Thread(target=_config_file_poll_loop, daemon=True).start()

    print("[Tray] run_app called (Pystray)")
    # Singleton Check
    lock_socket = check_singleton()
    if not lock_socket:
        _tray_startup_log("Tray singleton failed (another instance running) - exiting")
        print("[Tray] Singleton check failed (another instance running)")
        log("Tray", "Singleton check failed - aborting")
        return

    print("[Tray] Singleton check passed")
    log("Tray", "Singleton check passed")

    # Start Memory stack (Postgres, Redis, Sandbox) automatically if Docker is available
    threading.Thread(target=ensure_memory_stack_up, daemon=True).start()

    # Start Garbage Collector
    from vaf.core.garbage_collector import GarbageCollector
    GarbageCollector.get_instance().start()

    # Initialize SpeechManager on Main Thread to avoid COM issues
    try:
        log("Tray", "Initializing SpeechManager (Main Thread)...")
        from vaf.core.speech import SpeechManager
        SpeechManager.get_instance()
        log("Tray", "SpeechManager initialized")
    except Exception as e:
        log("Tray", f"SpeechManager init failed: {e}")

    # Start Web Server
    global server_thread
    print("[Tray] Starting Web Server thread...")
    log("Tray", "Spawning Web Server thread...")
    server_thread = threading.Thread(target=start_uvicorn, daemon=True)
    server_thread.start()

    # Start Headless Agent Loop (for Web UI processing)
    print("[Tray] Starting Agent thread...")
    log("Tray", "Spawning Agent thread...")
    from vaf.core.headless_runner import run_headless_agent
    t_agent = threading.Thread(target=run_headless_agent, daemon=True)
    t_agent.start()

    # Start Frontend (Next.js) automatically
    def start_frontend_bg():
        print("[Tray] Starting Frontend manager...")
        log("Tray", "Starting Frontend Manager...")
        from vaf.core.frontend_manager import FrontendManager
        auto_open = Config.get("web_ui_enabled", True)
        port = FrontendManager().start_frontend(log_callback=lambda msg, style: log("Frontend", msg))
        if not port:
            # After reboot, PATH/npm may not be ready yet; retry once after a short delay
            log("Tray", "Frontend start failed, retrying in 10s (e.g. after reboot)...")
            time.sleep(10)
            port = FrontendManager().start_frontend(log_callback=lambda msg, style: log("Frontend", msg))
        if port:
            # Wait for backend (Uvicorn on 8001) to be reachable so the Web UI can call the API
            log("Tray", "Waiting for backend (port 8001) to be reachable...")
            backend_ready = False
            for _ in range(60):
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(1)
                    if sock.connect_ex(("127.0.0.1", 8001)) == 0:
                        backend_ready = True
                        sock.close()
                        break
                    sock.close()
                except Exception:
                    pass
                time.sleep(0.5)
            if backend_ready:
                log("Tray", "Backend (8001) is reachable")
            else:
                log("Tray", "Backend (8001) not ready after 30s; opening Web UI anyway")
            print(f"[Tray] Frontend started on port {port}, opening browser...")
            log("Tray", f"Frontend started on port {port}")
            if auto_open:
                open_webui(None)
        else:
            print("[Tray] Frontend failed to start.")
            log("Tray", "Frontend failed to start")
    print("[Tray] Starting Frontend thread...")
    t_fe = threading.Thread(target=start_frontend_bg, daemon=True)
    t_fe.start()

    # ==========================================
    # macOS Rumps Loop (Priority)
    # ==========================================
    if platform.system() == "Darwin" and "rumps" in sys.modules:
        log("Tray", "Entering macOS Rumps main loop")
        print("[Tray] Starting Rumps App...")
        
        # Create Rumps App Instance
        app = VafTrayApp()
        
        # Helper to map Rumps callbacks to our logic functions
        # Note: Rumps handles the loop, so we just set up the app and run it.
        # We need to bridge the update_icon logic.
        
        # Since Rumps runs its own loop, we can't easily share the exact same update_icon function
        # expecting a Pystray icon. But VafTrayApp has its own update_loop!
        # See VafTrayApp class definition earlier in file.
        
        # However, we still need to start the background logic threads that update state?
        # VafTrayApp handles its own state updates via self.timer calling self.update_loop
        # which checks tray_context. So we just need to ensure tray_context is updated by the activity loop?
        
        # Actually, check_activity_loop updates GLOBAL state or calls a callback?
        # It calls 'callback(state)'
        
        # Let's adjust check_activity_loop to update the Rumps app if running?
        # Or better: VafTrayApp has a timer (self.timer) that runs self.update_loop every 1s.
        # that update_loop calls get_icon_path(status).
        # So as long as tray_context status changes, Rumps will update.
        
        # We still need the logic thread to update tray_context?
        # check_activity_loop does: 
        #   state = "active" if websockets > 0 else "idle"
        #   callback(state)
        
        # If we use Rumps, we don't need the pystray-specific check_activity_loop callback.
        # We can run check_activity_loop with a dummy callback or one that updates a global var.
        # But wait, tray_context has active_websockets.
        # So VafTrayApp.update_loop just needs to check tray_context.
        
        # Let's ensure command_listener is running (it is started above? no, below Pystray init).
        # We need to start command listener for Rumps too.
        
        log("Tray", "Starting Command Listener thread (Rumps)...")
        t_cmd = threading.Thread(target=command_listener, args=(lock_socket,), daemon=True)
        t_cmd.start()
        
        # Logic Thread for Rumps?
        # monitoring active_websockets is done by main.py/server via tray_context updates?
        # No, check_activity_loop CALCULATES state.
        
        # We need a bridge.
        def rumps_state_updater(state):
            # This callback updates the global state that Rumps reads?
            # VafTrayApp.update_loop reads tray_context?
            # Let's look at VafTrayApp again (it's defined earlier).
            pass

        # Actually, standard check_activity_loop logic:
        # It monitors active_websockets and calls callback.
        # We can define a callback that updates the rumps app icon directly if we have access to 'app'
        
        def rumps_icon_callback(state):
            if app:
                path = get_icon_path(state)
                if path:
                    app.icon = path
        
        log("Tray", "Starting Activity Logic thread (Rumps)...")
        t_logic = threading.Thread(target=check_activity_loop, args=(rumps_icon_callback,), daemon=True)
        t_logic.start()

        app.run()
        return
        
    # ==========================================
    # Pystray Logic (Windows/Linux fallback)
    # ==========================================

    # Menu
    print("[Tray] Initializing Pystray icon...")
    log("Tray", "Initializing System Tray Icon...")
    
    menu = pystray.Menu(
        pystray.MenuItem("Status: Idle", lambda icon, item: None, enabled=False),
        pystray.MenuItem("Open WebUI", open_webui, default=True),
        pystray.MenuItem("Persistent Server", toggle_persistence, checked=lambda item: tray_context.is_persistent()),
        pystray.MenuItem("Quit", quit_app)
    )

    icon = pystray.Icon("VAF", create_image("idle"), "VAF Agent", menu)

    last_icon_state = None

    def update_icon(icon_obj, state):
        nonlocal last_icon_state
        if state == last_icon_state:
            return
        last_icon_state = state
        
        if state == "idle":
            icon_obj.icon = create_image("idle")
            try:
                logger.info("[Tray] Icon state -> idle")
            except Exception:
                pass
        elif state == "active":
            icon_obj.icon = create_image("active")
            try:
                logger.info("[Tray] Icon state -> active")
            except Exception:
                pass
        elif state == "persistent":
            icon_obj.icon = create_image("persistent")
            try:
                logger.info("[Tray] Icon state -> persistent")
            except Exception:
                pass
    
    # Create icon
    icon = pystray.Icon("VAF", create_image("idle"), "VAF", menu)
    
    # NOTE: Do NOT set icon.visible=True before run() - the window doesn't exist yet.
    # pystray's default setup (when setup=None) sets visible=True after the icon is ready.
    print("[Tray] Starting Tray Icon (pystray)...")
    log("Tray", "Starting Tray Icon (pystray)...")
    
    # Start activity monitoring thread
    t_logic = threading.Thread(target=check_activity_loop, args=(lambda state: update_icon(icon, state),), daemon=True)
    t_logic.start()
    
    # Start command listener
    t_cmd = threading.Thread(target=command_listener, args=(lock_socket,), daemon=True)
    t_cmd.start()
    
    # CRITICAL: icon.run() from main thread (macOS). setup= shows icon after loop ready (Windows).
    # See docs/SYSTEM_TRAY.md § Platform Implementation Notes.
    _tray_startup_log("Tray icon.run() starting (blocks until quit)")
    print("[Tray] Running icon.run() on main thread...")
    log("Tray", "Entering Pystray main loop (icon.run)")
    icon.run(setup=lambda i: setattr(i, "visible", True))  # Blocks here until quit
    _tray_startup_log("Tray icon.run() ended (normal quit)")

if __name__ == "__main__":
    run_app()
