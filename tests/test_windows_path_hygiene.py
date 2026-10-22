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
never `str(...relative_to(...))` and never a bare `{x.relative_to(...)}` inside an
f-string, which is the same `str()` in disguise. The Windows leg found that
second spelling on 2026-09-10 in a spelling guard of the test suite: it rendered
its hits as `f"{path.relative_to(root)}:{no}"` and compared them with posix
literals, so every hit read as foreign on the Windows checkout and the leg went
red 42 minutes in, while eight sibling sites used the same idiom for their
failure messages only. A call site where the host-native form is the
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

## 5. A stand-in binary a test writes is not a program on every host.

A test that needs an executable writes a `#!/bin/sh` stub, marks it executable
and lets the code under test start it. Linux and macOS run it. Windows does not
accept the file as a program at all, `subprocess` raises, and code that reads a
program's output to decide something gets nothing and decides the other way. The
class hides twice over: here the stub runs, so no local run can fail on it, and
on the Windows leg the failure surfaces as the code under test making the wrong
choice, several frames away from the stub that caused it.

The Windows leg found exactly this in the llama.cpp pin tests: four of them
wrote a shell stub for `llama-server`, and `installed_build()` came back None on
that runner while the same tests passed on five other jobs, so the launcher
looked as though it never recognised the build it had just installed. Reproduced
here afterwards by refusing to execute the stub: the same four names and nothing
else.

The rule: a test may write an executable stub only when it is skipped on
Windows, and it then tests the exec itself and nothing more. A test that is
about a DECISION stands in for the one step that starts the program (here
`ServerManager._version_output`), which is what makes it hold on every host.

## 6. No two modules in one folder whose names differ only in case.

Windows and macOS compare file names without case; Linux does not. The web chat
had `askChoices.ts` (the question contract) next to `AskChoices.tsx` (the
component). `import AskChoices from './AskChoices'` looks for `.ts` before
`.tsx`, so on a case-blind file system it found the CONTRACT module, and the
build failed with "differs from already included file name only in casing" and
"has no default export" - on the macOS and Windows web-build legs only, while
every Linux build, local ones included, was green. The same holds for two tracked
paths that differ only in case: a checkout on those systems keeps one of them.
This guard reads the tracked names, so it fails HERE, on any OS.

## 7. A POSIX-only signal is never read on the Windows path.

`signal.SIGKILL` (like SIGHUP or SIGUSR1) does not exist on Windows. The tree stop
(`Platform.terminate_process_tree`) passed `_signal.SIGKILL` to a helper that ignores
it when there is no process group, which is always the case on Windows - but the
ARGUMENT was evaluated first, so the stop raised AttributeError after the tree was
already dead. Every caller then reported a failed stop: the Windows test leg found
`processes.stop` answering "could not be stopped" and `terminate_all` counting 0 of
2. On Linux the name exists, so no local run could fail. Here the Windows side of
the seam is simulated: `is_windows()` true and no SIGKILL in the signal module.

## 8. A path the protocol defines as POSIX is never normalised with the host's flavour.

An FTP path, a tar member name, a path inside a container: POSIX whatever the host.
`os.path.normpath(os.path.join("/", "docs"))` is `/docs` on Linux and `\\docs` on
Windows, and a following `.lstrip("/")` leaves the backslash standing, so the join
onto a local folder roots at the drive. The Windows leg found exactly this in the
FTP test server (tests/ftp_stub.py): every path read as outside its root, nine FTP
tests failed with "550 outside" or a closed connection, and every Linux and macOS
job was green. The rule: such a path goes through `posixpath`; the scan below
refuses the spelling that gives it away, an `os.path` normalise or join whose
result is then stripped, split or tested against "/".

## 9. A test does not expect a POSIX literal back from the str() of a Path.

