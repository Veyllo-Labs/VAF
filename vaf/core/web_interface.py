# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
import asyncio
import json
import logging
import os
import threading
import time
from typing import List, Dict, Any, Optional

# Throttle log pushes to WebUI so typing and UI stay responsive (max ~3 log updates/sec)
LOG_PUSH_THROTTLE_SEC = 0.35
from fastapi import WebSocket
from vaf.core.platform import Platform
from vaf.core.log_helper import append_domain_log
from pathlib import Path

import queue
from datetime import datetime as _dt
from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor


# Fire-and-forget pool for bridging sub-agent events (browser frames/steps) from a sub-agent
# subprocess to the main VAF process over HTTP, without blocking the caller's event loop.
_BRIDGE_POOL = _ThreadPoolExecutor(max_workers=2, thread_name_prefix="subagent-bridge")


def _current_turn_id() -> Optional[str]:
    """The turn this emit belongs to, or None. Never raises, never hard-imports.

    Every event that describes what a turn produced carries this, so the UI can
    bind a file to the answer it came with instead of to whatever answer
    happens to be newest when the event arrives.
    """
    try:
        from vaf.core.subagent_ipc import get_current_turn_id
        return get_current_turn_id()
    except Exception:
        return None


def _post_to_parent(data: dict) -> None:
    """POST one event to the main process's /api/subagent/stream (used from sub-agent subprocesses,
    which have no local WebSocket clients of their own)."""
    try:
        import requests as _req
        from vaf.core.config import Config
        port = 8005 if Config.get("local_network_tls_enabled", False) else 8001
        _req.post(f"http://127.0.0.1:{port}/api/subagent/stream", json=data, timeout=1.5)
    except Exception:
        pass


def _in_subagent_subprocess() -> bool:
    return os.environ.get("VAF_IN_SUBAGENT_TERMINAL", "").strip() in ("1", "true", "yes")


def _diag_log(msg: str) -> None:
    """Write one timestamped line to queue log (always enabled, not gated by debug_logs_enabled).
    Uses the same queue log as headless_runner so all events are in one chronological stream."""
    try:
        from vaf.core.log_helper import get_dated_log_path
        with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
            f.write(f"{_dt.now().isoformat()} {msg}\n")
    except Exception:
        pass


def _resolve_log_dir():
    """The shared resolver - this used to be a divergent copy.

    Its own candidate list had no source-checkout guard, so on a wheel
    install it created a logs/ directory inside site-packages.
    """
    from vaf.core.log_helper import get_app_log_dir
    return get_app_log_dir()


