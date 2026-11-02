# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A fair waiting queue in front of a scarce resource: one person with many requests no
longer holds up the others, and whoever waits learns where they stand.

Three pieces, stdlib only, exported on the facade (``vaf.ResourceQueue`` and friends):

- :class:`FairOrder` is the ORDER alone: waiting items grouped by a key (a person), FIFO
  within a key, and the next item always taken from the key that was served least
  recently, which is round robin across people. No threads, no clock: kept apart from
  admission so a dispatcher with workers of its own (one that hands tasks out instead of
  holding a caller) can use the same order.
- :class:`ResourceQueue` is admission: ``with queue.slot(key): ...`` holds one of the
  resource's slots for the length of a blocking call. A free slot with nobody waiting is
  granted at once (the fast path); otherwise the caller waits in fair order, is told its
  place (:class:`WaitInfo`), and leaves with :class:`QueueBusy` when the queue is full,
  its deadline passes, or its ``cancelled()`` turns true.
- :func:`resource_queue` / :func:`queue_metrics`: one queue per resource name per
  process (a second registration with other numbers is an error, never a silent change
  of the limits the first one declared), and its numbers (length, wait and service time, refusals) for the health
  check. The metrics carry counts only, never a key.

Why one slot is right for the speech containers, measured on a 16-core machine with the
containers healthy: three concurrent STT requests each took 1.84-1.94 s against 0.54-0.61 s
alone, and three TTS requests 3.11-3.19 s against 1.05 s, all three finishing together.
Neither container runs anything in parallel; they share the CPU. One after another the
three STT requests finish at 0.6 / 1.2 / 1.8 s: the first caller is answered three times
sooner and the last no later.

The rules a caller relies on:

- ``cancelled`` is consulted only while WAITING. An uncontended call never asks it, so a
  hang-up can only cancel work that has not started.
- A thread may not take a second slot of a queue it already holds (``RuntimeError``):
  with one slot that would wait on itself forever. Slots of DIFFERENT queues nest fine.
- ``on_wait(WaitInfo)`` fires once on entering the queue, again whenever the place
  changes, and with position 0 when the slot is granted after a wait. It never fires on
  the fast path. An exception in it is swallowed: a broken display never stops the wait.
- A :class:`WaitInfo` carries the caller's own place and wait, nothing about anyone else.
- No key (None) is one person: the canonical "default", the local admin's bucket. A lane
  that serves many people (a channel bridge, a server route) must pass each person's scope,
  or they all share that one bucket and its ``max_waiting_per_key``.

Named boundaries, each with the measurement that supports it:

- Per process. A CLI dictation and the web server share the STT container by processor
  sharing, exactly as before (measured 3.0x per request at three concurrent). The server
  process carries every web user, the voice call and the channel bridges, which is where
  contention arises.
- No async admission (``await slot_async``). A waiting speech caller already held an
  executor thread inside its HTTP call before this queue existed; three concurrent STT
  calls held three threads for 1.84 s each (5.5 thread-seconds), in the queue the same
  batch finishes in 3.6. ``max_waiting`` caps the waiters per resource, and
  ``max_waiting_per_key`` caps one person's share of them, so nobody can fill the queue
  and lock the others out (several tabs or sockets of one account would otherwise do it).
- No scale-out of the speech containers and no overflow to an API provider when a queue
  is long: both wait for a pilot that measures the need. The API speech lanes are not
  gated (no local resource), only measured, so that decision has its numbers.
- The admin "test voice" proxy (``/api/tts/synthesize``) is one click by an admin, not a
  product lane, and stays outside.
- Embeddings (ONNX MiniLM) and speaker ID (sherpa-onnx) run in-process in milliseconds,
  with a lock only around the model load.
- llama.cpp slots shared by sub-agents and the coder across processes: llama-server
  queues internally, and no cross-process primitive exists; the server's own chat turns
  wait in TaskQueue (vaf/core/task_queue.py), which takes turns by person on FairOrder.
  vLLM batches with its own scheduler; its waiting count is read for the health row.
  llama-server runs without ``--metrics``, so its waiting count is not read.
- Not in the chat-turn queue: thinking and automation runs (their own Agent in their own
  thread; a thinking run on the local model already yields to every queued or running
  turn, and moving automations in would change their timeout, room binding and tool
  stripping, a change of its own) and the voice call reply (a live call cannot wait
  behind a chat turn; it refuses as busy while the local model is exclusive). Channel
  turns get the fair order but no waiting message, which would be an extra outbound
  message on someone's messenger. TaskQueue has no cap per person and never refuses: a
  dropped chat message is worse than a wait, and the round robin bounds one person's
  cost to another at one turn per round per band.
