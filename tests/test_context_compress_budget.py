# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Compression fires on a cost budget for APIs, and its state actually persists.

Live incident behind every test here: a 559-message chat (three weeks old) sat
at ~65k tokens, far below 85% of the 128k window, so no compression lane ever
fired and every LLM round-trip resent the whole history on a paid API. On top,
`manage_context` built a SECOND ContextManager next to the one `__init__`
registered with the state registry, so a checkpoint's narrative summary was
stored on an object the session snapshot never serialized - the persisted
context state stayed empty and a restart replayed the full transcript back in.
"""
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# Mock llama_cpp before importing Agent.
sys.modules.setdefault("llama_cpp", MagicMock())

from vaf.core.agent import Agent
from vaf.core.config import Config
from vaf.core.context import ContextManager
from vaf.core.session_state import StateRegistry
from vaf.core.state_providers.context_state import ContextStateProvider


@pytest.fixture
def archive_dir(tmp_path, monkeypatch):
    """Keep context archives out of the real user store."""
    monkeypatch.setattr(ContextManager, "ARCHIVE_DIR", tmp_path / "context_archive")
    return tmp_path


def _fat_history(n_messages: int = 40, chars: int = 220):
    history = [{"role": "system", "content": "system prompt"}]
    for i in range(n_messages):
        role = "user" if i % 2 == 0 else "assistant"
        history.append({"role": role, "content": f"m{i} " + "x" * chars})
    return history


def _agent_stub(archive_dir, cm_tokens=30000, budget=500):
    a = Agent.__new__(Agent)
    a.context_manager = ContextManager(max_tokens=cm_tokens)
    a.api_backend = SimpleNamespace(last_request_usage={})
    a.config = SimpleNamespace(
        get=lambda key, default=None: budget if key == "context_compress_tokens" else default
    )
    a.history = []
    a.main_persistence = None
    return a


def test_set_max_tokens_rederives_thresholds_and_preserves_state(archive_dir):
    cm = ContextManager(max_tokens=8192)
    assert cm.trigger_threshold == 0.70 and cm.recent_memory_size == 12
    cm.state.narrative_summary = "keep me"
    cm.intent.primary_goal = "goal"

    cm.set_max_tokens(30000)

    assert cm.max_tokens == 30000
    # 20000 < 30000 <= 64000 row
    assert cm.trigger_threshold == 0.85 and cm.recent_memory_size == 50
    # In-place update: state layers survive, no fresh instance semantics.
    assert cm.state.narrative_summary == "keep me"
    assert cm.intent.primary_goal == "goal"


def test_api_budget_lowers_compression_limit(archive_dir):
    """get_token_usage pins the manager to min(window, budget) on API backends."""
    a = _agent_stub(archive_dir, cm_tokens=128000, budget=30000)
    a.provider = "veyllo"
    a.api_backend.get_model_context_window = lambda model=None: 128000
    a._estimate_token_usage = lambda: (1000, 128000)

    total, window = a.get_token_usage()

    assert window == 128000, "returned limit must stay the real model window"
    assert a.context_manager.max_tokens == 30000, "manager must trigger on the budget"
    # thresholds re-derived for the 30k row, not left at the 128k row's values
    assert a.context_manager.recent_memory_size == 50

    # budget 0 disables the ceiling: manager follows the window again
    a.config = SimpleNamespace(get=lambda key, default=None: 0 if key == "context_compress_tokens" else default)
    a.get_token_usage()
    assert a.context_manager.max_tokens == 128000


def test_budget_ignored_for_local_provider(archive_dir):
    a = _agent_stub(archive_dir, cm_tokens=32768, budget=500)
    a.api_backend = None
    assert a._compression_limit(32768) == 32768


def test_checkpoint_summary_reaches_state_snapshot(archive_dir):
    """The regression test for the two-instance bug.

    Sequence mirrors the live incident: manage_context runs first (on the old
    code this created the private second manager), then checkpoint_and_reset
    stores its summary, then the state registry captures the snapshot that
    session save persists. The summary must be IN that snapshot.
    """
    a = _agent_stub(archive_dir, cm_tokens=30000, budget=30000)
    a.state_registry = StateRegistry()
    a.state_registry.register("context", ContextStateProvider(a.context_manager))
    a.history = _fat_history(10)
    a.get_token_usage = lambda: (100, 128000)  # far below threshold: no summarize call

    a.manage_context()
    result = a.checkpoint_and_reset(summary="CHECKPOINT SUMMARY SURVIVES")
    assert "Context reset" in result

    snap = a.state_registry.capture_snapshot()
    # snapshot layout: providers.context.state = ContextStateProvider.get_state()
    persisted = snap.providers["context"]["state"]["state"]["narrative_summary"]
    assert persisted == "CHECKPOINT SUMMARY SURVIVES", (
        "checkpoint summary must land on the manager the state registry serializes"
    )


def test_session_load_compresses_replayed_history(archive_dir, monkeypatch):
    """Loading a fat session compresses it instead of resending everything.

    The session file keeps the full transcript by design, so load_session_context
    replays every message; without the load-path compression a restart undid
    every checkpoint and the next turn resent the whole conversation.
    """
    import vaf.core.session as session_mod
    import vaf.core.subagent_ipc as ipc_mod

    replayed = _fat_history(60)  # ~13k chars >> 0.70 * 500-token budget

    class FakeMessage(SimpleNamespace):
        pass

    fake_session = SimpleNamespace(
        messages=[
            FakeMessage(role=m["role"], content=m["content"], metadata=None,
                        tool_calls=None, tool_call_id=None, name=None)
            for m in replayed[1:]
        ],
        runtime_state=None,
        metadata={},
    )

    class FakeSessionManager:
        def __init__(self, *args, **kwargs):
            pass

        def load(self, session_id, restore_state=True, repoint=True):
            return fake_session

    monkeypatch.setattr(session_mod, "SessionManager", FakeSessionManager)
    monkeypatch.setattr(ipc_mod, "set_current_session_id", lambda sid: None)

    a = _agent_stub(archive_dir, cm_tokens=500, budget=500)
    a.state_registry = None
    a.current_session_id = "other-session"
    a.get_token_usage = lambda: (2000, 128000)
    a.init_chat = lambda: a.__dict__.__setitem__("history", [{"role": "system", "content": "sys"}])
    a._bind_session_persistence = lambda sid: None
    a._broadcast_context_status = lambda: None

    a.load_session_context("ab12cd34")

    assert a.current_session_id == "ab12cd34"
    assert len(a.history) < len(replayed), (
        "replayed history must be compressed on load, not sent at full size"
    )
    assert a.history[0]["role"] == "system"


def test_compress_budget_key_registered():
    """Ghost-key guard: the budget the agent reads must exist in DEFAULTS."""
    assert isinstance(Config.DEFAULTS.get("context_compress_tokens"), int)
    assert Config.DEFAULTS["context_compress_tokens"] > 0
