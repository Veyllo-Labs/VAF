# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
import os
import re
import subprocess
import uuid
import logging
import time
from typing import Optional, Tuple, List

logger = logging.getLogger("vaf.sandbox")

# Own bridge network for ephemeral fallback containers. Deliberately NOT the
# compose-managed "vaf-sandbox-network": docker compose refuses to adopt a
# same-name network it did not create (label mismatch), so sharing the name
# would break a later `docker compose up`. Same isolation semantics: no route
# to other VAF service networks, outbound internet stays available (pip).
EPHEMERAL_NETWORK = "vaf-sandbox-ephemeral"


def ephemeral_hardening_flags(network: Optional[str]) -> List[str]:
    """Docker-run flags mirroring the persistent vaf-sandbox hardening
    (cap_drop ALL + no-new-privileges + isolated bridge + host-gateway alias
    for the Tool Bridge). Pure so tests can assert the exact flag set.
    `network=None` builds the degraded set (caps only) for the retry path."""
    flags = [
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
    ]
    if network:
        flags += [
            "--network", network,
            "--add-host", "host.docker.internal:host-gateway",
        ]
    return flags


# Per-run workspace paths double as kill markers: unique per execution and
# present in every command of that run (cd/pip --target/rm -rf).
_RUN_MARKER_RE = re.compile(r"/tmp/vaf_[A-Za-z0-9_]+")


def extract_run_marker(command: str) -> Optional[str]:
    """The per-run workspace path embedded in a sandbox command, or None."""
    m = _RUN_MARKER_RE.search(command or "")
    return m.group(0) if m else None


def kill_run_processes_cmd(marker: str) -> str:
    """Pure-sh in-container killer for ONE run's process tree.

    Kills every process whose cwd is the run's workspace or whose cmdline
    carries the workspace path. Needs only procfs and shell builtins: slim
    images ship no procps, so the previous `pkill -9 -f python` silently
    no-opped (a timed-out pip kept running and re-populated the workspace
    AFTER cleanup) - and had it existed, it would have killed every OTHER
    user's run in the shared container too. The scan shell excludes itself
    (its own cmdline contains the marker)."""
    return (
        'for d in /proc/[0-9]*; do p="${d##*/}"; '
        '[ "$p" = "$$" ] && continue; '
        f'if [ "$(readlink "$d/cwd" 2>/dev/null)" = "{marker}" ] '
        f'|| grep -qa -- "{marker}" "$d/cmdline" 2>/dev/null; '
        'then kill -9 "$p" 2>/dev/null; fi; done'
    )


