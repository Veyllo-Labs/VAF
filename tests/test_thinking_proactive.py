"""Proactive intelligence layer (Stufe 2): the evidence-gate keeps the weak model from fabricating — a
PROACTIVE suggestion (no source note/todo) is silently dropped unless its `details` quote real retrieved
memory/history this run. Plus the anti-spam rate-limit. Storage isolated per test."""
import vaf.core.thinking_mode as tm
import vaf.core.thinking_requests as tr
from vaf.core.platform import Platform


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path))


def test_evidence_grounded_verbatim_only():
    pool = "[Source 1] User asked about the weather in Berlin on 07.05.2026: 12 degrees."
    assert tm._evidence_grounded("weather in Berlin on 07.05.2026", pool, 24) is True   # verbatim quote
    assert tm._evidence_grounded("the user loves skiing in the alps", pool, 24) is False  # fabricated
    assert tm._evidence_grounded("", pool, 24) is False
    assert tm._evidence_grounded("anything", "", 24) is False
    assert tm._evidence_grounded("Berlin", pool, 24) is True     # short details must appear in full
    assert tm._evidence_grounded("Tokyo", pool, 24) is False


def test_proactive_digest_dedups_real_memories(monkeypatch, tmp_path):
    """The proactive step is handed REAL memories retrieved in code (the weak model rarely searches and the
    forced grounding turn cannot gather). The digest runs several targeted queries and dedups identical
    snippets so the model sees distinct material."""
    _isolate(monkeypatch, tmp_path)
    calls = []

    def fake_search(query, k, user_scope_id=None, caller=None):
        calls.append(query)
        # the SAME snippet for every query -> must collapse to one in the digest
        return "[Source 1] (Relevance: 90%)\nUser checks the Berlin weather every morning at 7.\n\n---"

    import vaf.memory.rag as rag
    monkeypatch.setattr(rag, "run_memory_search_sync", fake_search)
    digest = tm._build_proactive_memory_digest(agent=None, user_scope_id="u-dg")
    assert "Berlin weather every morning" in digest
    assert digest.count("Berlin weather every morning") == 1   # deduped across the queries
    assert len(calls) >= 3                                       # several targeted, distinct queries


def test_proactive_decide_nudge_replaces_housekeeping_block(monkeypatch):
    """In the proactive grounding step there is NO open item — a blocked tool must return the DECISION
    nudge (ask_user / thinking_done), not the housekeeping 'resolve the open item / delete_automation_note'
    message that misleads the weak model into searching again."""
    from types import SimpleNamespace
    from vaf.core.agent import Agent
    monkeypatch.setenv("VAF_THINKING_MODE", "1")
    # proactive step, a non-search tool is reached for -> decision nudge (no delete_automation_note)
    fake = SimpleNamespace(_thinking_force_progress=True, _thinking_allow_search=True, _thinking_read_counts={})
    msg = Agent._thinking_read_cap_step(fake, "web_search")
    assert msg and "ask_user" in msg and "thinking_done" in msg
    assert "delete_automation_note" not in msg and "web_search" in msg
    # memory_search IS allowed in the proactive step, but capped at 2 -> 1st ok, 2nd nudges to decide
    fake2 = SimpleNamespace(_thinking_force_progress=True, _thinking_allow_search=True, _thinking_read_counts={})
    assert Agent._thinking_read_cap_step(fake2, "memory_search") is None
    assert "ask_user" in (Agent._thinking_read_cap_step(fake2, "memory_search") or "")
    # housekeeping forced node (NOT proactive) keeps the original resolve-the-item message
    fake3 = SimpleNamespace(_thinking_force_progress=True, _thinking_allow_search=False, _thinking_read_counts={})
    hk = Agent._thinking_read_cap_step(fake3, "memory_search")
    assert hk and "resolve the open item" in hk and "delete_automation_note" in hk


def test_proactive_prompt_allows_one_self_search():
    """The grounded prompt now lets the model dig into ONE specific thing with memory_search itself, then
    must quote a real memory verbatim."""
    assert "memory_search" in tm._PROMPT_PROACTIVE
    assert "VERBATIM" in tm._PROMPT_PROACTIVE
    assert "thinking_done" in tm._PROMPT_PROACTIVE   # still falls back to get-to-know when nothing grounds


