# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Background-request store: tracks questions/proposals the thinking run raised, with a status
lifecycle (asked -> replied -> done / declined; reconfirm re-opens to asked) and a 6-run recency window
so the next run does not re-ask. Storage is isolated per test to a tmp vaf_dir."""
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


def test_record_reply_sets_replied_and_merges(monkeypatch, tmp_path):
    """record_reply is called twice — once at pickup (user_reply), once at end-of-turn (main_reply) —
    and must move the request to 'replied' while merging only the provided field (no clobber)."""
    _isolate(monkeypatch, tmp_path)
    scope = "u-rr"
    e = tr.add_request(scope, "Automate tests?", run_seq=1, proposed_action="create automation")
    assert e["user_reply"] is None and e["main_reply"] is None and e["needs_reconfirm"] is False
    r1 = tr.record_reply(scope, e["id"], user_reply="bin noch am umbau aber danke")
    assert r1["status"] == "replied" and r1["user_reply"] == "bin noch am umbau aber danke"
    assert r1["main_reply"] is None                       # second field not touched yet
    r2 = tr.record_reply(scope, e["id"], main_reply="Alles gut, kein Thema.")
    assert r2["status"] == "replied"
    assert r2["user_reply"] == "bin noch am umbau aber danke"   # first field preserved
    assert r2["main_reply"] == "Alles gut, kein Thema."
    assert tr.record_reply(scope, "nope", user_reply="x") is None  # unknown id


def test_reopen_for_reconfirm(monkeypatch, tmp_path):
    """An undecidable 'replied' request re-opens to 'asked' with needs_reconfirm=True (followups kept)."""
    _isolate(monkeypatch, tmp_path)
    scope = "u-rc"
    e = tr.add_request(scope, "Automate tests?", run_seq=1)
    tr.record_reply(scope, e["id"], user_reply="warum fragst du?")
    rc = tr.reopen_for_reconfirm(scope, e["id"])
    assert rc["status"] == "asked" and rc["needs_reconfirm"] is True and rc["reconfirmed"] is True
    # A re-ask delivery clears the reconfirm flag.
    b = tr.bump_followup(scope, e["id"], new_question="Hey sry, hatten wir das gemacht?", run_seq=2)
    assert b["needs_reconfirm"] is False and b["followups"] == 1


def test_replied_excluded_from_open_proactive(monkeypatch, tmp_path):
    """A 'replied' request is awaiting classification and must NOT be picked as the open follow-up."""
    _isolate(monkeypatch, tmp_path)
    scope = "u-ex"
    e = tr.add_request(scope, "Automate tests?", run_seq=1)
    assert tr.get_open_proactive_request(scope, current_run_seq=1) is not None  # 'asked' -> open
    tr.record_reply(scope, e["id"], user_reply="hm")
    assert tr.get_open_proactive_request(scope, current_run_seq=1) is None      # 'replied' -> not open
    tr.reopen_for_reconfirm(scope, e["id"])
    assert tr.get_open_proactive_request(scope, current_run_seq=1) is not None  # reopened -> open again


def test_recent_prompt_has_replied_rule(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    scope = "u-rp"
    e = tr.add_request(scope, "Reminder einrichten?", run_seq=9)
    tr.record_reply(scope, e["id"], user_reply="ja")
    p = tr.recent_requests_prompt(scope, current_run_seq=10, within_runs=6)
    assert "[replied]" in p
    assert "awaiting classification" in p


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


def test_session_id_anchor_field_and_pin(monkeypatch, tmp_path):
    """The request stores the web-session anchor; set_request_session updates it; a follow-up keeps it."""
    _isolate(monkeypatch, tmp_path)
    scope = "u-sid"
    e = tr.add_request(scope, "Automate?", run_seq=1, session_id="sess-A")
    assert e["session_id"] == "sess-A" and tr.get_request(scope, e["id"])["session_id"] == "sess-A"
    tr.set_request_session(scope, e["id"], "sess-B")
    assert tr.get_request(scope, e["id"])["session_id"] == "sess-B"
    tr.bump_followup(scope, e["id"], new_question="still?", run_seq=2)
    assert tr.get_request(scope, e["id"])["session_id"] == "sess-B"   # follow-up does NOT change the anchor
    assert tr.add_request(scope, "q2", run_seq=1)["session_id"] is None   # default


def test_waiting_state_carries_session_id(monkeypatch, tmp_path):
    from vaf.core.platform import Platform
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path))
    import vaf.core.thinking_mode as tm
    tm.set_waiting_for_reply("u-w", username="mert", question_text="q", request_id="r1", session_id="sess-A")
    assert tm.get_waiting_for_reply("u-w").get("session_id") == "sess-A"
    tm.set_waiting_for_reply("u-w2", username="mert", question_text="q")   # default omits it
    assert tm.get_waiting_for_reply("u-w2").get("session_id") is None


def test_emit_message_pins_anchor_and_falls_back(monkeypatch, tmp_path):
    """emit_message_to_web_ui delivers to the anchor session when present; when the anchor is gone it
    falls back to the latest web session and RETURNS the fallback sid (so the caller can re-pin)."""
    from vaf.core.platform import Platform
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path))
    import vaf.core.thinking_mode as tm

    class _FakeSession:
        def add_message(self, *a, **k): pass
    class _FakeSM:
        def list(self, **k): return [{"id": "latest", "metadata": {}}]
        def load(self, sid):
            if sid == "dead":
                raise FileNotFoundError(sid)
            return _FakeSession()
        def save(self, s): pass
    emits = []
    class _FakeWI:
        def emit_agent_message_append(self, **k): emits.append(("append", k.get("session_id")))
        def emit_session_unread(self, sid): emits.append(("unread", sid))
    monkeypatch.setattr("vaf.core.session.SessionManager", _FakeSM)
    monkeypatch.setattr("vaf.core.web_interface.get_web_interface", lambda: _FakeWI())

    assert tm.emit_message_to_web_ui("u", "hi", session_id="sess-A") == "sess-A"   # anchor alive
    assert tm.emit_message_to_web_ui("u", "hi", session_id="dead") == "latest"     # anchor gone -> fallback
    assert ("append", "latest") in emits and ("unread", "latest") in emits


def test_ask_user_tool_creates_request_and_waiting(monkeypatch, tmp_path):
    """The ask_user tool records a tracked request (asked) and links it into waiting_for_reply, so the
    main agent can pick it up. Web UI delivery is stubbed out (no session in tests)."""
    from vaf.core.platform import Platform
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("VAF_THINKING_MODE", "1")
    import vaf.core.thinking_mode as tm
    monkeypatch.setattr(tm, "emit_message_to_web_ui", lambda scope, content, session_id=None: None)

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


def test_thinking_done_message_delivers_and_tracks(monkeypatch, tmp_path):
    """thinking_done(message=...) is the fallback channel: when a weak model composes the question but
    never calls ask_user, putting the text in thinking_done still records a tracked request, links the
    source note, and sets waiting_for_reply (Web UI delivery stubbed)."""
    from vaf.core.platform import Platform
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("VAF_THINKING_MODE", "1")
    import vaf.core.thinking_mode as tm
    monkeypatch.setattr(tm, "emit_message_to_web_ui", lambda scope, content, session_id=None: None)

    from vaf.tools.thinking_done import ThinkingDoneTool
    scope = "user-td"
    out = ThinkingDoneTool().run(
        summary="looked at the heat note",
        message="Soll ich dir eine Erinnerung einrichten, deine Wohnung kühl zu halten?",
        proposed_action="create cooling reminder automation",
        source_note_id="note-heat",
        user_scope_id=scope,
    )
    reqs = tr.list_requests(scope, status="asked")
    assert len(reqs) == 1
    assert reqs[0]["source_note_id"] == "note-heat"
    assert reqs[0]["proposed_action"] == "create cooling reminder automation"
    assert reqs[0]["id"] in out and "looked at the heat note" in out
    waiting = tm.get_waiting_for_reply(scope)
    assert waiting and waiting.get("request_id") == reqs[0]["id"]


def test_thinking_done_does_not_double_send_after_ask_user(monkeypatch, tmp_path):
    """If ask_user already raised a request this run, a trailing thinking_done(message=...) must NOT send
    a second message (the run_has_open_request guard)."""
    from vaf.core.platform import Platform
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("VAF_THINKING_MODE", "1")
    import vaf.core.thinking_mode as tm
    sent = []
    monkeypatch.setattr(tm, "emit_message_to_web_ui", lambda scope, content, session_id=None: (sent.append(content), "sid-1")[1])

    from vaf.tools.ask_user import AskUserTool
    from vaf.tools.thinking_done import ThinkingDoneTool
    scope = "user-dbl"
    tm.set_proactive_mode(scope, "open")   # allow the first free message; then test the double-send guard
    AskUserTool().run(message="Erste Frage?", user_scope_id=scope)
    assert len(tr.list_requests(scope, status="asked")) == 1
    out = ThinkingDoneTool().run(message="Zweite Frage?", user_scope_id=scope)
    assert len(tr.list_requests(scope)) == 1           # still only the ask_user request
    assert "already delivered" in out
    assert sent == ["Erste Frage?"]                    # the second message was suppressed


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
