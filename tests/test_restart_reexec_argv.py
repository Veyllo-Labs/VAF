# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`restart` in both terminal lanes re-execs the program that was started, never `python -m vaf`.

The re-exec exists twice, in the classic lane (`_run_modern` in vaf/cli/cmd/run.py) and in
the full-screen app (`_exec_restart` in vaf/cli/tui_app/app.py). argv[0] is the script the
interpreter ran: the console script, `vaf/main.py` under `python -m vaf.main`, or a wrapper
package's own `__main__.py` when VAF runs inside another program's `python -m` entry point.
Running it again with the same interpreter restarts the same program. A `-m vaf` re-exec could
not start at all, because the package has no `__main__` module, and for a wrapper it would
start VAF's CLI instead of the wrapper. Both copies are driven with ONE argv table, so a drift
between them turns one of the two tests red. Nothing is exec'd: `os.execl` is recorded.
"""
from __future__ import annotations

import os
import sys

import pytest

from test_cli_web_lane_queue_tasks import modern_lane  # noqa: F401 - modern_lane is a fixture

# (argv[0], how it is re-exec'd). Synthetic paths only.
ARGV_TABLE = [
    ("/opt/app/mypkg/__main__.py", "script"),
    ("/opt/app/vaf/main.py", "script"),
    ("/opt/app/venv/bin/vaf", "script"),
    ("C:\\app\\Scripts\\vaf.exe", "exe"),
]


def _expected(argv0: str, kind: str) -> tuple:
    if kind == "exe":
        return (argv0, argv0, "run")
    return (sys.executable, sys.executable, argv0, "run")


def _assert_no_module_start(calls) -> None:
    for call in calls:
        assert "-m" not in call, call


@pytest.mark.parametrize("argv0,kind", ARGV_TABLE)
def test_classic_lane_restart_reexecs_the_started_program(modern_lane, monkeypatch, argv0, kind):  # noqa: F811
    monkeypatch.setattr(sys, "argv", [argv0, "run"])
    modern_lane.rec["script"] = ["restart", None]
    # The recorded execl raises; the restart's own `except` prints "Restart failed" and the
    # loop goes on, so the console check of the fixture does not apply here.
    rec = modern_lane.run(console_breaks=True)
    assert rec["execl"] == [_expected(argv0, kind)]
    _assert_no_module_start(rec["execl"])


class _Stop(Exception):
    """Raised by the recorded os.execl, as the real one never returns."""


@pytest.mark.parametrize("argv0,kind", ARGV_TABLE)
def test_app_exec_restart_reexecs_the_started_program(monkeypatch, argv0, kind):
    from vaf.cli.tui_app import app as app_mod
    from vaf.cli.ui import UI
    from vaf.core.frontend_manager import FrontendManager

    stops = []
    monkeypatch.setattr(FrontendManager, "stop_frontend",
                        lambda self, *a, **k: stops.append(k))
    calls = []

    def _execl(*a):
        calls.append(a)
        raise _Stop("execl")
    monkeypatch.setattr(os, "execl", _execl)
    errors = []
    monkeypatch.setattr(UI, "error", staticmethod(lambda msg, *a, **k: errors.append(str(msg))))
    monkeypatch.setattr(sys, "argv", [argv0, "run"])

    app_mod._exec_restart()

    assert stops == [{"wait_for_exit": True}]
    assert calls == [_expected(argv0, kind)]
    _assert_no_module_start(calls)
    assert errors and "Restart failed" in errors[0]
