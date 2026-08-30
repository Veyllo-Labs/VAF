# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A caller asking about the PERSON must not be answered with learned document text.

A learned PDF contributes hundreds of chunks; the facts about someone are a handful. On a pure
similarity search the document mass therefore wins by volume, and the two places that ask for
personal facts in plain words had no way to say so.

Measured on a real store, 2026-08-30 - 704 chunks, 475 of them (67.5%) document-derived:

    query                                    document chunks in the top 8
    "user profile facts preferences ..."     4/8   <- the `known_facts` block of EVERY system prompt
    "plans, deadlines, appointments ..."     6/8   <- the relevance rung's watchlist
    "a recurring routine or habit ..."       6/8   <- the proactive digest

With the exclusion: 0/8 in all three, and still 8 results - recall is kept, not traded away. That
is the reason it belongs in SQL. The pre-existing `metadata_filter` runs in PYTHON over rows the
database already ranked, so filtering there would have returned the 2-4 personal chunks that
happened to be in the window instead of ranking within the personal memories."""
import re
from pathlib import Path

from sqlalchemy.dialects import postgresql

from vaf.memory.rag import _DOCUMENT_MEMORY_TYPES, _not_document_memory

_RAG = Path(__file__).resolve().parent.parent / "vaf" / "memory" / "rag.py"


def _sql(expr) -> str:
    return str(expr.compile(dialect=postgresql.dialect(),
                            compile_kwargs={"literal_binds": True}))


# ── the condition itself ──────────────────────────────────────────────────────────────────

def test_every_document_writing_lane_is_covered():
    """The names are what the writers actually store; a missed one is a silent hole.
    `document` alone was 455 of 704 chunks on the measured store."""
    assert set(_DOCUMENT_MEMORY_TYPES) == {
        "document", "document_index", "attachment_section", "attachment_ephemeral"
    }


def test_the_writers_still_use_these_names():
    """Guards the constant against the code that fills it: a renamed type would leave the
    exclusion pointing at nothing, and nothing would look different until a run went sour."""
    tree = _RAG.parent.parent
    writers = {
        "document": ["tools/learn_job.py", "tools/learn_attached_knowledge.py"],
        "document_index": ["tools/learn_document.py"],
        "attachment_section": ["memory/attachment_rag.py"],
        "attachment_ephemeral": ["memory/attachment_rag.py"],
    }
    for mem_type, files in writers.items():
        assert any(mem_type in (tree / f).read_text(encoding="utf-8") for f in files), \
            f"no writer left for type {mem_type!r} - the exclusion may be pointing at a dead name"


def test_the_condition_excludes_documents_and_keeps_untyped():
    sql = _sql(_not_document_memory())
    assert "IS NULL" in sql, "an untyped memory must survive - this store predates the type field"
    for t in _DOCUMENT_MEMORY_TYPES:
        assert t in sql, f"{t} is not excluded"
    assert "NOT IN" in sql.upper()


def test_the_condition_does_not_name_the_personal_types():
    """An INCLUSION list would drop every untyped legacy record. Excluding is the safe direction,
    and this pins the direction rather than the wording."""
    sql = _sql(_not_document_memory()).lower()
    for personal in ("'note'", "'conversation'"):
        assert personal not in sql


# ── the wiring: both lanes, both callers, and an unchanged default ────────────────────────

def _src() -> str:
    return _RAG.read_text(encoding="utf-8")


def test_both_lanes_of_the_hybrid_search_apply_it():
    """The fusion is only as clean as its worse half: the lexical lane would otherwise feed
    document chunks straight back into the RRF the vector lane just excluded them from."""
    src = _src()
    assert re.search(r"filters\.append\(_not_document_memory\(\)\)", src), "vector lane"
    assert re.search(r"lexical_filters\.append\(_not_document_memory\(\)\)", src), "lexical lane"


def test_it_is_applied_in_sql_not_after_the_fetch():
    """The whole reason it is not the existing metadata_filter. Both applications must sit in a
    filter list that reaches the WHERE clause, never in the post-fetch loop."""
    src = _src()
    for marker in ("filters.append(_not_document_memory())",
                   "lexical_filters.append(_not_document_memory())"):
        before = src.split(marker, 1)[0]
        assert before.rstrip().endswith(":") or "append" in marker, marker
    # the post-fetch loop must not have grown a type check
    post = src.split("# Apply metadata filter", 1)
    if len(post) > 1:
        assert "_DOCUMENT_MEMORY_TYPES" not in post[1][:800], \
            "the exclusion drifted into the post-fetch loop, where it costs recall"


def test_the_default_is_unchanged_for_every_existing_caller():
    src = _src()
    assert "exclude_documents: bool = False" in src, \
        "a default of True would change ordinary chat retrieval, which this change must not touch"


def test_the_two_measured_callers_opt_in():
    """Both ask for facts about the PERSON in plain words. They are the whole reason the
    parameter exists - N=2, measured, in two different modules."""
    src = _src()
    profile = src.split("def refresh_user_profile_summary", 1)[1][:1500]
    assert "exclude_documents=True" in profile, "the known_facts cache still takes document text"

    thinking = (_RAG.parent.parent / "core" / "thinking_mode.py").read_text(encoding="utf-8")
    digest = thinking.split("def _build_memory_digest", 1)[1][:2500]
    assert "exclude_documents=True" in digest, "the proactive/watchlist digest still takes document text"


# ── the promise the docs make ─────────────────────────────────────────────────────────────

def test_the_docs_state_that_ordinary_rag_is_unchanged():
    """The whole point of the default being off. A doc that only described the new behaviour
    would leave a reader unsure whether their document search still works - it does, and the
    measured proof belongs next to the claim."""
    doc = (_RAG.parent.parent.parent / "docs" / "memory" / "MEMORY_SYSTEM.md") \
        .read_text(encoding="utf-8")
    assert "Ordinary RAG is unchanged" in doc
    assert "attachment lane is untouched" in doc
    assert "exclude_documents" in doc

    thinking = (_RAG.parent.parent.parent / "docs" / "agents" / "Thinking-Mode.md") \
        .read_text(encoding="utf-8")
    assert "exclude_documents=True" in thinking, \
        "the rung's own doc does not say what its digest leaves out"
