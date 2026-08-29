# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Semantic de-duplication of proactive thinking-mode questions.

The text-based "don't repeat" guards only block the same WORDING, so the model kept re-asking the same
TOPIC reworded (always "work/VAF"). The dedup gate embeds the candidate question and rejects it when it is
too close to a recently asked/declined one, pushing the model to a genuinely different area. The gate is
fail-OPEN (never lose a question to an embedding error) and carries a per-run rejection budget so a run
never ends in silence. The cutoff is DERIVED from the pool's own nearest-neighbour distribution rather than
fixed: an absolute cosine has no stable meaning across embedding models. The embedding model is never
loaded here - `_embed_question` is monkeypatched.
"""
import vaf.core.config as cfg
import vaf.core.thinking_mode as tm
import vaf.core.thinking_requests as tr
from vaf.core.platform import Platform


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path))


def _set_cfg(monkeypatch, **overrides):
    orig = cfg.Config.get

    def fake(key, default=None):
        if key in overrides:
            return overrides[key]
        return orig(key, default)

    monkeypatch.setattr(cfg.Config, "get", staticmethod(fake))


def _topic_embedder(text: str):
    """Deterministic fake: same topic -> identical unit vector (cosine 1.0); different topic -> orthogonal."""
    t = (text or "").lower()
    if "vaf" in t or "cod" in t or "arbeit" in t or "work" in t:
        return [1.0, 0.0, 0.0]
    if "hobby" in t or "sport" in t or "urlaub" in t or "kochen" in t:
        return [0.0, 1.0, 0.0]
    return [0.0, 0.0, 1.0]


# --- _cosine ---------------------------------------------------------------------------------------

def test_cosine_math():
    assert abs(tm._cosine([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) - 1.0) < 1e-6   # identical
    assert abs(tm._cosine([1.0, 0.0], [0.0, 1.0])) < 1e-6                    # orthogonal
    # non-normalized inputs must still yield cosine 1.0 (defensive normalize — MiniLM isn't normalized)
    assert abs(tm._cosine([3.0, 0.0], [5.0, 0.0]) - 1.0) < 1e-6
    assert tm._cosine([0.0, 0.0], [1.0, 1.0]) == 0.0                         # zero vector -> 0


# --- _question_too_similar -------------------------------------------------------------------------

def test_too_similar_same_topic(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(tm, "_recent_question_texts", lambda scope, seq: ["Woran arbeitest du gerade bei VAF?"])
    monkeypatch.setattr(tm, "_embed_question", _topic_embedder)
    assert tm._question_too_similar("u1", "Bist du beim VAF-Coden oder an der Architektur?") is True


def test_not_similar_different_topic(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(tm, "_recent_question_texts", lambda scope, seq: ["Woran arbeitest du gerade bei VAF?"])
    monkeypatch.setattr(tm, "_embed_question", _topic_embedder)
    assert tm._question_too_similar("u1", "Hast du ein Hobby, bei dem du richtig abschalten kannst?") is False


def test_no_recent_history_allows(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(tm, "_recent_question_texts", lambda scope, seq: [])
    monkeypatch.setattr(tm, "_embed_question", _topic_embedder)
    assert tm._question_too_similar("u1", "Irgendeine Frage über VAF?") is False


def test_disabled_flag_allows(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _set_cfg(monkeypatch, thinking_question_dedup_enabled=False)
    monkeypatch.setattr(tm, "_recent_question_texts", lambda scope, seq: ["Woran arbeitest du gerade bei VAF?"])
    monkeypatch.setattr(tm, "_embed_question", _topic_embedder)
    assert tm._question_too_similar("u1", "Noch eine VAF-Coden-Frage?") is False


def test_embedder_error_fails_open(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(tm, "_recent_question_texts", lambda scope, seq: ["Woran arbeitest du gerade bei VAF?"])

    def boom(text):
        raise RuntimeError("embedding model unavailable")

    monkeypatch.setattr(tm, "_embed_question", boom)
    # fail-OPEN: an embedding failure must never drop a question
    assert tm._question_too_similar("u1", "Noch eine VAF-Frage?") is False


# --- the gate inside deliver_tracked_message -------------------------------------------------------

def test_gate_rejects_too_similar(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(tm, "_question_too_similar", lambda scope, msg: True)
    scope = "u-gate"
    tm.set_proactive_mode(scope, "open")
    tm.set_dedup_enforce(scope, True)
    req = tm.deliver_tracked_message(scope, "Eine zu ähnliche Frage?")
    assert req is None
    assert tm.take_reject_reason(scope) == "too_similar"
    assert tr.list_requests(scope) == []   # nothing recorded


def test_gate_allows_when_not_similar(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(tm, "_question_too_similar", lambda scope, msg: False)
    monkeypatch.setattr(tm, "_main_agent_busy", lambda scope: False)
    monkeypatch.setattr(tm, "emit_message_to_web_ui",
                        lambda scope, content, session_id=None: "sid-web")
    import vaf.core.messaging_connections as mc
    monkeypatch.setattr(mc, "send_to_main_messenger", lambda scope, uname, text: (False, None))
    scope = "u-gate2"
    tm.set_proactive_mode(scope, "open")
    tm.set_dedup_enforce(scope, True)
    req = tm.deliver_tracked_message(scope, "Eine ganz frische Frage?")
    assert req and req.get("delivered") is True
    assert len(tr.list_requests(scope)) == 1


def test_gate_bypassed_when_enforce_off(monkeypatch, tmp_path):
    """The final get-to-know attempt disables enforcement so a question always lands (no silence)."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(tm, "_question_too_similar", lambda scope, msg: True)  # would reject if enforced
    monkeypatch.setattr(tm, "_main_agent_busy", lambda scope: False)
    monkeypatch.setattr(tm, "emit_message_to_web_ui",
                        lambda scope, content, session_id=None: "sid-web")
    import vaf.core.messaging_connections as mc
    monkeypatch.setattr(mc, "send_to_main_messenger", lambda scope, uname, text: (False, None))
    scope = "u-gate3"
    tm.set_proactive_mode(scope, "open")
    tm.set_dedup_enforce(scope, False)   # final-attempt bypass
    req = tm.deliver_tracked_message(scope, "Notfalls dieselbe Frage?")
    assert req and req.get("delivered") is True
    assert len(tr.list_requests(scope)) == 1


