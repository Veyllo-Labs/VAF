# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
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

from vaf.core.identity_binding import (
    bind_identity,
    reassert_identity,
    resolve_scope_identity,
)
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


def _resolve_username_for_scope(user_scope_id: Any) -> Optional[str]:
    """Resolve the real account username for a user_scope_id from the local_users store.

    SECURITY: a non-admin thinking run must NEVER be given the literal username "admin".
    The username drives username-keyed file stores (UserWorkspace -> ~/.vaf/users/<username>/
    user_identity.json, contacts, mail, calendar). Handing a non-admin the string "admin"
    injects the ADMIN's personal identity/profile (name, preferences, dos/donts, timezone)
    into that user's thinking context (system_prompt.py <user_context> block + RAG query seed)
    — a cross-user data leak. Returns the real username when the scope maps to a local account,
    a synthetic per-scope username (scope_<8hex>) when the scope is unknown, and None on error
    so callers can decide a safe fallback. NEVER returns "admin" for a non-admin scope.
    """
    if not user_scope_id:
        return None
    try:
        import asyncio
        from sqlalchemy import select
        from vaf.auth.models import LocalUser
        from vaf.auth.database import get_auth_db

        async def _lookup() -> Optional[str]:
            async with get_auth_db() as db:
                res = await db.execute(
                    select(LocalUser.username).where(
                        LocalUser.user_scope_id == str(user_scope_id)
                    )
                )
                row = res.first()
                return row[0] if row else None

        try:
            asyncio.get_running_loop()
            running = True
        except RuntimeError:
            running = False

        username: Optional[str] = None
        if running:
            # Already inside an event loop (rare for the thinking thread) — run in a side thread.
            result_box: List[Optional[str]] = [None]

            def _run_in_thread() -> None:
                try:
                    result_box[0] = asyncio.run(_lookup())
                except Exception:
                    result_box[0] = None

            t = threading.Thread(target=_run_in_thread, daemon=True)
            t.start()
            t.join(timeout=10)
            username = result_box[0]
        else:
            username = asyncio.run(_lookup())

        if username and str(username).strip():
            return str(username).strip()
    except Exception as e:
        logger.debug("scope->username lookup failed for %s: %s", user_scope_id, e)

    # Unknown scope: never fall back to "admin". Use a synthetic, non-privileged, scope-derived
    # username so UserWorkspace resolves to an isolated (empty) workspace, not the admin's.
    try:
        return "scope_" + str(user_scope_id).replace("-", "")[:8]
    except Exception:
        return None


def _registered_scope_ids() -> set:
    """Return the set of user_scope_id strings for REGISTERED local accounts (local_users).

    Used to distinguish an infrequent-but-real LAN user from a stale orphan web-session UUID in the
    dead-session cap: a registered account is a real user and must not be dropped just for being idle
    past the cap, whereas an unknown orphan UUID should be. Returns an empty set on any error (the
    caller then falls back to the previous admin-vs-nonadmin behaviour, never crashing the scheduler).
    """
    try:
        import asyncio
        from sqlalchemy import select
        from vaf.auth.models import LocalUser
        from vaf.auth.database import get_auth_db

        async def _lookup() -> set:
            async with get_auth_db() as db:
                res = await db.execute(select(LocalUser.user_scope_id))
                return {str(r[0]).strip() for r in res.all() if r[0]}

        try:
            asyncio.get_running_loop()
            running = True
        except RuntimeError:
            running = False

        if running:
            box: List[set] = [set()]

            def _run_in_thread() -> None:
                try:
                    box[0] = asyncio.run(_lookup())
                except Exception:
                    box[0] = set()

            t = threading.Thread(target=_run_in_thread, daemon=True)
            t.start()
            t.join(timeout=10)
            return box[0]
        return asyncio.run(_lookup())
    except Exception as e:
        logger.debug("registered-scope lookup failed: %s", e)
        return set()


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


# --- Monotonic per-user run counter (drives the "recently asked" window for thinking_requests) ---
RUN_SEQ_FILENAME = "thinking_run_seq.json"


def _run_seq_path() -> Path:
    return Platform.data_dir() / RUN_SEQ_FILENAME


