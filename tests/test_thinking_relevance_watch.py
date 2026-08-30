# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The relevance rung: does anything current CHANGE something this user has planned?

Its value is impact, not information, and its dominant failure mode is degenerating into a news
ticker. Four properties hold it in place, all enforced in code:

1. Its message is an FYI, not a question: recorded so the main agent can pick up a reply, but with
   the chase ended, so it is never nudged and never re-asked. Those two are separate and were once
   confused here - skipping the record to avoid the nudge also took away the only thing that tells
   the main agent what a later reply refers to.
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

def test_a_relevance_notice_is_recorded_and_marked_as_not_awaited(monkeypatch, tmp_path):
    """Both halves at once, because the first version of this rung got them confused.

    It skipped the waiting record entirely so an FYI would not be nudged - and thereby took away
    the ONLY thing that tells the main agent what a later reply refers to. The record and the
    chase are separate: the record says what was sent, `chase_ended_at_ts` says nobody is waiting
    on an answer."""
    _deliverable(monkeypatch, tmp_path)
    scope = "u-fyi"
    tm.clear_run_evidence(scope)
    tm.set_proactive_mode(scope, "open")
    tm.set_message_kind(scope, "relevance")
    req = tm.deliver_tracked_message(scope, "Dein Zug am 14. faellt aus (Quelle: example.org, 2026-08-28).")
    assert req and req.get("kind") == "relevance"

    w = tm.get_waiting_for_reply(scope)
    assert w and "Dein Zug am 14." in (w.get("question_text") or ""), \
        "the main agent would have nothing to connect a reply to"
    assert tm.chase_is_active(w) is False, "an FYI must not be nudged after three minutes"


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
                               _force_tool_choice_used=False, _forced_round=False)
    ns._is_thinking_run = types.MethodType(Agent._is_thinking_run, ns)
    ns._forcing_this_generation = types.MethodType(Agent._forcing_this_generation, ns)
    ns._take_forced_tool_choice = types.MethodType(Agent._take_forced_tool_choice, ns)
    ns._take_forced_tool_choice(["web_search"])   # the forced REQUEST is built first
    blocked = Agent._thinking_read_cap_step(ns, "web_search")
    assert blocked and blocked.startswith("[BLOCKED]")
    assert "thinking_done" in blocked
    assert "normal and correct outcome" in blocked


# ── 5. EVERY background message leaves context for the main agent ─────────────────────────

def test_an_fyi_still_leaves_a_record_for_the_main_agent(monkeypatch, tmp_path):
    """The invariant, stated by the owner and violated by the first version of this rung:
    whenever the background pass writes into the user's chat, the main agent must be able to
    tell what a later reply refers to - it is the one that becomes active there.

    Live 2026-08-30: the run sent a researched notice about Anthropic rate limits, the user
    answered "Okay das waren jetzt viele Infos auf einmal :D", and the main agent - with no
    record to connect that to - called its OWN notice "nur interne System-Infos ... es gibt
    nichts zu tun"."""
    _deliverable(monkeypatch, tmp_path)
    scope = "u-fyi-ctx"
    tm.clear_run_evidence(scope)
    tm.set_proactive_mode(scope, "open")
    tm.set_message_kind(scope, "relevance")
    tm.deliver_tracked_message(scope, "Dein Zug am 14. faellt aus (Quelle: example.org).")

    w = tm.get_waiting_for_reply(scope)
    assert w, "an FYI left NO record - a reply to it reaches an agent that knows nothing"
    assert "Dein Zug am 14." in (w.get("question_text") or ""), "the record does not carry what was sent"


def test_but_nobody_chases_an_fyi(monkeypatch, tmp_path):
    """The other half, and the reason the record was skipped in the first place. The two are
    separable, and this module already separates them: the record says WHAT was sent,
    `chase_ended_at_ts` says nobody is waiting on an answer."""
    _deliverable(monkeypatch, tmp_path)
    scope = "u-fyi-nochase"
    tm.clear_run_evidence(scope)
    tm.set_proactive_mode(scope, "open")
    tm.set_message_kind(scope, "relevance")
    tm.deliver_tracked_message(scope, "Dein Zug am 14. faellt aus.")

    w = tm.get_waiting_for_reply(scope)
    assert tm.chase_is_active(w) is False, "an FYI would be nudged after three minutes"


