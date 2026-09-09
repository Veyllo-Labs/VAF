# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A chat the agent answers learns into its own namespace (vaf/core/headless_runner.py, the bridges).

The runner used to skip compaction for every task carrying `from_contact` (the GDPR gate).
Now that task learns into the chat's namespace: the enqueue carries the namespace keys, the
queued worker hands the STORED transcript and the namespace to the writer, and the turn's
retrieval names the same key. The bridges carry the person's name for the graph label. No
bridge harness exists in tests/, so these are source guards in the idiom of
test_a2a_room_learning.py, plus the metadata assertions the WhatsApp dispatch tests make.

MUTATION: restore the skip, drop `chat_key=` from the retrieval call, or drop `chat=` from the
worker call, and a guard goes red.
"""
from pathlib import Path

VAF = Path(__file__).resolve().parent.parent / "vaf"
RUNNER = (VAF / "core" / "headless_runner.py").read_text(encoding="utf-8")


def _between(src: str, start: str, end: str) -> str:
    i = src.index(start)
    return src[i:src.index(end, i)]


def test_the_routing_guard_lets_a_messenger_compaction_through():
    """The runner enqueues every compaction with source "web", messenger sessions included;
    the cross-channel routing guard drops web tasks aimed at messenger sessions. Without the
    exemption a WhatsApp or Telegram chat never compacted into its namespace."""
    guard = RUNNER.split("[ROUTING_BLOCK]", 1)[0].rsplit("if (", 1)[1]
    assert 'meta.get("compaction") is not True' in guard, "the guard must exempt the runner's own compaction task"
    assert 'source == "web"' in guard and '("telegram_", "discord_", "whatsapp_")' in guard


def test_the_gdpr_skip_is_gone_and_the_namespace_decides():
    assert "contact_chat_dsgvo" not in RUNNER
    assert "never learn from other people's messages" not in RUNNER
    block = _between(RUNNER, "COMPACTION_CHECK_START", "COMPACTION_CHECK_END")
    assert "ChatNamespace.from_task(task.session_id, _task_meta)" in block
    assert "_chat_ns.as_meta()" in block[block.index("tq.add("):], "the namespace keys travel in the queue task"


def test_the_contact_turn_retrieves_its_own_namespace_and_counts_both_blocks():
    block = _between(RUNNER, "# RAG: fetch memory context", "RAG Ergebnis")
    assert "ChatNamespace.from_task(task.session_id" in block
    assert "chat_key=(_chat_ns.key if _chat_ns else None)" in block
    assert "count_sources(memory_context)" in block
    assert 'count("[Source ")' not in RUNNER and "count('[Source ')" not in RUNNER, \
        "a chat-only turn would log snippets=0 with a hand-rolled count"


def test_the_queued_worker_hands_the_stored_transcript_and_the_namespace_to_the_writer():
    block = _between(RUNNER, "is_compaction = ", "(compaction)")
    assert "ChatNamespace.from_meta(task.metadata or {})" in block
    assert "session_dialogue_excerpt(task.session_id, user_label=_chat.label) if _chat else None" in block
    assert "conversation=_conversation, chat=_chat" in block


def test_a_telegram_contact_is_front_office_whether_the_burst_starts_with_text_or_voice():
    src = (VAF / "api" / "telegram_bridge.py").read_text(encoding="utf-8")
    text = _between(src, "async def handle_message", "async def handle_voice")
    voice = _between(src, "async def handle_voice", "async def handle_document")
    for part in (text, voice):
        assert '"from_contact": bool(entry.get("from_contact")),' in part
        assert '"chat_label": _telegram_display_name(user),' in part
    flush = _between(src, "async def _delayed_flush", "async def handle_message")
    assert 'metadata["chat_label"] = str(pending.get("chat_label") or "")' in flush
    image = _between(src, "async def _enqueue_telegram_image", "async def handle_message")
    assert 'metadata["chat_label"] = str(chat_label or "")' in image
    assert src.count("chat_label=_telegram_display_name(user),") == 2, "both image callers pass the label"


def test_a_relay_contact_gets_no_learning_counter_in_the_telegram_pane(monkeypatch, tmp_path):
    """A relay contact is answered by nobody and never compacts; a counter there would count
    turns that never learn."""
    import asyncio
    from types import SimpleNamespace

    import vaf.core.session as session_mod
    from vaf.api import telegram_routes as routes
    from vaf.core.config import Config

    # The route loads the session and its compaction state through SessionManager, which
    # hangs off ~/.vaf and not off Platform.data_dir: redirected here, or the assertions
    # would read whatever chats the developer's own installation holds under these ids.
    monkeypatch.setattr(session_mod, "default_sessions_dir", lambda: tmp_path / "sessions")

    cfg = {"telegram_config": {"whitelist": [{"telegram_user_id": "7", "user_scope_id": "s"}],
                               "relay_whitelist": [{"telegram_user_id": "9", "user_scope_id": "s"}]},
           "memory_compaction_interval": 15}
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, key, default=None: cfg.get(key, default)))
    monkeypatch.setattr(routes, "_is_telegram_admin", lambda request: True)
    monkeypatch.setattr(routes, "get_current_vaf_user", lambda request: {"user_scope_id": "s", "username": "alice"})
    request = SimpleNamespace(state=SimpleNamespace(user={}))
    owner = asyncio.run(routes.get_telegram_session_history("telegram_7", request))
    relay = asyncio.run(routes.get_telegram_session_history("telegram_9", request))
    assert "compaction_interval" in owner and owner["messages"] == []
    assert "compaction_interval" not in relay and "user_turn_count" not in relay and relay["messages"] == []


def test_the_whatsapp_bridge_labels_a_contact_task_and_never_the_owner():
    src = (VAF / "api" / "whatsapp_bridge.py").read_text(encoding="utf-8")
    block = _between(src, 'from_contact = policy_reason != "explicit_pair"', "7-second debounce")
    assert 'metadata["chat_label"] = _chat_label(' in block[block.index("if from_contact:"):]
