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

This scan knows one spelling, a `"...\\n..." in` needle. The class has more: a
MULTILINE pattern ending in `$` (the Windows leg found that one in
test_gpu_child_env, where `$` sat before the `\\r`), a `.split("\\n")`, an
`.index(")\\n")`. Those are caught at run time instead: scripts/hostile_env.py's
CRLF CHECKOUT axis hands every tracked text file back with CRLF through
`Path.read_bytes`, as a Windows checkout does, so any newline-sensitive reading
of unnormalized bytes fails there whatever its spelling.

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

## 10. A program is looked up by its name, never by a path built in place.

`shutil.which(os.path.join(folder, "nvcc"))` names the program with a directory part.
On Windows, Python 3.10 and 3.11 check such a command exactly as written (their
`shutil.which` returns early: `if os.path.dirname(cmd): if _access_check(cmd, mode):
return cmd`), without trying the PATHEXT extensions, so the `nvcc.exe` on disk is never
found. Python 3.12 splits off the folder and applies PATHEXT, and on Linux and macOS
there is no extension to miss, so only two of the eight test jobs could see it. The
Windows legs on 3.10 and 3.11 found exactly this in `vllm_server.flashinfer_buildable`
while 3.12, 3.13, macOS and Ubuntu were green. The rule: the name goes first and the
folder goes in as `path=`, `shutil.which("nvcc", path=folder)`, which every version
resolves with PATHEXT. The scan below refuses a lookup whose command is a path built
in place (a join, a `/` on a Path, a literal or f-string with a separator).

## 11. A node script in a test never searches a source it read itself for a newline.

Several web tests run node on the app's real TypeScript. A script that reads a tracked
source with `fs.readFileSync` gets the checkout's own line ends, and on a Windows
checkout (core.autocrlf) those are `\r\n`. A needle with a `\n` in it, such as the
closing-brace search `src.indexOf('\n}\n', i)` that cuts one component out of a file,
then finds nothing: `-1 + 3` made the slice end at 2 and the cut component empty, so the
test failed on the Windows leg only. Section 2's scan and hostile_env.py's CRLF axis
cannot see it: both work on Python's `Path.read_bytes`, and node reads the file from
disk, where a Linux checkout holds LF. Measured when this was added: 12 test modules run
node on a source they read, one of them searched it for a newline. The rule: cut or
search in Python after the LF normalization and hand node the result, or normalize in
the script (`.replace(/\r\n/g, '\n')`) before the first newline needle.

Sections 12 to 24 hold the same shape for the other two runners as well: a spelling
that this workstation runs one way and the Ubuntu, macOS or Windows runner another.
Each was a red CI job while every local gate was green.

## 12. A shell redirection never names a file descriptor of two digits.
dash, /bin/sh on Debian and Ubuntu, reads one digit in `<&n` and `>&n`: `read x <&12`
is "Syntax error: Bad fd number" there, and a pipe's number is 10 or more in any process
with a few files open. Here /bin/sh is bash, which takes any number. The Ubuntu jobs
failed on a test that interpolated a pipe into `/bin/sh -c f"read x <&{fd}"`. The rule:
pass the descriptor as stdin, or name it as `/proc/self/fd/<n>` (vaf/core/folder_mount.py).

## 13. A test does not rename argv[0] of the Python it starts.
`Popen([title, ...], executable=sys.executable)` sets the process title on Linux. The
python.org framework build on macOS starts through a launcher that executes the real
interpreter again with its own argv[0], so the title is gone before anyone reads the
process table: the running-app guard tests found no fake server on the macOS job. A test
that needs a retitled process starts a program that does not execute itself again
(`/bin/sleep` with the title as argv[0]).

## 14. A test that reads OSC 8 links builds its console with `legacy_windows=False`.
Rich decides at construction whether the console is a legacy Windows one, by asking the
real console for virtual-terminal mode; on the Windows runner there is none, and a legacy
console writes no hyperlink at all. Every link assertion of the terminal-text suites
failed there (26 tests), while the links were written as before on any terminal with VT.

## 15. Bytes are compared with the Windows line end in mind.
A text-mode write (`write_text`, a child's stderr) ends a line with `\\r\\n` on Windows.
A test that reads such a file back as bytes and compares it with `b"...\\n"`, or scans a
child's captured bytes for control characters and allows LF but not CR, fails there for a
reason unrelated to its subject. Write the fixture with `write_bytes`, and let a control
scan take `\\r\\n` as the line end it is.

## 16. A test that removes a git repository it made can remove a read-only file.
git writes its object files read-only. Linux removes them with the folder's write bit
alone; Windows refuses to delete a read-only file, so `shutil.rmtree` of a repository a
test committed into raised "Access is denied" on the Windows job (eight results, four tests).
Pass an error handler that clears the bit (tests/test_code_audit_folder_pinned.py).

## 17. A link helper makes a link of its target's own kind.
Windows has file links and directory links; `os.symlink(target, link,
target_is_directory=True)` to a file makes a link that cannot be opened as a file.
POSIX ignores the flag. A helper that passes `True` for whatever target it is handed
made `CLAUDE.md -> AGENTS.md` unreadable on the Windows job only.

## 18. A needle is never a prefix of the runner's own temporary folder.
A test that records the paths a tool touched and searches them for `C:\\Users` (a share
spelling it handed in) finds every path of its own `tmp_path` on the Windows runner, whose
temporary folder is under the runner account's profile in `C:\\Users`; on macOS it is
under `/private/var`.

## 19. An elapsed time measured with `monotonic()` is not held to the exact sleep.
`time.monotonic()` ticks at about 15.6 ms on Windows, so a real `sleep(0.05)` can measure
0.047 s: the fake-sleep guard's own test failed there. A lower bound on a real wait is
measured with `time.perf_counter()`, or kept well below the sleep.

## 20. A Textual test waits for the widget it queries, not only for its parent.
`len(list_view.children) >= 2` is true as soon as the items are mounted, before their own
children are; a `children[0].query_one("Static")` right after it found nothing on the
slower Windows runner. The settle predicate names the query itself.

## 21. A shorthand IPv4 address is handed to a resolver only where it takes one.
glibc's getaddrinfo reads `127.1` as 127.0.0.1; Windows' refuses it with error 11001,
before any connect is made. A test that loops create_connection over such spellings asks
first (`_resolver_takes`) whether this host's resolver accepts the spelling.

## 22. A filesystem stub in a test matches a path by its components, never by a separator-bearing piece of its str().
A test that stands in for `os.rename` and fails only the rename it means (`str(dst).endswith("/de")`)
never fires on Windows: the code under test renames Path objects, whose str() ends in `\\de`
there. The speech container's swap tests then took the happy path and reported `True is False`.
Match by `os.path.basename(...)`, `Path(p).name` or `Path(p).parts`, or compare with str() of a
Path. The scan reads stubs handed to `setattr` on `os`, `os.path`, `shutil`, `Path` or `pathlib`
as a nested function or a lambda; it does not follow a stub defined elsewhere and passed in, a
module patched through `importlib`, or a str() held in a name first (`t = str(d)`, then
`t.endswith("/de")`). Deliberately, a needle held in a name (`str(p).endswith(ext)`) is read as
unreadable and reported even when the name holds `".txt"`: the scan does not track values, and
the allowlist takes such a site with its reason.

## 23. A test never makes two wall-clock readings an order.
On Windows before Python 3.13, `time.time()` and `time.time_ns()` tick every 15.625 ms,
so readings taken in a row are usually equal. A test that named files
`f"{time.time_ns()}_bad.json"` and `f"{time.time_ns() + 1}_list.json"` and expected them
to sort before a third file named from a later reading found the third one sorted
between them on the Windows job. A test that needs an order spells it with fixed names.

## 24. A claim in `vaf/` is an exclusive create, never a rename.
A rename moves a file; on Windows it is no mutex. MoveFileEx opens the source by name and
renames by handle, so two consumers that opened it before the first rename both own it.
The A2A ticket claim let three peers join on one invitation on the Windows job, and the
WhatsApp request claim, written later on the same belief, lost send requests there
without a trace. A function whose name says claim and that moves a file
(`os.rename`, `os.replace`, a one-argument `.rename(` or `.replace(`) names `O_EXCL`
in the same function: the claim itself is `os.open(..., O_CREAT | O_EXCL)`.
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
_CARRIES_ENVIRON = {"child_env",       # vaf/core/node_runtime.py
                    "gpu_child_env"}   # vaf/core/gpu_detection.py


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
    comes back, SystemRoot included on Windows. MUTATION: return only PATH from child_env - red;
    let gpu_child_env drop anything besides the hide's own recorded names - red."""
    from vaf.core import gpu_detection, node_runtime
    assert _CARRIES_ENVIRON == {"child_env", "gpu_child_env"}, \
        "a new name here needs its own check below"
    env = node_runtime.child_env(sys.executable)
    assert {k: v for k, v in env.items() if k != "PATH"} == {
        k: v for k, v in os.environ.items() if k != "PATH"}
    # gpu_child_env drops only the variables the GPU hide set itself (and its record).
    hidden = {n for n in os.environ.get(gpu_detection.HIDE_RECORD, "").split(",") if n}
    hidden.add(gpu_detection.HIDE_RECORD)
    assert gpu_detection.gpu_child_env() == {
        k: v for k, v in os.environ.items() if k not in hidden}
    base = {"SYSTEMROOT": "C:\\Windows", "PATH": "x", "CUDA_VISIBLE_DEVICES": "",
            gpu_detection.HIDE_RECORD: "CUDA_VISIBLE_DEVICES"}
    assert gpu_detection.gpu_child_env(base) == {"SYSTEMROOT": "C:\\Windows", "PATH": "x"}


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


# -- 10. a program looked up by a path built in place ------------------------------

_PATH_BUILDERS = {"join", "Path", "PurePath", "PureWindowsPath", "PurePosixPath"}


def _built_path(node) -> bool:
    """True for a command argument that is a path assembled right there."""
    if isinstance(node, ast.Call):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name in _PATH_BUILDERS:
            return True
        if name in ("str", "fspath") and node.args:
            return _built_path(node.args[0])
        return False
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return True
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return "/" in node.value or "\\" in node.value
    if isinstance(node, ast.JoinedStr):
        return any(isinstance(v, ast.Constant) and ("/" in v.value or "\\" in v.value)
                   for v in node.values)
    return False


def _which_of_a_built_path(source: str):
    hits = []
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "which" and node.args and _built_path(node.args[0])):
            hits.append(node.lineno)
    return hits


