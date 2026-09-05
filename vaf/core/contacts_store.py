# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
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
from typing import Any, Dict, List, Optional, Set

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


def _normalize_scope(scope: Any) -> str:
    """Canonical string for scope (UUID normalized so different string formats match)."""
    if scope is None:
        return ""
    s = str(scope).strip()
    if not s:
        return ""
    try:
        return str(uuid.UUID(s))
    except (ValueError, TypeError):
        return s


def _contacts_path(username: Optional[str] = None, user_scope_id: Optional[str] = None) -> Path:
    data_dir = Platform.data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    if user_scope_id:
        scope_str = str(user_scope_id).strip()
        if _normalize_scope(scope_str) == _normalize_scope(_local_admin_scope_id()):
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


def _contacts_path_candidates(username: Optional[str] = None, user_scope_id: Optional[str] = None) -> List[Path]:
    """Return candidate paths to try (primary first, then fallbacks) so we find contacts whether saved by scope or username."""
    data_dir = Platform.data_dir()
    primary = _contacts_path(username, user_scope_id)
    candidates = [primary]
    if user_scope_id:
        scope_str = str(user_scope_id).strip()
        alt = data_dir / "scopes" / scope_str / "contacts.json"
        if alt != primary and alt not in candidates:
            candidates.append(alt)
        try:
            canonical = str(uuid.UUID(scope_str))
            if canonical != scope_str:
                alt2 = data_dir / "scopes" / canonical / "contacts.json"
                if alt2 not in candidates:
                    candidates.append(alt2)
        except (ValueError, TypeError):
            pass
    if username:
        u = _safe_username(username)
        if u and u != _local_admin():
            alt_user = data_dir / "users" / u / "contacts.json"
            if alt_user not in candidates:
                candidates.append(alt_user)
    # The local admin's book is a candidate for the local admin only. Deliberate: _load_all
    # walks on past an empty or missing file, so with the admin path in every caller's list
    # a tenant without contacts read the admin's whole book, and the next write copied it
    # into the tenant's own file.
    if _is_local_admin_caller(username, user_scope_id) and data_dir / "contacts.json" not in candidates:
        candidates.append(data_dir / "contacts.json")
    return candidates


def _is_local_admin_caller(username: Optional[str], user_scope_id: Optional[str]) -> bool:
    """Whether this identity is the machine's local admin: by scope when a scope is given,
    by username otherwise (an empty username has always meant the local admin here)."""
    if user_scope_id:
        return _normalize_scope(user_scope_id) == _normalize_scope(_local_admin_scope_id())
    u = _safe_username(username)
    return not u or u == _local_admin()


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
    """Load contacts; try candidate paths (scope, username, local) so we find them regardless of save path."""
    for path in _contacts_path_candidates(username, user_scope_id):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                raw = data
            elif isinstance(data, dict) and "contacts" in data:
                raw = data["contacts"] if isinstance(data["contacts"], list) else []
            else:
                raw = []
            if raw:
                return [_contact_ensure_channels(c) for c in raw]
        except Exception as e:
            logger.warning("contacts_store load failed for %s: %s", path, e)
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


# JIDs that carry no phone number: a LID is an opaque id, a group or a broadcast list is not
# a person. None of them may ever be read as digits of a phone.
_NON_PHONE_JID_SUFFIXES = ("@lid", "@g.us", "@broadcast", "@status", "@newsletter")


def phone_digits_canonical(value: str) -> str:
    """The digits that identify one phone number across every notation people type and
    channels emit: "+49 176 1234567", "0176 1234567", "0049 176 1234567" and
    "491761234567:3@s.whatsapp.net" all become "491761234567".

    Rules, in order: a JID contributes only its user part (before "@", without the
    ":device" suffix), and a JID that is not a phone (LID, group, broadcast) contributes
    nothing; digits only; a leading "00" is the international prefix and is dropped; a
    trunk zero followed by 10 to 12 digits in total is read as a German national number
    (the one national convention this store knows: 089 1234567, 0151 1234567 and
    0176 12345678 are all valid there) and becomes 49..., but only when the raw value said
    nothing about the country, so "+0176..." and "0049..." are never rewritten.
    This is the ONE canonicaliser: the WhatsApp bridge, the dashboard routes and the
    contact book all match numbers through it, so a contact typed as 0176... and a chat
    stored as +49176... are the same person everywhere."""
    raw = (value or "").strip()
    if not raw:
        return ""
    if "@" in raw:
        low = raw.lower()
        if any(low.endswith(suffix) for suffix in _NON_PHONE_JID_SUFFIXES):
            return ""
        raw = raw.split("@", 1)[0]
    raw = raw.split(":", 1)[0].strip()
    digits = "".join(c for c in raw if c.isdigit())
    if not digits:
        return ""
    country_given = raw.startswith("+") or digits.startswith("00")
    if digits.startswith("00"):
        digits = digits[2:]
    if not country_given and digits.startswith("0") and 10 <= len(digits) <= 12:
        return "49" + digits[1:]
    return digits