def test_proactive_gate_drops_ungrounded_keeps_grounded(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("VAF_THINKING_MODE", "1")
    monkeypatch.setattr(tm, "emit_message_to_web_ui", lambda scope, content: None)
    scope = "u-pg"
    tm.clear_run_evidence(scope)
    tm.set_run_evidence(scope, "[Source 1] User asks for the Berlin weather most mornings around 7am.")
    tm.set_proactive_mode(scope, "grounded")
    # ungrounded -> silently dropped (None), no request created
    assert tm.deliver_tracked_message(scope, "Soll ich X automatisieren?",
                                      proposed_action="create automation",
                                      details="the user enjoys hiking") is None
    assert tr.list_requests(scope) == []
    # grounded (verbatim quote) -> delivered + tracked
    out = tm.deliver_tracked_message(scope, "Soll ich dir das Wetter automatisch um 7 schicken?",
                                     proposed_action="create automation: daily Berlin weather 07:00",
                                     details="User asks for the Berlin weather most mornings around 7am")
    assert out is not None
    reqs = tr.list_requests(scope, status="asked")
    assert len(reqs) == 1 and reqs[0]["proposed_action"].startswith("create automation")


def test_proactive_gate_exempts_housekeeping(monkeypatch, tmp_path):
    """A delivery carrying a source_note_id/source_todo_id is housekeeping -> NOT evidence-gated."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("VAF_THINKING_MODE", "1")
    monkeypatch.setattr(tm, "emit_message_to_web_ui", lambda scope, content: None)
    scope = "u-hk"
    tm.set_proactive_mode(scope, "grounded")
    tm.set_run_evidence(scope, "")   # even in grounded mode with NO evidence
    out = tm.deliver_tracked_message(scope, "Note-Hilfe", source_note_id="n1", details="")
    assert out is not None and len(tr.list_requests(scope)) == 1


def test_free_message_blocked_in_off_mode(monkeypatch, tmp_path):
    """A FREE message (no source note/todo) outside a proactive step (mode 'off') is BLOCKED — this kills
    the turn-0 'no tasks, I'm ready when you need me' floskel that slipped through before."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("VAF_THINKING_MODE", "1")
    monkeypatch.setattr(tm, "emit_message_to_web_ui", lambda scope, content: None)
    scope = "u-np"
    tm.set_proactive_mode(scope, "off")
    assert tm.deliver_tracked_message(scope, "Alles klar, ich bin bereit wenn du was brauchst", details="") is None
    assert tr.list_requests(scope) == []


def test_get_to_know_fallback_prompt():
    """Silence is not the end: when nothing is grounded the run asks a get-to-know question (no gate)."""
    assert "ask_user" in tm._PROMPT_GET_TO_KNOW
    assert "get to know" in tm._PROMPT_GET_TO_KNOW.lower()
    assert "details" not in tm._PROMPT_GET_TO_KNOW   # a get-to-know question is NOT evidence-gated
    assert "thinking_done" in tm._PROMPT_PROACTIVE    # grounded step falls back to thinking_done -> get-to-know


def test_get_to_know_question_delivers_in_open_mode(monkeypatch, tmp_path):
    """The get-to-know step runs in mode 'open' — a question is delivered even with no `details`/evidence
    (a question states no fact, so it cannot fabricate)."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("VAF_THINKING_MODE", "1")
    monkeypatch.setattr(tm, "emit_message_to_web_ui", lambda scope, content: None)
    scope = "u-gtk"
    tm.set_proactive_mode(scope, "open")
    tm.set_run_evidence(scope, "")
    out = tm.deliver_tracked_message(scope, "Was beschäftigt dich gerade beruflich?")
    assert out is not None and len(tr.list_requests(scope)) == 1


def test_no_double_send_within_one_run(monkeypatch, tmp_path):
    """The weak model can call ask_user repeatedly inside ONE chat_step (the duplicate the user saw twice
    in the Web UI). deliver_tracked_message must emit only ONCE per run — the loop-level guard only fires
    between turns, so the dedup has to live here."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("VAF_THINKING_MODE", "1")
    sent = []
    monkeypatch.setattr(tm, "emit_message_to_web_ui",
                        lambda scope, content: (sent.append(content), "sid-1")[1])
    scope = "u-dbl2"
    tm.set_proactive_mode(scope, "open")
    a = tm.deliver_tracked_message(scope, "Was beschäftigt dich gerade?")
    b = tm.deliver_tracked_message(scope, "Habe alles verstanden! Was beschäftigt dich gerade?")
    assert a is not None and b is None                 # second suppressed
    assert len(tr.list_requests(scope)) == 1           # only one tracked request
    assert sent == ["Was beschäftigt dich gerade?"]    # only one emit to the Web UI


def test_proactive_rate_limited(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    scope = "u-rl"
    assert tm.proactive_rate_limited(scope, current_run_seq_val=10, min_runs=6) is False  # none yet
    tr.add_request(scope, "proaktiv?", run_seq=8)   # proactive (no source ids) at run 8
    assert tm.proactive_rate_limited(scope, current_run_seq_val=10, min_runs=6) is True   # 10-8 < 6
    assert tm.proactive_rate_limited(scope, current_run_seq_val=20, min_runs=6) is False  # 20-8 >= 6
    # a HOUSEKEEPING request (with a source) does NOT rate-limit proactive
    scope2 = "u-rl2"
    tr.add_request(scope2, "note?", run_seq=8, source_note_id="n1")
    assert tm.proactive_rate_limited(scope2, current_run_seq_val=10, min_runs=6) is False
