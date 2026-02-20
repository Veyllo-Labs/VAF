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
LAST_COMPLETED_FILENAME = "thinking_last_completed.json"
DECLINED_QUESTIONS_FILENAME = "thinking_declined_questions.json"
_DECLINED_MAX_ENTRIES = 20
_DECLINED_MAX_AGE_DAYS = 30


def _locks_path() -> Path:
    return Platform.data_dir() / LOCKS_FILENAME


def _key(user_scope_id: Any) -> str:
    """Canonical key for storage; local admin scope maps to 'default' so one user = one key."""
    if user_scope_id is None:
        return "default"
    try:
        from vaf.core.config import get_local_admin_scope_id
        if str(user_scope_id).strip() == str(get_local_admin_scope_id()).strip():
            return "default"
    except Exception:
        pass
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


# --- Cooldown: prevent rapid-fire thinking runs ---

def _last_completed_path() -> Path:
    return Platform.data_dir() / LAST_COMPLETED_FILENAME


def _set_last_run_completed(user_scope_id: Optional[str]) -> None:
    """Record that a thinking run just finished for this user."""
    path = _last_completed_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (json.JSONDecodeError, OSError):
        data = {}
    data[_key(user_scope_id)] = {"completed_at_ts": time.time()}
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _minutes_since_last_run(user_scope_id: Optional[str]) -> float:
    """Return minutes since last completed thinking run for this user. Returns inf if no record."""
    path = _last_completed_path()
    if not path.exists():
        return float("inf")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entry = data.get(_key(user_scope_id))
        if not entry:
            return float("inf")
        return (time.time() - float(entry["completed_at_ts"])) / 60.0
    except (json.JSONDecodeError, OSError, KeyError, TypeError, ValueError):
        return float("inf")


# --- Declined questions: prevent repeating questions the user already refused ---

def _declined_path() -> Path:
    return Platform.data_dir() / DECLINED_QUESTIONS_FILENAME


def _load_declined(user_scope_id: Optional[str]) -> List[Dict[str, str]]:
    """Load declined questions for this user (auto-expire old entries)."""
    path = _declined_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data.get(_key(user_scope_id)) or []
        if not isinstance(entries, list):
            return []
        cutoff = time.time() - _DECLINED_MAX_AGE_DAYS * 86400
        return [e for e in entries if isinstance(e, dict) and float(e.get("ts", 0)) > cutoff]
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return []


def _save_declined_entry(user_scope_id: Optional[str], question: str, user_reply: str) -> None:
    """Add a declined question to the persistent log."""
    path = _declined_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (json.JSONDecodeError, OSError):
        data = {}
    key = _key(user_scope_id)
    entries = data.get(key) or []
    if not isinstance(entries, list):
        entries = []
    entries.append({
        "question": (question or "")[:500],
        "user_reply": (user_reply or "")[:200],
        "ts": time.time(),
        "at": datetime.now().isoformat(),
    })
    # Keep only latest N entries
    data[key] = entries[-_DECLINED_MAX_ENTRIES:]
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _is_refusal(text: str) -> bool:
    """Return True if the user's reply is a refusal/decline."""
    t = (text or "").strip().lower()
    refusal_keywords = [
        "nein", "no", "nicht", "erstmal nicht", "later", "stop",
        "lass", "hör auf", "aufhören", "nie", "never", "don't",
        "kein", "bitte nicht", "ich will nicht", "brauch ich nicht",
    ]
    return any(kw in t for kw in refusal_keywords)


