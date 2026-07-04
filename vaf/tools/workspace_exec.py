# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Run a coder shell command with full access to its project workspace, but with
VAF's own source, config and secrets structurally out of reach.

The coding agent needs real host access (run scripts, npm/pip install, and docker
against the host daemon for a Dockerized project), but must never be able to touch
VAF's core files or itself and break the running system. String-filtering a shell is
not real security, so we confine with the KERNEL instead:

- Linux + bubblewrap: the command runs on the real host inside a bwrap jail. The
  workspace is bind-mounted read-write (edits persist); the system is read-only; the
  VAF repo, ~/.vaf and secrets are NOT mounted, so they simply do not exist for the
  command. FS writes are kernel-confined to the workspace. Host tools, network and
  (opt-in) the docker socket are available.
- Otherwise (no bwrap / macOS-todo / Windows): fall back to a container with ONLY the
  workspace mounted. Same confinement, minus host-docker-from-bash.
- No sandbox available at all: refuse. We never run a raw, unconfined host shell,
  which had no path guard and could overwrite VAF's core.

The one residual privilege is the docker socket (docker can bind-mount host paths):
_docker_mount_escapes rejects a docker command that -v/--mount/--volume-s a host path
outside the workspace.
"""
from __future__ import annotations

import os
import platform
import shlex
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Tuple

# The VAF repo root — the coder's workspace must never be this or inside it, and it is
# never mounted into the jail.
_VAF_PROJECT_ROOT = Path(__file__).resolve().parents[2]
SANDBOX_IMAGE = "python:3.11-slim"
_DOCKER_SOCK_CANDIDATES = ("/var/run/docker.sock", "/run/docker.sock")


def _docker_kwargs() -> dict:
    kw: dict = {}
    if platform.system() == "Windows" and getattr(subprocess, "CREATE_NO_WINDOW", None) is not None:
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kw


def _bwrap_available() -> bool:
    return platform.system() == "Linux" and shutil.which("bwrap") is not None


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, timeout=8, **_docker_kwargs())
        return r.returncode == 0
    except Exception:
        return False


def _docker_sock() -> str | None:
    for s in _DOCKER_SOCK_CANDIDATES:
        if os.path.exists(s):
            return s
    return None


def _invokes_docker(command: str) -> bool:
    # Detect a docker invocation so we can REFUSE it up front. The host docker socket
    # is host-root-equivalent (a container can --privileged/-v/--pid=host its way to the
    # whole host FS, outside this jail's mount namespace) and cannot be safely policed by
    # inspecting the command string, so confined bash does not expose it at all. Host
    # docker for Docker projects needs an isolated daemon - a separate, deliberate design.
    import re
    return bool(re.search(r"(^|[\s;&|(])docker($|[\s;&|)])", command))


# Environment: start empty and re-inject only non-secret basics, so exported API keys
# and tokens in the tray process environment never leak into the jail.
_ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ")


def _assert_safe_workspace(ws: str) -> None:
    p = Path(ws).resolve()
    root = _VAF_PROJECT_ROOT
    if p == root or root.is_relative_to(p) or p.is_relative_to(root):
        raise ValueError(f"refusing to run: workspace {p} overlaps the VAF source tree {root}")
    # Never root the jail at the real HOME or filesystem root: that would bind-mount the
    # user's home (incl. ~/.vaf secrets) or the whole system read-write.
    home = Path.home().resolve()
    if p == home or p == Path("/"):
        raise ValueError(f"refusing to run: workspace {p} is the home/root directory, not a project")


def _wrap_timeout(command: str, timeout: int) -> str:
    # In-namespace kill regardless of command name (same rationale as run_tests).
    return f"timeout -s KILL {int(timeout)} sh -c {shlex.quote(command)}"


def _build_bwrap_argv(workspace: str, command: str, timeout: int) -> list:
    argv = [
        "bwrap",
        "--clearenv",                     # drop the tray env (API keys/tokens) - re-inject basics below
        "--die-with-parent",
        "--unshare-pid",
        "--unshare-net",                  # no host loopback: the memory DB / VAF API / secrets are unreachable
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--bind", workspace, workspace,   # the workspace: read-write, persists to host
        "--chdir", workspace,
        "--setenv", "HOME", workspace,    # so ~/... resolves inside the jail, not the host home
        "--setenv", "PYTHONIOENCODING", "utf-8",
    ]
    for var in _ENV_ALLOWLIST:
        val = os.environ.get(var)
        if val is not None:
            argv += ["--setenv", var, val]
    # System dirs, read-only. Bind only those that exist (usrmerge layouts vary).
    for d in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/opt"):
        if os.path.isdir(d):
            argv += ["--ro-bind", d, d]
    # NOTE: the VAF repo, the real HOME, ~/.vaf and the docker socket are deliberately NOT
    # bound -> invisible/unreachable. Host FS writes are kernel-confined to the workspace.
    argv += ["--", "/bin/sh", "-c", _wrap_timeout(command, timeout)]
    return argv


def run_in_workspace(base_dir: str, command: str, timeout: int = 180) -> Tuple[int, str, str, str]:
    """Execute ``command`` confined to ``base_dir``. Returns (rc, stdout, stderr, mode).

    rc == -2 signals a refusal/precondition failure (message in stderr, mode="refused").
    """
    if not base_dir or not os.path.isdir(base_dir):
        return -2, "", f"Workspace not found: {base_dir!r}", "refused"
    ws = os.path.realpath(base_dir)
    _assert_safe_workspace(ws)

    if _invokes_docker(command):
        return -2, "", (
            "Refused: docker is not available in the confined coder shell. The host docker "
            "socket grants host-root access that this sandbox cannot safely contain. "
            "Host/docker tasks (e.g. inspecting a container) are handled by the main agent "
            "with the user's explicit confirmation, not from the coder's workspace shell."
        ), "refused"

    if _bwrap_available():
        rc, out, err = _run(_build_bwrap_argv(ws, command, timeout), timeout + 20)
        return rc, out, err, "host-jail (bwrap)"

    if _docker_available():
        rc, out, err = _run_in_container(ws, command, timeout)
        return rc, out, err, "container"

    return -2, "", (
        "Refused: no sandbox available (bubblewrap or Docker required). A raw host shell "
        "is not run, to protect VAF's core files. Install bubblewrap or start Docker."
    ), "refused"


def _run_in_container(ws: str, command: str, timeout: int) -> Tuple[int, str, str]:
    """Fallback: fresh container with ONLY the workspace mounted (rw). No host docker."""
    name = f"vaf_ws_{uuid.uuid4().hex[:8]}"
    argv = [
        "docker", "run", "--rm", "--name", name,
        "--user", f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else "0:0",
        "--network", "none",                       # no host loopback from the fallback container either
        "-v", f"{ws}:/workspace:z", "-w", "/workspace",  # :z relabels for SELinux hosts
        SANDBOX_IMAGE, "sh", "-c", _wrap_timeout(command, timeout),
    ]
    try:
        return _run(argv, timeout + 20)
    finally:
        try:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=10, **_docker_kwargs())
        except Exception:
            pass


def _run(argv: list, hard_timeout: int) -> Tuple[int, str, str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=hard_timeout, **_docker_kwargs())
        rc = -1 if r.returncode in (124, 137) else r.returncode
        return rc, r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired:
        return -1, "", "Timed out."
