# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Resolve which messaging channels (Telegram, Discord, Slack) are available for the current user
and their preferred channel for proactive messages (main_messenger from user_identity.json).

Used by the system prompt to inform the agent and by send_telegram / send_discord / send_slack tools.

Also persists and resolves user -> telegram_chat_id for proactive Telegram sends
(messaging_endpoints.json under Platform.data_dir()).
"""
import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from vaf.core.config import Config
from vaf.core.platform import Platform

# ── Channel registry (single source of truth) ────────────────────────────────
# The messaging platforms VAF knows. Copies of this list exist in schema enums,
# dispatch maps and guard tuples across the codebase; tests/test_channel_registry_sync.py
# fails when one drifts. When adding a platform, extend HERE first, then follow
# the checklist in docs/integrations/CONNECTIONS.md (Channel model).
KNOWN_CHANNELS = ("telegram", "whatsapp", "discord", "slack")
# Channels send_to_main_messenger can actually dispatch to today (Slack has no
# bridge yet, so it is known but not routable).
ROUTABLE_CHANNELS = ("telegram", "whatsapp", "discord")
# Channel -> per-platform send tool (interactive, explicit-platform lane).
CHANNEL_SEND_TOOLS = {ch: f"send_{ch}" for ch in KNOWN_CHANNELS}

_ENDPOINTS_LOCK = threading.Lock()
_ENDPOINTS_FILE = None


def _endpoints_path() -> Path:
    global _ENDPOINTS_FILE
    if _ENDPOINTS_FILE is None:
        _ENDPOINTS_FILE = Platform.data_dir() / "messaging_endpoints.json"
    return _ENDPOINTS_FILE


def _load_endpoints() -> Dict[str, Any]:
    path = _endpoints_path()
    if not path.exists():
        return {"by_scope": {}, "by_username": {}, "whatsapp_by_scope": {}, "whatsapp_by_username": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {
            "by_scope": data.get("by_scope") or {},
            "by_username": data.get("by_username") or {},
            "whatsapp_by_scope": data.get("whatsapp_by_scope") or {},
            "whatsapp_by_username": data.get("whatsapp_by_username") or {},
        }
    except Exception:
        return {"by_scope": {}, "by_username": {}, "whatsapp_by_scope": {}, "whatsapp_by_username": {}}


def _save_endpoints(data: Dict[str, Any]) -> None:
    path = _endpoints_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def save_telegram_chat_id(
    user_scope_id: Optional[Any],
    username: Optional[str],
    chat_id: str,
) -> None:
    """Persist telegram_chat_id for this user (by scope and username). Called from Telegram bridge when a message is received."""
    if not chat_id:
        return
    with _ENDPOINTS_LOCK:
        data = _load_endpoints()
        if user_scope_id is not None:
            data["by_scope"][str(user_scope_id)] = chat_id
        uname = (username or "").strip() or "admin"
        data["by_username"][uname] = chat_id
        _save_endpoints(data)


def _telegram_whitelist_entry_matches(
    entry: Dict[str, Any],
    scope_str: Optional[str],
    vaf_username: str,
) -> bool:
    """True if this whitelist entry matches the request (loose: case-insensitive username, normalized scope)."""
    if not isinstance(entry, dict):
        return False
    entry_scope = entry.get("user_scope_id")
    entry_scope_str = str(entry_scope).strip() if entry_scope is not None else None
    entry_name = (entry.get("vaf_username") or "").strip() or "admin"
    scope_ok = (not scope_str and not entry_scope_str) or (
        scope_str and entry_scope_str and scope_str.strip() == entry_scope_str
    )
    name_ok = (vaf_username or "admin").lower() == (entry_name or "admin").lower()
    if scope_str and entry_scope_str:
        return scope_ok and name_ok
    return name_ok


def get_telegram_chat_id_from_whitelist(
    user_scope_id: Optional[Any],
    username: Optional[str],
) -> Optional[str]:
    """
    Resolve Telegram chat_id from the Telegram whitelist (connected bot config).
    For private chats (DM), chat_id equals telegram_user_id.
    Matching is loose: case-insensitive username, normalized scope, so the verified
    account owner (who linked their Telegram) is found even if session identity differs slightly.
    Returns telegram_user_id as string, or None if no matching whitelist entry.
    """
    telegram_config = Config.get("telegram_config") or {}
    if not isinstance(telegram_config, dict) or not telegram_config.get("whitelist"):
        return None
    whitelist = telegram_config.get("whitelist") or []
    scope_str = str(user_scope_id).strip() if user_scope_id is not None else None
    vaf_username = (username or "").strip() or "admin"
    for entry in whitelist:
        if not isinstance(entry, dict):
            continue
        if _telegram_whitelist_entry_matches(entry, scope_str, vaf_username):
            tid = entry.get("telegram_user_id")
            return str(tid) if tid is not None else None
    return None


def get_telegram_chat_id(
    user_scope_id: Optional[Any],
    username: Optional[str],
) -> Optional[str]:
    """
    Return telegram_chat_id for this user. Used by send_telegram tool.
    Lookup: 1) persisted endpoints (from past Telegram message), 2) Telegram whitelist
    (loose match: case-insensitive username, normalized scope). The verified account owner
    (who linked their Telegram with the bot) does not need to be manually re-added to the
    whitelist – the bot recognizes them by the existing link.
    """
    uname = (username or "").strip() or "admin"
    scope_str = str(user_scope_id).strip() if user_scope_id is not None else None

    with _ENDPOINTS_LOCK:
        data = _load_endpoints()
        if user_scope_id is not None:
            cid = data["by_scope"].get(str(user_scope_id))
            if cid:
                return cid
        cid = data["by_username"].get(uname)
        if cid:
            return cid
        # Case-insensitive username lookup (session may send different casing)
        for key, val in (data.get("by_username") or {}).items():
            if (key or "").strip().lower() == uname.lower():
                return val
    # Fallback: Telegram whitelist (verified/linked user; loose match)
    chat_id = get_telegram_chat_id_from_whitelist(user_scope_id, username)
    if chat_id:
        save_telegram_chat_id(user_scope_id, username, chat_id)
        return chat_id
    # Single verified user: exactly one whitelist entry = the account that linked the bot;
    # they don't need to be manually on the whitelist – match them loosely.
    telegram_config = Config.get("telegram_config") or {}
    if isinstance(telegram_config, dict):
        whitelist = telegram_config.get("whitelist") or []
        if len(whitelist) == 1 and isinstance(whitelist[0], dict):
            entry = whitelist[0]
            if _telegram_whitelist_entry_matches(entry, scope_str, uname):
                tid = entry.get("telegram_user_id")
                chat_id = str(tid) if tid is not None else None
                if chat_id:
                    save_telegram_chat_id(user_scope_id, username, chat_id)
                    return chat_id
    return None


def save_whatsapp_chat_jid(
    user_scope_id: Optional[Any],
    username: Optional[str],
    chat_jid: str,
) -> None:
    """Persist WhatsApp chat JID for this user. Called from WhatsApp bridge when a message is received."""
    if not chat_jid:
        return
    with _ENDPOINTS_LOCK:
        data = _load_endpoints()
        if user_scope_id is not None:
            data["whatsapp_by_scope"][str(user_scope_id)] = chat_jid
        uname = (username or "").strip() or "admin"
        data["whatsapp_by_username"][uname] = chat_jid
        _save_endpoints(data)


def get_whatsapp_chat_jid_from_whitelist(
    user_scope_id: Optional[Any],
    username: Optional[str],
) -> Optional[str]:
    """
    Resolve WhatsApp JID from the whitelist (phone_number in E.164 maps to user).
    For proactive sends we need the user's WhatsApp JID - typically obtained when they first message us.
    Returns JID string if whitelist has phone_number for this user (we construct JID from it), or None.
    Note: We prefer persisted endpoints from actual message; this is fallback when user hasn't messaged yet.
    """
    whatsapp_config = Config.get("whatsapp_config") or {}
    if not isinstance(whatsapp_config, dict) or not whatsapp_config.get("whitelist"):
        return None
    whitelist = whatsapp_config.get("whitelist") or []
    scope_str = str(user_scope_id) if user_scope_id is not None else None
    vaf_username = (username or "").strip() or "admin"
    for entry in whitelist:
        if not isinstance(entry, dict):
            continue
        if scope_str and str(entry.get("user_scope_id")) == scope_str:
            phone = entry.get("phone_number")
            if phone:
                return _e164_to_jid(str(phone))
        if entry.get("vaf_username") == vaf_username:
            phone = entry.get("phone_number")
            if phone:
                return _e164_to_jid(str(phone))
    return None


def _e164_to_jid(phone: str) -> str:
    """Convert E.164 phone number to WhatsApp JID (e.g. 49123456789@s.whatsapp.net)."""
    digits = "".join(c for c in phone if c.isdigit())
    if digits.startswith("0"):
        digits = digits[1:]
    return f"{digits}@s.whatsapp.net"


def get_whatsapp_chat_jid(
    user_scope_id: Optional[Any],
    username: Optional[str],
) -> Optional[str]:
    """
    Return WhatsApp chat JID for this user. Used by send_whatsapp tool.
    Lookup order: 1) persisted endpoints, 2) whitelist (phone_number -> JID).
    """
    with _ENDPOINTS_LOCK:
        data = _load_endpoints()
        if user_scope_id is not None:
            jid = data["whatsapp_by_scope"].get(str(user_scope_id))
            if jid:
                return jid
        uname = (username or "").strip() or "admin"
        jid = data["whatsapp_by_username"].get(uname)
        if jid:
            return jid
    jid = get_whatsapp_chat_jid_from_whitelist(user_scope_id, username)
    if jid:
        save_whatsapp_chat_jid(user_scope_id, username, jid)
    return jid


def get_discord_user_id(
    user_scope_id: Optional[Any],
    username: Optional[str],
) -> Optional[str]:
    """
    Return Discord user ID for proactive DM sends.
    Currently single-admin: uses discord_config.admin_user_id when Discord is configured.
    """
    discord_config = Config.get("discord_config") or {}
    if not isinstance(discord_config, dict):
        return None
    if not discord_config.get("enabled") or not discord_config.get("verified"):
        return None
    return (discord_config.get("admin_user_id") or "").strip() or None


def get_messaging_connections(
    username: Optional[str] = None,
    user_scope_id: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Return available messaging channels and the user's preferred channel for proactive messages.

    Args:
        username: Current user's username (for user_identity.main_messenger and Telegram whitelist match).
        user_scope_id: Current user's scope ID (for Telegram whitelist match).

    Returns:
        {
            "available": ["telegram", "discord"],  # lowercase, ordered
            "main_messenger": "telegram" | None     # from user_identity.main_messenger if valid
        }
    """
    available: List[str] = []
    main_messenger: Optional[str] = None

    # Telegram: enabled + verified + user has a whitelist entry
    telegram_config = Config.get("telegram_config") or {}
    if isinstance(telegram_config, dict):
        if telegram_config.get("enabled") and telegram_config.get("verified") and telegram_config.get("bot_token"):
            whitelist = telegram_config.get("whitelist") or []
            scope_str = str(user_scope_id) if user_scope_id is not None else None
            vaf_username = (username or "").strip() or "admin"
            for entry in whitelist:
                if not isinstance(entry, dict):
                    continue
                if scope_str and str(entry.get("user_scope_id")) == scope_str:
                    available.append("telegram")
                    break
                if entry.get("vaf_username") == vaf_username:
                    available.append("telegram")
                    break

    # Discord: enabled + verified (single admin per instance for now)
    discord_config = Config.get("discord_config") or {}
    if isinstance(discord_config, dict):
        if discord_config.get("enabled") and discord_config.get("verified"):
            available.append("discord")

    # WhatsApp: enabled + user has linked auth (creds exist) + whitelist entry
    whatsapp_config = Config.get("whatsapp_config") or {}
    if isinstance(whatsapp_config, dict) and whatsapp_config.get("enabled"):
        try:
            from vaf.core.whatsapp_auth import whatsapp_auth_exists
            vaf_username = (username or "").strip() or "admin"
            if whatsapp_auth_exists(vaf_username):
                whitelist = whatsapp_config.get("whitelist") or []
                scope_str = str(user_scope_id) if user_scope_id is not None else None
                for entry in whitelist:
                    if not isinstance(entry, dict):
                        continue
                    if scope_str and str(entry.get("user_scope_id")) == scope_str:
                        available.append("whatsapp")
                        break
                    if entry.get("vaf_username") == vaf_username:
                        available.append("whatsapp")
                        break
        except Exception:
            pass

    # Slack: not yet configured in config; placeholder for future
    # if Config.get("slack_config", {}).get("enabled"): available.append("slack")

    # Deduplicate and keep order
    seen = set()
    ordered: List[str] = []
    for ch in available:
        if ch not in seen:
            seen.add(ch)
            ordered.append(ch)
    available = ordered

    # main_messenger from user_identity
    if username:
        try:
            from vaf.auth.user_workspace import get_user_workspace
            ws = get_user_workspace(username)
            ui = ws.get_user_identity()
            val = (ui.get("main_messenger") or "").strip().lower()
            if val in ("telegram", "discord", "slack", "whatsapp", "email"):
                main_messenger = val
        except Exception:
            pass

    return {"available": available, "main_messenger": main_messenger}


