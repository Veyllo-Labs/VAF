# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The <ipc-notification> push: a finished sub-agent wakes the main agent immediately.

Instead of relying on the headless runner's ~1s idle poll (which only fires when worker 1
happens to be free), a completed sub-agent PUSHES a signal — in-process it sets a shared
event, from a sub-agent subprocess it POSTs to the parent, whose handler sets the same
event. The runner consumes the event to run the result check at once, with the poll kept
as a fallback.
"""
import pytest

from vaf.core.subagent_ipc import (
    notify_result_ready,
    take_result_notification,
    _push_result_ready,
)


@pytest.fixture(autouse=True)
def _clear_event():
    # The event is module-global; drain it before each test for isolation.
    take_result_notification()
    yield
    take_result_notification()


def test_set_then_consume():
    assert take_result_notification() is False        # starts empty
    notify_result_ready()
    assert take_result_notification() is True          # push consumed
    assert take_result_notification() is False         # cleared


def test_multiple_pushes_collapse_to_one_consume():
    # One consume drains ALL pending results, so N completions -> 1 wake is correct.
    notify_result_ready()
    notify_result_ready()
    notify_result_ready()
    assert take_result_notification() is True
    assert take_result_notification() is False


def test_late_completion_is_not_lost():
    # A completion that lands AFTER a consume re-sets the flag (no missed wake-up).
    notify_result_ready()
    assert take_result_notification() is True
    notify_result_ready()                              # arrives after the read
    assert take_result_notification() is True


def test_in_process_producer_sets_the_event(monkeypatch):
    # Not a subprocess -> set the local event directly.
    monkeypatch.delenv("VAF_IN_SUBAGENT_TERMINAL", raising=False)
    _push_result_ready("task-1", "sess-1")
    assert take_result_notification() is True


def test_subprocess_producer_posts_and_does_not_set_local_event(monkeypatch):
    import vaf.core.web_interface as wi
    posted = {}
    monkeypatch.setattr(wi, "_post_to_parent", lambda d: posted.update(d))
    monkeypatch.setenv("VAF_IN_SUBAGENT_TERMINAL", "1")

    _push_result_ready("task-9", "sess-9")

    # Cross-process: it POSTs the notification for the parent to act on...
    assert posted.get("type") == "ipc_notification"
    assert posted.get("event") == "subagent_result"
    assert posted.get("taskId") == "task-9"
    assert posted.get("sessionId") == "sess-9"
    # ...and must NOT set the LOCAL event (the consumer lives in the parent process).
    assert take_result_notification() is False


def test_push_never_raises(monkeypatch):
    # A broken bridge must degrade to the poll fallback, never crash completion.
    import vaf.core.web_interface as wi

    def _boom(_):
        raise RuntimeError("bridge down")

    monkeypatch.setattr(wi, "_post_to_parent", _boom)
    monkeypatch.setenv("VAF_IN_SUBAGENT_TERMINAL", "1")
    _push_result_ready("t", "s")  # must not raise


# ── Ownership guard: the runner must not steal a result an in-process engine loop awaits ──
from vaf.core.subagent_ipc import mark_engine_owned, is_engine_owned  # noqa: E402


def test_engine_owned_mark_and_query():
    assert is_engine_owned("unmarked") is False
    mark_engine_owned("step-1")
    assert is_engine_owned("step-1") is True


def test_engine_owned_expires_after_ttl():
    import time
    mark_engine_owned("step-2")
    time.sleep(0.05)
    assert is_engine_owned("step-2", ttl=0.01) is False   # stale -> not owned (and cleaned up)
    assert is_engine_owned("step-2") is False             # confirmed removed


def test_engine_owned_empty_is_safe():
    assert is_engine_owned("") is False
    assert is_engine_owned(None) is False
    mark_engine_owned("")   # no-op, must not raise
    mark_engine_owned(None)
