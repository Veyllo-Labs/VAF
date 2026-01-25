"""
Python Exec Tool (Host)

Runs Python code via the host interpreter (sys.executable).
This is intentionally "risky" compared to python_sandbox because it can access
filesystem/network depending on the code.

It must be gated by Trust/Capability rules (once/always/cancel).
"""

from __future__ import annotations

import os
import sys
import subprocess
from pathlib import Path

from vaf.tools.base import BaseTool


class PythonExecTool(BaseTool):
    name = "python_exec"
    description = (
        "Run Python code using the host interpreter (UNSANDBOXED). "
        "RISKY: code may access files/network. Use only with explicit user approval."
    )

    parameters = {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python code to execute via sys.executable -c"},
            "timeout": {"type": "integer", "description": "Timeout seconds (default: 30)", "default": 30},
        },
        "required": ["code"],
    }

    def run(self, **kwargs) -> str:
        # Convert Path objects to strings (OS-independent defensive handling)
        # str() works for both strings and Path objects
        code = str(kwargs.get("code") or "").strip()
        timeout = int(kwargs.get("timeout") or 30)

        if not code:
            return "[ERROR] python_exec: missing code"

        # Try to use Docker Sandbox first
        try:
            from vaf.tools.sandbox import DockerSandbox
            import base64
            
            with DockerSandbox() as sandbox:
                # Wrap code to handle I/O correctly inside container
                # We simply pass it to python3 -c "..."
                # Escaping quotes for shell is tricky, so we base64 encode the script
                # and decode it inside the container to avoid syntax errors.
                
                b64_code = base64.b64encode(code.encode('utf-8')).decode('utf-8')
                safe_cmd = f"echo {b64_code} | base64 -d | python3"
                
                exit_code, out, err = sandbox.execute(safe_cmd, timeout=timeout)
                
                if exit_code != 0:
                     if err: return f"[ERROR] python_exec (SANDBOX exit={exit_code}):\n{err}"
                     return f"[ERROR] python_exec (SANDBOX exit={exit_code})"
                
                res_str = ""
                if out: res_str += out
                if err: res_str += f"\n[stderr]\n{err}"
                return res_str.strip() or "OK"

        except (ImportError, RuntimeError):
             # Fallback to Host Execution
             pass

        try:
            proc = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
        except subprocess.TimeoutExpired:
            return f"[ERROR] python_exec: timeout after {timeout}s"
        except Exception as e:
            return f"[ERROR] python_exec: {e}"

        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()

        if proc.returncode != 0:
            if err:
                return f"[ERROR] python_exec (exit={proc.returncode}):\n{err}"
            return f"[ERROR] python_exec (exit={proc.returncode})"

        if out and err:
            return f"{out}\n\n[stderr]\n{err}"
        if out:
            return out
        if err:
            return f"[stderr]\n{err}"

        return "OK"


