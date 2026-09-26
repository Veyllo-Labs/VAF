# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Forget a credential that came through a chat, everywhere VAF keeps that chat.

A person gives the agent a password in the chat - in the browser, in the terminal, over
Telegram - and the agent stores it (the store_credential tool, vaf/core/user_secrets.py). From
then on the value must not stay where the chat is kept: measured before this existed, one
password pasted into a chat travelled with every later turn and sat in the saved chat, the
stored intent, the context archives and the channel store.

What this does, and the one rule it keeps: NOTHING IS DELETED. A message that carried the
value keeps its place, its role and its tool-call pairing; only the value inside it becomes a
placeholder, `[VAF_SECRET_FTP_PASS]`. A history with a missing message breaks the provider's
contract (every tool call must be answered, turns must alternate), so a deletion would trade a
leak for a broken chat.

The sinks, in the order a value reaches them:

- the running agent's history, at once for everything the provider does not replay verbatim;
  Anthropic's raw assistant blocks (signed thinking) are left alone for the rest of the turn and
  dropped where they carry the value when the next turn starts (`forget_in_history`), because a
  changed signed block is refused and a missing one in the live turn too. Until then they are
  in this process's memory only: the save and the memory compaction scrub on their own;
- the agent's context manager (intent, state, the snapshots it archived) and the per-chat main
  persistence (the stored intent, working memory);
- every later save of the chat (`SessionManager.save` asks `session_env`), which is what
  catches the person's message: the runner writes it when the turn ends;
