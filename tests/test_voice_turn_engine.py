# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The voice turn pipeline's contracts, unit-tested for the first time.

Until the extraction (vaf/core/voice_turn.py) these behaviors lived inline in the
WebSocket handler and could only be observed through a live call: the speaker_ok
DERIVATION with its sticky window (VOICE_AGENT.md invariant 1 - test_voice_agent.py
only covered the marker drop inside voice_reply), the busy_local belt with its
server-side sub-agent truth (invariant 10), the gate-to-outcome wiring (invariant 8),
the arm gate for guest engagement, and the pending-question lifecycle. Each test
names the mutation that turns it red; time is INJECTED (clock seam), never slept.

The wire-facing twin is tests/test_voice_call_baseline.py (frozen shapes through the
real handler); this file drives the engine directly through its seams.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from vaf.core.voice_turn import VoiceTurnEngine, _HISTORY_MAX_CHARS


SCOPE = "deadbeef-0000-0000-0000-000000000000"
# Long but NOT repetitive: the real looks_garbled heuristic runs in these tests
# and flags token repetition as STT garbage (unique tokens must exceed half the
# total - found the hard way twice this round, first with a repeated phrase,
# then with a template that only varied its digits).
LONG = ("also ich wollte nochmal ausfuehrlich erzaehlen was diese woche alles "
        "passiert ist zuerst hatten wir montag das lange gespraech mit dem "
        "architekten ueber den umbau dann kam dienstag die lieferung der neuen "
        "kuechengeraete leider fehlte ein teil weshalb mittwoch jemand vom "
        "kundendienst vorbeikommen musste donnerstag habe ich endlich den "
        "vertrag unterschrieben und freitag sind wir abends noch essen gegangen "
        "um alles zu feiern samstag war dann grosser hausputz angesagt weil "
        "sonntag ueberraschend besuch aus muenchen angekommen ist")


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def env(monkeypatch):
    """The engine's world, pinned: module seams like the baseline driver, plus the
    injectable ports. Everything routed, nothing modeled."""
    import vaf.core.voice_agent as va
    import vaf.core.voice_policy as vp
    import vaf.core.speaker_id as sid
    from vaf.core.config import Config

    e: Dict[str, Any] = {
        "exclusive": False, "active_s": 2.0, "stt": ("hallo wie geht es dir", "de"),
        "profile": None, "score": None, "resolve": None,
        "reply": {"reply": "Hallo!", "delegate": None, "silent": False},
        "chime_decision": {"speak": False, "mode": "quiet", "scene": "multi",
                           "score": 0.0, "interesting": False, "trigger": None},
        "chime": "", "similar": False, "answer": None,
        "confirm_calls": [], "logs": [],
    }
    monkeypatch.setattr(va, "is_exclusive", lambda: e["exclusive"])
    monkeypatch.setattr(va, "active_speech_seconds", lambda wav: e["active_s"])
    monkeypatch.setattr(va, "voice_reply", lambda *a, **kw: (e["reply"], e.setdefault("reply_kwargs", kw))[0])
    monkeypatch.setattr(va, "chime_in_reply", lambda *a, **kw: e["chime"])
    monkeypatch.setattr(vp, "chime_decision", lambda *a, **kw: e["chime_decision"])
    monkeypatch.setattr(vp, "similar_to_any", lambda *a, **kw: e["similar"])
    if hasattr(vp, "answer_verdict"):
        monkeypatch.setattr(vp, "answer_verdict",
                            lambda *a, **kw: e["answer"] or {"verdict": vp.CONTINUE,
                                                             "reason": "non_owner",
                                                             "guest": False})
    monkeypatch.setattr(sid, "is_enabled", lambda: e["profile"] is not None)
    monkeypatch.setattr(sid, "load_profile", lambda scope: e["profile"])
    monkeypatch.setattr(sid, "score_wav", lambda wav, scope: e["score"])
    monkeypatch.setattr(sid, "resolve_label",
                        lambda score, sticky_self=False:
                        e["resolve"](score, sticky_self) if callable(e["resolve"])
                        else (e["resolve"] or {"label": None, "speaker_ok": True,
                                               "confident": None}))
    monkeypatch.setattr(sid, "label_prefix", lambda res, display: f"[{res.get('label')}]: ")

    real_get = Config.get
    cfg = {"voice_awareness_topics": [], "voice_awareness_activity": 0.5}
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: cfg.get(key, real_get(key, default))))
    e["cfg"] = cfg
    return e


