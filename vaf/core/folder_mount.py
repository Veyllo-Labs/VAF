# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A checked folder handed to a child process, and proven inside the child to be that folder.

Four lanes hand a judged project folder to another process by NAME: git (it turns its work
tree back into a path and changes into it), the coding agent's jailed shell (`bwrap --bind`),
the shell's docker fallback (`docker run -v`) and a sandbox environment's project mount
(`docker run -v`, and `docker start` again after an idle stop). The child, or the daemon
behind it, resolves that name later. A process of the account (a sandbox environment that
mounts a folder above the project, a jailed shell) can rename the folder aside and put a link
to another account's folder in its place in that moment, and the child then works there.
Measured with a racer looping rename-and-link, 1000 `git status` calls each: the handle pin
of `vaf.core.git_runner` alone leaked the other folder's files 5 times, the jailed shell's
`--bind <path>` 3 times; `docker start` mounted (and with `:z` relabelled) whatever the stored
path named at that moment, every time.

The primitive here closes that the same way for all four:
- `HeldFolder` holds the judged folder as a handle (`path_jail.open_folder`) and knows its
  identity (`st_dev:st_ino` of the handle).
- The child is given the folder by name, as before, and the first thing that runs INSIDE it is
  `GUARD`: it changes into the path and compares that folder's `dev:ino` with the held
  identity. Anything else is refused (exit 125, a marker on stderr) before the payload runs.
- `namespace_argv` builds a bubblewrap jail whose root is bwrap's own tmpfs, with the folder
  bound into it. That root is private: a rename or a link made on the host after the mount
  cannot change what the path names inside. Measured deterministically (the payload waited on
  a pipe while the host swapped the folder for a link): with the private root it listed the
  held folder, with a shared root (`--dev-bind / /`) the other one. A shared root also wrote
  the host: bwrap created an empty folder at the racer's path when the real one was aside (3
  in 1000). So the root is never shared.
