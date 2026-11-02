# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The fair queue primitive (vaf/core/fair_queue.py): order, place, refusals, numbers.

Threads are released by Events and every wait is bounded, so the order each test sees is
the queue's, never the scheduler's.
"""
import json
import threading
import time

import pytest

from vaf.core import fair_queue
from vaf.core.fair_queue import FairOrder, QueueBusy, ResourceQueue


@pytest.fixture(autouse=True)
def _fresh_registry(monkeypatch):
    monkeypatch.setattr(fair_queue, "_registry", {})


def _wait_until(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return False


class _Holder:
    """Holds one slot of `q` under `key` in its own thread until release()."""

    def __init__(self, q, key="holder"):
        self._release = threading.Event()
        self._held = threading.Event()
        self._t = threading.Thread(target=self._run, args=(q, key), daemon=True)
        self._t.start()
        assert self._held.wait(5)

    def _run(self, q, key):
        with q.slot(key):
            self._held.set()
            self._release.wait(10)

    def release(self):
        self._release.set()
        self._t.join(5)


def _waiter(q, key, out, name, **kw):
    def run():
        try:
            with q.slot(key, poll_s=0.01, **kw):
                out.append(name)
        except QueueBusy as busy:
            out.append((name, busy.reason))
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def _enqueue_in_order(q, specs, out, **kw):
    """Start waiters one by one, each only after the previous one is in the queue."""
    threads = []
    for key, name in specs:
        before = q.metrics()["waiting"]
        threads.append(_waiter(q, key, out, name, **kw.get(name, {})))
        assert _wait_until(lambda: q.metrics()["waiting"] == before + 1)
    return threads


def test_round_robin_between_users():
    """A holds the slot and queues A2, A3; then B1 arrives. B waits behind at most one of A's
    items per round. MUTATION: pop the oldest item globally (A2, A3, B1) - red."""
    q = ResourceQueue("rr", slots=1)
    holder = _Holder(q, "A")
    out = []
    threads = _enqueue_in_order(q, [("A", "A2"), ("A", "A3"), ("B", "B1")], out)
    holder.release()
    for t in threads:
        t.join(5)
    assert out == ["A2", "B1", "A3"]


def test_fair_order_alone_is_round_robin_and_never_served_first():
    order = FairOrder()
    for key, item in [("A", "a1"), ("A", "a2"), ("A", "a3"), ("B", "b1"), ("B", "b2")]:
        order.push(key, item)
    assert [order.position(i) for i in ("a1", "a2", "a3", "b1", "b2")] == [1, 3, 5, 2, 4]
    assert [order.pop() for _ in range(5)] == ["a1", "b1", "a2", "b2", "a3"]
    assert len(order) == 0 and order.keys_waiting() == 0


def test_pop_skips_the_ineligible_and_keeps_their_place():
    """A blocked item is passed, and comes out first once it is eligible again.
    MUTATION: ignore `eligible` (the blocked head comes out) - red."""
    order = FairOrder()
    for key, item in (("a", "a1"), ("a", "a2"), ("b", "b1")):
        order.push(key, item)
    assert order.pop(eligible=lambda it: it != "a1") == "a2"
    assert order.items() == ["a1", "b1"]
    assert order.pop() == "b1"          # a was served, b never was
    assert order.pop() == "a1"


def test_pop_ranks_by_the_first_eligible_arrival():
    """Among keys never served, the one whose first ELIGIBLE item arrived first wins.
    MUTATION: rank by the key's head arrival - a wins with a3, red."""
    order = FairOrder()
    for key, item in (("a", "a1"), ("b", "b1"), ("a", "a3")):
        order.push(key, item)
    assert order.pop(eligible=lambda it: it != "a1") == "b1"
    assert order.pop(eligible=lambda it: it != "a1") == "a3"


def test_nothing_eligible_raises():
    order = FairOrder()
    with pytest.raises(IndexError):
        order.pop(eligible=lambda it: True)
    order.push("a", "a1")
    with pytest.raises(IndexError):
        order.pop(eligible=lambda it: False)
    assert len(order) == 1 and order.items() == ["a1"]


