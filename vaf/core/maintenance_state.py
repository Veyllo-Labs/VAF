# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Process-wide snapshot of a machine-level maintenance job.

The generic counterpart to ``model_download_state``: one thread-safe singleton
any in-process thread can write and any surface (Web UI banner, TUI status
line, classic CLI) can read. The first consumer is the memory re-embed
migration; a later job (a DB backup, a store compaction) reuses the same lane
by setting a different ``kind`` instead of growing a sibling state object.

Why a pushable state exists at all: ``vaf/core/progress.py`` deliberately has
no process-wide progress sink and names the condition under which one is
earned - a consumer that cannot poll. The Web UI banner is that consumer: a
browser learns about a running migration only if the server pushes it.
Machine-level jobs have no session, so frames go out via the broadcast lane
(the model-download precedent), not via ``StatePublisher`` (which is
per-session by design and refuses session-less events).

Progress semantics: ``done``/``total`` are row counts - an honest total, known
up front. ``phase`` is code-authored (never model text), so it is safe on any
shared surface.
"""

from __future__ import annotations

import threading


class _MaintenanceState:
    """Thread-safe snapshot of the current maintenance job (at most one at a
    time; jobs serialize via their own cross-process file locks)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = False
        self._kind = ""
        self._done = 0
        self._total = 0
        self._phase = ""
        self._error = ""

    def update(self, *, kind: str, active: bool, done: int = 0, total: int = 0,
               phase: str = "", error: str = "") -> None:
        with self._lock:
            self._kind = kind or ""
            self._active = bool(active)
            self._done = int(done or 0)
            self._total = int(total or 0)
            self._phase = phase or ""
            self._error = error or ""

    def clear(self) -> None:
        with self._lock:
            self._active = False
            self._error = ""

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    def snapshot(self) -> dict:
        with self._lock:
            pct = round(self._done / self._total * 100, 1) if self._total else 0.0
            return {
                "active": self._active,
                "kind": self._kind,
                "done": self._done,
                "total": self._total,
                "pct": pct,
                "phase": self._phase,
                "error": self._error,
            }


# The single shared instance.
MAINTENANCE = _MaintenanceState()
