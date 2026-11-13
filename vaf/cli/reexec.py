# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Replace this process with a fresh start of the program that is running.

The `restart` command of both terminal lanes (the classic lane in vaf/cli/cmd/run.py and the
full-screen app in vaf/cli/tui_app/app.py) ends here, so the two cannot drift apart.

How the program was started decides how it is started again:

- A launcher that keeps its `.exe` suffix in argv[0] is exec'd directly.
- A `python -m <module>` start is re-exec'd as `python -m <module>`. The module name comes
  from `__main__.__spec__`: `vaf.main` for the launchers run_vaf.sh and run_vaf.bat, or
  `mypkg` for a wrapper package whose `__main__` module calls VAF.
  Running that module's file as a script instead would lose its package, so a relative
  import in it fails after the exec, where nothing can report it any more. Script mode also
  puts the module's own directory first on sys.path, where `vaf/github` shadows the
  `github` package (PyGithub) and the GitHub tools load without it. A module found only
  through the working directory cannot be started again if the process changed directory
  before the restart; VAF's launchers export PYTHONPATH and the installers run
  `pip install -e .`, so `vaf.main` does not depend on the working directory.
- Everything else runs argv[0] again as a script with the same interpreter: the console
  script, a script file, a directory or zip archive with a `__main__` module (its spec name
  is the bare `__main__`, which `-m` cannot start).

The package `vaf` itself has no `__main__` module, so `python -m vaf` is never produced.
Interpreter options of the first start (`-X`, `-u`) are not repeated, as before.
"""
from __future__ import annotations

import os
import sys

_MAIN_SUFFIX = ".__main__"


def _started_module() -> str | None:
    """The module a `python -m` start ran, or None for any other start."""
    spec = getattr(sys.modules.get("__main__"), "__spec__", None)
    name = getattr(spec, "name", None) or ""
    if name.endswith(_MAIN_SUFFIX):
        # `python -m mypkg` runs `mypkg.__main__`; `-m mypkg` is what starts it again.
        name = name[: -len(_MAIN_SUFFIX)]
    if not name or name == "__main__":
        return None
    return name


def restart_argv() -> list[str]:
    """The command that starts this program again; its first item is the file to exec."""
    argv = list(sys.argv)
    if argv and argv[0].endswith(".exe"):
        return argv
    module = _started_module()
    if module:
        return [sys.executable, "-m", module, *argv[1:]]
    return [sys.executable, *argv]


def reexec() -> None:
    """Replace this process with `restart_argv()`. Returns only by raising."""
    cmd = restart_argv()
    os.execl(cmd[0], *cmd)