def test_the_which_class_is_real():
    """The scan tells the two spellings apart: the joined path is refused, the name with
    `path=` passes, and so do a bare name and a variable holding a found program."""
    assert _which_of_a_built_path('shutil.which(os.path.join(home, "bin", "nvcc"))') == [1]
    assert _which_of_a_built_path('shutil.which(str(Path(home) / "bin" / "nvcc"))') == [1]
    assert _which_of_a_built_path('shutil.which(f"{home}/bin/nvcc")') == [1]
    assert _which_of_a_built_path('shutil.which("nvcc", path=os.path.join(home, "bin"))') == []
    assert _which_of_a_built_path('shutil.which("ninja", path=path)') == []
    assert _which_of_a_built_path("shutil.which(found)") == []


def test_no_program_is_looked_up_by_a_built_path():
    """MUTATION: put `shutil.which(os.path.join(cuda_home, "bin", "nvcc"))` back into
    vaf/core/vllm_server.py's flashinfer_buildable - red."""
    hits = []
    for rel, path in _tracked_python_files():
        if rel == "tests/test_windows_path_hygiene.py":
            continue
        try:
            found = _which_of_a_built_path(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        hits += [f"{rel}:{no}" for no in found]
    assert hits == [], ("look the program up by its name with the folder as path=, "
                        "shutil.which(name, path=folder): " + ", ".join(hits))


# ---------------------------------------------------------------------------
# 11. A node script's newline needle over a source it read from disk
# ---------------------------------------------------------------------------

# A search call whose string literal argument has a `\n` escape in it; group 1 is the
# name the call is made on, when it is made on a name.
_JS_NEWLINE_NEEDLE = re.compile(
    r"(?:([A-Za-z_$][\w$]*)\s*)?\.(?:indexOf|lastIndexOf|includes|startsWith|endsWith|split|search|match)"
    r"\(\s*(['\"`])(?:(?!\2).)*?\\n")
# A read of a file from disk; stdin (`readFileSync(0, ...)`, spaced or not) carries what
# Python sent. The whitespace sits inside the exclusion: outside it, `\s*` matches nothing
# and the lookahead then sees ` 0`.
_JS_READS_A_FILE = re.compile(r"readFileSync\((?!\s*0\s*[,)])")
# `JSON.parse(fs.readFileSync(` - a catalogue read, indifferent to CRLF.
_JS_FEEDS_JSON_PARSE = re.compile(r"JSON\.parse\(\s*(?:\w+\.)?$")
# Line ends normalized in the expression itself.
_JS_NORMALIZES = re.compile(r"replace\(\s*/\\r\\n/g|split\(\s*/\\r\?\\n/")
# `name = value` (with or without a declaration keyword), not `==` or `=>`.
_JS_ASSIGNMENT = re.compile(r"^\s*(?:(?:const|let|var)\s+)?([A-Za-z_$][\w$]*)\s*=(?![=>])(.*)$", re.S)
_JS_NAME = re.compile(r"[A-Za-z_$][\w$]*")


def _js_disk_read(text: str) -> bool:
    return any(not _JS_FEEDS_JSON_PARSE.search(text[max(0, m.start() - 40):m.start()])
               for m in _JS_READS_A_FILE.finditer(text))


def _node_newline_needles(source: str):
    """Lines of string constants (node scripts) holding a `\\n` needle over text read from
    disk whose line ends were not normalized BEFORE the search.

    The module's string constants are read in source order and statement by statement,
    because a test builds one script from several of them (a prelude that reads, a scenario
    that searches). A name holds unnormalized disk text from a read that does not normalize
    in the same expression, or from an expression over such a name; an assignment that
    normalizes, or that holds nothing read from disk, clears it. A read that only feeds
    `JSON.parse` is indifferent to CRLF and does not count. A normalization elsewhere in the
    module, or after the search, exempts nothing."""
    constants = sorted((node.lineno, node.col_offset, node.value) for node in ast.walk(ast.parse(source))
                       if isinstance(node, ast.Constant) and isinstance(node.value, str))
    tainted, hits = set(), set()
    for lineno, _, text in constants:
        for statement in re.split(r"[;\n]", text):
            for needle in _JS_NEWLINE_NEEDLE.finditer(statement):
                before = statement[:needle.start()]
                chained = _js_disk_read(before) and not _JS_NORMALIZES.search(before)
                if needle.group(1) in tainted or chained:
                    hits.add(lineno)
            assignment = _JS_ASSIGNMENT.match(statement)
            if not assignment:
                continue
            name, value = assignment.groups()
            from_disk = _js_disk_read(value) or bool(set(_JS_NAME.findall(value)) & tainted)
            if from_disk and not _JS_NORMALIZES.search(value):
                tainted.add(name)
            else:
                tainted.discard(name)
    return sorted(hits)


def test_the_node_newline_class_is_real():
    """The scan tells the spellings apart: a needle over a file the script read is
    refused; normalized first, over stdin only, or over a JSON read, it passes."""
    cut = "const i = src.indexOf('function X('), j = src.indexOf('\\n}\\n', i) + 3;"
    from_disk = "const src = fs.readFileSync(path.join(W, 'components', 'X.tsx'), 'utf8');"
    assert _node_newline_needles(f"A = {from_disk!r}\nB = {cut!r}\n") == [2]
    normalized = from_disk.replace("'utf8');", "'utf8').replace(/\\r\\n/g, '\\n');")
    assert _node_newline_needles(f"A = {normalized!r}\nB = {cut!r}\n") == []
    for spelling in ("fs.readFileSync(0, 'utf8')", "fs.readFileSync( 0, 'utf8')", "fs.readFileSync(0)"):
        stdin = f"const src = {spelling};"
        assert _node_newline_needles(f"A = {stdin!r}\nB = {cut!r}\n") == [], spelling
    # Normalization counts only for the text searched, and only before the search: an
    # unrelated script that normalizes its own read exempts nothing, and neither does a
    # normalization after the needle.
    other = "const doc = fs.readFileSync(path.join(W, 'docs', 'Y.md'), 'utf8').replace(/\\r\\n/g, '\\n');"
    assert _node_newline_needles(f"A = {other!r}\nB = {from_disk!r}\nC = {cut!r}\n") == [3]
    late = "src = src.replace(/\\r\\n/g, '\\n');"
    assert _node_newline_needles(f"A = {from_disk!r}\nB = {cut!r}\nC = {late!r}\n") == [2]
    assert _node_newline_needles(f"A = {from_disk!r}\nB = {late!r}\nC = {cut!r}\n") == []
    # A later read re-taints a name an earlier script had normalized.
    assert _node_newline_needles(f"A = {normalized!r}\nB = {from_disk!r}\nC = {cut!r}\n") == [3]
    # A search chained straight onto the read, and one over a slice of the read.
    chained = "const n = fs.readFileSync(path.join(W, 'X.tsx'), 'utf8').split('\\n').length;"
    assert _node_newline_needles(f"A = {chained!r}\n") == [1]
    sliced = "const body = src.slice(10); const k = body.indexOf('\\n}');"
    assert _node_newline_needles(f"A = {from_disk!r}\nB = {sliced!r}\n") == [2]
    catalogue = "const m = JSON.parse(fs.readFileSync(path.join(W, 'messages', 'en.json'), 'utf8'));"
    assert _node_newline_needles(f"A = {catalogue!r}\nB = {cut!r}\n") == []
    # What a Windows checkout does to the cut, measured the way node reads it.
    crlf = "const before = 0;\r\nfunction X() {\r\n  return 1;\r\n}\r\nconst after = 2;\r\n"
    start = crlf.find("function X(")
    assert crlf.find("\n}\n", start) == -1
    assert crlf[start:crlf.find("\n}\n", start) + 3] == ""


def test_no_node_script_searches_a_source_it_read_for_a_newline():
    """MUTATION: put the node-side cut back into tests/test_user_access_picker_web.py's
    `_SECTION` (`fs.readFileSync(... 'SettingsModal.tsx' ...)` and `indexOf('\\n}\\n', i)`)
    - red."""
    hits = []
    for rel, path in _tracked_python_files(("tests/",)):
        if rel == "tests/test_windows_path_hygiene.py":
            continue
        try:
            found = _node_newline_needles(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        hits += [f"{rel}:{no}" for no in found]
    assert hits == [], (
        "a node script searches a source it read from disk for a newline; on a Windows "
        "checkout the file holds \\r\\n and the needle finds nothing. Cut it in Python after "
        "the LF normalization and pass the result in, or normalize in the script first: "
        + ", ".join(hits))


# ── 12 to 24: one runner's reading of a spelling this workstation reads another way ──────

_SELF = "tests/test_windows_path_hygiene.py"


def _test_trees(roots=("tests/",)):
    """(rel, tree, source) of every tracked module under `roots` but this one."""
    for rel, path in _tracked_python_files(roots):
        if rel == _SELF:
            continue
        source = path.read_text(encoding="utf-8", errors="ignore")
        try:
            yield rel, ast.parse(source), source
        except SyntaxError:
            continue


def _call_name(call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else ""


def _keyword(call, name):
    return next((k.value for k in call.keywords if k.arg == name), None)


# 12 -- a two-digit file descriptor in a shell redirection

_FD_REDIRECT_LITERAL = re.compile(r"[<>]&\s*\d{2,}")
# Programs that read their `-c` argument as a shell script.
_SHELLS = {"sh", "bash", "dash", "zsh", "ksh", "mksh", "ash"}
# Calls that hand their command to /bin/sh, with the module that binds them and the keyword
# that names the command.
_SHELL_CALLS = {("os", "system"): "command", ("os", "popen"): "cmd",
                ("subprocess", "getoutput"): "cmd", ("subprocess", "getstatusoutput"): "cmd",
                ("asyncio", "create_subprocess_shell"): "cmd"}
# Calls that start a program from an argv: the spawners, and helpers that build or pass one.
_ARGV_CALLS = _SPAWNERS | {"spawn", "execv", "execvp"}
# Calls whose positional arguments are the argv itself, with how many arguments come before
# it: none for `create_subprocess_exec("sh", "-c", ...)`, the path for `os.execlp("sh", "sh",
# "-c", ...)`, the mode and the path for `os.spawnl(os.P_WAIT, "/bin/sh", "sh", "-c", ...)`.
_VARIADIC_CALLS = {"create_subprocess_exec": 0, "execl": 1, "execlp": 1, "execle": 1,
                   "execlpe": 1, "spawnl": 2, "spawnlp": 2, "spawnle": 2, "spawnlpe": 2}
# Programs that run the rest of their argv as a command, so a shell may follow them.
_WRAPPERS = {"env", "sudo", "nice", "timeout", "setsid", "taskset", "stdbuf", "nohup", "bwrap",
             "unshare", "nsenter", "firejail", "docker", "podman", "flatpak-spawn", "chroot",
             "ionice", "xvfb-run"}
# Calls that take the executable and the argv apart, with the executable's position: the
# argv's first item only names the program (`os.spawnv(os.P_WAIT, "/bin/sh", ["sh", ...])`).
_VECTOR_CALLS = {"execv": 0, "execvp": 0, "execve": 0, "execvpe": 0,
                 "spawnv": 1, "spawnvp": 1, "spawnve": 1, "spawnvpe": 1}
# A class body is a namespace of its own: its names are not the enclosing scope's, and the
# functions and comprehensions in it see the enclosing scope, not the class's names.
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
_SCOPES = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda,
           ast.ClassDef) + _COMPREHENSIONS


def _scope_parts(node) -> tuple:
    """(what the defining scope evaluates, what the node's own scope holds) for a scope node:
    a class's bases, keywords and decorators, a function's decorators and defaults and a
    comprehension's first iterable belong to the scope around it."""
    if isinstance(node, ast.ClassDef):
        return node.bases + [k.value for k in node.keywords] + node.decorator_list, node.body
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        a = node.args
        defaults = a.defaults + [d for d in a.kw_defaults if d is not None]
        if isinstance(node, ast.Lambda):
            return defaults, [node.body]
        return node.decorator_list + defaults, node.body
    if isinstance(node, _COMPREHENSIONS):
        first, rest = node.generators[0], node.generators[1:]
        results = [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
        return [first.iter], [first.target] + first.ifs + rest + results
    return [], list(ast.iter_child_nodes(node))


def _imports(tree) -> dict:
    """Local name -> (module, attribute) for what the module imports: `import os as _os` maps
    `_os` to ("os", None), `import os.path` binds `os` to ("os", None), `import os.path as p`
    binds `p` to ("os.path", None), `from os import system` maps `system` to ("os",
    "system")."""
    names: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    names[alias.asname] = (alias.name, None)
                else:
                    top = alias.name.split(".")[0]
                    names[top] = (top, None)
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                names[alias.asname or alias.name] = (node.module, alias.name)
    return names


def _scope_nodes(scope):
    """The nodes of one scope, without those of a scope nested in it, but with the parts of
    a nested scope this one evaluates (_scope_parts: a class's bases, a function's defaults)."""
    stack = list(_scope_parts(scope)[1])
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, _SCOPES):
            stack.extend(_scope_parts(node)[0])
        else:
            stack.extend(ast.iter_child_nodes(node))


def _comprehension_walrus_names(comp) -> list:
    """The names a comprehension's `:=` bind in the scope around it: through nested
    comprehensions too, never inside a lambda's body, which is a scope of its own, but in a
    lambda's defaults, which the comprehension evaluates."""
    found = []
    stack = [comp]
    while stack:
        node = stack.pop()
        if isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
            found.append(node.target.id)
        if isinstance(node, ast.Lambda):
            stack.extend(_scope_parts(node)[0])
        else:
            stack.extend(ast.iter_child_nodes(node))
    return found


def _declared(tree) -> set:
    """Names some scope declares `global` or `nonlocal`: any function may rebind them."""
    return {name for node in ast.walk(tree) if isinstance(node, (ast.Global, ast.Nonlocal))
            for name in node.names}


def _bindings(scope, outer: dict, declared: set = frozenset()) -> dict:
    """Name -> its value in this scope: a name bound exactly once here, by a plain assignment
    to it alone (`script = "..."`). Any other binding leaves it unknown: a second one, `+=`,
    an annotation, a `for` or `with ... as` target, `:=`, unpacking, `del`, an import, a `def`
    or `class`, `except ... as`, a parameter of this scope (its value comes from the caller),
    and a name declared `global` or `nonlocal` anywhere. A comprehension and a class body are
    scopes of their own (_SCOPES), so what they bind stays there, and only the class's own name
    binds here. A name not bound here keeps the outer scope's binding."""
    count: dict = {}
    plain: dict = {}

    def bind(name):
        count[name] = count.get(name, 0) + 1

    for node in _scope_nodes(scope):
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            bind(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bind(node.name)
        elif isinstance(node, _COMPREHENSIONS):
            # `:=` in a comprehension binds in the scope around it (PEP 572)
            for name in _comprehension_walrus_names(node):
                bind(name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bind(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bind(alias.asname or alias.name.split(".")[0])
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            plain[node.targets[0].id] = node.value
    names = dict(outer)
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        args = scope.args
        for arg in args.posonlyargs + args.args + args.kwonlyargs + [args.vararg, args.kwarg]:
            if arg is not None:
                names.pop(arg.arg, None)
    for name, times in count.items():
        if times == 1 and name in plain and name not in declared:
            names[name] = plain[name]
        else:
            names.pop(name, None)
    return names


def _resolved(node, names: dict, depth: int = 0):
    """`node`, or the one value its name was assigned."""
    while isinstance(node, ast.Name) and node.id in names and depth < 3:
        node = names[node.id]
        depth += 1
    return node


def _names_a_shell(node, names: dict) -> bool:
    node = _resolved(node, names)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.rsplit("/", 1)[-1] in _SHELLS
    return isinstance(node, ast.Name) and node.id in _SHELLS


def _argv_script(elts, names: dict):
    """The script of an argv: the word after `-c` (or a combined flag such as `-ec`) when the
    program is a shell, or a wrapper that runs one (`env -i bash -c ...`); None at the option
    terminator (`--`, `-`), where the next word is a script file, and when the program is no
    shell (`["echo", "bash", "-c", ...]` only prints)."""
    if not elts:
        return None
    first = _resolved(elts[0], names)
    if not _names_a_shell(first, names):
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)
                and first.value.rsplit("/", 1)[-1] in _WRAPPERS):
            return None
    shell_at = next((k for k, e in enumerate(elts) if _names_a_shell(e, names)), None)
    if shell_at is None:
        return None
    for k in range(shell_at + 1, len(elts)):
        flag = _resolved(elts[k], names)
        if not (isinstance(flag, ast.Constant) and isinstance(flag.value, str)):
            return None
        if flag.value in ("--", "-") or not flag.value.startswith("-"):
            return None
        if not flag.value.startswith("--") and "c" in flag.value:
            return elts[k + 1] if k + 1 < len(elts) else None
    return None


def _argv_elements(arg, names: dict, depth: int = 0) -> list:
    """The items of the argv an argument is built from, in order: a list or tuple literal,
    also put together with `+` (`["sh", "-c"] + [...]`); an operand that is no literal stands
    as an unknown item."""
    arg = _resolved(arg, names)
    if isinstance(arg, (ast.List, ast.Tuple)):
        return list(arg.elts)
    if isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Add) and depth < 8:
        return (_argv_elements(arg.left, names, depth + 1)
                + _argv_elements(arg.right, names, depth + 1))
    return [ast.Name(id="?")]


def _call_target(node, imports: dict):
    """(module, function) a call names: `os.system`, `_os.system` after `import os as _os`,
    `system` after `from os import system`; (None, name) for anything else."""
    func = node.func
    if isinstance(func, ast.Attribute):
        if isinstance(func.value, ast.Name):
            module = imports.get(func.value.id, (func.value.id, None))
            return (module[0] if module[1] is None else None), func.attr
        return None, func.attr
    if isinstance(func, ast.Name):
        bound = imports.get(func.id)
        if bound and bound[1] is not None:
            return bound[0], bound[1]
        return None, func.id
    return None, ""


def _shell_scripts(tree) -> list:
    """(script expression, the bindings of its scope) for each text a test hands to a shell:
    - the word after `-c` in an argv whose program is a shell (or a wrapper that runs one), by
      a literal or a variable assigned one in the same scope, handed to a spawner, an argv
      helper (`guard_argv(..., ["/bin/sh", "-c", f"..."])`) or, as separate arguments, to
      `create_subprocess_exec`; a list that is only printed or compared is no argv;
    - the command of `os.system`, `os.popen`, `subprocess.getoutput`/`getstatusoutput` and
      `asyncio.create_subprocess_shell`, also imported under another name, and of a spawner
      called with `shell=True` (of a sequence there, only its first item, which is what the
      shell runs).
    Command strings a test only classifies or prints are no shell's text."""
    imports = _imports(tree)
    declared = _declared(tree)
    scripts = []
    pending = [(tree, {})]
    while pending:
        scope, outer = pending.pop()
        names = _bindings(scope, outer, declared)
        for node in _scope_nodes(scope):
            if isinstance(node, _SCOPES):
                # a function in a class body sees the scope around the class
                pending.append((node, outer if isinstance(scope, ast.ClassDef) else names))
            if not isinstance(node, ast.Call):
                continue
            module, name = _call_target(node, imports)
            if (module, name) in _SHELL_CALLS:
                first = node.args[0] if node.args else _keyword(node, _SHELL_CALLS[(module, name)])
                if first is not None:
                    scripts.append((first, names))
                continue
            shell = _keyword(node, "shell")
            if name in _SPAWNERS and isinstance(shell, ast.Constant) and shell.value is True:
                first = node.args[0] if node.args else _keyword(node, "args")
                if first is not None:
                    # A sequence runs its first item; a command put together with `+` stays
                    # whole (`"echo x >&" + "12"`), and an empty list runs nothing.
                    items = _argv_elements(first, names)
                    if items and not (isinstance(items[0], ast.Name) and items[0].id == "?"):
                        first = items[0]
                    scripts.append((first, names))
                continue
            if name in _VARIADIC_CALLS:
                # The executable (the path) stands in for argv[0], which only names it.
                drop, args = _VARIADIC_CALLS[name], list(node.args)
                argv = args if not drop else args[drop - 1:drop] + args[drop + 1:]
                script = _argv_script(argv, names)
                if script is not None:
                    scripts.append((script, names))
                continue
            at = _VECTOR_CALLS.get(name)
            if at is not None:
                # the executable runs, the argv's first item only names it; both may be
                # keywords (`os.spawnvp(mode, file, args=[...])`), and an executable that is
                # no literal leaves the argv as it stands
                exe = node.args[at] if len(node.args) > at else (
                    _keyword(node, "path") or _keyword(node, "file"))
                vector = node.args[at + 1] if len(node.args) > at + 1 else _keyword(node, "args")
                if vector is not None:
                    argv = _argv_elements(vector, names)
                    known = _resolved(exe, names) if exe is not None else None
                    if isinstance(known, ast.Constant) and isinstance(known.value, str):
                        argv = [exe] + argv[1:]
                    script = _argv_script(argv, names)
                    if script is not None:
                        scripts.append((script, names))
                    continue
            if name in _ARGV_CALLS or name.endswith("argv"):
                for arg in list(node.args) + [k.value for k in node.keywords]:
                    script = _argv_script(_argv_elements(arg, names), names)
                    if script is not None:
                        scripts.append((script, names))
    return scripts


def _script_pieces(node, names: dict, depth: int = 0) -> list:
    """The script text in order, as ("text", str, line) and ("value", None, line) pieces:
    literals and f-string parts, through `+` and a variable bound in the scope; an
    interpolated value is a value piece with no literal of its own (`f"echo {len('x >&12')}"`
    runs `echo 7`), and anything else is a value too."""
    node = _resolved(node, names)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [("text", node.value, node.lineno)]
    if isinstance(node, ast.JoinedStr):
        return [("text", v.value, node.lineno) if isinstance(v, ast.Constant)
                else ("value", None, node.lineno) for v in node.values]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add) and depth < 8:
        return (_script_pieces(node.left, names, depth + 1)
                + _script_pieces(node.right, names, depth + 1))
    return [("value", None, getattr(node, "lineno", 0))]


def _fd_redirections(tree) -> list:
    """Lines of a `<&{x}`/`>&{x}` f-string, or a literal `<&12`, in text a shell runs
    (_shell_scripts): dash reads one digit there. Adjacent literal pieces are read together
    (`"echo x >&" + "12"`)."""
    hits = set()
    for script, names in _shell_scripts(tree):
        pieces = _script_pieces(script, names)
        text, line = "", 0
        for kind, value, lineno in pieces + [("value", None, 0)]:
            if kind == "text":
                text, line = text + value, line or lineno
                continue
            if _FD_REDIRECT_LITERAL.search(text):
                hits.add(line)
            if lineno and re.search(r"[<>]&\s*$", text):
                hits.add(line or lineno)        # a descriptor given by a value right after it
            text, line = "", 0
    return sorted(hits)


def test_the_fd_redirection_scanner_reads_both_spellings():
    assert _fd_redirections(ast.parse(
        'subprocess.run(["/bin/sh", "-c", f"read x <&{rr}; ls"])')) == [1]
    assert _fd_redirections(ast.parse('os.system("echo x >&12")')) == [1]
    assert _fd_redirections(ast.parse(
        'subprocess.run("echo x >&12", shell=True)')) == [1]
    assert _fd_redirections(ast.parse('subprocess.run(["bash", "-ec", "cat <&10"])')) == [1]
    assert _fd_redirections(ast.parse('subprocess.run([sh, "-c", "cat <&10"])')) == [1]
    assert _fd_redirections(ast.parse(
        'subprocess.run(["/bin/sh", "-c", "echo x >&2; read y < /proc/self/fd/12"])')) == []


def test_the_fd_redirection_scanner_reads_only_text_a_shell_runs():
    """A command string a test only classifies or prints is no shell's text; the argv of
    tests/test_folder_mount.py as it once was (a two-digit descriptor in the script that
    `/bin/sh -c` runs, inside a helper call) is, and so are a script or a shell held in a
    variable, an argv put together with `+`, `cmd=`, and `create_subprocess_shell`.
    MUTATION: scan every string constant again - red (the first assertion);
    MUTATION: drop the argv branch - red (the folder mount argv)."""
    assert _fd_redirections(ast.parse(
        'ROWS = [("( echo x ) 3>&01 >&3 | bash", "destructive_removal")]\n'
        'classify_command("echo x >&12", profile="host")\n'
        'print(["bash", "-c", "echo x >&12"])\n'
        'Recorder().system("echo x >&12")\n'
        'subprocess.run(["true", "echo x >&12"], shell=True)\n'
        'os.system(f"echo {len(\'x >&12\')}")\n'
        'subprocess.run(["bash", "--", "fixture.sh", "-c", "echo x >&12"])\n'
        'subprocess.run(["bash", "--", "-c", "echo x >&12"])')) == []
    assert _fd_redirections(ast.parse(
        'argv = (folder_mount.namespace_argv(held, a)\n'
        '        + folder_mount.guard_argv(a, held.identity,\n'
        '                                  ["/bin/sh", "-c", f"read x <&{rr}; ls -a {a}"],\n'
        '                                  started_fd=ws))')) == [3]
    assert _fd_redirections(ast.parse(
        'script = f"cat <&{rr}"\n'
        'subprocess.run(["sh", "-c", script])')) == [1]
    assert _fd_redirections(ast.parse(
        'shell = "/bin/sh"\n'
        'subprocess.run([shell, "-c", "cat <&10"])')) == [2]
    assert _fd_redirections(ast.parse(
        'subprocess.run(["sh", "-c"] + ["cat <&10"])')) == [1]
    assert _fd_redirections(ast.parse('subprocess.getoutput(cmd="echo x >&12")')) == [1]
    assert _fd_redirections(ast.parse(
        'asyncio.create_subprocess_shell("echo x >&12")')) == [1]
    assert _fd_redirections(ast.parse(
        'subprocess.run(["echo x >&12", "arg0"], shell=True)')) == [1]


def test_the_fd_redirection_scanner_follows_names_and_scopes():
    """Imports under another name, separate argv arguments, a `shell=True` sequence put
    together with `+`, literal pieces joined with `+`, only the program's own `-c`, and a
    name bound in its own scope.
    MUTATION: read calls by their bare name again - red (`import os as _os`);
    MUTATION: read each argument of `create_subprocess_exec` alone - red;
    MUTATION: check each literal piece alone - red (`"echo x >&" + "12"`);
    MUTATION: take any shell word in an argv for the program - red (`["echo", "bash", ...]`);
    MUTATION: one binding table for the whole module - red (the parameter case)."""
    assert _fd_redirections(ast.parse(
        'import os as _os\n'
        '_os.system("echo x >&12")')) == [2]
    assert _fd_redirections(ast.parse(
        'from os import system\n'
        'system("echo x >&12")')) == [2]
    assert _fd_redirections(ast.parse(
        'asyncio.create_subprocess_exec("sh", "-c", "cat <&10")')) == [1]
    assert _fd_redirections(ast.parse(
        'subprocess.run(["echo x >&12"] + ["arg0"], shell=True)')) == [1]
    assert _fd_redirections(ast.parse('os.system("echo x >&" + "12")')) == [1]
    assert _fd_redirections(ast.parse(
        'subprocess.run(["env", "-i", "bash", "-c", "cat <&10"])')) == [1]
    assert _fd_redirections(ast.parse(
        'os.system("echo x >&" + "2")\n'
        'subprocess.run(["echo", "bash", "-c", "echo x >&12"])\n'
        'def a():\n'
        '    script = "echo x >&12"\n'
        '    return script\n'
        'def b(script):\n'
        '    subprocess.run(["sh", "-c", script])\n'
        'b("true")')) == []
    assert _fd_redirections(ast.parse(
        'script = "echo x >&12"\n'
        'def b(script):\n'
        '    subprocess.run(["sh", "-c", script])')) == []
    assert _fd_redirections(ast.parse(
        'def a():\n'
        '    script = "echo x >&12"\n'
        'def c():\n'
        '    subprocess.run(["sh", "-c", script])')) == []


def test_the_fd_redirection_scanner_resolves_only_a_single_plain_assignment():
    """A name another binding touches is unknown: `+=` adds to it, a loop or `with` rebinds
    it, and so do `:=`, unpacking, an annotation and `global`. A comprehension's variable
    binds nothing outside it.
    MUTATION: count only plain assignments as bindings again - red (the loop, `with`, `:=`,
    unpacking, the annotation);
    MUTATION: ignore `global` - red;
    MUTATION: count a comprehension's variable - red;
    MUTATION: read a class body as part of the module - red (the method of the same name);
    MUTATION: give a method the class's names - red (the method row);
    MUTATION: a comprehension sees the class - red; a class header in the class - red;
    MUTATION: a comprehension's `:=` bound inside it - red (the walrus row);
    MUTATION: walk into a lambda's body from a comprehension - red (the lambda body row);
    MUTATION: stop at a lambda's defaults or at a nested comprehension - red (their rows)."""
    assert _fd_redirections(ast.parse(
        'cmd = "echo x"\n'
        'cmd += " >&12"\n'
        'os.system(cmd)')) == []
    assert _fd_redirections(ast.parse(
        'script = "echo x >&12"\n'
        'def f(items):\n'
        '    for script in items:\n'
        '        subprocess.run(["sh", "-c", script])')) == []
    for rebind in ('    with open(p) as script:\n        pass\n',
                   '    if (script := pick()):\n        pass\n',
                   '    script, other = pick()\n',
                   '    script: str = pick()\n',
                   '    global script\n'):
        assert _fd_redirections(ast.parse(
            'script = "echo x >&12"\n'
            'def f():\n' + rebind +
            '    subprocess.run(["sh", "-c", script])')) == [], rebind
    # the same in one scope: the plain assignment is not the only binding
    assert _fd_redirections(ast.parse(
        'script = "echo x >&12"\n'
        'for script in items:\n'
        '    subprocess.run(["sh", "-c", script])')) == []
    assert _fd_redirections(ast.parse(
        'script = "echo x >&12"\n'
        'names = [script for script in other]\n'
        'subprocess.run(["sh", "-c", script])')) == [1]
    # a method of the same name belongs to its class, not to the module
    assert _fd_redirections(ast.parse(
        'script = "echo x >&12"\n'
        'class A:\n'
        '    def script(self):\n'
        '        pass\n'
        'os.system(script)')) == [1]
    # a class body resolves its own names; a method in it does not see them
    assert _fd_redirections(ast.parse(
        'class A:\n'
        '    script = "echo x >&12"\n'
        '    os.system(script)')) == [2]
    assert _fd_redirections(ast.parse(
        'class A:\n'
        '    script = "echo x >&12"\n'
        '    def f(self):\n'
        '        os.system(script)')) == []
    # a comprehension in a class body sees the module, not the class; its first iterable is
    # evaluated in the class body
    assert _fd_redirections(ast.parse(
        'script = "cat <&10"\n'
        'class A:\n'
        '    script = "true"\n'
        '    results = [os.system(script) for _ in range(1)]')) == [1]
    assert _fd_redirections(ast.parse(
        'class A:\n'
        '    script = "cat <&10"\n'
        '    results = [x for x in os.system(script)]')) == [2]
    # `:=` in a comprehension rebinds the name in the scope around it
    assert _fd_redirections(ast.parse(
        'script = "cat <&10"\n'
        '[(script := "true") for _ in range(1)]\n'
        'os.system(script)')) == []
    # ... also from a nested comprehension and a lambda's default, never from a lambda's body
    assert _fd_redirections(ast.parse(
        'script = "cat <&10"\n'
        '[[(script := "true") for _ in y] for y in range(1)]\n'
        'os.system(script)')) == []
    assert _fd_redirections(ast.parse(
        'script = "cat <&10"\n'
        '[(lambda a=(script := "true"): a) for _ in range(1)]\n'
        'os.system(script)')) == []
    assert _fd_redirections(ast.parse(
        'script = "cat <&10"\n'
        '[(lambda: (script := "true")) for _ in range(1)]\n'
        'os.system(script)')) == [1]
    # a class's bases and a function's decorators are evaluated in the defining scope
    assert _fd_redirections(ast.parse(
        'script = "cat <&10"\n'
        'class A(make_base(os.system(script))):\n'
        '    script = "true"')) == [1]
    assert _fd_redirections(ast.parse(
        'script = "cat <&10"\n'
        'def f(x=os.system(script)):\n'
        '    script = "true"')) == [1]


def test_the_fd_redirection_scanner_reads_every_call_form():
    """`import os.path` binds `os`; a `shell=True` command put together with `+` keeps its
    text, and an empty one is no error; `execl*` and `spawnl*` run their path (after the mode)
    with the argv after argv[0], which only names it, and `spawnv*` take theirs as a list.
    MUTATION: map `import os.path` to `os.path` - red;
    MUTATION: take the first item of a `shell=True` sum always - red;
    MUTATION: read every variadic call's arguments from the first - red (`execlp`);
    MUTATION: take argv[0] for the program instead of the executable - red (`/bin/echo`);
    MUTATION: read a vector call's argv as it stands - red (`spawnv`, `execv` both ways);
    MUTATION: positional arguments only - red (`args=`); a variable executable taken for the
    program - red (`execv(exe, ...)`)."""
    assert _fd_redirections(ast.parse(
        'import os.path\n'
        'os.system("echo x >&12")')) == [2]
    assert _fd_redirections(ast.parse(
        'import os\n'
        'import os.path\n'
        'os.system("echo x >&12")')) == [3]
    assert _fd_redirections(ast.parse(
        'subprocess.run("echo x >&" + "12", shell=True)')) == [1]
    assert _fd_redirections(ast.parse('subprocess.run([], shell=True)')) == []
    assert _fd_redirections(ast.parse('os.execlp("sh", "sh", "-c", "cat <&10")')) == [1]
    assert _fd_redirections(ast.parse('os.execl("/bin/sh", "sh", "-c", "cat <&10")')) == [1]
    assert _fd_redirections(ast.parse(
        'os.execle("/bin/sh", "sh", "-c", "cat <&10", env)')) == [1]
    assert _fd_redirections(ast.parse(
        'os.spawnl(os.P_WAIT, "/bin/sh", "sh", "-c", "cat <&10")')) == [1]
    assert _fd_redirections(ast.parse(
        'os.spawnlp(os.P_WAIT, "sh", "sh", "-c", "cat <&10")')) == [1]
    # the executable runs, argv[0] only names it, also for the vector calls
    for call in ('os.spawnv(os.P_WAIT, "/bin/echo", ["sh", "-c", "echo x >&12"])',
                 'os.execv("/bin/echo", ["sh", "-c", "echo x >&12"])',
                 'os.execve("/bin/echo", ["sh", "-c", "echo x >&12"], env)'):
        assert _fd_redirections(ast.parse(call)) == [], call
    for call in ('os.spawnv(os.P_WAIT, "/bin/sh", ["echo", "-c", "cat <&10"])',
                 'os.execv("/bin/sh", ["echo", "-c", "cat <&10"])',
                 'os.spawnvpe(os.P_WAIT, "sh", ["echo", "-c", "cat <&10"], env)',
                 'os.spawnvp(os.P_WAIT, "sh", args=["echo", "-c", "cat <&10"])',
                 'os.execv(exe, ["sh", "-c", "cat <&10"])',
                 'os.spawnv(os.P_WAIT, exe, ["bash", "-c", "cat <&10"])'):
        assert _fd_redirections(ast.parse(call)) == [1], call
    # the executable runs, argv[0] only names it: `/bin/echo` prints the text
    assert _fd_redirections(ast.parse('os.execl("/bin/echo", "sh", "-c", "echo x >&12")')) == []
    assert _fd_redirections(ast.parse(
        'os.spawnl(os.P_WAIT, "/bin/echo", "sh", "-c", "echo x >&12")')) == []
    assert _fd_redirections(ast.parse('os.execl("/bin/sh", "echo", "-c", "cat <&10")')) == [1]
    for call in ('os.spawnv(os.P_WAIT, "/bin/sh", ["sh", "-c", "cat <&10"])',
                 'os.spawnvp(os.P_WAIT, "sh", ["sh", "-c", "cat <&10"])',
                 'os.spawnvpe(os.P_WAIT, "sh", ["sh", "-c", "cat <&10"], env)'):
        assert _fd_redirections(ast.parse(call)) == [1], call


def test_no_shell_redirection_names_a_two_digit_fd():
    """MUTATION: put back `read x <&{rr}` in tests/test_folder_mount.py - red."""
    hits = [f"{rel}:{no}" for rel, tree, _ in _test_trees(("vaf/", "scripts/", "tests/"))
            for no in _fd_redirections(tree)]
    assert hits == [], ("dash (/bin/sh on Ubuntu) reads one digit in `<&n`/`>&n`; pass the "
                        "descriptor as stdin or as /proc/self/fd/<n>: " + ", ".join(hits))


# 13 -- argv[0] of the Python a test starts

def _retitled_python(tree) -> list:
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and (_call_name(node) in _SPAWNERS
                                           or "spawn" in _call_name(node)):
            exe = _keyword(node, "executable")
            if (isinstance(exe, ast.Attribute) and exe.attr == "executable"
                    and isinstance(exe.value, ast.Name) and exe.value.id == "sys"):
                hits.append(node.lineno)
    return hits


def test_no_test_retitles_the_python_it_starts():
    """MUTATION: spawn the fake server as `[title, "-c", ...]` with
    `executable=sys.executable` again in tests/test_ci_running_app_guard.py - red."""
    hits = [f"{rel}:{no}" for rel, tree, _ in _test_trees() for no in _retitled_python(tree)]
    assert hits == [], ("the macOS framework Python executes itself again and drops a "
                        "retitled argv[0]; start a program that does not (/bin/sleep): "
                        + ", ".join(hits))


# 14 -- OSC 8 assertions and Rich's legacy Windows console

def _consoles_without_legacy_choice(tree) -> list:
    return [node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _call_name(node).endswith("Console")
            and _keyword(node, "legacy_windows") is None]


def test_the_legacy_console_class_is_real():
    import io

    from rich.console import Console
    for legacy, expected in ((True, False), (False, True)):
        console = Console(file=io.StringIO(), force_terminal=True, color_system="truecolor",
                          legacy_windows=legacy)
        console.print("[link=file:///tmp/p]/tmp/p[/link]")
        assert ("\x1b]8;" in console.file.getvalue()) is expected


def test_every_console_of_an_osc8_test_rules_out_the_legacy_console():
    """MUTATION: drop `legacy_windows=False` from `_console` in tests/test_terminal_markup.py
    - red."""
    hits = [f"{rel}:{no}" for rel, tree, source in _test_trees() if "]8;" in source
            for no in _consoles_without_legacy_choice(tree)]
    assert hits == [], ("a test that reads OSC 8 links builds a console that Rich makes a "
                        "legacy Windows one on the Windows runner, which writes no link; pass "
                        "legacy_windows=False: " + ", ".join(hits))


# 15 -- the Windows line end in bytes

# path -> reason a bytes literal with a newline is compared with a read there.
_BYTES_NEWLINE_ALLOWED = {
    "tests/test_writes_do_not_follow_links.py": "the ftp sink under test writes the bytes itself",
}


def _crlf_taken(func) -> bool:
    """The function turns `\\r\\n` into a line end before it scans."""
    return any(isinstance(n, ast.Constant) and n.value in (b"\r\n", "\r\n")
               for n in ast.walk(func))


def _bytes_newline_compares(tree) -> list:
    """A `b"...\\n"` compared with `.read()`/`.read_bytes()`, and a control-byte scan that lets
    LF (10) through but not CR (13), in a function that does not take `\\r\\n` as a line end."""
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or not any(isinstance(op, ast.Eq)
                                                         for op in node.ops):
            continue
        sides = [node.left, *node.comparators]
        literal = any(isinstance(s, ast.Constant) and isinstance(s.value, bytes)
                      and b"\n" in s.value for s in sides)
        read = any(isinstance(s, ast.Call) and _call_name(s) in ("read", "read_bytes")
                   for s in sides)
        if literal and read:
            hits.append(node.lineno)
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)) or _crlf_taken(func):
            continue
        for node in ast.walk(func):
            if not (isinstance(node, ast.Compare)
                    and any(isinstance(op, ast.NotIn) for op in node.ops)):
                continue
            for side in node.comparators:
                if isinstance(side, (ast.Tuple, ast.Set, ast.List)):
                    ints = {e.value for e in side.elts if isinstance(e, ast.Constant)
                            and isinstance(e.value, int)}
                    if 10 in ints and 13 not in ints:
                        hits.append(node.lineno)
    return sorted(set(hits))


