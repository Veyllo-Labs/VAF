# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Run a coder project's tests/checks inside the caller's own sandbox environment.

The coding agent writes tests but could not run them: the python_sandbox guard blocks
file I/O (so the sandbox can't be a write-backdoor to project files), and the project
files live on the host. So the agent shipped "tests pass" claims it never verified.

This runner copies the project into a throwaway directory INSIDE the caller's scratch
environment (vaf/core/environments.py: a container of their own, never shared with
another person), runs the check command (default: pytest) there, returns the real result,
and always removes the copy. The copy goes host -> container only, so the host project is
never written.
"""
from __future__ import annotations

import io
import os
import re
import tarfile
import uuid
from typing import Optional

from vaf.core.tool_dispatch import current_session_stop_check
from vaf.tools.base import BaseTool

# Directories never copied into the sandbox (heavy / irrelevant to a test run).
_EXCLUDE_DIRS = {
    ".git", "node_modules", "venv", ".venv", "env", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build", ".next",
    ".gradle", "target", ".idea", ".vscode",
}
_MAX_COPY_BYTES = 50 * 1024 * 1024  # refuse to copy a project larger than this (excl. the above)
_DEFAULT_COMMAND = "python3 -m pytest -q"


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


# OS package managers: the sandbox runs as an unprivileged user, so they can never work.
_PKG_MANAGERS = {"apt", "apt-get", "aptitude", "yum", "dnf", "apk", "pacman", "zypper", "brew"}

_GIT_REDIRECT = (
    "run_tests runs your tests in an ISOLATED sandbox - a COPY of the project with no .git. "
    "It is NOT a shell on your real repo, so git commands here always fail. Nothing was run.\n"
    "For the REAL repo use the dedicated tools instead:\n"
    "  - git_log         : view commit history\n"
    "  - project_history : list restorable versions (id, date, changed files)\n"
    "  - project_rollback: restore the project to an earlier version (safe, undoable)\n"
    "To change a file, use edit_file (surgical) or write_file."
)

_PKG_REDIRECT = (
    "run_tests runs in an ISOLATED sandbox as an unprivileged user, so installing OS packages "
    "({tool}) cannot work here. Nothing was run. Python and Node packages can be installed in "
    "the command itself (pip install --user ..., npm install), or run your tests with the "
    "tooling already present (e.g. 'python3 -m pytest -q')."
)


def _reject_non_test_command(command: Optional[str]) -> Optional[str]:
    """Redirect commands that misuse run_tests as a host shell (a real doom-loop trigger).

    The test sandbox is a copy of the project with no .git, run as an unprivileged user, so a
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


def _project_tarball(base_dir: str) -> bytes:
    """The project as a gzip tar, without _EXCLUDE_DIRS. Built in-process: no host `tar`
    binary to depend on, and no stderr pipe that could fill up and stall the copy."""
    buf = io.BytesIO()

    def _keep(info: tarfile.TarInfo):
        parts = info.name.split("/")
        if any(part in _EXCLUDE_DIRS for part in parts):
            return None
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        return info

    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for entry in sorted(os.listdir(base_dir)):
            if entry in _EXCLUDE_DIRS:
                continue
            tar.add(os.path.join(base_dir, entry), arcname=entry, filter=_keep)
    return buf.getvalue()


def _ensure_pytest(mgr, env) -> Optional[str]:
    """pytest is in the environment image; the fallback image the scratch environment runs
    on while that image is being built has none. Installed on demand there (into the
    unprivileged user's site), which lasts for that container's life. Returns an error
    string when pytest cannot be had, None when it is usable."""
    probe = mgr.exec_in(env, ["python3", "-m", "pytest", "--version"], timeout=30)
    if probe.returncode == 0:
        return None
    inst = mgr.exec_in(env, ["python3", "-m", "pip", "install", "--quiet", "--user",
                             "--disable-pip-version-check", "pytest"], timeout=180)
    if inst.returncode != 0:
        return ("Cannot run tests: the sandbox has no pytest and installing it failed "
                f"({(inst.stderr or inst.stdout or '').strip()[:200]}).")
    return None


def run_project_tests(base_dir: str, command: Optional[str] = None, timeout: int = 180,
                      user_scope_id=None) -> str:
    """Run ``command`` (default pytest) against a copy of ``base_dir`` in the caller's
    scratch environment.

    Returns a human/agent-readable result string (PASS/FAIL + captured output tail).
    Never raises for expected conditions (docker down, no project) - returns a message
    starting with "Cannot run tests:", which the coder's failure accounting counts.
    """
    cmd = (command or _DEFAULT_COMMAND).strip() or _DEFAULT_COMMAND

    redirect = _reject_non_test_command(cmd)
    if redirect:
        return redirect

    if not base_dir or not os.path.isdir(base_dir):
        return f"Cannot run tests: project directory not found ({base_dir!r})."
    size = _included_size(base_dir)
    if size > _MAX_COPY_BYTES:
        return (
            f"Cannot run tests: project is too large to copy into the sandbox "
            f"({size // (1024*1024)} MB > {_MAX_COPY_BYTES // (1024*1024)} MB after excluding "
            f"{', '.join(sorted(_EXCLUDE_DIRS))}). Narrow the project or run a smaller subset."
        )
    # The environment last: every check above is local and free, and this one starts a
    # container. Ordering it earlier made a too-large project report a sandbox problem.
    from vaf.core import containers
    from vaf.core.environments import EnvironmentRefused, get_environment_manager
    try:
        mgr = get_environment_manager()
        env = mgr.scratch_for(user_scope_id)
    except EnvironmentRefused as exc:
        return f"Cannot run tests: {exc}."
    except Exception as exc:
        return f"Cannot run tests: the sandbox could not be started ({exc})."

    run_dir = f"/tmp/vaf_tests_{uuid.uuid4().hex[:12]}"
    try:
        mk = mgr.exec_in(env, ["mkdir", "-p", run_dir], timeout=30)
        if mk.returncode != 0:
            return f"Cannot run tests: failed to prepare sandbox dir ({(mk.stderr or '').strip()[:200]})."
        try:
            payload = _project_tarball(base_dir)
        except OSError as exc:
            return f"Cannot run tests: reading the project failed ({exc}); nothing was copied."
        # --no-same-owner: the container user cannot chown, and ownership is irrelevant
        # to a test run.
        untar = containers.docker(["exec", "-i", env.container, "tar", "xzf", "-",
                                   "--no-same-owner", "-C", run_dir],
                                  timeout=120, input=payload, binary=True)
        if untar.returncode != 0:
            detail = (untar.stderr or b"").decode("utf-8", errors="replace").strip()[:200]
            return f"Cannot run tests: failed to copy project into the sandbox ({detail})."
        if "pytest" in cmd:
            missing = _ensure_pytest(mgr, env)
            if missing:
                return missing
        result = mgr.exec_in(env, ["sh", "-c", cmd], timeout=timeout, cwd=run_dir,
                             check_stop=current_session_stop_check())
        return _run_result(cmd, result, timeout)
    finally:
        # Always remove the copy; the scratch environment outlives this run.
        try:
            mgr.exec_in(env, ["rm", "-rf", run_dir], timeout=30)
        except Exception:
            pass


def run_tests_in_environment(env, command: Optional[str] = None, timeout: int = 180) -> str:
    """Run ``command`` in the project itself: the coder is bound to a project environment
    whose /workspace IS the project, so there is nothing to copy, the environment's
    installed packages are there, and a test that writes files writes them into the real
    project. Same refusals and result format as run_project_tests."""
    cmd = (command or _DEFAULT_COMMAND).strip() or _DEFAULT_COMMAND
    redirect = _reject_non_test_command(cmd)
    if redirect:
        return redirect
    from vaf.core.environments import EnvironmentRefused, get_environment_manager
    try:
        mgr = get_environment_manager()
        if "pytest" in cmd:
            missing = _ensure_pytest(mgr, env)
            if missing:
                return missing
        result = mgr.exec_in(env, ["sh", "-c", cmd], timeout=timeout, cwd="/workspace",
                             check_stop=current_session_stop_check())
    except EnvironmentRefused as exc:
        return f"Cannot run tests: {exc}."
    except Exception as exc:
        return f"Cannot run tests: the environment could not be reached ({exc})."
    return _run_result(cmd, result, timeout)


def _run_result(cmd: str, result, timeout: int) -> str:
    """The report of a finished, timed-out or stopped run. A stop is said as a stop: it
    used to read "Timed out", which sent the coder looking for a slow test."""
    if result.cancelled:
        return _format_result(cmd, -1, result.stdout, (result.stderr or "")
                              + "\nStopped: the user asked to stop.", header="TEST RUN STOPPED")
    if result.timed_out:
        return _format_result(cmd, -1, result.stdout, (result.stderr or "")
                              + f"\nTimed out after {int(timeout)}s.")
    return _format_result(cmd, result.returncode, result.stdout, result.stderr)


def _format_result(command: str, rc: int, out: str, err: str,
                   header: Optional[str] = None) -> str:
    combined = (out + ("\n" + err if err.strip() else "")).strip()
    # Keep the tail: pytest's summary (pass/fail counts, failing assertions) is at the end.
    if len(combined) > 4000:
        combined = "...(truncated)...\n" + combined[-4000:]
    if header:
        head = header
    elif rc == 0:
        head = "TESTS PASSED"
    elif rc == -1:
        head = "TEST RUN TIMED OUT"
    else:
        head = f"TESTS FAILED (exit {rc})"
    return f"{head}\n$ {command}\n\n{combined or '(no output)'}"


class RunTestsTool(BaseTool):
    """Coder tool: run the project's tests in the isolated sandbox and return the real result."""

    name = "run_tests"
    # Whose sandbox: the run goes into the caller's own scratch environment.
    identity_kwargs = ("user_scope_id",)
    category    = "code"
    coder_only = True              # coder-only: the main agent delegates via a coding_agent task
    permission_level = "read"      # reads the host project; executes only inside the sandbox
    side_effect_class = "none"     # never modifies the host project
    description = (
        "Run the project's tests inside your isolated Docker sandbox and return the REAL "
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

    # What a run does besides the tests, each step on its own clock (run_project_tests):
    # mkdir 30 s, copying the project in 120 s, the pytest probe 30 s, installing pytest
    # where the scratch environment still runs on the fallback image 180 s, removing the
    # copy 30 s - plus the 15 s backstop exec_bounded keeps behind each bounded step.
    PREPARATION_SECONDS = 30 + 120 + 30 + 180 + 30 + 5 * 15

    @staticmethod
    def _timeout(args) -> int:
        """The test command's own timeout: what the call says, else 180. A value that is not
        a number falls back too, here and in the budget alike, instead of raising."""
        try:
            return int((args or {}).get("timeout") or 180)
        except (TypeError, ValueError):
            return 180

    def budget_seconds(self, args):
        # The test command's own timeout plus everything around it: the dispatcher must not
        # stop waiting while pytest is still being installed.
        return self._timeout(args) + self.PREPARATION_SECONDS

    def __init__(self, base_dir: str = ".", environment=None):
        # base_dir defaults so the main agent's tool loader can instantiate the class (obj()) without
        # crashing; it is then excluded via coder_only. The coder passes the real project dir.
        self.base_dir = base_dir
        # A project environment whose /workspace is base_dir: the tests then run there, in
        # the project itself, instead of in a copy in the scratch environment. Per instance,
        # and so is what that means for the gate: a test run in the real project can write.
        self.environment = environment
        if environment is not None:
            self.permission_level = "write"
            self.side_effect_class = "reversible"

    def run(self, **kwargs) -> str:
        timeout = self._timeout(kwargs)
        if self.environment is not None:
            return run_tests_in_environment(self.environment, kwargs.get("command"),
                                            timeout=timeout)
        return run_project_tests(self.base_dir, kwargs.get("command"), timeout=timeout,
                                 user_scope_id=kwargs.get("user_scope_id"))