def _get_declined_questions_prompt(user_scope_id: Optional[str]) -> str:
    """Build prompt section listing declined questions so the agent knows not to ask them again."""
    entries = _load_declined(user_scope_id)
    if not entries:
        return ""
    lines = ["**Questions the user has already declined (DO NOT ask these again, DO NOT suggest these topics):**"]
    for e in entries:
        q = (e.get("question") or "").strip()
        r = (e.get("user_reply") or "").strip()
        if q:
            lines.append(f'- "{q}" → User said: "{r}"')
    return "\n".join(lines)


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
    question_text: str = "",
) -> None:
    """Record that we sent a question to the user; we will wait for reply, then nudge at 3 min, skip at 10 min."""
    key = _key(user_scope_id)
    data = _load_waiting()
    data[key] = {
        "question_sent_at_ts": time.time(),
        "nudge_sent_at_ts": None,
        "username": (username or "").strip() or "admin",
        "display_name": (display_name or username or "admin").strip() or "admin",
        "question_text": (question_text or "")[:500],
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
        # If user declined, save the actual sent question + reply to declined-questions log
        if _is_refusal(preview):
            # Get the real question text from the waiting state (before we clear it)
            waiting_data = _load_waiting()
            waiting_entry = waiting_data.get(key) or {}
            actual_question = (waiting_entry.get("question_text") or "").strip()
            if not actual_question:
                # Fallback: use last assistant summary if question_text wasn't stored
                actual_question = _get_last_thinking_summary(user_scope_id, max_chars=500)
            if actual_question:
                _save_declined_entry(user_scope_id, actual_question, preview)
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


def get_idle_user_scope_ids(idle_minutes: float) -> List[Optional[str]]:
    """
    Return list of user_scope_id that have been idle for at least idle_minutes.
    Reads last_interaction.json (same store as last_interaction module).
    Normalizes so that "default" and local_admin_scope_id count as one user (None).
    """
    from vaf.core.config import get_local_admin_scope_id
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
        local_admin_scope = str(get_local_admin_scope_id()).strip()
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
                    scope_id = None if key == "default" else key
                    if scope_id is not None and scope_id == local_admin_scope:
                        scope_id = None
                    out.append(scope_id)
            except (TypeError, ValueError):
                continue
        # Deduplicate: one canonical entry per logical user (local admin = None)
        seen = set()
        normalized = []
        for s in out:
            k = (s is None, s if s is not None else "")
            if k not in seen:
                seen.add(k)
                normalized.append(s)
        return normalized
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


def _get_last_thinking_summary(user_scope_id: Optional[str], max_chars: int = 2000) -> str:
    """
    Load the last 3 thinking-mode run logs for this user and build a structured summary.
    Includes: what the agent did (tool calls), what it said, and user replies.
    Falls back to single-run summary for the 500-char variant used by declined-questions.
    """
    try:
        log_dir = Platform.vaf_dir() / "thinking_mode_logs" / _key(user_scope_id)
        if not log_dir.exists():
            return ""
        files = sorted(log_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            return ""

        # For short max_chars (e.g. declined-questions caller), just return last assistant message
        if max_chars <= 500:
            raw = files[0].read_text(encoding="utf-8")
            data = json.loads(raw)
            messages = data.get("messages") or []
            for msg in reversed(messages):
                if isinstance(msg, dict) and msg.get("role") == "assistant":
                    content = msg.get("content") or ""
                    if isinstance(content, str) and content.strip():
                        return (content.strip()[:max_chars] + "…") if len(content) > max_chars else content.strip()
            return ""

        # Build structured summary from last 3 runs
        summaries = []
        for i, f in enumerate(files[:3]):
            try:
                raw = f.read_text(encoding="utf-8")
                data = json.loads(raw)
                messages = data.get("messages") or []
                started = data.get("started_at", "")[:16].replace("T", " ")

                # How long ago
                try:
                    started_ts = datetime.fromisoformat(data.get("started_at", "")).timestamp()
                    mins_ago = int((time.time() - started_ts) / 60)
                    if mins_ago < 60:
                        ago = f"{mins_ago}min ago"
                    else:
                        ago = f"{mins_ago // 60}h ago"
                except Exception:
                    ago = started

                # Collect tool calls and assistant message
                tools_used = []
                assistant_msg = ""
                for msg in messages:
                    if not isinstance(msg, dict):
                        continue
                    if msg.get("role") == "assistant":
                        for tc in msg.get("tool_calls") or []:
                            name = tc if isinstance(tc, str) else ((tc.get("function") or {}).get("name") or tc.get("name") or "?")
                            tools_used.append(name)
                        content = (msg.get("content") or "").strip()
                        if content and content != "(no content)":
                            assistant_msg = content[:300]

                parts = [f"Run {i+1} ({ago}):"]
                if tools_used:
                    parts.append(f"Tools: {', '.join(tools_used[:5])}")
                if assistant_msg:
                    parts.append(f"Message: \"{assistant_msg[:200]}\"")
                if not tools_used and not assistant_msg:
                    parts.append("No action taken.")

                summaries.append(" ".join(parts))
            except Exception:
                continue

        if not summaries:
            return ""
        result = "**Recent thinking activity:**\n" + "\n".join(summaries)
        return result[:max_chars] if len(result) > max_chars else result
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


def _history_has_thinking_done(history: List[Dict[str, Any]]) -> bool:
    """True if any assistant message in history includes a tool_call to thinking_done."""
    for msg in history or []:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            name = (tc.get("function") or {}).get("name") or tc.get("name") or ""
            if name == "thinking_done":
                return True
    return False


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
    Run one thinking pass for the user. Multiple agent turns until thinking_done is called
    or max_turns is reached. When the model calls thinking_done (or limit hit), the run
    ends and the lock is released.
    """
    from vaf.core.last_interaction import get_last_interaction
    from vaf.core.config import Config, get_local_admin_scope_id, get_local_admin_username

    scope_key = _key(user_scope_id)
    max_duration_minutes = int(Config.get("thinking_max_duration_minutes", 30) or 30)
    # So Agent._load_tools() sees thinking mode and registers thinking_done tool
    os.environ["VAF_THINKING_MODE"] = "1"
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

        # Load the user's main chat session so the thinking agent sees the full conversation history.
        # This gives the agent the same context as when the user chats normally (Telegram, WhatsApp, Web).
        _loaded_session = False
        try:
            from vaf.core.messaging_connections import (
                get_messaging_connections,
                get_telegram_chat_id,
                get_whatsapp_chat_jid,
            )
            uname = getattr(agent, "_current_username", None) or get_local_admin_username()
            conn = get_messaging_connections(username=uname, user_scope_id=user_scope_id)
            main_messenger = (conn.get("main_messenger") or "").strip().lower()

            chat_session_id = None
            if main_messenger == "telegram":
                tg_id = get_telegram_chat_id(user_scope_id, uname)
                if tg_id:
                    chat_session_id = f"telegram_{tg_id}"
            elif main_messenger == "whatsapp":
                jid = get_whatsapp_chat_jid(user_scope_id, uname)
                if jid:
                    chat_session_id = f"whatsapp_{jid}"
            # Fallback: local admin uses web-default
            if not chat_session_id:
                is_local_admin = (
                    user_scope_id is None
                    or str(user_scope_id).strip() == str(get_local_admin_scope_id()).strip()
                )
                if is_local_admin:
                    chat_session_id = "web-default"

            if chat_session_id:
                try:
                    agent.load_session_context(chat_session_id)
                    _loaded_session = True
                    logger.info("Thinking agent loaded chat session: %s", chat_session_id)
                except Exception as e:
                    logger.debug("Could not load chat session %s for thinking: %s", chat_session_id, e)
        except Exception as e:
            logger.debug("Could not resolve chat session for thinking: %s", e)

        # Append thinking mode notice and last run summary (context so we don't repeat or re-ask)
        if agent.history and agent.history[0].get("role") == "system":
            notice = (
                "\n\n## THINKING MODE\n"
                "You are the **main agent** in a background pass while the user is idle. "
                "Act: create automations, process todos. When you send a message, write like a normal human – never say you're in thinking mode or running in the background. At most one message per run. If you ask something, the system will wait for their reply (nudge after 3 min, skip after 10 min). "
                "You may use **multiple turns** in this pass. When you have finished all you can do for the user, call the **thinking_done** tool to end this pass."
            )
            last_summary = _get_last_thinking_summary(user_scope_id)
            if last_summary:
                notice += (
                    "\n\n" + last_summary
                    + "\n(For context only – do not repeat these actions or ask the same questions again.)"
                )
            last_reply = get_and_clear_last_reply(user_scope_id)
            if last_reply:
                notice += "\n\n**User reply to your last question:** " + last_reply
            declined_prompt = _get_declined_questions_prompt(user_scope_id)
            if declined_prompt:
                notice += "\n\n" + declined_prompt
            agent.history[0]["content"] = (agent.history[0]["content"] or "") + notice

        logger.info("Thinking started for user %s", scope_key[:8] if scope_key != "default" else "default")

        try:
            max_turns = int(Config.get("thinking_max_turns", 10) or 10)
            max_turns = max(1, min(max_turns, 30))
            # RAG context for first turn only
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

            for turn in range(max_turns):
                prompt = THINKING_PROMPT if turn == 0 else (
                    "Continue. When you have finished all you can do for the user, call thinking_done."
                )
                mem_ctx = (memory_context or None) if turn == 0 else None
                agent.chat_step(prompt, stream_callback=None, memory_context=mem_ctx)
                if _history_has_thinking_done(getattr(agent, "history", [])):
                    break

            # Persist run: JSON run log (for internal summary) + vaf_denk.log (for debugging)
            # NOT saved to WebUI sessions — thinking output is debug-only, visible in logs/vaf_denk.log
            try:
                started_iso, ended_iso, log_messages = _save_run_log(
                    user_scope_id, run_id, started_at_ts, getattr(agent, "history", [])
                )
                # Write human-readable log to logs/vaf_denk.log
                try:
                    from vaf.core.log_helper import log_thinking_run
                    duration = time.time() - started_at_ts
                    log_thinking_run(
                        run_id=run_id,
                        scope_key=scope_key,
                        started_at=started_iso,
                        ended_at=ended_iso,
                        duration_seconds=round(duration, 1),
                        messages=log_messages,
                    )
                except Exception as log_file_err:
                    logger.warning("Could not write vaf_denk.log: %s", log_file_err)
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
                            # Extract the actual message text from tool arguments
                            question_text = ""
                            try:
                                args_raw = (tc.get("function") or {}).get("arguments") or ""
                                if isinstance(args_raw, str):
                                    args_parsed = json.loads(args_raw)
                                elif isinstance(args_raw, dict):
                                    args_parsed = args_raw
                                else:
                                    args_parsed = {}
                                # Try common parameter names for the message text
                                question_text = (
                                    args_parsed.get("text")
                                    or args_parsed.get("message")
                                    or args_parsed.get("content")
                                    or ""
                                )
                            except (json.JSONDecodeError, TypeError, AttributeError):
                                pass
                            set_waiting_for_reply(user_scope_id, uname, display_name=display_name, question_text=question_text)
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
        os.environ.pop("VAF_THINKING_MODE", None)
        _set_last_run_completed(user_scope_id)
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

    # Cooldown: skip if a thinking run completed recently
    cooldown_min = int(Config.get("thinking_cooldown_minutes", 60) or 60)
    mins_since = _minutes_since_last_run(user_scope_id)
    if mins_since < cooldown_min:
        logger.debug("Thinking skipped for user: cooldown (%d/%d min)", int(mins_since), cooldown_min)
        return False

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
