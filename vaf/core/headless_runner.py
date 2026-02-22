import time
import threading
import traceback
import os
import sys
import gc
import re
import logging
import uuid

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
from vaf.core.config import Config
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


def _strip_tool_calls_json(text: str) -> str:
    """Remove raw tool_calls JSON blobs from reply text so bridges/TTS never send them to users."""
    if not text or ("tool_calls" not in text):
        return text
    out = []
    i = 0
    while i < len(text):
        start = text.find('{"tool_calls"', i)
        if start == -1:
            start = text.find("{'tool_calls'", i)
        if start == -1:
            out.append(text[i:])
            break
        out.append(text[i:start])
        # Match closing brace, skipping content inside double-quoted strings
        depth = 0
        j = start
        in_dq = False
        escape = False
        while j < len(text):
            c = text[j]
            if escape:
                escape = False
                j += 1
                continue
            if c == "\\" and in_dq:
                escape = True
                j += 1
                continue
            if c == '"' and not in_dq:
                in_dq = True
                j += 1
                continue
            if c == '"' and in_dq:
                in_dq = False
                j += 1
                continue
            if in_dq:
                j += 1
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    j += 1
                    break
            j += 1
        i = j
    return "".join(out)


# Phrases that must never appear in messages sent to contacts via Telegram/WhatsApp/Discord.
# If any of these are found in the outgoing text, the message is blocked or sanitized.
_INTERNAL_PHRASES = [
    "[SYSTEM_LOG_ONLY]",
    "[FRONT OFFICE",
    "MESSAGE FROM A CONTACT",
    "NOT FROM THE ACCOUNT OWNER",
    "API returned empty responses",
    "Do NOT report to the account owner",
    "Do NOT repeat or echo the contact",
    "REPLY IN:",
    "Contact details (use Language",
    "contact preferred_language",
]


def _sanitize_outgoing_message(text: str) -> str:
    """
    Safety net: strip internal system phrases from outgoing messages before sending
    to external channels (Telegram/WhatsApp/Discord). If the entire message is just
    internal content, return empty string.
    """
    if not text or not text.strip():
        return ""
    # Check if any internal phrase is present
    text_lower = text.lower()
    for phrase in _INTERNAL_PHRASES:
        if phrase.lower() in text_lower:
            # Try to extract just the agent's actual response by removing the contaminated block.
            # If the FRONT OFFICE prompt leaked, it's typically at the start or end — drop the whole thing.
            logging.getLogger(__name__).warning(
                "SANITIZE: blocked internal phrase %r in outgoing message (len=%d)", phrase, len(text)
            )
            return ""
    return text


def _user_asked_for_text(user_input: str) -> bool:
    """True if the user prompt looks like a request to write/compose text (for opening in Document Editor)."""
    if not (user_input and user_input.strip()):
        return False
    lower = user_input.strip().lower()
    triggers = (
        "schreib mir", "verfasse", "schreibe einen text", "verfasse einen text",
        "text schreiben", "schreib einen text", "schreibe mir", "write me a text",
        "write me ", "draft", "entwurf", "formuliere", "formulier mir",
        "schreib einen", "schreibe einen", "text über", "text zu ",
    )
    return any(t in lower for t in triggers)


def _format_sidebar_doc(d: dict) -> str:
    """Format one sidebar document for the agent: --- FILE: name (Speicherort: path) ---\\ncontent\\n----------------"""
    name = d.get("name", "")
    content = d.get("content", "")
    path = d.get("path")
    header = f"--- FILE: {name}"
    if path:
        header += f" (Speicherort: {path})"
    header += " ---"
    return f"{header}\n{content}\n----------------"


