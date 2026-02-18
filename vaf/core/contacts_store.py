"""
Central contacts list with optional personal file per contact.
Stored per user: data_dir/contacts.json (local admin) or data_dir/users/<username>/contacts.json.
Used by the agent (list_contacts, get_contact) and by bridges for contact whitelist (allow_as_assistant_user).
"""
import json
import logging
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from vaf.core.config import get_local_admin_scope_id, get_local_admin_username
from vaf.core.platform import Platform

logger = logging.getLogger("vaf.core.contacts_store")

_LOCK = threading.Lock()


def _local_admin() -> str:
    return get_local_admin_username().lower()


def _local_admin_scope_id() -> str:
    return get_local_admin_scope_id()


def _safe_username(username: Optional[str]) -> str:
    """Return a safe username for path construction. Prevents path traversal (e.g. '../../other')."""
    u = (username or "").strip()
    # Allow only alphanumeric, underscore, hyphen; collapse any other to empty → treat as invalid
    safe = "".join(c for c in u if c.isalnum() or c in "_-")
    return safe.lower() if safe else ""


def _contacts_path(username: Optional[str] = None, user_scope_id: Optional[str] = None) -> Path:
    data_dir = Platform.data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    if user_scope_id:
        scope_str = str(user_scope_id).strip()
        if scope_str == _local_admin_scope_id():
            return data_dir / "contacts.json"
        scope_dir = data_dir / "scopes" / scope_str
        scope_dir.mkdir(parents=True, exist_ok=True)
        return scope_dir / "contacts.json"
    u = _safe_username(username)
    if not u or u == _local_admin():
        return data_dir / "contacts.json"
    user_dir = data_dir / "users" / u
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir / "contacts.json"


CHANNEL_TYPES = ("phone", "whatsapp", "telegram", "email", "discord")


