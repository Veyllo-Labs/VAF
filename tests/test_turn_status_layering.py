# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Nothing outside the engine reads how a turn ended through its private fields.

`chat_step` records whether a turn failed and which streamed text was its failure in two
turn-local fields of the engine. Every host that keeps the stream needs that answer, and
three read the fields directly: the queue runner's chat lane (with its delivery and its room
fallback) and its sub-agent drain, the interactive CLI and the TUI, and `vaf run -p`. An
application built on VAF had no supported way to ask the same thing. That reach past the
facade was the gap; `last_turn` on `Agent` and `CoreAgent` (a frozen `vaf.TurnStatus`) is the
primitive that closed it, and the hosts read it now. This guard keeps the private reads
deleted, so the next host asks the public question an embedder asks.

AST walk, so a read spelled as a string (`getattr(agent, "_turn_failed", False)`) or as a
keyword (a stand-in built with `_turn_failed=True`) counts like an attribute access. The
engine itself, which sets the fields and serves the property, is the one file allowed to
name them. The roots cover the product code, the examples a stranger copies, the scripts and
the vendored contract suite, which must hold to the public surface by its own rule."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PRIVATE_TURN_FIELDS = frozenset({"_turn_failed", "_turn_failure"})
ENGINE = "vaf/core/agent.py"
ROOTS = ("vaf", "examples", "scripts", "tests/contract")


def _state_census_keys(tree) -> set:
    """The key nodes of an `AGENT_STATE` dict. The turn binding names every attribute an
    agent carries there, these two included, to say which turn resets them; naming a field
    in that census reads nothing."""
    keys = set()
    for node in ast.walk(tree):
        target = getattr(node, "target", None) or next(iter(getattr(node, "targets", [])), None)
        if (isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(target, ast.Name)
                and target.id == "AGENT_STATE" and isinstance(node.value, ast.Dict)):
            keys |= {id(k) for k in node.value.keys if k is not None}
    return keys


def _private_turn_reads(source: bytes):
    found = []
    tree = ast.parse(source)
    census = _state_census_keys(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in PRIVATE_TURN_FIELDS:
            found.append((node.lineno, node.attr))
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
              and node.value in PRIVATE_TURN_FIELDS and id(node) not in census):
            found.append((node.lineno, node.value))
        elif isinstance(node, ast.keyword) and node.arg in PRIVATE_TURN_FIELDS:
            found.append((node.value.lineno, node.arg))
    return found


def test_no_host_reads_the_engines_private_turn_fields():
    """MUTATION: put `getattr(agent, "_turn_failed", False)` back into the runner's room
    fallback - red, with the file and line named."""
    offenders = []
    for base in ROOTS:
        for path in sorted((ROOT / base).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if rel == ENGINE:
                continue
            offenders += [f"{rel}:{line} {name}" for line, name in _private_turn_reads(path.read_bytes())]
    assert not offenders, (
        "read how a turn ended through `agent.last_turn` (a vaf.TurnStatus), never the "
        "engine's private fields:\n  " + "\n  ".join(offenders))


def test_the_scan_sees_every_spelling_of_a_read():
    """The guard is only as good as its matcher: an attribute, a getattr string and a
    keyword are each found, and prose that merely names a field is not."""
    sample = (b'agent._turn_failed\n'
              b'getattr(agent, "_turn_failure", "")\n'
              b'SimpleNamespace(_turn_failed=True)\n'
              b'"""the engine\'s _turn_failed field"""\n')
    assert sorted(_private_turn_reads(sample)) == [
        (1, "_turn_failed"), (2, "_turn_failure"), (3, "_turn_failed")]


def test_the_engine_still_serves_the_fields_behind_the_property():
    """The exemption is not a blind spot: the engine is where the fields are set, and the
    property is what reads them."""
    found = {name for _, name in _private_turn_reads((ROOT / ENGINE).read_bytes())}
    assert found == PRIVATE_TURN_FIELDS
