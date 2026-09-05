# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Calendar API for the Web UI: the VAF calendar (vaf/core/calendar_store.py) with its
sync state (vaf/core/calendar_sync.py). Design: docs/integrations/CALENDAR_INTEGRATION.md.

Every route works on the caller's own calendar (one store per user scope), so an event id
from another scope is simply not found. Times arrive as the user types them and are read in
the user's zone through vaf.core.user_time.parse_user_datetime; a bare date means all day.
A local write tells the browser to refetch and, when the event is mirrored, asks the sync
supervisor to push now. Google and Microsoft accounts are sync sources here: the status
lists them with their sync state, the settings decide which one new events go to.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from vaf.core import calendar_sync
from vaf.core.calendar_store import DEFAULT_REMINDER_MINUTES, CalendarStore
from vaf.core.config import get_local_admin_scope_id

# Use the same account-config SSOT as mail so calendar status sees the same accounts as Email
from vaf.core.email_accounts import get_email_config as _get_email_config

logger = logging.getLogger("vaf.api.calendar")

router = APIRouter(prefix="/api/calendar", tags=["calendar"])


def _get_current_user(request: Request) -> Dict[str, Any]:
    """Current user with username and user_scope_id (from auth or local admin)."""
    from vaf.api.config_routes import get_current_user_or_local_admin
    return get_current_user_or_local_admin(request)


def _identity(user: Dict[str, Any]):
    username = (user.get("username") or "admin").strip() or "admin"
    scope = str(user.get("user_scope_id") or "").strip() or get_local_admin_scope_id()
    return username, scope


def _calendar_accounts(username: str, scope: str) -> List[Dict[str, Any]]:
    ec = _get_email_config(username, user_scope_id=scope)
    return [a for a in (ec.get("accounts") or []) if calendar_sync.wants_calendar_sync(a)]


def _zone(name: Optional[str]):
    try:
        return ZoneInfo(name) if name else timezone.utc
    except Exception:
        return timezone.utc


def _event_json(ev: Dict[str, Any], username: str) -> Dict[str, Any]:
    """The event as the window reads it: the stored row plus `start`/`end` as ISO strings in
    the USER's zone (the event's own zone is where it was created, which is not where the
    reader is; a Microsoft event arrives in UTC)."""
    from vaf.core.user_time import resolve_user_timezone_name
    tz_name = resolve_user_timezone_name(username) or ev.get("tz") or "UTC"
    zone = _zone(tz_name)
    if ev.get("all_day"):
        start, end = ev.get("start_date"), ev.get("end_date")
    else:
        start = datetime.fromtimestamp(float(ev["start_ts"]), zone).isoformat()
        end = datetime.fromtimestamp(float(ev["end_ts"]), zone).isoformat()
    keys = ("id", "title", "description", "location", "start_ts", "end_ts", "all_day", "start_date", "end_date",
            "tz", "status", "source", "account_id", "sync_state", "last_error", "link", "contact_ids",
            "reminder_minutes", "reminder_fired_at", "created_by", "created_at", "updated_at")
    out = {k: ev.get(k) for k in keys}
    out["start"], out["end"] = start, end
    return out


def _parse_when(text: Optional[str], username: str, *, what: str, end_of_day: bool = False):
    """(unix ts, all_day, tz name) for a user-typed time, or None when nothing was given.
    HTTP 400 for text that is not a time. `end_of_day` reads a bare date as the end of
    that day, the shape a range end has."""
    s = (text or "").strip()
    if not s:
        return None
    from vaf.core.user_time import parse_user_datetime, resolve_user_timezone_name
    parsed = parse_user_datetime(s, username)
    if parsed is None:
        raise HTTPException(status_code=400, detail=f"{what} must be ISO 8601, YYYY-MM-DD HH:MM or YYYY-MM-DD")
    dt, all_day = parsed
    tz_name = resolve_user_timezone_name(username)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_zone(tz_name))
    ts = dt.timestamp()
    if all_day and end_of_day:
        ts += 86400.0
    return ts, all_day, tz_name


