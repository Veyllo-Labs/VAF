"""
VAF Calendar Tools - List, create, update, and delete calendar events (Google Calendar, Microsoft Outlook).
Uses the same OAuth connection as email (Gmail/Outlook). User must connect Gmail or Outlook in Settings → Connections → Email first.
"""

from datetime import datetime, timezone, timedelta
from typing import Optional

from vaf.core.calendar_client import (
    create_event as client_create_event,
    delete_event as client_delete_event,
    list_events as client_list_events,
    resolve_calendar_account,
    update_event as client_update_event,
    get_calendar_accounts,
)
from vaf.tools.base import BaseTool
from vaf.tools.mail_utils import cred_scope_from_kwargs, cred_username_from_kwargs


def _default_time_bounds(days_ahead: int = 7):
    """Return (time_min, time_max) as ISO strings for the next days_ahead days."""
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=days_ahead)
    return now.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z")


class ListCalendarEventsTool(BaseTool):
    """List calendar events in a time range. Use when the user asks to see upcoming events, meetings, or schedule."""
    name = "list_calendar_events"
    description = (
        "List calendar events (Google Calendar or Microsoft Outlook) for the user in a given time range. "
        "Use when the user asks: What's on my calendar? Upcoming meetings? My schedule? Termine? "
        "Requires a connected Gmail or Outlook account (Settings → Connections → Email). "
        "Optional: time_min, time_max (ISO8601 or YYYY-MM-DD); default is next 7 days. "
        "Optional: provider (gmail/microsoft), account_id, calendar_id."
    )
    parameters = {
        "type": "object",
        "properties": {
            "time_min": {
                "type": "string",
                "description": "Start of range (ISO8601 or YYYY-MM-DD). Default: now.",
            },
            "time_max": {
                "type": "string",
                "description": "End of range (ISO8601 or YYYY-MM-DD). Default: 7 days from now.",
            },
            "provider": {
                "type": "string",
                "enum": ["gmail", "microsoft"],
                "description": "Optional. Which calendar (gmail or microsoft). If omitted, uses first connected.",
            },
            "account_id": {
                "type": "string",
                "description": "Optional. Email of the calendar account. If omitted, uses first connected.",
            },
            "calendar_id": {
                "type": "string",
                "description": "Optional. Calendar ID (Google: primary or id; Microsoft: calendar id). Default: primary/default.",
            },
            "max_results": {
                "type": "integer",
                "description": "Optional. Max events to return (default 50).",
            },
        },
        "required": [],
    }

    def run(self, **kwargs) -> str:
        user_scope_id = cred_scope_from_kwargs(kwargs)
        username = cred_username_from_kwargs(kwargs)
        account = resolve_calendar_account(
            provider=kwargs.get("provider"),
            account_id=kwargs.get("account_id"),
            username=username,
            user_scope_id=user_scope_id,
        )
        if not account:
            accounts = get_calendar_accounts(username=username, user_scope_id=user_scope_id)
            if not accounts:
                return (
                    "No calendar account connected. Connect Gmail or Outlook in Settings → Connections → Email; "
                    "the same account is used for calendar."
                )
            return (
                "Could not resolve calendar account. Connected: "
                + ", ".join((a.get("email") or a.get("account_id") or "") for a in accounts)
                + ". Use provider (gmail/microsoft) or account_id."
            )
        provider = (account.get("provider") or "gmail").strip().lower()
        account_id = account.get("account_id") or account.get("email") or ""
        time_min = (kwargs.get("time_min") or "").strip()
        time_max = (kwargs.get("time_max") or "").strip()
        if not time_min or not time_max:
            tmin, tmax = _default_time_bounds()
            time_min = time_min or tmin
            time_max = time_max or tmax
        calendar_id = (kwargs.get("calendar_id") or "").strip() or None
        max_results = kwargs.get("max_results")
        if max_results is None:
            max_results = 50
        try:
            max_results = max(1, min(100, int(max_results)))
        except (TypeError, ValueError):
            max_results = 50
        events = client_list_events(
            provider=provider,
            account_id=account_id,
            user_scope_id=user_scope_id,
            time_min=time_min,
            time_max=time_max,
            calendar_id=calendar_id,
            username=username,
            max_results=max_results,
        )
        if not events:
            return f"No events in the given range for {account_id}."
        lines = []
        for i, ev in enumerate(events, 1):
            summary = ev.get("summary") or "(no title)"
            start = ev.get("start") or ""
            end = ev.get("end") or ""
            eid = ev.get("id") or ""
            lines.append(f"{i}. {summary} | {start} – {end} | id: {eid}")
        return "Calendar events:\n" + "\n".join(lines)