def test_the_crlf_bytes_class_is_real(tmp_path):
    target = tmp_path / "f.txt"
    with open(target, "w", encoding="utf-8", newline="\r\n") as f:   # what write_text does there
        f.write("A-OWN\n")
    assert target.read_bytes() != b"A-OWN\n"
    assert _bytes_newline_compares(ast.parse('assert f.read() == b"A\\n"')) == [1]
    assert _bytes_newline_compares(ast.parse(
        "def f(d):\n    return [b for b in d if b not in (9, 10)]\n")) == [2]
    assert _bytes_newline_compares(ast.parse(
        "def f(d):\n    d = d.replace(b'\\r\\n', b'\\n')\n"
        "    return [b for b in d if b not in (9, 10)]\n")) == []


def test_no_bytes_comparison_forgets_the_windows_line_end():
    """MUTATION: compare `f.read() == b"A-OWN\\n"` again in
    tests/test_code_audit_folder_pinned.py - red; drop the `\\r\\n` line end from
    `_unsafe_bytes` in tests/test_terminal_text.py - red."""
    hits = [f"{rel}:{no}" for rel, tree, _ in _test_trees() if rel not in _BYTES_NEWLINE_ALLOWED
            for no in _bytes_newline_compares(tree)]
    assert hits == [], ("a text-mode write ends a line with \\r\\n on Windows; write the fixture "
                        "as bytes, or take \\r\\n as a line end: " + ", ".join(hits))


