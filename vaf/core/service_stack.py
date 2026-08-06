# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The Docker service stack: one place that finds, starts and stops it.

Moved OUT of the tray (vaf/tray.py), which had grown the only complete
implementation while `vaf run` started nothing - so a terminal-only start
left the memory DB down and `memory_search` reported an empty memory that
was in truth an unreachable one. The repo also carried four independent
"is the docker daemon up" probes (tray, python_sandbox, workspace_exec,
security_routes); this module is the one the launchers share. Sandbox-lane
callers keep their own probes deliberately - their error contract
("[SECURITY] Sandbox requires Docker") is part of the sandbox design.

Behavior is the tray's, verbatim where it matters:

- Engine bootstrap: macOS starts Docker Desktop or Colima, Windows starts
  Rancher/Docker Desktop (never re-configuring a runtime that is already
  running - that restarts the engine); Linux only probes.
- Two-phase compose up: CORE registry-image services first (postgres,
  redis, sandbox, stt, gotenberg), then the OPTIONAL locally-built ones
  (tts, vaf-browser) best-effort - a failed local build must never abort
  the `up` that carries the database.
- `docker compose` first, legacy `docker-compose` as the fallback when the
  CLI has no compose plugin (Homebrew docker + Colima).
- Stop uses `stop`, not `down`: containers and data survive for a fast
  restart.
- No compose file found (a pip install has none - the file ships with the
  repo checkout) -> honest no-op, callers can tell because the root
  resolves to None.

Every public function takes an optional `log` callable for the caller's
own voice (the tray logs, a boot phase prints); without one, lines go to
this module's logger.
"""
import logging
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

COMPOSE_FILENAME = "docker-compose.memory.yml"
CORE_SERVICES = ("postgres", "redis", "sandbox", "stt", "gotenberg")
OPTIONAL_SERVICES = ("tts", "vaf-browser")


def _say(log: Optional[Callable[[str], None]], message: str) -> None:
    if log is not None:
        try:
            log(message)
            return
        except Exception:
            pass
    logger.info(message)


def _ensure_macos_brew_path() -> None:
    """On macOS the tray is launched from a .app/launchd (PATH=/usr/bin:...)
    or login bash (which sources ~/.bash_profile, NOT the ~/.zprofile where
    Homebrew writes its shellenv), so Homebrew's bin - where the installer
    puts colima/docker - is missing from PATH and the engine looks absent."""
    if platform.system() != "Darwin":
        return
    for d in ("/opt/homebrew/bin", "/usr/local/bin"):
        if os.path.isdir(d) and d not in os.environ.get("PATH", "").split(os.pathsep):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


def resolve_docker_exe() -> str:
    """Resolve a usable docker executable. On Windows Rancher's docker.exe
    lives in ~/.rd/bin and is added to PATH only during first-run, so a
    process started earlier won't see it - fall back to Rancher's known
    locations so we don't wait forever on a daemon that IS up."""
    import shutil
    _ensure_macos_brew_path()
    found = shutil.which("docker")
    if found:
        return found
    if platform.system() == "Windows":
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
    return "docker"  # last resort: subprocess raises FileNotFoundError if truly absent