def _record_outbound(
    channel: str,
    endpoint: str,
    text: str,
    username: Optional[str],
    user_scope_id: Optional[Any],
    file_path: Optional[str] = None,
) -> None:
    """Mirror a router-delivered outbound message into the channel session history
    (and, where the bridge does not do it itself, the channel message store).

    The per-platform send TOOLS record their own sends; the router path
    (automation result push, send_to_user, ...) previously delivered without any
    trace, so the channel main agent lacked its own last message when the user
    replied to it (live 2026-07-14: the agent could not know which "Timer" the
    user meant and confabulated). Best-effort: never raises, never blocks a send.
    """
    uname = (username or "admin").strip() or "admin"
    if channel == "whatsapp":
        session_id = f"whatsapp_{uname}_{(endpoint.split('@', 1)[0] or 'self')}"
    else:
        session_id = f"{channel}_{endpoint}"
    try:
        from vaf.core.session import SessionManager, Session
        sm = SessionManager()
        try:
            session = sm.load(session_id, restore_state=False)
        except FileNotFoundError:
            session = Session(id=session_id, name=f"{channel.capitalize()} {endpoint}")
        session.add_message(role="assistant", content=text)
        sm.save(session, sync_state=False)
    except Exception:
        pass
    # Channel store: the WhatsApp bridge already records every outbound send
    # itself (whatsapp_bridge sender loop) - recording here again would duplicate
    # entries for read_/find_ tools.
    if channel != "whatsapp":
        try:
            from vaf.core.channel_message_store import append_message
            append_message(
                username=uname, chat_id=str(endpoint), body=text, direction="out",
                content_type=("document" if file_path else "text"),
                channel=channel, user_scope_id=user_scope_id,
            )
        except Exception:
            pass