def test_the_bytes_newline_allowlist_carries_no_dead_entries():
    for rel in _BYTES_NEWLINE_ALLOWED:
        assert _bytes_newline_compares(ast.parse((_REPO / rel).read_text(encoding="utf-8"))), rel


# 16 -- shutil.rmtree of a repository a test committed into

# (path, the removed folder as written) -> why nothing read-only can be in it.
_RMTREE_ALLOWED = {
    ("tests/test_project_git_runner.py", "pack"): "a fresh repository's pack folder is empty",
    ("tests/test_project_git_runner.py", "hooks"): "git writes its sample hooks writable",
}


def _bare_rmtrees(tree, source, rel="") -> list:
    """`shutil.rmtree(<a folder held in a name>)` with no error handler, in a module that
    commits with git. A folder spelled with a fixed part under it (`x / ".vaf"`) is one the
    test knows; a folder held in a name may be the whole repository."""
    if "commit" not in source or "git" not in source:
        return []
    return [node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _call_name(node) == "rmtree" and node.args
            and isinstance(node.args[0], (ast.Name, ast.Attribute))
            and (rel, ast.unparse(node.args[0])) not in _RMTREE_ALLOWED
            and not any(_keyword(node, k) is not None for k in ("onexc", "onerror",
                                                                 "ignore_errors"))]


