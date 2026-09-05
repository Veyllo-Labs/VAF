# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Keeps the VAF calendar (vaf/core/calendar_store.py) and the connected Google and
Microsoft calendars (vaf/core/calendar_client.py) in step, and fires the reminders of the
events in it. Design: docs/integrations/CALENDAR_INTEGRATION.md.

One account sync, in this order:
  1. push: every local change owed to the provider (a created or edited event, a deletion)
     is written into the account's primary calendar; a failure counts against the event and
     parks it as push_failed after the store's cap, retried after the next local edit;
  2. pull: the window [now - calendar_sync_past_days, now + calendar_sync_future_days] is
     read completely (strict listing) and folded in, the newer change winning per event;
  3. deletions: a mirrored event inside the window that a complete pull no longer returned
     was deleted at the provider and goes locally, with its reminder.
A 401 marks the account "re-consent needed"; it is not retried until its stored token
changes. Every other failure is recorded on the account and retried next sweep.

The supervisor runs the sweep once per calendar_sync_interval_minutes inside the web
backend on the shared base (vaf/core/sync_supervisor.py). request_sync() is the write-
through hook a route or a tool calls after a local change; in a process without the
supervisor (the CLI) it is a no-op and the next sweep pushes. The reminder tick,
fire_due_calendar_reminders(), rides the automation scheduler loop next to the one-shot
reminders (vaf/core/reminders.py) and is the same narrow lane: stored data, a text composed
deterministically in the user's language, delivered on the main channel; no agent run.
"""
import hashlib
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from vaf.core import calendar_client as cc
from vaf.core.calendar_store import REMINDER_GRACE_SECONDS, CalendarStore, push_enabled
from vaf.core.sync_supervisor import (
    Account,
    SyncSupervisor,
    account_key,
    collect_email_accounts,
    mask_account,
    running_supervisor,
)

logger = logging.getLogger("vaf.core.calendar_sync")

NAME = "calendar"
CALENDAR_PROVIDERS = ("gmail", "microsoft")

DEFAULT_INTERVAL_MINUTES = 5
DEFAULT_PAST_DAYS = 30
DEFAULT_FUTURE_DAYS = 365


# ── configuration ─────────────────────────────────────────────────────────────

def _cfg_int(key: str, default: int, lo: int, hi: int) -> int:
    try:
        from vaf.core.config import Config
        raw = Config.get(key, default)
        value = int(default if raw is None or raw == "" else raw)
    except Exception:
        value = default
    return max(lo, min(hi, value))


def sync_interval_seconds() -> float:
    return _cfg_int("calendar_sync_interval_minutes", DEFAULT_INTERVAL_MINUTES, 1, 24 * 60) * 60.0


def sync_window(now_ts: Optional[float] = None) -> Tuple[float, float]:
    """The pull window as UTC instants: [now - past_days, now + future_days]."""
    now = float(now_ts if now_ts is not None else datetime.now(timezone.utc).timestamp())
    past = _cfg_int("calendar_sync_past_days", DEFAULT_PAST_DAYS, 0, 3650)
    future = _cfg_int("calendar_sync_future_days", DEFAULT_FUTURE_DAYS, 1, 3650)
    return now - past * 86400.0, now + future * 86400.0


def _iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── accounts ──────────────────────────────────────────────────────────────────

def account_id_of(acc: Dict[str, Any]) -> str:
    return str(acc.get("account_id") or acc.get("email") or "").strip()


def provider_of(acc: Dict[str, Any]) -> str:
    return str(acc.get("provider") or "").strip().lower()


def wants_calendar_sync(acc: Dict[str, Any]) -> bool:
    """A calendar account is a Google or Microsoft account that is still enabled. A
    calendar-safe mail delete leaves `mail_enabled=False` and keeps the entry: that account
    is exactly the one this lane must keep serving, so mail_enabled is not consulted."""
    return provider_of(acc) in CALENDAR_PROVIDERS and bool(acc.get("enabled", True)) and bool(account_id_of(acc))


def calendar_accounts_for(scope: str, account_id: Optional[str] = None) -> List[Account]:
    """The calendar accounts of one scope from the config lanes (optionally one of them)."""
    out: List[Account] = []
    for s, u, acc in collect_email_accounts():
        if str(s) != str(scope) or not wants_calendar_sync(acc):
            continue
        if account_id and account_id_of(acc) != str(account_id):
            continue
        out.append((s, u, acc))
    return out


def _token_signature(account_id: str, provider: str, cred_username: Optional[str], scope: str) -> Optional[str]:
    """A fingerprint of the stored token record, read without the network: a re-consent (or a
    successful refresh) changes it, which is the only reason to retry a 401'd account."""
    try:
        from vaf.core.credential_store import get_email_credentials
        creds = get_email_credentials(account_id, provider, cred_username, user_scope_id=scope) or {}
    except Exception:
        return None
    material = f"{creds.get('refresh_token') or ''}|{creds.get('access_token') or ''}"
    if material == "|":
        return None
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def default_account_for(scope: str, account_id: Optional[str] = None,
                        store: Optional[CalendarStore] = None) -> Optional[Dict[str, Any]]:
    """The account a new event is mirrored into: the one named, else the store's push
    target, else the first connected calendar account; None when the scope has none (the
    event stays internal). A named account that is not connected is None as well, so the
    caller can say so instead of silently picking another."""
    accounts = calendar_accounts_for(scope)
    if account_id:
        for _s, _u, acc in accounts:
            if account_id_of(acc) == str(account_id).strip():
                return acc
        return None
    if not accounts:
        return None
    try:
        target = (store or CalendarStore(scope)).settings().get("push_target")
    except Exception:
        target = None
    if target:
        for _s, _u, acc in accounts:
            if account_id_of(acc) == target:
                return acc
    return accounts[0][2]


