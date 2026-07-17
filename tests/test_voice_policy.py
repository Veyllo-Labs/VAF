# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Stage 1 voice reflex policy (vaf/core/voice_policy.py).

Pins: the vocab-backed trigger prefilter, the embedding interest score (mocked so no
model download), the activity-scaled threshold, and the Phase-0 contract that
`classify()` mirrors the Tier-1 three-way verdict while exposing the signals.
"""
import vaf.core.voice_policy as vp


def test_trigger_match_hits_de_and_en():
    assert vp.trigger_match("Kannst du mir kurz helfen?")            # "kannst du"
    assert vp.trigger_match("Can you look this up for me")           # "can you"
    assert vp.trigger_match("ich weiß nicht wie das geht")          # real umlaut, matches STT
    assert vp.trigger_match("Hey bist du da?")                      # new address-check phrase
    assert vp.trigger_match("Wir sind gestern essen gegangen") is None


def test_de_triggers_use_real_umlauts_not_ascii():
    """German STT emits real umlauts; ASCII-transliterated triggers would never match."""
    assert vp.trigger_match("wie spät ist es")       # not "wie spaet"
    assert vp.trigger_match("wie spaet ist es") is None


def test_interest_score_uses_embeddings(monkeypatch):
    # Inject deterministic vectors: utterance == topic "wetter" -> cosine 1.0.
    vecs = {"wetter berlin morgen": [1.0, 0.0], "wetter": [1.0, 0.0], "fussball": [0.0, 1.0]}
    monkeypatch.setattr(vp, "_embed_one", lambda t: vecs.get(str(t).strip().lower()))
    assert vp.interest_score("wetter berlin morgen", ["wetter"]) == 1.0
    assert vp.interest_score("wetter berlin morgen", ["fussball"]) == 0.0
    assert vp.interest_score("anything", None) == 0.0          # no topics -> 0
    assert vp.interest_score("", ["wetter"]) == 0.0            # empty text -> 0


def test_activity_scales_threshold():
    # Quiet demands a higher bar than active.
    assert vp._threshold(0.0) > vp._threshold(1.0)
    assert vp._threshold(0.0) == vp._THR_QUIET
    assert vp._threshold(1.0) == vp._THR_ACTIVE


def test_is_interesting_requires_grounding(monkeypatch):
    """A trigger only LOWERS the grounding bar; it is never sufficient alone
    (anti-fabrication: no owner-relevance, no chime-in)."""
    # Trigger phrase but ZERO grounding -> not interesting.
    monkeypatch.setattr(vp, "interest_score", lambda *a, **k: 0.0)
    assert vp.is_interesting("kannst du das notieren", ["arbeit"], activity=1.0) is False
    # Trigger PLUS grounding -> interesting (the trigger eased the bar).
    monkeypatch.setattr(vp, "interest_score", lambda *a, **k: 0.5)
    assert vp.is_interesting("kannst du das notieren", ["arbeit"], activity=1.0) is True
    # Strong grounding WITHOUT a trigger -> interesting.
    monkeypatch.setattr(vp, "interest_score", lambda *a, **k: 0.95)
    assert vp.is_interesting("das quartalsergebnis war stark", ["arbeit"], activity=0.0) is True
    # No trigger, no grounding -> not interesting.
    monkeypatch.setattr(vp, "interest_score", lambda *a, **k: 0.0)
    assert vp.is_interesting("wir gehen spazieren", ["arbeit"], activity=0.0) is False


def test_classify_phase0_mirrors_tier1_verdict_and_exposes_signals(monkeypatch):
    monkeypatch.setattr(vp, "interest_score", lambda *a, **k: 0.9)
    # Owner speech -> respond_now (Tier-1), signals populated.
    r = vp.classify("Kannst du das notieren?", "self", topics=["notizen"], activity=0.5)
    assert r["verdict"] == "respond_now" and r["reason"] == "ok"
    assert r["trigger"] and r["interesting"] is True
    # Guest side-talk still store_only in Phase 0 (no chime-in upgrade yet), even if
    # the content is flagged interesting - the signal is there for Phase 2.
    r2 = vp.classify("[anderer_Sprecher]: erinnere mich an den Termin", "other",
                     topics=["termine"], activity=0.9)
    assert r2["verdict"] == "store_only" and r2["reason"] == "side_talk"
    assert r2["interesting"] is True


# ── Phase 2: scene-based internal modes + the one activity dial ────────────────

def test_derive_scene_one_to_one_vs_multi():
    assert vp.derive_scene("self", ["self", "self"]) == "one_to_one"
    assert vp.derive_scene("self", []) == "one_to_one"
    # A non-owner speaker now, OR one recently in the transcript -> multi.
    assert vp.derive_scene("other", ["self"]) == "multi"
    assert vp.derive_scene("named", []) == "multi"
    assert vp.derive_scene("self", ["other", "self"]) == "multi"


def test_derive_mode_system_chosen_from_scene_and_dial():
    # 1:1 with the verified owner tends to active; a busy room stays quiet.
    assert vp.derive_mode("one_to_one", "self", activity=0.5) == vp.MODE_ACTIVE
    assert vp.derive_mode("multi", "other", activity=0.5) == vp.MODE_QUIET
    # The dial at its floor pins notes_only (record, never interrupt), regardless of scene.
    assert vp.derive_mode("one_to_one", "self", activity=0.0) == vp.MODE_NOTES
    assert vp.derive_mode("multi", "other", activity=0.0) == vp.MODE_NOTES


def test_chime_decision_notes_mode_never_speaks(monkeypatch):
    monkeypatch.setattr(vp, "interest_score", lambda *a, **k: 1.0)  # maximally grounded
    dec = vp.chime_decision("das quartalsergebnis war stark", "other",
                            recent_labels=["self"], topics=["arbeit"], activity=0.0)
    assert dec["mode"] == vp.MODE_NOTES and dec["speak"] is False


def test_chime_decision_speaks_only_when_grounded(monkeypatch):
    # Grounded overheard side-talk in a busy room -> chime in.
    monkeypatch.setattr(vp, "interest_score", lambda *a, **k: 0.95)
    dec = vp.chime_decision("[anderer_Sprecher]: das quartalsergebnis war stark", "other",
                            recent_labels=["self"], topics=["arbeit"], activity=0.6)
    assert dec["mode"] == vp.MODE_QUIET and dec["speak"] is True
    # Ungrounded chatter -> no chime-in even at high activity (anti-fabrication).
    monkeypatch.setattr(vp, "interest_score", lambda *a, **k: 0.0)
    dec2 = vp.chime_decision("[anderer_Sprecher]: schönes wetter heute", "other",
                             recent_labels=["self"], topics=["arbeit"], activity=1.0)
    assert dec2["speak"] is False


def test_chime_decision_no_topics_never_speaks(monkeypatch):
    # A real embedding path, but the owner configured no topics -> score 0 -> silent.
    dec = vp.chime_decision("[anderer_Sprecher]: irgendwas", "other",
                            recent_labels=["self"], topics=[], activity=1.0)
    assert dec["speak"] is False and dec["score"] == 0.0


def test_activity_dial_shifts_chime_frequency(monkeypatch):
    """The ONE dial (quiet..active) demonstrably changes how readily the agent
    chimes in: the same grounded utterance speaks at high activity, stays silent
    at low activity (mid grounding between the two mode-scaled bars)."""
    monkeypatch.setattr(vp, "interest_score", lambda *a, **k: 0.62)
    args = dict(recent_labels=["self"], topics=["arbeit"])
    hot = vp.chime_decision("[anderer_Sprecher]: bericht", "other", activity=1.0, **args)
    cold = vp.chime_decision("[anderer_Sprecher]: bericht", "other", activity=0.3, **args)
    assert hot["speak"] is True and cold["speak"] is False


def test_similar_to_any_dedups_recent_chime_ins(monkeypatch):
    vecs = {"das meeting ist um drei": [1.0, 0.0],
            "das meeting ist um 3 uhr": [0.99, 0.01],
            "willst du kaffee": [0.0, 1.0]}
    monkeypatch.setattr(vp, "_embed_one", lambda t: vecs.get(str(t).strip().lower()))
    recent = ["das meeting ist um drei"]
    assert vp.similar_to_any("das meeting ist um 3 uhr", recent) is True   # near-duplicate
    assert vp.similar_to_any("willst du kaffee", recent) is False          # unrelated
    assert vp.similar_to_any("anything", []) is False                      # nothing to dedup against
