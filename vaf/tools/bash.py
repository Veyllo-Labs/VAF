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
import sys
import logging
from typing import Dict, Any
from pathlib import Path

logger = logging.getLogger("vaf.bash")

from vaf.tools.base import BaseTool

try:
    from vaf.core.platform import Platform
except ImportError:
    Platform = None


# Commands that are blocked for security
BLOCKED_COMMANDS = [
    "rm -rf /",
    "rm -rf /*",
    ":(){ :|:& };:",  # Fork bomb
    "dd if=/dev/zero of=/dev/sda",
    "mkfs",
    "format c:",
    "> /dev/sda",
    "sudo rm -rf",
    "curl | bash",
    "wget | bash",
]

# Commands that need warning
DANGEROUS_PATTERNS = [
    "rm -rf",
    "git reset --hard",
    "git clean -fd",
    "drop database",
    "drop table",
    "truncate",
    "fdisk",
]


def is_command_safe(command: str) -> tuple:
    """Check if command is safe to execute."""
    cmd_lower = command.lower()
    
    for blocked in BLOCKED_COMMANDS:
        if blocked.lower() in cmd_lower:
            return False, f"Blocked command pattern: {blocked}"
    
    for pattern in DANGEROUS_PATTERNS:
        if pattern.lower() in cmd_lower:
            return True, f"⚠️ Warning: '{pattern}' could be dangerous"
    
    return True, ""


class BashTool(BaseTool):
    """Execute shell commands on the system."""
    
    name = "bash"
    permission_level = "dangerous"
    side_effect_class = "irreversible"
    coder_only = True  # Only available to Coder Sub-Agent
    description = """Execute a shell command in the project directory.
    
Use this tool to:
- Run build commands (npm, cargo, pip, etc.)
- Execute tests
- Git operations
- File system operations (ls, cat, mkdir, etc.)
- Install dependencies
- Run scripts

Examples:
- bash(command="ls -la") - List files
- bash(command="npm install") - Install npm packages
- bash(command="python -m pytest") - Run tests
- bash(command="git status") - Check git status

IMPORTANT: Long-running commands timeout after 120 seconds."""
    
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
            }
        },
        "required": ["command"]
    }
    
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
        is_safe, warning = is_command_safe(command)
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
        return "\n".join(parts)