def test_items_and_copy():
    """items() lists arrival order; a copy pops on its own, with the same history.
    MUTATION: copy() sharing the deques - the original loses items, red."""
    order = FairOrder()
    for key, item in (("a", "a1"), ("b", "b1"), ("a", "a2"), ("c", "c1")):
        order.push(key, item)
    assert order.pop() == "a1"
    assert order.items() == ["b1", "a2", "c1"]
    twin = order.copy()
    assert [twin.pop() for _ in range(3)] == ["b1", "c1", "a2"]
    assert len(twin) == 0
    assert order.items() == ["b1", "a2", "c1"] and len(order) == 3
    assert [order.pop() for _ in range(3)] == ["b1", "c1", "a2"]


def test_positions_follow_the_fair_order():
    """Each waiter's on_wait reports the place it holds in the order of the test above.
    MUTATION: compute the position by arrival index (A3 2, B1 3) - red."""
    q = ResourceQueue("pos", slots=1)
    holder = _Holder(q, "A")
    seen = {"A2": [], "A3": [], "B1": []}
    out = []
    kw = {n: {"on_wait": (lambda info, n=n: seen[n].append(info.position))} for n in seen}
    threads = _enqueue_in_order(q, [("A", "A2"), ("A", "A3"), ("B", "B1")], out, **kw)
    assert _wait_until(lambda: seen["A3"] and seen["A3"][-1] == 3)
    assert seen["A2"][-1] == 1 and seen["B1"][-1] == 2
    holder.release()
    for t in threads:
        t.join(5)
    # Granted after a wait: the last report is position 0.
    assert all(v[-1] == 0 for v in seen.values())


def test_eta_needs_three_samples():
    """ETA is None before three service samples, then ceil(position / slots) * p50.
    MUTATION: drop the ceil(position / slots), use position * p50 - red."""
    now = [100.0]
    q = ResourceQueue("eta", slots=2, clock=lambda: now[0])
    assert q.estimate("x").eta_s is None
    for _ in range(2):
        with q.slot("svc"):
            now[0] += 2.0
    h1, h2 = _Holder(q, "h1"), _Holder(q, "h2")
    assert q.estimate("x").position == 1 and q.estimate("x").eta_s is None   # two samples
    h1.release()
    h1 = _Holder(q, "h1")                     # third sample (0 s), p50 still 2.0
    out = []
    threads = _enqueue_in_order(q, [("w1", "w1"), ("w2", "w2")], out)
    probe = q.estimate("x")
    assert probe.position == 3
    assert probe.eta_s == pytest.approx(4.0)  # ceil(3 / 2) * 2.0
    h1.release()
    h2.release()
    for t in threads:
        t.join(5)


def test_full_refuses_without_waiting():
    """MUTATION: skip the max_waiting check (the caller waits, then times out) - red."""
    q = ResourceQueue("full", slots=1, max_waiting=1)
    holder = _Holder(q)
    out = []
    threads = _enqueue_in_order(q, [("a", "a")], out)
    started = time.monotonic()
    with pytest.raises(QueueBusy) as excinfo:
        with q.slot("b", deadline_s=2.0):
            pass
    assert excinfo.value.reason == "full"
    assert time.monotonic() - started < 0.5
    assert q.metrics()["refused"]["full"] == 1
    holder.release()
    for t in threads:
        t.join(5)


def test_timeout_removes_the_ticket():
    """A waiter whose deadline passes leaves the queue: the one behind it moves up.
    MUTATION: leave the ticket in the order after the timeout (the next one stays at 2) - red."""
    q = ResourceQueue("to", slots=1)
    holder = _Holder(q)
    out = []
    seen = []
    threads = _enqueue_in_order(
        q, [("a", "a"), ("b", "b")], out,
        a={"deadline_s": 0.3}, b={"on_wait": lambda info: seen.append(info.position)})
    assert _wait_until(lambda: ("a", "timeout") in out)
    assert _wait_until(lambda: seen and seen[-1] == 1)
    assert q.metrics()["waiting"] == 1 and q.metrics()["refused"]["timeout"] == 1
    holder.release()
    for t in threads:
        t.join(5)
    assert out == [("a", "timeout"), "b"]