- The browser pool start never waits (admit, or fall back to the shared container);
  sandbox environments are per-person containers; admin jobs (repair, backup, update)
  refuse a second job, which is correct for them; mail and calendar sync run in the
  background where nobody waits live.
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, Hashable, List, Optional, Tuple

__all__ = ["FairOrder", "QueueBusy", "ResourceQueue", "WaitInfo", "queue_metrics",
           "resource_queue"]

# The metrics window: what `vaf health` and the admin endpoint summarize.
METRICS_WINDOW_S = 15 * 60
_MAX_SAMPLES = 500
# Serve stamps kept for keys that wait no longer. Dropping the OLDEST stamps preserves the
# order: a dropped key ranks as never served, which it would have ranked near anyway.
_MAX_STAMPS = 1024


class FairOrder:
    """Waiting items in round-robin order across keys, FIFO within a key.

    ``pop()`` takes the head of the key that was served least recently; a key never
    served comes first, and ties go to the earliest-arrived head. The serve stamp is a
    counter, so no clock is involved and the order is deterministic.

    A dispatcher whose items can be blocked for a while (a chat whose previous turn still
    runs) passes ``pop(eligible=...)``: only eligible items take part, and the blocked
    ones keep their places for later. ``items()`` and ``copy()`` let it compute every
    waiting item's place without touching the real order.
    """

    def __init__(self) -> None:
        self._queues: Dict[Hashable, Deque[Tuple[int, Any]]] = {}
        self._key_of: Dict[int, Hashable] = {}
        self._served: Dict[Hashable, int] = {}
        self._arrivals = 0
        self._stamp = 0

    def push(self, key: Hashable, item: Any) -> None:
        if id(item) in self._key_of:
            raise ValueError("this item is already waiting")
        self._arrivals += 1
        self._queues.setdefault(key, deque()).append((self._arrivals, item))
        self._key_of[id(item)] = key

    def _rank(self, served: Dict[Hashable, int], queues) -> Hashable:
        return min(queues, key=lambda k: (served.get(k, 0), queues[k][0][0]))

    def pop(self, eligible: Optional[Callable[[Any], bool]] = None) -> Any:
        """The next item in fair order; IndexError when nothing waits.

        With ``eligible``, only the items it accepts take part: among the keys holding one,
        the least recently served wins (ties: whose first eligible item arrived first), and
        that key's first eligible item is taken. The others keep their places, and
        IndexError comes when no waiting item is eligible.
        """
        if not self._queues:
            raise IndexError("nothing waits")
        if eligible is None:
            key = self._rank(self._served, self._queues)
            index = 0
        else:
            best: Optional[Tuple[Tuple[int, int], Hashable, int]] = None
            for k, q in self._queues.items():
                for i, (arrival, candidate) in enumerate(q):
                    if eligible(candidate):
                        rank = (self._served.get(k, 0), arrival)
                        if best is None or rank < best[0]:
                            best = (rank, k, i)
                        break
            if best is None:
                raise IndexError("nothing eligible waits")
            _, key, index = best
        q = self._queues[key]
        _, item = q[index]
        del q[index]
        if not q:
            del self._queues[key]
        del self._key_of[id(item)]
        self._stamp += 1
        self._served[key] = self._stamp
        self._trim()
        return item

    def remove(self, item: Any) -> bool:
        """Take a waiting item out (a timeout, a cancel). False when it is not waiting."""
        key = self._key_of.pop(id(item), None)
        if key is None:
            return False
        q = self._queues[key]
        for entry in list(q):
            if entry[1] is item:
                q.remove(entry)
                break
        if not q:
            del self._queues[key]
        self._trim()
        return True

    def position(self, item: Any) -> int:
        """The 1-based place in the order ``pop()`` would follow; 0 when not waiting."""
        if id(item) not in self._key_of:
            return 0
        queues = {k: deque(v) for k, v in self._queues.items()}
        served = dict(self._served)
        stamp = self._stamp
        place = 0
        while queues:
            key = self._rank(served, queues)
            _, head = queues[key].popleft()
            if not queues[key]:
                del queues[key]
            place += 1
            if head is item:
                return place
            stamp += 1
            served[key] = stamp
        return 0

    def waiting_for(self, key: Hashable) -> int:
        """How many items of this key wait."""
        q = self._queues.get(key)
        return len(q) if q else 0

    def keys_waiting(self) -> int:
        """How many keys have something waiting: a count, never who."""
        return len(self._queues)

    def items(self) -> List[Any]:
        """Every waiting item, in arrival order."""
        entries = [entry for q in self._queues.values() for entry in q]
        return [item for _, item in sorted(entries, key=lambda e: e[0])]

    def copy(self) -> "FairOrder":
        """An independent order with the same items, serve history and counters (the items
        themselves are shared): for working out places without moving anyone."""
        twin = FairOrder()
        twin._queues = {k: deque(q) for k, q in self._queues.items()}
        twin._key_of = dict(self._key_of)
        twin._served = dict(self._served)
        twin._arrivals = self._arrivals
        twin._stamp = self._stamp
        return twin

    def __len__(self) -> int:
        return len(self._key_of)

    def _trim(self) -> None:
        if not self._queues:
            # Nobody waits: every later arrival is new, so the history decides nothing.
            self._served.clear()
            return
        if len(self._served) > _MAX_STAMPS:
            idle = sorted((s, k) for k, s in self._served.items() if k not in self._queues)
            for _, key in idle[:len(self._served) - _MAX_STAMPS]:
                del self._served[key]


