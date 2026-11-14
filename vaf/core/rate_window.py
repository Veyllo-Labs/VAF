# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A per-key sliding window: at most ``limit`` units per key in any ``window_s`` seconds.

For a lane that serves several people: one person writing a line a millisecond must not
slow the server down for everybody else, and the bound has to be per person, not one
counter for the whole lane. A unit is whatever the caller counts: a message (weight 1),
or bytes (the weight of a line is its size).

``take`` is all-or-nothing: a charge that does not fit the window is refused whole and
nothing is recorded. ``refund`` gives back the newest charge of exactly that weight, for
work that was charged and then not done (a line that could not be stored). A charge is
dropped once it is strictly older than ``window_s`` (``now - t > window_s``), so a charge
exactly ``window_s`` old still counts. A weight of 0 always fits and records nothing.

Times are taken as monotonic, for every key and across keys. The window's own clock is
read under its lock, so two threads cannot record their moments out of order. A ``now``
handed in that is earlier than the newest charge the window recorded (a wall clock that
stepped back, a caller that read the clock long before it got here) is taken as that
newest moment: the charges stay in order, which is what the expiry, the bound and the
order keys are dropped in rest on. A ``now`` far in the future moves that moment along
for every later charge until ``clear``. Every number handed in must be finite;
``ValueError`` otherwise.

Bounded memory: a key whose charges all expired is dropped, and beyond ``max_keys`` keys
the key whose newest charge is oldest is dropped too (a refund that gives back a key's
newest charge moves the key back to where its remaining newest charge puts it). A dropped
key starts fresh, so a flood spread over more than ``max_keys`` keys within one window can
pass more than ``limit`` for some of them; ``max_keys`` is sized far above the people a
lane serves.

Thread-safe: one lock per window, held only for the arithmetic. Stdlib only.