- `run_guarded` starts it with a pipe whose write end the guard fills with one byte right
  before it hands over to the payload. An empty pipe proves nothing of the payload ran (a
  bwrap setup error, or the guard's refusal), so only then is the start repeated, at most
  three times; a payload that started is never run again, whatever its exit code.

Measured, the same racer, 1000 calls: the namespace with the guard and up to three attempts
answered 992 calls with the own folder, 0 with the other one, 0 failed starts (8 refused at
VAF's own open, before anything started). One start costs about 4 ms more than the handle
pin (5.5 against 1.4 ms per git process).

Why the guard and not bwrap's `--bind-fd`: strace of bwrap 0.13.1 shows it reads the handle's
path as text at argument time, opens that text again when it mounts (following a link) and
compares with the handle only afterwards, so a swap ends in a start error ("Race condition
binding dirfd": 5% of starts under the racer), never in a wrong view. It needs bwrap 0.10
(2024) or newer. The guard gives the same result on every bwrap version and works in a
container too, where there is no handle to bind.

`docker_shares_kernel` (`vaf.core.containers`) decides whether the guard can run in a
container: only a daemon on this kernel shows the host's `dev:ino` numbers inside (measured
equal on a native daemon, `44:25122028` both).

NAMED BOUNDARIES:
- No lane (macOS, Windows, Linux without bubblewrap or where it cannot make a user namespace:
  measured refused inside a default Docker container; Ubuntu's AppArmor restriction on
  unprivileged user namespaces is unmeasured). `namespace_available` decides by a real probe
  start, and the callers keep their by-name path there.
- A docker daemon in a VM (Docker Desktop, Rancher Desktop): the container sees other numbers,
  so the mount stays by name, unchecked.
- `docker run -v` with `:z` relabels the source docker resolved at its start, before the
  check inside can refuse. A folder swapped in during the moment between VAF's open and
  docker's start is relabelled (`container_file_t`), though nothing runs in it. A relabel
  through the held handle is the way to close it, and it needs its own SELinux measurement.
- `-v` creates a missing source as an empty root-owned folder; a folder renamed away in that
  same moment can leave one behind (the start is then refused by the check).

NAMED BOUNDARY, the facade: not on ``vaf/__init__.py``. Measured: the only caller outside
``vaf/core`` is the coder's shell (``vaf/tools/workspace_exec.py``); the public lanes that carry
the guarantee are ``vaf.run_project_git`` and ``vaf.get_environment_manager()``."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import Iterable, List, Optional, Sequence, Tuple

from vaf.core import path_jail

logger = logging.getLogger("vaf.core.folder_mount")

__all__ = ["GUARD", "GUARD_MARKER", "HandoverRefused", "HeldFolder", "SYSTEM_DIRS",
           "guard_argv", "guard_refused", "namespace_argv", "namespace_available",
           "namespace_unavailable_reason", "run_guarded"]

GUARD_MARKER = "vaf-folder-guard:"

# $1 the path inside the child, $2 the expected dev:ino, $3 a started-fd number or "-", then
# the payload. It changes into the path first and asks `.` afterwards, so the folder that was
# compared is the folder the payload starts in. The started byte is written only after the
# comparison, and a failed write refuses: the caller repeats a start only when the pipe stayed
# empty, so a payload that ran must always have left its byte.
GUARD = (
    'case $3 in -|[0-9]|[0-9][0-9]|[0-9][0-9][0-9]|[0-9][0-9][0-9][0-9]) ;; '
    '*) echo "' + GUARD_MARKER + ' bad started fd" >&2; exit 125 ;; esac; '
    'd=$1; want=$2; s=$3; shift 3; '
    'if ! cd -- "$d" 2>/dev/null || [ "$(stat -L -c %d:%i -- . 2>/dev/null)" != "$want" ]; then '
    'echo "' + GUARD_MARKER + ' $d is not the folder that was checked" >&2; exit 125; fi; '
    'if [ "$s" != - ]; then '
    '{ printf x >&"$s"; } 2>/dev/null || { echo "' + GUARD_MARKER + ' $d: cannot report the start" >&2; exit 125; }; '
    'eval "exec $s>&-"; fi; '
    'exec "$@"'
)

# Bound read-only into every namespace, those that exist (usrmerge layouts vary). The one copy:
# the jailed shell and the git lane both take it from here.
SYSTEM_DIRS: Tuple[str, ...] = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/opt")


class HandoverRefused(subprocess.CompletedProcess):
    """`run_guarded`'s answer when no start reached the payload: returncode -1, the reason in
    stderr. A class of its own, so a caller tells it from a payload's own exit code."""


def guard_refused(proc) -> bool:
    """True for `run_guarded`'s refusal."""
    return isinstance(proc, HandoverRefused)


class HeldFolder:
    """A judged folder, held: ``fd`` (the handle), ``real`` (the kernel's path for it) and
    ``identity`` (``"<st_dev>:<st_ino>"`` of the handle).

    ``HeldFolder(path, expect_folder=...)`` opens it with `path_jail.open_folder`, so a folder
    that is not ``expect_folder`` when it is opened raises `path_jail.FolderMoved` ("a link on
    the way"); other ``OSError`` pass through. Without folder handles (Windows) ``fd`` is -1,
    the folder is resolved by path and compared with ``expect_folder`` the same way, and the
    identity comes from ``os.stat``. A context manager; ``close()`` closes an owned handle."""

    def __init__(self, path, *, expect_folder=None) -> None:
        self._owned = True
        self.fd = -1
        if path_jail._DIR_FD:
            self.fd, self.real = path_jail.open_folder(path, expect_folder=expect_folder)
            try:
                st = os.fstat(self.fd)
            except BaseException:
                self.close()
                raise
        else:
            self.real = os.path.realpath(os.fspath(path))
            if expect_folder is not None:
                expected = os.path.normcase(os.path.abspath(os.fspath(expect_folder)))
                if os.path.normcase(self.real) != expected:
                    raise path_jail.FolderMoved(f"its folder is {self.real}, not {expected} "
                                                "(a link on the way)")
            st = os.stat(self.real)
        self.identity = f"{st.st_dev}:{st.st_ino}"

    @classmethod
    def adopt(cls, fd: int, real: str) -> "HeldFolder":
        """A folder handle the caller already opened and judged (and closes itself)."""
        held = cls.__new__(cls)
        held._owned = False
        held.fd, held.real = fd, real
        st = os.fstat(fd)
        held.identity = f"{st.st_dev}:{st.st_ino}"
        return held

    def close(self) -> None:
        if self._owned and self.fd >= 0:
            os.close(self.fd)
        self.fd = -1

    def __enter__(self) -> "HeldFolder":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def guard_argv(inside_path: str, identity: str, argv: Sequence[str],
               started_fd: Optional[int] = None) -> List[str]:
    """``argv`` led by the guard: it runs only in ``inside_path`` when that is ``identity``."""
    return ["/bin/sh", "-c", GUARD, "vaf-folder-guard", inside_path, identity,
            "-" if started_fd is None else str(int(started_fd)), *argv]


def namespace_argv(held: HeldFolder, dest: str, *, extra_ro: Iterable[str] = (),
                   unshare_net: bool = False, clearenv: bool = False,
                   setenv: Iterable[Tuple[str, str]] = (), chdir: Optional[str] = None,
                   scratch_tmp: bool = True, readonly_root: bool = False) -> List[str]:
    """The bwrap prefix (ending in ``--``) of a jail that holds the system folders read-only,
    ``extra_ro`` read-only where they exist, and ``held`` read-write at ``dest``.

    The root is bwrap's own tmpfs, never the host's (`--dev-bind / /`): that is what keeps a
    swap on the host after the mount out of the child's view. The folder is bound after
    ``--tmpfs /tmp``, so a folder under /tmp is not covered by it, and after the system
    folders, so one below /opt is not either. ``--clearenv`` comes before every ``--setenv``
    (bwrap applies them in order).

    ``scratch_tmp=False`` gives no writable /tmp, and ``readonly_root=True`` makes the root
    read-only once everything is mounted: then the held folder is the only place the child
    can write, and a write anywhere else fails (EROFS) instead of landing in the jail's own
    memory and vanishing with it."""
    argv = ["bwrap"]
    if clearenv:
        argv.append("--clearenv")
    argv += ["--die-with-parent", "--unshare-pid"]
    if unshare_net:
        argv.append("--unshare-net")
    argv += ["--proc", "/proc", "--dev", "/dev"]
    if scratch_tmp:
        argv += ["--tmpfs", "/tmp"]
    for d in SYSTEM_DIRS:
        if os.path.isdir(d):
            argv += ["--ro-bind", d, d]
    for p in extra_ro:
        argv += ["--ro-bind-try", p, p]
    argv += ["--bind", held.real, dest]
    if readonly_root:
        argv += ["--remount-ro", "/"]
    if chdir:
        argv += ["--chdir", chdir]
    for key, value in setenv:
        argv += ["--setenv", key, value]
    argv.append("--")
    return argv


def run_guarded(argv_prefix: Sequence[str], held: HeldFolder, inside_path: str,
                payload: Sequence[str], *, attempts: int = 3, runner=None,
                **run_kwargs) -> subprocess.CompletedProcess:
    """Run ``argv_prefix`` + the guard + ``payload``, the guard holding ``held``'s identity
    at ``inside_path``. ``runner`` (default ``subprocess.run``) is called as
    ``runner(argv, pass_fds=..., **run_kwargs)`` and returns a CompletedProcess.

    A start whose pipe stayed empty ran nothing of the payload and is repeated, up to
    ``attempts`` starts; then a `HandoverRefused` (returncode -1, "a link on the way") comes
    back. A start that reached the payload is returned as it is, never repeated. Exceptions of
    the runner (a timeout) pass through; every pipe end is closed on every path."""
    run = runner or subprocess.run
    extra_fds = tuple(run_kwargs.pop("pass_fds", ()) or ())
    last = ""
    argv: List[str] = list(argv_prefix)
    for _ in range(max(1, int(attempts))):
        r, w = os.pipe()
        try:
            argv = [*argv_prefix, *guard_argv(inside_path, held.identity, payload, started_fd=w)]
            proc = run(argv, pass_fds=(*extra_fds, w), **run_kwargs)
            os.close(w)
            w = -1
            os.set_blocking(r, False)
            try:
                started = os.read(r, 1) != b""
            except BlockingIOError:
                started = False
        finally:
            os.close(r)
            if w >= 0:
                os.close(w)
        if started:
            return proc
        err = proc.stderr or ""
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        lines = [ln for ln in err.strip().splitlines() if ln.strip()]
        last = lines[-1][:200] if lines else f"exit {proc.returncode}"
    return HandoverRefused(argv, -1, "",
                           f"{inside_path}: the folder changed while it was being handed over "
                           f"(a link on the way); the last start said: {last}")


_LOCK = threading.Lock()
_PROBE: Optional[Tuple[bool, str]] = None


def _probe() -> Tuple[bool, str]:
    if not sys.platform.startswith("linux"):
        return False, "only on Linux"
    if shutil.which("bwrap") is None:
        return False, "bubblewrap (bwrap) is not installed"
    folder = tempfile.mkdtemp(prefix="vaf-mount-probe-")
    try:
        with HeldFolder(folder) as held:
            proc = run_guarded(namespace_argv(held, held.real), held, held.real, ["true"],
                               capture_output=True, text=True, timeout=20,
                               stdin=subprocess.DEVNULL)
        if proc.returncode == 0:
            return True, ""
        detail = (proc.stderr or "").strip().splitlines()
        return False, ("bubblewrap could not start a namespace: "
                       + (detail[-1][:200] if detail else f"exit {proc.returncode}"))
    except Exception as e:
        return False, f"bubblewrap could not start a namespace: {e}"
    finally:
        try:
            os.rmdir(folder)
        except OSError:
            pass


def _probed() -> Tuple[bool, str]:
    global _PROBE
    with _LOCK:
        if _PROBE is None:
            _PROBE = _probe()
            if not _PROBE[0] and sys.platform.startswith("linux") and shutil.which("bwrap"):
                logger.warning("The private folder mount is not available, git and the coder's "
                               "shell hand folders over by name: %s", _PROBE[1])
        return _PROBE


def namespace_available() -> bool:
    """True where a bwrap namespace with a held folder really starts: Linux, bwrap on PATH and
    one probe start that exited 0. Probed once per process (thread-safe), so installing bwrap
    later needs a restart."""
    return _probed()[0]


def namespace_unavailable_reason() -> str:
    """Why `namespace_available` is False, or "" when it is True."""
    return _probed()[1]


def _reset_probe() -> None:
    """Test seam: forget the probe's answer."""
    global _PROBE
    with _LOCK:
        _PROBE = None