def make_engine(e, clock: Optional[Clock] = None) -> VoiceTurnEngine:
    state = {"history": [], "lang": "de", "scope": SCOPE, "chat_context": "",
             "agent_name": "Nobel", "agent_soul": "", "session": "s1",
             "chime_recent": []}
    eng = VoiceTurnEngine(
        state,
        transcribe=lambda wav, **kw: e["stt"],
        lane_speaks=lambda lang: True,
        subagents_busy=lambda: e.get("subagents", False),
        request_confirmation=lambda *a, **kw: e["confirm_calls"].append((a, kw)),
        clock=clock or Clock(),
        log=e["logs"].append,
    )
    e["state"] = state
    return eng


def turn(eng, **kw):
    kw.setdefault("session_id", "")
    kw.setdefault("main_busy", False)
    kw.setdefault("pending_task", "")
    kw.setdefault("username", "tenant")
    return eng.turn(b"WAV", **kw)


# ── invariant 10: the busy_local belt, server-side truth included ─────────────

def test_busy_belt_matrix(env):
    """Mutation: move the belt below the noise gate - the gated cases stop being
    busy_local and the frozen baseline's busy scenarios go red too."""
    eng = make_engine(env)
    env["exclusive"] = True
    assert turn(eng, main_busy=True).kind == "busy_local"
    env["subagents"] = True
    assert turn(eng, main_busy=False).kind == "busy_local", \
        "a live sub-agent holds the one model even when the frontend flag is clear"
    env["subagents"] = False
    assert turn(eng, main_busy=False).kind == "reply"
    env["exclusive"] = False
    env["subagents"] = True
    assert turn(eng, main_busy=False).kind == "reply", \
        "non-exclusive lanes never consult the belt"


def test_busy_belt_probe_failure_fails_open(env):
    """A broken IPC probe must not mute the call (observation, not a gate).
    Mutation: let the exception propagate or default True - red."""
    eng = make_engine(env)
    env["exclusive"] = True

    def boom():
        raise RuntimeError("ipc down")

    eng._subagents_busy = boom
    assert turn(eng).kind == "reply"


# ── early exits write NOTHING ─────────────────────────────────────────────────

def test_gated_turns_leave_no_trace(env):
    """Mutation: add a history append to the gate path - red. The asymmetry
    (exits before STT never touch state) is what keeps noise turns free."""
    eng = make_engine(env)
    env["active_s"] = 0.1
    out = turn(eng)
    assert out.kind == "no_speech" and out.error == "no_speech"
    assert env["state"]["history"] == []
    assert "pending_q" not in env["state"]

    env["active_s"] = 2.0
    env["stt"] = ("", "de")
    assert turn(eng).kind == "no_speech"
    assert env["state"]["history"] == []


def test_llm_failure_writes_nothing(env):
    eng = make_engine(env)
    env["reply"] = None
    out = turn(eng)
    assert out.kind == "llm_failed" and out.error == "llm_failed"
    assert env["state"]["history"] == []


# ── the truncation table (measured status quo) ────────────────────────────────

