from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import uvicorn
import threading
from vaf.core.web_interface import get_web_interface
from vaf.core.session import SessionManager
import json
from vaf.core.config import Config
from pathlib import Path
import logging

log = logging.getLogger("uvicorn")

app = FastAPI(title="VAF Local Server")

# Allow CORS for Next.js dev server
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

manager = get_web_interface()
session_mgr = SessionManager()

@app.get("/")
async def root():
    return {"status": "VAF Backend Online", "version": "1.0.0"}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        # Send initial session list
        sessions = session_mgr.list(limit=20)
        await websocket.send_json({
            "type": "session_list", 
            "sessions": [{"id": s["id"], "title": s["name"], "date": s["updated_at"]} for s in sessions]
        })

        while True:
            # Listen for client commands
            data_str = await websocket.receive_text()
            try:
                cmd = json.loads(data_str)
                type = cmd.get("type")
                
                # --- SESSION MANAGEMENT ---
                
                if type == "get_sessions":
                    sessions = session_mgr.list(limit=20)
                    await websocket.send_json({
                        "type": "session_list", 
                        "sessions": [{"id": s["id"], "title": s["name"], "date": s["updated_at"]} for s in sessions]
                    })
                
                elif type == "load_session":
                    sid = cmd.get("id")
                    try:
                        # Push command to main loop to switch session
                        manager.input_queue.put(f"__CMD__:LOAD_SESSION:{sid}")
                        
                        # 1. Load from disk (just to send history to frontend immediately)
                        loaded = session_mgr.load(sid)
                        
                        # Helper to clean historical messages (same logic as run.py)
                        import re
                        def clean_history_text(text):
                            if not text: return ""
                            # Convert rich dim to think tags
                            text = re.sub(r'\[dim\]', '<think>', text, flags=re.IGNORECASE)
                            text = re.sub(r'\[white dim\]', '<think>', text, flags=re.IGNORECASE)
                            text = re.sub(r'\[.*?dim.*?\]', '<think>', text, flags=re.IGNORECASE)
                            text = text.replace('[/dim]', '</think>')
                            if '<think>' in text and '</think>' not in text:
                                text = text.replace('[/]', '</think>')
                            # Strip remaining tags
                            text = re.sub(r'\[\/?[^\]]+\]', '', text)
                            return text
                        
                        # 3. Send history to Frontend
                        # Serialize messages for JSON
                        frontend_messages = []
                        for msg in loaded.messages:
                            role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", "user")
                            content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", "")
                            timestamp = msg.get("timestamp") if isinstance(msg, dict) else getattr(msg, "timestamp", None)
                            
                            # Clean content if it's from assistant (remove legacy artifacts)
                            if role == "assistant":
                                content = clean_history_text(content)
                            
                            frontend_messages.append({
                                "role": role,
                                "content": content,
                                "timestamp": timestamp
                            })

                        await websocket.send_json({
                            "type": "history_update",
                            "messages": frontend_messages,
                            "sessionId": sid
                        })
                    except Exception as e:
                        import traceback
                        traceback.print_exc()
                        print(f"Load error: {e}")

                elif type == "delete_session":
                    sid = cmd.get("id")
                    session_mgr.delete(sid)
                    # Broadcast update
                    sessions = session_mgr.list(limit=20)
                    await manager.broadcast({
                        "type": "session_list", 
                        "sessions": [{"id": s["id"], "title": s["name"], "date": s["updated_at"]} for s in sessions]
                    })

                elif type == "new_session":
                    # Push command to main loop to create new session
                    manager.input_queue.put("__CMD__:NEW_SESSION")
                    
                    # Create new session object AND SAVE IT IMMEDIATELY (temp, main loop will take over)
                    new_sess = session_mgr.new()
                    session_mgr.save(new_sess) 
                    
                    # Refresh list
                    sessions = session_mgr.list(limit=20)
                    
                    await websocket.send_json({
                        "type": "session_list", 
                        "sessions": [{"id": s["id"], "title": s["name"], "date": s["updated_at"]} for s in sessions]
                    })
                    
                    # Clear frontend chat
                    await websocket.send_json({
                        "type": "history_update",
                        "messages": [],
                        "sessionId": new_sess.id
                    })

                elif type == "rename_session":
                    sid = cmd.get("id")
                    new_name = cmd.get("newName")
                    if sid and new_name:
                        session_mgr.rename(sid, new_name)
                        # Notify Main Loop to update in-memory object
                        manager.input_queue.put(f"__CMD__:RENAME_SESSION:{sid}:{new_name}")
                        
                        # Broadcast update
                        sessions = session_mgr.list(limit=20)
                        await manager.broadcast({
                            "type": "session_list", 
                            "sessions": [{"id": s["id"], "title": s["name"], "date": s["updated_at"]} for s in sessions]
                        })

                elif type == "get_config":
                     # Send current config to frontend
                     cfg = Config.load()
                     await websocket.send_json({
                         "type": "config_update",
                         "config": cfg
                     })

                elif type == "get_models":
                    # Scan models directory for .gguf files
                    # Use absolute path based on project root (parent of 'vaf' package)
                    # core/web_server.py -> core/ -> vaf/ -> VAF/ -> VAF/models
                    project_root = Path(__file__).parent.parent.parent
                    models_dir = project_root / "models"
                    print(f"[DEBUG] Looking for models in: {models_dir}")
                    models = []
                    if models_dir.exists():
                        models = [f.name for f in models_dir.glob("*.gguf")]
                        print(f"[DEBUG] Found models: {models}")
                    else:
                        print(f"[DEBUG] Models directory not found at {models_dir}")
                    
                    await websocket.send_json({
                        "type": "models_list",
                        "models": models
                    })

                elif type == "get_api_models":
                    # Fetch available models from API providers
                    provider = cmd.get("provider", "openai")
                    api_key = cmd.get("api_key", "")
                    models = []
                    
                    try:
                        if provider == "openai" and api_key:
                            import httpx
                            async with httpx.AsyncClient() as client:
                                resp = await client.get(
                                    "https://api.openai.com/v1/models",
                                    headers={"Authorization": f"Bearer {api_key}"},
                                    timeout=10.0
                                )
                                if resp.status_code == 200:
                                    data = resp.json()
                                    # Filter to chat models only
                                    models = sorted([
                                        m["id"] for m in data.get("data", [])
                                        if "gpt" in m["id"] or "o1" in m["id"] or "o3" in m["id"]
                                    ])
                        elif provider == "anthropic":
                            # Anthropic doesn't have a public models endpoint, use hardcoded list
                            models = [
                                "claude-3-5-sonnet-20241022",
                                "claude-3-5-haiku-20241022", 
                                "claude-3-opus-20240229",
                                "claude-3-sonnet-20240229",
                                "claude-3-haiku-20240307",
                            ]
                        elif provider == "deepseek" and api_key:
                            import httpx
                            async with httpx.AsyncClient() as client:
                                resp = await client.get(
                                    "https://api.deepseek.com/models",
                                    headers={"Authorization": f"Bearer {api_key}"},
                                    timeout=10.0
                                )
                                if resp.status_code == 200:
                                    data = resp.json()
                                    models = [m["id"] for m in data.get("data", [])]
                                else:
                                    models = ["deepseek-chat", "deepseek-coder"]
                        elif provider == "google":
                            models = [
                                "gemini-1.5-pro-latest",
                                "gemini-1.5-flash-latest",
                                "gemini-1.0-pro",
                            ]
                        elif provider == "openrouter" and api_key:
                            import httpx
                            async with httpx.AsyncClient() as client:
                                resp = await client.get(
                                    "https://openrouter.ai/api/v1/models",
                                    headers={"Authorization": f"Bearer {api_key}"},
                                    timeout=10.0
                                )
                                if resp.status_code == 200:
                                    data = resp.json()
                                    models = [m["id"] for m in data.get("data", [])][:50]  # Limit
                    except Exception as e:
                        log.error(f"Failed to fetch models for {provider}: {e}")
                    
                    await websocket.send_json({
                        "type": "api_models_list",
                        "provider": provider,
                        "models": models
                    })

                elif type == "save_config":
                    new_config = cmd.get("config")
                    if new_config:
                        Config.save(new_config)
                        manager.input_queue.put("__CMD__:RELOAD_CONFIG")
                        await websocket.send_json({
                            "type": "config_saved",
                            "status": "success"
                        })

                elif type == "chat":
                    content = cmd.get("content")
                    if content:
                        manager.input_queue.put(content)
                        # Ack to console
                        print(f"[WebUI] Received input: {content[:20]}...")

            except json.JSONDecodeError:
                pass
                
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        print(f"WebSocket error: {e}")
        manager.disconnect(websocket)


def run_server(host="127.0.0.1", port=8001):
    """Run the Uvicorn server."""
    # Store the loop so the TUI thread can schedule updates
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    manager.set_server_loop(loop)
    
    config = uvicorn.Config(app=app, host=host, port=port, loop="asyncio", log_level="error")
    server = uvicorn.Server(config)
    
    # We run this in the thread provided by the caller
    loop.run_until_complete(server.serve())

def start_background_server(host="127.0.0.1", port=8001):
    """Start server in a daemon thread."""
    t = threading.Thread(target=run_server, args=(host, port), daemon=True)
    t.start()
    return t
