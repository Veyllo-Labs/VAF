"""
Thinking mode – background reflection when user is idle.
Starts one run per user when idle for thinking_idle_minutes; respects automation schedule;
cancels when user becomes active. Run logs (tool calls, history) are saved for inspection.
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


# --- Waiting for user reply (after agent asked a question in thinking mode) ---
WAITING_REPLY_FILENAME = "thinking_waiting_reply.json"
LAST_REPLY_FILENAME = "thinking_last_reply.json"
LAST_REPLY_PREVIEW_MAX = 500
LAST_THINKING_SESSION_FILENAME = "thinking_last_session_id.json"
USER_REPLIES_FILENAME = "thinking_user_replies.json"


def _waiting_path() -> Path:
    return Platform.data_dir() / WAITING_REPLY_FILENAME


def _load_waiting() -> Dict[str, dict]:
    path = _waiting_path()
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


def _save_waiting(data: Dict[str, dict]) -> None:
    path = _waiting_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def set_waiting_for_reply(
    user_scope_id: Optional[str],
    username: str,
    display_name: str = "",
) -> None:
    """Record that we sent a question to the user; we will wait for reply, then nudge at 3 min, skip at 10 min."""
    key = _key(user_scope_id)
    data = _load_waiting()
    data[key] = {
        "question_sent_at_ts": time.time(),
        "nudge_sent_at_ts": None,
        "username": (username or "").strip() or "admin",
        "display_name": (display_name or username or "admin").strip() or "admin",
    }
    _save_waiting(data)


def _last_reply_path() -> Path:
    return Platform.data_dir() / LAST_REPLY_FILENAME


def _load_last_reply() -> Dict[str, dict]:
    path = _last_reply_path()
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


def _save_last_reply(data: Dict[str, dict]) -> None:
    path = _last_reply_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def clear_waiting_for_reply(
    user_scope_id: Optional[str],
    user_reply_text: Optional[str] = None,
) -> None:
    """User replied or we skipped after 10 min; clear waiting state. If user_reply_text is given, save it for the next thinking run and for the thinking-session UI."""
    key = _key(user_scope_id)
    if user_reply_text is not None and (user_reply_text or "").strip():
        preview = (user_reply_text or "").strip()
        if len(preview) > LAST_REPLY_PREVIEW_MAX:
            preview = preview[:LAST_REPLY_PREVIEW_MAX] + "…"
        data = _load_last_reply()
        data[key] = {
            "reply_preview": preview,
            "reply_at_ts": time.time(),
        }
        _save_last_reply(data)
        # Attach reply to last thinking session so it can be shown in that session's UI
        last_sid = get_and_clear_last_thinking_session_id(user_scope_id)
        if last_sid:
            replies = _load_user_replies()
            replies[last_sid] = {"reply": preview, "at": datetime.now().isoformat()}
            _save_user_replies(replies)
    data = _load_waiting()
    if key in data:
        del data[key]
        _save_waiting(data)


def get_waiting_for_reply(user_scope_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Return waiting state for this user or None."""
    key = _key(user_scope_id)
    data = _load_waiting()
    return data.get(key)


def get_and_clear_last_reply(user_scope_id: Optional[str]) -> Optional[str]:
    """
    Return the saved user reply preview for the next thinking run, then remove it (one-time use).
    Returns None if no reply was stored.
    """
    key = _key(user_scope_id)
    data = _load_last_reply()
    entry = data.get(key)
    if not entry or not isinstance(entry, dict):
        return None
    preview = (entry.get("reply_preview") or "").strip()
    if key in data:
        del data[key]
        _save_last_reply(data)
    return preview if preview else None


# --- Last thinking session id (for associating user replies with a session in the UI) ---

def _last_session_id_path() -> Path:
    return Platform.data_dir() / LAST_THINKING_SESSION_FILENAME


def _user_replies_path() -> Path:
    return Platform.data_dir() / USER_REPLIES_FILENAME


def _load_last_session_ids() -> Dict[str, str]:
    path = _last_session_id_path()
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


