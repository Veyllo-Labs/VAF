"""
VAF Calendar Client - List, create, update, delete events via Google Calendar API and Microsoft Graph.
Uses the same OAuth tokens as email (get_valid_access_token from oauth_pkce); no separate credentials.
User-scoped via user_scope_id; accounts from email_config / email_config_by_scope.
"""
import logging
from typing import Any, Dict, List, Optional

import requests

from vaf.core.email_transport import _get_email_config
from vaf.core.oauth_pkce import get_valid_access_token

logger = logging.getLogger("vaf.core.calendar_client")

# Default calendar ID for Google (primary)
GOOGLE_PRIMARY_CALENDAR = "primary"
# Microsoft uses "me/calendar" for default calendar
MS_GRAPH_BASE = "https://graph.microsoft.com/v1.0/me"
GOOGLE_CALENDAR_BASE = "https://www.googleapis.com/calendar/v3"


def _oauth_calendar_accounts(
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return list of OAuth calendar-capable accounts (gmail, microsoft) for the user. Each dict: account_id, provider, email, enabled."""
    ec = _get_email_config(username, user_scope_id=user_scope_id)
    accounts = ec.get("accounts") or []
    return [
        a
        for a in accounts
        if (a.get("provider") or "").lower() in ("gmail", "microsoft")
        and (a.get("enabled") is not False)
        and (a.get("account_id") or a.get("email"))
    ]


def _resolve_account(
    provider: Optional[str],
    account_id: Optional[str],
    username: Optional[str],
    user_scope_id: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Return first matching OAuth calendar account. If provider/account_id given, match that; else first available."""
    candidates = _oauth_calendar_accounts(username, user_scope_id=user_scope_id)
    if not candidates:
        return None
    if account_id:
        aid = (account_id or "").strip().lower()
        for a in candidates:
            if (a.get("account_id") or a.get("email") or "").strip().lower() == aid:
                return a
    if provider:
        prov = (provider or "").strip().lower()
        for a in candidates:
            if (a.get("provider") or "").strip().lower() == prov:
                return a
    return candidates[0]


def _ensure_rfc3339(s: str) -> str:
    """Ensure datetime string is RFC3339 (Google and Microsoft accept ISO8601 with timezone)."""
    s = (s or "").strip()
    if not s:
        return s
    if s.endswith("Z") or "+" in s or (len(s) >= 6 and s[-6] in "-+" and s[-3] == ":"):
        return s
    if "T" in s:
        return s + "Z" if not s.endswith("Z") else s
    return s + "T00:00:00Z"


# --- Google Calendar API ---


def _google_list_events(
    access_token: str,
    time_min: str,
    time_max: str,
    calendar_id: str = GOOGLE_PRIMARY_CALENDAR,
    max_results: int = 100,
) -> List[Dict[str, Any]]:
    url = f"{GOOGLE_CALENDAR_BASE}/calendars/{calendar_id}/events"
    params = {
        "timeMin": _ensure_rfc3339(time_min),
        "timeMax": _ensure_rfc3339(time_max),
        "maxResults": max_results,
        "singleEvents": True,
        "orderBy": "startTime",
    }
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=30)
        if r.status_code != 200:
            logger.warning("Google Calendar list events failed: %s %s", r.status_code, r.text[:300])
            return []
        data = r.json()
        items = data.get("items") or []
        return items
    except Exception as e:
        logger.warning("Google Calendar list error: %s", e)
        return []


