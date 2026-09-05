# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The calendar reminder tick (calendar_sync.fire_due_calendar_reminders): the same narrow
lane as the one-shot reminders - stored data, a text composed deterministically in the
user's language, the main messenger plus the Web UI bell, no agent run. Fires once, respects
the grace window, stays silent for cancelled and deleted events, and rides the scheduler
loop between the two calls that were already pinned there."""
from pathlib import Path

import pytest

import vaf.core.calendar_sync as cs
import vaf.core.user_time as ut
from vaf.core import calendar_store as cal
from vaf.core.platform import Platform

ROOT = Path(__file__).resolve().parents[1]
SCOPE = "11111111-2222-3333-4444-555555555555"
NOW = 1_772_366_400.0          # 2026-03-01 12:00:00 UTC
MIN = 60.0
BERLIN = {"timezone": "Europe/Berlin", "date_format": "dd.mm.yyyy", "time_format": "24h"}


@pytest.fixture
def lane(monkeypatch, tmp_path):
    d = tmp_path / "data"
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: d))
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path / "vaf"))
    monkeypatch.setattr(cs, "_username_for", lambda scope, cred: "alice")
    monkeypatch.setattr(cs, "_language", lambda scope, username: "de")
    monkeypatch.setattr(ut, "_load_identity", lambda username=None, identity=None: identity or BERLIN)
    sent, notes = [], []
    import vaf.core.messaging_connections as mc
    import vaf.core.user_notifications as un
    monkeypatch.setattr(mc, "send_to_main_messenger",
                        lambda scope, username, text, *a, **k: (sent.append((scope, username, text)) or True, "telegram"))
    monkeypatch.setattr(un, "append_notification",
                        lambda scope, kind, title, status="success", summary=None, **k:
                        notes.append({"scope": scope, "kind": kind, "title": title, "status": status, "summary": summary}))
    contacts = {"c1": {"id": "c1", "name": "Lena Beispiel"}, "c2": {"id": "c2", "name": "Tom"}}
    import vaf.core.contacts_store as contacts_store
    monkeypatch.setattr(contacts_store, "get_contact_by_id",
                        lambda cid, username=None, user_scope_id=None: contacts.get(cid))
    return {"data_dir": d, "sent": sent, "notes": notes}


def _store(lane):
    return cal.CalendarStore(SCOPE, base_dir=lane["data_dir"])


# ── composition ──────────────────────────────────────────────────────────────────

def test_reminder_text_is_composed_in_the_users_language_with_place_and_people(lane):
    ev = {"title": "Zahnarzt", "start_ts": NOW + 30 * MIN, "tz": "Europe/Berlin", "location": "Hauptstr. 1",
          "contact_ids": ["c1", "c2"], "all_day": False}
    assert cs.compose_reminder_text(ev, username="alice", scope=SCOPE) == \
        "Erinnerung: Zahnarzt, 01.03.2026 13:30, Hauptstr. 1, mit Lena Beispiel und Tom"
    assert cs.compose_reminder_text(ev, username="alice", scope=SCOPE, language="en") == \
        "Reminder: Zahnarzt, 01.03.2026 13:30, Hauptstr. 1, with Lena Beispiel and Tom"
    bare = {"title": "Standup", "start_ts": NOW, "tz": None, "location": "", "contact_ids": [], "all_day": False}
    assert cs.compose_reminder_text(bare, username="alice", scope=SCOPE) == "Erinnerung: Standup, 01.03.2026 13:00"
    all_day = {"title": "Urlaub", "start_ts": NOW, "tz": "Europe/Berlin", "location": "", "contact_ids": [],
               "all_day": True}
    assert cs.compose_reminder_text(all_day, username="alice", scope=SCOPE) == "Erinnerung: Urlaub, 01.03.2026 (ganztägig)"
    assert cs.compose_reminder_text(all_day, username="alice", scope=SCOPE, language="en") == "Reminder: Urlaub, 01.03.2026 (all day)"


def test_time_formatting_helpers_drop_seconds_and_give_the_date_half(lane):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    dt = datetime(2026, 3, 1, 13, 30, 15, tzinfo=ZoneInfo("Europe/Berlin"))
    assert ut.format_user_datetime(dt, identity=BERLIN) == "01.03.2026 13:30:15"
    assert ut.format_user_datetime(dt, identity=BERLIN, seconds=False) == "01.03.2026 13:30"
    assert ut.format_user_date(dt, identity=BERLIN) == "01.03.2026"
    twelve = {**BERLIN, "time_format": "12h"}
    assert ut.format_user_datetime(dt, identity=twelve, seconds=False) == "01.03.2026 01:30 PM"
    assert ut.format_user_date(dt, identity={"timezone": "UTC"}, language="en") == "2026-03-01"


# ── firing ───────────────────────────────────────────────────────────────────────

def test_due_reminder_fires_once_on_the_main_channel_and_the_bell(lane):
    store = _store(lane)
    ev = store.add_event(title="Zahnarzt", start_ts=NOW + 10 * MIN, tz="Europe/Berlin", reminder_minutes=15,
                         contact_ids=["c1"], location="Hauptstr. 1")
    later = store.add_event(title="Later", start_ts=NOW + 2 * 3600, reminder_minutes=15)
    store.close()
    assert cs.fire_due_calendar_reminders(now_ts=NOW) == 1
    assert lane["sent"] == [(SCOPE, "alice", "Erinnerung: Zahnarzt, 01.03.2026 13:10, Hauptstr. 1, mit Lena Beispiel")]
    assert len(lane["notes"]) == 1 and lane["notes"][0]["kind"] == "automation" and lane["notes"][0]["status"] == "success"
    assert lane["notes"][0]["title"].startswith("Erinnerung: Zahnarzt")
    store = _store(lane)
    assert store.get_event(ev["id"])["reminder_fired_at"] == NOW
    assert store.get_event(later["id"])["reminder_fired_at"] is None
    store.close()
    # the next tick delivers nothing again
    assert cs.fire_due_calendar_reminders(now_ts=NOW + MIN) == 0
    assert len(lane["sent"]) == 1


def test_messenger_failure_still_leaves_the_bell_notification(lane, monkeypatch):
    import vaf.core.messaging_connections as mc
    monkeypatch.setattr(mc, "send_to_main_messenger", lambda *a, **k: (False, None))
    store = _store(lane)
    store.add_event(title="Nur Bell", start_ts=NOW + 5 * MIN, reminder_minutes=15)
    store.close()
    assert cs.fire_due_calendar_reminders(now_ts=NOW) == 1
    assert lane["sent"] == [] and len(lane["notes"]) == 1 and lane["notes"][0]["title"].startswith("Erinnerung: Nur Bell")


def test_past_the_grace_the_reminder_is_marked_missed_with_an_honest_note(lane):
    store = _store(lane)
    stale = store.add_event(title="Verpasst", start_ts=NOW - 8 * 3600, reminder_minutes=15)   # eight hours ago
    late = store.add_event(title="Spaet", start_ts=NOW - 3600, reminder_minutes=15)            # one hour ago: within grace
    store.close()
    assert cs.fire_due_calendar_reminders(now_ts=NOW) == 2
    assert [t for _s, _u, t in lane["sent"]] == ["Erinnerung: Spaet, 01.03.2026 12:00"]
    missed = [n for n in lane["notes"] if n["status"] == "error"]
    assert len(missed) == 1 and missed[0]["title"] == "Verpasste Erinnerung (Backend war offline)"
    assert missed[0]["summary"].startswith("Erinnerung: Verpasst")
    store = _store(lane)
    assert store.get_event(stale["id"])["reminder_missed_at"] == NOW and store.get_event(stale["id"])["reminder_fired_at"] is None
    assert store.get_event(late["id"])["reminder_fired_at"] == NOW
    store.close()
    assert cs.fire_due_calendar_reminders(now_ts=NOW + MIN) == 0


def test_cancelled_deleted_and_reminderless_events_stay_silent(lane):
    store = _store(lane)
    cancelled = store.add_event(title="Abgesagt", start_ts=NOW + 5 * MIN, reminder_minutes=15)
    store.update_event(cancelled["id"], status="cancelled")
    mirrored = store.add_event(title="Geloescht", start_ts=NOW + 5 * MIN, reminder_minutes=15,
                               account_id="a@gmail.example", external_id="ext1", source="gmail")
    store.delete_event(mirrored["id"])                                    # pending_delete until the provider confirms
    store.add_event(title="Ohne", start_ts=NOW + 5 * MIN, reminder_minutes=0)
    store.close()
    assert cs.fire_due_calendar_reminders(now_ts=NOW) == 0
    assert lane["sent"] == [] and lane["notes"] == []


def test_no_calendar_on_disk_means_no_work_and_no_file(lane):
    assert cs.fire_due_calendar_reminders(now_ts=NOW) == 0
    assert not (lane["data_dir"] / "scopes").exists()
    assert cal.CalendarStore.scopes_with_store(lane["data_dir"]) == []


def test_scopes_with_store_lists_only_real_calendars(lane):
    _store(lane).close()
    other = "66666666-7777-8888-9999-000000000000"
    (lane["data_dir"] / "scopes" / other).mkdir(parents=True)              # a scope directory without a calendar
    assert cal.CalendarStore.scopes_with_store(lane["data_dir"]) == [SCOPE]


# ── the scheduler loop ───────────────────────────────────────────────────────────

def test_scheduler_loop_fires_calendar_reminders_between_the_pinned_calls():
    source = (ROOT / "vaf" / "core" / "automation.py").read_text(encoding="utf-8")
    loop = source.split("def scheduler_loop():", 1)[1].split("time.sleep(30)", 1)[0]
    a = loop.index("fire_due_reminders()")
    b = loop.index("fire_due_calendar_reminders()")
    c = loop.index("self._fire_room_triggers()")
    assert a < b < c, "the calendar tick must ride the same tick as the one-shot reminders"


def test_the_four_sync_keys_are_registered_admin_only_and_documented():
    from vaf.core.config import Config
    doc = (ROOT / "docs" / "setup" / "CONFIG_SCHEMA.md").read_text(encoding="utf-8")
    expected = {"calendar_sync_interval_minutes": 5, "calendar_sync_past_days": 30,
                "calendar_sync_future_days": 365, "calendar_sync_push_enabled": True}
    for key, default in expected.items():
        assert Config.DEFAULTS[key] == default
        assert Config.is_global_config_key(key), f"{key} must be admin-only: it is instance policy"
        assert f"| `{key}` |" in doc
