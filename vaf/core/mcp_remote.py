# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Remote MCP servers: one live session per server, over the protocol as the specification
defines it, through the official `mcp` SDK.

What this replaces, measured: the HTTP path POSTed `{"name", "arguments"}` to
`<url>/tools/call` and `{}` to `<url>/tools/list` - no JSON-RPC, no `initialize`, no
`Mcp-Session-Id`, no `Authorization` - so no server that implements the specification ever
answered it, and the SSE path returned "not yet fully implemented". Hosted MCP servers are
remote and speak Streamable HTTP; none of them could be used.

The shape:
- One background thread runs one asyncio loop for every remote session in the process. A
  session is held open by a task of its own (the SDK's transports are anyio contexts, which
  must be entered and left in the same task); calls from any thread are submitted to the loop
  and waited for with a timeout. The caller's thread blocks, the loop never does.
- A session is keyed by transport, URL and a digest of the headers, so a changed token opens a
  new one. A session idle for `IDLE_PING_SECONDS` is pinged before it is used (a ping is
  idempotent); if the ping fails, the session is reopened. A `tools/call` is never retried: a
  tool may have acted before its answer was lost, and doing it twice is worse than an error.
- `transport` is "http" (Streamable HTTP, the current transport) or "sse" (the older one).

stdio servers are not here: they are local processes and keep their own loop in
vaf/tools/mcp_client.py. Named boundary: that loop is hand-written and works; moving it onto
the SDK is a separate change with its own risk (Windows process flags, the warm-process cache).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

TRANSPORTS = ("http", "sse")
IDLE_PING_SECONDS = 60.0
CONNECT_TIMEOUT_SECONDS = 20.0
CALL_TIMEOUT_SECONDS = 120.0


class RemoteMcpError(Exception):
    """A remote MCP server could not be reached or refused the request; the message says why."""


def _describe(exc: BaseException) -> str:
    """One readable reason out of whatever the transport raised (anyio wraps errors in groups)."""
    seen: List[BaseException] = []
    stack = [exc]
    while stack:
        cur = stack.pop()
        seen.append(cur)
        inner = getattr(cur, "exceptions", None)
        if inner:
            stack.extend(inner)
        elif cur.__cause__ is not None:
            stack.append(cur.__cause__)
    for cur in seen:
        response = getattr(cur, "response", None)
        status = getattr(response, "status_code", None)
        if status == 401:
            return "the server refused the access (401): the token is missing or wrong"
        if status == 403:
            return "the server refused the access (403)"
        if status:
            return f"the server answered HTTP {status}"
    for cur in seen:
        name = type(cur).__name__
        if name in ("ConnectError", "ConnectTimeout", "ReadTimeout", "RemoteProtocolError"):
            return f"the server could not be reached ({name})"
    leaf = seen[-1] if seen else exc
    return str(leaf) or type(leaf).__name__


def render_result(result: Any) -> str:
    """A CallToolResult as the text a tool returns: text parts joined, other parts named, the
    structured result when there is no text, `MCP Error:` in front of an error result."""
    parts: List[str] = []
    for item in getattr(result, "content", None) or []:
        kind = getattr(item, "type", "")
        if kind == "text":
            parts.append(str(getattr(item, "text", "")))
        elif kind in ("image", "audio"):
            parts.append(f"[{kind}: {getattr(item, 'mimeType', '') or 'unknown type'}]")
        elif kind == "resource_link":
            parts.append(f"[resource: {getattr(item, 'uri', '')}]")
        elif kind == "resource":
            res = getattr(item, "resource", None)
            text = getattr(res, "text", None)
            parts.append(str(text) if text is not None else f"[resource: {getattr(res, 'uri', '')}]")
    text = "\n".join(p for p in parts if p)
    if not text and getattr(result, "structuredContent", None) is not None:
        text = json.dumps(result.structuredContent, ensure_ascii=False)
    if getattr(result, "isError", False):
        return f"MCP Error: {text or 'the tool reported an error'}"
    return text


class _Session:
    def __init__(self) -> None:
        self.session: Any = None
        self.closed: Optional[asyncio.Event] = None
        self.task: Optional[asyncio.Task] = None
        self.last_used = 0.0
        self.dead = False


