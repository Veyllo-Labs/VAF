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

Every stdio request has a deadline (`call_stdio(timeout=...)`, `list_server_tools(timeout=...)`,
by default the tool budget `tool_timeout_seconds`) that covers the start, the handshake, the
write and the reads, and the caller's cancel flag ends it too (`run_bounded`'s budget or Stop,
`cancel_when`). A request that misses it retires its server process, which frees the lock and
the stream for the next call. Measured before: a non-JSON answer, a JSON scalar, an answer
holding a raw ``\\r``, a server whose unread stderr had filled up, and a large request to a
server that stopped reading each held the call, and the server's lock, forever. The pipes are
read and written on helper threads (`_ServerPipe`), without `select`, so the same holds on a
Windows pipe.

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

import contextlib
import itertools
import json
import subprocess
import threading
import time
from concurrent.futures import Future, wait as _wait_futures

from vaf.version import __version__
import sys
import os
from typing import Callable, Dict, Any, Optional, List
from pathlib import Path

from vaf.tools.base import BaseTool
from vaf.cli.ui import UI

# Seconds a stopped server gets between SIGTERM and SIGKILL.
_STOP_GRACE = 2.0
# Seconds `stop_all_servers` waits for server starts already under way.
_LAUNCH_WAIT = 1.0
# Seconds between two looks at the caller's cancel flag while a request waits on its server.
_IO_POLL = 0.25
# The longest output line read, in bytes. A line is held in memory whole and decoding it as
# JSON takes about five to six times its size at peak (measured), so a server that wrote one
# without end grew the application by about 0.9 GB/s until the deadline. 64 MiB still holds a
# file of 48 MB returned as base64 inside one result, larger than any tool result a model can
# take in, and bounds that peak at about 400 MB.
_MAX_LINE = 64 * 1024 * 1024
# The JSON-RPC error code of a request that got no answer in time (the TypeScript SDK's
# REQUEST_TIMEOUT; the Python SDK names the same value).
_REQUEST_TIMEOUT = -32001
# What `_await` saw.
_DONE, _CANCELLED, _TIMED_OUT = "done", "cancelled", "timed out"

# The words the MCP SDK's model (pydantic's lax bool) reads as false, in any case.
_FALSE_WORDS = frozenset({"false", "f", "no", "n", "off", "0"})


def _reports_error(flag: Any) -> bool:
    """Whether a result's `isError` says the tool failed, read as the remote lane reads it.

    The remote lane validates results through the SDK's `CallToolResult`, whose bool field
    takes True or False, the numbers 1 and 0, and the words true/t/yes/y/on/1 and the
    false words above, in any case; anything else fails the validation, and that call ends
    as an error. Measured against the SDK: `"true"`, `1` and `"yes"` are true, `" true"`,
    `2` and `None` are refused. A refused value reads as an error here too, so the same
    server's failure is never booked as a success on one lane and not the other."""
    if isinstance(flag, bool):
        return flag
    if isinstance(flag, (int, float)):
        return flag != 0
    if isinstance(flag, str):
        return flag.lower() not in _FALSE_WORDS
    return True


def _is_os_process(process: Any) -> bool:
    """A real child process (it has a pid), as opposed to a stand-in with only the methods."""
    pid = getattr(process, "pid", None)
    return isinstance(pid, int) and not isinstance(pid, bool)


def _stop_process_tree(process: subprocess.Popen, grace: float = _STOP_GRACE) -> None:
    """Stop a server process and everything it started, then reap it. Never raises.

    The same stop the sub-agent children and the background host commands get
    (`Platform.terminate_process_tree`). A leader that has already exited is past
    signalling by pid (its pid may belong to another process by now), so only the group
    it led is signalled then (`Platform.terminate_process_group`)."""
    try:
        from vaf.core.platform import Platform
        pgid = getattr(process, "_vaf_pgid", None)
        if process.poll() is not None:
            Platform.terminate_process_group(pgid, grace)
        else:
            Platform.terminate_process_tree(process.pid, grace, pgid=pgid)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass
    try:
        process.wait(timeout=grace)
    except Exception:
        pass


