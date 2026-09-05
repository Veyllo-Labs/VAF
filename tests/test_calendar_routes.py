# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The calendar routes on the store: status with sync state and settings, events in a
range read in the user's zone, create/patch/delete with the write-through follow-up, the
manual sync, the settings, and the daily-check automation that internal-only users get
too. Every route is scoped: another scope's event id is not found."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import vaf.api.calendar_routes as cr
import vaf.core.calendar_sync as cs
import vaf.core.config as cfg_mod
import vaf.core.user_time as ut
from vaf.core import calendar_store as cal
from vaf.core.platform import Platform

SCOPE = "11111111-2222-3333-4444-555555555555"
OTHER = "66666666-7777-8888-9999-000000000000"
ACC = {"account_id": "alice@gmail.example", "email": "alice@gmail.example", "provider": "gmail", "enabled": True}
BERLIN = {"timezone": "Europe/Berlin", "date_format": "dd.mm.yyyy", "time_format": "24h"}


@pytest.fixture
def lane(monkeypatch, tmp_path):
    d = tmp_path / "data"
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: d))
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path / "vaf"))
    state = {"accounts": [ACC], "pushes": [], "signals": []}
    values = {"email_config": {"accounts": state["accounts"]}, "email_config_by_scope": {},
              "local_admin_scope_id": SCOPE, "calendar_sync_push_enabled": True,
              "calendar_sync_interval_minutes": 5, "calendar_sync_past_days": 30, "calendar_sync_future_days": 365}
    monkeypatch.setattr(cfg_mod.Config, "get", staticmethod(lambda k, d=None: values.get(k, d)))
    monkeypatch.setattr(cr, "_get_email_config", lambda username, user_scope_id=None: {"accounts": state["accounts"]})
    monkeypatch.setattr(cs, "request_sync", lambda scope, account_id=None: state["pushes"].append((scope, account_id)) or True)
    monkeypatch.setattr(cs, "_signal", lambda scope: state["signals"].append(scope))
    monkeypatch.setattr(ut, "_load_identity", lambda username=None, identity=None: identity if identity is not None else BERLIN)
    state["values"] = values
    state["data_dir"] = d
    return state


def _client(user_scope=SCOPE):
    app = FastAPI()
    app.include_router(cr.router)
    app.dependency_overrides[cr._get_current_user] = lambda: {"username": "alice", "user_scope_id": user_scope}
    return TestClient(app)


def _store(lane, scope=SCOPE):
    return cal.CalendarStore(scope, base_dir=lane["data_dir"])


# ── status ───────────────────────────────────────────────────────────────────────

def test_status_lists_accounts_with_sync_state_and_the_settings(lane):
    c = _client()
    res = c.get("/api/calendar/status").json()
    assert res["google_available"] is True and res["microsoft_available"] is False
    assert res["has_calendar"] is True                                    # a connected account means a calendar
    assert res["accounts"] == [{"account_id": ACC["account_id"], "email": ACC["email"], "provider": "gmail",
                                "enabled": True, "last_sync_at": None, "last_error": None, "needs_reconsent": False}]
    assert res["settings"] == {"push_target": None, "default_reminder_minutes": cal.DEFAULT_REMINDER_MINUTES}
    assert res["sync"] == {"interval_minutes": 5, "push_enabled": True, "supervisor_running": False}
    store = _store(lane)
    store.mark_account_synced(ACC["account_id"], ts=1000.0)
    store.mark_account_synced("gone@gmail.example", error="x")           # a state row for an account no longer configured
    res = c.get("/api/calendar/status").json()
    assert res["accounts"][0]["last_sync_at"] == 1000.0 and len(res["accounts"]) == 1


def test_status_without_account_and_without_calendar_creates_nothing(lane):
    lane["accounts"].clear()
    res = _client().get("/api/calendar/status").json()
    assert res["has_calendar"] is False and res["accounts"] == [] and res["google_available"] is False
    assert not cal.CalendarStore.exists(SCOPE, base_dir=lane["data_dir"])


# ── events ───────────────────────────────────────────────────────────────────────

