# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Run a coder project's tests/checks inside the isolated Docker sandbox.

The coding agent writes tests but could not run them: the python_sandbox guard blocks
file I/O (so the sandbox can't be a write-backdoor to project files), and the project
files live on the host, not in the sandbox volume. So the agent shipped "tests pass"
claims it never verified.

This runner copies the project into a throwaway directory INSIDE the persistent
vaf-sandbox container (host -> sandbox only, never back), runs the check command
(default: pytest) there, returns the real result, and always cleans the copy up. It
never writes to the host project, so the guard's security intent is preserved while
the agent finally gets ground-truth test results.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
import uuid
from typing import Optional, Tuple

from vaf.tools.base import BaseTool

SANDBOX_CONTAINER = "vaf-sandbox"

# Directories never copied into the sandbox (heavy / irrelevant to a test run).
_EXCLUDE_DIRS = {
    ".git", "node_modules", "venv", ".venv", "env", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build", ".next",
    ".gradle", "target", ".idea", ".vscode",
}
_MAX_COPY_BYTES = 50 * 1024 * 1024  # refuse to copy a project larger than this (excl. the above)
_DEFAULT_COMMAND = "python3 -m pytest -q"


def _docker_kwargs() -> dict:
    import platform
    kw: dict = {}
    if platform.system() == "Windows" and getattr(subprocess, "CREATE_NO_WINDOW", None) is not None:
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kw


def _sandbox_running() -> bool:
    try:
        r = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", SANDBOX_CONTAINER],
            capture_output=True, text=True, timeout=5, **_docker_kwargs(),
        )
        return r.returncode == 0 and "true" in r.stdout.lower()
    except Exception:
        return False


def _included_size(base_dir: str) -> int:
    """Total bytes of the files that WOULD be copied (excluding _EXCLUDE_DIRS)."""
    total = 0
    for root, dirs, files in os.walk(base_dir):
        dirs[:] = [d for d in dirs if d not in _EXCLUDE_DIRS]
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
            if total > _MAX_COPY_BYTES:
                return total
    return total


# OS package managers that can never work in the fixed, network-less sandbox image.
_PKG_MANAGERS = {"apt", "apt-get", "aptitude", "yum", "dnf", "apk", "pacman", "zypper", "brew"}

_GIT_REDIRECT = (
    "run_tests runs your tests in an ISOLATED sandbox — a COPY of the project with no .git, no "
    "git binary and no network. It is NOT a shell on your real repo, so git commands here always "
    "fail. Nothing was run.\n"
    "For the REAL repo use the dedicated tools instead:\n"
    "  - git_log         : view commit history\n"
    "  - project_history : list restorable versions (id, date, changed files)\n"
    "  - project_rollback: restore the project to an earlier version (safe, undoable)\n"
    "To change a file, use edit_file (surgical) or write_file."
)

_PKG_REDIRECT = (
    "run_tests runs in an ISOLATED sandbox with a FIXED image and NO network, so installing OS "
    "packages ({tool}) cannot work here. Nothing was run. Run your tests with the tooling already "
    "present (e.g. 'python3 -m pytest -q')."
)


def _reject_non_test_command(command: Optional[str]) -> Optional[str]:
    """Redirect commands that misuse run_tests as a host shell (a real doom-loop trigger).

    The test sandbox is a network-less copy of the project with no .git and no git binary, so a
    ``git`` invocation or an OS-package install can never succeed here. Instead of letting the model
    burn loops rediscovering that, return a message pointing at the right tool. Returns ``None`` for
    anything that could be a legitimate test command (pytest, npm/cargo/go/make test, ...).
    """
    if not command:
        return None
    for seg in re.split(r"&&|\|\||;|\n|\|", command):
        toks = seg.strip().split()
        i = 0
        while i < len(toks) and toks[i] == "sudo":
            i += 1
        if i >= len(toks):
            continue
        head = toks[i]
        if head == "cd":
            continue  # navigation; the real verb is in the next segment
        if head == "git":
            return _GIT_REDIRECT
        if head in _PKG_MANAGERS:
            return _PKG_REDIRECT.format(tool=head)
    return None


