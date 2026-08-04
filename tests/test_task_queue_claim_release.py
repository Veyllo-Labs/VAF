# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A worker's claim on a session is released even when task_done() is never reached.

MEASURED BEFORE BUILDING (2026-08-04, on the live code). `vaf/cli/cmd/run.py`
calls `tq.task_done()` ZERO times against `vaf/core/headless_runner.py`'s nine.
Every `tq.get()` in the classic CLI therefore held its session in
`_session_inflight` permanently: `_cleanup_stale_inflight_locked` reaps only
NUMERIC worker keys of DEAD threads, and the CLI's main thread is neither.

THE CASCADE, which is why this is a fix and not a tidy-up. With the session
parked, `_pop_next_task_locked` skips every later task for it, so the second web
message left `get_queue_size() > 0` true while `get()` returned None. `run.py`
dereferenced that None, the broad handler at the bottom of the prompt loop caught
the AttributeError and set `console_broken = True` - a flag initialised once
OUTSIDE the loop and never cleared. From that point the classic CLI ran on bare
`input()` for the rest of the process: no completion, no history, no voice.

WHERE THE FIX SITS, and why not at the call sites. A worker holds at most one
task by construction (`_inflight_by_worker` is one slot per key), so asking for
the next one means the previous is over. Saying that once in `get()` repairs
every consumer, present and future, without touching one of them - rather than
adding a `task_done()` to two call sites and waiting for the third to forget.