_account_locks: Dict[str, threading.Lock] = {}
_account_locks_gate = threading.Lock()


def _lock_for(scope: str, account_id: str) -> threading.Lock:
    """One lock per account: the supervisor's sweep and a user's "sync now" must never push
    the same pending row twice (two creates at the provider for one event)."""
    key = f"{scope}:{account_id}"
    with _account_locks_gate:
        lock = _account_locks.get(key)
        if lock is None:
            lock = _account_locks[key] = threading.Lock()
        return lock


def _username_for(scope: str, cred_username: Optional[str]) -> Optional[str]:
    if cred_username:
        return cred_username
    try:
        from vaf.core.config import resolve_caller_username
        return resolve_caller_username(None, scope, allow_lookup=True)
    except Exception:
        return None


# ── push ──────────────────────────────────────────────────────────────────────

def _push_times(ev: Dict[str, Any], tz_fallback: Optional[str]) -> Tuple[str, str, Optional[str], bool]:
    """(start, end, tz, all_day) the way the provider client wants them: dates for an all-day
    event (end exclusive, as stored), otherwise wall-clock times in the event's zone."""
    if ev.get("all_day"):
        return str(ev.get("start_date") or ""), str(ev.get("end_date") or ""), ev.get("tz") or tz_fallback, True
    tz_name = ev.get("tz") or tz_fallback or "UTC"
    try:
        zone = ZoneInfo(tz_name)
    except Exception:
        tz_name, zone = "UTC", timezone.utc
    start = datetime.fromtimestamp(float(ev["start_ts"]), zone).strftime("%Y-%m-%dT%H:%M:%S")
    end = datetime.fromtimestamp(float(ev["end_ts"]), zone).strftime("%Y-%m-%dT%H:%M:%S")
    return start, end, tz_name, False


def push_pending(store: CalendarStore, scope: str, cred_username: Optional[str], acc: Dict[str, Any],
                 *, tz_fallback: Optional[str] = None) -> Dict[str, int]:
    """Write every change owed to this account. AuthError propagates (the account is dead
    for the pull as well); any other failure counts against the one event."""
    account_id, provider = account_id_of(acc), provider_of(acc)
    stats = {"pushed": 0, "deleted": 0, "failed": 0}
    for ev in store.pending_pushes(account_id):
        try:
            if ev["sync_state"] == "pending_delete":
                if not ev.get("external_id"):
                    store.purge_event(ev["id"])
                    stats["deleted"] += 1
                    continue
                ok = cc.delete_event(provider, account_id, scope, ev["external_id"],
                                     calendar_id=ev.get("external_calendar_id"), username=cred_username)
                if ok:
                    store.purge_event(ev["id"])
                    stats["deleted"] += 1
                else:
                    store.mark_push_failed(ev["id"], "the provider did not accept the deletion")
                    stats["failed"] += 1
                continue
            start, end, tz_name, all_day = _push_times(ev, tz_fallback)
            if ev.get("external_id"):
                res = cc.update_event(provider, account_id, scope, ev["external_id"], summary=ev.get("title") or "",
                                      start=start, end=end, description=ev.get("description") or "",
                                      calendar_id=ev.get("external_calendar_id"), username=cred_username,
                                      tz=tz_name, all_day=all_day, location=ev.get("location") or "")
            else:
                res = cc.create_event(provider, account_id, scope, ev.get("title") or "", start, end,
                                      description=ev.get("description") or None, username=cred_username,
                                      reminder_minutes=ev.get("reminder_minutes"), tz=tz_name, all_day=all_day,
                                      location=ev.get("location") or None)
            if res and res.get("id"):
                store.mark_pushed(ev["id"], external_id=str(res["id"]), external_updated=res.get("updated"),
                                  etag=res.get("etag"), link=res.get("link"))
                stats["pushed"] += 1
            else:
                store.mark_push_failed(ev["id"], "the provider did not accept the write")
                stats["failed"] += 1
        except cc.AuthError:
            raise
        except Exception as e:
            store.mark_push_failed(ev["id"], str(e))
            stats["failed"] += 1
    return stats


