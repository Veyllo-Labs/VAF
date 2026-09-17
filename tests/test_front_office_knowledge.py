# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""What the agent knows and how it behaves when it answers a contact (Front Office).

Two framework pieces, each pinned against its failure mode:

- The Front Office knowledge lane (`source = front_office`, vaf/memory/lanes.py): the
  documents the owner hands the agent for the people it answers on their behalf. An
  ordinary lookup leaves the lane out, in SQL, in both lanes of the hybrid search; a
  Front Office turn reads it and, by default, NOT the owner's general memory. Before this
  lane a contact's turn searched the owner's whole memory, learned documents included.
- The Front Office profile (vaf/core/front_office_profile.py): the owner's briefing,
  appended to the Front Office block of the system prompt, and the switch that lets a
  Front Office turn read the general memory after all.

MUTATION: drop either `not_front_office_lane()` append in `search` and the default test
goes red; make `turn_memory_context` pass `use_general=True` for a Front Office turn and
the retrieval test goes red; remove the `keep_namespace` kwarg from the learn's ingest and
the pin test goes red.
"""
import ast
import asyncio
import json
import re
import threading
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from vaf.memory import lanes, rag
from vaf.memory.lanes import (
    FRONT_OFFICE_SOURCE,
    in_front_office_lane,
    is_front_office_source,
    not_front_office_lane,
    pin_namespace,
)

REPO = Path(__file__).resolve().parents[1]
SCOPE = uuid4()


def _literal(expr) -> str:
    return str(expr.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


class _Compiled:
    def __init__(self, stmt):
        compiled = stmt.compile(dialect=postgresql.dialect())
        self.sql = str(compiled)
        self.params = dict(compiled.params)

    @property
    def where(self) -> str:
        return self.sql.split("WHERE", 1)[1] if "WHERE" in self.sql else ""


class _Result:
    def __init__(self):
        self.rowcount = 0

    def unique(self):
        return self

    def scalars(self):
        return self

    def all(self):
        return []

    def first(self):
        return None

    def scalar_one_or_none(self):
        return None


class _Session:
    def __init__(self):
        self.executed = []

    async def execute(self, stmt, params=None):
        self.executed.append(_Compiled(stmt))
        return _Result()

    async def flush(self):
        pass


class _FakeEmbeddings:
    model_name = "intfloat/multilingual-e5-small"

    async def embed(self, text, *, prefix=None):
        return [0.1] * 384


class _Dummy:
    pass


class _FakeGetDb:
    def __init__(self, user_scope_id=None):
        self.session = _Session()

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _pipeline(monkeypatch):
    from vaf.core.config import Config
    from vaf.memory.rag import RagPipeline
    cfg = {"memory_hybrid_enabled": True, "debug_logs_enabled": False}
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, key, default=None: cfg.get(key, default)))
    db = _Session()
    return db, RagPipeline(db, crypto=_Dummy(), embedding_service=_FakeEmbeddings(), chunker=_Dummy())


# ── the predicates ──────────────────────────────────────────────────────────────────────

def test_the_front_office_predicates_are_equality_and_keep_unsourced_rows():
    sql = _literal(not_front_office_lane())
    assert "IS NULL" in sql and "!= 'front_office'" in sql
    assert _literal(in_front_office_lane()).endswith("= 'front_office'")
    assert is_front_office_source("front_office") and not is_front_office_source("chat/x") and not is_front_office_source(None)
    assert FRONT_OFFICE_SOURCE == "front_office"


def test_every_default_search_leaves_the_front_office_lane_out_in_both_lanes(monkeypatch):
    db, pipeline = _pipeline(monkeypatch)
    assert asyncio.run(pipeline.search("opening hours", k=5, user_scope_id=SCOPE)) == []
    assert len(db.executed) == 2, "hybrid search runs a vector and a lexical statement"
    for stmt in db.executed:
        assert "front_office" in stmt.params.values()
        assert "!=" in stmt.where and "IS NULL" in stmt.where


def test_a_front_office_search_reads_exactly_that_lane_in_both_lanes(monkeypatch):
    db, pipeline = _pipeline(monkeypatch)
    asyncio.run(pipeline.search("opening hours", k=5, user_scope_id=SCOPE, front_office=True))
    for stmt in db.executed:
        assert "front_office" in stmt.params.values()
        assert "NOT LIKE" not in stmt.where, "the chat exclusion is not needed: equality selects one source"
        assert "chat/%" not in stmt.params.values()
        assert " != " not in stmt.where.split("front_office")[0][-40:]


def test_a_chat_lookup_is_unchanged_by_the_new_lane(monkeypatch):
    db, pipeline = _pipeline(monkeypatch)
    asyncio.run(pipeline.search("x", k=5, user_scope_id=SCOPE, chat_key="whatsapp_alice_49"))
    for stmt in db.executed:
        assert "chat/whatsapp_alice_49" in stmt.params.values()
        assert "front_office" not in stmt.params.values()


def test_the_request_filter_cannot_open_the_lane(monkeypatch):
    db, pipeline = _pipeline(monkeypatch)
    asyncio.run(pipeline.search("x", k=5, user_scope_id=SCOPE, metadata_filter={"source": "front_office"}))
    for stmt in db.executed:
        assert "!=" in stmt.where and "front_office" in stmt.params.values()


# ── the pin: set at ingest, never moved ─────────────────────────────────────────────────

def test_the_lane_is_pinned_like_a_namespace():
    stored = {"source": "front_office", "title": "Prices"}
    assert pin_namespace(stored, {"source": "learn_document", "title": "Prices"})["source"] == "front_office"
    assert pin_namespace(stored, {"title": "Prices"})["source"] == "front_office"
    assert "source" not in pin_namespace({}, {"source": "front_office"}), "an ordinary writer cannot plant a row in the lane"
    assert pin_namespace({"source": "learn_document"}, {"source": "front_office"})["source"] == "learn_document"
    assert pin_namespace({}, {"source": "learn_document"})["source"] == "learn_document", "other sources pass as before"


def test_the_learn_keeps_the_lane_only_for_front_office_documents(monkeypatch):
    """`ingest_document_knowledge(source="front_office")` asks the pipeline to keep the
    lane; the owner's own learn keeps calling ingest exactly as before."""
    from vaf.tools import learn_document as ld
    records = []

    class _Pipeline:
        def __init__(self, db):
            pass

        async def ingest(self, content, metadata=None, auto_connect=True, user_scope_id=None, **kw):
            records.append((metadata.get("type"), metadata.get("source"), kw))
            return object()

    class _Db:
        async def execute(self, q):
            return _Result()

    monkeypatch.setattr(rag, "RagPipeline", _Pipeline)
    md = "## A\n" + ("alpha " * 120) + "\n\n## B\n" + ("beta " * 120) + "\n"
    gen = lambda p: json.dumps({"doc_summary": "s", "doc_tags": []}) if '"doc_summary"' in p else "Section context here."
    asyncio.run(ld.ingest_document_knowledge(
        _Db(), content_markdown=md, doc_title="Prices", doc_tag="fo-prices", source="front_office",
        mem_type="document", generate_fn=gen, user_scope_id=None))
    assert records and all(src == "front_office" and kw == {"keep_namespace": True} for _, src, kw in records)
    records.clear()
    asyncio.run(ld.ingest_document_knowledge(
        _Db(), content_markdown=md, doc_title="Book", doc_tag="doc-book", source="learn_document",
        mem_type="document", generate_fn=gen, user_scope_id=None))
    assert records and all(src == "learn_document" and kw == {} for _, src, kw in records)