def _request_timeout(timeout: Any) -> float:
    """``timeout`` in seconds, or the tool budget (``tool_timeout_seconds``) when it is None or
    not a positive finite number (`bounded_run.seconds_or_default`)."""
    from vaf.core.bounded_run import seconds_or_default
    return seconds_or_default(timeout)


def _await(future: Future, deadline: float) -> str:
    """Wait for ``future`` until the monotonic ``deadline``: `_DONE`, `_TIMED_OUT`, or
    `_CANCELLED` once the caller's cancel flag is up (`bounded_run.cancel_requested`, set by
    `run_bounded` at its budget or Stop, and by `cancel_when`). The flag is looked at every
    `_IO_POLL` seconds, so a caller that gave up frees the server within that time."""
    from vaf.core.bounded_run import cancel_requested
    while True:
        if future.done():
            return _DONE
        if cancel_requested():
            return _CANCELLED
        left = deadline - time.monotonic()
        if left <= 0:
            return _TIMED_OUT
        _wait_futures([future], timeout=min(_IO_POLL, left))


class _ServerPipe:
    """The stdin and stdout of one local server process, written and read on helper threads,
    so the thread that owns a request can stop waiting at its deadline (`_await`).

    Each operation runs on a daemon thread of its own (``vaf-mcp-pipe``) that ends when its
    I/O ends, so no thread idles between requests. A blocking pipe read or write cannot be
    bounded portably otherwise: `select` does not take a Windows anonymous pipe, and a
    non-blocking pipe needs `fcntl` or `os.set_blocking`, which do not exist for one there
    either. A thread and a future behave the same on both.

    At most one read is in flight. A read whose wait gave up is not lost: the next
    `next_line` hands out the same future until `take` collects its line, so a line is never
    read twice or skipped. A read returns at most `_MAX_LINE` bytes. A failed write resolves to
    False, a failed read to the end of the stream (``b""``). Kept on the process as
    ``_vaf_pipe`` (`_pipe_for`), and holding the process's two streams, never the process: a
    pipe that held its process made a reference cycle, so a retired process and its pipe
    descriptors (and its last line) stayed until a full garbage collection (measured with
    collection off: 300 timed-out calls left 600 descriptors open).

    Named boundary: a descendant that keeps the output pipe open after the server was stopped
    leaves the read thread blocked until it closes the pipe; neither the request nor the
    server's lock waits for that thread."""

    def __init__(self, process) -> None:
        self._stdin = getattr(process, "stdin", None)
        self._stdout = getattr(process, "stdout", None)
        self._guard = threading.Lock()
        self._reading: Optional[Future] = None

    @staticmethod
    def _start(work: Callable[[], Any], failed: Any) -> Future:
        """``work()`` on a helper thread; its future resolves to the result, or to ``failed``
        when the work raises or no thread could be started. Never raises."""
        future: Future = Future()

        def _run() -> None:
            try:
                value = work()
            except Exception:
                value = failed
            future.set_result(value)

        try:
            threading.Thread(target=_run, name="vaf-mcp-pipe", daemon=True).start()
        except Exception:
            future.set_result(failed)
        return future

    def send(self, data: bytes) -> Future:
        """Write ``data`` and flush it; resolves to True once written, False on failure."""
        stdin = self._stdin

        def _write() -> bool:
            stdin.write(data)
            stdin.flush()
            return True

        return self._start(_write, False)

    def next_line(self) -> Future:
        """The read of the next output line, at most `_MAX_LINE` bytes, ``b""`` at the end of the
        stream: the one still in flight, or a new one."""
        with self._guard:
            if self._reading is None:
                stdout = self._stdout
                self._reading = self._start(lambda: stdout.readline(_MAX_LINE), b"")
            return self._reading

    def take(self, future: Future) -> Any:
        """The line a finished `next_line` read; the next `next_line` reads a new one."""
        with self._guard:
            if self._reading is future:
                self._reading = None
        return future.result()


_pipes_guard = threading.Lock()


