# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Output that a Windows console cannot encode must not kill the process.

THE FAILURE, as it actually happened. On Windows `sys.stdout` uses the active
code page - cp1252 by default - whenever it is not a real console: a pipe, a
redirect, a CI runner, a captured subprocess. Printing a character that code
page cannot encode raises UnicodeEncodeError and the process dies. A documented
example did exactly that on the first encrypted file it tried to show, because
it decoded ciphertext with errors="replace" and printed U+FFFD.

It took 27 minutes to learn, because only the Windows matrix entry sees it and
that entry is the slow one. These tests move the discovery to every run on every
platform, in under a second.

TWO RULES, because the failure has two shapes:

1. A stream fixed once beats a print policed a thousand times. Most unencodable
   output is not ours to begin with - a model reply, a filename, a page title,
   a dependency's exception text - and no lint over our own literals can see
   those coming. Every entry point therefore reconfigures its streams.
2. Where our own literals ARE the problem, the guard names them, because a file
   that prints a checkmark and forgot the reconfiguration is a crash waiting for
   a redirect.
"""
import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _narrow_env() -> dict:
    """A narrow-console environment a Python subprocess can actually START in.

    Stripping the env to two keys was the point - the narrowness must come from
    the environment, not from luck - but on Windows it stripped SYSTEMROOT, and a
    Python without it dies in _Py_HashRandomization_Init before the first line of
    the script runs (os.urandom needs the CryptoAPI, the CryptoAPI needs
    SystemRoot). The whole full-matrix lane was red on exactly that, which is the
    failure mode this file warns about in its own docstring: the test failed for
    an environment reason and said nothing about the code.
    """
    env = {"PYTHONIOENCODING": "cp1252", "PATH": ""}
    for keep in ("SYSTEMROOT", "SystemRoot", "SYSTEMDRIVE", "SystemDrive"):
        if keep in os.environ:
            env[keep] = os.environ[keep]
    return env

# vaf/main.py keeps its own inline copy on purpose: it runs before the
# dependency bootstrap and therefore cannot import from the package yet.
INLINE_BY_DESIGN = {"vaf/main.py"}


def _encodable(text: str) -> bool:
    try:
        text.encode("cp1252")
        return True
    except UnicodeEncodeError:
        return False


def _tracked_python():
    out = subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT,
                         capture_output=True, text=True).stdout.split()
    return [f for f in out if not f.startswith("vaf/vendor/")]


def _unencodable_print_lines(src: str):
    """Line numbers of print() calls carrying a literal cp1252 cannot encode."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    lines = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "print"):
            continue
        for arg in ast.walk(node):
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                    and not _encodable(arg.value):
                lines.append(node.lineno)
    return sorted(set(lines))


def test_every_runnable_file_that_prints_wide_characters_fixes_its_streams():
    """MUTATION: drop force_utf8_streams() from any script under scripts/ and
    this names it.

    Scoped to files that can be started directly, because those are the ones
    that get their own fresh streams. A module imported by the CLI inherits
    whatever `vaf/main.py` already set up.
    """
    offenders = {}
    for rel in _tracked_python():
        src = (ROOT / rel).read_text(encoding="utf-8")
        runnable = '__name__ == "__main__"' in src or "__name__ == '__main__'" in src
        if not runnable:
            continue
        lines = _unencodable_print_lines(src)
        if not lines:
            continue
        protected = "force_utf8_streams" in src or "reconfigure(encoding=" in src
        if not protected:
            offenders[rel] = lines

    assert not offenders, (
        "these can be run directly and print characters a Windows console cannot "
        f"encode, without fixing their streams first: {offenders}. Call "
        "vaf.core.console.force_utf8_streams() near the top.")


def test_the_cli_entry_point_still_carries_its_inline_copy():
    """The one duplicate the rule allows, pinned so it cannot quietly vanish.

    `vaf/main.py` cannot import the package before the dependency bootstrap has
    run, so it repeats the reconfiguration by hand. If someone replaces it with
    the shared helper the import will fail on a machine with missing
    dependencies - which is the situation the bootstrap exists for.
    """
    src = (ROOT / "vaf" / "main.py").read_text(encoding="utf-8")
    head = src[:src.index("def bootstrap")]

    assert 'reconfigure(encoding="utf-8"' in head, (
        "the CLI entry point no longer fixes its streams before anything prints")
    assert "backslashreplace" in head


@pytest.mark.parametrize("char,name", [("✓", "check mark"),
                                       ("�", "replacement character"),
                                       ("\U0001f680", "emoji")])
def test_the_helper_survives_a_narrow_console(tmp_path, char, name):
    """The behavioural half: a real subprocess, a real narrow encoding.

    U+FFFD is in the list because it is what `bytes.decode(errors="replace")`
    produces, and that is the exact character that killed the example - not
    something anyone wrote on purpose.
    """
    script = tmp_path / "probe.py"
    script.write_text(
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "from vaf.core.console import force_utf8_streams\n"
        "force_utf8_streams()\n"
        "print('start', %r, 'end')\n" % (str(ROOT), char),
        encoding="utf-8")

    proc = subprocess.run([sys.executable, str(script)], capture_output=True,
                          env=_narrow_env(),
                          text=True, timeout=60)

    assert proc.returncode == 0, f"{name} killed the process:\n{proc.stderr[-600:]}"
    assert "start" in proc.stdout and "end" in proc.stdout


def test_without_the_helper_a_narrow_console_really_does_kill_it(tmp_path):
    """The floor under the test above: prove the danger is real, not imagined.

    Without this, every test in this file could pass because the environment is
    forgiving rather than because the code is correct.
    """
    script = tmp_path / "unprotected.py"
    script.write_text("print('start', '�', 'end')\n", encoding="utf-8")

    proc = subprocess.run([sys.executable, str(script)], capture_output=True,
                          env=_narrow_env(),
                          text=True, timeout=60)

    assert proc.returncode != 0
    assert "UnicodeEncodeError" in proc.stderr
