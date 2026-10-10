# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The agent's handle on its background host commands: list, read, type into, stop.

``host_bash(background=true)`` starts a command and returns its id; this tool is how the
agent works with it afterwards (vaf/core/processes.py holds the processes). A process is
visible to the chat and the person that started it and to nobody else, so an id from
another chat reads as unknown.

``write`` and ``stop`` act on a command the person already approved when it was started;
they do not ask again. Starting is where the confirmation is.

Background commands started in a sandbox environment (``sandbox_exec(background=true)``,
ids ``e-<environment>-<process>``) are listed, read and stopped here too; they run in the
person's own container, and vaf/core/environments.py holds them. ``write`` does not reach
them: they have no input channel.
"""
from __future__ import annotations

from vaf.tools.base import BaseTool


class HostProcessTool(BaseTool):
    name = "host_process"
    category = "code"
    permission_level = "write"
    side_effect_class = "reversible"
    channel_restrictions = ("channel",)
    # The owner of a process is the person AND the chat: the scope comes from here, the
    # chat from the run's session.
    identity_kwargs = ("user_scope_id",)
    description = (
        "Work with the background commands this chat started with host_bash(background=true) "
        "or sandbox_exec(background=true): action='list' shows them, 'log' shows the end of "
        "one's output, 'write' sends a line to its input (host commands only, e.g. a server "
        "console command), 'stop' ends it and everything it started. The chat is woken on its "
        "own when a command ends; use 'log' to look before that."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "log", "write", "stop"],
                       "description": "What to do."},
            "id": {"type": "string", "description": "The id host_bash or sandbox_exec returned, e.g. p-1a2b3c4d or e-0a1b2c3d-p1a2b3c4d. Not needed for list."},
            "text": {"type": "string", "description": "For write: the line to send to the command's input."},
            "max_chars": {"type": "integer", "description": "For log: how much of the end of the output to show (default 4000, max 20000).", "default": 4000},
        },
        "required": ["action"],
    }

    def run(self, **kwargs) -> str:
        from vaf.core import processes
        from vaf.core.subagent_ipc import get_current_session_id

        action = str(kwargs.get("action") or "").strip().lower()
        session_id = get_current_session_id()
        scope = kwargs.get("user_scope_id")

        if action == "list":
            records = processes.list_for(session_id=session_id, user_scope_id=scope)
            lines = [r.describe() for r in records]
            lines += [f"{p['handle']}: {p['state']} - {p['command'][:120]}"
                      for p in self._environment_processes(scope, session_id)]
            if not lines:
                return "No background commands in this chat."
            return "\n".join(lines)

        if action not in ("log", "write", "stop"):
            return "[ERROR] host_process: action must be one of list, log, write, stop."
        proc_id = str(kwargs.get("id") or "").strip()
        if proc_id.startswith("e-"):
            return self._environment_action(action, proc_id, scope, kwargs)
        record = processes.get(str(kwargs.get("id") or ""), session_id=session_id,
                               user_scope_id=scope)
        if record is None:
            return ("[ERROR] host_process: no background command with that id in this chat. "
                    "action='list' shows the ones that exist.")

        if action == "log":
            try:
                max_chars = int(kwargs.get("max_chars") or 4000)
            except (TypeError, ValueError):
                max_chars = 4000
            max_chars = min(max(200, max_chars), 20000)
            body = processes.read_tail(record, max_chars=max_chars).strip() or "(no output yet)"
            return f"{record.describe()}\n{body}"
        if action == "write":
            text = kwargs.get("text")
            if text is None or str(text) == "":
                return "[ERROR] host_process: write needs the text to send."
            return processes.write(record, str(text))
        return processes.stop(record)

    @staticmethod
    def _environment_processes(scope, session_id):
        try:
            from vaf.core.environments import get_environment_manager
            return get_environment_manager().processes(scope, session_id=session_id or "")
        except Exception:
            return []

    @staticmethod
    def _environment_action(action, handle, scope, kwargs) -> str:
        from vaf.core.environments import EnvironmentRefused, get_environment_manager
        if action == "write":
            return ("[ERROR] host_process: a command in a sandbox environment has no input "
                    "to write to; stop it and start it again with what it needs.")
        try:
            mgr = get_environment_manager()
            if action == "log":
                try:
                    max_chars = int(kwargs.get("max_chars") or 4000)
                except (TypeError, ValueError):
                    max_chars = 4000
                max_chars = min(max(200, max_chars), 20000)
                return f"{handle}\n{mgr.process_log(scope, handle, max_chars=max_chars).strip() or '(no output yet)'}"
            return mgr.stop_process(scope, handle)
        except EnvironmentRefused as e:
            return f"[ERROR] host_process: {e}"
        except Exception as e:
            return f"[ERROR] host_process: the sandbox could not be reached ({e})"
