# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The calendar sync engine (vaf/core/calendar_sync.py) against a fake provider client:
push before pull, external ids and links stored, deletions propagated both ways, cancelled
instances, deletion detection only after a complete pull and only inside the window, a dead
token parks the account until its stored token changes, the push kill switch, the account
filter, reconciliation of removed accounts, and the write-through request that is a no-op
without a running supervisor."""
import asyncio
from types import SimpleNamespace

import pytest

import vaf.core.calendar_sync as cs
import vaf.core.config as cfg_mod
import vaf.core.sync_supervisor as ss
from vaf.core import calendar_store as cal
from vaf.core.platform import Platform

SCOPE = "11111111-2222-3333-4444-555555555555"
OTHER = "66666666-7777-8888-9999-000000000000"
ACC = {"account_id": "alice@gmail.example", "provider": "gmail", "enabled": True}
NOW = 1_772_366_400.0          # 2026-03-01 12:00:00 UTC
HOUR = 3600.0
DAY = 86400.0


@pytest.fixture
def data_dir(monkeypatch, tmp_path):
    d = tmp_path / "data"
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: d))
    monkeypatch.setattr(cs, "_token_signature", lambda *a, **k: "sig-1")
    monkeypatch.setattr(cs, "_username_for", lambda scope, cred: "alice")
    ss._reset_for_tests()
    yield d
    ss._reset_for_tests()


def _config(monkeypatch, accounts=None, by_scope=None, **overrides):
    # Only Config.get is faked; get_local_admin_scope_id() reads the admin scope from it.
    # Replacing that function on the config module would leak: a module imported for the
    # first time while the patch is active (contacts_store, through the store's migration)
    # binds the fake name for the rest of the process.
    values = {"email_config": {"accounts": accounts if accounts is not None else [ACC]},
              "email_config_by_scope": by_scope or {},
              "local_admin_scope_id": SCOPE,
              "calendar_sync_push_enabled": True, "calendar_sync_past_days": 30,
              "calendar_sync_future_days": 365, "calendar_sync_interval_minutes": 5}
    values.update(overrides)
    monkeypatch.setattr(cfg_mod.Config, "get", staticmethod(lambda k, d=None: values.get(k, d)))
    return values


class FakeClient:
    """Stands in for calendar_client: remembers every write, serves one list per pull."""

    def __init__(self, listing=None):
        self.listing = list(listing or [])
        self.created, self.updated, self.deleted = [], [], []
        self.list_calls = 0
        self.raise_on_list = None
        self.raise_on_write = None
        self.reject_writes = False
        self.next_id = 100
        self.AuthError = cs.cc.AuthError
        self.ProviderError = cs.cc.ProviderError

    def list_events(self, provider, account_id, scope, time_min, time_max, **kw):
        self.list_calls += 1
        self.last_window = (time_min, time_max, kw)
        if self.raise_on_list:
            raise self.raise_on_list
        return list(self.listing)

    def create_event(self, provider, account_id, scope, summary, start, end, **kw):
        if self.raise_on_write:
            raise self.raise_on_write
        self.created.append({"summary": summary, "start": start, "end": end, **kw})
        if self.reject_writes:
            return None
        self.next_id += 1
        return {"id": f"ext{self.next_id}", "updated": NOW, "etag": "e", "link": f"https://cal.example/ext{self.next_id}"}

    def update_event(self, provider, account_id, scope, event_id, **kw):
        if self.raise_on_write:
            raise self.raise_on_write
        self.updated.append({"id": event_id, **kw})
        if self.reject_writes:
            return None
        return {"id": event_id, "updated": NOW + 1, "etag": "e2", "link": f"https://cal.example/{event_id}"}

    def delete_event(self, provider, account_id, scope, event_id, **kw):
        if self.raise_on_write:
            raise self.raise_on_write
        self.deleted.append(event_id)
        return not self.reject_writes


def _install(monkeypatch, client):
    monkeypatch.setattr(cs, "cc", SimpleNamespace(
        AuthError=cs.cc.AuthError, ProviderError=cs.cc.ProviderError,
        list_events=client.list_events, create_event=client.create_event,
        update_event=client.update_event, delete_event=client.delete_event))
    return client


def _ext(ext_id, start_ts, *, title="From Google", status="confirmed", updated=NOW, **more):
    from datetime import datetime, timezone
    iso = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ev = {"id": ext_id, "summary": title, "description": "", "location": "", "start": iso(start_ts),
          "end": iso(start_ts + HOUR), "all_day": False, "tz": "UTC", "status": status, "updated": updated,
          "recurring_event_id": None, "link": f"https://cal.example/{ext_id}", "etag": "e"}
    ev.update(more)
    return ev


def _store(data_dir):
    return cal.CalendarStore(SCOPE, base_dir=data_dir)


# ── the account filter and the window ────────────────────────────────────────────

def test_wants_calendar_sync_keeps_calendar_only_leftovers_and_drops_the_rest():
    assert cs.wants_calendar_sync(ACC) is True
    assert cs.wants_calendar_sync({**ACC, "mail_enabled": False}) is True         # calendar-safe mail delete
    assert cs.wants_calendar_sync({**ACC, "enabled": False}) is False
    assert cs.wants_calendar_sync({"account_id": "a@x", "provider": "imap"}) is False
    assert cs.wants_calendar_sync({"account_id": "m@x", "provider": "microsoft"}) is True
    assert cs.wants_calendar_sync({"provider": "gmail"}) is False                  # no id at all


def test_window_and_interval_come_from_the_config_keys(monkeypatch):
    _config(monkeypatch, calendar_sync_past_days=7, calendar_sync_future_days=14, calendar_sync_interval_minutes=0)
    start, end = cs.sync_window(NOW)
    assert (start, end) == (NOW - 7 * DAY, NOW + 14 * DAY)
    assert cs.sync_interval_seconds() == 60.0                                      # clamped to one minute
    assert cs._iso_utc(NOW) == "2026-03-01T12:00:00Z"


# ── push before pull ─────────────────────────────────────────────────────────────

def test_local_event_is_pushed_first_and_then_recognised_by_the_pull(monkeypatch, data_dir):
    _config(monkeypatch)
    client = _install(monkeypatch, FakeClient())
    store = _store(data_dir)
    ev = store.add_event(title="Dentist", start_ts=NOW + DAY, end_ts=NOW + DAY + HOUR, tz="Europe/Berlin",
                         location="Main St 1", account_id=ACC["account_id"], reminder_minutes=15)
    assert ev["sync_state"] == "pending_push"
    # the pull answers with the freshly created event, as the provider would after the push
    client.listing = [_ext("ext101", NOW + DAY, title="Dentist", location="Main St 1", tz="Europe/Berlin",
                           start="2026-03-02T13:00:00+01:00", end="2026-03-02T14:00:00+01:00")]
    res = cs.sync_account(SCOPE, None, ACC, store=store, now_ts=NOW)
    assert res["ok"], res
    assert client.created and client.created[0]["summary"] == "Dentist"
    assert client.created[0]["start"] == "2026-03-02T13:00:00" and client.created[0]["tz"] == "Europe/Berlin"
    assert client.created[0]["location"] == "Main St 1" and client.created[0]["reminder_minutes"] == 15
    after = store.get_event(ev["id"])
    assert after["sync_state"] == "synced" and after["external_id"] == "ext101"
    assert after["link"] == "https://cal.example/ext101"
    assert res["push"]["pushed"] == 1 and res["pull"]["created"] == 0        # the pull found its own row
    assert len(store.list_events(NOW, NOW + 2 * DAY)) == 1                    # no duplicate
    assert store.account_state(ACC["account_id"])["last_sync_at"] is not None
    assert client.last_window[2].get("strict") is True                        # deletion detection needs a complete list


def test_edit_and_delete_propagate_to_the_provider(monkeypatch, data_dir):
    _config(monkeypatch)
    client = _install(monkeypatch, FakeClient())
    store = _store(data_dir)
    store.upsert_external(ACC["account_id"], _ext("ext1", NOW + DAY, title="Old"), source="gmail")
    store.upsert_external(ACC["account_id"], _ext("ext2", NOW + 2 * DAY, title="Doomed"), source="gmail")
    row1 = store.find_external(ACC["account_id"], "ext1")
    row2 = store.find_external(ACC["account_id"], "ext2")
    store.update_event(row1["id"], title="New", description="")
    store.delete_event(row2["id"])
    assert store.get_event(row2["id"]) is None                                 # gone for readers
    assert store.get_event(row2["id"], include_pending_delete=True)["sync_state"] == "pending_delete"   # owed to the provider
    client.listing = [_ext("ext1", NOW + DAY, title="New", updated=NOW + 1)]
    res = cs.sync_account(SCOPE, None, ACC, store=store, now_ts=NOW)
    assert res["ok"], res
    assert client.updated[0]["id"] == "ext1" and client.updated[0]["summary"] == "New"
    assert client.updated[0]["description"] == ""                              # a cleared field is cleared, not skipped
    assert client.deleted == ["ext2"]
    assert store.get_event(row2["id"]) is None                                 # purged after the provider confirmed
    assert store.get_event(row1["id"])["sync_state"] == "synced"
    assert res["changed"] == 2


def test_all_day_events_are_pushed_as_dates(monkeypatch, data_dir):
    _config(monkeypatch)
    client = _install(monkeypatch, FakeClient())
    store = _store(data_dir)
    store.add_event(title="Holiday", start_ts=NOW, all_day=True, tz="Europe/Berlin", start_date="2026-03-10",
                    account_id=ACC["account_id"])
    cs.sync_account(SCOPE, None, ACC, store=store, now_ts=NOW)
    assert client.created[0]["all_day"] is True
    assert (client.created[0]["start"], client.created[0]["end"]) == ("2026-03-10", "2026-03-11")


def test_a_rejected_write_counts_against_the_event_and_the_pull_still_runs(monkeypatch, data_dir):
    _config(monkeypatch)
    client = _install(monkeypatch, FakeClient([_ext("ext9", NOW + 3 * DAY)]))
    client.reject_writes = True
    store = _store(data_dir)
    ev = store.add_event(title="Stuck", start_ts=NOW + DAY, account_id=ACC["account_id"])
    res = cs.sync_account(SCOPE, None, ACC, store=store, now_ts=NOW)
    assert res["ok"] and res["push"]["failed"] == 1 and res["pull"]["created"] == 1
    after = store.get_event(ev["id"])
    assert after["sync_state"] == "pending_push" and after["push_attempts"] == 1 and after["last_error"]
    for _ in range(cal.PUSH_MAX_ATTEMPTS):
        cs.sync_account(SCOPE, None, ACC, store=store, now_ts=NOW)
    assert store.get_event(ev["id"])["sync_state"] == "push_failed"            # parked after the cap


def test_push_kill_switch_keeps_changes_pending_and_still_pulls(monkeypatch, data_dir):
    _config(monkeypatch, calendar_sync_push_enabled=False)
    client = _install(monkeypatch, FakeClient([_ext("ext9", NOW + 3 * DAY)]))
    store = _store(data_dir)
    ev = store.add_event(title="Held back", start_ts=NOW + DAY, account_id=ACC["account_id"])
    res = cs.sync_account(SCOPE, None, ACC, store=store, now_ts=NOW)
    assert res["ok"] and res["push"].get("push_disabled") == 1
    assert client.created == [] and store.get_event(ev["id"])["sync_state"] == "pending_push"
    assert res["pull"]["created"] == 1


# ── pull: cancellations, deletions, completeness ─────────────────────────────────

def test_cancelled_instance_cancels_locally_and_silences_its_reminder(monkeypatch, data_dir):
    _config(monkeypatch)
    client = _install(monkeypatch, FakeClient([_ext("ext1", NOW + DAY)]))
    store = _store(data_dir)
    cs.sync_account(SCOPE, None, ACC, store=store, now_ts=NOW)
    row = store.find_external(ACC["account_id"], "ext1")
    store.update_event(row["id"], reminder_minutes=30)                         # the user armed a reminder locally
    cs.sync_account(SCOPE, None, ACC, store=store, now_ts=NOW)               # pushes the edit, keeps the row
    client.listing = [_ext("ext1", NOW + DAY, status="cancelled", updated=NOW + 10)]
    res = cs.sync_account(SCOPE, None, ACC, store=store, now_ts=NOW)
    assert res["pull"]["cancelled"] == 1
    assert store.get_event(row["id"])["status"] == "cancelled"
    assert store.due_reminders(NOW + DAY) == []                                 # no reminder for a cancelled event
    assert store.list_events(NOW, NOW + 2 * DAY) == []                          # hidden by default
    assert len(store.list_events(NOW, NOW + 2 * DAY, include_cancelled=True)) == 1


def test_deletion_detection_is_inside_the_window_and_spares_pending_rows(monkeypatch, data_dir):
    _config(monkeypatch, calendar_sync_past_days=2, calendar_sync_future_days=10)
    client = _install(monkeypatch, FakeClient())
    store = _store(data_dir)
    aid = ACC["account_id"]
    for ext_id, ts in (("in", NOW + DAY), ("old", NOW - 5 * DAY), ("far", NOW + 20 * DAY), ("edited", NOW + 2 * DAY)):
        store.upsert_external(aid, _ext(ext_id, ts), source="gmail")
    edited = store.find_external(aid, "edited")
    store.update_event(edited["id"], title="local edit")                        # pending_push: the push decides
    client.reject_writes = True                                                  # ...and the push does not go through
    client.listing = []                                                          # the provider returns nothing in the window
    res = cs.sync_account(SCOPE, None, ACC, store=store, now_ts=NOW)
    assert res["ok"] and res["pull"]["removed"] == 1
    assert store.find_external(aid, "in") is None                               # gone at the provider: gone here
    assert store.find_external(aid, "old") is not None                          # outside the window: untouched
    assert store.find_external(aid, "far") is not None
    assert store.get_event(edited["id"])["sync_state"] == "pending_push"        # a pending row is never removed


def test_an_incomplete_pull_removes_nothing_and_records_the_error(monkeypatch, data_dir):
    _config(monkeypatch)
    client = _install(monkeypatch, FakeClient())
    store = _store(data_dir)
    store.upsert_external(ACC["account_id"], _ext("ext1", NOW + DAY), source="gmail")
    client.raise_on_list = cs.cc.ProviderError("page 2 failed: 503")
    res = cs.sync_account(SCOPE, None, ACC, store=store, now_ts=NOW)
    assert res["ok"] is False and "503" in res["error"]
    assert store.find_external(ACC["account_id"], "ext1") is not None
    state = store.account_state(ACC["account_id"])
    assert "503" in (state["last_error"] or "") and state["needs_reconsent"] is False
    # the next complete pull heals the account record
    client.raise_on_list = None
    client.listing = [_ext("ext1", NOW + DAY)]
    assert cs.sync_account(SCOPE, None, ACC, store=store, now_ts=NOW)["ok"]
    assert store.account_state(ACC["account_id"])["last_error"] is None


def test_newer_change_wins_in_both_directions(monkeypatch, data_dir):
    _config(monkeypatch)
    client = _install(monkeypatch, FakeClient())
    store = _store(data_dir)
    aid = ACC["account_id"]
    store.upsert_external(aid, _ext("ext1", NOW + DAY, title="v1", updated=NOW - 100), source="gmail")
    row = store.find_external(aid, "ext1")
    # the provider changed it later than anything local: the provider wins
    client.listing = [_ext("ext1", NOW + DAY, title="provider v2", updated=NOW + 50)]
    cs.sync_account(SCOPE, None, ACC, store=store, now_ts=NOW)
    assert store.get_event(row["id"])["title"] == "provider v2"
    # a local edit newer than the provider's copy is pushed first; the pull then returns
    # what the provider holds after that push, so the local change stands
    store.update_event(row["id"], title="local v3")
    client.listing = [_ext("ext1", NOW + DAY, title="local v3", updated=NOW + 60)]
    res = cs.sync_account(SCOPE, None, ACC, store=store, now_ts=NOW)
    assert client.updated[-1]["summary"] == "local v3"
    after = store.get_event(row["id"])
    assert after["title"] == "local v3" and after["sync_state"] == "synced" and res["push"]["pushed"] == 1
    # a pull whose copy is older than the pending local edit keeps the local one (kept_local)
    store.update_event(row["id"], title="local v4")
    client.reject_writes = True
    client.listing = [_ext("ext1", NOW + DAY, title="local v3", updated=NOW + 60)]
    res = cs.sync_account(SCOPE, None, ACC, store=store, now_ts=NOW)
    assert res["pull"]["kept_local"] == 1 and store.get_event(row["id"])["title"] == "local v4"


# ── a dead token ─────────────────────────────────────────────────────────────────

def test_auth_error_parks_the_account_until_its_token_changes(monkeypatch, data_dir):
    _config(monkeypatch)
    client = _install(monkeypatch, FakeClient())
    client.raise_on_list = cs.cc.AuthError("401")
    store = _store(data_dir)
    aid = ACC["account_id"]
    store.upsert_external(aid, _ext("ext1", NOW + DAY), source="gmail")
    res = cs.sync_account(SCOPE, None, ACC, store=store, now_ts=NOW)
    assert res["ok"] is False and res["needs_reconsent"] is True
    state = store.account_state(aid)
    assert state["needs_reconsent"] is True and state["sync_state"] == {"failed_token": "sig-1"}
    assert store.find_external(aid, "ext1") is not None                          # nothing was treated as deleted
    # same token: skipped without a network call
    client.raise_on_list = None
    client.listing = [_ext("ext1", NOW + DAY)]
    calls = client.list_calls
    res2 = cs.sync_account(SCOPE, None, ACC, store=store, now_ts=NOW)
    assert res2.get("skipped") == "reconsent" and client.list_calls == calls
    # a re-consent changed the stored token: retried and healed
    monkeypatch.setattr(cs, "_token_signature", lambda *a, **k: "sig-2")
    res3 = cs.sync_account(SCOPE, None, ACC, store=store, now_ts=NOW)
    assert res3["ok"] and store.account_state(aid)["needs_reconsent"] is False


def test_auth_error_during_the_push_stops_the_account_before_the_pull(monkeypatch, data_dir):
    _config(monkeypatch)
    client = _install(monkeypatch, FakeClient([_ext("ext1", NOW + DAY)]))
    client.raise_on_write = cs.cc.AuthError("401 on write")
    store = _store(data_dir)
    ev = store.add_event(title="Pending", start_ts=NOW + DAY, account_id=ACC["account_id"])
    res = cs.sync_account(SCOPE, None, ACC, store=store, now_ts=NOW)
    assert res["ok"] is False and res["needs_reconsent"] is True
    assert client.list_calls == 0
    assert store.get_event(ev["id"])["sync_state"] == "pending_push"           # still owed, not counted as failed


def test_disabled_account_is_skipped_without_a_network_call(monkeypatch, data_dir):
    _config(monkeypatch)
    client = _install(monkeypatch, FakeClient([_ext("ext1", NOW + DAY)]))
    store = _store(data_dir)
    store.set_account_enabled(ACC["account_id"], False)
    res = cs.sync_account(SCOPE, None, ACC, store=store, now_ts=NOW)
    assert res == {"ok": True, "account": ACC["account_id"], "changed": 0, "skipped": "disabled"}
    assert client.list_calls == 0


# ── reconciliation and the supervisor ────────────────────────────────────────────

def test_reconcile_detaches_only_accounts_that_are_gone_from_the_config(monkeypatch, data_dir):
    _config(monkeypatch)
    store = _store(data_dir)
    store.upsert_external("gone@gmail.example", _ext("g1", NOW + DAY), source="gmail")
    store.mark_account_synced("gone@gmail.example")
    store.upsert_external("off@gmail.example", _ext("o1", NOW + DAY), source="gmail")
    store.mark_account_synced("off@gmail.example")
    configured = {SCOPE: {ACC["account_id"], "off@gmail.example"}}              # "off" is disabled, not removed
    assert cs.reconcile_accounts(configured) == 1
    gone = store.find_external("gone@gmail.example", "g1")
    assert gone is None                                                          # detached: no external id any more
    kept = [e for e in store.list_events(NOW, NOW + 2 * DAY) if e["title"] == "From Google"]
    assert len(kept) == 2 and any(e["sync_state"] == "local_only" and e["account_id"] is None for e in kept)
    assert store.find_external("off@gmail.example", "o1")["sync_state"] == "synced"
    assert [s["account_id"] for s in store.list_account_states()] == ["off@gmail.example"]


def test_supervisor_filters_accounts_reconciles_and_signals_changes(monkeypatch, data_dir):
    _config(monkeypatch, accounts=[ACC, {"account_id": "mail@x", "provider": "imap"},
                                   {"account_id": "old@gmail.example", "provider": "gmail", "enabled": False}])
    client = _install(monkeypatch, FakeClient([_ext("ext1", NOW + DAY)]))
    sup = cs.CalendarSyncSupervisor()
    assert isinstance(sup, ss.SyncSupervisor) and sup.name == "calendar" and sup.sweep_interval() == 300.0
    signals = []
    sup.on_change(lambda scope, aid, stats: signals.append((scope, aid, stats["changed"])))
    store = _store(data_dir)
    store.upsert_external("removed@gmail.example", _ext("r1", NOW + DAY), source="gmail")
    store.mark_account_synced("removed@gmail.example")
    store.upsert_external("old@gmail.example", _ext("d1", NOW + DAY), source="gmail")
    store.mark_account_synced("old@gmail.example")
    store.close()

    async def go():
        sup._loop = asyncio.get_running_loop()
        return await sup.sweep()

    results = asyncio.run(go())
    assert len(results) == 1 and results[0]["ok"] and results[0]["pull"]["created"] == 1
    assert client.list_calls == 1                                                # the imap and the disabled account never reached the client
    assert signals == [(SCOPE, ACC["account_id"], 1)]
    store = _store(data_dir)
    assert store.find_external("removed@gmail.example", "r1") is None            # gone from the config: detached
    assert store.find_external("old@gmail.example", "d1") is not None           # disabled, still configured: kept
    # a second sweep with nothing new signals nothing
    signals.clear()
    asyncio.run(go())
    assert signals == []


def test_request_sync_is_a_noop_without_a_supervisor_and_deduplicates_with_one(monkeypatch, data_dir):
    _config(monkeypatch)
    _install(monkeypatch, FakeClient())
    assert cs.request_sync(SCOPE) is False                                       # the CLI case: nothing to ask
    seen = []

    class _Recording(cs.CalendarSyncSupervisor):
        def sync_one(self, scope, cred_username, acc):
            seen.append((scope, acc["account_id"]))
            return {"ok": True, "changed": 0}

        async def run(self):
            self._loop = asyncio.get_running_loop()
            await asyncio.sleep(3600)

    async def go():
        assert ss.start_supervisor(_Recording())
        await asyncio.sleep(0)
        first = cs.request_sync(SCOPE)
        second = cs.request_sync(SCOPE)                                          # same account still pending
        other = cs.request_sync(OTHER)                                           # a scope without a calendar account
        for _ in range(100):
            if not ss.running_supervisor("calendar")._pending:
                break
            await asyncio.sleep(0.02)
        ss._tasks["calendar"].cancel()
        return first, second, other

    assert asyncio.run(go()) == (True, False, False)
    assert seen == [(SCOPE, ACC["account_id"])]


# ── the default account, the write-through follow-up, the manual sync, the lock ───

def test_default_account_prefers_the_push_target_then_the_first(monkeypatch, data_dir):
    _config(monkeypatch, accounts=[ACC, {"account_id": "work@outlook.example", "provider": "microsoft"},
                                   {"account_id": "m@x", "provider": "imap"}])
    store = _store(data_dir)
    assert cs.default_account_for(SCOPE, store=store)["account_id"] == ACC["account_id"]
    store.set_settings(push_target="work@outlook.example")
    assert cs.default_account_for(SCOPE, store=store)["account_id"] == "work@outlook.example"
    assert cs.default_account_for(SCOPE, ACC["account_id"], store)["account_id"] == ACC["account_id"]
    assert cs.default_account_for(SCOPE, "nobody@x", store) is None                 # named but not connected
    assert cs.default_account_for(SCOPE, "m@x", store) is None                      # not a calendar account
    assert cs.default_account_for(OTHER, store=store) is None


def test_after_local_change_signals_the_browser_and_asks_for_the_push(monkeypatch, data_dir):
    _config(monkeypatch)
    signals, pushes = [], []
    monkeypatch.setattr(cs, "_signal", lambda scope: signals.append(scope))
    monkeypatch.setattr(cs, "request_sync", lambda scope, account_id=None: pushes.append((scope, account_id)) or True)
    assert cs.after_local_change(SCOPE) is False and signals == [SCOPE] and pushes == []
    assert cs.after_local_change(SCOPE, ACC["account_id"]) is True and pushes == [(SCOPE, ACC["account_id"])]


def test_the_signal_is_the_web_interfaces_calendar_notifier(monkeypatch):
    import vaf.core.web_interface as wi
    seen = []
    monkeypatch.setattr(wi, "notify_calendar_changed", lambda scope: seen.append(scope))
    cs._signal(SCOPE)
    assert seen == [SCOPE]
    assert callable(wi.notify_user_signal) and callable(wi.notify_rooms_changed)     # rooms ride the same primitive


def test_sync_scope_now_runs_every_account_of_the_scope_and_signals_changes(monkeypatch, data_dir):
    _config(monkeypatch, accounts=[ACC, {"account_id": "work@outlook.example", "provider": "microsoft", "enabled": True}])
    _install(monkeypatch, FakeClient([_ext("ext1", NOW + DAY)]))
    signals = []
    monkeypatch.setattr(cs, "_signal", lambda scope: signals.append(scope))
    results = cs.sync_scope_now(SCOPE)
    assert [r["account"] for r in results] == [ACC["account_id"], "work@outlook.example"]
    assert all(r["ok"] for r in results) and signals == [SCOPE]
    signals.clear()
    results = cs.sync_scope_now(SCOPE, ACC["account_id"])                            # nothing new: no signal
    assert len(results) == 1 and results[0]["changed"] == 0 and signals == []
    assert cs.sync_scope_now(OTHER) == []


def test_sync_account_is_serialised_per_account(monkeypatch, data_dir):
    import threading
    import time
    _config(monkeypatch)
    _install(monkeypatch, FakeClient())
    running = {"now": 0, "max": 0}
    gate = threading.Lock()

    def slow_list(*a, **k):
        with gate:
            running["now"] += 1
            running["max"] = max(running["max"], running["now"])
        time.sleep(0.05)
        with gate:
            running["now"] -= 1
        return []

    monkeypatch.setattr(cs.cc, "list_events", slow_list)
    threads = [threading.Thread(target=cs.sync_account, args=(SCOPE, None, ACC)) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert running["max"] == 1, "two syncs of one account ran at once: a pending row could be pushed twice"
