# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# Mock llama_cpp before importing Agent.
sys.modules.setdefault("llama_cpp", MagicMock())

from vaf.core.agent import Agent
from vaf.core.config import Config
from vaf.core.context import ContextManager


class DummyPersistence:
    def __init__(self, working_memory: dict, intent: str = ""):
        self._working_memory = working_memory
        self._intent = intent

    def get_working_memory(self) -> dict:
        return self._working_memory

    def get_user_intent(self) -> str:
        return self._intent


@pytest.fixture
def resume_enabled(monkeypatch):
    monkeypatch.setattr(
        Config,
        "get",
        classmethod(lambda cls, key, default=None: True if key == "resume_compaction_enabled" else default),
    )


def test_build_resume_block_has_stable_fields(resume_enabled):
    cm = ContextManager(max_tokens=8192)
    cm.intent.primary_goal = "implement resume compaction"
    cm.intent.sub_goals = ["wire checkpoint path"]
    cm.state.files_modified = [("vaf/core/context.py", 3)]
    cm.state.files_read = [("docs/memory/MEMORY_SYSTEM.md", 3)]
    cm.state.tools_used = ["read_file", "checkpoint_context", "read_file"]
    cm.state.key_decisions = ["Use deterministic rule-based resume fields."]

    history = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "Add a deterministic resume block after compression."},
        {"role": "assistant", "content": "I will wire it into compression and checkpoint."},
    ]
    working_memory = {
        "notes": [{"text": "Decision: keep the block deterministic."}],
        "plan": [{"text": "Update vaf/core/context.py and docs/memory/MEMORY_SYSTEM.md"}],
        "tasks": [
            {"text": "Add resume block builder", "status": "done"},
            {"text": "Verify checkpoint path", "status": "pending"},
        ],
    }

    block = cm.build_resume_block(history, working_memory=working_memory)

    assert block.startswith("=== RESUME CONTEXT ===")
    assert "CURRENT_WORK:" in block
    assert "PENDING_WORK: Verify checkpoint path" in block
    assert "KEY_FILES: vaf/core/context.py, docs/memory/MEMORY_SYSTEM.md" in block
    assert "TOOLS_USED: read_file, checkpoint_context" in block
    assert "KEY_DECISIONS:" in block
    assert block.endswith("=== END RESUME ===")


def test_compress_appends_resume_block(resume_enabled):
    cm = ContextManager(max_tokens=8192)
    cm.state.narrative_summary = "Implemented the first pass of resume compaction."
    history = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "Implement the resume block."},
        {"role": "assistant", "content": "Created: vaf/core/context.py"},
        {"role": "tool", "name": "read_file", "content": "Read: vaf/core/context.py"},
        {"role": "assistant", "content": "Modified: docs/memory/MEMORY_SYSTEM.md"},
        {"role": "user", "content": "Now verify the checkpoint behavior."},
    ]
    working_memory = {
        "plan": [{"text": "Implement resume block"}, {"text": "Verify checkpoint behavior"}],
        "tasks": [{"text": "Verify checkpoint behavior", "status": "pending"}],
    }

    compressed = cm.compress(history, working_memory=working_memory)

    assert len(compressed) >= 2
    assert compressed[1]["role"] == "system"
    assert "=== RESUME CONTEXT ===" in compressed[1]["content"]
    assert "NEXT_ACTION: Continue with: Verify checkpoint behavior" in compressed[1]["content"]


def test_checkpoint_and_reset_includes_resume_block(resume_enabled):
    working_memory = {
        "notes": [{"text": "Decision: append resume block after compression."}],
        "plan": [{"text": "Run focused tests"}],
        "tasks": [{"text": "Run focused tests", "status": "pending"}],
    }
    fake_agent = SimpleNamespace(
        history=[
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "Implement resume compaction."},
            {"role": "assistant", "content": "Modified: vaf/core/context.py"},
            {"role": "user", "content": "Checkpoint after the implementation pass."},
        ],
        context_manager=ContextManager(max_tokens=8192),
        main_persistence=DummyPersistence(working_memory, intent="implement resume compaction"),
        current_session_id=None,
    )

    result = Agent.checkpoint_and_reset(fake_agent, summary="Implementation pass complete.")

    assert result.startswith("[checkpoint] Context reset:")
    assert "[CONTEXT RESTORED]" in fake_agent.history[1]["content"]
    assert "=== RESUME CONTEXT ===" in fake_agent.history[1]["content"]
    assert "CURRENT_WORK:" in fake_agent.history[1]["content"]