def send_to_main_messenger(
    user_scope_id: Optional[Any],
    username: Optional[str],
    text: str,
    file_path: Optional[str] = None,
    record: bool = True,
) -> "tuple[bool, Optional[str]]":
    """Send ``text`` to the user's configured ``main_messenger`` (Telegram/WhatsApp/Discord).

    Single source of truth for "reach the user on their main channel", reused by the thinking-mode
    nudge, the proactive-question delivery AND proactive automation results. Returns ``(sent, channel)``:
      * ``(True, "telegram"|"whatsapp"|"discord")`` on success,
      * ``(False, None)`` when no main_messenger is configured, the channel id is missing, or the
        send fails.
    Never raises. (E-mail is intentionally NOT a valid main_messenger here.)

    ``file_path`` (optional): when given and the file exists, the text is sent as a normal message
    and the file is delivered as a *separate* attachment with a short caption. Sending the file
    separately (rather than as the text's caption) avoids the per-channel caption length limit
    (Telegram 1024 chars) so the full text AND the file always arrive. The attachment is
    best-effort: overall success is decided by the text send, so a too-large/failed attachment
    never reports the whole delivery as failed (the Web UI still carries the file link).

    ``record`` (default True): mirror the delivered text into the channel session history
    so the channel main agent has context when the user replies to it. Thinking-mode
    callers pass ``record=False`` - their tracked requests are reconstructed scope-keyed
    at reply time, and a session append would duplicate the question in context.
    """
    text = (text or "").strip()
    if not text:
        return False, None

    import os as _os
    attach = file_path if (file_path and _os.path.isfile(file_path)) else None
    caption = ("\U0001F4CE " + _os.path.basename(attach)) if attach else ""

    try:
        conn = get_messaging_connections(
            username=(username or "admin").strip() or "admin", user_scope_id=user_scope_id
        )
        main = (conn.get("main_messenger") or "").strip().lower()
        if main == "telegram":
            chat_id = get_telegram_chat_id(user_scope_id, username)
            if chat_id:
                from vaf.core.telegram_reply import send_telegram_reply
                if send_telegram_reply(chat_id, text):
                    if attach:
                        try:
                            send_telegram_reply(chat_id, caption, file_path=attach)
                        except Exception:
                            pass
                    if record:
                        _record_outbound("telegram", str(chat_id), text, username, user_scope_id, attach)
                    return True, "telegram"
        elif main == "whatsapp":
            jid = get_whatsapp_chat_jid(user_scope_id, username)
            if jid:
                from vaf.core.whatsapp_reply import send_whatsapp_reply
                # send_whatsapp_reply returns False when the bridge is down (callback unset), so a dead
                # bridge correctly degrades to (False, None) -> the caller falls back to the Web UI,
                # instead of falsely reporting success and silently swallowing the message.
                if send_whatsapp_reply((username or "admin"), jid, text, user_scope_id=user_scope_id):
                    if attach:
                        try:
                            from vaf.api.whatsapp_bridge import send_whatsapp_with_confirmation
                            send_whatsapp_with_confirmation(
                                (username or "admin"), jid, caption, document_path=attach
                            )
                        except Exception:
                            pass
                    if record:
                        _record_outbound("whatsapp", str(jid), text, username, user_scope_id, attach)
                    return True, "whatsapp"
        elif main == "discord":
            user_id = get_discord_user_id(user_scope_id, username)
            if user_id:
                discord_config = Config.get("discord_config") or {}
                bot_token = (discord_config.get("bot_token") or "").strip()
                if bot_token:
                    from vaf.core.discord_send import send_discord_dm
                    if send_discord_dm(bot_token, user_id, text, chunk=True):
                        if attach:
                            try:
                                send_discord_dm(bot_token, user_id, caption, file_path=attach)
                            except Exception:
                                pass
                        if record:
                            _record_outbound("discord", str(user_id), text, username, user_scope_id, attach)
                        return True, "discord"
    except Exception:
        pass
    return False, None


