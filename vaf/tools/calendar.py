# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
VAF Calendar Tools - list, create, update and delete the events of the VAF calendar.

The tools work on the user's own calendar (vaf/core/calendar_store.py), offline. A connected
Google or Microsoft account is a sync source: its events are in the store already, and an
event created here is mirrored into it by the sync supervisor unless the user wants it
internal. Times are read and printed in the user's zone (vaf/core/user_time.py); a bare
date makes an all-day event. Design: docs/integrations/CALENDAR_INTEGRATION.md.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from vaf.core import calendar_sync
from vaf.core.calendar_store import CalendarStore, store_for
from vaf.tools.base import BaseTool
from vaf.tools.mail_utils import cred_scope_from_kwargs, cred_username_from_kwargs

_CONNECT_HINT = "Connect Gmail or Outlook in Settings > Connections > Email; the same account is used for the calendar."


def _tz_username(kwargs: Dict[str, Any]) -> Optional[str]:
    """The caller's username for zone and format lookups (the local admin included; the
    credential lane treats the admin as None, the identity lane must not)."""
    return (kwargs.get("username") or "").strip() or None


def _open(kwargs: Dict[str, Any]) -> Tuple[CalendarStore, str, Optional[str]]:
    """(store, scope, username) for the caller."""
    username = _tz_username(kwargs)
    store = store_for(cred_username_from_kwargs(kwargs), cred_scope_from_kwargs(kwargs))
    return store, store.user_scope_id, username


def _zone(name: Optional[str]):
    try:
        return ZoneInfo(name) if name else None
    except Exception:
        return None


def _parse(text: Optional[str], username: Optional[str]):
    """(unix ts, all_day, tz name) or None."""
    s = (text or "").strip()
    if not s:
        return None
    from vaf.core.user_time import parse_user_datetime, resolve_user_timezone_name
    parsed = parse_user_datetime(s, username)
    if parsed is None:
        return None
    dt, all_day = parsed
    tz_name = resolve_user_timezone_name(username)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_zone(tz_name) or timezone.utc) if tz_name else dt.astimezone()
    return dt.timestamp(), all_day, tz_name


def _date_part(text: Optional[str]) -> Optional[str]:
    s = (text or "").strip()
    return s[:10] if len(s) >= 10 and s[4] == "-" and s[7] == "-" else None


def _when(ev: Dict[str, Any], username: Optional[str]) -> str:
    """The event's time the way the user reads it: date and time in their format, the end
    as a time when it is the same day; an all-day event by its date(s)."""
    from vaf.core.user_time import format_user_date, format_user_datetime, resolve_user_timezone_name
    # The USER's zone, not the event's: a Microsoft event arrives in UTC and a Google one in
    # the zone it was created in; the reader is in neither.
    zone = _zone(resolve_user_timezone_name(username)) or _zone(ev.get("tz")) or timezone.utc
    start = datetime.fromtimestamp(float(ev["start_ts"]), zone)
    if ev.get("all_day"):
        sd, ed = str(ev.get("start_date") or ""), str(ev.get("end_date") or "")
        text = format_user_date(start, username=username)
        try:
            days = (datetime.strptime(ed, "%Y-%m-%d") - datetime.strptime(sd, "%Y-%m-%d")).days
        except ValueError:
            days = 1
        if days > 1:
            last = datetime.fromtimestamp(float(ev["end_ts"]) - 1, zone)
            text += " - " + format_user_date(last, username=username)
        return text + " (all day)"
    end = datetime.fromtimestamp(float(ev["end_ts"]), zone)
    first = format_user_datetime(start, username=username, seconds=False)
    if end.date() == start.date():
        return f"{first} - {format_user_datetime(end, username=username, seconds=False).split(' ', 1)[1]}"
    return f"{first} - {format_user_datetime(end, username=username, seconds=False)}"


def _source(ev: Dict[str, Any]) -> str:
    if ev.get("account_id"):
        state = ev.get("sync_state") or ""
        label = {"pending_push": "not yet mirrored", "pending_delete": "being deleted",
                 "push_failed": "mirroring failed"}.get(state, "mirrored")
        return f"{ev.get('source') or 'vaf'}: {ev['account_id']} ({label})"
    return "internal"