def _maybe_open_draft_in_editor(session_id: str, user_input: str, response_text: str, source: str) -> None:
    """
    If the user asked for a text (e.g. "Schreib mir einen Text") and the response is substantial,
    save it to a draft file and open the Document Editor so the user can edit or save.
    Only runs for Web UI (source == 'web').
    """
    if not session_id or (str(source or "").strip().lower() != "web"):
        return
    if not _user_asked_for_text(user_input or ""):
        return
    # Strip <think>...</think> for draft content
    clean = re.sub(r"<think>.*?</think>", "", response_text, flags=re.DOTALL)
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    if len(clean) < 200:
        return
    try:
        # Use data_dir so /api/file and /api/file/save can read/write the draft (allowed_roots include data_dir)
        draft_dir = Platform.data_dir() / "drafts" / session_id
        draft_dir.mkdir(parents=True, exist_ok=True)
        draft_path = draft_dir / "entwurf.md"
        draft_path.write_text(clean, encoding="utf-8")
        from vaf.core.web_interface import notify_document_created
        notify_document_created(session_id, str(draft_path.resolve()), title="Entwurf")
    except Exception:
        pass


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
                if getattr(task, "source", None) == "whatsapp":
                    try:
                        from vaf.core.log_helper import log_whatsapp_inbound
                        log_whatsapp_inbound(f"HEADLESS processing session={task.session_id}")
                    except Exception:
                        pass

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
                    # Also persist telegram_chat_id for sessions from Telegram (needed when subagent completes later)
                    if (
                        meta.get("user_scope_id") is not None
                        or meta.get("username") is not None
                        or meta.get("telegram_chat_id") is not None
                        or meta.get("discord_channel_id") is not None
                        or meta.get("whatsapp_chat_jid") is not None
                    ):
                        try:
                            session = session_mgr.load(task.session_id)
                            if meta.get("user_scope_id") is not None:
                                session.metadata["user_scope_id"] = meta.get("user_scope_id")
                            if meta.get("username") is not None:
                                session.metadata["username"] = meta.get("username")
                            if meta.get("telegram_chat_id") is not None:
                                session.metadata["telegram_chat_id"] = meta["telegram_chat_id"]
                            if meta.get("discord_channel_id") is not None:
                                session.metadata["discord_channel_id"] = meta["discord_channel_id"]
                            if meta.get("whatsapp_chat_jid") is not None:
                                session.metadata["whatsapp_chat_jid"] = meta["whatsapp_chat_jid"]
                            if meta.get("voice_lang"):
                                session.metadata["voice_lang"] = meta["voice_lang"]
                            if getattr(task, "source", None) == "telegram":
                                session.metadata["source"] = "telegram"
                            if getattr(task, "source", None) == "discord":
                                session.metadata["source"] = "discord"
                            if getattr(task, "source", None) == "whatsapp":
                                session.metadata["source"] = "whatsapp"
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
                            k = int(Config.get("memory_rag_k", 5))
                            k = max(1, min(20, k))
                            memory_context = run_memory_search_sync(
                                task.input_text, k=k, user_scope_id=user_scope_id, caller="headless"
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

                    # RAG Ergebnis in Web-UI anzeigen (Trefferzahl = Snippets im System-Prompt)
                    try:
                        _rag_count = memory_context.count("[Source ") if memory_context else 0
                        get_web_interface().log(
                            f"RAG: {_rag_count} hit(s) (included in system prompt for this turn).",
                            level="info",
                            source="System",
                            session_id=task.session_id,
                        )
                    except Exception:
                        pass

                    # Streaming callback for WebUI - accumulates chunks, throttled emit so UI keeps up.
                    # Supports .clear() so agent can reset buffer on empty-response retry (only retry content is shown).
                    response_parts = []
                    _last_emit_time = [0.0]  # list so nested function can assign

                    def _emit(text):
                        if not (text and text.strip()):
                            return
                        now = time.time()
                        if now - _last_emit_time[0] >= STREAM_EMIT_THROTTLE_SEC:
                            try:
                                get_web_interface().emit_agent_message(
                                    role="assistant",
                                    content=text,
                                    session_id=task.session_id
                                )
                                _last_emit_time[0] = now
                            except Exception:
                                pass

                    def webui_stream_callback(text):
                        response_parts.append(text)
                        _emit("".join(response_parts))

                    def clear_stream_buffer():
                        response_parts.clear()

                    webui_stream_callback.clear = clear_stream_buffer

                    # So build_prompt knows current channel (WebUI vs Telegram)
                    agent._current_chat_source = getattr(task, "source", "web")

                    _meta = (task.metadata or {}) if getattr(task, "metadata", None) else {}
                    if _meta.get("from_contact"):
                        agent._front_office_mode = True
                        try:
                            from vaf.core.front_office_tools import FRONT_OFFICE_ALLOWED_TOOLS
                            agent._active_tools = tuple(n for n in FRONT_OFFICE_ALLOWED_TOOLS if n in agent.tools)
                        except Exception:
                            agent._active_tools = None
                    else:
                        agent._front_office_mode = False
                        agent._active_tools = None

                    # Sidebar documents: inject into this turn only (session history stays clean)
                    effective_input = input_text or ""
                    # When message is from a contact (front office), pass contact data so the agent can personalize
                    if _meta.get("from_contact"):
                        contact = None
                        _username = _meta.get("username") or "admin"
                        _user_scope = _meta.get("user_scope_id")
                        try:
                            from vaf.core.contacts_store import (
                                get_contact_by_telegram_user_id,
                                get_contact_by_whatsapp_phone,
                            )
                            task_source = getattr(task, "source", None)
                            if task_source == "telegram":
                                _tid = _meta.get("telegram_user_id")
                                if _tid:
                                    contact = get_contact_by_telegram_user_id(_tid, _username, user_scope_id=_user_scope)
                            elif task_source == "whatsapp":
                                _jid = _meta.get("whatsapp_chat_jid")
                                if _jid:
                                    contact = get_contact_by_whatsapp_phone(_jid, _username, user_scope_id=_user_scope)
                        except Exception:
                            pass
                        contact_block = ""
                        reply_lang_hint = ""
                        if contact:
                            from vaf.core.contacts_store import _contact_ensure_channels
                            c = _contact_ensure_channels(contact)
                            lines = []
                            lines.append(f"Contact: {c.get('name') or 'Unknown'}")
                            lines.append("Channels")
                            for ch in (c.get("channels") or []):
                                t, v = (ch.get("type") or "").strip().lower(), (ch.get("value") or "").strip()
                                if not v:
                                    continue
                                if t in ("whatsapp", "phone"):
                                    lines.append(f"  Phone (used as WhatsApp): {v}")
                                elif t == "telegram":
                                    lines.append(f"  Telegram: {v}")
                                elif t == "email":
                                    lines.append(f"  Email: {v}")
                                elif t == "discord":
                                    lines.append(f"  Discord: {v}")
                            if not any("Phone" in ln or "WhatsApp" in ln for ln in lines):
                                if c.get("whatsapp_phone"):
                                    lines.append(f"  Phone (used as WhatsApp): {c['whatsapp_phone']}")
                            lines.append("Personal file")
                            if c.get("preferred_language"):
                                lines.append(f"  Language: {c['preferred_language']}")
                                pl_code = (c["preferred_language"] or "").strip().lower()[:2]
                                _lang_names = {"de": "German", "en": "English", "tr": "Turkish", "fr": "French", "es": "Spanish", "ar": "Arabic"}
                                pl_name = _lang_names.get(pl_code) or pl_code
                                reply_lang_hint = f" REPLY IN: {pl_name} (contact preferred_language; use this language for your reply even if the message was in another language)."
                            if c.get("how_to_address"):
                                lines.append(f"  How to address: {c['how_to_address']}")
                            if c.get("allow_as_assistant_user"):
                                lines.append("  Can reach your assistant")
                            if c.get("birthday"):
                                lines.append(f"  Birthday: {c['birthday']}")
                            lines.append("Notes")
                            if c.get("notes"):
                                notes = (c["notes"] or "").strip()
                                if len(notes) > 600:
                                    notes = notes[:597] + "..."
                                lines.append(f"  {notes}")
                            else:
                                lines.append("  (none)")
                            contact_block = "\n".join(lines)
                        effective_input = (
                            "[FRONT OFFICE – MESSAGE FROM A CONTACT, NOT FROM THE ACCOUNT OWNER.] "
                            "The following message was sent by a contact to your front office. "
                            "You must respond directly TO this contact (they will receive your reply). "
                            "Do NOT report to the account owner (e.g. do not say 'I sent X to the contact' or 'I have sent Alice...'). "
                            "Do NOT repeat or echo the contact's message back; give a helpful reply.\n\n"
                            "Contact details (use Language / How to address / Notes when replying):\n"
                            + contact_block
                            + ("\n" + reply_lang_hint if reply_lang_hint else "")
                            + "\n\nMessage from the contact:\n\n"
                            + effective_input
                        )
                    elif getattr(task, "source", None) == "whatsapp":
                        effective_input = (
                            "[WhatsApp message.] Respond helpfully; do not repeat or echo the user's message back.\n\n"
                            + effective_input
                        )
                    try:
                        session_for_sidebar = session_mgr.load(task.session_id)
                        sidebar_docs = (getattr(session_for_sidebar, "runtime_state", None) or {}).get("sidebar_documents") or []
                        if sidebar_docs:
                            sidebar_block = "\n\n".join(
                                _format_sidebar_doc(d) for d in sidebar_docs
                            )
                            if _meta.get("from_contact"):
                                effective_input = effective_input + "\n\n" + sidebar_block
                            else:
                                effective_input = sidebar_block + "\n\n" + (input_text or "")
                    except FileNotFoundError:
                        pass  # no session yet, use raw input_text

                    # replace_editor_selection only when Document Editor is open with marked selections
                    try:
                        session_for_editor = session_mgr.load(task.session_id)
                        editor_selections = (getattr(session_for_editor, "runtime_state", None) or {}).get("editor_selections") or []
                        agent._excluded_tools = set() if editor_selections else {"replace_editor_selection"}
                    except Exception:
                        agent._excluded_tools = {"replace_editor_selection"}

                    # If the user is replying to a thinking-mode question, inject that question
                    # as context so the main agent knows what the conversation is about.
                    try:
                        from vaf.core.thinking_mode import get_waiting_for_reply as _get_waiting
                        _thinking_meta = (task.metadata or {}) if getattr(task, "metadata", None) else {}
                        _thinking_scope = _thinking_meta.get("user_scope_id")
                        _waiting = _get_waiting(_thinking_scope)
                        if _waiting and (_waiting.get("question_text") or "").strip():
                            _q_text = _waiting["question_text"].strip()
                            effective_input = (
                                f"[Context: You asked the user in a background thinking pass: \"{_q_text}\" — "
                                f"the following is their reply.]\n\n"
                                + effective_input
                            )
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
                        user_input=effective_input,
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

                    # Record this turn as "last interaction" for next turn's system prompt
                    try:
                        from vaf.core.last_interaction import update_last_interaction
                        meta = (task.metadata or {}) if getattr(task, "metadata", None) else {}
                        update_last_interaction(
                            user_scope_id=meta.get("user_scope_id"),
                            source=getattr(task, "source", "web"),
                            preview=(task.input_text or "")[:80],
                            voice=bool(meta.get("voice_lang")),
                        )
                    except Exception:
                        pass
                    try:
                        from vaf.core.thinking_mode import clear_waiting_for_reply, get_waiting_for_reply
                        meta = (task.metadata or {}) if getattr(task, "metadata", None) else {}
                        scope_id = meta.get("user_scope_id")
                        reply_text = (task.input_text or "").strip() if get_waiting_for_reply(scope_id) else None
                        clear_waiting_for_reply(scope_id, user_reply_text=reply_text)
                    except Exception:
                        pass

                    # Handle async-ack markers (sub-agent dispatched, no stream output)
                    response_text = str(response) if response is not None else ""
                    _is_system_log_only = response_text.startswith("[SYSTEM_LOG_ONLY]")
                    if response_text.startswith("[ASYNC_ACK]"):
                        clean_ack = response_text.replace("[ASYNC_ACK]", "").strip()
                        final_text = clean_ack or response_text
                        if clean_ack:
                            get_web_interface().emit_agent_message(
                                role="assistant",
                                content=clean_ack,
                                session_id=task.session_id
                            )
                    elif _is_system_log_only:
                        # Agent already sent this as system log; do NOT add assistant bubble
                        # and do NOT send to any external channel (Telegram/WhatsApp/Discord)
                        final_text = response_text.replace("[SYSTEM_LOG_ONLY]", "").strip()
                    else:
                        # Final response broadcast (in case streaming missed the final state)
                        final_text = "".join(response_parts) if response_parts else response_text
                        if not final_text or not str(final_text).strip():
                            final_text = "[Error] No response was produced. The server may have rejected the request (e.g. context too large). Try closing the Document Editor or starting a new chat."
                        get_web_interface().emit_agent_message(
                            role="assistant",
                            content=str(final_text),
                            session_id=task.session_id
                        )

                    # Emit message_complete event for Auto-TTS (skip speaking for SYSTEM_LOG_ONLY)
                    try:
                        get_web_interface().emit_message_complete(
                            content="" if response_text.startswith("[SYSTEM_LOG_ONLY]") else str(final_text),
                            session_id=task.session_id
                        )
                    except Exception:
                        pass

                    # When user asked for a text (e.g. "Schreib mir einen Text"), open it in Document Editor
                    if not response_text.startswith("[ASYNC_ACK]") and not response_text.startswith("[SYSTEM_LOG_ONLY]"):
                        try:
                            _maybe_open_draft_in_editor(
                                task.session_id or "",
                                task.input_text or "",
                                str(final_text),
                                getattr(task, "source", "web"),
                            )
                        except Exception:
                            pass

                    # If this task came from Telegram/WhatsApp/Discord, send reply back.
                    # NEVER send [SYSTEM_LOG_ONLY] responses to external channels.
                    if _is_system_log_only:
                        try:
                            from vaf.core.log_helper import log_whatsapp_reply
                            log_whatsapp_reply(f"HEADLESS SYSTEM_LOG_ONLY suppressed for session={task.session_id!r} len={len(final_text)}")
                        except Exception:
                            pass
                    try:
                        task_source = getattr(task, "source", None) if not _is_system_log_only else None
                        meta = (task.metadata or {}) if getattr(task, "metadata", None) else {}
                        chat_id = meta.get("telegram_chat_id")
                        try:
                            from vaf.core.log_helper import log_telegram_reply
                            log_telegram_reply(f"HEADLESS task_source={task_source!r} chat_id={chat_id!r} final_len={len(str(final_text))}")
                        except Exception:
                            pass
                        if task_source == "telegram" and chat_id:
                            from vaf.core.telegram_reply import send_telegram_reply
                            # Strip <think>...</think>, raw tool_calls JSON, and internal phrases
                            out = str(final_text)
                            out = re.sub(r'<think>.*?</think>', '', out, flags=re.DOTALL)
                            out = _strip_tool_calls_json(out)
                            out = re.sub(r'\n{3,}', '\n\n', out).strip()
                            out = _sanitize_outgoing_message(out)
                            if not out:
                                try:
                                    log_telegram_reply(
                                        f"HEADLESS reply empty/blocked after sanitize "
                                        f"raw_len={len(str(final_text))} chat_id={chat_id}"
                                    )
                                except Exception:
                                    pass
                            else:
                                send_telegram_reply(str(chat_id), out)
                        elif task_source == "discord":
                            discord_channel_id = meta.get("discord_channel_id")
                            if discord_channel_id:
                                from vaf.core.discord_reply import send_discord_reply
                                try:
                                    from vaf.core.log_helper import log_discord_reply
                                    log_discord_reply(
                                        f"HEADLESS task_source=discord channel_id={discord_channel_id!r} final_len={len(str(final_text))}"
                                    )
                                except Exception:
                                    pass
                                out = str(final_text)
                                out = re.sub(r'<think>.*?</think>', '', out, flags=re.DOTALL)
                                out = _strip_tool_calls_json(out)
                                out = re.sub(r'\n{3,}', '\n\n', out).strip()
                                out = _sanitize_outgoing_message(out)
                                if out:
                                    send_discord_reply(str(discord_channel_id), out)
                        elif task_source == "whatsapp":
                            chat_jid = meta.get("whatsapp_chat_jid")
                            username = meta.get("username") or "admin"
                            # Fallback: session may have whatsapp_chat_jid if task metadata was not persisted (e.g. self-chat @lid)
                            if not chat_jid:
                                try:
                                    _s = session_mgr.load(task.session_id)
                                    chat_jid = (_s.metadata or {}).get("whatsapp_chat_jid")
                                    if not username or username == "admin":
                                        username = (_s.metadata or {}).get("username") or "admin"
                                except Exception:
                                    pass
                            if chat_jid:
                                from vaf.core.whatsapp_reply import send_whatsapp_reply
                                try:
                                    from vaf.core.log_helper import log_whatsapp_reply
                                    log_whatsapp_reply(
                                        f"HEADLESS task_source=whatsapp jid={chat_jid!r} final_len={len(str(final_text))}"
                                    )
                                except Exception:
                                    pass
                                out = str(final_text)
                                out = re.sub(r'<think>.*?</think>', '', out, flags=re.DOTALL)
                                out = _strip_tool_calls_json(out)
                                out = re.sub(r'\n{3,}', '\n\n', out).strip()
                                out = _sanitize_outgoing_message(out)
                                if out:
                                    send_whatsapp_reply(username, str(chat_jid), out, user_scope_id=meta.get("user_scope_id") and str(meta["user_scope_id"]) or None)
                                else:
                                    try:
                                        from vaf.core.log_helper import log_whatsapp_reply
                                        log_whatsapp_reply(f"HEADLESS whatsapp reply BLOCKED by sanitize jid={chat_jid!r}")
                                    except Exception:
                                        pass
                            else:
                                try:
                                    from vaf.core.log_helper import log_whatsapp_reply
                                    log_whatsapp_reply(
                                        f"HEADLESS whatsapp reply SKIP no chat_jid session={task.session_id!r}"
                                    )
                                except Exception:
                                    pass
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
                            # New session from Telegram, Discord, WhatsApp, etc.: create so first exchange is persisted
                            _sid = task.session_id
                            meta_save = (task.metadata or {}) if getattr(task, "metadata", None) else {}
                            if _sid.startswith("telegram_"):
                                _label = f"Telegram {_sid.replace('telegram_', '', 1)}"
                            elif _sid.startswith("discord_"):
                                _label = f"Discord {_sid.replace('discord_', '', 1)}"
                            elif _sid.startswith("whatsapp_"):
                                _label = f"WhatsApp {_sid.replace('whatsapp_', '', 1)}"
                            else:
                                _label = _sid
                            session = Session(id=task.session_id, name=_label)
                            if meta_save.get("whatsapp_chat_jid") is not None:
                                session.metadata["whatsapp_chat_jid"] = meta_save["whatsapp_chat_jid"]
                            if meta_save.get("username") is not None:
                                session.metadata["username"] = meta_save["username"]
                            if meta_save.get("user_scope_id") is not None:
                                session.metadata["user_scope_id"] = meta_save["user_scope_id"]
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
                                # Increment persistent user_turn_count in runtime_state
                                if not hasattr(session, 'runtime_state') or session.runtime_state is None:
                                    session.runtime_state = {}
                                session.runtime_state["user_turn_count"] = session.runtime_state.get("user_turn_count", 0) + 1
                                if _clean_response:
                                    session.add_message(role="assistant", content=_clean_response)
                                session_mgr.save(session)
                                if is_debug_logging_enabled():
                                    logging.getLogger(__name__).debug(f"Session saved: +1 user, +1 assistant for {task.session_id}, turn_count={session.runtime_state.get('user_turn_count', 0)}")
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

                    # 3. Session compaction: main user chats (Web, Telegram, WhatsApp, Discord).
                    #    DSGVO: skip contact chats (from_contact=True) — never learn from other people's messages.
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
                            _task_meta = (task.metadata or {}) if getattr(task, "metadata", None) else {}
                            _is_contact = bool(_task_meta.get("from_contact"))
                            if _is_contact:
                                if is_debug_logging_enabled():
                                    from datetime import datetime as _dt
                                    try:
                                        with open(log_dir / "queue.log", "a", encoding="utf-8") as f:
                                            f.write(f"{_dt.now().isoformat()} COMPACTION_SKIP session_id={task.session_id} reason=contact_chat_dsgvo\n")
                                    except Exception:
                                        pass
                            elif Config.get("memory_enabled", True) and Config.get("memory_compaction_enabled", True):
                                # CRITICAL: Use PERSISTENT turn_count from session.runtime_state
                                try:
                                    _session_for_count = session_mgr.load(task.session_id)
                                    _runtime = getattr(_session_for_count, 'runtime_state', None) or {}
                                    turn_count = _runtime.get("user_turn_count", 0)
                                except Exception:
                                    turn_count = len([m for m in agent.history if _msg_role(m) == "user"])
                                _scope = (task.metadata or {}).get("user_scope_id") if getattr(task, "metadata", None) else None
                                if _scope is not None and not isinstance(_scope, UUID):
                                    try:
                                        _scope = UUID(str(_scope))
                                    except (ValueError, TypeError):
                                        _scope = None
                                tq.add(
                                    task.session_id,
                                    "__COMPACTION__",
                                    source="web",
                                    priority=15,
                                    metadata={"compaction": True, "user_scope_id": str(_scope) if _scope else None, "turn_count": turn_count},
                                )
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
                    # If task came from Telegram or Discord, send error reply so user sees something
                    try:
                        task_source_err = getattr(task, "source", None)
                        meta_err = (task.metadata or {}) if getattr(task, "metadata", None) else {}
                        err_msg = str(e).replace("\n", " ")[:400]
                        err_text = f"Sorry, something went wrong: {err_msg}"
                        if task_source_err == "telegram" and meta_err.get("telegram_chat_id"):
                            from vaf.core.telegram_reply import send_telegram_reply
                            send_telegram_reply(str(meta_err["telegram_chat_id"]), err_text)
                        elif task_source_err == "discord" and meta_err.get("discord_channel_id"):
                            from vaf.core.discord_reply import send_discord_reply
                            send_discord_reply(str(meta_err["discord_channel_id"]), err_text)
                        elif task_source_err == "whatsapp" and meta_err.get("whatsapp_chat_jid"):
                            from vaf.core.whatsapp_reply import send_whatsapp_reply
                            # Clear any pending voice reply so error messages are sent as text, not TTS'd
                            try:
                                from vaf.api.whatsapp_bridge import _voice_reply_pending, _voice_reply_lock
                                _err_username = meta_err.get("username") or "admin"
                                _err_jid = str(meta_err["whatsapp_chat_jid"])
                                with _voice_reply_lock:
                                    _voice_reply_pending.pop(f"{_err_username}|{_err_jid}", None)
                            except Exception:
                                pass
                            send_whatsapp_reply(
                                meta_err.get("username") or "admin",
                                str(meta_err["whatsapp_chat_jid"]),
                                err_text,
                                user_scope_id=meta_err.get("user_scope_id") and str(meta_err["user_scope_id"]) or None,
                            )
                    except Exception:
                        pass
                finally:
                    agent._front_office_mode = False
                    agent._active_tools = None
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
                            any_needs_retry = False
                            for result_task in pending_results:
                                # Ensure agent context is aligned with result session
                                session_id = result_task.session_id or getattr(agent, "current_session_id", None)
                                if session_id:
                                    agent.load_session_context(session_id)
                                    subagent_last_activity[session_id] = now
                                if agent._process_subagent_result(result_task):
                                    any_needs_retry = True
                                
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

                            if found_results_text or any_needs_retry:
                                user_lang = "auto"
                                for msg in reversed(agent.history):
                                    if msg.get("role") == "user":
                                        user_lang = agent._detect_user_language(msg.get("content", ""))
                                        break

                                native_lang = agent.LANGUAGE_NAMES_NATIVE.get(user_lang, user_lang)
                                combined_results = "\n\n---\n\n".join(r[:1000] for r in found_results_text) if found_results_text else ""

                                if any_needs_retry:
                                    if user_lang == "de":
                                        instruction_prompt = (
                                            "Der Sub-Agent-Ergebnis erfüllt die Anfrage des Benutzers NICHT.\n"
                                            "Du MUSST den Sub-Agent SOFORT erneut mit der genauen Aufgabe aufrufen, die in der Background Intelligence oben steht.\n"
                                            "Fasse NICHT zusammen. Rufe das Tool JETZT auf."
                                        )
                                    else:
                                        instruction_prompt = (
                                            "The sub-agent result did NOT fulfill the user's request.\n"
                                            "You MUST retry immediately by calling the sub-agent again with the exact task specified in the Background Intelligence above.\n"
                                            "Do NOT summarize. Call the tool now."
                                        )
                                elif user_lang == "de":
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

                                # Send subagent summary to Telegram/Discord if this session originated from there
                                try:
                                    sid = getattr(agent, "current_session_id", None)
                                    if sid and response_parts:
                                        session = session_mgr.load(sid)
                                        out = "".join(response_parts)
                                        out = re.sub(r"<think>.*?</think>", "", out, flags=re.DOTALL)
                                        out = re.sub(r"\n{3,}", "\n\n", out).strip()
                                        out = _strip_tool_calls_json(out)
                                        out = re.sub(r"\n{3,}", "\n\n", out).strip()
                                        if not out:
                                            out = "[No summary generated]"
                                        chat_id = session.metadata.get("telegram_chat_id")
                                        if chat_id:
                                            from vaf.core.telegram_reply import send_telegram_reply
                                            send_telegram_reply(str(chat_id), out)
                                        else:
                                            discord_channel_id = session.metadata.get("discord_channel_id")
                                            if discord_channel_id:
                                                from vaf.core.discord_reply import send_discord_reply
                                                send_discord_reply(str(discord_channel_id), out)
                                            else:
                                                whatsapp_chat_jid = session.metadata.get("whatsapp_chat_jid")
                                                if whatsapp_chat_jid:
                                                    from vaf.core.whatsapp_reply import send_whatsapp_reply
                                                    send_whatsapp_reply(
                                                        session.metadata.get("username") or "admin",
                                                        str(whatsapp_chat_jid),
                                                        out,
                                                        user_scope_id=session.metadata.get("user_scope_id") and str(session.metadata["user_scope_id"]) or None,
                                                    )
                                except Exception as e:
                                    logging.getLogger(__name__).warning(
                                        "Failed to send subagent summary to Telegram/Discord: %s", e
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
