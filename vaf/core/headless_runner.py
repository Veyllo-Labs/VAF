import time
import threading
import traceback
import os
import sys
import requests
from vaf.core.agent import Agent
from vaf.core.task_queue import TaskQueue
from vaf.core.session import SessionManager
from vaf.core.tray_context import TrayContext
from vaf.core.web_interface import get_web_interface
from vaf.core.platform import Platform
from pathlib import Path

def _get_debug_log_dir():
    candidates = []
    env_dir = os.environ.get("VAF_LOG_DIR")
    if env_dir:
        candidates.append(Path(env_dir))
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

            print("[Headless] Agent initialized and ready.")
            try:
                get_web_interface().log("Headless agent initialized and ready.", level="info", source="System")
                # Send initial token stats so WebUI shows context usage immediately
                used, total = agent.get_token_usage()
                api_backend = getattr(agent, 'api_backend', None)
                input_tokens = 0
                output_tokens = 0
                if api_backend:
                    usage = api_backend.session_usage
                    input_tokens = int(usage.get("input_tokens", 0))
                    output_tokens = int(usage.get("output_tokens", 0))
                    used = input_tokens + output_tokens
                stats = {
                    "used": used,
                    "total": total,
                    "percent": (used / total) if total else 0.0,
                    "api": bool(api_backend),
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens
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
    from vaf.core.config import Config

    def _get_subagent_model_info():
        cfg = Config.load()
        main_provider = cfg.get("provider", "local")
        subagent_provider = cfg.get("subagent_provider", "inherit")
        use_separate = cfg.get("subagent_use_separate_provider", False)
        effective_provider = subagent_provider if use_separate and subagent_provider != "inherit" else main_provider
        if effective_provider != "local":
            model = cfg.get(f"api_model_{effective_provider}", "") or cfg.get("model", "")
        else:
            model = cfg.get("model", "")
        return effective_provider, model
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

                # Process Input
                input_text = task.input_text
                is_cmd = str(input_text).startswith("__CMD__")

                # Check for System Commands (similar to run.py) - do NOT load model for commands
                if is_cmd:
                    _handle_command(input_text, agent, session_mgr)
                    tq.task_done()
                    continue

                # Compaction task: run session compaction (same serialized LLM as chat when local)
                is_compaction = (task.metadata or {}).get("compaction") is True
                if is_compaction:
                    from uuid import UUID
                    from vaf.memory.rag import run_session_compaction_sync
                    _scope = (task.metadata or {}).get("user_scope_id")
                    _turn = int((task.metadata or {}).get("turn_count", 0))
                    if _scope is not None and not isinstance(_scope, UUID):
                        try:
                            _scope = UUID(str(_scope))
                        except (ValueError, TypeError):
                            _scope = None
                    try:
                        run_session_compaction_sync(agent, _scope, task.session_id, _turn)
                    except Exception as e:
                        import logging
                        logging.getLogger(__name__).warning("Session compaction (queued) failed: %s", e)
                    tq.task_done()
                    continue

                # Lazy-load local model only when we have a real chat task (not for LOAD_SESSION etc.)
                try:
                    if agent.provider == "local" and not agent.api_backend and not agent.llm and not agent.use_server:
                        agent.load_model(skip_download_check=True)
                        if agent.llm or agent.use_server:
                            tray_ctx = TrayContext()
                            tray_ctx.set_model_loaded(True)
                            get_web_interface().push_update({
                                "type": "model_state",
                                "loaded": True,
                                "persistent": tray_ctx.is_persistent(),
                                "provider": "local"
                            })
                except Exception as e:
                    print(f"[Headless] Lazy model load failed: {e}")

                # Send initial token stats to WebUI (before processing)
                try:
                    used, total = agent.get_token_usage()
                    api_backend = getattr(agent, 'api_backend', None)
                    input_tokens = 0
                    output_tokens = 0
                    if api_backend:
                        usage = api_backend.session_usage
                        input_tokens = int(usage.get("input_tokens", 0))
                        output_tokens = int(usage.get("output_tokens", 0))
                        used = input_tokens + output_tokens
                    stats = {
                        "used": used,
                        "total": total,
                        "percent": (used / total) if total else 0.0,
                        "api": bool(api_backend),
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens
                    }
                    get_web_interface().emit_stats(stats, session_id=task.session_id)
                except Exception:
                    pass

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
                    # RAG: fetch memory context for this turn (sync helper)
                    memory_context = ""
                    try:
                        def _log_rag(msg: str) -> None:
                            try:
                                from datetime import datetime as _dt
                                log_dir = Platform.get_context_log_dir()
                                log_dir.mkdir(parents=True, exist_ok=True)
                                with open(log_dir / "rag_context.log", "a", encoding="utf-8") as f:
                                    f.write(f"{_dt.now().isoformat()} {msg}\n")
                            except Exception:
                                pass
                        if Config.get("memory_enabled", True):
                            from vaf.memory.rag import run_memory_search_sync
                            from uuid import UUID
                            user_scope_id = None
                            raw = task.metadata.get("user_scope_id") if getattr(task, "metadata", None) else None
                            if raw:
                                try:
                                    user_scope_id = UUID(str(raw))
                                except (ValueError, TypeError):
                                    pass
                            memory_context = run_memory_search_sync(
                                task.input_text, k=5, user_scope_id=user_scope_id
                            )
                            snippet_count = memory_context.count("[Source ") if memory_context else 0
                            if snippet_count == 0:
                                _log_rag(f"RAG snippets=0 (no matching memories or DB empty) query_len={len(task.input_text or '')}")
                            else:
                                _log_rag(f"RAG snippets={snippet_count} query_len={len(task.input_text or '')}")
                        else:
                            _log_rag("RAG skipped (memory_enabled=False)")
                    except Exception as e:
                        memory_context = ""
                        try:
                            from datetime import datetime as _dt
                            log_dir = Platform.get_context_log_dir()
                            log_dir.mkdir(parents=True, exist_ok=True)
                            with open(log_dir / "rag_context.log", "a", encoding="utf-8") as f:
                                f.write(f"{_dt.now().isoformat()} RAG failed: {e}\n")
                        except Exception:
                            pass

                    # Streaming callback for WebUI - accumulates chunks and sends updates
                    response_parts = []
                    def webui_stream_callback(text):
                        response_parts.append(text)
                        # DEBUG: Log callback invocation
                        try:
                            log_dir = _get_debug_log_dir()
                            with open(log_dir / "callback_debug.txt", "a", encoding="utf-8") as f:
                                f.write(f"[CALLBACK] text_len={len(text)} parts={len(response_parts)} session={task.session_id}\n")
                        except: pass
                        # Construct full text so far and send to WebUI
                        full_text = "".join(response_parts)
                        if full_text.strip():
                            try:
                                get_web_interface().emit_agent_message(
                                    role="assistant",
                                    content=full_text,
                                    session_id=task.session_id
                                )
                                # DEBUG: Confirm emit was called
                                log_dir = _get_debug_log_dir()
                                with open(log_dir / "callback_debug.txt", "a", encoding="utf-8") as f:
                                    f.write(f"[EMIT_DONE] session={task.session_id}\n")
                            except Exception as e:
                                log_dir = _get_debug_log_dir()
                                with open(log_dir / "callback_debug.txt", "a", encoding="utf-8") as f:
                                    f.write(f"[EMIT_ERROR] {e}\n")

                    # Set current user scope so memory_store tool can use it
                    agent._current_user_scope_id = (task.metadata or {}).get("user_scope_id") if getattr(task, "metadata", None) else None

                    response = agent.chat_step(
                        user_input=input_text,
                        stream_callback=webui_stream_callback,  # Stream to WebUI!
                        skip_input=False,
                        memory_context=memory_context or None,
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
                        if not final_text or not str(final_text).strip():
                            final_text = "[Error] No response was produced by the API backend."
                        get_web_interface().emit_agent_message(
                            role="assistant",
                            content=str(final_text),
                            session_id=task.session_id
                        )

                    # Send token/context stats to WebUI
                    try:
                        used, total = agent.get_token_usage()
                        api_backend = getattr(agent, 'api_backend', None)
                        input_tokens = 0
                        output_tokens = 0
                        if api_backend:
                            usage = api_backend.session_usage
                            input_tokens = int(usage.get("input_tokens", 0))
                            output_tokens = int(usage.get("output_tokens", 0))
                            used = input_tokens + output_tokens
                        stats = {
                            "used": used,
                            "total": total,
                            "percent": (used / total) if total else 0.0,
                            "api": bool(api_backend),
                            "input_tokens": input_tokens,
                            "output_tokens": output_tokens
                        }
                        get_web_interface().emit_stats(stats, session_id=task.session_id)
                    except Exception:
                        pass  # Ignore stats errors

                    # Auto-capture: optionally store high-value snippets to Memory-DB
                    try:
                        from vaf.memory.rag import run_auto_capture_sync
                        from uuid import UUID
                        _scope = (task.metadata or {}).get("user_scope_id") if getattr(task, "metadata", None) else None
                        if _scope is not None and not isinstance(_scope, UUID):
                            try:
                                _scope = UUID(str(_scope))
                            except (ValueError, TypeError):
                                _scope = None
                        if _scope is not None:
                            _assistant_text = "".join(response_parts) if response_parts else str(response_text or "")
                            run_auto_capture_sync(input_text or "", _assistant_text, _scope)
                    except Exception:
                        pass

                    # Session compaction: every N turns store durable memories (MEMORY:/NO_REPLY), ingest to RAG.
                    # When using a local LLM, enqueue compaction so it runs in the same queue (no parallel LLM use).
                    try:
                        from uuid import UUID
                        from vaf.memory.rag import run_session_compaction_sync
                        from vaf.core.config import Config
                        if Config.get("memory_enabled", True) and Config.get("memory_compaction_enabled", True):
                            turn_count = len([m for m in agent.history if m.get("role") == "user"])
                            _scope = (task.metadata or {}).get("user_scope_id") if getattr(task, "metadata", None) else None
                            if _scope is not None and not isinstance(_scope, UUID):
                                try:
                                    _scope = UUID(str(_scope))
                                except (ValueError, TypeError):
                                    _scope = None
                            if getattr(agent, "provider", "local") == "local":
                                tq.add(
                                    task.session_id,
                                    "__COMPACTION__",
                                    source="web",
                                    priority=15,
                                    metadata={"compaction": True, "user_scope_id": str(_scope) if _scope else None, "turn_count": turn_count},
                                )
                            else:
                                run_session_compaction_sync(agent, _scope, task.session_id, turn_count)
                    except Exception as e:
                        import logging
                        logging.getLogger(__name__).warning("Session compaction failed: %s", e)

                    # Result is already added to history and broadcast to WebUI by agent logic
                    print("[Headless] Task complete.")
                    
                except Exception as e:
                    print(f"[Headless] Error during chat step: {e}")
                    traceback.print_exc()
                    try:
                        tb = traceback.format_exc()
                        get_web_interface().log(
                            f"Chat_step failed for session {task.session_id}: {e}",
                            level="error",
                            source="System",
                            session_id=task.session_id
                        )
                        get_web_interface().log(
                            f"Traceback (chat_step):\n{tb}",
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
                                session_id = result_task.session_id or getattr(agent, "current_session_id", None)
                                if session_id:
                                    agent.load_session_context(session_id)
                                    subagent_last_activity[session_id] = now
                                agent._process_subagent_result(result_task)
                                
                                # Ensure WebUI opens the Sub-Agent window even if no active task was seen
                                sid = session_id
                                if sid:
                                    status_label = "Completed"
                                    if result_task.status == "failed":
                                        status_label = "Failed"
                                    elif result_task.status == "timeout":
                                        status_label = "Timed out"
                                    presence = "error" if result_task.status in ("failed", "timeout") else "idle"
                                    provider, model = _get_subagent_model_info()
                                    step_status = "completed" if result_task.status in ("completed", "failed", "timeout") else "running"
                                    steps = [{
                                        "id": result_task.task_id,
                                        "title": result_task.agent_type.replace("_", " ").title(),
                                        "description": result_task.task_description,
                                        "status": step_status,
                                        "actions": []
                                    }]
                                    subagent_last_steps[sid] = steps
                                    open_subagent_sessions.add(sid)
                                    subagent_last_activity[sid] = now
                                    get_web_interface()._push_session_update(sid, {
                                        "type": "subagent_update",
                                        "agentName": "Sub-Agent",
                                        "status": status_label,
                                        "presence": presence,
                                        "provider": provider,
                                        "model": model,
                                        "file": "",
                                        "code": "",
                                        "steps": steps
                                    })

                                if result_task.status == "completed":
                                    try:
                                        output_text = str(result_task.result or "")
                                        if output_text:
                                            max_len = 8000
                                            if len(output_text) > max_len:
                                                output_text = output_text[:max_len] + "\n... [Output Truncated]\n"
                                            if session_id:
                                                get_web_interface()._push_session_update(session_id, {
                                                "type": "subagent_output",
                                                "taskId": result_task.task_id,
                                                "agentType": result_task.agent_type,
                                                "status": "completed",
                                                "output": output_text
                                                })
                                    except Exception:
                                        pass
                                    found_results_text.append(
                                        f"Sub-Agent '{result_task.agent_type}' completed:\n{result_task.result}"
                                    )
                                elif result_task.status == "failed":
                                    try:
                                        error_text = str(result_task.error or "")
                                        if error_text:
                                            if session_id:
                                                get_web_interface()._push_session_update(session_id, {
                                                    "type": "subagent_output",
                                                    "taskId": result_task.task_id,
                                                    "agentType": result_task.agent_type,
                                                    "status": "failed",
                                                    "output": error_text
                                                })
                                    except Exception:
                                        pass
                                    found_results_text.append(
                                        f"Sub-Agent '{result_task.agent_type}' FAILED:\n{result_task.error}"
                                    )
                                elif result_task.status == "timeout":
                                    try:
                                        if session_id:
                                            get_web_interface()._push_session_update(session_id, {
                                                "type": "subagent_output",
                                                "taskId": result_task.task_id,
                                                "agentType": result_task.agent_type,
                                                "status": "timeout",
                                                "output": "Sub-Agent timed out."
                                            })
                                    except Exception:
                                        pass
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
                                provider, model = _get_subagent_model_info()
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
                                    "presence": "online",
                                    "provider": provider,
                                    "model": model,
                                    "file": "",
                                    "code": "",
                                    "steps": steps
                                })
                        else:
                            # Keep window visible briefly after completion to avoid flicker
                            now_ts = time.time()
                            for sid in list(open_subagent_sessions):
                                last_seen = subagent_last_activity.get(sid, 0.0)
                                provider, model = _get_subagent_model_info()
                                if now_ts - last_seen <= 15.0:
                                    get_web_interface()._push_session_update(sid, {
                                        "type": "subagent_update",
                                        "agentName": "Sub-Agent",
                                        "status": "Completed",
                                        "presence": "idle",
                                        "provider": provider,
                                        "model": model,
                                        "file": "",
                                        "code": "",
                                        "steps": subagent_last_steps.get(sid, [])
                                    })
                                else:
                                    get_web_interface()._push_session_update(sid, {
                                        "type": "subagent_update",
                                        "agentName": "Sub-Agent",
                                        "status": "Idle",
                                        "presence": "idle",
                                        "provider": provider,
                                        "model": model,
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
            # Ensure provider changes take effect for the running agent
            new_provider = new_cfg.get("provider", "local")
            old_provider = getattr(agent, "provider", "local")
            print(f"[Headless] RELOAD_CONFIG: old={old_provider} new={new_provider}")
            if old_provider != new_provider:
                agent.provider = new_provider
                if new_provider != "local":
                    try:
                        from vaf.core.api_backend import APIBackendManager
                        agent.api_backend = APIBackendManager(new_provider)
                        print(f"[Headless] API backend created for {new_provider}")
                    except Exception as e:
                        agent.api_backend = None
                        print(f"[Headless] API backend creation failed: {e}")
                    agent.use_server = False
                    agent.llm = None
                else:
                    agent.api_backend = None
                    print("[Headless] Switched to local provider")
                
                # Send updated stats to reflect provider change in UI
                try:
                    used, total = agent.get_token_usage()
                    stats = {
                        "used": used,
                        "total": total,
                        "percent": (used / total) if total else 0.0,
                        "api": bool(getattr(agent, 'api_backend', False))
                    }
                    get_web_interface().emit_stats(stats)
                    print(f"[Headless] Stats updated: api={stats['api']}")
                except Exception:
                    pass
            
    except Exception as e:
        print(f"[Headless] Command error: {e}")