def _requested_account(kwargs: Dict[str, Any], scope: str, store: CalendarStore):
    """The account an event is mirrored into. A provider or account the user named must be
    connected (else an error message the caller returns verbatim); nothing named means the
    push target, else the first connected account, else None (internal)."""
    provider = (kwargs.get("provider") or "").strip().lower() or None
    account_id = (kwargs.get("account_id") or "").strip() or None
    if not provider and not account_id:
        return None, calendar_sync.default_account_for(scope, store=store)
    for _s, _u, acc in calendar_sync.calendar_accounts_for(scope):
        if account_id and calendar_sync.account_id_of(acc) != account_id:
            continue
        if provider and calendar_sync.provider_of(acc) != provider:
            continue
        return None, acc
    wanted = account_id or provider
    return (f"No calendar account connected for {wanted}. {_CONNECT_HINT} "
            "Without provider and account_id the event goes into the VAF calendar."), None


def _resolve_contacts(kwargs: Dict[str, Any], username: Optional[str], scope: str):
    """(error message or None, contact ids). A name must match exactly one contact; the
    ambiguity answer follows get_contact so the model asks the user instead of guessing."""
    ids: List[str] = []
    contact_id = (kwargs.get("contact_id") or "").strip()
    name = (kwargs.get("contact") or "").strip()
    if not contact_id and not name:
        return None, ids
    try:
        from vaf.core.contacts_store import get_contact_by_id, get_contacts_by_name
    except ImportError as e:  # pragma: no cover - the contacts store ships with VAF
        return f"Contacts unavailable: {e}", ids
    if contact_id:
        c = get_contact_by_id(contact_id, username or "admin", user_scope_id=scope)
        if not c:
            return f"No contact with contact_id {contact_id}.", ids
        return None, [str(c.get("id"))]
    matches = get_contacts_by_name(name, username or "admin", user_scope_id=scope)
    if not matches:
        return (f"No contact found with name '{name}'. Use list_contacts to see existing contacts, "
                "or leave `contact` out to create the event without a contact link."), ids
    if len(matches) > 1:
        lines = [f"Multiple contacts have the name \"{name}\". You must ask the user which one they mean.",
                 "Contacts (retry with contact_id after the user confirms):"]
        for c in matches:
            lines.append(f"  - contact_id: {c.get('id') or '(no id)'} | {c.get('name') or ''}")
        return "\n".join(lines), ids
    return None, [str(matches[0].get("id"))]


def _mirror_note(ev: Dict[str, Any]) -> str:
    if ev.get("account_id"):
        return f" Mirrored into {ev['account_id']} by the next sync."
    return " Internal only (not mirrored into a connected calendar)."


