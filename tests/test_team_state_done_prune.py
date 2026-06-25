# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Team-state lifecycle: a finished sub-agent must show as 'done HH:MM', linger a few
turns so the main agent sees it stopped, then be pruned from the team list.

Regression: finished agents stayed in the prompt's team section indefinitely (and a
completion that arrived without details rendered as 'Starting...'), so the main agent kept
believing a sub-agent was still running.
"""
from vaf.core.main_persistence import (
    MainPersistenceManager,
    TEAM_DONE_PRUNE_TURNS,
)


def _mgr(tmp_path) -> MainPersistenceManager:
    return MainPersistenceManager(base_dir=str(tmp_path), session_id="s1")


def test_running_then_done_label_and_timestamp(tmp_path):
    m = _mgr(tmp_path)
    m.update_subagent_status(task_id="abcd1234ef", agent_type="document_agent",
                             status="running", details="Drafting sections")
    block = m.build_context_injection()
    assert "document_agent" in block
    assert "running" in block
    assert "Drafting sections" in block

    # Completion arrives -> done with a timestamp, no live "Doing:" line.
    m.update_subagent_status(task_id="abcd1234ef", agent_type="document_agent",
                             status="completed", result_summary="Created contract.docx")
    state = m.get_team_state()
    agent = next(iter(state.active_agents.values()))
    assert agent.completed_at is not None
    assert agent.prune_in_turns == TEAM_DONE_PRUNE_TURNS

    block = m.build_context_injection()
    assert "done" in block.lower()
    assert "Drafting sections" not in block          # live line dropped once finished
    assert "Created contract.docx" in block


def test_done_entry_pruned_after_three_turns(tmp_path):
    m = _mgr(tmp_path)
    m.update_subagent_status(task_id="t1", agent_type="coding_agent", status="running")
    m.update_subagent_status(task_id="t1", agent_type="coding_agent", status="completed")

    # Lingers for the grace turns, then disappears for everyone.
    for _ in range(TEAM_DONE_PRUNE_TURNS - 1):
        m.tick_team_state()
        assert len(m.get_team_state().active_agents) == 1
    m.tick_team_state()
    assert len(m.get_team_state().active_agents) == 0
    assert "No active agents" in m.build_context_injection()


def test_running_entry_never_pruned_by_turns(tmp_path):
    m = _mgr(tmp_path)
    m.update_subagent_status(task_id="r1", agent_type="research_agent", status="running")
    for _ in range(TEAM_DONE_PRUNE_TURNS + 3):
        m.tick_team_state()
    assert len(m.get_team_state().active_agents) == 1   # still working -> stays


def test_direct_completed_entry_not_labeled_starting(tmp_path):
    m = _mgr(tmp_path)
    # Completion that creates the entry directly (start never recorded one) must not
    # render the stale "Starting..." default.
    m.update_subagent_status(task_id="x9", agent_type="document_agent", status="completed")
    block = m.build_context_injection()
    assert "Starting..." not in block
    assert "done" in block.lower()