class WebInterfaceManager:
    """
    Manages WebSocket connections with session-scoped broadcasting.
    
    Each connection can subscribe to a specific session, and updates are only
    sent to connections subscribed to the relevant session. This prevents
    cross-contamination between chat windows.
    """
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(WebInterfaceManager, cls).__new__(cls)
            cls._instance.initialized = False
        return cls._instance
    
    def __init__(self):
        if self.initialized:
            return
        self.active_connections: List[WebSocket] = []
        self.connection_sessions: Dict[WebSocket, str] = {}  # ws -> session_id
        self.connection_users: Dict[WebSocket, str] = {}  # ws -> user_id (for RAG scope)
        self.connection_usernames: Dict[WebSocket, str] = {}  # ws -> username (for User identity block)
        self.connection_roles: Dict[WebSocket, str] = {}  # ws -> role (admin, user, guest)
        self.agent_instance = None
        self.tools_cache: List[Dict[str, str]] = []
        # Queue for incoming chat messages from Web UI -> Main Loop
        self.input_queue = queue.Queue()
        self.log_dir = _resolve_log_dir()
        
        self.latest_state = {
            "status": "idle", # idle, thinking, tool_use
            "last_message": None,
            "logs": [],
            "tasks": [],
            "system_metrics": {}
        }
        self.last_stats = None
        self.initialized = True
        self.agent_instance = None  # Reference to the active Agent
        self._server_loop = None
        self._last_log_push_time = 0.0
        # Pending trust-gate confirmations: session_id → {"event": Event, "decision": list[str|None]}
        self._pending_gates: Dict[str, Dict] = {}

    def register_gate(self, session_id: str) -> tuple:
        """Register a pending trust-gate for session_id. Returns (event, decision_box).
        The agent thread blocks on event.wait(); the WebSocket handler calls resolve_gate()."""
        event = threading.Event()
        decision_box: list = [None]
        self._pending_gates[session_id] = {"event": event, "decision": decision_box}
        return event, decision_box

    def resolve_gate(self, session_id: str, decision: str) -> bool:
        """Signal a waiting gate with the user's decision ("allow_once"|"allow_always"|"cancel").
        Returns True if a pending gate was found and signalled."""
        pending = self._pending_gates.pop(session_id, None)
        if pending:
            pending["decision"][0] = decision
            pending["event"].set()
            return True
        return False

    def register_agent(self, agent):
        """Register the active agent instance to allow control from Web UI."""
        self.agent_instance = agent
        try:
            from vaf.core.tool_contract import tool_category
            if agent and hasattr(agent, "tools"):
                self.tools_cache = [
                    {
                        "name": name,
                        "description": getattr(tool, "description", "No description"),
                        "category": tool_category(name, tool)
                    }
                    for name, tool in agent.tools.items()
                ]
                self.push_update({"type": "tools_list", "tools": self.tools_cache})
        except Exception:
            pass

    # ═══════════════════════════════════════════════════════════════════════════
    # CONNECTION MANAGEMENT (Session-Scoped)
    # ═══════════════════════════════════════════════════════════════════════════

    async def connect(self, websocket: WebSocket):
        """Accept a new WebSocket connection."""
        await websocket.accept()
        # Self-heal after restarts/reloads: always bind to the currently running
        # WebSocket event loop so thread-safe pushes use a live loop.
        try:
            current_loop = asyncio.get_running_loop()
            if (
                self._server_loop is None
                or self._server_loop.is_closed()
                or (not self._server_loop.is_running())
                or (self._server_loop is not current_loop)
            ):
                self.set_server_loop(current_loop)
        except Exception:
            pass
        self.active_connections.append(websocket)
        # Send initial state
        await websocket.send_text(json.dumps({
            "type": "state_full",
            "data": self.latest_state
        }))

    def disconnect(self, websocket: WebSocket):
        """Remove a WebSocket connection and its session subscription."""
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        if websocket in self.connection_sessions:
            del self.connection_sessions[websocket]
        if websocket in self.connection_users:
            del self.connection_users[websocket]
        if websocket in self.connection_usernames:
            del self.connection_usernames[websocket]
        if websocket in self.connection_roles:
            del self.connection_roles[websocket]

    def set_connection_user(self, websocket: WebSocket, user_id: str, username: Optional[str] = None, role: Optional[str] = None) -> None:
        """Store user id (and optionally username/role) for this connection (e.g. for RAG scope and User identity block)."""
        self.connection_users[websocket] = user_id
        if username is not None:
            self.connection_usernames[websocket] = username
        if role is not None:
            self.connection_roles[websocket] = role

    def get_connection_user(self, websocket: WebSocket) -> Optional[str]:
        """Get user id for this connection, or None."""
        return self.connection_users.get(websocket)

    def get_connection_username(self, websocket: WebSocket) -> Optional[str]:
        """Get username for this connection, or None."""
        return self.connection_usernames.get(websocket)
        
    def get_connection_user_role(self, websocket: WebSocket) -> Optional[str]:
        """Get user role for this connection, or None."""
        return self.connection_roles.get(websocket)

    def subscribe_to_session(self, websocket: WebSocket, session_id: str):
        """
        Subscribe a connection to receive updates for a specific session.
        
        This is called when a client loads or creates a session.
        """
        self.connection_sessions[websocket] = session_id

    def get_session_for_connection(self, websocket: WebSocket) -> Optional[str]:
        """Get the session ID a connection is subscribed to."""
        return self.connection_sessions.get(websocket)

    # ═══════════════════════════════════════════════════════════════════════════
    # BROADCASTING (Session-Scoped)
    # ═══════════════════════════════════════════════════════════════════════════

    async def broadcast(self, message: dict):
        """Broadcast a message to all connected clients (global)."""
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_text(json.dumps(message))
            except Exception:
                disconnected.append(connection)
        
        for conn in disconnected:
            self.disconnect(conn)

    async def broadcast_to_session(self, session_id: str, message: dict):
        """
        Broadcast a message only to clients subscribed to a specific session.
        """
        if not session_id:
            return await self.broadcast(message)

        message['sessionId'] = session_id  # Ensure sessionId is always present

        sent_count = 0
        disconnected = []
        for connection in self.active_connections:
            conn_session = self.connection_sessions.get(connection)
            # Send ONLY if connection is explicitly subscribed to this session.
            # This ensures privacy and prevents "message leakage" to new connections.
            if conn_session == session_id:
                try:
                    await connection.send_text(json.dumps(message))
                    sent_count += 1
                except Exception as send_err:
                    disconnected.append(connection)
                    # Log send failures so we can diagnose proxy relay issues
                    _diag_log(f"[SEND_FAIL] broadcast_to_session({session_id}) type={message.get('type')} err={send_err}")

        for conn in disconnected:
            self.disconnect(conn)

        # Diagnostic: log every broadcast attempt for key event types (always written, not gated by debug flag)
        msg_type = message.get('type', '')
        if msg_type in ('agent_message_update', 'tool_update', 'history_update', 'status_update',
                        'workflow_start', 'workflow_update', 'workflow_output_stream', 'workflow_done'):
            try:
                cur_loop = asyncio.get_running_loop()
                loop_id = id(cur_loop)
            except RuntimeError:
                loop_id = 'NO_LOOP'
            _diag_log(
                f"[BROADCAST] session={session_id} type={msg_type} "
                f"sent={sent_count} active={len(self.active_connections)} "
                f"subs={list(self.connection_sessions.values())} disconnected={len(disconnected)} "
                f"loop={loop_id}"
            )

    async def broadcast_to_user(self, user_id: str, message: dict):
        """
        Broadcast a message only to clients authenticated as a specific user.
        """
        if not user_id:
            return
            
        disconnected = []
        target_id = str(user_id).strip()
        for connection in self.active_connections:
            conn_user = self.connection_users.get(connection)
            if conn_user and str(conn_user).strip() == target_id:
                try:
                    await connection.send_text(json.dumps(message))
                except Exception:
                    disconnected.append(connection)
        
        for conn in disconnected:
            self.disconnect(conn)

    # ═══════════════════════════════════════════════════════════════════════════
    # PUBLIC API (for Agent/TUI to call)
    # ═══════════════════════════════════════════════════════════════════════════

    def update_status(self, status: str, session_id: str = None):
        """Update agent status (idle, thinking, etc)."""
        self.latest_state["status"] = status
        self._push_session_update(session_id, {"type": "status_update", "status": status})

    def log(self, message: str, level: str = "info", source: str = "system", session_id: str = None):
        """
        Add a log entry. Pushes to WebUI are throttled so the UI does not lag when many logs are emitted.
        If session_id is provided, the log is only sent to clients viewing that session.
        """
        log_entry = {
            "timestamp": __import__("datetime").datetime.now().isoformat(),
            "message": message,
            "level": level,
            "source": source
        }
        self.latest_state["logs"].append(log_entry)
        if len(self.latest_state["logs"]) > 1000:
            self.latest_state["logs"].pop(0)

        now = time.time()
        if now - self._last_log_push_time >= LOG_PUSH_THROTTLE_SEC:
            self._last_log_push_time = now
            self._push_session_update(session_id, {"type": "new_log", "entry": log_entry})

    def set_tasks(self, tasks: List[Dict], session_id: str = None):
        """Update the list of active/pending tasks."""
        self.latest_state["tasks"] = tasks
        self._push_session_update(session_id, {"type": "tasks_update", "tasks": tasks})
        
    def emit_agent_message(self, role: str, content: str, session_id: str = None):
        """Emit a message update. Content is the FULL message so far."""
        self._push_session_update(session_id, {
            "type": "agent_message_update",
            "role": role,
            "content": content,
            "turnId": _current_turn_id(),
        })

    def emit_agent_message_append(self, content: str, session_id: str = None, role: str = "assistant", kind: str = None):
        """Emit a COMPLETE, standalone message that must be appended as its own new
        bubble — never merged/streamed in-place.

        Used for proactive messages (e.g. automation results) where there is no live
        agent turn to attach to. The streaming `agent_message_update` path would
        otherwise overwrite the last assistant bubble or drop the text entirely.

        `kind` (optional) tags a system-activity / wake-up message (e.g. "timer") so the
        Web UI can render it in its own left-side area with a kind-specific look.
        """
        payload = {
            "type": "agent_message_append",
            "role": role,
            "content": content,
            "turnId": _current_turn_id(),
        }
        if kind:
            payload["kind"] = kind
        self._push_session_update(session_id, payload)

    def emit_clear_last_assistant(self, session_id: str = None):
        """Ask the Web UI to remove the last assistant message (e.g. before empty-response retry)."""
        self._push_session_update(session_id, {"type": "clear_last_assistant"})

    def emit_message_complete(self, content: str, session_id: str = None):
        """Emit when a message is fully complete (for Auto-TTS trigger)."""
        self._push_session_update(session_id, {
            "type": "message_complete",
            "content": content,
            "turnId": _current_turn_id(),
        })

    def emit_tool_update(self, event_type: str, tool_name: str, tool_id: str, data: str = None, session_id: str = None):
        """
        Emit a tool execution update.
        event_type: 'start', 'end', 'error'
        data: arguments (for start) or result (for end/error)
        """
        self._push_session_update(session_id, {
            "type": "tool_update",
            "subType": event_type,
            "toolId": tool_id,
            "name": tool_name,
            "data": data,
            "timestamp": __import__("datetime").datetime.now().isoformat()
        })

    def emit_agent_state(self, msg_type: str, state: dict, session_id: str = None):
        """Emit one live agent-view state object under its own wire type.

        The primitive behind the per-agent emitters below, and the one an embedder's own
        agent uses to feed a view of its own: VAF's five views are five call sites, not
        five mechanisms.

        Contract, each choice against its failure mode:

        - `msg_type` is the LITERAL type the frontend switches on, used UNDECORATED.
          Deriving it (`f"{kind}_state"`) would rename `subagent_update`, the type the
          coder's live editor feed rides on, to one no branch in `web/app/page.tsx`
          handles - and that chain has no default branch, so the pane goes dark with
          nothing logged anywhere.
        - The state is splatted at the TOP level because the frontend rebuilds every
          payload field by field; nesting it under one key drops the whole view at once.
        - `type` is written first, so a state key of the same name overrides it. Preserved
          from the five methods this replaces: no agent sends one, and a loud rename is
          easier to find than a silent ignore.
        - Nothing is added here. See `StatePublisher` in `vaf/core/progress.py` for why one
          injected field can erase a whole run's stream in the bridge lane.
        """
        self._bridge_or_push({"type": msg_type, **(state or {})}, session_id)

    def _bridge_or_push(self, payload: dict, session_id: str = None):
        """The one transport fork: HTTP bridge out of a sub-agent subprocess, WS otherwise.

        Contract, each choice against its failure mode:

        - `_in_subagent_subprocess()` is re-read on EVERY call and never cached:
          `VAF_IN_SUBAGENT_TERMINAL` is set and cleared INSIDE a live process by the
          workflow CLI and by the headless runner's leak guard, so the transport is a
          property of the moment, not of the object.
        - The two branches attach `sessionId` differently on purpose. The bridge stamps it
          here, only when truthy; the local branch leaves it to `_push_session_update`,
          which mutates the caller's dict in place. Both end with the key on the wire,
          which is what every frontend handler's cross-session filter tests. Unify them and
          one path loses either the key or the mutation the callers were tuned against.
        - Fire-and-forget on the shared two-worker `_BRIDGE_POOL`. Callers run inside a
          browser-use asyncio loop and inside an SSE parse loop, so a synchronous POST
          stalls the agent and not merely the view - and a daemon thread of its own would
          drop the terminal frame.
        - A falsy `session_id` still SENDS: unscoped `push_update` locally, an unstamped
          POST over the bridge, both of which reach every connected client. Preserved
          verbatim. It is reachable only from the browser agent, the one site that does not
          gate on a session id, and closing it is a behaviour change with its own test, not
          a line inside a refactor.
        """
        if _in_subagent_subprocess():
            if session_id:
                payload["sessionId"] = session_id
            # The ordering room rides beside the session: a room turn can spawn
            # a child with NO session (the runner's room frame binds no chat),
            # and the parent's endpoint routes by room when the session cannot
            # carry the event to anybody.
            _room = os.environ.get("VAF_ROOM_ID", "").strip()
            if _room and "roomId" not in payload:
                payload["roomId"] = _room
            _BRIDGE_POOL.submit(_post_to_parent, payload)
            return
        self._push_session_update(session_id, payload)

    def emit_browser_frame(self, frame_b64: str, url: str = "", session_id: str = None):
        """Emit a live browser screenshot frame for browser_agent live view in WebUI."""
        self._bridge_or_push({
            "type": "browser_frame_update",
            "frame": frame_b64,
            "url": url,
            "timestamp": _dt.now().isoformat(),
        }, session_id)

    def emit_browser_step(self, line: str, session_id: str = None):
        """Emit a single browser-use agent log line to the WebUI SubAgent console."""
        self._bridge_or_push({"type": "browser_step_update", "line": line}, session_id)

    def emit_browser_state(self, state: dict, session_id: str = None):
        """Emit the browser agent's structured live state (task, step, action plan,
        visited URLs, vision) for the browser window dock in the WebUI. The screenshot
        itself stays on the separate browser_frame_update stream."""
        self.emit_agent_state("browser_state", state, session_id=session_id)

    def emit_learn_state(self, state: dict, session_id: str = None):
        """Emit the batched document-learn progress (docName, batch, batchesTotal,
        phase) for the learning banner. Frame keys are ints and plain strings ONLY -
        and never SubAgentStreamUpdate's typed field names: its `progress` is
        Optional[int], and a "3/100" string there is a silent ValidationError that
        kills the whole bridge stream of the run."""
        self.emit_agent_state("learn_state", state, session_id=session_id)

    def emit_coder_code(self, file: str, code: str, session_id: str = None):
        """Emit the code currently being written (live editor feed).

        Sent as a minimal `subagent_update` — the frontend already maps
        file/code from that type and keeps all other fields unchanged. Any further
        field here would be read by that same handler: `status`, `presence`, `steps`
        and `agentName` all mean something on it.
        """
        self._bridge_or_push({"type": "subagent_update", "file": file, "code": code},
                             session_id)

    def emit_research_state(self, state: dict, session_id: str = None):
        """Emit the research agent's live state (outline, sources, section html).

        Feeds the research view of the SubAgent window: paper-style document
        viewer with the report growing section by section, outline progress,
        source citations and the status bar.
        """
        self.emit_agent_state("research_state", state, session_id=session_id)

    def emit_document_state(self, state: dict, session_id: str = None):
        """Emit the document agent's live state (sections, placeholders, section html).

        Feeds the document view of the SubAgent window: A4 paper viewer with the
        document growing section by section, outline progress, placeholder values
        (resolved from memory / chat) and the status bar. Sent by the document agent
        on meaningful changes (plan ready, section writing/done, placeholders resolved).
        """
        self.emit_agent_state("document_state", state, session_id=session_id)

    def emit_librarian_state(self, state: dict, session_id: str = None):
        """Emit the librarian agent's live state (filesystem map, storage, search).

        Feeds the read-only explorer view of the SubAgent window: a disk-usage style
        listing of folders with sizes, storage/drive gauges (local disk + Google Drive),
        the biggest-folders list and an activity feed. Sent by the librarian agent when
        it starts a task and when it finishes. The librarian only reads, never writes.
        """
        self.emit_agent_state("librarian_state", state, session_id=session_id)

    def emit_coder_state(self, state: dict, session_id: str = None):
        """Emit the coder's project state (file tree, git, progress) to the WebUI.

        Feeds the VS-Code-style SubAgent window: explorer file list with
        per-file status, source-control section and the status bar. Sent by the
        coding agent on meaningful changes (init, file written, task done,
        final commit).
        """
        self.emit_agent_state("coder_state", state, session_id=session_id)

    def emit_stats(self, stats: dict, session_id: str = None):
        """Emit context/token statistics."""
        self.last_stats = stats
        self._push_session_update(session_id, {
            "type": "stats",
            "stats": stats
        })

    def emit_session_unread(self, session_id: str):
        """Notify all connected clients that a session has a new unread agent message."""
        if not session_id:
            return
        self.push_update({"type": "session_unread", "sessionId": session_id})

    def emit_editor_apply_edit(self, session_id: str, selection_index: int, new_text: str, start: int = None, end: int = None):
        """
        Ask the Web UI to replace the text at the given marked selection in the Document Editor.
        If start/end are provided, the frontend replaces that character range; otherwise it uses selectionIndex.
        """
        if not session_id:
            return
        payload = {
            "type": "editor_apply_edit",
            "selectionIndex": selection_index,
            "newText": new_text,
        }
        if start is not None and end is not None:
            payload["start"] = start
            payload["end"] = end
        self._push_session_update(session_id, payload)

    # ═══════════════════════════════════════════════════════════════════════════
    # THREAD-SAFE BRIDGING
    # ═══════════════════════════════════════════════════════════════════════════
    
    def set_server_loop(self, loop):
        """Set the asyncio event loop for thread-safe broadcasting."""
        old_loop = self._server_loop
        self._server_loop = loop
        _diag_log(f"[LOOP_SET] new_loop={id(loop)} old_loop={id(old_loop) if old_loop else 'None'} running={loop.is_running()}")
        if self.tools_cache:
            asyncio.run_coroutine_threadsafe(
                self.broadcast({"type": "tools_list", "tools": self.tools_cache}),
                self._server_loop
            )

    def _get_dispatch_loop(self):
        """Return a live loop for run_coroutine_threadsafe, or None if unavailable."""
        loop = self._server_loop
        if not loop:
            return None
        try:
            if loop.is_closed() or (not loop.is_running()):
                _diag_log(f"[LOOP_INVALIDATED] loop={id(loop)} closed={loop.is_closed()} running={loop.is_running()}")
                self._server_loop = None
                return None
        except Exception:
            self._server_loop = None
            return None
        return loop
        
    def push_update(self, data: dict):
        """Thread-safe push update (global broadcast)."""
        loop = self._get_dispatch_loop()
        if loop:
            asyncio.run_coroutine_threadsafe(self.broadcast(data), loop)

    def push_update_to_user(self, user_id: str, data: dict):
        """Thread-safe push update to a specific user's connections (e.g. notifications)."""
        if not user_id:
            return
        loop = self._get_dispatch_loop()
        if loop:
            asyncio.run_coroutine_threadsafe(
                self.broadcast_to_user(str(user_id).strip(), data),
                loop
            )

    def room_route_for_session(self, session_id: Optional[str]):
        """`(room_id, owner_scope)` when an active sub-agent task ORDERED BY A ROOM
        runs for this session; None otherwise.

        This is the durable half of the room live-feed routing. The room-turn
        marker only lives as long as the turn, but a spawned subprocess streams
        for minutes AFTER the turn that ordered it ended - measured live: the
        sub-agent window opened on the turn's own events and then sat empty for
        a six-minute coder run, because every bridged event was broadcast to a
        session nobody watches. The task record is the source that survives the
        turn: it carries the ordering room, and its session resolves to the one
        owner allowed to see the feed (same ownership rule the worker cards
        apply: a scopeless legacy session belongs to the local admin, never to
        everyone). Every failure answers None, which keeps the session lane -
        fail closed means "display stays narrow", never "event crosses users".

        The IPC files are re-read at most every 1.5s, and an unknown session
        forces at most 4 refreshes/s: live feeds arrive per token, and a disk
        read per event would make the bridge the slowest part of the run.
        """
        if not session_id:
            return None
        sid = str(session_id)
        now = time.monotonic()
        cache = getattr(self, "_room_route_cache", None)
        if (isinstance(cache, dict) and now < cache["hard"]
                and (sid in cache["map"] or now < cache["soft"])):
            return cache["map"].get(sid)
        route_map: Dict[str, Any] = {}
        try:
            from vaf.core.subagent_ipc import get_ipc
            ipc = get_ipc()
            tasks = list(ipc.get_active_tasks(None)) + list(ipc.get_pending_tasks(None))
            for task in tasks:
                task_sid = getattr(task, "session_id", None)
                if not task_sid:
                    continue
                room_id = getattr(task, "room_id", None)
                if not room_id:
                    # A decided negative: the task exists and no room ordered it.
                    route_map.setdefault(str(task_sid), None)
                    continue
                scope = self._session_owner_scope(str(task_sid))
                route_map[str(task_sid)] = ((str(room_id), scope) if scope else None)
        except Exception:
            route_map = {}
        self._room_route_cache = {"map": route_map,
                                  "soft": now + 0.25, "hard": now + 1.5}
        return route_map.get(sid)

    def room_audience(self, room_id: Optional[str]) -> Optional[str]:
        """Whose screen a room event belongs on.

        The account whose agent is PRODUCING it, not the one that happens to own the
        room. The two are the same while a room holds one household and they are not
        the moment it admits several - there, routing by ownership would put one
        account's model text and tool activity on another account's screen, which is
        the one thing that must not cross that line.

        The acting scope is trusted ONLY while that agent's own room turn is running.
        Outside it the bound scope is whatever the last chat left behind, so using it
        then would be the same leak pointing the other way; the room's proven owner is
        the honest fallback.

        One answer for both lanes on purpose: the bridge and the in-process push each
        had their own copy of this decision, and two copies of a routing rule are two
        chances for one of them to keep sending to the wrong person.
        """
        room_id = str(room_id or "").strip()
        if not room_id:
            return None
        agent = getattr(self, "agent_instance", None)
        room_turn = getattr(agent, "_room_turn", None)
        if isinstance(room_turn, dict) \
                and str(room_turn.get("room_id") or "").strip() == room_id:
            acting = str(getattr(agent, "_current_user_scope_id", "") or "").strip()
            if acting:
                return acting
        return self.room_owner_scope(room_id)

    def room_owner_scope(self, room_id: Optional[str]) -> Optional[str]:
        """The tenant scope that hosts a room, or None when it cannot be PROVEN.

        The room manifest is the authority: a room-ordered worker's feed belongs
        to the room's own tenant, whatever session the turn happened to run in -
        and a room turn may legitimately run with NO session at all, which is
        exactly when this answer is the only one left. Cached briefly: the
        manifest is a disk read and live feeds arrive per token."""
        rid = str(room_id or "").strip()
        if not rid:
            return None
        now = time.monotonic()
        cache = getattr(self, "_room_owner_cache", None) or {}
        hit = cache.get(rid)
        if hit and now < hit[1]:
            return hit[0]
        scope: Optional[str] = None
        try:
            from vaf.core.a2a.room import Room
            raw = Room.open(rid).manifest.get("owner_scope")
            if raw is None:
                from vaf.core.config import get_local_admin_scope_id
                scope = str(get_local_admin_scope_id())
            else:
                scope = str(raw)
        except Exception:
            scope = None
        cache[rid] = (scope, now + 5.0)
        self._room_owner_cache = cache
        return scope

    def _session_owner_scope(self, session_id: str) -> Optional[str]:
        """The scope that owns a session, or None when ownership cannot be PROVEN.
        A scopeless legacy session belongs to the local admin - the rule every
        ownership reader in the harness applies - and an unloadable session
        belongs to nobody, so its feed is never routed per user."""
        try:
            from vaf.core.session import get_manager
            meta = getattr(get_manager().load(session_id), "metadata", None) or {}
            scope = meta.get("user_scope_id")
            if scope is not None:
                return str(scope)
            from vaf.core.config import get_local_admin_scope_id
            return str(get_local_admin_scope_id())
        except Exception:
            return None

    def _push_session_update(self, session_id: Optional[str], data: dict):
        """
        Thread-safe push update with session scoping.
        Falls back to HTTP POST when the asyncio loop is unavailable (e.g. after
        the event loop reference is invalidated between WebSocket connections).
        """
        if session_id:
            data['sessionId'] = session_id
            # A ROOM turn's live feed must reach the person WATCHING THE ROOM, and
            # that browser is not subscribed to the turn's session - measured live:
            # a real coder run looked like a hung one, because every update above
            # was filtered at both ends. While the agent's room-turn marker is up,
            # the event is stamped with the room and sent per USER instead of per
            # session: that reaches the session subscribers too (no duplicates),
            # reaches the room watcher, and never crosses an account boundary -
            # which is the only line that matters here (the session filter was
            # display isolation, not privacy; broadcast_to_user is the privacy).
            room_turn = getattr(getattr(self, "agent_instance", None),
                                "_room_turn", None)
            room_scope = getattr(getattr(self, "agent_instance", None),
                                 "_current_user_scope_id", None)
            if (isinstance(room_turn, dict) and room_turn.get("room_id")
                    and room_scope):
                data['roomId'] = str(room_turn["room_id"])
                loop = self._get_dispatch_loop()
                if loop:
                    asyncio.run_coroutine_threadsafe(
                        self.broadcast_to_user(str(room_scope), data),
                        loop
                    )
                else:
                    # Same stale-loop fallback the session path keeps; the stamp
                    # travels in the payload either way.
                    self._http_fallback_push(data)
                return
            # No live marker: delegated workers run AFTER the turn that ordered
            # them. The task record is the durable source for the same routing.
            route = self.room_route_for_session(session_id)
            if route:
                data['roomId'] = route[0]
                loop = self._get_dispatch_loop()
                if loop:
                    asyncio.run_coroutine_threadsafe(
                        self.broadcast_to_user(route[1], data),
                        loop
                    )
                else:
                    self._http_fallback_push(data)
                return
            loop = self._get_dispatch_loop()
            if loop:
                asyncio.run_coroutine_threadsafe(
                    self.broadcast_to_session(session_id, data),
                    loop
                )
                msg_type = data.get('type', '')
                if msg_type in ('agent_message_update', 'history_update', 'status_update',
                                'workflow_start', 'workflow_update', 'workflow_output_stream', 'workflow_done'):
                    _diag_log(
                        f"[PUSH_SCHEDULED] type={msg_type} session={session_id} "
                        f"loop={id(loop)} active_ws={len(self.active_connections)}"
                    )
            else:
                self._http_fallback_push(data)
        else:
            # No session, but a room turn may be the producer (the runner's room
            # frame binds no chat): the room is a full routing anchor of its
            # own. Only a PROVEN room tenant narrows the send; otherwise the
            # global lane stays exactly what it was.
            room_turn = getattr(getattr(self, "agent_instance", None),
                                "_room_turn", None)
            room_hint = str(data.get("roomId") or "").strip()
            if not room_hint and isinstance(room_turn, dict):
                room_hint = str(room_turn.get("room_id") or "").strip()
            room_scope = self.room_audience(room_hint) if room_hint else None
            if room_hint and room_scope:
                data['roomId'] = room_hint
                loop = self._get_dispatch_loop()
                if loop:
                    asyncio.run_coroutine_threadsafe(
                        self.broadcast_to_user(room_scope, data), loop)
                else:
                    self._http_fallback_push(data)
                return
            self.push_update(data)

    def _http_fallback_push(self, data: dict):
        """POST to the internal API when the asyncio loop reference is stale."""
        msg_type = data.get('type', '')
        try:
            import requests as _req
            from vaf.core.config import Config
            tls_on = Config.get("local_network_tls_enabled", False)
            port = 8005 if tls_on else 8001
            _req.post(f"http://127.0.0.1:{port}/api/subagent/stream", json=data, timeout=0.5)
            _diag_log(f"[PUSH_HTTP_OK] type={msg_type} session={data.get('sessionId')} port={port}")
        except Exception as exc:
            _diag_log(f"[PUSH_DROP] No _server_loop + HTTP fallback failed! type={msg_type} "
                      f"session={data.get('sessionId')} err={exc}")