def test_duplicate_guard_does_not_set_too_similar_reason(monkeypatch, tmp_path):
    """The top duplicate guard also returns None — but must NOT be mislabeled as 'too_similar'."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(tm, "_question_too_similar", lambda scope, msg: False)
    monkeypatch.setattr(tm, "_main_agent_busy", lambda scope: False)
    monkeypatch.setattr(tm, "emit_message_to_web_ui",
                        lambda scope, content, session_id=None: "sid-web")
    import vaf.core.messaging_connections as mc
    monkeypatch.setattr(mc, "send_to_main_messenger", lambda scope, uname, text: (False, None))
    scope = "u-dup"
    tm.set_proactive_mode(scope, "open")
    tm.set_dedup_enforce(scope, True)
    first = tm.deliver_tracked_message(scope, "Erste Frage?")
    assert first and first.get("delivered") is True
    # second delivery THIS run -> top duplicate guard returns None, reason must stay empty
    second = tm.deliver_tracked_message(scope, "Zweite Frage?")
    assert second is None
    assert tm.take_reject_reason(scope) == ""


# --- ask_user.run guidance -------------------------------------------------------------------------

def test_ask_user_too_similar_guidance(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(tm, "_question_too_similar", lambda scope, msg: True)
    from vaf.tools.ask_user import AskUserTool
    scope = "u-ask"
    tm.set_proactive_mode(scope, "open")
    tm.set_dedup_enforce(scope, True)
    out = AskUserTool().run(message="Schon wieder eine VAF-Frage?", user_scope_id=scope)
    assert "too similar" in out.lower()
    assert "different area" in out.lower()


def test_ask_user_off_mode_guidance_unchanged(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    from vaf.tools.ask_user import AskUserTool
    scope = "u-ask2"
    tm.set_proactive_mode(scope, "off")   # blocks free messages -> existing guidance, not "too_similar"
    out = AskUserTool().run(message="Eine Frage?", user_scope_id=scope)
    assert "too similar" not in out.lower()
    assert "do not retry" in out.lower()


# --- _recent_question_texts ------------------------------------------------------------------------

def test_recent_question_texts_merges_dedups_caps(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    scope = "u-recent"
    tr.add_request(scope, "Frage A über VAF?", run_seq=5)
    tr.add_request(scope, "Frage B über Hobbys?", run_seq=5)
    tr.add_request(scope, "Frage A über VAF?", run_seq=5)   # exact duplicate -> collapses
    tm._save_declined_entry(scope, "Frage C declined?", "nein")
    texts = tm._recent_question_texts(scope, current_run_seq_val=5)
    assert "Frage A über VAF?" in texts
    assert "Frage B über Hobbys?" in texts
    assert "Frage C declined?" in texts
    assert len(texts) == len(set(t.lower() for t in texts))   # no exact dupes
    # cap honored
    _set_cfg(monkeypatch, thinking_question_similarity_max_compare=2)
    assert len(tm._recent_question_texts(scope, current_run_seq_val=5)) <= 2


# --- leak safety: the comparison loop accumulates NO module-level state ----------------------------

def test_no_state_accumulation_over_many_calls(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(tm, "_recent_question_texts",
                        lambda scope, seq: ["recent VAF coding question", "another work item"])
    calls = {"n": 0}

    def counting_embed(text):
        calls["n"] += 1
        return _topic_embedder(text)

    monkeypatch.setattr(tm, "_embed_question", counting_embed)
    scope = "u-leak"
    before = (len(tm._REJECT_REASON), len(tm._DEDUP_ENFORCE), len(tm._PROACTIVE_MODE))
    for _ in range(2000):
        tm._question_too_similar(scope, "yet another vaf work question")
    after = (len(tm._REJECT_REASON), len(tm._DEDUP_ENFORCE), len(tm._PROACTIVE_MODE))
    # _question_too_similar must not grow any module-level structure
    assert after == before
    # bounded embed work per call: 1 candidate + 2 recent = 3
    assert calls["n"] == 2000 * 3


def test_clear_run_evidence_reclaims_dedup_state(monkeypatch, tmp_path):
    """The per-scope dedup dicts (which DO grow via the setters) are reclaimed at run setup/teardown so
    they never accumulate across runs/scopes."""
    _isolate(monkeypatch, tmp_path)
    scope = "u-reclaim"
    tm.set_dedup_enforce(scope, True)
    tm.set_reject_reason(scope, "too_similar")
    tm.set_proactive_mode(scope, "open")
    tm.clear_run_evidence(scope)
    k = tm._key(scope)
    assert k not in tm._DEDUP_ENFORCE
    assert k not in tm._REJECT_REASON
    assert k not in tm._PROACTIVE_MODE


# --- the retry budget, spent where the retry actually happens (no-silence guarantee) --------------

def _deliverable(monkeypatch, tmp_path):
    """A scope whose delivery path is fully stubbed, so the ONLY thing that can stop a message
    is the dedup gate under test."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(tm, "run_has_open_request", lambda scope: False)
    monkeypatch.setattr(tm, "_main_agent_busy", lambda scope: False)
    monkeypatch.setattr(tm, "emit_message_to_web_ui", lambda scope, content, session_id=None: "sid-web")
    import vaf.core.messaging_connections as mc
    monkeypatch.setattr(mc, "send_to_main_messenger", lambda scope, uname, text: (False, None))


