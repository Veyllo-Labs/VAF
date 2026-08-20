# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""is_busy_for_session() answers "is a turn running or queued in THIS chat".

MEASURED BEFORE BUILDING (2026-08-20, on the live code). The web server answered
this question TWICE by hand (vaf/core/web_server.py, the two history_update
`is_active` sites), both from process-global state: `manager.agent_instance` is
the ONE registered agent (headless worker 1) and `manager.latest_state["status"]`
is a single field any worker overwrites. With parallel_main_workers > 1 that
construction is wrong in both directions - a session busy on worker 2 reads
idle, and a LOAD_SESSION handled by worker 1 while another worker streams makes
an idle chat read busy, which is the stop button appearing in a chat the agent
never worked in. The queue has owned the correct per-session answer all along
(`_session_inflight` plus its heaps); this method is the missing read.
"""
import re
from pathlib import Path

import pytest

from vaf.core.task_queue import TaskQueue

ROOT = Path(__file__).resolve().parents[1]


def _fresh_queue(legacy: bool) -> TaskQueue:
    """A queue with no cross-test state, in the requested scheduling mode."""
    TaskQueue._instance = None  # type: ignore[attr-defined]
    tq = TaskQueue()
    tq._legacy_mode = legacy  # type: ignore[attr-defined]
    if not legacy:
        tq._class_weights = {  # type: ignore[attr-defined]
            tq.TASK_CLASS_INTERACTIVE: 5,
            tq.TASK_CLASS_AUTOMATION: 3,
            tq.TASK_CLASS_BACKGROUND: 1,
        }
        tq._reset_scheduler_budget_locked()  # type: ignore[attr-defined]
    return tq


@pytest.fixture(autouse=True)
def _singleton_restored():
    """Drop the queue this module built, so a later test gets a real one."""
    yield
    TaskQueue._instance = None  # type: ignore[attr-defined]


@pytest.mark.parametrize("legacy", [True, False], ids=["legacy", "weighted_fair"])
def test_queued_and_inflight_count_and_done_clears(legacy: bool) -> None:
    """The full lifecycle: queued -> busy, in flight -> busy, done -> idle.

    Queued must already count: the stop button exists to cancel a turn, and a
    turn waiting behind another session's turn is exactly what a user wants to
    be able to cancel.
    """
    tq = _fresh_queue(legacy)
    md = {"task_class": "interactive"}
    assert tq.is_busy_for_session("session-x") is False

    tq.add("session-x", "x1", source="web", metadata=md)
    assert tq.is_busy_for_session("session-x") is True, "queued task not seen"
    assert tq.is_busy_for_session("session-y") is False, (
        "another session reads busy from session-x's task - this is the "
        "cross-chat stop-button leak, per-session isolation is the point"
    )

    task = tq.get(timeout=0.01, worker_id="w1")
    assert task is not None and task.session_id == "session-x"
    assert tq.is_busy_for_session("session-x") is True, "in-flight task not seen"

    tq.task_done(task=task, worker_id="w1")
    assert tq.is_busy_for_session("session-x") is False, "done did not clear busy"


@pytest.mark.parametrize("legacy", [True, False], ids=["legacy", "weighted_fair"])
def test_a_drain_claim_counts_as_busy(legacy: bool) -> None:
    """try_claim_session() has no task object in any heap or worker slot.

    The drain's summary chat_step mutates the session exactly like a worker
    turn, so a probe that only iterated tasks would answer idle mid-summary.
    """
    tq = _fresh_queue(legacy)
    assert tq.try_claim_session("session-x") is True
    assert tq.is_busy_for_session("session-x") is True, (
        "drain claim invisible: the probe iterates tasks only and misses "
        "_session_inflight entries without a task object"
    )
    tq.release_session_claim("session-x")
    assert tq.is_busy_for_session("session-x") is False


@pytest.mark.parametrize("legacy", [True, False], ids=["legacy", "weighted_fair"])
def test_housekeeping_commands_never_mark_a_chat_busy(legacy: bool) -> None:
    """Every __CMD__ task is enqueued under session_id="system".

    A session switch enqueues LOAD_SESSION for the chat being OPENED; if that
    command counted, the act of looking at an idle chat would flash its stop
    button on.
    """
    tq = _fresh_queue(legacy)
    tq.add(
        "system",
        "__CMD__:LOAD_SESSION:session-x",
        source="web",
        metadata={"task_class": "background"},
    )
    assert tq.is_busy_for_session("session-x") is False
    assert tq.is_busy_for_session("") is False
    assert tq.is_busy_for_session(None) is False  # type: ignore[arg-type]


def test_web_server_history_update_consumes_the_primitive() -> None:
    """The WIRING, source-pinned: web_server's isActive must come from the queue.

    Source-level because the two sites live inside a websocket handler with no
    seam to drive from a test (the module import alone drags the FastAPI app up).
    The former construction read `manager.latest_state` next to the registered
    agent's `_session_id`; if either creeps back into an isActive computation,
    the per-session answer degrades to process-global state again and the fix
    reverts silently while every unit test above stays green.
    """
    src = (ROOT / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")

    assert "def _session_is_active" in src
    assert "is_busy_for_session" in src, (
        "web_server no longer consumes TaskQueue.is_busy_for_session - "
        "isActive has lost its per-session authority"
    )
    active_sites = re.findall(r"is_active\s*=\s*_session_is_active\(", src)
    assert len(active_sites) == 2, (
        f"expected the two history_update sites to assign is_active from "
        f"_session_is_active, found {len(active_sites)}"
    )
    assert not re.search(
        r"latest_state\.get\(\"status\"\)\s*!=\s*\"idle\"", src
    ), (
        "an isActive computation reads the process-global latest_state status "
        "again - that field is one value for all sessions and the headless "
        "product path never writes it"
    )
