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
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Tuple

logger = logging.getLogger(__name__)

COMPOSE_FILENAME = "docker-compose.memory.yml"
COMPOSE_ENV_FILENAME = "compose.env"


@dataclass(frozen=True)
class ServiceSpec:
    """One service of the stack, described once for everyone who needs it.

    The list used to exist three times over: the two service tuples here, the
    container-name tuple in the security dashboard's API, and the compose file
    itself. Only the compose file is authoritative for what actually runs, so
    tests/test_compose_service_registry_sync.py compares this registry against
    it and fails when they drift.

    `config_url_key` / `env_url_var` name where the HOST port VAF expects is
    configured. A container can be up and still unreachable because the two
    disagree (a published port changed, a config key was edited), which reads
    as "the database is gone" everywhere else - so the port belongs in the
    registry, next to the container it describes.
    """

    service_key: str          # compose service name ("postgres")
    container_name: str       # compose container_name ("vaf-memory-db")
    required: bool            # core registry image vs optional local build
    config_url_key: str = ""  # config key carrying the expected host port
    env_url_var: str = ""     # environment override (browser: VAF_BROWSER_CDP_URL)
    default_port: int = 0     # compose default host port; 0 = publishes none
    probe: str = ""           # "" | "tcp" | "postgres" | "http:<path>"


# Kept in the compose file's own order. Ports are the compose defaults
# ("127.0.0.1:${VAR:-<default>}:<container port>").
SERVICES: Tuple[ServiceSpec, ...] = (
    ServiceSpec("postgres", "vaf-memory-db", True,
                config_url_key="memory_db_url", default_port=5432, probe="postgres"),
    ServiceSpec("redis", "vaf-redis", True,
                config_url_key="redis_url", default_port=6379, probe="tcp"),
    ServiceSpec("sandbox", "vaf-sandbox", True),
    # STT gets a TCP probe, not its HTTP health path: the compose healthcheck
    # ends in `|| exit 0`, so the service answers "healthy" while still loading
    # its model and an HTTP probe would report a failure that is not one.
    ServiceSpec("stt", "vaf-stt", True,
                config_url_key="speech_stt_docker_url", default_port=5003, probe="tcp"),
    ServiceSpec("gotenberg", "vaf-gotenberg", True,
                config_url_key="document_conversion_docker_url", default_port=5005, probe="tcp"),
    ServiceSpec("tts", "vaf-tts", False,
                config_url_key="speech_tts_docker_url", default_port=5002, probe="http:/health"),
    ServiceSpec("vaf-browser", "vaf-browser", False,
                env_url_var="VAF_BROWSER_CDP_URL", default_port=9222, probe="http:/json/version"),
)

CORE_SERVICES = tuple(s.service_key for s in SERVICES if s.required)
OPTIONAL_SERVICES = tuple(s.service_key for s in SERVICES if not s.required)


def service_by_container(name: str) -> Optional[ServiceSpec]:
    """The spec whose container carries this name, or None for a stranger."""
    wanted = (name or "").lstrip("/")
    for spec in SERVICES:
        if spec.container_name == wanted:
            return spec
    return None


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


def diagnose_docker_daemon() -> dict:
    """Why is docker unreachable? Returns {ok, reason, detail}.

    `is_docker_daemon_running` answers yes/no and is the hot path; this is the
    slow variant that keeps stderr, because the three failure modes need three
    different remedies and a repair run that cannot tell them apart offers the
    wrong one. Reasons: ok, no_cli (no docker executable at all),
    permission_denied (the socket exists but this user may not read it - the
    Linux docker-group case), not_running (daemon down), timeout.
    """
    docker = resolve_docker_exe()
    kwargs = {"capture_output": True, "text": True}
    if platform.system() == "Windows" and getattr(subprocess, "CREATE_NO_WINDOW", None) is not None:
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run([docker, "info"], timeout=10, **kwargs)
    except FileNotFoundError:
        return {"ok": False, "reason": "no_cli",
                "detail": "No docker executable found on PATH."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "timeout",
                "detail": "docker info did not answer within 10 seconds."}
    except Exception as e:  # a broken PATH entry, a killed subprocess
        return {"ok": False, "reason": "not_running", "detail": str(e)[:300]}
    if result.returncode == 0:
        return {"ok": True, "reason": "ok", "detail": ""}
    err = ((result.stderr or "") + " " + (result.stdout or "")).strip()
    low = err.lower()
    if "permission denied" in low or "got permission denied" in low:
        return {"ok": False, "reason": "permission_denied", "detail": err[:300]}
    return {"ok": False, "reason": "not_running", "detail": err[:300]}


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


