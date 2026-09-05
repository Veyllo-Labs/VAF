# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
VAF calendar provider client: Google Calendar and Microsoft Graph as the sync sources of
the internal calendar (vaf/core/calendar_store.py, vaf/core/calendar_sync.py).

Stateless by design: every function is one request lane (paged where the provider pages),
nothing is stored here. Uses the same OAuth tokens as email (get_valid_access_token from
oauth_pkce); no separate credentials. User-scoped via user_scope_id; accounts come from
email_config / email_config_by_scope through the email SSOT.

Every provider function stays quiet on failure (None / [] / False plus a warning), with one
exception: a 401 raises AuthError, because a dead token is the one failure the sync engine
must not retry blindly (it flags the account for re-consent instead).
"""
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import requests

from vaf.core.email_accounts import get_email_config as _get_email_config
from vaf.core.oauth_pkce import get_valid_access_token

logger = logging.getLogger("vaf.core.calendar_client")

# Default calendar ID for Google (primary)
GOOGLE_PRIMARY_CALENDAR = "primary"
# Microsoft uses "me/calendar" for default calendar
MS_GRAPH_BASE = "https://graph.microsoft.com/v1.0/me"
GOOGLE_CALENDAR_BASE = "https://www.googleapis.com/calendar/v3"

# Provider page-size ceilings (Google events.list maxResults, Graph $top).
_GOOGLE_MAX_PAGE = 2500
_MS_MAX_PAGE = 999
# A window can hold many instances of a recurring series; this bounds one pull.
_MAX_PAGES = 40


class AuthError(Exception):
    """The provider answered 401: the token is dead and a refresh did not help."""


def _check_auth(response: Any, what: str) -> None:
    if getattr(response, "status_code", None) == 401:
        raise AuthError(f"{what}: 401 unauthorized")


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
    """The OAuth calendar account to use. An account_id must match (a wrong id is an answer,
    not a reason to fall through to somebody else's account); a provider picks the first
    account of that provider; nothing given picks the first account."""
    candidates = _oauth_calendar_accounts(username, user_scope_id=user_scope_id)
    if not candidates:
        return None
    if account_id:
        aid = (account_id or "").strip().lower()
        for a in candidates:
            if (a.get("account_id") or a.get("email") or "").strip().lower() == aid:
                return a
        return None
    if provider:
        prov = (provider or "").strip().lower()
        for a in candidates:
            if (a.get("provider") or "").strip().lower() == prov:
                return a
        return None
    return candidates[0]


_FRACTION_RE = re.compile(r"(\.\d{1,6})\d*")


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    """RFC 3339 / ISO 8601 as the providers write it (Graph appends seven fractional digits)
    to a datetime; naive input stays naive. None when unreadable."""
    raw = (s or "").strip()
    if not raw:
        return None
    raw = raw.replace("Z", "+00:00")
    raw = _FRACTION_RE.sub(lambda m: m.group(1), raw)
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _to_unix(s: Optional[str]) -> Optional[float]:
    dt = _parse_iso(s)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _ensure_rfc3339(s: str, tz: Optional[str] = None) -> str:
    """Datetime string as the providers accept it. An offset or Z stays; a naive datetime
    is left naive when a zone name travels beside it (Google reads dateTime + timeZone) and
    gets Z appended otherwise; a bare date becomes midnight UTC."""
    s = (s or "").strip()
    if not s:
        return s
    if s.endswith("Z") or "+" in s or (len(s) >= 6 and s[-6] in "-+" and s[-3] == ":"):
        return s
    if "T" in s:
        return s if tz else s + "Z"
    return s + "T00:00:00Z"


def _wall_clock(s: str, tz: Optional[str]) -> str:
    """Graph wants the wall-clock time IN the named zone with no offset. An aware input is
    converted into that zone (UTC when none is named); a naive input is taken as already
    being wall-clock time in it."""
    dt = _parse_iso(s)
    if dt is None:
        return (s or "").strip()
    if dt.tzinfo is not None:
        target = timezone.utc
        if tz:
            try:
                target = ZoneInfo(tz)
            except Exception:
                target = timezone.utc
        dt = dt.astimezone(target).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _date_only(s: str) -> str:
    return (s or "").strip()[:10]


def _next_day(date_str: str) -> str:
    try:
        return (datetime.strptime(date_str[:10], "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    except ValueError:
        return date_str


# --- Google Calendar API ---


def _google_list_events(
    access_token: str,
    time_min: str,
    time_max: str,
    calendar_id: str = GOOGLE_PRIMARY_CALENDAR,
    max_results: int = 250,
) -> List[Dict[str, Any]]:
    """Every instance in the window, across pages. Instances of a series come as single
    events (singleEvents) and cancelled ones are included (showDeleted) so a pull can mirror
    a deletion instead of silently keeping the row."""
    url = f"{GOOGLE_CALENDAR_BASE}/calendars/{calendar_id}/events"
    params: Dict[str, Any] = {
        "timeMin": _ensure_rfc3339(time_min),
        "timeMax": _ensure_rfc3339(time_max),
        "maxResults": max(1, min(int(max_results or 250), _GOOGLE_MAX_PAGE)),
        "singleEvents": True,
        "showDeleted": True,
        "orderBy": "startTime",
    }
    headers = {"Authorization": f"Bearer {access_token}"}
    items: List[Dict[str, Any]] = []
    try:
        for _ in range(_MAX_PAGES):
            r = requests.get(url, params=params, headers=headers, timeout=30)
            _check_auth(r, "Google Calendar list")
            if r.status_code != 200:
                logger.warning("Google Calendar list events failed: %s %s", r.status_code, r.text[:300])
                break
            data = r.json()
            items.extend(data.get("items") or [])
            token = data.get("nextPageToken")
            if not token:
                break
            params["pageToken"] = token
    except AuthError:
        raise
    except Exception as e:
        logger.warning("Google Calendar list error: %s", e)
    return items


def _google_time(start: str, tz: Optional[str], all_day: bool) -> Dict[str, str]:
    if all_day:
        return {"date": _date_only(start)}
    return {"dateTime": _ensure_rfc3339(start, tz), "timeZone": tz or "UTC"}


def _google_create_event(
    access_token: str,
    summary: str,
    start: str,
    end: str,
    description: Optional[str] = None,
    calendar_id: str = GOOGLE_PRIMARY_CALENDAR,
    reminder_minutes: Optional[int] = None,
    tz: Optional[str] = None,
    all_day: bool = False,
    location: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    url = f"{GOOGLE_CALENDAR_BASE}/calendars/{calendar_id}/events"
    body: Dict[str, Any] = {
        "summary": summary,
        "description": description or "",
        "start": _google_time(start, tz, all_day),
        "end": _google_time(end, tz, all_day),
    }
    if location:
        body["location"] = location
    if reminder_minutes is not None:
        body["reminders"] = {"useDefault": False, "overrides": [{"method": "popup", "minutes": reminder_minutes}]}
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    try:
        r = requests.post(url, json=body, headers=headers, timeout=30)
        _check_auth(r, "Google Calendar create")
        if r.status_code not in (200, 201):
            logger.warning("Google Calendar create failed: %s %s", r.status_code, r.text[:300])
            return None
        return r.json()
    except AuthError:
        raise
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
    tz: Optional[str] = None,
    all_day: bool = False,
    location: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    url = f"{GOOGLE_CALENDAR_BASE}/calendars/{calendar_id}/events/{event_id}"
    body: Dict[str, Any] = {}
    if summary is not None:
        body["summary"] = summary
    if description is not None:
        body["description"] = description
    if location is not None:
        body["location"] = location
    if start is not None:
        body["start"] = _google_time(start, tz, all_day)
    if end is not None:
        body["end"] = _google_time(end, tz, all_day)
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    try:
        r = requests.patch(url, json=body, headers=headers, timeout=30)
        _check_auth(r, "Google Calendar update")
        if r.status_code != 200:
            logger.warning("Google Calendar update failed: %s %s", r.status_code, r.text[:300])
            return None
        return r.json()
    except AuthError:
        raise
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
        _check_auth(r, "Google Calendar delete")
        if r.status_code not in (204, 404, 410):
            logger.warning("Google Calendar delete failed: %s %s", r.status_code, r.text[:300])
            return False
        return True
    except AuthError:
        raise
    except Exception as e:
        logger.warning("Google Calendar delete error: %s", e)
        return False


# --- Microsoft Graph ---


def _ms_list_events(
    access_token: str,
    time_min: str,
    time_max: str,
    calendar_id: Optional[str] = None,
    max_results: int = 250,
) -> List[Dict[str, Any]]:
    """Every instance in the window (calendarView expands series), across pages via
    @odata.nextLink. Times are requested in UTC so the wall clock is unambiguous."""
    if calendar_id:
        path = f"{MS_GRAPH_BASE}/calendars/{calendar_id}/calendarView"
    else:
        path = f"{MS_GRAPH_BASE}/calendar/calendarView"
    params: Optional[Dict[str, Any]] = {
        "startDateTime": _ensure_rfc3339(time_min),
        "endDateTime": _ensure_rfc3339(time_max),
        "$top": max(1, min(int(max_results or 250), _MS_MAX_PAGE)),
    }
    headers = {"Authorization": f"Bearer {access_token}", "Prefer": 'outlook.timezone="UTC"'}
    items: List[Dict[str, Any]] = []
    url = path
    try:
        for _ in range(_MAX_PAGES):
            r = requests.get(url, params=params, headers=headers, timeout=30)
            _check_auth(r, "Microsoft Calendar list")
            if r.status_code != 200:
                logger.warning("Microsoft Calendar list events failed: %s %s", r.status_code, r.text[:300])
                break
            data = r.json()
            items.extend(data.get("value") or [])
            nxt = data.get("@odata.nextLink")
            if not nxt:
                break
            url, params = nxt, None       # the next link carries its own query
    except AuthError:
        raise
    except Exception as e:
        logger.warning("Microsoft Calendar list error: %s", e)
    return items


def _ms_time(start: str, tz: Optional[str], all_day: bool) -> Dict[str, str]:
    if all_day:
        return {"dateTime": f"{_date_only(start)}T00:00:00", "timeZone": tz or "UTC"}
    return {"dateTime": _wall_clock(start, tz), "timeZone": tz or "UTC"}


def _ms_create_event(
    access_token: str,
    summary: str,
    start: str,
    end: str,
    description: Optional[str] = None,
    calendar_id: Optional[str] = None,
    reminder_minutes: Optional[int] = None,
    tz: Optional[str] = None,
    all_day: bool = False,
    location: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    path = f"{MS_GRAPH_BASE}/calendars/{calendar_id}/events" if calendar_id else f"{MS_GRAPH_BASE}/calendar/events"
    body: Dict[str, Any] = {
        "subject": summary,
        "body": {"contentType": "text", "content": description or ""},
        "start": _ms_time(start, tz, all_day),
        "end": _ms_time(end, tz, all_day),
    }
    if all_day:
        body["isAllDay"] = True
    if location:
        body["location"] = {"displayName": location}
    if reminder_minutes is not None:
        body["isReminderOn"] = True
        body["reminderMinutesBeforeStart"] = reminder_minutes
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    try:
        r = requests.post(path, json=body, headers=headers, timeout=30)
        _check_auth(r, "Microsoft Calendar create")
        if r.status_code not in (200, 201):
            logger.warning("Microsoft Calendar create failed: %s %s", r.status_code, r.text[:300])
            return None
        return r.json()
    except AuthError:
        raise
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
    tz: Optional[str] = None,
    all_day: bool = False,
    location: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    path = f"{MS_GRAPH_BASE}/calendars/{calendar_id}/events/{event_id}" if calendar_id else f"{MS_GRAPH_BASE}/events/{event_id}"
    body: Dict[str, Any] = {}
    if summary is not None:
        body["subject"] = summary
    if description is not None:
        body["body"] = {"contentType": "text", "content": description}
    if location is not None:
        body["location"] = {"displayName": location}
    if start is not None:
        body["start"] = _ms_time(start, tz, all_day)
    if end is not None:
        body["end"] = _ms_time(end, tz, all_day)
    if start is not None or end is not None:
        body["isAllDay"] = bool(all_day)
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    try:
        r = requests.patch(path, json=body, headers=headers, timeout=30)
        _check_auth(r, "Microsoft Calendar update")
        if r.status_code != 200:
            logger.warning("Microsoft Calendar update failed: %s %s", r.status_code, r.text[:300])
            return None
        return r.json()
    except AuthError:
        raise
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
        _check_auth(r, "Microsoft Calendar delete")
        if r.status_code not in (204, 404):
            logger.warning("Microsoft Calendar delete failed: %s %s", r.status_code, r.text[:300])
            return False
        return True
    except AuthError:
        raise
    except Exception as e:
        logger.warning("Microsoft Calendar delete error: %s", e)
        return False


# --- Normalized shape ---
#
# One dict for both providers: id, ical_uid, summary, description, location, start, end
# (RFC 3339 for timed events, a date for all-day ones), all_day, tz (IANA name when the
# provider names one), status (confirmed | cancelled), updated (unix float), recurring_event_id
# (the series an instance belongs to), link, etag. The provider-specific link keys
# (htmlLink, webLink) are kept beside `link` until every reader has moved to `link`.


def _normalize_google_event(e: Dict[str, Any]) -> Dict[str, Any]:
    start = e.get("start") or {}
    end = e.get("end") or {}
    all_day = bool(start.get("date")) and not start.get("dateTime")
    return {
        "id": e.get("id"),
        "ical_uid": e.get("iCalUID"),
        "summary": e.get("summary") or "(no title)",
        "description": (e.get("description") or "")[:2000],
        "location": e.get("location") or "",
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "all_day": all_day,
        "tz": start.get("timeZone") or None,
        "status": "cancelled" if (e.get("status") or "").lower() == "cancelled" else "confirmed",
        "updated": _to_unix(e.get("updated")),
        "recurring_event_id": e.get("recurringEventId"),
        "link": e.get("htmlLink"),
        "htmlLink": e.get("htmlLink"),
        "etag": e.get("etag"),
    }


def _normalize_ms_event(e: Dict[str, Any]) -> Dict[str, Any]:
    start = e.get("start") or {}
    end = e.get("end") or {}
    all_day = bool(e.get("isAllDay"))
    body = e.get("body") if isinstance(e.get("body"), dict) else {}
    description = e.get("bodyPreview") or body.get("content") or ""
    location = e.get("location") if isinstance(e.get("location"), dict) else {}
    start_s = start.get("dateTime")
    end_s = end.get("dateTime")
    if all_day:
        start_s = _date_only(start_s or "")
        end_s = _date_only(end_s or "")
    return {
        "id": e.get("id"),
        "ical_uid": e.get("iCalUId"),
        "summary": e.get("subject") or "(no title)",
        "description": str(description)[:2000],
        "location": location.get("displayName") or "",
        "start": start_s,
        "end": end_s,
        "all_day": all_day,
        "tz": start.get("timeZone") or None,
        "status": "cancelled" if e.get("isCancelled") else "confirmed",
        "updated": _to_unix(e.get("lastModifiedDateTime")),
        "recurring_event_id": e.get("seriesMasterId"),
        "link": e.get("webLink"),
        "webLink": e.get("webLink"),
        "etag": e.get("@odata.etag"),
    }


# --- Normalized public API ---


def list_events(
    provider: str,
    account_id: str,
    user_scope_id: Optional[str],
    time_min: str,
    time_max: str,
    calendar_id: Optional[str] = None,
    username: Optional[str] = None,
    max_results: int = 250,
) -> List[Dict[str, Any]]:
    """Every instance in the window as normalized dicts (see the shape above), across the
    provider's pages; max_results is the page size. [] without a token or for an unknown
    provider; AuthError on a dead token."""
    token = get_valid_access_token(account_id, provider, username=username, user_scope_id=user_scope_id)
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
    tz: Optional[str] = None,
    all_day: bool = False,
    location: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Create a calendar event. `tz` is the IANA zone the wall-clock times are in (UTC when
    absent); an all-day event takes dates, with `end` the exclusive next day. Returns the
    normalized event or None on failure; AuthError on a dead token."""
    token = get_valid_access_token(account_id, provider, username=username, user_scope_id=user_scope_id)
    if not token:
        return None
    prov = provider.strip().lower()
    cal = calendar_id or (GOOGLE_PRIMARY_CALENDAR if prov == "gmail" else None)
    if prov == "gmail":
        ev = _google_create_event(token, summary, start, end, description, cal, reminder_minutes, tz, all_day, location)
        return _normalize_google_event(ev) if ev else None
    if prov == "microsoft":
        ev = _ms_create_event(token, summary, start, end, description, cal, reminder_minutes, tz, all_day, location)
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
    tz: Optional[str] = None,
    all_day: bool = False,
    location: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Update a calendar event (only the given fields). Returns the normalized event or None
    on failure; AuthError on a dead token."""
    token = get_valid_access_token(account_id, provider, username=username, user_scope_id=user_scope_id)
    if not token:
        return None
    prov = provider.strip().lower()
    cal = calendar_id or (GOOGLE_PRIMARY_CALENDAR if prov == "gmail" else None)
    if prov == "gmail":
        ev = _google_update_event(token, cal, event_id, summary, start, end, description, tz, all_day, location)
        return _normalize_google_event(ev) if ev else None
    if prov == "microsoft":
        ev = _ms_update_event(token, event_id, cal, summary, start, end, description, tz, all_day, location)
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
    """Delete a calendar event. True if deleted or already gone; AuthError on a dead token."""
    token = get_valid_access_token(account_id, provider, username=username, user_scope_id=user_scope_id)
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
    """Resolve which account to use for calendar (a named account or provider must match;
    nothing named means the first available)."""
    return _resolve_account(provider, account_id, username, user_scope_id)