# ── pull ──────────────────────────────────────────────────────────────────────

def pull_window(store: CalendarStore, scope: str, cred_username: Optional[str], acc: Dict[str, Any],
                start_ts: float, end_ts: float) -> Dict[str, int]:
    """Read the window completely and fold it in; then drop what the provider no longer has.
    A failed page raises ProviderError before anything is treated as deleted."""
    account_id, provider = account_id_of(acc), provider_of(acc)
    events = cc.list_events(provider, account_id, scope, _iso_utc(start_ts), _iso_utc(end_ts),
                            username=cred_username, strict=True)
    stats = {"created": 0, "updated": 0, "cancelled": 0, "kept_local": 0, "unchanged": 0, "removed": 0}
    seen: List[str] = []
    for ev in events:
        ext_id = str(ev.get("id") or "").strip()
        if not ext_id:
            continue
        seen.append(ext_id)
        outcome = store.upsert_external(account_id, ev, source=provider)
        stats[outcome] = stats.get(outcome, 0) + 1
    stats["removed"] = store.mark_missing_external(account_id, seen, start_ts, end_ts)
    return stats


# ── one account ───────────────────────────────────────────────────────────────

def sync_account(scope: str, cred_username: Optional[str], acc: Dict[str, Any], *,
                 store: Optional[CalendarStore] = None, now_ts: Optional[float] = None) -> Dict[str, Any]:
    """Push, pull, deletions for one account; never raises. `changed` counts every row the
    sweep touched so the caller knows whether to tell the UI."""
    account_id, provider = account_id_of(acc), provider_of(acc)
    result: Dict[str, Any] = {"ok": False, "account": account_id, "changed": 0}
    if not account_id or provider not in CALENDAR_PROVIDERS:
        result["error"] = "not a calendar account"
        return result
    with _lock_for(scope, account_id):
        return _sync_account_locked(scope, cred_username, acc, account_id, provider, result,
                                    store=store, now_ts=now_ts)


def _sync_account_locked(scope: str, cred_username: Optional[str], acc: Dict[str, Any], account_id: str,
                         provider: str, result: Dict[str, Any], *, store: Optional[CalendarStore],
                         now_ts: Optional[float]) -> Dict[str, Any]:
    try:
        store = store or CalendarStore(scope)
    except Exception as e:
        result["error"] = f"calendar store unavailable: {e}"
        return result
    state = store.account_state(account_id)
    if not state.get("enabled", True):
        return {"ok": True, "account": account_id, "changed": 0, "skipped": "disabled"}
    token_sig = _token_signature(account_id, provider, cred_username, scope)
    if state.get("needs_reconsent"):
        failed_sig = (state.get("sync_state") or {}).get("failed_token")
        if token_sig is None or token_sig == failed_sig:
            return {"ok": False, "account": account_id, "changed": 0, "skipped": "reconsent",
                    "needs_reconsent": True}
    username = _username_for(scope, cred_username)
    try:
        from vaf.core.user_time import resolve_user_timezone_name
        tz_fallback = resolve_user_timezone_name(username)
    except Exception:
        tz_fallback = None
    try:
        if push_enabled():
            pushed = push_pending(store, scope, cred_username, acc, tz_fallback=tz_fallback)
        else:
            pushed = {"pushed": 0, "deleted": 0, "failed": 0, "push_disabled": 1}
        start_ts, end_ts = sync_window(now_ts)
        pulled = pull_window(store, scope, cred_username, acc, start_ts, end_ts)
        store.mark_account_synced(account_id, needs_reconsent=False, sync_state={})
        changed = (pushed["pushed"] + pushed["deleted"] + pulled["created"] + pulled["updated"]
                   + pulled["cancelled"] + pulled["removed"])
        result.update({"ok": True, "changed": changed, "push": pushed, "pull": pulled})
        return result
    except cc.AuthError as e:
        logger.warning("calendar sync: re-consent needed for %s: %s", mask_account(account_id), e)
        store.mark_account_synced(account_id, error=f"re-consent needed: {e}", needs_reconsent=True,
                                  sync_state={"failed_token": token_sig})
        result.update({"error": str(e), "needs_reconsent": True})
        return result
    except cc.ProviderError as e:
        logger.warning("calendar sync: provider error for %s: %s", mask_account(account_id), e)
        store.mark_account_synced(account_id, error=str(e))
        result["error"] = str(e)
        return result
    except Exception as e:
        logger.warning("calendar sync failed for %s: %s", mask_account(account_id), e)
        store.mark_account_synced(account_id, error=str(e))
        result["error"] = str(e)
        return result


