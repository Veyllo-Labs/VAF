
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

import atexit
import time
import threading
import signal
import platform
import webbrowser
from vaf.core.config import Config
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

# Configure Logging (only write tray_debug.log when Debug Logs is enabled)
logger = logging.getLogger("VAF_Tray")
logger.setLevel(logging.DEBUG)
if Config.get("debug_logs_enabled", False):
    log_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "tray_debug.log")
    try:
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
uvicorn_loop = None    # Event loop for the uvicorn server


def _atexit_stop_server():
    """Ensure llama-server is stopped when the process exits (e.g. killed from outside)."""
    try:
        server_mgr.stop_server(force_external=True)
    except Exception:
        pass


atexit.register(_atexit_stop_server)

def check_singleton():
    """Ensure only one instance runs. If another instance is running, notify it to open browser."""
    log("Tray", "Checking singleton status...")
    import socket
    try:
        # Try to bind to port 8002 to ensure singleton
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        
        # Windows: DO NOT use SO_REUSEADDR for singleton checks as it allows multiple binds.
        # Unix: SO_REUSEADDR is fine/needed to restart quickly.
        if platform.system() != "Windows":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            
        s.bind(("127.0.0.1", 8002))
        s.listen(5)
        log("Tray", "Singleton check passed (Port 8002 bound)")
        return s
    except socket.error as e:
        log("Tray", f"Singleton check failed: {e}")
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
            return
        compose_file = project_root / "docker-compose.memory.yml"
        # Use 'stop' instead of 'down' to preserve containers (faster restart, keeps data)
        for cmd in (
            ["docker", "compose", "-f", "docker-compose.memory.yml", "stop"],
            ["docker-compose", "-f", "docker-compose.memory.yml", "stop"],
        ):
            try:
                kwargs = {"cwd": str(project_root), "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
                if platform.system() == "Windows" and getattr(subprocess, "CREATE_NO_WINDOW", None) is not None:
                    kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                result = subprocess.run(cmd, **kwargs, timeout=60)
                if result.returncode == 0:
                    log("Tray", "Memory stack (DB/Redis/Sandbox/TTS/STT) stopped via Docker")
                return
            except FileNotFoundError:
                continue
            except subprocess.TimeoutExpired:
                log("Tray", "Memory stack stop timed out")
                return
    except Exception as e:
        logger.debug("[Tray] Memory stack stop failed: %s", e)

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
    global uvicorn_server, uvicorn_loop
    log("Tray", "start_uvicorn thread started")
    try:
        import asyncio
        import pythoncom
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

        pythoncom.CoInitialize()

        # Create a new event loop for this thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        uvicorn_loop = loop
        log("Tray", "Event loop created for Uvicorn")

        # Check if port 8001 is available
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        result = sock.connect_ex(('127.0.0.1', 8001))
        sock.close()
        if result == 0:
            log("Tray", "CRITICAL WARNING: Port 8001 is ALREADY IN USE! Server will likely fail.")
        else:
            log("Tray", "Port 8001 is free.")

        # When local_network_enabled is False, bind only to localhost (not reachable from LAN)
        local_network_enabled = Config.get("local_network_enabled", False)
        host = "0.0.0.0" if local_network_enabled else "127.0.0.1"
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

        # Manually disable signal handlers to prevent main thread interference
        server.install_signal_handlers = lambda: None

        # Force reset exit flag in case it was set by signal handlers during init
        server.should_exit = False

        log("Tray", "Running Uvicorn server (serve)...")
        try:
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
    global uvicorn_server, uvicorn_loop, server_thread
    log("Tray", "Backend server restart requested")

    try:
        # Stop the current server
        if uvicorn_server:
            log("Tray", "Stopping current uvicorn server...")
            uvicorn_server.should_exit = True
            # Give it a moment to shut down
            time.sleep(1)

        # Wait for old thread to finish
        if server_thread and server_thread.is_alive():
            log("Tray", "Waiting for old server thread to finish...")
            server_thread.join(timeout=5)

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
            if logo_path.exists():
                # Load logo
                img_src = Image.open(logo_path).convert("RGBA")
                
                # Autocrop: Remove transparent borders
                # We use a small threshold to catch nearly-transparent pixels
                bbox = img_src.getbbox()
                if bbox:
                    img_src = img_src.crop(bbox)
                
                # Create 64x64 canvas
                img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
                
                # Resize logo to fill the ENTIRE canvas (64x64)
                img_src.thumbnail((64, 64), Image.Resampling.LANCZOS)
                
                # Center the logo on the canvas
                offset = ((64 - img_src.width) // 2, (64 - img_src.height) // 2)
                img.paste(img_src, offset, img_src)
            else:
                # Fallback to transparent canvas
                img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
            
            draw = ImageDraw.Draw(img)
            
            # Draw a small indicator circle in the bottom right corner
            # Smaller indicator to not cover the now even larger logo
            draw.ellipse((46, 46, 64, 64), fill=(255, 255, 255, 255)) 
            draw.ellipse((48, 48, 62, 62), fill=color)
            
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

        if time.time() - last_log_ts >= 5:
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

def open_webui(_):
    log("Tray", "open_webui called")
    from vaf.core.frontend_manager import FrontendManager
    fm = FrontendManager()
    
    # Check/Start frontend
    port = fm.start_frontend()
    if not port:
        log("Tray", "open_webui: Failed to start/find Web UI port")
        return

    url = f"http://localhost:{port}"
    log("Tray", f"open_webui: Target URL is {url}")
    
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
                has_vaf = "VAF" in title or "localhost:3000" in title or "127.0.0.1:3000" in title
                
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
                        log("Tray", f"open_webui: Focus window failed: {e}")

            context = {'found': False, 'hwnd': None}
            win32gui.EnumWindows(window_enum_handler, context)
            
            if context['found']:
                log("Tray", f"open_webui: Existing window found and focused (hwnd={context['hwnd']})")
                return
        except Exception as e:
            log("Tray", f"open_webui: Windows focus check failed: {e}")

    if platform.system() == "Darwin":
        # ... (macOS logic stays same) ...
        pass

    # Fallback: Open new tab using the most reliable method
    log("Tray", "open_webui: Opening new browser tab...")
    if platform.system() == "Windows":
        # On Windows, use shell start command - most reliable for background processes
        try:
            # Use os.startfile which is the most reliable for URLs on Windows
            import os
            os.startfile(url)
            log("Tray", "open_webui: Opened via os.startfile")
            return
        except Exception as e:
            log("Tray", f"open_webui: os.startfile failed: {e}")
        
        try:
            # Fallback to start command
            subprocess.Popen(["cmd", "/c", "start", "", url], shell=False, 
                           creationflags=subprocess.CREATE_NO_WINDOW)
            log("Tray", "open_webui: Opened via cmd /c start")
            return
        except Exception as e2:
            log("Tray", f"open_webui: cmd start failed: {e2}")
    
    # Final fallback: webbrowser module
    try:
        import webbrowser
        webbrowser.open(url)
        log("Tray", "open_webui: Opened via webbrowser.open")
    except Exception as e:
        log("Tray", f"open_webui: webbrowser.open failed: {e}")

def toggle_persistence(item):
    new_state = not tray_context.is_persistent()
    tray_context.set_persistent(new_state)
    item.state = new_state # Update menu checkmark (if supported)

def quit_app(icon_or_app):
    """Handle quit action with safety check."""
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

    # Stop Docker memory stack (Postgres, Redis, Sandbox)
    try:
        stop_memory_stack()
    except Exception as e:
        print(f"Error stopping memory stack: {e}")

    # Stop local llama-server so it does not stay running after quit
    server_mgr.stop_server(force_external=True)
    time.sleep(0.5)  # Give taskkill / process exit a moment
    os._exit(0)

def on_config_changed(key, value):
    """Handle dynamic config changes."""
    # We only care about network binding changes for now
    if key in ["local_network_enabled", "local_network_port"]:
        def _restart_job():
            # Delay slightly to allow the config save to complete and response to be sent
            time.sleep(1)
            
            # Re-read config to be sure
            from vaf.core.config import Config
            is_enabled = Config.get("local_network_enabled", False)
            target_host = "0.0.0.0" if is_enabled else "127.0.0.1"
            
            msg = f"Config change detected: {key}={value}. Restarting servers with host={target_host}..."
            log("Tray", msg)
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

# ==========================================
# macOS Implementation (Rumps)
# ==========================================
# ==========================================
# macOS Implementation (Rumps)
if platform.system() == "Darwin":
    try:
        logger.info("[Tray] Attempting to import rumps...")
        import rumps
        logger.info("[Tray] Rumps imported successfully.")

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

                self._last_open = 0

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
                            if current_time - self.app_instance._last_open < 1.0:
                                return
                            
                            self.app_instance._last_open = current_time
                            logger.info("[Tray] App activation detected (Dock click/Focus).")
                            threading.Thread(target=open_webui, args=(None,), daemon=True).start()

                    self._observer_obj = ActivationObserver.alloc().init()
                    self._observer_obj.app_instance = self
                    
                    NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
                        self._observer_obj, 
                        objc.selector(self._observer_obj.onActivate_, signature=b"v@:@"),
                        NSApplicationDidBecomeActiveNotification, 
                        None
                    )
                    
                    logger.info("[Tray] Registered robust macOS Activation observer.")
                except Exception as e:
                    logger.error(f"[Tray] Failed to setup macOS observers: {e}")

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

            def on_open_webui(self, _):
                open_webui(None)

            def on_toggle_persist(self, sender):
                toggle_persistence(sender)
                
            def on_quit(self, _):
                # Check for active session
                if tray_context.is_active():
                    resp = rumps.alert("Active Session", "A VAF session is currently active. Are you sure you want to quit?", ok="Quit", cancel="Cancel")
                    if resp != 1:
                        return
                quit_app(self)

        def run_app():
            logger.info("[Tray] run_app (Rumps) called")
            # Register config observer
            Config.add_observer(on_config_changed)

            # Singleton Check
            lock_socket = check_singleton()
            if not lock_socket:
                logger.warning("[Tray] Singleton check failed")
                return

            # Start Memory stack (Postgres, Redis, Sandbox) automatically if Docker is available
            threading.Thread(target=ensure_memory_stack_up, daemon=True).start()

            # Start Web Server
            logger.info("[Tray] Starting Web Server thread...")
            t = threading.Thread(target=start_uvicorn, daemon=True)
            t.start()
            
            # Start Headless Agent Loop (for Web UI processing)
            from vaf.core.headless_runner import run_headless_agent
            t_agent = threading.Thread(target=run_headless_agent, daemon=True)
            t_agent.start()
            
            # Start Frontend (Next.js) automatically
            def start_frontend_bg():
                logger.info("[Tray] Starting Frontend manager...")
                from vaf.core.frontend_manager import FrontendManager
                auto_open = Config.get("web_ui_enabled", True)
                port = FrontendManager().start_frontend()
                if port:
                    logger.info(f"[Tray] Frontend started on port {port}, opening browser...")
                    if auto_open:
                        import webbrowser
                        webbrowser.open(f"http://localhost:{port}")
            t_fe = threading.Thread(target=start_frontend_bg, daemon=True)
            t_fe.start()
            
            # Start App
            logger.info("Initializing VafTrayApp().run()")
            
            # Start Command Listener thread
            t_cmd = threading.Thread(target=command_listener, args=(lock_socket,), daemon=True)
            t_cmd.start()
            
            VafTrayApp().run()

    except ImportError:
        # Fallback to pystray if rumps is not available
        pass

# ==========================================
# Cross-Platform Implementation (Pystray)
# ==========================================
if platform.system() != "Darwin" or "rumps" not in sys.modules:
    import pystray
    from PIL import Image, ImageDraw

    def create_image(color_name):
        # Map color names to tuples/strings that PIL accepts if needed, or pass through
        # But we want to match get_icon_path logic or reuse it
        # Pystray wants an Image object, not a path
        path = get_icon_path(color_name)
        if path:
            return Image.open(path)
        return Image.new('RGB', (64, 64), 'red') # Fallback

    def run_app():
        clear_log()
        log("Tray", "run_app called (Pystray)")
        # Register config observer
        Config.add_observer(on_config_changed)
        
        print("[Tray] run_app called (Pystray)")
        # Singleton Check
        lock_socket = check_singleton()
        if not lock_socket:
            print("[Tray] Singleton check failed (another instance running)")
            log("Tray", "Singleton check failed - aborting")
            return

        print("[Tray] Singleton check passed")
        log("Tray", "Singleton check passed")

        # Start Memory stack (Postgres, Redis, Sandbox) automatically if Docker is available
        threading.Thread(target=ensure_memory_stack_up, daemon=True).start()

        # Initialize SpeechManager on Main Thread to avoid COM issues
        try:
            log("Tray", "Initializing SpeechManager (Main Thread)...")
            from vaf.core.speech import SpeechManager
            SpeechManager.get_instance()
            log("Tray", "SpeechManager initialized")
        except Exception as e:
            log("Tray", f"SpeechManager init failed: {e}")

        # Start Web Server
        print("[Tray] Starting Web Server thread...")
        log("Tray", "Spawning Web Server thread...")
        t = threading.Thread(target=start_uvicorn, daemon=True)
        t.start()

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

        def update_icon(state):
            if state == "active":
                icon.icon = create_image("active")
                try:
                    logger.info("[Tray] Icon state -> active")
                except Exception:
                    pass
            elif state == "idle":
                icon.icon = create_image("idle")
                try:
                    logger.info("[Tray] Icon state -> idle")
                except Exception:
                    pass
            elif state == "persistent":
                icon.icon = create_image("persistent")
                try:
                    logger.info("[Tray] Icon state -> persistent")
                except Exception:
                    pass
        
        # Logic Thread
        log("Tray", "Starting Activity Logic thread...")
        t_logic = threading.Thread(target=check_activity_loop, args=(update_icon,), daemon=True)
        t_logic.start()

        # Command Listener thread (for singleton activation)
        log("Tray", "Starting Command Listener thread...")
        t_cmd = threading.Thread(target=command_listener, args=(lock_socket,), daemon=True)
        t_cmd.start()

        log("Tray", "Entering Pystray main loop (icon.run)")
        icon.run()

if __name__ == "__main__":
    run_app()
