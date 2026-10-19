# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""One job at a time that moves or reads the service containers, in this process.

A repair starts and restarts containers, a switch of the inference profile stops or starts
the speech containers, a backup dumps the database from its container (starting it when it
is stopped, stopping it again afterwards), and an update stops this whole process. Two of them at once fight over the same
containers: a backup that began while the database was stopped stops it again after a repair
has started it, and a profile switch in the middle of a dump fails the backup.

The web jobs used to keep this to themselves (a lock and three state dicts in
vaf/api/system_routes.py), so the backup schedule (vaf/core/backup_schedule.py), which runs
in the same process, could not see a repair and a repair could not see it. The claim lives
here now, in the framework, and both ask it.

NAMED BOUNDARY: per process. `vaf repair` or `vaf profile` in a terminal is another process
and is not seen here; a backup still cannot overlap another backup there, since the backup
folder has a file lock of its own (`create_backup`).
"""
from __future__ import annotations

import threading
from typing import Optional

REPAIR = "repair"
PROFILE_SWITCH = "profile switch"
BACKUP = "backup"
# An update started from the web stops this process: claimed before the updater is spawned
# and never given back once it is, so nothing that touches the containers starts meanwhile.
UPDATE = "update"

_lock = threading.Lock()
_running: Optional[str] = None


def claim(kind: str) -> Optional[str]:
    """Take the slot for ``kind``. None when taken; otherwise the kind already running, and
    nothing changed."""
    global _running
    with _lock:
        if _running is not None:
            return _running
        _running = kind
        return None


def release(kind: str) -> None:
    """Give the slot back; only the kind holding it can."""
    global _running
    with _lock:
        if _running == kind:
            _running = None


def running() -> Optional[str]:
    """The kind of job running now, or None."""
    return _running


def busy_message(kind: str, other: str) -> str:
    """The refusal for starting ``kind`` while ``other`` runs, one wording for every caller."""
    if kind == other:
        return f"{_a(kind)} is already running"
    return f"{_a(other)} is running; wait for it to finish first."


def _a(kind: str) -> str:
    return ("An " if kind[:1] in "aeiou" else "A ") + kind
