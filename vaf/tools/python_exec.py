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


def _granted_for_this_chat(user_scope_id) -> bool:
    try:
        from vaf.core.subagent_ipc import get_current_session_id
        from vaf.core.trust import has_chat_grant
        return has_chat_grant("python_exec", user_scope_id, get_current_session_id())
    except Exception:
        return False


class PythonExecTool(BaseTool):
    name = "python_exec"
    category    = "code"
    # The trust policy is per user now, so the tool must know WHOSE policy to
    # read: without the declaration it would read the local-admin bucket for
    # every tenant.
    identity_kwargs = ("user_scope_id",)
    permission_level = "dangerous"
    channel_restrictions = ("channel",)
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
        
        # Its own check, on top of the gate, because some lanes run tools without one (a
        # workflow step): a stored "always" for this person, or their grant for the chat
        # this call belongs to. A one-call approval cannot reach this far - the tool has no
        # way to tell which call it was given for.
        scope = kwargs.get("user_scope_id")
        policy = get_tool_policy("python_exec", scope)
        if policy != "allow" and not _granted_for_this_chat(scope):
            logger.warning("python_exec called without explicit trust policy")
            return (
                "[SECURITY] python_exec runs code UNSANDBOXED on your host system.\n"
                "This is blocked by default for security.\n\n"
                "Use 'python_sandbox' for safe execution, or configure trust policy to allow python_exec."
            )

        logger.warning(f"⚠️ Executing Python code on HOST (unsandboxed): {code[:50]}...")

        # The person's stored credentials the code names, as environment variables
        # (vaf/core/user_secrets.py); the code carries the name, never the value.
        from vaf.core import user_secrets
        secret_env = user_secrets.env_for(code, user_scope_id=scope)
        try:
            import platform
            run_kwargs = {
                "capture_output": True,
                "text": True,
                "timeout": timeout,
                "env": {**os.environ, "PYTHONIOENCODING": "utf-8", **secret_env},
            }
            if platform.system() == "Windows":
                run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            proc = subprocess.run([sys.executable, "-c", code], **run_kwargs)
        except subprocess.TimeoutExpired:
            return f"[ERROR] python_exec: timeout after {timeout}s"
        except Exception as e:
            return f"[ERROR] python_exec: {e}"

        out = user_secrets.scrub((proc.stdout or "").strip(), secret_env)
        err = user_secrets.scrub((proc.stderr or "").strip(), secret_env)

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