def run_project_tests(base_dir: str, command: Optional[str] = None, timeout: int = 180) -> str:
    """Run ``command`` (default pytest) against a copy of ``base_dir`` in the sandbox.

    Returns a human/agent-readable result string (PASS/FAIL + captured output tail).
    Never raises for expected conditions (docker down, no project) - returns a message.
    """
    cmd = (command or _DEFAULT_COMMAND).strip() or _DEFAULT_COMMAND

    redirect = _reject_non_test_command(cmd)
    if redirect:
        return redirect

    if not base_dir or not os.path.isdir(base_dir):
        return f"Cannot run tests: project directory not found ({base_dir!r})."
    if not _sandbox_running():
        return (
            "Cannot run tests: the Docker sandbox (vaf-sandbox) is not running, so tests "
            "cannot be executed in isolation. Start the memory stack (Docker) and retry."
        )
    size = _included_size(base_dir)
    if size > _MAX_COPY_BYTES:
        return (
            f"Cannot run tests: project is too large to copy into the sandbox "
            f"({size // (1024*1024)} MB > {_MAX_COPY_BYTES // (1024*1024)} MB after excluding "
            f"{', '.join(sorted(_EXCLUDE_DIRS))}). Narrow the project or run a smaller subset."
        )

    # uuid suffix: the sandbox is shared, and two runs in the same process/second must
    # not collide on the same dir (which cleanup would then delete out from under each other).
    run_dir = f"/workspace/testrun_{int(time.time())}_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    try:
        # 1) Isolated per-run dir in the sandbox.
        mk = subprocess.run(
            ["docker", "exec", SANDBOX_CONTAINER, "mkdir", "-p", run_dir],
            capture_output=True, text=True, timeout=15, **_docker_kwargs(),
        )
        if mk.returncode != 0:
            return f"Cannot run tests: failed to prepare sandbox dir ({(mk.stderr or '').strip()[:200]})."

        # 2) Stream the project in with excludes (host -> sandbox only; tar reads the host).
        tar_excludes = []
        for d in sorted(_EXCLUDE_DIRS):
            tar_excludes += ["--exclude", d]
        tar_cmd = ["tar", "czf", "-", "-C", base_dir, *tar_excludes, "."]
        # --no-same-owner: the container user cannot chown to the host uid/gid, and
        # ownership is irrelevant to a test run - without it tar exits non-zero on the
        # "Cannot change ownership" warning and the copy looks failed.
        untar_cmd = ["docker", "exec", "-i", SANDBOX_CONTAINER, "tar", "xzf", "-", "--no-same-owner", "-C", run_dir]
        # DEVNULL for the host tar's stderr: it is never drained, and a PIPE that fills up
        # would deadlock the copy. Copy success is judged by the untar rc + the host tar rc.
        tar_proc = subprocess.Popen(tar_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **_docker_kwargs())
        untar = subprocess.run(untar_cmd, stdin=tar_proc.stdout, capture_output=True, text=True, timeout=120, **_docker_kwargs())
        if tar_proc.stdout:
            tar_proc.stdout.close()
        tar_rc = tar_proc.wait(timeout=10)
        if untar.returncode != 0:
            return f"Cannot run tests: failed to copy project into the sandbox ({(untar.stderr or '').strip()[:200]})."
        if tar_rc != 0:
            # The host tar failed partway (e.g. a file vanished): the copy is incomplete,
            # so running tests on it would be misleading. Fail loudly instead.
            return f"Cannot run tests: reading the project failed (tar exit {tar_rc}); copy is incomplete."

        # 3) Execute the check command in the copied project, bounded by timeout.
        rc, out, err = _exec_bounded(cmd, run_dir, timeout)
        return _format_result(cmd, rc, out, err)
    finally:
        # 4) Always remove the copy (a shared persistent container must not accumulate runs).
        try:
            subprocess.run(
                ["docker", "exec", SANDBOX_CONTAINER, "rm", "-rf", run_dir],
                capture_output=True, timeout=15, **_docker_kwargs(),
            )
        except Exception:
            pass


