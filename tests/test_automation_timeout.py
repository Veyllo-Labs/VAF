# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Prompt-automation timeout semantics (live 2026-07-13, twice: 09:05 and 15:20).

run_bounded ABANDONS the worker on timeout; the old code ignored the sentinel
return entirely, declared the half stream the "result", wrapped it into a junk
output file and pushed it - while the abandoned worker finished 77-140s later
and delivered again (double message, double attachment). The runner now
evaluates the sentinel, waits a bounded grace for the worker (both live cases
would have recovered), and only past the grace delivers one honest timeout
note: no partial result, no file wrap.
"""
import threading
import time
from pathlib import Path

import vaf.core.automation as auto_mod
from vaf.core.automation import _wait_for_abandoned_run
from vaf.core.config import Config


# ── grace-wait helper ─────────────────────────────────────────────────────────

def test_already_done_returns_immediately():
    t0 = time.monotonic()
    assert _wait_for_abandoned_run({"done": True}, grace_seconds=30, poll=5) is True
    assert time.monotonic() - t0 < 1.0


def test_late_finish_within_grace_is_recovered():
    flag = {"done": False}
    threading.Timer(0.3, lambda: flag.__setitem__("done", True)).start()
    assert _wait_for_abandoned_run(flag, grace_seconds=5, poll=0.1) is True


def test_never_finishing_run_expires():
    assert _wait_for_abandoned_run({"done": False}, grace_seconds=0.4, poll=0.1) is False


# ── configuration ─────────────────────────────────────────────────────────────

def test_default_timeout_is_realistic():
    # 180s could not fit a real automation (mails + searches + coder + delivery)
    # and produced the incidents; the default is now 600.
    assert Config.DEFAULTS.get("automation_run_timeout_seconds") == 600


# ── wiring guards (source scans) ──────────────────────────────────────────────

def _src():
    return Path(auto_mod.__file__).read_text(encoding="utf-8")


def test_bounded_return_is_evaluated():
    src = _src()
    assert "_bounded_ret = _run_bounded(" in src, (
        "the run_bounded sentinel return is ignored again - a timed-out run is "
        "then indistinguishable from a finished one"
    )
    assert "_bounded_ret.startswith(_TO_PREFIX)" in src
    assert "_wait_for_abandoned_run(_chat_done)" in src


def test_unresolved_timeout_yields_honest_note_not_partial():
    src = _src()
    assert "Zeitlimit überschritten" in src
    assert 'result = (\n                        f"Error: Zeitlimit' in src, (
        "the timeout note must start with 'Error:' so the push carries status=error"
    )


def test_unresolved_timeout_skips_the_legacy_file_wrap():
    src = _src()
    assert "if task.output_path and not prompt_timeout_unresolved:" in src, (
        "the legacy output save regained the junk-wrap path on timeouts"
    )
