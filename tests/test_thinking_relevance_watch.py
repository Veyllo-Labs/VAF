# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The relevance rung: does anything current CHANGE something this user has planned?

Its value is impact, not information, and its dominant failure mode is degenerating into a news
ticker. Four properties hold it in place, all enforced in code:

1. Its message is an FYI, not a question. A tracked question arms a three-minute nudge and is
   re-asked up to three times when nobody replies - one notice would become up to eight touches.
2. Web results reach the run's evidence pool ONLY on this rung, so "grounded" keeps meaning
   "grounded in the user's own memory" everywhere else.
3. Two brakes: a cooldown, and a self-disable once its notices are being DECLINED (not merely
   unanswered - an FYI is never answered, so that would switch it off on its normal behaviour).
4. A memory outage does not silently degrade the assistant to small talk - an empty retrieval and an
   unreachable database mean opposite things and are told apart once per run."""
import types
from datetime import datetime, timedelta

import vaf.core.thinking_mode as tm
import vaf.core.thinking_requests as tr
from vaf.core.agent import Agent
from vaf.core.platform import Platform


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path))


def _deliverable(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(tm, "run_has_open_request", lambda scope: False)
    monkeypatch.setattr(tm, "_main_agent_busy", lambda scope: False)
    monkeypatch.setattr(tm, "_question_too_similar", lambda scope, msg: False)
    monkeypatch.setattr(tm, "emit_message_to_web_ui", lambda scope, content, session_id=None: "sid-web")
    import vaf.core.messaging_connections as mc
    monkeypatch.setattr(mc, "send_to_main_messenger", lambda scope, uname, text, record=True: (False, None))


# ── 1. an FYI is delivered, not awaited ───────────────────────────────────────────────────

def test_a_relevance_notice_does_not_arm_the_nudge(monkeypatch, tmp_path):
    _deliverable(monkeypatch, tmp_path)
    waited = {"n": 0}
    monkeypatch.setattr(tm, "set_waiting_for_reply", lambda *a, **kw: waited.__setitem__("n", waited["n"] + 1))
    scope = "u-fyi"
    tm.clear_run_evidence(scope)
    tm.set_proactive_mode(scope, "open")
    tm.set_message_kind(scope, "relevance")
    req = tm.deliver_tracked_message(scope, "Dein Zug am 14. faellt aus (Quelle: example.org, 2026-08-28).")
    assert req and req.get("kind") == "relevance"
    assert waited["n"] == 0, "an FYI must not set the waiting state - that is what arms the 3-minute nudge"


def test_an_ordinary_question_still_arms_it(monkeypatch, tmp_path):
    _deliverable(monkeypatch, tmp_path)
    waited = {"n": 0}
    monkeypatch.setattr(tm, "set_waiting_for_reply", lambda *a, **kw: waited.__setitem__("n", waited["n"] + 1))
    scope = "u-q"
    tm.clear_run_evidence(scope)
    tm.set_proactive_mode(scope, "open")
    tm.set_message_kind(scope, "")
    tm.deliver_tracked_message(scope, "Soll ich dir das einrichten?")
    assert waited["n"] >= 1


def test_a_relevance_notice_is_never_followed_up(monkeypatch, tmp_path):
    """`get_open_proactive_request` is what makes the next runs re-ask an unanswered question."""
    _isolate(monkeypatch, tmp_path)
    scope = "u-nofu"
    tr.add_request(scope, "Dein Zug am 14. faellt aus.", run_seq=1, kind="relevance")
    assert tr.get_open_proactive_request(scope, current_run_seq=1, within_runs=6) is None
    tr.add_request(scope, "Soll ich dir das einrichten?", run_seq=1)
    picked = tr.get_open_proactive_request(scope, current_run_seq=1, within_runs=6)
    assert picked and picked["question"].startswith("Soll ich")


def test_the_message_kind_does_not_leak_to_the_next_rung(monkeypatch, tmp_path):
    """The relevance rung usually falls through. If its kind survived, the get-to-know question
    that follows would be filed as an FYI and would never be followed up."""
    _isolate(monkeypatch, tmp_path)
    scope = "u-leak"
    tm.set_message_kind(scope, "relevance")
    tm.clear_run_evidence(scope)                 # run boundary
    assert tm.get_message_kind(scope) == ""


# ── 2. web evidence is pooled on this rung only ───────────────────────────────────────────

def _pool_predicate(node, function_name):
    """The product's rule, expressed exactly as the tool loop applies it."""
    return (function_name == "memory_search"
            or (function_name == "web_search" and node == "relevance"))


