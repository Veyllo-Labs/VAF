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
from vaf.core.session import SessionManager, Session, turn_context_messages_since_last_user
from vaf.core.tray_context import TrayContext
from vaf.core.web_interface import get_web_interface
from vaf.core.platform import Platform
from vaf.core.log_helper import append_domain_log, append_domain_log_always, get_app_log_dir, get_dated_log_path, is_debug_logging_enabled
from vaf.core.config import Config
from pathlib import Path

# Memory management constants - AGGRESSIVE to prevent 25GB situations
MEMORY_CHECK_INTERVAL = 30  # Check memory every 30 seconds
MEMORY_THRESHOLD_MB = 2048  # Trigger cleanup above 2GB
MEMORY_CRITICAL_MB = 4096  # Force aggressive cleanup above 4GB

# Throttle stream updates to WebUI so the UI does not lag behind (max ~12 updates/sec)
STREAM_EMIT_THROTTLE_SEC = 0.08
CHANNEL_HISTORY_WINDOW_MESSAGES = 15


class _StopGenerationRequested(Exception):
    """Internal control-flow exception for cooperative user stop."""
    pass


def _apply_channel_history_window(agent, source: str) -> None:
    """
    Keep only a small recent window for channel sessions (Telegram/WhatsApp/Discord)
    so stale long-tail chat history does not dominate tool decisions.
    """
    if source not in {"telegram", "whatsapp", "discord"}:
        return
    try:
        raw_limit = Config.get("channel_history_window_messages", CHANNEL_HISTORY_WINDOW_MESSAGES)
        limit = int(raw_limit)
    except Exception:
        limit = CHANNEL_HISTORY_WINDOW_MESSAGES
    limit = max(6, min(limit, 80))

    history = getattr(agent, "history", None)
    if not isinstance(history, list):
        return
    if len(history) <= limit + 1:
        return

    # Preserve system prompt if present, then keep only latest conversation entries.
    first = history[0] if history else None
    tail = history[-limit:]
    if isinstance(first, dict) and first.get("role") == "system":
        agent.history = [first] + tail
    else:
        agent.history = tail

def _strip_tool_calls_json(text: str) -> str:
    """Remove raw tool_calls JSON blobs and fragments from reply text."""
    if not text:
        return text
        
    # Check for common tool call JSON patterns
    if "tool_calls" not in text and '"name":' not in text and '"function":' not in text:
        return text
        
    # Pattern-based removal for leaked JSON fragments (incl. tail like ", "name": "update_working_memory"}, "type": "function", "index": 0}]}")
    import re
    patterns = [
        r'\[?\s*\{\s*"tool_calls":.*$',
        r'\{\s*"name":\s*"[^"]*",\s*"arguments":.*$',
        r'\{\s*"index":\s*\d+,\s*"id":.*$',
        r'",\s*"name":\s*"[^"]*",\s*"type":\s*"function".*$',
        r'"\}\s*,\s*"type":\s*"function".*$',
        r'",\s*"name":\s*"[^"]*"\}\s*,\s*"type":\s*"function".*$',
        r',\s*"name":\s*"[^"]*"?\s*$',
        r'\}\s*,\s*\{\s*"index":\s*\d+.*$',
        r'\}\s*,\s*\{\s*"name":\s*"[^"]*".*$',
        r'\]\s*\}\s*$',
        r'\}\s*\]\s*\}\s*$',
    ]
    
    out = text
    for p in patterns:
        out = re.sub(p, '', out, flags=re.DOTALL | re.MULTILINE)
        
    # Legacy brace counting for full blocks (fallback)
    if '{"tool_calls"' in out or "{'tool_calls'" in out:
        temp_out = []
        i = 0
        while i < len(out):
            start = out.find('{"tool_calls"', i)
            if start == -1:
                start = out.find("{'tool_calls'", i)
            if start == -1:
                temp_out.append(out[i:])
                break
            temp_out.append(out[i:start])
            # Match closing brace
            depth = 0
            j = start
            in_dq = False
            escape = False
            while j < len(out):
                c = out[j]
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
        out = "".join(temp_out)
        
    return out.strip()


# Phrases that must never appear in messages sent to contacts via Telegram/WhatsApp/Discord.
# If any of these are found in the outgoing text, the message is blocked or sanitized.
_INTERNAL_PHRASES = [
    "[SYSTEM_LOG_ONLY]",
    "[FRONT OFFICE",
    "[TOOL BLOCKED]",
    "MESSAGE FROM A CONTACT",
    "NOT FROM THE ACCOUNT OWNER",
    "API returned empty responses",
    "Do NOT report to the account owner",
    "Do NOT repeat or echo the contact",
    "REPLY IN:",
    "Contact details (use Language",
    "contact preferred_language",
]

# Regex to strip [WORKFLOW_ASYNC:...] lines so the rest of the message can be sent if only that line is internal.
_WORKFLOW_ASYNC_LINE = re.compile(r"^\[WORKFLOW_ASYNC:[^\]]+\][^\n]*\n?", re.MULTILINE)


