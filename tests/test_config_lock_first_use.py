# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`Config._locked()` creates its one file lock once, even when its first two users arrive at
the same time.

THE DEFECT, found by the switch-off fuzz in test_whatsapp_own_switch.py (one draw in about
9000) and measured with a probe around `Config._locked`: two threads found `_filelock` still
None, each created a `FileLock`, and the later assignment replaced the first. The thread that
had locked through the first instance then entered `Config.save`, whose nested `_locked()`
reached the second instance, opened a new descriptor and waited on its own `flock` for the full
10 s timeout (hold 10.019 s, nested wait 10.011 s); the other thread waited 10.017 s. Both then
went on UNLOCKED, which is the lost update the lock exists to prevent: a switch-off written by
one thread could be undone by the other's stale save.

THE RULE: the lazy creation runs under a process lock, so every thread gets the same
`FileLock` and a nested `_locked()` in the holding thread is reentrant.
"""
import threading
import time

import filelock
import pytest

from vaf.core.config import Config


def test_two_first_users_share_one_lock_and_nest(tmp_path, monkeypatch):
    """MUTATION: create the lock without the init lock (the plain `if ... is None` check)."""
    monkeypatch.setattr(Config, "APP_DIR", tmp_path)
    monkeypatch.setattr(Config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(Config, "_filelock", None)
    real = filelock.FileLock
    made = []

    def slow_to_make(*args, **kwargs):
        # The second construction is slower, so the first thread has locked through the
        # first instance before the second assignment lands.
        time.sleep(0.15 * len(made))
        lock = real(*args, **kwargs)
        made.append(lock)
        return lock
    monkeypatch.setattr(filelock, "FileLock", slow_to_make)

    start = threading.Barrier(2)
    took = {}

    def user(name):
        start.wait()
        began = time.monotonic()
        with Config._locked():
            time.sleep(0.3)             # the other thread arrives meanwhile
            with Config._locked():      # what Config.save does inside a caller's lock
                pass
        took[name] = time.monotonic() - began

    threads = [threading.Thread(target=user, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads)
    assert len(made) == 1, f"{len(made)} file locks for one config file"
    assert max(took.values()) < 5.0, took


@pytest.mark.parametrize("nested", [1, 3])
def test_one_thread_nests_without_waiting(tmp_path, monkeypatch, nested):
    monkeypatch.setattr(Config, "APP_DIR", tmp_path)
    monkeypatch.setattr(Config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(Config, "_filelock", None)
    began = time.monotonic()

    def enter(depth):
        with Config._locked():
            if depth:
                enter(depth - 1)
    enter(nested)
    assert time.monotonic() - began < 2.0
