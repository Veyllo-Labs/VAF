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
import ast
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


# ── the wiring: both lanes, both callers, and an unchanged default ────────────────────

def _src() -> str:
    return _RAG.read_text(encoding="utf-8")


def _search_fn():
    """`RagPipeline.search`, parsed. Structure, not text: the claim below is about WHERE the
    condition lands inside the statement, and a substring search cannot see that."""
    for node in ast.walk(ast.parse(_src())):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "search":
            if any(isinstance(n, ast.Name) and n.id == "lexical_filters" for n in ast.walk(node)):
                return node
    raise AssertionError("RagPipeline.search not found - this guard is pointing at nothing")


def _appended_to(fn, list_name):
    """Lines of `<list_name>.append(_not_document_memory())`."""
    return [n.lineno for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "append"
            and isinstance(n.func.value, ast.Name) and n.func.value.id == list_name
            and len(n.args) == 1 and isinstance(n.args[0], ast.Call)
            and isinstance(n.args[0].func, ast.Name)
            and n.args[0].func.id == "_not_document_memory"]


def _and_splat(fn, list_name):
    """The `and_(*<list_name>)` a `.where(...)` consumes, plus that `.where` call."""
    for n in ast.walk(fn):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "where"):
            continue
        for a in n.args:
            if (isinstance(a, ast.Call) and isinstance(a.func, ast.Name) and a.func.id == "and_"
                    and any(isinstance(s, ast.Starred) and isinstance(s.value, ast.Name)
                            and s.value.id == list_name for s in a.args)):
                return a, n
    return None, None


def _executed_line(fn, where_call):
    """Line of the `db.execute(<stmt>)` that runs the statement carrying `where_call`."""
    for n in ast.walk(fn):
        if not (isinstance(n, ast.Assign) and n.targets and isinstance(n.targets[0], ast.Name)):
            continue
        if not any(x is where_call for x in ast.walk(n)):
            continue
        stmt_name = n.targets[0].id
        for e in ast.walk(fn):
            if (isinstance(e, ast.Call) and isinstance(e.func, ast.Attribute)
                    and e.func.attr == "execute"
                    and any(isinstance(a, ast.Name) and a.id == stmt_name for a in e.args)):
                return e.lineno
    return -1


_LANES = ("filters", "lexical_filters")


def test_both_lanes_of_the_hybrid_search_apply_it():
    """The fusion is only as clean as its worse half: the lexical lane would otherwise feed
    document chunks straight back into the RRF the vector lane just excluded them from."""
    fn = _search_fn()
    for lane in _LANES:
        assert len(_appended_to(fn, lane)) == 1, \
            f"{lane}: expected exactly one `{lane}.append(_not_document_memory())`"


def test_it_is_applied_in_sql_not_after_the_fetch():
    """The whole reason it is not the existing metadata_filter. Each append must reach the WHERE
    clause of the statement its own lane executes, and reach it BEFORE the execute - an append
    that lands after the statement is built is dead code that changes no query."""
    fn = _search_fn()
    for lane in _LANES:
        appended = _appended_to(fn, lane)[0]
        splat, where_call = _and_splat(fn, lane)
        assert splat is not None, f"{lane} never reaches a WHERE clause - the list is unused"
        executed = _executed_line(fn, where_call)
        assert executed > 0, f"{lane}: the statement carrying that WHERE is never executed"
        assert appended < splat.lineno < executed, (
            f"{lane}: append(line {appended}) -> and_(line {splat.lineno}) -> "
            f"execute(line {executed}) is not in that order"
        )


def test_the_exclusion_is_never_applied_after_the_fetch():
    """Post-fetch filtering costs recall: the database has already ranked AND truncated, so
    dropping documents afterwards returns the 2-4 personal chunks that happened to be in the
    window instead of ranking within the personal memories. The two filter appends must
    therefore be the only uses of the condition anywhere in this function."""
    fn = _search_fn()
    used = sorted(n.lineno for n in ast.walk(fn)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                  and n.func.id == "_not_document_memory")
    assert used == sorted(_appended_to(fn, "filters") + _appended_to(fn, "lexical_filters")), \
        "the exclusion is used outside the two filter appends - check the post-fetch loop"
    assert not [n for n in ast.walk(fn)
                if isinstance(n, ast.Name) and n.id == "_DOCUMENT_MEMORY_TYPES"], \
        "search() compares the type tuple directly - that is a post-fetch check, not SQL"


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
