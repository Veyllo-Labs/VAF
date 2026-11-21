# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Create the fresh venv a CI lane installs requirements.lock into, and check that a Python is one.

A CI runner's Python is not empty: the image installs tools of its own into it (measured:
pipx, argcomplete and userpath in the windows-latest Python 3.12 that actions/setup-python
serves). Installing the lock into that interpreter tests the suite on the lock plus whatever
the image ships, overwrites packages those tools need, and makes
``check_requirements_lock.py --drift`` report the image's tools as drift. A venv without the
system site packages sees none of them, whatever the image ships, so no lane needs an edit
when GitHub changes its runner image. scripts/ci_check.sh builds its check venv the same way,
and the installers give a user one too.

    python scripts/ci_venv.py DIR     create a venv at DIR (pip included, nothing else), print
                                      its scripts directory and, when GITHUB_PATH is set,
                                      append that directory to it, so every later step of the
                                      job runs the venv's python, pip and console scripts
    python scripts/ci_venv.py --check exit 0 when the running Python is an isolated venv,
                                      else print why not and exit 1

DIR must be missing or empty: the script never clears a directory, so a wrong path cannot
wipe anything. Exit 1 on a refusal or a failed build, 2 on wrong usage.

Standard library only: it runs before anything is installed.
"""
from __future__ import annotations

import os
import site
import sys
import sysconfig
import venv

_USAGE = ("usage: ci_venv.py DIR\n"
          "       ci_venv.py --check")


def _norm(path: str) -> str:
    """One spelling per directory, so a trailing separator, a '..' segment or (on Windows)
    the case of a letter cannot hide a match."""
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def isolation_problem(prefix: str, base_prefix: str, sys_path, base_site_dirs, user_site_dir) -> str | None:
    """None when an interpreter with these values sees only its own site-packages, else one
    sentence saying why it does not. ``user_site_dir`` is the user site directory whether or
    not site.py enabled it: a PYTHONPATH entry can put it on sys.path all the same."""
    if _norm(prefix) == _norm(base_prefix):
        return (f"The interpreter at {prefix} is not a venv: its own site-packages count, and a "
                f"CI runner's Python ships packages the lock does not name.")
    on_path = {_norm(p) for p in sys_path if p}
    for base_site in base_site_dirs:
        if base_site and _norm(base_site) in on_path:
            return (f"The base interpreter's {base_site} is on sys.path: the venv was created "
                    f"with the system site packages.")
    if user_site_dir and _norm(user_site_dir) in on_path:
        return f"The user site {user_site_dir} is on sys.path: packages installed with --user count."
    return None


def _base_site_dirs(scheme: str | None = None, base: str | None = None, platbase: str | None = None) -> list:
    """The purelib and platlib directories of the interpreter this one was made from: the
    install scheme's layout filled in with the base prefixes. Taken from sysconfig, because
    site.getsitepackages() run inside a venv answers with the venv's own directories, never
    the base interpreter's. ``scheme`` and the prefixes default to the running interpreter's
    and are parameters so another platform's layout can be computed anywhere."""
    paths = sysconfig.get_paths(scheme or sysconfig.get_default_scheme(), vars={
        "base": base or sys.base_prefix, "platbase": platbase or base or sys.base_exec_prefix})
    return sorted({paths["purelib"], paths["platlib"]})


def current_isolation_problem() -> str | None:
    """isolation_problem() for the running interpreter."""
    # Asked for even when site.py left the user site out (ENABLE_USER_SITE is False in every
    # venv without the system site packages): only automatic insertion is switched off then.
    user_site = site.getusersitepackages()
    return isolation_problem(sys.prefix, sys.base_prefix, sys.path, _base_site_dirs(), user_site)


def scripts_dir(env_dir: str) -> str:
    """The absolute, native path of a venv's scripts directory (python, pip, console scripts)."""
    return os.path.join(os.path.abspath(env_dir), "Scripts" if sys.platform == "win32" else "bin")


def create(env_dir: str) -> int:
    if os.path.lexists(env_dir) and (not os.path.isdir(env_dir) or os.listdir(env_dir)):
        print(f"ERROR: {env_dir} exists and is not an empty directory; refusing to create a venv "
              f"there (nothing is cleared).", file=sys.stderr)
        return 1
    # The defaults of `python -m venv`: pip through ensurepip, no system site packages,
    # symlinks except on Windows.
    venv.EnvBuilder(with_pip=True, symlinks=os.name != "nt").create(env_dir)
    bin_dir = scripts_dir(env_dir)
    exe = "python.exe" if sys.platform == "win32" else "python"
    if not os.path.isfile(os.path.join(bin_dir, exe)):
        print(f"ERROR: no {exe} in {bin_dir} after creating the venv.", file=sys.stderr)
        return 1
    print(bin_dir)
    github_path = os.environ.get("GITHUB_PATH")
    if github_path:
        # One line per directory; the runner prepends it to PATH from the next step on.
        with open(github_path, "a", encoding="utf-8", newline="") as fh:
            fh.write(bin_dir + "\n")
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--check"]:
        problem = current_isolation_problem()
        if problem:
            print(problem)
            return 1
        return 0
    if len(argv) != 1 or argv[0].startswith("-"):
        print(_USAGE, file=sys.stderr)
        return 2
    return create(argv[0])


if __name__ == "__main__":
    sys.exit(main())