class CreateCalendarEventTool(BaseTool):
    """Create a calendar event. Use when the user wants to add a meeting, appointment, or reminder."""
    name = "create_calendar_event"
    description = (
        "Create a calendar event (Google Calendar or Microsoft Outlook). "
        "Use when the user wants to schedule a meeting, add an appointment, or set a reminder. "
        "Requires summary, start, and end (ISO8601 or YYYY-MM-DDTHH:MM). "
        "Optional: description, provider, account_id, calendar_id, reminder_minutes."
    )
    parameters = {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "Event title/summary.",
            },
            "start": {
                "type": "string",
                "description": "Start date-time (ISO8601 or YYYY-MM-DDTHH:MM, e.g. 2025-02-20T14:00:00).",
            },
            "end": {
                "type": "string",
                "description": "End date-time (ISO8601 or YYYY-MM-DDTHH:MM).",
            },
            "description": {
                "type": "string",
                "description": "Optional. Event description/body.",
            },
            "provider": {
                "type": "string",
                "enum": ["gmail", "microsoft"],
                "description": "Optional. gmail or microsoft. Default: first connected.",
            },
            "account_id": {
                "type": "string",
                "description": "Optional. Calendar account email.",
            },
            "calendar_id": {
                "type": "string",
                "description": "Optional. Calendar ID (default: primary).",
            },
            "reminder_minutes": {
                "type": "integer",
                "description": "Optional. Minutes before start to remind (e.g. 15).",
            },
        },
        "required": ["summary", "start", "end"],
    }

    def run(self, **kwargs) -> str:
        user_scope_id = cred_scope_from_kwargs(kwargs)
        username = cred_username_from_kwargs(kwargs)
        account = resolve_calendar_account(
            provider=kwargs.get("provider"),
            account_id=kwargs.get("account_id"),
            username=username,
            user_scope_id=user_scope_id,
        )
        if not account:
            return (
                "No calendar account connected. Connect Gmail or Outlook in Settings → Connections → Email."
            )
        provider = (account.get("provider") or "gmail").strip().lower()
        account_id = account.get("account_id") or account.get("email") or ""
        summary = (kwargs.get("summary") or "").strip()
        start = (kwargs.get("start") or "").strip()
        end = (kwargs.get("end") or "").strip()
        if not summary:
            return "summary is required."
        if not start or not end:
            return "start and end are required (e.g. 2025-02-20T14:00:00)."
        description = (kwargs.get("description") or "").strip() or None
        calendar_id = (kwargs.get("calendar_id") or "").strip() or None
        reminder_minutes = kwargs.get("reminder_minutes")
        if reminder_minutes is not None:
            try:
                reminder_minutes = int(reminder_minutes)
            except (TypeError, ValueError):
                reminder_minutes = None
        ev = client_create_event(
            provider=provider,
            account_id=account_id,
            user_scope_id=user_scope_id,
            summary=summary,
            start=start,
            end=end,
            description=description,
            calendar_id=calendar_id,
            username=username,
            reminder_minutes=reminder_minutes,
        )
        if not ev:
            return "Failed to create the event. Check that the account has calendar access (reconnect in Settings if needed)."
        return f"Created event: {ev.get('summary')} ({ev.get('start')} – {ev.get('end')}). Id: {ev.get('id')}."


