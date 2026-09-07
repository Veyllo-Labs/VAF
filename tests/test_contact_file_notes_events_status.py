# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The contact's file grows into a small CRM: a free status, dated notes, dated appointments,
and a summary (last contact over every channel link, next event, newest notes). Status and
notes live inside the contact record; the appointments are events of the user's calendar
(vaf/core/calendar_store.py) linked to the contact, read back through contact_events. Both
are isolated exactly like the record: one file or store per username or scope, and nothing
here reads across them."""
import asyncio
from types import SimpleNamespace

import pytest

from vaf.core import contacts_store as cs
from vaf.core.platform import Platform

SCOPE_A = "11111111-2222-3333-4444-555555555555"
SCOPE_B = "66666666-7777-8888-9999-000000000000"


@pytest.fixture
def scratch(monkeypatch, tmp_path):
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    return tmp_path


def test_status_notes_events_and_summary(scratch):
    c = cs.create_contact("Dana New", "alice", user_scope_id=SCOPE_A, whatsapp_phone="+491700000042")
    cid = c["id"]
    assert cs.update_contact(cid, "alice", user_scope_id=SCOPE_A, status="lead")["status"] == "lead"
    assert "lead" in cs.contact_status_values("alice", user_scope_id=SCOPE_A)
    assert cs.update_contact(cid, "alice", user_scope_id=SCOPE_A, status="warm friend")["status"] == "warm friend"
    assert "warm friend" in cs.contact_status_values("alice", user_scope_id=SCOPE_A)     # free label, offered afterwards

    n1 = cs.add_contact_note(cid, "interested in feature X", "alice", user_scope_id=SCOPE_A, source="agent")
    n2 = cs.add_contact_note(cid, "follow up next week", "alice", user_scope_id=SCOPE_A)
    assert cs.add_contact_note(cid, "   ", "alice", user_scope_id=SCOPE_A) is None
    ev_past = cs.add_contact_event(cid, "Kickoff", 1000.0, "alice", user_scope_id=SCOPE_A)
    ev_next = cs.add_contact_event(cid, "Meeting", 4_000_000_000.0, "alice", user_scope_id=SCOPE_A, note="bring the offer")
    ev_later = cs.add_contact_event(cid, "Review", 4_100_000_000.0, "alice", user_scope_id=SCOPE_A)
    cs.sync_channel_contacts("whatsapp", [{"endpoint": "+491700000042", "display_name": "Dana New", "last_seen_ts": 2000.0}], "alice", user_scope_id=SCOPE_A)

    contact = cs.get_contact_by_id(cid, "alice", user_scope_id=SCOPE_A)
    assert contact.get("events", []) == []                                            # the record holds none: the calendar does
    events = cs.contact_events(contact, "alice", user_scope_id=SCOPE_A)
    assert [e["title"] for e in events] == ["Kickoff", "Meeting", "Review"] and events[1]["source"] == "user"
    s = cs.contact_summary(contact, now_ts=3_000_000_000.0, events=events)
    assert s["status"] == "warm friend"
    assert s["last_contact"] == {"channel": "whatsapp", "ts": 2000.0}
    assert s["next_event"]["id"] == ev_next["id"] and s["next_event"]["note"] == "bring the offer"
    assert [e["id"] for e in s["upcoming_events"]] == [ev_next["id"], ev_later["id"]]        # the past one is not upcoming
    assert [n["id"] for n in s["recent_notes"]] == [n2["id"], n1["id"]] and s["notes_count"] == 2
    assert s["recent_notes"][1]["source"] == "agent"

    assert cs.delete_contact_note(cid, n1["id"], "alice", user_scope_id=SCOPE_A)
    assert not cs.delete_contact_note(cid, n1["id"], "alice", user_scope_id=SCOPE_A)
    assert cs.delete_contact_event(cid, ev_past["id"], "alice", user_scope_id=SCOPE_A)
    assert not cs.delete_contact_event(cid, ev_past["id"], "alice", user_scope_id=SCOPE_A)
    contact = cs.get_contact_by_id(cid, "alice", user_scope_id=SCOPE_A)
    assert len(contact["notes_log"]) == 1 and len(cs.contact_events(contact, "alice", user_scope_id=SCOPE_A)) == 2
    # the appointment is a calendar event linked to the contact, with the user's default reminder
    from vaf.core import calendar_store as cal
    row = cal.store_for("alice", SCOPE_A).get_event(ev_next["id"])
    assert row["contact_ids"] == [cid] and row["description"] == "bring the offer" and row["reminder_minutes"] == cal.DEFAULT_REMINDER_MINUTES


def test_notes_and_events_never_cross_a_scope_or_a_username(scratch):
    a = cs.create_contact("Dana New", "alice", user_scope_id=SCOPE_A, whatsapp_phone="+491700000042")
    cs.add_contact_note(a["id"], "private to A", "alice", user_scope_id=SCOPE_A)
    cs.add_contact_event(a["id"], "A's meeting", 4_000_000_000.0, "alice", user_scope_id=SCOPE_A)
    # Another scope neither sees the contact nor can it attach anything to it by id.
    assert cs.list_contacts("bob", user_scope_id=SCOPE_B) == []
    assert cs.add_contact_note(a["id"], "leak attempt", "bob", user_scope_id=SCOPE_B) is None
    assert cs.add_contact_event(a["id"], "leak attempt", 4_000_000_000.0, "bob", user_scope_id=SCOPE_B) is None
    assert cs.get_contact_by_id(a["id"], "bob", user_scope_id=SCOPE_B) is None
    assert not cs.delete_contact_note(a["id"], "any", "bob", user_scope_id=SCOPE_B)
    assert cs.contact_status_values("bob", user_scope_id=SCOPE_B) == list(cs.CONTACT_STATUS_DEFAULTS)
    # And A still has everything.
    back = cs.get_contact_by_id(a["id"], "alice", user_scope_id=SCOPE_A)
    assert [n["text"] for n in back["notes_log"]] == ["private to A"] and len(cs.contact_events(back, "alice", SCOPE_A)) == 1
    # and the leak attempt created no calendar for B, let alone an event in A's
    from vaf.core import calendar_store as cal
    assert not cal.CalendarStore.exists(SCOPE_B)
    assert not cs.delete_contact_event(a["id"], cs.contact_events(back, "alice", SCOPE_A)[0]["id"], "bob", user_scope_id=SCOPE_B)


def test_update_contact_tool_sets_status_and_appends_notes_and_events_in_the_callers_scope(scratch):
    from vaf.tools.update_contact import UpdateContactTool
    c = cs.create_contact("Dana New", "alice", user_scope_id=SCOPE_A)
    out = UpdateContactTool().run(contact_id=c["id"], username="alice", user_scope_id=SCOPE_A,
                                  status="customer", add_note="signed the offer",
                                  add_event_title="Onboarding call", add_event_when="2099-01-02 15:00")
    assert "fields status" in out and "note added" in out and "event added" in out
    back = cs.get_contact_by_id(c["id"], "alice", user_scope_id=SCOPE_A)
    assert back["status"] == "customer"
    assert back["notes_log"][0]["text"] == "signed the offer" and back["notes_log"][0]["source"] == "agent"
    events = cs.contact_events(back, "alice", SCOPE_A)
    assert events[0]["title"] == "Onboarding call" and events[0]["when_ts"] > 4_000_000_000 and events[0]["source"] == "agent"
    assert "add_event_when is required" in UpdateContactTool().run(contact_id=c["id"], username="alice", user_scope_id=SCOPE_A, add_event_title="x")
    # The other scope's tool call cannot touch it.
    assert "No contact found" in UpdateContactTool().run(contact_id=c["id"], username="bob", user_scope_id=SCOPE_B, status="archived")


def test_get_contact_tool_reports_status_last_contact_events_and_notes(scratch):
    from vaf.tools.get_contact import GetContactTool
    c = cs.create_contact("Dana New", "alice", user_scope_id=SCOPE_A, whatsapp_phone="+491700000042")
    cs.update_contact(c["id"], "alice", user_scope_id=SCOPE_A, status="lead")
    cs.add_contact_note(c["id"], "wants a demo", "alice", user_scope_id=SCOPE_A)
    cs.add_contact_event(c["id"], "Demo", 4_000_000_000.0, "alice", user_scope_id=SCOPE_A)
    cs.sync_channel_contacts("whatsapp", [{"endpoint": "+491700000042", "display_name": "Dana", "last_seen_ts": 2000.0}], "alice", user_scope_id=SCOPE_A)
    out = GetContactTool().run(name="Dana New", username="alice", user_scope_id=SCOPE_A)
    assert "Status: lead" in out and "Last contact: 1970-01-01 via whatsapp" in out
    assert "Upcoming:" in out and "Demo" in out and "Note (" in out and "wants a demo" in out
    assert "Linked via whatsapp" in out


def test_overview_route_is_scoped_and_survives_a_missing_calendar(scratch, monkeypatch):
    from vaf.api import contact_routes as routes
    c = cs.create_contact("Dana New", "alice", user_scope_id=SCOPE_A)
    cs.add_contact_event(c["id"], "Meeting", 4_000_000_000.0, "alice", user_scope_id=SCOPE_A)
    monkeypatch.setattr(cs, "contact_calendar_events", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no calendar")))
    req_a = SimpleNamespace(state=SimpleNamespace(user={"user_scope_id": SCOPE_A, "username": "alice"}))
    out = asyncio.run(routes.get_contact_overview(c["id"], req_a))
    assert out["next_event"]["title"] == "Meeting" and out["calendar_events"] == []
    req_b = SimpleNamespace(state=SimpleNamespace(user={"user_scope_id": SCOPE_B, "username": "bob"}))
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        asyncio.run(routes.get_contact_overview(c["id"], req_b))


def test_two_notes_from_one_clock_tick_still_come_newest_first(scratch):
    """Windows CI: add_contact_note stamps time.time(), which ticks coarsely there, so two
    notes added back to back carried the same ts and the summary listed the older one
    first. The order of equal timestamps is the order they were written, newest first."""
    c = cs.create_contact("Dana New", "alice", user_scope_id=SCOPE_A)
    contacts = cs._load_all("alice", SCOPE_A)
    for rec in contacts:
        if rec["id"] == c["id"]:
            rec["notes_log"] = [{"id": "older", "ts": 1000.0, "text": "first", "source": "user"},
                                {"id": "newer", "ts": 1000.0, "text": "second", "source": "user"}]
    cs._save_all(contacts, "alice", SCOPE_A)
    s = cs.contact_summary(cs.get_contact_by_id(c["id"], "alice", user_scope_id=SCOPE_A), now_ts=2000.0)
    assert [n["id"] for n in s["recent_notes"]] == ["newer", "older"]
