# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The embedder's authorizer reaches workflow steps - from every lane that can carry it.

The funnel stage is tested elsewhere (test_workflow_steps_take_the_policy_funnel); this
file pins the WIRING, because "stage tested, wiring not" is this repo's most-repeated
test gap: six of the seven WorkflowEngine construction sites have an agent object in
reach and must thread `authorize=getattr(<agent>, "_tool_authorizer", None)` into the
engine. The seventh - the workflow CLI subprocess - deliberately does not: a callable
cannot cross a process boundary. That boundary is PINNED here too, so it cannot be
"fixed" casually; the account allowlist still holds in that lane because its answer
comes from the resolver the subprocess registers itself at vaf.main import.

AST, not substring, per tests/README: a source grep matches comments and near-miss call
shapes; the AST finds the actual WorkflowEngine(...) call node and its keywords.
"""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

THREADED_SITES = (
    "vaf/tools/workflow_executor.py",
    "vaf/core/agent.py",
    "vaf/cli/cmd/run.py",
    "vaf/core/automation.py",
    "vaf/workflows/resume.py",
    "vaf/tools/agent_workflow_builder.py",
)


def _engine_calls(rel_path: str):
    tree = ast.parse((ROOT / rel_path).read_bytes())
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and ((isinstance(node.func, ast.Name) and node.func.id == "WorkflowEngine")
             or (isinstance(node.func, ast.Attribute) and node.func.attr == "WorkflowEngine"))
    ]


def _authorize_keyword(call: ast.Call):
    for kw in call.keywords:
        if kw.arg == "authorize":
            return kw
    return None


@pytest.mark.parametrize("rel_path", THREADED_SITES)
def test_the_lane_threads_the_agents_authorizer(rel_path):
    calls = _engine_calls(rel_path)
    assert calls, f"{rel_path}: no WorkflowEngine(...) call found - update this test"
    for call in calls:
        kw = _authorize_keyword(call)
        assert kw is not None, (
            f"{rel_path}: WorkflowEngine(...) has no authorize= keyword - an embedder's "
            f"set_tool_authorizer would silently not cover workflow steps on this lane"
        )
        # The value must be getattr(<something>, "_tool_authorizer", ...): reading the
        # attribute directly would raise on agents built before the attribute existed.
        value = kw.value
        assert isinstance(value, ast.Call) and isinstance(value.func, ast.Name) \
            and value.func.id == "getattr", (
                f"{rel_path}: authorize= is not a getattr(...) read"
            )
        assert len(value.args) >= 2 and isinstance(value.args[1], ast.Constant) \
            and value.args[1].value == "_tool_authorizer", (
                f"{rel_path}: authorize= does not read the agent's _tool_authorizer"
            )


def test_the_subprocess_lane_stays_without_an_authorizer():
    """The named boundary, asserted: the workflow CLI subprocess has no agent object,
    so it must NOT grow an authorize= keyword that would only ever be None with a
    misleading shape suggesting coverage."""
    calls = _engine_calls("vaf/cli/cmd/workflow.py")
    assert calls, "vaf/cli/cmd/workflow.py: no WorkflowEngine(...) call found"
    for call in calls:
        assert _authorize_keyword(call) is None, (
            "the subprocess lane grew an authorize= keyword; if a real authorizer "
            "source appeared for this lane, update the module docstring's boundary too"
        )