# Global Accessor
def get_web_interface():
    return WebInterfaceManager()


def start_model_download_broadcast(poll_seconds: float = 0.5):
    """Stream the current model download's progress to all WebSocket clients as
    ``model_download_progress`` -- the SAME message a WebUI-initiated download uses, so the existing
    download banner renders for tray/auto (first-run) downloads too, not only WebUI-initiated ones.
    Driven by ``model_download_state.MODEL_DOWNLOAD``; returns a ``stop()`` callable to end the stream.
    Runs in a daemon thread because the download itself blocks its own thread (hf_hub_download)."""
    import threading
    import time
    from vaf.core.model_download_state import MODEL_DOWNLOAD

    stop_evt = threading.Event()

    def _loop():
        wi = get_web_interface()
        last_bytes, last_t = 0, time.time()
        while not stop_evt.is_set():
            snap = MODEL_DOWNLOAD.snapshot()
            if snap["active"]:
                now = time.time()
                delta_b = snap["bytes_done"] - last_bytes
                delta_t = now - last_t
                speed = f"{delta_b / delta_t / 1e6:.1f} MB/s" if delta_t > 0 and delta_b > 0 else ""
                last_bytes, last_t = snap["bytes_done"], now
                try:
                    wi.push_update({
                        "type": "model_download_progress",
                        "repo_id": snap["repo"],
                        "progress_pct": snap["pct"],
                        "bytes_done": snap["bytes_done"],
                        "bytes_total": snap["bytes_total"],
                        "speed_str": speed,
                    })
                except Exception:
                    pass
            stop_evt.wait(poll_seconds)

    threading.Thread(target=_loop, daemon=True, name="model-download-broadcast").start()
    return stop_evt.set


