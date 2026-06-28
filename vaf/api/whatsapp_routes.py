# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
WhatsApp Integration API Routes

Handles WhatsApp bridge start/stop, QR display for linking, status, and whitelist management.
"""
import json
import logging
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from vaf.core.config import Config, get_local_admin_scope_id, get_local_admin_username

logger = logging.getLogger("vaf.api.whatsapp")

router = APIRouter(prefix="/api/whatsapp", tags=["whatsapp"])

# QR state per username: { "qr": base64_data, "ts": time }
_qr_state: Dict[str, Dict[str, Any]] = {}
# QR process per username (to terminate before starting a new one)
_qr_procs: Dict[str, subprocess.Popen] = {}
_qr_lock = threading.Lock()


def _jid_to_phone(jid: str) -> str:
    """Extract E.164 phone from WhatsApp JID. Skips @lid, validates length 7-15."""
    if not jid or not isinstance(jid, str):
        return ""
    if "@lid" in jid or jid.endswith("@broadcast") or jid.endswith("@status"):
        return ""
    part = jid.split("@")[0].split(":")[0].strip()
    if not part or not part.isdigit() or len(part) < 7 or len(part) > 15:
        return ""
    return f"+{part}"


def _normalize_chat_id(chat_id: str) -> str:
    """Return E.164 with exactly one leading + so ++49176... becomes +49176... (merge duplicates)."""
    if not chat_id or not isinstance(chat_id, str):
        return ""
    s = chat_id.strip().lstrip("+")
    digits = "".join(c for c in s if c.isdigit())
    if not digits or len(digits) < 7 or len(digits) > 15:
        return chat_id.strip()
    return f"+{digits}"


def _normalize_phone(phone: str) -> str:
    """Normalize phone to digits only for comparison."""
    return "".join(c for c in (phone or "") if c.isdigit())


def get_current_vaf_user(request: Request) -> Dict[str, str]:
    """Return user_scope_id and username for the current request."""
    user = getattr(request.state, "user", None)
    if user and user.get("user_scope_id") and user.get("username"):
        return {
            "user_scope_id": str(user["user_scope_id"]),
            "username": user.get("username", "admin"),
        }
    return {
        "user_scope_id": get_local_admin_scope_id(),
        "username": get_local_admin_username(),
    }


class WhitelistAddRequest(BaseModel):
    phone_number: str
    vaf_username: Optional[str] = None
    user_scope_id: Optional[str] = None


class ConfigUpdateRequest(BaseModel):
    enabled: Optional[bool] = None
    whitelist: Optional[list] = None


class LidAssignRequest(BaseModel):
    """Assign a LID JID to an E.164 number (contact/whitelist). So the bridge accepts messages from that @lid."""
    lid_jid: str  # e.g. "55877994332394@lid"
    phone_number: str  # E.164, e.g. "+4915256564444"


@router.get("/dashboard/debug")
async def get_whatsapp_dashboard_debug(request: Request):
    """Debug: raw_chats count from bridge. Helps diagnose empty chat list."""
    from vaf.api.whatsapp_bridge import get_connection_status, get_whatsapp_chats, is_bridge_running

    user_info = get_current_vaf_user(request)
    username = user_info["username"]
    raw_chats = get_whatsapp_chats(username, wait_timeout=3.0)
    return {
        "bridge_running": is_bridge_running(),
        "raw_chats_count": len(raw_chats),
        "username": username,
    }


def _is_whatsapp_admin(request: Request) -> bool:
    """True if current user is admin (can see all WhatsApp whitelist/sessions)."""
    from vaf.api.config_routes import get_current_user_or_local_admin
    user = get_current_user_or_local_admin(request)
    scope = user.get("user_scope_id")
    return scope is not None and str(scope) == str(get_local_admin_scope_id())


def _whatsapp_enabled_for_request(request: Request, whatsapp_config: Dict[str, Any], user_scope_id: Optional[str]) -> bool:
    """Return effective WhatsApp enabled flag for current user (admin=global, non-admin=scope toggle)."""
    if _is_whatsapp_admin(request):
        return bool((whatsapp_config or {}).get("enabled", False))
    by_scope = Config.get("connection_enabled_by_scope") or {}
    if not isinstance(by_scope, dict):
        return False
    toggles = by_scope.get(str(user_scope_id or "").strip(), {})
    if not isinstance(toggles, dict):
        return False
    return bool(toggles.get("whatsapp", False))


@router.get("/dashboard")
async def get_whatsapp_dashboard(request: Request):
    """Data for the WhatsApp dashboard: status, sessions, activity, stats, whitelist. No sensitive data. Non-admins see only their own whitelist and sessions."""
    import time as _time
    from typing import Any, Dict

    from vaf.api.whatsapp_bridge import get_connection_status, get_whatsapp_chats, is_bridge_running
    from vaf.core.whatsapp_auth import whatsapp_auth_exists

    user_info = get_current_vaf_user(request)
    username = user_info["username"]
    user_scope_id = user_info.get("user_scope_id")
    whatsapp_config = Config.get("whatsapp_config") or {}
    if not isinstance(whatsapp_config, dict):
        whatsapp_config = {}

    whitelist_raw = whatsapp_config.get("whitelist") or []
    whitelist_raw = [e for e in whitelist_raw if isinstance(e, dict) and e.get("phone_number")]
    if _is_whatsapp_admin(request):
        whitelist = whitelist_raw
    else:
        whitelist = [e for e in whitelist_raw if str(e.get("user_scope_id")) == str(user_scope_id)]

    current_linked = whatsapp_auth_exists(username)
    any_whitelist_linked = any(
        whatsapp_auth_exists((e.get("vaf_username") or "admin").strip())
        for e in whitelist if isinstance(e, dict)
    )
    linked = current_linked or any_whitelist_linked

    activity_raw = list(whatsapp_config.get("chat_activity") or [])[-100:]
    if _is_whatsapp_admin(request):
        activity = activity_raw
    else:
        my_phones = set()
        for e in whitelist:
            p = (e.get("phone_number") or "").strip()
            if p:
                my_phones.add(p)
                if not p.startswith("+"):
                    my_phones.add("+" + p)
        activity = [a for a in activity_raw if (a.get("chat_id") or "").strip() in my_phones or ("+" + (a.get("chat_id") or "").replace(" ", "")) in my_phones]

    def _phone_to_session_id(phone: str, vaf_username: str) -> str:
        digits = "".join(c for c in phone if c.isdigit())
        uname = (vaf_username or "admin").strip()
        return f"whatsapp_{uname}_{digits}"

    whitelist_by_phone: Dict[str, Dict[str, Any]] = {}
    for e in whitelist:
        phone = (e.get("phone_number") or "").strip()
        if not phone:
            continue
        chat_id = phone if phone.startswith("+") else f"+{phone}"
        whitelist_by_phone[chat_id] = e

    def _canonical_chat_key(cid: str) -> str:
        """Single key per logical chat: E.164 for numbers, raw id for @lid/groups."""
        if not cid or not isinstance(cid, str):
            return (cid or "").strip()
        s = (cid or "").strip()
        if s.endswith("@lid") or "@g.us" in s:
            return s
        return _normalize_chat_id(s) or s

    sessions_by_chat: Dict[str, Dict[str, Any]] = {}
    raw_chats = get_whatsapp_chats(username, wait_timeout=3.0)
    for c in raw_chats:
        jid = c.get("jid") or c.get("phone") or ""
        if not jid:
            continue
        is_group = c.get("is_group", False) or "@g.us" in str(jid)
        if is_group:
            chat_id = jid
            phone = jid
        else:
            phone = c.get("phone") or _jid_to_phone(jid)
            chat_id = phone if phone and phone.startswith("+") else _jid_to_phone(jid) if jid else ""
            if not chat_id and str(jid).endswith("@lid"):
                chat_id = str(jid)
                phone = chat_id
        if not chat_id:
            continue
        key = _canonical_chat_key(chat_id)
        vaf_username = username
        wl_entry = whitelist_by_phone.get(key) or whitelist_by_phone.get(chat_id)
        if wl_entry:
            vaf_username = (wl_entry.get("vaf_username") or "admin").strip()
        stype = "admin" if key in whitelist_by_phone or chat_id in whitelist_by_phone else "contact"
        if key in sessions_by_chat:
            rec = sessions_by_chat[key]
            rec["last_ts"] = max(rec.get("last_ts") or 0, int(c.get("last_ts") or 0))
            if c.get("name") and not (rec.get("name") or "").strip():
                rec["name"] = c.get("name")
        else:
            sessions_by_chat[key] = {
                "chat_id": key,
                "phone_number": (phone or chat_id) if key == chat_id else (key if not key.endswith("@lid") else phone or key),
                "vaf_username": vaf_username,
                "session_id": _phone_to_session_id(phone or chat_id, vaf_username),
                "type": stype,
                "name": c.get("name"),
                "last_ts": int(c.get("last_ts") or 0),
                "message_count": 0,
            }
    for a in activity:
        cid_raw = str(a.get("chat_id") or "")
        if not cid_raw:
            continue
        cid = _canonical_chat_key(cid_raw)
        if not cid:
            cid = cid_raw
        if cid not in sessions_by_chat:
            digits = "".join(c for c in cid if c.isdigit())
            sessions_by_chat[cid] = {
                "chat_id": cid,
                "phone_number": cid,
                "vaf_username": username,
                "session_id": f"whatsapp_{username}_{digits}",
                "type": "contact",
                "name": None,
                "last_ts": 0,
                "message_count": 0,
            }
        rec = sessions_by_chat[cid]
        ts = a.get("ts") or 0
        rec["last_ts"] = max(rec.get("last_ts") or 0, int(ts))
        rec["message_count"] = rec.get("message_count", 0) + 1
    for e in whitelist:
        phone = (e.get("phone_number") or "").strip()
        if not phone:
            continue
        chat_id = phone if phone.startswith("+") else f"+{phone}"
        if chat_id not in sessions_by_chat:
            vaf_username = (e.get("vaf_username") or "admin").strip()
            sessions_by_chat[chat_id] = {
                "chat_id": chat_id,
                "phone_number": phone,
                "vaf_username": vaf_username,
                "session_id": _phone_to_session_id(phone, vaf_username),
                "type": "admin",
                "name": None,
                "last_ts": 0,
                "message_count": 0,
            }
    # Include Front Office contacts (allow_as_assistant_user) so their chats appear even before Baileys syncs
    try:
        from vaf.core.contacts_store import get_contacts_allowing_assistant, _contact_whatsapp_values
        for contact in get_contacts_allowing_assistant(username):
            for phone in _contact_whatsapp_values(contact):
                if not phone or not phone.strip():
                    continue
                chat_id = phone.strip() if phone.strip().startswith("+") else f"+{phone.strip()}"
                if chat_id not in sessions_by_chat:
                    sessions_by_chat[chat_id] = {
                        "chat_id": chat_id,
                        "phone_number": chat_id,
                        "vaf_username": username,
                        "session_id": _phone_to_session_id(chat_id, username),
                        "type": "contact",
                        "name": (contact.get("name") or "").strip() or None,
                        "last_ts": 0,
                        "message_count": 0,
                    }
    except Exception:
        pass
    # Include chats from message store (persistent inbox: show all chats we have messages for, like mail/Telegram)
    try:
        from vaf.core.channel_message_store import list_chats_from_store
        for row in list_chats_from_store(username, limit=500, user_scope_id=user_info.get("user_scope_id")):
            cid = (row.get("chat_id") or "").strip()
            if not cid:
                continue
            key = _canonical_chat_key(cid)
            last_ts = int(row.get("last_ts") or 0)
            msg_count = int(row.get("message_count") or 0)
            if key in sessions_by_chat:
                rec = sessions_by_chat[key]
                rec["last_ts"] = max(rec.get("last_ts") or 0, last_ts)
                rec["message_count"] = max(rec.get("message_count") or 0, msg_count)
                if not (rec.get("name") or "").strip() and (row.get("chat_name") or "").strip():
                    rec["name"] = (row.get("chat_name") or "").strip()
            else:
                sessions_by_chat[key] = {
                    "chat_id": key,
                    "phone_number": key if not key.endswith("@lid") else cid,
                    "vaf_username": username,
                    "session_id": _phone_to_session_id(key if not key.endswith("@lid") else cid, username),
                    "type": "contact",
                    "name": (row.get("chat_name") or "").strip() or None,
                    "last_ts": last_ts,
                    "message_count": msg_count,
                }
    except Exception:
        pass
    # When the bridge didn't send a name (e.g. activity-only session), use VAF contact name if stored
    user_scope_id = user_info.get("user_scope_id")
    try:
        from vaf.core.contacts_store import get_contact_name_by_phone
        # Strict isolation: only resolve names in the current user's scope.
        scope_candidates = [user_scope_id]
        for rec in sessions_by_chat.values():
            if not (rec.get("name") or "").strip():
                phone = rec.get("phone_number") or rec.get("chat_id") or ""
                for scope in scope_candidates:
                    contact_name = get_contact_name_by_phone(phone, username, scope)
                    if contact_name:
                        rec["name"] = contact_name
                        break
    except Exception:
        pass
    for rec in sessions_by_chat.values():
        rec.setdefault("last_ts", 0)
        rec.setdefault("message_count", 0)
    # Overwrite message_count with actual session size so list and session view match (no "3 msgs" vs "0 Nachrichten")
    try:
        from vaf.core.session import SessionManager
        session_mgr = SessionManager()
        for rec in sessions_by_chat.values():
            sid = rec.get("session_id")
            if not sid or not str(sid).startswith("whatsapp_"):
                continue
            try:
                session = session_mgr.load(sid)
                rec["message_count"] = len(session.messages or [])
            except FileNotFoundError:
                rec["message_count"] = 0
            except Exception:
                pass
    except Exception:
        pass
    # Resolve LID→E.164 from config + Node so we can merge duplicate rows (same person as +55... and 123@lid)
    node_by_lid: Dict[str, str] = {}
    try:
        from vaf.api.whatsapp_bridge import get_lid_mappings
        for m in get_lid_mappings(username, wait_timeout=2.0):
            lid_val = m.get("lid") or ""
            e164_val = (m.get("e164") or "").strip()
            if lid_val and e164_val:
                node_by_lid[str(lid_val)] = e164_val
    except Exception:
        pass
    lid_to_e164 = dict((whatsapp_config.get("lid_to_e164") or {}) if isinstance(whatsapp_config, dict) else {})
    for lid_jid, e164 in list(lid_to_e164.items()):
        if not lid_jid or not e164 or "@lid" not in str(lid_jid):
            continue
        lid_digits = "".join(c for c in str(lid_jid).split("@")[0] if c.isdigit())
        if not lid_digits:
            continue
        canonical = _normalize_chat_id(e164)
        if not canonical:
            continue
        sid_lid = f"whatsapp_{username}_{lid_digits}"
        if canonical in sessions_by_chat:
            sessions_by_chat[canonical]["session_id"] = sid_lid
    # Merge LID rows into E.164 so the same contact (baba, Anne) appears only once
    for key in list(sessions_by_chat.keys()):
        if not str(key).endswith("@lid"):
            continue
        resolved = (lid_to_e164.get(key) or "").strip() or (node_by_lid.get(key) or "").strip()
        if not resolved:
            continue
        e164 = _normalize_chat_id(resolved)
        if not e164 or e164 == key:
            continue
        rec = sessions_by_chat[key]
        if e164 in sessions_by_chat:
            ex = sessions_by_chat[e164]
            ex["last_ts"] = max(ex.get("last_ts") or 0, rec.get("last_ts") or 0)
            ex["name"] = (ex.get("name") or "").strip() or (rec.get("name") or "").strip() or None
            ex["message_count"] = max(ex.get("message_count") or 0, rec.get("message_count") or 0)
            ex["session_id"] = (rec.get("session_id") or "").strip() or ex.get("session_id")
        else:
            rec2 = dict(rec)
            rec2["chat_id"] = e164
            rec2["phone_number"] = e164
            rec2["session_id"] = rec.get("session_id") or _phone_to_session_id(e164, username)
            sessions_by_chat[e164] = rec2
        del sessions_by_chat[key]
    # Infer LID→E.164 when we have one FO contact with no messages and one LID-style session with messages (same user)
    try:
        from vaf.core.contacts_store import get_contacts_allowing_assistant, _contact_whatsapp_values
        fo_phones = set()
        for contact in get_contacts_allowing_assistant(username):
            for phone in _contact_whatsapp_values(contact):
                if phone and phone.strip():
                    fo_phones.add(_normalize_chat_id(phone.strip()) or (phone.strip() if phone.strip().startswith("+") else f"+{phone.strip()}"))
        lid_sessions_with_messages = [
            rec for rec in sessions_by_chat.values()
            if (rec.get("message_count") or 0) > 0
            and str(rec.get("session_id") or "").startswith("whatsapp_")
            and rec.get("chat_id") not in whitelist_by_phone
            and _normalize_chat_id(rec.get("chat_id") or "") not in fo_phones
        ]
        fo_without_messages = [
            rec for rec in sessions_by_chat.values()
            if (rec.get("message_count") or 0) == 0
            and _normalize_chat_id(rec.get("chat_id") or "") in fo_phones
        ]
        if len(lid_sessions_with_messages) == 1 and len(fo_without_messages) == 1:
            lid_rec = lid_sessions_with_messages[0]
            fo_rec = fo_without_messages[0]
            sid = lid_rec.get("session_id") or ""
            chat_id_lid = lid_rec.get("chat_id") or ""
            lid_digits = "".join(c for c in str(chat_id_lid) if c.isdigit())
            if len(lid_digits) >= 10 and sid:
                e164_fo = _normalize_chat_id(fo_rec.get("chat_id") or "") or (fo_rec.get("chat_id") or "").strip()
                lid_jid = f"{lid_digits}@lid"
                if e164_fo and lid_jid not in lid_to_e164:
                    lid_to_e164[lid_jid] = e164_fo
                    try:
                        cfg = Config.load()
                        wc = (cfg.get("whatsapp_config") or {}) if isinstance(cfg.get("whatsapp_config"), dict) else {}
                        wc = dict(wc)
                        wc["lid_to_e164"] = dict(lid_to_e164)
                        cfg["whatsapp_config"] = wc
                        Config.save(cfg)
                    except Exception:
                        pass
                    fo_rec["session_id"] = sid
    except Exception:
        pass
    now_ts = int(_time.time())
    # Clamp last_ts to now so UI never shows future dates (e.g. bad Baileys timestamp for baba)
    for rec in sessions_by_chat.values():
        lt = rec.get("last_ts") or 0
        if lt > now_ts:
            rec["last_ts"] = now_ts
    sessions = sorted(sessions_by_chat.values(), key=lambda s: (s.get("last_ts") or 0), reverse=True)

    # FO phones (E.164) for answerable check
    fo_phones: set = set()
    try:
        from vaf.core.contacts_store import get_contacts_allowing_assistant, _contact_whatsapp_values
        for contact in get_contacts_allowing_assistant(username):
            for phone in _contact_whatsapp_values(contact):
                if phone and phone.strip():
                    cid = phone.strip() if phone.strip().startswith("+") else f"+{phone.strip()}"
                    fo_phones.add(_normalize_chat_id(cid) or cid)
    except Exception:
        pass

    # LID resolution (config + node) for session enrichment and lid_chats_to_assign
    lid_to_e164_cfg = dict((whatsapp_config.get("lid_to_e164") or {}) if isinstance(whatsapp_config, dict) else {})
    lid_mappings_from_node: list = []
    node_by_lid: dict = {}
    try:
        from vaf.api.whatsapp_bridge import get_lid_mappings
        lid_mappings_from_node = get_lid_mappings(username, wait_timeout=2.0)
        node_by_lid = {m.get("lid", ""): (m.get("e164") or "").strip() for m in lid_mappings_from_node if m.get("lid")}
    except Exception:
        pass

    def _resolved_e164(rec: dict) -> str | None:
        cid = str(rec.get("chat_id") or "")
        if "@lid" not in cid:
            return None
        lid_jid = cid if cid.endswith("@lid") else f"{''.join(c for c in cid if c.isdigit())}@lid"
        return (lid_to_e164_cfg.get(lid_jid) or "").strip() or (node_by_lid.get(lid_jid) or "").strip() or None

    for rec in sessions:
        cid = str(rec.get("chat_id") or "")
        is_lid = "@lid" in cid
        resolved = _resolved_e164(rec) if is_lid else None
        if is_lid and resolved:
            rec["resolved_e164"] = resolved
            rec["answerable"] = resolved in whitelist_by_phone or resolved in fo_phones
            rec["needs_assign"] = False
        elif is_lid:
            rec["resolved_e164"] = None
            rec["answerable"] = False
            rec["needs_assign"] = True
        else:
            # Only whitelist and Front Office contacts get Agent; others are read-only
            cid_norm = _normalize_chat_id(cid) or cid
            in_whitelist = cid in whitelist_by_phone or cid_norm in whitelist_by_phone
            in_fo = cid_norm in fo_phones or cid in fo_phones
            rec["type"] = "admin" if in_whitelist else ("contact" if in_fo else "unknown")
            rec["answerable"] = rec.get("type") in ("admin", "relay", "contact")
            rec["needs_assign"] = False
        # display_name: prefer name, then contact name for resolved/phone, then "Unknown chat" for LID, else phone
        disp = (rec.get("name") or "").strip() or None
        if not disp:
            phone = resolved or rec.get("phone_number") or cid
            if phone:
                try:
                    from vaf.core.contacts_store import get_contact_name_by_phone
                    for scope in [user_info.get("user_scope_id")]:
                        disp = get_contact_name_by_phone(phone, username, scope)
                        if disp:
                            break
                except Exception:
                    pass
        if not disp and is_lid:
            disp = "Unknown chat"
        if not disp:
            disp = (rec.get("phone_number") or cid or "").strip() or "Unknown chat"
        rec["display_name"] = disp

    bucket_seconds = 4 * 3600
    cutoff = now_ts - 7 * 24 * 3600
    buckets: Dict[int, int] = {}
    for t in range(int(cutoff // bucket_seconds) * bucket_seconds, now_ts + 1, bucket_seconds):
        buckets[t] = 0
    for a in activity:
        ts = a.get("ts") or 0
        bucket_ts = (int(ts) // bucket_seconds) * bucket_seconds
        if bucket_ts in buckets:
            buckets[bucket_ts] += 1
    stats_4h = [{"bucket_ts": ts, "count": c} for ts, c in sorted(buckets.items())]

    running = is_bridge_running()
    enabled_effective = _whatsapp_enabled_for_request(request, whatsapp_config, user_scope_id)
    connected = get_connection_status(username, wait_timeout=5.0) if (running and enabled_effective) else False

    try:
        from vaf.core.log_helper import get_dated_log_path
        log_path = str(get_dated_log_path("whatsapp_qr", "log"))
    except Exception:
        log_path = "logs/whatsapp_qr.log"

    # Front Office contacts (Can reach your assistant) with WhatsApp number – for dashboard display
    front_office_contacts: list = []
    try:
        from vaf.core.contacts_store import get_contacts_allowing_assistant, _contact_whatsapp_values
        user_scope_id = user_info.get("user_scope_id")
        for contact in get_contacts_allowing_assistant(username, user_scope_id=user_scope_id):
            name = (contact.get("name") or "").strip() or None
            for phone in _contact_whatsapp_values(contact):
                if not phone or not phone.strip():
                    continue
                chat_id = phone.strip() if phone.strip().startswith("+") else f"+{phone.strip()}"
                front_office_contacts.append({"name": name, "phone_number": chat_id})
    except Exception:
        pass

    # List of LID chats for "Assign to contact" (reuses lid_to_e164_cfg, node_by_lid, lid_mappings_from_node from above)
    lid_chats_to_assign: list = []
    try:
        for rec in sessions:
            sid = str(rec.get("session_id") or "")
            if not sid.startswith("whatsapp_"):
                continue
            parts = sid.split("_")
            if len(parts) < 3:
                continue
            digits = "".join(c for c in parts[-1] if c.isdigit())
            if len(digits) < 12:
                continue
            lid_jid = f"{digits}@lid"
            resolved_config = (lid_to_e164_cfg.get(lid_jid) or "").strip() or None
            resolved_node = (node_by_lid.get(lid_jid) or "").strip() or None
            lid_chats_to_assign.append({
                "lid_jid": lid_jid,
                "chat_id": rec.get("chat_id") or digits,
                "name": rec.get("name"),
                "session_id": sid,
                "resolved_e164_from_config": resolved_config,
                "resolved_e164_from_node": resolved_node,
            })
    except Exception:
        pass

    return {
        "configured": bool(whitelist) and linked,
        "linked": linked,
        "running": running,
        "connected": connected,
        "enabled": enabled_effective,
        "username": username,
        "sessions": sessions,
        "stats_4h": stats_4h,
        "activity": activity,
        "log_path": log_path,
        "whitelist": [
            {"phone_number": e.get("phone_number", ""), "vaf_username": e.get("vaf_username")}
            for e in whitelist
        ],
        "front_office_contacts": front_office_contacts,
        "lid_mappings_from_node": lid_mappings_from_node,
        "lid_chats_to_assign": lid_chats_to_assign,
    }


@router.get("/status")
async def get_whatsapp_status(request: Request):
    """Get WhatsApp bridge status and per-user linked state."""
    from vaf.api.whatsapp_bridge import is_bridge_running
    from vaf.core.whatsapp_auth import whatsapp_auth_exists

    user_info = get_current_vaf_user(request)
    username = user_info["username"]
    whatsapp_config = Config.get("whatsapp_config") or {}
    if not isinstance(whatsapp_config, dict):
        whatsapp_config = {}

    user_scope_id = user_info.get("user_scope_id")
    whitelist_raw = list(whatsapp_config.get("whitelist") or [])
    whitelist_raw = [e for e in whitelist_raw if isinstance(e, dict) and e.get("phone_number")]
    if _is_whatsapp_admin(request):
        whitelist = whitelist_raw
    else:
        whitelist = [e for e in whitelist_raw if str(e.get("user_scope_id")) == str(user_scope_id)]

    enabled_effective = _whatsapp_enabled_for_request(request, whatsapp_config, user_scope_id)
    linked = whatsapp_auth_exists(username)
    running = is_bridge_running()

    return {
        "enabled": enabled_effective,
        "running": bool(running and enabled_effective),
        "linked": linked,
        "configured": bool(whitelist) and linked,
        "connected": bool(running and enabled_effective and linked),
        "whitelist_count": len(whitelist),
        "username": username,
    }


@router.post("/start")
async def start_whatsapp_bridge():
    """Start the WhatsApp bridge."""
    from vaf.api.whatsapp_bridge import is_bridge_running, start_bridge

    whatsapp_config = Config.get("whatsapp_config") or {}
    if not isinstance(whatsapp_config, dict) or not whatsapp_config.get("enabled"):
        raise HTTPException(status_code=400, detail="WhatsApp not enabled. Enable in Settings -> Connections.")

    if is_bridge_running():
        return {"status": "started", "message": "WhatsApp bridge already running."}

    if start_bridge():
        return {"status": "started", "message": "WhatsApp bridge started."}
    raise HTTPException(status_code=500, detail="Failed to start WhatsApp bridge. Ensure Node.js is installed and npm install was run in vaf/whatsapp_node.")


@router.post("/stop")
async def stop_whatsapp_bridge():
    """Stop the WhatsApp bridge."""
    from vaf.api.whatsapp_bridge import is_bridge_running, stop_bridge

    if is_bridge_running():
        stop_bridge()
    return {"status": "stopped", "message": "WhatsApp bridge stopped."}


@router.post("/restart")
async def restart_whatsapp_bridge():
    """Restart the WhatsApp bridge (stop, wait for shutdown, start). Use when 'Restart bridge' doesn't reconnect."""
    from vaf.api.whatsapp_bridge import restart_bridge

    whatsapp_config = Config.get("whatsapp_config") or {}
    if not isinstance(whatsapp_config, dict) or not whatsapp_config.get("enabled"):
        raise HTTPException(status_code=400, detail="WhatsApp not enabled. Enable in Settings -> Connections.")
    if restart_bridge():
        return {"status": "restarted", "message": "WhatsApp bridge restarted. Wait 20-30 s, then refresh."}
    raise HTTPException(status_code=500, detail="Failed to restart bridge. Check Node.js and npm install in vaf/whatsapp_node.")


