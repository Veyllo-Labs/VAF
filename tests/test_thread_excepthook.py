# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Uncaught background-thread exceptions land in crash_<date>.log.

CPython's default threading.excepthook prints to stderr and returns: the
thread dies, the process lives, and in a terminal lane the only record
scrolls away. The live incident this pins: a rich spinner refresh thread was
killed mid-tool-call (CPython bpo-15108) and the traceback existed nowhere on
disk afterwards - no log file, nothing in the UI. install_thread_excepthook
(vaf/core/log_helper.py) closes that class; vaf/main.py installs it at the
process funnel so every product lane is covered, and the facade exports it
for embedders.

Also pinned here: crash_<date>.log has ONE writer, append_crash_log. Two
lanes used to hand-roll byte-similar open/mkdir/format blocks; a third copy
must be a decision, not an accident.
"""
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

import pytest

from vaf.core.log_helper import append_crash_log, install_thread_excepthook

# The raising threads below are the point of the file; our hook chains to
# pytest's own catcher, which would surface each one as a warning.
pytestmark = pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnhandledThreadExceptionWarning")

_REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("VAF_LOG_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def restored_hook():
    """threading.excepthook is process-global state; a test that leaves the
    installed hook behind pollutes every later test in the run."""
    before = threading.excepthook
    yield
    threading.excepthook = before


def _crash_text(log_dir) -> str:
    path = log_dir / f"crash_{datetime.now().strftime('%Y-%m-%d')}.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _raise_in_thread(exc: BaseException, name: str) -> None:
    def _boom():
        raise exc
    t = threading.Thread(target=_boom, name=name, daemon=True)
    t.start()
    t.join(timeout=10)


# ── the hook itself ──────────────────────────────────────────────────────────

def test_an_uncaught_thread_exception_lands_in_the_crash_log(log_dir, restored_hook, capsys):
    install_thread_excepthook()
    _raise_in_thread(ValueError("probe-a1b2c3"), "probe-thread")

    text = _crash_text(log_dir)
    assert "uncaught in thread probe-thread" in text
    assert "ValueError: probe-a1b2c3" in text
    assert "Traceback (most recent call last)" in text


def test_the_previous_hook_still_runs_after_ours(log_dir, restored_hook):
    """Chaining, not replacing: stderr printing (the default hook) and any hook
    an embedder installed first must survive the install."""
    seen = []
    threading.excepthook = lambda args: seen.append(args.exc_type)
    install_thread_excepthook()
    _raise_in_thread(ValueError("probe-chain"), "probe-chain-thread")

    assert seen == [ValueError]
    assert "probe-chain" in _crash_text(log_dir)


def test_installing_twice_logs_once(log_dir, restored_hook, capsys):
    install_thread_excepthook()
    install_thread_excepthook()
    _raise_in_thread(ValueError("probe-idem"), "probe-idem-thread")

    assert _crash_text(log_dir).count("ValueError: probe-idem") == 1


def test_systemexit_is_not_logged(log_dir, restored_hook):
    """The default hook treats SystemExit as a routine thread exit; logging it
    would flood the crash file with non-crashes."""
    install_thread_excepthook()
    _raise_in_thread(SystemExit(0), "probe-exit-thread")

    assert "probe-exit-thread" not in _crash_text(log_dir)


def test_debug_logs_off_still_writes(log_dir, restored_hook, monkeypatch, capsys):
    """Crash evidence must not depend on the debug switch: the incident this
    file pins happened WITH debug logs on and still left nothing, and a crash
    record a settings dialog can turn off is not a record (docs/DEBUGGING.md
    lists crash_*.log among the deliberately always-on files)."""
    import vaf.core.log_helper as lh
    monkeypatch.setattr(lh, "is_debug_logging_enabled", lambda: False)
    install_thread_excepthook()
    _raise_in_thread(ValueError("probe-ungated"), "probe-ungated-thread")

    assert "probe-ungated" in _crash_text(log_dir)


def test_the_funnel_installs_the_hook():
    """vaf/main.py must install the hook at module level, on the same funnel
    argument as the resolver registration: every process lane the product
    starts passes through that module. A subprocess keeps the import (which
    runs bootstrap and typer wiring) out of this test process."""
    code = (
        "import os, threading\n"
        "os.environ['VAF_SKIP_DEP_CHECK'] = '1'\n"
        "import vaf.main\n"
        "assert getattr(threading.excepthook, '_vaf_crash_hook', False), 'funnel did not install the hook'\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=_REPO, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr


# ── one writer for the crash log ─────────────────────────────────────────────

def test_append_crash_log_returns_the_path_it_wrote(log_dir):
    path = append_crash_log("probe lane", "Traceback (most recent call last):\n  boom\n")
    assert path is not None and path.exists()
    text = path.read_text(encoding="utf-8")
    assert "(probe lane)" in text and "boom" in text


def test_no_hand_rolled_crash_log_writers_outside_log_helper():
    """`get_dated_log_path("crash", ...)` outside log_helper is the start of the
    third hand copy; the two that existed (the classic run loop and the app
    bridge) were converted to append_crash_log when the writer was built."""
    out = subprocess.run(
        ["git", "ls-files", "-z", "vaf/"],
        cwd=_REPO, capture_output=True, check=True,
    ).stdout.decode("utf-8", errors="ignore")
    hits = []
    for rel in out.split("\0"):
        if not rel.endswith(".py") or rel == "vaf/core/log_helper.py":
            continue
        p = _REPO / rel
        if not p.is_file():
            continue
        content = p.read_bytes().decode("utf-8", errors="ignore")
        if 'get_dated_log_path("crash"' in content or "get_dated_log_path('crash'" in content:
            hits.append(rel)
    assert hits == [], f"hand-rolled crash-log writer(s); use append_crash_log: {hits}"