def sync_scope_now(scope: str, account_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """A user's "sync now": every calendar account of the scope (or the one named), run in
    the caller's thread, results returned. Serialised per account against the supervisor
    by the account lock; a sweep already running for that account finishes first."""
    results = [sync_account(s, u, acc) for s, u, acc in calendar_accounts_for(scope, account_id)]
    if any(r.get("changed") for r in results):
        _signal(scope)
    return results


def _signal(scope: str) -> None:
    """Tell the user's open calendar views to refetch; a no-op outside the web process."""
    try:
        from vaf.core.web_interface import notify_calendar_changed
        notify_calendar_changed(scope)
    except Exception:
        pass


def after_local_change(scope: str, account_id: Optional[str] = None) -> bool:
    """What every local write (route or tool) does afterwards: the browser is told to
    refetch, and if the event is mirrored the supervisor is asked to push now. Returns
    whether a push was scheduled (False in a process without the supervisor: the next
    sweep pushes)."""
    _signal(scope)
    if not account_id:
        return False
    return request_sync(scope, account_id)


def reconcile_accounts(configured_by_scope: Dict[str, set]) -> int:
    """Detach, in every calendar on disk, the accounts that no longer exist in the config
    lanes at all (a full removal, not a disabled or mail-deleted entry). Their events stay,
    local and read-only. Returns the number of accounts detached."""
    detached = 0
    for scope in CalendarStore.scopes_with_store():
        try:
            store = CalendarStore(scope)
            live = configured_by_scope.get(str(scope), set())
            for st in store.list_account_states():
                if st["account_id"] not in live:
                    kept = store.detach_account(st["account_id"])
                    detached += 1
                    logger.info("calendar sync: detached %s (%d events kept)", mask_account(st["account_id"]), kept)
        except Exception as e:
            logger.warning("calendar sync: reconciliation failed for a scope: %s", e)
    return detached


# ── the supervisor ────────────────────────────────────────────────────────────

class CalendarSyncSupervisor(SyncSupervisor):
    name = NAME

    def sweep_interval(self) -> float:
        return sync_interval_seconds()

    def wants(self, acc: Dict[str, Any]) -> bool:
        return wants_calendar_sync(acc)

    def sync_one(self, scope: str, cred_username: Optional[str], acc: Dict[str, Any]) -> Dict[str, Any]:
        return sync_account(scope, cred_username, acc)

    async def after_sweep(self, accounts: List[Account], wanted: List[Account], results: List[Any]) -> None:
        configured: Dict[str, set] = {}
        for s, _u, a in collect_email_accounts(include_disabled=True):
            configured.setdefault(str(s), set()).add(account_id_of(a))
        try:
            reconcile_accounts(configured)
        except Exception as e:
            logger.warning("calendar sync: reconciliation error: %s", e)
        for (s, _u, a), r in zip(wanted, results):
            if isinstance(r, dict) and r.get("changed"):
                self.notify_change(s, account_id_of(a), r)


def request_sync(scope: str, account_id: Optional[str] = None) -> bool:
    """Ask the running supervisor to sync one scope's calendar accounts now (after a local
    change). Returns False when nothing was scheduled: no supervisor in this process, no
    calendar account, or a sync already on its way; the next sweep pushes in every case."""
    sup = running_supervisor(NAME)
    if sup is None:
        return False
    scheduled = False
    for s, u, acc in calendar_accounts_for(scope, account_id):
        if sup.request_sync(account_key(s, acc), s, u, acc):
            scheduled = True
    return scheduled


# ── reminders ─────────────────────────────────────────────────────────────────

_TEXT = {
    "de": {"reminder": "Erinnerung", "with": "mit", "and": "und", "all_day": "ganztägig",
           "missed": "Verpasste Erinnerung (Backend war offline)"},
    "en": {"reminder": "Reminder", "with": "with", "and": "and", "all_day": "all day",
           "missed": "Missed reminder (backend was offline)"},
}


def _language(scope: str, username: Optional[str]) -> str:
    try:
        from vaf.core import vocab
        lang = vocab.resolve_user_language(scope, username)
    except Exception:
        lang = "en"
    return "de" if str(lang or "").lower().startswith("de") else "en"


def _join_names(names: Iterable[str], lang: str) -> str:
    names = [n for n in names if n]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + f" {_TEXT[lang]['and']} " + names[-1]


def _contact_names(contact_ids: Iterable[str], username: Optional[str], scope: str) -> List[str]:
    names: List[str] = []
    try:
        from vaf.core.contacts_store import get_contact_by_id
    except Exception:
        return names
    for cid in contact_ids:
        try:
            c = get_contact_by_id(str(cid), username, user_scope_id=scope)
        except Exception:
            c = None
        if c and c.get("name"):
            names.append(str(c["name"]))
    return names


def _event_zone(ev: Dict[str, Any], username: Optional[str]):
    if ev.get("tz"):
        try:
            return ZoneInfo(str(ev["tz"]))
        except Exception:
            pass
    try:
        from vaf.core.user_time import resolve_user_timezone
        zone = resolve_user_timezone(username)
    except Exception:
        zone = None
    return zone or timezone.utc


def compose_reminder_text(ev: Dict[str, Any], *, username: Optional[str], scope: str,
                          language: Optional[str] = None, contact_names: Optional[List[str]] = None) -> str:
    """"Erinnerung: {title}, {time}{, location}{, mit {names}}" in the user's language; the
    time in the user's date and time format, an all-day event by its date."""
    lang = language if language in _TEXT else _language(scope, username)
    words = _TEXT[lang]
    from vaf.core.user_time import format_user_date, format_user_datetime
    when = datetime.fromtimestamp(float(ev["start_ts"]), _event_zone(ev, username))
    if ev.get("all_day"):
        time_part = f"{format_user_date(when, username=username, language=lang)} ({words['all_day']})"
    else:
        time_part = format_user_datetime(when, username=username, language=lang, seconds=False)
    parts = [f"{words['reminder']}: {(ev.get('title') or '').strip() or '?'}", time_part]
    if (ev.get("location") or "").strip():
        parts.append(str(ev["location"]).strip())
    names = contact_names if contact_names is not None else _contact_names(ev.get("contact_ids") or [], username, scope)
    joined = _join_names(names, lang)
    if joined:
        parts.append(f"{words['with']} {joined}")
    return ", ".join(parts)


def _deliver(scope: str, username: Optional[str], text: str) -> bool:
    """Main messenger first (the canonical router), and the Web UI notification in every
    case so the reminder shows in the bell too; never raises."""
    sent = False
    try:
        from vaf.core.messaging_connections import send_to_main_messenger
        sent, _ch = send_to_main_messenger(scope, username, text)
    except Exception as e:
        logger.warning("calendar reminder: messenger delivery failed: %s", e)
    try:
        from vaf.core.user_notifications import append_notification
        append_notification(scope, kind="automation", title=text[:120], status="success", summary=text[:500])
    except Exception:
        pass
    return bool(sent)


def fire_due_calendar_reminders(now_ts: Optional[float] = None) -> int:
    """Scheduler-tick hook: deliver every due reminder of every calendar on disk. Within the
    grace after the reminder time it is still delivered (the backend may have been down);
    older ones are marked missed with an honest notification. Never raises. Returns the
    number of state changes."""
    now = float(now_ts if now_ts is not None else datetime.now(timezone.utc).timestamp())
    changed = 0
    for scope in CalendarStore.scopes_with_store():
        try:
            store = CalendarStore(scope)
            due = store.due_reminders(now)
        except Exception as e:
            logger.warning("calendar reminder: store unavailable for a scope: %s", e)
            continue
        if not due:
            continue
        username = _username_for(scope, None)
        lang = _language(scope, username)
        for ev in due:
            try:
                reminder_at = float(ev.get("reminder_at") or ev["start_ts"])
                if now - reminder_at > REMINDER_GRACE_SECONDS:
                    store.mark_reminder(ev["id"], "missed", now)
                    try:
                        from vaf.core.user_notifications import append_notification
                        append_notification(scope, kind="automation", title=_TEXT[lang]["missed"], status="error",
                                            summary=compose_reminder_text(ev, username=username, scope=scope,
                                                                          language=lang)[:500])
                    except Exception:
                        pass
                else:
                    text = compose_reminder_text(ev, username=username, scope=scope, language=lang)
                    _deliver(scope, username, text)
                    store.mark_reminder(ev["id"], "fired", now)
                changed += 1
            except Exception as e:
                logger.warning("calendar reminder failed for an event: %s", e)
    return changed
