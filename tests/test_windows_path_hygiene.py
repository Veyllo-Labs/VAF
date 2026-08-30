# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Windows-only defects the local suite cannot execute, caught here instead.

Every guard below shares one shape: a spelling that is byte-identical on Linux
and macOS and wrong on Windows, so no local run can ever fail on it and the
27-minute Windows leg is the only thing that says so. Each is made to fail
HERE, on any OS.

## 4. `os.path.isabs` alone never decides whether a fragment is rooted.

`os.path.isabs` answers for the HOST, and on Windows the answer changed:
Python 3.13 stopped calling a driveless rooted path ("/etc/x") absolute there,
where 3.10 to 3.12 called it absolute. A containment check built on that call
alone therefore reads an explicitly rooted target as ordinary relative text on
a Windows 3.13 runner and joins it onto the root instead of refusing it. The
nightly Windows leg found exactly that in `vaf.contained_path`, on 3.13 only,
while the same job on 3.12 stayed green - the pair of results IS the diagnosis.

The mirror case needs no Windows at all and is what makes this guard local: on
POSIX, `os.path.isabs("\\etc\\x")` is False too, and the separator
normalisation inside the helper then turns that fragment into a plain relative
path. Measured before the fix: `contained_path(root, "\\etc\\escape.txt")`
returned `<root>/etc/escape.txt` on Linux instead of refusing.

Nothing escapes the root in either case - the resolve step still contains the
target - but the caller is handed a DIFFERENT path than the one they named,
which is precisely what the primitive promises never to do.

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

## 3. No subprocess environment that Windows cannot start a Python in.

Passing `env=` to a subprocess REPLACES the environment; it does not extend it.
A dict holding only POSIX keys is a perfectly good environment on Linux and a
broken one on Windows, and it breaks twice over.

`SystemRoot` is load-bearing there. Without it the CryptoAPI is unreachable, so
`_Py_HashRandomization_Init` kills the interpreter before the first line runs,
and Winsock cannot resolve its provider DLL, so any import chain that reaches
`asyncio` raises `WinError 10106`. The Windows leg found exactly this in the
memory-package import test: `models.py` imports sqlalchemy, sqlalchemy imports
asyncio, `asyncio.windows_events` imports `_overlapped`, and the process died.
The threaded sibling then reported `NameError: name 'base_events' is not
defined`, which is the same failure wearing a second face: the aborted import
left a half-built module behind for the other thread to trip over.

`HOME` is not the home directory there either. `ntpath.expanduser` reads
`USERPROFILE` and ignores `HOME`, so a scratch home named only as `HOME` leaves
`~` literal, to be resolved against the working directory, which is usually this
checkout. The isolation does not fail loudly; it simply is not there.

This class had already been diagnosed once, in `_narrow_env` in
`test_windows_console_encoding.py`, and was never written down as a rule. It
came back in a new file within days, which is precisely what a guard is for.

