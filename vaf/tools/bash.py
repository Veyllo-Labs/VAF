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
    
    def run(self, **kwargs) -> str:
        command = kwargs.get("command", "")
        cwd = kwargs.get("cwd", None)
        timeout = kwargs.get("timeout", 120)
        
        if not command or not command.strip():
            return "Error: No command provided"
        
        # Security check
        is_safe, warning = is_command_safe(command)
        if not is_safe:
            return f"Error: {warning}"
        
        # Limit timeout
        timeout = min(max(10, timeout), 300)
        
        # Determine working directory
        if cwd:
            work_dir = Path(cwd).expanduser().resolve()
        else:
            work_dir = Path.cwd()
        
        if not work_dir.exists():
            return f"Error: Working directory does not exist: {work_dir}"
        
        # Try to use Docker Sandbox first
        try:
            from vaf.tools.sandbox import DockerSandbox
            with DockerSandbox() as sandbox:
                code, out, err = sandbox.execute(command, timeout=timeout, workdir=str(work_dir) if cwd else "/")
                
                result_parts = []
                if warning:
                    result_parts.append(warning)
                
                result_parts.append(f"$ {command}")
                result_parts.append(f"(in SANDBOX)") # Indicate sandbox usage
                
                if out:
                     if len(out) > 8000: out = out[:8000] + "\n... (truncated)"
                     result_parts.append(f"\nOutput:\n{out}")
                
                if err:
                     if len(err) > 4000: err = err[:4000] + "\n... (stderr truncated)"
                     result_parts.append(f"\nStderr:\n{err}")
                
                if code == 0:
                    result_parts.append(f"\n✓ Success (exit code: 0)")
                else:
                    result_parts.append(f"\n✗ Failed (exit code: {code})")
                
                return "\n".join(result_parts)
                
        except (ImportError, RuntimeError) as e:
            # Fallback to Host Execution if Docker fails/missing
            warning = f"⚠️  SANDBOX OFFLINE: {e}. Executing on HOST (Less Secure)!"
            logger.warning(warning)
            pass

        try:
            # Cross-platform shell arguments
            shell_args = {
                "cwd": work_dir,
                "capture_output": True,
                "text": True,
                "timeout": timeout,
                "shell": True,
                "env": {**os.environ, "PYTHONIOENCODING": "utf-8"}
            }
            
            # Use Platform module if available
            if Platform:
                if not Platform.is_windows():
                    shell_args["executable"] = Platform.default_shell()
            else:
                # Fallback: manual platform detection
                if sys.platform == "darwin":
                    shell_args["executable"] = "/bin/zsh" if os.path.exists("/bin/zsh") else "/bin/bash"
                elif sys.platform != "win32":
                    shell_args["executable"] = "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh"
            
            process = subprocess.run(command, **shell_args)
            
            # Build result
            result_parts = []
            
            if warning:
                result_parts.append(warning)
            
            result_parts.append(f"$ {command}")
            result_parts.append(f"(in {work_dir})")
            
            if process.stdout:
                # Truncate long output
                stdout = process.stdout
                if len(stdout) > 8000:
                    stdout = stdout[:8000] + "\n... (output truncated)"
                result_parts.append(f"\nOutput:\n{stdout}")
            
            if process.stderr:
                stderr = process.stderr
                if len(stderr) > 4000:
                    stderr = stderr[:4000] + "\n... (stderr truncated)"
                result_parts.append(f"\nStderr:\n{stderr}")
            
            if process.returncode == 0:
                result_parts.append(f"\n✓ Success (exit code: 0)")
            else:
                result_parts.append(f"\n✗ Failed (exit code: {process.returncode})")
            
            return "\n".join(result_parts)
            
        except subprocess.TimeoutExpired:
            return f"Error: Command timed out after {timeout} seconds"
        except Exception as e:
            return f"Error executing command: {e}"