def test_reject_counter_bypasses_within_one_run(monkeypatch, tmp_path):
    """The regression test for the 12-rejection deadlock.

    The model retries INSIDE one chat_step, because ask_user's rejection text tells it to. So the
    budget has to be spent here, in the gate. With a gate that rejects everything (which is exactly
    what a mis-calibrated threshold is), the run must still get a question out - after
    thinking_getto_max_attempts rejections the next one is delivered as it stands."""
    _deliverable(monkeypatch, tmp_path)
    monkeypatch.setattr(tm, "_question_too_similar", lambda scope, msg: True)  # rejects everything
    scope = "u-budget"
    tm.set_proactive_mode(scope, "open")
    tm.set_dedup_enforce(scope, True)

    assert tm.deliver_tracked_message(scope, "Frage 1") is None
    assert tm.deliver_tracked_message(scope, "Frage 2") is None
    assert tm.deliver_tracked_message(scope, "Frage 3") is None
    assert tm.get_ask_rejects(scope) == 3
    # budget spent -> the fourth question is delivered even though the gate would still reject it
    req = tm.deliver_tracked_message(scope, "Frage 4")
    assert req and req.get("delivered") is True


def test_reject_counter_resets_per_run(monkeypatch, tmp_path):
    """A counter that outlived its run would sit at the budget forever and silently switch the
    dedup gate off for that scope."""
    _deliverable(monkeypatch, tmp_path)
    monkeypatch.setattr(tm, "_question_too_similar", lambda scope, msg: True)
    scope = "u-budget-reset"
    tm.set_proactive_mode(scope, "open")
    tm.set_dedup_enforce(scope, True)
    tm.deliver_tracked_message(scope, "Frage 1")
    assert tm.get_ask_rejects(scope) == 1

    tm.clear_run_evidence(scope)                      # next run starts
    assert tm.get_ask_rejects(scope) == 0
    assert tm._key(scope) not in tm._ASK_REJECTS
    tm.set_proactive_mode(scope, "open")
    tm.set_dedup_enforce(scope, True)
    assert tm.deliver_tracked_message(scope, "Frage 2") is None   # gate enforced again


