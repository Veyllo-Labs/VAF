# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""One-shot reminder lane (calendar-check design decision, option b).

A reminder is stored DATA delivered verbatim by the scheduler - no agent run,
no tools - which is why the calendar check may schedule reminders although
create_automation stays stripped from automation runs (runaway guard intact).
Replaces the set_timer fallback that was in-memory only and session-anchored
(live 2026-07-13: reminders from a background run were non-persistent and
routed via the process-global session fallback).
"""
from datetime import datetime, timedelta
from pathlib import Path

import vaf.core.reminders as rem
from vaf.core.platform import Platform
from vaf.tools.schedule_reminder import ScheduleReminderTool

SCOPE = "ab12cd34-0000-4000-8000-000000000001"


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path))


def _in(minutes):
    return (datetime.now() + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M")


# ── store: create/caps/horizon ────────────────────────────────────────────────

def test_create_and_list(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    res = rem.create_reminder(SCOPE, "mert", "Meeting in 30 Minuten", _in(30))
    assert res["ok"], res
    items = rem.list_reminders(SCOPE)
    assert len(items) == 1 and items[0]["message"] == "Meeting in 30 Minuten"
    # cross-scope isolation
    assert rem.list_reminders("other-scope") == []


def test_past_horizon_and_cap_are_refused(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert not rem.create_reminder(SCOPE, "mert", "x", _in(-10))["ok"]
    far = (datetime.now() + timedelta(days=rem.MAX_HORIZON_DAYS + 1)).strftime("%Y-%m-%d %H:%M")
    assert not rem.create_reminder(SCOPE, "mert", "x", far)["ok"]
    for i in range(rem.MAX_PENDING_PER_USER):
        assert rem.create_reminder(SCOPE, "mert", f"r{i}", _in(30 + i))["ok"]
    capped = rem.create_reminder(SCOPE, "mert", "one too many", _in(200))
    assert not capped["ok"] and "cap" in capped["error"]


def test_cancel(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    r = rem.create_reminder(SCOPE, "mert", "x", _in(30))["reminder"]
    assert rem.cancel_reminder(SCOPE, r["id"]) is True
    assert rem.list_reminders(SCOPE) == []
    assert rem.cancel_reminder(SCOPE, "nope") is False


# ── firing: verbatim delivery, grace, missed ──────────────────────────────────

def _capture_router(monkeypatch, result=(True, "telegram")):
    sent = []
    import vaf.core.messaging_connections as mc
    monkeypatch.setattr(mc, "send_to_main_messenger",
                        lambda scope, user, text, file_path=None, record=True:
                        sent.append((scope, user, text)) or result)
    notes = []
    import vaf.core.user_notifications as un
    monkeypatch.setattr(un, "append_notification", lambda *a, **k: notes.append((a, k)) or {})
    return sent, notes


def test_due_reminder_is_delivered_verbatim(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    sent, notes = _capture_router(monkeypatch)
    r = rem.create_reminder(SCOPE, "mert", "Dein Meeting startet gleich.", _in(30))["reminder"]
    # not due yet
    assert rem.fire_due_reminders() == 0 and sent == []
    # force due (within grace)
    items = rem._load(SCOPE)
    items[0]["fire_at"] = (datetime.now() - timedelta(minutes=5)).isoformat()
    rem._save(SCOPE, items)
    assert rem.fire_due_reminders() == 1
    assert sent == [(SCOPE, "mert", "Dein Meeting startet gleich.")]  # verbatim, owner-scoped
    assert rem.list_reminders(SCOPE) == []  # no longer pending
    assert notes == []


def test_overdue_beyond_grace_is_marked_missed_honestly(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    sent, notes = _capture_router(monkeypatch)
    rem.create_reminder(SCOPE, "mert", "zu spaet", _in(30))
    items = rem._load(SCOPE)
    items[0]["fire_at"] = (datetime.now() - timedelta(hours=rem.GRACE_HOURS + 1)).isoformat()
    rem._save(SCOPE, items)
    assert rem.fire_due_reminders() == 1
    assert sent == []  # never delivered late beyond grace
    assert notes and "missed" in notes[0][1].get("title", "").lower()


def test_no_messenger_falls_back_to_notification(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    sent, notes = _capture_router(monkeypatch, result=(False, None))
    rem.create_reminder(SCOPE, "mert", "hallo", _in(30))
    items = rem._load(SCOPE)
    items[0]["fire_at"] = (datetime.now() - timedelta(minutes=1)).isoformat()
    rem._save(SCOPE, items)
    rem.fire_due_reminders()
    assert notes, "reminder without messenger must surface as a Web UI notification"


# ── tool + wiring ─────────────────────────────────────────────────────────────

def test_tool_roundtrip(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    t = ScheduleReminderTool()
    out = t.run(message="Test", fire_at=_in(45), username="mert", user_scope_id=SCOPE)
    assert "Reminder scheduled" in out and "verbatim" in out
    listing = t.run(action="list", user_scope_id=SCOPE)
    assert "Test" in listing
    rid = rem.list_reminders(SCOPE)[0]["id"]
    assert "cancelled" in t.run(action="cancel", reminder_id=rid, user_scope_id=SCOPE)
    assert t.run(action="list", user_scope_id=SCOPE) == "No pending reminders."


def test_wiring_thinking_exclusion_injection_scheduler_hook():
    import vaf.core.agent as agent_mod
    import vaf.core.automation as auto_mod
    import vaf.workflows.engine as engine_mod
    a = Path(agent_mod.__file__).read_text(encoding="utf-8")
    assert '"set_timer", "schedule_reminder",' in a, (
        "thinking runs must not schedule reminders (propose-only lane)"
    )
    assert 'if name == "schedule_reminder":' in a, "dispatch lost the owner-scope injection"
    assert "fire_due_reminders()" in Path(auto_mod.__file__).read_text(encoding="utf-8"), (
        "scheduler loop lost the reminder tick"
    )
    assert '"schedule_reminder"' in Path(engine_mod.__file__).read_text(encoding="utf-8")


def test_calendar_prompt_teaches_reminders_not_automations():
    from vaf.api.calendar_routes import DEFAULT_CALENDAR_CHECK_PROMPT as p
    assert "schedule_reminder" in p
    assert "delivered VERBATIM" in p
    assert "create one-off automation" not in p
    assert "call create_automation for each" not in p


def test_front_office_does_not_get_reminders():
    from vaf.core.front_office_tools import FRONT_OFFICE_ALLOWED_TOOLS
    assert "schedule_reminder" not in FRONT_OFFICE_ALLOWED_TOOLS