The other half of section 1, on the test side. A test hands the code `Path("/b/old")`,
the code records `str(path)` (right: an absolute path on this host, read on this host),
and the test asserts the record equals `"/b/old"`. On Windows the record is `\\b\\old`.
The Windows leg found exactly this in the backup schedule's prune record, an hour into
the run, while every other job was green. The rule: compare against `str(Path(...))` of
the same literal, or against `.as_posix()` when the record is meant to be POSIX; the scan
below refuses a literal that a test both wraps in a Path and asserts bare.
"""
import ast
import ntpath
import os
import posixpath
import re
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from typing import Optional
from unittest import mock

import pytest

_REPO = Path(__file__).resolve().parent.parent

# `str(<expr>.relative_to(...))` - the serialization idiom this guard bans.
_STR_RELATIVE_TO = re.compile(r"str\(\s*[\w.\[\]'\"]+\.relative_to\(")
# `{<expr>.relative_to(...)}` inside an f-string, with a format spec or conversion or
# nothing after the call: the same str() in disguise. `.as_posix()` after the call
# does not match, which is the spelling the rule asks for.
_FSTRING_RELATIVE_TO = re.compile(r"\{[^{}]*\.relative_to\([^()]*\)\s*[}:!]")

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
            if _STR_RELATIVE_TO.search(line) or _FSTRING_RELATIVE_TO.search(line):
                if rel in _ALLOWED:
                    break
                hits.append(f"{rel}:{i}: {line.strip()}")
    assert not hits, (
        "str(...relative_to(...)), and {x.relative_to(...)} in an f-string, render with "
        "the host's separator and break on Windows when the path is serialized. Use "
        ".relative_to(...).as_posix() - or add the file to _ALLOWED with the reason the "
        "native form is right:\n"
        + "\n".join(hits)
    )


def test_the_allowlist_carries_no_dead_entries():
    """An allowlist entry whose pattern is gone is noise that hides the next
    real hit behind a familiar name. Entries must earn their place."""
    for rel in _ALLOWED:
        path = _REPO / rel
        assert path.is_file(), f"allowlisted file no longer exists: {rel}"
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert _STR_RELATIVE_TO.search(text) or _FSTRING_RELATIVE_TO.search(text), (
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


# Helpers in other modules that return the whole environment plus keys of their own. The
# guard reads one file at a time and cannot follow the call, so they are named here, and
# test_named_environment_helpers_carry_the_environment holds each one to it.
_CARRIES_ENVIRON = {"child_env"}   # vaf/core/node_runtime.py


def _inherits_environ(region) -> bool:
    """True only when the WHOLE environment is carried over, not merely read.

    Reading one key out of os.environ does not make an environment startable, so
    a stray os.environ.get("PATH") must not silence the guard. Only a spread
    counts: {**os.environ}, dict(os.environ), os.environ.copy(), a
    comprehension over it, or a helper named in _CARRIES_ENVIRON.
    """
    for n in ast.walk(region):
        if isinstance(n, ast.Call):
            name = n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
            if name in _CARRIES_ENVIRON:
                return True
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


@pytest.mark.skipif(os.environ.get("VAF_SIMULATED_WINDOWS_HOME") == "1",
                    reason="measures the real platform's home semantics, which scripts/hostile_env.py replaces on purpose (windows-home axis)")
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


def test_named_environment_helpers_carry_the_environment():
    """What lets _CARRIES_ENVIRON vouch for a helper: every key of this process's environment
    comes back, SystemRoot included on Windows. MUTATION: return only PATH from child_env - red."""
    from vaf.core import node_runtime
    assert _CARRIES_ENVIRON == {"child_env"}, "a new name here needs its own check below"
    env = node_runtime.child_env(sys.executable)
    assert {k: v for k, v in env.items() if k != "PATH"} == {
        k: v for k, v in os.environ.items() if k != "PATH"}


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
#   * `raising=False` on the call - where the attribute is missing, pytest creates
#     it for the test and deletes it afterwards, so the stub runs on Windows too and
#     the test checks the same thing there, which is the more useful of the two; or
#   * a skip keyed on `hasattr(os, ...)` that covers the patching test: on the test
#     itself (`_posix_only` in tests/test_stop_frontend_safety.py), on its class, or
#     as the module's `pytestmark`. A skip on one test covers no other test in the
#     file. The scan reads each function with `_windows_runs` (section 5), the
#     reading the executable-stub guard uses, and only up to a statement that skips
#     the test on Windows: a patch before it fails there first.
# Each call is read from the syntax tree, so `raising=False` counts only in that
# call's own arguments, never on a neighbouring line.

# Absent on Windows (Python's os docs: "Availability: Unix"). getppid is not here: it
# exists on Windows too.
_POSIX_ONLY_OS_ATTRS = (
    "getpgid", "killpg", "setsid", "getuid", "geteuid", "getgid", "setpgrp",
    "fork", "forkpty", "nice", "wait3", "wait4", "WIFEXITED",
)


def _posix_patch(call) -> Optional[str]:
    """The attribute a `<anything>.setattr(<...>os, "<POSIX-only name>", ...)` call patches
    without `raising=False` among its own arguments, or None."""
    if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
            and call.func.attr == "setattr" and len(call.args) >= 2
            and re.search(r"\bos$", ast.unparse(call.args[0]))
            and isinstance(call.args[1], ast.Constant)
            and call.args[1].value in _POSIX_ONLY_OS_ATTRS):
        return None
    raising = next((k.value for k in call.keywords if k.arg == "raising"),
                   call.args[3] if len(call.args) > 3 else None)
    if isinstance(raising, ast.Constant) and raising.value is False:
        return None
    return call.args[1].value


def _posix_patches_without_a_guard(path: Path):
    """(line_no, attr) for each patch of a POSIX-only os attribute without `raising=False`
    that the Windows leg reaches (`_windows_runs`, section 5)."""
    source = path.read_text(encoding="utf-8")
    if "setattr" not in source:
        return []
    out = []
    for fn, last_line in _windows_runs(ast.parse(source)):
        for call in ast.walk(fn):
            attr = _posix_patch(call)
            if attr and call.lineno <= last_line:
                out.append((call.lineno, attr))
    return sorted(out)


def test_the_class_is_real_on_this_platform():
    """The list names what Windows lacks: a POSIX host has every one of them and Windows has
    none (measured on the Windows runner, Python 3.12), which is the whole gap - a test that
    patches one passes here and fails there. It used to ask for at least one of them, and a
    Windows host has none, so it failed there. MUTATION: add a name Windows has (startfile) -
    red on a POSIX host."""
    import os as _os
    present = [a for a in _POSIX_ONLY_OS_ATTRS if hasattr(_os, a)]
    if _os.name == "nt":
        assert present == [], f"Windows has {present}: not POSIX-only"
    else:
        assert present == list(_POSIX_ONLY_OS_ATTRS), \
            f"this host lacks {sorted(set(_POSIX_ONLY_OS_ATTRS) - set(present))}"


def test_the_posix_patch_scanner_reads_each_tests_own_skip(tmp_path):
    """A skip covers the test it sits on, its class's tests, or the module's: one test's mark
    exempted the whole file, so an unmarked test beside it went unscanned. A root-only skip
    (`hasattr(os, "geteuid") and os.geteuid() == 0`) does not skip on Windows, nor does one
    keyed on `getppid`, which Windows has. A statement that skips in the body comes after
    what runs before it, and `raising=False` counts in its own call only. MUTATION: go back
    to the whole-file exemption; ignore a skip bound to a name, in a decorator or in a
    `pytestmark`; ignore `hasattr`; count getppid as absent; leave out a function that skips
    in its body; read `raising` from the neighbouring lines - red."""
    patch = "    monkeypatch." + 'setattr(m.os, "getpgid", lambda pid: 1)\n'
    posix_only = 'pytest.mark.skipif(not hasattr(os, "killpg"), reason="r")'
    head = "import os\n\nimport pytest\n\n"
    windows_skip = '    if os.name == "nt":\n        pytest.skip("r")\n'
    cases = {
        "named": (head + f"_posix_only = {posix_only}\n\n\n@_posix_only\n"
                  "def test_marked(monkeypatch):\n" + patch +
                  "\n\ndef test_unmarked(monkeypatch):\n" + patch, [14]),
        "direct": (head + f"@{posix_only}\ndef test_marked(monkeypatch):\n" + patch, []),
        "module": (head + f"pytestmark = {posix_only}\n\n\ndef test_x(monkeypatch):\n" + patch, []),
        "module_named": (head + f"_posix_only = {posix_only}\npytestmark = [_posix_only]\n\n\n"
                         "def test_x(monkeypatch):\n" + patch, []),
        "class_named": (head + f"_posix_only = {posix_only}\n\n\nclass TestX:\n"
                        "    pytestmark = _posix_only\n\n    def test_x(self, monkeypatch):\n"
                        "    " + patch, []),
        "root_only": (head + '@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,'
                      ' reason="r")\ndef test_x(monkeypatch):\n' + patch, [7]),
        "getppid": (head + '@pytest.mark.skipif(not hasattr(os, "getppid"), reason="r")\n'
                    "def test_x(monkeypatch):\n" + patch, [7]),
        "before_skip": (head + "def test_x(monkeypatch):\n" + patch + windows_skip, [6]),
        "after_skip": (head + "def test_x(monkeypatch):\n" + windows_skip + patch, []),
        "raising": (head + "def test_x(monkeypatch):\n"
                    + patch.replace("lambda pid: 1)", "lambda pid: 1, raising=False)"), []),
        "raising_positional": (head + "def test_x(monkeypatch):\n"
                               + patch.replace("lambda pid: 1)", "lambda pid: 1, False)"), []),
        "neighbour": (head + "def test_x(monkeypatch):\n" + patch + "    monkeypatch."
                      + 'setattr(m.os, "killpg", lambda *a: None, raising=False)\n', [6]),
    }
    for name, (source, lines) in cases.items():
        path = tmp_path / f"test_{name}.py"
        path.write_text(source, encoding="utf-8")
        assert [n for n, _ in _posix_patches_without_a_guard(path)] == lines, name


def test_no_posix_only_os_attribute_is_patched_without_a_guard():
    offenders = []
    for path in sorted((_REPO / "tests").rglob("test_*.py")):
        for line_no, attr in _posix_patches_without_a_guard(path):
            offenders.append(f"{path.relative_to(_REPO).as_posix()}:{line_no}: os.{attr}")
    assert not offenders, (
        "monkeypatch.setattr on a POSIX-only os attribute fails at setup on Windows.\n"
        "Add raising=False (preferred: pytest then creates the missing attribute for the "
        "test, so the stub runs on Windows too), or skip that test on Windows with a skipif "
        "keyed on hasattr(os, ...) on the test, its class or the module's pytestmark:\n  "
        + "\n  ".join(offenders)
    )


# --- 5. an executable stub is not a program on Windows ---------------------------

# A quoted shebang: the tell that a test is writing a program for the host to run.
_SHEBANG_LITERAL = re.compile(r"""["']#!/(?:bin|usr)/""")
# chmod(0o755), TarInfo.mode = 0o755: the tell that it is meant to be STARTED. The mode
# is read as a number rather than matched digit by digit, so 0o700 counts as well.
_OCTAL_MODE = re.compile(r"(?:chmod\(\s*|\bmode\s*=\s*)(0o[0-7]{3,4})")
# What the platform probes this suite uses answer on Windows. A skip is a Windows skip when
# its condition, evaluated with these, is true: `os.name == "nt"` and
# `platform.system() != "Linux"` are, `os.name != "nt"` (a Windows-only test) is not.
_ON_WINDOWS = {"os.name": "nt", "sys.platform": "win32", "platform.system()": "Windows"}


