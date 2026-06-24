"""thinking_done(message=...) fallback delivery. agent.chat_step special-cases thinking_done and returns
BEFORE running the tool, so the delivery must happen via the shared helper deliver_thinking_done_fallback
(called from both ThinkingDoneTool.run and the agent dispatch). Web UI emit is stubbed."""
import vaf.core.thinking_mode as tm
import vaf.core.thinking_requests as tr
from vaf.core.platform import Platform


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path))


def test_fallback_delivers_and_tracks(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("VAF_THINKING_MODE", "1")
    monkeypatch.setattr(tm, "emit_message_to_web_ui", lambda scope, content, session_id=None: None)
    scope = "user-x"
    note = tm.deliver_thinking_done_fallback(
        scope, "Soll ich dir eine Erinnerung einrichten?",
        proposed_action="create reminder", source_note_id="n1",
    )
    reqs = tr.list_requests(scope, status="asked")
    assert len(reqs) == 1
    assert reqs[0]["source_note_id"] == "n1"
    assert reqs[0]["proposed_action"] == "create reminder"
    assert reqs[0]["id"] in note
    waiting = tm.get_waiting_for_reply(scope)
    assert waiting and waiting.get("request_id") == reqs[0]["id"]


def test_details_are_stored_for_the_main_agent(monkeypatch, tmp_path):
    """The `details` channel: a teaser message stores the concrete findings on the request so the main
    agent can answer a follow-up with the REAL content instead of re-deriving it."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("VAF_THINKING_MODE", "1")
    monkeypatch.setattr(tm, "emit_message_to_web_ui", lambda scope, content, session_id=None: None)
    scope = "user-d"
    tm.deliver_tracked_message(
        scope, "Ich habe 3 Kühl-Tipps gefunden – willst du sie?",
        source_note_id="n1",
        details="1) Lüfter ans Fenster nach außen. 2) feuchtes Tuch davor. 3) tagsüber Vorhänge zu.",
    )
    reqs = tr.list_requests(scope, status="asked")
    assert len(reqs) == 1
    assert reqs[0]["details"].startswith("1) Lüfter")
    assert "Vorhänge" in reqs[0]["details"]
    # the user-facing question must NOT contain the raw details dump
    assert "Vorhänge" not in reqs[0]["question"]


def test_fallback_empty_message_is_noop(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    scope = "user-y"
    assert tm.deliver_thinking_done_fallback(scope, "") == ""
    assert tm.deliver_thinking_done_fallback(scope, None) == ""
    assert tr.list_requests(scope) == []


def test_fallback_does_not_double_send(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("VAF_THINKING_MODE", "1")
    sent = []
    monkeypatch.setattr(tm, "emit_message_to_web_ui",
                        lambda scope, content, session_id=None: (sent.append(content), "sid-1")[1])
    scope = "user-z"
    tm.set_proactive_mode(scope, "open")   # allow the free message to deliver, then test the double-send guard
    tm.deliver_thinking_done_fallback(scope, "Erste Frage?")
    note = tm.deliver_thinking_done_fallback(scope, "Zweite Frage?")
    assert "already delivered" in note
    assert len(tr.list_requests(scope)) == 1
    assert sent == ["Erste Frage?"]


def test_thinking_done_tool_uses_fallback(monkeypatch, tmp_path):
    """End-to-end via the tool: ThinkingDoneTool.run(message=...) delivers + tracks (the path
    agent.chat_step now mirrors inline)."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("VAF_THINKING_MODE", "1")
    monkeypatch.setattr(tm, "emit_message_to_web_ui", lambda scope, content, session_id=None: None)
    from vaf.tools.thinking_done import ThinkingDoneTool
    scope = "user-td"
    out = ThinkingDoneTool().run(summary="did the thing", message="Frage?", source_note_id="n9",
                                 user_scope_id=scope)
    reqs = tr.list_requests(scope, status="asked")
    assert len(reqs) == 1 and reqs[0]["source_note_id"] == "n9"
    assert "did the thing" in out and reqs[0]["id"] in out
