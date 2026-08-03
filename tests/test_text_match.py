# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The German match primitive: both spellings meet, and the three ways that can go wrong.

Ninety-four sites matched German keywords written with proper umlauts against text that
frequently arrives transliterated - from users without umlaut keys, and from a chat model
that since 2026-07 emits ASCII substitutions mixed word by word. The guard against
ungrounded success claims looked for "ausgeführt" and therefore missed exactly the models
it was built to catch; the outgoing-mail risk gate looked for "überweisung" and missed
"ueberweisung".

The fix is one primitive, not ninety-four doubled lists: source stays proper German, the
tolerance is generated at the comparison. These tests pin the three failure modes that the
adversarial pass measured before the primitive was written, because each of them is silent:

1. Folding creates substrings that did not exist ("über" lives inside "blueberry"), so
   whole-word matching has to be available and has to be the documented default.
2. Rewriting an umlaut inside a character class COMPILES and silently widens the class to
   accept "(", ")", "?", ":" and "|" - measured on eleven real patterns, zero errors.
3. A capturing group instead of a non-capturing one silently turns findall results into
   tuples, and folding the subject string would return extracted values in folded form.
"""
import re

import pytest

from vaf.core.text_match import (
    compile_de,
    contains_any,
    contains_any_word,
    expand,
    fold,
    fold_all,
)


def test_exact_membership_needs_the_vocabulary_folded_once():
    """Several sites ask "is this token exactly one of these" rather than "does the text
    contain one of these" - presence acks, stopword sets, token-set intersection. Plain
    membership against unfolded needles misses the transliterated reply entirely."""
    acks = fold_all({"ja", "bin zurück", "zurück"})
    assert fold("Bin zurueck") in acks
    assert fold("bin zurück") in acks
    assert fold("nein") not in acks


def test_folded_sets_intersect_across_spellings():
    stopwords = fold_all({"für", "über", "und"})
    tokens = {fold(t) for t in ["Bericht", "fuer", "Berlin"]}
    assert tokens & stopwords == {"fuer"}


# ── fold ─────────────────────────────────────────────────────────────────────

def test_fold_maps_every_umlaut_and_lowercases():
    assert fold("Für größere Änderungen KÖNNTE") == "fuer groessere aenderungen koennte"
    assert fold("Straße") == "strasse"
    assert fold("") == ""


def test_fold_leaves_ascii_alone():
    """It must not invent umlauts: the reverse direction would destroy Michael and queue."""
    assert fold("Michael queued a message") == "michael queued a message"


# ── the point: both spellings meet ───────────────────────────────────────────

@pytest.mark.parametrize("written", ["täglich um 9", "taeglich um 9", "TÄGLICH um 9"])
def test_both_spellings_reach_the_same_needle(written):
    assert contains_any(written, ["täglich"])
    assert contains_any_word(written, ["täglich"])


def test_the_measured_defect_the_ungrounded_claim_guard_missed():
    """The concrete site this primitive was built for."""
    outcome_words = ["gelöscht", "ausgeführt", "bestätigt"]
    assert contains_any_word("die datei wurde ausgefuehrt", outcome_words)
    assert contains_any_word("die datei wurde ausgeführt", outcome_words)


def test_the_measured_defect_the_mail_risk_gate_missed():
    assert contains_any("bitte veranlassen sie die ueberweisung", ["überweisung"])


# ── hazard 1: folding creates substrings that did not exist ──────────────────

def test_whole_word_matching_keeps_ueber_out_of_blueberry():
    """Measured against 48262 English dictionary entries, this was the ONE real collision
    across 577 needles: "über" folds to "ueber", which sits inside "blueberry". A language
    heuristic using plain containment answered German for an English sentence."""
    english = "I want a blueberry muffin recipe, please."
    assert contains_any(english, ["über"]), "containment is substring-based, so it collides"
    assert not contains_any_word(english, ["über"]), "whole-word matching must not collide"
    # and the real thing still matches
    assert contains_any_word("gib mir infos ueber berlin", ["über"])


def test_whole_word_matching_keeps_spass_out_of_trespass():
    assert not contains_any_word("do not trespass here", ["spaß"])
    assert contains_any_word("das war ein spass", ["spaß"])


def test_a_phrase_needle_still_matches_inside_a_sentence():
    """Word boundaries are applied at the needle's outer edges, not between its words."""
    assert contains_any_word("mach das schritt fuer schritt bitte", ["schritt für schritt"])


