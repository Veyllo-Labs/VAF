# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""No GC heap walks in vaf/: gc.get_objects()/gc.get_referrers() are banned.

Both build a result list holding a STRONG reference to every tracked object,
including a container another thread has allocated but not finished building.
tuple(<genexp>) allocates its result GC-tracked and then shrinks it via
_PyTuple_Resize, which demands refcount exactly 1 - one extra reference in
that window and the BUILDING thread dies with "SystemError: bad argument to
internal function" (CPython bpo-15108). Any thread building a tuple is a
potential victim, anywhere in the process.

The live incident: the memory profiler's 30-second object census on a daemon
thread killed rich's spinner refresh thread mid-tool-call; the agent's turn
survived, the user saw a frozen spinner and no error, and the traceback
existed nowhere on disk. The census was deleted rather than gated (RSS plus
the growth warning are the log line's documented job and need no heap walk).

The count is pinned at zero. A site that truly needs a heap walk goes into
the allowlist below with its reason, and must only run while every other
thread is parked - which a live VAF process can never guarantee.
"""
import re
import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent

_HEAP_WALK = re.compile(r"gc\.get_objects\(|gc\.get_referrers\(")

# path -> reason the walk is safe there.
_ALLOWED: dict = {}


def _tracked_python_files():
    out = subprocess.run(
        ["git", "ls-files", "-z", "vaf/", "scripts/"],
        cwd=_REPO, capture_output=True, check=True,
    ).stdout.decode("utf-8", errors="ignore")
    for rel in out.split("\0"):
        if rel.endswith(".py") and (_REPO / rel).is_file():
            yield rel, _REPO / rel


def test_no_gc_heap_walks_outside_the_allowlist():
    hits = []
    for rel, path in _tracked_python_files():
        if rel in _ALLOWED:
            continue
        content = path.read_bytes().decode("utf-8", errors="ignore")
        for i, line in enumerate(content.splitlines(), start=1):
            if _HEAP_WALK.search(line) and not line.lstrip().startswith("#"):
                hits.append(f"{rel}:{i}: {line.strip()}")
    assert hits == [], (
        "gc heap walk in a live process (see module docstring; kills concurrent "
        "tuple builders, CPython bpo-15108):\n" + "\n".join(hits)
    )
