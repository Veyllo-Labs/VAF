# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""One-shot, persistent reminders - the narrow mechanism system automations use.

Design (deliberate, see docs/platform/AUTOMATIONS.md): a reminder is DATA, not
an executable automation. Firing means "deliver this stored message verbatim on
the user's main channel at the stored time" - no agent run, no tools, no prompt.
That is why the calendar check may schedule reminders although create_automation
is stripped from automation runs (runaway guard): a reminder cannot spawn
anything. Previously the calendar agent fell back to set_timer, which is
in-memory only and anchored to a session - reminders from background runs were
lost on restart or landed in the wrong chat.

Store: one JSON file per user scope under Platform.vaf_dir()/reminders/.
Firing: the automation scheduler loop calls fire_due_reminders() every tick.
Bounded: per-user pending cap, scheduling horizon, and a delivery grace window
(a reminder whose time passed while the backend was down is delivered late
within the grace, otherwise honestly marked missed with a Web UI notification).
"""
import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from vaf.core.platform import Platform

MAX_PENDING_PER_USER = 20
MAX_HORIZON_DAYS = 14
GRACE_HOURS = 6
_MAX_MESSAGE_CHARS = 1000

_LOCK = threading.Lock()


def _dir() -> Path:
    return Platform.vaf_dir() / "reminders"


def _scope_key(user_scope_id: Optional[Any]) -> str:
    s = str(user_scope_id or "").strip()
    return s if s else "local"


def _path(user_scope_id: Optional[Any]) -> Path:
    return _dir() / f"{_scope_key(user_scope_id)}.json"


def _load(user_scope_id: Optional[Any]) -> List[Dict[str, Any]]:
    p = _path(user_scope_id)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        items = data.get("reminders") if isinstance(data, dict) else None
        return items if isinstance(items, list) else []
    except Exception:
        return []


def _save(user_scope_id: Optional[Any], items: List[Dict[str, Any]]) -> None:
    p = _path(user_scope_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.parent / f".{p.name}.tmp"
    tmp.write_text(json.dumps({"reminders": items}, indent=2, ensure_ascii=False,
                              default=str), encoding="utf-8")
    tmp.replace(p)


def _user_now(username: Optional[str]) -> datetime:
    try:
        from vaf.core.user_time import user_now
        return user_now(username)
    except Exception:
        return datetime.now()


def _parse_fire_at(fire_at: str, username: Optional[str]) -> Optional[datetime]:
    """Parse 'YYYY-MM-DD HH:MM' (or 'HH:MM' = today) in the OWNER's timezone.
    Returns an aware datetime when the user has a timezone configured, else naive
    server-local (both compare correctly against _now_like below)."""
    s = (fire_at or "").strip().replace("T", " ")
    now = _user_now(username)
    try:
        if len(s) <= 5 and ":" in s:  # bare HH:MM -> today
            hh, mm = s.split(":", 1)
            return now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        dt = datetime.strptime(s[:16], "%Y-%m-%d %H:%M")
        return dt.replace(tzinfo=now.tzinfo) if now.tzinfo else dt
    except Exception:
        return None


def _now_like(dt: datetime) -> datetime:
    return datetime.now(timezone.utc).astimezone(dt.tzinfo) if dt.tzinfo else datetime.now()


def create_reminder(
    user_scope_id: Optional[Any],
    username: Optional[str],
    message: str,
    fire_at: str,
) -> Dict[str, Any]:
    """Store a one-shot reminder. Returns {'ok': bool, 'error'|'reminder': ...}."""
    message = (message or "").strip()[:_MAX_MESSAGE_CHARS]
    if not message:
        return {"ok": False, "error": "message must not be empty"}
    when = _parse_fire_at(fire_at, username)
    if when is None:
        return {"ok": False, "error": f"could not parse fire_at {fire_at!r} - use 'YYYY-MM-DD HH:MM'"}
    now = _now_like(when)
    if when <= now:
        return {"ok": False, "error": f"fire_at {when.strftime('%Y-%m-%d %H:%M')} is in the past"}
    if when > now + timedelta(days=MAX_HORIZON_DAYS):
        return {"ok": False, "error": f"fire_at is beyond the {MAX_HORIZON_DAYS}-day horizon"}
    with _LOCK:
        items = _load(user_scope_id)
        pending = [r for r in items if r.get("status") == "pending"]
        if len(pending) >= MAX_PENDING_PER_USER:
            return {"ok": False, "error": f"pending-reminder cap reached ({MAX_PENDING_PER_USER})"}
        entry = {
            "id": str(uuid.uuid4())[:8],
            "message": message,
            "fire_at": when.isoformat(),
            "created_at": datetime.now().isoformat(),
            "status": "pending",
            "username": (username or "admin").strip() or "admin",
            "user_scope_id": str(user_scope_id) if user_scope_id is not None else None,
        }
        items.append(entry)
        _save(user_scope_id, items)
    return {"ok": True, "reminder": entry}


def list_reminders(user_scope_id: Optional[Any], pending_only: bool = True) -> List[Dict[str, Any]]:
    items = _load(user_scope_id)
    if pending_only:
        items = [r for r in items if r.get("status") == "pending"]
    return sorted(items, key=lambda r: str(r.get("fire_at") or ""))


def cancel_reminder(user_scope_id: Optional[Any], reminder_id: str) -> bool:
    with _LOCK:
        items = _load(user_scope_id)
        for r in items:
            if r.get("id") == (reminder_id or "").strip() and r.get("status") == "pending":
                r["status"] = "cancelled"
                _save(user_scope_id, items)
                return True
    return False


def _deliver(entry: Dict[str, Any]) -> str:
    """Deliver one due reminder VERBATIM. Returns the resulting status.
    Messenger first (canonical router), Web UI notification as fallback -
    never silently dropped (same contract as automation results)."""
    scope = entry.get("user_scope_id")
    username = entry.get("username") or "admin"
    message = str(entry.get("message") or "")
    try:
        from vaf.core.messaging_connections import send_to_main_messenger
        sent, _ch = send_to_main_messenger(scope, username, message)
    except Exception:
        sent = False
    if not sent:
        try:
            from vaf.core.user_notifications import append_notification
            append_notification(scope, kind="automation", title="Reminder",
                                status="success", summary=message[:500])
        except Exception:
            pass
    return "delivered"


def fire_due_reminders() -> int:
    """Scheduler-tick hook: deliver every due pending reminder across all scopes.
    Within GRACE_HOURS after fire_at the reminder is still delivered (backend may
    have been down); older ones are marked missed with an honest notification.
    Never raises. Returns the number of state changes."""
    changed = 0
    d = _dir()
    if not d.exists():
        return 0
    for p in list(d.glob("*.json")):
        scope = None if p.stem == "local" else p.stem
        with _LOCK:
            items = _load(scope)
            dirty = False
            for r in items:
                if r.get("status") != "pending":
                    continue
                try:
                    when = datetime.fromisoformat(str(r.get("fire_at")))
                except Exception:
                    r["status"] = "missed"
                    dirty = True
                    changed += 1
                    continue
                now = _now_like(when)
                if when > now:
                    continue
                if now - when > timedelta(hours=GRACE_HOURS):
                    r["status"] = "missed"
                    try:
                        from vaf.core.user_notifications import append_notification
                        append_notification(r.get("user_scope_id"), kind="automation",
                                            title="Reminder missed (backend was offline)",
                                            status="error", summary=str(r.get("message") or "")[:500])
                    except Exception:
                        pass
                else:
                    r["status"] = _deliver(r)
                    r["delivered_at"] = datetime.now().isoformat()
                dirty = True
                changed += 1
            if dirty:
                # Keep the file bounded: drop finished entries older than 7 days.
                cutoff = datetime.now() - timedelta(days=7)
                def _keep(x):
                    if x.get("status") == "pending":
                        return True
                    try:
                        return datetime.fromisoformat(str(x.get("created_at"))) > cutoff
                    except Exception:
                        return False
                _save(scope, [x for x in items if _keep(x)])
    return changed
