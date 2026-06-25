# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Stufe-0 completion ledger: the deterministic housekeeping floor of a thinking run. The gate uses it to
refuse termination while a captured note/todo is still unhandled. Storage isolated per test to tmp."""
import vaf.core.thinking_ledger as tl
import vaf.core.automation_planner as ap
import vaf.core.thinking_requests as tr
from vaf.core.platform import Platform


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path))


def test_build_ledger_snapshots_open_notes_and_todos(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    scope = "u1"
    n = ap.add_note(scope, "es ist heiss", title="wetter")
    t = ap.add_todo(scope, "rechnung zahlen")
    # a handled note and a done todo must be excluded from the snapshot
    n2 = ap.add_note(scope, "schon erledigt")
    ap.set_note_handled(scope, n2["id"], True)
    t2 = ap.add_todo(scope, "fertig")
    ap.update_todo(scope, t2["id"], done=True)

    led = tl.build_ledger(scope)
    kinds = {(i["kind"], i["id"]) for i in led}
    assert ("note", n["id"]) in kinds
    assert ("todo", t["id"]) in kinds
    assert ("note", n2["id"]) not in kinds
    assert ("todo", t2["id"]) not in kinds
    assert any(i["label"] == "wetter" for i in led)   # note label = title


def test_note_resolved_when_deleted(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    scope = "u2"
    n = ap.add_note(scope, "x", title="t")
    item = {"kind": "note", "id": n["id"], "label": "t"}
    assert tl.item_resolved(scope, item, current_run_seq=1) is False
    ap.delete_note(scope, n["id"])
    assert tl.item_resolved(scope, item, current_run_seq=1) is True


def test_note_resolved_when_handled(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    scope = "u3"
    n = ap.add_note(scope, "x")
    item = {"kind": "note", "id": n["id"], "label": "x"}
    assert tl.item_resolved(scope, item, current_run_seq=1) is False
    ap.set_note_handled(scope, n["id"], True)
    assert tl.item_resolved(scope, item, current_run_seq=1) is True


def test_todo_resolved_when_deleted_or_done(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    scope = "u4"
    t = ap.add_todo(scope, "do x")
    item = {"kind": "todo", "id": t["id"], "label": "do x"}
    assert tl.item_resolved(scope, item, current_run_seq=1) is False
    ap.update_todo(scope, t["id"], done=True)
    assert tl.item_resolved(scope, item, current_run_seq=1) is True
    t2 = ap.add_todo(scope, "do y")
    item2 = {"kind": "todo", "id": t2["id"], "label": "do y"}
    ap.delete_todo(scope, t2["id"])
    assert tl.item_resolved(scope, item2, current_run_seq=1) is True


def test_resolved_when_request_raised_this_run_only(monkeypatch, tmp_path):
    """A tracked question raised THIS run (within_runs=1) resolves the item; a question from an OLD run
    does not (the item should be handled again, not silently treated as done)."""
    _isolate(monkeypatch, tmp_path)
    scope = "u5"
    n = ap.add_note(scope, "x", title="t")
    item = {"kind": "note", "id": n["id"], "label": "t"}
    tr.add_request(scope, "frage?", run_seq=10, source_note_id=n["id"])
    assert tl.item_resolved(scope, item, current_run_seq=10) is True     # same run
    assert tl.item_resolved(scope, item, current_run_seq=12) is False    # old run -> still unresolved


def test_unresolved_items_and_gate_nudge(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    scope = "u6"
    n = ap.add_note(scope, "kalt", title="heizung")
    t = ap.add_todo(scope, "fenster zu")
    led = tl.build_ledger(scope)
    unresolved = tl.unresolved_items(scope, led, current_run_seq=1)
    assert len(unresolved) == 2
    nudge = tl.build_gate_nudge(unresolved)
    assert n["id"] in nudge and t["id"] in nudge
    assert "heizung" in nudge                 # specific, names the item
    assert "thinking_done" in nudge
    ap.delete_note(scope, n["id"])            # resolve one
    assert len(tl.unresolved_items(scope, led, current_run_seq=1)) == 1


def test_ledger_orders_todos_before_notes(monkeypatch, tmp_path):
    """Act-able todos come first so a run does the most work before the one message-stop a note triggers."""
    _isolate(monkeypatch, tmp_path)
    scope = "u-ord"
    ap.add_note(scope, "kalt", title="heizung")
    ap.add_todo(scope, "rechnung zahlen")
    led = tl.build_ledger(scope)
    assert [i["kind"] for i in led] == ["todo", "note"]


def test_ledger_todo_carries_due_at(monkeypatch, tmp_path):
    """A todo's deadline is captured so the forced prompt can give the agent scheduling context."""
    _isolate(monkeypatch, tmp_path)
    scope = "u-due"
    ap.add_todo(scope, "Bericht abgeben", due_at="2026-06-25")
    led = tl.build_ledger(scope)
    todo = next(i for i in led if i["kind"] == "todo")
    assert todo["due_at"] == "2026-06-25"