def _google_create_event(
    access_token: str,
    summary: str,
    start: str,
    end: str,
    description: Optional[str] = None,
    calendar_id: str = GOOGLE_PRIMARY_CALENDAR,
    reminder_minutes: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    url = f"{GOOGLE_CALENDAR_BASE}/calendars/{calendar_id}/events"
    body = {
        "summary": summary,
        "description": description or "",
        "start": {"dateTime": _ensure_rfc3339(start), "timeZone": "UTC"},
        "end": {"dateTime": _ensure_rfc3339(end), "timeZone": "UTC"},
    }
    if reminder_minutes is not None:
        body["reminders"] = {"useDefault": False, "overrides": [{"method": "popup", "minutes": reminder_minutes}]}
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    try:
        r = requests.post(url, json=body, headers=headers, timeout=30)
        if r.status_code not in (200, 201):
            logger.warning("Google Calendar create failed: %s %s", r.status_code, r.text[:300])
            return None
        return r.json()
    except Exception as e:
        logger.warning("Google Calendar create error: %s", e)
        return None


def _google_update_event(
    access_token: str,
    calendar_id: str,
    event_id: str,
    summary: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    description: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    url = f"{GOOGLE_CALENDAR_BASE}/calendars/{calendar_id}/events/{event_id}"
    body = {}
    if summary is not None:
        body["summary"] = summary
    if description is not None:
        body["description"] = description
    if start is not None:
        body["start"] = {"dateTime": _ensure_rfc3339(start), "timeZone": "UTC"}
    if end is not None:
        body["end"] = {"dateTime": _ensure_rfc3339(end), "timeZone": "UTC"}
    if not body:
        # PATCH with empty body still returns current event
        pass
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    try:
        r = requests.patch(url, json=body, headers=headers, timeout=30)
        if r.status_code != 200:
            logger.warning("Google Calendar update failed: %s %s", r.status_code, r.text[:300])
            return None
        return r.json()
    except Exception as e:
        logger.warning("Google Calendar update error: %s", e)
        return None


def _google_delete_event(
    access_token: str,
    calendar_id: str,
    event_id: str,
) -> bool:
    url = f"{GOOGLE_CALENDAR_BASE}/calendars/{calendar_id}/events/{event_id}"
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        r = requests.delete(url, headers=headers, timeout=30)
        if r.status_code not in (204, 410):
            logger.warning("Google Calendar delete failed: %s %s", r.status_code, r.text[:300])
            return False
        return True
    except Exception as e:
        logger.warning("Google Calendar delete error: %s", e)
        return False


# --- Microsoft Graph ---


def _ms_list_events(
    access_token: str,
    time_min: str,
    time_max: str,
    calendar_id: Optional[str] = None,
    max_results: int = 100,
) -> List[Dict[str, Any]]:
    # calendarView for date range
    if calendar_id:
        path = f"{MS_GRAPH_BASE}/calendars/{calendar_id}/calendarView"
    else:
        path = f"{MS_GRAPH_BASE}/calendar/calendarView"
    params = {
        "startDateTime": _ensure_rfc3339(time_min),
        "endDateTime": _ensure_rfc3339(time_max),
        "$top": max_results,
    }
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        r = requests.get(path, params=params, headers=headers, timeout=30)
        if r.status_code != 200:
            logger.warning("Microsoft Calendar list events failed: %s %s", r.status_code, r.text[:300])
            return []
        data = r.json()
        return data.get("value") or []
    except Exception as e:
        logger.warning("Microsoft Calendar list error: %s", e)
        return []


def _ms_create_event(
    access_token: str,
    summary: str,
    start: str,
    end: str,
    description: Optional[str] = None,
    calendar_id: Optional[str] = None,
    reminder_minutes: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    path = f"{MS_GRAPH_BASE}/calendars/{calendar_id}/events" if calendar_id else f"{MS_GRAPH_BASE}/calendar/events"
    body = {
        "subject": summary,
        "body": {"contentType": "text", "content": description or ""},
        "start": {"dateTime": _ensure_rfc3339(start).replace("Z", "+00:00"), "timeZone": "UTC"},
        "end": {"dateTime": _ensure_rfc3339(end).replace("Z", "+00:00"), "timeZone": "UTC"},
    }
    if reminder_minutes is not None:
        body["isReminderOn"] = True
        body["reminderMinutesBeforeStart"] = reminder_minutes
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    try:
        r = requests.post(path, json=body, headers=headers, timeout=30)
        if r.status_code not in (200, 201):
            logger.warning("Microsoft Calendar create failed: %s %s", r.status_code, r.text[:300])
            return None
        return r.json()
    except Exception as e:
        logger.warning("Microsoft Calendar create error: %s", e)
        return None


def _ms_update_event(
    access_token: str,
    event_id: str,
    calendar_id: Optional[str] = None,
    summary: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    description: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    path = f"{MS_GRAPH_BASE}/calendars/{calendar_id}/events/{event_id}" if calendar_id else f"{MS_GRAPH_BASE}/events/{event_id}"
    body = {}
    if summary is not None:
        body["subject"] = summary
    if description is not None:
        body["body"] = {"contentType": "text", "content": description}
    if start is not None:
        body["start"] = {"dateTime": _ensure_rfc3339(start).replace("Z", "+00:00"), "timeZone": "UTC"}
    if end is not None:
        body["end"] = {"dateTime": _ensure_rfc3339(end).replace("Z", "+00:00"), "timeZone": "UTC"}
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    try:
        r = requests.patch(path, json=body, headers=headers, timeout=30)
        if r.status_code != 200:
            logger.warning("Microsoft Calendar update failed: %s %s", r.status_code, r.text[:300])
            return None
        return r.json()
    except Exception as e:
        logger.warning("Microsoft Calendar update error: %s", e)
        return None


def _ms_delete_event(
    access_token: str,
    event_id: str,
    calendar_id: Optional[str] = None,
) -> bool:
    path = f"{MS_GRAPH_BASE}/calendars/{calendar_id}/events/{event_id}" if calendar_id else f"{MS_GRAPH_BASE}/events/{event_id}"
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        r = requests.delete(path, headers=headers, timeout=30)
        if r.status_code not in (204, 404):
            logger.warning("Microsoft Calendar delete failed: %s %s", r.status_code, r.text[:300])
            return False
        return True
    except Exception as e:
        logger.warning("Microsoft Calendar delete error: %s", e)
        return False


# --- Normalized public API ---


def list_events(
    provider: str,
    account_id: str,
    user_scope_id: Optional[str],
    time_min: str,
    time_max: str,
    calendar_id: Optional[str] = None,
    username: Optional[str] = None,
    max_results: int = 100,
) -> List[Dict[str, Any]]:
    """List events in the given time range. Returns list of normalized event dicts (id, summary, start, end, htmlLink/webLink)."""
    token = get_valid_access_token(account_id, provider, username, user_scope_id=user_scope_id)
    if not token:
        return []
    prov = provider.strip().lower()
    cal = calendar_id or (GOOGLE_PRIMARY_CALENDAR if prov == "gmail" else None)
    if prov == "gmail":
        raw = _google_list_events(token, time_min, time_max, cal, max_results)
        return [_normalize_google_event(e) for e in raw]
    if prov == "microsoft":
        raw = _ms_list_events(token, time_min, time_max, cal, max_results)
        return [_normalize_ms_event(e) for e in raw]
    return []


def _normalize_google_event(e: Dict[str, Any]) -> Dict[str, Any]:
    start = e.get("start") or {}
    end = e.get("end") or {}
    return {
        "id": e.get("id"),
        "summary": e.get("summary") or "(no title)",
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "htmlLink": e.get("htmlLink"),
        "description": (e.get("description") or "")[:500],
    }


def _normalize_ms_event(e: Dict[str, Any]) -> Dict[str, Any]:
    start = e.get("start") or {}
    end = e.get("end") or {}
    return {
        "id": e.get("id"),
        "summary": e.get("subject") or "(no title)",
        "start": start.get("dateTime"),
        "end": end.get("dateTime"),
        "webLink": e.get("webLink"),
        "description": (e.get("body", {}).get("content") or "")[:500] if isinstance(e.get("body"), dict) else "",
    }


def create_event(
    provider: str,
    account_id: str,
    user_scope_id: Optional[str],
    summary: str,
    start: str,
    end: str,
    description: Optional[str] = None,
    calendar_id: Optional[str] = None,
    username: Optional[str] = None,
    reminder_minutes: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Create a calendar event. Returns normalized event dict or None on failure."""
    token = get_valid_access_token(account_id, provider, username, user_scope_id=user_scope_id)
    if not token:
        return None
    prov = provider.strip().lower()
    cal = calendar_id or (GOOGLE_PRIMARY_CALENDAR if prov == "gmail" else None)
    if prov == "gmail":
        ev = _google_create_event(token, summary, start, end, description, cal, reminder_minutes)
        return _normalize_google_event(ev) if ev else None
    if prov == "microsoft":
        ev = _ms_create_event(token, summary, start, end, description, cal, reminder_minutes)
        return _normalize_ms_event(ev) if ev else None
    return None


def update_event(
    provider: str,
    account_id: str,
    user_scope_id: Optional[str],
    event_id: str,
    summary: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    description: Optional[str] = None,
    calendar_id: Optional[str] = None,
    username: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Update a calendar event. Returns normalized event dict or None on failure."""
    token = get_valid_access_token(account_id, provider, username, user_scope_id=user_scope_id)
    if not token:
        return None
    prov = provider.strip().lower()
    cal = calendar_id or (GOOGLE_PRIMARY_CALENDAR if prov == "gmail" else None)
    if prov == "gmail":
        ev = _google_update_event(token, cal, event_id, summary, start, end, description)
        return _normalize_google_event(ev) if ev else None
    if prov == "microsoft":
        ev = _ms_update_event(token, event_id, cal, summary, start, end, description)
        return _normalize_ms_event(ev) if ev else None
    return None


def delete_event(
    provider: str,
    account_id: str,
    user_scope_id: Optional[str],
    event_id: str,
    calendar_id: Optional[str] = None,
    username: Optional[str] = None,
) -> bool:
    """Delete a calendar event. Returns True if deleted or already gone."""
    token = get_valid_access_token(account_id, provider, username, user_scope_id=user_scope_id)
    if not token:
        return False
    prov = provider.strip().lower()
    cal = calendar_id or (GOOGLE_PRIMARY_CALENDAR if prov == "gmail" else None)
    if prov == "gmail":
        return _google_delete_event(token, cal, event_id)
    if prov == "microsoft":
        return _ms_delete_event(token, event_id, cal)
    return False


def get_calendar_accounts(
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return OAuth calendar-capable accounts for the user (for tools and API status)."""
    return _oauth_calendar_accounts(username, user_scope_id=user_scope_id)


def resolve_calendar_account(
    provider: Optional[str] = None,
    account_id: Optional[str] = None,
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Resolve which account to use for calendar (first matching or first available)."""
    return _resolve_account(provider, account_id, username, user_scope_id)