def _probe(node) -> Optional[str]:
    """The Windows value of a platform probe (`os.name`, `__import__("os").name`,
    `sys.platform`, `platform.system()`), or None for anything else."""
    if isinstance(node, ast.Call) and not node.args and isinstance(node.func, ast.Attribute):
        if ast.unparse(node.func) == "platform.system":
            return _ON_WINDOWS["platform.system()"]
    if isinstance(node, ast.Attribute):
        owner = node.value
        if (isinstance(owner, ast.Call) and getattr(owner.func, "id", "") == "__import__"
                and owner.args and isinstance(owner.args[0], ast.Constant)):
            owner = ast.Name(id=str(owner.args[0].value))
        if isinstance(owner, ast.Name):
            return _ON_WINDOWS.get(f"{owner.id}.{node.attr}")
    return None


def _true_on_windows(node) -> Optional[bool]:
    """A skip condition evaluated as on Windows: True, False, or None when it does not depend
    on the platform alone (a missing git, a constant this scan cannot see)."""
    if isinstance(node, ast.BoolOp):
        values = [_true_on_windows(v) for v in node.values]
        if isinstance(node.op, ast.Or):
            return True if True in values else (None if None in values else False)
        return False if False in values else (None if None in values else True)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        inner = _true_on_windows(node.operand)
        return None if inner is None else not inner
    # `hasattr(os, "killpg")`: Windows has none of the POSIX-only names section 3 lists.
    if (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "hasattr"
            and len(node.args) == 2 and ast.unparse(node.args[0]) == "os"
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in _POSIX_ONLY_OS_ATTRS):
        return False
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "startswith" and node.args
            and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
        value = _probe(node.func.value)
        return None if value is None else value.startswith(node.args[0].value)
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        # Each side as Windows sees it, in its own place: `"win" in sys.platform` and
        # `sys.platform in ("win32", "cygwin")` are both membership, the other way round.
        left, right = _operand(node.left), _operand(node.comparators[0])
        if left is None or right is None or not (_probe(node.left) or _probe(node.comparators[0])):
            return None
        op = node.ops[0]
        if isinstance(op, ast.Eq):
            return left == right
        if isinstance(op, ast.NotEq):
            return left != right
        # A tuple takes anything; a string only a string (`1 in "win32"` would raise).
        if isinstance(op, (ast.In, ast.NotIn)) and (
                isinstance(right, tuple) or (isinstance(right, str) and isinstance(left, str))):
            return (left in right) == isinstance(op, ast.In)
    return None


