# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""CI guard for examples/: they must stay syntactically valid and honest.

Examples are the first code a new developer copies; a bit-rotted example is
worse than none. Most of them need a configured provider, so those are only
compiled. Example 08 needs nothing at all - it drives the session store against
a throwaway home - so it is actually RUN here, and its output is checked. That
run is the end-to-end proof of the storage story the docs tell: the chat text is
not in the encrypted bytes, a plaintext file still opens, and a deleted machine
key comes back from the recovery key.
"""
import importlib.util
import os
import py_compile
import re
import subprocess
import sys
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def test_every_example_script_compiles(tmp_path):
    scripts = sorted(EXAMPLES.rglob("*.py"))
    assert len(scripts) >= 4, "examples went missing"
    for script in scripts:
        py_compile.compile(str(script), cfile=str(tmp_path / "c.pyc"), doraise=True)


def test_the_storage_example_runs_and_tells_the_truth(tmp_path):
    """MUTATION: write sessions in plaintext, and the "present in the raw bytes"
    line flips to True; break recovery, and the last line never appears.

    A subprocess on purpose: the script repoints HOME before importing VAF, and
    doing that in-process would follow every later test into the sandbox.
    """
    script = EXAMPLES / "08_session_storage_and_encryption.py"
    # A DELIBERATELY NARROW console encoding. The example prints raw bytes from
    # disk, and the first version decoded them with errors="replace", which
    # produces U+FFFD - a character cp1252 cannot encode. It died on the first
    # encrypted file, but only on Windows, where that is the console default,
    # and only after the 27 minutes the Windows matrix entry takes. Forcing
    # cp1252 here means every Linux run proves the output is ASCII-safe in
    # seconds. It works precisely because ASCII is a subset of both.
    env = {**os.environ, "PYTHONPATH": str(EXAMPLES.parent), "TMPDIR": str(tmp_path),
           "PYTHONIOENCODING": "cp1252"}

    proc = subprocess.run([sys.executable, str(script)], capture_output=True,
                          text=True, timeout=300, env=env, cwd=str(EXAMPLES.parent))

    assert proc.returncode == 0, f"the example failed:\n{proc.stderr[-2000:]}"
    out = proc.stdout
    assert "'apple banana' present in the raw bytes: False" in out
    assert "'Bank' present in the raw bytes:         False" in out
    assert "VAFENC1:" in out                                  # part 3 wrote ciphertext
    assert "Alice owns:    ['Alice: travel']" in out          # part 2 isolation holds
    assert "nothing, as it should be" in out
    assert "after recovery: 'the safe combination is 42-17-8'" in out

    # And it stayed inside its own sandbox rather than the developer's home.
    sandbox = out.splitlines()[0]
    assert sandbox.startswith("Sandbox home: ") and str(tmp_path) in sandbox


def test_example_tool_is_a_valid_basetool():
    from vaf.tools.base import BaseTool

    tools_py = EXAMPLES / "vaf_example_tool" / "vaf_example_tool" / "tools.py"
    spec = importlib.util.spec_from_file_location("vaf_example_tool_tools", tools_py)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    cls = module.DiceRollTool
    assert issubclass(cls, BaseTool)
    tool = cls()
    assert tool.name == "dice_roll"
    assert not tool.coder_only, "entry-point tools target the main agent"
    result = tool.run(sides=6, count=2)
    assert isinstance(result, str) and "2d6" in result


def test_example_tool_entry_point_declaration_matches():
    pyproject = (EXAMPLES / "vaf_example_tool" / "pyproject.toml").read_text()
    match = re.search(
        r'^dice_roll\s*=\s*"vaf_example_tool\.tools:DiceRollTool"',
        pyproject,
        re.MULTILINE,
    )
    assert match, "the vaf.tools entry point must point at vaf_example_tool.tools:DiceRollTool"
    assert '[project.entry-points."vaf.tools"]' in pyproject
