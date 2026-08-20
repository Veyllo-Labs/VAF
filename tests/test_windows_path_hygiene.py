# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""No OS-dependent path separators in serialized relative paths.

`str(PurePath)` renders with the HOST's separator: `sub/a.bin` becomes
`sub\\a.bin` on a Windows machine. For a path that leaves the process - a JSON
API answer, a persisted record - that is a latent Windows-only defect the
local suite cannot execute, because on Linux `str()` and `as_posix()` are the
same string. The Windows CI leg found exactly this in the A2A workspace
listing: a file pushed as `sub/roundtrip.bin` was listed back as
`sub\\roundtrip.bin`, a name the remote fetch then missed. This guard makes
that class fail HERE, on any OS, instead of twenty-seven minutes into the
Windows runner.

The rule: a relative path that is serialized uses `.relative_to(...).as_posix()`,
never `str(...relative_to(...))`. A call site where the host-native form is the
CORRECT one (a path shown to something that works on this host's own terms)
goes into the allowlist below with its reason, so every new hit is a decision,
not an accident.
"""
import re
import subprocess
from pathlib import Path, PureWindowsPath

_REPO = Path(__file__).resolve().parent.parent

# `str(<expr>.relative_to(...))` - the serialization idiom this guard bans.
_STR_RELATIVE_TO = re.compile(r"str\(\s*[\w.\[\]'\"]+\.relative_to\(")

# path -> reason the host-native form is deliberate there.
_ALLOWED = {
    "vaf/core/workspace.py": (
        "get_context_info feeds the LLM prompt on THIS host; an agent working "
        "a Windows machine is told Windows paths, which is the truth there"
    ),
}


def _tracked_python_files():
    out = subprocess.run(
        ["git", "ls-files", "-z", "vaf/", "scripts/"],
        cwd=_REPO, capture_output=True, check=True,
    ).stdout.decode("utf-8", errors="ignore")
    for rel in out.split("\0"):
        if rel.endswith(".py") and (_REPO / rel).is_file():
            yield rel, _REPO / rel


def test_the_class_is_real_on_windows_paths():
    """Why this file exists, demonstrated: the two spellings differ exactly on
    Windows, which is exactly where the local suite never runs."""
    p = PureWindowsPath("sub") / "a.bin"
    assert str(p) == "sub\\a.bin"
    assert p.as_posix() == "sub/a.bin"


def test_no_str_of_relative_to_outside_the_allowlist():
    hits = []
    for rel, path in _tracked_python_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for i, line in enumerate(text.splitlines(), start=1):
            if _STR_RELATIVE_TO.search(line):
                if rel in _ALLOWED:
                    break
                hits.append(f"{rel}:{i}: {line.strip()}")
    assert not hits, (
        "str(...relative_to(...)) renders with the host's separator and breaks "
        "on Windows when the path is serialized. Use .relative_to(...).as_posix() "
        "- or add the file to _ALLOWED with the reason the native form is right:\n"
        + "\n".join(hits)
    )


def test_the_allowlist_carries_no_dead_entries():
    """An allowlist entry whose pattern is gone is noise that hides the next
    real hit behind a familiar name. Entries must earn their place."""
    for rel in _ALLOWED:
        path = _REPO / rel
        assert path.is_file(), f"allowlisted file no longer exists: {rel}"
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert _STR_RELATIVE_TO.search(text), (
            f"allowlisted file no longer contains the pattern, drop it: {rel}"
        )
