# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The contact's file grows into a small CRM: a free status, dated notes, dated events, and
a summary (last contact over every channel link, next event, newest notes). Everything
lives inside the contact record, so it is isolated exactly like the record: one file per
username or scope, and nothing here reads across files."""
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
    s = cs.contact_summary(contact, now_ts=3_000_000_000.0)
    assert s["status"] == "warm friend"
    assert s["last_contact"] == {"channel": "whatsapp", "ts": 2000.0}
    assert s["next_event"]["id"] == ev_next["id"] and s["next_event"]["note"] == "bring the offer"
    assert [e["id"] for e in s["upcoming_events"]] == [ev_next["id"], ev_later["id"]]        # the past one is not upcoming
    assert [n["id"] for n in s["recent_notes"]] == [n2["id"], n1["id"]] and s["notes_count"] == 2
    assert s["recent_notes"][1]["source"] == "agent"

    assert cs.delete_contact_note(cid, n1["id"], "alice", user_scope_id=SCOPE_A)
    assert not cs.delete_contact_note(cid, n1["id"], "alice", user_scope_id=SCOPE_A)
    assert cs.delete_contact_event(cid, ev_past["id"], "alice", user_scope_id=SCOPE_A)
    contact = cs.get_contact_by_id(cid, "alice", user_scope_id=SCOPE_A)
    assert len(contact["notes_log"]) == 1 and len(contact["events"]) == 2


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
    assert [n["text"] for n in back["notes_log"]] == ["private to A"] and len(back["events"]) == 1


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
    assert back["events"][0]["title"] == "Onboarding call" and back["events"][0]["when_ts"] > 4_000_000_000
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
