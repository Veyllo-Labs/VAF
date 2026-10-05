# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A test that tells two environment variables apart by case says what Windows does.

Windows environment variables are case-insensitive, and Python mirrors that: `HTTP_PROXY`
and `http_proxy` are ONE variable there. A test that sets one spelling and expects the other
to stay unset holds on Linux and macOS and fails only on the Windows runner - measured
twice, once in the mail image proxy test and once in the egress site-proxy test, each green
in every local gate. No local run can catch it, because the difference is the operating
system's, so this guard reads the tests instead: a test function that names one variable in
two spellings must also branch on the platform (`os.name`, `sys.platform`,
`platform.system`)."""
import ast
from pathlib import Path

_TESTS = Path(__file__).resolve().parent
_PLATFORM_WORDS = ("os.name", "sys.platform", "platform.system", "skipif")


def _env_names(fn: ast.AST):
    """The variable names a function sets, deletes or reads through monkeypatch or
    os.environ, as written."""
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ("setenv", "delenv", "getenv", "get", "pop") and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                target = ast.unparse(node.func.value)
                if node.func.attr in ("setenv", "delenv") or "environ" in target:
                    yield first.value
        elif isinstance(node, ast.Subscript) and "environ" in ast.unparse(node.value):
            if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                yield node.slice.value


def test_case_only_env_pairs_branch_on_the_platform():
    offenders = []
    for path in sorted(_TESTS.rglob("test_*.py")):
        source = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            spellings = {}
            for name in _env_names(fn):
                spellings.setdefault(name.lower(), set()).add(name)
            if not any(len(v) > 1 for v in spellings.values()):
                continue
            body = ast.get_source_segment(source, fn) or ""
            if not any(word in body for word in _PLATFORM_WORDS):
                offenders.append(f"{path.relative_to(_TESTS.parent).as_posix()}::{fn.name}")
    assert not offenders, ("these tests tell environment variables apart by case and do not "
                           "say what Windows does: " + ", ".join(offenders))
