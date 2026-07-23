# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
VAF System Tray – persistent background service with platform-specific implementations.

Platform split:
  - macOS (Darwin): Uses rumps for native Cocoa menu bar. Requires main-thread run loop.
  - Windows/Linux: Uses pystray for system tray. Icon must be shown only after event loop is ready.

Key platform considerations (see docs/platform/SYSTEM_TRAY.md):
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

# Linux: point BOTH toolkits at X11/XWayland. Native Wayland causes GTK protocol errors and
# an EGL/GLX conflict in Qt WebEngine - and with the GPU in-process it can deadlock the
# Chromium compositor against the Qt scene graph (SIGABRT, live incident 2026-07-20).
# force_x11 OVERRIDES a session-exported QT_QPA_PLATFORM=wayland; the previous
# os.environ.setdefault() silently failed to, because KDE/GNOME Wayland sessions export it.
# VAF_ALLOW_WAYLAND=1 opts out. Runs here, before any heavy import and long before Qt loads.
_DISPLAY_PLATFORM_STATUS = ""
if sys.platform == "linux":
    # stdlib-only module, and vaf/core/__init__.py is empty - no import cascade here.
    from vaf.core.display_platform import force_x11 as _force_x11
    _DISPLAY_PLATFORM_STATUS = _force_x11()
    # Disable DMA-buf renderer — causes GBM buffer errors under XWayland with many GPU drivers.
    # Compositing stays enabled so CSS animations and custom cursors render correctly.
    os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")
    _typelib_path = "/usr/lib64/girepository-1.0:/usr/lib/girepository-1.0"
    _existing = os.environ.get("GI_TYPELIB_PATH", "")
    if _typelib_path not in _existing:
        os.environ["GI_TYPELIB_PATH"] = f"{_typelib_path}:{_existing}".strip(":")

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

# Capture HARD crashes that leave no Python traceback — a native segfault/abort (e.g. the Qt WebEngine
# renderer dying) terminates the process before any `except` runs, so a silent death looks like "the log
# just stops". faulthandler dumps every thread's Python stack to a file on SIGSEGV/SIGABRT/SIGFPE/SIGBUS,
# so the NEXT occurrence is diagnosable (which thread was where) instead of a guess.
try:
    import faulthandler as _faulthandler
    _fh_log = open(Path(os.environ.get("VAF_LOG_DIR", str(repo_log_dir))) / "faulthandler.log", "a", buffering=1)
    _faulthandler.enable(file=_fh_log, all_threads=True)
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
from vaf.core.tray_context import TrayContext
import uvicorn
from vaf.startup_logger import log, clear_log

# Report the Linux display-server decision taken at the top of this module (before any Qt
# import). Logged here because _tray_startup_log needs get_dated_log_path, imported above.
if _DISPLAY_PLATFORM_STATUS:
    _tray_startup_log(f"display platform: {_DISPLAY_PLATFORM_STATUS}")
try:
    from PIL import Image, ImageDraw
except ImportError:
    Image = None


import logging

# Configure Logging (only write tray_debug_YYYY-MM-DD.log when Debug Logs is enabled)
logger = logging.getLogger("VAF_Tray")
logger.setLevel(logging.DEBUG)
if Config.get("debug_logs_enabled", True):
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
uvicorn_loop = None    # Event loop for the uvicorn server
internal_api_server = None  # 8005 internal HTTP channel server (TLS mode) — stoppable on restart/disable


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


def _ensure_macos_brew_path():
    """On macOS the tray is launched from a .app/launchd (PATH=/usr/bin:/bin:/usr/sbin:/sbin) or
    login bash (which sources ~/.bash_profile, NOT the ~/.zprofile where Homebrew writes its
    shellenv), so Homebrew's bin - where the installer puts colima/docker - is missing from PATH
    and the engine looks absent. Prepend the standard Homebrew bin dirs so colima/docker resolve."""
    if platform.system() != "Darwin":
        return
    for d in ("/opt/homebrew/bin", "/usr/local/bin"):
        if os.path.isdir(d) and d not in os.environ.get("PATH", "").split(os.pathsep):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


def _resolve_docker_exe():
    """Resolve a usable docker executable. On Windows Rancher's docker.exe lives in ~/.rd/bin
    and is added to PATH only during first-run, so a process started earlier won't see it -
    fall back to Rancher's known locations so we don't wait forever on a daemon that IS up.
    On macOS a Homebrew-installed docker/colima may be off the GUI/launchd PATH (see
    _ensure_macos_brew_path)."""
    import shutil
    _ensure_macos_brew_path()
    found = shutil.which("docker")
    if found:
        return found
    if platform.system() == "Windows":
        # docker.exe sits in the same dir as rdctl (Rancher bundles its CLIs); derive from rdctl.
        rd = shutil.which("rdctl")
        if rd:
            cand = os.path.join(os.path.dirname(rd), "docker.exe")
            if os.path.exists(cand):
                return cand
        for c in (
            os.path.join(os.environ.get("USERPROFILE", ""), ".rd", "bin", "docker.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Rancher Desktop", "resources", "resources", "win32", "bin", "docker.exe"),
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Rancher Desktop", "resources", "resources", "win32", "bin", "docker.exe"),
        ):
            if c and os.path.exists(c):
                return c
    return "docker"  # last resort: subprocess will raise FileNotFoundError if truly absent