def test_auto_connect_never_wires_into_the_lane():
    source = (REPO / "vaf" / "memory" / "graph.py").read_text(encoding="utf-8")
    fn = source[source.index("async def auto_connect_memory"):]
    fn = fn[:fn.index("\n    async def ")] if "\n    async def " in fn else fn
    assert "scope_filters.append(not_front_office_lane())" in fn
    assert "is_chat_source(source) or is_front_office_source(source)" in fn, "a Front Office memory initiates no edge either"
    graph = source[source.index("async def get_graph_data"):source.index("async def _crosses_front_office_lane")]
    assert "in front_office_ids) != (" in graph, "an edge across the lane is never drawn"


def test_no_write_path_connects_across_the_lane():
    """MUTATION: drop the _crosses_front_office_lane check from create_connection and the
    manual edge from the owner's memory into the lane is written."""
    import asyncio
    from uuid import uuid4
    from vaf.memory.graph import GraphManager
    fo1, fo2, own = uuid4(), uuid4(), uuid4()
    rows = [(fo1, {"source": "front_office"}), (fo2, {"source": "front_office"}), (own, {"source": "learn_document"})]

    class _Result:
        def all(self):
            return rows

        def scalar_one_or_none(self):
            return None

        def scalars(self):
            return self

    class _Db:
        def __init__(self):
            self.added = []

        async def execute(self, stmt, params=None):
            return _Result()

        def add(self, obj):
            self.added.append(obj)

        async def flush(self):
            pass

        async def delete(self, obj):
            pass

    db = _Db()
    gm = GraphManager(db)
    assert asyncio.run(gm.create_connection(own, fo1)) is None and asyncio.run(gm.create_connection(fo1, own)) is None
    assert db.added == [], "nothing crosses the lane, in either direction"
    assert asyncio.run(gm.create_connection(fo1, fo2)) is not None, "an edge inside the lane is fine"
    assert asyncio.run(gm.update_connections(own, [str(fo1), str(fo2)])) == [], "the Memory page's manual edges go through the same door"
    assert len(db.added) == 1