def _operand(node):
    """A comparison side as Windows sees it: a probe's value, a constant, or a tuple of
    constants (a list or set literal counts as one). None for anything else."""
    value = _probe(node)
    if value is not None:
        return value
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)) and all(
            isinstance(e, ast.Constant) for e in node.elts):
        return tuple(e.value for e in node.elts)
    return None


def _skips_windows(node, named=frozenset()) -> bool:
    """True when ``node`` (a decorator, a `pytestmark` value or a list of marks) IS a `skipif`
    whose condition is true on Windows, or a name in ``named`` (`_named_skips`). Only the
    mark itself: a skip inside a parametrize case (`pytest.param(..., marks=...)`) skips that
    case, and the others still run there. A string condition
    (`skipif("sys.platform == 'win32'")`, which pytest evaluates) is parsed and read the same
    way, never executed."""
    if isinstance(node, ast.Name):
        return node.id in named
    if isinstance(node, (ast.List, ast.Tuple)):
        return any(_skips_windows(e, named) for e in node.elts)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "skipif"):
        condition = node.args[0] if node.args else next(
            (k.value for k in node.keywords if k.arg == "condition"), None)
        if isinstance(condition, ast.Constant) and isinstance(condition.value, str):
            try:
                condition = ast.parse(condition.value.strip(), mode="eval").body
            except SyntaxError:
                return False
        return condition is not None and _true_on_windows(condition) is True
    return False


def _marks_something_executable(segment: str) -> bool:
    return any(int(m.group(1), 8) & 0o111 for m in _OCTAL_MODE.finditer(segment))