class DockerSandbox:
    """
    Provides an isolated execution environment using Docker.
    Mirrors the 'Safety First' approach of Clawdbot.
    """
    def __init__(self, image: str = "python:3.11-slim", name_prefix: str = "vaf_sandbox"):
        self.image = image
        # Unique ID to allow multiple agents/sandboxes at once
        self.container_name = f"{name_prefix}_{uuid.uuid4().hex[:8]}"
        self.is_running = False

    def _is_daemon_running(self) -> bool:
        """Checks if the Docker daemon is responding."""
        try:
            import platform
            kwargs = {"check": True, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
            if platform.system() == "Windows":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            subprocess.run(["docker", "info"], **kwargs)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    def _attempt_daemon_start(self):
        """Attempts to start the Docker daemon based on the OS."""
        import platform
        sys_os = platform.system()
        
        try:
            if sys_os == "Darwin": # macOS
                logger.info("Attempting to start Docker Desktop via 'open'...")
                subprocess.run(["open", "-a", "Docker"], check=True)
            elif sys_os == "Windows":
                logger.info("Attempting to start Docker Desktop via Registry/Path...")
                # Try common installation path
                docker_path = r"C:\Program Files\Docker\Docker\Docker Desktop.exe"
                if os.path.exists(docker_path):
                    subprocess.Popen([docker_path], start_new_session=True, creationflags=subprocess.CREATE_NO_WINDOW)
                else:
                    # Fallback: Just try the command if it's in PATH
                    subprocess.Popen(["Docker Desktop.exe"], shell=True, start_new_session=True, creationflags=subprocess.CREATE_NO_WINDOW)
            elif sys_os == "Linux":
                # On Linux, we rely on socket activation, but can try a kickstart
                logger.info("Attempting to trigger docker.socket...")
                subprocess.run(["docker", "ps"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            logger.warning(f"Failed to auto-trigger Docker start: {e}")

    def start(self):
        """Starts the persistent container with auto-start logic."""
        if self.is_running:
            return

        # 1. Check if running, if not, try to start
        if not self._is_daemon_running():
            self._attempt_daemon_start()
            
            # 2. Polling loop (Wait up to 30 seconds)
            logger.info("Waiting for Docker daemon to come online (max 30s)...")
            start_wait = time.time()
            while time.time() - start_wait < 30:
                if self._is_daemon_running():
                    logger.info("Docker is now online!")
                    break
                time.sleep(2)
            else:
                logger.error("Docker daemon failed to start in time.")
                raise RuntimeError("Docker is not running and could not be started.")

        logger.info(f"Starting sandbox container: {self.container_name} ({self.image})")
        
        import platform
        _win_flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if platform.system() == "Windows" else {}

        try:
            # Check if image exists locally
            subprocess.run(["docker", "image", "inspect", self.image],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **_win_flags)
        except subprocess.CalledProcessError:
            logger.info(f"Image {self.image} not found locally, pulling...")
            subprocess.run(["docker", "pull", self.image], check=True, **_win_flags)

        # Run container with resource limits, auto-remove, and the same
        # hardening the persistent compose sandbox got in its network-isolation
        # hardening pass - this fallback path was skipped back then, so
        # ephemeral containers landed on the DEFAULT bridge with full caps
        # (concurrent sandboxes of different users could reach each other).
        network = self._ensure_isolated_network(_win_flags)
        base_cmd = [
            "docker", "run", "-d", "--rm",
            "--name", self.container_name,
            "--memory", "512m",  # Limit memory
            "--cpus", "0.5",     # Limit CPU
        ]
        tail = [self.image, "sleep", "infinity"]

        try:
            subprocess.run(base_cmd + ephemeral_hardening_flags(network) + tail,
                           check=True, stdout=subprocess.DEVNULL, **_win_flags)
            self.is_running = True
            time.sleep(1)
        except subprocess.CalledProcessError:
            # Degraded retry: keep the capability hardening (the bigger win),
            # drop only the network flags. Loud on purpose - if this fires the
            # cross-container isolation is NOT in effect.
            logger.warning(
                "Sandbox start with isolated network failed; retrying WITHOUT "
                "network isolation (default bridge). Check the %s network.",
                network or EPHEMERAL_NETWORK,
            )
            try:
                subprocess.run(base_cmd + ephemeral_hardening_flags(None) + tail,
                               check=True, stdout=subprocess.DEVNULL, **_win_flags)
                self.is_running = True
                time.sleep(1)
            except subprocess.CalledProcessError as e:
                logger.error(f"Failed to start sandbox container: {e}")
                raise RuntimeError(f"Could not start container: {e}")

    def _ensure_isolated_network(self, _win_flags: dict) -> Optional[str]:
        """Return the ephemeral isolation network, creating it if missing.
        None (-> degraded caps-only flags) if docker cannot provide it."""
        kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, **_win_flags}
        try:
            if subprocess.run(["docker", "network", "inspect", EPHEMERAL_NETWORK],
                              **kwargs).returncode == 0:
                return EPHEMERAL_NETWORK
            # Racing creators are fine: the loser's create fails, the re-inspect wins.
            subprocess.run(["docker", "network", "create", EPHEMERAL_NETWORK], **kwargs)
            if subprocess.run(["docker", "network", "inspect", EPHEMERAL_NETWORK],
                              **kwargs).returncode == 0:
                return EPHEMERAL_NETWORK
        except Exception as e:
            logger.warning(f"Could not ensure sandbox network: {e}")
        return None

    def stop(self):
        """Kills and removes the container."""
        if self.is_running:
            import platform
            logger.info(f"Stopping sandbox: {self.container_name}")
            # -f forces removal even if running
            kwargs = {"check": False, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
            if platform.system() == "Windows":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            subprocess.run(["docker", "rm", "-f", self.container_name], **kwargs)
            self.is_running = False

    def execute(self, command: str, timeout: int = 30, workdir: str = "/") -> Tuple[int, str, str]:
        """
        Executes a shell command inside the container.
        Returns: (exit_code, stdout, stderr)
        """
        if not self.is_running:
            self.start()

        logger.debug(f"Executing in sandbox: {command}")

        # Construct the docker exec command
        # We use 'sh -c' to handle pipes, redirects, and multiple commands
        exec_cmd = [
            "docker", "exec", 
            "-w", workdir,
            self.container_name, 
            "sh", "-c", command
        ]
        
        try:
            import platform
            kwargs = {"capture_output": True, "text": True, "timeout": timeout}
            if platform.system() == "Windows":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            result = subprocess.run(exec_cmd, **kwargs)
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            logger.warning(f"Command timed out after {timeout}s: {command}")
            # Kill THIS run's process tree inside the container (scoped by the
            # per-run workspace marker) - otherwise the payload keeps running
            # after the host-side exec client is gone.
            marker = extract_run_marker(command)
            if marker:
                kill_kwargs = {"capture_output": True, "timeout": 10}
                if platform.system() == "Windows":
                    kill_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                try:
                    subprocess.run(
                        ["docker", "exec", self.container_name, "sh", "-c",
                         kill_run_processes_cmd(marker)],
                        **kill_kwargs)
                except Exception:
                    pass
            return -1, "", "Error: Execution timed out."
        except Exception as e:
            return -1, "", str(e)

    def write_file(self, path: str, content: str):
        """Writes content to a file inside the sandbox."""
        if not self.is_running:
            self.start()
            
        # Basic implementation: echo content to file
        # Note: This has limits on content size/escaping. 
        # For large files, 'docker cp' via temp file is better.
        
        # Using a safer approach with basic base64 to avoid escaping hell
        import base64
        b64_content = base64.b64encode(content.encode('utf-8')).decode('utf-8')
        
        cmd = f"echo {b64_content} | base64 -d > {path}"
        code, _, err = self.execute(cmd)
        if code != 0:
            raise RuntimeError(f"Failed to write file {path}: {err}")

    def read_file(self, path: str) -> str:
        """Reads a file from the sandbox."""
        if not self.is_running:
            self.start()
            
        code, out, err = self.execute(f"cat {path}")
        if code != 0:
            raise FileNotFoundError(f"Could not read {path}: {err}")
        return out

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
