import time
import threading
import traceback
import os
import sys
import gc
import re
import logging

# CRITICAL: Disable CUDA for PyTorch BEFORE any torch import to prevent memory explosion
# PyTorch pre-allocates GPU memory even when using CPU-only models!
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")  # Hide GPU from PyTorch
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "max_split_size_mb:32")

import requests
from vaf.core.agent import Agent
from vaf.core.task_queue import TaskQueue
from vaf.core.session import SessionManager, Session
from vaf.core.tray_context import TrayContext
from vaf.core.web_interface import get_web_interface
from vaf.core.platform import Platform
from vaf.core.log_helper import append_domain_log, is_debug_logging_enabled
from pathlib import Path

# Memory management constants - AGGRESSIVE to prevent 25GB situations
MEMORY_CHECK_INTERVAL = 30  # Check memory every 30 seconds
MEMORY_THRESHOLD_MB = 2048  # Trigger cleanup above 2GB
MEMORY_CRITICAL_MB = 4096  # Force aggressive cleanup above 4GB

# Throttle stream updates to WebUI so the UI does not lag behind (max ~12 updates/sec)
STREAM_EMIT_THROTTLE_SEC = 0.08

def _get_debug_log_dir():
    """Resolve log dir so queue.log, callback_debug.txt, etc. land in project logs (e.g. d:\\VAF\\logs)."""
    candidates = []
    env_dir = os.environ.get("VAF_LOG_DIR")
    if env_dir:
        candidates.append(Path(env_dir))
    # Prefer repo root / logs so all debug logs sit in one place
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