def _pytestmark_skips_windows(body, named=frozenset()) -> bool:
    """A `pytestmark` among these statements (a module's, a test class's) that skips on
    Windows, a mark bound to a name included (`pytestmark = _posix_only`, or in a list). Read
    from the statements themselves: the text before the first `def` also holds that
    function's decorators, and a skip on the first test would then have exempted every test
    after it."""
    for node in body:
        if (isinstance(node, (ast.Assign, ast.AnnAssign))
                and any(getattr(t, "id", "") == "pytestmark"
                        for t in (node.targets if isinstance(node, ast.Assign) else [node.target]))
                and node.value is not None and _skips_windows(node.value, named)):
            return True
    return False


# Modules that do not exist on Windows: `pytest.importorskip` of one skips there.
_POSIX_ONLY_MODULES = frozenset({"fcntl", "grp", "posix", "pty", "pwd", "resource", "syslog",
                                 "termios", "tty"})


def _skips_imperatively(body) -> bool:
    """A statement among these (a module's, a function's) that skips on Windows when it runs:
    `if <true on Windows>: pytest.skip(...)`, or `pytest.importorskip` of a module Windows
    does not have. The condition is read with `_true_on_windows`, never executed."""
    for node in body:
        if isinstance(node, ast.If) and _true_on_windows(node.test) is True and any(
                isinstance(c, ast.Call) and getattr(c.func, "attr", getattr(c.func, "id", ""))
                == "skip" for stmt in node.body for c in ast.walk(stmt)):
            return True
        value = node.value if isinstance(node, (ast.Expr, ast.Assign)) else None
        if (isinstance(value, ast.Call) and getattr(value.func, "attr", "") == "importorskip"
                and value.args and isinstance(value.args[0], ast.Constant)
                and value.args[0].value in _POSIX_ONLY_MODULES):
            return True
    return False


def _named_skips(tree: ast.Module) -> frozenset:
    """Names bound at module level to a skip that fires on Windows
    (`_posix_only = pytest.mark.skipif(...)`), read in order, so a name bound to such a
    name counts too. A decorator or a `pytestmark` naming one skips like the mark itself."""
    named: set = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and _skips_windows(stmt.value, frozenset(named)):
            named.update(t.id for t in stmt.targets if isinstance(t, ast.Name))
    return frozenset(named)


def _windows_runs(tree: ast.Module):
    """(function, last line) for every outermost function (a test, a helper, a method) the
    Windows leg enters: not under the module's skip (its `pytestmark`, or a statement that
    skips there), an enclosing class's skip (its decorator or its own `pytestmark`), or its
    own skip decorator. A function nested in one is part of it. A function that skips itself
    with a statement in its body runs on Windows up to that statement, so its last line is
    the one before it; otherwise it is the function's last line."""
    named = _named_skips(tree)

    def last_line(fn) -> int:
        for stmt in fn.body:
            if _skips_imperatively([stmt]):
                return stmt.lineno - 1
        return fn.end_lineno

    def walk(node, skipped):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                yield from walk(child, skipped
                                or any(_skips_windows(d, named) for d in child.decorator_list)
                                or _pytestmark_skips_windows(child.body, named))
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not (skipped or any(_skips_windows(d, named) for d in child.decorator_list)):
                    yield child, last_line(child)
            else:
                yield from walk(child, skipped)
    yield from walk(tree, _pytestmark_skips_windows(tree.body, named)
                    or _skips_imperatively(tree.body))


def _functions_run_on_windows(tree: ast.Module):
    """Every function `_windows_runs` yields that also runs to its end on Windows: one that
    skips itself in its body is left out whole, since what these scans look for (a stub
    started, a literal asserted) may come after the skip."""
    for fn, _ in _windows_runs(tree):
        if not _skips_imperatively(fn.body):
            yield fn


def _exec_stub_functions(path: Path):
    """(line_no, name) per function that writes an executable stub without a Windows skip."""
    source = path.read_text(encoding="utf-8")
    if not _SHEBANG_LITERAL.search(source):
        return []
    offenders = []
    for node in _functions_run_on_windows(ast.parse(source)):
        segment = ast.get_source_segment(source, node) or ""
        if _SHEBANG_LITERAL.search(segment) and _marks_something_executable(segment):
            offenders.append((node.lineno, node.name))
    return offenders


@pytest.mark.skipif(os.name == "nt", reason="the gap itself: this host starts a shell stub")
def test_the_exec_stub_class_is_real(tmp_path):
    """Why the guard below exists, demonstrated: the stub this host starts happily is not
    a program on Windows, and no local run can say so."""
    stub = tmp_path / "stub"
    stub.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    stub.chmod(0o755)
    assert subprocess.run([str(stub)], capture_output=True, text=True).stdout.strip() == "ok"