def start_maintenance_broadcast(poll_seconds: float = 1.0):
    """Stream the current maintenance job's progress to all WebSocket clients
    as ``maintenance_progress`` frames. Driven by ``maintenance_state
    .MAINTENANCE`` (generic: the memory re-embed today, any machine-level job
    with honest counts tomorrow). Machine-level jobs have no session, so this
    is a broadcast like the model-download banner, not a StatePublisher lane.
    The poll interval is the throttle (WEBSOCKET_FLOW rate-cap rule); frames
    are only sent while a job is active, plus one final inactive frame so the
    banner can close itself. Returns a ``stop()`` callable."""
    import threading
    import time as _time
    from vaf.core.maintenance_state import MAINTENANCE

    stop_evt = threading.Event()

    def _frame(snap: dict) -> dict:
        return {
            "type": "maintenance_progress",
            "kind": snap["kind"],
            "active": snap["active"],
            "done": snap["done"],
            "total": snap["total"],
            "pct": snap["pct"],
            "phase": snap["phase"],
            "error": snap["error"],
        }

    def _loop():
        wi = get_web_interface()
        was_active = False
        while not stop_evt.is_set():
            snap = MAINTENANCE.snapshot()
            if snap["active"] or was_active:
                try:
                    wi.push_update(_frame(snap))
                except Exception:
                    pass
            was_active = snap["active"]
            stop_evt.wait(poll_seconds)

    threading.Thread(target=_loop, daemon=True, name="maintenance-broadcast").start()
    return stop_evt.set


