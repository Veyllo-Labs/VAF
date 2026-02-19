"""
Thinking Mode (Denkmodus) - Background reflection when user is idle.
Starts one run per user when idle for thinking_idle_minutes; respects automation schedule;
cancels when user becomes active; stores suggestions via save_thinking_suggestion tool.
"""
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Any, Dict

from vaf.core.platform import Platform

logger = logging.getLogger(__name__)

LOCKS_FILENAME = "thinking_mode_locks.json"


def _locks_path() -> Path:
    return Platform.data_dir() / LOCKS_FILENAME


def _key(user_scope_id: Any) -> str:
    if user_scope_id is None:
        return "default"
    return str(user_scope_id).strip()


def _load_locks() -> Dict[str, dict]:
    path = _locks_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
        if not raw.strip():
            return {}
        return json.loads(raw)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_locks(data: Dict[str, dict]) -> None:
    path = _locks_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def acquire_lock(user_scope_id: Optional[str], max_duration_minutes: int = 30) -> Optional[str]:
    """
    Acquire lock for this user. Returns run_id if acquired, None if already locked.
    If existing lock is older than max_duration_minutes, replace it (stale).
    """
    key = _key(user_scope_id)
    locks = _load_locks()
    now = time.time()
    existing = locks.get(key)
    if existing:
        try:
            started = float(existing.get("started_at_ts", 0))
            if now - started < max_duration_minutes * 60:
                return None
        except (TypeError, ValueError):
            pass
    run_id = str(uuid.uuid4())[:8]
    locks[key] = {
        "started_at": datetime.now().isoformat(),
        "started_at_ts": now,
        "run_id": run_id,
    }
    _save_locks(locks)
    return run_id


def release_lock(user_scope_id: Optional[str]) -> None:
    """Release lock for this user."""
    key = _key(user_scope_id)
    locks = _load_locks()
    if key in locks:
        del locks[key]
        _save_locks(locks)


def is_locked(user_scope_id: Optional[str], max_duration_minutes: int = 30) -> bool:
    """True if user has an active lock (or stale lock within max_duration)."""
    key = _key(user_scope_id)
    locks = _load_locks()
    existing = locks.get(key)
    if not existing:
        return False
    try:
        started = float(existing.get("started_at_ts", 0))
        return (time.time() - started) < max_duration_minutes * 60
    except (TypeError, ValueError):
        return True


def get_idle_user_scope_ids(idle_minutes: float) -> List[str]:
    """
    Return list of user_scope_id that have been idle for at least idle_minutes.
    Reads last_interaction.json (same store as last_interaction module).
    """
    from vaf.core.last_interaction import get_last_interaction
    path = Platform.data_dir() / "last_interaction.json"
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
        if not raw.strip():
            return []
        data = json.loads(raw)
        now = time.time()
        threshold = now - (idle_minutes * 60)
        out = []
        for key in data:
            if not isinstance(key, str):
                continue
            entry = data.get(key)
            if not isinstance(entry, dict):
                continue
            ts = entry.get("ts")
            if ts is None:
                continue
            try:
                if float(ts) <= threshold:
                    out.append(key if key != "default" else None)
            except (TypeError, ValueError):
                continue
        return out
    except (json.JSONDecodeError, OSError):
        return []


def should_skip_for_automation(user_scope_id: Optional[str], buffer_minutes: int) -> bool:
    """True if an automation runs within buffer_minutes for this user (skip thinking start)."""
    from vaf.core.automation import get_next_automation_run_utc
    next_run = get_next_automation_run_utc(user_scope_id)
    if next_run is None:
        return False
    delta = (next_run - datetime.now()).total_seconds()
    return 0 <= delta < buffer_minutes * 60


THINKING_PROMPT = """You are the main agent in **Thinking Mode** (Denkmodus). The user has been idle; use this time to **act** on their behalf. Work through the steps below, then summarize in a short reply in the user's language. When you have finished, reply with what you did – that concludes this pass.

**Priority: act first.** Create automations, process todos/notes. If you need the user's decision (e.g. "Should I do X?"), ask them **once** via main_messenger (Telegram/WhatsApp/etc. according to their main_messenger). Do **not** send multiple messages; do **not** mention "Denkmodus" or "check the app".

1. **System health:** Unread important emails, upcoming reminders – note briefly in your final reply if relevant.

2. **Todos and notes:** Call list_automation_todos and list_automation_notes. Work through open todos (mark done where appropriate). Act.

3. **Automations:** Call list_automations. If something is clearly missing (e.g. weekly report tomorrow), **create_automation** yourself. If you are unsure, ask the user **once** via main_messenger.

4. **User knowledge / proactive help:** If you can help concretely, do it. If you need the user's input, send **at most one** short message via main_messenger asking – no spam.

**Mindset:** The user's interest comes first. This is your chance to really help – take load off them. Ask yourself: What can I automate for them? What can I get done for them that I'm allowed to do? What notes and todos do we have – what can I take care of for the user right now? Then do it.

When you are done, reply briefly here: what you did (todos processed, automations created, one message sent if any). That concludes this pass."""


