# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Run a coder shell command with full access to its project workspace, but with
VAF's own source, config and secrets structurally out of reach.

The coding agent needs real host access (run scripts, npm/pip install), but must never be
able to touch VAF's core files or itself and break the running system. String-filtering a
shell is not real security, so we confine with the KERNEL instead:

- Linux + bubblewrap: the command runs on the real host inside a bwrap jail. The
  workspace is bind-mounted read-write (edits persist); the system is read-only; the
  VAF repo, the home directory, ~/.vaf, secrets and the docker socket are NOT mounted,
  so they simply do not exist for the command. FS writes are kernel-confined to the
  workspace. Host tools are available; the network is not (see below). The jail is built
  by `vaf.core.folder_mount`: the judged workspace is held as a handle, bound by name on the
  jail's own root, and checked inside the jail, before the command, to be the folder that
  was judged (a folder swapped for a link in that moment is refused, rc -2). Measured under a
  racer looping rename-and-link: the plain `--bind <path>` showed the other folder in 3 of
  1000 starts, with the check in 0 of 300 shell starts (and 0 of 1000 git starts). Whether the jail can start at all is that module's
  one probe (`namespace_available`), so a host whose bwrap cannot make a namespace takes the
  container below instead of failing every command.
- Otherwise (no working bwrap / macOS / Windows): fall back to a container with ONLY the
  workspace mounted and no network. Same confinement. On a docker daemon that shares this
  kernel the same check runs in the container before the command; NAMED BOUNDARY: a daemon in
  a VM (Docker Desktop) shows other folder numbers, so there the mount stays by name. A
  workspace inside a folder the jail binds read-only (/opt) takes this lane too: below such
  a bind its view would not be private.
- NAMED BOUNDARY, macOS: the workspace is held with its resolved path as the expected real
  path, and the kernel path of the handle keeps the case on disk, so a folder spelled in
  another case on a case-insensitive volume is refused, never wrongly accepted (the same limit
  the project git runner has there; unmeasured on a Mac).
- No sandbox available at all: refuse. We never run a raw, unconfined host shell,
  which had no path guard and could overwrite VAF's core.

Docker is refused up front (`_invokes_docker`), in both lanes: the host docker socket is
host-root-equivalent and cannot be policed by reading the command, so the jail does not
expose it at all.

