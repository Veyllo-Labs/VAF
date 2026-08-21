# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""memory_update + memory_save's duplicate gate: change a memory, don't twin it.

The lane this pins: memory_save checks for a NEAR-DUPLICATE first (pure-cosine
vector search - the hybrid fusion's RRF ranks are unreadable for a similarity
bar) and, instead of writing a twin, answers with the existing memory's id and
hands the DECISION to the model: update in place via memory_update, or insist
on a separate save with confirm_new=true. The check is best-effort by design -
any failure saves anyway, because a duplicate check must never stand between
the user's "remember this" and the save the way a dead database would.
"""
import re
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

import vaf.tools.context_tools as ct

ROOT = Path(__file__).resolve().parents[1]
SCOPE = str(uuid4())


def _src(memory_id: str, text: str, score: float):
    return SimpleNamespace(memory_id=memory_id, text=text, score=score)


@pytest.fixture()
def ingest_recorder(monkeypatch):
    """Replace the async-bridge so no test ever touches a database. The recorder
    stands in for BOTH lanes that pass through it (ingest and update); the
    duplicate helper is patched separately per test."""
    calls = []

    def _fake_bridge(coro):
        coro.close()  # never awaited on purpose - silence the warning
        calls.append(coro)
        return "Memory stored."

    monkeypatch.setattr(ct, "_run_async_in_new_loop", _fake_bridge)
    return calls


def test_duplicate_found_hands_the_decision_back(monkeypatch, ingest_recorder):
    mem_id = str(uuid4())
    monkeypatch.setattr(ct, "_find_similar_memories",
                        lambda content, scope, k=3: [_src(mem_id, "User lives in Berlin", 0.95)])
    out = ct.MemorySaveTool().run(content="User lives in Berlin now",
                                  tags=["user"], user_scope_id=SCOPE)
    assert "Not saved yet" in out
    assert mem_id in out, "the existing memory's id must be named - it is what memory_update takes"
    assert "memory_update" in out and "confirm_new" in out
    assert not ingest_recorder, "a duplicate notice must not also save"


def test_confirm_new_saves_despite_duplicate(monkeypatch, ingest_recorder):
    monkeypatch.setattr(ct, "_find_similar_memories",
                        lambda *a, **k: pytest.fail("confirm_new must skip the duplicate check"))
    out = ct.MemorySaveTool().run(content="User lives in Berlin now",
                                  tags=["user"], user_scope_id=SCOPE, confirm_new=True)
    assert out == "Memory stored."
    assert len(ingest_recorder) == 1


def test_no_duplicates_saves_normally(monkeypatch, ingest_recorder):
    monkeypatch.setattr(ct, "_find_similar_memories", lambda *a, **k: [])
    out = ct.MemorySaveTool().run(content="A brand new fact",
                                  tags=["note"], user_scope_id=SCOPE)
    assert out == "Memory stored."
    assert len(ingest_recorder) == 1


def test_dedup_failure_fails_open_to_saving(monkeypatch, ingest_recorder):
    def _boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(ct, "_find_similar_memories", _boom)
    out = ct.MemorySaveTool().run(content="A fact", tags=["note"], user_scope_id=SCOPE)
    assert out == "Memory stored."
    assert len(ingest_recorder) == 1


def test_two_chunks_of_one_memory_are_one_answer(monkeypatch, ingest_recorder):
    mem_id = str(uuid4())
    monkeypatch.setattr(ct, "_find_similar_memories", lambda *a, **k: [
        _src(mem_id, "chunk one", 0.91),
        _src(mem_id, "chunk two", 0.93),
    ])
    out = ct.MemorySaveTool().run(content="x", tags=["t"], user_scope_id=SCOPE)
    assert out.count(mem_id) == 1, "chunk hits of the same memory must collapse to one line"
    assert "93%" in out, "the memory keeps its best chunk's score"


def test_memory_update_requires_a_real_uuid(ingest_recorder):
    out = ct.MemoryUpdateTool().run(memory_id="Source 1", content="new text",
                                    user_scope_id=SCOPE)
    assert out.startswith("Error:") and "UUID" in out
    assert not ingest_recorder


def test_memory_update_happy_path(monkeypatch, ingest_recorder):
    mem_id = str(uuid4())
    out = ct.MemoryUpdateTool().run(memory_id=mem_id, content="corrected fact",
                                    user_scope_id=SCOPE)
    assert out == "Memory stored."  # the patched bridge's canned answer
    assert len(ingest_recorder) == 1


def test_memory_update_unknown_id_is_a_clear_answer(monkeypatch):
    def _not_found(coro):
        coro.close()
        raise ValueError("Memory x not found")
    monkeypatch.setattr(ct, "_run_async_in_new_loop", _not_found)
    out = ct.MemoryUpdateTool().run(memory_id=str(uuid4()), content="text",
                                    user_scope_id=SCOPE)
    assert "no memory with id" in out
    assert "memory_search" in out


def test_document_memories_refuse_the_in_place_edit():
    """A learned document's section is a RECORD of what the source says. An
    in-place edit would replace source text with model prose while the section
    keeps wearing the document's name, and nothing can restore the original
    (no version history; the PDF may be gone). The refusal must name the
    honest lanes instead: learn_document for a new version, memory_save for a
    correcting note alongside."""
    refusal = ct._memory_update_refusal({"doc_tag": "mietrecht-2024", "tags": ["knowledge"]})
    assert refusal and "mietrecht-2024" in refusal
    assert "learn_document" in refusal and "memory_save" in refusal

    eph = ct._memory_update_refusal({"source": "attachment_ephemeral"})
    assert eph and "learn_attached_knowledge" in eph

    assert ct._memory_update_refusal({"source": "memory_save", "type": "note"}) is None
    assert ct._memory_update_refusal({}) is None
    assert ct._memory_update_refusal(None) is None


def test_update_consults_the_refusal_before_writing(monkeypatch):
    """The WIRING: the coroutine must ask _memory_update_refusal BEFORE calling
    update_memory - a guard that runs after the write protects nothing."""
    from contextlib import asynccontextmanager

    wrote = []

    class _FakePipeline:
        def __init__(self, db):
            pass

        async def get_memory(self, memory_id, decrypt=True, user_scope_id=None):
            return {"id": str(memory_id), "metadata": {"doc_tag": "howto-docker"}}

        async def update_memory(self, *a, **k):
            wrote.append(True)

    @asynccontextmanager
    async def _fake_db(user_scope_id=None):
        yield object()

    import vaf.memory.database as mdb
    import vaf.memory.rag as mrag
    monkeypatch.setattr(mdb, "get_db", _fake_db)
    monkeypatch.setattr(mrag, "RagPipeline", _FakePipeline)

    out = ct.MemoryUpdateTool().run(memory_id=str(uuid4()), content="new text",
                                    user_scope_id=SCOPE)
    assert "Not updated" in out and "howto-docker" in out
    assert not wrote, "the guard must refuse BEFORE update_memory runs"


def test_memory_update_rides_every_registry_memory_save_is_in():
    """The WIRING: the save's duplicate notice tells the model to call
    memory_update, so the two must travel together through every gate list -
    a lane where only one of them exists turns the notice into a dead end
    (or, in a room, into a write the authority gate never sees)."""
    from vaf.core.agent import Agent
    assert "memory_update" in Agent._ROOM_AUTHORITY_TOOLS

    src = (ROOT / "vaf" / "core" / "agent.py").read_text(encoding="utf-8")
    thinking_block = re.search(
        r"if _rk_thinking:\s*\n\s*if instance\.name in \(([^)]*)\)", src
    )
    assert thinking_block and "memory_update" in thinking_block.group(1), (
        "thinking runs exclude memory_save but not memory_update - a background "
        "run could write durable memory through the sibling"
    )
    core_tuple = re.search(
        r'for name in \(("update_intent"[^)]*)\):\s*\n\s*if name in self\.tools', src
    )
    assert core_tuple and "memory_update" in core_tuple.group(1), (
        "memory_update is not in the always-include core tuple, so the duplicate "
        "notice names a tool the restricted set cannot call"
    )


def test_declarations_match_the_sibling():
    """Scope injection and trainer refusal are declarative: identity_kwargs is
    what hands the tool its user scope, side_effect_class=irreversible is what
    keeps the Whare Wananga runner from probing it."""
    up = ct.MemoryUpdateTool
    assert up.permission_level == "write"
    assert up.side_effect_class == "irreversible"
    assert "user_scope_id" in up.identity_kwargs
