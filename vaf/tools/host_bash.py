# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Host shell: one command, run directly on the host.

It runs outside every sandbox and outside the per-user file jail, on purpose - some tasks
genuinely need the real host ("check my running docker container", a host CLI, a local
build). WHO may use it is an account permission (the account allowlist in user management;
the standard preset includes it). HOW each use is controlled:

  1. permission_level = "dangerous" -> the framework's confirmation gate. In the chat the
     person sees tool, command and reason, and answers: only this time, for this chat,
     always, or cancel (vaf/core/tool_dispatch.resolve_confirmation_gate).
  2. The coding agent and workflow steps run it WITHOUT asking (vaf/tools/coder.py,
     vaf/workflows/engine.py). Deliberate: both run unattended, a dialog would stall them,
     and a coder that needs a host build or a host CLI must not stop for it. What still
     applies there: the account allowlist, the policy block, and (for workflow steps) an
     application's authorizer.
  3. The main agent's DIRECT call from a messaging channel (Telegram/WhatsApp/Discord) is
     refused in TWO layers, because the person cannot be shown the confirmation there:
       a. channel_restrictions -> the policy-layer block (evaluate_tool_policy).
       b. a non-liftable guard in run(), fed by the `_is_channel_session` the chat lane
          hands over (vaf/core/agent.py). It holds even when the admin sets
          channel_tools_unrestricted, which lifts 3a for the convenience tools. Only the
          chat lane hands it over: the guard protects the turn where somebody would have
          been asked, not the unattended lanes in 2.

A cheap blocklist stops the few catastrophic patterns (command_policy, host profile).
"""
from __future__ import annotations

import os
import platform
import subprocess

from vaf.tools.base import BaseTool
from vaf.core.command_policy import is_command_safe  # offline classifier, strict profile


class HostBashTool(BaseTool):
    name = "host_bash"
    category    = "code"
    permission_level = "dangerous"   # -> confirmation gate in execute_tool
    channel_restrictions = ("channel",)  # hard-blocked on every chat channel
    side_effect_class = "irreversible"
    coder_only = False               # offered to the main agent; the coder may call it too
    description = (
        "Run a shell command directly on the HOST (no sandbox). For host/system tasks that "
        "need real host access - e.g. inspecting a running docker container, host services, "
        "or a host CLI. Asks the user before it runs, unless the user already allowed it for "
        "this chat or always, and is available only in the local app (never over "
        "Telegram/WhatsApp/Discord). Prefer safer tools when host access is not actually "
        "required."
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
        # otherwise run host commands unconfirmed. The chat lane injects the authoritative
        # _is_channel_session it already computed; refuse unconditionally when it is a channel.
        if kwargs.get("_is_channel_session"):
            return (
                "[BLOCKED] host_bash is not available over remote messaging channels "
                "(Telegram/WhatsApp/Discord). Host/docker commands must be run from the local "
                "app, where each command is shown and confirmed before it executes."
            )

        timeout = min(max(10, int(kwargs.get("timeout") or 120)), 300)

        # Reisleine: refuse the handful of catastrophic patterns even after confirmation.
        is_safe, warning = is_command_safe(command, profile="host")
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