def _run_thinking_for_user(
    user_scope_id: Optional[str],
    run_id: str,
    started_at_ts: float,
) -> None:
    """
    Run one thinking pass for the user. Single agent turn: one chat_step(THINKING_PROMPT)
    (the model may invoke multiple tools in that turn); when the model returns a final
    reply, the run ends and the lock is released. No loop – one pass only.
    """
    from vaf.core.last_interaction import get_last_interaction
    from vaf.core.config import Config, get_local_admin_scope_id, get_local_admin_username

    scope_key = _key(user_scope_id)
    max_duration_minutes = int(Config.get("thinking_max_duration_minutes", 30) or 30)
    try:
        from vaf.core.agent import Agent
        import re

        agent = Agent(verbose=False)
        agent.load_model()
        # Set user context BEFORE init_chat() so system prompt (User Identity, RAG scope) and tools get the right user
        agent._current_user_scope_id = user_scope_id
        if not user_scope_id or str(user_scope_id).strip() == str(get_local_admin_scope_id()).strip():
            agent._current_username = get_local_admin_username()
        else:
            agent._current_username = "admin"
        agent.init_chat()

        # Append thinking mode notice to system prompt
        if agent.history and agent.history[0].get("role") == "system":
            notice = (
                "\n\n## THINKING MODE (Denkmodus)\n"
                "You are the **main agent** in a background pass while the user is idle. "
                "Act: create automations, process todos. When unsure, ask the user once via main_messenger. When you are done, reply briefly with what you did."
            )
            agent.history[0]["content"] = (agent.history[0]["content"] or "") + notice

        logger.info("Thinking started for user %s", scope_key[:8] if scope_key != "default" else "default")
        os.environ["VAF_THINKING_MODE"] = "1"

        try:
            # RAG context for this turn
            memory_context = ""
            try:
                if Config.get("memory_enabled", True):
                    from vaf.memory.rag import run_memory_search_sync
                    from uuid import UUID as _UUID
                    k = max(1, min(20, int(Config.get("memory_rag_k", 5))))
                    task_scope = None
                    if user_scope_id:
                        try:
                            task_scope = _UUID(str(user_scope_id))
                        except (ValueError, TypeError):
                            pass
                    memory_context = run_memory_search_sync(
                        query=THINKING_PROMPT[:200], k=k, user_scope_id=task_scope, caller="thinking_mode"
                    ) or ""
            except Exception:
                memory_context = ""

            agent.chat_step(THINKING_PROMPT, stream_callback=None, memory_context=memory_context or None)
        except Exception as e:
            logger.exception("Thinking run error for user %s: %s", scope_key[:8] if scope_key != "default" else "default", e)
        finally:
            try:
                agent.shutdown()
            except Exception:
                pass
            os.environ.pop("VAF_THINKING_MODE", None)

        logger.info("Thinking completed for user %s", scope_key[:8] if scope_key != "default" else "default")
    finally:
        release_lock(user_scope_id)


def maybe_start_thinking_for_user(user_scope_id: Optional[str]) -> bool:
    """
    If user is idle, no automation soon, and no lock: acquire lock and start thinking in a background thread.
    Returns True if a run was started.
    """
    from vaf.core.config import Config
    idle_min = float(Config.get("thinking_idle_minutes", 10) or 10)
    buffer_min = int(Config.get("thinking_automation_buffer_minutes", 10) or 10)
    max_duration = int(Config.get("thinking_max_duration_minutes", 30) or 30)

    if should_skip_for_automation(user_scope_id, buffer_min):
        logger.debug("Thinking skipped for user: next automation within %d min", buffer_min)
        return False
    run_id = acquire_lock(user_scope_id, max_duration_minutes=max_duration)
    if run_id is None:
        logger.debug("Thinking already running for user")
        return False
    started_at_ts = time.time()
    thread = threading.Thread(
        target=_run_thinking_for_user,
        args=(user_scope_id, run_id, started_at_ts),
        daemon=True,
    )
    thread.start()
    return True


def thinking_loop_iteration() -> None:
    """
    One iteration of the thinking mode loop: for each idle user, maybe start a thinking run.
    Call this periodically (e.g. every thinking_check_interval_seconds).
    """
    from vaf.core.config import Config
    if not Config.get("thinking_enabled", True):
        return
    idle_min = float(Config.get("thinking_idle_minutes", 10) or 10)
    idle_users = get_idle_user_scope_ids(idle_min)
    for scope in idle_users:
        if maybe_start_thinking_for_user(scope):
            break  # Start one at a time per iteration to avoid thundering herd


_background_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


def _background_loop() -> None:
    """Daemon loop: every N seconds run thinking_loop_iteration."""
    from vaf.core.config import Config
    interval = max(30, int(Config.get("thinking_check_interval_seconds", 60) or 60))
    while not _stop_event.is_set():
        try:
            thinking_loop_iteration()
        except Exception as e:
            logger.exception("Thinking mode loop error: %s", e)
        if _stop_event.wait(timeout=interval):
            break


def start_thinking_mode_background() -> None:
    """Start the thinking mode background thread (e.g. from web server). Idempotent."""
    global _background_thread
    from vaf.core.config import Config
    if not Config.get("thinking_enabled", True):
        return
    if _background_thread is not None and _background_thread.is_alive():
        return
    _stop_event.clear()
    _background_thread = threading.Thread(target=_background_loop, daemon=True)
    _background_thread.start()
    logger.info("Thinking mode background loop started (interval %s s)", Config.get("thinking_check_interval_seconds", 60))


def stop_thinking_mode_background() -> None:
    """Stop the background loop (e.g. on server shutdown)."""
    global _background_thread
    _stop_event.set()
    _background_thread = None
