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


class _CreationGate:
    """Stands in for `Config._filelock_init` and notes that a second thread has passed the
    first `_filelock is None` check and now waits to create the lock."""

    def __init__(self, second_arrived):
        self.lock = threading.Lock()
        self.count = 0
        self.count_lock = threading.Lock()
        self.second_arrived = second_arrived

    def __enter__(self):
        with self.count_lock:
            self.count += 1
            if self.count >= 2:
                self.second_arrived.set()
        self.lock.acquire()
        return self

    def __exit__(self, *exc):
        self.lock.release()


def test_two_first_users_share_one_lock_and_nest(tmp_path, monkeypatch):
    """The first construction holds until the second thread has passed its own
    `_filelock is None` check: either it reaches the constructor as well (two locks), or it
    waits at the creation lock (one lock). No sleep decides the order. MUTATION: create the
    lock without the creation lock (the plain `if ... is None` check)."""
    monkeypatch.setattr(Config, "APP_DIR", tmp_path)
    monkeypatch.setattr(Config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(Config, "_filelock", None)
    second_arrived = threading.Event()
    monkeypatch.setattr(Config, "_filelock_init", _CreationGate(second_arrived), raising=False)
    real = filelock.FileLock
    made = []

    def hold_the_first(*args, **kwargs):
        made.append(threading.current_thread().name)
        if len(made) == 1:
            assert second_arrived.wait(timeout=10), "the second thread never passed its check"
        else:
            second_arrived.set()
        return real(*args, **kwargs)
    monkeypatch.setattr(filelock, "FileLock", hold_the_first)

    start = threading.Barrier(2)
    took, errors = {}, []

    def user(name):
        try:
            start.wait()
            began = time.monotonic()
            with Config._locked():
                with Config._locked():      # what Config.save does inside a caller's lock
                    time.sleep(0.05)
            took[name] = time.monotonic() - began
        except BaseException as e:  # noqa: BLE001 - the assertion below reports it
            errors.append(e)

    threads = [threading.Thread(target=user, args=(n,), daemon=True) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads) and not errors, errors
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
