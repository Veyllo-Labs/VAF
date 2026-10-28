# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
MCP Client Tool - Bridge to external Model Context Protocol servers

This tool allows VAF to interact with external MCP servers, enabling
integration with tools written in any language and following the MCP standard.

MCP (Model Context Protocol) uses JSON-RPC over stdio, Streamable HTTP or SSE. A local
server (stdio) is a process of ours, driven by the loop below; a remote one (http / sse) goes
through vaf/core/mcp_remote.py, which speaks the protocol through the official SDK.

The stdio loop is shared by every caller in the process (the raw `mcp_call`, discovery, the
registered `mcp_<server>_<tool>` tools), so one request at a time runs per server process, and
every request carries its own id. Before, all calls used the same id and nothing serialised
them: two chats calling the same server at once could each read the other's answer.

The raw `mcp_call` starts only a server an administrator registered (`mcp_servers.json`,
vaf/core/mcp_registry.py `registered_server`). Its arguments are the MODEL's, and it used to
start whatever `server_command` said, on the host, as the user VAF runs as: measured, a regular
account's turn created a file with `server_command="touch <file>"`, since the tool has no
`admin_only`, a write-level tool asks nobody, and the coder's lane asks nobody at all. The
refusal holds for every account, the admin's included, because the admin did not write the
command either. Code of your own that has chosen a command calls `call_stdio` directly.

A call on a registered server is held to the rules its own ``mcp_<server>_<tool>`` tool
carries, so this lane is no way around them:
- the account allowlist is asked for THAT tool's name (`caller_allows_tool`, which in the
  coder's child reads the answer that crossed as `VAF_ALLOWED_TOOLS`): a list that grants
  `mcp_call` and not `mcp_<server>_delete_file` does not run the delete here;
- `mcp_native_tools_enabled` off turns the registered servers off here too;
- a tool at level `dangerous` (it asks on every call) and a server that signs every account in
  (its tools run as the caller) are refused and stay with their own tools;
- in a coding run only `read`-level tools are reached, and no `server_url`: the coder's tools
  are for looking things up (vaf/core/coder_tools.py).

