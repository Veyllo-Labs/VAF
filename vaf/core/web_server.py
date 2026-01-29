from vaf.startup_logger import log
log("WebServer", "Module load started")

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import uvicorn
import threading
log("WebServer", "Basic imports done")

from vaf.core.web_interface import get_web_interface
from vaf.core.session import SessionManager
from vaf.cli.autosuggest import SmartAutoSuggest
import json
from vaf.core.config import Config
from pathlib import Path
from typing import Optional, List
import logging
from vaf.core.tray_context import TrayContext
log("WebServer", "VAF imports done")

log_uvicorn = logging.getLogger("uvicorn")

app = FastAPI(title="VAF Local Server")

# Allow CORS for Next.js dev server
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

log("WebServer", "Getting WebInterfaceManager...")
manager = get_web_interface()
log("WebServer", "Getting SessionManager...")
session_mgr = SessionManager()
log("WebServer", "SmartAutoSuggest will be lazy loaded...")
autosuggest = None
tray_context = TrayContext()
log("WebServer", "Module initialization complete")

def get_autosuggest():
    global autosuggest
    if autosuggest is None:
        log("WebServer", "Lazy loading SmartAutoSuggest...")
        autosuggest = SmartAutoSuggest()
    return autosuggest

@app.on_event("startup")
async def startup_event():
    # Set the event loop for thread-safe broadcasting
    loop = asyncio.get_running_loop()
    manager.set_server_loop(loop)
    log("WebServer", "VAF Web Interface: Event loop registered")
    
    # Register TTS Callbacks for UI sync
    from vaf.core.speech import SpeechManager
    sm = SpeechManager.get_instance()
    
    def on_tts_loading():
        if manager._server_loop:
            asyncio.run_coroutine_threadsafe(
                manager.broadcast({"type": "tts_state", "status": "loading"}),
                manager._server_loop
            )

    def on_tts_start(text):
        if manager._server_loop:
            asyncio.run_coroutine_threadsafe(
                manager.broadcast({"type": "tts_state", "status": "playing", "text": text}),
                manager._server_loop
            )
            
    def on_tts_end():
        if manager._server_loop:
            asyncio.run_coroutine_threadsafe(
                manager.broadcast({"type": "tts_state", "status": "stopped"}),
                manager._server_loop
            )
            
    sm.on_speech_loading = on_tts_loading
    sm.on_speech_start = on_tts_start
    sm.on_speech_end = on_tts_end

def _detect_language_simple(text: str) -> str:
    """Simple heuristic for language detection (de/en)."""
    t = text.lower()
    # German indicators
    de_words = [" das ", " und ", " der ", " die ", " ist ", " nicht ", " ich ", " sie ", " es ", " wie ", " was ", " eine ", " ein ", " mit ", " von "]
    if any(w in t for w in de_words): return "de"
    if any(ch in t for ch in ["ä", "ö", "ü", "ß"]): return "de"
    return "en" # Default fallback

@app.get("/")
async def root():
    return {"status": "VAF Backend Online", "version": "1.0.0"}

from pydantic import BaseModel

class WorkflowUpdate(BaseModel):
    type: str
    sessionId: Optional[str] = None
    workflowId: Optional[str] = None
    name: Optional[str] = None
    steps: Optional[List] = None
    stepId: Optional[str] = None
    status: Optional[str] = None
    progress: Optional[int] = None
    result: Optional[str] = None

class Heartbeat(BaseModel):
    client_id: str
    timestamp: float = 0.0

@app.post("/api/heartbeat")
async def receive_heartbeat(hb: Heartbeat):
    """Receive heartbeat from CLI clients to keep server active."""
    tray_context.register_activity()
    return {"status": "ok", "active": True}