def test_history_entries_share_one_cap(env):
    """ONE cap (800) for every stored entry, applied inside _history_add - it
    matches what voice_reply reads anyway (it caps entries at 800 building the
    prompt). Replaced the old 200/400/uncapped asymmetry in a deliberate,
    baseline-regenerating change. Mutation: cap at a call site instead of inside
    _history_add, or change the constant - red here AND in the baseline."""
    assert _HISTORY_MAX_CHARS == 800
    # Above the cap WITHOUT repeating tokens (looks_garbled runs real and flags
    # repetition - this line is the third time this round that heuristic bit).
    hyper = LONG + (" ausserdem wollte ich erwaehnen dass der nachbar gestern das "
                    "geruest abgebaut hat und die einfahrt endlich wieder frei ist "
                    "der maler kommt uebrigens erst naechsten monat weil sein "
                    "lieferant die spezielle silikatfarbe nicht rechtzeitig "
                    "beschaffen konnte was mich ehrlich gesagt ziemlich nervt "
                    "trotzdem bleibt der zeitplan fuer das dach unveraendert")

    # normal reply: capped at 800 now (was uncapped)
    eng = make_engine(env)
    env["stt"] = (hyper, "de")
    assert turn(eng).kind == "reply"
    assert len(env["state"]["history"][-2]["content"]) == _HISTORY_MAX_CHARS

    # model silence: same cap (was 400)
    eng2 = make_engine(env)
    env["reply"] = {"reply": "<silent/>", "delegate": None, "silent": True}
    assert turn(eng2).kind == "silent"
    assert len(env["state"]["history"][-1]["content"]) == _HISTORY_MAX_CHARS

    # side-talk silence: same cap (was 200)
    eng3 = make_engine(env)
    env["reply"] = {"reply": "Hallo!", "delegate": None, "silent": False}
    env["profile"] = {"meta": {"display_name": "Alice"}}
    env["resolve"] = {"label": "other", "speaker_ok": False, "confident": "other"}
    env["stt"] = ("wir gehen dann gleich einkaufen ja " * 40, "de")
    out = turn(eng3)
    assert out.kind == "silent"
    assert len(env["state"]["history"][-1]["content"]) == _HISTORY_MAX_CHARS


# ── invariant 1: the speaker_ok DERIVATION with the sticky window ─────────────

def test_sticky_window_via_injected_clock(env):
    """A confident self verifies; a borderline clip inside STICKY_WINDOW_S stays
    the owner; a clear other flips immediately and BREAKS the window.
    Mutation: drop the `elif confident == "other"` reset - the third phase keeps
    speaker_ok True for a stranger = the spoofing hole."""
    import vaf.core.speaker_id as sid
    clock = Clock(1000.0)
    eng = make_engine(env, clock)
    env["profile"] = {"meta": {"display_name": "Alice"}}

    seen_sticky: List[bool] = []

    def resolver(score, sticky_self):
        seen_sticky.append(sticky_self)
        if env.get("phase") == "self":
            return {"label": "self", "speaker_ok": True, "confident": "self"}
        if env.get("phase") == "borderline":
            return {"label": "self" if sticky_self else "unsure",
                    "speaker_ok": bool(sticky_self), "confident": "borderline"}
        return {"label": "other", "speaker_ok": False, "confident": "other"}

    env["resolve"] = resolver

    env["phase"] = "self"
    assert turn(eng).speaker_ok is True
    assert env["state"]["last_self_ts"] == 1000.0

    clock.t += min(30.0, sid.STICKY_WINDOW_S / 2)
    env["phase"] = "borderline"
    assert turn(eng).speaker_ok is True, "inside the window a borderline clip stays the owner"
    assert seen_sticky[-1] is True

    env["phase"] = "other"
    turn(eng)
    assert env["state"]["last_self_ts"] is None, "a clear stranger breaks the window"

    env["phase"] = "borderline"
    assert turn(eng).speaker_ok is False, "after the break, borderline is a guest again"