Named boundaries: `mcp_call` itself is not `admin_only`, not opt-in per account and not a
confirmation tool, because with the rules above it reaches nothing the account does not already
have as the registered tools, and it starts only the processes discovery starts. A coding run
is recognised by the agent type its child process is started with (`VAF_AGENT_TYPE`, also set
by a workflow for an in-process coding step); a coder run inline in the application's process
(`sub_agents_in_separate_terminals` off, or a terminal that could not be opened) carries no such
mark and is held to the account's rules only. A `server_url` (http / sse) the model passes
outside a coding run is still reached, under the instance destination policy and without any
stored token.
"""

import itertools
import json
import subprocess
import threading

from vaf.version import __version__
import sys
import os
from typing import Dict, Any, Optional, List
from pathlib import Path

from vaf.tools.base import BaseTool
from vaf.cli.ui import UI


class MCPClientTool(BaseTool):
    """
    Tool that acts as a bridge to external MCP servers.
    
    This allows VAF to use tools from the MCP ecosystem without
    requiring them to be rewritten as VAF tools.
    """
    
    name = "mcp_call"
    category    = "mcp"
    # Who is calling: the account allowlist is asked for the registered tool this call reaches.
    identity_kwargs = ("user_scope_id", "user_role")
    permission_level = "write"
    side_effect_class = "irreversible"
    description = ("Call a tool of an MCP (Model Context Protocol) server: one an administrator "
                   "registered (Settings > Advanced > MCP), named in `server`, or a remote server "
                   "without a login at `server_url`. An unregistered command is never started.")
    # A model that learned the old parameter keeps working, as long as what it names is a
    # registered server (`registered_server` also takes a local server's exact command).
    input_aliases = {"server": ["server_command", "server_name"]}

    parameters = {
        "type": "object",
        "properties": {
            "server": {
                "type": "string",
                "description": "Name of a registered MCP server (its tools are also listed as mcp_<server>_<tool>)"
            },
            "tool_name": {
                "type": "string",
                "description": "Name of the MCP tool to call"
            },
            "arguments": {
                "type": "object",
                "description": "Arguments to pass to the MCP tool (as key-value pairs)"
            },
            "transport": {
                "type": "string",
                "enum": ["stdio", "http", "sse"],
                "description": "Only with server_url: http (Streamable HTTP) or sse (the older SSE transport). A registered server uses its own transport.",
                "default": "stdio"
            },
            "server_url": {
                "type": "string",
                "description": "URL of a remote server that needs no login (http/sse). A server that needs one is registered in Settings instead, where its token is kept."
            }
        },
        "required": ["tool_name"]
    }
    
    def __init__(self):
        super().__init__()
        # Cache for active MCP server processes
        self._server_processes: Dict[str, subprocess.Popen] = {}
        self._server_initialized: Dict[str, bool] = {}
        # One request at a time per server process, each with its own id (module docstring).
        self._locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._request_ids = itertools.count(1)
    
    def run(self, **kwargs) -> str:
        """Call `tool_name` with `arguments` on the registered server `server` (a name, or a local
        server's exact command; `server_command` is the old name of the field), or on the remote
        server at `server_url` over `transport` (http / sse). Returns the tool's text."""
        server = str(kwargs.get("server") or kwargs.get("server_command") or "").strip()
        # Stripped once, for every check AND the call: "echo " would otherwise miss the
        # per-tool level of "echo" and still reach it.
        tool_name = str(kwargs.get("tool_name") or "").strip()
        arguments = kwargs.get("arguments", {})
        transport = kwargs.get("transport", "stdio")
        server_url = kwargs.get("server_url", "")
        scope, role = kwargs.get("user_scope_id"), kwargs.get("user_role")

        if not tool_name:
            return "Error: tool_name is required"
        try:
            if server:
                return self._call_registered(server, tool_name, arguments, scope, role)
            if _in_coding_run():
                return ("Error: in a coding run, mcp_call reaches only the read-level tools of a "
                        "registered MCP server; name one in `server`.")
            if transport in ("http", "sse"):
                if not server_url:
                    return "Error: server_url is required for the http and sse transports"
                return self.call_remote(transport, server_url, tool_name, arguments)
            if server_url:
                return "Error: a server_url needs the transport http or sse"
            if transport == "stdio":
                return self._refuse_unregistered("", scope, role)
            return f"Error: Unsupported transport '{transport}'"
        except Exception as e:
            return f"Error calling MCP tool: {e}"

    def _call_registered(self, server: str, tool_name: str, arguments: Dict[str, Any],
                         scope: Optional[str] = None, role: Optional[str] = None) -> str:
        """The model's call on a registered server, through the same path as that server's own
        registered tools, and under the rules those tools carry (module docstring)."""
        from vaf.core.config import Config
        if not bool(Config.get("mcp_native_tools_enabled", True)):
            return ("Error: MCP servers are switched off on this installation "
                    "(mcp_native_tools_enabled).")
        from vaf.core import mcp_registry
        found = mcp_registry.registered_server(server)
        if found is None:
            return self._refuse_unregistered(server, scope, role)
        name, cfg = found
        own_tool = mcp_registry.native_tool_name(name, tool_name)
        # The account allowlist names the server's own tools, so it is asked for that name:
        # a list that grants mcp_call and not mcp_<server>_delete_file must not run it here.
        from vaf.core.tool_dispatch import caller_allows_tool
        try:
            allowed = caller_allows_tool(own_tool, scope, role)
        except Exception:
            return (f"Error: the account allowlist could not be read, so {own_tool} is refused. "
                    f"A broken guard must not quietly become no guard.")
        if not allowed:
            return (f"Error: the tool '{own_tool}' is not enabled for your account. An "
                    f"administrator can enable it in user management.")
        from vaf.core.mcp_oauth import uses_sign_in
        if uses_sign_in(cfg):
            return (f"Error: the MCP server '{name}' signs every account in on its own. Call its "
                    f"tool as {own_tool}, which runs as you.")
        level = mcp_registry.tool_permission(cfg, tool_name)
        if _in_coding_run() and level != "read":
            return (f"Error: in a coding run, mcp_call reaches only read-level MCP tools; "
                    f"'{tool_name}' on the MCP server '{name}' is {level}.")
        if level == "dangerous":
            return (f"Error: '{tool_name}' on the MCP server '{name}' asks for a confirmation on "
                    f"every call. Call it as {own_tool}.")
        return mcp_registry.call_registered_tool(name, cfg, tool_name, arguments or {})

    @staticmethod
    def _refuse_unregistered(server: str, scope: Optional[str] = None,
                             role: Optional[str] = None) -> str:
        from vaf.core.mcp_registry import native_tool_prefix, registered_server_names
        from vaf.core.tool_dispatch import caller_allowlist, policy_admin_flag
        names = registered_server_names()
        if scope and not policy_admin_flag(role, scope):
            # Only the servers this account has at least one tool of: a restricted account is
            # not told about the others.
            try:
                allowed = caller_allowlist(scope, role)
            except Exception:
                allowed = frozenset()
            if allowed is not None:
                names = [n for n in names
                         if any(t.startswith(native_tool_prefix(n)) for t in allowed)]
        shown = server if len(server) <= 80 else server[:77] + "..."
        what = f"'{shown}' is not one" if server else "name one in `server`"
        known = (f" Registered: {', '.join(names)}." if names
                 else " None is registered; an administrator adds one under Settings > Advanced > MCP.")
        return (f"Error: mcp_call reaches only an MCP server an administrator registered, never a "
                f"command of its own; {what}.{known}")

    def _lock_for(self, server_command: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(server_command, threading.Lock())

    def call_remote(self, transport: str, server_url: str, tool_name: str, arguments: Dict[str, Any],
                    headers: Optional[Dict[str, str]] = None, auth=None, egress=None) -> str:
        """Call a tool of a remote server (Streamable HTTP or SSE) through the shared session pool;
        `auth` (vaf.core.mcp_remote.RemoteAuth) is the account's own session at a server that
        signs every account in (vaf/core/mcp_oauth.py). `egress` is the destination policy: None
        (a URL the model chose) is the instance policy, which never reaches this machine; a
        registered server passes `mcp_remote.registered_egress(url)`."""
        from vaf.core.mcp_remote import RemoteMcpError, get_remote_pool
        try:
            return get_remote_pool().call_tool(transport, server_url, tool_name, arguments or {},
                                               headers=headers, auth=auth, egress=egress)
        except RemoteMcpError as exc:
            from vaf.core.mcp_remote import display_url
            return f"Error: MCP server at {display_url(server_url)}: {exc}"
    
    def _ensure_server(self, server_command: str, env: Optional[Dict[str, str]] = None) -> Optional[subprocess.Popen]:
        """Spawn (if needed) and initialize the stdio MCP server, cached by command. Returns the
        running process, or None on failure. Shared by mcp_call, discovery, and dynamic MCP tools.
        `env` (e.g. API tokens from the manifest) is merged onto os.environ for the child process."""
        cache_key = server_command
        proc = self._server_processes.get(cache_key)
        if proc is not None and proc.poll() is None:
            return proc
        # (Re)spawn the server process.
        try:
            cmd_parts = server_command.split() if isinstance(server_command, str) else server_command
            import platform
            popen_kwargs = {
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "bufsize": 1,
            }
            if env:
                popen_kwargs["env"] = {**os.environ, **{str(k): str(v) for k, v in env.items()}}
            if platform.system() == "Windows":
                popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            process = subprocess.Popen(cmd_parts, **popen_kwargs)
            self._server_processes[cache_key] = process
        except Exception:
            return None
        # Initialize handshake.
        init = self._json_rpc(process, "initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "VAF", "version": __version__},
        }, request_id=next(self._request_ids))
        if init is None or "error" in init:
            try:
                process.terminate()
            except Exception:
                pass
            self._server_processes.pop(cache_key, None)
            return None
        # Per the MCP spec, signal initialized (fire-and-forget notification).
        try:
            process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
            process.stdin.flush()
        except Exception:
            pass
        self._server_initialized[cache_key] = True
        return process

    def _json_rpc(self, process: subprocess.Popen, method: str, params: Dict[str, Any],
                  request_id: int, max_reads: int = 20) -> Optional[Dict[str, Any]]:
        """Send a JSON-RPC request and return the response object matching request_id, skipping
        notifications / unrelated lines. Returns None on EOF or error (never raises)."""
        try:
            req = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            process.stdin.write(json.dumps(req) + "\n")
            process.stdin.flush()
        except Exception:
            return None
        for _ in range(max_reads):
            try:
                line = process.stdout.readline()
            except Exception:
                return None
            if not line:
                return None
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == request_id:
                return msg
            # Otherwise a notification or another response — keep reading.
        return None

    def list_server_tools(self, server_command: str, transport: str = "stdio",
                          server_url: str = "", env: Optional[Dict[str, str]] = None,
                          headers: Optional[Dict[str, str]] = None, auth=None,
                          egress=None) -> List[Dict[str, Any]]:
        """Discover the tools a server offers via tools/list. Returns a list of tool dicts (each
        with name / description / inputSchema), or [] when the server has none or could not be
        asked. A remote server's reason is raised as RemoteMcpError for the caller to show."""
        if transport in ("http", "sse"):
            if not server_url:
                return []
            from vaf.core.mcp_remote import get_remote_pool
            return get_remote_pool().list_tools(transport, server_url, headers=headers, auth=auth,
                                                egress=egress)
        if transport != "stdio":
            return []
        try:
            with self._lock_for(server_command):
                process = self._ensure_server(server_command, env)
                if process is None:
                    return []
                resp = self._json_rpc(process, "tools/list", {}, request_id=next(self._request_ids))
            if not resp or "error" in resp or "result" not in resp:
                return []
            tools = resp["result"].get("tools", [])
            return tools if isinstance(tools, list) else []
        except Exception:
            return []

    def call_stdio(self, server_command: str, tool_name: str, arguments: Dict[str, Any],
                   env: Optional[Dict[str, str]] = None) -> str:
        """Call an MCP tool via stdio transport (JSON-RPC over stdin/stdout), starting
        `server_command` if it is not running yet. For code that chose the command itself (the
        registry runs a registered server's): a command from a model's arguments goes through
        `run`, which starts only a registered one."""
        with self._lock_for(server_command):
            process = self._ensure_server(server_command, env)
            if process is None:
                return "Error: Failed to initialize MCP server"
            response = self._json_rpc(process, "tools/call",
                                      {"name": tool_name, "arguments": arguments or {}},
                                      request_id=next(self._request_ids))
        if response is None:
            return "Error: No response from MCP server"
        if "error" in response:
            error = response["error"]
            return f"MCP Error: {error.get('message', 'Unknown error')} (code: {error.get('code', 'unknown')})"
        if "result" not in response:
            return "Error: No result in MCP response"

        result = response["result"]
        # MCP tools return a content array.
        if "content" in result:
            content = result["content"]
            if isinstance(content, list) and len(content) > 0:
                text_parts = []
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            text_parts.append(item.get("text", ""))
                        elif "text" in item:
                            text_parts.append(str(item["text"]))
                return "\n".join(text_parts) if text_parts else str(result)
            return str(result)
        return str(result)
    
    def __del__(self):
        """Cleanup: Close all server processes on tool destruction."""
        for process in self._server_processes.values():
            try:
                if process.poll() is None:  # Process still running
                    process.terminate()
                    process.wait(timeout=5)
            except Exception:
                pass


def _in_coding_run() -> bool:
    """Whether this call runs inside a coding agent's run: the agent type the sub-agent child
    is started with (vaf/core/subagent_spawn.py) and a workflow sets for an in-process coding
    step (vaf/workflows/engine.py). The coder may only LOOK things up through MCP
    (vaf/core/coder_tools.py, "reading, never writing")."""
    return (os.environ.get("VAF_AGENT_TYPE") or "").strip() == "coding_agent"


# Process-wide shared MCP client. mcp_call, discovery, and dynamic MCP tools all go through this so
# they reuse the same warm server processes (cached in _server_processes) instead of re-spawning.
_shared_client: Optional[MCPClientTool] = None


def get_mcp_client() -> MCPClientTool:
    """Return the process-wide shared MCPClientTool (created on first use)."""
    global _shared_client
    if _shared_client is None:
        _shared_client = MCPClientTool()
    return _shared_client

