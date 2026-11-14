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
            size = rng.choice((0, rng.randint(1, 300)))
            got = budget_new.take(key, size, limit=2000, now=now)
            want = budget_old.take(key, size, now)
            assert got == want, (step, "take", key, size, now)
        elif op < 0.7:
            size = rng.randint(1, 300)
            budget_new.refund(key, size)
            budget_old.refund(key, size)
            # Expired charges linger in both until a take of that key prunes them, and not by
            # the same takes (the old copy pruned on a zero-weight take, the window records
            # nothing for one), so either may give back an expired charge the other no longer
            # holds. What counts is the live state, compared here, and every later decision.
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


# ── hardening: eviction after a refund, zero weights, the order of moments, finite numbers ──

def test_a_refund_puts_the_key_back_in_idle_order():
    """"a" at 0, "b" at 10, "a" at 20; the refund of a's charge at 20 leaves a's newest at
    0, so the next key beyond `max_keys` drops "a", not "b" (whose budget is live)."""
    clock = _Clock(0.0)
    window = RateWindow(1000, 5, clock=clock, max_keys=2)
    assert window.take("a")
    clock.now = 10.0
    assert window.take("b")
    clock.now = 20.0
    assert window.take("a")
    assert window.refund("a", 1)
    clock.now = 21.0
    assert window.take("c")
    assert window.used("b") == 1, "the key with a live newer charge was dropped"
    assert window.used("a") == 0


def test_a_zero_weight_records_nothing():
    window = RateWindow(60, 0, clock=_Clock())
    for _ in range(10000):
        assert window.take("a", 0)
    assert len(window) == 0, "every zero-weight take kept a record"
    assert window.take("b", 1, limit=1) and window.take("b", 0, limit=1)
    assert not window.take("b", 1, limit=1)


def test_an_expired_charge_given_back_changes_nothing():
    """Found by the differential fuzz (1,000,000 draws, seed 308, step 12,256): a zero-weight
    take prunes nothing, so a refund can still find a charge that has expired and gives it
    back. That changes no live total and no decision."""
    window = RateWindow(3600, 1000)
    assert window.take("k2", 228, now=0)
    assert window.take("k2", 0, now=4000)
    assert window.used("k2", now=4000) == 0
    window.refund("k2", 228)
    assert window.used("k2", now=4000) == 0
    assert window.take("k2", 1000, now=4000) and not window.take("k2", 1, now=4000)


def test_a_zero_weight_fits_under_a_lowered_limit():
    window = RateWindow(60, clock=_Clock())
    assert window.take("a", 5, limit=10)
    assert window.take("a", 0, limit=4), "zero always fits, also over a lowered limit"
    with pytest.raises(ValueError):
        window.take("a", 0, limit=4, now=float("nan"))


def test_a_refund_that_finds_nothing_walks_without_indexing(monkeypatch):
    """A deque's index is O(n): a refund walked by index costs O(n^2) when it finds nothing
    (100 ms per call at 100,000 charges). The walk reads no item by index."""
    import vaf.core.rate_window as rate_window
    reads = []

    class _Counted(deque):
        def __getitem__(self, index):
            reads.append(index)
            return super().__getitem__(index)

    monkeypatch.setattr(rate_window, "deque", _Counted)
    window = RateWindow(60, 10 ** 6, clock=_Clock())
    for _ in range(500):
        assert window.take("a")
    reads.clear()
    assert not window.refund("a", 2)
    assert window.refund("a", 1)
    assert len([r for r in reads if r != -1]) == 0, "the refund read items by index"


def test_used_with_a_moment_ahead_forgets_nothing():
    """`used` only reads: asked about a moment far ahead it counts what is live then, and the
    charges are still there for the next take at the window's own time."""
    window = RateWindow(60, 3)
    for moment in (1, 2, 3):
        assert window.take("k", now=moment)
    assert window.used("k", now=1e6) == 0
    assert not window.take("k", now=4), "a read with a future moment deleted live charges"
    assert window.used("k", now=4) == 3


def test_an_earlier_moment_is_taken_as_the_newest_recorded():
    """A moment handed in out of order (a stepped-back wall clock) is taken as the newest
    recorded one, so the expiry and the drop order keep holding: "a" at 50, "a" handed 0,
    "b" at 61; at 61 both charges of "a" are still within the minute."""
    window = RateWindow(60, 2)
    assert window.take("a", now=50)
    assert window.take("a", now=0)
    assert window.take("b", now=61)
    assert window.used("a", now=61) == 2
    assert not window.take("a", now=61), "a third charge of 'a' within one minute"
    window.clear()
    assert window.take("a", 2, now=0)
    assert window.take("a", 1, now=61), "clear kept the newest moment: the charge stood at 61"


def test_the_windows_clock_is_read_under_its_lock():
    seen = []
    window = None

    def clock():
        seen.append(window._lock.locked())
        return 5.0

    window = RateWindow(60, 3, clock=clock)
    window.take("a")
    window.used("a")
    assert seen and all(seen), "the clock was read before the lock was held"