def test_reject_counter_is_per_scope(monkeypatch, tmp_path):
    """One user's spent budget must never open another user's gate."""
    _deliverable(monkeypatch, tmp_path)
    monkeypatch.setattr(tm, "_question_too_similar", lambda scope, msg: True)
    for s in ("u-budget-a", "u-budget-b"):
        tm.set_proactive_mode(s, "open")
        tm.set_dedup_enforce(s, True)
    for _ in range(3):
        tm.deliver_tracked_message("u-budget-a", "Frage")
    assert tm.ask_rejects_exhausted("u-budget-a") is True
    assert tm.ask_rejects_exhausted("u-budget-b") is False
    assert tm.deliver_tracked_message("u-budget-b", "Frage") is None


# --- follow-up re-ask exemption --------------------------------------------------------------------

def test_followup_reask_is_exempt_from_dedup(monkeypatch, tmp_path):
    """A follow-up re-asks the SAME open question on purpose; the dedup gate must NOT block it (it would
    otherwise see the original request as a near-duplicate and kill the entire follow-up mechanism)."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(tm, "run_has_open_request", lambda scope: False)
    monkeypatch.setattr(tm, "_question_too_similar", lambda scope, msg: True)  # would reject if applied
    monkeypatch.setattr(tm, "_main_agent_busy", lambda scope: False)
    monkeypatch.setattr(tm, "emit_message_to_web_ui", lambda scope, content, session_id=None: "sid-web")
    import vaf.core.messaging_connections as mc
    monkeypatch.setattr(mc, "send_to_main_messenger", lambda scope, uname, text: (False, None))
    scope = "u-fu"
    existing = tr.add_request(scope, "Soll ich dir eine VAF-Erinnerung einrichten?", run_seq=1)
    tm.set_proactive_mode(scope, "open")
    tm.set_dedup_enforce(scope, True)
    tm.set_followup_context(scope, existing["id"])   # marks THIS delivery as a follow-up re-ask
    try:
        req = tm.deliver_tracked_message(scope, "Soll ich das jetzt einrichten - ja oder nein?")
    finally:
        tm.clear_followup_context(scope)
    assert req and req.get("delivered") is True   # follow-up delivered, NOT blocked by dedup
    assert tm.take_reject_reason(scope) == ""


# --- the derived cutoff (this is where the measured incident is pinned) ---------------------------

def _geometry(monkeypatch, pool_names, pool_nn, cand_best, **cfg_over):
    """State the two similarities the gate actually reads, independently.

    The gate asks two separate questions - how close the POOL sits to itself, and how close the
    CANDIDATE sits to its nearest pool member. Real vectors cannot express those two freely in a
    handful of dimensions (placing three vectors at chosen angles to a reference also fixes their
    angles to each other), so the similarity function is stubbed with a table instead. Each
    "embedding" is a one-element id vector; `_cosine` looks the pair up.
    """
    ids = {name: float(i) for i, name in enumerate(pool_names)}
    ids["__cand__"] = -1.0

    def embed(text):
        return [ids.get(text, ids["__cand__"])]

    def cosine(a, b):
        ia, ib = a[0], b[0]
        if ia == ib:
            return 1.0
        if -1.0 in (ia, ib):          # candidate against any pool member
            return cand_best if max(ia, ib) == 0.0 else 0.0
        return pool_nn                # pool member against pool member

    monkeypatch.setattr(tm, "_recent_question_texts", lambda scope, seq: list(pool_names))
    monkeypatch.setattr(tm, "_embed_question", embed)
    monkeypatch.setattr(tm, "_cosine", cosine)
    _set_cfg(monkeypatch, **cfg_over)


def test_calibrated_gate_accepts_the_measured_band(monkeypatch, tmp_path):
    """The real incident, encoded.

    Measured with all-MiniLM-L6-v2 against the user's actual 12-question pool: nine genuinely
    different candidate questions scored 0.872-0.912, while the pool's own similarities ran
    0.800-0.943 (median 0.879). Under the old fixed 0.80 every one of those nine was rejected and the
    run could not get a single question out. The derived cutoff sits inside the pool's own band, so a
    candidate that is no closer to the pool than the pool is to itself gets through."""
    _isolate(monkeypatch, tmp_path)
    _geometry(monkeypatch, ["p1", "p2", "p3"], pool_nn=0.940, cand_best=0.890,
              thinking_question_similarity_threshold=0.80,
              thinking_question_similarity_percentile=90,
              thinking_question_similarity_max=0.97,
              thinking_question_similarity_min_pool=3)
    assert tm._question_too_similar("u-cal", "cand") is False


def test_derived_cutoff_clears_the_band_the_fixed_one_sat_under(monkeypatch, tmp_path):
    """The mutation half, stated as arithmetic on the measured numbers.

    Old gate: reject when cosine >= 0.80. The nine real candidates measured 0.872-0.912, so all nine
    were rejected - and the pool's OWN similarities started at 0.800, i.e. the threshold sat at the
    floor of what this embedder produces for any two questions at all. The derived cutoff is a
    percentile of that same pool distribution, so it lands ABOVE the candidate band by construction.
    """
    import vaf.core.thinking_mode as _tm
    measured_candidates = [0.912, 0.890, 0.891, 0.887, 0.895, 0.872, 0.878, 0.876, 0.890]
    pool_nn = [0.800, 0.879, 0.943]        # the measured pool spread

    class _V(list):
        pass

    vecs = [_V([i]) for i in range(len(pool_nn))]
    monkeypatch.setattr(_tm, "_cosine", lambda a, b: pool_nn[max(int(a[0]), int(b[0]))])
    cutoff = _tm._pool_cutoff(vecs, 90, 0.80)

    assert all(c >= 0.80 for c in measured_candidates)      # every one died on the old fixed threshold
    assert all(c < cutoff for c in measured_candidates)     # every one survives the derived cutoff


def test_near_identical_text_is_always_rejected(monkeypatch, tmp_path):
    """The one absolute worth keeping: near-1.0 cosine means near-identical TEXT in any model, which
    is the property the narrow-cone effect does not distort."""
    _isolate(monkeypatch, tmp_path)
    _geometry(monkeypatch, ["p1", "p2", "p3"], pool_nn=0.940, cand_best=0.985,
              thinking_question_similarity_threshold=0.80,
              thinking_question_similarity_percentile=90,
              thinking_question_similarity_max=0.97,
              thinking_question_similarity_min_pool=3)
    assert tm._question_too_similar("u-cal", "cand") is True


def test_candidate_above_the_pool_percentile_is_rejected(monkeypatch, tmp_path):
    """Closer to an existing question than the pool's own members are to each other -> a real repeat."""
    _isolate(monkeypatch, tmp_path)
    _geometry(monkeypatch, ["p1", "p2", "p3"], pool_nn=0.900, cand_best=0.955,
              thinking_question_similarity_threshold=0.80,
              thinking_question_similarity_percentile=90,
              thinking_question_similarity_max=0.97,
              thinking_question_similarity_min_pool=3)
    assert tm._question_too_similar("u-cal", "cand") is True


