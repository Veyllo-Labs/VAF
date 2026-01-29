import time
import threading
import traceback
from vaf.core.agent import Agent
from vaf.core.task_queue import TaskQueue
from vaf.core.session import SessionManager
from vaf.core.web_interface import get_web_interface

def run_headless_agent():
    """
    Run a headless agent loop that processes tasks from the TaskQueue.
    This is designed to run in a background thread within the Tray App.
    """
    print("[Headless] Starting Agent Loop...")
    
    # Initialize Agent
    # We use verbose=False to keep logs clean
    try:
        agent = Agent(verbose=False, register_signals=False)
        agent.init_chat()
        
        # Register with Web Interface
        get_web_interface().register_agent(agent)
        
        # Ensure model is ready (might already be loaded by tray logic, but safe to check)
        if not agent.api_backend:
             agent.ensure_model_exists()
             agent.load_model(skip_download_check=True)
             
        print("[Headless] Agent initialized and ready.")
        
    except Exception as e:
        print(f"[Headless] Agent initialization failed: {e}")
        traceback.print_exc()
        return

    # Task Queue
    tq = TaskQueue()
    session_mgr = SessionManager()
    
    # Main Loop
    while True:
        try:
            # check for tasks
            if tq.get_queue_size() > 0:
                task = tq.get()
                
                if not task:
                    time.sleep(0.1)
                    continue
                    
                print(f"[Headless] Processing task for session {task.session_id}...")
                
                # Load Session Context
                try:
                    agent.load_session_context(task.session_id)
                    
                    # Sync internal session ID
                    if hasattr(agent, '_session_id') and agent._session_id != task.session_id:
                        agent._unregister_session()
                        agent._session_id = task.session_id
                        agent._register_session()
                        
                except Exception as e:
                    print(f"[Headless] Failed to load session context: {e}")
                    # Create fallback session?
                    # valid session is needed for chat_step to work properly with history
                    pass

                # Send initial token stats to WebUI (before processing)
                try:
                    used, total = agent.get_token_usage()
                    stats = {
                        "used": used,
                        "total": total,
                        "percent": round((used / total) * 100) if total else 0,
                        "api": bool(getattr(agent, 'api_backend', False))
                    }
                    get_web_interface().emit_stats(stats, session_id=task.session_id)
                except Exception:
                    pass

                # Process Input
                input_text = task.input_text
                
                # Check for System Commands (similar to run.py)
                if str(input_text).startswith("__CMD__"):
                    _handle_command(input_text, agent, session_mgr)
                    tq.task_done()
                    continue
                
                # Normal Chat Step
                # usage: chat_step(user_input, ...)
                try:
                    # We pass skip_input=False because we ARE providing input
                    # We disable workflows for now if they require TUI, or we need to ensure workflows work headless
                    # Agent.chat_step logic handles tool execution.

                    # Streaming callback for WebUI - accumulates chunks and sends updates
                    response_parts = []
                    def webui_stream_callback(text):
                        response_parts.append(text)
                        # Construct full text so far and send to WebUI
                        full_text = "".join(response_parts)
                        if full_text.strip():
                            get_web_interface().emit_agent_message(
                                role="assistant",
                                content=full_text,
                                session_id=task.session_id
                            )

                    response = agent.chat_step(
                        user_input=input_text,
                        stream_callback=webui_stream_callback,  # Stream to WebUI!
                        skip_input=False
                    )

                    # Final response broadcast (in case streaming missed the final state)
                    final_text = "".join(response_parts) if response_parts else response
                    if final_text:
                         get_web_interface().emit_agent_message(
                             role="assistant",
                             content=final_text,
                             session_id=task.session_id
                         )

                    # Send token/context stats to WebUI
                    try:
                        used, total = agent.get_token_usage()
                        stats = {
                            "used": used,
                            "total": total,
                            "percent": round((used / total) * 100) if total else 0,
                            "api": bool(getattr(agent, 'api_backend', False))
                        }
                        get_web_interface().emit_stats(stats, session_id=task.session_id)
                    except Exception:
                        pass  # Ignore stats errors

                    # Result is already added to history and broadcast to WebUI by agent logic
                    print("[Headless] Task complete.")
                    
                except Exception as e:
                    print(f"[Headless] Error during chat step: {e}")
                    traceback.print_exc()
                
                tq.task_done()
            else:
                # Sleep briefly to avoid busy loop
                time.sleep(0.1)
                
                # Optional: Check for sub-agent results periodically if needed
                # (Active workflows might need resuming)
                
        except Exception as e:
            print(f"[Headless] Loop error: {e}")
            time.sleep(1)

def _handle_command(cmd_str, agent, session_mgr):
    """Handle system commands from Web UI."""
    try:
        parts = cmd_str.strip().split(":")
        if len(parts) < 2: return
        
        cmd_type = parts[1]
        
        if cmd_type == "NEW_SESSION":
            # Agent/Session logic is handled by Web Server mostly, 
            # but we might need to reset agent state
            agent.init_chat()
            
        elif cmd_type == "RELOAD_CONFIG":
            from vaf.core.config import Config
            new_cfg = Config.load()
            agent.config = new_cfg
            
    except Exception as e:
        print(f"[Headless] Command error: {e}")
