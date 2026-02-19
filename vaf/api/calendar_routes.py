"""
Calendar API: status and optional events for the Web UI.
Calendar uses the same OAuth accounts as email (Gmail/Outlook); no separate credentials.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query, Request

from vaf.core.calendar_client import list_events, resolve_calendar_account

# Use the same _get_email_config as the email API so calendar status sees the same accounts as Email
from vaf.api.email_routes import _get_email_config

logger = logging.getLogger("vaf.api.calendar")

router = APIRouter(prefix="/api/calendar", tags=["calendar"])


def _get_current_user(request: Request) -> Dict[str, Any]:
    """Current user with username and user_scope_id (from auth or local admin)."""
    from vaf.api.config_routes import get_current_user_or_local_admin
    return get_current_user_or_local_admin(request)


@router.get("/status")
async def calendar_status(_user: Dict[str, Any] = Depends(_get_current_user)):
    """
    Return whether the current user has a Google and/or Microsoft calendar account connected.
    Calendar uses the same accounts as Email (Gmail/Outlook); no separate connection step.
    """
    username = _user.get("username") or "admin"
    user_scope_id = _user.get("user_scope_id")
    ec = _get_email_config(username, user_scope_id=user_scope_id)
    accounts = ec.get("accounts") or []
    google_available = any((a.get("provider") or "").lower() == "gmail" and (a.get("enabled") is not False) for a in accounts)
    microsoft_available = any((a.get("provider") or "").lower() == "microsoft" and (a.get("enabled") is not False) for a in accounts)
    providers = [a.get("provider") or "?" for a in accounts]
    msg = (
        f"calendar status: user={username} scope={((user_scope_id or '')[:8] + '..') if user_scope_id else 'none'} "
        f"accounts={len(accounts)} gmail={google_available} ms={microsoft_available} providers={providers}"
    )
    logger.info("%s", msg)
    try:
        from vaf.core.log_helper import append_domain_log_always
        append_domain_log_always("backend", f"[CALENDAR] {msg}")
    except Exception:
        pass
    if accounts and not (google_available or microsoft_available):
        logger.debug("calendar status: account providers = %s", [a.get("provider") for a in accounts])
    return {
        "google_available": google_available,
        "microsoft_available": microsoft_available,
    }


@router.get("/events")
async def calendar_events(
    time_min: Optional[str] = Query(None, description="Start of range (ISO8601 or YYYY-MM-DD)"),
    time_max: Optional[str] = Query(None, description="End of range (ISO8601 or YYYY-MM-DD)"),
    _user: Dict[str, Any] = Depends(_get_current_user),
):
    """
    List calendar events for the current user in the given time range.
    Uses the first connected Gmail or Microsoft account. Optional time_min, time_max (default: next 7 days).
    """
    username = _user.get("username") or "admin"
    user_scope_id = _user.get("user_scope_id")
    account = resolve_calendar_account(username=username, user_scope_id=user_scope_id)
    if not account:
        return {"events": [], "account": None}
    provider = (account.get("provider") or "gmail").strip().lower()
    account_id = account.get("account_id") or account.get("email") or ""
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=7)
    if not time_min:
        time_min = now.isoformat().replace("+00:00", "Z")
    if not time_max:
        time_max = end.isoformat().replace("+00:00", "Z")
    events = list_events(
        provider=provider,
        account_id=account_id,
        user_scope_id=user_scope_id,
        time_min=time_min,
        time_max=time_max,
        username=username,
        max_results=50,
    )
    return {"events": events, "account": account_id}
