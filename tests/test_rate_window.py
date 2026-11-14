# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf.core.rate_window.RateWindow`: at most N units per key in any window of seconds.

The window replaced hand-written copies of itself in the Veyllo App link (messages per
device, sessions per peer and per connection, room bytes per account). The differential
fuzz below holds it against reference copies of those functions exactly as they were, so a
conversion that changed one decision is red.

MUTATIONS, each turns the named test red:
- `>=` for `>` at the window's edge -> test_a_charge_exactly_a_window_old_still_counts and
  the differential fuzz;
- record part of a weight that does not fit -> test_take_is_all_or_nothing;
- refund the oldest equal charge -> test_refund_gives_back_the_newest_equal_charge;
- ignore the per-call limit -> test_a_limit_per_call_overrides_the_windows;
- keep a key whose charges expired -> test_an_idle_key_is_dropped;
- no eviction beyond `max_keys` -> test_the_keys_idle_longest_go_first;
- take outside the lock -> test_eight_threads_get_exactly_the_limit.
"""
import os
import random
import threading
import time
from collections import deque

import pytest

from vaf.core.rate_window import RateWindow


class _Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_take_is_all_or_nothing():
    window = RateWindow(60, 10, clock=_Clock())
    assert window.take("a", 6)
    assert not window.take("a", 5), "5 more would make 11 of 10"
    assert window.used("a") == 6, "a refused charge took part of its weight"
    assert window.take("a", 4)
    assert not window.take("a", 1)
    assert not window.take("b", 11), "a weight above the limit is refused"
    assert window.used("b") == 0


def test_a_limit_per_call_overrides_the_windows():
    window = RateWindow(60, 100, clock=_Clock())
    assert window.take("a", limit=2) and window.take("a", limit=2)
    assert not window.take("a", limit=2)
    assert window.take("a"), "the window's own limit is 100"
    plain = RateWindow(60, clock=_Clock())
    with pytest.raises(ValueError):
        plain.take("a")
    assert plain.take("a", limit=1)
    with pytest.raises(ValueError):
        plain.take("a", -1, limit=5)


def test_a_charge_exactly_a_window_old_still_counts():
    clock = _Clock(0.0)
    window = RateWindow(60, 1, clock=clock)
    assert window.take("a")
    clock.now = 60.0
    assert not window.take("a"), "a charge exactly window_s old is still inside the window"
    clock.now = 60.001
    assert window.take("a")


def test_refund_gives_back_the_newest_equal_charge():
    clock = _Clock(0.0)
    window = RateWindow(100, 30, clock=clock)
    assert window.take("a", 10)          # t=0
    clock.now = 50.0
    assert window.take("a", 10)          # t=50
    assert window.refund("a", 10)
    # Had the oldest gone back, the charge of t=50 would still count at t=101.
    clock.now = 101.0
    assert window.used("a") == 0
    assert window.take("a", 30)
    assert not window.refund("a", 7), "no charge of that weight"
    assert not window.refund("nobody", 1)


def test_an_idle_key_is_dropped():
    clock = _Clock(0.0)
    window = RateWindow(10, 5, clock=clock)
    for key in range(50):
        assert window.take(key)
    assert len(window) == 50
    clock.now = 11.0
    assert window.take("fresh")
    assert len(window) == 1, "the keys whose charges all expired are still held"
    window.forget("fresh")
    assert len(window) == 0
    assert window.take("x") and window.take("y")
    window.clear()
    assert len(window) == 0 and window.used("x") == 0


def test_the_keys_idle_longest_go_first():
    clock = _Clock(0.0)
    window = RateWindow(1000, 1, clock=clock, max_keys=3)
    for key in ("a", "b", "c"):
        clock.now += 1
        assert window.take(key)
    clock.now += 1
    assert window.take("d")
    assert len(window) == 3
    # "a" was idle longest and starts fresh; "b" is still charged.
    assert window.take("a")
    assert not window.take("c")


class _YieldingKey:
    """One key whose hash gives the other threads a turn: a take that is not one step under
    its lock then loses its check to another thread between reading the total and charging."""

    def __hash__(self):
        time.sleep(0.0001)
        return 308

    def __eq__(self, other):
        return isinstance(other, _YieldingKey)


def test_eight_threads_get_exactly_the_limit():
    window = RateWindow(60, 100, clock=lambda: 5.0)
    key = _YieldingKey()
    granted = []
    start = threading.Barrier(8)

    def worker():
        start.wait()
        granted.append(sum(1 for _ in range(50) if window.take(key)))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sum(granted) == 100


# ── differential fuzz against the converted copies, as they were ──────────────────────────

class _OldByteBudget:
    """app_link/room.py `_say_budget_ok` / `_say_refund` before the conversion."""

    def __init__(self, limit, window_s):
        self.limit, self.window_s, self.spent = limit, window_s, {}

    def take(self, scope, size, now):
        spent = self.spent.setdefault(scope, deque())
        while spent and now - spent[0][0] > self.window_s:
            spent.popleft()
        if sum(n for _t, n in spent) + size > self.limit:
            return False
        spent.append((now, size))
        return True

    def refund(self, scope, size):
        spent = self.spent.get(scope)
        if not spent:
            return False
        for index in range(len(spent) - 1, -1, -1):
            if spent[index][1] == size:
                del spent[index]
                return True
        return False


class _OldCounter:
    """app_link/bridge.py `_rate_ok` (and session.py `SessionLimiter._hit`) before the
    conversion."""

    def __init__(self, limit, window_s):
        self.limit, self.window_s, self.times = limit, window_s, {}

    def take(self, key, now):
        times = self.times.setdefault(key, deque())
        while times and now - times[0] > self.window_s:
            times.popleft()
        if len(times) >= self.limit:
            return False
        times.append(now)
        return True


def _fuzz(draws: int, seed: int = 308) -> int:
    rng = random.Random(seed)
    budget_new = RateWindow(3600, clock=lambda: 0.0)
    budget_old = _OldByteBudget(2000, 3600)
    count_new = RateWindow(60, clock=lambda: 0.0)
    count_old = _OldCounter(30, 60)
    now = 0.0
    for step in range(draws):
        now += rng.choice((0.0, 0.0, rng.uniform(0, 5), rng.uniform(0, 120), 60.0, 3600.0))
        key = f"k{rng.randint(1, 5)}"
        op = rng.random()
        if op < 0.55:
            size = rng.randint(1, 300)
            got = budget_new.take(key, size, limit=2000, now=now)
            want = budget_old.take(key, size, now)
            assert got == want, (step, "take", key, size, now)
        elif op < 0.7:
            size = rng.randint(1, 300)
            got = budget_new.refund(key, size)
            want = budget_old.refund(key, size)
            # The old copy kept expired charges until its next take of that key, so it may give
            # back one of those where the window has none left (no decision changes); a live
            # charge the window gives back, the old copy gives back too.
            assert want or not got, (step, "refund", key, size)
            assert budget_new.used(key, now=now) == sum(
                n for t, n in budget_old.spent.get(key, ()) if now - t <= 3600), (step, "refund")
        else:
            got = count_new.take(key, limit=30, now=now)
            want = count_old.take(key, now)
            assert got == want, (step, "count", key, now)
    return draws


def test_the_window_decides_as_the_copies_it_replaced():
    """5,000 seeded operations (a few hundred ms); `VAF_RATE_WINDOW_FUZZ_DRAWS` runs more outside the
    suite."""
    assert _fuzz(int(os.environ.get("VAF_RATE_WINDOW_FUZZ_DRAWS", "5000"))) > 0


def test_the_conversions_use_the_window():
    """The three app-link copies were converted, not kept beside the window."""
    from vaf.core.app_link import bridge, room, session
    assert not hasattr(bridge, "_msg_times") and isinstance(bridge._MSG_RATE, RateWindow)
    assert not hasattr(room, "_say_bytes") and isinstance(room._SAY_BYTES, RateWindow)
    assert not hasattr(session.SessionLimiter, "_hit")
    limiter = session.SessionLimiter(clock=lambda: 0.0)
    assert isinstance(limiter._per_peer, RateWindow) and isinstance(limiter._per_conn, RateWindow)


def test_a_wall_clock_window_takes_a_moment_given_by_the_caller():
    window = RateWindow(3600, 10, clock=time.time)
    then = time.time() - 7200
    assert window.take("a", 10, now=then)
    assert window.take("a", 10), "a charge two hours old is outside the hour"
