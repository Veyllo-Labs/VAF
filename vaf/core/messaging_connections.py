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


def get_telegram_chat_id_from_whitelist(
    user_scope_id: Optional[Any],
    username: Optional[str],
) -> Optional[str]:
    """
    Resolve Telegram chat_id from the Telegram whitelist (connected bot config).
    For private chats (DM), chat_id equals telegram_user_id, so we can use the whitelist
    to reach the user even if they have not sent a message yet.
    Returns telegram_user_id as string, or None if no matching whitelist entry.
    """
    telegram_config = Config.get("telegram_config") or {}
    if not isinstance(telegram_config, dict) or not telegram_config.get("whitelist"):
        return None
    whitelist = telegram_config.get("whitelist") or []
    scope_str = str(user_scope_id) if user_scope_id is not None else None
    vaf_username = (username or "").strip() or "admin"
    for entry in whitelist:
        if not isinstance(entry, dict):
            continue
        if scope_str and str(entry.get("user_scope_id")) == scope_str:
            tid = entry.get("telegram_user_id")
            return str(tid) if tid is not None else None
        if entry.get("vaf_username") == vaf_username:
            tid = entry.get("telegram_user_id")
            return str(tid) if tid is not None else None
    return None


def get_telegram_chat_id(
    user_scope_id: Optional[Any],
    username: Optional[str],
) -> Optional[str]:
    """
    Return telegram_chat_id for this user. Used by send_telegram tool.
    Lookup order: 1) persisted endpoints (from past Telegram message), 2) Telegram whitelist
    (for private chats, chat_id = telegram_user_id, so we can resolve from whitelist).
    """
    with _ENDPOINTS_LOCK:
        data = _load_endpoints()
        if user_scope_id is not None:
            cid = data["by_scope"].get(str(user_scope_id))
            if cid:
                return cid
        uname = (username or "").strip() or "admin"
        cid = data["by_username"].get(uname)
        if cid:
            return cid
    # Fallback: resolve from Telegram whitelist (DM chat_id = telegram_user_id)
    chat_id = get_telegram_chat_id_from_whitelist(user_scope_id, username)
    if chat_id:
        save_telegram_chat_id(user_scope_id, username, chat_id)
    return chat_id


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