def _is_docker_daemon_running():
    """Return True if Docker daemon is reachable (docker info succeeds)."""
    try:
        docker = _resolve_docker_exe()
        kwargs = {"check": True, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if platform.system() == "Windows" and getattr(subprocess, "CREATE_NO_WINDOW", None) is not None:
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        subprocess.run([docker, "info"], **kwargs, timeout=5)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _is_container_runtime_running():
    """True if a container-engine GUI (Rancher Desktop OR Docker Desktop) is already running
    (Windows only). If one is, we must NOT launch/reconfigure another - that restarts the engine
    and makes startup take far longer (the exact annoyance this guards against)."""
    if platform.system() != "Windows":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        names = (out.stdout or "").lower()
        return ("rancher desktop.exe" in names) or ("docker desktop.exe" in names)
    except Exception:
        return False


def _attempt_docker_daemon_start():
    """Try to start the container engine (Windows/macOS) so the daemon becomes available.

    Returns True if a runtime was actually launched, False if none was found.
    """
    import shutil
    _ensure_macos_brew_path()  # make a Homebrew-installed colima/docker visible before we probe
    try:
        if platform.system() == "Darwin":
            # We don't know which engine the user has, so detect instead of assuming.
            # Prefer Docker Desktop if its app is actually installed; otherwise use Colima
            # (the free engine VAF recommends + auto-installs). Never assume one is present.
            docker_desktop = (
                os.path.exists("/Applications/Docker.app")
                or os.path.exists(os.path.expanduser("~/Applications/Docker.app"))
            )
            colima = shutil.which("colima")
            if docker_desktop:
                try:
                    log("Tray", "Starting Docker Desktop (macOS)...")
                    subprocess.run(["open", "-a", "Docker"], check=True)
                    return True
                except Exception as _e:
                    log("Tray", f"Docker Desktop start failed: {_e}")
                    # fall through to Colima if that is also available
            if colima:
                log("Tray", "Starting the container engine via Colima (macOS)...")
                try:
                    subprocess.run([colima, "start"], check=True, timeout=300)
                    return True
                except subprocess.TimeoutExpired:
                    # A first-ever provision can exceed 5 min; the lima VM keeps booting in the
                    # background, so report success and let the readiness poll catch the daemon.
                    log("Tray", "Colima is still provisioning the VM (will keep polling for the daemon)...")
                    return True
                except Exception as _e:
                    log("Tray", f"colima start failed: {_e}")
            log("Tray", "No container engine (Docker Desktop / Colima) found to start on macOS.")
            return False
        elif platform.system() == "Windows":
            _cf = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            # If Rancher Desktop is already running, do NOT rdctl-reconfigure or relaunch it: a
            # reconfigure restarts the engine -> it drops offline -> endless waiting. Just let
            # ensure_memory_stack_up() keep polling until the engine finishes coming up.
            if _is_container_runtime_running():
                log("Tray", "A container runtime (Rancher/Docker Desktop) is already running - waiting for its engine (no restart).")
                return True
            _launched = False
            # First try the headless rdctl path (engine=moby, Kubernetes off) - same as
            # install.ps1. rdctl alone may not finish a first-ever provision, so we also launch
            # the GUI exe below, which reliably triggers the first-run WSL2 setup.
            _rdctl = shutil.which("rdctl")
            if _rdctl:
                log("Tray", "Starting Rancher headless via rdctl (dockerd/moby, Kubernetes off)...")
                try:
                    _r = subprocess.run(
                        [_rdctl, "start", "--container-engine.name", "moby", "--kubernetes.enabled=false"],
                        capture_output=True, text=True, timeout=300, creationflags=_cf,
                    )
                    log("Tray", f"rdctl start exited {_r.returncode}: {((_r.stderr or _r.stdout) or '').strip()[:300]}")
                    _launched = True
                except Exception as _e:
                    log("Tray", f"rdctl start failed: {_e}")
            # Prefer Rancher Desktop (what the installer sets up), fall back to Docker Desktop.
            _candidates = [
                os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Rancher Desktop", "Rancher Desktop.exe"),
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Rancher Desktop", "Rancher Desktop.exe"),
                os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Docker", "Docker", "Docker Desktop.exe"),
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Docker", "Docker Desktop.exe"),
            ]
            for _exe in _candidates:
                if _exe and os.path.exists(_exe):
                    log("Tray", f"Starting container runtime: {os.path.basename(_exe)}...")
                    subprocess.Popen([_exe], start_new_session=True, creationflags=_cf)
                    _launched = True
                    break
            if not _launched:
                log("Tray", "No Rancher Desktop / Docker Desktop found to auto-start.")
            return _launched
        else:
            subprocess.run(["docker", "ps"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            return True
    except Exception as e:
        log("Tray", f"Container engine start attempt failed: {e}")
        return False


def _compose_plugin_missing(stderr: str) -> bool:
    """True when 'docker compose' failed because the docker CLI has no compose PLUGIN (as
    opposed to compose itself failing). Seen with Homebrew docker + Colima on macOS when
    ~/.docker/config.json lacks cliPluginsExtraDirs: the CLI parses 'compose -f ...' as its
    own flags. The standalone docker-compose binary usually exists in that setup (install.sh
    brews it), so this state must fall through to the legacy binary, not give up."""
    s = (stderr or "").lower()
    return (
        "unknown shorthand flag" in s
        or "unknown flag" in s
        or "is not a docker command" in s
        or "unknown docker command" in s
    )


def ensure_memory_stack_up():
    """Start Docker memory stack (Postgres, Redis, Sandbox, TTS, STT). If Docker daemon is not running, try to start Docker Desktop and wait for it."""
    try:
        _ensure_macos_brew_path()  # make a Homebrew-installed colima/docker visible to this process
        # If the engine is not running, start it and wait - retrying across a few rounds. The tray
        # spawns this once, and a first-ever Colima/WSL2 provision can exceed a single wait window;
        # without a retry a slow first boot leaves the DB down -> the user stranded on the login page.
        if not _is_docker_daemon_running():
            engine_ready = False
            for attempt in range(1, 4):
                launched = _attempt_docker_daemon_start()
                if not launched:
                    log("Tray", f"No container runtime found yet (attempt {attempt}/3); retrying in 30s...")
                    time.sleep(30)
                    continue
                log("Tray", f"Waiting for the container engine to be ready (attempt {attempt}/3, max 300s; first run is slow)...")
                deadline = time.time() + 300
                while time.time() < deadline:
                    if _is_docker_daemon_running():
                        engine_ready = True
                        break
                    time.sleep(2)
                if engine_ready:
                    log("Tray", "Docker daemon is ready")
                    break
                log("Tray", f"Engine not ready after attempt {attempt}/3; retrying...")
            if not engine_ready:
                log("Tray", "Container engine did not come up; memory stack (RAG DB) unavailable. Start it (colima start / Docker Desktop / Rancher) and restart VAF.")
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
        # Two-phase, blocking, exit-checked: bring up the CORE registry-image services first so a
        # failed OPTIONAL build (tts/vaf-browser - e.g. a VM clock skew breaking apt) can never abort
        # the whole 'up' and leave zero containers (incl. the DB). Optional services are best-effort.
        docker = _resolve_docker_exe()  # Rancher's docker.exe may not be on this process's PATH
        core = ["postgres", "redis", "sandbox", "stt", "gotenberg"]
        optional = ["tts", "vaf-browser"]
        for base in (
            [docker, "compose", "-f", "docker-compose.memory.yml", "up", "-d", "--quiet-pull"],
            ["docker-compose", "-f", "docker-compose.memory.yml", "up", "-d"],
        ):
            try:
                kwargs = {"cwd": str(project_root), "capture_output": True, "text": True}
                if platform.system() == "Windows" and getattr(subprocess, "CREATE_NO_WINDOW", None) is not None:
                    kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                result = subprocess.run(base + core, timeout=600, **kwargs)  # first-run pulls are slow
                if result.returncode == 0:
                    log("Tray", "Core memory stack (DB/Redis/Sandbox/STT/Gotenberg) started")
                    try:  # optional build services: best-effort, must not block the core stack
                        opt = subprocess.run(base + optional, timeout=600, **kwargs)
                        if opt.returncode != 0:
                            log("Tray", "Optional TTS/browser did not build (often a VM clock skew) - core stack is up")
                    except Exception:
                        pass
                    return
                stderr = (result.stderr or "").strip()
                if _compose_plugin_missing(stderr):
                    log("Tray", "docker CLI has no compose plugin - trying legacy docker-compose...")
                    continue  # the legacy binary usually exists in this setup
                log("Tray", f"core compose up failed (code {result.returncode}): {stderr[:500]}")
                return
            except FileNotFoundError:
                continue  # try the docker-compose fallback
            except subprocess.TimeoutExpired:
                log("Tray", "docker compose up timed out (first-run image pull may still be in progress)")
                return
        log("Tray", "Warning: memory stack (RAG DB) may not have started; no docker/docker-compose CLI found (macOS/Colima: brew install docker-compose, or add cliPluginsExtraDirs to ~/.docker/config.json)")
    except Exception as e:
        logger.debug("[Tray] Memory stack auto-start skipped: %s", e)


def _wait_for_db_ready(max_wait: float = 25.0) -> bool:
    """Block up to max_wait seconds until PostgreSQL accepts queries; True when it does.

    The memory stack is started in a parallel thread, and on a normal restart Postgres needs
    a few seconds between "container up" and "accepting queries". A short wait here lets the
    web server's auth-DB init succeed on its FIRST attempt, so the first page render always
    shows the correct login/setup state. This is only a head start, not the correctness fix:
    when the wait times out (e.g. a first Rancher/WSL2 provision taking minutes) the web
    server's background retry creates the auth tables later. Skips immediately when no
    container engine is running - nothing is coming up soon and a DB-less install must not
    pay a startup delay.
    """
    try:
        import asyncpg
    except ImportError:
        return False
    import asyncio as _asyncio
    from urllib.parse import urlsplit

    # Same normalizer as the app itself (handles the bare user:pass@host/db config form);
    # strip SQLAlchemy-only query params, which asyncpg.connect would reject.
    from vaf.memory.database import get_database_url
    dsn = get_database_url().replace("postgresql+asyncpg://", "postgresql://", 1)
    dsn = urlsplit(dsn)._replace(query="").geturl()

    async def _probe() -> bool:
        conn = await asyncpg.connect(dsn, timeout=3)
        try:
            await conn.execute("SELECT 1")
        finally:
            await conn.close()
        return True

    async def _wait() -> bool:
        deadline = time.monotonic() + max_wait
        first = True
        while time.monotonic() < deadline:
            try:
                # wait_for bounds the WHOLE probe (a WSL2 port proxy can accept TCP and
                # then stall mid-query, which the connect timeout alone would not cover).
                if await _asyncio.wait_for(_probe(), 5.0):
                    if not first:
                        log("Tray", "Database is ready - starting web server")
                    return True
            except (asyncpg.InvalidPasswordError,
                    asyncpg.InvalidAuthorizationSpecificationError,
                    asyncpg.InvalidCatalogNameError):
                # A PostgreSQL answered but rejects our credentials/database: that is not
                # VAF's DB still booting, waiting longer cannot fix it.
                log("Tray", "A PostgreSQL is answering on the configured port but rejects VAF's credentials/database - not waiting (auth init retries in background)")
                return False
            except Exception:
                pass
            if first:
                first = False
                if not _is_docker_daemon_running():
                    log("Tray", "No container engine running - not waiting for the database (auth init retries in background)")
                    return False
                log("Tray", f"Waiting for the database to accept connections (max {int(max_wait)}s)...")
            await _asyncio.sleep(1.0)
        log("Tray", f"Database not ready after {int(max_wait)}s - starting web server anyway (auth init retries in background)")
        return False

    try:
        return _asyncio.run(_wait())
    except Exception as e:
        log("Tray", f"DB readiness pre-check skipped: {e}")
        return False


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

def start_uvicorn(wait_for_db: bool = True):
    """Start uvicorn server in a separate thread.

    wait_for_db: boot paths keep the default (short DB head start for a correct first
    render); the network-settings restart path passes False - the stack is already up
    there, and a down DB must not add a 25s backend outage per config change.
    """
    global uvicorn_server, uvicorn_loop
    log("Tray", "start_uvicorn thread started")
    try:
        import asyncio
        import sys
        import os

        # Web server app is imported lazily here (not at module level) so that
        # `import vaf.tray` does not pull in fastapi/uvicorn/web_server. Only the
        # tray path, which actually serves the app, needs it.
        from vaf.core.web_server import app, mark_webui_process

        # This process serves the web UI, so its sub-agents must run PIPED into the browser
        # panel rather than opening host terminal windows. This path does NOT go through
        # web_server.run_server (it drives uvicorn itself), so the declaration has to be made
        # here too - otherwise the spawn decision falls back to a flag that a transient
        # WebSocket drop used to clear, which is how a host terminal window appeared for a
        # web-launched sub-agent (live incident 2026-07-20).
        mark_webui_process()

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

        # Give PostgreSQL (started in a parallel thread) a short head start so the auth-DB
        # init in the FastAPI startup event succeeds on the first attempt and the first page
        # render shows the correct login/setup state. Bounded; must run BEFORE the uvicorn
        # event loop is created (asyncio.run would unset it otherwise).
        if wait_for_db:
            _wait_for_db_ready(max_wait=25.0)

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
        # Mask the /ws?token=<jwt> query param out of uvicorn's access log.
        from vaf.core.log_helper import redacted_uvicorn_log_config
        _log_cfg = redacted_uvicorn_log_config()
        if tls_enabled and ssl_cert and ssl_key:
            import os
            if os.path.isfile(ssl_cert) and os.path.isfile(ssl_key):
                print(f"[Tray] Starting Uvicorn with TLS (HTTPS/WSS) on port 8001 ({host})...")
                log("Tray", f"Initializing Uvicorn Config with TLS ({host}:8001)...")
                config = uvicorn.Config(
                    app, host=host, port=8001, log_level="info", use_colors=False,
                    log_config=_log_cfg,
                    ssl_certfile=ssl_cert, ssl_keyfile=ssl_key
                )
            else:
                log("Tray", "TLS enabled but cert/key files missing or invalid; starting without TLS")
                config = uvicorn.Config(app, host=host, port=8001, log_level="info", use_colors=False, log_config=_log_cfg)
        else:
            print(f"[Tray] Starting Uvicorn thread on port 8001 ({host})...")
            log("Tray", f"Initializing Uvicorn Config ({host}:8001)...")
            config = uvicorn.Config(app, host=host, port=8001, log_level="info", use_colors=False, log_config=_log_cfg)
        server = uvicorn.Server(config)
        uvicorn_server = server

        # --- START INTEGRATED HTTPS PROXY (single entry point for HTTPS, no Nginx required) ---
        # When TLS is on, proxy listens on 0.0.0.0:local_network_https_port and forwards to 127.0.0.1:3000/8001.
        # run_https_proxy tries that port and transparently falls back to 8443 when it is privileged
        # (<1024, e.g. 443) and cannot be bound without admin/root — on EVERY platform, not just Windows.
        # The effective port + bind result land in vaf.network.runtime_status so the UI shows the real URL.
        if tls_enabled and ssl_cert and ssl_key and os.path.isfile(ssl_cert) and os.path.isfile(ssl_key):
            try:
                from vaf.network.https_proxy import run_https_proxy
                https_port = Config.get("local_network_https_port", 443)
                def _run_https_proxy():
                    def _log(msg: str, _style: str = "info"):
                        log("HTTPS-Proxy", msg)
                    run_https_proxy("0.0.0.0", https_port, ssl_cert, ssl_key, log_callback=_log)
                threading.Thread(target=_run_https_proxy, daemon=True).start()
                log("Tray", f"Starting integrated HTTPS proxy (configured port {https_port}, auto-fallback to 8443 if privileged)")
            except Exception as px:
                log("Tray", f"HTTPS proxy failed to start: {px}")

        # --- START INTERNAL NON-SSL API CHANNEL (Port 8005) only when TLS is on ---
        # Next.js proxies to 8005 to avoid SSL trust issues. When TLS is off, Next.js proxies to 8001 directly.
        if tls_enabled:
            def _start_internal_api():
                global internal_api_server
                try:
                    import asyncio
                    log("Tray", "Starting internal API channel on port 8005...")
                    internal_cfg = uvicorn.Config(app, host="127.0.0.1", port=8005, log_level="warning", use_colors=False,
                                                  log_config=redacted_uvicorn_log_config())
                    internal_srv = uvicorn.Server(internal_cfg)
                    internal_srv.install_signal_handlers = lambda: None
                    internal_api_server = internal_srv  # track so restart/disable can stop it
                    internal_loop = asyncio.new_event_loop()
                    internal_loop.run_until_complete(internal_srv.serve())
                except Exception as ie:
                    log("Tray", f"Internal API channel failed: {ie}")
                finally:
                    internal_api_server = None
            threading.Thread(target=_start_internal_api, daemon=True).start()

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

    global internal_api_server
    try:
        # Stop the integrated HTTPS proxy + internal 8005 channel FIRST. They run in their own daemon
        # threads with no other handle, so without this they keep listening on the LAN port (8443) and
        # 8005 even after hosting is disabled — i.e. "turn network off" would NOT actually close LAN
        # access. start_uvicorn re-creates them only when TLS is on, so stopping them here is correct for
        # both restart and disable.
        try:
            from vaf.network.https_proxy import stop_https_proxy
            stop_https_proxy()
            log("Tray", "Stopped integrated HTTPS proxy (8443)")
        except Exception as _pe:
            log("Tray", f"Stopping HTTPS proxy failed: {_pe}")
        if internal_api_server is not None:
            try:
                internal_api_server.should_exit = True
                log("Tray", "Stopped internal API channel (8005)")
            except Exception:
                pass
            internal_api_server = None

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

        # Start new server thread (no DB gate: the stack is already up on a settings
        # restart, and a down DB must not add a 25s backend outage per config change)
        log("Tray", "Starting new server thread with updated settings...")
        server_thread = threading.Thread(target=start_uvicorn, kwargs={"wait_for_db": False}, daemon=True)
        server_thread.start()
        log("Tray", "Backend server restart completed")
        return True
    except Exception as e:
        log("Tray", f"Backend server restart failed: {e}")
        logger.error(f"Backend server restart failed: {e}")
        return False


# --- Serialized + coalesced network restart -------------------------------------------------------
# Enabling LAN mode writes SEVERAL config keys in one save (local_network_enabled + local_network_tls_
# enabled + …). The config observer fires once per changed key, and the file-poll loop can fire too — so
# without coordination we spawned MULTIPLE restart threads that raced on the SAME FrontendManager
# singleton (os.getpgid() on a self.process the other thread just set to None) and the global uvicorn
# server. Two threads tearing down + restarting the same processes at the same millisecond is the
# untraceable hard crash seen on "Apply Change". This lock makes at most ONE restart run at a time;
# a burst of key changes collapses into a single restart that reads the latest config.
_network_restart_lock = threading.Lock()
_network_restart_pending = threading.Event()
_network_restart_reason = {"key": None, "value": None}


def _do_network_restart(key, value):
    """Perform ONE network restart: protect the desktop webview, restart frontend + backend, then bring
    the webview back. Only ever called by the single serialized worker in _schedule_network_restart."""
    target_host = "127.0.0.1"  # network on => bind localhost, LAN access goes via the integrated proxy

    msg = f"Config change detected: {key}={value}. Restarting servers with host={target_host}..."
    log("Tray", msg)
    try: logger.info(f"[Tray] {msg}")
    except Exception: pass

    # Audit log
    try:
        from vaf.core.user_notifications import append_notification
        from vaf.core.config import get_local_admin_scope_id
        append_notification(
            user_scope_id=str(get_local_admin_scope_id()),
            kind="system",
            title="Network restart triggered",
            status="success",
            summary=f"Changed {key} to {value}.\nRestarting Backend & Frontend for network binding: {target_host}",
        )
    except Exception:
        pass

    # NOTE: do NOT navigate the webview during the teardown. Navigating it (to a splash) WHILE
    # stop_frontend simultaneously kills the page server (:3000) raced inside QtWebEngine and took the
    # whole app down (SIGKILL, no Python traceback). The webview safely stays on its current page for the
    # brief restart; we reload it AFTER the frontend is listening again (below) — the same ordering the
    # boot path uses (navigate only once :3000 is ready), which is reliable.

    # Restart Frontend (Next.js). start_frontend() blocks until the port is listening again.
    try:
        from vaf.core.frontend_manager import FrontendManager
        fm = FrontendManager()

        def fe_logger(m, style):
            log("Tray", f"[FE] {m}")
            try: logger.info(f"[Tray] [FE] {m}")
            except Exception: pass

        log("Tray", "Stopping frontend...")
        try: logger.info("[Tray] Stopping frontend...")
        except Exception: pass
        fm.stop_frontend(wait_for_exit=True)

        log("Tray", f"Starting frontend (host={target_host})...")
        try: logger.info(f"[Tray] Starting frontend (host={target_host})...")
        except Exception: pass
        fm.start_frontend(force_restart=True, host=target_host, log_callback=fe_logger)

        log("Tray", "Frontend restarted.")
        try: logger.info("[Tray] Frontend restarted.")
        except Exception: pass
    except Exception as e:
        log("Tray", f"Frontend restart failed: {e}")
        try: logger.error(f"[Tray] Frontend restart failed: {e}")
        except Exception: pass

    # Restart Backend (Uvicorn)
    try: logger.info("[Tray] Restarting backend...")
    except Exception: pass
    restart_backend_server()

    # Frontend is listening again → bring the webview back to the live app. The frontend's own reconnect
    # overlay covers any brief window before the freshly-restarted backend finishes coming up.
    try:
        from vaf.core import desktop_window
        desktop_window.navigate("http://127.0.0.1:3000")
    except Exception:
        pass


def _schedule_network_restart(key, value):
    """Coalesce + serialize network restarts. Safe to call from the config observer AND the file-poll
    loop; concurrent/duplicate calls collapse into a single restart instead of racing."""
    _network_restart_reason["key"] = key
    _network_restart_reason["value"] = value
    _network_restart_pending.set()
    if not _network_restart_lock.acquire(blocking=False):
        # A restart worker is already running; it will pick up the pending flag (and latest config).
        log("Tray", f"Network restart already in progress; coalescing change {key}={value}")
        return

    def _worker():
        try:
            while _network_restart_pending.is_set():
                # Debounce FIRST so the whole burst of key changes from one 'enable LAN' save settles,
                # then clear — so everything that arrived up to now is consumed by this single restart.
                # (Clearing before the sleep would let mid-sleep keys trigger a redundant second pass.)
                time.sleep(1.0)
                _network_restart_pending.clear()
                try:
                    _do_network_restart(_network_restart_reason["key"], _network_restart_reason["value"])
                except Exception as e:
                    log("Tray", f"Network restart failed: {e}")
                    try: logger.exception("[Tray] Network restart failed")
                    except Exception: pass
        finally:
            _network_restart_lock.release()

    threading.Thread(target=_worker, daemon=True).start()


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

    if not filename.exists():
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
            
            # Atomic write: never let a concurrent reader (pystray) observe a
            # half-written PNG. Write to a temp file, then rename into place.
            # format="PNG" is required because Pillow would otherwise infer the
            # format from the .tmp extension and fail ("unknown file extension").
            tmp = filename.with_name(filename.name + ".tmp")
            img.save(tmp, format="PNG")
            os.replace(tmp, filename)
        except Exception as e:
            print(f"Failed to create icon: {e}")
            return None
        
    return str(filename)

def _work_in_flight():
    """Is the machine doing something on the user's behalf right now?

    Returns (busy, reason). The idle watchdog uses this to decide whether the local model may
    be unloaded, and it is the signal that was missing entirely: the watchdog only knew "is a
    browser attached" and "has the user typed lately", both of which describe the USER. On
    2026-07-20 a tool call was in flight while the user had been quiet for a while, the model
    was unloaded underneath it, and the retry storm that followed is what looked like the app
    hanging.

    Three sources, all cheap and already maintained elsewhere:
      - the task queue: a chat turn being processed, or one waiting to be
      - the sub-agent IPC registry: a coder/research/document/workflow child at work
      - an open live voice call

    FAILS TOWARDS KEEPING THE MODEL. If a probe raises we cannot know, and the damage of
    guessing wrong differs by orders of magnitude: a needlessly warm model costs VRAM until
    the next check, while a wrong unload destroys work in progress. The reason is logged, so
    a permanently failing probe is visible instead of silently pinning the model forever.
    """
    try:
        from vaf.core.task_queue import TaskQueue
        tq = TaskQueue()
        if tq.is_busy():
            return True, "Task running"
        if tq.get_queue_size() > 0:
            return True, "Task queued"
    except Exception as e:
        log("Tray", f"work-in-flight probe (queue) failed, keeping model loaded: {e}")
        return True, "Busy probe failed"

    try:
        from vaf.core.subagent_ipc import get_ipc
        if get_ipc().get_active_tasks():
            return True, "Sub-agent running"
    except Exception as e:
        log("Tray", f"work-in-flight probe (sub-agents) failed, keeping model loaded: {e}")
        return True, "Busy probe failed"

    try:
        # A live call holds the model even when nothing is queued: the next utterance must
        # not wait for a reload. Read the registry the call handler maintains; importing
        # web_server here is free, the tray already serves it.
        #
        # Intersect with the LIVE sockets instead of trusting the dict's non-emptiness. The
        # teardown paths pop their entry, but a wedged-but-not-yet-disconnected socket could
        # still leave one behind, and this term is uniquely dangerous: unlike the others it
        # can be permanently true, which would pin the model forever and silently switch off
        # idle unloading altogether. Found in review before it shipped.
        from vaf.core.web_server import _VOICE_CALLS, manager as _ws_manager
        if _VOICE_CALLS:
            live = {id(ws) for ws in getattr(_ws_manager, "active_connections", [])}
            if any(key in live for key in _VOICE_CALLS):
                return True, "Voice call"
    except Exception:
        # No call registry in this build: not an error, just nothing to report.
        pass

    return False, ""


def check_activity_loop(update_icon_callback):
    """Monitor activity and manage model state."""
    last_loaded = None
    last_persistent = None
    last_provider = Config.get("provider", "local")  # track provider to react to a local<->API switch
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
                # -1 = AUTO: keep it -1 so the backend omits -ngl and llama.cpp auto-fits layers to VRAM
                # (offload overflow to CPU) instead of forcing all layers and aborting when it won't fit.
                
                started = server_mgr.start_server(
                    model_path=server_mgr.ensure_model_present(model),
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

        # Unified "user really away" signal = the local user is idle past the unload window, using the
        # SAME alias-merged per-user idle logic the thinking run uses (the local admin is logical None).
        # NOTE: do NOT read a single scope key here — get_last_interaction(None) only reads "default",
        # which missed the real admin scope and unloaded the model while the user was actively chatting.
        unload_idle_min = float(Config.get("model_unload_idle_minutes", 30) or 30)
        try:
            from vaf.core.thinking_mode import get_idle_user_scope_ids as _idle_ids
            really_away = None in _idle_ids(unload_idle_min)
        except Exception:
            really_away = False   # fail safe: keep the model loaded
        # Never unload while a thinking run is active or imminently due (think first, then unload).
        try:
            from vaf.core.thinking_mode import should_defer_model_unload as _sdmu
            thinking_defer = bool(_sdmu())
        except Exception:
            thinking_defer = False

        # Is actual WORK running right now? This is the one thing the idle watchdog never
        # asked. Its signals were "is a browser attached" (is_active) and "has the user typed
        # lately" (really_away) - both are about the USER, not about the machine. On
        # 2026-07-20 a long tool call was in flight while the user had not typed for a while,
        # so really_away won, the model was unloaded mid-run, and the request storm that
        # followed is what the user saw as the app being stuck.
        # Work is the opposite of idle: while it runs, nothing may pull the model out.
        work_busy, work_reason = _work_in_flight()

        # Collect any llama-server child a previous stop gave up waiting for. Unreaped killed
        # children linger as zombies for the life of this long-running process (two were left
        # behind by the 2026-07-20 incident); poll() is non-blocking, so this is free.
        try:
            from vaf.core.backend import reap_abandoned_children
            _reaped = reap_abandoned_children()
            if _reaped:
                log("Tray", f"Reaped {_reaped} abandoned llama-server child process(es)")
        except Exception:
            pass

        # Check provider type - any non-local provider is an API/cloud provider and needs no local model.
        # Kept provider-agnostic on purpose: a hardcoded allowlist used to omit newer providers (e.g.
        # `veyllo`), which made the tray load a phantom local model while in API mode.
        provider = Config.get("provider", "local")
        is_cloud_provider = provider != "local"

        # Provider switched at runtime -> manage the local model directly. The steady-state unload branch
        # below is gated on `not is_cloud_provider`, so a model loaded BEFORE a switch to a cloud/API
        # provider would otherwise stay warm forever. Free it here (level-triggered: retried each tick, and
        # deferred only while a thinking run is active), and (re)load it when switching back to local.
        # Dedicated local VOICE model (voice_agent_provider=local): the llama
        # server legitimately runs next to a cloud MAIN provider - it serves
        # only the live call. The cloud-unload must spare it while websockets
        # are connected (a possible call); the normal ws-idle unload below
        # still frees it when the UI is gone.
        voice_local_lane = False
        if is_cloud_provider and tray_context.model_loaded:
            try:
                voice_local_lane = (
                    (Config.get("voice_agent_provider", "") or "").strip().lower() == "local"
                    and has_websocket)
            except Exception:
                voice_local_lane = False
        if is_cloud_provider and tray_context.model_loaded and not thinking_defer and not voice_local_lane:
            log("Tray", f"Cloud provider '{provider}' active — unloading local model to free memory.")
            try:
                server_mgr.stop_server(force_external=True)
            except Exception as e:
                log("Tray", f"Model unload error: {e}")
            tray_context.set_model_loaded(False)
            update_icon_callback("active" if has_websocket else "idle")
            emit_model_state()
            last_provider = provider
            time.sleep(1)
            continue
        if (not is_cloud_provider) and provider != last_provider and not tray_context.model_loaded:
            log("Tray", "Provider switched to local — loading model...")
            start_model_async("Provider->local")
        last_provider = provider

        # For cloud providers, "ready" means we have a WebSocket connection
        # For local providers, "ready" means model is loaded
        is_ready = has_websocket if is_cloud_provider else is_loaded
        
        if is_persistent:
            if not is_loaded and not is_cloud_provider:
                print("Persistent mode enabled. Loading model...")
                log("Tray", "Persistent mode enabled. Loading model...")
                start_model_async("Persistent")
            update_icon_callback("persistent")
        else:
            # Keep the local model warm while a thinking run is active/due (think first, then unload), OR
            # while the app is actively used — UNLESS the user is REALLY away (no message for
            # model_unload_idle_minutes), in which case "active" (e.g. WebUI just sitting open) no longer
            # keeps it warm.
            # work_busy is unconditional on purpose: it is NOT gated on really_away, because
            # "the user has not typed for a while" says nothing about whether the machine is
            # busy on their behalf.
            keep_warm = thinking_defer or work_busy or ((not really_away) and (is_active or telegram_grace))
            if keep_warm:
                if not is_loaded and not is_cloud_provider:
                    reason = ("Thinking" if (thinking_defer and not (is_active or telegram_grace))
                              else (work_reason or "Activity") if work_busy else "Activity")
                    print(f"{reason} detected. Loading model...")
                    log("Tray", f"{reason} detected. Loading model...")
                    start_model_async(reason)
                update_icon_callback("active")
            elif is_loaded and not is_cloud_provider:
                # Not warm and thinking has nothing to do -> consider unloading the local model.
                # Trigger: the user is REALLY away (last message), or the legacy ws-idle fallback. Server/
                # headless never reaches here (the unload watchdog only runs in the desktop tray).
                no_web = tray_context.active_websockets == 0
                had_web = tray_context.last_websocket_disconnect > 0 or tray_context.last_websocket_activity > 0
                ws_idle = (
                    (had_web and time_since_ws_activity > tray_context.idle_timeout and time_since_disconnect > tray_context.idle_timeout)
                    or (not had_web and time_since_last > tray_context.idle_timeout)
                )
                if really_away or (no_web and ws_idle and not telegram_grace):
                    why = f"user away >{unload_idle_min:.0f}min" if really_away else f"ws idle {tray_context.idle_timeout}s"
                    print(f"Idle ({why}) reached. Unloading model...")
                    log("Tray", f"Idle ({why}) reached. Unloading model (loaded={is_loaded}).")
                    server_mgr.stop_server(force_external=True) # We own it effectively here
                    tray_context.set_model_loaded(False)
                    log("Tray", "Model unloaded.")
                    update_icon_callback("idle")
                else:
                    update_icon_callback("active" if is_ready else "idle")
            else:
                # Show "active" if ready (cloud: websocket connected, local: model loaded)
                update_icon_callback("active" if is_ready else "idle")

        if time.time() - last_log_ts >= 60:
            last_log_ts = time.time()
            state_line = (
                f"IdleCheck state: loaded={tray_context.model_loaded} "
                f"persistent={is_persistent} ws={tray_context.active_websockets} "
                f"lastHeartbeat={time_since_last:.1f}s lastWs={time_since_ws_activity:.1f}s "
                f"lastDisconnect={time_since_disconnect:.1f}s telegramGrace={telegram_grace} "
                # Without this the busy term is invisible: it only ever prints when it causes
                # a LOAD, so a term stuck at True would keep the model pinned and leave no
                # trace in the very log this incident was reconstructed from.
                f"work={work_reason or '-'}"
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
    """The port the integrated HTTPS proxy ACTUALLY bound (runtime truth), or the configured port as a
    fallback before it has bound. The proxy auto-falls-back from a privileged port (443) to 8443 on any
    platform, so this no longer guesses per-OS — it reads the real result."""
    configured = Config.get("local_network_https_port", 443)
    try:
        from vaf.network import runtime_status
        return runtime_status.effective_https_port(default=configured)
    except Exception:
        return configured


def open_webui(icon_or_item=None, item=None):
    """Show the VAF desktop window (or bring it to the front if already open)."""
    logger.info("[Tray] open_webui called")
    try:
        from vaf.core import desktop_window as _dw
        _dw.show()
        return
    except Exception as e:
        logger.warning("[Tray] open_webui: desktop_window unavailable (%s), falling back to browser", e)

def toggle_persistence(icon=None, item=None):
    """Pystray passes (icon, item); Rumps passes (sender). Accept both."""
    new_state = not tray_context.is_persistent()
    tray_context.set_persistent(new_state)
    if item is not None and hasattr(item, "state"):
        item.state = new_state

def quit_app(icon=None, item=None):
    """Handle quit action. Pystray passes (icon, item)."""
    # Disarm signal handlers so that a SIGTERM we send ourselves during cleanup
    # (the POSIX pkill below) does not re-enter quit_app(). signal.signal() only
    # works in the main thread, and pystray invokes quit_app() from a worker
    # thread (notably on Windows), so guard against that to avoid a ValueError.
    import signal as _signal
    if threading.current_thread() is threading.main_thread():
        try:
            _signal.signal(_signal.SIGTERM, _signal.SIG_IGN)
            _signal.signal(_signal.SIGINT, _signal.SIG_IGN)
        except (ValueError, OSError):
            pass

    # Hard timeout: if cleanup hangs longer than 25s, force exit.
    # 25s gives Docker enough time to stop containers (can take 10-20s).
    def force_exit():
        time.sleep(25)
        print("Force quitting after timeout...")
        os._exit(1)
    threading.Thread(target=force_exit, daemon=True).start()

    print("Shutting down...")
    tray_context.should_exit = True

    # Stop pystray icon so the tray menu disappears immediately.
    if icon is not None:
        try:
            icon.stop()
        except Exception:
            pass

    # Docker stop is slow (10-20s) — run it in the background so it does not
    # block the rest of the cleanup sequence.  The 25s force_exit gives it
    # enough headroom to finish before we call os._exit(0).
    def _stop_docker():
        try:
            stop_memory_stack()
        except Exception as e:
            print(f"Error stopping memory stack: {e}")
    threading.Thread(target=_stop_docker, daemon=True).start()

    # Stop Web UI (Next.js)
    try:
        from vaf.core.frontend_manager import FrontendManager
        FrontendManager().stop_frontend()
    except Exception as e:
        print(f"Error stopping frontend: {e}")

    # Stop uvicorn Web Server (API Backend on port 8001)
    global uvicorn_server
    if uvicorn_server:
        try:
            uvicorn_server.should_exit = True
            print("Web server (uvicorn) shutdown signal sent")
        except Exception as e:
            print(f"Error stopping web server: {e}")

    # Stop local llama-server
    try:
        server_mgr.stop_server(force_external=True)
    except Exception as e:
        print(f"Error stopping llama-server: {e}")

    # Close the desktop window (causes webview.start() to return on main thread)
    try:
        from vaf.core import desktop_window as _dw
        _dw.destroy()
    except Exception:
        pass

    # Kill any remaining Node.js / Python VAF processes.
    # Use SIGTERM first (allows graceful shutdown), then the force_exit
    # timer acts as the SIGKILL backstop after 25s.
    if platform.system() != "Windows":
        try:
            subprocess.run(["pkill", "-TERM", "-f", "node.*VAF"],
                           stderr=subprocess.DEVNULL, timeout=2)
            subprocess.run(["pkill", "-TERM", "-f", "python.*vaf.main"],
                           stderr=subprocess.DEVNULL, timeout=2)
        except Exception:
            pass

    # Small pause to let the Docker background thread and SIGTERM propagate.
    time.sleep(2)

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
            # -1 = AUTO: keep it -1 so the backend omits -ngl and llama.cpp auto-fits layers to VRAM
            # (offload overflow to CPU) instead of forcing all layers and aborting when it won't fit.
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
                model_path = server_mgr.ensure_model_present(model)
                started = server_mgr.start_server(model_path=model_path, port=8080, n_ctx=n_ctx, n_gpu_layers=gpu_layers)
                if started:
                    tray_context.set_model_loaded(True)
                    log("Tray", "llama-server restarted successfully with new settings.")
                else:
                    log("Tray", "llama-server restart failed.")
            except Exception as e:
                log("Tray", f"llama-server restart error: {e}")
        threading.Thread(target=_restart_llama, daemon=True).start()

    # Network binding changes → restart uvicorn + frontend.
    # Enabling LAN flips several of these keys in one save; _schedule_network_restart coalesces the burst
    # into a SINGLE serialized restart (no concurrent teardown of the frontend singleton / uvicorn).
    elif key in ["local_network_enabled", "local_network_port", "local_network_port_frontend", "local_network_tls_enabled", "local_network_https_port"]:
        _schedule_network_restart(key, value)

    # Provider or API key changed -> apply to the already-running agent live (no VAF restart),
    # so finishing onboarding with a cloud key (or switching provider in Settings) takes effect
    # immediately and no local GGUF is downloaded. The key VALUE is never logged.
    elif key == "provider" or key.startswith("api_key_"):
        def _apply_provider():
            time.sleep(0.5)  # let the config save finish
            try:
                from vaf.core.web_interface import get_web_interface
                ag = getattr(get_web_interface(), "agent_instance", None)
                if ag is not None and hasattr(ag, "reload_api_backend"):
                    if ag.reload_api_backend(force=key.startswith("api_key_")):
                        log("Tray", "Provider/key change applied to the running agent.")
            except Exception as e:
                log("Tray", f"Provider apply error: {e}")
        threading.Thread(target=_apply_provider, daemon=True).start()

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
# Cross-Platform Implementation (Pystray)
# ==========================================
# NOTE: rumps (macOS-only) was removed — it conflicts with pywebview for main-thread ownership.
# pystray runs detached (background thread); pywebview owns the main thread on all platforms.

# Skip pystray import in headless mode (VAF_NATIVE_WRAPPER=1) — no display needed
pystray = None
if os.environ.get("VAF_NATIVE_WRAPPER") != "1":
    # NOTE: pystray probes the platform display backend AT IMPORT (e.g. the X11
    # _xorg backend opens an Xlib.display.Display()). On a headless machine
    # (no $DISPLAY, e.g. CI) that raises a non-ImportError (Xlib.error.DisplayNameError),
    # which would otherwise crash module import. Catch broadly so importing vaf.tray
    # stays safe headless; the tray is simply disabled when no display is available.
    try:
        import pystray
        logger.info("[Tray] Using pystray for tray icon")
    except Exception as e:
        logger.warning("[Tray] pystray not available (%s) — tray icon disabled", e)

def create_image(color_name):
    """Create PIL Image for pystray icon."""
    path = get_icon_path(color_name)
    if path:
        # Eagerly decode and detach from the file. pystray resizes this image
        # lazily from its own setup thread; a bare Image.open() stays bound to the
        # PNG file, and that deferred cross-thread read can race a concurrent icon
        # rewrite -> "AssertionError: self.png is None" and no tray icon.
        # convert("RGBA") forces the decode now and returns an in-memory copy.
        img = Image.open(path).convert("RGBA")
        # Windows: taskbar expects 16x16 or 32x32 (see docs/platform/SYSTEM_TRAY.md)
        if platform.system() == "Windows" and (img.width > 32 or img.height > 32):
            img = img.resize((32, 32), Image.Resampling.LANCZOS)
        return img
    return Image.new('RGBA', (32, 32), (255, 0, 0, 255))  # Fallback


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
    _last_network_config["local_network_https_port"] = Config.get("local_network_https_port", 443)
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
    print("[VAF] Starting Web Server thread...")
    t = threading.Thread(target=start_uvicorn, daemon=True)
    t.start()
    
    # Start Headless Agent Loop
    print("[VAF] Starting Agent thread...")
    from vaf.core.headless_runner import run_headless_agent
    t_agent = threading.Thread(target=run_headless_agent, daemon=True, name="HeadlessAgent")
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
    _last_network_config["local_network_https_port"] = Config.get("local_network_https_port", 443)
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
    print("[Tray] Starting Web Server thread...")
    log("Tray", "Spawning Web Server thread...")
    t = threading.Thread(target=start_uvicorn, daemon=True)
    t.start()

    # Start Headless Agent Loop (for Web UI processing)
    print("[Tray] Starting Agent thread...")
    log("Tray", "Spawning Agent thread...")
    from vaf.core.headless_runner import run_headless_agent
    t_agent = threading.Thread(target=run_headless_agent, daemon=True, name="HeadlessAgent")
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
            print(f"[Tray] Frontend started on port {port}")
            log("Tray", f"Frontend started on port {port}")
            if auto_open:
                # The desktop window is localhost — it must ALWAYS load the local frontend directly,
                # regardless of TLS/LAN. Routing it through the public HTTPS proxy URL is wrong: that
                # port is for REMOTE devices, it hits a self-signed cert, and before the proxy has bound
                # _effective_https_port() falls back to 443 → "https://127.0.0.1" → connection refused
                # (the blank GUI). The integrated proxy stays up for LAN clients; this only changes what
                # the desktop window itself displays. Matches the restart path (desktop_window.navigate).
                actual_url = f"http://127.0.0.1:{port}"
                try:
                    from vaf.core import desktop_window as _dw
                    _dw.navigate(actual_url)
                    _dw.show()
                    log("Tray", f"Desktop window navigated to {actual_url}")
                except Exception as _dw_err:
                    log("Tray", f"Desktop window unavailable ({_dw_err}), opening browser")
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

    if not pystray:
        log("Tray", "pystray not available — skipping tray icon, starting desktop window only")
        # Start command listener even without tray (for singleton IPC)
        t_cmd = threading.Thread(target=command_listener, args=(lock_socket,), daemon=True)
        t_cmd.start()
    else:
        menu = pystray.Menu(
            pystray.MenuItem("Status: Idle", lambda icon, item: None, enabled=False),
            pystray.MenuItem("Open WebUI", open_webui, default=True),
            pystray.MenuItem("Persistent Server", toggle_persistence, checked=lambda item: tray_context.is_persistent()),
            pystray.MenuItem("Quit", quit_app)
        )

        icon = pystray.Icon("VAF", create_image("idle"), "VAF", menu)

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

        print("[Tray] Starting Tray Icon (pystray)...")
        log("Tray", "Starting Tray Icon (pystray)...")

        # Start activity monitoring thread
        t_logic = threading.Thread(target=check_activity_loop, args=(lambda state: update_icon(icon, state),), daemon=True)
        t_logic.start()

        # Start command listener
        t_cmd = threading.Thread(target=command_listener, args=(lock_socket,), daemon=True)
        t_cmd.start()

        # Run pystray in background — main thread belongs to pywebview
        _tray_startup_log("Tray icon.run_detached() starting")
        print("[Tray] Starting Tray Icon detached (background thread)...")
        log("Tray", "Starting pystray detached (background thread)")
        icon.run_detached()
        icon.visible = True

    # Initialise pywebview desktop window and hand main thread to its GUI loop
    _default_port = Config.get("local_network_port_frontend", 3000)
    _startup_url = f"http://127.0.0.1:{_default_port}"
    # Open on a self-contained SPLASH (loading screen), not the frontend URL:
    # the frontend may not be listening yet, and if another local app occupies
    # :3000 the window would briefly show THAT page. start_frontend_bg() above
    # navigates the window to the real (resolved) frontend port once it's ready.
    try:
        _splash_path = Path(__file__).parent / "media" / "splash.html"
        _splash_html = _splash_path.read_text(encoding="utf-8") if _splash_path.exists() else None
    except Exception:
        _splash_html = None
    try:
        from vaf.core import desktop_window as _dw
        _dw.init(_startup_url, title="VAF", html=_splash_html)
        _tray_startup_log("pywebview window created, starting GUI loop")
        log("Tray", "Starting pywebview GUI loop (main thread)")
        # Window/taskbar icon: prefer a .ico on Windows (the native window icon wants one),
        # fall back to the high-res PNG (Linux/macOS). vaf_icon_v6.ico is generated from the
        # logo at install time; app_icon.ico is the committed fallback.
        _media = Path(__file__).parent / "media"
        _icon_candidates = []
        if platform.system() == "Windows":
            _icon_candidates = [_media / "vaf_icon_v6.ico", _media / "app_icon.ico"]
        _icon_candidates.append(_media / "logo_original.png")
        _app_icon = next((str(p) for p in _icon_candidates if p.exists()), None)
        _dw.start(icon_path=_app_icon)  # blocks until destroy
    except ImportError:
        # pywebview not installed — keep process alive with a simple wait loop
        _tray_startup_log("pywebview not installed, running in browser-only mode")
        log("Tray", "pywebview not available — browser mode (main thread waiting)")
        while not tray_context.should_exit:
            time.sleep(1)
    _tray_startup_log("Tray main loop ended (normal quit)")

if __name__ == "__main__":
    run_app()