def run_headless_agent():
    """
    Run a headless agent loop that processes tasks from the TaskQueue.
    This is designed to run in a background thread within the Tray App.
    """
    # IMMEDIATE: Create log dir and write startup marker (consolidated in headless.log)
    log_dir = _get_debug_log_dir()
    try:
        append_domain_log("headless", "[STARTUP] Headless Runner STARTING")
        append_domain_log("headless", f"[STARTUP] PID: {os.getpid()}")
        append_domain_log("headless", f"[STARTUP] Log dir: {log_dir}")
    except Exception as e:
        print(f"[Headless] Failed to write startup log: {e}")

    # Start Memory Profiler IMMEDIATELY
    try:
        from vaf.core.memory_profiler import start_profiler
        start_profiler()
        print(f"[Headless] Memory Profiler started - logging to {log_dir / 'memory.log'}")
    except Exception as e:
        print(f"[Headless] Memory Profiler failed to start: {e}")
        append_domain_log("headless", f"[STARTUP] Memory Profiler FAILED: {e}")

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
    last_memory_check = 0.0
    waited_for_server_ready = False  # Wait for 8080 to return 200 before first chat (avoids 503)
    open_subagent_sessions = set()
    subagent_last_activity = {}
    subagent_last_steps = {}
    from vaf.core.config import Config

    def _check_and_cleanup_memory():
        """Check memory usage and cleanup if needed."""
        nonlocal last_memory_check
        now = time.time()
        if now - last_memory_check < MEMORY_CHECK_INTERVAL:
            return
        last_memory_check = now

        try:
            import psutil
            process = psutil.Process()
            memory_mb = process.memory_info().rss / (1024 * 1024)

            # Log memory usage to file for debugging (consolidated in memory.log)
            append_domain_log("memory", f"[USAGE] Memory: {memory_mb:.0f}MB")

            if memory_mb > MEMORY_CRITICAL_MB:
                print(f"[Headless] CRITICAL: Memory usage {memory_mb:.0f}MB > {MEMORY_CRITICAL_MB}MB - aggressive cleanup!")
                _aggressive_memory_cleanup()
            elif memory_mb > MEMORY_THRESHOLD_MB:
                print(f"[Headless] WARNING: Memory usage {memory_mb:.0f}MB > {MEMORY_THRESHOLD_MB}MB - running cleanup...")
                _standard_memory_cleanup()
            else:
                # Still run gc periodically
                gc.collect()
        except ImportError:
            # psutil not available, just run gc
            gc.collect()
        except Exception as e:
            print(f"[Headless] Memory check error: {e}")

    def _standard_memory_cleanup():
        """Standard memory cleanup - clear caches, run gc (but KEEP models loaded)."""
        try:
            # Clear embedding cache only (NOT the model - reloading is slow and fragments memory)
            from vaf.memory.embeddings import get_embedding_service
            svc = get_embedding_service()
            if svc:
                svc.clear_cache()
        except Exception as e:
            print(f"[Headless] Embedding cleanup error: {e}")

        # Force garbage collection
        gc.collect()

        try:
            import psutil
            memory_mb = psutil.Process().memory_info().rss / (1024 * 1024)
            print(f"[Headless] After cleanup: {memory_mb:.0f}MB")
        except:
            pass

    def _aggressive_memory_cleanup():
        """Aggressive memory cleanup - unload models if needed."""
        _standard_memory_cleanup()

        try:
            # Unload embedding model completely
            from vaf.memory.embeddings import reset_embedding_service
            reset_embedding_service()
            print("[Headless] Unloaded embedding model")
        except Exception as e:
            print(f"[Headless] Embedding reset error: {e}")

        # Multiple gc passes for better cleanup
        for _ in range(3):
            gc.collect()

        # REMOVED: torch import causes 1GB+ RAM explosion!
        # CUDA cache clearing is not needed when CUDA_VISIBLE_DEVICES="" is set

        try:
            import psutil
            memory_mb = psutil.Process().memory_info().rss / (1024 * 1024)
            print(f"[Headless] After aggressive cleanup: {memory_mb:.0f}MB")
        except:
            pass

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
                try:
                    if is_debug_logging_enabled():
                        from datetime import datetime as _dt
                        with open(log_dir / "queue.log", "a", encoding="utf-8") as f:
                            prev = (task.input_text or "")[:60]
                            f.write(f"{_dt.now().isoformat()} QUEUE_GET session_id={task.session_id} preview={repr(prev)} queue_size_after={tq.get_queue_size()}\n")
                except Exception:
                    pass
                print(f"[Headless] Processing task for session {task.session_id}...")
                # Do not log "Processing task..." to Web UI – redundant after every user message

                # Set current user from task metadata before load_session_context so init_chat/build_prompt get User identity
                meta = (task.metadata or {}) if getattr(task, "metadata", None) else {}
                agent._current_user_scope_id = meta.get("user_scope_id")
                agent._current_username = meta.get("username")
                # Debug: Log user scope for RAG troubleshooting (consolidated in rag.log)
                append_domain_log("rag", f"[Headless] Task user_scope_id={meta.get('user_scope_id')}, username={meta.get('username')}")

                # Load Session Context
                try:
                    agent.load_session_context(task.session_id)
                    # Persist user to session metadata so load_session_context gets it on next load
                    if meta.get("user_scope_id") is not None or meta.get("username") is not None:
                        try:
                            session = session_mgr.load(task.session_id)
                            if meta.get("user_scope_id") is not None:
                                session.metadata["user_scope_id"] = meta.get("user_scope_id")
                            if meta.get("username") is not None:
                                session.metadata["username"] = meta.get("username")
                            session_mgr.save(session)
                        except Exception:
                            pass
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
                    try:
                        if is_debug_logging_enabled():
                            from datetime import datetime as _dt
                            with open(log_dir / "queue.log", "a", encoding="utf-8") as f:
                                f.write(f"{_dt.now().isoformat()} QUEUE_DONE session_id={task.session_id} (cmd)\n")
                    except Exception:
                        pass
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
                        # Load the correct session context (important for queued compaction, e.g. Telegram)
                        agent.load_session_context(task.session_id)
                        run_session_compaction_sync(agent, _scope, task.session_id, _turn)
                    except Exception as e:
                        logging.getLogger(__name__).warning("Session compaction (queued) failed: %s", e)
                    try:
                        if is_debug_logging_enabled():
                            from datetime import datetime as _dt
                            with open(log_dir / "queue.log", "a", encoding="utf-8") as f:
                                f.write(f"{_dt.now().isoformat()} QUEUE_DONE session_id={task.session_id} (compaction)\n")
                    except Exception:
                        pass
                    tq.task_done()
                    continue

                # Telegram relay: contact can only send messages to the main user (no tools, safe reply only)
                meta = (task.metadata or {}) if getattr(task, "metadata", None) else {}
                if meta.get("relay") and meta.get("telegram_chat_id"):
                    try:
                        from vaf.core.telegram_reply import send_telegram_reply
                        to_user = meta.get("relay_to_username") or meta.get("username") or "the user"
                        reply = f"Got it. I'll pass that on to {to_user}. Thanks."
                        send_telegram_reply(str(meta["telegram_chat_id"]), reply)
                    except Exception:
                        pass
                    try:
                        if is_debug_logging_enabled():
                            from datetime import datetime as _dt
                            with open(log_dir / "queue.log", "a", encoding="utf-8") as f:
                                f.write(f"{_dt.now().isoformat()} QUEUE_DONE session_id={task.session_id} (relay)\n")
                    except Exception:
                        pass
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

                # Wait for local server (8080) to be ready before first chat to avoid 503 "Loading model"
                if agent.provider == "local" and getattr(agent, "use_server", False) and not waited_for_server_ready:
                    import requests as _req
                    _deadline = time.time() + 120  # max 2 min
                    while time.time() < _deadline:
                        try:
                            r = _req.get("http://127.0.0.1:8080/v1/models", timeout=3)
                            if r.status_code == 200:
                                waited_for_server_ready = True
                                break
                        except Exception:
                            pass
                        try:
                            get_web_interface().log("Model is loading, please wait...", level="info", source="System", session_id=task.session_id)
                        except Exception:
                            pass
                        time.sleep(2)
                    if not waited_for_server_ready:
                        waited_for_server_ready = True  # don't block forever

                # Normal Chat Step
                # usage: chat_step(user_input, ...)
                _chat_start = time.time()
                try:
                    try:
                        if is_debug_logging_enabled():
                            from datetime import datetime as _dt
                            with open(log_dir / "queue.log", "a", encoding="utf-8") as f:
                                f.write(f"{_dt.now().isoformat()} QUEUE_CHAT_START session_id={task.session_id}\n")
                    except Exception:
                        pass
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
                            append_domain_log("rag", msg)
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
                            try:
                                _rag_t0 = time.time()
                                if is_debug_logging_enabled():
                                    from datetime import datetime as _dt
                                    with open(log_dir / "queue.log", "a", encoding="utf-8") as f:
                                        f.write(f"{_dt.now().isoformat()} RAG_START session_id={task.session_id} query_len={len(task.input_text or '')}\n")
                            except Exception:
                                _rag_t0 = time.time()
                            memory_context = run_memory_search_sync(
                                task.input_text, k=5, user_scope_id=user_scope_id, caller="headless"
                            )
                            try:
                                _rag_dur = time.time() - _rag_t0
                                if is_debug_logging_enabled():
                                    from datetime import datetime as _dt
                                    with open(log_dir / "queue.log", "a", encoding="utf-8") as f:
                                        f.write(f"{_dt.now().isoformat()} RAG_DONE session_id={task.session_id} duration_sec={_rag_dur:.1f} snippet_count={memory_context.count('[Source ') if memory_context else 0}\n")
                            except Exception:
                                pass
                            snippet_count = memory_context.count("[Source ") if memory_context else 0
                            if snippet_count == 0:
                                _log_rag(f"RAG snippets=0 (no matching memories or DB empty) query_len={len(task.input_text or '')}")
                            else:
                                _log_rag(f"RAG snippets={snippet_count} query_len={len(task.input_text or '')}")
                        else:
                            _log_rag("RAG skipped (memory_enabled=False)")
                    except Exception as e:
                        memory_context = ""
                        append_domain_log("rag", f"RAG failed: {e}")

                    # Streaming callback for WebUI - accumulates chunks, throttled emit so UI keeps up
                    response_parts = []
                    _last_emit_time = [0.0]  # list so nested function can assign

                    def webui_stream_callback(text):
                        response_parts.append(text)
                        full_text = "".join(response_parts)
                        if not full_text.strip():
                            return
                        now = time.time()
                        if now - _last_emit_time[0] >= STREAM_EMIT_THROTTLE_SEC:
                            try:
                                get_web_interface().emit_agent_message(
                                    role="assistant",
                                    content=full_text,
                                    session_id=task.session_id
                                )
                                _last_emit_time[0] = now
                            except Exception:
                                pass

                    try:
                        if is_debug_logging_enabled():
                            from datetime import datetime as _dt
                            with open(log_dir / "queue.log", "a", encoding="utf-8") as f:
                                f.write(f"{_dt.now().isoformat()} CHAT_STEP_CALL session_id={task.session_id}\n")
                    except Exception:
                        pass
                    response = agent.chat_step(
                        user_input=input_text,
                        stream_callback=webui_stream_callback,  # Stream to WebUI!
                        skip_input=False,
                        memory_context=memory_context or None,
                    )
                    try:
                        if is_debug_logging_enabled():
                            from datetime import datetime as _dt
                            with open(log_dir / "queue.log", "a", encoding="utf-8") as f:
                                f.write(f"{_dt.now().isoformat()} CHAT_STEP_RETURNED session_id={task.session_id} response_len={len(str(response)) if response else 0}\n")
                    except Exception:
                        pass

                    # Handle async-ack markers (sub-agent dispatched, no stream output)
                    response_text = str(response) if response is not None else ""
                    if response_text.startswith("[ASYNC_ACK]"):
                        clean_ack = response_text.replace("[ASYNC_ACK]", "").strip()
                        final_text = clean_ack or response_text
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

                    # Emit message_complete event for Auto-TTS
                    try:
                        get_web_interface().emit_message_complete(
                            content=str(final_text),
                            session_id=task.session_id
                        )
                    except Exception:
                        pass

                    # If this task came from Telegram bridge, send reply back to Telegram
                    try:
                        task_source = getattr(task, "source", None)
                        meta = (task.metadata or {}) if getattr(task, "metadata", None) else {}
                        chat_id = meta.get("telegram_chat_id")
                        try:
                            from vaf.core.log_helper import log_telegram_reply
                            log_telegram_reply(f"HEADLESS task_source={task_source!r} chat_id={chat_id!r} final_len={len(str(final_text))}")
                        except Exception:
                            pass
                        if task_source == "telegram" and chat_id:
                            from vaf.core.telegram_reply import send_telegram_reply
                            # Strip <think>...</think> so Telegram gets clean text only (no reasoning block)
                            out = str(final_text)
                            out = re.sub(r'<think>.*?</think>', '', out, flags=re.DOTALL)
                            out = re.sub(r'\n{3,}', '\n\n', out).strip()
                            if not out:
                                try:
                                    log_telegram_reply(
                                        f"HEADLESS reply empty after strip → [No reply text] "
                                        f"raw_len={len(str(final_text))} chat_id={chat_id}"
                                    )
                                except Exception:
                                    pass
                                out = "[No reply text]"
                            send_telegram_reply(str(chat_id), out)
                    except Exception:
                        pass

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

                    # === POST-CHAT PROCESSING ===
                    # Order matters for memory safety:
                    # 1. Session save (lightweight, critical for persistence)
                    # 2. Auto-capture (queues ONNX work, can be skipped on error)
                    # 3. Compaction (queues LLM work, can be skipped on error)
                    # If any step fails, we skip subsequent memory-intensive operations
                    _post_chat_ok = True

                    # 1. Save session FIRST (before any memory-intensive operations)
                    # CONTEXT EFFICIENCY: Only save clean content (no <think> blocks)
                    try:
                        if is_debug_logging_enabled():
                            from datetime import datetime as _dt
                            with open(log_dir / "queue.log", "a", encoding="utf-8") as f:
                                f.write(f"{_dt.now().isoformat()} SESSION_SAVE_START session_id={task.session_id}\n")
                    except Exception:
                        pass
                    try:
                        try:
                            session = session_mgr.load(task.session_id)
                        except FileNotFoundError:
                            # New Telegram (or other) session: create so first exchange is persisted
                            session = Session(
                                id=task.session_id,
                                name=f"Telegram {task.session_id.replace('telegram_', '', 1)}",
                            )
                        _user_input = input_text or ""
                        _assistant_response = "".join(response_parts) if response_parts else str(response_text or "")

                        def _clean_for_session(text: str) -> str:
                            """Remove reasoning blocks to keep session context efficient."""
                            if not text:
                                return ""
                            cleaned = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
                            cleaned = re.sub(r'<redacted_reasoning>.*?</redacted_reasoning>', '', cleaned, flags=re.DOTALL)
                            cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
                            return cleaned.strip()

                        _clean_response = _clean_for_session(_assistant_response)

                        if _user_input.strip():
                            # Helper to get role/content from Message object or dict
                            def _msg_role(m):
                                return m.role if hasattr(m, 'role') else m.get("role")
                            def _msg_content(m):
                                return m.content if hasattr(m, 'content') else m.get("content", "")

                            last_user_msg = None
                            for msg in reversed(session.messages):
                                if _msg_role(msg) == "user":
                                    last_user_msg = _msg_content(msg)
                                    break

                            if last_user_msg != _user_input.strip():
                                session.add_message(role="user", content=_user_input.strip())
                                if _clean_response:
                                    session.add_message(role="assistant", content=_clean_response)
                                session_mgr.save(session)
                                if is_debug_logging_enabled():
                                    logging.getLogger(__name__).debug(f"Session saved: +1 user, +1 assistant for {task.session_id}")
                            elif _clean_response:
                                last_assistant_msg = None
                                for msg in reversed(session.messages):
                                    if _msg_role(msg) == "assistant":
                                        last_assistant_msg = _msg_content(msg)
                                        break
                                if last_assistant_msg != _clean_response:
                                    session.add_message(role="assistant", content=_clean_response)
                                    session_mgr.save(session)
                    except Exception as e:
                        logging.getLogger(__name__).warning(f"Failed to save session: {e}")
                        _post_chat_ok = False  # Skip memory-intensive operations
                        gc.collect()  # Defensive GC on error
                    try:
                        if is_debug_logging_enabled():
                            from datetime import datetime as _dt
                            with open(log_dir / "queue.log", "a", encoding="utf-8") as f:
                                f.write(f"{_dt.now().isoformat()} SESSION_SAVE_END session_id={task.session_id} ok={_post_chat_ok}\n")
                    except Exception:
                        pass

                    # 2. Auto-capture: DISABLED - causes 13GB+ memory spikes
                    # The memory leak occurs during pipeline.ingest() ~13 seconds after EMBED_DONE
                    # TODO: Investigate asyncpg/SQLAlchemy memory leak in auto_capture_memory()
                    if False and _post_chat_ok:  # HARD DISABLED
                        try:
                            if is_debug_logging_enabled():
                                from datetime import datetime as _dt
                                with open(log_dir / "queue.log", "a", encoding="utf-8") as f:
                                    f.write(f"{_dt.now().isoformat()} AUTO_CAPTURE_START session_id={task.session_id}\n")
                        except Exception:
                            pass
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
                        except Exception as e:
                            logging.getLogger(__name__).debug(f"Auto-capture skipped: {e}")
                            _post_chat_ok = False
                            gc.collect()
                        try:
                            if is_debug_logging_enabled():
                                from datetime import datetime as _dt
                                with open(log_dir / "queue.log", "a", encoding="utf-8") as f:
                                    f.write(f"{_dt.now().isoformat()} AUTO_CAPTURE_END session_id={task.session_id}\n")
                        except Exception:
                            pass

                    # 3. Session compaction: queue durable memories (ONLY if previous steps succeeded)
                    if _post_chat_ok:
                        try:
                            if is_debug_logging_enabled():
                                from datetime import datetime as _dt
                                with open(log_dir / "queue.log", "a", encoding="utf-8") as f:
                                    f.write(f"{_dt.now().isoformat()} COMPACTION_CHECK_START session_id={task.session_id}\n")
                        except Exception:
                            pass
                        try:
                            from uuid import UUID
                            from vaf.memory.rag import run_session_compaction_sync
                            from vaf.core.config import Config
                            if Config.get("memory_enabled", True) and Config.get("memory_compaction_enabled", True):
                                # Only main-user messages (role=user) count; each session is single-user (relay/other bots have separate sessions)
                                turn_count = len([m for m in agent.history if _msg_role(m) == "user"])
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
                            logging.getLogger(__name__).warning("Session compaction failed: %s", e)
                        try:
                            if is_debug_logging_enabled():
                                from datetime import datetime as _dt
                                with open(log_dir / "queue.log", "a", encoding="utf-8") as f:
                                    f.write(f"{_dt.now().isoformat()} COMPACTION_CHECK_END session_id={task.session_id}\n")
                        except Exception:
                            pass

                    # Result is already added to history and broadcast to WebUI by agent logic
                    _duration = time.time() - _chat_start
                    try:
                        if is_debug_logging_enabled():
                            from datetime import datetime as _dt
                            with open(log_dir / "queue.log", "a", encoding="utf-8") as f:
                                f.write(f"{_dt.now().isoformat()} QUEUE_CHAT_END session_id={task.session_id} duration_sec={_duration:.1f}\n")
                    except Exception:
                        pass
                    print("[Headless] Task complete.")

                    # Memory cleanup: clear response parts list
                    if 'response_parts' in dir():
                        response_parts.clear()

                except Exception as e:
                    _duration = time.time() - _chat_start
                    try:
                        if is_debug_logging_enabled():
                            from datetime import datetime as _dt
                            err_preview = str(e).replace("\n", " ")[:120]
                            with open(log_dir / "queue.log", "a", encoding="utf-8") as f:
                                f.write(f"{_dt.now().isoformat()} QUEUE_CHAT_FAIL session_id={task.session_id} duration_sec={_duration:.1f} error={repr(err_preview)}\n")
                    except Exception:
                        pass
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
                    # If task came from Telegram, send error reply so user sees something
                    try:
                        task_source = getattr(task, "source", None)
                        meta = (task.metadata or {}) if getattr(task, "metadata", None) else {}
                        if task_source == "telegram" and meta.get("telegram_chat_id"):
                            from vaf.core.telegram_reply import send_telegram_reply
                            err_msg = str(e).replace("\n", " ")[:400]
                            send_telegram_reply(str(meta["telegram_chat_id"]), f"Sorry, something went wrong: {err_msg}")
                    except Exception:
                        pass
                try:
                    if is_debug_logging_enabled():
                        from datetime import datetime as _dt
                        with open(log_dir / "queue.log", "a", encoding="utf-8") as f:
                            f.write(f"{_dt.now().isoformat()} QUEUE_DONE session_id={task.session_id} (chat)\n")
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

                # Periodic memory check
                _check_and_cleanup_memory()

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