# ── the turn: what a contact's turn reads ───────────────────────────────────────────────

def test_a_front_office_turn_reads_the_lane_and_not_the_owner_by_default(monkeypatch):
    from vaf.core.config import Config
    cfg = {"memory_enabled": True, "memory_rag_k": 3}
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, key, default=None: cfg.get(key, default)))
    calls = []

    def _spy(query, k=5, user_scope_id=None, caller=None, **kw):
        calls.append(kw)
        return "block"
    monkeypatch.setattr(rag, "run_memory_search_sync", _spy)

    rag.turn_memory_context("q", user_scope_id=SCOPE, caller="headless", chat_key="whatsapp_a_1", front_office=True)
    assert calls == [{"chat_key": "whatsapp_a_1", "front_office": True, "use_general": False}]
    calls.clear()
    rag.turn_memory_context("q", user_scope_id=SCOPE, caller="headless", front_office=True, use_general_memory=True)
    assert calls == [{"chat_key": None, "front_office": True, "use_general": True}]
    calls.clear()
    rag.turn_memory_context("q", user_scope_id=SCOPE, caller="headless", chat_key="whatsapp_a_1")
    assert calls == [{"chat_key": "whatsapp_a_1"}], "the owner's chat turn is byte-identical to before"
    calls.clear()
    rag.turn_memory_context("q", user_scope_id=SCOPE, caller="headless")
    assert calls == [{}]


