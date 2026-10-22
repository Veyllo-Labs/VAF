# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`run_bounded_all` and `DaemonPool`: several calls that may hang, bounded together.

The calls are lookups a person waits for (the host a room invitation names), so what
matters is that the answers that came in time are used, that nothing queued starts after
the caller left, and that calls which hang occupy a fixed number of threads, none of which
holds up the exit of the process.
"""
import threading

from vaf.core.bounded_run import DaemonPool, run_bounded_all


def _threads(prefix):
    return [t for t in threading.enumerate() if t.name.startswith(prefix)]


def test_the_answers_that_arrived_in_time_are_kept():
    """A slow call and one that raised are left out; the quick one counts on its own.
    MUTATION: return nothing unless every call finished - red."""
    release = threading.Event()

    def _boom():
        raise RuntimeError("no")

    pool = DaemonPool(3, "test-bounded-kept")
    try:
        answers = run_bounded_all({"quick": lambda: 1, "slow": lambda: release.wait(5),
                                   "boom": _boom}, timeout=0.3, pool=pool)
    finally:
        release.set()
    assert answers == {"quick": 1}


def test_a_call_still_queued_when_the_time_is_up_never_runs():
    """MUTATION: leave the queued future uncancelled - red (it runs after the release)."""
    release = threading.Event()
    ran = []
    pool = DaemonPool(1, "test-bounded-queued")
    try:
        answers = run_bounded_all({"hang": lambda: release.wait(5),
                                   "queued": lambda: ran.append(1)}, timeout=0.1, pool=pool)
    finally:
        release.set()
    # The one worker takes the queued item next; a later call shows it has passed it.
    assert pool.submit(lambda: "after").result(timeout=5) == "after"
    assert answers == {} and ran == []


def test_calls_that_hang_occupy_the_cap_and_daemon_threads_only():
    """MUTATION: start a thread per call - red (ten threads)."""
    release = threading.Event()
    pool = DaemonPool(3, "test-bounded-cap")
    try:
        for _ in range(10):
            pool.submit(release.wait, 5)
        threads = _threads("test-bounded-cap")
    finally:
        release.set()
    assert len(threads) == 3
    assert all(t.daemon for t in threads), "a hung call must not hold up the exit"
