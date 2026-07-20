# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""What a spawned VAF child does with its terminal window when it is finished.

The child owns the window, not the shell that started it. That is the only shape that works
on all three platforms:

- Linux used to append `; exec bash` to every emulator invocation
  (`Platform.open_new_terminal`), which replaces the finished process with an interactive
  shell, so the window could never close. The countdown below fired correctly, printed
  "[OK] Terminal closing." and exited - and the window stayed, showing a prompt (live
  incident 2026-07-20).
- A shell tail that holds only on a non-zero exit was considered and rejected: macOS
  `do script` runs the user's LOGIN shell, zsh since Catalina, where `read -n1 -p` is a
  bashism that errors out and closes the window instantly; and the Windows branch is
  `cmd /c` with no tail at all, so failures would vanish unread.
- The child is Python, so one implementation covers Windows, macOS and Linux identically.

The contract:

  success            -> short countdown, then exit (the window closes)
  failure            -> stay, so the error can actually be read
  no_auto_close=True -> stay, which is what `--no-auto-close` has always promised
  not a real terminal (piped WebUI child, redirected output) -> exit at once

The last case matters: in WebUI mode the countdown's output is forwarded into the browser
console as spam, and its late exit used to overlap the next workflow step.
"""
import os
import sys
import time

# Default grace period before a successful run closes its window.
AUTO_CLOSE_DELAY = 5


def _is_interactive_window() -> bool:
    """True only when a human is watching this process in a real terminal window.

    VAF_SPAWN_MODE is checked FIRST and wins: it is set per spawn by whoever opened this
    process and therefore knows what it opened, while VAF_WEBUI_ACTIVE only says that the
    PARENT serves a web UI and is inherited by everything it starts. Checking the ambient
    flag first would make an explicit "terminal" spawn from inside the desktop app unable to
    hold its own window open on failure.
    """
    mode = os.environ.get("VAF_SPAWN_MODE", "").strip().lower()
    if mode == "piped":
        return False
    if mode != "terminal":
        if os.environ.get("VAF_WEBUI_ACTIVE", "").strip().lower() in ("1", "true", "yes"):
            return False
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False


def finish_terminal(
    *,
    success: bool = True,
    no_auto_close: bool = False,
    delay: int = AUTO_CLOSE_DELAY,
    exit_code: int = None,
) -> None:
    """End the process, deciding what happens to its terminal window. Never returns.

    exit_code defaults to 0 on success and 1 otherwise. Callers that must exit 0 despite
    success=False (a paused workflow, where a non-zero exit would be read as a crash by the
    piped watcher) pass it explicitly.
    """
    code = exit_code if exit_code is not None else (0 if success else 1)

    if not _is_interactive_window():
        sys.exit(code)

    if not success or no_auto_close:
        # Hold the window. Without this the fix that lets a window close would make every
        # failure unreadable, which is worse than the bug being fixed.
        try:
            print()
            if not success:
                print("[X] This task did not finish successfully. The output above is the log.")
            print("Press Enter to close this window...")
            sys.stdin.readline()
        except (EOFError, KeyboardInterrupt, BrokenPipeError, OSError):
            pass
        sys.exit(code)

    try:
        print()
        for remaining in range(delay, 0, -1):
            sys.stdout.write(f"\r[*] Terminal closing in {remaining} seconds...  ")
            sys.stdout.flush()
            time.sleep(1)
        sys.stdout.write("\r[OK] Terminal closing.                           \n")
        sys.stdout.flush()
    except (BrokenPipeError, OSError, KeyboardInterrupt):
        pass
    sys.exit(code)
