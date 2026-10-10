# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The agent's hands in a sandbox environment (vaf/core/environments.py).

An environment is a container, a volume and a network of the caller's own: a place to
run, install and test code without touching the host or anybody else's work. These tools
create and remove one, run commands in it, read and write its files, and move files
between it and the person's own folders. Background processes started here (a dev
server) are listed, read and stopped with host_process, which already holds the chat's
background commands.

Every tool acts as the caller (identity_kwargs); another person's environment answers
like a missing one. None runs from a messaging channel, where nobody could be asked.
"""
from __future__ import annotations

from typing import Any, Optional

from vaf.tools.base import BaseTool

_NETWORK_HELP = (
    "network: 'none' (no route out, the host unreachable), 'registries' (only package "
    "registries such as PyPI and npm, through a filtering proxy; it does not stop data "
    "leaving through those registries) or 'open' (the internet, and also the local "
    "network and whatever listens on this machine's open ports). Default: 'none' for "
    "temporary, 'registries' for project."
)


def _manager():
    from vaf.core.environments import get_environment_manager
    return get_environment_manager()


def _refused(tool: str, exc: Exception) -> str:
    return f"[ERROR] {tool}: {exc}"


def _stop_check():
    """True when the current chat asked to stop (the sandbox lanes poll it)."""
    try:
        from vaf.core.subagent_ipc import get_current_session_id
        from vaf.core.task_queue import TaskQueue
        sid = get_current_session_id()
        tq = TaskQueue()
        return lambda: bool(sid) and tq.should_stop(sid)
    except Exception:
        return lambda: False


class SandboxManageTool(BaseTool):
    name = "sandbox_manage"
    category = "code"
    permission_level = "write"
    side_effect_class = "irreversible"          # delete removes the environment's files
    channel_restrictions = ("channel",)
    identity_kwargs = ("user_scope_id", "username", "user_role", "session_id")
    # A project environment mounts a host folder read-write: the person's own write jail.
    file_access = "write"
    description = (
        "Create, list, stop or delete a sandbox environment: a Docker container of your own "
        "to run, install and test code in without touching this machine. kind='temporary' is "
        "removed whole 24 h after its last use; kind='project' stays (packages stay installed) "
        "and may mount a project folder at /workspace (project_path). Use sandbox_exec to run "
        "commands in it, sandbox_files to read and write its files. " + _NETWORK_HELP
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "list", "stop", "delete"],
                       "description": "What to do."},
            "environment": {"type": "string",
                            "description": "The environment's id (from create or list); for stop and delete."},
            "kind": {"type": "string", "enum": ["temporary", "project"],
                     "description": "For create. Default temporary."},
            "name": {"type": "string", "description": "For create: a short label."},
            "network": {"type": "string", "enum": ["none", "registries", "open"],
                        "description": "For create. See the tool description."},
            "project_path": {"type": "string",
                             "description": "For create with kind=project: a project folder of yours to mount at /workspace."},
            "memory_mb": {"type": "integer", "description": "For create: memory limit in MB (default 1024)."},
        },
        "required": ["action"],
    }

    def run(self, **kwargs) -> str:
        from vaf.core.environments import EnvironmentRefused
        action = str(kwargs.get("action") or "").strip().lower()
        scope = kwargs.get("user_scope_id")
        try:
            mgr = _manager()
            if action == "list":
                envs = mgr.list(scope)
                if not envs:
                    return "No sandbox environments."
                return "\n".join(e.describe() for e in envs)
            if action == "create":
                project_path = kwargs.get("project_path") or None
                if project_path:
                    from vaf.tools.filesystem import is_safe_path
                    ok, why = is_safe_path(str(project_path))
                    if not ok:
                        return _refused(self.name, why)
                env = mgr.create(scope, kind=str(kwargs.get("kind") or "temporary"),
                                 name=str(kwargs.get("name") or ""), project_path=project_path,
                                 network=kwargs.get("network") or None,
                                 memory_mb=kwargs.get("memory_mb") or None,
                                 session_id=str(kwargs.get("session_id") or ""),
                                 user_role=kwargs.get("user_role"))
                lines = [f"Created {env.describe()}",
                         f"Run commands with sandbox_exec(environment=\"{env.id}\", command=...)."]
                if env.degraded:
                    lines.append(f"Note: {env.degraded}.")
                return "\n".join(lines)
            env_id = str(kwargs.get("environment") or "")
            if action == "stop":
                return f"Stopped {mgr.stop(scope, env_id).id}."
            if action == "delete":
                return f"Deleted {mgr.delete(scope, env_id).id} (container, files and network)."
            return _refused(self.name, "action must be one of create, list, stop, delete")
        except EnvironmentRefused as e:
            return _refused(self.name, e)
        except Exception as e:
            return _refused(self.name, f"the sandbox could not be reached ({e})")


class SandboxExecTool(BaseTool):
    name = "sandbox_exec"
    category = "code"
    permission_level = "write"
    side_effect_class = "reversible"            # changes only the environment
    channel_restrictions = ("channel",)
    identity_kwargs = ("user_scope_id", "username", "user_role", "session_id")
    # Its own clock: `timeout -s KILL` inside the container plus a stop-aware backstop.
    self_supervised = True
    MAX_TIMEOUT_SECONDS = 1800
    description = (
        "Run a shell command in one of your sandbox environments (working directory "
        "/workspace unless cwd says otherwise) and get its exit code and output. "
        "background=true starts a command that keeps running, such as a dev server: you get "
        "its id at once, read or stop it with host_process, and this chat gets a new turn "
        "when it ends. The environment's network decides what the command can reach."
    )
    parameters = {
        "type": "object",
        "properties": {
            "environment": {"type": "string", "description": "The environment's id."},
            "command": {"type": "string", "description": "The shell command."},
            "cwd": {"type": "string", "description": "Working directory inside the environment."},
            "timeout": {"type": "integer", "description": "Seconds (default 120, max 1800).", "default": 120},
            "background": {"type": "boolean", "description": "Keep it running and return at once.",
                           "default": False},
        },
        "required": ["environment", "command"],
    }

    def run(self, **kwargs) -> str:
        from vaf.core.environments import EnvironmentRefused
        scope = kwargs.get("user_scope_id")
        env_id = str(kwargs.get("environment") or "")
        command = str(kwargs.get("command") or "").strip()
        if not command:
            return _refused(self.name, "no command")
        try:
            mgr = _manager()
            if kwargs.get("background"):
                handle = mgr.start_process(scope, env_id, command,
                                           session_id=str(kwargs.get("session_id") or ""),
                                           username=kwargs.get("username"),
                                           user_role=kwargs.get("user_role"),
                                           cwd=kwargs.get("cwd") or None)
                return (f"Started {handle} in the background. host_process(action=\"log\", "
                        f"id=\"{handle}\") shows its output; this chat is woken when it ends.")
            try:
                timeout = int(kwargs.get("timeout") or 120)
            except (TypeError, ValueError):
                timeout = 120
            timeout = min(max(1, timeout), self.MAX_TIMEOUT_SECONDS)
            r = mgr.exec(scope, env_id, command, timeout=timeout, cwd=kwargs.get("cwd") or None,
                         check_stop=_stop_check())
        except EnvironmentRefused as e:
            return _refused(self.name, e)
        except Exception as e:
            return _refused(self.name, f"the sandbox could not be reached ({e})")
        out = (r.stdout or "") + (f"\n[stderr]\n{r.stderr}" if (r.stderr or "").strip() else "")
        if len(out) > 12000:
            out = "...(truncated)...\n" + out[-12000:]
        if r.cancelled:
            head = "cancelled by stop request"
        elif r.timed_out:
            head = f"timed out after {timeout}s"
        else:
            head = f"exit {r.returncode}"
        return f"[{head}]\n{out.strip() or '(no output)'}"


class SandboxFilesTool(BaseTool):
    name = "sandbox_files"
    category = "code"
    permission_level = "write"
    side_effect_class = "reversible"
    channel_restrictions = ("channel",)
    identity_kwargs = ("user_scope_id", "username", "user_role", "session_id")
    description = (
        "Read, write or list files INSIDE one of your sandbox environments (relative paths "
        "resolve against /workspace). These are the environment's files, not this machine's: "
        "to move files between the two, use sandbox_transfer."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["read", "write", "list"], "description": "What to do."},
            "environment": {"type": "string", "description": "The environment's id."},
            "path": {"type": "string", "description": "A path inside the environment (default /workspace)."},
            "content": {"type": "string", "description": "For write: the whole new file content."},
            "depth": {"type": "integer", "description": "For list: how deep to go (default 2, max 6)."},
        },
        "required": ["action", "environment"],
    }

    def run(self, **kwargs) -> str:
        from vaf.core.environments import EnvironmentRefused
        scope = kwargs.get("user_scope_id")
        env_id = str(kwargs.get("environment") or "")
        action = str(kwargs.get("action") or "").strip().lower()
        path = str(kwargs.get("path") or ".")
        try:
            mgr = _manager()
            if action == "read":
                return mgr.read_file(scope, env_id, path)
            if action == "write":
                content = kwargs.get("content")
                if content is None:
                    return _refused(self.name, "write needs the content")
                return f"Wrote {mgr.write_file(scope, env_id, path, str(content))}."
            if action == "list":
                return mgr.list_files(scope, env_id, path, depth=int(kwargs.get("depth") or 2)) or "(empty)"
            return _refused(self.name, "action must be one of read, write, list")
        except EnvironmentRefused as e:
            return _refused(self.name, e)
        except Exception as e:
            return _refused(self.name, f"the sandbox could not be reached ({e})")


class SandboxTransferTool(BaseTool):
    name = "sandbox_transfer"
    category = "code"
    permission_level = "write"
    side_effect_class = "reversible"
    channel_restrictions = ("channel",)
    identity_kwargs = ("user_scope_id", "username", "user_role", "session_id")
    # host_path is a folder or file of the person's own, read (copy_in) or written (copy_out).
    file_access = "write"
    description = (
        "Copy files between one of your sandbox environments and your own folders on this "
        "machine. action='copy_in' copies host_path (a file or folder) into the environment "
        "(to path, default /workspace); action='copy_out' copies path from the environment into "
        "the folder host_path. Only regular files and folders arrive on this machine."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["copy_in", "copy_out"], "description": "Direction."},
            "environment": {"type": "string", "description": "The environment's id."},
            "host_path": {"type": "string", "description": "Your file or folder on this machine (copy_in), or the folder to copy into (copy_out)."},
            "path": {"type": "string", "description": "The path inside the environment."},
        },
        "required": ["action", "environment", "host_path"],
    }

    def run(self, **kwargs) -> str:
        from vaf.core.environments import EnvironmentRefused
        from vaf.tools.filesystem import is_safe_path
        scope = kwargs.get("user_scope_id")
        env_id = str(kwargs.get("environment") or "")
        action = str(kwargs.get("action") or "").strip().lower()
        host_path = str(kwargs.get("host_path") or "").strip()
        if not host_path:
            return _refused(self.name, "host_path is required")
        ok, why = is_safe_path(host_path)
        if not ok:
            return _refused(self.name, why)
        try:
            mgr = _manager()
            if action == "copy_in":
                where = mgr.copy_in(scope, env_id, host_path, str(kwargs.get("path") or "."))
                return f"Copied into the environment: {where}"
            if action == "copy_out":
                path = str(kwargs.get("path") or "").strip()
                if not path:
                    return _refused(self.name, "copy_out needs the path inside the environment")
                written = mgr.copy_out(scope, env_id, path, host_path)
                self._announce(kwargs.get("session_id"), written)
                if not written:
                    return "Nothing was copied (no regular files at that path)."
                more = f" (+{len(written) - 20} more)" if len(written) > 20 else ""
                return "Copied to this machine:\n" + "\n".join(written[:20]) + more
            return _refused(self.name, "action must be one of copy_in, copy_out")
        except EnvironmentRefused as e:
            return _refused(self.name, e)
        except Exception as e:
            return _refused(self.name, f"the sandbox could not be reached ({e})")

    @staticmethod
    def _announce(session_id: Optional[Any], written) -> None:
        """The chat shows what arrived, like an export from python_sandbox."""
        if not session_id:
            return
        try:
            from vaf.core.web_interface import notify_file_created
            for path in list(written)[:5]:
                notify_file_created(session_id, path)
        except Exception:
            pass


class SandboxPreviewTool(BaseTool):
    name = "sandbox_preview"
    category = "code"
    permission_level = "write"                 # saves the screenshot into the chat workspace
    side_effect_class = "reversible"
    channel_restrictions = ("channel",)
    identity_kwargs = ("user_scope_id", "username", "user_role", "session_id")
    description = (
        "Look at a page one of your sandbox environments serves or holds: a screenshot "
        "(saved into the chat workspace and shown in the chat), the console output, page "
        "errors and the rendered text. target is a URL (localhost means the environment "
        "itself, so a dev server started with sandbox_exec background=true is reachable "
        "whatever address it binds) or a path under /workspace. Taken by a headless browser "
        "inside the environment; no clicking or forms."
    )
    parameters = {
        "type": "object",
        "properties": {
            "environment": {"type": "string", "description": "The environment's id."},
            "target": {"type": "string",
                       "description": "e.g. http://localhost:5173/ or index.html (under /workspace)."},
            "width": {"type": "integer", "description": "Viewport width (default 1280)."},
            "height": {"type": "integer", "description": "Viewport height (default 800)."},
            "wait_ms": {"type": "integer", "description": "Time for scripts to run before the shot (default 1500, max 10000)."},
        },
        "required": ["environment", "target"],
    }

    def run(self, **kwargs) -> str:
        from vaf.core.environments import EnvironmentRefused
        from vaf.tools.render_check import RenderCheckTool
        try:
            result = _manager().render(
                kwargs.get("user_scope_id"), str(kwargs.get("environment") or ""),
                str(kwargs.get("target") or ""), width=int(kwargs.get("width") or 1280),
                height=int(kwargs.get("height") or 800), wait_ms=int(kwargs.get("wait_ms") or 1500))
        except EnvironmentRefused as e:
            return _refused(self.name, e)
        except Exception as e:
            return _refused(self.name, f"the sandbox could not be reached ({e})")
        # The same developer's report render_check gives, from the same formatter.
        return RenderCheckTool.__new__(RenderCheckTool)._format(result)
