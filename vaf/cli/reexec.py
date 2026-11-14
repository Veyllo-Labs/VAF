# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Replace this process with a fresh start of the program that is running.

The `restart` command of both terminal lanes (the classic lane in vaf/cli/cmd/run.py and the
full-screen app in vaf/cli/tui_app/app.py) ends here, so the two cannot drift apart.

The command started again is `sys.orig_argv`, the interpreter's own command line, with this
interpreter in front: `[sys.executable, *sys.orig_argv[1:]]`. Every start form therefore comes
back exactly as it was started:

- `python -m vaf.main` (the launchers run_vaf.sh and run_vaf.bat) and a wrapper package's
  `python -m mypkg` stay module starts. Running the module's file as a script instead would
  put its own directory first on sys.path, where `vaf/github` shadows the `github` package
  (PyGithub), and a relative import in a wrapper's `__main__` module would fail after the
  exec, where nothing can report it any more.
- Interpreter options (`-X utf8`, `-u`) are repeated, and `-c` keeps its code: `sys.argv`
  holds neither, it starts with `-c` in place of the code.
- The console script: pip's wrapper strips `.exe` from `sys.argv[0]`, so on Windows argv[0]
  names `Scripts\\vaf`, a file that does not exist. The launcher hands Python the `.exe`
  itself as the script, a zip archive with a `__main__` module that Python runs again.
- A program that rewrites `sys.argv` before it calls VAF is started again from its own
  start, not from the name it put into argv[0].

The executable is `sys.executable` and never `orig_argv[0]`, which is the name as typed: a
bare `python` under a PATH lookup is no path to exec.

On Windows, `os.execl` ends in the C runtime's `_wexecv`, which joins the arguments with
single spaces and quotes nothing, so a folder or an argument with a space would be split.
Each argument is quoted with `subprocess.list2cmdline`, the quoting `subprocess` itself uses
there; the path of the program is passed as it is.

Named boundaries:

- An interpreter that does not report its start (an embedded Python with an empty
  `sys.orig_argv` or `sys.executable`) is refused with an error instead of a guess.
- VAF's frozen build runs only the tray (vaf/main.py) and never reaches this.
- A relative script path or a module found through the working directory is found again
  only from the same directory, as at the first start; the terminal lanes never change
  directory, and VAF's launchers export PYTHONPATH.
- On Windows an exec starts the new process and then ends the old one, so the shell that
  waited for the old process gets its prompt back while the new one shares the console.
  What that does to the console has not been measured; the command that starts is the one
  above either way.

The package `vaf` itself has no `__main__` module and nothing types `python -m vaf`, so the
restart never produces it.
"""
from __future__ import annotations

import os
import subprocess
import sys

# A module constant, not a call at exec time, so tests can exercise either platform's
# argument handling on any host.
_WINDOWS = os.name == "nt"


def restart_argv() -> list[str]:
    """The command that starts this program again; its first item is the file to exec."""
    if not sys.executable or not sys.orig_argv:
        raise RuntimeError(
            "this Python does not report how it was started, so VAF cannot start itself again")
    return [sys.executable, *sys.orig_argv[1:]]


def reexec() -> None:
    """Replace this process with `restart_argv()`. Returns only by raising."""
    cmd = restart_argv()
    args = [subprocess.list2cmdline([a]) for a in cmd] if _WINDOWS else cmd
    os.execl(cmd[0], *args)