def test_the_read_only_class_is_real(tmp_path):
    import stat
    target = tmp_path / "obj"
    target.write_bytes(b"x")
    target.chmod(stat.S_IREAD)
    assert not os.access(target, os.W_OK) or os.geteuid() == 0   # what git leaves behind
    assert _bare_rmtrees(ast.parse("shutil.rmtree(p)"), "git commit") == [1]
    assert _bare_rmtrees(ast.parse("shutil.rmtree(p, onexc=h)"), "git commit") == []
    target.chmod(stat.S_IREAD | stat.S_IWRITE)


def test_no_git_test_removes_a_repository_without_a_read_only_handler():
    """MUTATION: `shutil.rmtree(folder)` without the handler in
    tests/test_code_audit_folder_pinned.py - red."""
    hits = [f"{rel}:{no}" for rel, tree, source in _test_trees()
            for no in _bare_rmtrees(tree, source, rel)]
    assert hits == [], ("git's objects are read-only and Windows refuses to delete them; "
                        "pass an onexc/onerror handler that clears the bit: " + ", ".join(hits))


# 17 -- a link helper fixes the link's kind for any target

def _fixed_kind_link_helpers(tree) -> list:
    hits = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        params = {a.arg for a in func.args.args + func.args.kwonlyargs}
        for node in ast.walk(func):
            if not (isinstance(node, ast.Call) and _call_name(node) in ("symlink", "symlink_to")):
                continue
            kind = _keyword(node, "target_is_directory")
            if kind is None and _call_name(node) == "symlink" and len(node.args) > 2:
                kind = node.args[2]
            target = node.args[0] if node.args else None
            if (isinstance(kind, ast.Constant) and kind.value is True
                    and isinstance(target, ast.Name) and target.id in params):
                hits.append(node.lineno)
    return hits