def test_sticky_window_expires(env):
    import vaf.core.speaker_id as sid
    clock = Clock(1000.0)
    eng = make_engine(env, clock)
    env["profile"] = {"meta": {"display_name": "Alice"}}

    def resolver(score, sticky_self):
        if env.get("phase") == "self":
            return {"label": "self", "speaker_ok": True, "confident": "self"}
        return {"label": "self" if sticky_self else "unsure",
                "speaker_ok": bool(sticky_self), "confident": "borderline"}

    env["resolve"] = resolver
    env["phase"] = "self"
    turn(eng)
    clock.t += sid.STICKY_WINDOW_S + 1.0
    env["phase"] = "borderline"
    assert turn(eng).speaker_ok is False, "the window is a WINDOW, not a latch"


def test_non_owner_turn_requests_confirmation(env):
    eng = make_engine(env)
    env["profile"] = {"meta": {"display_name": "Alice"}}
    env["resolve"] = {"label": "other", "speaker_ok": False, "confident": "other"}
    env["stt"] = ("wir gehen gleich einkaufen ja", "de")
    turn(eng, session_id="sess-9")
    assert len(env["confirm_calls"]) == 1
    assert env["confirm_calls"][0][1]["session_id"] == "sess-9"


# ── the arm gate: speaker_ok AND confident != borderline ─────────────────────

def test_bridged_borderline_may_speak_but_never_arm(env):
    """Mutation: loosen the arm gate to plain speaker_ok - the borderline case
    arms engagement and a short clip right after the owner can turn the mode on."""
    eng = make_engine(env)
    env["profile"] = {"meta": {"display_name": "Alice"}}
    env["reply"] = {"reply": "Klar.", "delegate": None, "silent": False,
                    "engage_guest": True}
    # bridged borderline: speaks as owner, must NOT arm
    env["resolve"] = {"label": "self", "speaker_ok": True, "confident": "borderline"}
    out = turn(eng)
    assert out.kind == "reply" and out.speaker_ok is True
    assert "engage_guests" not in env["state"]
    # real self: arms
    env["resolve"] = {"label": "self", "speaker_ok": True, "confident": "self"}
    turn(eng)
    assert "engage_guests" in env["state"]


def test_guest_can_never_arm(env):
    eng = make_engine(env)
    env["profile"] = {"meta": {"display_name": "Alice"}}
    env["resolve"] = {"label": "other", "speaker_ok": False, "confident": "other"}
    env["reply"] = {"reply": "Ok!", "delegate": None, "silent": False,
                    "engage_guest": True}
    env["answer"] = {"verdict": "continue", "reason": "non_owner", "guest": False}
    env["stt"] = ("bitte antworte ihr doch mal", "de")
    turn(eng)
    assert "engage_guests" not in env["state"]


# ── pending question lifecycle ────────────────────────────────────────────────

def test_owner_question_arms_pending_q_and_guest_reply_does_not_clear_it(env):
    """Owner-only in BOTH directions. Mutation: remove the speaker_ok gate on the
    arm - a guest reply would then rewrite the owner's pending question."""
    import vaf.core.voice_policy as vp
    clock = Clock(1000.0)
    eng = make_engine(env, clock)
    env["reply"] = {"reply": "Soll ich das Licht ausmachen?", "delegate": None,
                    "silent": False}
    assert turn(eng).kind == "reply"
    assert env["state"]["pending_q"]["text"] == "Soll ich das Licht ausmachen?"
    assert env["state"]["pending_q"]["turns_left"] == vp.PENDING_Q_TURNS

    # guest ANSWER: spoken reply, question stays open (budget decremented)
    env["profile"] = {"meta": {"display_name": "Alice"}}
    env["resolve"] = {"label": "other", "speaker_ok": False, "confident": "other"}
    env["answer"] = {"verdict": vp.ANSWER, "reason": "guest_on_topic", "guest": True}
    env["reply"] = {"reply": "Gern!", "delegate": None, "silent": False}
    out = turn(eng)
    assert out.kind == "reply"
    assert "pending_q" in env["state"], "the OWNER has not answered - the question stays open"


