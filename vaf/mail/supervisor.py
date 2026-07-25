# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""MailSyncSupervisor: background sync for the v2 engine (EMAIL_CLIENT.md).

Runs as one asyncio task inside the web backend. Every cycle it re-reads
the configured accounts every cycle, collects every
configured account across all user scopes, and syncs each account in a worker
thread with per-account crash isolation - one broken account never stalls the
others. One IDLE watcher thread per eager account gives near-instant new-mail
pickup on INBOX (re-issued before the 29-minute server limit; a dead IDLE
socket means "sync now", per RFC 2177 practice); folders beyond INBOX ride the
periodic sweep. The sync lane covers every account reachable over IMAP - the
password lane plus OAuth accounts once they are imap_ready (re-consented). After
each sweep a provider-agnostic send drain delivers queued outbox sends for EVERY
account (including non-imap_ready Gmail/Microsoft, or accounts whose IMAP was
down), so a queued send is never stranded behind IMAP availability.

New-mail hook (decision E3): observers registered via on_new_mail() are called
with (user_scope_id, account_id, stats) after any sync that ingested mail -
the future automation trigger and WS delta emitter attach here.
"""
import asyncio
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("vaf.mail.supervisor")

SWEEP_INTERVAL_SEC = 300          # periodic full-tier sweep per account
IDLE_REISSUE_SEC = 25 * 60        # re-issue IDLE before the 29-min server cap
IDLE_CHECK_SEC = 30               # idle_check poll granularity
_MAX_PARALLEL_SYNCS = 2

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


def _collect_accounts() -> List[Tuple[str, Optional[str], Dict[str, Any]]]:
    """(user_scope_id, cred_username, account) for every enabled account in
    every config lane. Scope-explicit by construction: the admin lane uses the
    admin's real scope UUID. This is the SEND-DRAIN set - deliberately wider than
    the sync set (see _wants_sync), so a queued mail still leaves even for an
    account whose mailbox is no longer polled."""
    from vaf.core.config import Config, get_local_admin_scope_id
    out: List[Tuple[str, Optional[str], Dict[str, Any]]] = []
    admin_scope = get_local_admin_scope_id()
    ec = Config.get("email_config") or {}
    for acc in (ec.get("accounts") or []):
        if acc.get("enabled", True):
            out.append((admin_scope, None, acc))
    by_scope = Config.get("email_config_by_scope") or {}
    if isinstance(by_scope, dict):
        for scope, cfg in by_scope.items():
            if str(scope) == str(admin_scope):
                continue
            for acc in ((cfg or {}).get("accounts") or []):
                if acc.get("enabled", True):
                    out.append((str(scope), None, acc))
    # Legacy email_config_by_user accounts are deliberately NOT synced into
    # v2: the v2 store is scope-keyed with no username dimension, so mapping
    # them to the admin scope would commingle different users' mail (isolation
    # violation caught in review). They stay fully on the legacy lane until
    # their install migrates to scope-keyed config.
    return out


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


class MailSyncSupervisor:
    def __init__(self):
        self._watchers: Dict[str, _IdleWatcher] = {}
        self._pending: set = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._sem = asyncio.Semaphore(_MAX_PARALLEL_SYNCS)

    def _request_sync(self, key: str, scope: str, cred_username: Optional[str],
                      acc: Dict[str, Any]) -> None:
        """Thread-safe: schedule an immediate account sync on the loop."""
        loop = self._loop
        if loop is None or key in self._pending:
            return
        self._pending.add(key)

        async def _go():
            try:
                async with self._sem:  # same cap as the sweep (review finding)
                    await asyncio.to_thread(_sync_one, scope, cred_username, acc)
            finally:
                self._pending.discard(key)

        fut = asyncio.run_coroutine_threadsafe(_go(), loop)
        # if scheduling itself failed, never strand the dedup key
        if fut.cancelled():
            self._pending.discard(key)

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        await asyncio.sleep(90)  # let the server settle before first sweep
        sem = self._sem
        while True:
            try:
                accounts = _collect_accounts()
                imap_accounts = [(s, u, a) for s, u, a in accounts
                                 if _wants_sync(a)
                                 and ((a.get("provider") or "imap").lower() == "imap"
                                      or a.get("imap_ready"))]

                async def _bounded(s, u, a):
                    async with sem:
                        return await asyncio.to_thread(_sync_one, s, u, a)

                results = await asyncio.gather(
                    *[_bounded(s, u, a) for s, u, a in imap_accounts],
                    return_exceptions=True)
                ok = sum(1 for r in results if isinstance(r, dict) and r.get("ok"))
                if imap_accounts:
                    logger.info("mail v2 sweep: %d/%d accounts ok", ok, len(imap_accounts))

                # Provider-agnostic send drain AFTER the sync: delivers queued
                # sends for EVERY account (incl. non-imap_ready gmail/microsoft
                # and accounts whose IMAP was down), so a queued send is never
                # stranded. imap accounts already drained their sends above, so
                # this is a cheap no-op for them (guarded by a pending-send check).
                async def _bounded_drain(s, u, a):
                    async with sem:
                        return await asyncio.to_thread(_drain_sends, s, u, a)

                await asyncio.gather(*[_bounded_drain(s, u, a) for s, u, a in accounts],
                                     return_exceptions=True)

                self._ensure_idle_watchers(imap_accounts)
            except Exception as e:
                logger.warning("mail v2 supervisor cycle error: %s", e)
            await asyncio.sleep(SWEEP_INTERVAL_SEC)

    def _ensure_idle_watchers(self, accounts) -> None:
        alive_keys = set()
        for scope, cred_username, acc in accounts:
            key = f"{scope}:{acc.get('account_id') or acc.get('email')}"
            alive_keys.add(key)
            w = self._watchers.get(key)
            if w is None or not w.is_alive():
                w = _IdleWatcher(scope, cred_username, acc,
                                 request_sync=lambda k=key, s=scope, u=cred_username, a=acc:
                                 self._request_sync(k, s, u, a))
                self._watchers[key] = w
                w.start()
        for key in list(self._watchers):
            if key not in alive_keys:
                self._watchers.pop(key).stop_event.set()
