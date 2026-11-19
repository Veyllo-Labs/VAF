# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`websocket_endpoint` never re-imports a name the module already imports.

THE DEFECT. Three branches of the WebSocket handler carried a function-local
`from vaf.core.config import is_admin_identity`. An import binds a LOCAL name, and Python
decides locality for the whole function at compile time, so every other branch that used
`is_admin_identity` read an unbound local until one of those three branches had run on
the socket. Measured: `get_automations` as the first frame of a connection answered
"cannot access local variable 'is_admin_identity' ...", and about 25 admin gates were
in the same position. The browser hid it because it sends `get_config` first; any other
client does not.

The guard walks the handler's own statements (not nested functions or lambdas, which
have their own scope) for an import of a name the module binds at top level.
"""
import ast
from pathlib import Path

_SOURCE = Path(__file__).resolve().parent.parent / "vaf" / "core" / "web_server.py"


def _bound(alias: ast.alias) -> str:
    return (alias.asname or alias.name).split(".")[0]


def _module_names(tree: ast.Module) -> set:
    """Names the module binds at top level, including imports under a module-level
    try/if."""
    names = set()

    def visit(stmts):
        for node in stmts:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names.update(_bound(a) for a in node.names)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names.update(t.id for t in targets if isinstance(t, ast.Name))
            elif isinstance(node, (ast.Try, ast.If)):
                visit(node.body)
                visit(node.orelse)
                for handler in getattr(node, "handlers", []):
                    visit(handler.body)
                visit(getattr(node, "finalbody", []))
    visit(tree.body)
    return names


def shadowing_imports(source: str, function: str = "websocket_endpoint") -> list:
    """(line, name) for every import inside `function` that rebinds a module-level name."""
    tree = ast.parse(source)
    top = _module_names(tree)
    fn = next(n for n in tree.body
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == function)
    found = []

    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda,
                                  ast.ClassDef)):
                continue
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                found.extend((child.lineno, _bound(a)) for a in child.names if _bound(a) in top)
            walk(child)
    walk(fn)
    return found


def test_websocket_endpoint_has_no_shadowing_import():
    """MUTATION: re-add `from vaf.core.config import is_admin_identity` in one branch."""
    assert shadowing_imports(_SOURCE.read_text(encoding="utf-8")) == []


def test_the_guard_sees_a_shadowing_import():
    synthetic = '''
from vaf.core.config import is_admin_identity
try:
    from vaf.core.path_jail import safe_entry_name
except ImportError:
    pass

async def websocket_endpoint(websocket):
    if type == "a":
        ok = is_admin_identity(None, None)
    elif type == "b":
        from vaf.core.config import is_admin_identity
        from vaf.core.path_jail import safe_entry_name as safe_entry_name
        from vaf.core.config import is_admin_identity as _local_alias
    def nested():
        from vaf.core.config import is_admin_identity
    return lambda: None
'''
    assert [name for _line, name in shadowing_imports(synthetic)] == [
        "is_admin_identity", "safe_entry_name"]