def _kill_run(workdir: str) -> None:
    """Kill every process whose cwd is this run's dir (any language), and ONLY this run's.

    The command runs with `docker exec -w <workdir>`, so it and its children inherit that
    cwd - but cwd is not in the argv, so a name-based `pkill -f` would miss non-pytest
    commands and a global `pkill -f pytest` would also kill OTHER concurrent runs. Matching
    by /proc/<pid>/cwd targets exactly this run's process tree in the shared container.
    """
    script = (
        'for p in /proc/[0-9]*; do '
        f'[ "$(readlink "$p/cwd" 2>/dev/null)" = "{workdir}" ] && '
        'kill -9 "${p##*/}" 2>/dev/null; done'
    )
    try:
        subprocess.run(
            ["docker", "exec", SANDBOX_CONTAINER, "sh", "-c", script],
            capture_output=True, timeout=10, **_docker_kwargs(),
        )
    except Exception:
        pass


def _exec_bounded(command: str, workdir: str, timeout: int) -> Tuple[int, str, str]:
    # Wrap with the in-container `timeout` so the kill fires INSIDE the container PID
    # namespace, regardless of the command name. Killing the host-side `docker exec`
    # client does NOT stop the process in the container, so a bare subprocess timeout
    # would leak model-supplied non-pytest commands (npm test, a hung build, sleep) in
    # the shared sandbox. The host timeout is a slightly larger backstop; on either path
    # a cwd-scoped kill reaps this run's tree (and no other run's).
    wrapped = f"timeout -s KILL {int(timeout)} sh -c {_shquote(command)}"
    exec_cmd = ["docker", "exec", "-w", workdir, SANDBOX_CONTAINER, "sh", "-c", wrapped]
    try:
        r = subprocess.run(exec_cmd, capture_output=True, text=True, timeout=timeout + 15, **_docker_kwargs())
        # 124 = timeout expired (SIGTERM), 137 = 128+9 SIGKILL from `timeout -s KILL`.
        if r.returncode in (124, 137):
            _kill_run(workdir)
            return -1, r.stdout or "", (r.stderr or "") + f"\nTimed out after {timeout}s."
        return r.returncode, r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired:
        _kill_run(workdir)
        return -1, "", f"Timed out after {timeout}s."


def _shquote(s: str) -> str:
    import shlex
    return shlex.quote(s)


def _format_result(command: str, rc: int, out: str, err: str) -> str:
    combined = (out + ("\n" + err if err.strip() else "")).strip()
    # Keep the tail: pytest's summary (pass/fail counts, failing assertions) is at the end.
    if len(combined) > 4000:
        combined = "...(truncated)...\n" + combined[-4000:]
    if rc == 0:
        head = "TESTS PASSED"
    elif rc == -1:
        head = "TEST RUN TIMED OUT"
    else:
        head = f"TESTS FAILED (exit {rc})"
    return f"{head}\n$ {command}\n\n{combined or '(no output)'}"


class RunTestsTool(BaseTool):
    """Coder tool: run the project's tests in the isolated sandbox and return the real result."""

    name = "run_tests"
    permission_level = "read"      # reads the host project; executes only inside the sandbox
    side_effect_class = "none"     # never modifies the host project
    description = (
        "Run the project's tests inside the isolated Docker sandbox and return the REAL "
        "pass/fail result. Use this to VERIFY your code after writing tests - do not claim "
        "tests pass without running them. Default command: pytest."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command to run in the project dir (default: 'python3 -m pytest -q').",
            }
        },
        "required": [],
    }

    def __init__(self, base_dir: str):
        self.base_dir = base_dir

    def run(self, **kwargs) -> str:
        return run_project_tests(self.base_dir, kwargs.get("command"), timeout=int(kwargs.get("timeout", 180) or 180))