def test_cancel_leaves_the_queue():
    """cancelled() turning true takes the waiter out within poll_s."""
    q = ResourceQueue("cx", slots=1)
    holder = _Holder(q)
    gone = threading.Event()
    out = []
    threads = _enqueue_in_order(q, [("a", "a")], out, a={"cancelled": gone.is_set})
    gone.set()
    started = time.monotonic()
    assert _wait_until(lambda: out == [("a", "cancelled")], timeout=1.0)
    assert time.monotonic() - started < 0.5
    assert q.metrics()["refused"]["cancelled"] == 1 and q.metrics()["waiting"] == 0
    holder.release()
    for t in threads:
        t.join(5)


def test_fast_path_never_asks_cancelled():
    """A free slot with nobody waiting is granted without consulting cancelled - so a
    hang-up can never cancel a turn that did not wait. MUTATION: poll cancelled before
    the fast path - red."""
    q = ResourceQueue("fast", slots=1)
    asked = []

    def cancelled():
        asked.append(1)
        return True

    entered = []
    with q.slot("a", cancelled=cancelled, deadline_s=0.0):
        entered.append(1)
    assert entered == [1] and asked == []


def test_reentrant_slot_raises():
    """Taking a slot of a queue this thread already holds raises instead of waiting on
    itself. MUTATION: remove the held-set check (the thread hangs, the watchdog fires) - red."""
    q = ResourceQueue("re", slots=1)
    result = []

    def run():
        with q.slot("a"):
            try:
                with q.slot("a"):
                    result.append("nested")
            except RuntimeError:
                result.append("refused")

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(2.0)
    assert not t.is_alive(), "a re-entrant slot deadlocked"
    assert result == ["refused"]
    # Different queues nest.
    other = ResourceQueue("re2", slots=1)
    with q.slot("a"):
        with other.slot("a"):
            pass


def test_metrics_shape_and_no_keys():
    """Counts and durations per queue, never a key. MUTATION: add the keys to metrics(), or
    drop refused.cancelled - red."""
    q = fair_queue.resource_queue("shape", slots=1, max_waiting=1)
    secret_a, secret_b, secret_c = "alice-scope-7f3a", "bob-scope-91c2", "carol-scope-55d0"
    with q.slot(secret_a):
        pass
    holder = _Holder(q, secret_a)
    out = []
    threads = _enqueue_in_order(q, [(secret_b, "b")], out)
    with pytest.raises(QueueBusy):
        with q.slot(secret_c):
            pass
    snap = fair_queue.queue_metrics()
    holder.release()
    for t in threads:
        t.join(5)
    assert [m["name"] for m in snap] == ["shape"]
    m = snap[0]
    assert {"name", "slots", "running", "waiting", "waiting_users", "served", "refused",
            "wait_ms", "service_ms", "last_refusal_age_s"} <= set(m)
    assert m["waiting"] == 1 and m["running"] == 1 and m["waiting_users"] == 1
    assert set(m["refused"]) == {"timeout", "full", "cancelled"}
    assert m["refused"]["full"] == 1
    assert set(m["wait_ms"]) == {"p50", "p95", "max"} and set(m["service_ms"]) == {"p50", "p95"}
    serialized = json.dumps(snap)
    for secret in (secret_a, secret_b, secret_c, "alice", "bob", "carol"):
        assert secret not in serialized


def test_the_registry_hands_out_one_queue_per_name():
    q = fair_queue.resource_queue("reg", slots=1, max_waiting=3)
    assert fair_queue.resource_queue("reg", slots=1, max_waiting=3) is q


def test_a_second_registration_cannot_drop_the_share():
    """A caller asking for other numbers gets an error, never the existing queue: in
    either order, a per-person share cannot silently disappear. MUTATION: hand back the
    existing queue whatever was asked - red."""
    q = fair_queue.resource_queue("share-reg", slots=1, max_waiting=12, max_waiting_per_key=3)
    with pytest.raises(ValueError):
        fair_queue.resource_queue("share-reg", slots=1, max_waiting=12)
    assert fair_queue.resource_queue("share-reg", slots=1, max_waiting=12,
                                     max_waiting_per_key=3) is q
    assert q.max_waiting_per_key == 3
    fair_queue.resource_queue("weak-first", slots=1, max_waiting=12)
    with pytest.raises(ValueError):
        fair_queue.resource_queue("weak-first", slots=1, max_waiting=12, max_waiting_per_key=3)