def _strip_workflow_async_from_message(text: str) -> str:
    """Remove [WORKFLOW_ASYNC:...] status lines from text so they are never sent to contacts."""
    if not text or not text.strip():
        return text
    cleaned = _WORKFLOW_ASYNC_LINE.sub("", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


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


def _maybe_open_draft_in_editor(
    session_id: str,
    user_input: str,
    response_text: str,
    source: str,
    *,
    editor_has_content: bool = False,
) -> None:
    """
    If the user asked for a text (e.g. "Schreib mir einen Text") and the response is substantial,
    save it to a draft file and open the Document Editor so the user can edit or save.
    Only runs for Web UI (source == 'web').
    """
    if not session_id or (str(source or "").strip().lower() != "web"):
        return
    if editor_has_content:
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


def run_headless_agent(worker_id: int = 1, total_workers: int = 1):
    """
    Run a headless agent loop that processes tasks from the TaskQueue.
    This is designed to run in a background thread within the Tray App.
    """
    def _lifecycle(msg: str) -> None:
        """Log lifecycle events unconditionally (ignores debug_logs_enabled toggle)."""
        try:
            append_domain_log_always("headless", f"[LIFECYCLE] {msg}")
        except Exception:
            pass

    # IMMEDIATE: Create log dir and write startup marker (consolidated in headless_YYYY-MM-DD.log)
    # Uses _always variant so these appear even when debug_logs_enabled=False.
    log_dir = get_app_log_dir()
    try:
        append_domain_log_always("headless", f"[STARTUP] Headless Runner STARTING (worker={worker_id}/{total_workers})")
        append_domain_log_always("headless", f"[STARTUP] PID: {os.getpid()} worker={worker_id}")
        append_domain_log_always("headless", f"[STARTUP] Log dir: {log_dir}")
    except Exception as e:
        print(f"[Headless] Failed to write startup log: {e}")

    # Clean up ALL IPC queues from previous runs.
    # Active/pending tasks from a dead process can never complete,
    # and completed results from old sessions confuse the new agent.
    _lifecycle("IPC cleanup starting")
    if worker_id == 1:
        try:
            from vaf.core.subagent_ipc import get_ipc
            ipc = get_ipc()
            stale_active = ipc._read_json(ipc.active_file)
            stale_pending = ipc._read_json(ipc.pending_file)
            stale_results = ipc._read_json(ipc.results_file)
            total = len(stale_active) + len(stale_pending) + len(stale_results)
            if total:
                ipc.clear_all()
                append_domain_log_always("headless", f"[STARTUP] Cleared {total} stale IPC entries "
                                  f"(active={len(stale_active)}, pending={len(stale_pending)}, "
                                  f"results={len(stale_results)})")
                print(f"[Headless] Cleared {total} stale IPC entries from previous run")
        except Exception as e:
            _lifecycle(f"IPC cleanup failed (non-critical): {e}")
            print(f"[Headless] IPC cleanup failed (non-critical): {e}")

    # Kill leftover sub-agent processes from previous runs.
    _lifecycle("Stale process cleanup starting")
    if worker_id == 1:
        try:
            from vaf.core.platform import Platform
            killed = Platform.stop_webui_subagent_processes(session_id=None)
            if killed:
                append_domain_log_always("headless", f"[STARTUP] Killed {killed} stale sub-agent processes")
                print(f"[Headless] Killed {killed} stale sub-agent processes")
        except Exception as e:
            _lifecycle(f"Stale process cleanup failed (non-critical): {e}")
            print(f"[Headless] Stale process cleanup failed (non-critical): {e}")

    # Start Memory Profiler IMMEDIATELY
    if worker_id == 1:
        try:
            from vaf.core.memory_profiler import start_profiler
            start_profiler()
            print(f"[Headless] Memory Profiler started - logging to {get_dated_log_path('memory', 'log')}")
        except Exception as e:
            print(f"[Headless] Memory Profiler failed to start: {e}")
            append_domain_log_always("headless", f"[STARTUP] Memory Profiler FAILED: {e}")

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
    _lifecycle("Pre-Agent-init: starting Agent() constructor")
    
    # Initialize Agent (retry on failure)
    # We use verbose=False to keep logs clean
    agent = None
    _agent_attempt = 0
    while agent is None:
        _agent_attempt += 1
        _lifecycle(f"Agent init attempt {_agent_attempt}")
        try:
            agent = Agent(verbose=False, register_signals=False)
            _lifecycle("Agent() constructor OK, calling init_chat()")
            agent.init_chat()
            _lifecycle("init_chat() OK")

            # Register with Web Interface (single owner to avoid pointer races)
            if worker_id == 1:
                get_web_interface().register_agent(agent)

            print(f"[Headless] Agent initialized and ready (worker={worker_id}).")
            _lifecycle(f"Agent initialized and ready (worker={worker_id})")
            try:
                if worker_id == 1:
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
                    # NOTE: Do NOT overwrite `used` with session_usage here.
                    # session_usage is CUMULATIVE (billing) and grows unboundedly.
                    # get_token_usage() already returns the correct context snapshot.
                stats = {
                    "used": used,
                    "total": total,
                    "percent": round((used / total) * 100, 1) if total else 0.0,
                    "api": bool(api_backend),
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens
                }
                if worker_id == 1:
                    get_web_interface().emit_stats(stats)
            except Exception:
                pass
        except Exception as e:
            _lifecycle(f"Agent init FAILED attempt {_agent_attempt}: {e}")
            print(f"[Headless] Agent initialization failed: {e}")
            traceback.print_exc()
            try:
                get_web_interface().log(f"Headless agent init failed: {e}", level="error", source="System")
            except Exception:
                pass
            time.sleep(2)

    # Task Queue — wrapped in try/except so daemon thread crashes are captured
    try:
        _lifecycle("Creating TaskQueue singleton")
        tq = TaskQueue()
        _lifecycle("TaskQueue OK, resetting runtime state")
        # Self-heal: after worker restarts, clear stale in-flight runtime locks so
        # queued tasks cannot remain blocked behind orphaned session locks.
        tq.reset_runtime_state(include_queued=False)
        _lifecycle("TaskQueue runtime state reset OK")
        # CRITICAL: Connect SessionManager to agent's registry for full state persistence
        _lifecycle("Creating SessionManager")
        session_mgr = SessionManager(state_registry=agent.state_registry)
        _lifecycle("SessionManager OK")
    except Exception as _setup_err:
        _lifecycle(f"FATAL setup crash after Agent init: {_setup_err}")
        append_domain_log_always("headless", f"[FATAL] {traceback.format_exc()}")
        raise
    
    # Main Loop
    last_subagent_check = 0.0
    last_subagent_ui_update = 0.0
    last_memory_check = 0.0
    last_queue_metrics = 0.0
    waited_for_server_ready = False  # Wait for 8080 to return 200 before first chat (avoids 503)
    open_subagent_sessions = set()
    subagent_last_activity = {}
    subagent_last_steps = {}
    from vaf.core.config import Config
    _lifecycle("Config imported, ready for main loop")

    # Optional parallel worker pool (safe rollout via config).
    if worker_id == 1 and total_workers == 1:
        try:
            cfg = Config.load()
            policy = str(cfg.get("queue_policy", "legacy") or "legacy").strip().lower()
            worker_count = int(cfg.get("parallel_main_workers", 1) or 1)
            if policy == "weighted_fair" and worker_count > 1:
                for wid in range(2, worker_count + 1):
                    threading.Thread(
                        target=run_headless_agent,
                        kwargs={"worker_id": wid, "total_workers": worker_count},
                        daemon=True,
                    ).start()
                append_domain_log_always("headless", f"[STARTUP] Spawned {worker_count - 1} additional worker(s)")
        except Exception as e:
            append_domain_log_always("headless", f"[STARTUP] Worker spawn skipped: {e}")

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

    _lifecycle(">>> ENTERING MAIN LOOP <<<")
    _loop_iteration_count = 0
    while True:
        try:
            _loop_iteration_count += 1
            if _loop_iteration_count <= 3:
                _lifecycle(f"Main loop iteration {_loop_iteration_count}")
            if worker_id == 1:
                _now = time.time()
                if _now - last_queue_metrics >= 2.0:
                    last_queue_metrics = _now
                    try:
                        qstats = tq.get_queue_stats()
                        if is_debug_logging_enabled():
                            from datetime import datetime as _dt
                            with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
                                f.write(
                                    f"{_dt.now().isoformat()} [METRICS] "
                                    f"interactive={qstats.get('interactive', 0)} "
                                    f"automation={qstats.get('automation', 0)} "
                                    f"background={qstats.get('background', 0)} "
                                    f"inflight_total={qstats.get('inflight_total', 0)} "
                                    f"inflight_sessions={qstats.get('inflight_sessions', 0)}\n"
                                )
                        get_web_interface().push_update(
                            {
                                "type": "queue_stats",
                                "stats": qstats,
                            }
                        )
                    except Exception:
                        pass
            # check for tasks
            task = tq.get()
            if task:
                try:
                    if is_debug_logging_enabled():
                        from datetime import datetime as _dt
                        with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
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
                # Defensive session/source integrity checks.
                try:
                    expected_sid = str(meta.get("enqueue_session_id") or "").strip()
                    if expected_sid and str(task.session_id) != expected_sid:
                        append_domain_log(
                            "headless",
                            f"[ROUTING_WARN] session mismatch: task.session_id={task.session_id} "
                            f"metadata.enqueue_session_id={expected_sid} source={getattr(task, 'source', '')}",
                        )
                    source = str(getattr(task, "source", "") or "").strip().lower()
                    if source == "web" and str(task.session_id).startswith(("telegram_", "discord_", "whatsapp_")):
                        append_domain_log(
                            "headless",
                            f"[ROUTING_BLOCK] Dropping cross-channel task from web to {task.session_id}",
                        )
                        try:
                            tq.task_done(task=task)
                        except Exception:
                            tq.task_done()
                        continue
                except Exception:
                    pass
                agent._current_user_scope_id = meta.get("user_scope_id")
                agent._current_username = meta.get("username")
                agent._current_user_role = meta.get("role")
                # Debug: Log user scope for RAG troubleshooting (consolidated in rag.log)
                append_domain_log("rag", f"[Headless] Task user_scope_id={meta.get('user_scope_id')}, username={meta.get('username')}")

                # Publish current session ID so sub-agent tools (coding_agent, etc.)
                # can register their child processes under the right session.
                # Without this, stop_webui_subagent_processes() can't find them.
                try:
                    from vaf.core.subagent_ipc import set_current_session_id
                    set_current_session_id(task.session_id)
                except Exception:
                    pass

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
                    # Re-apply task user context after session load:
                    # load_session_context() may hydrate stale session metadata (e.g. legacy admin scope),
                    # but for queued channel tasks the TaskQueue metadata is authoritative for routing/isolation.
                    if meta.get("user_scope_id") is not None:
                        agent._current_user_scope_id = meta.get("user_scope_id")
                    if meta.get("username") is not None:
                        agent._current_username = meta.get("username")
                    if meta.get("role") is not None:
                        agent._current_user_role = meta.get("role")
                    # Sync internal session ID
                    if hasattr(agent, '_session_id') and agent._session_id != task.session_id:
                        agent._unregister_session()
                        agent._session_id = task.session_id
                        agent._register_session()
                    # Channel sessions are intentionally bounded to a short recent history
                    # window to avoid stale chat outputs being reused instead of fresh tool calls.
                    _apply_channel_history_window(agent, getattr(task, "source", None))
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
                            with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
                                f.write(f"{_dt.now().isoformat()} QUEUE_DONE session_id={task.session_id} (cmd)\n")
                    except Exception:
                        pass
                    tq.task_done()
                    continue

                # Timer fired (message-only): deliver a proactive assistant message — no LLM turn.
                from vaf.core.timers import TIMER_MSG_PREFIX
                if str(input_text).startswith(TIMER_MSG_PREFIX):
                    timer_msg = str(input_text)[len(TIMER_MSG_PREFIX):]
                    try:
                        # Proactive standalone message: APPEND it as its own new bubble. The streaming
                        # emit_agent_message would merge into / overwrite the last assistant bubble the
                        # agent had already shown before the timer fired (observed: the timer text
                        # replaced the "Timer is set" reply instead of appearing below it).
                        get_web_interface().emit_agent_message_append(
                            content=timer_msg, session_id=task.session_id, role="assistant"
                        )
                    except Exception:
                        pass
                    try:
                        session = session_mgr.load(task.session_id)
                        session.add_message("assistant", timer_msg)
                        session_mgr.save(session)
                    except Exception:
                        pass
                    try:
                        if is_debug_logging_enabled():
                            from datetime import datetime as _dt
                            with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
                                f.write(f"{_dt.now().isoformat()} QUEUE_DONE session_id={task.session_id} (timer)\n")
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
                            with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
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
                            with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
                                f.write(f"{_dt.now().isoformat()} QUEUE_DONE session_id={task.session_id} (relay)\n")
                    except Exception:
                        pass
                    tq.task_done()
                    continue

                # Lazy-load local model only when we have a real chat task (not for LOAD_SESSION etc.)
                try:
                    if agent.provider == "local" and not agent.api_backend and not agent.llm and not agent.use_server:
                        from vaf.core.model_download_state import MODEL_DOWNLOAD
                        # On first use the load below downloads the model (blocking, filelock-serialized so it
                        # never races the tray). Show an immediate status so the first prompt isn't a frozen
                        # UI; the real answer streams automatically once the model is ready (auto-retry).
                        _showed_loading = False
                        if not os.path.exists(getattr(agent, "model_path", "") or "") or MODEL_DOWNLOAD.active:
                            try:
                                get_web_interface().emit_agent_message(
                                    "assistant",
                                    "Preparing the local model (downloading on first use) — I'll answer as soon as it's ready…",
                                    session_id=task.session_id,
                                )
                                _showed_loading = True
                            except Exception:
                                pass
                        agent.load_model(skip_download_check=True)
                        _loaded = bool(agent.llm or agent.use_server)
                        if _showed_loading and _loaded:
                            # Drop the placeholder; the normal response streams in its place.
                            try:
                                get_web_interface().emit_clear_last_assistant(session_id=task.session_id)
                            except Exception:
                                pass
                        if _loaded:
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
                        # NOTE: Do NOT overwrite `used` with session_usage here.
                        # session_usage is CUMULATIVE (billing) and grows unboundedly.
                        # get_token_usage() already returns the correct context snapshot.
                    stats = {
                        "used": used,
                        "total": total,
                        "percent": round((used / total) * 100, 1) if total else 0.0,
                        "api": bool(api_backend),
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens
                    }
                    get_web_interface().emit_stats(stats, session_id=task.session_id)
                    # Keep Context Window breakdown (system/history/tools) in sync with totals.
                    agent._broadcast_context_status()
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
                # If user requested stop, skip starting chat_step for this queued task.
                # NOTE: do NOT clear before checking; that would drop the stop signal.
                if tq.should_stop(task.session_id):
                    tq.clear_stop(task.session_id)
                    try:
                        get_web_interface().emit_agent_message(
                            role="assistant",
                            content="[Generation stopped by user]",
                            session_id=task.session_id
                        )
                        get_web_interface().emit_message_complete(
                            content="[Generation stopped by user]",
                            session_id=task.session_id
                        )
                    except Exception:
                        pass
                    tq.task_done()
                    continue
                # usage: chat_step(user_input, ...)
                _chat_start = time.time()
                try:
                    try:
                        if is_debug_logging_enabled():
                            from datetime import datetime as _dt
                            with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
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
                                    with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
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
                                    with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
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
                        # Cooperative stop: abort an already-running chat_step turn
                        # as soon as the next stream callback arrives.
                        if tq.should_stop(task.session_id):
                            raise _StopGenerationRequested("Stop requested by user")
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
                            "CRITICAL: Do NOT call send_whatsapp, send_telegram, or any messaging tool — your reply text is automatically delivered to the contact. Just write your reply as normal text. "
                            "Do NOT report to the account owner (e.g. do not say 'I sent X to the contact' or 'I have sent Anne...'). "
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
                        append_domain_log("rag", f"SIDEBAR_CHECK session={task.session_id} docs={len(sidebar_docs)} names={[str((d or {}).get('name','?'))[:40] for d in sidebar_docs[:3]]}")
                        from vaf.core.log_helper import log_attachment as _log_attach
                        _log_attach("AGENT_SEES", session=task.session_id, docs=len(sidebar_docs),
                            names=[str((d or {}).get('name','?'))[:60] for d in sidebar_docs[:5]],
                            content_lens=[len(str((d or {}).get('content') or '')) for d in sidebar_docs[:5]])
                        if sidebar_docs:
                            sidebar_names = [str((d or {}).get("name") or "").strip() for d in sidebar_docs]
                            sidebar_names = [n for n in sidebar_names if n]
                            sidebar_name_list = ", ".join(sidebar_names[:5])
                            if len(sidebar_names) > 5:
                                sidebar_name_list += ", ..."
                            # Explicit anchor so the model knows these documents are active working context
                            # even when the user did not mark a specific selection in the editor/viewer.
                            context_header = (
                                "[DOCUMENT CONTEXT ACTIVE]\n"
                                "The user currently works with attached document(s) in the Web UI.\n"
                                "Use the retrieved attachment snippets below as primary context for this turn.\n"
                            )
                            if sidebar_name_list:
                                context_header += f"Attached: {sidebar_name_list}\n"

                            # Warn the agent when any attachment exceeded the indexing size limit.
                            _max_attach_chars = int(Config.get("attachment_rag_max_chars_per_doc", 24000) or 24000)
                            _truncated_docs = [
                                d for d in sidebar_docs
                                if len(str((d or {}).get("content") or "")) > _max_attach_chars
                            ]
                            if _truncated_docs:
                                _trunc_names = ", ".join(
                                    str((d or {}).get("name") or "unknown") for d in _truncated_docs
                                )
                                _warn = (
                                    f"\n⚠️ ATTACHMENT SIZE WARNING: The following document(s) exceed the "
                                    f"{_max_attach_chars:,} character limit. "
                                    f"Content beyond the limit has been lost. "
                                    f"To process the full document use: learn_document(path=\"<path>\").\n"
                                    f"Truncated: {_trunc_names}\n"
                                )
                                for _td in _truncated_docs:
                                    _td_path = str((_td or {}).get("path") or "")
                                    _td_name = str((_td or {}).get("name") or "unknown")
                                    if _td_path:
                                        _warn += f'  → {_td_name}: learn_document(path="{_td_path}")\n'
                                context_header += _warn

                            # Attachments that FIT in context are inlined IN FULL. RAG top-k retrieval is
                            # query-driven and silently drops pages the query does not semantically match
                            # (observed: "what's on page 6?" surfaced only page 1, because a page number is
                            # not its content). A document under the indexing cap is only ~a few thousand
                            # tokens, so inlining the whole thing every turn is cheap and lets the agent read
                            # every page. The RAG lane stays for documents too large to inline.
                            snippet_lines = []
                            _oversize_present = False
                            for idx, doc in enumerate(sidebar_docs, 1):
                                d_name = str((doc or {}).get("name") or f"Attachment {idx}")
                                d_content = str((doc or {}).get("content") or "").strip()
                                if not d_content:
                                    continue
                                if len(d_content) <= _max_attach_chars:
                                    snippet_lines.append(
                                        f"[Attachment {idx}] {d_name} (full document, {len(d_content)} chars)\n{d_content}"
                                    )
                                else:
                                    _oversize_present = True

                            # Hybrid retrieval lane: only for oversize attachments (full text not inlined).
                            if _oversize_present and bool(Config.get("attachment_rag_enabled", False)):
                                try:
                                    from vaf.memory.attachment_rag import search_session_attachments_sync
                                    att_hits = search_session_attachments_sync(
                                        query=(input_text or ""),
                                        session_id=str(task.session_id),
                                        user_scope_id=meta.get("user_scope_id"),
                                    )
                                    for idx, hit in enumerate(att_hits, 1):
                                        h_name = str((hit or {}).get("attachment_name") or "Attachment")
                                        h_score = float((hit or {}).get("score") or 0.0)
                                        h_text = str((hit or {}).get("text") or "").strip()
                                        if not h_text:
                                            continue
                                        snippet_lines.append(
                                            f"[Attachment Source {idx}] {h_name} (Relevance: {h_score:.0%})\n{h_text}"
                                        )
                                except Exception as e:
                                    append_domain_log("rag", f"ATTACH_SEARCH failed: {e}")

                            if not snippet_lines:
                                # Last resort: a deterministic excerpt when an oversize doc could not be
                                # inlined AND the index is not ready yet OR indexing failed.
                                fallback_chars = int(Config.get("attachment_rag_snippet_chars", 900) or 900)
                                for idx, doc in enumerate(sidebar_docs[:2], 1):
                                    d_name = str((doc or {}).get("name") or f"Attachment {idx}")
                                    d_content = str((doc or {}).get("content") or "").strip()
                                    if not d_content:
                                        continue
                                    if len(d_content) > fallback_chars:
                                        d_content = d_content[:fallback_chars].rstrip() + "\n... [fallback truncated]"
                                    snippet_lines.append(f"[Attachment Fallback {idx}] {d_name}\n{d_content}")
                                if snippet_lines:
                                    # Tell the agent why it only sees a fallback excerpt, and how to read more.
                                    snippet_lines.append(
                                        "[Note] The attachment search index returned no results (it may still be "
                                        "indexing, or indexing failed). The excerpt above may be truncated — if you "
                                        "need the full document and a local file path is available, read it directly "
                                        "with list_files / read_file."
                                    )

                            sidebar_block = "\n\n".join([context_header, "\n\n".join(snippet_lines)])
                            sidebar_block += (
                                "\n\nIf the user asks to keep knowledge from these attachments for future chats, "
                                "suggest using learn_attached_knowledge and ask explicit confirmation first."
                            )
                            if _meta.get("from_contact"):
                                effective_input = effective_input + "\n\n" + sidebar_block
                            else:
                                effective_input = sidebar_block + "\n\n" + (input_text or "")
                    except Exception as _sidebar_err:
                        append_domain_log("rag", f"SIDEBAR_CHECK_ERR session={task.session_id} err={_sidebar_err!r}")
                        pass  # no session or corrupted session file, use raw input_text

                    # Code viewer: inject current open file with numbered lines into this turn only.
                    # Content is stored in runtime_state by web_server.py and cleared here after use.
                    try:
                        session_for_cv = session_mgr.load(task.session_id)
                        cv_file = (getattr(session_for_cv, "runtime_state", None) or {}).get("code_viewer_file") or {}
                        cv_content = (cv_file.get("content") or "").strip()
                        if cv_content:
                            cv_name = cv_file.get("name") or "File"
                            cv_path = cv_file.get("path") or ""
                            cv_lines = cv_content.splitlines()
                            width = len(str(len(cv_lines)))
                            numbered = "\n".join(
                                f"{i + 1:>{width}}: {line}" for i, line in enumerate(cv_lines)
                            )
                            cv_ext = cv_path.rsplit(".", 1)[-1] if "." in cv_path else ""
                            cv_block = (
                                f"--- CURRENTLY OPEN IN CODE VIEWER: {cv_name}"
                                + (f" ({cv_path})" if cv_path else "")
                                + f" ({len(cv_lines)} lines) ---\n"
                                + f"```{cv_ext}\n{numbered}\n```\n"
                                + "---\n\n"
                            )
                            effective_input = cv_block + effective_input
                            # Clear after injection — frontend will resend next turn if viewer still open
                            session_for_cv.runtime_state.pop("code_viewer_file", None)
                            session_mgr.save(session_for_cv, sync_state=False)
                    except Exception:
                        pass

                    # Session workspace + active project: inject so agent knows where to edit files.
                    # [SESSION WORKSPACE] = stable workspace root set on first file creation (session.project_path).
                    # [ACTIVE PROJECT]    = most recently created/edited project (runtime_state["last_project_path"]).
                    # Old sessions without session.project_path fall back to [PROJECT CONTEXT] for compatibility.
                    try:
                        session_for_proj = session_mgr.load(task.session_id)
                        _workspace = getattr(session_for_proj, "project_path", "") or ""
                        _last_proj = (getattr(session_for_proj, "runtime_state", None) or {}).get("last_project_path") or ""

                        # Self-heal sessions that recorded an unsafe dir (e.g. /home/<user>)
                        # before the project-dir guard existed: never re-inject such paths,
                        # otherwise the agent keeps sending the coder back there.
                        from vaf.tools.coder import is_unsafe_project_dir as _is_unsafe_proj
                        if _last_proj and _is_unsafe_proj(_last_proj):
                            _last_proj = ""
                        if _workspace and _is_unsafe_proj(_workspace):
                            _workspace = ""

                        if _workspace and os.path.isdir(_workspace):
                            _edit_path = (
                                _last_proj
                                if (_last_proj and _last_proj != _workspace and os.path.isdir(_last_proj))
                                else _workspace
                            )
                            proj_note = (
                                f"[SESSION WORKSPACE] All files for this chat are stored in: {_workspace}\n"
                                f"[ACTIVE PROJECT] Most recently created/edited: {_edit_path}\n"
                                f'To edit or modify: coding_agent(task="<your task>", project_path="{_edit_path}")\n\n'
                            )
                            effective_input = proj_note + effective_input
                        elif _last_proj and os.path.isdir(_last_proj):
                            # Fallback for sessions predating session.project_path support
                            proj_note = (
                                f"[PROJECT CONTEXT] The most recently created project is at: {_last_proj}\n"
                                f'To edit, update, or modify files from this project call: '
                                f'coding_agent(task="<your edit task>", project_path="{_last_proj}")\n\n'
                            )
                            effective_input = proj_note + effective_input
                    except Exception:
                        pass

                    # Editor write tools depend on whether an editor document is open and whether selections exist.
                    editor_has_content = False
                    try:
                        session_for_editor = session_mgr.load(task.session_id)
                        runtime_state = getattr(session_for_editor, "runtime_state", None) or {}
                        editor_document = runtime_state.get("editor_document") or {}
                        editor_has_content = bool((editor_document.get("content") or "").strip())
                        editor_selections = runtime_state.get("editor_selections") or []
                        excluded_tools = set()
                        if not editor_has_content:
                            excluded_tools.update({"replace_editor_selection", "replace_editor_text"})
                        elif not editor_selections:
                            excluded_tools.add("replace_editor_selection")
                        agent._excluded_tools = excluded_tools
                    except Exception:
                        agent._excluded_tools = {"replace_editor_selection", "replace_editor_text"}

                    try:
                        if is_debug_logging_enabled():
                            from datetime import datetime as _dt
                            with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
                                f.write(f"{_dt.now().isoformat()} CHAT_STEP_CALL session_id={task.session_id}\n")
                    except Exception:
                        pass
                    # Do not run workflow matching for contact messages (WhatsApp/Telegram/Discord).
                    # Workflows are for the account owner in Web/CLI; contact chat should be normal LLM reply only.
                    task_source = getattr(task, "source", None) or ""
                    disable_workflows = str(task_source).lower() in ("whatsapp", "telegram", "discord")

                    # Keep WebUI sub-agents inside the WebUI panel (no host terminal popups)
                    # even after restarts where global env flags may be unset.
                    task_meta_for_env = (task.metadata or {}) if getattr(task, "metadata", None) else {}
                    origin_channel = str(task_meta_for_env.get("origin_channel") or "").strip().lower()
                    source_channel = str(task_source).strip().lower()
                    prev_webui_active = os.environ.get("VAF_WEBUI_ACTIVE")
                    force_webui_active = origin_channel == "web" or source_channel == "web"
                    _task_images = task_meta_for_env.get("images") or None
                    # Timer wake-turn: a timer task has no preceding user message, so without a boundary
                    # the agent's reply overwrites the previous assistant bubble (same slot + timestamp).
                    # Emit the trigger as a "wake" system-activity message (kind="timer") -> the Web UI
                    # renders it in its own LEFT-side area (not the user side), and it also serves as the
                    # boundary so the agent's reply lands in its OWN new bubble with a correct timestamp.
                    # chat_step persists the input to history itself, so this is a live-display emit only.
                    if task_meta_for_env.get("timer"):
                        try:
                            # role="user" keeps the (proven) bubble boundary so the agent's reply lands in
                            # its own new bubble; kind="timer" tells the Web UI to render it as a LEFT-side
                            # wake card (clock + amber) instead of a user bubble.
                            get_web_interface().emit_agent_message_append(
                                content=str(effective_input), session_id=task.session_id,
                                role="user", kind="timer",
                            )
                        except Exception:
                            pass
                    try:
                        if force_webui_active:
                            os.environ["VAF_WEBUI_ACTIVE"] = "1"
                        response = agent.chat_step(
                            user_input=effective_input,
                            stream_callback=webui_stream_callback,  # Stream to WebUI!
                            skip_input=False,
                            disable_workflows=disable_workflows,
                            memory_context=memory_context or None,
                            images=_task_images,
                        )
                        if tq.should_stop(task.session_id):
                            raise _StopGenerationRequested("Stop requested by user")
                    finally:
                        if force_webui_active:
                            if prev_webui_active is None:
                                os.environ.pop("VAF_WEBUI_ACTIVE", None)
                            else:
                                os.environ["VAF_WEBUI_ACTIVE"] = prev_webui_active
                    try:
                        if is_debug_logging_enabled():
                            from datetime import datetime as _dt
                            with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
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
                        # Final response broadcast: always send full content once so UI has complete message.
                        # (Streaming is throttled, so the last chunk(s) may never have been emitted.)
                        raw_final = "".join(response_parts) if response_parts else response_text

                        # SANITIZE: Remove leaked JSON fragments before showing to user
                        final_text = _strip_tool_calls_json(raw_final)

                        # If agent stopped internally (via should_stop check inside chat_step),
                        # clear the streaming bubble and emit a clean stop notice — same as the
                        # _StopGenerationRequested path — so no partial text leaks through.
                        _agent_stopped_internally = "[Generation stopped by user]" in response_text
                        if _agent_stopped_internally:
                            partial = final_text.replace("[Generation stopped by user]", "").strip()
                            get_web_interface().emit_clear_last_assistant(task.session_id)
                            final_text = (partial + "\n\n*[Generation stopped]*").strip() if partial else "*[Generation stopped]*"
                            get_web_interface().emit_agent_message(
                                role="assistant",
                                content=final_text,
                                session_id=task.session_id
                            )
                        else:
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

                    # ── Hallucination guard ────────────────────────────────────────────
                    # If the agent DESCRIBED calling a sub-agent tool (coding_agent,
                    # librarian_agent …) but the response has no [ASYNC_ACK] marker,
                    # no IPC task was actually registered — it hallucinated.
                    # Inject a correction and re-run chat_step once to force the real call.
                    #
                    # IMPORTANT: We require BOTH an agent-name reference AND an active-running
                    # indicator to be present. This prevents false positives when the agent is
                    # merely reporting a failure ("Der Coding Agent ist gecrasht") or telling
                    # the user it will wait.
                    _agent_name_phrases = [
                        # Exact tool names (underscore form)
                        "coding_agent", "librarian_agent", "research_agent",
                        # Natural-language aliases (EN + DE)
                        "coding agent", "sub-agent", "subagent",
                        "der sub-agent", "der subagent",
                    ]
                    _running_indicator_phrases = [
                        # English – agent described as actively running/started
                        "is now working", "working on it", "has been started",
                        "is currently working", "is running", "has started",
                        "is being created", "agent is working",
                        # German – agent described as actively running/started
                        "arbeitet gerade", "wird erstellt", "wird bearbeitet",
                        "ist gestartet", "startet gerade", "läuft gerade",
                        "ist bereits am laufen", "ist am arbeiten",
                    ]
                    # Exemption: agent explicitly said it would wait → not a hallucination
                    _wait_phrases = [
                        "warten", "warte", "ich warte", "sobald", "wait", "waiting",
                        "bereit für", "ready for",
                    ]
                    _final_lower = final_text.lower()
                    _is_async = response_text.startswith("[ASYNC_ACK]")
                    _is_sys   = response_text.startswith("[SYSTEM_LOG_ONLY]")
                    _hallucination_detected = (
                        not _is_async and not _is_sys
                        and any(p in _final_lower for p in _agent_name_phrases)
                        and any(p in _final_lower for p in _running_indicator_phrases)
                        and not any(p in _final_lower for p in _wait_phrases)
                    )
                    if _hallucination_detected:
                        try:
                            from vaf.core.subagent_ipc import get_ipc as _get_ipc
                            _ipc = _get_ipc()
                            _active = _ipc.get_active_tasks(session_id=task.session_id)
                            _pending = _ipc.get_pending_results(session_id=task.session_id)
                            if not _active and not _pending:
                                # No actual sub-agent registered → hallucination detected
                                get_web_interface().log(
                                    "Hallucination detected: agent claimed sub-agent is running "
                                    "but no IPC task found. Injecting correction.",
                                    level="warning", source="System", session_id=task.session_id
                                )
                                _correction = (
                                    "[SYSTEM CORRECTION] You just wrote that a sub-agent or the "
                                    "coding agent is working, but you never actually called the "
                                    "tool. Calling a tool means using a tool call — not writing "
                                    "text about it. You MUST now call the appropriate tool "
                                    "(e.g. coding_agent, librarian_agent) to actually start the "
                                    "task. Do NOT write another text response — call the tool."
                                )
                                # Skip correction if user already pressed stop
                                if tq.should_stop(task.session_id):
                                    tq.clear_stop(task.session_id)
                                    get_web_interface().log(
                                        "Hallucination correction skipped — user requested stop.",
                                        level="info", source="System", session_id=task.session_id
                                    )
                                else:
                                  agent.history.append({"role": "user", "content": _correction})
                                  _corr_parts = []
                                  def _corr_stream(text):
                                      if tq.should_stop(task.session_id):
                                          raise _StopGenerationRequested("Stop during hallucination correction")
                                      _corr_parts.append(text)
                                      _t = "".join(_corr_parts)
                                      if _t.strip():
                                          get_web_interface().emit_agent_message(
                                              "assistant", _t, session_id=task.session_id
                                          )
                                  agent.chat_step(
                                      user_input=_correction,
                                      stream_callback=_corr_stream,
                                      skip_input=True,   # already added above
                                      disable_workflows=True,
                                      disable_tools=False,
                                  )
                                  if _corr_parts:
                                      _corr_final = "".join(_corr_parts)
                                      get_web_interface().emit_agent_message(
                                          "assistant", _corr_final, session_id=task.session_id
                                      )
                                      get_web_interface().emit_message_complete(
                                          content=_corr_final, session_id=task.session_id
                                      )
                        except Exception as _hg_err:
                            print(f"[Headless] Hallucination guard error: {_hg_err}")
                    # ── End hallucination guard ───────────────────────────────────────

                    # When user asked for a text (e.g. "Schreib mir einen Text"), open it in Document Editor
                    if not response_text.startswith("[ASYNC_ACK]") and not response_text.startswith("[SYSTEM_LOG_ONLY]"):
                        try:
                            _maybe_open_draft_in_editor(
                                task.session_id or "",
                                task.input_text or "",
                                str(final_text),
                                getattr(task, "source", "web"),
                                editor_has_content=editor_has_content,
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
                            # Strip <think>...</think>, raw tool_calls JSON, workflow-async lines, and internal phrases
                            out = str(final_text)
                            out = re.sub(r'<think>.*?</think>', '', out, flags=re.DOTALL)
                            out = _strip_tool_calls_json(out)
                            out = re.sub(r'\n{3,}', '\n\n', out).strip()
                            out = _strip_workflow_async_from_message(out)
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
                                out = _strip_workflow_async_from_message(out)
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
                                out = _strip_workflow_async_from_message(out)
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
                            # NOTE: Do NOT overwrite `used` with session_usage here.
                            # session_usage is CUMULATIVE (billing) and grows unboundedly.
                            # get_token_usage() already returns the correct context snapshot.
                        stats = {
                            "used": used,
                            "total": total,
                            "percent": round((used / total) * 100, 1) if total else 0.0,
                            "api": bool(api_backend),
                            "input_tokens": input_tokens,
                            "output_tokens": output_tokens
                        }
                        get_web_interface().emit_stats(stats, session_id=task.session_id)
                        # Keep Context Window breakdown (system/history/tools) in sync with totals.
                        agent._broadcast_context_status()
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
                            with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
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
                            """Keep full content for display; only strip redacted blocks."""
                            if not text:
                                return ""
                            # Keep <think> blocks so the UI can show thinking after reload.
                            # <think> is stripped from LLM context in agent.py when replaying history.
                            cleaned = re.sub(r'<redacted_reasoning>.*?</redacted_reasoning>', '', text, flags=re.DOTALL)
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
                                _img_meta = None
                                if _task_images:
                                    _img_meta = {"images": [
                                        {"name": _im.get("name", "image"), "mime_type": _im.get("mime_type", "image/jpeg"), "data": _im.get("data", "")}
                                        for _im in _task_images
                                    ]}
                                session.add_message(role="user", content=_user_input.strip(), metadata=_img_meta)
                                # Increment persistent user_turn_count in runtime_state
                                if not hasattr(session, 'runtime_state') or session.runtime_state is None:
                                    session.runtime_state = {}
                                session.runtime_state["user_turn_count"] = session.runtime_state.get("user_turn_count", 0) + 1
                                # Persist this turn's context artifacts from the live agent
                                # history — either the raw tool scaffolding (assistant tool_calls
                                # + role:"tool" results) or, after the turn-end squash, the
                                # "[Context: ...]" summary that records each tool's outcome/error.
                                # This keeps the agent aware of what it did (and which errors it
                                # hit) across session reloads. Plain assistant text is saved
                                # separately below as _clean_response, so it is skipped here.
                                try:
                                    for _tm in turn_context_messages_since_last_user(getattr(agent, "history", []) or [], _user_input):
                                        session.add_message(
                                            role=_tm.get("role"),
                                            content=str(_tm.get("content") or ""),
                                            tool_calls=_tm.get("tool_calls"),
                                            tool_call_id=_tm.get("tool_call_id"),
                                            name=_tm.get("name"),
                                        )
                                except Exception:
                                    pass
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
                                # sync_state=True ensures the ContextManager state is saved to runtime_state
                                session_mgr.save(session, sync_state=True)
                    except Exception as e:
                        logging.getLogger(__name__).warning(f"Failed to save session: {e}")
                        _post_chat_ok = False  # Skip memory-intensive operations
                        gc.collect()  # Defensive GC on error
                    try:
                        if is_debug_logging_enabled():
                            from datetime import datetime as _dt
                            with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
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
                                with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
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
                                with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
                                    f.write(f"{_dt.now().isoformat()} AUTO_CAPTURE_END session_id={task.session_id}\n")
                        except Exception:
                            pass

                    # 3. Session compaction: main user chats (Web, Telegram, WhatsApp, Discord).
                    #    DSGVO: skip contact chats (from_contact=True) — never learn from other people's messages.
                    if _post_chat_ok:
                        try:
                            if is_debug_logging_enabled():
                                from datetime import datetime as _dt
                                with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
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
                                        with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
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
                                with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
                                    f.write(f"{_dt.now().isoformat()} COMPACTION_CHECK_END session_id={task.session_id}\n")
                        except Exception:
                            pass

                    # Result is already added to history and broadcast to WebUI by agent logic
                    _duration = time.time() - _chat_start
                    try:
                        if is_debug_logging_enabled():
                            from datetime import datetime as _dt
                            with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
                                f.write(f"{_dt.now().isoformat()} QUEUE_CHAT_END session_id={task.session_id} duration_sec={_duration:.1f}\n")
                    except Exception:
                        pass
                    print("[Headless] Task complete.")

                    # Memory cleanup: clear response parts list
                    if 'response_parts' in dir():
                        response_parts.clear()

                except _StopGenerationRequested:
                    try:
                        tq.clear_stop(task.session_id)
                    except Exception:
                        pass
                    # Capture whatever partial content streamed before the stop
                    _partial = ""
                    try:
                        if "response_parts" in dir():
                            _partial = "".join(response_parts).strip()
                            response_parts.clear()
                    except Exception:
                        pass
                    try:
                        # Clear the streaming bubble so no partial text leaks through
                        # (chunks already queued in the asyncio loop arrive before this,
                        # then this wipes them, then the final message appears cleanly)
                        get_web_interface().emit_clear_last_assistant(task.session_id)
                        final_stop_content = (_partial + "\n\n*[Generation stopped]*").strip() if _partial else "*[Generation stopped]*"
                        get_web_interface().emit_agent_message(
                            role="assistant",
                            content=final_stop_content,
                            session_id=task.session_id,
                        )
                        get_web_interface().emit_message_complete(
                            content=final_stop_content,
                            session_id=task.session_id,
                        )
                    except Exception:
                        pass
                    try:
                        if is_debug_logging_enabled():
                            from datetime import datetime as _dt
                            with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
                                f.write(f"{_dt.now().isoformat()} QUEUE_CHAT_STOPPED session_id={task.session_id}\n")
                    except Exception:
                        pass
                except Exception as e:
                    _duration = time.time() - _chat_start
                    try:
                        if is_debug_logging_enabled():
                            from datetime import datetime as _dt
                            err_preview = str(e).replace("\n", " ")[:120]
                            with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
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
                        with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
                            f.write(f"{_dt.now().isoformat()} QUEUE_DONE session_id={task.session_id} (chat)\n")
                except Exception:
                    pass
                tq.task_done()
            else:
                # Background maintenance runs only on primary worker to avoid duplicates.
                if worker_id != 1:
                    time.sleep(0.05)
                    continue
                # Periodically check for sub-agent results and summarize for WebUI
                now = time.time()
                if now - last_subagent_check >= 1.0:
                    last_subagent_check = now
                    try:
                        pending_results = agent._check_subagent_results()
                        if pending_results:
                            found_results_text = []
                            any_needs_retry = False
                            cancelled_sessions = set()
                            for result_task in pending_results:
                                # Ensure agent context is aligned with result session
                                session_id = result_task.session_id or getattr(agent, "current_session_id", None)
                                if session_id:
                                    agent.load_session_context(session_id)
                                    subagent_last_activity[session_id] = now
                                err_text = str(getattr(result_task, "error", "") or "")
                                err_lower = err_text.lower()
                                is_user_cancelled = (
                                    result_task.status == "failed"
                                    and (
                                        "[user_cancelled]" in err_lower
                                        or "stopped/cancelled by user via stop button" in err_lower
                                        or "stopped by user via stop button" in err_lower
                                        or "cancelled by user via stop button" in err_lower
                                    )
                                )
                                if agent._process_subagent_result(result_task):
                                    any_needs_retry = True
                                
                                # Ensure WebUI opens the Sub-Agent window even if no active task was seen.
                                # workflow:* tasks belong to the WorkflowRuntime panel — skip ONLY the
                                # SubAgentWindow update, but still allow found_results_text to be populated.
                                _is_workflow_task = str(getattr(result_task, 'agent_type', '') or '').startswith('workflow:')
                                sid = session_id
                                if sid and not _is_workflow_task:
                                    status_label = "Completed"
                                    if is_user_cancelled:
                                        status_label = "Stopped/Cancelled"
                                    elif result_task.status == "failed":
                                        status_label = "Failed"
                                    elif result_task.status == "timeout":
                                        status_label = "Timed out"
                                    presence = "idle" if is_user_cancelled else ("error" if result_task.status in ("failed", "timeout") else "idle")
                                    provider, model = _get_subagent_model_info()
                                    step_status = "timeout" if is_user_cancelled else ("completed" if result_task.status in ("completed", "failed", "timeout") else "running")
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
                                elif is_user_cancelled:
                                    try:
                                        if session_id:
                                            cancelled_sessions.add(session_id)
                                            get_web_interface()._push_session_update(session_id, {
                                                "type": "subagent_output",
                                                "taskId": result_task.task_id,
                                                "agentType": result_task.agent_type,
                                                "status": "timeout",
                                                "output": "Sub-Agent stopped/cancelled by user."
                                            })
                                    except Exception:
                                        pass
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

                            if cancelled_sessions:
                                try:
                                    for sid in cancelled_sessions:
                                        tq.clear_stop(sid)
                                except Exception:
                                    pass
                            if found_results_text or any_needs_retry:
                                user_lang = "auto"
                                for msg in reversed(agent.history):
                                    if msg.get("role") == "user":
                                        user_lang = agent._detect_user_language(msg.get("content", ""))
                                        break

                                native_lang = agent.LANGUAGE_NAMES_NATIVE.get(user_lang, user_lang)
                                # Coding/workflow results can be ~2000 chars — keep them whole so path+files survive.
                                _is_coding_result = any(
                                    str(getattr(_pt, 'agent_type', '') or '').startswith(('coding_agent', 'workflow:'))
                                    for _pt in pending_results
                                )
                                _per_limit = 4000 if _is_coding_result else 1000
                                combined_results = "\n\n---\n\n".join(r[:_per_limit] for r in found_results_text) if found_results_text else ""

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
                                elif _is_coding_result:
                                    # Coding/workflow task: agent MUST relay file info before anything else.
                                    if user_lang == "de":
                                        instruction_prompt = (
                                            f"Der Coding Agent / Workflow hat folgendes Ergebnis geliefert:\n\n"
                                            f"{combined_results}\n\n"
                                            f"**Antworte dem User JETZT auf DEUTSCH** — in dieser Reihenfolge:\n"
                                            f"1. Wo die Datei(en) liegen (vollständiger Pfad)\n"
                                            f"2. Dateiname(n) und Größe\n"
                                            f"3. Wie er/sie es öffnet oder nutzt\n"
                                            f"Erst DANACH darfst du kurz auf Qualitätsprobleme hinweisen — aber nur wenn wirklich nötig.\n"
                                            f"Rufe KEINE Tools auf bevor du geantwortet hast. ANTWORTE AUSSCHLIESSLICH AUF DEUTSCH."
                                        )
                                    else:
                                        instruction_prompt = (
                                            f"The coding agent / workflow delivered the following result:\n\n"
                                            f"{combined_results}\n\n"
                                            f"**Reply to the user NOW in {native_lang}** — in this exact order:\n"
                                            f"1. Where the file(s) are located (full path)\n"
                                            f"2. File name(s) and size\n"
                                            f"3. How to open or use it\n"
                                            f"Only AFTER that may you briefly mention quality issues — and only if truly necessary.\n"
                                            f"Do NOT call any tools before replying. RESPOND EXCLUSIVELY IN {native_lang.upper()}."
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

                                # POST-CHAT: Emit final message + save session
                                # (mirrors main chat path – ensures browser sees the response even
                                # if WebSocket dropped during streaming, and persists it for reload)
                                _result_sid = getattr(agent, "current_session_id", None)
                                _final_result_text = "".join(response_parts)
                                _clean_result = re.sub(r"<think>.*?</think>", "", _final_result_text, flags=re.DOTALL)
                                _clean_result = _strip_tool_calls_json(_clean_result)
                                _clean_result = re.sub(r"\n{3,}", "\n\n", _clean_result).strip()

                                if _clean_result:
                                    # 1. Final emit – guarantees browser has complete message
                                    try:
                                        get_web_interface().emit_agent_message(
                                            "assistant",
                                            _clean_result,
                                            session_id=_result_sid
                                        )
                                    except Exception:
                                        pass
                                    # 2. message_complete – closes streaming bubble, triggers TTS
                                    try:
                                        get_web_interface().emit_message_complete(
                                            content=_clean_result,
                                            session_id=_result_sid
                                        )
                                    except Exception:
                                        pass
                                    # 3. Save to session so browser reload / reconnect shows the response
                                    try:
                                        if _result_sid:
                                            _saved_sess = session_mgr.load(_result_sid)
                                            _saved_sess.add_message(role="assistant", content=_clean_result)
                                            session_mgr.save(_saved_sess)
                                    except Exception:
                                        pass

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
                        import traceback as _tb
                        print(f"[Headless] Sub-agent result processing error: {e}")
                        # Emit a fallback notification so the browser is not left waiting
                        try:
                            _fb_sid = getattr(agent, "current_session_id", None)
                            _fb_msg = f"Sub-Agent completed — summary could not be generated ({type(e).__name__})."
                            get_web_interface().emit_agent_message("assistant", _fb_msg, session_id=_fb_sid)
                            get_web_interface().emit_message_complete(content=_fb_msg, session_id=_fb_sid)
                        except Exception:
                            pass

                # Periodically update Sub-Agent window state for WebUI
                if now - last_subagent_ui_update >= 1.0:
                    last_subagent_ui_update = now
                    try:
                        from vaf.core.subagent_ipc import get_ipc, get_current_session_id
                        ipc = get_ipc()
                        active_tasks = ipc.get_active_tasks()
                        # Skip workflow:* tasks — they are already displayed in the
                        # VAFWorkflowRuntime panel and must not open the SubAgentWindow.
                        active_tasks = [t for t in active_tasks if not str(getattr(t, 'agent_type', '') or '').startswith('workflow:')]
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
                                heartbeat_ages = []
                                for task in tasks:
                                    status = "running"
                                    if task.status == "completed":
                                        status = "completed"
                                    elif task.status == "pending":
                                        status = "pending"
                                    try:
                                        hb = getattr(task, "last_heartbeat", None)
                                        if hb:
                                            from datetime import datetime as _dt
                                            age = max(0.0, (now - _dt.fromisoformat(hb).timestamp()))
                                            heartbeat_ages.append(int(age))
                                    except Exception:
                                        pass
                                    steps.append({
                                        "id": task.task_id,
                                        "title": task.agent_type.replace("_", " ").title(),
                                        "description": task.task_description,
                                        "status": status,
                                        "actions": []
                                    })

                                subagent_last_steps[sid] = steps
                                hb_status = ""
                                if heartbeat_ages:
                                    hb_status = f" (heartbeat {min(heartbeat_ages)}s ago)"
                                get_web_interface()._push_session_update(sid, {
                                    "type": "subagent_update",
                                    "agentName": "Sub-Agent",
                                    "status": f"Running sub-agent tasks...{hb_status}",
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
            
        elif cmd_type == "LOAD_SESSION" and len(parts) >= 3:
            sid = parts[2].strip()
            # Explicitly load the session context (history + runtime state)
            agent.load_session_context(sid)
            print(f"[Headless] LOAD_SESSION: Switched agent context to {sid}")

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
                        "percent": round((used / total) * 100, 1) if total else 0.0,
                        "api": bool(getattr(agent, 'api_backend', False))
                    }
                    get_web_interface().emit_stats(stats)
                    print(f"[Headless] Stats updated: api={stats['api']}")
                except Exception:
                    pass
            
    except Exception as e:
        print(f"[Headless] Command error: {e}")