def test_small_pool_keeps_only_the_ceiling(monkeypatch, tmp_path):
    """Fewer questions than min_pool: no distribution to calibrate against, so the derived half
    stands down and a merely-similar question gets through. The near-duplicate ceiling still
    applies - it needs no calibration."""
    _isolate(monkeypatch, tmp_path)
    _geometry(monkeypatch, ["p1"], pool_nn=1.0, cand_best=0.930,
              thinking_question_similarity_max=0.97,
              thinking_question_similarity_min_pool=3)
    assert tm._question_too_similar("u-cal", "cand") is False

    _geometry(monkeypatch, ["p1"], pool_nn=1.0, cand_best=0.990,
              thinking_question_similarity_max=0.97,
              thinking_question_similarity_min_pool=3)
    assert tm._question_too_similar("u-cal", "cand") is True


def test_cutoff_never_falls_below_the_floor():
    """A very broad pool must not drag the cutoff down to where different questions get rejected."""
    import math

    def v(c):
        a = math.acos(c)
        return [math.cos(a), math.sin(a)]

    # three near-orthogonal vectors -> tiny nearest-neighbour similarities
    spread = [[1.0, 0.0], [0.0, 1.0], [-1.0, 0.0]]
    assert tm._pool_cutoff(spread, 90, 0.80) == 0.80
    # and a tight pool pushes the cutoff ABOVE the floor
    tight = [v(0.90), v(0.92), v(0.95)]
    assert tm._pool_cutoff(tight, 90, 0.80) > 0.80