def _compose_env() -> dict:
    """Environment for `compose up`, carrying the Redis password from the keyring.

    Redis caches DECRYPTED memory content and persists it, and it shipped with no
    password, no ACL and no TLS. The password is generated once into the keyring
    and handed to compose here, so it never reaches config.json or a shell history.
    """
    env = dict(os.environ)
    try:
        from vaf.memory.cache import redis_password
        password = redis_password()
        if password:
            env["REDIS_PASSWORD"] = password
    except Exception:
        pass
    return env


def compose_env_file() -> "Path":
    """Where the compose password file lives: beside the other secrets, never in the repo.

    It used to be `<checkout>/.env`, and that location was wrong twice over.
    On Windows the installation directory is frequently outside the user
    profile (`C:\\VAF`, `D:\\VAF`), where the drive-root ACL grants every local
    account read - and `harden_path` cannot fix that, because chmod restricts
    nothing there. The file holds the password to a Redis that caches DECRYPTED
    memory content, so it was the shortest way around the whole shield.

    The second reason is the working tree itself: editors treat a root `.env`
    as project configuration. VS Code's Python extension offers to inject it
    into every integrated terminal, which would put the password into the
    environment of every process started from the IDE.

    `~/.vaf` is inside the profile on every platform, is the same directory the
    KEK already uses, and is trivially reachable from the shell launchers as
    `$HOME/.vaf/compose.env`.
    """
    from pathlib import Path

    from vaf.core.config import Config
    return Path(Config.APP_DIR) / COMPOSE_ENV_FILENAME


def _retire_repo_env_file(project_root, log=None) -> None:
    """Take REDIS_PASSWORD out of any `<checkout>/.env` an older version wrote.

    Only our own line: the file may carry variables that belong to the user, so
    it is rewritten without ours and removed only if nothing else was in it.
    Bytes, not text - `write_text` would rewrite every newline in a file we do
    not own on Windows.
    """
    from pathlib import Path

    try:
        legacy = Path(project_root) / ".env"
        if not legacy.exists():
            return
        kept = [ln for ln in legacy.read_bytes().splitlines(keepends=True)
                if not ln.startswith(b"REDIS_PASSWORD=")]
        if len(kept) == len(legacy.read_bytes().splitlines(keepends=True)):
            return
        if any(ln.strip() for ln in kept):
            legacy.write_bytes(b"".join(kept))
            _say(log, f"Removed REDIS_PASSWORD from {legacy}; it now lives in "
                      f"{compose_env_file()}")
        else:
            legacy.unlink()
            _say(log, f"Removed {legacy}; the compose password now lives in "
                      f"{compose_env_file()}")
    except Exception as e:
        _say(log, f"Could not retire the repository env file: {e}")


def _write_compose_env_file(project_root, log=None) -> None:
    """Put REDIS_PASSWORD where `docker compose` itself will read it.

    Passing it through this process's environment only covers the runs VAF
    starts. `start_vaf.sh`, `run_vaf.sh` and `install.sh` all call compose
    directly, so Redis would come up with NO password while the client always
    sends one - authentication errors on every cache call. A file both paths
    name explicitly is the one place they agree on. Owner-only where that means
    anything: it holds the password.
    """
    try:
        from vaf.memory.cache import redis_password
        password = redis_password()
        env_path = compose_env_file()
        _retire_repo_env_file(project_root, log)
        if not password:
            # An unreadable keyring answers "" here. Leaving a stale line behind
            # would start Redis WITH a password while the client sends none, and
            # the only trace would be NOAUTH on every cache call.
            if env_path.exists():
                env_path.unlink()
            return
        env_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_secret_write(env_path, f"REDIS_PASSWORD={password}\n".encode("utf-8"))
        _harden(env_path)
    except Exception as e:
        _say(log, f"Could not write the compose env file: {e}")


def _atomic_secret_write(path, payload: bytes) -> None:
    from vaf.core.secure_store import _atomic_write_bytes
    _atomic_write_bytes(path, payload)


