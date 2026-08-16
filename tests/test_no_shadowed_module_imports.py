# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A name used in a function that a LATER line of the same function imports.

Python binds a name for the whole function body the moment the body imports or
assigns it anywhere, including lines that run long before that import. So this,
1750 lines apart in one function, crashes every time the early line runs:

    from x import f          # module level
    def g():
        f()                  # UnboundLocalError
        if never_taken:
            from x import f  # this line is what makes f local

It shipped in 0.1.0a22 and killed every headless turn. The agent answered
nothing, which looked exactly like a hung local model - the loop died before it
ever reached the model. The guilty local import sat inside the A2A room-wake
block, so a room turn bound the name in time and kept working, while every
normal chat on the same host was dead. A host could therefore look healthy in
an agent room and be mute in its own chat window, which is how it survived
review and a release.

Ruff does not catch it here. Its F823 knows the pattern (the small probe below
is reported), and the CI gate already selects F82 - but on a 2750-line function
with the import nested in try/if/while it stays silent. Measured on the broken
file: four F401, no F823. So the guard is this scan, not a lint selector.

The scanning rules come from Opus (Claude Code on the reporting machine), who
found the bug and wrote the equivalent standalone checker; the three that matter
are all corrections of a naive version that produced false alarms: compare
against the EARLIEST local import of a name, never the first one a tree walk
happens to yield; skip names that are function parameters or were assigned
before the use, because those are already bound; and do not descend into nested
functions or lambdas, which own their scope.
"""
import ast
from pathlib import Path
from typing import Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE = REPO_ROOT / "vaf"


def _bindings_and_uses(func) -> Tuple[Dict[str, int], Dict[str, int], Dict[str, int]]:
    """Earliest local import, earliest binding and earliest read, per name."""
    imports: Dict[str, int] = {}
    binds: Dict[str, int] = {}
    uses: Dict[str, int] = {}

    def note(where: Dict[str, int], name: str, lineno: int) -> None:
        if name and (name not in where or lineno < where[name]):
            where[name] = lineno

    for arg in [*func.args.posonlyargs, *func.args.args, *func.args.kwonlyargs]:
        note(binds, arg.arg, func.lineno)
    for extra in (func.args.vararg, func.args.kwarg):
        if extra:
            note(binds, extra.arg, func.lineno)

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            if node is func:
                self.generic_visit(node)      # a nested def owns its own scope

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Lambda(self, node):
            pass

        def visit_Import(self, node):
            for alias in node.names:
                note(imports, alias.asname or alias.name.split(".")[0], node.lineno)

        def visit_ImportFrom(self, node):
            for alias in node.names:
                note(imports, alias.asname or alias.name, node.lineno)

        def visit_Name(self, node):
            if isinstance(node.ctx, ast.Load):
                note(uses, node.id, node.lineno)
            else:
                note(binds, node.id, node.lineno)

        def visit_arg(self, node):
            note(binds, node.arg, node.lineno)

    for statement in func.body:
        Visitor().visit(statement)
    return imports, binds, uses


def _scan(path: Path) -> List[Tuple[str, str, int, int]]:
    """(function, name, used_line, local_import_line) per dangerous case."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return []

    found: List[Tuple[str, str, int, int]] = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        imports, binds, uses = _bindings_and_uses(func)
        for name, import_line in imports.items():
            used = uses.get(name)
            if used is None or used >= import_line:
                continue
            bound = binds.get(name)
            if bound is not None and bound <= used:
                continue          # a parameter, or assigned first: already bound
            found.append((func.name, name, used, import_line))
    return found


def test_no_name_is_read_before_a_local_import_binds_it():
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        for func, name, used, imported in _scan(path):
            offenders.append(
                f"{path.relative_to(REPO_ROOT)}:{used}: {name!r} is read here, but line "
                f"{imported} imports it again inside {func}() - that makes {name!r} local "
                f"for the whole body, so this line raises UnboundLocalError. Move the "
                f"import to module level, or rename the local one."
            )
    assert not offenders, ("Shadowed local imports (each crashes when its line runs):\n"
                           + "\n".join(f"  - {o}" for o in offenders))


def test_the_scan_finds_the_shape_it_exists_for(tmp_path):
    """The 0.1.0a22 headless crash in seven lines. A guard that cannot fail on
    the real thing guards nothing."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "from os import getcwd\n"
        "\n"
        "def run(flag):\n"
        "    print(getcwd())\n"
        "    if flag:\n"
        "        from os import getcwd\n"
        "        print(getcwd())\n",
        encoding="utf-8",
    )
    hits = _scan(probe)
    assert hits, "the scan missed the very pattern it exists for"
    func, name, used, imported = hits[0]
    assert (func, name) == ("run", "getcwd")
    assert used < imported


def test_a_local_import_used_only_afterwards_is_left_alone(tmp_path):
    """Local imports are ordinary and almost always harmless. A guard that
    flagged all of them would be switched off within a week."""
    probe = tmp_path / "ok.py"
    probe.write_text(
        "from os import getcwd\n"
        "\n"
        "def run(flag):\n"
        "    if flag:\n"
        "        from os import getcwd\n"
        "    return getcwd()\n",
        encoding="utf-8",
    )
    assert _scan(probe) == []


def test_a_parameter_is_not_mistaken_for_a_shadowed_import(tmp_path):
    """The first false alarm this scan produced: a parameter is bound on entry,
    so reading it before a same-named local import is fine."""
    probe = tmp_path / "param.py"
    probe.write_text(
        "import cancel\n"
        "\n"
        "def run(cancel):\n"
        "    if cancel:\n"
        "        return 1\n"
        "    import cancel\n"
        "    return cancel\n",
        encoding="utf-8",
    )
    assert _scan(probe) == []


def test_a_nested_function_keeps_its_own_scope(tmp_path):
    """The second false alarm: an inner def has its own namespace, so an import
    inside it says nothing about the outer function's names."""
    probe = tmp_path / "nested.py"
    probe.write_text(
        "from os import getcwd\n"
        "\n"
        "def outer():\n"
        "    print(getcwd())\n"
        "    def inner():\n"
        "        from os import getcwd\n"
        "        return getcwd()\n"
        "    return inner\n",
        encoding="utf-8",
    )
    assert _scan(probe) == []
