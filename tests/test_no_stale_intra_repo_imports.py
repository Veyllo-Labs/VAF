# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A `from vaf.x import y` must name something `vaf.x` actually has.

THE CLASS, not the incident. Deleting a private helper is a two-line change in the file
that defines it and an ImportError in every other file that named it - and this repository
is full of defensive `try: ... except Exception: pass` blocks, so that ImportError does not
crash anything. It removes a feature in silence.

Measured, from the change that produced this file: `cloud_storage._get_username` was deleted
because it read an environment variable nobody sets. `librarian.py` imported it to build the
"connected clouds" tile, inside `except Exception: pass`, on the live WebUI emit path. The
suite stayed green, every gate stayed green, and the tile would simply have stopped
appearing. An adversarial review found it; nothing in 3078 tests could.

WHY A STATIC CHECK RATHER THAN IMPORTING EVERYTHING. Importing every module would find the
same thing, but only for imports at module level - and this one was inside a function, which
is where defensive imports live precisely because they are allowed to fail. The parse sees
those too.

WHAT IT DELIBERATELY DOES NOT CLAIM: that a name resolves at runtime. A module may define
names dynamically (PEP 562 `__getattr__`, conditional definitions), so those modules are
exempted by NAME below, with the reason. The check is "the target module does not define
this and does not define names dynamically", which is narrow enough to be free of noise and
still catches a deletion.
"""
import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
VAF = ROOT / "vaf"

# Modules that resolve names at runtime, so a static miss proves nothing about them.
DYNAMIC_MODULES = {
    "vaf": "PEP 562 __getattr__ serves the lazy public facade (BaseTool, user_jail, ToolCaller)",
}


def _defined_names(path: pathlib.Path) -> set | None:
    """Top-level names a module binds, or None if it binds them dynamically."""
    try:
        tree = ast.parse(path.read_bytes().decode())
    except (SyntaxError, UnicodeDecodeError):       # pragma: no cover
        return None
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == "__getattr__":
                return None                          # dynamic; cannot be judged statically
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.Try):
            # Optional dependencies are bound in both arms; take everything either binds.
            for sub in node.body + [s for h in node.handlers for s in h.body] + node.orelse:
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    for a in sub.names:
                        names.add(a.asname or a.name.split(".")[0])
                elif isinstance(sub, ast.Assign):
                    for t in sub.targets:
                        if isinstance(t, ast.Name):
                            names.add(t.id)
                elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.add(sub.name)
    return names


def _module_file(dotted: str) -> pathlib.Path | None:
    parts = dotted.split(".")
    if parts[0] != "vaf":
        return None
    direct = ROOT.joinpath(*parts).with_suffix(".py")
    if direct.exists():
        return direct
    package = ROOT.joinpath(*parts, "__init__.py")
    return package if package.exists() else None


def test_no_import_names_something_its_module_does_not_define():
    """One stale name is a feature that disappears without a stack trace."""
    stale = []
    for source in sorted(VAF.rglob("*.py")):
        try:
            tree = ast.parse(source.read_bytes().decode())
        except (SyntaxError, UnicodeDecodeError):    # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level or not node.module:
                continue
            if not node.module.startswith("vaf") or node.module in DYNAMIC_MODULES:
                continue
            target = _module_file(node.module)
            if target is None or target == source:
                continue
            defined = _defined_names(target)
            if defined is None:                      # dynamic target
                continue
            for alias in node.names:
                if alias.name == "*" or alias.name in defined:
                    continue
                # A submodule of a package is a legitimate `from pkg import sub`.
                if _module_file(f"{node.module}.{alias.name}") is not None:
                    continue
                stale.append(
                    f"{source.relative_to(ROOT)}:{node.lineno} imports "
                    f"{alias.name!r} from {node.module}, which does not define it"
                )
    assert not stale, (
        "stale intra-repo import(s):\n  " + "\n  ".join(stale) +
        "\nThese raise ImportError at the call, and in this repository that usually happens "
        "inside `except Exception: pass` - so the feature disappears and nothing fails."
    )