def _save_last_session_ids(data: Dict[str, str]) -> None:
    path = _last_session_id_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def set_last_thinking_session_id(user_scope_id: Optional[str], session_id: str) -> None:
    """Record the thinking session id for this user so the next user reply can be attached to it in the UI."""
    key = _key(user_scope_id)
    data = _load_last_session_ids()
    data[key] = str(session_id).strip()
    _save_last_session_ids(data)


def get_and_clear_last_thinking_session_id(user_scope_id: Optional[str]) -> Optional[str]:
    """Return the last thinking session id for this user and remove it (used when saving a reply to that session)."""
    key = _key(user_scope_id)
    data = _load_last_session_ids()
    sid = data.pop(key, None)
    if sid is not None:
        _save_last_session_ids(data)
    return sid if sid else None


def _load_user_replies() -> Dict[str, Dict[str, Any]]:
    path = _user_replies_path()
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


def _save_user_replies(data: Dict[str, Dict[str, Any]]) -> None:
    path = _user_replies_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def get_user_reply_for_session(session_id: str) -> Optional[Dict[str, Any]]:
    """Return the stored user reply for this thinking session, if any. Does not remove it."""
    if not session_id or not str(session_id).strip().startswith("thinking_"):
        return None
    data = _load_user_replies()
    return data.get(str(session_id))


def pop_user_reply_for_session(session_id: str) -> Optional[Dict[str, Any]]:
    """Return and remove the stored user reply for this thinking session, if any."""
    if not session_id or not str(session_id).strip().startswith("thinking_"):
        return None
    data = _load_user_replies()
    entry = data.pop(session_id, None)
    if entry is not None:
        _save_user_replies(data)
    return entry


def _send_nudge(user_scope_id: Optional[str], username: str, display_name: str) -> bool:
    """Send a short nudge via main_messenger (e.g. 'Hey Mert, bist du da?'). Returns True if sent."""
    try:
        from vaf.core.messaging_connections import (
            get_messaging_connections,
            get_telegram_chat_id,
            get_whatsapp_chat_jid,
            get_discord_user_id,
        )
        from vaf.core.config import Config

        conn = get_messaging_connections(username=(username or "admin").strip() or "admin", user_scope_id=user_scope_id)
        main = (conn.get("main_messenger") or "").strip().lower()
        name = (display_name or username or "").strip() or "admin"
        nudge = f"Hey {name}, bist du da?"
        if main == "telegram":
            chat_id = get_telegram_chat_id(user_scope_id, username)
            if chat_id:
                from vaf.core.telegram_reply import send_telegram_reply
                send_telegram_reply(chat_id, nudge)
                return True
        elif main == "whatsapp":
            jid = get_whatsapp_chat_jid(user_scope_id, username)
            if jid:
                from vaf.core.whatsapp_reply import send_whatsapp_reply
                send_whatsapp_reply(username or "admin", jid, nudge, user_scope_id=user_scope_id)
                return True
        elif main == "discord":
            user_id = get_discord_user_id(user_scope_id, username)
            if user_id:
                discord_config = Config.get("discord_config") or {}
                bot_token = (discord_config.get("bot_token") or "").strip()
                if bot_token:
                    from vaf.core.discord_send import send_discord_dm
                    if send_discord_dm(bot_token, user_id, nudge, chunk=True):
                        return True
        return False
    except Exception as e:
        logger.warning("Thinking nudge send failed: %s", e)
        return False


def _process_waiting_reply(user_scope_id: Optional[str]) -> str:
    """
    If user is in 'waiting for reply' state: send nudge at 3 min, clear at 10 min.
    Returns: 'allow_run' (no waiting or just cleared), 'skip' (still waiting or nudge sent).
    """
    from vaf.core.config import Config
    w = get_waiting_for_reply(user_scope_id)
    if not w:
        return "allow_run"
    try:
        question_ts = float(w.get("question_sent_at_ts", 0))
        nudge_ts = w.get("nudge_sent_at_ts")
        if nudge_ts is not None:
            try:
                nudge_ts = float(nudge_ts)
            except (TypeError, ValueError):
                nudge_ts = None
    except (TypeError, ValueError):
        return "allow_run"
    now = time.time()
    elapsed_min = (now - question_ts) / 60.0
    nudge_min = float(Config.get("thinking_wait_nudge_minutes", 3) or 3)
    skip_min = float(Config.get("thinking_wait_skip_minutes", 10) or 10)
    if elapsed_min < nudge_min:
        return "skip"
    if elapsed_min >= skip_min:
        clear_waiting_for_reply(user_scope_id)
        return "allow_run"
    if nudge_ts is None:
        if _send_nudge(
            user_scope_id,
            w.get("username") or "admin",
            w.get("display_name") or w.get("username") or "admin",
        ):
            data = _load_waiting()
            key = _key(user_scope_id)
            if key in data:
                data[key]["nudge_sent_at_ts"] = now
                _save_waiting(data)
        return "skip"
    return "skip"


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