def test_containment_still_reaches_stems():
    """Deliberate stem matching is why contains_any exists at all."""
    assert contains_any("er hat es ueberarbeitet", ["überarbeit"])
    assert not contains_any_word("er hat es ueberarbeitet", ["überarbeit"])


def test_folding_is_wrong_for_an_umlaut_presence_test():
    """Documented hazard, pinned so nobody "fixes" a presence test with this module:
    folded needles become ae/oe/ue/ss, and "ss" occurs in most English prose."""
    assert "ss" in fold("this was a great success")
    assert not any(ch in "this was a great success" for ch in ("ä", "ö", "ü", "ß"))


# ── hazard 2: character classes and quantifiers are refused, not guessed ─────

def test_an_umlaut_inside_a_character_class_is_refused():
    """Left to a naive rewrite this COMPILES and silently accepts regex metacharacters as
    class members - the failure mode is invisible, which is why it must raise."""
    with pytest.raises(ValueError, match="character class"):
        expand(r"[äöüa-z]+")


def test_a_quantifier_after_an_umlaut_is_refused():
    with pytest.raises(ValueError, match="quantifier"):
        expand(r"hä+")


@pytest.mark.parametrize("pattern", [r"(?<=über)\s*vertrag", r"(?<!ü)ber"])
def test_an_umlaut_inside_a_lookbehind_is_refused(pattern):
    """Python needs a fixed-width lookbehind, and the alternation is one character or two,
    so the expanded pattern would not compile at all. Refusing names the cause; letting
    re.compile fail would surface as a bare "look-behind requires fixed-width pattern"."""
    with pytest.raises(ValueError, match="lookbehind"):
        expand(pattern)


def test_a_lookahead_is_still_expanded():
    """Only LOOKBEHIND has the fixed-width rule; a lookahead is fine."""
    assert compile_de(r"test(?=über)").search("testueber")


def test_escapes_and_comments_are_copied_verbatim():
    assert expand(r"\ä") == r"\ä"
    assert expand(r"(?#Größe)abc") == r"(?#Größe)abc"


def test_a_class_without_umlauts_passes_through_untouched():
    assert expand(r"[a-z0-9_]+ö") == r"[a-z0-9_]+(?:ö|oe)"


def test_a_pattern_ending_in_an_umlaut_is_not_mistaken_for_a_quantifier():
    """`"" in "*+?{"` is True in Python, so an umlaut in final position looked like it was
    followed by a quantifier and every such pattern was refused. Found by the test above."""
    assert expand("größe") == "gr(?:ö|oe)(?:ß|ss)e"
    assert compile_de("größe").search("groesse des ordners")


# ── hazard 3: groups and extraction survive ──────────────────────────────────

def test_group_numbering_is_unchanged():
    """A capturing group here would turn findall results into tuples."""
    src = r"(?:ich möchte|bitte)\s+(.+?)(?:\.|$)"
    assert compile_de(src).groups == re.compile(src).groups == 1


def test_extraction_returns_the_original_text_not_a_folded_one():
    """The reason expand() rewrites the PATTERN instead of folding the subject: folding
    changes length, so offsets shift and the extracted value comes back transliterated."""
    pat = compile_de(r"(?:ich möchte)\s+(.+)")
    assert pat.search("ich moechte Büro/plan.md lesen").group(1) == "Büro/plan.md lesen"


def test_expanded_pattern_matches_both_spellings():
    pat = compile_de(r"\b(ausgeführt|durchgeführt|erstellt)\b")
    assert pat.search("die datei wurde ausgefuehrt")
    assert pat.search("die datei wurde ausgeführt")


def test_expand_is_a_noop_without_umlauts():
    assert expand(r"\b(created|deleted)\b") == r"\b(created|deleted)\b"
