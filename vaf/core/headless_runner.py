import time
import threading
import traceback
import os
import sys
from vaf.core.agent import Agent
from vaf.core.task_queue import TaskQueue
from vaf.core.session import SessionManager
from vaf.core.web_interface import get_web_interface

def run_headless_agent():
    """
    Run a headless agent loop that processes tasks from the TaskQueue.
    This is designed to run in a background thread within the Tray App.
    """
    # Ensure UTF-8 output to avoid Windows charmap crashes
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if sys.stderr and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print("[Headless] Starting Agent Loop...")
    
    # Initialize Agent (retry on failure)
    # We use verbose=False to keep logs clean
    agent = None
    while agent is None:
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
            try:
                get_web_interface().log("Headless agent initialized and ready.", level="info", source="System")
                # Send initial token stats so WebUI shows context usage immediately
                used, total = agent.get_token_usage()
                stats = {
                    "used": used,
                    "total": total,
                    "percent": (used / total) if total else 0.0,
                    "api": bool(getattr(agent, 'api_backend', False))
                }
                get_web_interface().emit_stats(stats)
            except Exception:
                pass
        except Exception as e:
            print(f"[Headless] Agent initialization failed: {e}")
            traceback.print_exc()
            try:
                get_web_interface().log(f"Headless agent init failed: {e}", level="error", source="System")
            except Exception:
                pass
            time.sleep(2)

    # Task Queue
    tq = TaskQueue()
    session_mgr = SessionManager()
    
    # Main Loop
    last_subagent_check = 0.0
    last_subagent_ui_update = 0.0
    open_subagent_sessions = set()
    subagent_last_activity = {}
    subagent_last_steps = {}
    while True:
        try:
            # check for tasks
            task = tq.get()
            if task:
                print(f"[Headless] Processing task for session {task.session_id}...")
                try:
                    get_web_interface().log(
                        f"Processing task for session {task.session_id}...",
                        level="info",
                        source="System",
                        session_id=task.session_id
                    )
                except Exception:
                    pass
                
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
                        "percent": (used / total) if total else 0.0,
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
                    try:
                        get_web_interface().log(
                            f"Starting chat_step for session {task.session_id}...",
                            level="info",
                            source="System",
                            session_id=task.session_id
                        )
                    except Exception:
                        pass
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

                    # Handle async-ack markers (sub-agent dispatched, no stream output)
                    response_text = str(response) if response is not None else ""
                    if response_text.startswith("[ASYNC_ACK]"):
                        clean_ack = response_text.replace("[ASYNC_ACK]", "").strip()
                        if clean_ack:
                            get_web_interface().emit_agent_message(
                                role="assistant",
                                content=clean_ack,
                                session_id=task.session_id
                            )
                    else:
                        # Final response broadcast (in case streaming missed the final state)
                        final_text = "".join(response_parts) if response_parts else response_text
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
                            "percent": (used / total) if total else 0.0,
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
                    try:
                        get_web_interface().log(
                            f"Chat_step failed for session {task.session_id}: {e}",
                            level="error",
                            source="System",
                            session_id=task.session_id
                        )
                    except Exception:
                        pass
                
                tq.task_done()
            else:
                # Periodically check for sub-agent results and summarize for WebUI
                now = time.time()
                if now - last_subagent_check >= 1.0:
                    last_subagent_check = now
                    try:
                        pending_results = agent._check_subagent_results()
                        if pending_results:
                            found_results_text = []
                            for result_task in pending_results:
                                # Ensure agent context is aligned with result session
                                if result_task.session_id:
                                    agent.load_session_context(result_task.session_id)
                                    subagent_last_activity[result_task.session_id] = now
                                agent._process_subagent_result(result_task)

                                if result_task.status == "completed":
                                    found_results_text.append(
                                        f"Sub-Agent '{result_task.agent_type}' completed:\n{result_task.result}"
                                    )
                                elif result_task.status == "failed":
                                    found_results_text.append(
                                        f"Sub-Agent '{result_task.agent_type}' FAILED:\n{result_task.error}"
                                    )
                                elif result_task.status == "timeout":
                                    found_results_text.append(
                                        f"Sub-Agent '{result_task.agent_type}' TIMEOUT."
                                    )

                            if found_results_text:
                                user_lang = "auto"
                                for msg in reversed(agent.history):
                                    if msg.get("role") == "user":
                                        user_lang = agent._detect_user_language(msg.get("content", ""))
                                        break

                                native_lang = agent.LANGUAGE_NAMES_NATIVE.get(user_lang, user_lang)
                                combined_results = "\n\n---\n\n".join(r[:1000] for r in found_results_text)
                                if user_lang == "de":
                                    instruction_prompt = (
                                        "Hier sind die Ergebnisse der Sub-Agenten:\n\n"
                                        f"{combined_results}\n\n"
                                        "Bitte erstelle eine KURZE ZUSAMMENFASSUNG dieser Ergebnisse für den Benutzer auf DEUTSCH.\n"
                                        "Konzentriere dich auf den Inhalt (was wurde gefunden/getan).\n"
                                        "Bleib prägnant aber informativ.\n"
                                        "Du kannst `read_file` nutzen, wenn du den Inhalt sehen musst.\n"
                                        "ANTWORTE AUSSCHLIESSLICH AUF DEUTSCH."
                                    )
                                else:
                                    instruction_prompt = (
                                        "The sub-agent(s) have completed their tasks.\n\n"
                                        f"**RESULTS:**\n{combined_results}\n\n"
                                        f"Please provide a BRIEF SUMMARY of these results for the user in {native_lang}.\n"
                                        "Focus on the content (what was found/done).\n"
                                        "Keep it concise but informative.\n"
                                        "You may use `read_file` if you need to see the content before summarizing.\n"
                                        f"RESPOND EXCLUSIVELY IN {native_lang.upper()}."
                                    )

                                response_parts = []

                                def webui_stream_callback(text):
                                    response_parts.append(text)
                                    full_text = "".join(response_parts)
                                    if full_text.strip():
                                        get_web_interface().emit_agent_message(
                                            "assistant",
                                            full_text,
                                            session_id=getattr(agent, "current_session_id", None)
                                        )

                                agent.chat_step(
                                    user_input=instruction_prompt,
                                    stream_callback=webui_stream_callback,
                                    skip_input=False,
                                    disable_workflows=True,
                                    disable_tools=False
                                )
                    except Exception as e:
                        print(f"[Headless] Sub-agent result processing error: {e}")

                # Periodically update Sub-Agent window state for WebUI
                if now - last_subagent_ui_update >= 1.0:
                    last_subagent_ui_update = now
                    try:
                        from vaf.core.subagent_ipc import get_ipc, get_current_session_id
                        ipc = get_ipc()
                        active_tasks = ipc.get_active_tasks()
                        if active_tasks:
                            tasks_by_session = {}
                            for task in active_tasks:
                                sid = task.session_id or getattr(agent, "current_session_id", None) or get_current_session_id()
                                if not sid:
                                    continue
                                tasks_by_session.setdefault(sid, []).append(task)

                            for sid, tasks in tasks_by_session.items():
                                subagent_last_activity[sid] = now
                                open_subagent_sessions.add(sid)
                                steps = []
                                for task in tasks:
                                    status = "running"
                                    if task.status == "completed":
                                        status = "completed"
                                    elif task.status == "pending":
                                        status = "pending"
                                    steps.append({
                                        "id": task.task_id,
                                        "title": task.agent_type.replace("_", " ").title(),
                                        "description": task.task_description,
                                        "status": status,
                                        "actions": []
                                    })

                                subagent_last_steps[sid] = steps
                                get_web_interface()._push_session_update(sid, {
                                    "type": "subagent_update",
                                    "agentName": "Sub-Agent",
                                    "status": "Running sub-agent tasks...",
                                    "file": "",
                                    "code": "",
                                    "steps": steps
                                })
                        else:
                            # Keep window visible briefly after completion to avoid flicker
                            now_ts = time.time()
                            for sid in list(open_subagent_sessions):
                                last_seen = subagent_last_activity.get(sid, 0.0)
                                if now_ts - last_seen <= 15.0:
                                    get_web_interface()._push_session_update(sid, {
                                        "type": "subagent_update",
                                        "agentName": "Sub-Agent",
                                        "status": "Completed",
                                        "file": "",
                                        "code": "",
                                        "steps": subagent_last_steps.get(sid, [])
                                    })
                                else:
                                    get_web_interface()._push_session_update(sid, {
                                        "type": "subagent_update",
                                        "agentName": "Sub-Agent",
                                        "status": "Idle",
                                        "file": "",
                                        "code": "",
                                        "steps": []
                                    })
                                    open_subagent_sessions.discard(sid)
                                    subagent_last_activity.pop(sid, None)
                                    subagent_last_steps.pop(sid, None)
                    except Exception as e:
                        print(f"[Headless] Sub-agent UI update error: {e}")

                # Sleep briefly to avoid busy loop
                time.sleep(0.1)
                
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