THE GUARD IS SEPARATE FROM THE FIX. `get_queue_size()` and `get()` are two lock
acquisitions, so None stays reachable (another consumer, or a session parked by
a legitimate in-flight task). The two call sites keep a None check; the source
test below pins that they do.
"""
import re
from pathlib import Path

import pytest

from vaf.core.task_queue import TaskQueue

ROOT = Path(__file__).resolve().parents[1]


def _fresh_queue(legacy: bool) -> TaskQueue:
    """A queue with no cross-test state, in the requested scheduling mode.

    Both modes are exercised because `legacy` is the DEFAULT (`queue_policy`
    is read once at construction and defaults to legacy), while the existing
    weighted-fair tests only ever force the non-default branch.
    """
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
def test_a_second_get_frees_the_session_the_caller_never_finished(legacy: bool) -> None:
    """The whole point: a missed task_done() must not park a session forever.

    Asserted from a THIRD worker, not from the forgetful one. A worker that
    releases and re-takes in one call could pass a self-check while the session
    stayed blocked for everyone else, which is exactly the reported symptom.
    """
    tq = _fresh_queue(legacy)
    md = {"task_class": "interactive"}
    tq.add("session-x", "x1", source="web", metadata=md)
    tq.add("session-y", "y1", source="web", metadata=md)
    tq.add("session-x", "x2", source="web", metadata=md)

    first = tq.get(timeout=0.01, worker_id="w1")
    assert first is not None and first.session_id == "session-x"

    # No task_done(). The worker simply asks for its next task.
    second = tq.get(timeout=0.01, worker_id="w1")
    assert second is not None and second.session_id == "session-y"

    third = tq.get(timeout=0.01, worker_id="w2")
    assert third is not None, (
        "session-x is still parked: the caller's abandoned claim was never "
        "released, so every later task for that session is unreachable"
    )
    assert third.session_id == "session-x"


@pytest.mark.parametrize("legacy", [True, False], ids=["legacy", "weighted_fair"])
def test_a_named_worker_recovers_although_the_stale_reaper_cannot_help_it(legacy: bool) -> None:
    """The reaper is not what fixes this, and the test must prove that.

    `_cleanup_stale_inflight_locked` skips non-numeric keys on purpose (a custom
    key may outlive its thread) and only reaps DEAD threads anyway. "w1" is
    non-numeric and this thread is alive, so if the release in `get()` were made
    conditional on the reaper's rules, nothing would ever free this claim.
    """
    tq = _fresh_queue(legacy)
    md = {"task_class": "interactive"}
    tq.add("session-x", "x1", source="web", metadata=md)
    tq.add("session-y", "y1", source="web", metadata=md)

    held = tq.get(timeout=0.01, worker_id="w1")
    assert held is not None and held.session_id == "session-x"
    assert "w1" in tq._inflight_by_worker  # type: ignore[attr-defined]

    moved_on = tq.get(timeout=0.01, worker_id="w1")
    assert moved_on is not None and moved_on.session_id == "session-y"
    # Two sessions on purpose: with both tasks on one session the worker would
    # re-take it immediately and the set would look unchanged either way.
    inflight = tq._session_inflight  # type: ignore[attr-defined]
    assert "session-x" not in inflight
    assert "session-y" in inflight


@pytest.mark.parametrize("legacy", [True, False], ids=["legacy", "weighted_fair"])
def test_the_release_never_frees_a_claim_the_caller_does_not_hold(legacy: bool) -> None:
    """`_session_inflight` is shared with the out-of-queue drain claim.

    It is a set, not a refcount, so a release that discarded by session rather
    than by the caller's own slot would hand the drain's session to a worker
    mid-summary - the history/session.json write race `try_claim_session` exists
    to prevent.
    """
    tq = _fresh_queue(legacy)
    md = {"task_class": "interactive"}
    tq.add("session-y", "y1", source="web", metadata=md)

    done = tq.get(timeout=0.01, worker_id="w1")
    assert done is not None and done.session_id == "session-y"
    tq.task_done(task=done, worker_id="w1")

    # The drain now claims that same session, out of queue.
    assert tq.try_claim_session("session-y") is True

    # The worker asks for another task. Its own slot is empty, so it must
    # discard nothing - least of all the drain's claim.
    assert tq.get(timeout=0.01, worker_id="w1") is None
    assert tq.try_claim_session("session-y") is False, (
        "the drain's claim was released by a worker that never held it"
    )
    tq.release_session_claim("session-y")


@pytest.mark.parametrize("legacy", [True, False], ids=["legacy", "weighted_fair"])
def test_the_active_task_pointer_follows_the_release(legacy: bool) -> None:
    """`active_task` must not keep naming a task the queue has let go.

    Same defect class as the session claim - a pointer nobody releases - and one
    line away in the same block, so it is fixed here rather than left for a
    reader who would have to rediscover why it disagrees with the worker map.
    """
    tq = _fresh_queue(legacy)
    md = {"task_class": "interactive"}
    tq.add("session-x", "x1", source="web", metadata=md)
    tq.add("session-y", "y1", source="web", metadata=md)

    first = tq.get(timeout=0.01, worker_id="w1")
    assert tq.active_task is first

    second = tq.get(timeout=0.01, worker_id="w1")
    assert second is not None and second is not first
    assert tq.active_task is second, (
        "active_task still names the abandoned task, so it no longer appears "
        "in _inflight_by_worker at all"
    )


def test_both_classic_queue_polls_guard_against_an_empty_get() -> None:
    """Source-level, because the poll lives inside a ~900-line prompt loop.

    `get_queue_size()` and `get()` take the lock separately, so None survives the
    fix above and must be handled. Pinned by source because there is no seam to
    drive these two branches from a test, and an unguarded None here does not
    raise loudly - it silently downgrades the console for the whole process.
    """
    src = (ROOT / "vaf" / "cli" / "cmd" / "run.py").read_text(encoding="utf-8")
    lines = src.splitlines()
    polls = [i for i, line in enumerate(lines) if re.search(r"\btask\s*=\s*tq\.get\(", line)]

    assert len(polls) == 2, (
        f"expected the two known TaskQueue polls in run.py, found {len(polls)} - "
        "a new one needs its own None guard before this count is updated"
    )
    for index in polls:
        window = "\n".join(lines[index:index + 12])
        assert "if task is None:" in window, (
            f"the queue poll at run.py:{index + 1} dereferences a possibly-None task; "
            "the AttributeError sets console_broken, which is never cleared"
        )