def is_docker_daemon_running() -> bool:
    """True if the Docker daemon is reachable (docker info succeeds)."""
    try:
        docker = resolve_docker_exe()
        kwargs = {"check": True, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if platform.system() == "Windows" and getattr(subprocess, "CREATE_NO_WINDOW", None) is not None:
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        subprocess.run([docker, "info"], **kwargs, timeout=5)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _is_container_runtime_running() -> bool:
    """True if a container-engine GUI (Rancher Desktop OR Docker Desktop) is
    already running (Windows only). If one is, we must NOT launch/reconfigure
    another - that restarts the engine and makes startup take far longer."""
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


def attempt_docker_daemon_start(log: Optional[Callable[[str], None]] = None) -> bool:
    """Try to start the container engine (Windows/macOS) so the daemon
    becomes available. Returns True if a runtime was actually launched."""
    import shutil
    _ensure_macos_brew_path()
    try:
        if platform.system() == "Darwin":
            docker_desktop = (
                os.path.exists("/Applications/Docker.app")
                or os.path.exists(os.path.expanduser("~/Applications/Docker.app"))
            )
            colima = shutil.which("colima")
            if docker_desktop:
                try:
                    _say(log, "Starting Docker Desktop (macOS)...")
                    subprocess.run(["open", "-a", "Docker"], check=True)
                    return True
                except Exception as _e:
                    _say(log, f"Docker Desktop start failed: {_e}")
            if colima:
                _say(log, "Starting the container engine via Colima (macOS)...")
                try:
                    subprocess.run([colima, "start"], check=True, timeout=300)
                    return True
                except subprocess.TimeoutExpired:
                    # A first-ever provision can exceed 5 min; the lima VM keeps
                    # booting in the background - let the readiness poll catch it.
                    _say(log, "Colima is still provisioning the VM (will keep polling for the daemon)...")
                    return True
                except Exception as _e:
                    _say(log, f"colima start failed: {_e}")
            _say(log, "No container engine (Docker Desktop / Colima) found to start on macOS.")
            return False
        elif platform.system() == "Windows":
            _cf = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if _is_container_runtime_running():
                _say(log, "A container runtime (Rancher/Docker Desktop) is already running - waiting for its engine (no restart).")
                return True
            _launched = False
            _rdctl = shutil.which("rdctl")
            if _rdctl:
                _say(log, "Starting Rancher headless via rdctl (dockerd/moby, Kubernetes off)...")
                try:
                    _r = subprocess.run(
                        [_rdctl, "start", "--container-engine.name", "moby", "--kubernetes.enabled=false"],
                        capture_output=True, text=True, timeout=300, creationflags=_cf,
                    )
                    _say(log, f"rdctl start exited {_r.returncode}: {((_r.stderr or _r.stdout) or '').strip()[:300]}")
                    _launched = True
                except Exception as _e:
                    _say(log, f"rdctl start failed: {_e}")
            _candidates = [
                os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Rancher Desktop", "Rancher Desktop.exe"),
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Rancher Desktop", "Rancher Desktop.exe"),
                os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Docker", "Docker", "Docker Desktop.exe"),
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Docker", "Docker Desktop.exe"),
            ]
            for _exe in _candidates:
                if _exe and os.path.exists(_exe):
                    _say(log, f"Starting container runtime: {os.path.basename(_exe)}...")
                    subprocess.Popen([_exe], start_new_session=True, creationflags=_cf)
                    _launched = True
                    break
            if not _launched:
                _say(log, "No Rancher Desktop / Docker Desktop found to auto-start.")
            return _launched
        else:
            subprocess.run(["docker", "ps"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            return True
    except Exception as e:
        _say(log, f"Container engine start attempt failed: {e}")
        return False


def _compose_plugin_missing(stderr: str) -> bool:
    """True when 'docker compose' failed because the docker CLI has no compose
    PLUGIN (as opposed to compose itself failing). Seen with Homebrew docker +
    Colima on macOS: the CLI parses 'compose -f ...' as its own flags. The
    standalone docker-compose binary usually exists in that setup, so this
    state must fall through to the legacy binary, not give up."""
    s = (stderr or "").lower()
    return (
        "unknown shorthand flag" in s
        or "unknown flag" in s
        or "is not a docker command" in s
        or "unknown docker command" in s
    )


def find_stack_root() -> Optional[Path]:
    """The directory holding the compose file, or None.

    Checked: the current working directory, then the repo root (two levels
    above this file). A pip install carries no compose file - None IS the
    honest answer there, and every caller treats it as "nothing to start"."""
    cwd_file = Path.cwd() / COMPOSE_FILENAME
    if cwd_file.exists():
        return Path.cwd()
    repo_root = Path(__file__).resolve().parents[2]
    if (repo_root / COMPOSE_FILENAME).exists():
        return repo_root
    return None


def ensure_service_stack(log: Optional[Callable[[str], None]] = None) -> bool:
    """Bring the service stack up (idempotent). Returns True when the CORE
    stack came up (or already ran); False when the engine never became ready,
    no compose file exists, or compose failed."""
    try:
        _ensure_macos_brew_path()
        # If the engine is not running, start it and wait - retrying across a
        # few rounds. A first-ever Colima/WSL2 provision can exceed a single
        # wait window; without a retry a slow first boot leaves the DB down.
        if not is_docker_daemon_running():
            engine_ready = False
            for attempt in range(1, 4):
                launched = attempt_docker_daemon_start(log)
                if not launched:
                    _say(log, f"No container runtime found yet (attempt {attempt}/3); retrying in 30s...")
                    time.sleep(30)
                    continue
                _say(log, f"Waiting for the container engine to be ready (attempt {attempt}/3, max 300s; first run is slow)...")
                deadline = time.time() + 300
                while time.time() < deadline:
                    if is_docker_daemon_running():
                        engine_ready = True
                        break
                    time.sleep(2)
                if engine_ready:
                    _say(log, "Docker daemon is ready")
                    break
                _say(log, f"Engine not ready after attempt {attempt}/3; retrying...")
            if not engine_ready:
                _say(log, "Container engine did not come up; service stack (memory DB) unavailable. Start it (colima start / Docker Desktop / Rancher) and restart VAF.")
                return False

        project_root = find_stack_root()
        if project_root is None:
            return False
        # Two-phase, blocking, exit-checked: bring up the CORE registry-image
        # services first so a failed OPTIONAL build (tts/vaf-browser - e.g. a
        # VM clock skew breaking apt) can never abort the whole 'up' and leave
        # zero containers (incl. the DB). Optional services are best-effort.
        docker = resolve_docker_exe()  # Rancher's docker.exe may be off PATH
        for base in (
            [docker, "compose", "-f", COMPOSE_FILENAME, "up", "-d", "--quiet-pull"],
            ["docker-compose", "-f", COMPOSE_FILENAME, "up", "-d"],
        ):
            try:
                kwargs = {"cwd": str(project_root), "capture_output": True, "text": True}
                if platform.system() == "Windows" and getattr(subprocess, "CREATE_NO_WINDOW", None) is not None:
                    kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                result = subprocess.run(base + list(CORE_SERVICES), timeout=600, **kwargs)
                if result.returncode == 0:
                    _say(log, "Core service stack (DB/Redis/Sandbox/STT/Gotenberg) started")
                    try:  # optional build services: best-effort, never block the core
                        opt = subprocess.run(base + list(OPTIONAL_SERVICES), timeout=600, **kwargs)
                        if opt.returncode != 0:
                            _say(log, "Optional TTS/browser did not build (often a VM clock skew) - core stack is up")
                    except Exception:
                        pass
                    return True
                stderr = (result.stderr or "").strip()
                if _compose_plugin_missing(stderr):
                    _say(log, "docker CLI has no compose plugin - trying legacy docker-compose...")
                    continue
                _say(log, f"core compose up failed (code {result.returncode}): {stderr[:500]}")
                return False
            except FileNotFoundError:
                continue
            except subprocess.TimeoutExpired:
                _say(log, "docker compose up timed out (first-run image pull may still be in progress)")
                return False
        _say(log, "Warning: service stack may not have started; no docker/docker-compose CLI found (macOS/Colima: brew install docker-compose, or add cliPluginsExtraDirs to ~/.docker/config.json)")
        return False
    except Exception as e:
        logger.debug("Service stack auto-start skipped: %s", e)
        return False


def stop_service_stack(log: Optional[Callable[[str], None]] = None) -> bool:
    """Stop the stack with 'stop' (never 'down'): containers and data survive
    for a fast restart. Returns True when a stop command succeeded."""
    try:
        project_root = find_stack_root()
        if project_root is None:
            _say(log, f"{COMPOSE_FILENAME} not found, skipping Docker stop")
            return False
        _say(log, f"Stopping Docker stack at {project_root}")
        for cmd in (
            ["docker", "compose", "-f", COMPOSE_FILENAME, "stop"],
            ["docker-compose", "-f", COMPOSE_FILENAME, "stop"],
        ):
            try:
                kwargs = {"cwd": str(project_root), "capture_output": True, "text": True}
                if platform.system() == "Windows" and getattr(subprocess, "CREATE_NO_WINDOW", None) is not None:
                    kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                result = subprocess.run(cmd, **kwargs, timeout=60)
                if result.returncode == 0:
                    _say(log, "Service stack stopped")
                    return True
                _say(log, f"Docker stop command failed with code {result.returncode}: {result.stderr}")
            except FileNotFoundError:
                continue
            except subprocess.TimeoutExpired:
                _say(log, "Docker stop timed out after 60s")
                break
            except Exception as e:
                _say(log, f"Docker stop error: {e}")
                break
        _say(log, "Warning: Docker stack may not have stopped properly")
        return False
    except Exception as e:
        _say(log, f"Service stack stop failed: {e}")
        return False
