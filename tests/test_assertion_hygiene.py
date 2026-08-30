# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""CI guard: an assertion that can never fail is a test that proves nothing.

The suite's whole value rests on a red test meaning something is broken. An assertion that is
true for every possible input inverts that: it reports health it never measured, and it does so
in the file whose docstring claims the opposite. This is worse than a missing test, because a
missing test is visible in coverage and a dead one is not.

Three shapes are caught, all decidable without running anything:

1. `assert <real check> or <constant-truthy>` - the whole assertion is the constant. Found live
   in two places, and in both the dead branch was the claim the test was named for. One of them
   also hid a positional index that only worked because `fold_votes` happens to sort newest
   first; the id lookup that replaced it says what it means.
2. An or-branch that holds for every value of the enclosing literal loop - the same defect one
   indirection deeper, so a plain constant-folder walks straight past it. This is the shape the
   guard was written for: `assert <check> or "append" in marker` inside
   `for marker in ("filters.append(...)", "lexical_filters.append(...)")`. Both markers contain
   the word, so the check behind it never ran, and the test guarded a SQL-vs-post-fetch
   distinction that a substring search could not have decided anyway.
3. `assert <truthy literal>` - the marker at the end of an import smoke test. The imports are
   the test there; the marker only makes the function look like it asserts something.

The allowlist is deliberately absent. Every shape above has an honest form that is barely longer
(assert what the loop body proves, look the entry up by id, drop the marker), so an exception
would only ever be a way to keep a dead test."""
import ast
from pathlib import Path

_TESTS = Path(__file__).resolve().parent


def _const_truthy(node) -> bool:
    """True when this expression is a literal that is always truthy."""
    try:
        return bool(ast.literal_eval(node))
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return False


def _membership_holds(node, var: str, value) -> bool:
    """Evaluate `<x> in <y>` with `var` bound to `value`, both sides otherwise literal."""
    if not (isinstance(node, ast.Compare) and len(node.ops) == 1
            and isinstance(node.ops[0], (ast.In, ast.NotIn))):
        return False
    if {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} - {var}:
        return False

    def resolve(n):
        if isinstance(n, ast.Name) and n.id == var:
            return value
        return ast.literal_eval(n)

    try:
        held = resolve(node.left) in resolve(node.comparators[0])
    except (ValueError, TypeError, SyntaxError):
        return False
    return (not held) if isinstance(node.ops[0], ast.NotIn) else held


def _iterable_literal(node):
    """The values a `for ... in <node>` walks, when they are decidable without running it."""
    try:
        values = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None
    if not isinstance(values, (list, tuple, set, frozenset, str, dict)):
        return None
    values = list(values)
    # An empty iterable never enters the body, so "holds for every value" would be vacuously
    # true about an assertion that never runs. Say nothing instead.
    return values or None


def _rebound_inside(loop, name: str) -> bool:
    """True when the loop body assigns `name` itself, so the value is no longer the loop's."""
    target_nodes = set(ast.walk(loop.target))
    return any(isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Store)
               and n not in target_nodes
               for stmt in loop.body for n in ast.walk(stmt))


def _loop_values_in_force(node, parents: dict) -> dict:
    """The literal values a name takes AT THIS assertion - nothing wider.

    Walking the ancestry instead of collecting a function's loops wholesale is what keeps the
    guard honest in four ways, each of which was a live defect in the first version of this
    file (all four measured, all four pinned by tests below):

      - an assertion AFTER the loop is not inside its body, so it inherits nothing;
      - two loops that happen to share a variable name no longer pool their values, which
        would have made `all(...)` unsatisfiable and quietly retired the guard;
      - a nested function starts empty, because what an enclosing loop leaves behind is one
        value, not all of them;
      - the nearest enclosing loop wins, which is what shadowing means.
    """
    out: dict = {}
    child, parent = node, parents.get(node)
    while parent is not None:
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            break
        if (isinstance(parent, (ast.For, ast.AsyncFor))
                and isinstance(parent.target, ast.Name)
                and parent.target.id not in out
                and any(child is stmt for stmt in parent.body)):
            values = _iterable_literal(parent.iter)
            if values and not _rebound_inside(parent, parent.target.id):
                out[parent.target.id] = values
        child, parent = parent, parents.get(parent)
    return out