def test_no_link_helper_makes_every_link_a_directory_link():
    """MUTATION: `os.symlink(target, link, target_is_directory=True)` back in
    tests/test_git_and_notes_folder_pinned.py's `_symlink_or_skip` - red."""
    assert _fixed_kind_link_helpers(ast.parse(
        "def f(link, target):\n    os.symlink(target, link, target_is_directory=True)\n")) == [2]
    hits = [f"{rel}:{no}" for rel, tree, _ in _test_trees() for no in _fixed_kind_link_helpers(tree)]
    assert hits == [], ("a directory link to a file cannot be opened as a file on Windows; "
                        "decide the kind from the target: " + ", ".join(hits))


# 18 -- a needle that the runner's temporary folder holds

# The roots themselves, not a folder under them a test names as a seam value.
_RUNNER_TEMP_PREFIXES = {"C:\\Users", "C:\\Users\\", "C:/Users", "\\Users", "AppData",
                         "runneradmin", "/private/var", "/home/runner"}


def _runner_prefix_needles(tree) -> list:
    """A collection of string needles holding a prefix of a CI runner's temporary folder."""
    return [node.lineno for node in ast.walk(tree)
            if isinstance(node, (ast.Tuple, ast.List, ast.Set))
            and any(isinstance(e, ast.Constant) and e.value in _RUNNER_TEMP_PREFIXES
                    for e in node.elts)]


