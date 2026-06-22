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
