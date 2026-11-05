# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The sitecustomize scripts/hostile_env.py writes, run in a child interpreter.

A child, because the snippet patches the import system, platform, email.utils and
posixpath.expanduser for the whole process it runs in.
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

_CHILD = r'''
import os, sys
snippet, target, listing = sys.argv[1:4]
# Windows' normcase lowercases; posixpath's is the identity, which hid the mismatch.
os.path.normcase = lambda s: os.fspath(s).lower()
with open(listing, "w", encoding="utf-8") as f:
    f.write(os.path.normcase(os.path.realpath(target)))
os.environ["VAF_HOSTILE_CRLF_FILES"] = listing
with open(snippet, encoding="utf-8") as f:
    exec(compile(f.read(), "sitecustomize", "exec"))
import pathlib
sys.stdout.write(pathlib.Path(target).read_bytes().hex())
'''


def _blocker() -> str:
    spec = importlib.util.spec_from_file_location("hostile_env", _ROOT / "scripts" / "hostile_env.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._BLOCKER.format(names=repr(set()))


def test_crlf_checkout_converts_a_mixed_case_name_under_a_lowercasing_normcase(tmp_path):
    """The CRLF CHECKOUT axis compares normcase'd paths, so its name prefilter must
    normcase the name too, or a tracked README.md is skipped on a Windows host.
    MUTATION: test `self.name` raw against the name set - red."""
    snippet = tmp_path / "sitecustomize_snippet.py"
    snippet.write_text(_blocker(), encoding="utf-8")
    target = tmp_path / "ReadMe.TXT"
    target.write_bytes(b"one\ntwo\n")
    out = subprocess.run(
        [sys.executable, "-c", _CHILD, str(snippet), str(target), str(tmp_path / "list.txt")],
        capture_output=True, text=True, check=True, timeout=60)
    assert bytes.fromhex(out.stdout) == b"one\r\ntwo\r\n"