def test_unbounded_queue_never_waits():
    q = ResourceQueue("open", slots=None)
    inside = threading.Barrier(3, timeout=2)

    def run():
        with q.slot("same-person"):
            inside.wait()

    threads = [threading.Thread(target=run, daemon=True) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert q.metrics()["served"] == 3 and q.estimate().eta_s is None


def test_the_admin_aliases_are_one_person(monkeypatch):
    """Fairness keys on the canonical scope: the local admin's scope and no scope at all
    are the same bucket, so the admin cannot skip ahead by alternating them."""
    import vaf.core.config as config
    monkeypatch.setattr(config, "get_local_admin_scope_id", lambda: "admin-scope-1")
    assert fair_queue._canonical_key("admin-scope-1") == fair_queue._canonical_key(None)
    assert fair_queue._canonical_key(" tenant-b ") == "tenant-b"


def test_one_person_cannot_lock_the_others_out():
    """A fills its own share of the waiters; A's next request is refused as A's own
    "full", and B is still admitted and served before A's second. MUTATION: drop the
    per-key check (A's third request waits, B gets the global "full") - red."""
    q = ResourceQueue("share", slots=1, max_waiting=3, max_waiting_per_key=2)
    holder = _Holder(q, "C")
    out = []
    threads = _enqueue_in_order(q, [("A", "A1"), ("A", "A2")], out)
    with_a = []
    t = _waiter(q, "A", with_a, "A3")
    t.join(2)
    assert with_a == [("A3", "full")]
    threads += _enqueue_in_order(q, [("B", "B1")], out)
    holder.release()
    for th in threads:
        th.join(5)
    assert out == ["A1", "B1", "A2"]
    assert q.metrics()["refused"]["full"] == 1


def test_own_share_says_so():
    busy = QueueBusy("stt", "full", position=3, own_share=True)
    assert "2 of your requests" in str(busy) and busy.own_share


class _Boom(BaseException):
    """Not an Exception: the callbacks' quiet guard lets it through, like an interrupt."""


def test_a_slot_granted_while_the_waiter_fails_is_handed_on():
    """The waiter's callback lets the slot go to it and then dies: the slot it was granted
    in that instant must be handed on, never kept. MUTATION: drop the release in the
    waiter's cleanup (the queue stays occupied) - red."""
    q = ResourceQueue("handover", slots=1)
    holder = _Holder(q)
    calls = []

    def on_wait(info):
        calls.append(info.position)
        if info.position == 1 and len(calls) == 1:
            holder.release()
            assert _wait_until(lambda: q.metrics()["running"] == 1 and not q.metrics()["waiting"])
            raise _Boom()

    caught = []

    def run():
        try:
            with q.slot("a", on_wait=on_wait, poll_s=0.01):
                caught.append("entered")
        except _Boom:
            caught.append("boom")

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(5)
    assert caught == ["boom"]
    assert q.metrics()["running"] == 0
    with q.slot("b", deadline_s=0.0):
        pass


def test_a_failing_grant_report_does_not_leak_the_slot():
    """The report of the grant (position 0) dies: the slot and the thread's hold are
    released. MUTATION: drop the release around the final report - red."""
    q = ResourceQueue("leak", slots=1)
    holder = _Holder(q)

    def on_wait(info):
        if info.position == 0:
            raise _Boom()

    result = []

    def run():
        try:
            with q.slot("a", on_wait=on_wait, poll_s=0.01):
                result.append("entered")
        except _Boom:
            result.append("boom")
        try:
            with q.slot("a", deadline_s=1.0):
                result.append("again")
        except RuntimeError:
            result.append("still held")

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert _wait_until(lambda: q.metrics()["waiting"] == 1)
    holder.release()
    t.join(5)
    assert result == ["boom", "again"]
    assert q.metrics()["running"] == 0
