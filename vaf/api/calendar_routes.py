# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
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


# Name of the auto-created daily calendar check automation (visible in Automations UI).
CALENDAR_DAILY_CHECK_NAME = "Daily calendar check"

DEFAULT_CALENDAR_CHECK_PROMPT = """You are running the daily calendar check. Your job is to think carefully and act, not just list events.

1. Call list_calendar_events for the next 24 to 48 hours (use time_min and time_max as ISO8601).

2. Analyze each event: How important is it? Consider: meetings, deadlines, presentations, reviews, customer calls, first or last appointment of the day, long duration. Ignore low-value blocks (e.g. "Focus time", "Lunch" unless relevant).

3. For each important event, decide what to do:
   - Reminder: If the event is in the future and the user would benefit from a reminder (e.g. 30 minutes before), create a one-off automation with create_automation: frequency "once", time = 30 minutes before the event start. In the prompt for that one-off automation, instruct the agent to send the reminder via the user's main_messenger (see User Identity): use the matching tool—send_telegram, send_whatsapp, send_discord, send_slack, or send_mail—depending on main_messenger. Use the exact time in HH:MM for the "time" parameter.
   - Prepare now: If the event starts within the next 30–60 minutes, send a reminder or preparation help now via the user's main_messenger: look up main_messenger in the User Identity (above in your context) and call the corresponding tool (send_telegram, send_whatsapp, send_discord, send_slack, or send_mail) with a short summary and "Your meeting [X] starts soon". If main_messenger is not set, skip sending and only summarize in your reply.
   - Optional: Use memory_search to find relevant notes for a meeting and include a one-line hint in the reminder.

4. Execute: Actually call create_automation for each one-off reminder you decided on, and for any immediate reminders call the send tool that matches the user's main_messenger (send_telegram, send_whatsapp, send_discord, send_slack, or send_mail). Do not just say what you would do—do it.

5. At the end, reply briefly in the user's language: what you found, how many events, which reminders or messages you created/sent."""


@router.post("/ensure-daily-check-automation")
async def ensure_daily_check_automation(_user: Dict[str, Any] = Depends(_get_current_user)):
    """
    If the current user has a calendar connected and does not yet have the "Daily calendar check"
    automation, create it. This automation appears in the Automations UI and runs daily at 08:00
    (user can change time). Idempotent: safe to call every time; only creates if missing.
    """
    username = _user.get("username") or "admin"
    user_scope_id = _user.get("user_scope_id")
    ec = _get_email_config(username, user_scope_id=user_scope_id)
    accounts = ec.get("accounts") or []
    google_available = any((a.get("provider") or "").lower() == "gmail" and (a.get("enabled") is not False) for a in accounts)
    microsoft_available = any((a.get("provider") or "").lower() == "microsoft" and (a.get("enabled") is not False) for a in accounts)
    if not google_available and not microsoft_available:
        return {"ok": False, "created": False, "reason": "no_calendar"}
    try:
        from vaf.core.automation import AutomationManager, AutomationTask
        from vaf.core.config import get_local_admin_scope_id
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