@pytest.mark.parametrize("call", [
    lambda: RateWindow(float("nan")), lambda: RateWindow(float("inf")),
    lambda: RateWindow(60, float("nan")),
    lambda: RateWindow(60, 3).take("a", float("nan")),
    lambda: RateWindow(60, 3).take("a", float("inf")),
    lambda: RateWindow(60).take("a", limit=float("nan")),
    lambda: RateWindow(60, 3).take("a", now=float("nan")),
    lambda: RateWindow(60, 3).used("a", now=float("inf")),
    lambda: RateWindow(60, 3).refund("a", float("nan")),
])
def test_a_number_that_is_not_finite_is_refused(call):
    with pytest.raises(ValueError):
        call()


class _Model:
    """What the window promises, written the plain way: lists, the newest moment clamped,
    expired keys swept after a charge, beyond `max_keys` the key whose newest charge is
    oldest dropped."""

    def __init__(self, window_s, max_keys):
        self.window_s, self.max_keys, self.keys, self.latest = window_s, max_keys, {}, -1e18

    def _expire(self, key, moment):
        items = [c for c in self.keys.get(key, []) if moment - c[0] <= self.window_s]
        if items:
            self.keys[key] = items
        else:
            self.keys.pop(key, None)

    def take(self, key, weight, limit, now):
        if weight == 0:
            return True
        moment = max(now, self.latest)
        self._expire(key, moment)
        if sum(w for _t, w in self.keys.get(key, [])) + weight > limit:
            return False
        self.keys.setdefault(key, []).append((moment, weight))
        self.latest = moment
        for other in [k for k, v in self.keys.items() if moment - v[-1][0] > self.window_s]:
            del self.keys[other]
        while len(self.keys) > self.max_keys:
            del self.keys[min(self.keys, key=lambda k: self.keys[k][-1][0])]
        return True

    def refund(self, key, weight):
        items = self.keys.get(key, [])
        for index in range(len(items) - 1, -1, -1):
            if items[index][1] == weight:
                del items[index]
                if not items:
                    del self.keys[key]
                return True
        return False

    def used(self, key, now):
        moment = max(now, self.latest)
        return sum(w for t, w in self.keys.get(key, []) if moment - t <= self.window_s)


def _model_fuzz(draws: int, seed: int, *, ordered: bool) -> None:
    """`ordered`: distinct rising moments and three keys at most, so the drop order is
    tested; otherwise moments in any order (the clamp) with room for every key."""
    rng = random.Random(seed)
    max_keys = 3 if ordered else 100
    window = RateWindow(50, max_keys=max_keys, clock=lambda: 0.0)
    model = _Model(50, max_keys)
    now = 0.0
    for step in range(draws):
        if ordered:
            now += rng.choice((0.001, rng.uniform(0.001, 2), rng.uniform(0.001, 2),
                               rng.uniform(1, 60)))
        else:
            now = max(0.0, now + rng.uniform(-40, 30))
        key = rng.randint(1, 6)
        op = rng.random()
        if op < 0.55:
            weight = rng.choice((0, 1, 2, rng.randint(1, 9)))
            got = window.take(key, weight, limit=10, now=now)
            assert got == model.take(key, weight, 10, now), (step, "take", key, weight, now)
        elif op < 0.8:
            weight = rng.choice((1, 2, 1, 2, rng.randint(1, 9)))
            assert window.refund(key, weight) == model.refund(key, weight), (step, "refund")
        else:
            assert window.used(key, now=now) == model.used(key, now), (step, "used", key, now)


def test_the_window_keeps_its_promises_in_any_order():
    """Seeded model fuzz, 4,000 operations each way; `VAF_RATE_WINDOW_FUZZ_DRAWS` runs
    more."""
    draws = max(4000, int(os.environ.get("VAF_RATE_WINDOW_FUZZ_DRAWS", "4000")) // 4)
    for seed in (308, 310, 312, 314):
        _model_fuzz(draws, seed, ordered=True)
    _model_fuzz(draws, 309, ordered=False)


def test_the_conversions_use_the_window():
    """The three app-link copies were converted, not kept beside the window."""
    from vaf.core.app_link import bridge, room, session
    assert not hasattr(bridge, "_msg_times") and isinstance(bridge._MSG_RATE, RateWindow)
    assert not hasattr(room, "_say_bytes") and isinstance(room._SAY_BYTES, RateWindow)
    # Monotonic: a wall clock stepping back would hold every account's charges.
    assert room._SAY_BYTES._clock is time.monotonic
    assert not hasattr(session.SessionLimiter, "_hit")
    limiter = session.SessionLimiter(clock=lambda: 0.0)
    assert isinstance(limiter._per_peer, RateWindow) and isinstance(limiter._per_conn, RateWindow)


def test_a_wall_clock_window_takes_a_moment_given_by_the_caller():
    window = RateWindow(3600, 10, clock=time.time)
    then = time.time() - 7200
    assert window.take("a", 10, now=then)
    assert window.take("a", 10), "a charge two hours old is outside the hour"