def maintenance_snapshot_frame() -> dict:
    """The same frame the broadcast sends, for the late-joiner request lane
    (a client connecting mid-job has missed every pushed frame)."""
    from vaf.core.maintenance_state import MAINTENANCE
    snap = MAINTENANCE.snapshot()
    return {
        "type": "maintenance_progress",
        "kind": snap["kind"],
        "active": snap["active"],
        "done": snap["done"],
        "total": snap["total"],
        "pct": snap["pct"],
        "phase": snap["phase"],
        "error": snap["error"],
    }


def internal_api_base() -> str:
    """Base URL for SUBPROCESS -> backend HTTP calls (single source, Rule 2).

    With local_network_tls_enabled the public port 8001 speaks HTTPS; plain-HTTP
    requests there die silently. The backend keeps a plain-HTTP INTERNAL port
    (8005) for exactly this. Several senders hardcoded http://127.0.0.1:8001 and
    lost every event in TLS setups - live incident: the @workflow subprocess's
    workflow_start/update/done never reached the UI, so the SubAgent window
    showed instead of the Workflow Runtime panel.
    """
    try:
        from vaf.core.config import Config
        tls_on = Config.get("local_network_tls_enabled", False)
    except Exception:
        tls_on = False
    return "http://127.0.0.1:8005" if tls_on else "http://127.0.0.1:8001"


