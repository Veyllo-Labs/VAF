# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Host shell for the MAIN agent (not the coder).

The coder runs bash inside a kernel-jailed workspace (vaf/tools/workspace_exec.py) and
cannot touch VAF or the host. Some tasks, though, genuinely need the real host - e.g.
"check my running docker container", inspect host services, run a host CLI. Those belong
to the main agent, and this tool provides them under two hard controls:

  1. permission_level = "dangerous"  -> the framework's confirmation gate fires: the user
     approves each run in the Web UI (tool + command + reason shown) before it executes.
  2. Remote channels (Telegram/WhatsApp/Discord) are blocked in TWO layers, because there is
     no safe way to show the confirmation there:
       a. channel_restrictions        -> the policy-layer block (evaluate_tool_policy).
       b. a non-liftable guard in run() -> even when the admin sets channel_tools_unrestricted
          (which lifts 2a for the convenience tools), host_bash still refuses on a channel,
          using the authoritative is_channel_session that execute_tool injects. Local app only.

It runs UNSANDBOXED on the host on purpose (that is the point). A cheap blocklist stops the
few catastrophic patterns; the real safety is the per-command human approval + local-only gate.
"""
from __future__ import annotations

import os
import platform
import subprocess

from vaf.tools.base import BaseTool
from vaf.tools.bash import is_command_safe  # shared catastrophic-pattern blocklist


class HostBashTool(BaseTool):
    name = "host_bash"
    permission_level = "dangerous"   # -> confirmation gate in execute_tool
    channel_restrictions = ("channel", "telegram", "whatsapp", "discord")  # hard-blocked on remote
    side_effect_class = "irreversible"
    coder_only = False               # this is the MAIN agent's tool, not the coder's
    description = (
        "Run a shell command directly on the HOST (no sandbox). For host/system tasks that "
        "need real host access - e.g. inspecting a running docker container, host services, "
        "or a host CLI. Requires the user's explicit confirmation each time and is available "
        "only in the local app (never over Telegram/WhatsApp/Discord). Prefer safer tools "
        "when host access is not actually required."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to run on the host."},
            "timeout": {"type": "integer", "description": "Timeout seconds (default 120, max 300).", "default": 120},
        },
        "required": ["command"],
    }

    def run(self, **kwargs) -> str:
        command = str(kwargs.get("command") or "").strip()
        if not command:
            return "[ERROR] host_bash: no command provided"

        # Non-liftable channel guard (defense in depth). channel_restrictions above is the
        # policy-layer block, but it is lifted when the admin sets channel_tools_unrestricted
        # (default ON on a fresh install). host_bash on a remote channel is categorically not
        # allowed: there is no way to show the confirmation there, so a Telegram message could
        # otherwise run host commands unconfirmed. execute_tool injects the authoritative
        # is_channel_session it already computed; refuse unconditionally when it is a channel.
        if kwargs.get("_is_channel_session"):
            return (
                "[BLOCKED] host_bash is not available over remote messaging channels "
                "(Telegram/WhatsApp/Discord). Host/docker commands must be run from the local "
                "app, where each command is shown and confirmed before it executes."
            )

        timeout = min(max(10, int(kwargs.get("timeout") or 120)), 300)

        # Reisleine: refuse the handful of catastrophic patterns even after confirmation.
        is_safe, warning = is_command_safe(command)
        if not is_safe:
            return f"[BLOCKED] {warning}"

        run_kwargs = {
            "capture_output": True, "text": True, "timeout": timeout, "shell": True,
            "env": {**os.environ, "PYTHONIOENCODING": "utf-8"},
        }
        if platform.system() == "Windows" and getattr(subprocess, "CREATE_NO_WINDOW", None) is not None:
            run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            proc = subprocess.run(command, **run_kwargs)
        except subprocess.TimeoutExpired:
            return f"[HOST] Command timed out after {timeout}s: {command}"
        except Exception as e:
            return f"[HOST][ERROR] {e}"

        parts = ["[HOST EXECUTION - confirmed]", f"$ {command}"]
        if warning:
            parts.insert(0, warning)
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if out:
            parts.append("\n" + (out[:8000] + "\n... (truncated)" if len(out) > 8000 else out))
        if err:
            parts.append("\n[stderr]\n" + (err[:4000] + "\n... (truncated)" if len(err) > 4000 else err))
        parts.append("\nOK" if proc.returncode == 0 else f"\nExit {proc.returncode}")
        return "\n".join(parts)