The rule: an `env=` dict either carries the whole environment (a `**os.environ`
spread, `dict(os.environ)`, a comprehension over it) or it names `SystemRoot`
itself. Reading a single key out of `os.environ` does not count, because one key
does not make an environment startable. And a dict that sets `HOME` names
`USERPROFILE` beside it.
"""
import ast
import ntpath
import os
import posixpath
import re
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from unittest import mock

import pytest

_REPO = Path(__file__).resolve().parent.parent

# `str(<expr>.relative_to(...))` - the serialization idiom this guard bans.
_STR_RELATIVE_TO = re.compile(r"str\(\s*[\w.\[\]'\"]+\.relative_to\(")

# path -> reason the host-native form is deliberate there.
_ALLOWED = {
    "vaf/core/workspace.py": (
        "get_context_info feeds the LLM prompt on THIS host; an agent working "
        "a Windows machine is told Windows paths, which is the truth there"
    ),
    "tests/test_windows_path_hygiene.py": (
        "the guard names the banned spelling in its own prose and regex; "
        "nothing here serializes a path"
    ),
}


# tests/ joined the scan on 2026-08-28: two ratchet guards in the i18n suite
# keyed their debt dicts on str(relative_to(...)), so on a Windows checkout
# every hit file read as NEW and the leg went red 45 minutes in - the exact
# class this file pins, sitting in the one tree it did not scan.
def _tracked_python_files(roots=("vaf/", "scripts/", "tests/")):
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


# -- 3. subprocess environments Windows cannot start a Python in ---------------

# Functions that hand an env= straight to the OS. A helper that merely stores the
# dict (a fake terminal opener in a fixture, an MCP manifest) is not one of these
# and is deliberately not scanned: it may well merge onto os.environ downstream.
_SPAWNERS = {"run", "Popen", "call", "check_call", "check_output",
             "execve", "execvpe", "spawnve", "posix_spawn"}

# The one key a Python subprocess cannot start without on Windows. SystemDrive
# belongs beside it in real code, but requiring it too would reject the correct
# helper this rule is modelled on, so the guard asks for the load-bearing one.
_WINDOWS_CRITICAL = "SYSTEMROOT"


def _is_spawn(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr in _SPAWNERS
    return isinstance(func, ast.Name) and func.id in _SPAWNERS


def _mentions_environ(node) -> bool:
    return any(isinstance(n, ast.Attribute) and n.attr == "environ"
               for n in ast.walk(node))


def _inherits_environ(region) -> bool:
    """True only when the WHOLE environment is carried over, not merely read.

    Reading one key out of os.environ does not make an environment startable, so
    a stray os.environ.get("PATH") must not silence the guard. Only a spread
    counts: {**os.environ}, dict(os.environ), os.environ.copy(), or a
    comprehension over it.
    """
    for n in ast.walk(region):
        if isinstance(n, ast.DictComp):
            if any(_mentions_environ(g.iter) for g in n.generators):
                return True
        if isinstance(n, ast.Dict):
            for key, value in zip(n.keys, n.values):
                if key is None and _mentions_environ(value):
                    return True
        if isinstance(n, ast.Call):
            if isinstance(n.func, ast.Name) and n.func.id == "dict" \
                    and any(_mentions_environ(a) for a in n.args):
                return True
            if isinstance(n.func, ast.Attribute) and n.func.attr == "copy" \
                    and _mentions_environ(n.func.value):
                return True
    return False


def _assigned_in(func, name: str) -> bool:
    """True if the function body rebinds `name`, rather than only receiving it."""
    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                return True
        if isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                return True
    return False


def _not_this_guards_business(env_node, owner) -> bool:
    """Spellings this guard deliberately does not judge.

    `env=None` is the subprocess default and inherits the whole environment. A
    bare `os.environ`, or a copy of it, IS the environment. And a name that
    arrives as a parameter is the caller's decision, which cannot be read from
    the spawn site - judging it here would be a guess dressed as a rule.
    """
    if isinstance(env_node, ast.Constant):
        return True
    if _mentions_environ(env_node) and not isinstance(env_node, (ast.Dict, ast.DictComp)):
        return True
    if isinstance(env_node, ast.Name):
        func = owner.get(env_node)
        if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            spec = func.args
            params = {a.arg for a in
                      [*spec.posonlyargs, *spec.args, *spec.kwonlyargs]}
            if spec.vararg:
                params.add(spec.vararg.arg)
            if spec.kwarg:
                params.add(spec.kwarg.arg)
            if env_node.id in params and not _assigned_in(func, env_node.id):
                return True
    return False


def _innermost_function(tree):
    """node -> the innermost FunctionDef containing it, or None at module level."""
    owner = {}

    def walk(node, current):
        for child in ast.iter_child_nodes(node):
            owner[child] = current
            deeper = child if isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef)) else current
            walk(child, deeper)

    walk(tree, None)
    return owner


def _env_region(env_node, tree, owner):
    """The region whose contents decide the verdict for one env= expression.

    A dict literal answers for itself. Anything else (a local name, a helper
    call) is answered by the function that builds it, so a loop that adds the
    Windows keys afterwards is seen rather than missed. That is exactly how the
    correct helper in test_windows_console_encoding.py is written.
    """
    if isinstance(env_node, ast.Dict):
        return env_node
    if isinstance(env_node, ast.Call) and isinstance(env_node.func, ast.Name):
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name == env_node.func.id:
                return node
    return owner.get(env_node) or tree


def _posix_only_envs(source: str):
    """(line, reason) for every spawn whose env= cannot start a Python on Windows."""
    tree = ast.parse(source)
    owner = _innermost_function(tree)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _is_spawn(node)):
            continue
        env_kw = next((k for k in node.keywords if k.arg == "env"), None)
        if env_kw is None:
            continue
        if _not_this_guards_business(env_kw.value, owner):
            continue
        region = _env_region(env_kw.value, tree, owner)
        if _inherits_environ(region):
            continue
        names = {n.value.upper() for n in ast.walk(region)
                 if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        if _WINDOWS_CRITICAL not in names:
            yield node.lineno, "no SystemRoot: Windows cannot start the interpreter"
        elif "HOME" in names and "USERPROFILE" not in names:
            yield node.lineno, "HOME without USERPROFILE: the scratch home is ignored on Windows"


def test_the_env_class_is_real():
    """Why this guard exists, demonstrated: one environment, two meanings.

    The two path modules read different keys, so an env that redirects the home
    directory on POSIX leaves it untouched on Windows. Worse than untouched: the
    tilde stays literal, so it resolves against the working directory, which for
    a test subprocess is normally the checkout itself.
    """
    only_home = {"HOME": "/scratch"}
    with mock.patch.dict(os.environ, only_home, clear=True):
        assert posixpath.expanduser("~") == "/scratch"
        assert ntpath.expanduser("~") == "~"
    with mock.patch.dict(os.environ, {**only_home, "USERPROFILE": "/scratch"},
                         clear=True):
        assert ntpath.expanduser("~") == "/scratch"


def test_no_posix_only_subprocess_env():
    hits = []
    for rel, path in _tracked_python_files(("vaf/", "scripts/", "tests/")):
        try:
            found = list(_posix_only_envs(
                path.read_text(encoding="utf-8", errors="ignore")))
        except SyntaxError:
            continue
        for lineno, reason in found:
            hits.append(f"{rel}:{lineno}: {reason}")
    assert not hits, (
        "env= REPLACES the environment rather than extending it, and this one "
        "holds only POSIX keys. Carry the whole environment ({**os.environ, ...}) "
        "or name the Windows keys beside the POSIX ones:\n"
        "    for keep in (\"SYSTEMROOT\", \"SystemRoot\", \"SYSTEMDRIVE\", \"SystemDrive\"):\n"
        "        if keep in os.environ:\n"
        "            env[keep] = os.environ[keep]\n"
        + "\n".join(hits)
    )


def test_the_isabs_class_is_real():
    """Why the section above exists, demonstrated on this very interpreter.

    Both directions in one place: the POSIX flavour cannot see a Windows root,
    and (from 3.13) the Windows flavour cannot see a POSIX one.
    """
    assert not posixpath.isabs("\\etc\\x")
    assert PureWindowsPath("/etc/x").root == "\\"      # rooted, and yet:
    if sys.version_info >= (3, 13):
        assert not ntpath.isabs("/etc/x")               # ... not "absolute"
    assert ntpath.isabs("C:\\x") and ntpath.isabs("\\\\srv\\share\\x")


def test_contained_path_refuses_a_rooted_fragment_under_windows_semantics(tmp_path):
    """The 3.13 Windows failure, executed on this host.

    `os.path` is swapped for `ntpath`, which is what the check faces on a
    Windows runner. The assertion is on the REASON, not just on the refusal:
    before the fix this raised too, but from the containment step further down
    with "must stay inside the root", which is an artefact of the simulation
    rather than the guard doing its job. Matching the message is what makes
    this test fail when the rootedness check is taken back out.
    """
    import vaf

    with mock.patch.object(os, "path", ntpath):
        with pytest.raises(vaf.PathEscape, match="must be relative"):
            vaf.contained_path(tmp_path, "/etc/escape.txt")


# ── 3. POSIX-only os attributes patched where they do not exist ───────────────
#
# The class, measured when it went red: 10 `monkeypatch.setattr(<mod>.os, "<posix
# name>", ...)` calls across two test files. On Linux every one of them succeeds
# and the tests are green; on Windows the attribute does not exist, so setattr
# raises at SETUP and the test fails before it has exercised anything - which is
# a failure of the harness, not a finding about the product. Five such tests took
# the Windows leg red 27 minutes into a push while the whole local suite was
# green, because this machine has every one of these attributes.
#
# The product itself is fine either way: its POSIX calls sit inside try/except and
# fall back to the portable path. Only the patching needs to say so.
#
# Two spellings are accepted, both mechanical:
#   * `raising=False` on the call - the patch becomes a no-op and the test runs
#     the platform's real fallback, which is the more useful of the two; or
#   * a module-level skip keyed on `hasattr(os, ...)`, the pattern
#     tests/test_stop_frontend_safety.py established.

_POSIX_ONLY_OS_ATTRS = (
    "getpgid", "killpg", "setsid", "getuid", "geteuid", "getgid", "setpgrp",
    "fork", "forkpty", "getppid", "nice", "wait3", "wait4", "WIFEXITED",
)

_POSIX_PATCH_RE = re.compile(
    r"monkeypatch\.setattr\(\s*[^,]*\bos\s*,\s*[\"'](" + "|".join(_POSIX_ONLY_OS_ATTRS) + r")[\"']"
)


def _posix_patches_without_a_guard(path: Path):
    """(line_no, attr) for each unguarded patch of a POSIX-only os attribute."""
    source = path.read_text(encoding="utf-8")
    # A module-level skip keyed on the attribute's presence covers the whole file.
    if re.search(r"skipif\([^)]*hasattr\(\s*os\s*,", source, re.S):
        return []
    out = []
    lines = source.splitlines()
    for i, line in enumerate(lines, 1):
        m = _POSIX_PATCH_RE.search(line)
        if not m:
            continue
        # the call may wrap; look at it and the next two lines for raising=False
        window = "\n".join(lines[i - 1:i + 2])
        if "raising=False" not in window:
            out.append((i, m.group(1)))
    return out


def test_the_class_is_real_on_this_platform():
    """Windows has none of these; this host has them all, which is the whole gap."""
    import os as _os
    assert any(hasattr(_os, a) for a in _POSIX_ONLY_OS_ATTRS), \
        "the guard below would be vacuous if this host had none of them either"


def test_no_posix_only_os_attribute_is_patched_without_a_guard():
    offenders = []
    for path in sorted((_REPO / "tests").rglob("test_*.py")):
        for line_no, attr in _posix_patches_without_a_guard(path):
            offenders.append(f"{path.relative_to(_REPO).as_posix()}:{line_no}: os.{attr}")
    assert not offenders, (
        "monkeypatch.setattr on a POSIX-only os attribute fails at setup on Windows.\n"
        "Add raising=False (preferred: the test then runs the platform's real fallback), "
        "or a module-level skipif keyed on hasattr(os, ...):\n  " + "\n  ".join(offenders)
    )
