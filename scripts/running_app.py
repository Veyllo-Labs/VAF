# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Is a Next server or the VAF tray running? The web-build stage of
scripts/ci_check.sh asks this before it rebuilds `.next`, because a build under a
live app corrupts the pages it is serving.

Exit 0: nothing found. Exit 1: something found; every match is written to stderr
as `PID <pid> (<kind>): <command>`. Exit 2: the process table could not be read.
Exit 3: the check itself failed (its traceback goes to stderr), so a crash is
never mistaken for a match. The gate refuses on every code but 0 rather than
guess.

The match is on the process TITLE and on exact argv ELEMENTS, never on the joined
command line. The gate used to run `pgrep -f "next-server|vaf.main tray"`, which
matched any shell whose `-c` payload merely carried those words: a shell that
waited for a server, or grepped for one, failed a full CI run at its web build
with no server alive, and the refusal did not say which process it had seen.

What a real Next server looks like: `next/dist/server/lib/start-server.js` sets
`process.title = "next-server (v<version>)"`, in the `next start` process itself
and in the child `next dev` forks. libuv writes the title over the original argv
area, so argv[0] becomes that title (cut to the old argv span when the launch
line was short, `next-server` then), and on Linux the kernel's comm, which psutil
reports as the name, takes its first 15 bytes: `next-server (v1`. That is why
`pgrep -x next-server` never matches a real server, and why the rule here is "the
first whitespace-separated word of the name, or of argv[0], is exactly
`next-server`". A Next release that drops or renames that title would let the
gate pass with a server up; check start-server.js on a Next bump.

The tray is matched by `vaf.core.instance.is_tray_entry`, the framework's own
matcher (`... -m vaf.main tray` or `.../vaf tray`, on exact argv elements), so
there is one definition of "the tray" in the tree.

Named boundary: the gate runs on Linux. On macOS psutil's name is the kernel's
p_comm, which the title may not change, so detection there rests on argv[0] alone
and has not been measured. ci_check.sh is bash and never runs on Windows.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path
from typing import Iterable, NamedTuple

# The tree this script lives in, ahead of an installed vaf and of any other
# checkout on PYTHONPATH: the gate checks a worktree with that worktree's own
# matcher. An entry already on the path is moved to the front, not left behind
# whatever precedes it.
_ROOT = str(Path(__file__).resolve().parent.parent)


def _is_root(entry) -> bool:
    try:
        return str(Path(entry).resolve()) == _ROOT
    except Exception:
        return False


sys.path[:] = [entry for entry in sys.path if not _is_root(entry)]
sys.path.insert(0, _ROOT)

NEXT_TITLE = "next-server"
_COMMAND_LIMIT = 300


class Match(NamedTuple):
    pid: int
    kind: str  # "next-server" or "tray"
    argv: tuple


def _first_word(text) -> str:
    if not isinstance(text, str):
        return ""
    words = text.split()
    return words[0] if words else ""


def is_next_server(name, argv) -> bool:
    """True when the first word of the process name, or of argv[0], is exactly
    `next-server`. The rest of argv is never looked at."""
    if _first_word(name) == NEXT_TITLE:
        return True
    try:
        first = argv[0] if argv else None
    except Exception:
        return False
    return _first_word(first) == NEXT_TITLE


def _process_table() -> Iterable[tuple]:
    """(pid, name, argv) for every process psutil can see. A process that
    vanishes or refuses while it is read is skipped; anything else raises."""
    import psutil

    gone = (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess)
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            info = proc.info
            yield info.get("pid"), info.get("name"), info.get("cmdline")
        except gone:
            continue


def find_running_app(processes: Iterable[tuple] | None = None) -> list[Match]:
    """Every Next server and VAF tray in `processes` (or the live process
    table), this process excluded. A process matching both is reported once,
    as a Next server."""
    from vaf.core import instance

    me = os.getpid()
    found: list[Match] = []
    for pid, name, argv in (_process_table() if processes is None else processes):
        if pid == me:
            continue
        argv = tuple(argv or ())
        if is_next_server(name, argv):
            # An unreadable argv still names the process in the refusal.
            found.append(Match(pid, "next-server", argv or (str(name),)))
        elif instance.is_tray_entry(list(argv)):
            found.append(Match(pid, "tray", argv))
    return found


def _command(match: Match) -> str:
    # A retitled Next pads its argv area with NULs, which psutil returns as
    # empty elements.
    text = " ".join(str(a) for a in match.argv if str(a))
    return (text or "?")[:_COMMAND_LIMIT]


def main() -> int:
    try:
        import psutil  # noqa: F401
    except Exception as exc:
        print(f"  cannot read the process table: psutil is unavailable ({exc})", file=sys.stderr)
        return 2
    try:
        table = list(_process_table())
    except Exception as exc:
        print(f"  cannot read the process table: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    try:
        found = find_running_app(table)
        lines = [f"  PID {match.pid} ({match.kind}): {_command(match)}" for match in found]
    except Exception:
        print("  the running-app check failed:", file=sys.stderr)
        print(traceback.format_exc().rstrip(), file=sys.stderr)
        return 3
    for line in lines:
        print(line, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