class ListCalendarEventsTool(BaseTool):
    """List calendar events in a time range. Use when the user asks to see upcoming events, meetings, or schedule."""
    name = "list_calendar_events"
    category    = "calendar"
    identity_kwargs = ("user_scope_id", "username")
    permission_level = "read"
    side_effect_class = "none"
    description = (
        "List the user's calendar events in a time range, from the VAF calendar (internal events and the "
        "ones synced from Google Calendar or Microsoft Outlook alike). "
        "Use when the user asks: What's on my calendar? Upcoming meetings? My schedule? Termine? "
        "Optional: time_min, time_max (ISO 8601, YYYY-MM-DD HH:MM or YYYY-MM-DD, in the user's timezone); "
        "default is now to 7 days ahead. Optional: contact_id (only events linked to that contact)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "time_min": {
                "type": "string",
                "description": "Start of range (ISO 8601, YYYY-MM-DD HH:MM or YYYY-MM-DD). Default: now.",
            },
            "time_max": {
                "type": "string",
                "description": "End of range (a bare date means the end of that day). Default: 7 days from now.",
            },
            "contact_id": {
                "type": "string",
                "description": "Optional. Only events linked to this contact (from get_contact).",
            },
            "provider": {
                "type": "string",
                "enum": ["gmail", "microsoft"],
                "description": "Optional. Only events mirrored from this provider's account.",
            },
            "account_id": {
                "type": "string",
                "description": "Optional. Only events mirrored from this account (email address).",
            },
            "max_results": {
                "type": "integer",
                "description": "Optional. Max events to return (default 50).",
            },
        },
        "required": [],
    }

    def run(self, **kwargs) -> str:
        store, scope, username = _open(kwargs)
        now_ts = datetime.now(timezone.utc).timestamp()
        start = _parse(kwargs.get("time_min"), username)
        end = _parse(kwargs.get("time_max"), username)
        if kwargs.get("time_min") and start is None:
            return "time_min must be ISO 8601, YYYY-MM-DD HH:MM or YYYY-MM-DD."
        if kwargs.get("time_max") and end is None:
            return "time_max must be ISO 8601, YYYY-MM-DD HH:MM or YYYY-MM-DD."
        start_ts = start[0] if start else now_ts
        end_ts = (end[0] + (86400.0 if end[1] else 0.0)) if end else start_ts + 7 * 86400.0
        if end_ts < start_ts:
            return "time_max lies before time_min."
        provider = (kwargs.get("provider") or "").strip().lower() or None
        account_id = (kwargs.get("account_id") or "").strip() or None
        if provider or account_id:
            error, acc = _requested_account(kwargs, scope, store)
            if error:
                return error
            account_id = calendar_sync.account_id_of(acc)
        try:
            max_results = max(1, min(100, int(kwargs.get("max_results") or 50)))
        except (TypeError, ValueError):
            max_results = 50
        events = store.list_events(start_ts, end_ts, contact_id=(kwargs.get("contact_id") or "").strip() or None)
        if account_id:
            events = [e for e in events if e.get("account_id") == account_id]
        if not events:
            return "No events in the given range."
        lines = []
        for i, ev in enumerate(events[:max_results], 1):
            parts = [ev.get("title") or "(no title)", _when(ev, username)]
            if (ev.get("location") or "").strip():
                parts.append(str(ev["location"]).strip())
            parts.append(_source(ev))
            parts.append(f"id: {ev.get('id')}")
            lines.append(f"{i}. " + " | ".join(parts))
        more = len(events) - max_results
        tail = f"\n... and {more} more (raise max_results or narrow the range)." if more > 0 else ""
        return "Calendar events:\n" + "\n".join(lines) + tail