def _get_last_thinking_summary(user_scope_id: Optional[str], max_chars: int = 1200) -> str:
    """Load the most recent thinking-mode run log for this user and return the last assistant reply (context for next run)."""
    try:
        log_dir = Platform.vaf_dir() / "thinking_mode_logs" / _key(user_scope_id)
        if not log_dir.exists():
            return ""
        files = sorted(log_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            return ""
        raw = files[0].read_text(encoding="utf-8")
        data = json.loads(raw)
        messages = data.get("messages") or []
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                content = msg.get("content") or ""
                if isinstance(content, str) and content.strip():
                    return (content.strip()[:max_chars] + "…") if len(content) > max_chars else content.strip()
        return ""
    except Exception:
        return ""


def _build_run_log_messages(agent_history: List[Dict[str, Any]], max_content_len: int = 4000) -> List[Dict[str, Any]]:
    """Build messages list for run log / session: role, content (truncated), tool_calls (names)."""
    messages = []
    for msg in agent_history:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        content = msg.get("content")
        if isinstance(content, str) and len(content) > max_content_len:
            content = content[:max_content_len] + "\n... [truncated]"
        entry = {"role": role, "content": content}
        if "tool_calls" in msg and msg["tool_calls"]:
            names = [tc.get("function", {}).get("name", tc.get("name", "?")) for tc in msg["tool_calls"]]
            entry["tool_calls"] = names
        messages.append(entry)
    return messages


def _save_run_log(
    user_scope_id: Optional[str],
    run_id: str,
    started_at_ts: float,
    agent_history: List[Dict[str, Any]],
) -> tuple:
    """Persist thinking run for inspection; return (started_at_iso, ended_at_iso, messages) for session save."""
    ended = time.time()
    started_iso = datetime.fromtimestamp(started_at_ts).isoformat()
    ended_iso = datetime.fromtimestamp(ended).isoformat()
    messages = _build_run_log_messages(agent_history)
    try:
        log_dir = Platform.vaf_dir() / "thinking_mode_logs" / _key(user_scope_id)
        log_dir.mkdir(parents=True, exist_ok=True)
        out = {
            "run_id": run_id,
            "user_scope_id": _key(user_scope_id),
            "started_at": started_iso,
            "ended_at": ended_iso,
            "duration_seconds": round(ended - started_at_ts, 1),
            "messages": messages,
        }
        path = log_dir / f"{run_id}_{datetime.fromtimestamp(started_at_ts).strftime('%Y%m%d_%H%M%S')}.json"
        path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.debug("Thinking run log saved: %s", path)
    except Exception as e:
        logger.warning("Could not save thinking run log: %s", e)
    return (started_iso, ended_iso, messages)


THINKING_PROMPT = """You are the main agent in **Thinking Mode**. The user has been idle; use this time to **act** on their behalf. Work through the steps below, then summarize in a short reply in the user's language. When you have finished, reply with what you did – that concludes this pass.

**Priority: act first.** Create automations, process todos/notes. If you need the user's decision (e.g. "Should I do X?"), ask them **once** via main_messenger (Telegram/WhatsApp/etc. according to their main_messenger). The system will wait for their reply: if they don't answer within a few minutes, they get a short nudge; if they still don't answer, we skip that question and do other things in a later run. So you only need to ask once and then end your pass.

**Messages to the user (critical):** You may send **at most one** message to the user in this entire run. Write it like a normal human would: natural, friendly, no meta-talk. Never say "I'm in thinking mode", "I'm running in the background" or that you're an agent – just write the message (e.g. "I've set up the weekly report for tomorrow" or "Quick question: should I do X?"). If you already sent one message, do not send another in this run. After you ask something, the system waits for their reply in the background; you end this pass and they can answer later.

1. **System health:** Unread important emails, upcoming reminders – note briefly in your final reply if relevant.

2. **Todos and notes:** Call list_automation_todos and list_automation_notes. Work through open todos (mark done where appropriate). Act.

3. **Automations:** Call list_automations. If something is clearly missing (e.g. weekly report tomorrow), **create_automation** yourself. If you are unsure, ask the user **once** via main_messenger (one message only, natural wording).

4. **User knowledge / proactive help:** If you can help concretely, do it. If you need the user's input, send **at most one** short message via main_messenger – natural, human tone. No spam.

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

        agent = Agent(verbose=False)
        agent.load_model()
        # Set user context BEFORE init_chat() so system prompt (User Identity, RAG scope) and tools get the right user
        agent._current_user_scope_id = user_scope_id
        if not user_scope_id or str(user_scope_id).strip() == str(get_local_admin_scope_id()).strip():
            agent._current_username = get_local_admin_username()
        else:
            agent._current_username = "admin"
        agent.init_chat()

        # Append thinking mode notice and last run summary (context so we don't repeat or re-ask)
        if agent.history and agent.history[0].get("role") == "system":
            notice = (
                "\n\n## THINKING MODE\n"
                "You are the **main agent** in a background pass while the user is idle. "
                "Act: create automations, process todos. When you send a message, write like a normal human – never say you're in thinking mode or running in the background. At most one message per run. If you ask something, the system will wait for their reply (nudge after 3 min, skip after 10 min); end this pass after your one message."
            )
            last_summary = _get_last_thinking_summary(user_scope_id)
            if last_summary:
                notice += (
                    "\n\n**Last thinking-mode run (for context only – do not repeat these actions or ask the same again):**\n"
                    + last_summary
                )
            last_reply = get_and_clear_last_reply(user_scope_id)
            if last_reply:
                notice += "\n\n**User reply to your last question:** " + last_reply
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
            # Persist run for inspection (tool calls, messages) and as session for Web UI chat list
            try:
                started_iso, ended_iso, log_messages = _save_run_log(
                    user_scope_id, run_id, started_at_ts, getattr(agent, "history", [])
                )
                try:
                    from vaf.core.session import SessionManager
                    sid = SessionManager().save_thinking_run(
                        user_scope_id, run_id, started_iso, ended_iso, log_messages
                    )
                    if sid:
                        set_last_thinking_session_id(user_scope_id, sid)
                except Exception as sess_err:
                    logger.warning("Could not save thinking run as session: %s", sess_err)
            except Exception as log_err:
                logger.warning("Thinking run log save failed: %s", log_err)
            # If agent sent a message (question), wait for reply: nudge at 3 min, skip question after 10 min
            try:
                history = getattr(agent, "history", []) or []
                sent_tools = {"send_telegram", "send_whatsapp", "send_discord"}
                for msg in history:
                    if not isinstance(msg, dict) or msg.get("role") != "assistant":
                        continue
                    for tc in msg.get("tool_calls") or []:
                        name = (tc.get("function") or {}).get("name") or tc.get("name") or ""
                        if name in sent_tools:
                            uname = getattr(agent, "_current_username", None) or "admin"
                            display_name = uname
                            try:
                                from vaf.auth.user_workspace import get_user_workspace
                                ws = get_user_workspace(uname)
                                ui = ws.get_user_identity() or {}
                                display_name = (ui.get("name") or "").strip() or uname
                            except Exception:
                                pass
                            set_waiting_for_reply(user_scope_id, uname, display_name=display_name)
                            break
                    else:
                        continue
                    break
            except Exception:
                pass
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
        if _process_waiting_reply(scope) == "skip":
            continue  # Waiting for reply: nudge at 3 min, allow run after 10 min
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
