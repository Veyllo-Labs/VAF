# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
import asyncio
import json
import logging
from typing import Dict, List, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager
import uvicorn
import concurrent.futures
from vaf.version import __version__

from vaf.core.protocol import (
    VAFMessage, 
    CommandRequest, 
    EventFrame, 
    LogPayload,
    SystemStatusPayload
)
from vaf.core.agent import Agent

# Configure Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vaf.gateway")

# --- UI Patching (Redirect CLI output to WS) ---
from vaf.cli.ui import UI

class GatewayUIAdapter:
    def __init__(self, manager):
        self.manager = manager
    
    def event(self, category, message, style=""):
        asyncio.run_coroutine_threadsafe(
            self.manager.broadcast(EventFrame(
                source="system",
                type="log",
                payload=LogPayload(level="info", message=f"[{category}] {message}", component="agent").model_dump()
            )),
            loop=asyncio.get_running_loop()
        )

    def error(self, message):
        asyncio.run_coroutine_threadsafe(
            self.manager.broadcast(EventFrame(
                source="system",
                type="log",
                payload=LogPayload(level="error", message=message, component="agent").model_dump()
            )),
            loop=asyncio.get_running_loop()
        )
    
    # Mock other UI methods to be silent or redirect
    def print(self, *args, **kwargs): pass
    def success(self, msg): self.event("Success", msg)
    def warning(self, msg): self.event("Warning", msg)

class ConnectionManager:
    """
    Manages WebSocket connections (The 'Gateway' Pattern).
    Keeps track of clients like CLI, WebUI, or Discord Bridge.
    """
    def __init__(self):
        # active_connections: client_id -> WebSocket
        self.active_connections: Dict[str, WebSocket] = {}
        self.client_types: Dict[str, str] = {} # id -> type (cli, web, etc)

    async def connect(self, websocket: WebSocket, client_id: str, client_type: str = "unknown"):
        await websocket.accept()
        self.active_connections[client_id] = websocket
        self.client_types[client_id] = client_type
        logger.info(f"Client connected: {client_id} ({client_type})")
        
        # Announce new connection
        await self.broadcast(EventFrame(
            source="gateway",
            type="log",
            payload=LogPayload(level="info", message=f"New connection: {client_type}", component="gateway").model_dump()
        ))

    async def disconnect(self, client_id: str):
        if client_id in self.active_connections:
            del self.active_connections[client_id]
            del self.client_types[client_id]
            logger.info(f"Client disconnected: {client_id}")

    async def send_personal_message(self, message: VAFMessage, client_id: str):
        if client_id in self.active_connections:
            await self.active_connections[client_id].send_text(message.model_dump_json())

    async def broadcast(self, message: VAFMessage, exclude: Optional[str] = None):
        """Sends a message to all connected clients."""
        json_msg = message.model_dump_json()
        for c_id, connection in self.active_connections.items():
            if c_id != exclude:
                try:
                    await connection.send_text(json_msg)
                except Exception as e:
                    logger.error(f"Failed to send to {c_id}: {e}")

# Global Manager
manager = ConnectionManager()
agent_instance: Optional[Agent] = None
executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup logic
    logger.info("VAF Gateway starting up...")
    
    # Initialize Agent
    global agent_instance
    logger.info("Initializing VAF Agent...")
    try:
        # Patch UI to redirect to Gateway
        # Note: This is a hacky global patch, but necessary since Agent uses static UI methods
        adapter = GatewayUIAdapter(manager)
        UI.event = adapter.event
        UI.error = adapter.error
        UI.success = adapter.success
        UI.warning = adapter.warning
        
        agent_instance = Agent(verbose=False, run_kind="chat")
        
        # Run init_chat in thread to avoid blocking startup
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(executor, agent_instance.init_chat)
        logger.info("VAF Agent ready.")
    except Exception as e:
        logger.error(f"Failed to init agent: {e}")

    yield
    # Shutdown logic
    logger.info("VAF Gateway shutting down...")
    executor.shutdown()

app = FastAPI(title="VAF Gateway", version=__version__, lifespan=lifespan)

@app.get("/")
async def root():
    return {"status": "online", "system": "VAF Agentic Gateway", "version": __version__}

@app.get("/api/file")
async def download_file(path: str = Query(..., description="Absolute path to local file")):
    from vaf.core.platform import Platform
    from pathlib import Path
    import mimetypes

    try:
        target = Platform.normalize_path(path)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid path")

    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    allowed_roots = [
        Platform.documents_dir().resolve(),
        Platform.downloads_dir().resolve(),
        Platform.data_dir().resolve(),
    ]

    if not any(target.is_relative_to(root) for root in allowed_roots):
        raise HTTPException(status_code=403, detail="Access denied")

    mime_type, _ = mimetypes.guess_type(str(target))
    return FileResponse(
        path=str(target),
        media_type=mime_type or "application/octet-stream",
        filename=target.name,
    )