def _get_whatsapp_compaction_info(session_id: str) -> tuple:
    """Return (last_compaction_at_turn, compaction_interval) for a session."""
    from vaf.core.config import Config

    interval = int(Config.get("memory_compaction_interval", 15))
    try:
        from vaf.core.session import SessionManager
        _sm = SessionManager()
        _session = _sm.load(session_id)
        _runtime = getattr(_session, "runtime_state", None) or {}
        if "last_compaction_at_turn" in _runtime:
            last = int(_runtime["last_compaction_at_turn"])
            return (last, interval)
    except Exception:
        pass
    try:
        compaction_path = Config.APP_DIR / "compaction_state.json"
        if compaction_path.exists():
            with open(compaction_path, "r", encoding="utf-8") as f:
                state = json.load(f)
            v = state.get(session_id)
            if isinstance(v, dict) and "turn" in v:
                return (int(v.get("turn", 0)), interval)
            if isinstance(v, (int, float)):
                return (int(v), interval)
    except Exception:
        pass
    return (0, interval)


@router.get("/session/{session_id}/history")
async def get_whatsapp_session_history(session_id: str, request: Request):
    """Return message history and compaction stats for a WhatsApp session."""
    if not session_id.startswith("whatsapp_"):
        raise HTTPException(status_code=400, detail="Invalid session id")
    current_user = get_current_vaf_user(request)
    if not _is_whatsapp_admin(request):
        # Session IDs are scoped by vaf_username: whatsapp_<username>_<digits>
        prefix = f"whatsapp_{(current_user.get('username') or '').strip()}_"
        if not session_id.startswith(prefix):
            raise HTTPException(status_code=403, detail="Access denied")
    try:
        from vaf.core.session import SessionManager

        session_mgr = SessionManager()
        session = session_mgr.load(session_id)
        messages = [
            {"role": m.role, "content": (m.content or "")[:2000], "timestamp": getattr(m, "timestamp", None)}
            for m in (session.messages or [])
        ]
        runtime_state = getattr(session, "runtime_state", None) or {}
        user_turn_count = runtime_state.get("user_turn_count", 0)
        if user_turn_count == 0 and session.messages:
            user_turn_count = sum(1 for m in (session.messages or []) if getattr(m, "role", None) == "user")
        last_compaction_at_turn, compaction_interval = _get_whatsapp_compaction_info(session_id)
        return {
            "session_id": session_id,
            "messages": messages,
            "user_turn_count": user_turn_count,
            "compaction_interval": compaction_interval,
            "last_compaction_at_turn": last_compaction_at_turn,
        }
    except FileNotFoundError:
        last_compaction_at_turn, compaction_interval = _get_whatsapp_compaction_info(session_id)
        return {
            "session_id": session_id,
            "messages": [],
            "user_turn_count": 0,
            "compaction_interval": compaction_interval,
            "last_compaction_at_turn": last_compaction_at_turn,
        }
    except Exception as e:
        logger.exception("WhatsApp session history error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


def _run_qr_login(username: str) -> None:
    """Spawn Node process for QR login, capture QR to _qr_state."""
    from vaf.core.log_helper import log_whatsapp_qr
    from vaf.core.whatsapp_auth import get_whatsapp_auth_dir
    import shutil
    import json

    log_whatsapp_qr(f"[VAF] QR flow started for user={username}")
    auth_dir = get_whatsapp_auth_dir(username)
    auth_dir.mkdir(parents=True, exist_ok=True)
    node = shutil.which("node")
    wa_js = Path(__file__).resolve().parents[1] / "whatsapp_node" / "wa-bridge.js"
    if not node or not wa_js.exists():
        with _qr_lock:
            _qr_state[username] = {"error": "Node or wa-bridge.js not found. Install Node.js 18+ and run 'npm install' in vaf/whatsapp_node/.", "ts": 0}
        return
    kwargs = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "bufsize": 1,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        proc = subprocess.Popen(
            [node, str(wa_js), "--auth-dir", str(auth_dir.resolve())],
            **kwargs,
        )
        log_whatsapp_qr(f"[VAF] Node process spawned pid={proc.pid}")
        with _qr_lock:
            _qr_procs[username] = proc

        def _log_stderr():
            try:
                from vaf.core.log_helper import log_whatsapp_qr
                for line in (proc.stderr or []):
                    s = (line or "").strip()
                    if s:
                        log_whatsapp_qr(f"[stderr] {s}")
                        logger.warning("[wa-bridge] %s", s)
            except Exception:
                pass

        _stderr_thread = threading.Thread(target=_log_stderr, daemon=True)
        _stderr_thread.start()

        try:
            for line in proc.stdout:
                line = (line or "").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                typ = obj.get("type")
                if typ == "qr":
                    log_whatsapp_qr(f"[VAF] Received: qr (len={len(str(obj.get('qr') or ''))})")
                    qr_data = obj.get("qr", "")
                    with _qr_lock:
                        _qr_state[username] = {"qr": qr_data, "ts": __import__("time").time()}
                elif typ == "connected":
                    self_jid = obj.get("selfJid") or ""
                    phone = _jid_to_phone(self_jid)
                    log_whatsapp_qr(f"[VAF] Received: connected selfJid={self_jid} phone={phone}")
                    with _qr_lock:
                        _qr_state[username] = {"connected": True, "phone": phone, "ts": __import__("time").time()}
                    proc.terminate()
                    return
                elif typ == "error":
                    log_whatsapp_qr(f"[VAF] Received: error msg={obj.get('message', '')}")
                    with _qr_lock:
                        _qr_state[username] = {"error": obj.get("message", "Unknown error"), "ts": __import__("time").time()}
            if proc.poll() is not None:
                with _qr_lock:
                    s = _qr_state.get(username, {})
                    if not s.get("connected") and not s.get("error"):
                        code = proc.returncode or -1
                        log_whatsapp_qr(f"[VAF] Process exited without connected/error code={code}")
                        _qr_state[username] = {"error": f"Process exited (code {code}). Check logs/whatsapp_qr.log for details.", "ts": __import__("time").time()}
        finally:
            with _qr_lock:
                _qr_procs.pop(username, None)
    except Exception as e:
        with _qr_lock:
            _qr_state[username] = {"error": str(e), "ts": 0}
        with _qr_lock:
            _qr_procs.pop(username, None)


@router.get("/qr/log-path")
async def get_qr_log_path(request: Request):
    """Return path to whatsapp_qr_YYYY-MM-DD.log for debugging."""
    from vaf.core.log_helper import get_dated_log_path
    return {"path": str(get_dated_log_path("whatsapp_qr", "log"))}


@router.get("/qr")
async def get_qr_code(request: Request):
    """Get current QR code for linking (or status). Poll until connected."""
    try:
        user_info = get_current_vaf_user(request)
        username = user_info["username"]

        with _qr_lock:
            state = _qr_state.get(username, {})

        if state.get("connected"):
            phone = state.get("phone") or ""
            return {"status": "connected", "message": "WhatsApp linked successfully.", "phone": phone}
        if state.get("error"):
            return {"status": "error", "error": state["error"]}
        if state.get("qr"):
            return {"status": "qr", "qr": state["qr"]}
        return {"status": "waiting", "message": "Start QR flow from Settings -> Connections."}
    except Exception as e:
        logger.exception("WhatsApp QR endpoint error: %s", e)
        return {"status": "error", "error": f"Internal error: {e}"}


@router.post("/qr/reset")
async def reset_whatsapp_auth(request: Request):
    """Clear WhatsApp auth for current user. Use when 'Logged out' to allow a fresh QR scan."""
    import shutil

    from vaf.core.whatsapp_auth import get_whatsapp_auth_dir

    user_info = get_current_vaf_user(request)
    username = user_info["username"]
    auth_dir = get_whatsapp_auth_dir(username)
    removed = 0
    if auth_dir.exists():
        for p in auth_dir.iterdir():
            try:
                if p.is_file():
                    p.unlink()
                    removed += 1
                elif p.is_dir():
                    shutil.rmtree(p)
                    removed += 1
            except OSError:
                pass
    return {"status": "reset", "message": "Auth cleared. You can start a new QR flow."}


@router.post("/qr/start")
async def start_qr_flow(request: Request):
    """Start QR login flow for current user. QR will appear in /qr poll."""
    user_info = get_current_vaf_user(request)
    username = user_info["username"]

    with _qr_lock:
        old_proc = _qr_procs.pop(username, None)
        _qr_state.pop(username, None)
    if old_proc is not None and old_proc.poll() is None:
        try:
            old_proc.terminate()
            old_proc.wait(timeout=3)
        except Exception:
            pass

    t = threading.Thread(target=_run_qr_login, args=(username,), daemon=True)
    t.start()
    return {"status": "started", "message": "Scan the QR code in WhatsApp (Linked Devices). Poll GET /api/whatsapp/qr for the QR."}


@router.post("/whitelist/remove")
async def remove_whitelist_entry(request: Request, body: WhitelistAddRequest):
    """Remove a whitelist entry by phone number. Non-admins can only remove their own entry."""
    phone = (body.phone_number or "").strip()
    if not phone:
        raise HTTPException(status_code=400, detail="phone_number required")
    user_info = get_current_vaf_user(request)
    user_scope_id = user_info.get("user_scope_id")
    config = Config.load()
    wc = config.get("whatsapp_config") or {}
    if not isinstance(wc, dict):
        wc = {}
    whitelist = list(wc.get("whitelist") or [])
    if not _is_whatsapp_admin(request):
        # Non-admin: only allow removing an entry that belongs to this user
        entry = next((e for e in whitelist if isinstance(e, dict) and str(e.get("phone_number", "")).strip() == phone), None)
        if entry and str(entry.get("user_scope_id")) != str(user_scope_id):
            raise HTTPException(status_code=403, detail="You can only remove your own whitelist entry.")
    whitelist = [e for e in whitelist if not (isinstance(e, dict) and str(e.get("phone_number", "")).strip() == phone)]
    wc["whitelist"] = whitelist
    config["whatsapp_config"] = wc
    Config.save(config)
    return {"status": "removed", "message": "Whitelist entry removed.", "whitelist_count": len(whitelist)}


@router.post("/whitelist/add")
async def add_whitelist_entry(request: Request, body: WhitelistAddRequest):
    """Add a whitelist entry for WhatsApp (phone_number -> user)."""
    user_info = get_current_vaf_user(request)
    phone = (body.phone_number or "").strip()
    if not phone:
        raise HTTPException(status_code=400, detail="phone_number required")
    is_admin = _is_whatsapp_admin(request)
    if is_admin:
        vaf_username = (body.vaf_username or user_info["username"]).strip()
        user_scope_id = body.user_scope_id or user_info["user_scope_id"]
    else:
        # Non-admin users may only create/update their own whitelist entry.
        vaf_username = (user_info["username"] or "admin").strip()
        user_scope_id = user_info["user_scope_id"]

    config = Config.load()
    wc = config.get("whatsapp_config") or {}
    if not isinstance(wc, dict):
        wc = {"enabled": wc.get("enabled", False) if isinstance(wc, dict) else False, "whitelist": []}
    whitelist = list(wc.get("whitelist") or [])
    for i, e in enumerate(whitelist):
        if isinstance(e, dict) and (
            str(e.get("user_scope_id")) == str(user_scope_id) or e.get("vaf_username") == vaf_username
        ):
            whitelist[i] = {**e, "phone_number": phone, "user_scope_id": user_scope_id, "vaf_username": vaf_username}
            wc["whitelist"] = whitelist
            config["whatsapp_config"] = wc
            Config.save(config)
            return {"status": "updated", "message": "Whitelist entry updated."}
    whitelist.append({
        "phone_number": phone,
        "user_scope_id": user_scope_id,
        "vaf_username": vaf_username,
    })
    wc["whitelist"] = whitelist
    if "enabled" not in wc:
        wc["enabled"] = True
    config["whatsapp_config"] = wc
    Config.save(config)
    return {"status": "added", "message": "Whitelist entry added."}


@router.post("/lid-assign")
async def assign_lid_to_number(request: Request, body: LidAssignRequest):
    """Assign a LID (e.g. 55877994332394@lid) to an E.164 number so the bridge accepts messages from that chat. The number must be in whitelist or a Front Office contact."""
    lid_jid = (body.lid_jid or "").strip()
    phone = (body.phone_number or "").strip()
    if not lid_jid or "@lid" not in lid_jid:
        raise HTTPException(status_code=400, detail="lid_jid required (e.g. 55877994332394@lid)")
    if not phone:
        raise HTTPException(status_code=400, detail="phone_number required (E.164)")
    if not phone.startswith("+"):
        phone = "+" + phone
    config = Config.load()
    wc = config.get("whatsapp_config") or {}
    if not isinstance(wc, dict):
        wc = {}
    lid_map = dict(wc.get("lid_to_e164") or {})
    lid_map[lid_jid] = phone
    wc["lid_to_e164"] = lid_map
    config["whatsapp_config"] = wc
    Config.save(config)
    return {"status": "assigned", "message": f"LID {lid_jid} assigned to {phone}. Bridge will accept messages from this chat.", "lid_jid": lid_jid, "phone_number": phone}


@router.post("/sync-chats")
async def sync_whatsapp_chats_route(request: Request):
    """Request full chat list sync from WhatsApp (Baileys fetchMessageHistory). Use when the left chat list is incomplete. Returns updated chats count."""
    import asyncio
    from vaf.api.whatsapp_bridge import is_bridge_running, sync_whatsapp_chats

    user_info = get_current_vaf_user(request)
    username = user_info["username"]
    if not is_bridge_running():
        raise HTTPException(status_code=503, detail="WhatsApp bridge not running. Start it in Connections first.")
    chats = await asyncio.to_thread(sync_whatsapp_chats, username, 25.0)
    return {"status": "ok", "chats_count": len(chats), "message": "Chat list synced from WhatsApp. Refresh the dashboard to see all chats."}


@router.get("/config")
async def get_whatsapp_config(request: Request):
    """Get WhatsApp config (for UI)."""
    user_info = get_current_vaf_user(request)
    whatsapp_config = Config.get("whatsapp_config") or {}
    if not isinstance(whatsapp_config, dict):
        whatsapp_config = {}
    user_scope_id = user_info.get("user_scope_id")
    whitelist = list(whatsapp_config.get("whitelist") or [])
    if _is_whatsapp_admin(request):
        visible_whitelist = whitelist
    else:
        visible_whitelist = [e for e in whitelist if isinstance(e, dict) and str(e.get("user_scope_id")) == str(user_scope_id)]
    return {
        "enabled": _whatsapp_enabled_for_request(request, whatsapp_config, user_scope_id),
        "whitelist": visible_whitelist,
    }
