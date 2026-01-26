import asyncio
import json
import logging
from typing import List, Dict, Any
from fastapi import WebSocket

import queue

class WebInterfaceManager:
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
        self.agent_instance = None
        # Queue for incoming chat messages from Web UI -> Main Loop
        self.input_queue = queue.Queue()
        
        self.latest_state = {
            "status": "idle", # idle, thinking, tool_use
            "last_message": None,
            "logs": [],
            "tasks": [],
            "system_metrics": {}
        }
        self.initialized = True
        self.agent_instance = None # Reference to the active Agent

    def register_agent(self, agent):
        """Register the active agent instance to allow control from Web UI."""
        self.agent_instance = agent

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        # Send initial state
        await websocket.send_text(json.dumps({
            "type": "state_full",
            "data": self.latest_state
        }))

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        """Broadcast a message to all connected clients."""
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_text(json.dumps(message))
            except Exception:
                disconnected.append(connection)
        
        for conn in disconnected:
            self.disconnect(conn)

    # --- Public API for Agent/TUI to call ---

    def update_status(self, status: str):
        """Update agent status (idle, thinking, etc)."""
        self.latest_state["status"] = status
        self.push_update({"type": "status_update", "status": status})

    def log(self, message: str, level: str = "info", source: str = "system"):
        """Add a log entry."""
        log_entry = {
            "timestamp":  __import__("datetime").datetime.now().isoformat(),
            "message": message,
            "level": level,
            "source": source
        }
        self.latest_state["logs"].append(log_entry)
        # Keep logs capped at 1000
        if len(self.latest_state["logs"]) > 1000:
             self.latest_state["logs"].pop(0)
             
        self.push_update({"type": "new_log", "entry": log_entry})

    def set_tasks(self, tasks: List[Dict]):
        """Update the list of active/pending tasks."""
        self.latest_state["tasks"] = tasks
        self.push_update({"type": "tasks_update", "tasks": tasks})
        
    def emit_agent_message(self, role: str, content: str, session_id: str = None):
        """Emit a message update. Content is the FULL message so far."""
        # Use push_update to ensure thread-safety from Main Thread to Server Loop
        self.push_update({
            "type": "agent_message_update", 
            "role": role, 
            "content": content,
            "sessionId": session_id
        })

    def _sync_broadcast(self, data):
        """Helper to run async broadcast from sync context."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We are in an event loop (e.g. inside FastAPI), create task
                loop.create_task(self.broadcast(data))
            else:
                # We are purely sync, can't easily broadcast to running loop in other thread
                # This is a limitation, but usually TUI runs in main thread and Server in another.
                # We need a thread-safe queue or similar if this happens.
                # For now, we assume the Server thread pulls state or we use a thread-safe wrapper.
                pass
        except RuntimeError:
            # No event loop
            pass
            
    # Thread-safe bridging
    # Since TUI runs in MainThread and FastAPI in a separate Thread,
    # we need a way to push updates.
    # The simplest way is to having the FastAPI loop periodically poll OR
    # use `run_coroutine_threadsafe` if we have access to the server loop.
    
    _server_loop = None
    
    def set_server_loop(self, loop):
        self._server_loop = loop
        
    def push_update(self, data: dict):
        """Thread-safe push update."""
        if self._server_loop:
            asyncio.run_coroutine_threadsafe(self.broadcast(data), self._server_loop)

# Global Accessor
def get_web_interface():
    return WebInterfaceManager()