def _dead_assertions(path: Path) -> list:
    """Every assertion in the file, visited exactly once, judged against the loop values that
    are in force where it actually stands."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    parents = {child: parent for parent in ast.walk(tree)
               for child in ast.iter_child_nodes(parent)}
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        test = node.test
        if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
            bindings = _loop_values_in_force(node, parents)
            for branch in test.values:
                if _const_truthy(branch):
                    found.append((node.lineno, "or-branch is always true: "
                                               f"{ast.unparse(branch)!r}"))
                    break
                if any(all(_membership_holds(branch, var, v) for v in values)
                       for var, values in bindings.items()):
                    found.append((node.lineno, "or-branch holds for every loop value: "
                                               f"{ast.unparse(branch)!r}"))
                    break
        elif _const_truthy(test):
            found.append((node.lineno, "the whole assertion is a truthy literal: "
                                       f"{ast.unparse(test)!r}"))
    return sorted(found)


def test_no_assertion_in_the_suite_can_pass_unconditionally():
    offenders = []
    for path in sorted(_TESTS.glob("test_*.py")):
        for line, why in _dead_assertions(path):
            offenders.append(f"{path.name}:{line} - {why}")
    assert not offenders, (
        "assertions that can never fail:\n  " + "\n  ".join(offenders)
        + "\n\nAssert what the test is named for, or delete the line - do not weaken it."
    )


# ── the guard must be able to see each shape it claims to catch ───────────────────────────

def test_the_guard_detects_all_three_shapes(tmp_path):
    """Without this the guard would be one green test proving nothing, which is the very
    thing it exists to forbid."""
    sample = tmp_path / "test_sample.py"
    sample.write_text(
        "def test_or_constant():\n"
        "    assert 1 == 2 or True\n"
        "\n"
        "def test_loop_tautology():\n"
        "    for marker in ('a.append(x)', 'b.append(x)'):\n"
        "        assert 1 == 2 or 'append' in marker\n"
        "\n"
        "def test_marker():\n"
        "    assert True\n"
        "\n"
        "def test_honest():\n"
        "    value = 3\n"
        "    assert value == 3\n"
        "    for name in ('a', 'b'):\n"
        "        assert name in ('a', 'b')\n",
        encoding="utf-8",
    )
    hits = _dead_assertions(sample)
    assert [line for line, _ in hits] == [2, 6, 9], hits
    reasons = " ".join(why for _, why in hits)
    assert "always true" in reasons and "every loop value" in reasons and "truthy literal" in reasons


def test_a_deterministic_repeat_call_is_not_flagged(tmp_path):
    """`f(x) == f(x)` looks vacuous and is not: it is how this suite pins that a handle, a peer
    tag and a cache key are stable across calls. Flagging it would push authors to delete real
    determinism checks, so the guard stays with what is decidable from the syntax alone."""
    sample = tmp_path / "test_sample.py"
    sample.write_text(
        "def test_stable():\n"
        "    assert derive('k', 'room') == derive('k', 'room')\n"
        "    assert key('q', 'scope-a') == key('q', 'scope-a')\n",
        encoding="utf-8",
    )
    assert _dead_assertions(sample) == []

# ── the loop binding must follow the nesting, not the file ────────────────────────────────

def _hits(tmp_path, code, name="test_p.py"):
    sample = tmp_path / name
    sample.write_text(code, encoding="utf-8")
    return [line for line, _ in _dead_assertions(sample)]


def test_a_loop_value_does_not_leak_past_the_loop(tmp_path):
    """The first version of this guard collected a function's loops wholesale, so an assertion
    standing AFTER the loop was judged against values it can no longer hold. It reported a
    healthy test as dead - and a guard that cries wolf is a guard someone deletes."""
    assert _hits(tmp_path, """
def test_x():
    for marker in ('a.append(x)', 'b.append(x)'):
        pass
    marker = compute()
    assert real_check() or 'append' in marker
""") == []


def test_two_loops_sharing_a_name_do_not_pool_their_values(tmp_path):
    """The worse half of the same defect, and the reason it had to be fixed rather than
    tolerated: pooled values made `all(...)` unsatisfiable, so a genuine tautology in the first
    loop went unreported. The guard stopped guarding and stayed green about it - which is the
    exact failure this whole file exists to forbid."""
    assert _hits(tmp_path, """
def test_x():
    for name in ('a.append(x)', 'b.append(x)'):
        assert real_check() or 'append' in name
    for name in ('zzz',):
        assert other_check()
""") == [4]


def test_a_nested_function_starts_with_no_binding(tmp_path):
    """What an enclosing loop leaves behind is one value, not all of them. Line 7 is still
    caught - a constant needs no binding - which is what keeps this from being a blanket skip."""
    assert _hits(tmp_path, """
def test_outer():
    for marker in ('a.append(x)', 'b.append(x)'):
        pass
    def inner():
        assert real_check() or 'append' in marker
    assert 1 == 2 or True
""") == [7]


def test_an_assertion_is_reported_once(tmp_path):
    """Overlapping scope walks reported everything inside a nested function twice. A duplicate
    in the failure message reads as two separate offences and sends the reader hunting."""
    assert _hits(tmp_path, """
def test_outer():
    def inner():
        assert True
    inner()
""") == [4]


def test_the_binding_yields_to_what_the_body_does_with_it(tmp_path):
    """Four ways the values stop being the loop's, all of them silent if unhandled: the body
    rebinds the name, an inner loop shadows it, the iterable is empty so the body never runs,
    and the `else` clause runs only after the loop is over."""
    rebound = """
def test_x():
    for marker in ('a.append(x)', 'b.append(x)'):
        marker = normalise(marker)
        assert real_check() or 'append' in marker
"""
    shadowed = """
def test_x():
    for m in ('a.append(x)', 'b.append(x)'):
        for m in ('zzz',):
            assert real_check() or 'append' in m
"""
    never_runs = """
def test_x():
    for m in ():
        assert real_check() or 'append' in m
"""
    after_the_loop = """
def test_x():
    for m in ('a.append(x)', 'b.append(x)'):
        pass
    else:
        assert real_check() or 'append' in m
"""
    for i, code in enumerate((rebound, shadowed, never_runs, after_the_loop)):
        assert _hits(tmp_path, code, f"test_case_{i}.py") == [], code