def notify_rooms_changed(user_scope_id: Optional[str] = None) -> None:
    """Tell a browser that this user's ROOM list changed, so it refetches.

    A SIGNAL and not the list itself, deliberately. Building the sidebar payload here
    would mean this module - the engine's - importing the web server's projection, and
    the dependency would point the wrong way round for the sake of saving one round
    trip. The browser already knows how to ask (`get_sessions`); it only ever needed to
    be told that the answer changed.

    It exists because a room appearing is the one change nothing announced. Closing,
    renaming and deleting all happen inside a WebSocket command, which can answer on
    the spot - but a room is OPENED by the agent, in a tool call, with no socket
    command in flight and nothing looking at the store. The row simply was not there
    until the whole interface was reloaded by hand.

    Safe with no Web session at all (Telegram, an automation, the terminal): it
    returns without doing anything.
    """
    if not user_scope_id:
        return
    try:
        wi = get_web_interface()
        loop = wi._get_dispatch_loop()
        if not loop:
            return
        asyncio.run_coroutine_threadsafe(
            wi.broadcast_to_user(str(user_scope_id), {"type": "rooms_changed"}), loop)
    except Exception:
        # A sidebar that did not refresh is a nuisance; an exception raised into a tool
        # result is a failed tool call for something the user never asked about.
        pass