class CreateCalendarEventTool(BaseTool):
    """Create a calendar event. Use when the user wants to add a meeting, appointment, or reminder."""
    name = "create_calendar_event"
    category    = "calendar"
    identity_kwargs = ("user_scope_id", "username")
    permission_level = "write"
    side_effect_class = "reversible"
    description = (
        "Create an event in the user's calendar. Use when the user wants to schedule a meeting, add an "
        "appointment, or be reminded of a date. Requires summary and start (ISO 8601 or YYYY-MM-DD HH:MM "
        "in the user's timezone; a bare YYYY-MM-DD makes an all-day event). Optional: end (default one hour), "
        "description, location, contact (a contact's name, links the event to that contact; or contact_id), "
        "reminder_minutes (minutes before the start; default the user's setting, 0 for none), "
        "internal_only (keep the event out of the connected Google/Outlook calendar), provider or account_id "
        "(mirror into that specific connected account)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "Event title."},
            "start": {
                "type": "string",
                "description": "Start (ISO 8601 or YYYY-MM-DD HH:MM in the user's timezone, e.g. 2026-02-20T14:00; a bare YYYY-MM-DD means all day).",
            },
            "end": {"type": "string", "description": "Optional. End (same formats). Default: one hour after the start."},
            "description": {"type": "string", "description": "Optional. Event description/body."},
            "location": {"type": "string", "description": "Optional. Where it takes place."},
            "contact": {"type": "string", "description": "Optional. Name of a saved contact to link the event to."},
            "contact_id": {"type": "string", "description": "Optional. A contact's id (from get_contact) instead of the name."},
            "reminder_minutes": {
                "type": "integer",
                "description": "Optional. Minutes before the start to remind the user (default: the user's setting; 0: no reminder).",
            },
            "internal_only": {
                "type": "boolean",
                "description": "Optional. True keeps the event in the VAF calendar only, not in a connected Google/Outlook calendar.",
            },
            "provider": {
                "type": "string",
                "enum": ["gmail", "microsoft"],
                "description": "Optional. Mirror into this provider's connected account. Default: the user's push target or first connected account.",
            },
            "account_id": {"type": "string", "description": "Optional. Mirror into this connected account (email address)."},
        },
        "required": ["summary", "start"],
    }

    def run(self, **kwargs) -> str:
        store, scope, username = _open(kwargs)
        summary = (kwargs.get("summary") or "").strip()
        if not summary:
            return "summary is required."
        start = _parse(kwargs.get("start"), username)
        if start is None:
            return "start is required (ISO 8601 or YYYY-MM-DD HH:MM, e.g. 2026-02-20T14:00; a bare date means all day)."
        start_ts, all_day, tz_name = start
        end = _parse(kwargs.get("end"), username)
        if kwargs.get("end") and end is None:
            return "end must be ISO 8601 or YYYY-MM-DD HH:MM."
        end_ts = end[0] if end else None
        if end_ts is not None and end_ts < start_ts:
            return "end lies before start."
        error, contact_ids = _resolve_contacts(kwargs, username, scope)
        if error:
            return error
        account_id = None
        if not bool(kwargs.get("internal_only")):
            error, acc = _requested_account(kwargs, scope, store)
            if error:
                return error
            account_id = calendar_sync.account_id_of(acc) if acc else None
        extra: Dict[str, Any] = {}
        if kwargs.get("reminder_minutes") is not None:
            try:
                extra["reminder_minutes"] = max(0, int(kwargs.get("reminder_minutes")))
            except (TypeError, ValueError):
                pass
        ev = store.add_event(
            title=summary, start_ts=start_ts, end_ts=end_ts, all_day=all_day, tz=tz_name,
            description=(kwargs.get("description") or "").strip(), location=(kwargs.get("location") or "").strip(),
            contact_ids=contact_ids, created_by="agent", account_id=account_id,
            start_date=_date_part(kwargs.get("start")) if all_day else None,
            end_date=_date_part(kwargs.get("end")) if (all_day and kwargs.get("end")) else None,
            **extra,
        )
        calendar_sync.after_local_change(scope, ev.get("account_id"))
        reminder = (f" Reminder {ev['reminder_minutes']} minutes before." if ev.get("reminder_minutes") else "")
        return f"Created event: {ev['title']} ({_when(ev, username)}). Id: {ev['id']}.{reminder}{_mirror_note(ev)}"