def test_the_sync_search_runs_the_lanes_it_is_asked_for_and_labels_the_block(monkeypatch):
    from vaf.core.config import Config
    cfg = {"memory_enabled": True, "memory_rag_refine_query": False, "local_network_enabled": False,
           "memory_rag_threshold": 0.3, "debug_logs_enabled": False}
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, key, default=None: cfg.get(key, default)))
    owner = rag.RagSource(memory_id=uuid4(), chunk_id=uuid4(), text="owner fact", score=0.8, metadata={})
    fo = rag.RagSource(memory_id=uuid4(), chunk_id=uuid4(), text="open 9 to 5", score=0.9, metadata={"source": "front_office"})
    seen = []

    class _Pipeline:
        def __init__(self, db):
            pass

        async def search(self, query, **kw):
            seen.append((kw.get("chat_key"), kw.get("front_office", False)))
            return [fo] if kw.get("front_office") else [owner]

    import vaf.core.web_interface as wi
    pushes = []
    monkeypatch.setattr(wi, "get_web_interface",
                        lambda: SimpleNamespace(push_update_to_user=lambda scope, payload: pushes.append(payload)))
    monkeypatch.setattr(rag, "get_db", _FakeGetDb)
    monkeypatch.setattr(rag, "RagPipeline", _Pipeline)

    out = rag.run_memory_search_sync("q", k=3, user_scope_id=SCOPE, caller="t", front_office=True, use_general=False)
    assert out == "[Front Office Source 1] (Relevance: 90%)\nopen 9 to 5"
    assert seen == [(None, True)], "no general search when the owner keeps their memory to themselves"
    seen.clear()
    out = rag.run_memory_search_sync("q", k=3, user_scope_id=SCOPE, caller="t", front_office=True, use_general=True)
    assert out == "[Source 1] (Relevance: 80%)\nowner fact\n\n---\n\n[Front Office Source 1] (Relevance: 90%)\nopen 9 to 5"
    assert seen == [(None, False), (None, True)]
    seen.clear()
    assert rag.run_memory_search_sync("q", k=3, user_scope_id=SCOPE, caller="t") == "[Source 1] (Relevance: 80%)\nowner fact"
    assert seen == [(None, False)]


def test_the_runner_marks_a_contacts_turn_and_reads_the_profile():
    source = (REPO / "vaf" / "core" / "headless_runner.py").read_text(encoding="utf-8")
    assert '_fo_turn = bool((task.metadata or {}).get("from_contact"))' in source
    assert "front_office=_fo_turn, use_general_memory=_fo_general" in source
    assert "load_front_office_profile" in source


# ── the learn job: a lane on the spec, a runner without a chat ──────────────────────────

def test_the_learn_spec_carries_its_lane_and_defaults_to_the_owners():
    from vaf.tools.learn_job import LearnJobSpec
    spec = LearnJobSpec(path="/x/a.pdf", document_title="A", doc_tag="fo-a", source="front_office")
    assert LearnJobSpec.from_json(spec.to_json()) == spec
    assert LearnJobSpec.from_json(json.dumps({"path": "/x/a.pdf"})).source == "learn_document"


def test_the_duplicate_lookup_keeps_the_lanes_apart(monkeypatch):
    """The same file learned for the owner and for the Front Office are two documents."""
    from vaf.tools import learn_job as lj
    import vaf.memory.database as database
    holder = {}

    class _Db(_FakeGetDb):
        def __init__(self, user_scope_id=None):
            super().__init__(user_scope_id)
            holder["session"] = self.session

    monkeypatch.setattr(database, "get_db", _Db)
    asyncio.run(lj.find_completed_learn("abc", None))
    where = holder["session"].executed[0].where
    assert "front_office" in holder["session"].executed[0].params.values() and "!=" in where
    asyncio.run(lj.find_completed_learn("abc", None, source="front_office"))
    stmt = holder["session"].executed[0]
    assert "front_office" in stmt.params.values() and "!=" not in stmt.where