def test_the_exec_stub_scanner_reads_the_mode_and_honours_a_skip(tmp_path):
    shebang = "#!" + "/bin/sh"          # split, so this file is not a hit of its own guard
    body = f'    p.write_text("{shebang}\\necho hi\\n")\n    p.chmod(0o700)\n'
    bad = tmp_path / "test_bad.py"
    bad.write_text("def test_x(p):\n" + body, encoding="utf-8")
    assert _exec_stub_functions(bad), "an executable stub with no Windows skip is a hit"
    good = tmp_path / "test_good.py"
    good.write_text('import os\n\nimport pytest\n\n\n@pytest.mark.skipif(os.name == "nt", reason="r")\n'
                    "def test_x(p):\n" + body, encoding="utf-8")
    assert not _exec_stub_functions(good), "a test skipped on Windows may start what it writes"
    unread = tmp_path / "test_payload.py"
    unread.write_text(f'def test_x(p):\n    p.write_bytes(b"{shebang}\\ncurl x | sh\\n")\n', encoding="utf-8")
    assert not _exec_stub_functions(unread), "a payload nothing starts is not this guard's business"


def test_no_test_writes_an_executable_stub_it_expects_the_host_to_run():
    offenders = []
    for rel, path in _tracked_python_files(roots=("tests/",)):
        for line_no, name in _exec_stub_functions(path):
            offenders.append(f"{rel}:{line_no}: {name}")
    assert not offenders, (
        "a shell stub is not a program on Windows, so a test that starts one decides "
        "differently there. Stand in for the step that starts it, or skip the test on "
        "Windows (os.name == 'nt'):\n  " + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# 6. Case-blind name collisions
# ---------------------------------------------------------------------------

# The extensions an import resolves without naming them: two such files in one folder whose
# names differ only in case are one module on a case-blind file system.
_MODULE_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py")


def _case_blind_collisions(paths):
    """Groups of tracked paths that a case-blind file system cannot tell apart: the same
    path in another case, or modules of one folder whose names without the extension
    differ only in case."""
    by_path, by_module = {}, {}
    for rel in paths:
        by_path.setdefault(rel.lower(), set()).add(rel)
        stem, ext = posixpath.splitext(rel)
        if ext.lower() in _MODULE_EXTENSIONS:
            by_module.setdefault(stem.lower(), set()).add(rel)
    groups = [g for g in by_path.values() if len(g) > 1]
    groups += [g for g in by_module.values()
               if len({posixpath.splitext(r)[0] for r in g}) > 1]
    return sorted(sorted(g) for g in groups)


def test_the_case_blind_class_is_real():
    assert _case_blind_collisions([
        "web/components/chat/askChoices.ts", "web/components/chat/AskChoices.tsx",
    ]) == [["web/components/chat/AskChoices.tsx", "web/components/chat/askChoices.ts"]]
    assert _case_blind_collisions(["docs/Readme.md", "docs/README.md"])
    # One module in two languages of the same name and case is resolved the same everywhere.
    assert _case_blind_collisions(["web/lib/x.ts", "web/lib/x.tsx", "vaf/a.py", "vaf/b.py"]) == []


def test_no_two_tracked_names_differ_only_in_case():
    out = subprocess.run(["git", "ls-files", "-z"], cwd=_REPO, capture_output=True,
                         check=True).stdout.decode("utf-8", errors="ignore")
    collisions = _case_blind_collisions([rel for rel in out.split("\0") if rel])
    assert not collisions, (
        "these names are one file or one module on Windows and macOS; rename one of each "
        "group:\n  " + "\n  ".join(" / ".join(g) for g in collisions)
    )



# ---------------------------------------------------------------------------
# 7. POSIX-only signals on the Windows path
# ---------------------------------------------------------------------------

def test_the_tree_stop_runs_under_windows_semantics(monkeypatch):
    """MUTATION: pass `_signal.SIGKILL` to the group helper again and this raises."""
    pytest.importorskip("psutil")    # the psutil lane is the one Windows takes in practice
    import signal

    from vaf.core.platform import Platform
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        monkeypatch.setattr(Platform, "is_windows", staticmethod(lambda: True))
        monkeypatch.delattr(signal, "SIGKILL", raising=False)
        Platform.terminate_process_tree(child.pid, grace=2.0)
        assert child.wait(timeout=10) is not None, "the tree was stopped"
    finally:
        if child.poll() is None:
            child.kill()


# -- 8. POSIX protocol paths normalised with the host's flavour --------------------

_HOST_FLAVOUR_ON_POSIX_PATH = re.compile(
    r"os\.path\.(normpath|join)\(.*\)\.(lstrip|rstrip|strip|startswith|endswith|split)\(\s*['\"]/['\"]")


def test_the_protocol_path_class_is_real():
    """The two flavours part exactly where an FTP path is joined and stripped."""
    assert posixpath.normpath(posixpath.join("/", "docs/report.pdf")).lstrip("/") == "docs/report.pdf"
    assert ntpath.normpath(ntpath.join("/", "docs/report.pdf")).lstrip("/") == "\\docs\\report.pdf"


def test_no_posix_path_is_normalised_with_the_host_flavour():
    """MUTATION: put os.path back into tests/ftp_stub.py's _path - red."""
    hits = []
    for rel, path in _tracked_python_files():
        if rel == "tests/test_windows_path_hygiene.py":
            continue
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _HOST_FLAVOUR_ON_POSIX_PATH.search(line):
                hits.append(f"{rel}:{no}")
    assert hits == [], "use posixpath for a POSIX protocol path: " + ", ".join(hits)


def test_the_ftp_test_server_maps_paths_under_windows_semantics(tmp_path):
    """The stub's own mapping with os.path swapped for ntpath, as on the Windows leg.
    MUTATION: back to os.path in _path - red (one file named "\\docs\\report.pdf")."""
    import types
    sys.path.insert(0, str(_REPO / "tests"))
    try:
        import ftp_stub
    finally:
        sys.path.remove(str(_REPO / "tests"))
    stub = ftp_stub.FtpStub(tmp_path)
    try:
        # Only the stub's own `os` sees ntpath: pathlib's resolve() reads os.path too, and
        # swapping it process-wide would test the simulation instead of the stub.
        with mock.patch.object(ftp_stub, "os", types.SimpleNamespace(path=ntpath)):
            mapped = stub._path("/", "docs/report.pdf")
        assert mapped == tmp_path.resolve() / "docs" / "report.pdf"
    finally:
        stub._listener.close()


# ---------------------------------------------------------------------------
# 9. A POSIX literal expected back from the str() of a Path
# ---------------------------------------------------------------------------

# The host-flavoured constructors. PurePosixPath renders "/" on every host, so a literal
# asserted back from it is right everywhere and is not this guard's business.
_PATH_CALLS = {"Path", "PurePath", "PosixPath"}


def _posix_literal_expectations(source: str):
    """(line_no, function, literal) per literal a test passes to Path(...) and asserts bare.
    A literal compared with an `.as_posix()` is right on every host and is left alone."""
    hits = []
    for fn in _functions_run_on_windows(ast.parse(source)):
        wrapped, path_args = set(), set()
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and node.args
                    and getattr(node.func, "id", getattr(node.func, "attr", "")) in _PATH_CALLS
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str) and "/" in node.args[0].value):
                wrapped.add(node.args[0].value)
                path_args.add(id(node.args[0]))
        if not wrapped:
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Assert):
                continue
            # The other side of a comparison with an `.as_posix()` is POSIX on every host.
            posix_side = set()
            for cmp in ast.walk(node.test):
                if isinstance(cmp, ast.Compare):
                    sides = [cmp.left, *cmp.comparators]
                    if any(_calls_as_posix(s) for s in sides):
                        posix_side.update(id(c) for s in sides if not _calls_as_posix(s)
                                          for c in ast.walk(s) if isinstance(c, ast.Constant))
            for sub in ast.walk(node.test):
                if (isinstance(sub, ast.Constant) and sub.value in wrapped
                        and id(sub) not in path_args and id(sub) not in posix_side):
                    hits.append((sub.lineno, fn.name, sub.value))
    return hits


