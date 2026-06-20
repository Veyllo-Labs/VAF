"""Background-request store: tracks questions/proposals the thinking run raised, with a status
lifecycle (asked -> confirmed -> done / declined) and a 6-run recency window so the next run does not
re-ask. Storage is isolated per test to a tmp vaf_dir."""
import vaf.core.thinking_requests as tr
from vaf.core.platform import Platform


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path))


def test_add_and_status_lifecycle(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    scope = "u1"
    e = tr.add_request(scope, "Soll ich XYZ erledigen?", run_seq=10, proposed_action="create automation X")
    assert e["status"] == "asked" and e["run_seq"] == 10 and e["id"]
    assert tr.get_request(scope, e["id"])["status"] == "asked"
    assert tr.update_request_status(scope, e["id"], "confirmed")["status"] == "confirmed"
    assert tr.update_request_status(scope, e["id"], "done")["status"] == "done"
    assert tr.update_request_status(scope, e["id"], "bogus") is None  # invalid status ignored


def test_recency_window(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    scope = "u2"
    old = tr.add_request(scope, "old q", run_seq=1)
    new = tr.add_request(scope, "new q", run_seq=10)
    # current run = 12, window 6 -> run_seq=10 in (12-10=2<6); run_seq=1 out (12-1=11)
    ids = {e["id"] for e in tr.list_requests(scope, within_runs=6, current_run_seq=12)}
    assert new["id"] in ids and old["id"] not in ids


def test_recent_prompt_lists_within_window(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    scope = "u3"
    tr.add_request(scope, "Reminder einrichten?", run_seq=9, proposed_action="set reminder")
    tr.add_request(scope, "Sehr alte Frage", run_seq=1)
    p = tr.recent_requests_prompt(scope, current_run_seq=10, within_runs=6)
    assert "Reminder einrichten?" in p
    assert "Sehr alte Frage" not in p   # out of window
    assert "do NOT ask these again" in p


def test_empty_prompt_when_nothing_recent(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert tr.recent_requests_prompt("u4", current_run_seq=5, within_runs=6) == ""
    tr.add_request("u4", "q", run_seq=0)  # run_seq 0 vs current 100 -> out of window
    assert tr.recent_requests_prompt("u4", current_run_seq=100, within_runs=6) == ""


def test_status_filter(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    scope = "u5"
    a = tr.add_request(scope, "q1", run_seq=1)
    b = tr.add_request(scope, "q2", run_seq=1)
    tr.update_request_status(scope, b["id"], "declined")
    assert {e["id"] for e in tr.list_requests(scope, status="asked")} == {a["id"]}
    assert {e["id"] for e in tr.list_requests(scope, status="declined")} == {b["id"]}


def test_ask_user_tool_creates_request_and_waiting(monkeypatch, tmp_path):
    """The ask_user tool records a tracked request (asked) and links it into waiting_for_reply, so the
    main agent can pick it up. Web UI delivery is stubbed out (no session in tests)."""
    from vaf.core.platform import Platform
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("VAF_THINKING_MODE", "1")
    import vaf.core.thinking_mode as tm
    monkeypatch.setattr(tm, "emit_message_to_web_ui", lambda scope, content: None)

    from vaf.tools.ask_user import AskUserTool
    scope = "user-xyz"
    out = AskUserTool().run(
        message="Soll ich dir eine Erinnerung einrichten?",
        proposed_action="create reminder automation",
        source_note_id="note123",
        user_scope_id=scope,
    )
    reqs = tr.list_requests(scope, status="asked")
    assert len(reqs) == 1
    rid = reqs[0]["id"]
    assert reqs[0]["proposed_action"] == "create reminder automation"
    assert reqs[0]["source_note_id"] == "note123"   # linked so confirm can clear the note
    assert rid in out
    waiting = tm.get_waiting_for_reply(scope)
    assert waiting and waiting.get("request_id") == rid
    assert "Erinnerung" in (waiting.get("question_text") or "")


def test_handled_note_disappears_from_list(monkeypatch, tmp_path):
    """A note marked handled is hidden from list_notes by default (so it stops re-surfacing in the
    thinking gather and the user's list), but is still visible with include_handled=True."""
    from vaf.core.platform import Platform
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path))
    import vaf.core.automation_planner as ap
    n = ap.add_note("s1", "es ist heiss, abkuehlen", title="wetter")
    assert n["handled"] is False
    assert [x["id"] for x in ap.list_notes("s1")] == [n["id"]]
    assert ap.set_note_handled("s1", n["id"], True) is True
    assert ap.list_notes("s1") == []                                   # hidden by default
    assert [x["id"] for x in ap.list_notes("s1", include_handled=True)] == [n["id"]]
    assert ap.set_note_handled("s1", n["id"], False) is True           # reversible
    assert [x["id"] for x in ap.list_notes("s1")] == [n["id"]]