def test_no_needle_matches_the_runners_temporary_folder():
    """MUTATION: `"C:\\\\Users"` back into `_SHARE_MARKS` in tests/test_remote_path_refused.py -
    red."""
    hits = [f"{rel}:{no}" for rel, tree, _ in _test_trees() for no in _runner_prefix_needles(tree)]
    assert hits == [], ("the Windows runner's tmp_path is under its account's profile in C:\\Users and the "
                        "macOS one under /private/var; make the needle the spelling itself: "
                        + ", ".join(hits))


# 19 -- an elapsed monotonic() held to the exact sleep

def _exact_sleep_bounds(tree) -> list:
    hits = []
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef) or not func.name.startswith("test"):
            continue
        body = ast.unparse(func)
        if "monotonic" not in body:
            continue
        slept = {n.args[0].value for n in ast.walk(func)
                 if isinstance(n, ast.Call) and _call_name(n) == "sleep" and n.args
                 and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, float)}
        for node in ast.walk(func):
            if (isinstance(node, ast.Compare) and any(isinstance(o, ast.GtE) for o in node.ops)
                    and any(isinstance(c, ast.Constant) and c.value in slept
                            for c in node.comparators)):
                hits.append(node.lineno)
    return hits


def test_no_monotonic_elapsed_is_held_to_the_exact_sleep():
    """MUTATION: measure `time.monotonic() - start >= 0.05` again in
    tests/test_sleep_patch_hygiene.py - red."""
    hits = [f"{rel}:{no}" for rel, tree, _ in _test_trees() for no in _exact_sleep_bounds(tree)]
    assert hits == [], ("time.monotonic() ticks at about 15.6 ms on Windows; measure a real "
                        "wait with time.perf_counter(): " + ", ".join(hits))


# 20 -- a Textual query into a child that may not be mounted yet

_CHILD_QUERY = re.compile(r"\.children\[-?\d+\]\.query_one\(")


def test_no_textual_test_queries_into_a_child_it_did_not_wait_for():
    """MUTATION: `lv.children[0].query_one("Static")` after a settle on the count, back in
    tests/test_app_link_room.py - red."""
    hits = [f"{rel}:{no}" for rel, _tree, source in _test_trees()
            for no, line in enumerate(source.splitlines(), 1) if _CHILD_QUERY.search(line)]
    assert hits == [], ("a list's items are mounted before their own children; settle on the "
                        "query itself: " + ", ".join(hits))


# 21 -- a shorthand IPv4 spelling handed to the resolver

def _shorthand(text) -> bool:
    import ipaddress
    import socket
    if not isinstance(text, str) or "." not in text or not re.fullmatch(r"[0-9.]+", text):
        return False
    try:
        ipaddress.ip_address(text)
        return False
    except ValueError:
        pass
    try:
        socket.inet_aton(text)
        return True
    except OSError:
        return False


def _unasked_shorthand_dials(tree) -> list:
    hits = []
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef):
            continue
        calls = {_call_name(n) for n in ast.walk(func) if isinstance(n, ast.Call)}
        if not calls & {"create_connection", "getaddrinfo"} or "_resolver_takes" in calls:
            continue
        hits += [n.lineno for n in ast.walk(func)
                 if isinstance(n, ast.Constant) and _shorthand(n.value)]
    return hits


def test_no_shorthand_address_reaches_a_resolver_unasked():
    """MUTATION: loop create_connection over `("127.0.0.1", "localhost", "127.1")` again in
    tests/test_service_port_guard.py - red."""
    assert _unasked_shorthand_dials(ast.parse(
        "def t():\n    for h in ('127.1',):\n        socket.create_connection((h, 1))\n")) == [2]
    hits = [f"{rel}:{no}" for rel, tree, _ in _test_trees() for no in _unasked_shorthand_dials(tree)]
    assert hits == [], ("Windows' getaddrinfo refuses 127.1 (error 11001) before any connect; "
                        "ask _resolver_takes first: " + ", ".join(hits))


# 22 -- a filesystem stub matching a separator-bearing piece of a path's str()

# The last name of a `setattr` target that holds filesystem calls: os, os.path, shutil,
# pathlib and pathlib.Path, whatever module they are reached through (`path_jail.os`).
_FS_TARGETS = {"os", "path", "shutil", "Path", "pathlib"}

# (path, outermost function, needle as written) -> why the separator is right on every host.
_SEPARATOR_MATCH_ALLOWED = {
    ("tests/test_code_audit_folder_pinned.py", "test_a_held_folder_is_found_without_a_kernel_path",
     "'/proc/self/fd/'"): (
        "the product spells that handle path as a POSIX literal on every host "
        "(path_jail._fd_path reads os.readlink(f\"/proc/self/fd/{fd}\"))"),
}


def _fs_setattr_stub(call, local_defs):
    """The stub a `setattr(<filesystem module>, "name", stub)` call installs (a lambda, or a
    function defined in the same scope), else None. The two-argument string form
    (`setattr("os.rename", stub)`) counts as well."""
    if _call_name(call) != "setattr" or len(call.args) < 2:
        return None
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str) and len(call.args) == 2:
        target, stub = first.value.rsplit(".", 1)[0], call.args[1]
    elif len(call.args) >= 3:
        try:
            target = ast.unparse(first)
        except Exception:
            return None
        stub = call.args[2]
    else:
        return None
    if target.split(".")[-1] not in _FS_TARGETS:
        return None
    if isinstance(stub, ast.Lambda):
        return stub
    if isinstance(stub, ast.Name):
        return local_defs.get(stub.id)
    return None


def _str_of(node, params) -> bool:
    """`str(p)` or `os.fspath(p)` of one of the stub's own parameters."""
    return (isinstance(node, ast.Call) and _call_name(node) in ("str", "fspath")
            and len(node.args) == 1 and isinstance(node.args[0], ast.Name)
            and node.args[0].id in params)


def _separator_needle(node) -> bool:
    """A needle the scan cannot read (a name, a subscript, a call it does not know), or text
    holding a separator: a literal, the literal parts of an f-string (`f"/{lang}"`; the
    `{os.sep}` of `f"{os.sep}de"` is a value, not a literal part), and the literal or f-string
    inside str() or os.fspath(). str() or os.fspath() of anything else, a Path value, renders
    the host's own separator and is read as host-neutral."""
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return any(_separator_needle(e) for e in node.elts)
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and ("/" in node.value or "\\" in node.value)
    if isinstance(node, ast.JoinedStr):
        return any(_separator_needle(v) for v in node.values if isinstance(v, ast.Constant))
    if isinstance(node, ast.Call) and _call_name(node) in ("str", "fspath"):
        return (len(node.args) == 1 and isinstance(node.args[0], (ast.Constant, ast.JoinedStr))
                and _separator_needle(node.args[0]))
    return True


