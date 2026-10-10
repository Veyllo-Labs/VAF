# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Run the suite in an environment that is deliberately less generous than this one.

WHY THIS EXISTS. Three CI failures in a row were environment differences, not
logic errors, and the local suite could not have caught any of them because it
only ever runs in ONE environment: this workstation, UTF-8 output, Linux, every
optional dependency installed and every model already downloaded.

  - A key-placement branch that only Windows takes.
  - Output a cp1252 console cannot encode. Our stdout is UTF-8, so the crash was
    unreachable here.
  - A voice test that silently needed a model file this machine had cached and a
    fresh runner did not, reporting "engine unavailable" where the test expected
    "too short".

Each cost a round trip through CI, and the Windows entry takes 27 minutes to
answer. This script makes the cheap two thirds of that variance reproducible
locally, in the time the suite already takes:

  NARROW OUTPUT   PYTHONIOENCODING=cp1252, so any print of a character outside
                  that code page fails here rather than on the Windows runner.
  NO EXTRAS       the optional, heavy, sometimes-network-fetching packages are
                  hidden from the import system, so a test that quietly depends
                  on one fails here rather than on the runner that lacks it.
  SCRATCH HOME    HOME points at a throwaway directory. Not a CI concern - a
                  safety one. The suite has twice written into the real user
                  store, once destroying a recovery key, and the isolation
                  fixtures do not cover every axis.
  WINDOWS HOME    "~" (and so Path.home()) resolves from USERPROFILE and
                  ignores HOME, as ntpath.expanduser does. A test that points
                  only HOME at a scratch directory still sees the real profile
                  on Windows; it failed on the Windows runner alone (the file
                  routes' ownership tests, 403 from the roots check) because
                  here HOME was the home. Set USERPROFILE too, or patch
                  Path.home, which every OS honours.
  LINKED HOME     the scratch home is reached through a symlink, so a resolved
                  path and the spelling HOME gives differ. On macOS that is the
                  normal case for a scratch home (/var is a link to /private/var)
                  and it failed only there: is_safe_path compared an unresolved
                  path with the resolved data dir and refused every skill file.
                  A home moved to another disk and linked back is the same case
                  on any OS. Where the host cannot create a link the real
                  directory is used and the axis says so.
  OLD MAIL PARSER email.utils as Python 3.10.11 and 3.11.9 ship it, the last
                  Windows and macOS installers of those series and what
                  setup-python runs there: getaddresses and parseaddr without
                  the `strict` keyword and with the lenient pre-CVE-2023-27043
                  parse. The nightly matrix failed on exactly those versions for
                  thirteen nights (a display name read as the sender) and then
                  again on a test fixture that passed `strict=False`; every
                  local run had a patched stdlib and stayed green.

WHAT IT CANNOT DO. Real Windows file semantics - ACLs, MoveFileEx sharing
violations, the read-only-flag-only chmod - need a real Windows machine. Those
are covered by seams and simulated branches in tests/test_at_rest_cross_platform.py
instead, and the honest boundary is written down there rather than pretended
away here. Windows-only SERIALIZATION defects (str(PurePath) renders with the
host's separator, invisible on Linux where it equals as_posix) are covered by a
static guard, tests/test_windows_path_hygiene.py, which fails on any OS. Nor can
it put the CHECKOUT INSIDE THE HOME, where the Linux and macOS runners have it
(/home/runner/work/...): the source tree is located by its resolved path, so
only a second checkout inside the scratch home would reproduce that, and the
suite needs files no fresh checkout has. A test that compares the real home with
the source tree passed here and failed there (the home's parent also overlapped
the source tree, and that refusal answered first); such a test gives itself a home
of its own (HOME and USERPROFILE under tmp_path), as test_refuses_a_folder_above_home
does. Measured when this was written: no other test compares the two. The
standing rule: every Windows CI red that the local gates could not have caught
adds its class to one of these three places in the SAME fix - a hostile axis
here, a simulated branch there, or a static guard.

Usage:  venv/bin/python scripts/hostile_env.py [pytest args...]
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Installed on a developer machine, absent or broken on a fresh runner. Hiding
# them turns "works here" into "works anywhere" for the tests that touch them.
OPTIONAL_PACKAGES = (
    "sherpa_onnx",       # speaker id: also downloads a model on first use
    "faster_whisper",    # speech to text
    "playwright",        # browser automation
    "transformers",      # speech extra: the Smart Turn judge's feature extractor (fail-open)
    "pytesseract",
    # requirements marker python_version >= "3.11": absent on the 3.10 CI
    # runner, present on every dev machine - the exact gap that shipped an
    # unguarded import (test_browser_agent_vision_lane, the controller test).
    "browser_use",
)

_BLOCKER = '''
import sys


class _HideOptional:
    """Make a chosen set of imports fail the way a fresh runner fails them."""

    HIDDEN = {names}

    def find_module(self, name, path=None):
        return self if name.split(".")[0] in self.HIDDEN else None

    def load_module(self, name):
        raise ImportError(f"{{name}} is not installed in this environment")

    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in self.HIDDEN:
            raise ImportError(f"{{name}} is not installed in this environment")
        return None


sys.meta_path.insert(0, _HideOptional())


import os as _os
import posixpath as _posixpath

_posix_expanduser = _posixpath.expanduser


def _windows_expanduser(path):
    """'~' from USERPROFILE, never from HOME - ntpath.expanduser's rule on Windows."""
    p = _os.fspath(path)
    if not isinstance(p, str) or not p.startswith("~"):
        return _posix_expanduser(path)
    end = p.find("/", 1)
    end = len(p) if end == -1 else end
    if end != 1:                        # ~user: not this axis's question
        return _posix_expanduser(path)
    profile = _os.environ.get("USERPROFILE")
    return (profile + p[1:]) if profile else p


_posixpath.expanduser = _windows_expanduser


import email.utils as _email_utils

_patched_getaddresses = _email_utils.getaddresses
_patched_parseaddr = _email_utils.parseaddr


def _old_getaddresses(fieldvalues):
    """The unpatched signature and parse: no strict keyword, the legacy result."""
    return _patched_getaddresses(fieldvalues, strict=False)


def _old_parseaddr(addr):
    return _patched_parseaddr(addr, strict=False)


if getattr(_email_utils, "supports_strict_parsing", False):
    _email_utils.getaddresses = _old_getaddresses
    _email_utils.parseaddr = _old_parseaddr
    del _email_utils.supports_strict_parsing
'''


def main() -> int:
    args = sys.argv[1:] or ["tests/", "--ignore=tests/test_gpu_inference.py", "-q"]

    with tempfile.TemporaryDirectory(prefix="vaf-hostile-") as tmp:
        real_home = Path(tmp) / "home-real"
        (real_home / ".vaf").mkdir(parents=True)
        home = Path(tmp) / "home"
        try:
            home.symlink_to(real_home, target_is_directory=True)
            linked = True
        except OSError:
            home, linked = real_home, False
        site = Path(tmp) / "site"
        site.mkdir()
        (site / "sitecustomize.py").write_text(
            _BLOCKER.format(names=repr(set(OPTIONAL_PACKAGES))), encoding="utf-8")

        env = dict(
            os.environ,
            HOME=str(home),
            USERPROFILE=str(home),
            # Tells the two tests that MEASURE this platform's home semantics that the
            # windows-home axis has replaced them on purpose.
            VAF_SIMULATED_WINDOWS_HOME="1",
            PYTHONIOENCODING="cp1252",
            PYTHONPATH=os.pathsep.join([str(site), str(ROOT)]),
        )

        print("Running the suite with:")
        print(f"  narrow output   PYTHONIOENCODING=cp1252")
        print(f"  no extras       {', '.join(OPTIONAL_PACKAGES)}")
        print(f"  scratch home    {home}")
        print(f"  windows home    '~' reads USERPROFILE, not HOME")
        print(f"  linked home     " + (f"{home} -> {real_home}" if linked
                                     else "not available here (no symlinks)"))
        print(f"  old mail parser email.utils without strict parsing (3.10.11, 3.11.9)")
        print()
        return subprocess.run([sys.executable, "-m", "pytest", *args],
                              cwd=str(ROOT), env=env).returncode


if __name__ == "__main__":
    raise SystemExit(main())
