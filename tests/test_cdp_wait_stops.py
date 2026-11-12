# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The browser's DevTools wait ends with the run that asked for it.

`resolve_cdp_ws_url` retries the DevTools port of a browser that is not answering until
its deadline (VAF_BROWSER_CDP_WAIT_S, 30 s). A stopped browser run used to leave that wait
behind on an executor thread: measured, 40 refused connects and the handover mutex held
for 29.1 s after the run had returned. `bounded_run.cancel_when` arms the thread's cancel
flag from a check, and the wait asks that flag before every attempt and in its pause.

The clock is faked: the pause and the deadline are read from it, so the timings below are
exact and the tests take no wall time.
"""
import threading
import urllib.error
import urllib.request

import pytest

import vaf.core.bounded_run as bounded_run
import vaf.core.browser_interactive as bi
from vaf.core.bounded_run import cancel_check, cancel_requested, cancel_when


class _Clock:
    """`time` for resolve_cdp_ws_url: sleep advances monotonic, nothing waits."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr(bi, "time", fake)
    return fake


@pytest.fixture
def refusing(monkeypatch, clock):
    """A DevTools port that refuses every attempt; the moments of the attempts on the
    fake clock."""
    attempts = []

    def _urlopen(url, timeout=None):
        attempts.append(round(clock.now, 6))
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    return attempts


# -- resolve_cdp_ws_url ---------------------------------------------------------------

def test_the_wait_gives_up_within_a_pause_step_of_the_run_ending(clock, refusing, monkeypatch):
    """MUTATION: sleep the whole backoff pause in one call again (the flag is seen only
    when the 1.0 s pause after the second attempt is over, at 1.5 s).
    MUTATION: drop the check inside the pause."""
    monkeypatch.setenv("VAF_BROWSER_CDP_WAIT_S", "30")
    # The run ends 0.7 s in: inside the 1.0 s pause after the second attempt.
    run_ended_at = 0.7

    with cancel_when(lambda: clock.now >= run_ended_at):
        with pytest.raises(RuntimeError, match="abandoned"):
            bi.resolve_cdp_ws_url("http://localhost:9222")
    gave_up = clock.now
    assert gave_up - run_ended_at < 0.3, (
        f"gave up {gave_up - run_ended_at:.2f} s after the run ended; a stopped run's wait "
        f"must end within one 0.1 s step of its pause")
    assert refusing == [0.0, 0.5], f"attempts: {refusing}"


def test_no_attempt_once_the_run_has_ended(clock, refusing):
    """MUTATION: ask the flag only after the attempt (one more dial of a run that is over)."""
    with cancel_when(lambda: True):
        with pytest.raises(RuntimeError, match="abandoned"):
            bi.resolve_cdp_ws_url("http://localhost:9222")
    assert refusing == []


def test_outside_a_scope_the_deadline_and_backoff_are_unchanged(clock, refusing, monkeypatch):
    """MUTATION: change a backoff value, or stop at the first cancel check outside a scope."""
    monkeypatch.setenv("VAF_BROWSER_CDP_WAIT_S", "2")
    with pytest.raises(RuntimeError, match="did not respond within 2s"):
        bi.resolve_cdp_ws_url("http://localhost:9222")
    # 0.5 s, 1.0 s, then 1.5 s between attempts; the deadline is checked after an attempt.
    assert [round(at, 3) for at in refusing] == [0.0, 0.5, 1.5, 3.0]
    assert max(clock.sleeps) <= 0.1 + 1e-9, "the pause must be slept in short steps"


def test_outside_a_scope_a_browser_that_answers_is_resolved(clock, monkeypatch):
    class _Resp:
        def read(self):
            return b'{"webSocketDebuggerUrl": "ws://container:9222/devtools/browser/abc"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=None: _Resp())
    with cancel_when(lambda: False):
        assert bi.resolve_cdp_ws_url("ws://localhost:9222") == \
            "ws://localhost:9222/devtools/browser/abc"
    assert bi.resolve_cdp_ws_url("http://localhost:9222") == \
        "ws://localhost:9222/devtools/browser/abc"


# -- cancel_when ----------------------------------------------------------------------

def test_the_scope_restores_the_previous_flag_on_exit_and_on_an_exception():
    """MUTATION: do not restore the previous flag on exit."""
    assert cancel_requested() is False
    with cancel_when(lambda: True):
        assert cancel_requested() is True
        assert cancel_check()() is True
        with cancel_when(lambda: False):
            assert cancel_requested() is True, "the outer scope's flag must hold inside"
        assert cancel_requested() is True
    assert cancel_requested() is False
    assert getattr(bounded_run._thread_cancel, "event", None) is None

    with pytest.raises(ValueError):
        with cancel_when(lambda: True):
            raise ValueError("boom")
    assert cancel_requested() is False
    assert getattr(bounded_run._thread_cancel, "event", None) is None


def test_an_abandoned_run_bounded_worker_stays_cancelled_inside_a_scope():
    """MUTATION: ignore the previous flag inside the scope."""
    started = threading.Event()
    saw_cancel = threading.Event()

    def _work():
        with cancel_when(lambda: False):
            started.set()
            for _ in range(300):
                if cancel_requested():
                    saw_cancel.set()
                    return "cancelled"
                threading.Event().wait(0.01)
        return "ran on"

    result = bounded_run.run_bounded(_work, timeout=10, stop_check=started.is_set, poll=0.05)
    assert bounded_run.is_abort_sentinel(result)
    assert saw_cancel.wait(3.0), "the abandoned worker's own flag was hidden by the scope"


def test_a_check_that_raises_counts_as_not_cancelled():
    """MUTATION: let the check's exception escape."""
    def _broken():
        raise OSError("record unreadable")

    with cancel_when(_broken):
        assert cancel_requested() is False
        assert cancel_check()() is False
    assert cancel_requested() is False


# -- the give-back parker ---------------------------------------------------------------

def test_the_suite_arms_no_give_back_parker():
    """MUTATION: remove the `_no_give_back_parker` fixture from tests/conftest.py.

    The parker sleeps 20 s and then parks the browser through its DevTools port, inside
    whatever test runs by then: measured, 12 browser tests left one dialing 9222 twice."""
    manager = bi.InteractiveBrowserManager(cdp_base_url="http://127.0.0.1:9")
    emitted = []
    manager._emit = lambda event, sid=None: emitted.append(event)
    manager._elsewhere_holder = ("scope-a", "sess-1")
    before = set(threading.enumerate())
    manager.give_back_elsewhere()
    assert emitted, "the give-back was not announced, so the parker was never asked for"
    parkers = [t for t in threading.enumerate()
               if t not in before and t.name == "browser-giveback-parker"]
    assert parkers == []
