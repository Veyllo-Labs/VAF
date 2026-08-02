# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The per-user WORKFLOW list is enforced - at the one place every lane converges.

MEASURED BEFORE BUILDING: `permissions["workflows"]` was written by the admin routes and
read by NOTHING - the funnel checks tool NAMES, so the per-user tool enforcement could
only ever gate `execute_workflow` as a whole, never a single template. Enforcing per
template id needs a START gate, and the right place is `WorkflowEngine.execute()`: all
seven construction sites end up there, including both resume lanes, so one check covers
every entry and a revocation between pause and resume bites. Two saved-template lanes
did not carry the template id at all before this (the router lane and the CLI resume) -
the gate would have been blind there; they pass it now, pinned below.

SEMANTICS mirror the tool allowlist, each half where it belongs: exemptions (no scope,
admin via the shared policy_admin_flag rule, NO template id = ad-hoc run) live at the
gate; the resolver only answers "which template ids for this scope"; absent/EMPTY stored
list = unrestricted ([] is the API model's creation default); a RAISING registered
resolver refuses; the harness resolver itself never raises (DB down = unrestricted,
desktop-correct).
"""
import ast
from pathlib import Path
from unittest.mock import patch

import pytest

from vaf.core.tool_dispatch import set_workflow_allowlist_resolver
from vaf.workflows.engine import WorkflowEngine, WorkflowStep

ROOT = Path(__file__).resolve().parents[1]
SCOPE = "ab12cd34-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _registry_isolated():
    """Save/RESTORE the process-global registry, never bare-clear it."""
    from vaf.core.tool_dispatch import get_workflow_allowlist_resolver
    previous = get_workflow_allowlist_resolver()
    set_workflow_allowlist_resolver(None)
    yield
    set_workflow_allowlist_resolver(previous)


class _Probe:
    name = "probe"
    description = "records calls"
    permission_level = "read"
    parameters = {"type": "object", "properties": {}}
    identity_kwargs = ()

    def __init__(self):
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return "PROBE_RAN"


def _run(template_id, *, role="user", scope=SCOPE, tool=None):
    tool = tool or _Probe()
    engine = WorkflowEngine(tools={"probe": tool}, callback=lambda *a, **k: None,
                            user_scope_id=scope, user_role=role)
    engine._template_id = template_id
    result = engine.execute(
        [WorkflowStep(tool="probe", input_template="x", output_name="out1")],
        variables={},
    )
    return tool, result


# ── the gate itself ──────────────────────────────────────────────────────────────────

def test_a_blocked_template_is_refused_before_any_step_runs():
    set_workflow_allowlist_resolver(lambda scope: {"some_other_workflow"})
    tool, result = _run("daily_report")

    assert not result.success
    assert tool.calls == [], "a step ran despite the blocked template"
    assert "not enabled for your account" in str(result.error or "")


def test_an_allowed_template_runs():
    set_workflow_allowlist_resolver(lambda scope: {"daily_report"})
    tool, result = _run("daily_report")

    assert result.success, result.error
    assert len(tool.calls) == 1


def test_an_admin_is_never_restricted():
    set_workflow_allowlist_resolver(lambda scope: {"some_other_workflow"})
    tool, result = _run("daily_report", role="admin")

    assert result.success, result.error


def test_an_ad_hoc_run_without_a_template_id_is_not_checked():
    """run_temp and automation inline steps carry no template id; their lever is the
    TOOL permission of the lane that builds them, and the resolver is never consulted."""
    called = []
    set_workflow_allowlist_resolver(lambda scope: called.append(scope) or frozenset())
    tool, result = _run("")

    assert result.success, result.error
    assert called == [], "the resolver was consulted for an ad-hoc run"


def test_a_scopeless_engine_is_not_checked():
    called = []
    set_workflow_allowlist_resolver(lambda scope: called.append(scope) or frozenset())
    tool, result = _run("daily_report", scope=None)

    assert result.success, result.error
    assert called == []


def test_no_registered_resolver_means_unrestricted():
    tool, result = _run("daily_report")

    assert result.success, result.error


def test_a_raising_registered_resolver_refuses():
    def _boom(scope):
        raise RuntimeError("backend exploded")

    set_workflow_allowlist_resolver(_boom)
    tool, result = _run("daily_report")

    assert not result.success
    assert tool.calls == []
    assert "resolver failed" in str(result.error or "")


def test_a_revocation_between_pause_and_resume_bites():
    """resume_workflow re-enters execute(), so the gate fires on the resumed half too."""
    set_workflow_allowlist_resolver(lambda scope: {"some_other_workflow"})
    tool = _Probe()
    engine = WorkflowEngine(tools={"probe": tool}, callback=lambda *a, **k: None,
                            user_scope_id=SCOPE, user_role="user")
    engine._template_id = "daily_report"
    result = engine.execute(
        [WorkflowStep(tool="probe", input_template="x", output_name="out1")],
        variables={},
    )

    assert not result.success
    assert tool.calls == []


# ── the harness resolver's pinned semantics ─────────────────────────────────────────

@pytest.mark.parametrize("perms_value", [None, {}, {"workflows": None}, {"workflows": []}, {"other": 1}])
def test_absent_or_empty_means_unrestricted(monkeypatch, perms_value):
    import vaf.auth.permissions as perms

    monkeypatch.setattr(perms, "_wf_cache", {})
    monkeypatch.setattr(perms, "_lookup_allowed_workflows",
                        lambda scope: perms._workflows_from_permissions(perms_value))

    assert perms.resolve_allowed_workflows(SCOPE) is None


def test_a_real_list_becomes_the_allowlist(monkeypatch):
    import vaf.auth.permissions as perms

    monkeypatch.setattr(perms, "_wf_cache", {})
    monkeypatch.setattr(perms, "_lookup_allowed_workflows",
                        lambda scope: perms._workflows_from_permissions({"workflows": ["a", "b", ""]}))

    assert perms.resolve_allowed_workflows(SCOPE) == frozenset({"a", "b"})


def test_the_harness_resolver_never_raises(monkeypatch):
    import vaf.auth.permissions as perms

    def _boom(scope):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(perms, "_wf_cache", {})
    monkeypatch.setattr(perms, "_lookup_allowed_workflows", _boom)
    assert perms.resolve_allowed_workflows(SCOPE) is None


def test_invalidation_clears_both_allowlists(monkeypatch):
    import vaf.auth.permissions as perms

    monkeypatch.setattr(perms, "_cache", {SCOPE: (0.0, frozenset({"t"}))})
    monkeypatch.setattr(perms, "_wf_cache", {SCOPE: (0.0, frozenset({"w"}))})
    perms.invalidate_permissions_cache(SCOPE)

    assert SCOPE not in perms._cache
    assert SCOPE not in perms._wf_cache, (
        "the admin update route writes tools and workflows in ONE save; an invalidation "
        "that clears only one cache lets a workflow revocation idle out the TTL"
    )


# ── the two lanes that were blind ────────────────────────────────────────────────────

def _assigns_template_id(rel_path: str) -> bool:
    tree = ast.parse((ROOT / rel_path).read_bytes())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Attribute) and target.attr == "_template_id":
                return True
    return False


@pytest.mark.parametrize("rel_path", [
    "vaf/core/agent.py",          # the router lane: runs saved templates
    "vaf/cli/cmd/run.py",         # the CLI resume lane: PausedWorkflow carries the id
    "vaf/tools/workflow_executor.py",
    "vaf/cli/cmd/workflow.py",
    "vaf/workflows/resume.py",
])
def test_every_saved_template_lane_carries_the_id(rel_path):
    assert _assigns_template_id(rel_path), (
        f"{rel_path}: no _template_id assignment - the start gate is blind on this lane; "
        f"a saved-template run there cannot be governed by the per-user workflow list"
    )