def test_create_reads_the_users_zone_mirrors_by_default_and_asks_for_the_push(lane):
    c = _client()
    res = c.post("/api/calendar/events", json={"title": "Dentist", "start": "2026-03-01T14:00", "end": "2026-03-01 15:00",
                                               "location": "Main St 1", "contact_ids": ["c1"], "reminder_minutes": 30})
    assert res.status_code == 200, res.text
    ev = res.json()["event"]
    assert ev["start"] == "2026-03-01T14:00:00+01:00" and ev["end"] == "2026-03-01T15:00:00+01:00"
    assert ev["start_ts"] == 1772370000.0 and ev["tz"] == "Europe/Berlin" and ev["all_day"] is False
    assert ev["account_id"] == ACC["account_id"] and ev["sync_state"] == "pending_push"
    assert ev["contact_ids"] == ["c1"] and ev["reminder_minutes"] == 30 and ev["created_by"] == "user"
    assert lane["pushes"] == [(SCOPE, ACC["account_id"])] and lane["signals"] == [SCOPE]
    # the visible range comes back with the same shape
    listed = c.get("/api/calendar/events", params={"time_min": "2026-03-01", "time_max": "2026-03-01"}).json()
    assert [e["id"] for e in listed["events"]] == [ev["id"]] and listed["account"] == ACC["account_id"]
    assert c.get("/api/calendar/events", params={"time_min": "2026-03-02", "time_max": "2026-03-03"}).json()["events"] == []
    assert c.get("/api/calendar/events", params={"contact_id": "c1", "time_min": "2026-03-01", "time_max": "2026-03-01"}).json()["events"][0]["id"] == ev["id"]
    assert c.get("/api/calendar/events", params={"contact_id": "c2", "time_min": "2026-03-01", "time_max": "2026-03-01"}).json()["events"] == []


def test_create_all_day_internal_and_the_error_cases(lane):
    c = _client()
    res = c.post("/api/calendar/events", json={"title": "Holiday", "start": "2026-03-10", "internal_only": True})
    ev = res.json()["event"]
    assert ev["all_day"] is True and ev["start"] == "2026-03-10" and ev["end"] == "2026-03-11"
    assert ev["account_id"] is None and ev["sync_state"] == "local_only"
    assert lane["pushes"] == [] and lane["signals"] == [SCOPE]              # told the browser, asked no push
    assert ev["reminder_minutes"] == cal.DEFAULT_REMINDER_MINUTES              # the user's default applies
    assert c.post("/api/calendar/events", json={"title": " ", "start": "2026-03-10"}).status_code == 400
    assert c.post("/api/calendar/events", json={"title": "x", "start": "next tuesday"}).status_code == 400
    assert c.post("/api/calendar/events", json={"title": "x", "start": "2026-03-10T10:00", "end": "2026-03-10T09:00"}).status_code == 400
    assert c.post("/api/calendar/events", json={"title": "x", "start": "2026-03-10", "account_id": "nobody@x"}).status_code == 400
    assert c.get("/api/calendar/events", params={"time_min": "2026-03-02", "time_max": "2026-02-27"}).status_code == 400
    assert c.get("/api/calendar/events", params={"time_min": "yesterday"}).status_code == 400


def test_the_push_target_decides_the_mirror_account(lane):
    lane["accounts"].append({"account_id": "work@outlook.example", "provider": "microsoft", "enabled": True})
    c = _client()
    _store(lane).set_settings(push_target="work@outlook.example")
    ev = c.post("/api/calendar/events", json={"title": "Standup", "start": "2026-03-02T09:00"}).json()["event"]
    assert ev["account_id"] == "work@outlook.example"
    ev2 = c.post("/api/calendar/events", json={"title": "Private", "start": "2026-03-02T19:00",
                                               "account_id": ACC["account_id"]}).json()["event"]
    assert ev2["account_id"] == ACC["account_id"]


def test_patch_moves_keep_the_duration_and_ids_are_scoped(lane):
    c = _client()
    ev = c.post("/api/calendar/events", json={"title": "Call", "start": "2026-03-01T14:00", "end": "2026-03-01T14:30",
                                              "internal_only": True}).json()["event"]
    res = c.patch(f"/api/calendar/events/{ev['id']}", json={"start": "2026-03-01T16:00", "title": "Call (moved)"})
    moved = res.json()["event"]
    assert moved["start"] == "2026-03-01T16:00:00+01:00" and moved["end"] == "2026-03-01T16:30:00+01:00"
    assert moved["title"] == "Call (moved)"
    assert c.patch(f"/api/calendar/events/{ev['id']}", json={"end": "2026-03-01T15:00"}).status_code == 400
    assert c.patch(f"/api/calendar/events/{ev['id']}", json={"title": ""}).status_code == 400
    assert c.patch(f"/api/calendar/events/{ev['id']}", json={"reminder_minutes": 0}).json()["event"]["reminder_minutes"] is None
    assert c.patch(f"/api/calendar/events/{ev['id']}", json={"all_day": True}).json()["event"]["all_day"] is True
    # another scope cannot see or change it
    other = _client(OTHER)
    assert other.patch(f"/api/calendar/events/{ev['id']}", json={"title": "x"}).status_code == 404
    assert other.delete(f"/api/calendar/events/{ev['id']}").status_code == 404
    assert other.get("/api/calendar/events", params={"time_min": "2026-03-01", "time_max": "2026-03-01"}).json()["events"] == []