def _separator_matches(source: str):
    """(line, outermost function, needle as written) per piece of a path's str() a filesystem
    stub matches with `.endswith`, `.startswith` or `in`, in a function the Windows leg runs."""
    hits = []
    for fn in _functions_run_on_windows(ast.parse(source)):
        local_defs = {n.name: n for n in ast.walk(fn)
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not fn}
        stubs = [s for n in ast.walk(fn) if isinstance(n, ast.Call)
                 for s in [_fs_setattr_stub(n, local_defs)] if s is not None]
        for stub in stubs:
            a = stub.args
            params = {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs}
            params |= {x.arg for x in (a.vararg, a.kwarg) if x is not None}
            for node in ast.walk(stub):
                needles = []
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in ("endswith", "startswith")
                        and _str_of(node.func.value, params) and node.args):
                    needles.append(node.args[0])
                elif isinstance(node, ast.Compare):
                    left = node.left
                    for op, right in zip(node.ops, node.comparators):
                        if isinstance(op, (ast.In, ast.NotIn)) and _str_of(right, params):
                            needles.append(left)
                        left = right
                for needle in needles:
                    if _separator_needle(needle):
                        hits.append((needle.lineno, fn.name, ast.unparse(needle)))
    return hits


def test_the_separator_match_class_is_real():
    """MUTATION: read every needle that is not a literal as host-neutral - red on the tuple
    parameter case."""
    from pathlib import PureWindowsPath as W
    assert not str(W("models") / "de").endswith("/de")
    tts = ('import os\n'
           'def _failing(monkeypatch, *failing):\n'
           '    def rename(src, dst):\n'
           '        for a, b in failing:\n'
           '            if str(src).endswith(a) and str(dst).endswith(b):\n'
           '                raise PermissionError(13, "x")\n'
           '    monkeypatch.setattr(os, "rename", rename)\n')
    assert _separator_matches(tts) == [(5, "_failing", "a"), (5, "_failing", "b")]
    literal = ('def test_x(monkeypatch):\n'
               '    monkeypatch.setattr(mod.os.path, "exists", lambda p: str(p).endswith("/de"))\n')
    assert _separator_matches(literal) == [(2, "test_x", "'/de'")]
    contained = ('def test_x(m):\n'
                 '    m.setattr(Path, "exists", lambda self: "\\\\de" in os.fspath(self))\n')
    assert _separator_matches(contained) == [(2, "test_x", "'\\\\de'")]
    for neutral in ('lambda s, d: os.path.basename(d) == "de"',
                    'lambda s, d: str(d).startswith(str(root))',
                    'lambda s, d: str(d).endswith(".de.incoming")',
                    'lambda s, d: str(d).endswith(f"{os.sep}de")',
                    'lambda s, d: str(d).endswith(f"{os.sep}{lang}")',
                    'lambda s, d: str(d).endswith(str("de"))',
                    'lambda s, d: str(d).endswith(str(root / "de"))'):
        assert _separator_matches('def test_x(m):\n    m.setattr(os, "rename", '
                                  + neutral + ')\n') == [], neutral
    # A separator inside a conversion or in an f-string's literal part is still a literal one.
    # MUTATIONS: read every conversion, or every f-string, as host-neutral again - red.
    for spelled, needle in (('str("/de")', "str('/de')"), ('os.fspath("/de")', "os.fspath('/de')"),
                            ('f"/{lang}"', "f'/{lang}'"), ('str(f"\\\\{lang}")', "str(f'\\\\{lang}')")):
        assert _separator_matches('def test_x(m):\n    m.setattr(os, "rename", lambda s, d: '
                                  'str(d).endswith(' + spelled + '))\n') == [
            (2, "test_x", needle)], spelled
    # Not a filesystem stub: a separator in a needle is the code's own business there.
    assert _separator_matches('def test_x(m):\n    m.setattr(client, "get", '
                              'lambda u: str(u).endswith("/api"))\n') == []
    # The string form of setattr names its target in the text.
    assert _separator_matches('def test_x(m):\n    m.setattr("os.rename", '
                              'lambda s, d: str(d).endswith("/de"))\n') == [(2, "test_x", "'/de'")]
    skipped = ('import os, pytest\n@pytest.mark.skipif(os.name == "nt", reason="posix")\n'
               + literal)
    assert _separator_matches(skipped) == []
    module_skip = ('import sys, pytest\nif sys.platform == "win32":\n'
                   '    pytest.skip("posix only", allow_module_level=True)\n' + literal)
    assert _separator_matches(module_skip) == []


def test_no_filesystem_stub_matches_a_separator_piece():
    """MUTATION: `str(src).endswith(a) and str(dst).endswith(b)` back in
    tests/test_tts_container_verified.py's `_failing_renames` - red."""
    hits = [f"{rel}:{no} {fn} {needle}" for rel, _tree, source in _test_trees()
            for no, fn, needle in _separator_matches(source)
            if (rel, fn, needle) not in _SEPARATOR_MATCH_ALLOWED]
    assert hits == [], ("str() of a Path renders the host's separator, so a stub matching a "
                        "piece of it with / or \\ never fires on Windows; compare "
                        "os.path.basename(...) or Path(p).parts, or add the site to "
                        "_SEPARATOR_MATCH_ALLOWED with its reason: " + ", ".join(hits))


def test_the_separator_match_allowlist_carries_no_dead_entries():
    for rel, fn, needle in _SEPARATOR_MATCH_ALLOWED:
        found = {(f, n) for _, f, n in _separator_matches(
            (_REPO / rel).read_text(encoding="utf-8"))}
        assert (fn, needle) in found, f"allowlisted site no longer matches, drop it: {rel} {fn}"


# 23 -- two wall-clock readings made an order in a file name

_CLOCK_READINGS = {"time_ns", "time"}


def _clock_arithmetic_in_fstrings(tree) -> list:
    """`f"{time.time_ns() + 1}..."`: a constant added to a clock reading inside an f-string,
    the spelling that forces an order onto readings a coarse clock makes equal."""
    def reading(node):
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in _CLOCK_READINGS and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "time")
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        for part in node.values:
            value = part.value if isinstance(part, ast.FormattedValue) else None
            if (isinstance(value, ast.BinOp)
                    and any(reading(s) for s in (value.left, value.right))
                    and any(isinstance(s, ast.Constant) for s in (value.left, value.right))):
                hits.append(node.lineno)
    return hits


def test_no_clock_reading_is_made_an_order_in_a_file_name():
    """MUTATION: name the list file `f"{time.time_ns() + 1}_list.json"` again in
    tests/test_whatsapp_send_request_claim.py - red."""
    assert _clock_arithmetic_in_fstrings(ast.parse('n = f"{time.time_ns() + 1}_x.json"')) == [1]
    assert _clock_arithmetic_in_fstrings(ast.parse('n = f"{time.time_ns()}_x.json"')) == []
    hits = [f"{rel}:{no}" for rel, tree, _ in _test_trees() for no in _clock_arithmetic_in_fstrings(tree)]
    assert hits == [], ("time.time_ns() ticks every 15.625 ms on Windows before Python 3.13, so "
                        "readings in a row are equal; spell the order with fixed names: "
                        + ", ".join(hits))


# 24 -- a claim that moves a file instead of creating one exclusively

def _claims_without_an_exclusive_create(tree) -> list:
    hits = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)) or "claim" not in func.name.lower():
            continue
        moves = False
        exclusive = False
        for node in ast.walk(func):
            if isinstance(node, ast.Attribute) and node.attr == "O_EXCL":
                exclusive = True
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("rename", "replace")):
                continue
            owner = node.func.value
            if isinstance(owner, ast.Name) and owner.id == "os":
                moves = True
            elif len(node.args) == 1 and not node.keywords:
                moves = True
        if moves and not exclusive:
            hits.append(func.lineno)
    return hits


def test_no_claim_moves_a_file_without_an_exclusive_create():
    """MUTATION: the rename claim (`path.rename(claimed)`) back in
    `whatsapp_bridge._claim_request_file` - red."""
    assert _claims_without_an_exclusive_create(ast.parse(
        "def claim(p, q):\n    p.rename(q)\n")) == [1]
    assert _claims_without_an_exclusive_create(ast.parse(
        "def claim(p, q):\n    os.open(q, os.O_CREAT | os.O_EXCL)\n    os.replace(p, q)\n")) == []
    assert _claims_without_an_exclusive_create(ast.parse(
        "def claim(s):\n    return s.replace('a', 'b')\n")) == []
    hits = [f"{rel}:{no}" for rel, path in _tracked_python_files(("vaf/",))
            for no in _claims_without_an_exclusive_create(ast.parse(path.read_text(encoding="utf-8")))]
    assert hits == [], ("a rename is no mutex on Windows (MoveFileEx opens by name, renames by "
                        "handle); claim with os.open(..., O_CREAT | O_EXCL): " + ", ".join(hits))
