# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A fake clock for tests that wait: sleeping records the wait and moves the clock, at once.

`sleep` moves the clock for whichever thread calls it. That is right where a test hands the
clock to the code under test itself (a gate built with `sleep=clock.sleep`), threads of the
test included.

`sleep_in_test_thread` is the one to install process-wide, as `time.sleep` or a module's sleep
seam. A patched `time.sleep` is the attribute on the shared `time` module, so every thread in
the process reaches it, including a background loop an earlier test left running. With plain
`sleep` such a loop stops sleeping and spins, and every turn adds its wait to this test's clock
and to `sleeps` (seen as a full-suite failure: two requests logged at 117840 s and 332100 s of
fake time where 0 s and 41 s were expected). Only the thread that built the clock moves it; any
other thread sleeps for real.
"""
import threading
import time

# Captured at import, during collection, before any test patches time.sleep.
_REAL_SLEEP = time.sleep


class FakeClock:
    def __init__(self, sleeps=None):
        self.t = 0.0
        # A caller may pass its own list, so a test can keep asserting on the list it holds.
        self.sleeps = [] if sleeps is None else sleeps
        self._lock = threading.Lock()
        self._owner = threading.get_ident()

    def monotonic(self):
        return self.t

    def sleep(self, s):
        with self._lock:
            self.sleeps.append(float(s))
            self.t += float(s)

    def sleep_in_test_thread(self, s):
        """Fake on the thread that built the clock, real on every other thread."""
        if threading.get_ident() == self._owner:
            self.sleep(s)
        else:
            _REAL_SLEEP(s)
