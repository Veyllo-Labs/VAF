# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The shared sync supervisor base (vaf/core/sync_supervisor.py): the account collection
both lanes read, the sweep with its cap and per-account crash isolation, the deduplicated
request_sync lane, one running instance per process, and the mail supervisor as a subclass
that kept its own filter. Extracted from the mail supervisor when the calendar became the
second lane; the mail tests in tests/test_mail_supervisor_accounts.py pin the subclass."""
import asyncio
import threading
from pathlib import Path

import pytest

import vaf.core.config as cfg_mod
import vaf.core.sync_supervisor as ss
import vaf.mail.supervisor as mail_sup

ROOT = Path(__file__).resolve().parents[1]


def _accounts(monkeypatch, accounts, by_scope=None):
    # Only Config.get is faked; get_local_admin_scope_id() reads "admin-scope" from it (a fake
    # placed on the config module leaks into every module first imported while it is active).
    store = {"email_config": {"accounts": accounts},
             "email_config_by_scope": by_scope or {},
             "email_config_by_user": {"bob": {"accounts": [{"account_id": "legacy@x"}]}},
             "local_admin_scope_id": "admin-scope"}
    monkeypatch.setattr(cfg_mod.Config, "get", staticmethod(lambda k, d=None: store.get(k, d)))


@pytest.fixture(autouse=True)
def _fresh_registry():
    ss._reset_for_tests()
    yield
    ss._reset_for_tests()


class _Fake(ss.SyncSupervisor):
    name = "fake"
    settle_sec = 0

    def __init__(self, fail=()):
        super().__init__()
        self.synced = []
        self.after = None
        self.fail = set(fail)
        self.threads = set()

    def wants(self, acc):
        return acc.get("provider") == "x"

    def sync_one(self, scope, cred_username, acc):
        self.threads.add(threading.get_ident())
        aid = acc["account_id"]
        if aid in self.fail:
            raise RuntimeError(f"boom {aid}")
        self.synced.append((scope, aid))
        return {"ok": True, "account": aid}

    async def after_sweep(self, accounts, wanted, results):
        self.after = (accounts, wanted, results)


# ── the account collection ───────────────────────────────────────────────────────

def test_collect_reads_both_scope_lanes_and_never_the_legacy_user_lane(monkeypatch):
    _accounts(monkeypatch,
              [{"account_id": "a@x"}, {"account_id": "off@x", "enabled": False}],
              by_scope={"scope-1": {"accounts": [{"account_id": "s1@x"}]},
                        "admin-scope": {"accounts": [{"account_id": "dup@x"}]}})
    rows = ss.collect_email_accounts()
    assert [(s, a["account_id"]) for s, _u, a in rows] == [("admin-scope", "a@x"), ("scope-1", "s1@x")]
    assert all(u is None for _s, u, _a in rows)
    # "gone" versus "disabled": the reconciliation must see the switched-off entry too.
    with_off = [a["account_id"] for _s, _u, a in ss.collect_email_accounts(include_disabled=True)]
    assert with_off == ["a@x", "off@x", "s1@x"]
    assert "legacy@x" not in with_off


def test_mail_supervisor_keeps_its_collector_name_on_the_shared_function(monkeypatch):
    _accounts(monkeypatch, [{"account_id": "a@x", "provider": "imap"}])
    assert mail_sup._collect_accounts is ss.collect_email_accounts
    assert [a["account_id"] for _s, _u, a in mail_sup._collect_accounts()] == ["a@x"]


# ── the sweep ────────────────────────────────────────────────────────────────────

def test_sweep_syncs_wanted_accounts_off_the_loop_and_isolates_a_crash(monkeypatch):
    _accounts(monkeypatch, [
        {"account_id": "a@x", "provider": "x"},
        {"account_id": "bad@x", "provider": "x"},
        {"account_id": "other@x", "provider": "y"},
    ])
    sup = _Fake(fail={"bad@x"})

    async def go():
        sup._loop = asyncio.get_running_loop()
        return await sup.sweep()

    results = asyncio.run(go())
    assert sup.synced == [("admin-scope", "a@x")]                     # only the wanted, minus the crash
    assert isinstance(results[0], dict) and results[0]["ok"]
    assert isinstance(results[1], RuntimeError)                          # returned, not raised
    accounts, wanted, after_results = sup.after
    assert len(accounts) == 3 and [a["account_id"] for _s, _u, a in wanted] == ["a@x", "bad@x"]
    assert after_results == results
    assert threading.get_ident() not in sup.threads                      # sync_one ran in a worker thread


def test_sweep_respects_the_parallel_cap(monkeypatch):
    _accounts(monkeypatch, [{"account_id": f"{i}@x", "provider": "x"} for i in range(6)])
    peak = {"now": 0, "max": 0}
    lock = threading.Lock()

    class _Slow(_Fake):
        def sync_one(self, scope, cred_username, acc):
            import time
            with lock:
                peak["now"] += 1
                peak["max"] = max(peak["max"], peak["now"])
            time.sleep(0.05)
            with lock:
                peak["now"] -= 1
            return {"ok": True}

    sup = _Slow()

    async def go():
        sup._loop = asyncio.get_running_loop()
        await sup.sweep()

    asyncio.run(go())
    assert 1 <= peak["max"] <= ss.DEFAULT_MAX_PARALLEL


# ── request_sync ─────────────────────────────────────────────────────────────────

def test_request_sync_is_a_noop_without_a_loop_and_deduplicates_while_pending(monkeypatch):
    _accounts(monkeypatch, [])
    sup = _Fake()
    acc = {"account_id": "a@x", "provider": "x"}
    assert sup.request_sync("k", "admin-scope", None, acc) is False       # no loop yet: nothing scheduled

    gate = threading.Event()

    class _Blocking(_Fake):
        def sync_one(self, scope, cred_username, acc):
            gate.wait(2)
            return super().sync_one(scope, cred_username, acc)

    sup = _Blocking()

    async def go():
        sup._loop = asyncio.get_running_loop()
        first = await asyncio.to_thread(sup.request_sync, "k", "admin-scope", None, acc)
        second = await asyncio.to_thread(sup.request_sync, "k", "admin-scope", None, acc)
        gate.set()
        for _ in range(100):
            if not sup._pending:
                break
            await asyncio.sleep(0.02)
        third = await asyncio.to_thread(sup.request_sync, "k", "admin-scope", None, acc)
        for _ in range(100):
            if not sup._pending:
                break
            await asyncio.sleep(0.02)
        return first, second, third

    first, second, third = asyncio.run(go())
    assert (first, second, third) == (True, False, True)
    assert sup.synced == [("admin-scope", "a@x"), ("admin-scope", "a@x")]   # two syncs for three requests


# ── one instance per process ─────────────────────────────────────────────────────

def test_start_supervisor_runs_once_per_process_and_running_supervisor_finds_it():
    started = []

    class _Idle(_Fake):
        async def run(self):
            started.append(self)
            self._loop = asyncio.get_running_loop()
            await asyncio.sleep(3600)

    async def go():
        assert ss.running_supervisor("fake") is None
        a, b = _Idle(), _Idle()
        first = ss.start_supervisor(a)
        second = ss.start_supervisor(b)                    # the second lifespan under TLS
        await asyncio.sleep(0)
        live = ss.running_supervisor("fake")
        task = ss._tasks["fake"]
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        gone = ss.running_supervisor("fake")
        third = ss.start_supervisor(_Idle())               # a finished task does not block a restart
        ss._tasks["fake"].cancel()
        return first, second, live is a, len(started), gone, third

    assert asyncio.run(go()) == (True, False, True, 1, None, True)


# ── the mail supervisor as a subclass ────────────────────────────────────────────

def test_mail_supervisor_is_a_subclass_with_its_own_filter():
    sup = mail_sup.MailSyncSupervisor()
    assert isinstance(sup, ss.SyncSupervisor) and sup.name == "mail"
    assert sup.sweep_interval() == mail_sup.SWEEP_INTERVAL_SEC
    assert sup.wants({"account_id": "a@x", "provider": "imap"}) is True
    assert sup.wants({"account_id": "g@x", "provider": "gmail"}) is False            # not imap_ready
    assert sup.wants({"account_id": "g@x", "provider": "gmail", "imap_ready": True}) is True
    assert sup.wants({"account_id": "a@x", "provider": "imap", "auto_sync_enabled": False}) is False
    assert sup.wants({"account_id": "a@x", "provider": "imap", "mail_enabled": False}) is False


def test_web_server_starts_both_supervisors_through_the_guard():
    src = (ROOT / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    assert "asyncio.create_task(MailSyncSupervisor().run())" not in src, "the bare start ran twice under TLS"
    assert "start_supervisor(MailSyncSupervisor())" in src
    assert "CalendarSyncSupervisor()" in src and "start_supervisor(_cal_sup)" in src
    assert "_cal_sup.on_change(" in src, "a changed sweep must reach the browser (calendar_changed)"
    assert "Email auto-sync background task started" not in src, "a log line for a lane that no longer exists"
