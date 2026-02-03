import asyncio
import json
import logging
import os
import time
from typing import List, Dict, Any, Optional

# Throttle log pushes to WebUI so typing and UI stay responsive (max ~3 log updates/sec)
LOG_PUSH_THROTTLE_SEC = 0.35
from fastapi import WebSocket
from vaf.core.platform import Platform
from vaf.core.log_helper import append_domain_log
from pathlib import Path

import queue

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

    def set_connection_user(self, websocket: WebSocket, user_id: str, username: Optional[str] = None) -> None:
        """Store user id (and optionally username) for this connection (e.g. for RAG scope and User identity block)."""
        self.connection_users[websocket] = user_id
        if username is not None:
            self.connection_usernames[websocket] = username

    def get_connection_user(self, websocket: WebSocket) -> Optional[str]:
        """Get user id for this connection, or None."""
        return self.connection_users.get(websocket)

    def get_connection_username(self, websocket: WebSocket) -> Optional[str]:
        """Get username for this connection, or None."""
        return self.connection_usernames.get(websocket)

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
        
        # DEBUG
        # if message.get('type') == 'agent_message_update':
        #    print(f"[DEBUG] Broadcasting update to {session_id}: {message.get('content')[:20]}...")
        
        disconnected = []
        for connection in self.active_connections:
            conn_session = self.connection_sessions.get(connection)
            # Send if:
            # 1. Connection is subscribed to this session, OR
            # 2. Connection is not subscribed to any session yet (new connection)
            if conn_session is None or conn_session == session_id:
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

    def emit_stats(self, stats: dict, session_id: str = None):
        """Emit context/token statistics."""
        self.last_stats = stats
        self._push_session_update(session_id, {
            "type": "stats",
            "stats": stats
        })

    # ═══════════════════════════════════════════════════════════════════════════
    # THREAD-SAFE BRIDGING
    # ═══════════════════════════════════════════════════════════════════════════
    
    def set_server_loop(self, loop):
        """Set the asyncio event loop for thread-safe broadcasting."""
        self._server_loop = loop
        if self.tools_cache:
            asyncio.run_coroutine_threadsafe(
                self.broadcast({"type": "tools_list", "tools": self.tools_cache}),
                self._server_loop
            )
        
    def push_update(self, data: dict):
        """Thread-safe push update (global broadcast)."""
        if self._server_loop:
            asyncio.run_coroutine_threadsafe(self.broadcast(data), self._server_loop)

    def _push_session_update(self, session_id: Optional[str], data: dict):
        """
        Thread-safe push update with session scoping.
        """
        if session_id:
            data['sessionId'] = session_id
            if self._server_loop:
                asyncio.run_coroutine_threadsafe(
                    self.broadcast_to_session(session_id, data),
                    self._server_loop
                )
            else:
                # WARNING: No server loop means messages are silently dropped!
                append_domain_log("webui", f"[WARNING] No server loop! Message dropped for session {session_id}")
        else:
            # Fallback to global broadcast for non-session events
            self.push_update(data)


# Global Accessor
def get_web_interface():
    return WebInterfaceManager()