def _pipe_for(process) -> _ServerPipe:
    """The `_ServerPipe` of ``process``, made on first use and kept on the process (as
    ``_vaf_pgid`` is), so every request on it shares the one read in flight."""
    with _pipes_guard:
        pipe = getattr(process, "_vaf_pipe", None)
        if pipe is None:
            pipe = _ServerPipe(process)
            with contextlib.suppress(AttributeError, TypeError):
                process._vaf_pipe = pipe
        return pipe


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
    # Offered on another agent's request, and decided per call (`agent_request_for`) by the
    # level of the server tool it reaches.
    agent_request = "allow"
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
        # Guards every write to the process cache, `_stopping`, `_closing` and `_launching`,
        # and the identity check before an eviction. Held only for those few lines, never
        # across a read, a start or a stop. Reentrant: `stop_all_servers` also runs inside a
        # signal handler (`Agent.shutdown`), which interrupts the main thread between two
        # bytecodes, possibly inside one of those lines; a plain lock would deadlock there.
        self._state_lock = threading.RLock()
        self._state_changed = threading.Condition(self._state_lock)
        # Processes whose stop runs on a daemon thread right now (`_retire`), out of the cache
        # but not yet gone: `stop_all_servers` finishes them, since the interpreter does not
        # wait for a daemon thread at exit.
        self._stopping: set = set()
        # Set by `stop_all_servers`: no server is started any more, and one whose start was
        # under way goes to `_stopping` instead of the cache.
        self._closing = False
        self._launching = 0
        # Set once `stop_all_servers` took its list: a start that ends after that stops its
        # server itself.
        self._swept = False
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

    def agent_request_for(self, args: Dict[str, Any]) -> str:
        """On another agent's request, the same answer the server's own registered tool gives
        (`mcp_registry.make_mcp_tool`): open for a tool the administrator set to read, refused
        for every other one, and for a call that names no registered server (a remote URL: its
        tools' levels are unknown). Reads the arguments the way `run` does. Fail closed."""
        try:
            from vaf.core import mcp_registry
            server = str(args.get("server") or args.get("server_command") or "").strip()
            found = mcp_registry.registered_server(server) if server else None
            if found is None:
                return "refuse_person"
            _name, cfg = found
            level = mcp_registry.tool_permission(cfg, str(args.get("tool_name") or "").strip())
            return "allow" if level == "read" else "refuse_person"
        except Exception:
            return "refuse_person"

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
    
    def _ensure_server(self, server_command: str, env: Optional[Dict[str, str]] = None, *,
                       deadline: Optional[float] = None, timeout: Optional[float] = None,
                       why: Optional[List[str]] = None) -> Optional[subprocess.Popen]:
        """Spawn (if needed) and initialize the stdio MCP server, cached by command. Returns the
        running process, or None on failure. Shared by mcp_call, discovery, and dynamic MCP tools.
        `env` (e.g. API tokens from the manifest) is merged onto os.environ for the child process.
        Called under the server's lock.

        ``deadline`` (monotonic) is the request's, set by the caller once it holds the lock, and
        ``timeout`` the length it was set from (`_request_timeout`; without a deadline, one is
        set from it here). The handshake and ``notifications/initialized`` wait for it at
        most; a server that misses it is retired and None returned, and ``why`` (a list, when
        given) gets the deadline's own words, "no answer within N s". Nothing the server wrote
        is put there.

        The pipes are binary and the output is decoded as UTF-8 (`_json_rpc`); stderr goes to
        the null device, since nothing reads it and a server whose log filled the pipe (64 KB
        on Linux) blocked on its next write and never answered again.

        The server leads its own process group (`processes.own_process_group`), so stopping it
        reaches what it started: a launcher (a shell script, a wrapper) whose child inherits
        the output pipe keeps it open after the launcher alone is gone."""
        timeout = _request_timeout(timeout)
        if deadline is None:
            deadline = time.monotonic() + timeout
        cache_key = server_command
        proc = self._server_processes.get(cache_key)
        if proc is not None and proc.poll() is None:
            return proc
        if proc is not None:
            # The cached server has exited, but what it started may still run in its group (a
            # launcher that left its child behind). Its group is stopped, and waited for, before
            # the replacement starts: two would otherwise overlap on what the old one still
            # holds (a port, a lock file). Bounded by the stop's grace and a SIGKILL. For that
            # time it is in `_stopping`, entered before it leaves the cache (see `_retire`), so
            # an exit in the window still stops its group.
            if _is_os_process(proc):
                with self._state_lock:
                    self._stopping.add(proc)
                    self._evict(proc)
                try:
                    _stop_process_tree(proc)
                finally:
                    with self._state_lock:
                        self._stopping.discard(proc)
            else:
                self._evict(proc)
        with self._state_lock:
            if self._closing:
                return None  # VAF is stopping its servers: no new one
            self._launching += 1
        # (Re)spawn the server process.
        process = None
        try:
            cmd_parts = server_command.split() if isinstance(server_command, str) else server_command
            from vaf.core.processes import own_process_group
            popen_kwargs = {
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                # The stdio transport lets a server log to stderr and a client ignore it; a
                # pipe nobody reads stops the server once it is full (docstring above).
                "stderr": subprocess.DEVNULL,
                # POSIX: start_new_session; Windows: a new process group, without a window.
                **own_process_group(),
            }
            if env:
                popen_kwargs["env"] = {**os.environ, **{str(k): str(v) for k, v in env.items()}}
            process = subprocess.Popen(cmd_parts, **popen_kwargs)
            # The group the server leads, recorded now: after the leader has exited, only the
            # group is signalled, never its pid, which may belong to another process by then.
            process._vaf_pgid = process.pid if os.name != "nt" else None
        except Exception:
            process = None
        finally:
            with self._state_lock:
                closing, swept = self._closing, self._swept
                # Registered before it stops counting as under way: a stop of all servers that
                # no longer waits for it (a signal handler on this very thread) finds it.
                if process is not None:
                    if closing:
                        # Started while VAF stops its servers, never handshaken: handed to
                        # that stop, which waits for the starts under way.
                        self._stopping.add(process)
                    else:
                        self._server_processes[cache_key] = process
                self._launching -= 1
                self._state_changed.notify_all()
        if process is not None and swept:
            # That stop gave up waiting (`_LAUNCH_WAIT`) and took its list before this start
            # ended: nobody else stops this server, so this start does, before it returns.
            # Without a grace: the server was never handshaken and nobody uses it, and this
            # thread, a daemon one as a rule, is frozen when the interpreter finishes, so a
            # grace would leave a server that ignores SIGTERM without its SIGKILL.
            try:
                _stop_process_tree(process, grace=0)
            finally:
                with self._state_lock:
                    self._stopping.discard(process)
        if process is None or closing:
            return None
        # A caller that gave up while the server started gets no handshake: the fresh process
        # is retired before any I/O, so nothing waits on a server nobody asked for any more.
        from vaf.core.bounded_run import cancel_requested
        if cancel_requested():
            self._retire(process)
            return None
        # Initialize handshake. A server that misses the deadline, or whose caller gives up
        # during it, is retired inside `_json_rpc`; the retirement below is then a no-op.
        init = self._json_rpc(process, "initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "VAF", "version": __version__},
        }, request_id=next(self._request_ids), deadline=deadline, timeout=timeout)
        if init is None or "error" in init:
            error = (init or {}).get("error")
            if why is not None and isinstance(error, dict) and error.get("code") == _REQUEST_TIMEOUT:
                why.append(str(error.get("message", "")))
            self._retire(process)
            return None
        # Per the MCP spec, signal initialized. A failed write is left to the request after
        # it, as before; a write that is still blocked at the deadline retires the server.
        note = (json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        if _await(_pipe_for(process).send(note.encode("utf-8")), deadline) != _DONE:
            if why is not None and time.monotonic() >= deadline:
                why.append(f"no answer within {timeout:g} s")
            self._retire(process)
            return None
        self._server_initialized[cache_key] = True
        return process

    def _evict(self, process) -> bool:
        """Drop `process` from the cache wherever it is cached, by identity, atomically with
        the check: a replacement cached under the same command in the meantime stays."""
        found = False
        with self._state_lock:
            for key in list(self._server_processes):
                if self._server_processes.get(key) is process:
                    self._server_processes.pop(key, None)
                    found = True
        return found

    def _retire(self, process) -> None:
        """Drop a server process from the cache and stop it, so the next call starts a fresh
        one. Returns at once, with or without the server's lock held: the stop (SIGTERM to
        the process group, a grace of `_STOP_GRACE` seconds, then SIGKILL, and the reaping)
        runs on a daemon thread, because a server that ignores SIGTERM held its caller for
        the grace (measured: a 1 s connection test took 6 s). The process stays in
        `_stopping` until that thread is done, so `stop_all_servers` can finish it at exit,
        where the thread is frozen before its SIGKILL. It enters `_stopping` and then leaves
        the cache, in one hold of `_state_lock`: another thread never looks between the
        two, and a signal handler on this thread, which can, finds it in both rather than in
        neither (`stop_all_servers` stops it once).

        A process is retired once (``_vaf_retired``, set under `_state_lock`): a request that
        missed its deadline in the handshake retires it in `_json_rpc`, and the handshake's
        own failure path then asks again. A second stop would signal a group whose leader
        the first may have reaped, so its id may name another group by then. Never raises."""
        with self._state_lock:
            if getattr(process, "_vaf_retired", False):
                return
            with contextlib.suppress(AttributeError, TypeError):
                process._vaf_retired = True
            if _is_os_process(process):
                self._stopping.add(process)
            self._evict(process)
        if not _is_os_process(process):
            try:
                process.terminate()
            except Exception:
                pass
            return

        def _stop() -> None:
            try:
                _stop_process_tree(process)
            finally:
                with self._state_lock:
                    self._stopping.discard(process)

        try:
            threading.Thread(target=_stop, daemon=True, name="mcp-server-stop").start()
        except Exception:
            _stop()

    def stop_all_servers(self) -> None:
        """Stop every local server this client started that may still run (at exit): the
        cached ones and the ones whose stop is still in flight (`_stopping`). The servers
        lead their own process group, so a terminal's Ctrl+C no longer reaches them with
        VAF's; this does, with a short grace before SIGKILL.

        No server is started from here on (`_closing`), and a start already under way is
        waited for (at most `_LAUNCH_WAIT` seconds) and then stopped with the rest, without
        its handshake. A cached server whose leader has already exited is stopped by its
        group: a launcher can leave a child behind in it. That is as safe as for a live one,
        because nothing else polls a cached process (`_ensure_server` evicts one the moment
        its poll says it exited), so the leader is an unreaped zombie until the poll in the
        stop, and its group id cannot be handed out again before that. A stop in flight is
        finished, group included. A process that is in both places (`_retire` enters
        `_stopping` before it leaves the cache) is stopped once.

        A start that outlasts the wait is not in the list; it kills its own server and its
        group when it ends (`_swept`), at once and without a grace, since its thread does
        not outlive the interpreter. Named boundary: a process that leaves before that start
        ends (a normal exit once its exit handlers are done, or `os._exit`, as
        `Agent.shutdown` does after this) never lets it end, and a signal handler that runs
        this on the very thread that is starting a server waits out `_LAUNCH_WAIT` for a
        start it interrupted; such a server outlives VAF unless it exits on the end of its
        stdin."""
        with self._state_lock:
            self._closing = True
            self._state_changed.wait_for(lambda: self._launching <= 0, timeout=_LAUNCH_WAIT)
            self._swept = True
            cached = list(self._server_processes.values())
            in_flight = [p for p in self._stopping if not any(p is c for c in cached)]
        for process in cached:
            self._evict(process)
            if _is_os_process(process):
                _stop_process_tree(process, grace=0.5)
            with self._state_lock:
                self._stopping.discard(process)
        for process in in_flight:
            _stop_process_tree(process, grace=0.5)
            with self._state_lock:
                self._stopping.discard(process)

    def _json_rpc(self, process: subprocess.Popen, method: str, params: Dict[str, Any],
                  request_id: int, max_reads: int = 20, *, deadline: Optional[float] = None,
                  timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """Send a JSON-RPC request and return the response object matching request_id, skipping
        notifications / unrelated lines. Returns None on EOF or error (never raises).

        The write and every read wait until the monotonic ``deadline`` at most (without one,
        ``timeout`` seconds from now; `_request_timeout`), through the process's `_ServerPipe`.
        At the deadline the process is retired and the answer is the error -32001, "no answer
        within N s" with ``timeout`` as N. A deadline already past when the request would be
        written retires the process the same way and writes nothing, so a tool never runs
        after its time is up (a handshake can use up the whole budget). When the caller gives
        up first (its cancel flag, `_await`), the process is retired and the answer is None.
        Retiring is what frees the stream: the read or write left behind belongs to that
        process, and the next request starts a fresh one. A write still blocked counts the
        same as a read: a server that stopped reading its input held a large request's write
        forever. The end of the output, and a write that failed, retire the process too: its
        pipe cannot be used again, and a server that closed its output while it lived on
        failed every later call until VAF restarted.

        A line is read up to `_MAX_LINE` bytes. One that reaches the limit without its line
        feed ends the request with the error -32700 and retires the process, since the rest of
        the line is still on the stream.

        The output is read as bytes, split only on ``b"\\n"`` and decoded as UTF-8, so a raw
        ``\\r``, which JSON allows as whitespace, stays inside its line, and a ``\\r\\n`` ending
        is stripped with the rest. Bytes that are not UTF-8 are replaced, so a log line of
        them is skipped as text that is not JSON, never taken for an unreadable answer.

        A stdout line that is not JSON (log text) or is JSON but not an object (a stray
        ``print(1)``, a status string, a batch, which VAF never sends) is skipped: a JSON-RPC
        message is an object. A line that is JSON the decoder cannot read, nested deeper than
        it allows or holding an integer past Python's digit limit, may be the answer itself,
        so it ends the request with a parse error (-32700) instead: skipping it would leave
        the reader waiting for an answer that already came, until the deadline. Such a line
        also retires the process (`_retire`), because the stream may still hold output nobody
        can attribute. Callers hold the server's lock."""
        timeout = _request_timeout(timeout)
        if deadline is None:
            deadline = time.monotonic() + timeout
        pipe = _pipe_for(process)
        try:
            req = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            data = (json.dumps(req) + "\n").encode("utf-8")
        except Exception:
            return None
        if time.monotonic() >= deadline:
            return self._gave_up(process, _TIMED_OUT, request_id, timeout)
        sent = pipe.send(data)
        state = _await(sent, deadline)
        if state != _DONE:
            return self._gave_up(process, state, request_id, timeout)
        if not sent.result():
            self._retire(process)
            return None
        for _ in range(max_reads):
            reading = pipe.next_line()
            state = _await(reading, deadline)
            if state != _DONE:
                return self._gave_up(process, state, request_id, timeout)
            raw = pipe.take(reading)
            if not raw:
                self._retire(process)
                return None
            if isinstance(raw, (bytes, bytearray)):
                if len(raw) >= _MAX_LINE and not raw.endswith(b"\n"):
                    self._retire(process)
                    return {"jsonrpc": "2.0", "id": request_id,
                            "error": {"code": -32700,
                                      "message": "the MCP server sent a line longer than "
                                                 f"{_MAX_LINE // (1024 * 1024)} MiB"}}
                raw = bytes(raw).strip()
                line = raw.decode("utf-8", errors="replace") if raw else ""
            else:
                line = str(raw).strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # not JSON: log text on stdout
            except (ValueError, RecursionError) as exc:
                # The line has no id anybody can read, so it may be this answer or the
                # leftover of an earlier request, and what follows it on the stream belongs
                # to nobody now: the next request would read it first. The process is
                # retired here, under the server's lock, so the next request starts a
                # fresh one on a clean stream.
                self._retire(process)
                return {"jsonrpc": "2.0", "id": request_id,
                        "error": {"code": -32700,
                                  "message": "the MCP server sent a line that cannot be read "
                                             f"({str(exc)[:200]})"}}
            if not isinstance(msg, dict):
                continue  # a JSON-RPC message is an object; a scalar or a batch is not our answer
            if msg.get("id") == request_id:
                return msg
            # Otherwise a notification or another response — keep reading.
        return None

    def _gave_up(self, process, state: str, request_id: int,
                 timeout: float) -> Optional[Dict[str, Any]]:
        """A request that stopped waiting (`_await`): the process is retired, since its stream
        still holds the write or read left behind; the answer is None for a caller that gave
        up, and the error -32001 at the deadline (the remote lane's words)."""
        self._retire(process)
        if state == _CANCELLED:
            return None
        return {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": _REQUEST_TIMEOUT, "message": f"no answer within {timeout:g} s"}}

    def list_server_tools(self, server_command: str, transport: str = "stdio",
                          server_url: str = "", env: Optional[Dict[str, str]] = None,
                          headers: Optional[Dict[str, str]] = None, auth=None,
                          egress=None, *, timeout: Optional[float] = None) -> List[Dict[str, Any]]:
        """Discover the tools a server offers via tools/list. Returns a list of tool dicts (each
        with name / description / inputSchema), or [] when the server has none or could not be
        asked. A remote server's reason is raised as RemoteMcpError for the caller to show.

        A local listing has the deadline `call_stdio` has (``timeout``; None: the tool budget,
        ``tool_timeout_seconds``), counted from the moment it has the server's lock: the start,
        the handshake and the listing included, with the same named boundary. At that deadline, or once its caller gives up
        (the cancel flag of `bounded_run`, also armed by `cancel_when`), the process is retired
        and the listing returns []. A listing whose caller gave up while it waited for the lock
        returns [] without starting or asking a server. A remote listing keeps the remote
        lane's own deadlines and ignores ``timeout``."""
        if transport in ("http", "sse"):
            if not server_url:
                return []
            from vaf.core.mcp_remote import get_remote_pool
            return get_remote_pool().list_tools(transport, server_url, headers=headers, auth=auth,
                                                egress=egress)
        if transport != "stdio":
            return []
        from vaf.core.bounded_run import cancel_requested
        timeout = _request_timeout(timeout)
        try:
            with self._lock_for(server_command):
                deadline = time.monotonic() + timeout
                if cancel_requested():
                    return []
                process = self._ensure_server(server_command, env, deadline=deadline,
                                              timeout=timeout)
                if process is None:
                    return []
                # Asked again once the server is up: the caller may have given up on a cold
                # start, and then the listing is not sent.
                if cancel_requested():
                    return []
                resp = self._json_rpc(process, "tools/list", {},
                                      request_id=next(self._request_ids), deadline=deadline,
                                      timeout=timeout)
            if not resp or "error" in resp or "result" not in resp:
                return []
            tools = resp["result"].get("tools", [])
            return tools if isinstance(tools, list) else []
        except Exception:
            return []

    def call_stdio(self, server_command: str, tool_name: str, arguments: Dict[str, Any],
                   env: Optional[Dict[str, str]] = None, *,
                   timeout: Optional[float] = None) -> str:
        """Call an MCP tool via stdio transport (JSON-RPC over stdin/stdout), starting
        `server_command` if it is not running yet. For code that chose the command itself (the
        registry runs a registered server's): a command from a model's arguments goes through
        `run`, which starts only a registered one.

        Returns the tool's text, or an ``Error:`` / ``MCP Error:`` line, and does not raise for
        anything the server writes. Its deadline is ``timeout`` seconds (None, or a value that
        is not a positive finite number: the tool budget, ``tool_timeout_seconds``) from the
        moment it has the server's lock, and covers a fresh start, the handshake, the write and
        the reads. At that deadline the process is stopped and dropped, the lock is free, the
        next call starts a fresh process, and this call answers ``MCP Error: no answer within N
        s (code: -32001)``, or ``Error: Failed to initialize MCP server (no answer within N
        s)`` when the deadline passed during the handshake. Under `run_bounded` the caller's
        own timeout or Stop ends it the same way within `_IO_POLL` seconds (its cancel flag).

        Named boundary: two bounded steps can run past the deadline. Replacing a cached
        process that has exited first stops what it left in its group (up to `_STOP_GRACE`
        seconds and a SIGKILL); a request whose deadline that used up then writes nothing.
        And the line read last is decoded as a whole, which for a line near `_MAX_LINE` of
        deeply nested JSON takes about two seconds (measured: 0.033 s per MB)."""
        from vaf.core.bounded_run import cancel_requested
        cancelled = "Error: the MCP call was cancelled before it reached the server"
        timeout = _request_timeout(timeout)
        with self._lock_for(server_command):
            # The time spent waiting for the lock is the caller's own; the server's starts now.
            deadline = time.monotonic() + timeout
            # A caller that gave up while this call waited for the lock (run_bounded's
            # timeout or stop) has already been told the call did not finish, so the call
            # must not reach the server any more. Asked again once the server is up: a
            # cold start (spawn and handshake) can outlast the caller's budget. A call
            # that got past the second check before its caller gave up still writes.
            if cancel_requested():
                return cancelled
            why: List[str] = []
            process = self._ensure_server(server_command, env, deadline=deadline, timeout=timeout,
                                          why=why)
            if process is None:
                # The handshake's deadline is named; any other failure keeps the plain line.
                return "Error: Failed to initialize MCP server" + (f" ({why[0]})" if why else "")
            if cancel_requested():
                return cancelled
            response = self._json_rpc(process, "tools/call",
                                      {"name": tool_name, "arguments": arguments or {}},
                                      request_id=next(self._request_ids), deadline=deadline,
                                      timeout=timeout)
        if response is None:
            return "Error: No response from MCP server"
        # The answer's shape is the server's: anything outside the specification's shapes
        # comes back as text rather than raising out of this call.
        if "error" in response:
            error = response["error"]
            if not isinstance(error, dict):
                return f"MCP Error: {str(error)[:500]} (code: unknown)"
            return f"MCP Error: {error.get('message', 'Unknown error')} (code: {error.get('code', 'unknown')})"
        if "result" not in response:
            return "Error: No result in MCP response"

        result = response["result"]
        # A tool that failed says so in `isError`, whatever its content looks like; the line
        # is the remote lane's (`mcp_remote.error_line`), which the shared result classifier
        # reads as a failure (`context.tool_result_is_error`), with its fallback when the
        # content carries no message (an empty array, text that is empty or only
        # whitespace), not the result's own dump.
        if isinstance(result, dict) and "isError" in result and _reports_error(result["isError"]):
            from vaf.core.mcp_remote import error_line
            if isinstance(result.get("content"), list):
                return error_line(self._content_text(result["content"]))
            return error_line(self._result_text(result))
        return self._result_text(result)

    @staticmethod
    def _content_text(content: List[Any]) -> Optional[str]:
        """The text items of a content array joined, or None when it has none."""
        text_parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    text = item.get("text", "")
                    text_parts.append(text if isinstance(text, str) else str(text))
                elif "text" in item:
                    text_parts.append(str(item["text"]))
        return "\n".join(text_parts) if text_parts else None

    @classmethod
    def _result_text(cls, result: Any) -> str:
        """The text of a `tools/call` result: the text items joined, else the result as text."""
        # MCP tools return a content array.
        if isinstance(result, dict) and isinstance(result.get("content"), list):
            text = cls._content_text(result["content"])
            if text is not None:
                return text
        return str(result)
    
    def __del__(self):
        """Cleanup: stop all server processes on tool destruction."""
        try:
            self.stop_all_servers()
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
        # The servers lead their own process group, out of reach of a terminal's Ctrl+C.
        import atexit
        atexit.register(_shared_client.stop_all_servers)
    return _shared_client


def stop_shared_servers() -> None:
    """Stop the shared client's local servers, if a shared client exists; never creates one.
    For an exit path that skips `atexit` (`os._exit` after a signal). Never raises."""
    client = _shared_client
    if client is None:
        return
    try:
        client.stop_all_servers()
    except Exception:
        pass

