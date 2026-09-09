# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""MailSyncSupervisor: background sync for the v2 engine (EMAIL_CLIENT.md).

Runs as one asyncio task inside the web backend on the shared supervisor base
(vaf/core/sync_supervisor.py): every sweep collects every configured account across
all user scopes and syncs each IMAP-reachable one in a worker thread with per-account
crash isolation - one broken account never stalls the others. One IDLE watcher thread
per eager account gives near-instant new-mail pickup on INBOX (re-issued before the
29-minute server limit; a dead IDLE socket means "sync now", per RFC 2177 practice);
folders beyond INBOX ride the periodic sweep. The sync lane covers every account
reachable over IMAP - the password lane plus OAuth accounts once they are imap_ready
(re-consented). After each sweep a provider-agnostic send drain delivers queued outbox
sends for EVERY account (including non-imap_ready Gmail/Microsoft, or accounts whose
IMAP was down), so a queued send is never stranded behind IMAP availability.

New-mail hook (decision E3): observers registered via on_new_mail() are called
with (user_scope_id, account_id, stats) after any sync that ingested mail -
the future automation trigger and WS delta emitter attach here.
"""
import asyncio
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from vaf.core.sync_supervisor import Account, SyncSupervisor, account_key, collect_email_accounts

logger = logging.getLogger("vaf.mail.supervisor")

SWEEP_INTERVAL_SEC = 300          # periodic full-tier sweep per account
IDLE_REISSUE_SEC = 25 * 60        # re-issue IDLE before the 29-min server cap
IDLE_CHECK_SEC = 30               # idle_check poll granularity
_new_mail_observers: List[Callable[[str, str, Dict[str, Any]], None]] = []


def on_new_mail(cb: Callable[[str, str, Dict[str, Any]], None]) -> None:
    """Register an observer for 'account ingested new mail' (E3 hook)."""
    _new_mail_observers.append(cb)


def _notify_new_mail(scope: str, account_id: str, stats: Dict[str, Any]) -> None:
    for cb in list(_new_mail_observers):
        try:
            cb(scope, account_id, stats)
        except Exception as e:
            logger.warning("new-mail observer failed: %s", e)


def _wants_sync(acc: Dict[str, Any]) -> bool:
    """Whether this account may be synced by the engine.

    Three separate user intents, all of which the sweep must honor:
    - `enabled`: the account itself is active.
    - `mail_enabled`: a calendar-safe mail delete sets this False and KEEPS the
      config entry (plus the shared OAuth token) so Calendar keeps working. Without
      this check the sweep re-creates the account row and re-ingests the messages
      the delete just purged - a delete that resurrects its own data.
    - `auto_sync_enabled`: the per-account toggle in the account panel. The legacy
      lane honors it; ignoring it here would make switching auto-sync OFF *raise*
      the sync rate (from every 30 min to a 5-min sweep plus a permanent IDLE
      connection), i.e. a switch that does the opposite of what it says.
    Send draining is deliberately NOT gated by this - a queued mail must still
    leave even when the mailbox is not being polled.
    """
    return (acc.get("enabled", True)
            and acc.get("mail_enabled", True)
            and acc.get("auto_sync_enabled", True))


# The account collection lives in vaf/core/sync_supervisor.collect_email_accounts (the
# calendar supervisor reads the same lanes); this is the SEND-DRAIN set, deliberately
# wider than the sync set (see _wants_sync), so a queued mail still leaves even for an
# account whose mailbox is no longer polled.
_collect_accounts = collect_email_accounts


def _sync_one(scope: str, cred_username: Optional[str], acc: Dict[str, Any]) -> Dict[str, Any]:
    """Blocking: one full account sync (runs inside asyncio.to_thread)."""
    from vaf.mail.imap_client import MailAuthError, _safe_logout, build_imap_client
    from vaf.mail.service import MailService
    from vaf.mail.sync import ImapSyncEngine
    account_id = acc.get("account_id") or acc.get("email") or ""
    try:
        client = build_imap_client(acc, cred_username, scope)
    except (MailAuthError, ValueError) as e:
        return {"ok": False, "account": account_id, "error": str(e)}
    try:
        svc = MailService(scope)
        eng = ImapSyncEngine(svc.store, account_id, acc.get("provider") or "imap",
                             acc.get("email") or account_id, client)
        # replay queued local writes first (flags/move/append/send) so user
        # actions reach the server before the next read pass re-syncs state
        try:
            from vaf.core.config import Config
            from vaf.mail.writeback import OpExecutor
            OpExecutor(svc.store, eng.account_pk, client, acc, scope,
                       cred_username=cred_username).process(
                write_enabled=bool(Config.get("mail_engine_write_enabled", False)))
        except Exception as e:
            logger.warning("op replay failed for %s: %s", (account_id or "")[:3] + "***", e)
        stats = eng.sync_account()
        new_total = sum(int(s.get("new", 0)) for s in stats.values())
        if new_total:
            _notify_new_mail(scope, account_id, {"new": new_total, "folders": stats})
        try:
            from vaf.mail.migrate import import_legacy_artifacts
            import_legacy_artifacts(svc.store, cred_username or "", scope,
                                    account_id=account_id)
        except Exception as e:
            logger.info("legacy artifact import skipped: %s", e)
        try:
            from vaf.core.config import Config
            svc.store.maybe_evict_old_bodies(int(Config.get("mail_body_retention_days", 365)))
        except Exception as e:
            logger.info("retention pass skipped: %s", e)
        return {"ok": True, "account": account_id, "stats": stats}
    except Exception as e:
        logger.warning("account sync failed for %s: %s", (account_id or "")[:3] + "***", e)
        return {"ok": False, "account": account_id, "error": str(e)}
    finally:
        _safe_logout(client)


def _sync_changed(stats: Dict[str, Any]) -> bool:
    """Whether a sync changed what a conversation list shows: new mail, flag updates (read,
    answered) or vanished messages in any folder. The new-mail hook above stays what it is;
    this is the wider question the inbox asks."""
    return any(int(s.get("new", 0) or 0) + int(s.get("flag_updates", 0) or 0) + int(s.get("vanished", 0) or 0)
               for s in stats.values() if isinstance(s, dict))


def _drain_sends(scope: str, cred_username: Optional[str], acc: Dict[str, Any]) -> Dict[str, Any]:
    """Deliver queued SEND ops for one account regardless of IMAP availability -
    sends go through the SMTP/API transport and need no IMAP session. Runs AFTER
    the IMAP sync in the sweep, so imap accounts keep their Sent-APPEND (their
    sends already drained in _sync_one); only sends the IMAP sync could not
    handle (non-imap_ready accounts, or accounts whose IMAP client failed to
    build) land here. The atomic op claim makes running both passes safe."""
    from vaf.core.config import Config
    from vaf.mail.imap_client import MailAuthError, NullImapClient, _safe_logout, build_imap_client
    from vaf.mail.service import MailService
    from vaf.mail.writeback import OpExecutor
    account_id = acc.get("account_id") or acc.get("email") or ""
    svc = MailService(scope)
    apk = svc.store.account_pk(account_id)
    if apk is None:
        return {"ok": True, "drained": 0}
    # Only do work when a send is actually queued (avoid opening IMAP for nothing).
    if not any(o["kind"] == "send" for o in svc.store.pending_ops(apk)):
        return {"ok": True, "drained": 0}
    client = None
    try:
        try:
            client = build_imap_client(acc, cred_username, scope)
        except (MailAuthError, ValueError):
            client = None  # send still works; Sent-APPEND is skipped for this pass
        stats = OpExecutor(svc.store, apk, client or NullImapClient(), acc, scope,
                           cred_username=cred_username).process(
            write_enabled=bool(Config.get("mail_engine_write_enabled", False)) and client is not None,
            allowed_kinds={"send"})
        return {"ok": True, "drained": int(stats.get("done", 0))}
    except Exception as e:
        logger.warning("send drain failed for %s: %s", (account_id or "")[:3] + "***", e)
        return {"ok": False, "error": str(e)}
    finally:
        if client is not None:
            _safe_logout(client)


class _IdleWatcher(threading.Thread):
    """One IDLE connection pinned to INBOX. On server activity (or a dead
    socket) it requests an immediate account sync via the callback. Restarted
    by the supervisor sweep when it dies (crash isolation)."""

    def __init__(self, scope: str, cred_username: Optional[str], acc: Dict[str, Any],
                 request_sync: Callable[[], None]):
        super().__init__(daemon=True, name=f"mail-idle-{(acc.get('account_id') or '')[:3]}***")
        self.scope, self.cred_username, self.acc = scope, cred_username, acc
        self.request_sync = request_sync
        self.stop_event = threading.Event()

    def run(self) -> None:
        from vaf.mail.imap_client import MailAuthError, _safe_logout, build_imap_client
        try:
            client = build_imap_client(self.acc, self.cred_username, self.scope)
        except (MailAuthError, ValueError):
            return  # no IDLE lane; periodic sweep still covers the account
        try:
            if not client.has_capability("IDLE"):
                return  # server has no IDLE; periodic sweep covers the account
            client.select_folder("INBOX", readonly=True)
            while not self.stop_event.is_set():
                client.idle()
                started = time.monotonic()
                triggered = False
                while (time.monotonic() - started) < IDLE_REISSUE_SEC and not self.stop_event.is_set():
                    responses = client.idle_check(timeout=IDLE_CHECK_SEC)
                    # only real mailbox events count - servers emit periodic
                    # "OK still here" keepalives that must not trigger resyncs
                    if any(len(r) > 1 and r[1] in (b"EXISTS", b"RECENT", b"EXPUNGE", b"FETCH")
                           for r in (responses or []) if isinstance(r, tuple)):
                        triggered = True
                        break
                client.idle_done()
                if triggered:
                    self.request_sync()
        except Exception:
            # dead IDLE socket means "resync now" (RFC 2177 practice)
            self.request_sync()
        finally:
            _safe_logout(client)


class MailSyncSupervisor(SyncSupervisor):
    name = "mail"

    def __init__(self):
        super().__init__()
        self._watchers: Dict[str, _IdleWatcher] = {}

    def sweep_interval(self) -> float:
        return SWEEP_INTERVAL_SEC

    def wants(self, acc: Dict[str, Any]) -> bool:
        return bool(_wants_sync(acc)
                    and ((acc.get("provider") or "imap").lower() == "imap" or acc.get("imap_ready")))

    def sync_one(self, scope: str, cred_username: Optional[str], acc: Dict[str, Any]) -> Dict[str, Any]:
        out = _sync_one(scope, cred_username, acc)
        if out.get("ok") and _sync_changed(out.get("stats") or {}):
            self.notify_change(scope, out.get("account") or "", out.get("stats") or {})
        return out

    async def after_sweep(self, accounts: List[Account], wanted: List[Account], results: List[Any]) -> None:
        # Provider-agnostic send drain AFTER the sync: delivers queued sends for EVERY
        # account (incl. non-imap_ready gmail/microsoft and accounts whose IMAP was
        # down), so a queued send is never stranded. imap accounts already drained
        # their sends in _sync_one, so this is a cheap no-op for them (guarded by a
        # pending-send check).
        async def _bounded_drain(s, u, a):
            async with self._sem:
                return await asyncio.to_thread(_drain_sends, s, u, a)

        await asyncio.gather(*[_bounded_drain(s, u, a) for s, u, a in accounts],
                             return_exceptions=True)
        self._ensure_idle_watchers(wanted)

    def _ensure_idle_watchers(self, accounts) -> None:
        alive_keys = set()
        for scope, cred_username, acc in accounts:
            key = account_key(scope, acc)
            alive_keys.add(key)
            w = self._watchers.get(key)
            if w is None or not w.is_alive():
                w = _IdleWatcher(scope, cred_username, acc,
                                 request_sync=lambda k=key, s=scope, u=cred_username, a=acc:
                                 self.request_sync(k, s, u, a))
                self._watchers[key] = w
                w.start()
        for key in list(self._watchers):
            if key not in alive_keys:
                self._watchers.pop(key).stop_event.set()