class UpdateCalendarEventTool(BaseTool):
    """Update an existing calendar event."""
    name = "update_calendar_event"
    category    = "calendar"
    identity_kwargs = ("user_scope_id", "username")
    permission_level = "write"
    side_effect_class = "reversible"
    description = (
        "Update an existing calendar event (title, time, description, location, reminder). "
        "Requires event_id (from list_calendar_events). Optional: summary, start, end, description, location, "
        "reminder_minutes (0 removes the reminder). A moved start keeps the event's duration unless end is given."
    )
    parameters = {
        "type": "object",
        "properties": {
            "event_id": {"type": "string", "description": "Event ID (from list_calendar_events)."},
            "summary": {"type": "string", "description": "Optional. New title."},
            "start": {"type": "string", "description": "Optional. New start (ISO 8601 or YYYY-MM-DD HH:MM in the user's timezone)."},
            "end": {"type": "string", "description": "Optional. New end."},
            "description": {"type": "string", "description": "Optional. New description."},
            "location": {"type": "string", "description": "Optional. New location."},
            "reminder_minutes": {"type": "integer", "description": "Optional. Minutes before the start; 0 removes the reminder."},
            "provider": {"type": "string", "enum": ["gmail", "microsoft"], "description": "Ignored; kept for older callers."},
            "account_id": {"type": "string", "description": "Ignored; kept for older callers."},
            "calendar_id": {"type": "string", "description": "Ignored; kept for older callers."},
        },
        "required": ["event_id"],
    }

    def run(self, **kwargs) -> str:
        store, scope, username = _open(kwargs)
        event_id = (kwargs.get("event_id") or "").strip()
        if not event_id:
            return "event_id is required."
        cur = store.get_event(event_id)
        if not cur:
            return f"Event not found: {event_id}. Use list_calendar_events to find the id."
        fields: Dict[str, Any] = {}
        if (kwargs.get("summary") or "").strip():
            fields["title"] = kwargs["summary"].strip()
        for key in ("description", "location"):
            if kwargs.get(key) is not None:
                fields[key] = str(kwargs.get(key)).strip()
        if kwargs.get("reminder_minutes") is not None:
            try:
                fields["reminder_minutes"] = max(0, int(kwargs.get("reminder_minutes")))
            except (TypeError, ValueError):
                return "reminder_minutes must be a number of minutes."
        all_day = bool(cur.get("all_day"))
        start = _parse(kwargs.get("start"), username)
        if kwargs.get("start") and start is None:
            return "start must be ISO 8601 or YYYY-MM-DD HH:MM."
        end = _parse(kwargs.get("end"), username)
        if kwargs.get("end") and end is None:
            return "end must be ISO 8601 or YYYY-MM-DD HH:MM."
        if start is not None:
            all_day = start[1]
            fields["start_ts"], fields["all_day"] = start[0], all_day
            if start[2]:
                fields["tz"] = start[2]
            fields["start_date"] = _date_part(kwargs.get("start")) if all_day else None
            if end is None:
                if all_day:
                    fields["end_ts"], fields["end_date"] = start[0] + 86400.0, None     # the store derives the next day
                else:
                    fields["end_ts"] = start[0] + (float(cur["end_ts"]) - float(cur["start_ts"]))
        if end is not None:
            fields["end_ts"] = end[0]
            if all_day:
                fields["end_date"] = _date_part(kwargs.get("end"))
        if fields.get("end_ts", float(cur["end_ts"])) < fields.get("start_ts", float(cur["start_ts"])):
            return "end lies before start."
        if not fields:
            return "Nothing to change: give summary, start, end, description, location or reminder_minutes."
        ev = store.update_event(event_id, **fields)
        if not ev:
            return f"Event not found: {event_id}."
        calendar_sync.after_local_change(scope, ev.get("account_id"))
        return f"Updated event: {ev['title']} ({_when(ev, username)}).{_mirror_note(ev) if ev.get('account_id') else ''}"


class DeleteCalendarEventTool(BaseTool):
    """Delete a calendar event."""
    name = "delete_calendar_event"
    category    = "calendar"
    identity_kwargs = ("user_scope_id", "username")
    permission_level = "write"
    side_effect_class = "irreversible"
    description = (
        "Delete a calendar event. Requires event_id (from list_calendar_events). A mirrored event is also "
        "removed from the connected Google/Outlook calendar by the next sync."
    )
    parameters = {
        "type": "object",
        "properties": {
            "event_id": {"type": "string", "description": "Event ID (from list_calendar_events)."},
            "provider": {"type": "string", "enum": ["gmail", "microsoft"], "description": "Ignored; kept for older callers."},
            "account_id": {"type": "string", "description": "Ignored; kept for older callers."},
            "calendar_id": {"type": "string", "description": "Ignored; kept for older callers."},
        },
        "required": ["event_id"],
    }

    def run(self, **kwargs) -> str:
        store, scope, _username = _open(kwargs)
        event_id = (kwargs.get("event_id") or "").strip()
        if not event_id:
            return "event_id is required."
        ev = store.delete_event(event_id)
        if ev is None:
            return f"Event not found: {event_id} (may already be deleted)."
        mirrored = bool(ev.get("external_id") and ev.get("account_id"))
        calendar_sync.after_local_change(scope, ev.get("account_id") if mirrored else None)
        if mirrored:
            return f"Event deleted: {ev['title']}. It is removed from {ev['account_id']} by the next sync."
        return f"Event deleted: {ev['title']}."
