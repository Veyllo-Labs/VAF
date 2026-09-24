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
    # Whose command it is: a background command belongs to this person's chat, and the
    # turn that wakes the chat when it ends runs as them.
    identity_kwargs = ("user_scope_id", "username", "user_role")
    description = (
        "Run a shell command directly on the HOST (no sandbox). For host/system tasks that "
        "need real host access - e.g. inspecting a running docker container, host services, "
        "or a host CLI. Asks the user before it runs, unless the user already allowed it for "
        "this chat or always, and is available only in the local app (never over "
        "Telegram/WhatsApp/Discord). Prefer safer tools when host access is not actually "
        "required. With background=true the command keeps running after the call returns "
        "(a local server, a long upload or build): you get its id at once, read or stop it "
        "with host_process, and this chat gets a new turn when it ends."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to run on the host."},
            "timeout": {"type": "integer", "description": "Timeout seconds (default 120, max 600). Raise it for a build or an upload that legitimately runs for minutes.", "default": 120},
            "background": {"type": "boolean", "description": "Start the command and return at once instead of waiting for it; for work that runs longer than you want to wait or keeps running (a server). The chat is woken when it ends.", "default": False},
        },
        "required": ["command"],
    }

    MAX_TIMEOUT_SECONDS = 600   # a real build (a Maven package run was measured at 5 min)

    @classmethod
    def _command_timeout(cls, args) -> int:
        try:
            requested = int(args.get("timeout") or 120)
        except (TypeError, ValueError):
            requested = 120
        return min(max(10, requested), cls.MAX_TIMEOUT_SECONDS)

    def budget_seconds(self, args):
        # The dispatcher waits for the command's OWN timeout plus a margin to collect the
        # output. It used to stop waiting at the generic 120 s while the command itself was
        # allowed 300, so a long build was abandoned half-way and reported as a timeout.
        # A background start returns at once.
        if args.get("background"):
            return 30
        return self._command_timeout(args) + 15

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

        timeout = self._command_timeout(kwargs)

        # Reisleine: refuse the handful of catastrophic patterns even after confirmation.
        is_safe, warning = is_command_safe(command, profile="host")
        if not is_safe:
            return f"[BLOCKED] {warning}"

        if kwargs.get("background"):
            return self._start_background(command, kwargs)

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

        parts = ["[HOST EXECUTION]", f"$ {command}"]
        if warning:
            parts.insert(0, warning)
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        # Start AND end of each stream: a build prints its error last.
        from vaf.core.tool_dispatch import clip_middle
        if out:
            parts.append("\n" + clip_middle(out, 8000, marker="... ({left_out} chars left out) ..."))
        if err:
            parts.append("\n[stderr]\n" + clip_middle(err, 4000, marker="... ({left_out} chars left out) ..."))
        parts.append("\nOK" if proc.returncode == 0 else f"\nExit {proc.returncode}")
        return "\n".join(parts)

    @staticmethod
    def _start_background(command: str, kwargs: dict) -> str:
        """Start the command detached (vaf/core/processes.py) and return its id at once."""
        from vaf.core import processes
        from vaf.core.subagent_ipc import get_current_session_id
        from vaf.core.tool_dispatch import is_channel_session

        session_id = get_current_session_id()
        # Where nobody could be woken, a background command would run on unseen: a
        # messaging-channel chat has no wake delivery, and a sub-agent's own process ends
        # long before the command does, taking the watcher with it.
        if is_channel_session("", session_id):
            return ("[BLOCKED] background commands are not available in a messaging-channel "
                    "chat: nobody could be told when it ends. Run it without background.")
        if os.environ.get("VAF_IN_SUBAGENT_TERMINAL", "").strip().lower() in ("1", "true", "yes"):
            return ("[BLOCKED] a sub-agent cannot start a background command: its process ends "
                    "before the command does. Run it without background, or report that the "
                    "main chat should start it.")
        try:
            record = processes.start(
                command, session_id=session_id, user_scope_id=kwargs.get("user_scope_id"),
                username=kwargs.get("username"), role=kwargs.get("user_role"),
            )
        except processes.ProcessRefused as e:
            return f"[BLOCKED] {e}"
        return (
            f"[HOST BACKGROUND] started {record.id}\n$ {command}\n"
            f"It keeps running; its output goes to a private log. This chat gets a new turn "
            f"when it ends. host_process(action=\"log\", id=\"{record.id}\") shows the output, "
            f"action=\"write\" sends a line to its input, action=\"stop\" ends it."
        )