def test_non_question_owner_reply_clears_stale_pending_q(env):
    eng = make_engine(env)
    env["state"]["pending_q"] = {"text": "alt?", "asked_at": 999.0,
                                 "turns_left": 2, "reask_count": 0}
    env["answer"] = {"verdict": "continue", "reason": "owner_side_talk", "guest": False}
    env["reply"] = {"reply": "Alles klar, erledigt.", "delegate": None, "silent": False}
    turn(eng)
    assert "pending_q" not in env["state"]


# ── recheck: cooldown and pending expiry via the clock ────────────────────────

def test_recheck_cooldown_prevents_nagging(env):
    """Mutation: flip the cooldown comparison - the second ambiguous turn asks
    again immediately and the agent nags."""
    import vaf.core.voice_agent as va
    clock = Clock(1000.0)
    eng = make_engine(env, clock)
    env["profile"] = {"meta": {"display_name": "Alice"}}
    env["resolve"] = {"label": "unsure", "speaker_ok": False, "confident": None}
    env["stt"] = ("kannst du bitte kurz antworten", "de")

    out1 = turn(eng)
    assert out1.kind == "clarify" and env["state"]["pending_speaker_check"]
    # drop the pending check (simulate an expired answer window), stay in cooldown
    env["state"].pop("pending_speaker_check")
    clock.t += 10.0
    out2 = turn(eng)
    assert out2.kind != "clarify", "inside the cooldown the agent must not re-ask"
    clock.t += 60.0
    out3 = turn(eng)
    assert out3.kind == "clarify", "after the cooldown a directed unsure turn asks again"


def test_recovered_owner_drops_the_pending_check(env):
    clock = Clock(1000.0)
    eng = make_engine(env, clock)
    env["profile"] = {"meta": {"display_name": "Alice"}}
    env["state"]["pending_speaker_check"] = {"text": "x", "asked_at": clock.t}
    env["resolve"] = {"label": "self", "speaker_ok": True, "confident": "self"}
    out = turn(eng)
    assert out.kind == "reply"
    assert "pending_speaker_check" not in env["state"]


# ── chime dedup + guest digest privacy ────────────────────────────────────────

def test_chime_dedup_ring(env):
    """Mutation: skip the similar_to_any check - the same remark chimes twice."""
    eng = make_engine(env)
    env["profile"] = {"meta": {"display_name": "Alice"}}
    env["resolve"] = {"label": "other", "speaker_ok": False, "confident": "other"}
    env["stt"] = ("die bundesliga war gestern spannend " * 4, "de")
    env["chime_decision"] = {"speak": True, "mode": "quiet", "scene": "multi",
                             "score": 0.9, "interesting": True, "trigger": None}
    env["chime"] = "Interessant!"
    assert turn(eng).kind == "chime_in"
    assert env["state"]["chime_recent"] == ["Interessant!"]
    env["similar"] = True
    assert turn(eng).kind == "silent", "a repeated remark stays silent"


def test_second_start_replaces_engine_and_state():
    """The registry contract: a second voice_call_start builds a NEW record and a
    NEW engine - nothing merges. Mutation: merge instead of replace - stale
    pending state survives into the new call."""
    old = {"history": [{"role": "user", "content": "alt"}], "lang": "de",
           "scope": SCOPE, "session": "s1", "chime_recent": ["x"],
           "pending_q": {"text": "alt?"}}
    new_state = {"history": [], "lang": "de", "scope": SCOPE, "session": "s1",
                 "chime_recent": []}
    eng = VoiceTurnEngine(new_state)
    assert eng.state is new_state
    assert eng.state["history"] == [] and "pending_q" not in eng.state
    assert old["pending_q"], "the old dict is simply abandoned, never merged"


# ── notes ─────────────────────────────────────────────────────────────────────

def test_note_greeting_and_spoken_caps(env):
    eng = make_engine(env)
    eng.note_greeting("Hallo!")
    assert env["state"]["history"][-1] == {"role": "assistant", "content": "Hallo!"}
    eng.note_spoken("R" * 2000)
    assert len(env["state"]["history"][-1]["content"]) == _HISTORY_MAX_CHARS
