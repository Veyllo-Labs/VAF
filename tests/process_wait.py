# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Waiting for processes a test did not start itself to end.

A process whose parent died is reparented to PID 1 (or a subreaper), and in a container
whose PID 1 does not reap, it stays a zombie: its pid exists, so psutil.wait_procs keeps
it "alive" until the timeout, although it ran its last instruction long ago. Here a zombie
counts as ended, and so does a process that disappears while its status is read.
"""
import time

import psutil


def ended(proc: psutil.Process) -> bool:
    """Whether `proc` has stopped running: gone, reaped or a zombie."""
    try:
        return not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def still_running(procs, timeout: float = 10.0) -> list:
    """The processes of `procs` that have not ended within `timeout` seconds."""
    deadline = time.monotonic() + timeout
    left = [p for p in procs if not ended(p)]
    while left and time.monotonic() < deadline:
        time.sleep(0.05)
        left = [p for p in left if not ended(p)]
    return left