NAMED BOUNDARY - no network, not even for a build's dependency repository. A Maven, Gradle or
npm build that must download dependencies fails in here, and that is deliberate: the jail's
`--unshare-net` is what keeps the host-loopback services (the memory DB, the VAF API) out of
reach, and bubblewrap has only all or nothing. "Only Maven Central and the Paper repository"
would need a user-space network stack (pasta or slirp4netns) in front of a filtering proxy,
which is infrastructure of its own. Not needed: such a build runs through host_bash, which the
coder uses without asking where the account has it (granted by name, outside this jail), or in a
project sandbox environment, whose `registries` network profile is exactly that filter, one
layer out - the way for a regular account, which has no host_bash unless the admin names it.
"""
from __future__ import annotations

import os
import platform
import shlex
import shutil
import subprocess
import uuid
from typing import Tuple

from vaf.core import folder_mount

# The workspace guard (VAF's code, VAF's state, the home directory's configuration, HOME and
# / are refused) is the core one, shared with the sandbox environments' project mount.
from vaf.core.workspace_guard import assert_safe_workspace as _assert_safe_workspace  # noqa: E402
SANDBOX_IMAGE = "python:3.11-slim"
_DOCKER_SOCK_CANDIDATES = ("/var/run/docker.sock", "/run/docker.sock")


def _docker_kwargs() -> dict:
    kw: dict = {}
    if platform.system() == "Windows" and getattr(subprocess, "CREATE_NO_WINDOW", None) is not None:
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kw


def _bwrap_available() -> bool:
    return folder_mount.namespace_available()


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


def _wrap_timeout(command: str, timeout: int) -> str:
    # In-namespace kill regardless of command name (same rationale as run_tests).
    return f"timeout -s KILL {int(timeout)} sh -c {shlex.quote(command)}"


def _build_bwrap_argv(held: "folder_mount.HeldFolder", workspace: str) -> list:
    """The jail around the command: `folder_mount.namespace_argv` (system folders read-only,
    the held workspace read-write at its own path, the jail's own root) plus what the shell
    adds. --clearenv drops the tray's environment (API keys, tokens) and only the basics are
    set again; --unshare-net keeps the host loopback (the memory DB, the VAF API) out of
    reach. The VAF repo, the real HOME, ~/.vaf and the docker socket are deliberately NOT
    bound: they do not exist for the command."""
    setenv = [("HOME", workspace),               # ~/... resolves inside the jail
              ("PYTHONIOENCODING", "utf-8")]
    for var in _ENV_ALLOWLIST:
        val = os.environ.get(var)
        if val is not None:
            setenv.append((var, val))
    return folder_mount.namespace_argv(held, workspace, unshare_net=True, clearenv=True,
                                       chdir=workspace, setenv=setenv)


def run_in_workspace(base_dir: str, command: str, timeout: int = 180, *, user_scope_id,
                     user_role, scratch: bool = False) -> Tuple[int, str, str, str]:
    """Execute ``command`` confined to ``base_dir``. Returns (rc, stdout, stderr, mode).

    rc == -2 signals a refusal/precondition failure (message in stderr, mode="refused").

    ``user_scope_id`` and ``user_role`` say whose run this is, and have no default: a caller
    that forgets them gets a TypeError instead of a shell. The folder is asked
    `vaf.core.workspace_guard.project_dir_refusal` for that identity before anything runs,
    so the jail is never rooted in another account's tree, whatever handed the folder in.
    Both come from trusted code (the coder's caller, the workflow engine's identity), never
    from a model's argument; None for both is a caller with no account (the machine owner's
    freedoms). ``scratch=True`` is a run's own throwaway folder (the coder's content-only
    mode, ``tempfile.mkdtemp``): it lies outside every account's project tree, so the
    account half is skipped for it, but only when the folder lies inside the system temp
    folder.
    """
    from vaf.core.path_jail import is_remote_path
    if base_dir and is_remote_path(str(base_dir)):
        # Before the existence probe: on Windows it already contacts the share.
        from vaf.core.workspace_guard import REMOTE_PROJECT_DIR
        return -2, "", f"Refused: cannot run in {base_dir}: {REMOTE_PROJECT_DIR}.", "refused"
    if not base_dir or not os.path.isdir(base_dir):
        return -2, "", f"Workspace not found: {base_dir!r}", "refused"
    ws = os.path.realpath(base_dir)
    _assert_safe_workspace(ws)
    from vaf.core.workspace_guard import is_inside_temp, project_dir_refusal
    if not (scratch and is_inside_temp(ws)):
        reason = project_dir_refusal(ws, user_scope_id=user_scope_id, user_role=user_role)
        if reason:
            return -2, "", f"Refused: cannot run in {ws}: {reason}.", "refused"

    if _invokes_docker(command):
        return -2, "", (
            "Refused: docker is not available in the confined coder shell. The host docker "
            "socket grants host-root access that this sandbox cannot safely contain. "
            "Host/docker tasks (e.g. inspecting a container) are handled by the main agent "
            "with the user's explicit confirmation, not from the coder's workspace shell."
        ), "refused"

    # The folder that was judged, held until the command ends: the jail and the container
    # check inside, before the command, that the name still shows this very folder.
    from vaf.core.path_jail import PathEscape
    try:
        held = folder_mount.HeldFolder(ws, expect_folder=ws)
    except (PathEscape, OSError):
        return -2, "", (f"Refused: cannot run in {ws}: the folder was moved or replaced by a "
                        "link after it was checked. Check the folder and run the command "
                        "again."), "refused"
    with held:
        # A workspace inside a folder the jail binds read-only (/opt, /usr) would share that
        # bind's view of the host, so its view would not be private: the container instead.
        if _bwrap_available() and not folder_mount.overlaps_ro(ws):
            def _start(argv, **kwargs):
                rc, out, err = _run(argv, timeout + 20, **kwargs)
                return subprocess.CompletedProcess(argv, rc, out, err)

            proc = folder_mount.run_guarded(_build_bwrap_argv(held, ws), held, ws,
                                            ["/bin/sh", "-c", _wrap_timeout(command, timeout)],
                                            runner=_start)
            if folder_mount.guard_refused(proc):
                # The cause of the last start and what to do, not only "the folder changed":
                # a jail that cannot start (a process limit) reads differently from a swap.
                return -2, "", f"Refused: {proc.stderr}.", "refused"
            return proc.returncode, proc.stdout, proc.stderr, "host-jail (bwrap)"

        if _docker_available():
            from vaf.core.containers import docker_shares_kernel
            identity = held.identity if docker_shares_kernel() else None
            rc, out, err = _run_in_container(ws, command, timeout, identity=identity)
            if identity is not None and rc == 125 and folder_mount.GUARD_MARKER in err:
                return -2, "", (f"Refused: cannot run in {ws}: the container did not get the "
                                "folder that was checked (it was moved or replaced by a link "
                                "while the container started). Check the folder and run the "
                                "command again."), "refused"
            return rc, out, err, "container"

    if folder_mount.bwrap_installed():
        why = folder_mount.namespace_unavailable_reason() or (
            f"{ws} lies inside a folder the jail binds read-only")
        return -2, "", (
            f"Refused: no sandbox available: {why}. A raw host shell is not run, to protect "
            "VAF's core files. Start Docker, or allow unprivileged user namespaces for "
            "bubblewrap on this host."), "refused"
    return -2, "", (
        "Refused: no sandbox available (bubblewrap or Docker required). A raw host shell "
        "is not run, to protect VAF's core files. Install bubblewrap or start Docker."
    ), "refused"


def _run_in_container(ws: str, command: str, timeout: int, *,
                      identity: str | None = None) -> Tuple[int, str, str]:
    """Fallback: fresh container with ONLY the workspace mounted (rw). No host docker.
    With ``identity`` (a daemon on this kernel) the command is led by
    `folder_mount`'s guard: /workspace must be that folder, or exit 125 and nothing ran.
    No retry: no pipe crosses into a container, so a refusal cannot be told from a start."""
    name = f"vaf_ws_{uuid.uuid4().hex[:8]}"
    payload = ["sh", "-c", _wrap_timeout(command, timeout)]
    if identity is not None:
        payload = folder_mount.guard_argv("/workspace", identity, payload)
    argv = [
        "docker", "run", "--rm", "--name", name,
        "--user", f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else "0:0",
        "--network", "none",                       # no host loopback from the fallback container either
        "-v", f"{ws}:/workspace:z", "-w", "/workspace",  # :z relabels for SELinux hosts
        SANDBOX_IMAGE, *payload,
    ]
    try:
        return _run(argv, timeout + 20)
    finally:
        try:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=10, **_docker_kwargs())
        except Exception:
            pass


def _run(argv: list, hard_timeout: int, **kwargs) -> Tuple[int, str, str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=hard_timeout,
                           **_docker_kwargs(), **kwargs)
        rc = -1 if r.returncode in (124, 137) else r.returncode
        return rc, r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired:
        return -1, "", "Timed out."