def whatsapp_store_key(value: str) -> Optional[str]:
    """The key under which the message store and the WhatsApp dashboard file a chat with
    this number: "+" followed by the canonical digits, or None when the value is not a
    phone number (a LID, a group, fewer than 7 or more than 15 digits). The bridge writes
    inbound and outbound rows under exactly this key, so anything that wants to find a
    person's messages asks here instead of building the key itself."""
    digits = phone_digits_canonical(value)
    if not digits or len(digits) < 7 or len(digits) > 15:
        return None
    return f"+{digits}"


# The older private name; the callers that grew up with it keep working.
_phone_digits_canonical = phone_digits_canonical


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


def get_contact_name_by_phone(phone: str, username: Optional[str] = None, user_scope_id: Optional[str] = None) -> Optional[str]:
    """Return the display name of the first contact that has this phone (any channel). Used to show names in chat lists.
    Uses canonical digits (0-prefix German mobile -> 49) so +49152... matches contact 0152...."""
    norm = _phone_digits_canonical(phone or "")
    if not norm:
        return None
    with _LOCK:
        for c in _load_all(username, user_scope_id):
            for p in _contact_whatsapp_values(c):
                if _phone_digits_canonical(p) == norm:
                    name = (c.get("name") or "").strip()
                    return name if name else None
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
                    "status",
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


# ── status, notes, events: the personal file grows into a small CRM ─────────────
#
# Everything below lives INSIDE the contact record, so it inherits the store's
# isolation for free: the record sits in the file of one username or one scope
# (see _contacts_path), and no query here crosses files.

# The status is a free label; these are the suggestions a fresh contact book offers.
CONTACT_STATUS_DEFAULTS = ("lead", "in_contact", "customer", "archived")


def _find_index(contacts: List[Dict[str, Any]], contact_id: str) -> int:
    for i, c in enumerate(contacts):
        if c.get("id") == contact_id:
            return i
    return -1


def contact_status_values(username: Optional[str] = None, user_scope_id: Optional[str] = None) -> List[str]:
    """The suggestions for the status field: the defaults plus every status in use."""
    out = list(CONTACT_STATUS_DEFAULTS)
    with _LOCK:
        for c in _load_all(username, user_scope_id):
            s = (c.get("status") or "").strip()
            if s and s not in out:
                out.append(s)
    return out


def add_contact_note(
    contact_id: str,
    text: str,
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
    *,
    source: str = "user",
) -> Optional[Dict[str, Any]]:
    """Append a dated note to a contact ("interested in feature X", "follow up next week").
    `source` says who wrote it, "user" or "agent". Returns the note, None for an unknown contact."""
    import time as _time
    body = (text or "").strip()
    if not body:
        return None
    note = {"id": str(uuid.uuid4()), "ts": _time.time(), "text": body[:4000], "source": (source or "user").strip() or "user"}
    with _LOCK:
        contacts = _load_all(username, user_scope_id)
        i = _find_index(contacts, contact_id)
        if i < 0:
            return None
        log = contacts[i].get("notes_log") if isinstance(contacts[i].get("notes_log"), list) else []
        log.append(note)
        contacts[i]["notes_log"] = log[-500:]
        _save_all(contacts, username, user_scope_id)
    return note


def delete_contact_note(contact_id: str, note_id: str, username: Optional[str] = None, user_scope_id: Optional[str] = None) -> bool:
    with _LOCK:
        contacts = _load_all(username, user_scope_id)
        i = _find_index(contacts, contact_id)
        if i < 0:
            return False
        log = contacts[i].get("notes_log") if isinstance(contacts[i].get("notes_log"), list) else []
        kept = [n for n in log if n.get("id") != note_id]
        if len(kept) == len(log):
            return False
        contacts[i]["notes_log"] = kept
        _save_all(contacts, username, user_scope_id)
        return True


