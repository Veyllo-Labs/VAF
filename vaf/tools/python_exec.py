# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Python Exec Tool (HOST - UNSAFE)

WARNING: This tool executes Python code directly on the host system.
It should ONLY be used when:
1. The user explicitly requests host execution
2. The code is from a trusted source
3. Docker sandbox is not suitable (e.g., needs host filesystem access)

For safe code execution, use python_sandbox instead.
"""

from __future__ import annotations

import os
import sys
import subprocess
import logging
from pathlib import Path

from vaf.tools.base import BaseTool
from vaf.core.trust import get_tool_policy

logger = logging.getLogger("vaf.python_exec")


class PythonExecTool(BaseTool):
    name = "python_exec"
    permission_level = "dangerous"
    channel_restrictions = ["channel", "telegram", "whatsapp", "discord"]
    side_effect_class = "irreversible"
    description = (
        "⚠️ UNSAFE: Run Python code directly on the HOST system (no sandbox). "
        "Only use when you need host filesystem/network access. "
        "Requires explicit user approval. Prefer 'python_sandbox' for safe execution. "
        "NEVER use this to write files — use write_file/document_writer instead. "
        "Large strings (HTML, code) will cause 'EOF when reading a line' errors via -c."
    )

    parameters = {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python code to execute via sys.executable -c"},
            "timeout": {"type": "integer", "description": "Timeout seconds (default: 30)", "default": 30},
        },
        "required": ["code"],
    }

    # "task" observed live (live incident: a weak model's schema-rejected
    # calls); "script" is the other common name for a code payload.
    input_aliases = {
        "code": ["task", "script"],
    }

    def run(self, **kwargs) -> str:
        code = str(kwargs.get("code") or "").strip()
        timeout = int(kwargs.get("timeout") or 30)

        if not code:
            return "[ERROR] python_exec: missing code"
        
        # Check if this tool is explicitly allowed
        policy = get_tool_policy("python_exec")
        if policy not in ("allow", "once"):
            logger.warning("python_exec called without explicit trust policy")
            return (
                "[SECURITY] python_exec runs code UNSANDBOXED on your host system.\n"
                "This is blocked by default for security.\n\n"
                "Use 'python_sandbox' for safe execution, or configure trust policy to allow python_exec."
            )

        logger.warning(f"⚠️ Executing Python code on HOST (unsandboxed): {code[:50]}...")

        try:
            import platform
            run_kwargs = {
                "capture_output": True,
                "text": True,
                "timeout": timeout,
                "env": {**os.environ, "PYTHONIOENCODING": "utf-8"},
            }
            if platform.system() == "Windows":
                run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            proc = subprocess.run([sys.executable, "-c", code], **run_kwargs)
        except subprocess.TimeoutExpired:
            return f"[ERROR] python_exec: timeout after {timeout}s"
        except Exception as e:
            return f"[ERROR] python_exec: {e}"

        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()

        # Add warning to output
        warning = "⚠️ [HOST EXECUTION - No Sandbox]\n\n"

        if proc.returncode != 0:
            if err:
                return f"{warning}[ERROR] (exit={proc.returncode}):\n{err}"
            return f"{warning}[ERROR] (exit={proc.returncode})"

        if out and err:
            return f"{warning}{out}\n\n[stderr]\n{err}"
        if out:
            return f"{warning}{out}"
        if err:
            return f"{warning}[stderr]\n{err}"

        return f"{warning}OK"