class RemotePool:
    """The process's remote MCP sessions. Thread-safe; every method may be called from any
    thread except the pool's own loop."""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._start_lock = threading.Lock()
        self._sessions: Dict[Tuple[str, str, str], _Session] = {}
        self._key_locks: Dict[Tuple[str, str, str], asyncio.Lock] = {}

    # -- the loop -------------------------------------------------------------------------

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._start_lock:
            if self._loop is None or not self._loop.is_running():
                loop = asyncio.new_event_loop()
                ready = threading.Event()

                def _run() -> None:
                    asyncio.set_event_loop(loop)
                    loop.call_soon(ready.set)
                    loop.run_forever()

                self._thread = threading.Thread(target=_run, name="vaf-mcp-remote", daemon=True)
                self._thread.start()
                ready.wait(5)
                self._loop = loop
            return self._loop

    def _submit(self, coro, timeout: float):
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:     # Python 3.10: not the builtin TimeoutError
            future.cancel()
            raise RemoteMcpError(f"no answer within {int(timeout)} s")
        except RemoteMcpError:
            raise
        except BaseException as exc:  # noqa: BLE001 - one readable reason for the caller
            raise RemoteMcpError(_describe(exc)) from exc

    # -- sessions -------------------------------------------------------------------------

    @staticmethod
    def _key(transport: str, url: str, headers: Optional[Dict[str, str]]) -> Tuple[str, str, str]:
        digest = hashlib.sha256(json.dumps(headers or {}, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        return (transport, url, digest)

    async def _run_session(self, entry: _Session, transport: str, url: str,
                           headers: Dict[str, str], ready: "asyncio.Future") -> None:
        """Hold one session open until `entry.closed` is set; the SDK's contexts are entered and
        left in this one task."""
        from mcp import ClientSession
        from mcp.types import Implementation
        from vaf.version import __version__

        info = Implementation(name="VAF", version=__version__)
        try:
            if transport == "sse":
                from mcp.client.sse import sse_client
                async with sse_client(url, headers=headers or None, timeout=CONNECT_TIMEOUT_SECONDS) as (read, write):
                    async with ClientSession(read, write, client_info=info) as session:
                        await session.initialize()
                        entry.session = session
                        ready.set_result(session)
                        await entry.closed.wait()
            else:
                from mcp.client.streamable_http import streamable_http_client
                from mcp.shared._httpx_utils import create_mcp_http_client
                async with create_mcp_http_client(headers=headers or None) as http:
                    async with streamable_http_client(url, http_client=http) as (read, write, _session_id):
                        async with ClientSession(read, write, client_info=info) as session:
                            await session.initialize()
                            entry.session = session
                            ready.set_result(session)
                            await entry.closed.wait()
        except BaseException as exc:  # noqa: BLE001 - reported to the waiter, the entry is dropped
            if not ready.done():
                ready.set_exception(exc)
            else:
                logger.info("MCP session to %s ended: %s", url, _describe(exc))
        finally:
            entry.dead = True
            entry.session = None

    async def _session(self, transport: str, url: str, headers: Dict[str, str]):
        key = self._key(transport, url, headers)
        lock = self._key_locks.setdefault(key, asyncio.Lock())
        async with lock:
            entry = self._sessions.get(key)
            if entry is not None and not entry.dead and entry.session is not None:
                if time.monotonic() - entry.last_used > IDLE_PING_SECONDS:
                    try:
                        await asyncio.wait_for(entry.session.send_ping(), timeout=10)
                    except BaseException:  # noqa: BLE001 - a session that does not answer a ping is gone
                        await self._close_entry(key)
                        entry = None
                if entry is not None and not entry.dead:
                    entry.last_used = time.monotonic()
                    return entry.session
            entry = _Session()
            entry.closed = asyncio.Event()
            ready = asyncio.get_running_loop().create_future()
            entry.task = asyncio.create_task(self._run_session(entry, transport, url, headers, ready))
            try:
                session = await asyncio.wait_for(ready, timeout=CONNECT_TIMEOUT_SECONDS)
            except BaseException:
                entry.closed.set()
                raise
            entry.last_used = time.monotonic()
            self._sessions[key] = entry
            return session

    async def _close_entry(self, key) -> None:
        entry = self._sessions.pop(key, None)
        if entry is not None and entry.closed is not None:
            entry.closed.set()
            if entry.task is not None:
                try:
                    await asyncio.wait_for(entry.task, timeout=5)
                except BaseException:  # noqa: BLE001 - closing is best effort
                    pass

    # -- the calls ------------------------------------------------------------------------

    def list_tools(self, transport: str, url: str, headers: Optional[Dict[str, str]] = None,
                   timeout: float = CONNECT_TIMEOUT_SECONDS) -> List[Dict[str, Any]]:
        """The server's tools as plain dicts (name, description, inputSchema, execution...)."""
        async def _go():
            session = await self._session(transport, url, dict(headers or {}))
            result = await session.list_tools()
            return [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in result.tools]
        return self._submit(_go(), timeout)

    def call_tool(self, transport: str, url: str, tool: str, arguments: Optional[Dict[str, Any]] = None,
                  headers: Optional[Dict[str, str]] = None, timeout: float = CALL_TIMEOUT_SECONDS) -> str:
        """One `tools/call`, rendered as text. Never retried (see the module docstring)."""
        async def _go():
            session = await self._session(transport, url, dict(headers or {}))
            result = await session.call_tool(tool, arguments or {})
            return render_result(result)
        return self._submit(_go(), timeout)

    def close(self, url: Optional[str] = None) -> None:
        """Close the sessions of one URL, or all of them (a changed or removed server)."""
        if self._loop is None or not self._loop.is_running():
            return

        async def _go():
            for key in [k for k in list(self._sessions) if url is None or k[1] == url]:
                await self._close_entry(key)
        try:
            asyncio.run_coroutine_threadsafe(_go(), self._loop).result(10)
        except BaseException:  # noqa: BLE001 - closing is best effort
            pass


_pool: Optional[RemotePool] = None
_pool_lock = threading.Lock()


def get_remote_pool() -> RemotePool:
    """The process-wide pool (created on first use)."""
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = RemotePool()
        return _pool


def bearer_headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if str(token or "").strip() else {}
