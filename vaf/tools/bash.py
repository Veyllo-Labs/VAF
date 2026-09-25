# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
VAF Bash Tool - Execute shell commands
Allows the AI agent to run shell commands securely
Works on Windows, macOS, and Linux
"""
import subprocess
import os
import re
import sys
import logging
from typing import Dict, Any
from pathlib import Path

logger = logging.getLogger("vaf.bash")

from vaf.tools.base import BaseTool
from vaf.core.command_policy import is_command_safe

try:
    from vaf.core.platform import Platform
except ImportError:
    Platform = None


# What a download failing for want of a network prints, across the usual tools (curl, pip,
# npm, Maven, Gradle, git, apt). Matched only on a FAILED command's output.
_NO_NETWORK_RE = re.compile(
    r"Could not resolve host|Temporary failure in name resolution|Name or service not known|"
    r"Network is unreachable|getaddrinfo (?:ENOTFOUND|EAI_AGAIN)|EAI_AGAIN|ENETUNREACH|"
    r"Failed to establish a new connection|Could not transfer artifact|UnknownHostException|"
    r"Could not resolve dependencies|unable to access 'http",
    re.IGNORECASE,
)


class BashTool(BaseTool):
    """Execute shell commands on the system."""

    name = "bash"
    category    = "code"
    permission_level = "dangerous"
    side_effect_class = "irreversible"
    coder_only = True  # Only available to Coder Sub-Agent
    # NAMED EXCEPTION: this tool declares NO identity and NO file_access, and that is
    # deliberate, not forgotten - the difference this comment exists to preserve. The coder
    # must be able to use its tools at full strength (build, test, install), and a shell
    # confined to a per-user file jail is not a shell. The containment for a user who should
    # not have this power is the per-user tool permission, which the dispatch funnel and the
    # coder's child process now ENFORCE (vaf/auth/permissions.py): an admin can withhold the
    # coder entirely, or allow the coder and withhold bash by name - it is offered in the
    # user manager's picker via GET /api/users/tool-universe. What remains true: a tenant
    # who is ALLOWED bash has an unjailed shell, by design, so granting it is the decision.
    # Frozen in tests/test_coder_identity_boundary.py so a silent change shows up.
    # The description says what the jail really is. It used to offer "npm install" as an
    # example while the jail has no network at all (workspace_exec: --unshare-net), so every
    # dependency download failed here first and the run found host_bash only after the error.
    description = """Execute a shell command in the project directory, inside a sandbox.

The project directory is writable; the rest of the system is read-only, and there is NO
network. Use it for everything that works offline:
- Build and test with what is already there (compile, python -m pytest, a local script)
- Git operations
- File system operations (ls, cat, mkdir, etc.)

Anything that downloads - npm install, pip install, a Maven or Gradle build that fetches
dependencies, curl, git clone - fails here. Run it with host_bash instead when you have it:
same command, on the host, with network.

Examples:
- bash(command="ls -la") - List files
- bash(command="python -m pytest") - Run tests
- bash(command="git status") - Check git status

`timeout` (default 120, at most 300 seconds) is how long the command may run."""
    
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute"
            },
            "cwd": {
                "type": "string",
                "description": "Working directory (optional)"
            },
            "timeout": {
                "type": "integer",
                "description": "Seconds the command may run (default 120, at most 300)."
            }
        },
        "required": ["command"]
    }
    
    def budget_seconds(self, args):
        # The command's own timeout (default 120, at most 300) plus the jail's start-up: the
        # dispatcher must not stop waiting before the command itself is allowed to end.
        try:
            own = int((args or {}).get("timeout") or 120)
        except (TypeError, ValueError):
            own = 120
        return min(max(10, own), 300) + 30

    def __init__(self, base_dir: str = None):
        # base_dir = the coder's project workspace. Bound at registration (like the git
        # tools) so bash defaults to the project, not the tray process cwd, and so the
        # sandbox confines writes to exactly this workspace.
        self.base_dir = base_dir

    def run(self, **kwargs) -> str:
        import shlex as _shlex
        command = kwargs.get("command", "")
        cwd = kwargs.get("cwd", None)
        timeout = kwargs.get("timeout", 120)

        if not command or not command.strip():
            return "Error: No command provided"

        # Cheap first-line blocklist (defense in depth; the real confinement is the jail).
        # profile="jailed": this lane runs under bubblewrap (--clearenv,
        # --unshare-net) or a --network none container, so wiping the throwaway
        # workspace is ordinary work and a network fetch reaches nothing. Only
        # what would hurt the machine or the jail root is refused.
        is_safe, warning = is_command_safe(command, profile="jailed")
        if not is_safe:
            return f"Error: {warning}"

        timeout = min(max(10, timeout), 300)

        workspace = self.base_dir or kwargs.get("base_dir")
        if not workspace:
            # No project workspace bound: refuse rather than fall back to the process cwd
            # (which could be the home dir and would root the jail at HOME + its secrets).
            return "Error: bash has no project workspace to run in."
        # A relative cwd is a subdir of the workspace; cd into it inside the jail.
        run_command = command
        if cwd:
            run_command = f"cd {_shlex.quote(str(cwd))} && {command}"

        from vaf.tools.workspace_exec import run_in_workspace
        rc, out, err, mode = run_in_workspace(workspace, run_command, timeout=timeout)

        parts = []
        if warning:
            parts.append(warning)
        parts.append(f"$ {command}")
        if mode == "refused":
            return f"{err}"
        parts.append(f"(workspace: {mode})")
        if out:
            if len(out) > 8000:
                out = out[:8000] + "\n... (output truncated)"
            parts.append(f"\nOutput:\n{out}")
        if err:
            if len(err) > 4000:
                err = err[:4000] + "\n... (stderr truncated)"
            parts.append(f"\nStderr:\n{err}")
        parts.append("\nSuccess (exit code: 0)" if rc == 0 else f"\nFailed (exit code: {rc})")
        if rc != 0 and _NO_NETWORK_RE.search(f"{out}\n{err}"):
            # The failure is the missing network, not the command: say so, so the next step
            # is the host and not a round of guessing at the build.
            parts.append("\nThis sandbox has no network. Run the same command with host_bash "
                         "if you have it: it runs on the host, with network.")
        return "\n".join(parts)
