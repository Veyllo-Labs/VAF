# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A tracked file that a test compares byte for byte with bytes built in code keeps LF.

THE CLASS. Windows runners check out with `core.autocrlf=true`, so every tracked text file
arrives with CRLF there unless `.gitattributes` pins its line ends. A test that compares such a
file's raw bytes with bytes a generator or a literal produces (always LF) is green on Linux and
macOS and red on Windows only: no local run can see it. The common-password list was the first
(its generator test reproduces it byte for byte); the loader itself copes with CRLF.

WHAT THIS DOES. It reads every test module, finds `<path>.read_bytes()` compared with `==` to
bytes built in code (a call such as `.encode(...)` or a bytes literal), resolves `<path>` when
it is a module-level constant under the repository, and requires `eol=lf` for each tracked one
(`git check-attr`). A path joined from a constant directory and a name only known at run time
(`FIXTURES / name`, the name a generator yields in a loop) pins every tracked file in that
directory: the app link's vector files were exactly that, and the scan used to skip them. A comparison of a file with itself, or with a file a test wrote, is not this
class and is left alone.
"""
import ast
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _path_of(node, consts, here: Path):
    """A module-level path expression as a Path, or None when it cannot be resolved."""
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _path_of(node.left, consts, here)
        if left is not None and isinstance(node.right, ast.Constant) and isinstance(node.right.value, str):
            return left / node.right.value
        if left is not None and not _is_glob(left):
            # A name known only at run time: every file of that directory may be the one.
            return left / "*"
        return None
    # Path(__file__).resolve().parents[N] / .parent
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and node.value.attr == "parents":
        if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, int):
            return here.parents[node.slice.value]
        return None
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        inner = _path_of(node.value, consts, here)
        return inner.parent if inner is not None else None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "resolve":
        return _path_of(node.func.value, consts, here)
    if (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Path" and node.args
            and isinstance(node.args[0], ast.Name) and node.args[0].id == "__file__"):
        return here
    return None


def _is_glob(path: Path) -> bool:
    return "*" in path.name


def _built_in_code(node) -> bool:
    """Bytes a generator or a literal produced: a call that is not another read_bytes(), or a
    bytes constant."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, bytes)
    return isinstance(node, ast.Call) and getattr(node.func, "attr", "") != "read_bytes"


def byte_pinned_tracked_files():
    found = {}
    for test in sorted((ROOT / "tests").glob("*.py")):
        tree = ast.parse(test.read_bytes())
        consts = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                value = _path_of(node.value, consts, test.resolve())
                if value is not None:
                    consts[node.targets[0].id] = value
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq)):
                continue
            sides = (node.left, node.comparators[0])
            for side, other in (sides, sides[::-1]):
                if (isinstance(side, ast.Call) and getattr(side.func, "attr", "") == "read_bytes"
                        and _built_in_code(other)):
                    path = _path_of(side.func.value, consts, test.resolve())
                    if path is not None and ROOT in path.parents:
                        found.setdefault(path.relative_to(ROOT).as_posix(), set()).add(test.name)
    return found


def _tracked_files(rel: str):
    """The tracked files a pinned path names: itself, or every match of a directory glob."""
    out = subprocess.run(["git", "ls-files", "--", rel], cwd=ROOT, capture_output=True,
                         text=True).stdout.split()
    return out


def _eol(rel: str) -> str:
    out = subprocess.run(["git", "check-attr", "eol", "--", rel], cwd=ROOT, capture_output=True,
                         text=True).stdout.strip()
    return out.rsplit(":", 1)[-1].strip()


def test_byte_pinned_files_keep_lf():
    """MUTATION: drop the `vaf/auth/data/*.txt` rule from .gitattributes - red."""
    pinned = {}
    for rel, tests in byte_pinned_tracked_files().items():
        for tracked in _tracked_files(rel):
            pinned.setdefault(tracked, set()).update(tests)
    missing = sorted(f"{rel} (pinned by {', '.join(sorted(tests))})"
                     for rel, tests in pinned.items() if _eol(rel) != "lf")
    assert not missing, ("tracked files that a test compares byte for byte, with no `eol=lf` in "
                         ".gitattributes (a Windows checkout turns them CRLF):\n" + "\n".join(missing))


def test_the_scan_sees_the_known_case():
    """The guard's own floor: a scan that finds nothing passes while measuring nothing."""
    found = byte_pinned_tracked_files()
    assert "vaf/auth/data/common_passwords.txt" in found
    # A directory joined with a name known only at run time (the vector generator's loop).
    assert "tests/fixtures/app_link/*" in found
    assert "tests/fixtures/app_link/qr_vectors.json" in _tracked_files("tests/fixtures/app_link/*")
