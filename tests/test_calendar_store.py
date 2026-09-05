# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The VAF calendar store: one SQLite file per scope, fail-closed, versioned by schema_meta,
owner-only; events as UTC instants with their zone, all-day events by date; mirrored events
folded in with the newer change winning; pending pushes and deletions; contact links; the
one-time move of the contact-book events; reminders due; and a glance that creates nothing."""
import os
import sqlite3
import sys

import pytest

from vaf.core import calendar_store as cal
from vaf.core.platform import Platform

SCOPE_A = "11111111-2222-3333-4444-555555555555"
SCOPE_B = "66666666-7777-8888-9999-000000000000"
BERLIN = "Europe/Berlin"
T0 = 1_772_366_400.0          # 2026-03-01 12:00:00 UTC


@pytest.fixture
def data_dir(monkeypatch, tmp_path):
    d = tmp_path / "data"
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: d))
    return d


def _store(data_dir, scope=SCOPE_A):
    return cal.CalendarStore(scope, base_dir=data_dir)


# ── file, schema, isolation ──────────────────────────────────────────────────────

def test_store_is_fail_closed_versioned_and_owner_only(data_dir):
    with pytest.raises(ValueError):
        cal.CalendarStore("")
    assert cal.CalendarStore.exists(SCOPE_A, base_dir=data_dir) is False
    s = _store(data_dir)
    assert s.db_path == data_dir / "scopes" / SCOPE_A / "calendar.db" and s.db_path.exists()
    assert cal.CalendarStore.exists(SCOPE_A, base_dir=data_dir) is True
    assert s._meta("schema_version") == str(cal.SCHEMA_VERSION) and s._meta("created_at")
    if sys.platform != "win32":
        assert oct(os.stat(s.db_path).st_mode & 0o777) == "0o600"
        assert oct(os.stat(s.db_path.parent).st_mode & 0o777) == "0o700"
    s.close()
    # A file from a newer build is refused instead of being misread.
    conn = sqlite3.connect(s.db_path)
    conn.execute("UPDATE schema_meta SET value='99' WHERE key='schema_version'")
    conn.commit(); conn.close()
    with pytest.raises(RuntimeError):
        _store(data_dir)


def test_two_scopes_are_two_files(data_dir):
    a, b = _store(data_dir), _store(data_dir, SCOPE_B)
    a.add_event(title="A only", start_ts=T0)
    assert a.list_events(T0 - 10, T0 + 10)[0]["title"] == "A only"
    assert b.list_events(T0 - 10, T0 + 10) == []
    assert a.db_path != b.db_path


# ── events ───────────────────────────────────────────────────────────────────────

def test_add_update_delete_and_ranges(data_dir):
    s = _store(data_dir)
    ev = s.add_event(title="  Standup ", start_ts=T0, tz=BERLIN, description="agenda", location="Room 1",
                     contact_ids=["c1"], created_by="agent", reminder_minutes=10)
    assert ev["title"] == "Standup" and ev["end_ts"] == T0 + 3600 and ev["tz"] == BERLIN
    assert ev["contact_ids"] == ["c1"] and ev["created_by"] == "agent" and ev["sync_state"] == "local_only"
    assert ev["reminder_minutes"] == 10 and ev["reminder_at"] == T0 - 600 and ev["source"] == "vaf"
    # default reminder comes from the settings; 0 means none
    assert s.add_event(title="Default", start_ts=T0 + 7200)["reminder_minutes"] == cal.DEFAULT_REMINDER_MINUTES
    s.set_settings(default_reminder_minutes=0)
    assert s.add_event(title="None", start_ts=T0 + 9000)["reminder_minutes"] is None
    # overlap query, oldest first, cancelled hidden
    assert [e["title"] for e in s.list_events(T0 - 1, T0 + 9001)] == ["Standup", "Default", "None"]
    assert [e["title"] for e in s.list_events(T0 + 3600, T0 + 7201)] == ["Default"]        # end is exclusive
    up = s.update_event(ev["id"], title="Standup moved", start_ts=T0 + 60, end_ts=T0 + 1860, reminder_minutes=5, status="cancelled")
    assert up["title"] == "Standup moved" and up["start_ts"] == T0 + 60 and up["end_ts"] == T0 + 1860 and up["reminder_minutes"] == 5
    assert [e["title"] for e in s.list_events(T0 - 1, T0 + 9001)] == ["Default", "None"]
    assert [e["title"] for e in s.list_events(T0 - 1, T0 + 9001, include_cancelled=True)][0] == "Standup moved"
    assert s.update_event("missing", title="x") is None
    gone = s.delete_event(up["id"])
    assert gone["id"] == up["id"] and s.get_event(up["id"]) is None            # a local event is removed outright
    assert s.delete_event(up["id"]) is None


def test_all_day_events_sit_at_midnight_in_their_zone(data_dir):
    s = _store(data_dir)
    ev = s.add_event(title="Holiday", start_ts=T0, all_day=True, tz=BERLIN, start_date="2026-03-05", end_date="2026-03-06")
    assert ev["all_day"] is True and ev["start_date"] == "2026-03-05" and ev["end_date"] == "2026-03-06"
    assert ev["start_ts"] == cal._midnight_ts("2026-03-05", BERLIN) and ev["end_ts"] == cal._midnight_ts("2026-03-06", BERLIN)
    # a date-only event from a timestamp: the date is read in the zone, the end is the next day
    ev2 = s.add_event(title="Day from ts", start_ts=cal._midnight_ts("2026-03-07", BERLIN) + 5, all_day=True, tz=BERLIN)
    assert (ev2["start_date"], ev2["end_date"]) == ("2026-03-07", "2026-03-08")
    assert [e["title"] for e in s.list_events(cal._midnight_ts("2026-03-05", BERLIN), cal._midnight_ts("2026-03-06", BERLIN))] == ["Holiday"]


def test_contact_links_and_search(data_dir):
    s = _store(data_dir)
    s.add_event(title="Call with Bob", start_ts=T0, contact_ids=["bob"])
    s.add_event(title="Team lunch", start_ts=T0 + 3600, description="ask bob@example.com about it")
    s.add_event(title="Unrelated", start_ts=T0 + 7200)
    assert [e["title"] for e in s.events_for_contact("bob")] == ["Call with Bob"]
    assert [e["title"] for e in s.list_events(T0 - 1, T0 + 9000, contact_id="bob")] == ["Call with Bob"]
    assert s.events_for_contact("bo") == []                                              # ids match whole, not by prefix
    hits = s.search_events(["Bob", "bob@example.com"], T0 - 1, T0 + 9000)
    assert [e["title"] for e in hits] == ["Call with Bob", "Team lunch"]
    assert s.search_events([" "], T0 - 1, T0 + 9000) == []


# ── mirrored events ──────────────────────────────────────────────────────────────

def _ext(eid, title, start, end, *, updated, status="confirmed", all_day=False, tz="UTC", location=""):
    return {"id": eid, "summary": title, "description": "", "location": location, "start": start, "end": end,
            "all_day": all_day, "tz": tz, "status": status, "updated": updated, "recurring_event_id": None,
            "link": f"https://cal.example/{eid}", "etag": f"e-{updated}"}


def test_upsert_external_creates_updates_cancels_and_keeps_newer_local_edits(data_dir):
    s = _store(data_dir)
    acc = "me@example.com"
    ev = _ext("g1", "Standup", "2026-03-02T09:00:00+01:00", "2026-03-02T09:30:00+01:00", updated=T0)
    assert s.upsert_external(acc, ev, source="gmail") == "created"
    row = s.find_external(acc, "g1")
    assert row["sync_state"] == "synced" and row["source"] == "gmail" and row["reminder_minutes"] is None
    assert row["start_ts"] == cal._iso_to_ts("2026-03-02T09:00:00+01:00", None) and row["link"] == "https://cal.example/g1"
    assert s.upsert_external(acc, ev, source="gmail") == "unchanged"
    newer = _ext("g1", "Standup (moved)", "2026-03-02T10:00:00+01:00", "2026-03-02T10:30:00+01:00", updated=T0 + 100)
    assert s.upsert_external(acc, newer, source="gmail") == "updated"
    assert s.find_external(acc, "g1")["title"] == "Standup (moved)"
    # a local edit waiting to be pushed is newer than the provider's copy: it stays, the push wins
    s.update_event(row["id"], title="Local edit")
    assert s.get_event(row["id"])["sync_state"] == "pending_push"
    older_remote = _ext("g1", "Remote title", "2026-03-02T10:00:00+01:00", "2026-03-02T10:30:00+01:00", updated=T0 + 100)
    assert s.upsert_external(acc, older_remote, source="gmail") == "kept_local"
    assert s.get_event(row["id"])["title"] == "Local edit"
    # ...unless the provider's change is newer than the local one
    much_newer = _ext("g1", "Remote wins", "2026-03-02T10:00:00+01:00", "2026-03-02T10:30:00+01:00", updated=cal._now() + 3600)
    assert s.upsert_external(acc, much_newer, source="gmail") == "updated"
    assert s.get_event(row["id"])["title"] == "Remote wins" and s.get_event(row["id"])["sync_state"] == "synced"
    # a cancelled instance folds in as cancelled; a cancelled unknown one is ignored
    assert s.upsert_external(acc, _ext("g1", "x", "2026-03-02T10:00:00Z", "2026-03-02T10:30:00Z", updated=cal._now() + 7200, status="cancelled"), source="gmail") == "cancelled"
    assert s.get_event(row["id"])["status"] == "cancelled"
    assert s.upsert_external(acc, _ext("never", "x", "2026-03-02T10:00:00Z", "2026-03-02T10:30:00Z", updated=T0, status="cancelled"), source="gmail") == "unchanged"
    # all-day instance
    assert s.upsert_external(acc, _ext("g2", "Holiday", "2026-03-05", "2026-03-06", updated=T0, all_day=True, tz=BERLIN), source="gmail") == "created"
    g2 = s.find_external(acc, "g2")
    assert g2["all_day"] and g2["start_date"] == "2026-03-05" and g2["start_ts"] == cal._midnight_ts("2026-03-05", BERLIN)


def test_mark_missing_external_removes_only_synced_rows_inside_the_window(data_dir):
    s = _store(data_dir)
    acc = "me@example.com"
    s.upsert_external(acc, _ext("keep", "Keep", "2026-03-02T09:00:00Z", "2026-03-02T10:00:00Z", updated=T0), source="gmail")
    s.upsert_external(acc, _ext("gone", "Gone", "2026-03-03T09:00:00Z", "2026-03-03T10:00:00Z", updated=T0), source="gmail")
    s.upsert_external(acc, _ext("outside", "Outside", "2026-04-03T09:00:00Z", "2026-04-03T10:00:00Z", updated=T0), source="gmail")
    edited = s.find_external(acc, "gone")
    s.upsert_external(acc, _ext("edited", "Edited", "2026-03-04T09:00:00Z", "2026-03-04T10:00:00Z", updated=T0), source="gmail")
    s.update_event(s.find_external(acc, "edited")["id"], title="Edited locally")           # pending_push: the push decides
    window = (cal._iso_to_ts("2026-03-01T00:00:00Z", None), cal._iso_to_ts("2026-03-31T00:00:00Z", None))
    removed = s.mark_missing_external(acc, ["keep"], *window)
    assert removed == 1 and s.get_event(edited["id"]) is None
    assert s.find_external(acc, "keep") and s.find_external(acc, "outside") and s.find_external(acc, "edited")


def test_pending_pushes_and_their_outcomes(data_dir):
    s = _store(data_dir)
    acc = "me@example.com"
    ev = s.add_event(title="New", start_ts=T0, account_id=acc)
    assert ev["sync_state"] == "pending_push"
    assert [e["id"] for e in s.pending_pushes(acc)] == [ev["id"]] and s.pending_pushes("other@example.com") == []
    s.mark_pushed(ev["id"], external_id="ext-1", external_updated=T0 + 1, etag="e", link="https://cal.example/ext-1")
    pushed = s.get_event(ev["id"])
    assert pushed["sync_state"] == "synced" and pushed["external_id"] == "ext-1" and pushed["link"] == "https://cal.example/ext-1"
    # an edit owes a push again; a delete of a mirrored event is a pending delete, purged after the push
    s.update_event(ev["id"], location="Cafe")
    assert s.get_event(ev["id"])["sync_state"] == "pending_push"
    for _ in range(cal.PUSH_MAX_ATTEMPTS - 1):
        assert s.mark_push_failed(ev["id"], "boom") == "pending_push"
    assert s.mark_push_failed(ev["id"], "boom") == "push_failed"
    assert s.get_event(ev["id"])["last_error"] == "boom" and s.pending_pushes(acc) == []
    s.update_event(ev["id"], title="Try again")                                           # a new edit re-arms the push
    assert s.get_event(ev["id"])["sync_state"] == "pending_push"
    s.delete_event(ev["id"])
    assert s.get_event(ev["id"]) is None                                          # gone for readers
    assert s.get_event(ev["id"], include_pending_delete=True)["sync_state"] == "pending_delete"
    assert s.list_events(T0 - 1, T0 + 9000) == [] and [e["id"] for e in s.pending_pushes(acc)] == [ev["id"]]
    s.purge_event(ev["id"])
    assert s.get_event(ev["id"]) is None


def test_detach_account_keeps_events_read_only_and_clears_the_target(data_dir):
    s = _store(data_dir)
    acc = "me@example.com"
    s.set_settings(push_target=acc)
    s.upsert_external(acc, _ext("g1", "Mirrored", "2026-03-02T09:00:00Z", "2026-03-02T10:00:00Z", updated=T0), source="gmail")
    mirrored = s.find_external(acc, "g1")
    s.upsert_external(acc, _ext("g2", "To delete", "2026-03-03T09:00:00Z", "2026-03-03T10:00:00Z", updated=T0), source="gmail")
    s.delete_event(s.find_external(acc, "g2")["id"])
    s.mark_account_synced(acc, ts=T0)
    assert s.account_state(acc)["last_sync_at"] == T0
    kept = s.detach_account(acc)
    assert kept == 1
    row = s.get_event(mirrored["id"])
    assert row["account_id"] is None and row["external_id"] is None and row["sync_state"] == "local_only" and row["source"] == "gmail"
    assert s.find_external(acc, "g2") is None and s.settings()["push_target"] is None
    assert s.list_account_states() == []


def test_account_state_and_settings(data_dir):
    s = _store(data_dir)
    assert s.settings() == {"push_target": None, "default_reminder_minutes": cal.DEFAULT_REMINDER_MINUTES}
    assert s.set_settings(push_target="me@example.com", default_reminder_minutes="30") == {"push_target": "me@example.com", "default_reminder_minutes": 30}
    assert s.account_state("x")["enabled"] is True and s.account_state("x")["needs_reconsent"] is False
    s.set_account_enabled("x", False)
    s.mark_account_synced("x", error="401 unauthorized", needs_reconsent=True, sync_state={"token": "abc"})
    st = s.account_state("x")
    assert st["enabled"] is False and st["needs_reconsent"] and st["last_error"] == "401 unauthorized" and st["sync_state"] == {"token": "abc"}
    assert st["last_sync_at"] is None                                                       # an error is not a sync
    s.mark_account_synced("x", ts=T0, needs_reconsent=False)
    assert s.account_state("x")["last_sync_at"] == T0 and s.account_state("x")["last_error"] is None


# ── reminders ────────────────────────────────────────────────────────────────────

def test_due_reminders_and_marks(data_dir):
    s = _store(data_dir)
    soon = s.add_event(title="Soon", start_ts=T0 + 600, reminder_minutes=15)                # due at T0 - 300
    later = s.add_event(title="Later", start_ts=T0 + 7200, reminder_minutes=15)             # due at T0 + 6300
    s.add_event(title="Silent", start_ts=T0 + 60, reminder_minutes=None)
    cancelled = s.add_event(title="Cancelled", start_ts=T0 + 60, reminder_minutes=5)
    s.update_event(cancelled["id"], status="cancelled")
    assert [e["id"] for e in s.due_reminders(T0)] == [soon["id"]]
    s.mark_reminder(soon["id"], "fired", ts=T0)
    assert s.due_reminders(T0) == [] and s.get_event(soon["id"])["reminder_fired_at"] == T0
    assert [e["id"] for e in s.due_reminders(T0 + 7000)] == [later["id"]]
    s.mark_reminder(later["id"], "missed", ts=T0 + 7000)
    assert s.due_reminders(T0 + 7000) == [] and s.get_event(later["id"])["reminder_missed_at"] == T0 + 7000
    # moving an event arms its reminder again
    s.update_event(soon["id"], start_ts=T0 + 100_000)
    assert s.get_event(soon["id"])["reminder_fired_at"] is None


# ── the contact-book migration ───────────────────────────────────────────────────

def _seed_legacy_event(username, scope, contact_id, title, when_ts, *, note=None, source="user", event_id="legacy-1"):
    """A record the way the contact book wrote it before the calendar existed."""
    from vaf.core import contacts_store as cs
    contacts = cs._load_all(username, scope)
    for c in contacts:
        if c["id"] == contact_id:
            c.setdefault("events", []).append({"id": event_id, "ts": 1.0, "when_ts": when_ts, "title": title,
                                               "source": source, "note": note})
    cs._save_all(contacts, username, scope)
    return {"id": event_id}


def test_contact_events_move_into_the_calendar_once(data_dir):
    from vaf.core import contacts_store as cs
    c = cs.create_contact("Dana New", "alice", user_scope_id=SCOPE_A)
    ev = _seed_legacy_event("alice", SCOPE_A, c["id"], "Kickoff", T0 + 86400, note="bring the deck", source="agent")
    assert len(cs.get_contact_by_id(c["id"], "alice", user_scope_id=SCOPE_A)["events"]) == 1
    assert cs.contact_events(cs.get_contact_by_id(c["id"], "alice", user_scope_id=SCOPE_A), "alice", SCOPE_A)[0]["id"] == ev["id"]  # readable before the move
    s = _store(data_dir)
    moved = s.events_for_contact(c["id"])
    assert len(moved) == 1 and moved[0]["title"] == "Kickoff" and moved[0]["description"] == "bring the deck"
    assert moved[0]["legacy_id"] == ev["id"] and moved[0]["created_by"] == "agent" and moved[0]["start_ts"] == T0 + 86400
    assert moved[0]["reminder_minutes"] is None and moved[0]["sync_state"] == "local_only"
    assert cs.get_contact_by_id(c["id"], "alice", user_scope_id=SCOPE_A)["events"] == []
    assert s._meta("contacts_migrated_at")
    s.close()
    again = _store(data_dir)
    assert len(again.events_for_contact(c["id"])) == 1                                     # idempotent


def test_migration_writes_nothing_when_there_is_nothing_to_move(data_dir):
    _store(data_dir, SCOPE_B)
    assert not (data_dir / "scopes" / SCOPE_B / "contacts.json").exists()
    assert _store(data_dir, SCOPE_B)._meta("contacts_migrated_at")


def test_the_local_admins_book_is_migrated_from_the_root_path(data_dir):
    from vaf.core import contacts_store as cs
    from vaf.core.config import get_local_admin_scope_id, get_local_admin_username
    admin_user, admin_scope = get_local_admin_username(), get_local_admin_scope_id()
    c = cs.create_contact("Admin Friend", admin_user, user_scope_id=admin_scope)
    _seed_legacy_event(admin_user, admin_scope, c["id"], "Admin meeting", T0 + 3600)
    assert (data_dir / "contacts.json").exists()
    s = _store(data_dir, admin_scope)
    assert [e["title"] for e in s.events_for_contact(c["id"])] == ["Admin meeting"]
    assert cs.get_contact_by_id(c["id"], admin_user, user_scope_id=admin_scope)["events"] == []


def test_scope_helpers(monkeypatch, data_dir):
    from vaf.core.config import get_local_admin_scope_id
    assert cal.scope_for("alice", SCOPE_A) == SCOPE_A
    assert cal.scope_for("alice", None) == get_local_admin_scope_id()
    assert cal.store_exists("alice", SCOPE_A) is False
    cal.store_for("alice", SCOPE_A).close()
    assert cal.store_exists("alice", SCOPE_A) is True
    assert cal.push_enabled() is True


def test_a_row_waiting_for_its_deletion_is_gone_for_readers(data_dir):
    s = _store(data_dir)
    ev = s.add_event(title="Mirrored", start_ts=T0, account_id="a@gmail.example", external_id="ext1", sync_state="synced")
    assert s.delete_event(ev["id"])["id"] == ev["id"]
    assert s.get_event(ev["id"]) is None                                          # readers: gone
    assert s.get_event(ev["id"], include_pending_delete=True)["sync_state"] == "pending_delete"   # the sync: still owed
    assert s.delete_event(ev["id"]) is None and s.update_event(ev["id"], title="x") is None
    assert s.mark_push_failed(ev["id"], "boom") == "pending_delete"               # counted, not lost
    assert s.count_events() == 0 and s.count_events(include_cancelled=True) == 0
    s.add_event(title="Live", start_ts=T0)
    assert s.count_events() == 1


def test_a_contact_event_added_after_the_move_is_a_calendar_event(data_dir, monkeypatch):
    """The wrapper: add_contact_event writes the calendar (creating it on first use, which
    moves the book's legacy events), links the contact, takes the user's default reminder,
    and asks for the mirror push; delete_contact_event removes it again."""
    import vaf.core.calendar_sync as sync
    from vaf.core import contacts_store as cs
    pushes = []
    monkeypatch.setattr(sync, "after_local_change", lambda scope, account_id=None: pushes.append((scope, account_id)) or False)
    c = cs.create_contact("Dana New", "alice", user_scope_id=SCOPE_A)
    _seed_legacy_event("alice", SCOPE_A, c["id"], "Old", T0 - 86400)
    ev = cs.add_contact_event(c["id"], "Kickoff", T0 + 86400, "alice", user_scope_id=SCOPE_A, note="bring the deck", source="agent")
    assert ev and ev["when_ts"] == T0 + 86400 and ev["source"] == "agent" and ev["note"] == "bring the deck"
    assert ev["reminder_minutes"] == cal.DEFAULT_REMINDER_MINUTES and ev["account_id"] is None      # no account configured here
    assert pushes == [(SCOPE_A, None)]
    s = _store(data_dir)
    assert [e["title"] for e in s.events_for_contact(c["id"])] == ["Old", "Kickoff"]                # the legacy one moved first
    assert cs.get_contact_by_id(c["id"], "alice", user_scope_id=SCOPE_A)["events"] == []
    assert cs.add_contact_event(c["id"], "x", T0, "bob", user_scope_id=SCOPE_B) is None             # not bob's contact
    assert cs.delete_contact_event(c["id"], ev["id"], "alice", user_scope_id=SCOPE_A) is True
    assert cs.delete_contact_event(c["id"], ev["id"], "alice", user_scope_id=SCOPE_A) is False
    assert [e["title"] for e in s.events_for_contact(c["id"])] == ["Old"]
    # an event of the same calendar that is not this contact's cannot be removed through the contact
    other = s.add_event(title="Not linked", start_ts=T0)
    assert cs.delete_contact_event(c["id"], other["id"], "alice", user_scope_id=SCOPE_A) is False
    assert s.get_event(other["id"]) is not None