def test_cutoff_is_the_floor_for_a_degenerate_pool():
    assert tm._pool_cutoff([], 90, 0.80) == 0.80
    assert tm._pool_cutoff([[1.0, 0.0]], 90, 0.80) == 0.80



# --- memory_enabled guard short-circuits BEFORE any embedding (leak-relevant) ----------------------

def test_memory_disabled_short_circuits_before_embed(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _set_cfg(monkeypatch, memory_enabled=False)
    monkeypatch.setattr(tm, "_recent_question_texts", lambda scope, seq: ["Woran arbeitest du bei VAF?"])

    def boom(text):
        raise AssertionError("must NOT touch the embedding subsystem when memory is disabled")

    monkeypatch.setattr(tm, "_embed_question", boom)
    assert tm._question_too_similar("u1", "Noch eine VAF-Frage?") is False


# --- recency window actually excludes old runs (bounds the per-turn embed set) ---------------------

def test_recent_question_texts_excludes_old_runs(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _set_cfg(monkeypatch, thinking_question_similarity_runs=12)
    scope = "u-window"
    tr.add_request(scope, "Alte Frage von vor Wochen?", run_seq=0)
    tr.add_request(scope, "Aktuelle Frage?", run_seq=20)
    texts = tm._recent_question_texts(scope, current_run_seq_val=20)
    assert "Aktuelle Frage?" in texts
    assert "Alte Frage von vor Wochen?" not in texts   # run_seq 0 is outside the 12-run window


# --- the thinking_done fallback clears any reject_reason it set (no stale per-scope state) ----------

def test_fallback_clears_stale_too_similar_reason(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(tm, "run_has_open_request", lambda scope: False)
    monkeypatch.setattr(tm, "_question_too_similar", lambda scope, msg: True)
    scope = "u-fb"
    tm.set_proactive_mode(scope, "open")
    tm.set_dedup_enforce(scope, True)
    note = tm.deliver_thinking_done_fallback(scope, "Schon wieder eine VAF-Frage?")
    assert "not sent" in note.lower() or "not grounded" in note.lower()
    # the fallback consumed/cleared the reason it set -> no stale "too_similar" left for a later ask_user
    assert tm.take_reject_reason(scope) == ""