def add_contact_event(
    contact_id: str,
    title: str,
    when_ts: float,
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
    *,
    source: str = "user",
    note: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Attach a dated event to a contact ("meeting 10 Sep 15:00"). `when_ts` is unix time;
    the caller resolves the user's timezone (see vaf.core.user_time). Calendar events matched
    by name or address are NOT stored here, they are read live (contact_calendar_events)."""
    import time as _time
    label = (title or "").strip()
    try:
        when = float(when_ts)
    except (TypeError, ValueError):
        return None
    if not label or when <= 0:
        return None
    event = {"id": str(uuid.uuid4()), "ts": _time.time(), "when_ts": when, "title": label[:500],
             "source": (source or "user").strip() or "user", "note": (note or "").strip()[:2000] or None}
    with _LOCK:
        contacts = _load_all(username, user_scope_id)
        i = _find_index(contacts, contact_id)
        if i < 0:
            return None
        events = contacts[i].get("events") if isinstance(contacts[i].get("events"), list) else []
        events.append(event)
        events.sort(key=lambda e: float(e.get("when_ts") or 0))
        contacts[i]["events"] = events[-500:]
        _save_all(contacts, username, user_scope_id)
    return event


def delete_contact_event(contact_id: str, event_id: str, username: Optional[str] = None, user_scope_id: Optional[str] = None) -> bool:
    with _LOCK:
        contacts = _load_all(username, user_scope_id)
        i = _find_index(contacts, contact_id)
        if i < 0:
            return False
        events = contacts[i].get("events") if isinstance(contacts[i].get("events"), list) else []
        kept = [e for e in events if e.get("id") != event_id]
        if len(kept) == len(events):
            return False
        contacts[i]["events"] = kept
        _save_all(contacts, username, user_scope_id)
        return True


def contact_summary(contact: Dict[str, Any], now_ts: Optional[float] = None) -> Dict[str, Any]:
    """What the agent and the dashboard want at a glance: status, when and where the last
    contact happened (the newest of all channel links), the next stored event, the
    newest notes. Pure: reads the record, touches nothing."""
    import time as _time
    now = float(now_ts if now_ts is not None else _time.time())
    last: Optional[Dict[str, Any]] = None
    for chan, link in (contact.get("links") or {}).items():
        if not isinstance(link, dict):
            continue
        try:
            ts = float(link.get("last_seen_ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts and (last is None or ts > last["ts"]):
            last = {"channel": chan, "ts": ts}
    events = [e for e in (contact.get("events") or []) if isinstance(e, dict)]
    upcoming = sorted((e for e in events if float(e.get("when_ts") or 0) >= now), key=lambda e: float(e.get("when_ts") or 0))
    notes = [n for n in (contact.get("notes_log") or []) if isinstance(n, dict)]
    return {
        "status": (contact.get("status") or "").strip() or None,
        "last_contact": last,
        "next_event": upcoming[0] if upcoming else None,
        "upcoming_events": upcoming[:10],
        "recent_notes": sorted(notes, key=lambda n: float(n.get("ts") or 0), reverse=True)[:5],
        "notes_count": len(notes),
    }


def contact_calendar_events(
    contact: Dict[str, Any],
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
    days: int = 30,
) -> List[Dict[str, Any]]:
    """Upcoming calendar events that mention this contact (name or one of its addresses in
    the title or description), from the user's connected calendar. Read live, never stored;
    empty when no calendar is connected or the lookup fails. Best-effort by design: the
    calendar API is a network call and this is a glance, not a sync."""
    try:
        from datetime import datetime, timedelta, timezone
        from vaf.core.calendar_client import list_events, resolve_calendar_account
        account = resolve_calendar_account(username=username or "admin", user_scope_id=user_scope_id)
        if not account:
            return []
        now = datetime.now(timezone.utc)
        events = list_events(
            provider=(account.get("provider") or "gmail").strip().lower(),
            account_id=account.get("account_id") or account.get("email") or "",
            user_scope_id=user_scope_id,
            time_min=now.isoformat().replace("+00:00", "Z"),
            time_max=(now + timedelta(days=max(1, int(days)))).isoformat().replace("+00:00", "Z"),
            username=username,
            max_results=100,
        )
    except Exception:
        return []
    needles = [s.lower() for s in [contact.get("name") or ""] + _contact_email_values(contact) if (s or "").strip()]
    if not needles:
        return []
    out = []
    for e in events or []:
        hay = f"{e.get('summary') or ''} {e.get('description') or ''}".lower()
        if any(n in hay for n in needles):
            out.append(e)
    return out


def find_contact_by_phone(phone: str, username: Optional[str] = None, user_scope_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The contact carrying this phone number on any phone/WhatsApp channel, regardless of
    the Front Office flag (get_contact_by_whatsapp_phone answers the ingress question and
    only sees contacts that may reach the assistant). Canonical-digit match, so 0152...
    and +49152... are one number."""
    norm = _phone_digits_canonical(phone or "")
    if not norm:
        return None
    with _LOCK:
        for c in _load_all(username, user_scope_id):
            for p in _contact_whatsapp_values(c):
                if _phone_digits_canonical(p) == norm:
                    return _contact_ensure_channels(dict(c))
    return None


def _looks_like_a_number(name: str) -> bool:
    digits = "".join(ch for ch in (name or "") if ch.isdigit())
    return bool(digits) and len(digits) >= 7 and len(digits) >= len((name or "").replace(" ", "").lstrip("+")) - 2


def sync_channel_contacts(
    channel: str,
    entries: List[Dict[str, Any]],
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> Dict[str, int]:
    """Fold what a messaging channel knows about people into the contact book.

    One entry per person: {"endpoint": E.164 phone, "display_name": the name the channel
    shows, "last_seen_ts": unix time of the newest message}. Rules, in this order:
      * an entry without a name is skipped: a bare number is not a contact yet, it is a
        chat, and the WhatsApp window already lists those;
      * the endpoint is matched against every phone/WhatsApp channel value of every
        contact (canonical digits); a match records the link on that contact and fills
        its name only when the contact had none or was named after its number;
      * no match creates the contact with the channel's name and the number as a
        `whatsapp` channel;
      * `allow_as_assistant_user` is never touched: whether a person may reach the
        assistant stays a decision the user takes in the contact book.
    The link itself is `links[channel] = {endpoint, display_name, last_seen_ts, linked_at}`,
    the field the dashboard's channel icon and "last contact via" line read. One load,
    one save. Returns {"created": n, "linked": n, "skipped": n}."""
    import time as _time
    chan = (channel or "").strip().lower()
    out = {"created": 0, "linked": 0, "skipped": 0}
    if chan not in CHANNEL_TYPES or not entries:
        return out
    with _LOCK:
        contacts = _load_all(username, user_scope_id)
        by_digits: Dict[str, Dict[str, Any]] = {}
        for c in contacts:
            for p in _contact_whatsapp_values(c):
                key = _phone_digits_canonical(p)
                if key and key not in by_digits:
                    by_digits[key] = c
        changed = False
        for e in entries:
            endpoint = str((e or {}).get("endpoint") or "").strip()
            name = str((e or {}).get("display_name") or "").strip()
            key = _phone_digits_canonical(endpoint)
            if not key or not name or _looks_like_a_number(name):
                out["skipped"] += 1
                continue
            try:
                seen = float((e or {}).get("last_seen_ts") or 0) or None
            except (TypeError, ValueError):
                seen = None
            link = {"endpoint": endpoint if endpoint.startswith("+") else "+" + key,
                    "display_name": name, "last_seen_ts": seen}
            existing = by_digits.get(key)
            if existing is not None:
                links = existing.get("links") if isinstance(existing.get("links"), dict) else {}
                prev = links.get(chan) if isinstance(links.get(chan), dict) else {}
                if prev.get("display_name") == name and (prev.get("last_seen_ts") or 0) >= (seen or 0):
                    continue
                link["linked_at"] = prev.get("linked_at") or _time.time()
                if prev.get("last_seen_ts") and (seen or 0) < float(prev["last_seen_ts"]):
                    link["last_seen_ts"] = prev["last_seen_ts"]
                links[chan] = link
                existing["links"] = links
                if not (existing.get("name") or "").strip() or _looks_like_a_number(existing.get("name") or ""):
                    existing["name"] = name
                out["linked"] += 1
                changed = True
                continue
            link["linked_at"] = _time.time()
            contact: Dict[str, Any] = {
                "id": str(uuid.uuid4()),
                "name": name,
                "channels": [{"type": "whatsapp" if chan == "whatsapp" else chan, "value": link["endpoint"]}],
                "whatsapp_phone": None, "telegram_username": None, "telegram_user_id": None, "email": None,
                "preferred_language": None, "how_to_address": None, "birthday": None, "notes": None,
                "allow_as_assistant_user": False,
                "links": {chan: link},
            }
            _sync_legacy_from_channels(contact)
            contacts.append(contact)
            by_digits[key] = contact
            out["created"] += 1
            changed = True
        if changed:
            _save_all(contacts, username, user_scope_id)
    return out


def get_contacts_allowing_assistant(username: Optional[str] = None, user_scope_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return contacts with allow_as_assistant_user=True, for bridge whitelist checks."""
    with _LOCK:
        return [dict(c) for c in _load_all(username, user_scope_id) if c.get("allow_as_assistant_user")]


# ── endpoints: where a contact's messages live ──────────────────────────────────
#
# The message store keys a chat by channel-specific ids: "+<E.164 digits>" for WhatsApp,
# the numeric chat id for Telegram, the numeric user id for Discord; mail is keyed by
# address. A contact record holds the values a person typed or a channel synced, in
# whatever notation. These two functions are the one place that turns a record into store
# keys, so the dashboard, the bridge, the cross-chat filter and the timeline never build
# a key by hand again.

def _lid_keys_for_digits(digit_keys: List[str], lid_map: Optional[Dict[str, Any]]) -> List[str]:
    """The "<lid>@lid" jids whose mapped number is one of these canonical digit strings.
    Agent-sent messages to a LID-addressed account are stored under the raw lid jid, so a
    person's messages can sit under two keys; the persisted lid_to_e164 map joins them."""
    if lid_map is None:
        try:
            from vaf.core.config import Config
            wc = Config.get("whatsapp_config") or {}
            lid_map = (wc.get("lid_to_e164") or {}) if isinstance(wc, dict) else {}
        except Exception:
            lid_map = {}
    if not isinstance(lid_map, dict) or not lid_map:
        return []
    wanted = set(digit_keys)
    out: List[str] = []
    for lid, e164 in lid_map.items():
        lid_s = str(lid or "").strip()
        if not lid_s.endswith("@lid"):
            continue
        if phone_digits_canonical(str(e164 or "")) in wanted and lid_s not in out:
            out.append(lid_s)
    return out


def contact_endpoints(
    contact: Dict[str, Any],
    *,
    with_lids: bool = False,
    lid_map: Optional[Dict[str, Any]] = None,
) -> Dict[str, List[str]]:
    """The store keys of one contact per channel: {"whatsapp": ["+4917..."], "telegram":
    ["12345"], "discord": ["9876"], "email": ["bob@example.com"]}.

    WhatsApp keys come from whatsapp_store_key over every phone/WhatsApp value; with
    with_lids=True the "<lid>@lid" jids mapped to the same number are appended (the
    timeline and statistics want both keys, the dashboard's phone lists do not). Telegram
    keeps numeric values only: an "@username" cannot be matched to a stored chat id.
    Addresses are lowercased. Pure over the record, apart from the optional config read
    for the lid map (pass lid_map to avoid it)."""
    c = _contact_ensure_channels(contact)
    out: Dict[str, List[str]] = {"whatsapp": [], "telegram": [], "discord": [], "email": []}
    for p in _contact_whatsapp_values(c):
        key = whatsapp_store_key(p)
        if key and key not in out["whatsapp"]:
            out["whatsapp"].append(key)
    if with_lids and out["whatsapp"]:
        for lid in _lid_keys_for_digits([k[1:] for k in out["whatsapp"]], lid_map):
            if lid not in out["whatsapp"]:
                out["whatsapp"].append(lid)
    for v in _contact_telegram_values(c):
        v = (v or "").strip()
        if v.isdigit() and v not in out["telegram"]:
            out["telegram"].append(v)
    for ch in (c.get("channels") or []):
        if (ch.get("type") or "").strip().lower() == "discord":
            v = (ch.get("value") or "").strip()
            if v and v not in out["discord"]:
                out["discord"].append(v)
    for e in _contact_email_values(c):
        e = (e or "").strip().lower()
        if e and e not in out["email"]:
            out["email"].append(e)
    return out


def front_office_endpoints(
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
    channel: str = "whatsapp",
) -> Set[str]:
    """The store keys, on one channel, of every contact who may reach the assistant
    ("Can reach your assistant"). The WhatsApp bridge decides ingress with it and the
    dashboard labels chats with it; one implementation, the same keys everywhere."""
    out: Set[str] = set()
    chan = (channel or "").strip().lower()
    for c in get_contacts_allowing_assistant(username, user_scope_id=user_scope_id):
        out.update(contact_endpoints(c).get(chan) or [])
    return out