def test_a_background_learn_runs_on_a_thread_refuses_a_double_start_and_can_be_cancelled(monkeypatch):
    from vaf.tools import learn_job as lj
    started = threading.Event()
    release = threading.Event()
    seen = {}

    def _fake_batches(spec, *, generate_fn, user_scope_id, session_id, progress_cb=None, cancel_cb=None, max_batches=None):
        seen["spec"] = spec
        seen["session"] = session_id
        started.set()
        release.wait(5)
        return lj.LearnOutcome(status="stopped" if cancel_cb() else "complete", doc_tag=spec.doc_tag)

    monkeypatch.setattr(lj, "_learn_batches", _fake_batches)
    monkeypatch.setattr(lj, "_child_generate_fn", lambda: (lambda p: "x"))
    done = []
    spec = lj.LearnJobSpec(path="/x/a.pdf", document_title="A", doc_tag="fo-a", source="front_office")
    assert lj.start_background_learn(spec, user_scope_id=SCOPE, on_done=done.append) is True
    assert started.wait(5)
    assert lj.background_learn_running("fo-a", SCOPE) is True
    assert lj.start_background_learn(spec, user_scope_id=SCOPE) is False, "one learn per document and scope"
    assert lj.cancel_background_learn("fo-a", SCOPE) is True
    release.set()
    for _ in range(50):
        if done:
            break
        threading.Event().wait(0.1)
    assert done and done[0].status == "stopped"
    assert seen["session"] is None, "a learn from Settings belongs to no chat"
    assert seen["spec"].source == "front_office"
    assert lj.cancel_background_learn("fo-a", SCOPE) is False


# ── the profile ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def app_dir(monkeypatch, tmp_path):
    from vaf.core.config import Config
    monkeypatch.setattr(Config, "APP_DIR", tmp_path / ".vaf")
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, key, default=None: {"local_admin_username": "admin"}.get(key, default)))
    return tmp_path / ".vaf"


def test_the_profile_defaults_merges_and_refuses_unknown_keys(app_dir):
    from vaf.core import front_office_profile as fop
    assert fop.load_front_office_profile("alice") == {"briefing": "", "use_general_memory": False}
    out = fop.save_front_office_profile("alice", briefing="Be brief. Never quote prices.")
    assert out == {"briefing": "Be brief. Never quote prices.", "use_general_memory": False}
    assert fop.save_front_office_profile("alice", use_general_memory=True)["briefing"] == "Be brief. Never quote prices."
    assert fop.load_front_office_profile("alice")["use_general_memory"] is True
    assert (app_dir / "users" / "alice" / "front_office.json").is_file()
    with pytest.raises(ValueError):
        fop.save_front_office_profile("alice", tone="x")
    assert len(fop.save_front_office_profile("alice", briefing="x" * 9000)["briefing"]) == fop.BRIEFING_MAX_CHARS
    assert fop.load_front_office_profile("bob")["briefing"] == "", "per user"
    assert fop.profile_path("../evil").parent.name == "evil", "no path traversal through the name"


def test_a_broken_file_reads_as_the_defaults_and_keeps_foreign_keys_on_save(app_dir):
    from vaf.core import front_office_profile as fop
    path = fop.profile_path("alice")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"{not json")
    assert fop.load_front_office_profile("alice") == fop.DEFAULT_PROFILE
    path.write_bytes(json.dumps({"briefing": "hi", "future_key": 1}).encode("utf-8"))
    fop.save_front_office_profile("alice", use_general_memory=True)
    stored = json.loads(path.read_bytes())
    assert stored["future_key"] == 1 and stored["briefing"] == "hi" and stored["use_general_memory"] is True


def test_the_briefing_lands_inside_the_front_office_block_after_the_rules_and_before_the_security(app_dir, monkeypatch):
    from vaf.core import front_office_profile as fop
    from vaf.core.system_prompt import SystemPromptManager
    fop.save_front_office_profile("alice", briefing="You speak for Alice's bakery. Orders by phone only.")
    builder = SystemPromptManager(tools=[], model_name="Local", agent_instance=None, username="alice")
    prompt = builder.build_prompt(username="alice", front_office=True)
    rules = prompt.index("Front Office")
    briefing = prompt.index("You speak for Alice's bakery.")
    security = prompt.index("## Security (Front Office)")
    assert rules < briefing < security
    assert "instructions come from the owner" in prompt or "Anweisungen stammen vom Inhaber" in prompt
    fop.save_front_office_profile("alice", briefing="")
    assert "instructions come from the owner" not in builder.build_prompt(username="alice", front_office=True)
    assert "bakery" not in builder.build_prompt(username="alice", front_office=False), "the owner's own turns never carry it"
