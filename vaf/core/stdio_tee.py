# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""OS-level stdout/stderr tee for the tray process.

A tray started in a terminal (or from the desktop entry) writes its output only
to that terminal, so `vaf top` has no file to follow and used to surface some
unrelated older log instead. This tee duplicates fd 1 and 2 through a pipe into
the service log file WHILE passing everything through to wherever they pointed
before. It is fd-level on purpose: child processes (the Next.js frontend, the
supervisors) inherit the redirected descriptors, so their output lands in the
file too - a Python-level sys.stdout wrapper would miss all of them.

When NOT to tee (`should_tee_stdio` encodes this):
- stdout already IS a regular file: some launcher (vaf start, vaf.sh) owns the
  logging; teeing would double every line into a second file.
- the journal owns the output: our own systemd unit says so explicitly
  (VAF_LOG_TO_JOURNAL=1), or stdout literally IS the journald stream
  (JOURNAL_STREAM matches fd 1 by device:inode - the documented systemd
  contract). INVOCATION_ID is deliberately NOT used: on systemd-managed
  desktops every terminal inherits it, which silently disabled the tee for
  exactly the terminal-started tray it exists for (live finding).
"""

import os
import sys
import threading
from pathlib import Path

_installed = False


def _stdout_is_journal_stream() -> bool:
    """systemd contract: JOURNAL_STREAM carries "dev:ino" of the fd it wired to
    the journal; it only means something when fd 1 still IS that object."""
    value = os.environ.get("JOURNAL_STREAM", "")
    if ":" not in value:
        return False
    try:
        dev_s, ino_s = value.split(":", 1)
        st = os.fstat(1)
        return st.st_dev == int(dev_s) and st.st_ino == int(ino_s)
    except Exception:
        return False


def should_tee_stdio() -> bool:
    """True when this process's output would otherwise reach no followable log."""
    if os.environ.get("VAF_LOG_TO_JOURNAL"):
        return False
    try:
        import stat
        if stat.S_ISREG(os.fstat(1).st_mode):
            return False
    except Exception:
        return False
    if _stdout_is_journal_stream():
        return False
    return True


def tee_stdio_to_file(path: Path) -> bool:
    """Install the tee. Returns True when installed; never raises.

    Best-effort by contract: a failure leaves the process exactly as it was,
    because breaking tray startup for logging convenience is the wrong trade.
    """
    global _installed
    if _installed:
        return True
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        import stat
        log_fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        passthrough_fd = os.dup(1)
        read_fd, write_fd = os.pipe()

        def _pump() -> None:
            try:
                while True:
                    chunk = os.read(read_fd, 65536)
                    if not chunk:
                        break
                    for fd in (passthrough_fd, log_fd):
                        try:
                            os.write(fd, chunk)
                        except OSError:
                            pass
            except Exception:
                pass

        # Order matters: the pump must be READING before anything is redirected
        # into the pipe. If the thread cannot start (pids.max, RLIMIT_NPROC), a
        # pipe with no reader would swallow fd 1 and 2 and block the tray and
        # every child forever once the 64K buffer fills - a silent hang with no
        # symptom. Starting first means a failure here leaves stdio untouched.
        try:
            threading.Thread(target=_pump, daemon=True, name="vaf-stdio-tee").start()
        except Exception:
            for fd in (log_fd, passthrough_fd, read_fd, write_fd):
                try:
                    os.close(fd)
                except Exception:
                    pass
            return False

        os.dup2(write_fd, 1)
        # An explicit `2>file` redirect belongs to the caller: replacing it would
        # silently empty their file. Only take fd 2 over when it is not one.
        try:
            keep_stderr = stat.S_ISREG(os.fstat(2).st_mode)
        except Exception:
            keep_stderr = False
        if not keep_stderr:
            os.dup2(write_fd, 2)
        os.close(write_fd)

        # stdout now feeds a pipe, which flips Python to block buffering; the
        # person watching the terminal must keep seeing lines as they happen.
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(line_buffering=True)
            except Exception:
                pass

        _installed = True
        return True
    except Exception:
        return False