def _status_payload(username: str, scope: str) -> Dict[str, Any]:
    accounts = _calendar_accounts(username, scope)
    google_available = any(calendar_sync.provider_of(a) == "gmail" for a in accounts)
    microsoft_available = any(calendar_sync.provider_of(a) == "microsoft" for a in accounts)
    has_calendar = CalendarStore.exists(scope)
    settings: Dict[str, Any] = {"push_target": None, "default_reminder_minutes": DEFAULT_REMINDER_MINUTES}
    states: Dict[str, Dict[str, Any]] = {}
    if has_calendar or accounts:
        store = CalendarStore(scope)
        settings = store.settings()
        states = {st["account_id"]: st for st in store.list_account_states()}
        has_calendar = True
    rows = []
    for acc in accounts:
        aid = calendar_sync.account_id_of(acc)
        st = states.get(aid) or {}
        rows.append({
            "account_id": aid,
            "email": acc.get("email") or aid,
            "provider": calendar_sync.provider_of(acc),
            "enabled": bool(st.get("enabled", True)),
            "last_sync_at": st.get("last_sync_at"),
            "last_error": st.get("last_error"),
            "needs_reconsent": bool(st.get("needs_reconsent")),
        })
    return {
        "google_available": google_available,
        "microsoft_available": microsoft_available,
        "has_calendar": has_calendar,
        "accounts": rows,
        "settings": settings,
        "sync": {
            "interval_minutes": int(calendar_sync.sync_interval_seconds() // 60),
            "push_enabled": calendar_sync.push_enabled(),
            "supervisor_running": calendar_sync.running_supervisor(calendar_sync.NAME) is not None,
        },
    }


@router.get("/status")
async def calendar_status(_user: Dict[str, Any] = Depends(_get_current_user)):
    """The calendar of the current user: whether a Google or Microsoft account is connected
    (`google_available`/`microsoft_available`, as before), every calendar account with its
    sync state, the settings (push target, default reminder) and the sync cadence."""
    username, scope = _identity(_user)
    payload = _status_payload(username, scope)
    msg = (f"calendar status: user={username} scope={scope[:8]}.. accounts={len(payload['accounts'])} "
           f"gmail={payload['google_available']} ms={payload['microsoft_available']} calendar={payload['has_calendar']}")
    logger.info("%s", msg)
    try:
        from vaf.core.log_helper import append_domain_log_always
        append_domain_log_always("backend", f"[CALENDAR] {msg}")
    except Exception:
        pass
    return payload


@router.get("/events")
async def calendar_events(
    time_min: Optional[str] = Query(None, description="Start of range (ISO 8601, YYYY-MM-DD HH:MM or YYYY-MM-DD)"),
    time_max: Optional[str] = Query(None, description="End of range; a bare date means the end of that day"),
    contact_id: Optional[str] = Query(None, description="Only events linked to this contact"),
    include_cancelled: bool = Query(False),
    _user: Dict[str, Any] = Depends(_get_current_user),
):
    """The events of the current user's calendar in the range (default: the next 7 days),
    mirrored and internal alike, from the store; no provider call."""
    username, scope = _identity(_user)
    now_ts = datetime.now(timezone.utc).timestamp()
    start = _parse_when(time_min, username, what="time_min")
    end = _parse_when(time_max, username, what="time_max", end_of_day=True)
    start_ts = start[0] if start else now_ts
    end_ts = end[0] if end else start_ts + 7 * 86400.0
    if end_ts < start_ts:
        raise HTTPException(status_code=400, detail="time_max lies before time_min")
    store = CalendarStore(scope)
    events = store.list_events(start_ts, end_ts, include_cancelled=include_cancelled,
                               contact_id=(contact_id or "").strip() or None)
    account = calendar_sync.default_account_for(scope, store=store)
    return {"events": [_event_json(e, username) for e in events],
            "account": calendar_sync.account_id_of(account) if account else None}


class EventCreate(BaseModel):
    title: str
    start: str
    end: Optional[str] = None
    all_day: Optional[bool] = None
    description: Optional[str] = ""
    location: Optional[str] = ""
    contact_ids: Optional[List[str]] = None
    reminder_minutes: Optional[int] = None      # None: the user's default; 0: none
    internal_only: bool = False
    account_id: Optional[str] = None


class EventPatch(BaseModel):
    title: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None
    all_day: Optional[bool] = None
    description: Optional[str] = None
    location: Optional[str] = None
    contact_ids: Optional[List[str]] = None
    reminder_minutes: Optional[int] = None


def _date_part(text: Optional[str]) -> Optional[str]:
    s = (text or "").strip()
    return s[:10] if len(s) >= 10 and s[4] == "-" and s[7] == "-" else None


@router.post("/events")
async def create_calendar_event(body: EventCreate, _user: Dict[str, Any] = Depends(_get_current_user)):
    """Create an event in the current user's calendar. Unless `internal_only`, it is mirrored
    into the named account, else the push target, else the first connected calendar account;
    without one it stays internal."""
    username, scope = _identity(_user)
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title must not be empty")
    start = _parse_when(body.start, username, what="start")
    if start is None:
        raise HTTPException(status_code=400, detail="start is required")
    start_ts, start_all_day, tz_name = start
    all_day = bool(body.all_day) if body.all_day is not None else start_all_day
    end = _parse_when(body.end, username, what="end")
    end_ts = end[0] if end else None
    if end_ts is not None and end_ts < start_ts:
        raise HTTPException(status_code=400, detail="end lies before start")
    store = CalendarStore(scope)
    account_id = None
    if not body.internal_only:
        account = calendar_sync.default_account_for(scope, body.account_id, store)
        if body.account_id and account is None:
            raise HTTPException(status_code=400, detail="that calendar account is not connected")
        account_id = calendar_sync.account_id_of(account) if account else None
    kwargs: Dict[str, Any] = {}
    if body.reminder_minutes is not None:
        kwargs["reminder_minutes"] = max(0, int(body.reminder_minutes))
    ev = store.add_event(
        title=title, start_ts=start_ts, end_ts=end_ts, all_day=all_day, tz=tz_name,
        description=body.description or "", location=body.location or "",
        contact_ids=body.contact_ids or [], created_by="user", account_id=account_id,
        start_date=_date_part(body.start) if all_day else None,
        end_date=_date_part(body.end) if (all_day and body.end) else None,
        **kwargs,
    )
    calendar_sync.after_local_change(scope, ev.get("account_id"))
    return {"event": _event_json(ev, username)}


@router.patch("/events/{event_id}")
async def update_calendar_event(event_id: str, body: EventPatch, _user: Dict[str, Any] = Depends(_get_current_user)):
    """Change the given fields of one event of the current user's calendar."""
    username, scope = _identity(_user)
    store = CalendarStore(scope)
    cur = store.get_event(event_id)
    if not cur:
        raise HTTPException(status_code=404, detail="Event not found")
    fields: Dict[str, Any] = {}
    if body.title is not None:
        if not body.title.strip():
            raise HTTPException(status_code=400, detail="title must not be empty")
        fields["title"] = body.title.strip()
    all_day = bool(body.all_day) if body.all_day is not None else bool(cur.get("all_day"))
    start = _parse_when(body.start, username, what="start")
    if start is not None:
        fields["start_ts"] = start[0]
        if body.all_day is None and body.start:
            all_day = start[1]
        if start[2]:
            fields["tz"] = start[2]
        if all_day:
            fields["start_date"] = _date_part(body.start)
    end = _parse_when(body.end, username, what="end")
    if end is not None:
        fields["end_ts"] = end[0]
        if all_day:
            fields["end_date"] = _date_part(body.end)
    if body.all_day is not None or start is not None:
        fields["all_day"] = all_day
    for key in ("description", "location", "contact_ids"):
        value = getattr(body, key)
        if value is not None:
            fields[key] = value
    if body.reminder_minutes is not None:
        fields["reminder_minutes"] = max(0, int(body.reminder_minutes))
    new_start = fields.get("start_ts", float(cur["start_ts"]))
    if "start_ts" in fields and "end_ts" not in fields:
        if all_day:
            fields["end_ts"], fields["end_date"] = new_start + 86400.0, None      # the store derives the next day
        else:
            fields["end_ts"] = new_start + (float(cur["end_ts"]) - float(cur["start_ts"]))   # a move keeps the duration
    elif fields.get("end_ts", float(cur["end_ts"])) < new_start:
        raise HTTPException(status_code=400, detail="end lies before start")
    ev = store.update_event(event_id, **fields) if fields else cur
    calendar_sync.after_local_change(scope, ev.get("account_id"))
    return {"event": _event_json(ev, username)}


@router.delete("/events/{event_id}")
async def delete_calendar_event(event_id: str, _user: Dict[str, Any] = Depends(_get_current_user)):
    """Remove one event. A mirrored one is gone from the views at once and deleted at the
    provider by the next push (`pending_delete` until then)."""
    _username, scope = _identity(_user)
    store = CalendarStore(scope)
    ev = store.delete_event(event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="Event not found")
    mirrored = bool(ev.get("external_id") and ev.get("account_id"))
    calendar_sync.after_local_change(scope, ev.get("account_id") if mirrored else None)
    return {"ok": True, "pending_delete": mirrored}


@router.post("/sync")
async def sync_now(account_id: Optional[str] = Query(None), _user: Dict[str, Any] = Depends(_get_current_user)):
    """Sync the current user's calendar accounts now (or the one named) and report per
    account. Runs in a worker thread, serialised per account against the supervisor."""
    _username, scope = _identity(_user)
    results = await asyncio.to_thread(calendar_sync.sync_scope_now, scope, (account_id or "").strip() or None)
    return {"results": results, "changed": sum(int(r.get("changed") or 0) for r in results)}


class SettingsBody(BaseModel):
    push_target: Optional[str] = None
    default_reminder_minutes: Optional[int] = None
    accounts: Optional[List[Dict[str, Any]]] = None    # [{"account_id": ..., "enabled": bool}]


@router.put("/settings")
async def update_settings(body: SettingsBody, _user: Dict[str, Any] = Depends(_get_current_user)):
    """The push target (which account new events go to), the default reminder, and the
    per-account sync switch. Answers with the full status."""
    username, scope = _identity(_user)
    store = CalendarStore(scope)
    known = {calendar_sync.account_id_of(a) for a in _calendar_accounts(username, scope)}
    if body.push_target is not None:
        target = body.push_target.strip()
        if target and target not in known:
            raise HTTPException(status_code=400, detail="that calendar account is not connected")
        store.set_settings(push_target=target)
    if body.default_reminder_minutes is not None:
        store.set_settings(default_reminder_minutes=max(0, int(body.default_reminder_minutes)))
    for row in body.accounts or []:
        aid = str((row or {}).get("account_id") or "").strip()
        if aid and aid in known:
            store.set_account_enabled(aid, bool(row.get("enabled", True)))
    calendar_sync.after_local_change(scope)
    return _status_payload(username, scope)


# Name of the auto-created daily calendar check automation (visible in Automations UI).
CALENDAR_DAILY_CHECK_NAME = "Daily calendar check"

DEFAULT_CALENDAR_CHECK_PROMPT = """You are running the daily calendar check. Your job is to think carefully and act, not just list events.

1. Call list_calendar_events for the next 24 to 48 hours (use time_min and time_max as ISO8601).

2. Analyze each event: How important is it? Consider: meetings, deadlines, presentations, reviews, customer calls, first or last appointment of the day, long duration. Ignore low-value blocks (e.g. "Focus time", "Lunch" unless relevant).

3. For each important event, decide what to do:
   - Reminder: If the event is in the future and the user would benefit from a reminder (e.g. 30 minutes before), call schedule_reminder(message=..., fire_at="YYYY-MM-DD HH:MM"). Write the FINAL reminder text into message - it is delivered VERBATIM at fire_at on the user's main messenger, no agent processes it. Use the event's date and a time 30 minutes before its start.
   - Prepare now: If the event starts within the next 30 to 60 minutes, send a reminder or preparation help now with send_to_user: a short summary and "Your meeting [X] starts soon".
   - Optional: Use memory_search to find relevant notes for a meeting and include a one-line hint in the reminder.

4. Execute: Actually call schedule_reminder for each future reminder you decided on, and send_to_user for any immediate ones. Do not just say what you would do - do it. (create_automation is not available in this run - never try to create automations here.)

5. At the end, reply briefly in the user's language: what you found, how many events, which reminders or messages you created/sent."""


@router.post("/ensure-daily-check-automation")
async def ensure_daily_check_automation(_user: Dict[str, Any] = Depends(_get_current_user)):
    """
    If the current user has a calendar (a connected account, or events in the VAF calendar)
    and does not yet have the "Daily calendar check" automation, create it. This automation
    appears in the Automations UI and runs daily at 08:00 (user can change time).
    Idempotent: safe to call every time; only creates if missing.
    """
    username, scope = _identity(_user)
    user_scope_id = _user.get("user_scope_id")
    has_account = bool(_calendar_accounts(username, scope))
    has_events = CalendarStore.exists(scope) and CalendarStore(scope).count_events() > 0
    if not has_account and not has_events:
        return {"ok": False, "created": False, "reason": "no_calendar"}
    try:
        from vaf.core.automation import AutomationManager, AutomationTask
        local_scope = get_local_admin_scope_id()
        # Local admin: store in root automations/ so CLI scheduler (get_manager()) sees it.
        use_scope = None if (not user_scope_id or str(user_scope_id).strip() == str(local_scope).strip()) else user_scope_id
        mgr = AutomationManager(user_scope_id=use_scope) if use_scope else AutomationManager()
        existing = [t for t in mgr.list() if (t.name or "").strip() == CALENDAR_DAILY_CHECK_NAME]
        if existing:
            return {"ok": True, "created": False, "task_id": existing[0].id}
        task = AutomationTask(
            name=CALENDAR_DAILY_CHECK_NAME,
            prompt=DEFAULT_CALENDAR_CHECK_PROMPT,
            frequency="daily",
            time="08:00",
            enabled=True,
            user_scope_id=user_scope_id,  # Store scope on task so run_task sets agent context
        )
        task = mgr.create(task)
        logger.info("Created daily calendar check automation for user scope %s", (user_scope_id or "local")[:8])
        return {"ok": True, "created": True, "task_id": task.id}
    except Exception as e:
        logger.exception("Failed to ensure daily calendar check automation")
        return {"ok": False, "created": False, "error": str(e)}