def run_agent_step(agent: Agent, text: str, context: dict, server_user_scope_id: str = None, server_username: str = None):
    """Blocking function to run the agent step.

    Args:
        agent: The VAF Agent instance
        text: User input text
        context: Client-sent context (platform, etc. — user_scope_id is IGNORED from here)
        server_user_scope_id: Server-validated user_scope_id from JWT (trusted)
        server_username: Server-validated username from JWT (trusted)
    """
    from vaf.core.config import Config

    # Session context: so the agent knows which channel (e.g. Discord vs CLI)
    source = (context or {}).get("platform", "cli")
    if isinstance(source, str) and source.strip():
        agent._current_chat_source = source.strip().lower()
    else:
        agent._current_chat_source = "cli"

    # User context: set username and scope on agent for tool execution
    # (email, whatsapp, contacts, user_identity, etc. all read _current_username)
    if server_username:
        agent._current_username = server_username
    elif not getattr(agent, "_current_username", None):
        agent._current_username = (Config.get("local_admin_username") or "admin")

    if server_user_scope_id:
        from uuid import UUID as _UUID
        agent._current_user_scope_id = server_user_scope_id
        # Also store as UUID for memory tools
        try:
            agent._current_user_scope_id = _UUID(str(server_user_scope_id))
        except (ValueError, TypeError):
            pass
    elif not getattr(agent, "_current_user_scope_id", None):
        from uuid import UUID as _UUID
        from vaf.core.config import get_local_admin_scope_id
        local_scope = get_local_admin_scope_id()
        try:
            agent._current_user_scope_id = _UUID(str(local_scope))
        except (ValueError, TypeError):
            pass

    # RAG: fetch memory context for this turn (pre-injection, before LLM)
    # SECURITY: user_scope_id comes from server-validated JWT, NOT from client context
    memory_context = ""
    try:
        if Config.get("memory_enabled", True):
            from vaf.memory.rag import run_memory_search_sync
            user_scope_id = getattr(agent, "_current_user_scope_id", None)
            k = int(Config.get("memory_rag_k", 5))
            k = max(1, min(20, k))
            memory_context = run_memory_search_sync(
                query=text, k=k, user_scope_id=user_scope_id, caller="gateway"
            )
    except Exception:
        memory_context = ""

    result = agent.chat_step(
        user_input=text,
        stream_callback=None,  # simplified for now
        memory_context=memory_context or None,
    )
    return result

@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str, client_type: str = "cli"):
    await manager.connect(websocket, client_id, client_type)

    # SECURITY: Extract user identity from authenticated session (server-side),
    # so clients cannot impersonate other users by sending fake credentials.
    server_user_scope_id = None
    server_username = None
    user_state = getattr(websocket.state, "user", None) if hasattr(websocket, "state") else None
    if user_state and isinstance(user_state, dict):
        server_user_scope_id = user_state.get("user_scope_id")
        server_username = user_state.get("username")

    try:
        while True:
            data = await websocket.receive_text()
            try:
                # 1. Parse generic message to inspect headers
                raw_msg = json.loads(data)
                logger.debug(f"Received from {client_id}: {raw_msg}")

                # Echo / Acknowledge
                await manager.send_personal_message(
                    EventFrame(
                        source="gateway",
                        type="status",
                        payload=SystemStatusPayload(state="thinking", active_agent="default").model_dump()
                    ),
                    client_id
                )

                if raw_msg.get("type") == "agent.prompt":
                    if not agent_instance:
                         await manager.send_personal_message(
                            EventFrame(source="gateway", type="error", payload={"message": "Agent not initialized"}),
                            client_id
                        )
                         continue

                    # Execute Agent in ThreadPool
                    text = raw_msg.get("payload", {}).get("text", "")
                    context = raw_msg.get("payload", {}).get("context", {})

                    # SECURITY: Strip user_scope_id from client context — use server-validated value only
                    context.pop("user_scope_id", None)

                    loop = asyncio.get_running_loop()
                    response_text = await loop.run_in_executor(
                        executor,
                        run_agent_step,
                        agent_instance,
                        text,
                        context,
                        server_user_scope_id,
                        server_username,
                    )
                    
                    # Send Completion
                    completion = EventFrame(
                        source="agent",
                        type="response",
                        payload={"text": response_text}
                    )
                    await manager.broadcast(completion)
                    
                    # Back to Idle
                    await manager.broadcast(
                        EventFrame(
                            source="gateway", 
                            type="status", 
                            payload=SystemStatusPayload(state="idle", active_agent="default").model_dump()
                        )
                    )

            except json.JSONDecodeError:
                logger.error("Invalid JSON received")
            except Exception as e:
                logger.error(f"Error processing message: {e}")
                
    except WebSocketDisconnect:
        await manager.disconnect(client_id)

def run_gateway(host: str = "127.0.0.1", port: int = 8000):
    """Entry point to run the gateway programmatically."""
    uvicorn.run(app, host=host, port=port)

if __name__ == "__main__":
    run_gateway()
