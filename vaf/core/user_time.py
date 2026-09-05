# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
User-timezone helpers — the single source of truth for "what time is it for THIS user".

The per-user `timezone` / `date_format` / `time_format` live in
~/.vaf/users/<user>/user_identity.json (the web Settings "Date & Time" panel). When a user
has set a timezone, every time-based decision and display should be evaluated in THAT zone,
not in raw server-local time. Historically only the system-prompt "Today is …" line honored
it; this module factors that logic into reusable functions so the rest of the codebase can
adopt it consistently.

Design:
  * stdlib `zoneinfo` only (no new dependency); the OS ships the tz database.
  * "Server default" = `timezone` unset/empty -> functions fall back to naive
    `datetime.now()` (server-local), byte-identical to the previous behavior, so users who
    never set a timezone see no change.
  * Never raises: a missing user, unreadable identity, or invalid IANA string degrades to
    server-local. There is no IANA validation on the stored string today, so ZoneInfo
    construction is always guarded.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, date
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


def _load_identity(username: Optional[str], identity: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return the user_identity dict; prefer a caller-supplied one (avoids re-reading on hot
    paths), else load it for `username`. Returns {} on any failure / when username is None."""
    if identity is not None:
        return identity
    if not username:
        return {}
    try:
        from vaf.auth.user_workspace import get_user_workspace
        return get_user_workspace(username).get_user_identity() or {}
    except Exception:
        return {}


def resolve_user_timezone(
    username: Optional[str] = None,
    identity: Optional[Dict[str, Any]] = None,
) -> Optional[ZoneInfo]:
    """ZoneInfo for the user's configured timezone, or None for "Server default" / invalid.

    None means callers should fall back to naive server-local `datetime.now()`.
    """
    ui = _load_identity(username, identity)
    tz_str = (ui.get("timezone") or "").strip() or None
    if not tz_str:
        return None
    try:
        return ZoneInfo(tz_str)
    except Exception:
        # No IANA validation exists on the stored string; degrade to server-local.
        logger.debug("user_time: invalid timezone %r for user %r; using server-local", tz_str, username)
        return None


def resolve_user_timezone_name(
    username: Optional[str] = None,
    identity: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """The user's configured IANA timezone STRING if set and valid, else None.

    Use for APIs that take a tz name rather than a tzinfo (e.g. the `schedule`
    library's Job.at(time, tz)). None -> caller should use server-local.
    """
    ui = _load_identity(username, identity)
    tz_str = (ui.get("timezone") or "").strip() or None
    if not tz_str:
        return None
    try:
        ZoneInfo(tz_str)  # validate; no IANA validation exists on the stored string
        return tz_str
    except Exception:
        return None


def user_now(
    username: Optional[str] = None,
    identity: Optional[Dict[str, Any]] = None,
) -> datetime:
    """Current time in the user's timezone (tz-aware) — or naive server-local when unset.

    This is the single primitive every time-based site should call instead of
    `datetime.now()` so a user's configured timezone is the source of truth.
    """
    tz = resolve_user_timezone(username, identity)
    return datetime.now(tz) if tz else datetime.now()


def user_today(
    username: Optional[str] = None,
    identity: Optional[Dict[str, Any]] = None,
) -> date:
    """Today's calendar date in the user's timezone (for "done today" / day-of-month / log-date)."""
    return user_now(username, identity).date()


# Date-format preset (as stored by the Settings panel) -> strftime pattern.
_DATE_STRFTIME = {
    "dd.mm.yyyy": "%d.%m.%Y",
    "yyyy-mm-dd": "%Y-%m-%d",
    "mm/dd/yyyy": "%m/%d/%Y",
    "dd.mm.yy": "%d.%m.%y",
}

_DAYS_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_DAYS_DE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def user_date_time_format(
    identity: Optional[Dict[str, Any]] = None,
    language: Optional[str] = None,
) -> str:
    """Combined "<date> <time>" strftime pattern for the user's date_format/time_format,
    with the same language-based fallbacks the system prompt uses (de -> dd.mm.yyyy + 24h)."""
    ui = identity or {}
    date_key = (ui.get("date_format") or "").strip() or None
    time_key = (ui.get("time_format") or "").strip() or None
    if date_key and date_key in _DATE_STRFTIME:
        date_fmt = _DATE_STRFTIME[date_key]
    elif language == "de":
        date_fmt = "%d.%m.%Y"
    else:
        date_fmt = "%Y-%m-%d"
    if time_key == "12h":
        # NOT "%p": strftime("%p") is locale-dependent and yields an EMPTY string in non-AM/PM
        # locales (e.g. de_DE) — so any process that imports a lib calling setlocale() would drop
        # the marker. {ampm} is a literal placeholder filled deterministically in format_user_datetime.
        time_fmt = "%I:%M:%S {ampm}"
    else:  # "24h", or any unset/default (de & en both default to 24h today)
        time_fmt = "%H:%M:%S"
    return f"{date_fmt} {time_fmt}"


def format_user_datetime(
    dt: Optional[datetime] = None,
    *,
    username: Optional[str] = None,
    identity: Optional[Dict[str, Any]] = None,
    language: Optional[str] = None,
    seconds: bool = True,
) -> str:
    """Format `dt` (default: now in the user's tz) per the user's date/time preferences.

    Returns just the "<date> <time>" string (no weekday/sentence wrapper; callers add that).
    `seconds=False` drops the seconds field, the shape appointments and reminders read in.
    """
    ui = _load_identity(username, identity)
    if dt is None:
        dt = user_now(username, ui)
    pattern = user_date_time_format(ui, language)
    if not seconds:
        pattern = pattern.replace(":%S", "")
    out = dt.strftime(pattern)
    if "{ampm}" in out:  # locale-independent AM/PM (see user_date_time_format)
        out = out.replace("{ampm}", "AM" if dt.hour < 12 else "PM")
    return out


def format_user_date(
    dt: Optional[datetime] = None,
    *,
    username: Optional[str] = None,
    identity: Optional[Dict[str, Any]] = None,
    language: Optional[str] = None,
) -> str:
    """The date half of format_user_datetime, for all-day events and day headings."""
    ui = _load_identity(username, identity)
    if dt is None:
        dt = user_now(username, ui)
    return dt.strftime(user_date_time_format(ui, language).split(" ", 1)[0])


def user_weekday_name(dt: datetime, language: Optional[str] = None) -> str:
    """Localized weekday name for `dt` (de/en), matching the system-prompt wording."""
    return (_DAYS_DE if language == "de" else _DAYS_EN)[dt.weekday()]


_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_ONLY_RE = re.compile(r"^\d{1,2}:\d{2}$")


def parse_user_datetime(
    text: str,
    username: Optional[str] = None,
    *,
    identity: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> Optional[Tuple[datetime, bool]]:
    """One grammar for every time a user or the model types, read in the USER's zone.

    Returns (datetime, all_day) or None when the text is not a time:
      * "2026-03-01T14:00", "2026-03-01 14:00", with or without seconds: that wall-clock
        time in the user's zone (aware when the user has one, naive server-local otherwise,
        the module's contract);
      * an ISO 8601 string with an offset or "Z": taken as given;
      * "2026-03-01": the day itself, all_day=True, at midnight in the user's zone;
      * "14:00": today in the user's zone (the reminder grammar).
    This replaced three private parsers (contact routes, the update_contact tool, the
    reminders store) that agreed on the format and disagreed on the details; the calendar
    lane is the fourth caller and the reason they were unified."""
    s = (text or "").strip()
    if not s:
        return None
    tz = resolve_user_timezone(username, identity)
    all_day = bool(_DATE_ONLY_RE.match(s))
    try:
        if _TIME_ONLY_RE.match(s):
            hh, mm = s.split(":", 1)
            base = now if now is not None else user_now(username, identity)
            return base.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0), False
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None and tz is not None:
        dt = dt.replace(tzinfo=tz)
    return dt, all_day
