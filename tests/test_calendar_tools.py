# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The four calendar tools on the store: times read and printed in the user's zone, a
bare date books an all-day event, a contact name links the event (ambiguity is asked, not
guessed), the mirror account is the push target unless the user names one or keeps the
event internal, a named account that is not connected is refused with the wording the
capability classifier knows, and edits and deletions ask for the push."""
import pytest

import vaf.core.calendar_sync as cs
import vaf.core.config as cfg_mod
import vaf.core.user_time as ut
from vaf.core import calendar_store as cal
from vaf.core.platform import Platform
from vaf.tools.calendar import (
    CreateCalendarEventTool,
    DeleteCalendarEventTool,
    ListCalendarEventsTool,
    UpdateCalendarEventTool,
)

SCOPE = "11111111-2222-3333-4444-555555555555"
ACC = {"account_id": "alice@gmail.example", "provider": "gmail", "enabled": True}
BERLIN = {"timezone": "Europe/Berlin", "date_format": "dd.mm.yyyy", "time_format": "24h"}
IDENT = {"username": "alice", "user_scope_id": SCOPE}


@pytest.fixture
def lane(monkeypatch, tmp_path):
    d = tmp_path / "data"
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: d))
    state = {"accounts": [ACC], "pushes": [], "signals": [], "data_dir": d}
    values = {"email_config": {"accounts": state["accounts"]}, "email_config_by_scope": {},
              "local_admin_scope_id": SCOPE, "calendar_sync_push_enabled": True}
    monkeypatch.setattr(cfg_mod.Config, "get", staticmethod(lambda k, d=None: values.get(k, d)))
    monkeypatch.setattr(cs, "request_sync", lambda scope, account_id=None: state["pushes"].append((scope, account_id)) or True)
    monkeypatch.setattr(cs, "_signal", lambda scope: state["signals"].append(scope))
    monkeypatch.setattr(ut, "_load_identity", lambda username=None, identity=None: identity if identity is not None else BERLIN)
    contacts = {"c1": {"id": "c1", "name": "Lena Beispiel"}, "c2": {"id": "c2", "name": "Lena Zwei"}, "c3": {"id": "c3", "name": "Tom"}}
    import vaf.core.contacts_store as contacts_store
    monkeypatch.setattr(contacts_store, "get_contact_by_id", lambda cid, username=None, user_scope_id=None: contacts.get(cid))
    monkeypatch.setattr(contacts_store, "get_contacts_by_name",
                        lambda name, username=None, user_scope_id=None:
                        [c for c in contacts.values() if name.lower() in c["name"].lower()])
    return state


def _store(lane):
    return cal.CalendarStore(SCOPE, base_dir=lane["data_dir"])


# ── create ───────────────────────────────────────────────────────────────────────

def test_create_books_in_the_users_zone_links_the_contact_and_mirrors(lane):
    out = CreateCalendarEventTool().run(summary="Dentist", start="2026-03-01T14:00", location="Main St 1",
                                        contact="Tom", reminder_minutes=30, **IDENT)
    assert out.startswith("Created event: Dentist (01.03.2026 14:00 - 15:00). Id: "), out
    assert "Reminder 30 minutes before." in out and f"Mirrored into {ACC['account_id']}" in out
    ev = _store(lane).list_events(0, 4e9)[0]
    assert ev["start_ts"] == 1772370000.0 and ev["end_ts"] == 1772373600.0 and ev["tz"] == "Europe/Berlin"
    assert ev["contact_ids"] == ["c3"] and ev["created_by"] == "agent" and ev["location"] == "Main St 1"
    assert ev["account_id"] == ACC["account_id"] and ev["sync_state"] == "pending_push"
    assert lane["pushes"] == [(SCOPE, ACC["account_id"])] and lane["signals"] == [SCOPE]


def test_create_all_day_internal_and_reminder_off(lane):
    out = CreateCalendarEventTool().run(summary="Holiday", start="2026-03-10", end="2026-03-12", internal_only=True,
                                        reminder_minutes=0, **IDENT)
    assert "Created event: Holiday (10.03.2026 - 11.03.2026 (all day))" in out and "Internal only" in out
    ev = _store(lane).list_events(0, 4e9)[0]
    assert ev["all_day"] is True and (ev["start_date"], ev["end_date"]) == ("2026-03-10", "2026-03-12")
    assert ev["account_id"] is None and ev["reminder_minutes"] is None
    assert lane["pushes"] == []


def test_create_refuses_bad_input_and_unconnected_accounts(lane):
    tool = CreateCalendarEventTool()
    assert tool.run(summary="", start="2026-03-10", **IDENT) == "summary is required."
    assert tool.run(summary="x", start="next week", **IDENT).startswith("start is required")
    assert tool.run(summary="x", start="2026-03-10T10:00", end="2026-03-10T09:00", **IDENT) == "end lies before start."
    out = tool.run(summary="x", start="2026-03-10T10:00", provider="microsoft", **IDENT)
    assert out.startswith("No calendar account connected for microsoft"), out          # the classifier's prefix
    out = tool.run(summary="x", start="2026-03-10T10:00", account_id="nobody@x", **IDENT)
    assert out.startswith("No calendar account connected for nobody@x")
    assert _store(lane).count_events() == 0
    lane["accounts"].clear()
    out = tool.run(summary="No account", start="2026-03-10T10:00", **IDENT)                 # nothing named: internal
    assert "Internal only" in out and _store(lane).count_events() == 1


def test_contact_ambiguity_is_asked_not_guessed(lane):
    tool = CreateCalendarEventTool()
    out = tool.run(summary="x", start="2026-03-10T10:00", contact="Lena", **IDENT)
    assert out.startswith('Multiple contacts have the name "Lena"') and "contact_id: c1" in out and "contact_id: c2" in out
    assert _store(lane).count_events() == 0
    out = tool.run(summary="x", start="2026-03-10T10:00", contact="Nobody", **IDENT)
    assert out.startswith("No contact found with name 'Nobody'")
    out = tool.run(summary="x", start="2026-03-10T10:00", contact_id="c2", **IDENT)
    assert out.startswith("Created event") and _store(lane).list_events(0, 4e9)[0]["contact_ids"] == ["c2"]
    assert tool.run(summary="x", start="2026-03-10T10:00", contact_id="c9", **IDENT) == "No contact with contact_id c9."


# ── list ─────────────────────────────────────────────────────────────────────────

def test_list_prints_the_range_in_the_users_zone_with_source_and_ids(lane):
    tool = ListCalendarEventsTool()
    assert tool.run(time_min="2026-03-01", time_max="2026-03-07", **IDENT) == "No events in the given range."
    store = _store(lane)
    a = store.add_event(title="Internal", start_ts=1772370000.0, contact_ids=["c1"], location="Home")
    store.upsert_external(ACC["account_id"], {"id": "ext1", "summary": "From Google", "start": "2026-03-02T09:00:00Z",
                                              "end": "2026-03-02T09:30:00Z", "all_day": False, "tz": "UTC",
                                              "status": "confirmed", "updated": 1.0}, source="gmail")
    store.add_event(title="Whole day", start_ts=1772400000.0, all_day=True, tz="Europe/Berlin", start_date="2026-03-03")
    out = tool.run(time_min="2026-03-01", time_max="2026-03-03", **IDENT)
    lines = out.splitlines()
    assert lines[0] == "Calendar events:"
    assert lines[1] == f"1. Internal | 01.03.2026 14:00 - 15:00 | Home | internal | id: {a['id']}"
    assert lines[2].startswith("2. From Google | 02.03.2026 10:00 - 10:30 | gmail: alice@gmail.example (mirrored) | id: ")
    assert lines[3].startswith("3. Whole day | 03.03.2026 (all day) | internal | id: ")
    assert tool.run(time_min="2026-03-01", time_max="2026-03-03", contact_id="c1", **IDENT).count("\n") == 1
    only_google = tool.run(time_min="2026-03-01", time_max="2026-03-03", provider="gmail", **IDENT)
    assert "From Google" in only_google and "Internal" not in only_google
    assert tool.run(time_min="2026-03-01", time_max="2026-03-03", provider="microsoft", **IDENT).startswith("No calendar account connected for microsoft")
    capped = tool.run(time_min="2026-03-01", time_max="2026-03-03", max_results=1, **IDENT)
    assert capped.endswith("... and 2 more (raise max_results or narrow the range).")
    assert tool.run(time_min="tomorrowish", **IDENT).startswith("time_min must be")


# ── update and delete ────────────────────────────────────────────────────────────

def test_update_moves_keep_the_duration_and_ask_for_the_push(lane):
    store = _store(lane)
    ev = store.add_event(title="Call", start_ts=1772370000.0, end_ts=1772371800.0, tz="Europe/Berlin",
                         account_id=ACC["account_id"], external_id="ext9", sync_state="synced")
    tool = UpdateCalendarEventTool()
    assert tool.run(event_id="nope", **IDENT).startswith("Event not found: nope")
    assert tool.run(event_id=ev["id"], **IDENT).startswith("Nothing to change")
    out = tool.run(event_id=ev["id"], start="2026-03-01T16:00", summary="Call (moved)", **IDENT)
    assert out.startswith("Updated event: Call (moved) (01.03.2026 16:00 - 16:30).") and "Mirrored into" in out
    after = store.get_event(ev["id"])
    assert after["sync_state"] == "pending_push" and lane["pushes"] == [(SCOPE, ACC["account_id"])]
    assert tool.run(event_id=ev["id"], end="2026-03-01T15:00", **IDENT) == "end lies before start."
    assert tool.run(event_id=ev["id"], reminder_minutes=0, **IDENT).startswith("Updated event")
    assert store.get_event(ev["id"])["reminder_minutes"] is None
    out = tool.run(event_id=ev["id"], start="2026-03-04", **IDENT)
    assert "(04.03.2026 (all day))" in out and store.get_event(ev["id"])["all_day"] is True


def test_delete_internal_is_gone_and_mirrored_waits_for_the_provider(lane):
    store = _store(lane)
    internal = store.add_event(title="Internal", start_ts=1772370000.0)
    mirrored = store.add_event(title="Mirrored", start_ts=1772370000.0, account_id=ACC["account_id"],
                               external_id="ext1", sync_state="synced")
    tool = DeleteCalendarEventTool()
    assert tool.run(event_id="", **IDENT) == "event_id is required."
    assert tool.run(event_id=internal["id"], **IDENT) == "Event deleted: Internal."
    assert store.get_event(internal["id"]) is None and lane["pushes"] == []
    out = tool.run(event_id=mirrored["id"], **IDENT)
    assert out == f"Event deleted: Mirrored. It is removed from {ACC['account_id']} by the next sync."
    assert store.get_event(mirrored["id"], include_pending_delete=True)["sync_state"] == "pending_delete"
    assert lane["pushes"] == [(SCOPE, ACC["account_id"])] and lane["signals"] == [SCOPE, SCOPE]
    assert tool.run(event_id=mirrored["id"], **IDENT).startswith("Event not found")   # pending rows are not found twice


def test_identity_kwargs_and_names_are_unchanged():
    for cls, name in ((ListCalendarEventsTool, "list_calendar_events"), (CreateCalendarEventTool, "create_calendar_event"),
                      (UpdateCalendarEventTool, "update_calendar_event"), (DeleteCalendarEventTool, "delete_calendar_event")):
        assert cls.name == name and cls.identity_kwargs == ("user_scope_id", "username")
        assert "Requires a connected" not in cls.description
