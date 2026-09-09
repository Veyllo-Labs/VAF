# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A compaction can learn into a chat namespace, and a turn inside that chat reads it back.

`run_session_compaction_sync(chat=ChatNamespace(...))` is the writer: the prompt asks about
the person in that chat, the facts land under `source = chat/<session id>` with the namespace
keys, the dedup check looks only inside the namespace, the user-profile cache is never
refreshed, and the transcript comes from the STORED session (the live agent history holds the
Front Office wrapper). `turn_memory_context(chat_key=)` is the reader: the general call every
lane makes, unchanged, plus a second `[Chat Source N]` block.

MUTATION: drop `chat.as_meta()` from the ingest meta, or the `chat_key` from the dedup
search, or the `chat is not None` guard in the profile refresh, and the chat-run test goes
red; make the second retrieval call unconditional and the reader test goes red.
"""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

from vaf.memory import rag
from vaf.memory.lanes import ChatNamespace

SCOPE = uuid4()
ALICE = ChatNamespace("whatsapp_alice_491700000042", "whatsapp", "Alice")


class _FakePipeline:
    searches = []
    ingests = []

    def __init__(self, db):
        pass

    async def search(self, query, **kw):
        _FakePipeline.searches.append(kw)
        return []

    async def ingest(self, content, metadata=None, auto_connect=True, user_scope_id=None, keep_namespace=False):
        _FakePipeline.ingests.append({"content": content, "meta": dict(metadata or {}),
                                      "auto_connect": auto_connect, "scope": user_scope_id,
                                      "keep_namespace": keep_namespace})
        return object()


class _FakeGetDb:
    def __init__(self, user_scope_id=None):
        pass

    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _SyncThread:
    """Runs the target inline so the profile refresh and the ingest are observable."""

    def __init__(self, target=None, daemon=None):
        self._target = target

    def start(self):
        self._target()

    def join(self, timeout=None):
        pass

    def is_alive(self):
        return False


class _Agent:
    def __init__(self, reply):
        self.reply = reply
        self.prompts = []
        self.history = [{"role": "user", "content": "[FRONT OFFICE - MESSAGE FROM A CONTACT] birthday 1 May"},
                        {"role": "assistant", "content": "noted"}]

    def _generate_for_compaction(self, prompt):
        self.prompts.append(prompt)
        return self.reply


def _arm(monkeypatch, reply='MEMORY: "Alice prefers to be called Ali." [preferences]'):
    from vaf.core.config import Config

    _FakePipeline.searches = []
    _FakePipeline.ingests = []
    cfg = {"memory_enabled": True, "memory_compaction_enabled": True, "memory_compaction_interval": 1,
           "debug_logs_enabled": False}
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, key, default=None: cfg.get(key, default)))
    monkeypatch.setattr(rag, "get_db", _FakeGetDb)
    monkeypatch.setattr(rag, "RagPipeline", _FakePipeline)
    monkeypatch.setattr(rag.threading, "Thread", _SyncThread)
    monkeypatch.setattr(rag, "_load_compaction_state", lambda: {})
    saved = []
    monkeypatch.setattr(rag, "_save_compaction_state", lambda state: saved.append(dict(state)))
    monkeypatch.setattr(rag, "_trim_telegram_history_after_compaction", lambda **kw: None)
    refreshed = []
    monkeypatch.setattr(rag, "refresh_user_profile_summary", lambda scope: refreshed.append(scope))
    import vaf.core.web_interface as wi
    monkeypatch.setattr(wi, "get_web_interface", lambda: SimpleNamespace(_push_session_update=lambda *a, **k: None))
    return _Agent(reply), saved, refreshed


def test_a_chat_run_writes_into_its_namespace_and_dedups_only_there(monkeypatch):
    agent, saved, refreshed = _arm(monkeypatch)
    rag.run_session_compaction_sync(agent, SCOPE, ALICE.key, 1,
                                    conversation="Alice: call me Ali\n\nAssistant: sure", chat=ALICE)
    assert len(_FakePipeline.ingests) == 1
    rec = _FakePipeline.ingests[0]
    assert rec["meta"] == {"source": "chat/whatsapp_alice_491700000042", "type": "conversation",
                           "tags": ["preferences"], "chat_key": ALICE.key,
                           "chat_channel": "whatsapp", "chat_label": "Alice"}
    assert rec["auto_connect"] is False and rec["scope"] == SCOPE
    assert rec["keep_namespace"] is True, "the compaction is the one writer that owns the namespace keys"
    assert _FakePipeline.searches[0]["chat_key"] == ALICE.key, "the dedup check must stay inside the namespace"
    assert _FakePipeline.searches[0]["hybrid"] is False, "pure cosine, or one shared token dedups everything"
    assert refreshed == [], "a chat run never rebuilds the owner's profile cache"
    assert saved == [{ALICE.key: 1}]
    prompt = agent.prompts[0]
    assert "person the assistant is talking to in this chat: Alice" in prompt
    assert "Do NOT store facts about the assistant's owner" in prompt
    assert "GROUNDING" in prompt and "SELF-CONTAINED" in prompt and "NO_REPLY" in prompt
    assert "Alice: call me Ali" in prompt and "[FRONT OFFICE" not in prompt


def test_a_chat_run_that_finds_nothing_still_leaves_the_profile_cache_alone(monkeypatch):
    agent, saved, refreshed = _arm(monkeypatch, reply="NO_REPLY")
    rag.run_session_compaction_sync(agent, SCOPE, ALICE.key, 1, conversation="Alice: hi", chat=ALICE)
    assert _FakePipeline.ingests == [] and refreshed == [] and saved == [{ALICE.key: 1}]


def test_a_chat_run_without_a_transcript_never_reads_the_agent_or_calls_the_model(monkeypatch):
    agent, saved, refreshed = _arm(monkeypatch)
    rag.run_session_compaction_sync(agent, SCOPE, ALICE.key, 1, conversation="", chat=ALICE)
    rag.run_session_compaction_sync(agent, SCOPE, ALICE.key, 1, conversation=None, chat=ALICE)
    assert agent.prompts == [] and _FakePipeline.ingests == [] and saved == []


def test_an_owner_run_is_unchanged(monkeypatch):
    agent, saved, refreshed = _arm(monkeypatch)
    rag.run_session_compaction_sync(agent, SCOPE, "web_session", 1)
    rec = _FakePipeline.ingests[0]
    assert rec["meta"]["source"].startswith("memory/") and "chat_key" not in rec["meta"]
    assert rec["keep_namespace"] is False
    assert _FakePipeline.searches[0]["chat_key"] is None and _FakePipeline.searches[0]["hybrid"] is False
    assert refreshed == [SCOPE]
    assert "You are storing durable memories from this chat." in agent.prompts[0]
    assert agent.prompts[0].count("Assistant: noted") == 1, "the owner run still reads the agent history"


def test_an_explicit_source_still_wins_over_the_chat_source(monkeypatch):
    """The room lane names its own source; a caller that does both keeps the explicit one."""
    agent, saved, refreshed = _arm(monkeypatch)
    rag.run_session_compaction_sync(agent, SCOPE, ALICE.key, 1, conversation="Alice: hi",
                                    source="room/r1", chat=ALICE)
    assert _FakePipeline.ingests[0]["meta"]["source"] == "room/r1"
    assert _FakePipeline.ingests[0]["meta"]["chat_key"] == ALICE.key


def test_the_stored_session_is_the_transcript_and_carries_the_persons_name(monkeypatch):
    import vaf.core.session as session_mod

    messages = [SimpleNamespace(role="system", content="prompt"),
                SimpleNamespace(role="user", content="call me Ali\nplease"),
                SimpleNamespace(role="tool", content="{}"),
                SimpleNamespace(role="assistant", content="sure, Ali")]
    monkeypatch.setattr(session_mod, "SessionManager",
                        lambda: SimpleNamespace(load=lambda sid: SimpleNamespace(messages=messages)))
    out = rag.session_dialogue_excerpt("whatsapp_alice_49", user_label="Alice")
    assert out == "Alice: call me Ali please\n\nAssistant: sure, Ali"

    def _boom(sid):
        raise FileNotFoundError(sid)
    monkeypatch.setattr(session_mod, "SessionManager", lambda: SimpleNamespace(load=_boom))
    assert rag.session_dialogue_excerpt("whatsapp_alice_49", user_label="Alice") == ""


def test_the_agent_excerpt_is_byte_identical_for_the_owner():
    agent = SimpleNamespace(history=[{"role": "system", "content": "x"},
                                     {"role": "user", "content": "hi\nthere"},
                                     {"role": "assistant", "content": "hello"}])
    assert rag._build_compaction_conversation_excerpt(agent) == "User: hi there\n\nAssistant: hello"


def test_a_turn_inside_a_chat_names_its_namespace_in_the_one_call_every_lane_makes(monkeypatch):
    from vaf.core.config import Config

    cfg = {"memory_enabled": True, "memory_rag_k": 3}
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, key, default=None: cfg.get(key, default)))
    calls = []

    def _spy(query, k=5, user_scope_id=None, caller=None, **kw):
        calls.append((query, k, user_scope_id, caller, kw))
        return "block"
    monkeypatch.setattr(rag, "run_memory_search_sync", _spy)

    assert rag.turn_memory_context("q", user_scope_id=SCOPE, caller="headless") == "block"
    assert calls == [("q", 3, SCOPE, "headless", {})], "without a key the general call is the only call, unchanged"
    calls.clear()
    assert rag.turn_memory_context("q", user_scope_id=SCOPE, caller="headless", chat_key=ALICE.key) == "block"
    assert calls == [("q", 3, SCOPE, "headless", {"chat_key": ALICE.key})], "one call, the key on it"


def test_the_search_carries_both_lanes_in_one_block_and_one_snippet_push(monkeypatch):
    """The general lane first, then the chat's namespace, formatted as two blocks, pushed to
    the owner's RAG-Snippets panel ONCE with both lists: a second push would replace the
    first and the panel would under-report exactly the turns this lane exists for."""
    from vaf.core.config import Config

    cfg = {"memory_enabled": True, "memory_rag_refine_query": False, "local_network_enabled": False,
           "memory_rag_threshold": 0.3, "debug_logs_enabled": False}
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, key, default=None: cfg.get(key, default)))
    owner = rag.RagSource(memory_id=uuid4(), chunk_id=uuid4(), text="owner fact", score=0.8, metadata={})
    chat = rag.RagSource(memory_id=uuid4(), chunk_id=uuid4(), text="Alice wants Ali", score=0.9,
                         metadata={"chat_key": ALICE.key})
    seen = []

    class _Pipeline:
        def __init__(self, db):
            pass

        async def search(self, query, **kw):
            seen.append(kw.get("chat_key"))
            return [chat] if kw.get("chat_key") else [owner]
    pushes = []
    import vaf.core.web_interface as wi
    monkeypatch.setattr(wi, "get_web_interface",
                        lambda: SimpleNamespace(push_update_to_user=lambda scope, payload: pushes.append(payload)))
    monkeypatch.setattr(rag, "get_db", _FakeGetDb)
    monkeypatch.setattr(rag, "RagPipeline", _Pipeline)

    out = rag.run_memory_search_sync("q", k=3, user_scope_id=SCOPE, caller="t", chat_key=ALICE.key)
    assert out == "[Source 1] (Relevance: 80%)\nowner fact\n\n---\n\n[Chat Source 1] (Relevance: 90%)\nAlice wants Ali"
    assert seen == [None, ALICE.key]
    assert len(pushes) == 1 and [s["text"] for s in pushes[0]["sources"]] == ["owner fact", "Alice wants Ali"]
    assert rag.count_sources(out) == 2
    seen.clear(); pushes.clear()
    assert rag.run_memory_search_sync("q", k=3, user_scope_id=SCOPE, caller="t") == "[Source 1] (Relevance: 80%)\nowner fact"
    assert seen == [None] and len(pushes) == 1


def test_the_chat_header_is_formatted_by_the_shared_formatter_and_rejected_as_a_fact():
    src = rag.RagSource(memory_id=uuid4(), chunk_id=uuid4(), text="Alice wants Ali", score=0.9, metadata={})
    assert rag._format_sources([src], label="Chat Source").startswith("[Chat Source 1] (Relevance: 90%)\n")
    assert rag.count_sources("") == 0
    kept, rejected = rag._apply_fact_gates([("[Chat Source 1] (Relevance: 90%) Alice wants Ali", [])])
    assert kept == [] and rejected[0][1] == "junk_marker"


def test_the_dead_contact_session_check_is_gone():
    assert not hasattr(rag, "_is_contact_session")