def _calls_as_posix(node) -> bool:
    return any(isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
               and c.func.attr == "as_posix" for c in ast.walk(node))


def test_the_posix_literal_class_is_real():
    assert str(PureWindowsPath("/b/old")) == "\\b\\old"
    assert _posix_literal_expectations(
        'def test_x():\n    p = Path("/b/old")\n    assert record(p) == ["/b/old"]\n'
    ) == [(3, "test_x", "/b/old")]
    # The same literal compared through Path is right on every host, and so is one that a
    # PurePosixPath renders.
    assert _posix_literal_expectations(
        'def test_x():\n    p = Path("/b/old")\n    assert record(p) == [str(Path("/b/old"))]\n'
    ) == []
    assert _posix_literal_expectations(
        'def test_x():\n    p = PurePosixPath("/b/old")\n    assert str(p) == "/b/old"\n'
    ) == []
    # Compared with as_posix(), the literal is right on every host; through str() it is not.
    assert _posix_literal_expectations(
        'def test_x():\n    p = Path("/b/old")\n    assert p.as_posix() == "/b/old"\n'
    ) == []
    assert _posix_literal_expectations(
        'def test_x():\n    p = Path("/b/old")\n    assert p.as_posix() == "/b/old"\n'
        '    assert str(p) == "/b/old"\n'
    ) == [(4, "test_x", "/b/old")]


def test_a_skip_on_the_class_covers_its_methods():
    """A test class skipped on Windows, by its decorator or by its own `pytestmark`, does not
    run there. MUTATION: read only the function's own decorators - red."""
    method = '    def test_b(self):\n        p = Path("/b/old")\n        assert record(p) == ["/b/old"]\n'
    decorated = ('import os, pytest\n@pytest.mark.skipif(os.name == "nt", reason="r")\n'
                 'class TestX:\n' + method)
    marked = ('import os, pytest\nclass TestX:\n'
              '    pytestmark = pytest.mark.skipif(os.name == "nt", reason="r")\n' + method)
    plain = 'import os, pytest\nclass TestX:\n' + method
    assert _posix_literal_expectations(decorated) == []
    assert _posix_literal_expectations(marked) == []
    assert _posix_literal_expectations(plain) == [(5, "test_b", "/b/old")]