def test_forced_todo_prompt_converts_to_automation():
    """A future TODO is resolved by turning it into an automation: reminder built autonomously
    (create_automation + clear the todo), action proposed via ask_user. Deadline included; dedup guard."""
    import vaf.core.thinking_mode as tm
    from datetime import datetime, timedelta
    future = (datetime.now() + timedelta(days=5)).date().isoformat()
    p = tm._build_forced_item_prompt({"kind": "todo", "id": "t9", "label": "Quartalsbericht", "due_at": future})
    assert "create_automation" in p          # reminder built autonomously
    assert "delete_automation_todo" in p     # then clear the todo
    assert "ask_user" in p and "source_todo_id" in p   # action -> ask
    assert future in p                       # deadline as scheduling context
    assert "duplicate" in p.lower()          # dedup guard against existing automations
    # a note still uses the note path (no automation)
    pn = tm._build_forced_item_prompt({"kind": "note", "id": "n1", "label": "wetter"})
    assert "create_automation" not in pn and "source_note_id" in pn


def test_forced_todo_overdue_asks_not_schedules():
    """An OVERDUE todo (deadline in the past) must NOT be scheduled into the future — ask if still relevant."""
    import vaf.core.thinking_mode as tm
    from datetime import datetime, timedelta
    past = (datetime.now() - timedelta(days=3)).date().isoformat()
    p = tm._build_forced_item_prompt({"kind": "todo", "id": "t1", "label": "Steuer abgeben", "due_at": past})
    assert "OVERDUE" in p or "PASSED" in p
    assert "create_automation" not in p              # do NOT build a future reminder for a passed deadline
    assert "ask_user" in p and "source_todo_id" in p
    assert past in p


def test_deadline_status_helper():
    import vaf.core.thinking_mode as tm
    from datetime import datetime, timedelta
    assert tm._deadline_status("") == ""
    assert tm._deadline_status("not-a-date") == ""
    today = datetime.now().date().isoformat()
    assert "TODAY" in tm._deadline_status(today)
    assert "OVERDUE" in tm._deadline_status((datetime.now() - timedelta(days=2)).date().isoformat())
    assert "in 4 days" in tm._deadline_status((datetime.now() + timedelta(days=4)).date().isoformat())


def test_recent_runs_window_suppresses_reask(monkeypatch, tmp_path):
    """An item asked in a PREVIOUS run stays 'resolved' within the recency window (no re-ask every run),
    then re-surfaces once the window passes."""
    _isolate(monkeypatch, tmp_path)
    scope = "u-rw"
    n = ap.add_note(scope, "x", title="t")
    item = {"kind": "note", "id": n["id"], "label": "t"}
    tr.add_request(scope, "frage?", run_seq=10, source_note_id=n["id"])
    # default recent_runs=1: a request from run 10 does NOT cover run 13 -> would re-ask
    assert tl.item_resolved(scope, item, current_run_seq=13, recent_runs=1) is False
    # recent_runs=6: 13-10=3 < 6 -> resolved (do NOT re-ask)
    assert tl.item_resolved(scope, item, current_run_seq=13, recent_runs=6) is True
    # window passed: 17-10=7 >= 6 -> unresolved again (re-surfaces)
    assert tl.item_resolved(scope, item, current_run_seq=17, recent_runs=6) is False


def test_empty_ledger_never_blocks(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert tl.build_ledger("u7") == []
    assert tl.unresolved_items("u7", [], current_run_seq=1) == []


def test_turn_used_progress_tool():
    """The progress-gate's per-turn detector: gathering/analysing tools do NOT count as progress; a
    decisive act/ask/clear/finish tool does."""
    import vaf.core.thinking_mode as tm

    def _asst(*names):
        return {"role": "assistant", "tool_calls": [{"function": {"name": n}} for n in names]}

    # pure gather/analyse turn (the 15:38 spin) -> NOT progress
    assert tm._turn_used_progress_tool([_asst("web_search", "web_search", "memory_search")]) is False
    assert tm._turn_used_progress_tool([{"role": "assistant", "content": "let me think..."}]) is False
    # a decisive tool -> progress
    assert tm._turn_used_progress_tool([_asst("ask_user")]) is True
    assert tm._turn_used_progress_tool([_asst("web_search", "delete_automation_note")]) is True
    assert tm._turn_used_progress_tool([_asst("thinking_done")]) is True
