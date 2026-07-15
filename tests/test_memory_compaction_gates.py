# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Compaction fact-extraction guardrails (vaf/memory/rag.py).

Pins the sharpened prompt rules (self-containment, absolute dating,
durability, novelty - added after a live review found dangling references,
undated snapshots and stored conversation state) and the model-independent
gates between parse and ingest (length bounds, junk markers, per-run cap).
"""
from vaf.memory.rag import (
    _MAX_FACTS_PER_COMPACTION,
    _apply_fact_gates,
    _build_compaction_prompt,
    _parse_memory_reply,
)


def test_prompt_carries_the_sharpened_rules():
    p = _build_compaction_prompt("User: hi", "2026-07-15")
    assert "Today is 2026-07-15" in p
    assert "SELF-CONTAINED" in p and "never 'the patent'" in p
    assert "as of 2026-07-15" in p                      # dating rule, concrete
    assert "DURABILITY" in p and "has not sent the email yet" in p
    assert "NOVELTY" in p
    assert "GROUNDING" in p                             # original rule kept
    assert "NO_REPLY" in p and 'MEMORY: "fact in English"' in p


def test_gates_length_and_junk():
    kept, rejected = _apply_fact_gates([
        ("User's name is Mert and his company is Veyllo GmbH.", ["personal"]),
        ("Ok.", []),                                     # too short
        ("x" * 600, []),                                 # too long
        ("NO_REPLY", []),                                # junk marker
        ("Final check: compliance confirmed.", []),      # meta junk
        ('MEMORY: "nested protocol line" [oops]', []),   # nested marker
    ])
    assert len(kept) == 1
    reasons = [r for _, r in rejected]
    # Length is checked first: the bare "NO_REPLY" line is too_short before
    # the junk-marker check would see it.
    assert reasons == ["too_short", "too_long", "too_short", "junk_marker", "junk_marker"]


def test_gates_cap_per_run():
    many = [(f"Durable fact number {i} about the user's project setup.", []) for i in range(30)]
    kept, rejected = _apply_fact_gates(many)
    assert len(kept) == _MAX_FACTS_PER_COMPACTION
    assert sum(1 for _, r in rejected if r == "cap") == 30 - _MAX_FACTS_PER_COMPACTION


def test_parse_then_gate_pipeline():
    reply = (
        "<think>let me see</think>\n"
        'MEMORY: "Mert owns patent US12375457B2 privately, not via Veyllo GmbH." [work, patent]\n'
        'MEMORY: "Ok." [junk]\n'
        "NO_REPLY trailing noise\n"
    )
    kept, rejected = _apply_fact_gates(_parse_memory_reply(reply))
    assert len(kept) == 1
    assert kept[0][0].startswith("Mert owns patent")
    assert kept[0][1] == ["work", "patent"]
    assert rejected[0][1] == "too_short"


# ---------------------------------------------------------------------------
# Lexical query stopword filtering (vocab-book key "stopwords")
# ---------------------------------------------------------------------------

def test_kai_question_scores_after_stopword_filter():
    """Live incident: 'Kannst du dich noch an Kai erinnern?' scored Kai
    chunks 1/7 = 0.11 (filler dilution) while a bare 'Kai' query scored 1.0.
    With function words filtered the same question must clear the 0.3 bar."""
    from vaf.memory.rag import (_tokenize_lexical_query, _content_tokens,
                                _lexical_score_query_to_text)
    fact = "Mert is working with Kai on a Pro FIT funding application for Veyllo GmbH."
    raw = _tokenize_lexical_query("Kannst du dich noch an Kai erinnern?")
    old_score = _lexical_score_query_to_text(raw, fact)
    new_score = _lexical_score_query_to_text(_content_tokens(raw), fact)
    assert old_score < 0.3 <= new_score
    # The bare-name query keeps working
    assert _lexical_score_query_to_text(_content_tokens(["kai"]), fact) >= 0.7


def test_stopword_filter_never_empties_a_query():
    from vaf.memory.rag import _content_tokens
    assert _content_tokens(["was", "ist", "das"]) == ["was", "ist", "das"]
    assert _content_tokens([]) == []


def test_tokenizer_keeps_umlauts():
    from vaf.memory.rag import _tokenize_lexical_query
    assert "müller" in _tokenize_lexical_query("Kennst du Herrn Müller?")
    assert "können" in _tokenize_lexical_query("können wir das?")