class UpdateCalendarEventTool(BaseTool):
    """Update an existing calendar event."""
    name = "update_calendar_event"
    description = (
        "Update an existing calendar event (change title, time, or description). "
        "Requires event_id (from list_calendar_events). Optional: summary, start, end, description, provider, calendar_id."
    )
    parameters = {
        "type": "object",
        "properties": {
            "event_id": {
                "type": "string",
                "description": "Event ID (from list_calendar_events).",
            },
            "summary": {"type": "string", "description": "Optional. New title."},
            "start": {"type": "string", "description": "Optional. New start (ISO8601)."},
            "end": {"type": "string", "description": "Optional. New end (ISO8601)."},
            "description": {"type": "string", "description": "Optional. New description."},
            "provider": {
                "type": "string",
                "enum": ["gmail", "microsoft"],
                "description": "Optional. gmail or microsoft.",
            },
            "account_id": {"type": "string", "description": "Optional. Calendar account email."},
            "calendar_id": {"type": "string", "description": "Optional. Calendar ID."},
        },
        "required": ["event_id"],
    }

    def run(self, **kwargs) -> str:
        user_scope_id = cred_scope_from_kwargs(kwargs)
        username = cred_username_from_kwargs(kwargs)
        event_id = (kwargs.get("event_id") or "").strip()
        if not event_id:
            return "event_id is required."
        account = resolve_calendar_account(
            provider=kwargs.get("provider"),
            account_id=kwargs.get("account_id"),
            username=username,
            user_scope_id=user_scope_id,
        )
        if not account:
            return "No calendar account connected. Connect Gmail or Outlook in Settings → Connections → Email."
        provider = (account.get("provider") or "gmail").strip().lower()
        account_id = account.get("account_id") or account.get("email") or ""
        summary = (kwargs.get("summary") or "").strip() or None
        start = (kwargs.get("start") or "").strip() or None
        end = (kwargs.get("end") or "").strip() or None
        description = (kwargs.get("description") or "").strip() or None
        calendar_id = (kwargs.get("calendar_id") or "").strip() or None
        ev = client_update_event(
            provider=provider,
            account_id=account_id,
            user_scope_id=user_scope_id,
            event_id=event_id,
            summary=summary,
            start=start,
            end=end,
            description=description,
            calendar_id=calendar_id,
            username=username,
        )
        if not ev:
            return "Failed to update the event. Check event_id and that the account has calendar access."
        return f"Updated event: {ev.get('summary')} ({ev.get('start')} – {ev.get('end')})."


class DeleteCalendarEventTool(BaseTool):
    """Delete a calendar event."""
    name = "delete_calendar_event"
    description = (
        "Delete a calendar event. Requires event_id (from list_calendar_events). "
        "Optional: provider, account_id, calendar_id."
    )
    parameters = {
        "type": "object",
        "properties": {
            "event_id": {
                "type": "string",
                "description": "Event ID (from list_calendar_events).",
            },
            "provider": {
                "type": "string",
                "enum": ["gmail", "microsoft"],
                "description": "Optional. gmail or microsoft.",
            },
            "account_id": {"type": "string", "description": "Optional. Calendar account email."},
            "calendar_id": {"type": "string", "description": "Optional. Calendar ID."},
        },
        "required": ["event_id"],
    }

    def run(self, **kwargs) -> str:
        user_scope_id = cred_scope_from_kwargs(kwargs)
        username = cred_username_from_kwargs(kwargs)
        event_id = (kwargs.get("event_id") or "").strip()
        if not event_id:
            return "event_id is required."
        account = resolve_calendar_account(
            provider=kwargs.get("provider"),
            account_id=kwargs.get("account_id"),
            username=username,
            user_scope_id=user_scope_id,
        )
        if not account:
            return "No calendar account connected. Connect Gmail or Outlook in Settings → Connections → Email."
        provider = (account.get("provider") or "gmail").strip().lower()
        account_id = account.get("account_id") or account.get("email") or ""
        calendar_id = (kwargs.get("calendar_id") or "").strip() or None
        ok = client_delete_event(
            provider=provider,
            account_id=account_id,
            user_scope_id=user_scope_id,
            event_id=event_id,
            calendar_id=calendar_id,
            username=username,
        )
        if not ok:
            return "Failed to delete the event (may already be deleted or event_id invalid)."
        return "Event deleted."