Users: the web room lane's write rate per account (web_server.py), the Veyllo App link's
messages per device (app_link/bridge.py), its new sessions per carrier connection and per
peer (app_link/session.py) and the room's ``room_say`` bytes per account
(app_link/room.py). NAMED BOUNDARY, the windows still written by hand elsewhere:
the relay's claim budget (app_link/relay.py, a strict ``< 60`` with a separate append),
the two key-less vector windows of the attachment store (attachment_rag.py, configured
there) and the agent loop's tool-execution window (agent.py); each is a later conversion,
not a different contract.
"""
from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict, deque
from typing import Callable, Deque, Hashable, Optional, Tuple

__all__ = ["RateWindow"]


def _finite(value: float, what: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{what} must be a finite number")
    return number


class _Charges:
    __slots__ = ("items", "total")

    def __init__(self) -> None:
        self.items: Deque[Tuple[float, float]] = deque()
        self.total = 0.0


class RateWindow:
    """At most ``limit`` units per key within any ``window_s`` seconds.

    ``clock`` gives the time when a call names none (``time.monotonic`` by default; a
    wall clock when charges must be compared with stored times). ``limit`` may be left
    out here and given per call, so a limit read from a constant at call time stays the
    one in force.
    """

    def __init__(self, window_s: float, limit: Optional[float] = None, *,
                 clock: Callable[[], float] = time.monotonic, max_keys: int = 10000) -> None:
        window = _finite(window_s, "window_s")
        if window <= 0:
            raise ValueError("window_s must be positive")
        if int(max_keys) < 1:
            raise ValueError("max_keys must be at least 1")
        self.window_s = window
        self.limit = None if limit is None else _finite(limit, "limit")
        self._clock = clock
        self._max_keys = int(max_keys)
        self._lock = threading.Lock()
        # The newest moment a charge was recorded at; no later charge is recorded before it.
        self._latest = -math.inf
        # Ordered by the time of each key's newest charge, oldest first: the front is the
        # key idle longest, which is the one dropped first.
        self._keys: "OrderedDict[Hashable, _Charges]" = OrderedDict()

    def _moment(self, now: Optional[float]) -> float:
        """The moment of a call; read under the lock when the window's own clock gives it."""
        return _finite(self._clock() if now is None else now, "now")

    def _expire(self, charges: _Charges, now: float) -> None:
        items = charges.items
        while items and now - items[0][0] > self.window_s:
            charges.total -= items.popleft()[1]
        if not items:
            # Exact again whenever the window empties, so float sums never drift.
            charges.total = 0.0

    def _live(self, key: Hashable, now: float) -> Optional[_Charges]:
        charges = self._keys.get(key)
        if charges is None:
            return None
        self._expire(charges, now)
        if not charges.items:
            del self._keys[key]
            return None
        return charges

    def _sweep(self, now: float) -> None:
        """Drop the keys idle longest while their newest charge has expired, and every key
        beyond ``max_keys``. Amortised O(1): each key is dropped at most once per charge."""
        while self._keys:
            key, charges = next(iter(self._keys.items()))
            if len(self._keys) <= self._max_keys and charges.items \
                    and now - charges.items[-1][0] <= self.window_s:
                return
            del self._keys[key]

    def _place(self, key: Hashable, charges: _Charges) -> None:
        """Put ``key`` back where its newest charge belongs in the idle order (after a
        refund made that charge older). O(keys) under the lock (about 4.5 ms at 10,000 keys),
        and only a refund of a key's newest charge pays it; a lane that refunds often across
        many keys should size ``max_keys`` with that in mind."""
        newest = charges.items[-1][0]
        others = list(self._keys.items())
        self._keys.clear()
        placed = False
        for other, held in others:
            if not placed and held.items and held.items[-1][0] > newest:
                self._keys[key] = charges
                placed = True
            self._keys[other] = held
        if not placed:
            self._keys[key] = charges

    def take(self, key: Hashable, weight: float = 1, *, limit: Optional[float] = None,
             now: Optional[float] = None) -> bool:
        """Charge ``weight`` units to ``key`` and answer True, or answer False and charge
        nothing when they do not fit: the key's live charges plus ``weight`` would exceed
        the limit (``limit`` here, else the window's). Raises ``ValueError`` when neither
        names a limit, when ``weight`` is negative, or when a number is not finite."""
        bound = self.limit if limit is None else _finite(limit, "limit")
        if bound is None:
            raise ValueError("RateWindow.take needs a limit (here or in the constructor)")
        weight = _finite(weight, "weight")
        if weight < 0:
            raise ValueError("weight must not be negative")
        if weight == 0:
            # Fits whatever the key has spent, also under a limit lowered for this call, and
            # leaves nothing behind.
            if now is not None:
                _finite(now, "now")
            return True
        with self._lock:
            # A moment earlier than the newest recorded charge is taken as that charge's.
            moment = max(self._moment(now), self._latest)
            charges = self._live(key, moment)
            spent = charges.total if charges is not None else 0.0
            if spent + weight > bound:
                return False
            if charges is None:
                charges = _Charges()
                self._keys[key] = charges
            else:
                self._keys.move_to_end(key)
            charges.items.append((moment, weight))
            charges.total += weight
            self._latest = moment
            self._sweep(moment)
            return True

    def refund(self, key: Hashable, weight: float) -> bool:
        """Give back the newest charge of exactly ``weight`` units to ``key``. True when one
        was found."""
        weight = _finite(weight, "weight")
        with self._lock:
            charges = self._keys.get(key)
            if charges is None:
                return False
            items = charges.items
            # Walked from the newest end without indexing (a deque's index is O(n), so an
            # indexed walk that finds nothing would be quadratic in the key's charges).
            for position, (_at, amount) in enumerate(reversed(items)):
                if amount == weight:
                    index = len(items) - 1 - position
                    newest = position == 0
                    del items[index]
                    charges.total -= weight
                    if not items:
                        del self._keys[key]
                    elif newest:
                        del self._keys[key]
                        self._place(key, charges)
                    return True
            return False

    def used(self, key: Hashable, *, now: Optional[float] = None) -> float:
        """The units ``key`` has spent within the window up to ``now``. Only reads: a ``now``
        ahead of the window's time counts the charges live then, and forgets none of them."""
        with self._lock:
            moment = max(self._moment(now), self._latest)
            charges = self._keys.get(key)
            if charges is None:
                return 0.0
            return sum(amount for at, amount in charges.items if moment - at <= self.window_s)

    def forget(self, key: Hashable) -> None:
        """Drop every charge of ``key``."""
        with self._lock:
            self._keys.pop(key, None)

    def clear(self) -> None:
        """Drop every charge of every key, and the newest moment with them."""
        with self._lock:
            self._keys.clear()
            self._latest = -math.inf

    def __len__(self) -> int:
        """How many keys hold charges (expired ones included until they are swept)."""
        with self._lock:
            return len(self._keys)
