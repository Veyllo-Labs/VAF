import asyncio
import json
import logging
from typing import Dict, List, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from contextlib import asynccontextmanager
import uvicorn
import concurrent.futures

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
        
        agent_instance = Agent(verbose=False)
        
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

app = FastAPI(title="VAF Gateway", lifespan=lifespan)

@app.get("/")
async def root():
    return {"status": "online", "system": "VAF Agentic Gateway", "version": "0.1.0"}

def run_agent_step(agent: Agent, text: str, context: dict):
    """Blocking function to run the agent step."""
    # This runs in a thread
    result = agent.chat_step(
        user_input=text,
        # We can implement a stream callback here to push deltas to WS
        stream_callback=None # simplified for now
    )
    return result

@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str, client_type: str = "cli"):
    await manager.connect(websocket, client_id, client_type)
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
                    
                    loop = asyncio.get_running_loop()
                    response_text = await loop.run_in_executor(
                        executor, 
                        run_agent_step, 
                        agent_instance, 
                        text, 
                        context
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
