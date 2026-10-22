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

from vaf.core.bounded_run import DaemonPool, InFlight, run_bounded_all


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


def test_a_worker_that_could_not_start_does_not_hold_its_place():
    """A pool whose every start failed never ran anything again: the dead threads filled the
    cap. MUTATION: count the worker before it started - red."""
    real = threading.Thread.start
    fails = [1]

    def _flaky(self):
        if self.name.startswith("test-bounded-start") and fails[0]:
            fails[0] -= 1
            raise RuntimeError("can't start new thread")
        return real(self)

    pool = DaemonPool(1, "test-bounded-start")
    threading.Thread.start = _flaky
    try:
        try:
            pool.submit(lambda: "lost")
        except RuntimeError:
            pass
    finally:
        threading.Thread.start = real
    assert pool.submit(lambda: "served").result(timeout=5) == "served"


def test_a_key_already_running_is_not_started_again():
    """With ``in_flight``, a later caller shares a running call and waits for it only until
    the deadline of the caller that started it. MUTATION: start every key again - red."""
    import time

    release = threading.Event()
    started = []

    def _hang():
        started.append(1)
        release.wait(5)

    pool, in_flight = DaemonPool(2, "test-bounded-shared"), InFlight()
    try:
        assert run_bounded_all({"k": _hang}, timeout=0.2, pool=pool, in_flight=in_flight) == {}
        later = time.monotonic()
        assert run_bounded_all({"k": _hang}, timeout=0.6, pool=pool, in_flight=in_flight) == {}
        later = time.monotonic() - later
    finally:
        release.set()
    # At once is under 0.2 s even on a loaded runner; a call started again waits its 0.6 s.
    assert started == [1] and later < 0.2, (started, later)


def test_a_queued_key_stays_queued_for_the_next_caller():
    """Cancelled from the queue at the first caller's deadline, a key was started again by
    every later caller, who then waited its full time for it (another account's invitation
    waited 32 s at 60 connections). With ``in_flight`` it stays queued, once. MUTATION:
    cancel it anyway - red."""
    import time

    release = threading.Event()
    ran = []
    pool, in_flight = DaemonPool(1, "test-bounded-queued-shared"), InFlight()
    try:
        run_bounded_all({"a": lambda: release.wait(5), "b": lambda: ran.append("b")},
                        timeout=0.1, pool=pool, in_flight=in_flight)
        later = time.monotonic()
        run_bounded_all({"b": lambda: ran.append("b again")}, timeout=0.6, pool=pool,
                        in_flight=in_flight)
        later = time.monotonic() - later
    finally:
        release.set()
    assert pool.submit(lambda: "after").result(timeout=5) == "after"
    assert later < 0.2 and ran == ["b"], (later, ran)


def test_a_callers_own_timeout_caps_its_wait_for_a_shared_key():
    """A shared key waited until the deadline of the caller that started it, so a caller
    with 0.2 s returned after 2.90 s. MUTATION: wait for the starter's deadline only - red."""
    import time

    release = threading.Event()
    pool, in_flight = DaemonPool(2, "test-bounded-own-timeout"), InFlight()
    starter = threading.Thread(target=run_bounded_all, args=({"k": lambda: release.wait(10)},),
                               kwargs={"timeout": 3.0, "pool": pool, "in_flight": in_flight})
    starter.start()
    try:
        time.sleep(0.1)
        later = time.monotonic()
        assert run_bounded_all({"k": lambda: 1}, timeout=0.2, pool=pool,
                               in_flight=in_flight) == {}
        later = time.monotonic() - later
    finally:
        release.set()
        starter.join(5)
    # 0.2 s plus timer slack on a loaded runner, far below the starter's 3 s.
    assert later < 1.2, later


def test_join_wait_caps_the_wait_for_a_call_another_caller_started():
    """A caller that joins a running call waits for it at most ``join_wait``; a call that
    ends late then costs only the caller that started it. MUTATION: ignore join_wait - red
    (the joiner waits out the starter's 3 s)."""
    import time

    release = threading.Event()
    pool, in_flight = DaemonPool(2, "test-bounded-join"), InFlight(join_wait=0.1)
    starter = threading.Thread(target=run_bounded_all, args=({"k": lambda: release.wait(10)},),
                               kwargs={"timeout": 3.0, "pool": pool, "in_flight": in_flight})
    starter.start()
    try:
        time.sleep(0.1)
        later = time.monotonic()
        assert run_bounded_all({"k": lambda: 1}, timeout=5.0, pool=pool,
                               in_flight=in_flight) == {}
        later = time.monotonic() - later
    finally:
        release.set()
        starter.join(5)
    assert later < 1.2, later
