# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Guards for the vaf-browser container entrypoint (docker/browser/entrypoint.sh).

Two regressions must never silently return:

1. Debian's chromium 150.0.7871.46 SIGTRAPs ~1s into startup when --no-first-run is set and the
   profile resolves to an EEA region (the container reports TZ=Europe/Berlin) - a real M149->M150
   search-engine-choice regression, Debian bug #1141618. The fix, verified empirically by bisecting
   the launch flags on the box, is to launch WITHOUT --no-first-run; the first-run search-engine
   choice is instead kept quiet with --disable-search-engine-choice-screen +
   --search-engine-choice-country=US. Reintroducing --no-first-run bricks the browser tool on every
   box that has built its image against Chromium 150+.

2. The entrypoint used to launch Chromium once and then `exec socat`, so any single Chromium death
   left socat forwarding forever to a dead port and the browser service was permanently down until a
   manual recreate. It must supervise Chromium (relaunch loop), reap orphaned child processes so a
   crash-loop cannot pile up zombies, and run socat only while the CDP endpoint is live.

Pure text assertions on the shell script - no Docker, no containers.
"""
from pathlib import Path

ENTRYPOINT = Path(__file__).resolve().parent.parent / "docker" / "browser" / "entrypoint.sh"


def _script() -> str:
    return ENTRYPOINT.read_text(encoding="utf-8")


def test_no_first_run_absent_and_choice_flags_present():
    src = _script()
    # ignore comment lines: the fix is explained in comments that name the flag on purpose
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    # --no-first-run is the confirmed M150 startup-SIGTRAP trigger; it must NOT be passed
    assert "--no-first-run" not in code
    # the first-run search-engine choice is kept quiet without crashing instead
    assert "--disable-search-engine-choice-screen" in code
    assert "--search-engine-choice-country=US" in code


def test_chromium_is_supervised_not_launch_once():
    src = _script()
    # the old launch-once tail permanently bricked the service after any single Chromium crash
    assert "exec socat" not in src
    # a supervise/relaunch loop with socat backgrounded (a child of the supervisor, not exec'd)
    assert "while :" in src or "while true" in src
    assert "socat TCP-LISTEN:9222" in src
    assert "start_chromium" in src
    assert "wait_for_cdp" in src
    # orphaned child processes from a prior crash are reaped so a crash-loop cannot pile up zombies
    assert "pkill" in src


def test_clean_shutdown_trap_present():
    # the supervisor is PID 1 now (socat is no longer exec'd), so it must trap SIGTERM or every
    # `docker stop` hangs for the full grace period before SIGKILL
    src = _script()
    assert "trap cleanup TERM INT" in src
