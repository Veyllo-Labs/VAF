# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Remember for a short while that a local service could not be reached.

A connect to a closed local port is refused at once on Linux and macOS, but on Windows
only after about two seconds of SYN retries. A hot path that asks an absent local service
again for every call therefore costs nothing here and seconds per call on Windows. The
cure is the same everywhere: after a failed connect, answer "unreachable" without dialing
for a fixed pause; the first success forgets it. A service that comes back is reached
again after the pause at the latest, and every caller gets exactly what an unreachable
service gives it in the meantime.

Users: the auth store (vaf/auth/permissions.py, the account directory and the permission
row) and the local API (vaf/core/web_interface.py, the push fallback; the terminal app's
heartbeat reports into the same instance).

Named boundary: not on the public facade (vaf/__init__.py). Both users are VAF's own
services on fixed local ports, and an embedder never dials either; no embedder-facing code
has been measured hand-rolling such a pause, and docs/EMBEDDING.md names no path that dials
VAF's web API or its account store (the one local service it does name, the model server
on port 8080, is started or reused by the backend before a call). Promote it when a facade
primitive (a tool, a channel, a provider) is shown to repeat the same local connect per call.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Dict, Hashable, Optional


class OutageMemory:
    """Per key (a URL, a database url), the monotonic time until which it counts as down."""

    def __init__(self, pause_s: float, clock: Optional[Callable[[], float]] = None) -> None:
        self.pause_s = float(pause_s)
        self._clock = clock
        self._until: Dict[Hashable, float] = {}
        self._lock = threading.Lock()

    def _now(self) -> float:
        return (self._clock or time.monotonic)()

    def is_down(self, key: Hashable) -> bool:
        """True while a failure of `key` is younger than the pause: do not dial."""
        with self._lock:
            return self._now() < self._until.get(key, 0.0)

    def failed(self, key: Hashable) -> None:
        """A connect to `key` failed: do not dial it again for the pause."""
        with self._lock:
            self._until[key] = self._now() + self.pause_s

    def reached(self, key: Hashable) -> None:
        """A connect to `key` succeeded: forget any remembered failure."""
        with self._lock:
            self._until.pop(key, None)

    def forget(self) -> None:
        with self._lock:
            self._until.clear()
