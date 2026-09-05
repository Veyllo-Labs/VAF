# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The machinery shared by the background sync supervisors (mail, calendar).

A supervisor is one asyncio task inside the web backend. Every sweep it collects the
configured accounts across all user scopes (collect_email_accounts), keeps the ones its
subclass wants, syncs each one in a worker thread under a shared cap with per-account crash
isolation (one broken account never stalls the others) and lets the subclass finish the
sweep (a send drain, IDLE watchers, a change signal). request_sync() schedules an immediate
sync of one account from any thread and is deduplicated per account while one is pending.

start_supervisor() runs a supervisor once per process although the startup hook of the web
server runs once per uvicorn server (8001 and the internal 8005 channel under TLS): a second
call finds the live task and does nothing; a task stranded on a stopped loop counts as gone.
running_supervisor() hands a route or a tool the live instance so a user action can trigger a
sync now; in a process without one (the CLI) it returns None and the caller treats the
request as a no-op, the next sweep of the web server picks the change up.

Extracted from vaf/mail/supervisor.py when the calendar became the second lane that needed
the same sweep, dedup and crash isolation; the mail supervisor is a subclass now.
"""
import asyncio
import logging
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("vaf.core.sync_supervisor")

SETTLE_SEC = 90                    # let the server settle before the first sweep
DEFAULT_SWEEP_INTERVAL_SEC = 300
DEFAULT_MAX_PARALLEL = 2

Account = Tuple[str, Optional[str], Dict[str, Any]]   # (user_scope_id, cred_username, account)


def mask_account(account_id: Optional[str]) -> str:
    """The first three characters of an account id for log lines; the rest is a person's address."""
    return (account_id or "")[:3] + "***"


def collect_email_accounts(include_disabled: bool = False) -> List[Account]:
    """(user_scope_id, cred_username, account) for every enabled account in every config lane.
    Scope-explicit by construction: the admin lane uses the admin's real scope UUID. This is
    the widest set (the mail send drain uses it as is); a supervisor narrows it with wants().
    `include_disabled` also lists the entries switched off, for a caller that must tell
    "disabled" from "gone" (the calendar's account reconciliation).
    """
    from vaf.core.config import Config, get_local_admin_scope_id
    out: List[Account] = []
    admin_scope = get_local_admin_scope_id()

    def _keep(acc: Dict[str, Any]) -> bool:
        return bool(include_disabled or acc.get("enabled", True))

    ec = Config.get("email_config") or {}
    for acc in (ec.get("accounts") or []):
        if _keep(acc):
            out.append((admin_scope, None, acc))
    by_scope = Config.get("email_config_by_scope") or {}
    if isinstance(by_scope, dict):
        for scope, cfg in by_scope.items():
            if str(scope) == str(admin_scope):
                continue
            for acc in ((cfg or {}).get("accounts") or []):
                if _keep(acc):
                    out.append((str(scope), None, acc))
    # Legacy email_config_by_user accounts are deliberately NOT collected: the scope-keyed
    # stores have no username dimension, so mapping them to the admin scope would commingle
    # different users' data. They stay on the legacy lane until their install migrates.
    return out


def account_key(scope: str, acc: Dict[str, Any]) -> str:
    return f"{scope}:{acc.get('account_id') or acc.get('email') or ''}"