def test_web_results_are_pooled_on_the_relevance_rung():
    assert _pool_predicate("relevance", "web_search") is True


def test_web_results_are_not_pooled_on_the_other_rungs():
    for node in ("proactive", "automation_review", "getto", "forced_item", ""):
        assert _pool_predicate(node, "web_search") is False, (
            f"a web result on node {node!r} would let a message claim to be 'grounded in the user's "
            "memory' while quoting a web page")


def test_memory_results_are_pooled_everywhere():
    for node in ("relevance", "proactive", "getto", ""):
        assert _pool_predicate(node, "memory_search") is True


# ── 3. the two brakes ─────────────────────────────────────────────────────────────────────

def _cfg(monkeypatch, **over):
    import vaf.core.config as cfg
    orig = cfg.Config.get
    monkeypatch.setattr(cfg.Config, "get",
                        staticmethod(lambda k, d=None: over[k] if k in over else orig(k, d)))


def test_disabled_by_config(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _cfg(monkeypatch, thinking_relevance_enabled=False)
    assert tm.relevance_watch_allowed("u-off") == (False, "disabled")


def test_cooldown_blocks_a_second_notice(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _cfg(monkeypatch, thinking_relevance_enabled=True, thinking_relevance_cooldown_hours=72)
    scope = "u-cool"
    tr.add_request(scope, "Dein Zug faellt aus.", run_seq=1, kind="relevance")
    allowed, why = tm.relevance_watch_allowed(scope)
    assert (allowed, why) == (False, "cooldown")


def test_an_old_notice_does_not_block(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _cfg(monkeypatch, thinking_relevance_enabled=True, thinking_relevance_cooldown_hours=1)
    scope = "u-cool2"
    e = tr.add_request(scope, "Dein Zug faellt aus.", run_seq=1, kind="relevance")
    old = (datetime.now() - timedelta(hours=5)).isoformat()
    tr._save(tr._path(scope), [{**e, "created_at": old}])
    assert tm.relevance_watch_allowed(scope)[0] is True


def test_the_rung_disables_itself_when_its_notices_are_declined(monkeypatch, tmp_path):
    """Two explicit rejections out of the last ten and it stops on its own, rather than waiting for
    someone to find a setting."""
    _isolate(monkeypatch, tmp_path)
    _cfg(monkeypatch, thinking_relevance_enabled=True, thinking_relevance_cooldown_hours=0)
    scope = "u-selfoff"
    entries = []
    for i in range(10):
        e = tr.add_request(scope, f"Notiz {i}", run_seq=1, kind="relevance")
        e = {**e, "status": "declined" if i < 2 else "done",
             "created_at": (datetime.now() - timedelta(hours=10 + i)).isoformat()}
        entries.append(e)
    tr._save(tr._path(scope), entries)
    allowed, why = tm.relevance_watch_allowed(scope)
    assert (allowed, why) == (False, "self_disabled")


def test_unanswered_notices_do_not_disable_the_rung(monkeypatch, tmp_path):
    """The bug this pins: an FYI is never replied to, so it stays at status 'asked' forever. Counting
    that as rejection would have switched the rung off permanently after ten perfectly good notices -
    on exactly the behaviour it is designed for."""
    _isolate(monkeypatch, tmp_path)
    _cfg(monkeypatch, thinking_relevance_enabled=True, thinking_relevance_cooldown_hours=0)
    scope = "u-unanswered"
    entries = []
    for i in range(10):
        e = tr.add_request(scope, f"Notiz {i}", run_seq=1, kind="relevance")
        entries.append({**e, "status": "asked",      # never replied to - the NORMAL case
                        "created_at": (datetime.now() - timedelta(hours=10 + i)).isoformat()})
    tr._save(tr._path(scope), entries)
    assert tm.relevance_watch_allowed(scope)[0] is True


def test_it_stays_on_while_its_notices_land(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _cfg(monkeypatch, thinking_relevance_enabled=True, thinking_relevance_cooldown_hours=0)
    scope = "u-selfon"
    entries = []
    for i in range(10):
        e = tr.add_request(scope, f"Notiz {i}", run_seq=1, kind="relevance")
        entries.append({**e, "status": "done",
                        "created_at": (datetime.now() - timedelta(hours=10 + i)).isoformat()})
    tr._save(tr._path(scope), entries)
    assert tm.relevance_watch_allowed(scope)[0] is True


def test_an_unreadable_request_store_does_not_silence_the_rung(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _cfg(monkeypatch, thinking_relevance_enabled=True)

    def boom(*a, **kw):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(tr, "list_requests", boom)
    assert tm.relevance_watch_allowed("u-err") == (True, "ok")


# ── 4. an empty memory and an unreachable one are not the same thing ──────────────────────

def test_memory_status_reports_unavailable_when_the_probe_fails(monkeypatch):
    import vaf.memory.database as db
    monkeypatch.setattr(db, "check_db_connection_sync", lambda timeout_seconds=5.0: False)
    assert tm._memory_status("u1") == "unavailable"


def test_memory_status_reports_empty_when_the_database_answers(monkeypatch):
    import vaf.memory.database as db
    monkeypatch.setattr(db, "check_db_connection_sync", lambda timeout_seconds=5.0: True)
    assert tm._memory_status("u1") == "empty"


def test_memory_disabled_is_empty_not_unavailable(monkeypatch):
    _cfg(monkeypatch, memory_enabled=False)
    assert tm._memory_status("u1") == "empty"


# ── the prompt keeps the rung from becoming a newsletter ──────────────────────────────────

def test_prompt_makes_silence_the_normal_outcome():
    p = tm._PROMPT_RELEVANCE
    assert "normal outcome" in p
    assert "Staying quiet is a correct result" in p
    assert "is NOT an output" in p                       # a digest is a failure, not a result
    assert "Never speculate" in p


def test_prompt_states_the_query_rule():
    """The query goes to a third-party search engine. The user chose a prompt rule over a mechanical
    filter; the rule must therefore at least be unambiguous about what may not go into it."""
    p = tm._PROMPT_RELEVANCE
    assert "outside search engine" in p
    assert "a query a stranger could have typed" in p
    for forbidden in ("name", "employer", "internal project name", "email address"):
        assert forbidden in p


def test_prompt_requires_a_verifiable_source():
    p = tm._PROMPT_RELEVANCE
    assert "source URL" in p and "date" in p
    assert "the exact search query you ran" in p         # what was searched stays visible to the user


# ── the rung's nudge ──────────────────────────────────────────────────────────────────────

def test_the_relevance_nudge_offers_silence_as_a_first_class_option():
    ns = types.SimpleNamespace(_run_kind="thinking", _thinking_node="relevance",
                               _thinking_read_counts={}, _force_tool_choice="required",
                               _force_tool_choice_used=False)
    ns._is_thinking_run = types.MethodType(Agent._is_thinking_run, ns)
    ns._forcing_this_generation = types.MethodType(Agent._forcing_this_generation, ns)
    blocked = Agent._thinking_read_cap_step(ns, "web_search")
    assert blocked and blocked.startswith("[BLOCKED]")
    assert "thinking_done" in blocked
    assert "normal and correct outcome" in blocked