def test_an_ordinary_question_is_chased(monkeypatch, tmp_path):
    _deliverable(monkeypatch, tmp_path)
    scope = "u-q-chase"
    tm.clear_run_evidence(scope)
    tm.set_proactive_mode(scope, "open")
    tm.set_message_kind(scope, "")
    tm.deliver_tracked_message(scope, "Soll ich dir das einrichten?")
    assert tm.chase_is_active(tm.get_waiting_for_reply(scope)) is True


def test_no_delivery_branch_may_skip_the_record():
    """A static guard on the invariant itself, because it is easy to break again the way it was
    broken the first time: by adding "this kind does not need a latch" to one branch. Every
    set_waiting_for_reply in the delivery path must be unconditional."""
    from pathlib import Path
    src = (Path(tm.__file__)).read_text(encoding="utf-8")
    body = src.split("def deliver_tracked_message", 1)[1].split("\ndef ", 1)[0]
    assert "if not _is_fyi:\n        set_waiting_for_reply" not in body, \
        "a delivery branch skips the record again - the main agent would lose the context"
    assert body.count("set_waiting_for_reply(") >= 3, "a delivery branch lost its record entirely"


def test_the_note_calls_a_notice_a_notice():
    """Calling it a question makes the agent hunt for an answer in a remark and, finding none,
    disown the notice."""
    note = Agent._build_reply_pickup_note("Dein Zug faellt aus.", "", "", False, "", "", "relevance")
    assert "not a question" in note
    assert "nothing to carry out" in note
    assert "Never call it an internal or system message" in note
    assert "you sent it to them on purpose" in note


# ── 6. which brake does what ──────────────────────────────────────────────────────────────

def test_the_cooldown_bounds_frequency_and_nothing_else():
    """It was 72 hours on the assumption that it also kept the same thing from being reported
    twice. It does not, and it never did - three mechanisms already do that job, so the clock was
    only ever a frequency bound, and three days is a blunt one: a finding about tomorrow would
    have waited past the event."""
    from vaf.core.config import Config
    assert Config.DEFAULTS["thinking_relevance_cooldown_hours"] == 6


def test_a_repeat_is_stopped_by_the_mechanisms_that_actually_do_that():
    """Pinned as wiring, because the temptation is to build a fourth one. All three exist:

    1. the declined-questions log, 30 days, injected as "DO NOT ask these again";
    2. the semantic dedup gate - this rung delivers in mode `grounded`, and the gate's comparison
       pool is fed from BOTH the recent requests and that declined log;
    3. the self-disable after 2 declined of the last 10."""
    from pathlib import Path
    src = Path(tm.__file__).read_text(encoding="utf-8")

    pool = src.split("def _recent_question_texts", 1)[1].split("\ndef ", 1)[0]
    assert "_load_declined" in pool, "the dedup gate no longer sees declined questions"
    assert "list_requests" in pool, "the dedup gate no longer sees recent requests"

    gate = src.split("_mode in (\"open\", \"grounded\")", 1)[1][:400]
    assert "_question_too_similar" in gate, "a grounded delivery no longer passes the dedup gate"

    allowed = src.split("def relevance_watch_allowed", 1)[1].split("\ndef ", 1)[0]
    assert 'status") or "") == "declined"' in allowed, "the self-disable no longer counts declines"


def test_the_rung_delivers_in_a_mode_the_dedup_gate_covers():
    """The link that makes point 2 true. `off` would skip the gate entirely."""
    from pathlib import Path
    src = Path(tm.__file__).read_text(encoding="utf-8")
    rung = src.split('_node = "relevance"', 1)[1][:300]
    assert 'set_proactive_mode(user_scope_id, "grounded")' in rung, \
        "the relevance rung left the mode that subjects it to the dedup gate"