def _harden(path) -> None:
    try:
        from vaf.core.secure_store import harden_path
        harden_path(path)
    except Exception:
        pass


def _browser_image_max_age_days() -> int:
    """The browser image's freshness budget in days; 0 disables the age gate.

    Env override first (VAF_BROWSER_IMAGE_MAX_AGE_DAYS), then the admin-only
    config key, then the default - the browser pool's setting order. Lazy and
    forgiving on purpose: this runs at stack start, before much of VAF is up.
    """
    default = 14
    try:
        raw = os.environ.get("VAF_BROWSER_IMAGE_MAX_AGE_DAYS")
        if raw is None or not str(raw).strip():
            from vaf.core.config import Config
            raw = Config.get("browser_image_max_age_days")
        return default if raw is None else max(0, int(raw))
    except Exception:
        return default


def _browser_image_age_days() -> Optional[float]:
    """Age of the browser IMAGE in days, or None when it cannot be known.

    The compose service has no image: key, so the built image's name is
    project-dependent (v2 `<project>-vaf-browser`, legacy `<project>_vaf-browser`);
    it is resolved through the PINNED container name instead - the same route
    browser_pool takes for its template. A missing container answers None: an
    absent container usually means an absent image, and nothing can be stale
    that does not exist. None also on any parse or docker trouble - the gate
    then stands down rather than rebuilding on a guess.
    """
    try:
        docker = resolve_docker_exe()
        r = subprocess.run([docker, "inspect", "vaf-browser", "--format", "{{.Config.Image}}"],
                           capture_output=True, text=True, timeout=20)
        image = (r.stdout or "").strip()
        if r.returncode != 0 or not image:
            return None
        r = subprocess.run([docker, "image", "inspect", image, "--format", "{{.Created}}"],
                           capture_output=True, text=True, timeout=20)
        created_raw = (r.stdout or "").strip()
        if r.returncode != 0 or not created_raw:
            return None
        from datetime import datetime, timezone
        # Docker prints RFC3339 with nanoseconds; fromisoformat wants at most
        # microseconds, so the fractional part is trimmed.
        created_raw = re.sub(r"\.(\d{6})\d*", r".\1", created_raw.replace("Z", "+00:00"))
        created = datetime.fromisoformat(created_raw)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - created).total_seconds() / 86400.0)
    except Exception:
        return None


def _maybe_rebuild_stale_browser_image(base, kwargs, log=None) -> None:
    """The age gate: one cache-less, base-pulling browser rebuild when the
    image is older than the budget. Never raises, never blocks the start; a
    failed fresh build is a security event (`browser_image_stale`) because the
    stack then keeps serving a browser engine without current security fixes.
    """
    try:
        budget = _browser_image_max_age_days()
        if budget <= 0:
            return
        age = _browser_image_age_days()
        if age is None or age <= budget:
            return
        _say(log, f"browser image is {age:.0f} days old (budget {budget}); "
                  "rebuilding with a fresh base")
        # `base` already carries `up -d ...`; the build command is derived by
        # cutting at "up" so both loop variants (docker compose / legacy
        # docker-compose) stay valid.
        build_cmd = base[:base.index("up")] + ["build", "--pull", "--no-cache", "vaf-browser"]
        r = subprocess.run(build_cmd, timeout=900, **kwargs)
        if r.returncode != 0:
            tail = ((r.stderr or "") or (r.stdout or "")).strip().splitlines()
            detail = tail[-1].strip()[:200] if tail else "no output"
            _say(log, f"stale-browser rebuild failed; the old image keeps serving. "
                      f"Last line: {detail}")
            try:
                from vaf.core.security_events import log_security_event
                log_security_event("browser_image_stale",
                                   detail=f"image {age:.0f}d old, rebuild failed: {detail}"[:200],
                                   channel="browser")
            except Exception:
                pass
    except Exception:
        pass


