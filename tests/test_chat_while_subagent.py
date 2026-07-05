# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Chat-while-a-sub-agent-runs: unit cover for the safety pieces.

The concurrency itself already existed; these tests cover what the feature adds:
the per-agent-type delegation-intent slot (no clobber), the TaskQueue session claim
for the runner's out-of-queue drain, the narrowed team_await gate (casual "fertig"
passes, work-referencing claims still bounce), and the config default.
"""
import types

import pytest

from vaf.core.task_queue import TaskQueue
from vaf.core.agent import Agent


# ── team_await narrowing ────────────────────────────────────────────────────
def _fake_agent(live):
    """Minimal stand-in exposing exactly what _detect_premature_done_claim reads."""
    a = types.SimpleNamespace()
    a.get_live_session_subagents = lambda: live
    return a


LIVE_CODER = [{
    "task_id": "t1", "agent_type": "coding_agent",
    "task_description": "Fix the Neural Link game deadlock in index.html",
    "running_seconds": 90,
}]


def _detect(text, live):
    return Agent._detect_premature_done_claim(_fake_agent(live), text)


def test_casual_done_passes_while_coder_runs():
    # Bare small-talk completion words must NOT bounce (they erased streamed replies).
    blocked, _ = _detect("Erledigt! Hab ich mir notiert.", LIVE_CODER)
    assert blocked is False
    blocked, _ = _detect("All done with that shopping list!", LIVE_CODER)
    assert blocked is False


def test_work_referencing_claim_still_bounces():
    blocked, labels = _detect("Der Code ist fertig, das Projekt ist abgeschlossen!", LIVE_CODER)
    assert blocked is True
    assert labels and "coding_agent" in labels[0]
    # Task-description words count as references too.
    blocked, _ = _detect("Alles erledigt mit index.html!", LIVE_CODER)
    assert blocked is True


def test_no_live_subagent_never_bounces():
    blocked, _ = _detect("Task complete, everything is done!", [])
    assert blocked is False


def test_no_completion_marker_is_cheap_pass():
    # No marker -> no IPC touched, no bounce.
    called = {"n": 0}
    a = types.SimpleNamespace()
    def _live():
        called["n"] += 1
        return LIVE_CODER
    a.get_live_session_subagents = _live
    blocked, _ = Agent._detect_premature_done_claim(a, "Wie geht es dir?")
    assert blocked is False
    assert called["n"] == 0


# ── per-agent-type delegation-intent slot ───────────────────────────────────
def test_delegation_intent_not_clobbered_by_other_agent(tmp_path):
    from vaf.core.main_persistence import MainPersistenceManager
    mpm = MainPersistenceManager(base_dir=str(tmp_path))
    mpm.write_subagent_delegation_intent("build the game", "fix deadlock", "coding_agent")
    # Light chat delegates a DIFFERENT agent meanwhile:
    mpm.write_subagent_delegation_intent("find reddit stats", "research reddit", "research_agent")
    # The coder's slot survives:
    d = mpm.get_subagent_delegation_intent("coding_agent")
    assert d and d["intent"] == "build the game" and d["goal"] == "fix deadlock"
    # And the researcher gets its own:
    d2 = mpm.get_subagent_delegation_intent("research_agent")
    assert d2 and d2["intent"] == "find reddit stats"
    # Legacy no-arg call returns the last write (backward compatible):
    d3 = mpm.get_subagent_delegation_intent()
    assert d3 and d3["agent_type"] == "research_agent"


def test_delegation_intent_never_borrows_other_agents_slot(tmp_path):
    from vaf.core.main_persistence import MainPersistenceManager
    mpm = MainPersistenceManager(base_dir=str(tmp_path))
    # Legacy-style file: only top-level fields, written by another agent type.
    mpm.write_subagent_delegation_intent("research this", "goal", "research_agent")
    # Simulate pre-fix file (no by_agent for coding_agent): the coder must get None,
    # NOT the researcher's intent — validating against it caused retry storms.
    data = mpm._get_validation_data()
    data.pop("by_agent", None)
    mpm._save_json(mpm.context_dir / "subagent_validation.json", data)
    assert mpm.get_subagent_delegation_intent("coding_agent") is None


# ── TaskQueue session claim (runner drain vs worker chat turn) ─────────────
def test_try_claim_session_blocks_second_claim():
    tq = TaskQueue()
    sid = "test-claim-abc123"
    tq.release_session_claim(sid)  # clean slate
    assert tq.try_claim_session(sid) is True
    assert tq.try_claim_session(sid) is False   # busy -> drain must skip
    tq.release_session_claim(sid)
    assert tq.try_claim_session(sid) is True    # free again
    tq.release_session_claim(sid)


def test_empty_session_claim_is_noop():
    tq = TaskQueue()
    assert tq.try_claim_session("") is True
    tq.release_session_claim("")  # must not raise


# ── config default ──────────────────────────────────────────────────────────
def test_concurrent_chat_config_default_on():
    from vaf.core.config import Config
    assert Config.DEFAULTS.get("subagent_concurrent_chat_enabled") is True