def test_delete_purges_internal_and_marks_mirrored_pending(lane):
    c = _client()
    ev = c.post("/api/calendar/events", json={"title": "Gone", "start": "2026-03-01T14:00", "internal_only": True}).json()["event"]
    res = c.delete(f"/api/calendar/events/{ev['id']}").json()
    assert res == {"ok": True, "pending_delete": False}
    assert c.delete(f"/api/calendar/events/{ev['id']}").status_code == 404
    store = _store(lane)
    store.upsert_external(ACC["account_id"], {"id": "ext1", "summary": "Mirrored", "start": "2026-03-05T10:00:00Z",
                                              "end": "2026-03-05T11:00:00Z", "all_day": False, "tz": "UTC",
                                              "status": "confirmed", "updated": 1.0}, source="gmail")
    row = store.find_external(ACC["account_id"], "ext1")
    lane["pushes"].clear()
    res = c.delete(f"/api/calendar/events/{row['id']}").json()
    assert res == {"ok": True, "pending_delete": True}
    assert store.get_event(row["id"]) is None
    assert store.get_event(row["id"], include_pending_delete=True)["sync_state"] == "pending_delete"
    assert lane["pushes"] == [(SCOPE, ACC["account_id"])]
    assert c.get("/api/calendar/events", params={"time_min": "2026-03-05", "time_max": "2026-03-05"}).json()["events"] == []


# ── sync and settings ────────────────────────────────────────────────────────────

def test_sync_now_runs_the_scope_in_a_thread_and_reports(lane, monkeypatch):
    seen = []
    monkeypatch.setattr(cs, "sync_scope_now", lambda scope, account_id=None: seen.append((scope, account_id)) or
                        [{"ok": True, "account": ACC["account_id"], "changed": 2}])
    res = _client().post("/api/calendar/sync", params={"account_id": ACC["account_id"]}).json()
    assert res["changed"] == 2 and res["results"][0]["ok"] and seen == [(SCOPE, ACC["account_id"])]


def test_settings_route_validates_the_target_and_switches_accounts(lane):
    c = _client()
    assert c.put("/api/calendar/settings", json={"push_target": "nobody@x"}).status_code == 400
    res = c.put("/api/calendar/settings", json={"push_target": ACC["account_id"], "default_reminder_minutes": 60,
                                                "accounts": [{"account_id": ACC["account_id"], "enabled": False},
                                                             {"account_id": "nobody@x", "enabled": False}]}).json()
    assert res["settings"] == {"push_target": ACC["account_id"], "default_reminder_minutes": 60}
    assert res["accounts"][0]["enabled"] is False
    assert lane["signals"] == [SCOPE]
    res = c.put("/api/calendar/settings", json={"push_target": ""}).json()
    assert res["settings"]["push_target"] is None


# ── the daily check ──────────────────────────────────────────────────────────────

class _FakeManager:
    created = []

    def __init__(self, *a, **k):
        pass

    def list(self):
        return []

    def create(self, task):
        _FakeManager.created.append(task)
        return SimpleNamespace(id="task-1")


def test_daily_check_is_created_for_internal_only_calendars_too(lane, monkeypatch):
    import vaf.core.automation as auto_mod
    monkeypatch.setattr(auto_mod, "AutomationManager", _FakeManager)
    _FakeManager.created.clear()
    lane["accounts"].clear()
    c = _client()
    assert c.post("/api/calendar/ensure-daily-check-automation").json() == {"ok": False, "created": False, "reason": "no_calendar"}
    _store(lane).add_event(title="Internal", start_ts=1772370000.0)
    res = c.post("/api/calendar/ensure-daily-check-automation").json()
    assert res == {"ok": True, "created": True, "task_id": "task-1"}
    assert _FakeManager.created[0].name == cr.CALENDAR_DAILY_CHECK_NAME
    assert "schedule_reminder" in cr.DEFAULT_CALENDAR_CHECK_PROMPT and "delivered VERBATIM" in cr.DEFAULT_CALENDAR_CHECK_PROMPT