@dataclass(frozen=True)
class WaitInfo:
    """Where one caller stands: ``position`` 1 is next, 0 means granted (or no wait).
    ``eta_s`` is None until the queue has three service samples to estimate from."""
    resource: str
    position: int
    eta_s: Optional[float]
    waited_s: float


class QueueBusy(Exception):
    """A queue refused a caller: ``reason`` is ``full`` (too many already wait, or this
    caller's own share is used up: ``own_share``), ``timeout`` (the deadline passed) or
    ``cancelled`` (the caller gave up)."""

    def __init__(self, resource: str, reason: str, *, position: int = 0,
                 waited_s: float = 0.0, eta_s: Optional[float] = None,
                 own_share: bool = False) -> None:
        self.resource = resource
        self.reason = reason
        self.position = position
        self.waited_s = waited_s
        self.eta_s = eta_s
        self.own_share = own_share
        super().__init__(self._sentence())

    def _sentence(self) -> str:
        if self.reason == "full" and self.own_share:
            return (f"{self.resource} is busy: {self.position - 1} of your requests are "
                    "already waiting, try again when they are done.")
        if self.reason == "full":
            return (f"{self.resource} is busy: {max(self.position - 1, 0)} requests are "
                    "already waiting, try again shortly.")
        if self.reason == "timeout":
            return (f"{self.resource} is busy: no free slot after waiting "
                    f"{self.waited_s:.1f} s at place {self.position}.")
        return f"{self.resource}: the wait was cancelled after {self.waited_s:.1f} s."


class _Ticket:
    __slots__ = ("key", "enqueued", "granted", "granted_at")

    def __init__(self, key: str, enqueued: float) -> None:
        self.key = key
        self.enqueued = enqueued
        self.granted = False
        self.granted_at = 0.0


def _canonical_key(key: Any) -> str:
    """The person a request belongs to, canonical: the admin's aliases are one bucket and
    no key at all is "default" (the same rule as every per-person store)."""
    try:
        from vaf.core.trust import _scope_key
        return _scope_key(None if key is None else str(key))
    except Exception:
        text = str(key or "").strip()
        return text or "default"


def _percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[rank - 1]


def _ms(value: Optional[float]) -> Optional[int]:
    return None if value is None else int(round(value * 1000))


_held = threading.local()