@pytest.mark.parametrize("condition, exempt", [
    ('os.name == "nt"', True),
    ('__import__("os").name == "nt"', True),
    ('sys.platform == "win32"', True),
    ('sys.platform.startswith("win")', True),
    ('platform.system() != "Linux"', True),
    ('os.name == "nt" or not hasattr(os, "fork")', True),
    ('os.name != "nt"', False),                      # a Windows-only test: it RUNS there
    ('sys.platform != "win32"', False),
    ('shutil.which("git") is None', False),          # not about the platform
    ('os.name == "nt" and shutil.which("x") is None', False),
    ('sys.platform in ("win32", "cygwin")', True),   # membership in a tuple literal
    ('"win" in sys.platform', True),                 # and the other way round
    ('"linux" in sys.platform', False),
    ('sys.platform not in ["win32"]', False),
    ('"sys.platform == \'win32\'"', True),           # a string pytest evaluates, read here
    ('"os.name != \'nt\'"', False),
    ('"not valid python ("', False),
    ('1 in sys.platform', False),                    # not a question this scan can answer
])
def test_a_skip_counts_only_when_its_condition_is_true_on_windows(condition, exempt):
    """MUTATION: count any skipif that names os.name - the Windows-only test is exempted - red."""
    source = ('import os, sys, platform, shutil, pytest\n'
              f'@pytest.mark.skipif({condition}, reason="r")\n'
              'def test_b():\n    p = Path("/b/old")\n    assert record(p) == ["/b/old"]\n')
    assert (_posix_literal_expectations(source) == []) is exempt


@pytest.mark.parametrize("source, exempt", [
    ('import sys, pytest\nif sys.platform == "win32":\n'
     '    pytest.skip("posix only", allow_module_level=True)\n', True),
    ('import pytest\npytest.importorskip("fcntl")\n', True),
    ('import pytest\npytest.importorskip("psutil")\n', False),   # not about Windows
    ('import pytest\nif True:\n    pytest.skip("x", allow_module_level=True)\n', False),
])
def test_a_module_that_skips_itself_on_windows_is_not_scanned(source, exempt):
    """MUTATION: ignore the module's own statements - red."""
    source += 'def test_b():\n    p = Path("/b/old")\n    assert record(p) == ["/b/old"]\n'
    assert (_posix_literal_expectations(source) == []) is exempt


@pytest.mark.parametrize("guard, exempt", [
    ('if os.name == "nt":\n        pytest.skip("posix only")\n', True),
    ('if os.name != "nt":\n        pytest.skip("windows only")\n', False),
    ('if not shutil.which("git"):\n        pytest.skip("needs git")\n', False),
])
def test_a_function_that_skips_itself_on_windows_is_not_scanned(guard, exempt):
    """MUTATION: read only decorators - the first case is reported, red."""
    source = ('import os, shutil, pytest\ndef test_b():\n    ' + guard
              + '    p = Path("/b/old")\n    assert record(p) == ["/b/old"]\n')
    assert (_posix_literal_expectations(source) == []) is exempt


def test_a_skip_on_one_parametrize_case_does_not_exempt_the_others():
    """`pytest.param(..., marks=skipif(os.name == "nt"))` skips that case only; the function
    still runs on Windows for the rest. MUTATION: look for a skipif anywhere inside the
    decorator again - the hit goes unreported, red."""
    source = ('import os, pytest\n'
              '@pytest.mark.parametrize("x", [1, pytest.param(2, marks=pytest.mark.skipif('
              'os.name == "nt", reason="r"))])\n'
              'def test_b(x):\n    p = Path("/b/old")\n    assert record(p) == ["/b/old"]\n')
    assert _posix_literal_expectations(source) == [(5, "test_b", "/b/old")]


def test_a_skip_on_the_first_test_does_not_exempt_the_file():
    """The text before the first `def` holds its decorators too. MUTATION: read the module
    skip from that text again - red, the second test's hit goes unreported."""
    source = ('import os, pytest\n'
              '@pytest.mark.skipif(os.name == "nt", reason="posix only")\n'
              'def test_a():\n    pass\n'
              'def test_b():\n    p = Path("/b/old")\n    assert record(p) == ["/b/old"]\n')
    assert _posix_literal_expectations(source) == [(7, "test_b", "/b/old")]
    whole = ('import os, pytest\n'
             'pytestmark = pytest.mark.skipif(os.name == "nt", reason="posix only")\n') + source
    assert _posix_literal_expectations(whole) == []


def test_no_test_expects_a_posix_literal_back_from_a_path():
    """MUTATION: assert `["/b/old"]` in tests/test_backup_schedule.py again - red."""
    hits = []
    for rel, path in _tracked_python_files(roots=("tests/",)):
        for no, fn, literal in _posix_literal_expectations(path.read_text(encoding="utf-8")):
            hits.append(f"{rel}:{no} {fn} {literal!r}")
    assert not hits, (
        "str() of a Path renders with the host's separator; compare with str(Path(...)) "
        "of the literal, or with .as_posix() if the record is meant to be POSIX:\n  "
        + "\n  ".join(hits)
    )
