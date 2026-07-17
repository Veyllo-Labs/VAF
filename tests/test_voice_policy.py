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