def _load_run_seq() -> Dict[str, int]:
    p = _run_seq_path()
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def next_run_seq(user_scope_id: Optional[str]) -> int:
    """Increment and return this user's monotonic thinking-run sequence number (called at run start)."""
    key = _key(user_scope_id)
    data = _load_run_seq()
    seq = int(data.get(key, 0)) + 1
    data[key] = seq
    try:
        with open(_run_seq_path(), "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass
    return seq


def current_run_seq(user_scope_id: Optional[str]) -> int:
    """Current thinking-run sequence number for this user (0 if none yet)."""
    return int(_load_run_seq().get(_key(user_scope_id), 0))


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


def _is_presence_ack(text: str) -> bool:
    """True only for a BARE 'I'm here' style reply to a presence nudge ('are you there?') — e.g. 'ja',
    'yes', 'da', 'bin wieder da', a wave emoji. Used ONLY (and only after a nudge was actually sent) to
    decide whether to re-ask the real question instead of mis-recording this as its answer. Deliberately
    narrow via exact match: anything with real content ('ja mach das', 'nein!', 'für was?') is NOT a bare
    ack and is handled as a normal answer. This is NOT the accepted/declined classifier (that is the
    next-run LLM step) — just a 'did the user only signal they are back?' gate."""
    t = (text or "").strip().lower().strip(" .!?…")
    if not t or len(t) > 16:
        return False
    acks = {
        "ja", "jo", "joa", "jap", "jup", "jep", "yes", "yep", "yup", "yo", "yeah", "jaa", "jaaa",
        "hier", "da", "bin da", "bin wieder da", "wieder da", "bin zurück", "zurück",
        "back", "i'm back", "im back", "here", "present", "anwesend", "👋", "👍", "🙋",
    }
    return t in acks


def set_waiting_for_reply(
    user_scope_id: Optional[str],
    username: str,
    display_name: str = "",
    question_text: str = "",
    request_id: Optional[str] = None,
    session_id: Optional[str] = None,
    channel: str = "web",
    escalated_to_web: bool = False,
) -> None:
    """Record that we sent a question to the user; we will wait for reply, then nudge after
    thinking_wait_nudge_minutes and stop chasing after thinking_wait_skip_minutes.
    request_id links to the thinking_requests entry so the main agent can pick up the proposal and update its status.
    session_id is the web session the question was delivered to (the anchor/web fallback), so the nudge/escalation
    lands in the SAME chat instead of re-picking the 'latest' session.
    channel is where the question was actually delivered ("telegram"/"whatsapp"/"discord"/"web"); when it is a
    messenger and the user never answers, _process_waiting_reply escalates ONCE to the Web UI (escalated_to_web).
    Setting a new question always re-opens the chase (chase_ended_at_ts back to None): the newest question is the
    one being waited on, and it replaces whatever was in this slot."""
    key = _key(user_scope_id)
    data = _load_waiting()
    data[key] = {
        "question_sent_at_ts": time.time(),
        "nudge_sent_at_ts": None,
        "chase_ended_at_ts": None,
        "username": (username or "").strip() or "admin",
        "display_name": (display_name or username or "admin").strip() or "admin",
        "question_text": (question_text or "")[:500],
        "request_id": (request_id or "").strip() or None,
        "session_id": (session_id or "").strip() or None,
        "channel": (channel or "web").strip().lower() or "web",
        "escalated_to_web": bool(escalated_to_web),
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
    """User replied or we stopped chasing (thinking_wait_skip_minutes); clear waiting state. If user_reply_text is given, save it for the next thinking run and for the thinking-session UI."""
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
        # NOTE: the decline decision (and the declined-questions dedup log) is no longer made here by a
        # keyword guess. The reply is captured onto the tracked request (status 'replied'); the NEXT
        # thinking run classifies the outcome from the full triple and writes the declined-log on DECLINED
        # (see _classify_replied_requests).
    data = _load_waiting()
    if key in data:
        del data[key]
        _save_waiting(data)


def _end_chase_for_fyi(user_scope_id: Optional[str]) -> None:
    """Record an FYI the way a given-up question is recorded: kept, but never chased.

    An FYI (a relevance notice) is not awaiting a decision, so it must not arm the nudge
    or be re-asked. The first version achieved that by writing NO waiting record at all - and that
    re-created, one step earlier in the lifecycle, exactly the defect `end_reply_chase` below was
    built to fix: with no record, a user who DOES reply reaches a main agent that has no idea what
    they are replying to.

    Live 2026-08-30, and it reads like the incident in that docstring. The run sent a researched
    notice about Anthropic rate limits; the user answered "Okay das waren jetzt viele Infos auf
    einmal :D"; the main agent, with nothing to connect that to, called its OWN notice "nur interne
    System-Infos ... es gibt nichts zu tun" and disowned it.

    The two things are separable and this module already separates them: the record carries what was
    sent, `chase_ended_at_ts` says nobody is waiting on an answer. So write the record, then end the
    chase immediately."""
    try:
        end_reply_chase(user_scope_id)
    except Exception as e:
        logger.debug("Thinking: could not end the chase for an FYI: %s", e)


def end_reply_chase(user_scope_id: Optional[str]) -> None:
    """Stop CHASING an unanswered question, but keep the record so a late reply is still understood.

    Two different things were one thing here, and conflating them is what made the agent blind:

    - chasing (one nudge, one escalation, then stop) is about not pestering the user;
    - remembering WHAT was asked is about understanding their answer whenever it comes.

    Deleting the record at the give-up ended both at once, so a reply that arrived half an
    hour later reached a main agent with no idea a question was ever asked - it answered as if nothing
    had been raised (live incident: question escalated to the web chat at 11:49:50, given up at
    11:59:51, the user answered at 12:31 and got a blank "nothing much going on"). The record now
    outlives the chase and is bounded by `thinking_reply_wait_ttl_hours` (the same TTL that already
    guards a latch left behind by a crashed or disabled thinking mode) instead of by ten minutes.

    Idempotent; a missing entry is a no-op.
    """
    key = _key(user_scope_id)
    data = _load_waiting()
    entry = data.get(key)
    if entry is None or entry.get("chase_ended_at_ts"):
        return
    entry["chase_ended_at_ts"] = time.time()
    _save_waiting(data)


def chase_is_active(entry: Optional[Dict[str, Any]]) -> bool:
    """Is this waiting record still being chased (nudges/escalation), rather than only kept for a late
    reply? Callers that mean "the agent is blocked on the user" ask this; callers that mean "what did we
    ask them" use the record itself."""
    return bool(entry) and not entry.get("chase_ended_at_ts")


def get_waiting_for_reply(user_scope_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Return waiting state for this user or None.

    A record whose chase has ended (`chase_ended_at_ts`) is still returned: it is what lets the main
    agent understand a late reply. Use `chase_is_active()` to ask the other question.

    TTL safety net: the normal lifecycle skips an unanswered question after
    thinking_wait_skip_minutes (_process_waiting_reply) - but only when a thinking run
    actually fires. If thinking mode is disabled, crashed, or the app was
    restarted, a stale wait could otherwise latch onto the user's NEXT
    message hours or days later and reframe a fresh request as "the reply to
    your old question". A wait older than thinking_reply_wait_ttl_hours
    (default 12h, 0 disables) is treated as expired: cleared and not
    returned. Fail-open on any error (the entry is returned unexpired)."""
    key = _key(user_scope_id)
    data = _load_waiting()
    entry = data.get(key)
    if not entry:
        return None
    try:
        from vaf.core.config import Config
        ttl_h = float(Config.get("thinking_reply_wait_ttl_hours", 12) or 0)
        sent_ts = float(entry.get("question_sent_at_ts") or 0)
        if ttl_h > 0 and sent_ts > 0 and (time.time() - sent_ts) > ttl_h * 3600:
            del data[key]
            _save_waiting(data)
            return None
    except Exception:
        pass
    return entry


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


def _send_nudge(user_scope_id: Optional[str], username: str, display_name: str, session_id: Optional[str] = None,
                channel: Optional[str] = None) -> bool:
    """Send a short nudge via main_messenger (e.g. 'Hey Alice, bist du da?'). Returns True if sent. The web
    fallback delivers to `session_id` (the anchor session the question was asked in) when given and still
    present, so the nudge lands in the SAME chat as the question — not whatever chat is currently 'latest'.
    `channel` is where the question was delivered: when it is "web" (incl. after a messenger escalation) the
    messenger send is skipped so the nudge lands in the web chat the user is actually looking at."""
    try:
        name = (display_name or username or "").strip() or "admin"
        # Varied, multilingual nudge from the VAF vocabulary book (rotates per user, in the user's
        # preferred_language). Falls back to a plain line if the vocab data is unavailable.
        try:
            from vaf.core import vocab
            nudge = vocab.pick(
                "nudge",
                vocab.resolve_user_language(user_scope_id, username),
                scope=user_scope_id,
                name=name,
            ) or f"Hey {name}, are you there?"
        except Exception:
            nudge = f"Hey {name}, are you there?"
        # Primary: the user's configured main messenger (Telegram/WhatsApp/Discord) — unless the question
        # is anchored to the web chat (channel="web", e.g. after an escalation), then go straight to web.
        if (channel or "").strip().lower() != "web":
            from vaf.core.messaging_connections import send_to_main_messenger
            # Recorded in the channel session like the question it chases: when
            # the user answers "ja" to "are you there?", the agent must see that
            # it asked. kind="nudge" is the same tag the Web UI path persists.
            sent, _ch = send_to_main_messenger(user_scope_id, username, nudge, kind="nudge")
            if sent:
                return True
        # Fallback: no messenger configured — push to the ANCHOR Web UI session (the chat the question
        # was asked in), falling back to the latest web session only if the anchor is gone.
        try:
            from vaf.core.web_interface import get_web_interface
            from vaf.core.session import SessionManager
            wi = get_web_interface()
            if not wi:
                return False
            sm = SessionManager()
            # Append + persist (not emit_agent_message, which overwrites the last assistant bubble
            # and is lost on refresh). kind drives the away-scene avatar animation. The anchor
            # chat first; when it is gone, the latest web chat.
            sid = (session_id or "").strip() or None
            if sid and sm.append_background_message(sid, nudge, kind="nudge") is None:
                sid = None
            if not sid:
                sid = _latest_web_session_id(user_scope_id)
                # ASKED THE SAME QUESTION AS THREE LINES ABOVE, and it was not asked
                # here: a chat can be deleted between being named as the newest one and
                # being written to, and a save can fail. Without the check the emit went
                # out anyway and the run logged a delivery that never landed.
                if sid and sm.append_background_message(sid, nudge, kind="nudge") is None:
                    sid = None
            if sid:
                wi.emit_agent_message_append(content=nudge, session_id=sid, role="assistant", kind="nudge")
                wi.emit_session_unread(sid)
                logger.info("Thinking nudge sent via Web UI session %s", sid)
                return True
        except Exception as _we:
            logger.debug("Thinking nudge Web UI fallback failed: %s", _we)
        return False
    except Exception as e:
        logger.warning("Thinking nudge send failed: %s", e)
        return False


def _escalation_prefix(lang: str, channel_label: str) -> str:
    """Note prepended to the one-time Web-UI re-ask of an unanswered messenger question."""
    lg = (lang or "en")[:2].lower()
    if lg == "de":
        return f"_(Ich hatte dich dazu bereits auf {channel_label} gefragt, aber noch keine Antwort erhalten:)_"
    return f"_(I already asked you this on {channel_label} but haven't heard back yet:)_"


def _escalate_question_to_web(user_scope_id: Optional[str], w: Dict[str, Any], channel: str) -> Optional[str]:
    """One-time Web-UI re-ask of a messenger question the user never answered, with a note that it was
    already asked on that channel. Returns the web session id used, or None if no web chat was reachable."""
    try:
        question = (w.get("question_text") or "").strip()
        if not question:
            return None
        try:
            from vaf.core import vocab
            lang = vocab.resolve_user_language(user_scope_id, w.get("username"))
        except Exception:
            lang = "en"
        ch_label = {"telegram": "Telegram", "whatsapp": "WhatsApp", "discord": "Discord"}.get(channel, channel)
        text = f"{_escalation_prefix(lang, ch_label)}\n\n{question}"
        anchor = (w.get("session_id") or "").strip() or _latest_web_session_id(user_scope_id)
        return emit_message_to_web_ui(user_scope_id, text, session_id=anchor)
    except Exception as e:
        logger.debug("escalate_question_to_web failed: %s", e)
        return None


def _process_waiting_reply(user_scope_id: Optional[str]) -> str:
    """
    If user is in 'waiting for reply' state: nudge after thinking_wait_nudge_minutes,
    stop chasing after thinking_wait_skip_minutes.
    Returns: 'allow_run' (nothing to chase any more), 'skip' (still waiting or nudge sent).

    "Stop chasing" is not "forget": the question record stays readable for the main agent until the
    TTL, so a reply that arrives later is still understood (see end_reply_chase).
    """
    from vaf.core.config import Config
    w = get_waiting_for_reply(user_scope_id)
    if not chase_is_active(w):
        # No record, or one we already gave up chasing. Runs are free to proceed; the record is kept
        # for the main agent alone and must not nudge, escalate or block a run a second time.
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
    nudge_min = float(Config.get("thinking_wait_nudge_minutes", 30) or 30)
    skip_min = float(Config.get("thinking_wait_skip_minutes", 40) or 40)
    # If elapsed_min is very small (user just active), don't even think about nudging
    if elapsed_min < nudge_min:
        return "skip"
    
    # 🛡️ RECENT ACTIVITY PROTECTION: Don't nudge if user was active on ANY channel in last N mins
    try:
        from vaf.core.last_interaction import get_last_interaction
        li = get_last_interaction(user_scope_id)
        if li and li.get("ts"):
            nudge_activity_min = float(Config.get("thinking_nudge_activity_minutes", 5) or 5)
            if (time.time() - li["ts"]) < (nudge_activity_min * 60):
                return "skip"
    except Exception:
        pass

    if elapsed_min >= skip_min:
        # Unanswered. If the question went to a messenger and we haven't escalated yet, re-ask ONCE in
        # the Web UI (with a note that we already asked on that channel), then give the web its own
        # nudge/skip window. Otherwise (web, or already escalated) give up and clear.
        ch = (w.get("channel") or "web").strip().lower()
        if ch in ("telegram", "whatsapp", "discord") and not w.get("escalated_to_web"):
            esc_sid = _escalate_question_to_web(user_scope_id, w, ch)
            if esc_sid:
                data = _load_waiting()
                key = _key(user_scope_id)
                if key in data:
                    data[key]["channel"] = "web"
                    data[key]["escalated_to_web"] = True
                    data[key]["session_id"] = esc_sid
                    data[key]["question_sent_at_ts"] = now  # fresh web window
                    data[key]["nudge_sent_at_ts"] = None
                    _save_waiting(data)
                logger.info("Thinking: %s question unanswered - escalated once to Web UI", ch)
                return "skip"
            # No reachable web chat to escalate to - fall through and stop chasing.
        # Stop chasing, KEEP the question: the user may still answer, and the main agent needs to
        # know what they are answering when they do.
        end_reply_chase(user_scope_id)
        return "allow_run"
    if nudge_ts is None:
        if _send_nudge(
            user_scope_id,
            w.get("username") or "admin",
            w.get("display_name") or w.get("username") or "admin",
            session_id=w.get("session_id"),
            channel=w.get("channel"),
        ):
            data = _load_waiting()
            key = _key(user_scope_id)
            if key in data:
                data[key]["nudge_sent_at_ts"] = now
                _save_waiting(data)
        return "skip"
    return "skip"


def _get_known_scope_ids() -> set:
    """
    Return the set of all user_scope_id values that are actually configured in VAF
    (Telegram whitelist, WhatsApp whitelist, Discord contacts, etc.).
    The local admin scope is represented as None in this set.
    Used to filter out stale/legacy scope_id entries in last_interaction.json.
    """
    from vaf.core.config import Config, get_local_admin_scope_id
    local_admin = str(get_local_admin_scope_id()).strip()
    known: set = {None}  # None always represents the local admin

    try:
        # Telegram whitelist
        tg_cfg = Config.get("telegram_config") or {}
        for entry in (tg_cfg.get("whitelist") or []):
            sid = str(entry.get("user_scope_id") or "").strip()
            if not sid:
                continue
            if sid == local_admin or sid == "default":
                known.add(None)
            else:
                known.add(sid)
    except Exception:
        pass

    try:
        # WhatsApp contacts
        wa_cfg = Config.get("whatsapp_config") or {}
        for entry in (wa_cfg.get("contacts") or []):
            sid = str(entry.get("user_scope_id") or "").strip()
            if not sid:
                continue
            if sid == local_admin or sid == "default":
                known.add(None)
            else:
                known.add(sid)
    except Exception:
        pass

    try:
        # Discord connections (if any user-scoped entries exist)
        disc_cfg = Config.get("discord_config") or {}
        for entry in (disc_cfg.get("users") or []):
            sid = str(entry.get("user_scope_id") or "").strip()
            if not sid:
                continue
            if sid == local_admin or sid == "default":
                known.add(None)
            else:
                known.add(sid)
    except Exception:
        pass

    return known


def get_idle_user_scope_ids(idle_minutes: float) -> List[Optional[str]]:
    """
    Return list of user_scope_id that have been idle for at least idle_minutes.
    Reads last_interaction.json (same store as last_interaction module).
    Normalizes so that "default" and local_admin_scope_id count as one user (None).

    IMPORTANT: The same logical user may appear under MULTIPLE keys in last_interaction.json
    (e.g. "default", "00000000-...", and their real JWT UUID). We MUST map all aliases
    of a user to a single logical ID and take the NEWEST timestamp before deciding idle status.
    """
    from vaf.core.config import get_local_admin_scope_id, Config
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
        # Upper bound on idle age: a scope silent for longer than this is treated as dead, not
        # "idle". Without it, stale/orphan web-session scope IDs (left in last_interaction.json
        # long after the session ended) are each seen as a distinct idle user and generate a
        # phantom thinking run every cooldown window, forever. 0 disables the cap.
        max_idle_age_hours = float(Config.get("thinking_max_idle_age_hours", 168) or 0)
        max_idle_age_sec = max_idle_age_hours * 3600 if max_idle_age_hours > 0 else None
        local_admin_scope = str(get_local_admin_scope_id()).strip()
        # Registered accounts (local_users) are REAL users, not orphan web-session UUIDs. The
        # dead-session cap below must only drop truly unknown orphans; a registered but infrequent
        # LAN user (e.g. checks in weekly) must keep getting proactive runs, the same as the admin.
        registered_scopes = _registered_scope_ids() if max_idle_age_sec is not None else set()

        # Step 1: Map all known scope IDs to logical users.
        # Logical ID -> newest TS seen. (None = local admin)
        latest_ts: Dict[Optional[str], float] = {}
        # Logical ID -> source of the newest interaction
        latest_source: Dict[Optional[str], str] = {}

        # Load known scope mappings from configuration to group aliases
        alias_map: Dict[str, Optional[str]] = {"default": None, local_admin_scope: None}
        try:
            # Telegram
            tg_cfg = Config.get("telegram_config") or {}
            for entry in (tg_cfg.get("whitelist") or []):
                sid = str(entry.get("user_scope_id") or "").strip()
                if sid:
                    alias_map[sid] = None if (sid == "default" or sid == local_admin_scope) else sid
            
            # WhatsApp
            wa_cfg = Config.get("whatsapp_config") or {}
            for entry in (wa_cfg.get("whitelist") or []):
                sid = str(entry.get("user_scope_id") or "").strip()
                if sid:
                    alias_map[sid] = None if (sid == "default" or sid == local_admin_scope) else sid
            
            # Discord
            disc_cfg = Config.get("discord_config") or {}
            for entry in (disc_cfg.get("users") or []):
                sid = str(entry.get("user_scope_id") or "").strip()
                if sid:
                    alias_map[sid] = None if (sid == "default" or sid == local_admin_scope) else sid
        except Exception: pass

        for key in data:
            if not isinstance(key, str): continue
            entry = data.get(key)
            if not isinstance(entry, dict): continue
            ts = entry.get("ts")
            if ts is None: continue
            try:
                ts_float = float(ts)
            except (TypeError, ValueError): continue

            # Map alias to logical user
            logical_id = alias_map.get(key, key)
            if (key == "default" or key == local_admin_scope or logical_id == local_admin_scope):
                logical_id = None

            if logical_id not in latest_ts or ts_float > latest_ts[logical_id]:
                latest_ts[logical_id] = ts_float
                latest_source[logical_id] = entry.get("source", "web")

        # Step 2: Only include logical users who are truly idle across all aliases
        result: List[Optional[str]] = []
        for logical_id, ts_float in latest_ts.items():
            if ts_float > threshold:
                continue

            # Apply 2-minute grace period for ANY activity to avoid race conditions
            # This ensures that if the user just messaged via Telegram/WhatsApp,
            # we don't start thinking immediately even if the idle threshold was technically met.
            if (now - ts_float) < 120:
                continue

            # Dead-session cap: a scope silent past the max idle age that is NOT a registered account
            # is an orphan (e.g. an old web-session UUID), not a real idle user -> never run for it.
            # The local admin (logical_id None) is exempt so a genuinely long-away admin still works,
            # AND any registered local_users account is exempt so a real but infrequent LAN user is
            # not mistaken for a stale orphan (fairness). Only truly UNKNOWN idle UUIDs are dropped.
            if (
                max_idle_age_sec is not None
                and logical_id is not None
                and str(logical_id).strip() not in registered_scopes
                and (now - ts_float) > max_idle_age_sec
            ):
                continue

            result.append(logical_id)
        return result
    except (json.JSONDecodeError, OSError):
        return []


def resolve_thinking_provider() -> str:
    """The provider a thinking run will actually use.

    `thinking_provider` wins when set; `inherit` (the default) means the run's own Agent reads
    the main `provider` from config. Mirrors how the run itself decides - see the
    VAF_PROVIDER assignment in run_thinking_cycle - and exists so the unload watchdog cannot
    answer that question differently than the run does.
    """
    from vaf.core.config import Config
    configured = (Config.get("thinking_provider") or "inherit").strip().lower()
    if configured and configured != "inherit":
        return configured
    return (Config.get("provider", "local") or "local").strip().lower()


def should_defer_model_unload() -> bool:
    """True if the LOCAL model should stay loaded for the background thinking run — a run is currently
    active, or one is eligible to start right now (a user idle past the threshold AND cooldown elapsed).
    The DESKTOP model-unload watchdog (tray.py) calls this so it never pulls the model out from under
    thinking: think first, then unload. (Server/headless never runs that watchdog.) Returns False on any
    error or when thinking is disabled.

    It also returns False whenever the run would not use the local model at all. That case is not
    hypothetical: with a cloud provider the run inherits it and never touches the GGUF, yet the
    deferral kept ~3.4 GB of VRAM pinned - and unlike an active run, an ELIGIBLE one has no upper
    bound. Eligibility stays true for as long as somebody is idle, so "think first, then unload"
    quietly became "never unload" (measured: a scope eligible for 146 minutes past its cooldown
    without a run happening, on a machine whose local model was no longer being used by anything)."""
    try:
        from vaf.core.config import Config
        if not Config.get("thinking_enabled", True):
            return False
        # 0) The run will not use the local model -> nothing to defer for.
        if resolve_thinking_provider() != "local":
            return False
        # 1) A run is executing right now (lock held for the local user).
        if is_locked(None):
            return True
        # 2) A run is eligible to start now: some user idle past the threshold AND cooldown elapsed.
        idle_minutes = float(Config.get("thinking_idle_minutes", 10) or 10)
        cooldown = float(Config.get("thinking_cooldown_minutes", 110) or 110)
        for scope in get_idle_user_scope_ids(idle_minutes):
            if is_locked(scope) or _minutes_since_last_run(scope) >= cooldown:
                return True
        return False
    except Exception:
        return False


def should_skip_for_automation(user_scope_id: Optional[str], buffer_minutes: int) -> bool:
    """True if an automation runs within buffer_minutes for this user (skip thinking start)."""
    from vaf.core.automation import get_next_automation_run_utc
    next_run = get_next_automation_run_utc(user_scope_id)
    if next_run is None:
        return False
    delta = (next_run - datetime.now()).total_seconds()
    return 0 <= delta < buffer_minutes * 60


def is_in_quiet_hours(user_scope_id: Any = None) -> bool:
    """
    True if quiet hours are enabled and the user's CURRENT LOCAL time is inside the window.
    Used to avoid starting thinking mode during the user's sleep (e.g. 23:00–07:00).

    The window is evaluated in the USER's timezone (user_identity.timezone — the single source
    of truth), falling back to server-local when unset. Per-user quiet_hours_enabled/start/end
    in user_identity override the global thinking_quiet_hours_* config; None = inherit global.
    Overnight spans (start > end) are supported. Called with no scope -> global/server-local
    behavior, byte-identical to before.
    """
    from vaf.core.config import Config
    from vaf.core.user_time import user_now

    username = _resolve_username_for_scope(user_scope_id) if user_scope_id is not None else None
    ui = {}
    if username:
        try:
            from vaf.auth.user_workspace import get_user_workspace
            ui = get_user_workspace(username).get_user_identity() or {}
        except Exception:
            ui = {}

    # Per-user override falls back to the global config value.
    enabled = ui.get("quiet_hours_enabled")
    if enabled is None:
        enabled = Config.get("thinking_quiet_hours_enabled", False)
    if not enabled:
        return False
    start_str = (ui.get("quiet_hours_start") or Config.get("thinking_quiet_hours_start") or "23:00").strip()
    end_str = (ui.get("quiet_hours_end") or Config.get("thinking_quiet_hours_end") or "07:00").strip()
    try:
        start_t = datetime.strptime(start_str, "%H:%M").time()
        end_t = datetime.strptime(end_str, "%H:%M").time()
    except (ValueError, TypeError):
        return False
    now = user_now(username, ui).time()
    if start_t > end_t:
        return now >= start_t or now < end_t
    return start_t <= now < end_t


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


def _history_delta(agent_history: List[Dict[str, Any]], start_index: int) -> List[Dict[str, Any]]:
    """Return only entries created after start_index (run-local history slice)."""
    if start_index <= 0:
        return list(agent_history or [])
    if not agent_history:
        return []
    return list(agent_history[start_index:])


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


THINKING_PROMPT = """You are running a background check for the user while they are idle. Your job: **understand, act, help** — in that order.

## YOUR IDENTITY
You are the user's personal AI assistant. You know them from your long-term memory and conversation history. You speak their language naturally. You NEVER reveal that you are running in the background or in "thinking mode".

## STRICT RULES
1. **Max 1 message** to the user per run. Zero messages is perfectly fine — often preferred.
2. **Act > Ask.** If you can do something useful, DO IT. Only ask when you genuinely need the user's decision.
3. **Never ask generic questions** like "Should I set up an automation?" or "Can I help you?". If you ask, be specific and concrete about WHAT and WHY.
4. **Never mention** thinking mode, background pass, system internals, tool errors, or your reasoning process.
5. **Never repeat** questions from the declined list or recent thinking activity.
6. Messages must be **natural, short, human** — like a helpful friend texting.
7. **ALWAYS call thinking_done** at the end. No exceptions.
8. **NEVER** include internal reasoning, debugging output, tool results, error messages, or chain-of-thought in message text. To contact the user you MUST call a TOOL: `ask_user` (preferred) or `thinking_done(message=...)`. Writing the question as plain assistant text does NOT reach the user — it is silently dropped. The `message` parameter must contain ONLY the final, polished, user-facing text.

## NOTES & TODOS ARE REAL, ACTIONABLE TASKS — NOT NOISE
Every automation **note** or **todo** in your list was **deliberately saved by the USER**. They are not
there by accident — each one is a task that deserves action. **NEVER** dismiss a note as "just venting",
"a complaint", or "an observation". A note like *"it's hot, I should figure out how to cool down"* is a
**request for help** → either ACT on it (e.g. `web_search` + a concrete suggestion) or ask ONE specific
question via `ask_user` (pass its `source_note_id`). Treat a note that says *"I should X"* as *"help me
with X"*. Only conclude "Nothing actionable" when the notes AND todos lists are genuinely **empty**.

## WORKFLOW

### Step 1: GATHER (this turn)
Call these tools now:
- `list_automation_todos` — open todos?
- `list_automation_notes` — notes to process?
- `list_automations` — what exists? anything obviously missing?
- `memory_search` — actively recall what the user is currently working on / recently cared about, so you can judge what is genuinely helpful right now. (Read-only: never write to memory.)

### Step 2: DECIDE (fast-exit rules)
Apply these rules IN ORDER:

**IF** you notice a new user preference or pattern:
  → Call `save_thinking_suggestion` (category: `user_knowledge`) — DONE.

**IF** there's a specific, recurring interest needing status (e.g. DHL):
  → Call `web_search` (max 1), save as `thinking_note_add` — DONE.

**IF** the notes AND todos lists are genuinely EMPTY and automations look fine:
  → Call `thinking_done` with summary "Nothing actionable." — DONE. (If ANY note or todo exists it is actionable by default — do NOT exit here; handle it below.)

**IF** there is ANY open todo (it is a task the user set — do it):
  → Do it now (a check/test: run it and report; otherwise act, or — if it needs the user's decision — ask via `ask_user(..., source_todo_id="<id>")`). Once done, clear it with `delete_automation_todo(todo_id="<id>")`. Then `thinking_done`.

**IF** there is ANY note (the user saved it deliberately → it IS actionable):
  → Either ACT on it (e.g. `web_search` + a concrete suggestion, create an automation, update a todo)
    and THEN clear it (`delete_automation_note(note_id=...)`), OR — if it needs the user's decision —
    ask ONE specific question via `ask_user(..., source_note_id="<id>")`. Then `thinking_done`. NEVER
    skip a note as "not actionable".

**IF** an automation is obviously missing and you're confident about what to create:
  → Create it, call `thinking_done` with summary — DONE.

IMPORTANT — never re-do a handled item: every note/todo carries an `id`. Once you have acted on it,
clear it (`delete_automation_todo(todo_id=...)` / `delete_automation_note(note_id=...)`); if you ask the
user about it, pass its id to ask_user (below) so the system clears it on confirm. A cleared todo /
handled note disappears from your next run.

**IF** you need the user's decision on something concrete and specific:
  → Call `ask_user(message="<one clean, specific question or proposal>", proposed_action="<short note of what you'd do if they agree>", source_note_id="<id if the question is about a note>", source_todo_id="<id if about a todo>")`. Put ONLY the final user-facing text in `message` — no reasoning, no "I should…", no tool talk. This delivers the message to the user's main channel (Telegram/WhatsApp/Discord if configured, otherwise the Web UI), tracks it, and waits for the reply; the MAIN agent carries out `proposed_action` once the user confirms, and the linked note/todo is marked handled so it never comes back. You do NOT pick the channel — `ask_user` routes it.
  → If you reach the end and realise you never called ask_user, deliver the same message via
    `thinking_done(message="<the question>", proposed_action="...", source_note_id="<id>")` — it uses the
    exact same tracked path. Either way the message MUST go through a tool call.
  → NEVER write the question as plain assistant text, NEVER use send_mail, NEVER invent contact addresses.
  → The system handles waiting for the reply. Then call `thinking_done`.

**IF** a tool call fails:
  → Log it silently. Try the next thing. Do NOT send error details to the user.
  → If all tools fail, call `thinking_done` with summary "Tools unavailable, will retry next run."

### Step 3: ACT
Execute exactly ONE concrete action from Step 2. Then call `thinking_done`.

## WHEN TO SEND A MESSAGE (strict criteria)
Only send a message to the user if ALL of these are true:
- You need their decision (not just informing them)
- The question is about something SPECIFIC (not generic)
- You haven't asked this before (check declined questions + recent activity)
- It genuinely helps the user (not just "filling" the thinking run)

Channel rules: contact the user ONLY with the `ask_user` tool — it delivers your `message` to the user's
configured main channel (Telegram/WhatsApp/Discord, or the Web UI if none) and tracks it as a request.
Never write the question as plain assistant text; e-mail is NEVER a channel for a background run.

## INTEL GATHERING (Pre-Computation)
If the conversation history shows a clear, specific, and recurring interest (e.g. a specific DHL package, a stock price, or an upcoming event), you are allowed to:
1. Perform ONE (max 1) light research call using `web_search` to find current status.
2. Save the result as a note using `thinking_note_add` (e.g. "DHL Update: Delivery delayed").
3. DO NOT message the user about this unless it's critical or they asked to be notified. Just have the info ready for when they next ask.

## PROACTIVE PROFILE EVOLUTION (Learning)
If you notice new patterns in user behavior, preferences, or personal facts (e.g. "User always asks for news at 8am", "User is interested in X"):
1. DO NOT update the user identity directly.
2. Instead, call `save_thinking_suggestion` with category `user_knowledge`.
3. Provide a clear suggestion text (e.g. "Update user profile: add preference for news at 8am").
4. The user will review and approve these suggestions later.

When you do send a message:
- Use their language, keep it short (1-2 sentences)
- Frame it as a concrete proposal, e.g. "Hey, I noticed you have X — should I set up Y for that?"
- NEVER: "Can I help you with something?" / "Should I set up an automation?"

## BUDGET
- Maximum 5 turns total. Be efficient.
- Most runs should finish in 2-3 turns (gather → decide → done).
- Use `thinking_note_add` to save important context for the next run.

Call thinking_done with a brief summary when finished."""


_SENT_TOOLS = {"send_telegram", "send_whatsapp", "send_discord", "send_slack", "send_mail", "reply_mail", "forward_mail", "send_to_user"}


def _filter_thinking_send_tools(tools: dict, main_messenger: str) -> list:
    """Remove ALL outbound send tools (this NAME SET, not a prefix match) from a thinking run.

    `ask_user` now delivers to the user's configured main channel (Telegram/WhatsApp/Discord, else the
    Web UI) AND tracks the request, so the agent never needs a raw send tool to reach the user — leaving
    one around only invites an untracked or duplicate send by a weak model. `main_messenger` is kept for
    signature stability (callers still pass it). Returns the removed tool names.
    """
    removed = []
    for tool_name in _SENT_TOOLS:
        if tools.pop(tool_name, None) is not None:
            removed.append(tool_name)
    return removed


def _latest_web_session_id(user_scope_id: Optional[str]) -> Optional[str]:
    """The id of the user's latest non-thinking, non-messenger Web UI session, or None. SessionManager.list
    is sorted newest-first and skips hidden sessions, so [0] is the genuine latest visible web chat."""
    try:
        from vaf.core.session import SessionManager
        sm = SessionManager()
        all_sessions = sm.list(limit=10, user_scope_id=user_scope_id)
        web_sessions = [
            s for s in all_sessions
            if (s.get("metadata") or {}).get("source") not in ("thinking", "telegram", "discord", "whatsapp")
        ]
        return web_sessions[0]["id"] if web_sessions else None
    except Exception:
        return None


def emit_message_to_web_ui(
    user_scope_id: Optional[str], content: str, session_id: Optional[str] = None
) -> Optional[str]:
    """Push a clean, final agent message to a Web UI chat session (used by the `ask_user` tool). When
    `session_id` (the anchor) is given and that session still exists, deliver THERE so a question and its
    later nudge/follow-up stay in the same chat; otherwise fall back to the latest web session. Returns the
    session id ACTUALLY used (so the caller can re-pin), or None. NEVER inspects raw chain-of-thought."""
    content = (content or "").strip()
    if not content:
        return None
    try:
        from vaf.core.web_interface import get_web_interface
        from vaf.core.session import SessionManager
        wi = get_web_interface()
        if not wi:
            return None
        sm = SessionManager()
        # Persist + stream as a new bubble (survives a chat refresh). kind drives the avatar
        # animation. Prefer the anchor session if it still exists; else the latest web session.
        sid = (session_id or "").strip() or None
        if sid and sm.append_background_message(sid, content, kind="thinking") is None:
            sid = None
        if not sid:
            sid = _latest_web_session_id(user_scope_id)
            if not sid:
                return None
            # The same check the anchor session gets above. A question the user never
            # sees is worse here than in the nudge path: the run goes on believing it
            # asked and waits for an answer to something nobody was shown.
            if sm.append_background_message(sid, content, kind="thinking") is None:
                return None
        wi.emit_agent_message_append(content=content, session_id=sid, role="assistant", kind="thinking")
        wi.emit_session_unread(sid)
        logger.info("Thinking Mode: ask_user message emitted to Web UI session %s", sid)
        return sid
    except Exception as _e:
        logger.debug("Thinking Mode: Web UI emit failed: %s", _e)
        return None


def deliver_tracked_message(
    user_scope_id: Optional[str],
    message: str,
    proposed_action: Optional[str] = None,
    source_note_id: Optional[str] = None,
    source_todo_id: Optional[str] = None,
    username: Optional[str] = None,
    details: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Deliver ONE clean, user-facing message from the background run and track it as a request.

    This is the single delivery path shared by `ask_user` (the primary, explicit channel) and the
    `thinking_done(message=...)` fallback (used when a weak model composes the message but forgets to
    call ask_user). It (1) records a tracked request (status 'asked', stamped with the current run_seq +
    the source note/todo so a confirm can clear them), (2) sets waiting_for_reply so the main agent
    picks up the user's reply, and (3) delivers the exact text to the user's configured main messenger
    (Telegram/WhatsApp/Discord via send_to_main_messenger), falling back to a Web UI emit only when no
    messenger is configured or the send fails. The text is ALWAYS an explicit caller argument —
    chain-of-thought is never scraped, so reasoning cannot leak. Returns the request dict with an extra
    `delivered` flag, or None if `message` was empty."""
    from vaf.core.config import get_local_admin_scope_id, get_local_admin_username
    from vaf.core import thinking_requests as treq

    message = (message or "").strip()
    if not message:
        return None
    user_scope_id = user_scope_id or get_local_admin_scope_id()

    # ONE message per run, enforced HERE (not just between turns): the weak local model often calls
    # ask_user several times within a SINGLE chat_step (the duplicate emits the user saw twice). The
    # loop-level run_has_open_request guard only runs BETWEEN turns, so it cannot stop an intra-turn
    # repeat — this is the only place that can. Applies to every message type (a confirm clears the
    # one item this run processed; the next run handles the next).
    if run_has_open_request(user_scope_id):
        logger.info("Thinking: a message was already delivered this run — duplicate suppressed")
        return None

    # MESSAGE GATE: a FREE message (no source note/todo) is governed by the per-run mode. Housekeeping
    # messages (carrying a source_note_id/source_todo_id) are always exempt — their evidence IS the item.
    #   off      -> block (gather/forced-resolution: a free message there is premature/generic, e.g. the
    #               turn-0 "no tasks, I'm ready when you need me" floskel)
    #   grounded -> proactive suggestion: deliver only if details quote real retrieved memory/history
    #   open     -> get-to-know question: allowed (a question states no fact, cannot fabricate)
    if not (source_note_id or "").strip() and not (source_todo_id or "").strip():
        _mode = get_proactive_mode(user_scope_id)
        if _mode == "off":
            logger.info("Thinking: free message blocked (not in a proactive step) — premature/generic")
            return None
        if _mode == "grounded":
            from vaf.core.config import Config
            _pool = get_run_evidence(user_scope_id)
            # Provider-calibrated evidence bar: strict for the weak LOCAL model (fabricates), lenient for a
            # strong HOSTED model (rarely fabricates). Selected automatically by the active thinking provider.
            _main_local = (Config.get("provider") or "local").strip().lower() == "local"
            _t_prov = (Config.get("thinking_provider") or "inherit").strip().lower()
            _think_local = _main_local if _t_prov == "inherit" else (_t_prov == "local")
            _min_chars = int(Config.get("thinking_proactive_evidence_min_chars", 24) or 24)
            if not _think_local:
                _min_chars = int(Config.get("thinking_proactive_evidence_min_chars_api", 12) or 12)
            # Grounded if EITHER the user-facing message OR the details quotes real retrieved memory — the
            # model often puts the verbatim quote in the message it shows the user and leaves details empty.
            if not (_evidence_grounded(details or "", _pool, _min_chars)
                    or _evidence_grounded(message or "", _pool, _min_chars)):
                logger.info("Thinking: proactive suggestion dropped — neither message nor details grounded in retrieved memory")
                return None
        # _mode == "open" -> allowed (get-to-know question)
        # SEMANTIC DEDUP: for either proactive mode, reject a question too close to one asked/declined
        # recently so the model is pushed to a genuinely different topic (breaks the "always work/VAF"
        # loop). Fail-open inside _question_too_similar. Note/todo-sourced asks are exempt
        # (this whole block only runs for FREE messages). A FOLLOW-UP re-ask is ALSO exempt: it intentionally
        # repeats the SAME open question (a pointed yes/no), which the gate would otherwise reject as a
        # near-duplicate of the very request it is following up on.
        #
        # The gate has a BUDGET, spent here and nowhere else: after `thinking_getto_max_attempts`
        # rejections in this run the next question is delivered as it stands. This is the only place
        # the retry loop can be closed, because the retry happens inside ONE chat_step - ask_user's
        # rejection text tells the model to call it again immediately. A bypass one level up counted
        # once per outer turn, so it never fired while a run burned 12 tool turns down here.
        if (_mode in ("open", "grounded")
                and get_dedup_enforce(user_scope_id)
                and not get_followup_context(user_scope_id)
                and not ask_rejects_exhausted(user_scope_id)):
            if _question_too_similar(user_scope_id, message):
                set_reject_reason(user_scope_id, "too_similar")
                bump_ask_rejects(user_scope_id)
                return None

    run_seq = current_run_seq(user_scope_id)
    _fu_id = get_followup_context(user_scope_id)
    req = None
    if _fu_id and not (source_note_id or "").strip() and not (source_todo_id or "").strip():
        # This free message is a FOLLOW-UP on an existing open question — update that request (bump its
        # follow-up counter + refresh recency/text) instead of creating a duplicate entry.
        # details/proposed_action ENRICH: a follow-up that carries substance fills a gap the original
        # left; one that carries none must not blank what the original already knew.
        req = treq.bump_followup(
            user_scope_id, _fu_id, new_question=message, run_seq=run_seq,
            details=(details or "").strip() or None,
            proposed_action=(proposed_action or "").strip() or None,
        )
    if req is None:
        req = treq.add_request(
            user_scope_id,
            question=message,
            run_seq=run_seq,
            proposed_action=(proposed_action or "").strip() or None,
            thinking_run_id=os.environ.get("VAF_THINKING_RUN_ID"),
            source_note_id=(source_note_id or "").strip() or None,
            source_todo_id=(source_todo_id or "").strip() or None,
            details=(details or "").strip() or None,
            kind=get_message_kind(user_scope_id) or None,
        )
    _is_fyi = (req.get("kind") or "") == "relevance"
    uname = (username or "").strip() or get_local_admin_username()
    # Anchor the question to ONE web session: a follow-up reuses the original request's session; a new
    # question resolves the latest web session NOW. The nudge + later follow-up reuse this anchor (via the
    # waiting state / the request) instead of independently re-picking 'latest', so they stay in the same chat.
    _anchor_sid = (req.get("session_id") if req else None) or _latest_web_session_id(user_scope_id)

    # PRIMARY: deliver to the user's configured main messenger (Telegram/WhatsApp/Discord). The
    # question is recorded in that channel's session (kind="thinking", like the Web UI path
    # persists it), so the main agent answering there has asked it in its own transcript; the
    # waiting latch adds the proposal and the findings on top when the user replies. The latch
    # alone was the record once, and it is one scope-keyed slot any turn on the scope can
    # consume - a room wake took it, and the user's real answer on Telegram met an agent with
    # no trace of the question (live 2026-09-02). The web session stays the anchor for the
    # later escalation / web fallback.
    from vaf.core.messaging_connections import send_to_main_messenger
    sent_channel = None
    try:
        _ok, sent_channel = send_to_main_messenger(user_scope_id, uname, message, kind="thinking")
        if not _ok:
            sent_channel = None
    except Exception:
        sent_channel = None

    if sent_channel:
        set_waiting_for_reply(
            user_scope_id, username=uname, display_name=uname,
            question_text=message, request_id=req["id"], session_id=_anchor_sid, channel=sent_channel,
        )
        if _is_fyi:
            _end_chase_for_fyi(user_scope_id)
        # Pin the request to the web anchor so a later escalation / follow-up can reach the Web UI.
        if _anchor_sid and req.get("session_id") != _anchor_sid:
            treq.set_request_session(user_scope_id, req["id"], _anchor_sid)
            req = treq.get_request(user_scope_id, req["id"]) or req
        logger.info("Thinking: question delivered via %s (request %s)", sent_channel, req.get("id"))
        req = dict(req)
        req["delivered"] = True
        return req

    # FALLBACK: no main messenger configured (or the send failed) — deliver to the Web UI as before.
    set_waiting_for_reply(
        user_scope_id, username=uname, display_name=uname,
        question_text=message, request_id=req["id"], session_id=_anchor_sid, channel="web",
    )
    if _is_fyi:
        _end_chase_for_fyi(user_scope_id)
    # Deliver-gate: if the main agent is actively handling a user turn, do NOT push this live into the
    # middle of that turn. The request is already recorded + waiting_for_reply set (and the run loop
    # persists it to the session), so it surfaces on the user's next load. Defer the live emit, never drop.
    if _main_agent_busy(user_scope_id):
        logger.info("Thinking: main agent active — deferring live delivery (request %s recorded, surfaces on next visit)", req.get("id"))
        sid = None
    else:
        sid = emit_message_to_web_ui(user_scope_id, message, session_id=_anchor_sid)
    # Pin the request to the session actually used (or the resolved anchor when deferred), so a later run's
    # follow-up reuses it. If the live emit fell back to a different session (anchor was gone), re-pin the
    # waiting state too so the nudge targets the same chat the user now sees the question in.
    _effective_sid = sid or _anchor_sid
    if _effective_sid and req.get("session_id") != _effective_sid:
        treq.set_request_session(user_scope_id, req["id"], _effective_sid)
        req = treq.get_request(user_scope_id, req["id"]) or req
    if sid and sid != _anchor_sid:
        set_waiting_for_reply(
            user_scope_id, username=uname, display_name=uname,
            question_text=message, request_id=req["id"], session_id=sid, channel="web",
        )
        if _is_fyi:
            _end_chase_for_fyi(user_scope_id)
    req = dict(req)
    req["delivered"] = bool(sid)
    return req


def run_has_open_request(user_scope_id: Optional[str]) -> bool:
    """True if the background run already raised a tracked request THIS run (so a fallback delivery via
    thinking_done must not send a second message). Uses the current run_seq as the run boundary."""
    from vaf.core import thinking_requests as treq
    cur = current_run_seq(user_scope_id)
    return bool(treq.list_requests(user_scope_id, within_runs=1, current_run_seq=cur))


def _main_agent_busy(user_scope_id: Optional[str]) -> bool:
    """True if the MAIN agent is actively handling (or has queued) a turn FOR THIS SAME USER —
    provider-independent. The single headless worker marks a session in-flight for the whole turn
    (TaskQueue.get -> task_done), so the in-flight/queued metadata scope is the universal 'this user's
    turn in progress' signal that the 10-min idle gate misses. The thinking run runs in its own thread
    and never enqueues, so this never self-suppresses.

    FAIRNESS: scoped to THIS user (was previously global is_busy()/queue-size, which let one busy
    LAN user or the admin block every other user's background runs and deliveries). Admin aliases
    (None/'default'/local-admin-UUID) collapse via _key so the admin's own turn still self-gates.
    Used by the start-gate (do not START a run mid-turn) and the deliver-gate (do not PUSH a message
    mid-turn). Fails safe to NOT busy so an error never blocks delivery."""
    try:
        from vaf.core.task_queue import TaskQueue
        _tq = TaskQueue()
        return bool(_tq.is_busy_for_scope(_key(user_scope_id), _key))
    except Exception:
        return False


def _any_agent_busy() -> bool:
    """True if ANY user's turn is in-flight/queued (global). Used ONLY as an extra start-gate when the
    main and thinking providers are both the single local model — concurrent runs would contend on one
    model. NOT used for delivery or on API/server providers (that would starve non-admins)."""
    try:
        from vaf.core.task_queue import TaskQueue
        _tq = TaskQueue()
        return bool(_tq.is_busy() or _tq.get_queue_size() > 0)
    except Exception:
        return False


# --- Proactive evidence pool (Stufe 2) ---------------------------------------------------------------
# The real memory/history retrieved THIS run, so the evidence-gate can verify a PROACTIVE suggestion is
# grounded in it (not fabricated by the weak local model). Per-scope, in-memory, cleared at run end. The
# per-run message MODE governs what a FREE (no source note/todo) message is allowed to do:
#   "off"      -> block free messages (gather/forced-resolution: a free message there is premature/generic)
#   "grounded" -> proactive grounded step: deliver only if details quote real retrieved memory/history
#   "open"     -> get-to-know step: a question states no fact, so it is allowed without evidence
# Housekeeping messages (carrying a source_note_id/source_todo_id) are always exempt.
_RUN_EVIDENCE: Dict[str, str] = {}
_PROACTIVE_MODE: Dict[str, str] = {}
_RUN_EVIDENCE_MAX = 20000  # keep the tail bounded


def set_run_evidence(user_scope_id: Optional[str], text: str) -> None:
    _RUN_EVIDENCE[_key(user_scope_id)] = (text or "")[:_RUN_EVIDENCE_MAX]


def add_run_evidence(user_scope_id: Optional[str], text: str) -> None:
    """Append real retrieved evidence (e.g. a memory_search result) to this run's pool."""
    text = (text or "").strip()
    if not text:
        return
    k = _key(user_scope_id)
    combined = (_RUN_EVIDENCE.get(k, "") + "\n" + text)
    _RUN_EVIDENCE[k] = combined[-_RUN_EVIDENCE_MAX:]


def get_run_evidence(user_scope_id: Optional[str]) -> str:
    return _RUN_EVIDENCE.get(_key(user_scope_id), "")


def clear_run_evidence(user_scope_id: Optional[str]) -> None:
    _RUN_EVIDENCE.pop(_key(user_scope_id), None)
    _PROACTIVE_MODE.pop(_key(user_scope_id), None)
    # Semantic-dedup per-scope flags must also reset each run so stale state never carries over.
    # _ASK_REJECTS especially: a counter that survived a run would sit permanently at or above the
    # budget and silently disable the dedup gate for that scope forever.
    _DEDUP_ENFORCE.pop(_key(user_scope_id), None)
    _REJECT_REASON.pop(_key(user_scope_id), None)
    _ASK_REJECTS.pop(_key(user_scope_id), None)
    _MESSAGE_KIND.pop(_key(user_scope_id), None)


def set_proactive_mode(user_scope_id: Optional[str], mode: str) -> None:
    """mode in {'off','grounded','open'} — governs delivery of a FREE (no source) message this run."""
    _PROACTIVE_MODE[_key(user_scope_id)] = mode if mode in ("off", "grounded", "open") else "off"


def get_proactive_mode(user_scope_id: Optional[str]) -> str:
    return _PROACTIVE_MODE.get(_key(user_scope_id), "off")


# Per-scope flags for the semantic question-dedup (small bool/str ONLY — never vectors).
#   _DEDUP_ENFORCE: whether the dedup gate is active this turn (the loop disables it on the final
#     get-to-know attempt so a run never ends in silence). Default True.
#   _REJECT_REASON: why deliver_tracked_message last returned None this turn, so ask_user.run can give
#     the right guidance ("too_similar" vs the generic gates). Read-once (popped) by the tool.
#   _ASK_REJECTS: how many questions the dedup gate has rejected THIS RUN. The retry loop lives inside
#     one chat_step - ask_user's rejection text tells the model to call it again - so the bound has to
#     live where the repetition happens. It used to live one level up, counted once per OUTER loop turn,
#     and therefore stayed at 1 while a run burned 12 tool turns on rejected questions.
#   _MESSAGE_KIND: what KIND of message this rung sends. "" is a question awaiting a decision;
#     "relevance" is an FYI, which must not be nudged after three minutes and must not be re-asked as
#     an unanswered question - a warning nobody replies to would otherwise be pushed up to eight times.
_DEDUP_ENFORCE: Dict[str, bool] = {}
_REJECT_REASON: Dict[str, str] = {}
_ASK_REJECTS: Dict[str, int] = {}
_MESSAGE_KIND: Dict[str, str] = {}


def set_dedup_enforce(user_scope_id: Optional[str], enforce: bool) -> None:
    _DEDUP_ENFORCE[_key(user_scope_id)] = bool(enforce)


def get_dedup_enforce(user_scope_id: Optional[str]) -> bool:
    return _DEDUP_ENFORCE.get(_key(user_scope_id), True)


def set_reject_reason(user_scope_id: Optional[str], reason: str) -> None:
    _REJECT_REASON[_key(user_scope_id)] = reason


def take_reject_reason(user_scope_id: Optional[str]) -> str:
    """Pop the last delivery-rejection reason for this scope ('' if none)."""
    return _REJECT_REASON.pop(_key(user_scope_id), "")


def set_message_kind(user_scope_id: Optional[str], kind: str) -> None:
    _MESSAGE_KIND[_key(user_scope_id)] = (kind or "").strip()


def get_message_kind(user_scope_id: Optional[str]) -> str:
    return _MESSAGE_KIND.get(_key(user_scope_id), "")


def bump_ask_rejects(user_scope_id: Optional[str]) -> int:
    """Count one dedup rejection for this run and return the new total."""
    k = _key(user_scope_id)
    _ASK_REJECTS[k] = _ASK_REJECTS.get(k, 0) + 1
    return _ASK_REJECTS[k]


def get_ask_rejects(user_scope_id: Optional[str]) -> int:
    return _ASK_REJECTS.get(_key(user_scope_id), 0)


def ask_rejects_exhausted(user_scope_id: Optional[str]) -> bool:
    """Has this run used up its dedup rejections, so the next question must be delivered as-is?

    The bound is per RUN, not per turn: a question always lands within a single step, which is
    the only place the retry loop can actually be closed."""
    from vaf.core.config import Config
    try:
        budget = int(Config.get("thinking_getto_max_attempts", 3) or 3)
    except (TypeError, ValueError):
        budget = 3
    return get_ask_rejects(user_scope_id) >= max(1, budget)


# Per-scope "the free message being delivered this run is a FOLLOW-UP on request <id>" — set in the
# follow-up rung so deliver_tracked_message updates that request (bumps its counter) instead of creating a
# duplicate. Cleared at run setup + end.
_FOLLOWUP_CTX: Dict[str, str] = {}


def set_followup_context(user_scope_id: Optional[str], request_id: Optional[str]) -> None:
    if request_id:
        _FOLLOWUP_CTX[_key(user_scope_id)] = str(request_id)
    else:
        _FOLLOWUP_CTX.pop(_key(user_scope_id), None)


def get_followup_context(user_scope_id: Optional[str]) -> Optional[str]:
    return _FOLLOWUP_CTX.get(_key(user_scope_id))


def clear_followup_context(user_scope_id: Optional[str]) -> None:
    _FOLLOWUP_CTX.pop(_key(user_scope_id), None)


def _normalize_ev(s: str) -> str:
    import re
    # Lowercase, then fold any run of non-alphanumerics (hyphens, punctuation, whitespace) to a single
    # space — so "Three-Second-Loop" matches "Three-Second Loop". Latin letters/umlauts are preserved so
    # German memories still match. Both sides pass through this, so the verbatim-substring check stays
    # symmetric: only separators are tolerated, never paraphrase or invention.
    return re.sub(r"[^0-9a-zÀ-ɏ]+", " ", (s or "").lower()).strip()


def _evidence_grounded(details: str, pool: str, min_chars: int) -> bool:
    """True if `details` quotes a verbatim (whitespace/case-normalized) substring of length >= min_chars
    from `pool` — proof a proactive suggestion is grounded in REAL retrieved memory/history, not invented.
    Short details (< min_chars) must appear in full."""
    d = _normalize_ev(details)
    p = _normalize_ev(pool)
    if not d or not p:
        return False
    n = max(8, int(min_chars or 24))
    if len(d) < n:
        return d in p
    for i in range(0, len(d) - n + 1):
        if d[i:i + n] in p:
            return True
    return False


# --- Semantic question de-duplication (Stufe 3: topic breadth) ------------------------------------
# Text-based "don't repeat" only blocks the same WORDING, so the model kept re-asking the SAME topic
# reworded (always "work/VAF"). These helpers embed the candidate proactive question and reject it when it
# is semantically too close to a recently asked/declined one, forcing a genuinely different area.
# LEAK-SAFE: reuse the SAME embedding singleton the run already uses every run (no new vector lane), bound
# to <= max_compare + 1 embeds/turn (recent ones are LRU cache hits), nothing persisted, fully sync, and
# fail-OPEN on any error so a question is never lost to the gate.

def _cosine(a: List[float], b: List[float]) -> float:
    """Cosine similarity with DEFENSIVE normalization - embed_sync only
    L2-normalizes for the E5 family, so vectors here may arrive unnormalized
    (MiniLM and custom models)."""
    try:
        import numpy as _np
        va = _np.asarray(a, dtype=_np.float32)
        vb = _np.asarray(b, dtype=_np.float32)
        na = float(_np.linalg.norm(va))
        nb = float(_np.linalg.norm(vb))
        if na == 0.0 or nb == 0.0:
            return 0.0
        return float(_np.dot(va, vb) / (na * nb))
    except Exception:
        return 0.0


def _embed_question(text: str) -> List[float]:
    """Embed ONE short question via the shared embedding singleton (leak-safe; monkeypatched in tests)."""
    from vaf.memory.embeddings import get_embedding_service
    return get_embedding_service().embed_sync((text or "").strip(), prefix="query")


def _recent_question_texts(user_scope_id: Optional[str], current_run_seq_val: int) -> List[str]:
    """Recent proactive questions to compare against: asked/declined within the recency window, newest
    first, exact-deduped and capped (small bound keeps the per-turn embed work tiny)."""
    from vaf.core.config import Config
    runs = int(Config.get("thinking_question_similarity_runs", 12) or 12)
    cap = int(Config.get("thinking_question_similarity_max_compare", 12) or 12)
    texts: List[str] = []
    seen: set = set()

    def _add(q: str) -> None:
        q = (q or "").strip()
        if not q:
            return
        key = q.lower()
        if key in seen:
            return
        seen.add(key)
        texts.append(q)

    try:
        from vaf.core import thinking_requests as _treq
        for r in _treq.list_requests(user_scope_id, within_runs=runs, current_run_seq=current_run_seq_val):
            _add(r.get("question") or "")
    except Exception:
        pass
    try:
        for e in _load_declined(user_scope_id):
            _add(e.get("question") or "")
    except Exception:
        pass
    return texts[:cap]


def _pool_cutoff(pool_vecs: List[List[float]], percentile: float, floor: float) -> float:
    """Reject threshold DERIVED from the pool's own nearest-neighbour distribution.

    An absolute cosine cutoff is not portable across embedding models, and on an anisotropic one it
    is not even meaningful: the vectors occupy a narrow cone, so every pair of same-language,
    same-register questions scores high regardless of topic. Measured on this product with
    all-MiniLM-L6-v2 and a real 12-question pool, unrelated candidates scored 0.872-0.912 while the
    pool's own minimum pairwise similarity was 0.800 - i.e. the configured 0.80 sat at the FLOOR of
    what the model produces for any two questions, and nothing could ever pass. Since
    `memory_embedding_model` is configurable, a replacement constant would break the same way on the
    next model swap.

    So: take each pool question's similarity to its NEAREST other pool question (that is exactly the
    quantity the gate measures for the candidate), and cut at a percentile of those. Self-calibrating
    by construction, and a model swap re-calibrates it for free. `floor` keeps a very broad pool from
    dragging the cutoff down to where genuinely different questions would be rejected.
    """
    n = len(pool_vecs)
    if n < 2:
        return floor
    nn = []
    for i in range(n):
        best = 0.0
        for j in range(n):
            if i == j:
                continue
            sim = _cosine(pool_vecs[i], pool_vecs[j])
            if sim > best:
                best = sim
        nn.append(best)
    nn.sort()
    # Nearest-rank percentile: no interpolation, so the result is always an observed value.
    idx = min(len(nn) - 1, max(0, int(round((percentile / 100.0) * len(nn) + 0.5)) - 1))
    return max(floor, nn[idx])


def _question_too_similar(user_scope_id: Optional[str], candidate: str) -> bool:
    """True if `candidate` is semantically too close to a recently asked/declined question. Fail-OPEN: any
    error (dedup off, memory off, no model, embedding failure) returns False so a question is never lost."""
    candidate = (candidate or "").strip()
    if not candidate:
        return False
    try:
        from vaf.core.config import Config
        if not Config.get("thinking_question_dedup_enabled", True):
            return False
        if not Config.get("memory_enabled", True):
            return False
        recent = _recent_question_texts(user_scope_id, current_run_seq(user_scope_id))
        if not recent:
            return False

        pool_vecs = [_embed_question(q) for q in recent]      # embedded ONCE, shared by both steps
        cand_vec = _embed_question(candidate)
        best = 0.0
        for v in pool_vecs:
            sim = _cosine(cand_vec, v)
            if sim > best:
                best = sim

        # The one absolute that IS defensible, and it needs no calibration: a cosine this high means
        # near-identical TEXT in any model, which is the property the narrow-cone effect does not
        # distort. Checked BEFORE the pool-size stand-down, so a verbatim repeat is caught even when
        # there is not yet enough history to derive a cutoff from.
        hard_max = float(Config.get("thinking_question_similarity_max", 0.97) or 0.97)
        if best >= hard_max:
            logger.info(
                "Thinking: proactive question rejected as a near-duplicate (cosine=%.3f >= %.2f, "
                "pool=%d): %r", best, hard_max, len(recent), candidate[:80],
            )
            return True

        # Below this many recent questions there is no distribution to calibrate against, so the
        # derived half of the gate stands down rather than guessing. A fresh user therefore gets only
        # the near-duplicate ceiling for their first few questions; the text-based recent/declined
        # prompts cover that window.
        min_pool = max(2, int(Config.get("thinking_question_similarity_min_pool", 3) or 3))
        if len(recent) < min_pool:
            return False

        floor = float(Config.get("thinking_question_similarity_threshold", 0.80) or 0.80)
        pct = float(Config.get("thinking_question_similarity_percentile", 90) or 90)
        cutoff = _pool_cutoff(pool_vecs, pct, floor)
        if best > cutoff:
            logger.info(
                "Thinking: proactive question rejected as too similar (cosine=%.3f > cutoff=%.3f, "
                "pool=%d): %r", best, cutoff, len(recent), candidate[:80],
            )
            return True
        logger.debug(
            "Thinking: question accepted (cosine=%.3f cutoff=%.3f pool=%d)", best, cutoff, len(recent)
        )
        return False
    except Exception as e:
        logger.debug("Thinking: question-dedup check failed (fail-open): %s", e)
        return False


def proactive_rate_limited(user_scope_id: Optional[str], current_run_seq_val: int, min_runs: int) -> bool:
    """DEPRECATED — no longer called by the run loop. Silence is never the goal: a clear-floor run always
    reaches out; repeats are prevented by the recent/declined dedup prompts, not by suppressing whole runs.
    Kept for tests/back-compat. (True if a source-less proactive request was raised within `min_runs` runs.)"""
    if min_runs <= 0:
        return False
    from vaf.core import thinking_requests as treq
    try:
        recent = treq.list_requests(user_scope_id, within_runs=min_runs, current_run_seq=current_run_seq_val)
    except Exception:
        return False
    for r in recent:
        if not (r.get("source_note_id") or "").strip() and not (r.get("source_todo_id") or "").strip():
            return True
    return False


def deliver_thinking_done_fallback(
    user_scope_id: Optional[str],
    message: Optional[str],
    proposed_action: Optional[str] = None,
    source_note_id: Optional[str] = None,
    source_todo_id: Optional[str] = None,
    username: Optional[str] = None,
    details: Optional[str] = None,
) -> str:
    """The `thinking_done(message=...)` fallback delivery, shared by BOTH the ThinkingDoneTool and the
    agent's thinking_done dispatch (agent.chat_step special-cases thinking_done and returns before running
    the tool, so without this the message would be silently dropped in the real run). Delivers the message
    via the same tracked path as ask_user unless one was already raised this run. Returns a short status
    note to append to the thinking_done summary ('' if nothing was delivered)."""
    message = (message or "").strip()
    if not message:
        return ""
    from vaf.core.config import get_local_admin_scope_id
    scope = user_scope_id or get_local_admin_scope_id()
    if run_has_open_request(scope):
        return " (a question was already delivered this run; the extra message was not re-sent)"
    req = deliver_tracked_message(
        scope, message,
        proposed_action=proposed_action,
        source_note_id=source_note_id,
        source_todo_id=source_todo_id,
        username=username,
        details=details,
    )
    if req and req.get("delivered"):
        return f" (message delivered to the user, tracked as request {req['id']})"
    if req:
        return f" (message recorded as request {req['id']}; it will surface on the user's next visit)"
    # None: a gate rejected it. Unlike ask_user.run (which consumes the reason to craft guidance), this
    # fallback has no model turn to steer — so clear any reject_reason it set, to avoid leaving stale
    # per-scope state that a later ask_user could misread as its own "too_similar" rejection.
    take_reject_reason(scope)
    return " (the fallback message was not grounded/eligible and was not sent)"


# Content-driven prompts for thinking-mode turns 1+ (turn 0 uses THINKING_PROMPT). The phase is driven by
# WORK DONE (is the housekeeping floor clear?), NOT by turn count. There is deliberately no "wrap up now /
# FINAL TURN" termination pressure: the model is allowed to keep working until the housekeeping ledger is
# resolved (the completion gate enforces this), then it climbs one rung to proactive upkeep. The hard
# turn cap (SAFETY 1) remains only as a backstop.

# Floor not yet clear: open notes/todos remain. Decisive — pick ONE item and resolve it with ONE tool
# call in the next response. Default to ACT (a note is a request for help). No more analysis / no prose.
_PROMPT_HOUSEKEEPING = (
    "You have open notes/todos the user saved. Pick the FIRST one and resolve it in your NEXT response "
    "with exactly ONE tool call — do not write analysis, do not search again:\n"
    "- DEFAULT = ACT: a note like 'it's too hot, how do I cool down' is a request for help. Turn what you "
    "already found into ONE concrete suggestion and deliver it with "
    "`ask_user(message=\"<the suggestion as a short, specific proposal>\", source_note_id=\"<id>\")` — that "
    "single call delivers it AND clears the note.\n"
    "- Only if you truly cannot help without more info, ask ONE specific question the same way.\n"
    "- If you already acted on an item, clear it now: `delete_automation_note(note_id=\"<id>\")` / "
    "`delete_automation_todo(todo_id=\"<id>\")`.\n"
    "Respond with the tool call ONLY. Once every item is resolved, call thinking_done."
)

# Force-decision: injected after several turns of gathering/analysing without a decisive action. The model
# must emit exactly one progress tool now and stop searching.
_PROMPT_FORCE_DECISION = (
    "STOP. You have searched and analysed across several turns without resolving anything. In your NEXT "
    "response output EXACTLY ONE tool call and NO prose:\n"
    "- If a note/todo is still open: `ask_user(message=\"<one concrete suggestion or question>\", "
    "source_note_id=\"<id>\")` (this delivers AND clears it), or `delete_automation_note(note_id=\"<id>\")` "
    "if you already acted.\n"
    "- Otherwise: `thinking_done(summary=\"...\")`.\n"
    "Do NOT call web_search, memory_search or list_* again. Decide now."
)

# The proactive ladder, named. `_proactive_step` walks these in order; a rung that finds nothing calls
# thinking_done and the post-step keep-alive re-enters the loop at the next one. They are constants and not
# literals because the keep-alive compares against the LAST one: a bare number there silently stops matching
# the moment a rung is inserted, and a run that skipped the new rungs would then spin to the turn limit
# without ever reaching the get-to-know question - losing exactly the "a run never ends in silence"
# guarantee the ladder exists to provide.
_STEP_GROUNDED = 0            # offer ONE suggestion, only if real memory supports it
_STEP_AUTOMATION_REVIEW = 1   # the user already has automations: improve one instead of adding another
_STEP_RELEVANCE = 2           # check whether something current CHANGES anything for this user
_STEP_GETTO = 3               # fact-free get-to-know question (the always-available fallback)
_STEP_DONE = 4                # the ladder is finished for this run


# Tools that count as DECISIVE progress in a thinking run (resolve an item, contact the user, or finish).
# Used by the progress-gate to detect "gathering/analysing forever without acting".
_PROGRESS_TOOLS = frozenset({
    "ask_user", "thinking_done",
    "delete_automation_note", "delete_automation_todo", "add_automation_todo",
    "create_automation", "update_automation", "save_thinking_suggestion",
})


def _turn_used_progress_tool(history_slice: List[Dict[str, Any]]) -> bool:
    """True if any assistant message in this turn's history slice called a decisive progress tool."""
    for m in history_slice or []:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            name = (tc.get("function") or {}).get("name") or tc.get("name") or ""
            if name in _PROGRESS_TOOLS:
                return True
    return False


def _deadline_status(due_at: str) -> str:
    """Deterministic deadline context for the forced todo prompt (so the weak model doesn't have to do
    date math): 'OVERDUE — …', 'due TODAY …', 'due TOMORROW …', 'due in N days …', or '' if no/unparseable
    date. Compares by calendar date."""
    due_at = (due_at or "").strip()
    if not due_at:
        return ""
    try:
        from datetime import datetime, date as _date
        try:
            d = datetime.fromisoformat(due_at).date()
        except ValueError:
            d = _date.fromisoformat(due_at[:10])
        delta = (d - datetime.now().date()).days
    except Exception:
        return ""
    if delta < 0:
        return f"OVERDUE — the deadline ({due_at}) is {-delta} day(s) in the PAST"
    if delta == 0:
        return f"due TODAY ({due_at})"
    if delta == 1:
        return f"due TOMORROW ({due_at})"
    return f"due in {delta} days ({due_at})"


def _build_forced_item_prompt(item: Dict[str, Any]) -> str:
    """The custom prompt for a FORCED-RESOLUTION node: it names exactly one open item and is paired with
    tool_choice='required' + gather tools disabled, so the model cannot search or write prose — it must
    emit a decisive tool call for THIS item. A NOTE → ask_user (help/question) or delete. A TODO → turn it
    into an automation: a low-risk REMINDER built autonomously (create_automation + clear the todo), or an
    ACTION automation proposed via ask_user. The deadline is given as a deterministic status; an OVERDUE
    todo is asked-about, not scheduled into the future; existing automations must not be duplicated."""
    iid = (item.get("id") or "").strip()
    label = (item.get("label") or "").strip() or "(no text)"
    if item.get("kind") == "todo":
        due = (item.get("due_at") or "").strip()
        status = _deadline_status(due)
        due_ctx = f" {status}." if status else " No fixed deadline."
        if status.startswith("OVERDUE"):
            return (
                f"Resolve the user's todo NOW — todo [{iid}]: \"{label}\".{due_ctx} The deadline has PASSED, "
                "so do NOT schedule a future reminder. Emit ONE tool call this turn (no prose, no searching):\n"
                f"- Ask whether it is still relevant: ask_user(message=\"<'{label}' war fällig {due} — ist das "
                f"noch aktuell, soll ich helfen?>\", source_todo_id=\"{iid}\").\n"
                f"- OR, if it is clearly done/obsolete, just clear it: delete_automation_todo(todo_id=\"{iid}\").\n"
                "Emit the tool call now."
            )
        rem_prompt = f"Remind the user: {label}" + (f" (due {due})" if due else "")
        ask_msg = "Soll ich dafür eine Automation einrichten" + (f", die bis {due} läuft?" if due else "?")
        return (
            f"Resolve the user's todo NOW — todo [{iid}]: \"{label}\".{due_ctx} A todo is a task to turn "
            "into an AUTOMATION so it isn't forgotten; use the deadline to choose WHEN to schedule it. You "
            "already have the list of existing automations — do NOT create one that DUPLICATES an existing "
            "automation. Resolve it this turn (no prose, no searching — gathering is disabled):\n"
            f"- REMINDER (just notify the user near the deadline) → build it YOURSELF, then clear the todo: "
            f"create_automation(name=\"<short name>\", prompt=\"{rem_prompt}\", frequency=\"<once|daily|"
            f"weekly|monthly>\", time=\"HH:MM\") — pick the frequency/time that fits the deadline — THEN "
            f"delete_automation_todo(todo_id=\"{iid}\").\n"
            f"- ACTION automation (it would DO something externally — send a mail, run a task, change "
            f"files) → do NOT build it yourself: ask_user(message=\"{ask_msg}\", proposed_action=\"create "
            f"automation for: {label}\", source_todo_id=\"{iid}\").\n"
            f"- If an existing automation ALREADY covers this todo, just clear it: "
            f"delete_automation_todo(todo_id=\"{iid}\").\n"
            "Emit the tool call(s) now."
        )
    return (
        f"Resolve the user's note NOW — note [{iid}]: \"{label}\". A note is a request for help. Emit "
        "EXACTLY ONE tool call this turn (no prose, no searching — gathering is disabled):\n"
        f"- BEST: ask_user(message=\"<one short, concrete suggestion or question about this, in the "
        f"user's language>\", source_note_id=\"{iid}\") — this delivers it to the user AND clears the note. "
        "If your message references things you found (e.g. tips/options), ALSO pass details=\"<the actual "
        "content — the real list/facts>\" so the user can get the specifics later without you re-deriving them.\n"
        f"- If you have already sent a suggestion for it, call delete_automation_note(note_id=\"{iid}\").\n"
        "Emit the ask_user (or delete) tool call now."
    )

# Queries for the proactive rung: what could be automated, what is being worked on, what is liked.
_PROACTIVE_DIGEST_QUERIES = [
    "a recurring routine or habit the user does regularly - daily or weekly, a repetitive task",
    "what the user is currently working on - their project, goal or focus",
    "the user's preferences, interests, likes and recurring needs",
]

# Queries for the relevance rung: what the user has COMMITTED to and what matters to them, because
# that is what something in the world can actually affect. Health is deliberately not a category:
# it exists in memory only as undated free text, the message lands on a lock screen the product does
# not control, and being wrong about it costs far more than being wrong about a train.
_WATCHLIST_DIGEST_QUERIES = [
    "plans, deadlines, appointments and commitments the user has stated, with their dates",
    "what the user is working on and what matters to them right now, their priorities",
    "the user's interests, things they follow, places and products they use or own",
]


def _memory_status(user_scope_id: Optional[str]) -> str:
    """'ok' | 'empty' | 'unavailable' - because an empty retrieval means two opposite things.

    `run_memory_search_sync` returns "" for a genuinely empty store AND for a pgvector container
    that is down. Left undistinguished, a database outage silently degrades every background run to
    small talk, for as long as it lasts, with nothing anywhere saying so. The probe that answers
    exactly this question already exists and is already used this way by the memory tools."""
    from vaf.core.config import Config
    if not Config.get("memory_enabled", True):
        return "empty"
    try:
        from vaf.memory.database import check_db_connection_sync
        return "empty" if check_db_connection_sync(timeout_seconds=3) else "unavailable"
    except Exception:
        return "unavailable"


def _build_memory_digest(user_scope_id: Optional[str], queries: List[str], k: Optional[int] = None) -> str:
    """Deterministically pull real memory snippets for the given queries; "" on any failure."""
    try:
        from vaf.core.config import Config
        if not Config.get("memory_enabled", True):
            return ""
        from vaf.memory.rag import run_memory_search_sync
        from uuid import UUID as _UUID
        task_scope = None
        if user_scope_id:
            try:
                task_scope = _UUID(str(user_scope_id))
            except (ValueError, TypeError):
                task_scope = None
        if k is None:
            k = int(Config.get("thinking_proactive_memory_k", 4) or 4)
        k = max(2, min(8, int(k)))
        seen: set = set()
        chunks: List[str] = []
        for q in queries:
            try:
                res = run_memory_search_sync(
                    query=q, k=k, user_scope_id=task_scope, caller="thinking_proactive",
                    # Both digests ask about the PERSON - a routine, a plan, an interest. Learned
                    # document text answers none of those and outnumbers them: measured 2026-08-30,
                    # these queries returned 14/20 and 17/20 document chunks, so two of the four
                    # "REAL MEMORIES about the user" a run was handed were PDF text.
                    exclude_documents=True,
                ) or ""
            except Exception:
                res = ""
            for part in res.split("---"):
                p = part.strip()
                if not p:
                    continue
                key = _normalize_ev(p)[:160]
                if not key or key in seen:
                    continue
                seen.add(key)
                chunks.append(p)
        return ("\n---\n".join(chunks))[:6000]
    except Exception:
        return ""


def _build_proactive_memory_digest(agent: Any, user_scope_id: Optional[str]) -> str:
    """Deterministically pull a representative sample of the user's REAL memories for the proactive step.
    The weak local model often never searches on its own, and the forced grounding turn cannot gather -
    so the run does the retrieval in code: a few targeted queries aimed at proactive value (recurring
    routines, current work, preferences) -> a deduped, length-bounded digest of real memory snippets. The
    model is shown this digest AND may still memory_search ONCE itself for specifics; it then quotes ONE
    snippet verbatim (the evidence-gate checks against the same text, which is seeded into the pool)."""
    return _build_memory_digest(user_scope_id, _PROACTIVE_DIGEST_QUERIES)


def _automation_review_state(user_scope_id: Optional[str], username: Optional[str] = None):
    """(enabled_task_count, findings) for this user's automations. ([], 0) on any failure.

    Scoped through the SAME accessor the list tool uses, with the run's resolved scope - never the
    process default, or one tenant's automations would decide another tenant's rung."""
    try:
        from vaf.tools.automation import _manager_for_scope
        from vaf.core.automation import review_findings, load_run_log
        manager, _ = _manager_for_scope(user_scope_id)
        tasks = [
            t for t in manager.list()
            if t.user_scope_id is None or str(t.user_scope_id) == str(user_scope_id)
        ]
    except Exception as e:
        logger.debug("Thinking: automation review could not read automations: %s", e)
        return 0, []
    enabled = [t for t in tasks if t.enabled]
    # The recorded outcomes, where they exist. They only start existing once a task runs again
    # after this was built, so a missing log is normal and simply narrows what can be said.
    run_logs = {}
    for t in tasks:
        try:
            log = load_run_log(manager._path_for_task(t))
            if log:
                run_logs[t.id] = log
        except Exception:
            continue
    try:
        return len(enabled), review_findings(tasks, run_logs=run_logs)
    except Exception as e:
        logger.debug("Thinking: automation review findings failed: %s", e)
        return len(enabled), []


def _drop_recently_raised(user_scope_id: Optional[str], findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Findings about an automation this run already asked about recently.

    De-duplicated on the AUTOMATION, not on the phrasing: an unfixed finding is still true next run,
    so keying on wording would re-send it every time and starve the rungs below - the run stops after
    one message. The automation's id is what makes a proposal identifiable, and the prompt requires
    it to appear in the proposal."""
    try:
        from vaf.core import thinking_requests as _treq
        from vaf.core.config import Config
        runs = int(Config.get("thinking_question_similarity_runs", 12) or 12)
        recent = _treq.list_requests(user_scope_id, within_runs=runs,
                                     current_run_seq=current_run_seq(user_scope_id))
        seen_text = " ".join(
            f"{r.get('question') or ''} {r.get('proposed_action') or ''} {r.get('details') or ''}"
            for r in recent
        ).lower()
    except Exception:
        return findings
    return [f for f in findings if str(f.get("task_id", "")).lower() not in seen_text]


def relevance_watch_allowed(user_scope_id: Optional[str]) -> tuple:
    """(allowed, reason) for the relevance rung. Two brakes, both deliberate.

    COOLDOWN, because this rung fires whenever everything else is clear - the common case for anyone
    not drowning in notes. At the default run cadence that is a dozen chances a day, and an unbounded
    FYI lane is a notification machine, not an assistant.

    SELF-DISABLE, because the only honest measure of this rung is how the user reacted to what it
    sent. Two DECLINED notices in the last ten and it stops on its own, rather than waiting for
    someone to find a setting. Declined specifically, not "unanswered": an FYI is not a question and
    is never replied to, so treating silence as rejection would switch the rung off for good on
    exactly the behaviour it is designed for. The reply classification it reads already exists."""
    from vaf.core.config import Config
    from vaf.core import thinking_requests as _treq
    if not Config.get("thinking_relevance_enabled", True):
        return False, "disabled"
    try:
        recent = [
            r for r in _treq.list_requests(user_scope_id, within_runs=200,
                                           current_run_seq=current_run_seq(user_scope_id))
            if (r.get("kind") or "") == "relevance"
        ]
    except Exception:
        return True, "ok"          # no history readable -> do not silence the rung on a read error
    if recent:
        # NOT `or 72`: a configured 0 means "no cooldown" and must survive, which the usual
        # or-fallback silently turns back into the default.
        _raw = Config.get("thinking_relevance_cooldown_hours", 72)
        try:
            hours = float(72 if _raw is None else _raw)
        except (TypeError, ValueError):
            hours = 72.0
        if hours > 0:
            from datetime import datetime as _dt
            newest = None
            for r in recent:            # by timestamp, not by list order
                try:
                    at = _dt.fromisoformat(str(r.get("created_at") or ""))
                except (ValueError, TypeError):
                    continue
                if newest is None or at > newest:
                    newest = at
            if newest is not None and (_dt.now() - newest).total_seconds() < hours * 3600:
                return False, "cooldown"
    # Counts DECLINED only. An FYI is not a question, so it is never replied to and never leaves
    # status "asked" - counting that as "ignored" would have made the rung disable itself on its
    # own normal behaviour, permanently, after ten perfectly good notices. What can honestly be
    # measured is an explicit negative reaction, and that is what this reads.
    last_ten = recent[:10]
    if len(last_ten) >= 10:
        declined = sum(1 for r in last_ten if (r.get("status") or "") == "declined")
        if declined >= 2:
            return False, "self_disabled"
    return True, "ok"


def _build_automation_review_digest(findings: List[Dict[str, Any]]) -> str:
    """The findings as evidence text, so a proposal quoting one passes the existing grounded gate.

    No new gate: this goes into the run's evidence pool exactly like a retrieved memory does."""
    return "\n".join(
        f"- {f['task_name']} ({f['task_id']}): {f['detail']}" for f in findings
    )[:4000]


# Automation-review rung. The findings are computed in CODE and handed over; the model's only job is
# to phrase ONE of them and propose a fix. It may not read an automation's prompt and invent an
# improvement: a prompt reads like evidence while saying nothing about how the job behaved, which is
# the same fabrication surface the un-forced proactive step exists to avoid.
_PROMPT_AUTOMATION_REVIEW = (
    "The user already has several automations, so do NOT propose a new one. Below are CHECKED "
    "observations about the ones they have - each was computed from the stored record, not guessed.\n"
    "Pick the ONE that is most worth raising and write a short, friendly message about it, in the "
    "user's language, naming the automation and its id, and proposing a concrete fix:\n"
    "  ask_user(message=\"<what you noticed + what you suggest>\", proposed_action=\"update automation "
    "<id>: <the change>\", details=\"<the observation, quoted verbatim from the list below>\")\n"
    "HARD RULES: state ONLY what the observation says. You may NOT say an automation failed, errored, "
    "is broken, runs too long or produces bad output - none of that is recorded anywhere, and 'no "
    "successful run since <date>' looks exactly the same as 'the machine was switched off'. Do NOT "
    "call update_automation/create_automation/delete_automation yourself - propose it and let the user "
    "decide. If none of the observations is worth the user's attention, call "
    "thinking_done(\"Nothing worth raising.\") and say nothing. EXACTLY ONE tool call, no prose.\n\n"
    "CHECKED OBSERVATIONS:\n"
)


# Relevance rung. The point is IMPACT, not news: the run already knows what the user has committed
# to, and asks whether anything current changes it. A summary of headlines is a failure of this rung,
# not an output, and falling through silently is its normal case - which is why it is not forced.
_PROMPT_RELEVANCE = (
    "Below is what you actually know about this user's plans, commitments and interests. Pick ONE "
    "item that something in the world could plausibly AFFECT, and check it with web_search (at most "
    "two searches).\n"
    "Then decide honestly:\n"
    "A) If what you found genuinely CHANGES something for this user - a date, a cost, a route, a "
    "deadline, something they own or use - tell them, in their language, as an IMPACT statement: what "
    "you found, and why it matters for THEIR specific plan. Include the source and its date.\n"
    "   ask_user(message=\"<what changes for them, and why>\", details=\"<the exact search query you "
    "ran, the source URL and its date, and a VERBATIM quote of the memory this concerns>\")\n"
    "B) OTHERWISE - and this is the normal outcome - call thinking_done(\"Nothing relevant.\") and say "
    "nothing at all. Staying quiet is a correct result here.\n"
    "HARD RULES: a news summary or a digest is NOT an output - if it does not change something "
    "concrete for THIS user, choose B. Never speculate: 'could', 'might', 'possibly' means you have "
    "nothing, so choose B. Never state a fact you did not find. Your search query goes to an outside "
    "search engine, so write it as a query a stranger could have typed: a public topic plus at most a "
    "city or a date. Do NOT put the user's name, their employer, an internal project name, an email "
    "address or anything private into it. EXACTLY ONE final tool call, no prose.\n\n"
    "WHAT YOU KNOW ABOUT THEM:\n"
)


# Floor clear: PROACTIVE intelligence (Stufe 2). Offer ONE suggestion ONLY if the REAL retrieved memories
# genuinely support it (every stated fact must come from them, quoted in `details`). This node is NOT forced
# and carries NO "you must produce a suggestion" pressure: forcing a fact-containing message on an empty/thin
# desk is exactly what made a strong model INVENT a routine to satisfy the mandate. If nothing is genuinely
# grounded, the model defers and the next rung asks a fact-FREE get-to-know question (which states no fact and
# can never be a fabrication). Facts may enter the chat ONLY through real grounding; everything else is a
# question about the user.
_PROMPT_PROACTIVE = (
    "Your housekeeping is clear — no open notes or todos. Now think proactively for the user. Below are "
    "REAL memories retrieved for you; you may ALSO call memory_search ONCE with a precise query before you "
    "decide. You have a CHOICE — there is NO pressure to produce a suggestion:\n"
    "A) ONLY if these REAL memories genuinely support a specific, useful suggestion (e.g. something the user "
    "actually does REPEATEDLY that you could automate, and that no existing automation covers): offer it — "
    "ask_user(message=\"<e.g. 'Du fragst fast jeden Morgen nach dem Wetter — soll ich dir das automatisch um "
    "7:00 schicken?'>\", proposed_action=\"create automation: <what + when>\", details=\"<QUOTE the exact real "
    "memory it is based on>\"). EVERY fact in your message must come from the memories above.\n"
    "B) OTHERWISE — if you are unsure, or making a suggestion would require inventing ANY detail (a habit, a "
    "number, a routine, a preference the memories do not literally state) — do NOT force it: call "
    "thinking_done('Nothing grounded.') and you will then ask ONE friendly get-to-know question instead.\n"
    "HARD RULES: NEVER invent or embellish — a half-remembered, paraphrased, or 'probably' fact is an "
    "invention; when in doubt choose B. Never generic ('can I help?'); never repeat a recent/declined "
    "question. At most ONE memory_search, then EXACTLY ONE tool call (ask_user OR thinking_done) — no prose."
)

# Forced fallback: nothing grounded to suggest -> still NOT silence. Ask ONE question to get to know the
# user better (so future runs can help). A question states no fact, so it is not evidence-gated.
_PROMPT_GET_TO_KNOW = (
    "You found nothing concrete to suggest from memory right now — but silence is NOT the goal. Ask the "
    "user ONE specific, friendly question to get to know them better, so you can help them more next time: "
    "their current focus or work, a routine they would like automated, a recurring task, or an interest. "
    "Emit EXACTLY ONE tool call: ask_user(message=\"<the question, in the user's language>\"). Make it "
    "specific and natural (NOT 'how can I help?'); never repeat a recent or declined question. No other tools."
)

# Appended to the get-to-know prompt on a RETRY after the semantic-dedup gate rejected the previous
# attempt as too similar to a recent question. Pushes the model OFF the over-used topic into a clearly
# different life area, so the proactive questions fan out instead of circling the same subject.
_GET_TO_KNOW_RETRY_HINT = (
    "\n\nIMPORTANT: your previous question was too similar to one you already asked recently. Choose a "
    "CLEARLY DIFFERENT area this time — for example a hobby or interest, daily life, health/wellbeing, "
    "people in their life, learning something new, travel, food, or a future goal — and AVOID the work/"
    "project topic if that is what you keep asking about. Pick a genuinely fresh subject."
)

# Floor clear but proactive disabled / rate-limited: just finish.
_PROMPT_NOTHING_TODO = (
    "Your housekeeping is clear and there is nothing proactive to raise right now. Call "
    "thinking_done('Nothing actionable.')."
)


# What a follow-up must hand over, in both lanes. A reminder is deliberately terse, and the person
# reading it on a messenger hours later often answers "what?" rather than yes or no - at which point the
# MAIN agent has to take the topic over, and it can only work from what the request carries. So the
# reminder is not allowed to be the whole record: `details` says what this was about, in a form somebody
# who never saw the original run can act on. Live 2026-08-30: a "Sollen wir heute mit dem Commit
# weitermachen - ja oder nein?" met a "Hey sry was ?", and the main agent had nothing but that one line,
# so it asked which message was meant instead of naming the subject.
_FOLLOWUP_CONTEXT_RULE = (
    "\nHand the context over with it, ALWAYS: details=\"<what this is actually about - the subject in "
    "your own words, what you found or proposed, and anything the user would need to make sense of the "
    "reminder>\", proposed_action=\"<the concrete thing to do if they say yes>\". The user may well answer "
    "'what?' rather than yes or no, and the main agent then takes over from `details` alone - a reminder "
    "without it leaves it with nothing to say."
)


def _build_followup_prompt(question: str, reconfirm: bool = False) -> str:
    """Re-ask the SAME open question instead of proposing a new topic. Normally a pointed yes/no
    follow-up (the user has not replied yet). When `reconfirm` is True the user DID reply once but
    ambiguously and the run could not tell whether it got done, so ask a SOFT, retrospective check-back
    (a recap) rather than a fresh pitch. Both lanes must carry the subject over - see
    `_FOLLOWUP_CONTEXT_RULE`."""
    q = (question or "").strip().replace("\n", " ")[:300]
    if reconfirm:
        return (
            "Earlier you asked the user about the thing below, and they DID reply — but it was ambiguous "
            "and you could not tell whether this ended up happening:\n"
            f"  \"{q}\"\n"
            "Do NOT pitch it again from scratch. Ask ONE casual, friendly check-back, phrased as a RECAP in "
            "the user's language — e.g. 'Hey, sorry — hatten wir das eigentlich gemacht/eingerichtet?'. "
            "Emit EXACTLY ONE tool call: ask_user(message=\"<the check-back>\")."
            + _FOLLOWUP_CONTEXT_RULE + " No other tools, no prose."
        )
    return (
        "You earlier reached out to the user with the question below, and they have NOT replied yet:\n"
        f"  \"{q}\"\n"
        "Do NOT introduce a new topic. Ask ONE short, friendly FOLLOW-UP on the SAME thing, phrased so it is "
        "easy to answer with a quick yes/no (e.g. 'Soll ich das einrichten — ja oder nein?'), in the user's "
        "language. Emit EXACTLY ONE tool call: ask_user(message=\"<the follow-up>\")."
        + _FOLLOWUP_CONTEXT_RULE + " No other tools, no prose."
    )


def _get_turn_prompt(turn: int, ledger_clear: bool = True) -> str:
    """Turn 0 = THINKING_PROMPT (gather + Stufe-0). Turns 1+ are content-driven: keep doing housekeeping
    while the ledger has unresolved items, otherwise the proactive scan (the loop decides whether proactive
    is allowed; see the run loop)."""
    if turn == 0:
        return THINKING_PROMPT
    return _PROMPT_PROACTIVE if ledger_clear else _PROMPT_HOUSEKEEPING


def _extract_run_summary(agent_history: List[Dict[str, Any]]) -> str:
    """Extract a concise summary of what the thinking run actually did."""
    summary_parts = []
    tools_used = []
    final_conclusion = ""
    
    for msg in agent_history:
        if not isinstance(msg, dict): continue
        if msg.get("role") == "assistant":
            # Track tool calls
            for tc in msg.get("tool_calls") or []:
                name = (tc.get("function") or {}).get("name") or tc.get("name") or ""
                if name and name not in ("thinking_done", "thinking_note_add", "list_automation_todos", "list_automation_notes", "list_automations"):
                    tools_used.append(name)
                
                # Check for thinking_done summary
                if name == "thinking_done":
                    args = tc.get("function", {}).get("arguments") or tc.get("arguments") or "{}"
                    if isinstance(args, str):
                        try:
                            args_dict = json.loads(args)
                            final_conclusion = args_dict.get("summary") or ""
                        except Exception: pass
                    elif isinstance(args, dict):
                        final_conclusion = args.get("summary") or ""

    if tools_used:
        unique_tools = list(dict.fromkeys(tools_used))
        summary_parts.append(f"Tools: {', '.join(unique_tools)}")
    
    if final_conclusion:
        summary_parts.append(f"Result: {final_conclusion}")
    
    if not summary_parts:
        return "No actionable items found."

    return " | ".join(summary_parts)


_PROMPT_CLASSIFY_REPLY = (
    "A background suggestion was made to a user, and the user replied. Decide the OUTCOME.\n\n"
    "Your question: \"{question}\"\n"
    "Proposed action: \"{action}\"\n"
    "The user replied: \"{user_reply}\"\n"
    "The assistant then said to the user: \"{main_reply}\"\n\n"
    "Use BOTH messages — the assistant's reply may reveal it already CARRIED OUT or DROPPED the task. "
    "Answer with ONE word only:\n"
    "ACCEPTED — the user agreed, or the task was taken over / already done.\n"
    "DECLINED — the user said no, not now, or refused. A reply that clearly refuses (e.g. 'nein', 'no', "
    "'nope', 'kein Bedarf', 'brauch ich nicht') is DECLINED even if it also asks a question (e.g. "
    "'für was? nein!').\n"
    "UNCLEAR — ONLY if neither message lets you tell; do not pick this just because the reply is short.\n"
    "Answer:"
)


def _classify_reply_outcome(
    agent: Any, question: str, action: str, user_reply: str, main_reply: str
) -> Optional[str]:
    """One isolated LLM call classifying a replied proactive request as ACCEPTED / DECLINED / UNCLEAR.
    Lenient parse: the first decisive keyword found in the model's text wins (a reasoning model may emit
    <think> first, or punctuate). Returns 'UNCLEAR' on an undecidable/empty answer, or None if the LLM
    call itself FAILED — so the caller can leave the request 'replied' and retry on a later run rather
    than prompting the user a reconfirm over a transient outage."""
    prompt = _PROMPT_CLASSIFY_REPLY.format(
        question=(question or "")[:500],
        action=(action or "(none)")[:300],
        user_reply=(user_reply or "")[:500],
        main_reply=(main_reply or "(the assistant did not reply)")[:300],
    )
    try:
        raw = agent._generate_for_classification(prompt) or ""
    except Exception:
        return None
    up = raw.upper()
    positions = {k: up.find(k) for k in ("ACCEPTED", "DECLINED", "UNCLEAR") if k in up}
    if not positions:
        return "UNCLEAR"
    return min(positions, key=positions.get)


def _classify_replied_requests(agent: Any, user_scope_id: Optional[str]) -> None:
    """At the start of a run, classify any proactive questions the user has answered since the last run
    (status 'replied') from the triple {question, user reply, the main agent's own reply}:
      ACCEPTED -> 'done' (+ mark any source note/todo handled, since the user agreed),
      DECLINED -> 'declined' (+ the declined-questions dedup log),
      UNCLEAR  -> re-open to 'asked' with needs_reconfirm so the follow-up node asks ONE soft
                  retrospective check-back next.
    On any error the request stays 'replied' and is retried next run. This LLM-based step replaces the
    old brittle `_is_refusal` keyword classifier; it owns the accepted-vs-declined decision."""
    try:
        from vaf.core import thinking_requests as _treq
    except Exception:
        return
    try:
        replied = _treq.list_requests(user_scope_id, status="replied")
    except Exception:
        return
    for req in replied[:5]:  # newest first; cap the per-run burst (any stragglers resolve next run)
        rid = req.get("id")
        if not rid or not (req.get("user_reply") or "").strip():
            continue  # nothing captured to classify yet
        outcome = _classify_reply_outcome(
            agent,
            req.get("question") or "",
            req.get("proposed_action") or "",
            req.get("user_reply") or "",
            req.get("main_reply") or "",
        )
        if outcome is None:
            logger.info("Thinking reply-classify: request %s — LLM call failed, leaving 'replied'", rid)
            continue  # the LLM call failed -> leave status 'replied', retry on a later run
        logger.info(
            "Thinking reply-classify: request %s -> %s | user_reply=%r main_reply=%r",
            rid, outcome, (req.get("user_reply") or "")[:80], (req.get("main_reply") or "")[:80],
        )
        try:
            if outcome == "ACCEPTED":
                _treq.update_request_status(user_scope_id, rid, "done")
                try:
                    from vaf.core import automation_planner as _ap
                    if (req.get("source_note_id") or "").strip():
                        _ap.set_note_handled(user_scope_id, req["source_note_id"], True)
                    if (req.get("source_todo_id") or "").strip():
                        _ap.update_todo(user_scope_id, req["source_todo_id"], done=True)
                except Exception:
                    pass
            elif outcome == "DECLINED":
                _treq.update_request_status(user_scope_id, rid, "declined")
                try:
                    _save_declined_entry(user_scope_id, req.get("question") or "", req.get("user_reply") or "")
                except Exception:
                    pass
            elif req.get("reconfirmed"):
                # Already reconfirmed once and STILL undecidable -> stop pestering. Resolve to declined:
                # never auto-act on a proposal the user did not clearly accept; they can raise it again.
                _treq.update_request_status(user_scope_id, rid, "declined")
                try:
                    _save_declined_entry(user_scope_id, req.get("question") or "", req.get("user_reply") or "")
                except Exception:
                    pass
            else:  # UNCLEAR (first time) -> ONE soft retrospective reconfirm next run
                _treq.reopen_for_reconfirm(user_scope_id, rid)
        except Exception:
            continue  # leave status 'replied' -> retried next run


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

    # The local admin is normalized to None for idle-tracking, but their actual data (automation
    # notes/todos, RAG, sessions) lives under the real local_admin_scope_id — where the Web UI / main
    # agent write. Resolve to that real scope so every DATA read (the agent's tools, the deterministic
    # workspace/automation injection, RAG) reads the same store the user sees. _key() still maps it
    # back to "default", so the thinking-mode bookkeeping (locks/cooldown/...) is unchanged.
    if user_scope_id is None:
        user_scope_id = get_local_admin_scope_id()

    scope_key = _key(user_scope_id)
    run_status = "success"
    run_summary = "Thinking run completed."
    max_duration_minutes = int(Config.get("thinking_max_duration_minutes", 30) or 30)
    # Bump the per-user run counter so ask_user can stamp requests with the current run sequence
    # (drives the "recently asked" window so the agent does not re-ask within ~6 runs).
    next_run_seq(user_scope_id)
    # So Agent._load_tools() sees thinking mode and registers thinking_done / thinking_note_add tools
    os.environ["VAF_THINKING_MODE"] = "1"
    # Pass scope_key to thinking_note_add tool via env (tool reads VAF_THINKING_SCOPE_ID)
    os.environ["VAF_THINKING_SCOPE_ID"] = scope_key
    os.environ["VAF_THINKING_RUN_ID"] = run_id
    # Background-task pro routing: the thinking run is a background task, so deepseek-auto must resolve
    # to deepseek-v4-pro (the user's "background task = pro" design). A dedicated flag is used instead of
    # VAF_IN_WORKFLOW_TERMINAL (which would make maybe_start_thinking_for_user SKIP the run) or
    # VAF_IN_AUTOMATION (which carries other automation/buffer semantics). Cleared in the finally below.
    os.environ["VAF_BACKGROUND_PRO"] = "1"

    # 🚀 COST EFFICIENCY: Use specific provider/model for thinking if configured
    t_provider = (Config.get("thinking_provider") or "inherit").strip().lower()
    t_model = Config.get("thinking_model")
    if t_provider != "inherit":
        os.environ["VAF_PROVIDER"] = t_provider
    if t_model:
        os.environ["VAF_MODEL_OVERRIDE"] = str(t_model)

    try:
        from vaf.core.agent import Agent

        agent = Agent(verbose=False, run_kind="thinking")
        agent.load_model()
        # Set user context BEFORE init_chat() so system prompt (User Identity, RAG scope) and tools get the right user
        identity = resolve_scope_identity(user_scope_id)
        bind_identity(agent, identity)
        agent.init_chat()

        # Load the user's main chat session so the thinking agent sees the full conversation history.
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
                    # The bridge names the owner's chat session by user and number digits
                    # (whatsapp_<username>_<digits>), the same way _record_outbound does.
                    chat_session_id = f"whatsapp_{uname}_{jid.split('@', 1)[0].split(':', 1)[0] or 'self'}"
            # Fallback: user-scoped default session
            if not chat_session_id:
                safe_scope = scope_key.replace("-", "")[:8]
                chat_session_id = f"web-default-{safe_scope}"

            if chat_session_id:
                try:
                    agent.load_session_context(chat_session_id)
                    # load_session_context assigns the session's stored identity
                    # unconditionally, including None. A session with no username
                    # stored would null ours, and the `or admin-name` fallbacks
                    # below then read the OWNER's workspace into this tenant's
                    # prompt. The run's own identity is authoritative.
                    reassert_identity(agent, identity)
                    logger.info("Thinking agent loaded chat session: %s", chat_session_id)
                except Exception as e:
                    logger.debug("Could not load chat session %s for thinking: %s", chat_session_id, e)
        except Exception as e:
            logger.debug("Could not resolve chat session for thinking: %s", e)

        # Channel guard: the thinking agent may contact the user only via the
        # configured main_messenger; without one, every send tool is removed
        # and questions reach the user as plain text via the Web UI fallback.
        try:
            from vaf.core.messaging_connections import get_messaging_connections as _gmc
            _guard_uname = getattr(agent, "_current_username", None) or get_local_admin_username()
            _guard_conn = _gmc(username=_guard_uname, user_scope_id=user_scope_id) or {}
            _guard_mm = (_guard_conn.get("main_messenger") or "").strip().lower()
        except Exception:
            _guard_mm = ""
        try:
            _removed_send_tools = _filter_thinking_send_tools(agent.tools, _guard_mm)
            if _removed_send_tools:
                logger.info(
                    "Thinking Mode: removed send tools %s (main_messenger=%r)",
                    _removed_send_tools, _guard_mm or "not set",
                )
        except Exception as e:
            logger.debug("Thinking Mode: send-tool filter failed: %s", e)

        # Append thinking mode notice and last run summary (context so we don't repeat or re-ask)
        if agent.history and agent.history[0].get("role") == "system":
            # Determine time since last interaction for temporal clarity
            li = get_last_interaction(user_scope_id)
            rel_time = ""
            if li and li.get("ts"):
                try:
                    if hasattr(agent, "prompt_manager"):
                        rel_time = f" (Letzte Nutzer-Nachricht war: {agent.prompt_manager._format_relative_time(li['ts'])})"
                except Exception:
                    pass

            notice = (
                f"\n\n## THINKING MODE (background pass){rel_time}\n"
                "You are running a background check while the user is idle. "
                "Act > Ask. Max 1 message. Never reveal you're in thinking mode. "
                "ALWAYS call thinking_done when finished — no exceptions. "
                "If nothing to do, call thinking_done('Nothing actionable.') immediately."
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
            # Requests you already raised recently (asked/confirmed/done/declined) so you do NOT re-ask
            # within the recency window (default 6 runs).
            try:
                from vaf.core import thinking_requests as _treq
                _recent = int(Config.get("thinking_recent_request_runs", 6) or 6)
                _req_prompt = _treq.recent_requests_prompt(
                    user_scope_id, current_run_seq=current_run_seq(user_scope_id), within_runs=_recent,
                )
                if _req_prompt:
                    notice += "\n\n" + _req_prompt
            except Exception as _req_err:
                logger.debug("Could not load recent thinking requests: %s", _req_err)
            try:
                from vaf.core.thinking_notes import build_notes_prompt
                notes_prompt = build_notes_prompt(scope_key)
                if notes_prompt:
                    notice += "\n\n" + notes_prompt
            except Exception as _notes_err:
                logger.debug("Could not load thinking notes: %s", _notes_err)
            # Thinking Workspace context: blend existing todos/notes and open workspace tasks.
            try:
                from vaf.core.thinking_workspace import collect_existing_task_sources, list_tasks

                existing_items = collect_existing_task_sources(user_scope_id, limit=6)
                open_tasks = list_tasks(user_scope_id, status="open")[:5]
                if existing_items or open_tasks:
                    lines = ["", "**Thinking Workspace context (MVP):**"]
                    if open_tasks:
                        lines.append("- Open workspace tasks:")
                        for t in open_tasks:
                            lines.append(f"  - [{t.get('id')}] {t.get('title')} (source: {t.get('source')})")
                    if existing_items:
                        lines.append("- Existing task candidates:")
                        for item in existing_items:
                            content = (item.get("content") or "")[:120]
                            lines.append(f"  - ({item.get('source')}) {item.get('title')}: {content}")
                    lines.append(
                        "- If you prepare an externally visible action, create a handoff proposal instead of direct apply."
                    )
                    notice += "\n" + "\n".join(lines)
            except Exception as _ws_err:
                logger.debug("Could not load workspace context: %s", _ws_err)
            agent.history[0]["content"] = (agent.history[0]["content"] or "") + notice

        logger.info("Thinking started for user %s", scope_key[:8] if scope_key != "default" else "default")

        try:
            # Turn 0 gathers; every further rung of the ladder needs a turn of its own, so a run that
            # walks the whole ladder must still be able to reach the get-to-know question.
            max_turns = int(Config.get("thinking_max_turns", 8) or 8)
            max_turns = max(1, min(max_turns, 10))
            # Progress-gate: after this many turns with no decisive (act/ask/clear) tool, force a one-tool
            # decision. Give the loop room to reach the threshold + 2 turns to comply (capped at 10).
            _progress_threshold = max(2, int(Config.get("thinking_no_progress_turns", 5) or 5))
            max_turns = min(10, max(max_turns, _progress_threshold + 2))
            # RAG context for first turn only — build user-specific query
            memory_context = ""
            try:
                if Config.get("memory_enabled", True):
                    from vaf.memory.rag import turn_memory_context
                    from uuid import UUID as _UUID
                    task_scope = None
                    if user_scope_id:
                        try:
                            task_scope = _UUID(str(user_scope_id))
                        except (ValueError, TypeError):
                            pass
                    # Build user-specific RAG query from identity + recent chat topics
                    rag_query_parts = []
                    try:
                        from vaf.auth.user_workspace import get_user_workspace
                        uname = getattr(agent, "_current_username", None) or "admin"
                        ws = get_user_workspace(uname)
                        ui = ws.get_user_identity() or {}
                        name = (ui.get("name") or "").strip()
                        if name:
                            rag_query_parts.append(name)
                        for pref in (ui.get("preferences") or [])[:3]:
                            rag_query_parts.append(str(pref))
                        for do in (ui.get("dos") or [])[:2]:
                            rag_query_parts.append(str(do))
                    except Exception:
                        pass
                    try:
                        user_msgs = [m for m in (getattr(agent, "history", []) or [])
                                     if isinstance(m, dict) and m.get("role") == "user"]
                        for msg in user_msgs[-3:]:
                            content = (msg.get("content") or "")[:100]
                            if content.strip():
                                rag_query_parts.append(content.strip())
                    except Exception:
                        pass
                    rag_query = (" ".join(rag_query_parts).strip() or "user profile preferences tasks projects")[:300]
                    memory_context = turn_memory_context(
                        rag_query, user_scope_id=task_scope, caller="thinking_mode")
            except Exception:
                memory_context = ""

            # Log/summary must include only messages created during THIS run,
            # not preloaded session history.
            run_history_start = len(getattr(agent, "history", []) or [])

            # Stufe-0 completion gate: snapshot the open notes/todos at run START. Before the run is
            # allowed to finish (thinking_done), every captured item must be acted-and-cleared or turned
            # into a tracked question this run; otherwise the run gets ONE targeted nudge naming the
            # specific items. user_scope_id is already resolved (None -> admin) above, so the ledger reads
            # the same per-user store the agent's tools write under.
            from vaf.core import thinking_ledger as _tledger
            _gate_enabled = bool(Config.get("thinking_gate_enabled", True))
            _cur_seq = current_run_seq(user_scope_id)
            _run_ledger = _tledger.build_ledger(user_scope_id) if _gate_enabled else []
            # An item already asked within this many runs counts as handled-for-now: the forced node does
            # not re-ask it and the completion gate does not block on it (it re-surfaces after the window).
            _recent_runs = max(1, int(Config.get("thinking_recent_request_runs", 6) or 6))
            _gate_nudged = False
            _no_progress_turns = 0      # consecutive turns with no decisive (act/ask/clear) tool
            _force_decision_pending = False

            # Classify any proactive questions the user answered since the last run (status 'replied'),
            # using the full triple {question, user reply, the main agent's own reply}. ACCEPTED -> done,
            # DECLINED -> declined, UNCLEAR -> re-open for a soft reconfirm. Runs BEFORE the open-follow-up
            # lookup below so a reconfirm-reopened request becomes this run's follow-up question.
            try:
                _classify_replied_requests(agent, user_scope_id)
            except Exception:
                pass

            # Proactive (Stufe 2): seed the evidence pool with this run's real retrieved memory + recent
            # user history, so a proactive suggestion can be verified as grounded (not fabricated). The
            # agent's memory_search calls add to the pool live (see agent.chat_step). Phase off by default.
            _proactive_enabled = bool(Config.get("thinking_proactive_enabled", True))
            # (rate-limit removed: a clear floor ALWAYS reaches out; repeats handled by dedup prompts.)
            # Proactive grounding turns: 0,1 = grounded (model also searches itself); 2 = get-to-know; 3 = done.
            _proactive_step = _STEP_GROUNDED
            _proactive_digest = ""    # real memories retrieved in code, shown to the model + in the pool
            clear_run_evidence(user_scope_id)
            set_proactive_mode(user_scope_id, "off")
            clear_followup_context(user_scope_id)
            # Follow-up vs new topic: if a previous proactive question is still UNANSWERED, the run re-asks
            # THAT one (pointed) instead of proposing a new topic — up to thinking_followup_max times, then
            # the topic rests (no question, no nudge) until the user reacts.
            _open_followup = None
            _followup_action = None  # 'ask' | 'rest'
            if _proactive_enabled:
                try:
                    from vaf.core import thinking_requests as _treq
                    _of = _treq.get_open_proactive_request(user_scope_id, _cur_seq, within_runs=_recent_runs)
                    if _of:
                        _max_fu = max(0, int(Config.get("thinking_followup_max", 3) or 3))
                        _open_followup = _of
                        _followup_action = "ask" if int(_of.get("followups") or 0) < _max_fu else "rest"
                except Exception:
                    _open_followup = None
            try:
                _seed = (memory_context or "")
                _user_hist = [str(m.get("content") or "") for m in (getattr(agent, "history", []) or [])
                              if isinstance(m, dict) and m.get("role") == "user"]
                if _user_hist:
                    _seed = _seed + "\n" + "\n".join(_user_hist[-12:])
                set_run_evidence(user_scope_id, _seed)
            except Exception:
                pass
            # Hand the proactive step REAL memories: the weak model rarely searches on its own and the
            # forced grounding turn cannot gather, so retrieve a targeted sample in code. Seeded into the
            # evidence pool so a verbatim quote of it passes the gate; also injected into the prompt below.
            if _proactive_enabled:
                try:
                    _proactive_digest = _build_proactive_memory_digest(agent, user_scope_id)
                    if _proactive_digest:
                        add_run_evidence(user_scope_id, _proactive_digest)
                except Exception:
                    _proactive_digest = ""

            # Rung availability is decided ONCE, here, and read inside the elif chain. Deciding it in
            # the loop body instead would make a disabled rung consume a turn on its way to being
            # skipped, and the last rung - the question that keeps a run from ending in silence - is
            # what runs out of turns.
            # An empty memory retrieval means two opposite things, and the proactive ladder is built
            # entirely on memory. Told apart HERE, once per run: a database outage would otherwise
            # degrade every background run to small talk for as long as it lasts, silently.
            if _proactive_enabled and not (_proactive_digest or "").strip():
                _mem_status = _memory_status(user_scope_id)
                if _mem_status == "unavailable":
                    logger.warning("Thinking: memory unavailable - proactive ladder skipped this run")
                    try:
                        from vaf.core.log_helper import append_domain_log_always
                        append_domain_log_always(
                            "backend", "[THINKING] memory unavailable - proactive ladder skipped")
                    except Exception:
                        pass
                    _proactive_enabled = False   # routes to the existing "nothing to do" branch and ends

            _review_findings: List[Dict[str, Any]] = []
            _watchlist = ""
            if _proactive_enabled and Config.get("thinking_relevance_enabled", True):
                _rel_ok, _rel_why = relevance_watch_allowed(user_scope_id)
                if _rel_ok:
                    _watchlist = _build_memory_digest(user_scope_id, _WATCHLIST_DIGEST_QUERIES)
                    if _watchlist:
                        add_run_evidence(user_scope_id, _watchlist)
                else:
                    logger.info("Thinking: relevance watch skipped (%s)", _rel_why)

            if _proactive_enabled and Config.get("thinking_automation_review_enabled", True):
                _n_enabled, _found = _automation_review_state(user_scope_id)
                _min_autos = max(1, int(Config.get("thinking_automation_review_min_automations", 3) or 3))
                if _n_enabled >= _min_autos:
                    _review_findings = _drop_recently_raised(user_scope_id, _found)
                    if _review_findings:
                        add_run_evidence(user_scope_id, _build_automation_review_digest(_review_findings))
                        logger.info("Thinking: automation review has %d finding(s) over %d automations",
                                    len(_review_findings), _n_enabled)

            for turn in range(max_turns):
                _unresolved = (
                    _tledger.unresolved_items(user_scope_id, _run_ledger, _cur_seq, recent_runs=_recent_runs)
                    if (_gate_enabled and _run_ledger) else []
                )
                _ledger_clear = not _unresolved
                _force_tc = None
                _node = ""             # which rung this turn is: selects the read-cap's block text + budget
                _allow_search = False   # proactive grounding turns let the model memory_search itself
                set_proactive_mode(user_scope_id, "off")  # default: block free messages; proactive branch opens it
                set_message_kind(user_scope_id, "")       # reset per turn: only the relevance rung sends an FYI
                if turn == 0:
                    # Turn 0: gather (THINKING_PROMPT) — read the notes/todos/memory before acting.
                    prompt = _get_turn_prompt(0, _ledger_clear)
                elif not _ledger_clear:
                    # FORCED-RESOLUTION NODE (the enforceable gate-tree): pick the first open item and
                    # compel the model to resolve it — tool_choice='required' + gather disabled means it
                    # MUST emit ask_user/delete for this item; it can no longer escape into search or prose.
                    prompt = _build_forced_item_prompt(_unresolved[0])
                    _force_tc = "required"
                    _node = "forced_item"
                elif _force_decision_pending:
                    prompt = _PROMPT_FORCE_DECISION
                    _force_decision_pending = False
                    _force_tc = "required"
                    _node = "forced_item"
                elif not _proactive_enabled:
                    # Floor clear but proactivity DISABLED -> just finish. (Rate-limiting no longer silences
                    # a run: silence is never the goal. Repeats are prevented by the recent/declined dedup
                    # prompts injected into the persistent system message; frequency by cooldown + quiet hours.)
                    prompt = _PROMPT_NOTHING_TODO
                elif _open_followup is not None and _followup_action == "ask":
                    # FOLLOW-UP: a previous proactive question is still open -> re-ask THAT one instead of a
                    # new topic. Normally a pointed yes/no; if it was re-opened for reconfirm (the user
                    # replied ambiguously and we could not tell if it got done), a SOFT retrospective recap.
                    # The delivery bumps the original request's follow-up counter (set_followup_context)
                    # rather than creating a duplicate.
                    prompt = _build_followup_prompt(
                        _open_followup.get("question") or "",
                        reconfirm=bool(_open_followup.get("needs_reconfirm")),
                    )
                    _force_tc = "required"
                    _node = "getto"
                    set_proactive_mode(user_scope_id, "open")
                    set_followup_context(user_scope_id, _open_followup.get("id"))
                    _proactive_step = _STEP_DONE
                elif _open_followup is not None and _followup_action == "rest":
                    # Already followed up the max number of times with no reply -> let the topic rest this
                    # run: no new question (and therefore no nudge) until the user reacts on their own.
                    prompt = _PROMPT_NOTHING_TODO
                elif _proactive_step <= _STEP_GROUNDED:
                    # PROACTIVE grounding (ONE pass, NOT forced): offer ONE suggestion ONLY if the REAL
                    # memories genuinely support it, ELSE defer to the fact-free get-to-know question. We do
                    # NOT set force_tool_choice here: forcing a fact-containing message ("you must suggest
                    # something") is exactly what pressured a strong model to INVENT a routine. The model is
                    # handed a digest of REAL memories and may memory_search ONCE itself (still read-capped).
                    # If it grounds nothing, it calls thinking_done and the next rung asks a fact-free question.
                    _digest_block = (
                        "\n\nREAL MEMORIES about the user (every fact you state must come from these; quote "
                        "the source in `details`):\n" + _proactive_digest
                    ) if _proactive_digest else ""
                    prompt = _PROMPT_PROACTIVE + _digest_block
                    _allow_search = True
                    _node = "proactive"
                    set_proactive_mode(user_scope_id, "grounded")
                    _proactive_step = _STEP_AUTOMATION_REVIEW
                elif _proactive_step <= _STEP_AUTOMATION_REVIEW and _review_findings:
                    # AUTOMATION REVIEW: the user already has automations, so stop offering new ones
                    # and offer to improve one instead. NOT forced, for the same reason the grounded
                    # rung is not: a rung that MUST produce a message is a rung that invents one when
                    # it has nothing. The findings were computed in code; the model only phrases them.
                    prompt = _PROMPT_AUTOMATION_REVIEW + _build_automation_review_digest(_review_findings)
                    _node = "automation_review"
                    set_proactive_mode(user_scope_id, "grounded")
                    _proactive_step = _STEP_RELEVANCE
                elif _proactive_step <= _STEP_RELEVANCE and _watchlist:
                    # RELEVANCE WATCH: does anything current change something the user has planned?
                    # Not forced, and falling through is the expected outcome - a rung that MUST send
                    # something sends a news digest, which is precisely what this must not become.
                    prompt = _PROMPT_RELEVANCE + _watchlist
                    _node = "relevance"
                    _allow_search = True
                    set_proactive_mode(user_scope_id, "grounded")
                    set_message_kind(user_scope_id, "relevance")
                    _proactive_step = _STEP_GETTO
                else:
                    # GET-TO-KNOW (FORCED): nothing grounded -> still ask ONE get-to-know question (no
                    # evidence-gate; a question states no fact). Always ends the run with a question.
                    prompt = _PROMPT_GET_TO_KNOW
                    if get_ask_rejects(user_scope_id) > 0:
                        # An earlier question this run was rejected by the semantic-dedup gate (too
                        # similar to a recent one). Steer the model to a genuinely different area.
                        prompt += _GET_TO_KNOW_RETRY_HINT
                    _force_tc = "required"
                    _node = "getto"
                    set_proactive_mode(user_scope_id, "open")
                    set_dedup_enforce(user_scope_id, True)
                    # ONE rung, ONE turn. The retry budget is spent inside deliver_tracked_message, where
                    # the retry actually happens (the model re-calls ask_user within this single step), so
                    # a question always lands here and the rung never has to be re-entered. Counting the
                    # retries out here instead is what let a run spin: the counter advanced once per turn
                    # while the model retried a dozen times inside one.
                    _proactive_step = _STEP_DONE
                _turn_hist_start = len(getattr(agent, "history", []) or [])
                mem_ctx = (memory_context or None) if turn == 0 else None
                agent.chat_step(
                    prompt,
                    stream_callback=None,
                    memory_context=mem_ctx,
                    thinking_mode=True,
                    force_tool_choice=_force_tc,
                    allow_memory_search=_allow_search,
                    thinking_node=_node,
                )
                current_history = (getattr(agent, "history", []) or [])
                run_history = _history_delta(current_history, run_history_start)

                # A tracked question was raised this run (ask_user / forced node) -> the run's job is DONE:
                # end NOW and wait for the user's reply. Max 1 message per run. Continuing (e.g. climbing to
                # the proactive rung) would leave the background run alive and racing the main agent on the
                # shared local model when the user replies — the cause of the 17:51 concurrency + handoff
                # race (note never marked handled). The main agent picks up the reply.
                if run_has_open_request(user_scope_id):
                    logger.info("Thinking: a question was raised this run — ending to wait for the user's reply")
                    break

                if _history_has_thinking_done(run_history):
                    _unresolved = (
                        _tledger.unresolved_items(user_scope_id, _run_ledger, _cur_seq, recent_runs=_recent_runs)
                        if (_gate_enabled and _run_ledger) else []
                    )
                    if _unresolved and not _gate_nudged:
                        # Single-shot completion gate: the model tried to finish with open housekeeping.
                        # Inject ONE targeted nudge naming the items, then let the loop continue so the
                        # model can act-or-ask. Mirrors the main loop's single-nudge task verification.
                        _gate_nudged = True
                        logger.info("Thinking: GATE nudge for %d unresolved item(s)", len(_unresolved))
                        agent.chat_step(
                            _tledger.build_gate_nudge(_unresolved),
                            stream_callback=None,
                            memory_context=None,
                            thinking_mode=True,
                            force_tool_choice="required",   # backstop: compel a decisive tool call
                            thinking_node="forced_item",    # the nudge names real open items, so the
                                                            # housekeeping block text is correct here
                        )
                        current_history = (getattr(agent, "history", []) or [])
                        run_history = _history_delta(current_history, run_history_start)
                        continue
                    if _unresolved and _gate_nudged:
                        logger.warning(
                            "Thinking: GATE incomplete run — %d item(s) still unresolved after one nudge",
                            len(_unresolved),
                        )
                        break
                    # Floor clear, but silence is NEVER the end: if the proactive flow has not yet asked the
                    # user anything (no grounded suggestion delivered, get-to-know step not done), keep going
                    # so the run ALWAYS asks ONE question (grounded suggestion or a get-to-know question).
                    if _proactive_enabled and _proactive_step < _STEP_DONE:
                        logger.info("Thinking: floor clear but proactive rung still pending (step %d of %d) - continuing", _proactive_step, _STEP_DONE)
                        continue
                    logger.info("Thinking: breaking loop (thinking_done detected, ledger clear)")
                    break

                # PROGRESS-GATE: the completion gate guards the EXIT (thinking_done); this guards against
                # spinning INSIDE the loop — gathering/analysing turn after turn without a decisive action
                # (the 15:38 run did ~10 web_search calls + reasoning, never acting). Count consecutive
                # turns with no progress tool; at the threshold, force a one-tool decision next turn.
                _turn_slice = (getattr(agent, "history", []) or [])[_turn_hist_start:]
                if _turn_used_progress_tool(_turn_slice):
                    _no_progress_turns = 0
                else:
                    _no_progress_turns += 1
                if _no_progress_turns >= _progress_threshold and not _force_decision_pending:
                    logger.warning(
                        "Thinking: PROGRESS-GATE — %d turns without a decisive tool, forcing a decision",
                        _no_progress_turns,
                    )
                    _force_decision_pending = True
                    _no_progress_turns = 0   # give the forced turn a clean slate

                # SAFETY 2: If after turn 2 agent hasn't made any tool calls at all, abort
                if turn >= 2:
                    has_any_tool_call = any(
                        isinstance(m, dict) and m.get("role") == "assistant" and m.get("tool_calls")
                        for m in run_history
                    )
                    if not has_any_tool_call:
                        logger.warning("Thinking: [SAFETY_LIMIT] no tool calls after %d turns, aborting", turn + 1)
                        break

                # SAFETY 3: Abort if user became active during this run (e.g. opened WebUI).
                # Don't check on turn 0 — the run just started and last_interaction may still
                # show the idle timestamp that triggered this run.
                if turn > 0:
                    try:
                        from vaf.core.last_interaction import _store_path as _li_path
                        lp = _li_path()
                        if lp.exists():
                            # Find newest TS across all aliases for this logical user
                            li_data = json.loads(lp.read_text(encoding="utf-8"))
                            local_admin = str(get_local_admin_scope_id()).strip()
                            my_aliases = {scope_key, "default", local_admin}
                            
                            # Find newest ts among my aliases
                            newest_li_ts = 0.0
                            for k, v in li_data.items():
                                if k in my_aliases and isinstance(v, dict):
                                    newest_li_ts = max(newest_li_ts, float(v.get("ts", 0)))
                            
                            if newest_li_ts > 0:
                                secs_since = time.time() - newest_li_ts
                                if secs_since < 60:  # User active in last 60 seconds
                                    logger.info(
                                        "Thinking: logical user became active (%ds ago), aborting run",
                                        int(secs_since),
                                    )
                                    # 🧠 INTERRUPT PERSISTENCE (Strategy B):
                                    # Save current state so we don't forget what we were doing
                                    try:
                                        from vaf.core.thinking_notes import add_note
                                        history = run_history
                                        last_turns = history[-4:] if len(history) >= 4 else history
                                        tools_called = []
                                        last_msg = ""
                                        for m in last_turns:
                                            if m.get("role") == "assistant":
                                                if m.get("tool_calls"):
                                                    for tc in m["tool_calls"]:
                                                        name = (tc.get("function") or {}).get("name") or tc.get("name") or "?"
                                                        if name not in ("thinking_done", "thinking_note_add"):
                                                            tools_called.append(name)
                                                if m.get("content") and m["content"].strip() != "Thinking...":
                                                    last_msg = m["content"].strip()[:100]
                                        
                                        summary = f"Run {run_id} unterbrochen (Turn {turn+1})."
                                        if tools_called:
                                            summary += f" Letzte Tools: {', '.join(list(set(tools_called))[:3])}."
                                        if last_msg:
                                            summary += f" Letzter Gedanke: \"{last_msg}...\""
                                        
                                        add_note(scope_key, summary)
                                        logger.info("Thinking: Context saved to notes before abort.")
                                    except Exception as _note_err:
                                        logger.debug("Thinking: Could not save abort note: %s", _note_err)
                                    break
                    except Exception as _abort_err:
                        logger.debug("Thinking abort check failed: %s", _abort_err)

            # Populate run summary from this run only (exclude preloaded session history)
            final_history = (getattr(agent, "history", []) or [])
            run_history = _history_delta(final_history, run_history_start)
            run_summary = _extract_run_summary(run_history)

            # Persist run: JSON run log (for internal summary) + vaf_think.log (for debugging)
            # NOT saved to WebUI sessions — thinking output is debug-only, visible in logs/vaf_think.log
            try:
                started_iso, ended_iso, log_messages = _save_run_log(
                    user_scope_id, run_id, started_at_ts, run_history
                )
                # Write human-readable log to logs/vaf_think.log
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
                    logger.warning("Could not write vaf_think.log: %s", log_file_err)
            except Exception as log_err:
                logger.warning("Thinking run log save failed: %s", log_err)
            # Persist run artifacts into Thinking Workspace and create a review handoff.
            try:
                from vaf.core.thinking_workspace import (
                    create_task as _ws_create_task,
                    write_workspace_file as _ws_write_file,
                    create_handoff as _ws_create_handoff,
                )

                ws_task = _ws_create_task(
                    user_scope_id=user_scope_id,
                    title=f"Thinking run {run_id}",
                    source="thinking_run",
                    description=(run_summary or "")[:300],
                )
                task_id = ws_task.get("id")
                if task_id:
                    artifact = [
                        f"# Thinking Run {run_id}",
                        "",
                        f"- scope: {scope_key}",
                        f"- status: {run_status}",
                        f"- started: {started_iso}",
                        f"- ended: {ended_iso}",
                        "",
                        "## Summary",
                        run_summary or "(no summary)",
                    ]
                    _ws_write_file(user_scope_id, task_id, "run_summary.md", "\n".join(artifact))
                    if run_summary:
                        _ws_create_handoff(
                            user_scope_id=user_scope_id,
                            task_id=task_id,
                            title=f"Review thinking proposal {run_id}",
                            content=run_summary,
                            proposed_action="review_and_approve",
                        )
            except Exception as _ws_save_err:
                logger.debug("Could not persist thinking workspace artifacts: %s", _ws_save_err)
        except Exception as e:
            logger.exception("Thinking run error for user %s: %s", scope_key[:8] if scope_key != "default" else "default", e)
            run_status = "error"
            run_summary = str(e)[:500] if str(e) else "Thinking run failed."
        finally:
            try:
                agent.shutdown()
            except Exception:
                pass
            os.environ.pop("VAF_THINKING_MODE", None)

        logger.info("Thinking completed for user %s", scope_key[:8] if scope_key != "default" else "default")
    finally:
        os.environ.pop("VAF_THINKING_MODE", None)
        os.environ.pop("VAF_THINKING_SCOPE_ID", None)
        os.environ.pop("VAF_BACKGROUND_PRO", None)   # don't leak the pro-routing flag into the main process
        os.environ.pop("VAF_THINKING_RUN_ID", None)  # don't leak the run id into the main process / next run
        # VAF_PROVIDER / VAF_MODEL_OVERRIDE are process-global: if thinking_provider/thinking_model are
        # configured, leaving them set would silently re-route the MAIN agent and EVERY other user's
        # subsequent turns to the thinking provider/model for the rest of the process lifetime.
        os.environ.pop("VAF_PROVIDER", None)
        os.environ.pop("VAF_MODEL_OVERRIDE", None)
        clear_run_evidence(user_scope_id)   # drop the proactive evidence pool + phase flag (no cross-run leak)
        clear_followup_context(user_scope_id)   # don't leak the follow-up target into a later run
        _set_last_run_completed(user_scope_id)
        try:
            from vaf.core.user_notifications import append_notification
            append_notification(
                user_scope_id,
                kind="thinking",
                title="Thinking run completed",
                status=run_status,
                summary=run_summary,
                run_id=run_id,
            )
        except Exception as notif_err:
            logger.debug("Could not append thinking notification: %s", notif_err)
        
        # 🔓 RELEASE GLOBAL LOCK
        try:
            from vaf.core.lock_manager import LockManager
            LockManager.release(f"thinking_{_key(user_scope_id)}")
        except Exception:
            pass

        release_lock(user_scope_id)


def maybe_start_thinking_for_user(user_scope_id: Optional[str]) -> bool:
    """
    If user is idle, no automation soon, and no lock: acquire lock and start thinking in a background thread.
    Returns True if a run was started.
    """
    from vaf.core.config import Config
    from vaf.core.lock_manager import LockManager
    idle_min = float(Config.get("thinking_idle_minutes", 10) or 10)
    buffer_min = int(Config.get("thinking_automation_buffer_minutes", 10) or 10)
    max_duration = int(Config.get("thinking_max_duration_minutes", 30) or 30)

    # 🔒 GLOBAL LOCK PROTECTION
    lock_id = f"thinking_{_key(user_scope_id)}"
    if LockManager.is_locked(lock_id, timeout_hours=max_duration/60.0):
        msg = f"[LOCK] Thinking mode for user '{_key(user_scope_id)}' is already running. Skipping."
        from vaf.core.log_helper import append_domain_log_always
        append_domain_log_always("backend", msg)
        logger.debug(msg)
        return False

    # Cooldown: skip if a thinking run completed recently
    cooldown_min = int(Config.get("thinking_cooldown_minutes", 110) or 110)
    mins_since = _minutes_since_last_run(user_scope_id)
    if mins_since < cooldown_min:
        logger.debug("Thinking skipped for user: cooldown (%d/%d min)", int(mins_since), cooldown_min)
        return False

    if should_skip_for_automation(user_scope_id, buffer_min):
        logger.debug("Thinking skipped for user: next automation within %d min", buffer_min)
        return False

    # Do not think while any sub-agent task is actively running.
    try:
        from vaf.core.subagent_ipc import get_ipc
        _active_tasks = get_ipc().get_active_tasks()
        if _active_tasks:
            logger.debug("Thinking skipped: %d active sub-agent task(s) running", len(_active_tasks))
            return False
    except Exception:
        pass

    # Do not think while a workflow is executing in the main process.
    # The engine sets VAF_IN_WORKFLOW_TERMINAL=1 for the duration of a run.
    import os as _os
    if _os.environ.get("VAF_IN_WORKFLOW_TERMINAL", "").strip() in ("1", "true", "yes"):
        logger.debug("Thinking skipped: workflow is currently running (VAF_IN_WORKFLOW_TERMINAL)")
        return False

    # "Idle by last message" is not enough: the main agent may still be mid-turn (a long generation /
    # multi-step tools) from an older message, so the last-interaction timestamp looks idle while a user
    # turn is actually running. Do NOT start a thinking run while the main agent is active — on ANY
    # provider (start-gate). On local this also avoids model contention; on API it avoids tangling the UI
    # with the user's live turn. The thinking run runs in its own thread and never enqueues, so this never
    # self-suppresses.
    main_provider = (Config.get("provider") or "local").strip().lower()
    t_provider = (Config.get("thinking_provider") or "inherit").strip().lower()
    both_local = (main_provider == "local") and (t_provider in ("inherit", "local"))
    # Per-user start-gate: don't start a run while THIS user's own turn is in flight (fairness — a
    # different user's busy turn must not block this user). Additionally, when the main and thinking
    # providers are the same single local model, keep a GLOBAL gate to avoid loading that one model
    # with two concurrent generations (model contention). On API/server, no global gate.
    if _main_agent_busy(user_scope_id):
        logger.debug("Thinking skipped: this user's turn in progress (both_local=%s)", both_local)
        return False
    if both_local and _any_agent_busy():
        logger.debug("Thinking skipped: shared local model busy with another turn (model contention)")
        return False

    # Acquire internal lock
    run_id = acquire_lock(user_scope_id, max_duration_minutes=max_duration)
    if run_id is None:
        logger.debug("Thinking already running for user (internal lock)")
        return False
    
    # Acquire global lock
    if not LockManager.acquire(lock_id, timeout_hours=max_duration/60.0):
        release_lock(user_scope_id)
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
    When quiet hours are enabled, no run is started during that time window (e.g. 23:00–07:00).
    """
    from vaf.core.config import Config
    if not Config.get("thinking_enabled", True):
        return
    idle_min = float(Config.get("thinking_idle_minutes", 10) or 10)
    idle_users = get_idle_user_scope_ids(idle_min)
    for scope in idle_users:
        # Quiet hours are evaluated PER USER in the user's own timezone (a Tokyo user and a
        # Berlin user get correct windows), instead of one global server-local gate.
        if is_in_quiet_hours(scope):
            logger.debug("Thinking mode skipped for %s: quiet hours", scope)
            continue
        if _process_waiting_reply(scope) == "skip":
            continue  # Waiting for reply: nudge, then allow a run once the chase ends
        if maybe_start_thinking_for_user(scope):
            break  # Start one at a time per iteration to avoid thundering herd


_background_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


def _background_loop() -> None:
    """Daemon loop: every N seconds run thinking_loop_iteration."""
    from vaf.core.config import Config
    # Startup grace period: wait before the first check so that thinking mode
    # does not fire immediately on a freshly started VAF (the last interaction
    # timestamp from a previous session would otherwise look like a long idle).
    startup_grace = max(60, int(Config.get("thinking_startup_grace_seconds", 300) or 300))
    if _stop_event.wait(timeout=startup_grace):
        return  # stopped before grace period elapsed
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


# --- Admin dashboard snapshot (Logs > Overview "Background agent" panel) ---

def scope_storage_key(user_scope_id: Any) -> str:
    """Public alias of the canonical storage key ('default' = local admin)."""
    return _key(user_scope_id)


def _last_run_overview(scope_key: str) -> Optional[Dict[str, Any]]:
    """Newest run-log overview for one storage key: end time, duration, tools used."""
    try:
        d = Platform.vaf_dir() / "thinking_mode_logs" / scope_key
        files = [p for p in d.glob("*.json")] if d.exists() else []
        if not files:
            return None
        newest = max(files, key=lambda p: p.stat().st_mtime)
        data = json.loads(newest.read_text(encoding="utf-8"))
        tools: List[str] = []
        for m in data.get("messages") or []:
            for name in m.get("tool_calls") or []:
                if name and name not in tools:
                    tools.append(str(name))
        return {
            "ended_at": data.get("ended_at"),
            "duration_s": data.get("duration_seconds"),
            "tools": tools[:12],
        }
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return None


def thinking_status_snapshot() -> Dict[str, Dict[str, Any]]:
    """Per-user snapshot of the background agent for the admin dashboard.

    Returns a dict keyed by canonical storage key (see _key; 'default' is the
    local admin). Strictly READ-ONLY: unlike get_waiting_for_reply() an
    expired waiting latch is skipped, never deleted - a status probe must not
    mutate lifecycle state. Per key:
      running / run_started_ts   active lock younger than the 30-min run bound
      waiting                    sanitized latch (question, since, channel, ...)
      minutes_since_last_run     None if this user never completed a run
      last_run                   newest run-log overview (ended_at, duration, tools)
    """
    locks = _load_locks()
    waiting = _load_waiting()
    try:
        path = _last_completed_path()
        last_completed = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(last_completed, dict):
            last_completed = {}
    except (json.JSONDecodeError, OSError):
        last_completed = {}
    keys = set(locks) | set(waiting) | set(last_completed) | set(_load_run_seq())
    logs_root = Platform.vaf_dir() / "thinking_mode_logs"
    try:
        if logs_root.exists():
            keys.update(d.name for d in logs_root.iterdir() if d.is_dir())
    except OSError:
        pass

    try:
        from vaf.core.config import Config
        ttl_h = float(Config.get("thinking_reply_wait_ttl_hours", 12) or 0)
    except Exception:
        ttl_h = 12.0
    now = time.time()
    out: Dict[str, Dict[str, Any]] = {}
    for key in keys:
        lock = locks.get(key) or {}
        started_ts = float(lock.get("started_at_ts") or 0)
        w = waiting.get(key)
        if w:
            sent_ts = float(w.get("question_sent_at_ts") or 0)
            if ttl_h > 0 and sent_ts > 0 and (now - sent_ts) > ttl_h * 3600:
                w = None  # expired: skip (read-only - the lifecycle owns deletion)
            elif not chase_is_active(w):
                # The chase is over; the record only survives so a late reply is understood. The
                # panel's line is "waiting for a reply", and that has stopped being true - the open
                # question stays visible in the same panel's request list with its 'asked' badge.
                w = None
        out[key] = {
            "running": bool(lock) and (now - started_ts) < 30 * 60,
            "run_started_ts": started_ts or None,
            "waiting": {
                "question": str(w.get("question_text") or "")[:300],
                "since_ts": float(w.get("question_sent_at_ts") or 0) or None,
                "channel": str(w.get("channel") or "web"),
                "nudged": bool(w.get("nudge_sent_at_ts")),
                "escalated": bool(w.get("escalated_to_web")),
                "username": str(w.get("username") or ""),
            } if w else None,
            "minutes_since_last_run": (
                round((now - float(last_completed[key]["completed_at_ts"])) / 60.0, 1)
                if isinstance(last_completed.get(key), dict) and last_completed[key].get("completed_at_ts")
                else None
            ),
            "last_run": _last_run_overview(key),
        }
    return out
