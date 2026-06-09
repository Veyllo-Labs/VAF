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


def _resolve_log_dir() -> Path:
    """Resolve log dir so emit_debug.txt and webui_push_debug.txt land in project logs (e.g. d:\\VAF\\logs)."""
    candidates = []
    env_dir = os.environ.get("VAF_LOG_DIR")
    if env_dir:
        candidates.append(Path(env_dir))
    # Prefer repo root / logs so WebUI debug logs sit next to callback_debug.txt, queue.log, etc.
    repo_logs = Path(__file__).resolve().parents[2] / "logs"
    candidates.append(repo_logs)
    candidates.append(Platform.data_dir() / "logs")
    candidates.append(Platform.vaf_dir() / "logs")
    candidates.append(Path(__file__).resolve().parents[1] / "logs")
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except Exception:
            continue
    return Path.cwd()

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
            if agent and hasattr(agent, "tools"):
                self.tools_cache = [
                    {
                        "name": name,
                        "description": getattr(tool, "description", "No description"),
                        "category": getattr(tool, "category", "general")
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
            "content": content
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
            "content": content
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

    def emit_browser_frame(self, frame_b64: str, url: str = "", session_id: str = None):
        """Emit a live browser screenshot frame for browser_agent live view in WebUI."""
        payload = {
            "type": "browser_frame_update",
            "frame": frame_b64,
            "url": url,
            "timestamp": _dt.now().isoformat(),
        }
        # browser_agent runs in its own subprocess (no local WS clients) and emits frames from
        # inside the browser-use asyncio loop — bridge to the main process off-thread so the
        # loop never blocks on the HTTP post.
        if _in_subagent_subprocess():
            if session_id:
                payload["sessionId"] = session_id
            _BRIDGE_POOL.submit(_post_to_parent, payload)
            return
        self._push_session_update(session_id, payload)

    def emit_browser_step(self, line: str, session_id: str = None):
        """Emit a single browser-use agent log line to the WebUI SubAgent console."""
        payload = {
            "type": "browser_step_update",
            "line": line,
        }
        if _in_subagent_subprocess():
            if session_id:
                payload["sessionId"] = session_id
            _BRIDGE_POOL.submit(_post_to_parent, payload)
            return
        self._push_session_update(session_id, payload)

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

    def _push_session_update(self, session_id: Optional[str], data: dict):
        """
        Thread-safe push update with session scoping.
        Falls back to HTTP POST when the asyncio loop is unavailable (e.g. after
        the event loop reference is invalidated between WebSocket connections).
        """
        if session_id:
            data['sessionId'] = session_id
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


def notify_file_created(session_id: Optional[str], file_path, title: Optional[str] = None) -> None:
    """
    Notify the Web UI that a file was created so it shows a download/open link.
    Works from main process (WebSocket) and from subprocess (HTTP POST fallback).
    Safe when there is no Web session (Telegram, automation): returns immediately.
    """
    if not session_id or not file_path:
        return
    resolved = Path(file_path).resolve().as_posix()
    payload = {
        "type": "file_created",
        "sessionId": session_id,
        "filePath": resolved,
        "title": title or Path(file_path).name,
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


def notify_document_created(session_id: Optional[str], file_path, title: Optional[str] = None) -> None:
    """
    Notify the Web UI that a document was created so the Document Editor opens with it.
    Call this from any code path that creates a document (workflow, document_agent, etc.).
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