class SyncSupervisor:
    """Subclasses set `name`, implement sync_one() and narrow wants(); the rest is shared."""

    name = "sync"
    max_parallel = DEFAULT_MAX_PARALLEL
    settle_sec = SETTLE_SEC

    def __init__(self) -> None:
        self._pending: set = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._sem = asyncio.Semaphore(self.max_parallel)
        self._observers: List[Callable[[str, str, Dict[str, Any]], None]] = []

    # ── the contract a subclass fills in ─────────────────────────────────────

    def sweep_interval(self) -> float:
        """Seconds between two sweeps; read per sweep so a config change needs no restart."""
        return DEFAULT_SWEEP_INTERVAL_SEC

    def collect(self) -> List[Account]:
        return collect_email_accounts()

    def wants(self, acc: Dict[str, Any]) -> bool:
        """Whether this account is synced by this supervisor (the sweep's own filter)."""
        return True

    def sync_one(self, scope: str, cred_username: Optional[str], acc: Dict[str, Any]) -> Dict[str, Any]:
        """Blocking: one full account sync (runs inside asyncio.to_thread). Returns a dict
        with at least `ok`; a raised exception is caught per account and logged."""
        raise NotImplementedError

    async def after_sweep(self, accounts: List[Account], wanted: List[Account],
                          results: List[Any]) -> None:
        """Runs after every sweep with the full collected set, the synced subset and their
        results (a dict per account, or the exception it raised)."""
        return None

    # ── observers ────────────────────────────────────────────────────────────

    def on_change(self, cb: Callable[[str, str, Dict[str, Any]], None]) -> None:
        """Register an observer called with (user_scope_id, account_id, stats) after a sync
        that changed something, as decided by notify_change()'s caller."""
        self._observers.append(cb)

    def notify_change(self, scope: str, account_id: str, stats: Dict[str, Any]) -> None:
        for cb in list(self._observers):
            try:
                cb(scope, account_id, stats)
            except Exception as e:
                logger.warning("%s change observer failed: %s", self.name, e)

    # ── the shared machinery ─────────────────────────────────────────────────

    async def _bounded(self, scope: str, cred_username: Optional[str], acc: Dict[str, Any]) -> Any:
        async with self._sem:
            return await asyncio.to_thread(self.sync_one, scope, cred_username, acc)

    async def sweep(self) -> List[Any]:
        """One sweep: collect, filter, sync under the cap, hand the subclass the results."""
        accounts = self.collect()
        wanted = [(s, u, a) for s, u, a in accounts if self.wants(a)]
        results = await asyncio.gather(*[self._bounded(s, u, a) for s, u, a in wanted],
                                       return_exceptions=True)
        ok = sum(1 for r in results if isinstance(r, dict) and r.get("ok"))
        if wanted:
            logger.info("%s sweep: %d/%d accounts ok", self.name, ok, len(wanted))
        for (s, _u, a), r in zip(wanted, results):
            if isinstance(r, BaseException):
                logger.warning("%s sync raised for %s: %s", self.name,
                               mask_account(a.get("account_id") or a.get("email")), r)
        await self.after_sweep(accounts, wanted, list(results))
        return list(results)

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        await asyncio.sleep(self.settle_sec)
        while True:
            try:
                await self.sweep()
            except Exception as e:
                logger.warning("%s supervisor cycle error: %s", self.name, e)
            await asyncio.sleep(self.sweep_interval())

    def request_sync(self, key: str, scope: str, cred_username: Optional[str],
                     acc: Dict[str, Any]) -> bool:
        """Thread-safe: schedule an immediate sync of one account on the supervisor's loop.
        One pending request per key; returns False when nothing was scheduled (no loop yet,
        or a sync for that key is already on its way)."""
        loop = self._loop
        if loop is None or key in self._pending:
            return False
        self._pending.add(key)

        async def _go():
            try:
                await self._bounded(scope, cred_username, acc)
            except Exception as e:
                logger.warning("%s requested sync failed for %s: %s", self.name,
                               mask_account(acc.get("account_id") or acc.get("email")), e)
            finally:
                self._pending.discard(key)

        try:
            fut = asyncio.run_coroutine_threadsafe(_go(), loop)
        except Exception:
            self._pending.discard(key)
            return False
        if fut.cancelled():          # if scheduling itself failed, never strand the dedup key
            self._pending.discard(key)
            return False
        return True


# ── one instance per process ──────────────────────────────────────────────────

_gate = threading.Lock()
_running: Dict[str, SyncSupervisor] = {}
_tasks: Dict[str, "asyncio.Task[Any]"] = {}


def _task_alive(task: Optional["asyncio.Task[Any]"]) -> bool:
    if task is None or task.done():
        return False
    try:
        return task.get_loop().is_running()
    except Exception:
        return False


def start_supervisor(supervisor: SyncSupervisor) -> bool:
    """Start `supervisor` as a task on the running loop unless one of its name is alive.
    Returns True when this call started it. Must be called from inside a running loop."""
    with _gate:
        if _task_alive(_tasks.get(supervisor.name)):
            return False
        _tasks[supervisor.name] = asyncio.create_task(supervisor.run())
        _running[supervisor.name] = supervisor
        return True


def running_supervisor(name: str) -> Optional[SyncSupervisor]:
    """The live supervisor of that name, or None (a process without one, or a dead task)."""
    with _gate:
        if not _task_alive(_tasks.get(name)):
            return None
        return _running.get(name)


def _reset_for_tests() -> None:
    with _gate:
        _running.clear()
        _tasks.clear()