def _contact_ensure_channels(c: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure contact has a 'channels' list; derive from legacy fields if missing. Returns a copy."""
    out = dict(c)
    if "channels" in out and isinstance(out["channels"], list) and len(out["channels"]) > 0:
        return out
    channels: List[Dict[str, str]] = []
    if out.get("whatsapp_phone"):
        channels.append({"type": "phone", "value": (out.get("whatsapp_phone") or "").strip()})
    if out.get("telegram_user_id"):
        channels.append({"type": "telegram", "value": (out.get("telegram_user_id") or "").strip()})
    if out.get("telegram_username"):
        channels.append({"type": "telegram", "value": (out.get("telegram_username") or "").strip()})
    if out.get("email"):
        channels.append({"type": "email", "value": (out.get("email") or "").strip()})
    out["channels"] = channels
    return out


def _contact_whatsapp_values(c: Dict[str, Any]) -> List[str]:
    """Return all WhatsApp phone values for this contact. Includes type 'whatsapp' and 'phone' (phone is used as WhatsApp)."""
    c = _contact_ensure_channels(c)
    return [ch["value"] for ch in (c.get("channels") or []) if ch.get("value") and ch.get("type") in ("whatsapp", "phone")]


def _contact_telegram_values(c: Dict[str, Any]) -> List[str]:
    """Return all Telegram values (user_id or username) for this contact."""
    c = _contact_ensure_channels(c)
    return [ch["value"] for ch in (c.get("channels") or []) if ch.get("type") == "telegram" and ch.get("value")]


def _contact_email_values(c: Dict[str, Any]) -> List[str]:
    """Return all email values for this contact."""
    c = _contact_ensure_channels(c)
    return [ch["value"] for ch in (c.get("channels") or []) if ch.get("type") == "email" and ch.get("value")]


def _sync_legacy_from_channels(contact: Dict[str, Any]) -> None:
    """In-place: set legacy fields from first of each channel type (for bridge backward compat). Phone counts as WhatsApp."""
    channels = contact.get("channels") or []
    contact["whatsapp_phone"] = next(
        (ch["value"] for ch in channels if ch.get("value") and ch.get("type") in ("whatsapp", "phone")),
        None,
    )
    contact["telegram_user_id"] = next((ch["value"] for ch in channels if ch.get("type") == "telegram" and (ch.get("value") or "").strip().isdigit()), None)
    contact["telegram_username"] = next((ch["value"] for ch in channels if ch.get("type") == "telegram" and (ch.get("value") or "").strip().startswith("@")), None)
    contact["email"] = next((ch["value"] for ch in channels if ch.get("type") == "email" and ch.get("value")), None)


def _load_all(username: Optional[str] = None, user_scope_id: Optional[str] = None) -> List[Dict[str, Any]]:
    path = _contacts_path(username, user_scope_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            raw = data
        elif isinstance(data, dict) and "contacts" in data:
            raw = data["contacts"] if isinstance(data["contacts"], list) else []
        else:
            raw = []
        return [_contact_ensure_channels(c) for c in raw]
    except Exception as e:
        logger.warning("contacts_store load failed: %s", e)
        return []


def _save_all(contacts: List[Dict[str, Any]], username: Optional[str] = None, user_scope_id: Optional[str] = None) -> None:
    path = _contacts_path(username, user_scope_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(contacts, indent=2), encoding="utf-8")


def list_contacts(username: Optional[str] = None, user_scope_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return all contacts for the user. Each contact has id, name, channels, personal file fields.
    Isolation: data is stored per username or user_scope_id (local admin: contacts.json; others: users/<username>/ or scopes/<user_scope_id>/contacts.json)."""
    with _LOCK:
        return list(_load_all(username, user_scope_id))


def get_contact_by_id(contact_id: str, username: Optional[str] = None, user_scope_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Return one contact by id, or None."""
    with _LOCK:
        for c in _load_all(username, user_scope_id):
            if c.get("id") == contact_id:
                return dict(c)
    return None


def get_contact_by_name(name: str, username: Optional[str] = None, user_scope_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Return first contact whose name matches (case-insensitive), or None."""
    matches = get_contacts_by_name(name, username, user_scope_id=user_scope_id)
    return matches[0] if matches else None


def get_contacts_by_name(name: str, username: Optional[str] = None, user_scope_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return all contacts whose name matches (case-insensitive). Use to detect duplicates."""
    name_clean = (name or "").strip()
    if not name_clean:
        return []
    with _LOCK:
        return [dict(c) for c in _load_all(username, user_scope_id) if (c.get("name") or "").strip().lower() == name_clean.lower()]


def _normalize_phone_for_match(value: str) -> str:
    """Return digits only (for JID or E.164 comparison)."""
    return "".join(c for c in (value or "") if c.isdigit())


def get_contact_by_telegram_user_id(telegram_user_id: str, username: Optional[str] = None, user_scope_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Return the contact who has this telegram_user_id and allow_as_assistant_user=True, or None."""
    tid = (telegram_user_id or "").strip()
    if not tid:
        return None
    with _LOCK:
        for c in _load_all(username, user_scope_id):
            if not c.get("allow_as_assistant_user"):
                continue
            for val in _contact_telegram_values(c):
                if (val or "").strip() == tid:
                    return _contact_ensure_channels(dict(c))
    return None


def get_contact_by_whatsapp_phone(whatsapp_jid_or_phone: str, username: Optional[str] = None, user_scope_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Return the contact who has this WhatsApp number (JID or E.164) and allow_as_assistant_user=True, or None."""
    raw = (whatsapp_jid_or_phone or "").strip()
    if not raw:
        return None
    norm = _normalize_phone_for_match(raw.split("@")[0] if "@" in raw else raw)
    if not norm:
        return None
    with _LOCK:
        for c in _load_all(username, user_scope_id):
            if not c.get("allow_as_assistant_user"):
                continue
            for p in _contact_whatsapp_values(c):
                if _normalize_phone_for_match(p) == norm:
                    return _contact_ensure_channels(dict(c))
    return None


def _normalize_channels(channels: Any) -> List[Dict[str, str]]:
    """Validate and return list of {type, value}. Drops invalid entries."""
    if not isinstance(channels, list):
        return []
    out: List[Dict[str, str]] = []
    for ch in channels:
        if not isinstance(ch, dict):
            continue
        t = (ch.get("type") or "").strip().lower()
        v = (ch.get("value") or "").strip()
        if t in CHANNEL_TYPES and v:
            out.append({"type": t, "value": v})
    return out


def create_contact(
    name: str,
    username: Optional[str] = None,
    *,
    user_scope_id: Optional[str] = None,
    channels: Optional[List[Dict[str, str]]] = None,
    whatsapp_phone: Optional[str] = None,
    telegram_username: Optional[str] = None,
    telegram_user_id: Optional[str] = None,
    email: Optional[str] = None,
    preferred_language: Optional[str] = None,
    how_to_address: Optional[str] = None,
    birthday: Optional[str] = None,
    notes: Optional[str] = None,
    allow_as_assistant_user: bool = False,
) -> Dict[str, Any]:
    """Create a contact and return it with id. Use channels (list of {type, value}) and/or legacy fields."""
    ch_list = _normalize_channels(channels) if channels else []
    if not ch_list:
        if (whatsapp_phone or "").strip():
            ch_list.append({"type": "whatsapp", "value": (whatsapp_phone or "").strip()})
        if (telegram_user_id or "").strip():
            ch_list.append({"type": "telegram", "value": (telegram_user_id or "").strip()})
        if (telegram_username or "").strip():
            ch_list.append({"type": "telegram", "value": (telegram_username or "").strip()})
        if (email or "").strip():
            ch_list.append({"type": "email", "value": (email or "").strip()})
    contact: Dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "name": (name or "").strip(),
        "channels": ch_list,
        "whatsapp_phone": None,
        "telegram_username": None,
        "telegram_user_id": None,
        "email": None,
        "preferred_language": (preferred_language or "").strip() or None,
        "how_to_address": (how_to_address or "").strip() or None,
        "birthday": (birthday or "").strip() or None,
        "notes": (notes or "").strip() or None,
        "allow_as_assistant_user": bool(allow_as_assistant_user),
    }
    _sync_legacy_from_channels(contact)
    with _LOCK:
        contacts = _load_all(username, user_scope_id)
        contacts.append(contact)
        _save_all(contacts, username, user_scope_id)
    return _contact_ensure_channels(dict(contact))


def update_contact(
    contact_id: str,
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
    **updates: Any,
) -> Optional[Dict[str, Any]]:
    """Update contact by id. Only provided fields are updated. 'channels' = list of {type, value}. Returns updated contact or None."""
    with _LOCK:
        contacts = _load_all(username, user_scope_id)
        for i, c in enumerate(contacts):
            if c.get("id") == contact_id:
                allowed = {
                    "name", "channels", "whatsapp_phone", "telegram_username", "telegram_user_id", "email",
                    "preferred_language", "how_to_address", "birthday", "notes", "allow_as_assistant_user",
                }
                for k, v in updates.items():
                    if k not in allowed:
                        continue
                    if k == "allow_as_assistant_user":
                        contacts[i][k] = bool(v)
                    elif k == "channels":
                        contacts[i]["channels"] = _normalize_channels(v)
                        _sync_legacy_from_channels(contacts[i])
                    elif v is None or (isinstance(v, str) and not v.strip()):
                        contacts[i][k] = None
                    else:
                        contacts[i][k] = v.strip() if isinstance(v, str) else v
                if "channels" in updates:
                    _sync_legacy_from_channels(contacts[i])
                _save_all(contacts, username, user_scope_id)
                return _contact_ensure_channels(dict(contacts[i]))
    return None


def delete_contact(contact_id: str, username: Optional[str] = None, user_scope_id: Optional[str] = None) -> bool:
    """Delete contact by id. Returns True if deleted."""
    with _LOCK:
        contacts = _load_all(username, user_scope_id)
        new_list = [c for c in contacts if c.get("id") != contact_id]
        if len(new_list) == len(contacts):
            return False
        _save_all(new_list, username, user_scope_id)
        return True


def get_contacts_allowing_assistant(username: Optional[str] = None, user_scope_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return contacts with allow_as_assistant_user=True, for bridge whitelist checks."""
    with _LOCK:
        return [dict(c) for c in _load_all(username, user_scope_id) if c.get("allow_as_assistant_user")]