def notify_file_created(session_id: Optional[str], file_path, title: Optional[str] = None,
                        turn_id: Optional[str] = None) -> None:
    """
    Notify the Web UI that a file was created so it shows a download/open link.
    Works from main process (WebSocket) and from subprocess (HTTP POST fallback).
    Safe when there is no Web session (Telegram, automation): returns immediately.

    `turn_id` addresses the message the file belongs to. It defaults to the turn
    the caller is running in, and is passed explicitly only where the file is
    announced BEFORE that turn starts (an image attached to the message being
    sent). Without it the browser had to guess "the newest answer that exists
    right now" - which, while a tool is running, is still the PREVIOUS answer.
    """
    if not session_id or not file_path:
        return
    resolved = Path(file_path).resolve().as_posix()
    payload = {
        "type": "file_created",
        "sessionId": session_id,
        "filePath": resolved,
        "title": title or Path(file_path).name,
        "turnId": turn_id or _current_turn_id(),
    }
    wi = get_web_interface()
    if getattr(wi, "_server_loop", None):
        # In-process path: the /api/workflow/update endpoint (which anchors the
        # session workspace) is bypassed here, so run the shared setter directly.
        # Without this, files written by the main agent or the workflow engine
        # never set session.project_path and the [SESSION WORKSPACE] note never
        # fired for such chats (live incident).
        try:
            from vaf.core.session import record_created_file
            record_created_file(session_id, file_path)
        except Exception:
            pass
        wi._push_session_update(session_id, payload)
    else:
        try:
            import requests
            from vaf.core.config import Config
            tls_on = Config.get("local_network_tls_enabled", False)
            port = 8005 if tls_on else 8001
            requests.post(f"http://127.0.0.1:{port}/api/workflow/update", json=payload, timeout=1)
        except Exception:
            pass