- the last-interaction preview and the channel store;
- the debug logs of the last two days (`scrub_recent_logs`);
- whatever a lane registered with `add_listener`: the terminal's input history, the
  word-suggestion corpus, the Telegram bridge (which deletes the person's message there), the
  web chat (which drops its cached copy).

NAMED BOUNDARIES - where the value stays, stated so nobody promises more:
- the model provider received the turn;
- WhatsApp and Discord keep the message on the platform (a bot cannot delete a person's
  message there); Telegram can, within 48 hours;
- the tray log, which a logging handler holds open (it carries voice transcripts, not typed
  text), and a terminal's scrollback; the debug logs of the day and the day before ARE scrubbed
  (`scrub_recent_logs`);
- context archives written by another agent process, which record no chat to find them by.

A value shorter than user_secrets.MIN_SCRUB_LENGTH is not replaced: replacing every "abc" in a
chat would wreck the chat and protect nothing.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from vaf.core.user_secrets import MIN_SCRUB_LENGTH

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_by_session: Dict[str, Dict[str, str]] = {}
_listeners: List[Callable[..., None]] = []


# ── the replacement ─────────────────────────────────────────────────────────────

def forms(value: str) -> List[str]:
    """The value as it appears in text, and as it appears inside one and two levels of JSON
    (a tool call's arguments are a JSON string, stored inside the chat's JSON)."""
    once = json.dumps(value, ensure_ascii=False)[1:-1]
    twice = json.dumps(once, ensure_ascii=False)[1:-1]
    out: List[str] = []
    for form in (value, once, twice):
        if form not in out:
            out.append(form)
    return out


def usable(env: Dict[str, str]) -> Dict[str, str]:
    """The entries of env that can be forgotten: a name and a value long enough to find."""
    return {str(k): str(v) for k, v in (env or {}).items()
            if k and isinstance(v, str) and len(v) >= MIN_SCRUB_LENGTH}


def scrub_text(text: Any, env: Dict[str, str]) -> Any:
    """`text` with every form of every value replaced by `[NAME]`; anything else unchanged."""
    if not isinstance(text, str) or not env:
        return text
    for name, value in sorted(env.items(), key=lambda kv: -len(kv[1])):
        for form in forms(value):
            if form in text:
                text = text.replace(form, f"[{name}]")
    return text


def contains(obj: Any, env: Dict[str, str]) -> bool:
    try:
        blob = json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        blob = str(obj)
    return any(form in blob for value in env.values() for form in forms(value))


def scrub_obj(obj: Any, env: Dict[str, str]) -> Any:
    """A copy of a JSON-shaped object with every string scrubbed."""
    if isinstance(obj, str):
        return scrub_text(obj, env)
    if isinstance(obj, dict):
        return {k: scrub_obj(v, env) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return type(obj)(scrub_obj(v, env) for v in obj)
    return obj


def scrub_messages(messages: List[Dict[str, Any]], env: Dict[str, str], *,
                   drop_replay_cache: bool) -> int:
    """Scrub OpenAI-shaped history dicts IN PLACE; returns how many changed.

    Every string a message carries - content (text or parts), tool-call arguments, reasoning -
    is scrubbed; nothing is removed, so the count and the tool-call pairing stay. The
    Anthropic replay cache (`_anthropic_blocks`) is a verbatim copy of signed blocks: it is
    never edited, and with `drop_replay_cache` it is dropped where it carries the value (the
    converter then rebuilds the message from its fields, as it does after a trimmed call)."""
    env = usable(env)
    if not env:
        return 0
    changed = 0
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        before = contains({k: v for k, v in msg.items() if k != "_anthropic_blocks"}, env)
        if before:
            for key, value in list(msg.items()):
                if key != "_anthropic_blocks":
                    msg[key] = scrub_obj(value, env)
            changed += 1
        if drop_replay_cache and "_anthropic_blocks" in msg and contains(msg["_anthropic_blocks"], env):
            del msg["_anthropic_blocks"]
            changed += 0 if before else 1
    return changed


def scrub_file(path: Path, env: Dict[str, str]) -> bool:
    """Rewrite one file with the values replaced, keeping it encrypted if it was. True when
    something was replaced. Never raises."""
    env = usable(env)
    try:
        path = Path(path)
        if not env or not path.is_file():
            return False
        from vaf.core import data_files
        raw = path.read_bytes()
        encrypted = data_files.is_encrypted(raw)
        text = (data_files.decrypt_bytes(raw) if encrypted else raw).decode("utf-8")
        new = scrub_text(text, env)
        if new == text:
            return False
        data_files.write_bytes_atomic(path, new.encode("utf-8"), encrypt=encrypted)
        return True
    except Exception as e:
        logger.warning("could not scrub %s: %s", path, e)
        return False


# ── the registry the saves read ──────────────────────────────────────────────────

def remember(session_id: Optional[str], env: Dict[str, str]) -> None:
    """From now on every save of this chat in this process replaces these values."""
    env = usable(env)
    if not session_id or not env:
        return
    with _lock:
        _by_session.setdefault(str(session_id), {}).update(env)


def session_env(session_id: Optional[str]) -> Dict[str, str]:
    """The values forgotten in this chat (process lifetime); {} for none."""
    if not session_id:
        return {}
    with _lock:
        return dict(_by_session.get(str(session_id), {}))


def add_listener(fn: Callable[..., None]) -> None:
    """A lane's own sink. Called as fn(env=..., session_id=..., user_scope_id=..., username=...)
    when a value is forgotten; `transcript_scrubbed=True` instead of env when a save of the chat
    has just replaced values (the web chat reloads its copy then). Must not raise; is isolated
    if it does."""
    with _lock:
        if fn not in _listeners:
            _listeners.append(fn)


def _notify(**kwargs) -> None:
    with _lock:
        listeners = list(_listeners)
    for fn in listeners:
        try:
            fn(**kwargs)
        except Exception as e:
            logger.warning("forget listener %s failed: %s", getattr(fn, "__name__", fn), e)


def transcript_scrubbed(session_id: str) -> None:
    """A save of this chat has just replaced a forgotten value (SessionManager.save)."""
    _notify(env=None, session_id=str(session_id), user_scope_id=None, username=None,
            transcript_scrubbed=True)


# ── the one entry point ─────────────────────────────────────────────────────────

def forget(env: Dict[str, str], *, session_id: Optional[str], user_scope_id: Optional[str] = None,
           username: Optional[str] = None, agent: Any = None) -> None:
    """Forget these values ({NAME: value}) everywhere VAF keeps this chat. Never raises."""
    env = usable(env)
    if not env:
        return
    remember(session_id, env)
    if agent is not None:
        try:
            scrub_messages(getattr(agent, "history", None) or [], env, drop_replay_cache=False)
        except Exception as e:
            logger.warning("history scrub failed: %s", e)
        for holder in ("context_manager", "main_persistence"):
            try:
                target = getattr(agent, holder, None)
                if target is not None and hasattr(target, "forget"):
                    target.forget(env)
            except Exception as e:
                logger.warning("%s scrub failed: %s", holder, e)
    try:
        from vaf.core.last_interaction import _store_path
        scrub_file(_store_path(), env)
    except Exception:
        pass
    try:
        from vaf.core.channel_message_store import scrub_values
        scrub_values(env, username=username, user_scope_id=user_scope_id)
    except Exception as e:
        logger.warning("channel store scrub failed: %s", e)
    scrub_recent_logs(env)
    _notify(env=env, session_id=session_id, user_scope_id=user_scope_id, username=username,
            transcript_scrubbed=False)


def scrub_recent_logs(env: Dict[str, str]) -> int:
    """The day's and the previous day's debug logs: they carry short previews of a message (the
    memory search query, the queue line) and the model's streamed output. Each line is written by
    opening the file anew (log_helper.append_domain_log), so a file can be replaced under it. Not
    the tray log, which a logging handler holds open, and not the timeline: it is hash-chained,
    and what could carry a credential there - a tool's arguments - is masked before it is
    written. Returns how many files changed."""
    env = usable(env)
    if not env:
        return 0
    try:
        from datetime import date, timedelta
        from vaf.core.log_helper import get_app_log_dir
        log_dir = Path(get_app_log_dir())
        days = {date.today().isoformat(), (date.today() - timedelta(days=1)).isoformat()}
        changed = 0
        for path in log_dir.glob("*.log"):
            if path.name.startswith("tray_debug") or not any(d in path.name for d in days):
                continue
            changed += scrub_file(path, env)
        return changed
    except Exception as e:
        logger.warning("log scrub failed: %s", e)
        return 0


def forget_in_history(history: List[Dict[str, Any]], session_id: Optional[str]) -> int:
    """At the start of a turn: scrub the history with everything forgotten in this chat and drop
    the replay blocks that carry it (safe once the turn that produced them is over)."""
    return scrub_messages(history, session_env(session_id), drop_replay_cache=True)