def get_contact_whitelist_telegram_entry(telegram_user_id: str) -> Optional[Dict[str, Any]]:
    """
    If telegram_user_id is in any user's contact list with allow_as_assistant_user=True,
    return an entry dict like Telegram whitelist: user_scope_id, vaf_username, telegram_user_id.
    Used by Telegram bridge to allow contacts as assistant users.
    """
    telegram_config = Config.get("telegram_config") or {}
    if not isinstance(telegram_config, dict):
        return None
    whitelist = telegram_config.get("whitelist") or []
    seen: set = set()
    try:
        from vaf.core.contacts_store import get_contacts_allowing_assistant, _contact_telegram_values
    except Exception:
        return None
    for entry in whitelist:
        if not isinstance(entry, dict):
            continue
        scope = entry.get("user_scope_id")
        uname = (entry.get("vaf_username") or "admin").strip()
        key = (str(scope), uname)
        if key in seen:
            continue
        seen.add(key)
        for c in get_contacts_allowing_assistant(uname):
            for val in _contact_telegram_values(c):
                if (val or "").strip() == str(telegram_user_id).strip():
                    return {
                    "user_scope_id": scope,
                    "vaf_username": uname,
                    "telegram_user_id": str(telegram_user_id),
                    "from_contact": True,  # So bridge/headless can treat as front-office (not the account owner)
                }
    return None