class ResourceQueue:
    """Admission to a resource with ``slots`` parallel places (None: unbounded, a
    pass-through that only measures), at most ``max_waiting`` waiters, of them at most
    ``max_waiting_per_key`` from one person (None: no share of their own), in fair order."""

    def __init__(self, name: str, *, slots: Optional[int] = 1, max_waiting: int = 12,
                 max_waiting_per_key: Optional[int] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if slots is not None and slots < 1:
            raise ValueError("slots must be at least 1, or None for unbounded")
        self.name = str(name)
        self.slots = slots
        self.max_waiting = max(0, int(max_waiting))
        self.max_waiting_per_key = (None if max_waiting_per_key is None
                                    else max(0, int(max_waiting_per_key)))
        self._clock = clock
        self._cond = threading.Condition()
        self._order = FairOrder()
        self._running = 0
        self._waits: Deque[Tuple[float, float]] = deque(maxlen=_MAX_SAMPLES)
        self._services: Deque[Tuple[float, float]] = deque(maxlen=_MAX_SAMPLES)
        self._refusals: Deque[Tuple[float, str]] = deque(maxlen=_MAX_SAMPLES)

    # ── internals (caller holds the condition) ──────────────────────────────

    def _has_room(self) -> bool:
        return self.slots is None or self._running < self.slots

    def _grant_waiting(self) -> None:
        now = self._clock()
        while self._order and self._has_room():
            ticket = self._order.pop()
            ticket.granted = True
            ticket.granted_at = now
            self._running += 1
        self._cond.notify_all()

    def _eta(self, position: int) -> Optional[float]:
        if self.slots is None or position <= 0:
            return None
        cutoff = self._clock() - METRICS_WINDOW_S
        samples = [v for t, v in self._services if t >= cutoff]
        if len(samples) < 3:
            return None
        return math.ceil(position / self.slots) * _percentile(samples, 50)

    def _refuse(self, reason: str, position: int, waited: float,
                own_share: bool = False) -> QueueBusy:
        self._refusals.append((self._clock(), reason))
        return QueueBusy(self.name, reason, position=position, waited_s=waited,
                         eta_s=self._eta(position), own_share=own_share)

    def _release(self, granted_at: float) -> None:
        with self._cond:
            now = self._clock()
            self._running -= 1
            self._services.append((now, max(0.0, now - granted_at)))
            self._grant_waiting()

    # ── public ──────────────────────────────────────────────────────────────

    def estimate(self, key: Any = None) -> WaitInfo:
        """What a new caller with this key would face right now."""
        with self._cond:
            if self._has_room() and not self._order:
                return WaitInfo(self.name, 0, None, 0.0)
            probe = object()
            self._order.push(_canonical_key(key), probe)
            try:
                position = self._order.position(probe)
            finally:
                self._order.remove(probe)
            return WaitInfo(self.name, position, self._eta(position), 0.0)

    def slot(self, key: Any = None, *, deadline_s: Optional[float] = None,
             cancelled: Optional[Callable[[], bool]] = None,
             on_wait: Optional[Callable[[WaitInfo], None]] = None,
             poll_s: float = 0.25) -> "_Slot":
        """Hold one slot for the ``with`` block. Raises :class:`QueueBusy` (full, timeout,
        cancelled) instead of entering, and ``RuntimeError`` when this thread already
        holds a slot of this queue."""
        return _Slot(self, _canonical_key(key), deadline_s, cancelled, on_wait, poll_s)

    def _acquire(self, key: str, deadline_s: Optional[float],
                 cancelled: Optional[Callable[[], bool]],
                 on_wait: Optional[Callable[[WaitInfo], None]], poll_s: float) -> float:
        held = getattr(_held, "names", None)
        if held is None:
            held = _held.names = set()
        if id(self) in held:
            raise RuntimeError(f"this thread already holds a slot of the {self.name!r} queue")
        with self._cond:
            now = self._clock()
            if self._has_room() and not self._order:
                self._running += 1
                self._waits.append((now, 0.0))
                held.add(id(self))
                return now
            # The person's own share first: one account with many tabs fills its share,
            # never the whole queue, so everybody else is still admitted.
            if (self.max_waiting_per_key is not None
                    and self._order.waiting_for(key) >= self.max_waiting_per_key):
                raise self._refuse("full", self._order.waiting_for(key) + 1, 0.0,
                                   own_share=True)
            if len(self._order) >= self.max_waiting:
                raise self._refuse("full", len(self._order) + 1, 0.0)
            ticket = _Ticket(key, now)
            self._order.push(key, ticket)
        last_position: Optional[int] = None
        try:
            while True:
                with self._cond:
                    if ticket.granted:
                        break
                    now = self._clock()
                    waited = now - ticket.enqueued
                    position = self._order.position(ticket)
                    if deadline_s is not None and waited >= deadline_s:
                        raise self._refuse("timeout", position, waited)
                    info = WaitInfo(self.name, position, self._eta(position), waited)
                if position != last_position:
                    last_position = position
                    _call_quietly(on_wait, info)
                if cancelled is not None and _ask_quietly(cancelled):
                    with self._cond:
                        if not ticket.granted:
                            raise self._refuse("cancelled", position,
                                               self._clock() - ticket.enqueued)
                    break
                with self._cond:
                    if not ticket.granted:
                        step = poll_s
                        if deadline_s is not None:
                            step = min(step, max(0.0, deadline_s - waited))
                        self._cond.wait(max(step, 0.001))
        except BaseException:
            # The one place a waiter leaves: a refusal (timeout, cancelled) or anything
            # unexpected takes the ticket out, and the ones behind it move up.
            with self._cond:
                granted = ticket.granted
                if not granted:
                    self._order.remove(ticket)
                    self._cond.notify_all()
            if granted:
                # Granted in the instant this thread failed: hand the slot on.
                self._release(ticket.granted_at)
            raise
        with self._cond:
            self._waits.append((ticket.granted_at, max(0.0, ticket.granted_at - ticket.enqueued)))
            waited = ticket.granted_at - ticket.enqueued
        held.add(id(self))
        try:
            _call_quietly(on_wait, WaitInfo(self.name, 0, None, waited))
        except BaseException:
            # The slot is held but __enter__ never returns, so no __exit__ will free it.
            held.discard(id(self))
            self._release(ticket.granted_at)
            raise
        return ticket.granted_at

    def metrics(self) -> Dict[str, Any]:
        """Counts and durations over the last METRICS_WINDOW_S seconds; no key anywhere."""
        with self._cond:
            now = self._clock()
            cutoff = now - METRICS_WINDOW_S
            waits = [v for t, v in self._waits if t >= cutoff]
            services = [v for t, v in self._services if t >= cutoff]
            refusals = [(t, r) for t, r in self._refusals if t >= cutoff]
            refused = {"timeout": 0, "full": 0, "cancelled": 0}
            for _, reason in refusals:
                refused[reason] = refused.get(reason, 0) + 1
            last = max((t for t, _ in self._refusals), default=None)
            return {
                "name": self.name,
                "slots": self.slots,
                "running": self._running,
                "waiting": len(self._order),
                "waiting_users": self._order.keys_waiting(),
                "served": len(services),
                "refused": refused,
                "wait_ms": {"p50": _ms(_percentile(waits, 50)),
                            "p95": _ms(_percentile(waits, 95)),
                            "max": _ms(max(waits) if waits else None)},
                "service_ms": {"p50": _ms(_percentile(services, 50)),
                               "p95": _ms(_percentile(services, 95))},
                "last_refusal_age_s": None if last is None else round(now - last, 1),
            }


class _Slot:
    """The context manager ``ResourceQueue.slot`` returns."""

    def __init__(self, queue: ResourceQueue, key: str, deadline_s, cancelled, on_wait,
                 poll_s) -> None:
        self._queue = queue
        self._key = key
        self._args = (deadline_s, cancelled, on_wait, poll_s)
        self._granted_at: Optional[float] = None

    def __enter__(self) -> "_Slot":
        self._granted_at = self._queue._acquire(self._key, *self._args)
        return self

    def __exit__(self, *exc) -> None:
        names = getattr(_held, "names", None)
        if names is not None:
            names.discard(id(self._queue))
        if self._granted_at is not None:
            self._queue._release(self._granted_at)
            self._granted_at = None


def _call_quietly(fn: Optional[Callable[[WaitInfo], None]], info: WaitInfo) -> None:
    if fn is None:
        return
    try:
        fn(info)
    except Exception:
        pass


def _ask_quietly(fn: Callable[[], bool]) -> bool:
    try:
        return bool(fn())
    except Exception:
        return False


_registry: Dict[str, ResourceQueue] = {}
_registry_lock = threading.Lock()


def resource_queue(name: str, *, slots: Optional[int] = 1, max_waiting: int = 12,
                   max_waiting_per_key: Optional[int] = None) -> ResourceQueue:
    """The process-wide queue for ``name``, created on first use. A later call must ask
    for the same numbers: one with others raises ValueError, because handing back the
    existing queue would silently drop the limits one of the two callers relies on (a
    per-person share registered second would simply not exist)."""
    wanted = (slots, max(0, int(max_waiting)),
              None if max_waiting_per_key is None else max(0, int(max_waiting_per_key)))
    with _registry_lock:
        queue = _registry.get(name)
        if queue is None:
            return _registry.setdefault(name, ResourceQueue(
                name, slots=slots, max_waiting=max_waiting,
                max_waiting_per_key=max_waiting_per_key))
        held = (queue.slots, queue.max_waiting, queue.max_waiting_per_key)
        if held != wanted:
            raise ValueError(
                f"the queue {name!r} exists with slots, max_waiting, max_waiting_per_key = "
                f"{held}; a second registration asked for {wanted}")
        return queue


def queue_metrics() -> List[Dict[str, Any]]:
    """``metrics()`` of every queue this process registered, sorted by name."""
    with _registry_lock:
        queues = sorted(_registry.values(), key=lambda q: q.name)
    return [q.metrics() for q in queues]