def notify_document_created(session_id: Optional[str], file_path, title: Optional[str] = None, open_mode: str = "editor") -> None:
    """
    Notify the Web UI that a document was created so the Document Editor opens with it.
    Call this from any code path that creates a document (workflow, document_agent, etc.).

    open_mode: "editor" (default) auto-opens the editable Document Editor. "viewer" instead opens the
    file read-only in the Document Viewer (sidebar) and RAG-indexes it — used for research_agent reports,
    which are meant to be read/referenced rather than edited.
    Works from main process (WebSocket) and from subprocess (HTTP POST to /api/workflow/update).

    Safe when there is no Web session (e.g. Telegram or automation): if session_id is missing,
    we return immediately; the document is already saved and the flow is not disturbed.
    """
    if not session_id or not file_path:
        return
    resolved = Path(file_path).resolve().as_posix()
    payload = {
        "type": "document_ready",
        "sessionId": session_id,
        "filePath": resolved,
        "title": title or Path(file_path).name,
        "openMode": open_mode,
    }
    wi = get_web_interface()
    if getattr(wi, "_server_loop", None):
        wi._push_session_update(session_id, payload)
    else:
        try:
            import requests
            from vaf.core.config import Config
            tls_on = Config.get("local_network_tls_enabled", False)
            port = 8005 if tls_on else 8001
            requests.post(f"http://127.0.0.1:{port}/api/workflow/update", json=payload, timeout=1)
        except Exception:
            pass