@app.post("/api/workflow/update")
async def receive_workflow_update(update: WorkflowUpdate):
    """Receive workflow updates from external processes (like separate terminals)."""
    data = update.dict(exclude_none=True)
    if manager._server_loop:
        # Broadcast to relevant session
        if update.sessionId:
            await manager.broadcast_to_session(update.sessionId, data)
        else:
            await manager.broadcast(data)
    return {"status": "ok"}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    print(f"[WebSocket] Connection attempt from {websocket.client.host}")
    log("API", f"WebSocket connection attempt from {websocket.client.host}")
    try:
        await manager.connect(websocket)
        print(f"[WebSocket] Connected! Active: {len(manager.active_connections)}")
        log("API", f"WebSocket connected! Active: {len(manager.active_connections)}")
        tray_context.set_websocket_count(len(manager.active_connections)) # Update active count
    except Exception as e:
        log("API", f"WebSocket handshake failed: {e}")
        raise e
    try:
        # Send initial session list
        sessions = session_mgr.list(limit=20)
        await websocket.send_json({
            "type": "session_list", 
            "sessions": [{"id": s["id"], "title": s["name"], "date": s["updated_at"], "messageCount": s["message_count"]} for s in sessions]
        })
        # Send cached stats if available
        if manager.last_stats:
             await websocket.send_json({
                "type": "stats",
                "stats": manager.last_stats
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
                        "sessions": [{"id": s["id"], "title": s["name"], "date": s["updated_at"], "messageCount": s["message_count"]} for s in sessions]
                    })                
                elif type == "load_session":
                    sid = cmd.get("id")
                    try:
                        # Subscribe this connection to the session for scoped updates
                        manager.subscribe_to_session(websocket, sid)
                        
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

                        # Check if this session is currently active in the agent
                        is_active = False
                        try:
                            # If agent is working on this session ID right now
                            # We use internal _session_id which we synced in agent.py
                            if (manager.agent_instance and 
                                getattr(manager.agent_instance, '_session_id', None) == sid and
                                manager.latest_state.get("status") != "idle"):
                                is_active = True
                        except: pass

                        await websocket.send_json({
                            "type": "history_update",
                            "messages": frontend_messages,
                            "sessionId": sid,
                            "isActive": is_active,
                            "currentStatus": manager.latest_state.get("status", "idle") if is_active else "idle"
                        })

                        # 4. Send initial estimated stats (so UI is not empty)
                        try:
                            # Estimate based on loaded history
                            total_chars = sum(len(str(m.get("content", ""))) for m in frontend_messages)
                            est_tokens = total_chars // 3  # Rough estimate
                            
                            # Get max context from config
                            cfg = Config.load()
                            max_ctx = cfg.get("n_ctx", 8192)
                            is_api = cfg.get("provider", "local") != "local"
                            
                            if is_api and max_ctx <= 16384:
                                max_ctx = 128000
                                
                            stats = {
                                "used": est_tokens,
                                "total": max_ctx,
                                "percent": (est_tokens / max_ctx) if max_ctx else 0.0,
                                "api": is_api
                            }
                            await websocket.send_json({
                                "type": "stats",
                                "stats": stats
                            })
                        except Exception as e:
                            print(f"[WebServer] Stats estimation error: {e}")

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
                        "sessions": [{"id": s["id"], "title": s["name"], "date": s["updated_at"], "messageCount": s["message_count"]} for s in sessions]
                    })

                elif type == "new_session":
                    # Push command to main loop to create new session
                    manager.input_queue.put("__CMD__:NEW_SESSION")
                    
                    # Create new session object AND SAVE IT IMMEDIATELY (temp, main loop will take over)
                    new_sess = session_mgr.new()
                    session_mgr.save(new_sess)
                    
                    # Subscribe this connection to the new session for scoped updates
                    manager.subscribe_to_session(websocket, new_sess.id)
                    
                    # Refresh list
                    sessions = session_mgr.list(limit=20)
                    
                    await websocket.send_json({
                        "type": "session_list", 
                        "sessions": [{"id": s["id"], "title": s["name"], "date": s["updated_at"], "messageCount": s["message_count"]} for s in sessions]
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
                            "sessions": [{"id": s["id"], "title": s["name"], "date": s["updated_at"], "messageCount": s["message_count"]} for s in sessions]
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
                    # print(f"[DEBUG] Looking for models in: {models_dir}")
                    models = []
                    if models_dir.exists():
                        models = [f.name for f in models_dir.glob("*.gguf")]
                        # print(f"[DEBUG] Found models: {models}")
                    else:
                        # print(f"[DEBUG] Models directory not found at {models_dir}")
                        pass
                    
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
                        log("WebServer", f"Failed to fetch models for {provider}: {e}")
                    
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

                elif type == "get_autosuggest":
                    text = cmd.get("text", "")
                    if text:
                        # Use the internal _get_best_suggestion method
                        suggestion = get_autosuggest()._get_best_suggestion(text)
                        await websocket.send_json({
                            "type": "autosuggest_result",
                            "suggestion": suggestion
                        })

                elif type == "chat":
                    content = cmd.get("content")
                    files = cmd.get("files", [])  # List of file objects with {name, data, mimeType}
                    
                    if content or files:
                        # Learn from user input
                        if content:
                            get_autosuggest().learn(content)
                        
                        # Process files if attached
                        if files:
                            print(f"[WebUI] Processing {len(files)} attached file(s)...")
                            file_contents = await process_uploaded_files(files)
                            if file_contents:
                                # Append file contents to message (like CLI @filename behavior)
                                content = content + "\n\n" + file_contents if content else file_contents
                        
                        # Use TaskQueue for serialized execution
                        from vaf.core.task_queue import TaskQueue
                        tq = TaskQueue()
                        
                        # Get session ID: prefer from message, then connection, then fallback
                        session_id = cmd.get("sessionId") or manager.get_session_for_connection(websocket)
                        if not session_id:
                            # Fallback if not subscribed yet (should be rare)
                            session_id = "web-default"
                            
                        # Add to queue
                        tq.add(session_id=session_id, input_text=content, source="web")
                        
                        # Ack to console
                        file_info = f" [{len(files)} file(s)]" if files else ""
                        print(f"[WebUI] Queued input{file_info} for session {session_id}: {content[:50]}...")
                
                elif type == "get_tools":
                    # Return list of available tools from agent
                    try:
                        # Access global agent if available
                        from vaf.cli.cmd.run import global_agent
                        if global_agent and hasattr(global_agent, 'tools'):
                            tools_list = [
                                {
                                    "name": name,
                                    "description": getattr(tool, 'description', 'No description'),
                                    "category": getattr(tool, 'category', 'general')
                                }
                                for name, tool in global_agent.tools.items()
                            ]
                            await websocket.send_json({
                                "type": "tools_list",
                                "tools": tools_list
                            })
                        else:
                            await websocket.send_json({
                                "type": "tools_list",
                                "tools": []
                            })
                    except Exception as e:
                        await websocket.send_json({
                            "type": "tools_list",
                            "tools": [],
                            "error": str(e)
                        })
                
                elif type == "get_workflows":
                    # Return list of available workflow templates
                    try:
                        from vaf.workflows.templates import list_templates
                        workflows = list_templates()
                        await websocket.send_json({
                            "type": "workflows_list",
                            "workflows": workflows
                        })
                    except Exception as e:
                        await websocket.send_json({
                            "type": "workflows_list",
                            "workflows": [],
                            "error": str(e)
                        })
                
                elif type == "get_automations":
                    # Return list of saved automations
                    try:
                        from vaf.core.automation import AutomationManager
                        mgr = AutomationManager()
                        tasks = mgr.list()
                        automations_list = [
                            {
                                "id": task.id,
                                "name": task.name,
                                "description": task.description,
                                "frequency": task.frequency,
                                "time": task.time,
                                "enabled": task.enabled,
                                "next_run": task.next_run_iso,
                                "last_run": task.last_run
                            }
                            for task in tasks
                        ]
                        await websocket.send_json({
                            "type": "automations_list",
                            "automations": automations_list
                        })
                    except Exception as e:
                        await websocket.send_json({
                            "type": "automations_list",
                            "automations": [],
                            "error": str(e)
                        })
                
                elif type == "process_audio":
                    # Process audio for STT - OFFLINE ONLY (faster-whisper)
                    import base64
                    import tempfile
                    import os
                    
                    print(f"DEBUG: process_audio request received (OFFLINE MODE)") # DEBUG
                    temp_path = None
                    try:
                        audio_b64 = cmd.get("audio")
                        if not audio_b64:
                            print("DEBUG: No audio data provided") # DEBUG
                            await websocket.send_json({
                                "type": "stt_error",
                                "error": "No audio data provided"
                            })
                            continue
                        
                        print(f"DEBUG: Audio data length: {len(audio_b64)}") # DEBUG

                        # Decode base64 audio
                        audio_data = base64.b64decode(audio_b64)
                        
                        # Save to temp file (WebM from browser)
                        with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as temp_audio:
                            temp_audio.write(audio_data)
                            temp_path = temp_audio.name
                        print(f"DEBUG: Saved audio to {temp_path}") # DEBUG
                        
                        try:
                            # Check STT enabled
                            from vaf.core.speech import SpeechManager
                            sm = SpeechManager.get_instance()
                            
                            if not sm.is_stt_enabled():
                                print("DEBUG: STT disabled in config") # DEBUG
                                await websocket.send_json({
                                    "type": "stt_error",
                                    "error": "STT is disabled in settings"
                                })
                                continue
                            
                            # OFFLINE STT: faster-whisper
                            try:
                                print("DEBUG: Using faster-whisper (OFFLINE)...") # DEBUG
                                from faster_whisper import WhisperModel
                                
                                # Initialize model (base = good speed/accuracy balance)
                                print("DEBUG: Initializing WhisperModel (base, offline)...") # DEBUG
                                model = WhisperModel("base", device="cpu", compute_type="int8")
                                
                                # Transcribe
                                print(f"DEBUG: Transcribing {temp_path}...") # DEBUG
                                segments, info = model.transcribe(temp_path, beam_size=5)
                                text = " ".join([segment.text for segment in segments])
                                print(f"DEBUG: Transcription result: '{text}'") # DEBUG
                                
                                await websocket.send_json({
                                    "type": "stt_result",
                                    "text": text.strip()
                                })
                            except ImportError as ie:
                                print(f"DEBUG: faster-whisper not installed: {ie}") # DEBUG
                                await websocket.send_json({
                                    "type": "stt_error",
                                    "error": "faster-whisper not installed. Install with: pip install faster-whisper"
                                })
                            except Exception as transcribe_error:
                                print(f"DEBUG: Transcription error: {transcribe_error}") # DEBUG
                                await websocket.send_json({
                                    "type": "stt_error",
                                    "error": f"Transcription failed: {str(transcribe_error)}"
                                })
                        finally:
                            # Clean up temp file
                            if temp_path and os.path.exists(temp_path):
                                try:
                                    os.unlink(temp_path)
                                except:
                                    pass
                            
                    except Exception as e:
                        print(f"DEBUG: General Error in process_audio: {e}") # DEBUG
                        await websocket.send_json({
                            "type": "stt_error",
                            "error": str(e)
                        })
                
                elif type == "speak":
                    # TTS Output
                    text = cmd.get("text")
                    if text:
                        from vaf.core.speech import SpeechManager
                        sm = SpeechManager.get_instance()
                        
                        # Detect Language
                        lang = _detect_language_simple(text)
                        sm.speak(text, lang=lang)
                
                elif type == "stop_speech":
                    # Stop TTS
                    from vaf.core.speech import SpeechManager
                    sm = SpeechManager.get_instance()
                    sm.stop()

            except json.JSONDecodeError:
                pass
                
    except WebSocketDisconnect:
        manager.disconnect(websocket)
        tray_context.set_websocket_count(len(manager.active_connections)) # Update active count
    except Exception as e:
        print(f"WebSocket error: {e}")
        manager.disconnect(websocket)
        tray_context.set_websocket_count(len(manager.active_connections)) # Update active count


async def process_uploaded_files(files: list) -> str:
    """
    Process uploaded files and return their text content.
    Mimics CLI @filename behavior by reading file contents and formatting them.
    
    Args:
        files: List of file objects with {name, data, mimeType}
        
    Returns:
        Formatted string with file contents
    """
    import base64
    import tempfile
    import os
    from pathlib import Path
    
    if not files:
        return ""
    
    results = []
    
    for file_obj in files:
        try:
            filename = file_obj.get("name", "unknown")
            file_data = file_obj.get("data", "")
            mime_type = file_obj.get("mimeType", "")
            
            print(f"[WebUI] Processing file: {filename} ({mime_type})")
            
            # Decode base64 data
            if file_data.startswith("data:"):
                # Remove data URL prefix (e.g., "data:application/pdf;base64,")
                file_data = file_data.split(",", 1)[1] if "," in file_data else file_data
            
            decoded_data = base64.b64decode(file_data)
            
            # Save to temporary file
            file_ext = Path(filename).suffix or ".txt"
            with tempfile.NamedTemporaryFile(suffix=file_ext, delete=False) as temp_file:
                temp_file.write(decoded_data)
                temp_path = temp_file.name
            
            try:
                # Use Librarian to read file contents
                from vaf.tools.librarian import LibrarianTool
                librarian = LibrarianTool()
                
                # Read file using Librarian's _read_file method
                content = librarian._read_file(Path(temp_path), enable_chunking=True)
                
                # Format like CLI does: --- FILE: name ---\nCONTENTS\n----------------
                formatted = f"\n\n--- FILE: {filename} ---\n{content}\n----------------\n"
                results.append(formatted)
                
                print(f"[WebUI] Successfully processed: {filename}")
                
            finally:
                # Clean up temp file
                try:
                    os.unlink(temp_path)
                except:
                    pass
                    
        except Exception as e:
            error_msg = f"\n\n--- FILE: {filename} ---\n[ERROR] Failed to process file: {str(e)}\n----------------\n"
            results.append(error_msg)
            print(f"[WebUI] Error processing {filename}: {e}")
    
    return "".join(results)



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
