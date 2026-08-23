# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Windows-only defects the local suite cannot execute, caught here instead.

Both guards below share one shape: a spelling that is byte-identical on Linux
and macOS and wrong on Windows, so no local run can ever fail on it and the
27-minute Windows leg is the only thing that says so. Each is made to fail
HERE, on any OS.

## 1. No OS-dependent path separators in serialized relative paths.

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

## 2. No newline-sensitive needle against bytes that were never normalized.

`read_text()` decodes in TEXT mode, so Python's universal-newline translation
turns a CRLF file into `\\n` on every platform. `read_bytes().decode(...)` does
not: it hands back exactly what is on disk. Git materializes text files with
the platform's own line endings, and this repo pins only `.bat`/`.cmd`/`.sh` in
`.gitattributes`, so on a Windows checkout a `.tsx` file really does hold
`\\r\\n`. A guard test that asserts a needle containing `\\n` against such bytes
therefore passes on Linux and macOS and fails on Windows for a reason that has
nothing to do with what it means to test.

The Windows leg found exactly this: `"HOTBAR_KINDS\\n" in page` against
`page.tsx` read as bytes, reported as "the tiles stopped deriving from the
browser-excluded list" while the tiles were perfectly fine.

The rule: read source you are INSPECTING with `read_text()`. The house rule
that bans `read_text()`/`write_text()` is about bulk REWRITES, where universal
newlines silently rewrites a CRLF file on save; reading for a static assertion
is the opposite case, and there the translation is the thing you want.
"""
import ast
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


def _tracked_python_files(roots=("vaf/", "scripts/")):
    out = subprocess.run(
        ["git", "ls-files", "-z", *roots],
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


# ── 2. newline-sensitive needles against unnormalized bytes ───────────────────

def _reads_bytes(node) -> bool:
    """True for a `.read_bytes()` chain, with or without a `.decode(...)` on top.

    That is the spelling with no newline translation. `read_text()` is the safe
    one and deliberately does not match here.
    """
    cur = node
    while isinstance(cur, ast.Call):
        func = cur.func
        if not isinstance(func, ast.Attribute):
            return False
        if func.attr == "read_bytes":
            return True
        cur = func.value
    return False


def _unnormalized_newline_needles(source: str):
    """(line, needle) for every `"...\\n..." in <bytes-derived text>` in a module.

    The taint is followed one hop further than the read itself, because the
    readers in this repo are usually a helper (`_css()`) or a module constant,
    not the call site of the assertion.
    """
    tree = ast.parse(source)
    tainted_names, tainted_funcs = set(), set()

    def taint_from(value) -> bool:
        if _reads_bytes(value):
            return True
        names = {n.id for n in ast.walk(value) if isinstance(n, ast.Name)}
        return bool(names & (tainted_names | tainted_funcs))

    # Two passes so a helper defined below its caller is still seen.
    for _ in range(2):
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and taint_from(node.value):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        tainted_names.add(target.id)
            elif isinstance(node, ast.FunctionDef):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Return) and sub.value is not None \
                            and taint_from(sub.value):
                        tainted_funcs.add(node.name)

    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not any(isinstance(op, ast.In) for op in node.ops):
            continue
        left = node.left
        if not (isinstance(left, ast.Constant) and isinstance(left.value, str)):
            continue
        if "\n" not in left.value:
            continue
        right = node.comparators[0]
        if isinstance(right, ast.Name):
            tainted = right.id in tainted_names
        elif isinstance(right, ast.Call) and isinstance(right.func, ast.Name):
            tainted = right.func.id in tainted_funcs
        else:
            tainted = _reads_bytes(right)
        if tainted:
            hits.append((node.lineno, left.value))
    return hits


def test_the_newline_class_is_real():
    """Why this guard exists, demonstrated: the two reads differ exactly on a
    CRLF file, which is exactly what a Windows checkout produces."""
    crlf = b"const A = 1;\r\nconst B = 2;\r\n"
    assert "const A = 1;\n" not in crlf.decode("utf-8")          # read_bytes().decode()
    assert "const A = 1;\n" in crlf.decode("utf-8").replace("\r\n", "\n")


def test_no_newline_needle_against_unnormalized_bytes():
    hits = []
    for rel, path in _tracked_python_files(("tests/",)):
        try:
            found = _unnormalized_newline_needles(
                path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for lineno, needle in found:
            hits.append(f"{rel}:{lineno}: {needle!r}")
    assert not hits, (
        "a needle containing a newline is matched against bytes that skipped "
        "universal-newline translation. On a Windows checkout the file holds "
        "\\r\\n and the assertion fails for a reason unrelated to its subject. "
        "Read the source with read_text(encoding=...) instead:\n" + "\n".join(hits)
    )