def _warn_about_default_db_password(log=None) -> bool:
    """Say it out loud when the published default database password is still in use.

    Deliberately a warning and not an auto-rotation: rotating a password the app
    is mid-connection with, half-succeeding, locks the user out of their own
    memories. `vaf secure rotate-db` does it as an explicit, verified step.
    """
    try:
        from vaf.core.config import Config
        # BOTH DSNs, not just the app one. Since the RLS cutover a default
        # install has two roles, and the owner (DDL) role is the stronger of
        # the two - a check that only reads memory_db_url goes silent while
        # the superuser is still reachable by the published secret. Live run
        # 2026-08-13: the app role was rotated, the owner role was not, and
        # this warning would have said "all clear".
        dsn = (str(Config.get("memory_db_url", "") or "")
               + " " + str(Config.get("memory_db_owner_url", "") or ""))
    except Exception:
        return False
    if "vaf_dev_secret" not in dsn and "vaf_app_dev_secret" not in dsn:
        return False
    _say(log, "SECURITY: the memory database still uses the shipped default password. "
              "Tell your agent to run `vaf secure rotate-db` to set a random one, "
              "or open a terminal and type `vaf secure rotate-db` yourself.")
    try:
        from vaf.core.security_events import log_security_event
        # The remedy travels INSIDE the event text: this line is read in the
        # Logs window, far away from any documentation, and a warning that
        # names a problem without naming the way out just re-fires on every
        # start until someone goes searching.
        log_security_event(
            "default_db_password",
            detail="The memory database is using the published default password. "
                   "Tell your agent to run `vaf secure rotate-db` to set a random "
                   "one, or open a terminal and type `vaf secure rotate-db` "
                   "yourself.",
        )
    except Exception:
        pass
    return True


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
        _warn_about_default_db_password(log)
        _write_compose_env_file(project_root, log)
        # Two-phase, blocking, exit-checked: bring up the CORE registry-image
        # services first so a failed OPTIONAL build (tts/vaf-browser - e.g. a
        # VM clock skew breaking apt) can never abort the whole 'up' and leave
        # zero containers (incl. the DB). Optional services are best-effort.
        docker = resolve_docker_exe()  # Rancher's docker.exe may be off PATH
        # --env-file is required now: the password file lives beside the other
        # secrets instead of in the project directory, so compose no longer
        # picks it up implicitly.
        env_args = ["--env-file", str(compose_env_file())] if compose_env_file().exists() else []
        for base in (
            [docker, "compose", *env_args, "-f", COMPOSE_FILENAME, "up", "-d", "--quiet-pull"],
            ["docker-compose", *env_args, "-f", COMPOSE_FILENAME, "up", "-d"],
        ):
            try:
                kwargs = {"cwd": str(project_root), "capture_output": True, "text": True,
                          "env": _compose_env()}
                if platform.system() == "Windows" and getattr(subprocess, "CREATE_NO_WINDOW", None) is not None:
                    kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                result = subprocess.run(base + list(CORE_SERVICES), timeout=600, **kwargs)
                if result.returncode == 0:
                    _say(log, "Core service stack (DB/Redis/Sandbox/STT/Gotenberg) started")
                    try:  # optional build services: best-effort, never block the core
                        # --build, deliberately. These two are BUILT from this repo rather
                        # than pulled, and plain `up -d` reuses whatever image already
                        # exists - so a checkout that moves ahead of its images keeps
                        # running the old ones with nothing to show that it does. That is
                        # how an image predating the browser's KasmVNC stream kept serving
                        # CDP while every stream request answered 502, sixteen days after
                        # the code had moved on. An unchanged context is a cache hit and
                        # costs seconds; a changed one is exactly the rebuild that was
                        # missing.
                        #
                        # The cache hit has a second face: the browser's apt layer
                        # (unpinned Debian Chromium) never re-runs while the
                        # Dockerfile text above it is unchanged, so the ENGINE ages
                        # silently no matter how often --build runs. The age gate
                        # forces one cache-less, base-pulling rebuild once the image
                        # is older than browser_image_max_age_days; its failure is
                        # loud (security event) but never blocks the start - the old
                        # browser still beats none.
                        _maybe_rebuild_stale_browser_image(base, kwargs, log)
                        opt = subprocess.run(base + ["--build"] + list(OPTIONAL_SERVICES),
                                             timeout=600, **kwargs)
                        if opt.returncode != 0:
                            # Name the real reason. The old wording guessed "VM clock skew"
                            # at every failure, which sent a genuine build error looking for
                            # a clock problem that was not there.
                            _detail = ((opt.stderr or "") or (opt.stdout or "")).strip().splitlines()
                            _tail = _detail[-1].strip()[:200] if _detail else "no output"
                            _say(log, f"Optional TTS/browser did not build - core stack is up. "
                                      f"Last line: {_tail}")
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
