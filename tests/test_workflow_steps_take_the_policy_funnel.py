# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Workflow steps run through the shared funnel - policy, allowlist and authorizer hold.

WHAT A STEP COULD DO BEFORE THIS: the engine called `run_tool_bounded` directly, so a
workflow step was never asked the three questions every chat tool call answers - may
this caller use the tool at all (`admin_only`, channel policy), is the tool on this
ACCOUNT's allowlist, and does the embedder's authorizer object. "Which door did the
caller come through" was still a security answer for workflows.

WHAT HOLDS NOW, and the deliberate exceptions: non-spawn steps go through ONE
`ToolCaller` built per run (policy, account allowlist, authorizer deny, argument
repair, declared identity). The confirmation gate stays OFF for this lane
(`gate_enabled=False` - the lane has run gated tools without asking since it existed,
and taking that away is a separate decision), which also means an authorizer's `ask()`
degrades to no opinion here - only `deny()` binds. The spawn branch stays off the
funnel (spawn + IPC wait is not a tool run). The rollback modes
(`workflow_identity_injection` = legacy/off) restore the ENTIRE pre-C2 lane: name-list
identity AND absence of per-step policy - under rollback the funnel is not even built.
"""
from unittest.mock import patch

import pytest

from vaf.core.tool_dispatch import set_account_allowlist_resolver
from vaf.workflows.engine import WorkflowEngine, WorkflowStep

SCOPE = "ab12cd34-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _registry_isolated():
    """Save/RESTORE the process-global resolver registry, never bare-clear it."""
    from vaf.core.tool_dispatch import get_account_allowlist_resolver
    previous = get_account_allowlist_resolver()
    set_account_allowlist_resolver(None)
    yield
    set_account_allowlist_resolver(previous)


def _mode(value: str):
    """Pin the identity switch for one test (pattern: test_workflow_declared_identity)."""
    from vaf.core.config import Config
    real_get = Config.get

    def fake_get(key, default=None):
        if key == "workflow_identity_injection":
            return value
        return real_get(key, default)

    return patch.object(Config, "get", staticmethod(fake_get))


class _Probe:
    """Minimal contract-complete tool; records whether it ran."""
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


class _AdminOnly(_Probe):
    name = "admin_probe"
    admin_only = True


def _run_step(tool, *, tool_name=None, engine_kwargs=None, steps=None):
    name = tool_name or tool.name
    engine = WorkflowEngine(tools={name: tool}, callback=lambda *a, **k: None,
                            **(engine_kwargs or {}))
    step_list = steps or [WorkflowStep(tool=name, input_template="x", output_name="out1")]
    return engine, engine.execute(step_list, variables={})


# ── policy is live for a step ─────────────────────────────────────────────────────────

def test_an_admin_only_step_is_refused_for_a_non_admin():
    tool = _AdminOnly()
    _, result = _run_step(tool, engine_kwargs={"user_scope_id": SCOPE, "user_role": "user"})

    assert not result.success
    assert tool.calls == [], "the tool ran despite the admin_only block"
    assert "requires an admin session" in str(result.error or "")


def test_an_admin_identity_still_runs_it():
    tool = _AdminOnly()
    _, result = _run_step(tool, engine_kwargs={"user_scope_id": SCOPE, "user_role": "admin"})

    assert result.success, result.error
    assert len(tool.calls) == 1


def test_a_security_error_fails_the_step_and_is_never_chained():
    """A refusal is a step FAILURE, not step output: with stop_on_error the run stops,
    and the next step's tool never sees the refusal text as its input."""
    blocked = _AdminOnly()
    downstream = _Probe()
    engine = WorkflowEngine(tools={"admin_probe": blocked, "probe": downstream},
                            callback=lambda *a, **k: None,
                            user_scope_id=SCOPE, user_role="user")
    result = engine.execute([
        WorkflowStep(tool="admin_probe", input_template="x", output_name="out1"),
        WorkflowStep(tool="probe", input_template="{out1}", output_name="out2"),
    ], variables={})

    assert not result.success
    assert blocked.calls == []
    assert downstream.calls == [], "the refusal was chained into the next step"


# ── the account allowlist holds inside a step ─────────────────────────────────────────

def test_the_account_allowlist_holds_inside_a_workflow_step():
    set_account_allowlist_resolver(lambda scope: {"something_else"})
    tool = _Probe()
    _, result = _run_step(tool, engine_kwargs={"user_scope_id": SCOPE, "user_role": "user"})

    assert not result.success
    assert tool.calls == []
    assert "not enabled for your account" in str(result.error or "")


def test_an_admin_is_exempt_from_the_allowlist():
    set_account_allowlist_resolver(lambda scope: {"something_else"})
    tool = _Probe()
    _, result = _run_step(tool, engine_kwargs={"user_scope_id": SCOPE, "user_role": "admin"})

    assert result.success, result.error
    assert len(tool.calls) == 1


def test_an_authorizer_allow_cannot_lift_an_account_ban():
    set_account_allowlist_resolver(lambda scope: {"something_else"})
    tool = _Probe()
    _, result = _run_step(tool, engine_kwargs={
        "user_scope_id": SCOPE, "user_role": "user",
        "authorize": lambda req: req.allow(),
    })

    assert not result.success
    assert tool.calls == []


def test_an_authorizer_deny_refuses_the_step():
    tool = _Probe()
    _, result = _run_step(tool, engine_kwargs={
        "user_scope_id": SCOPE, "user_role": "user",
        "authorize": lambda req: req.deny("not on this plan"),
    })

    assert not result.success
    assert tool.calls == []
    assert "not on this plan" in str(result.error or "")


# ── the rollback restores the ENTIRE pre-C2 lane ─────────────────────────────────────

def test_rollback_restores_the_entire_pre_c2_lane():
    """Under legacy the funnel is not even entered: an admin_only tool runs again for a
    non-admin (the pre-C2 absence of policy), proven with a bomb on ToolCaller.execute."""
    import vaf.core.tool_dispatch as td

    tool = _AdminOnly()
    with _mode("legacy"), patch.object(
        td.ToolCaller, "execute",
        side_effect=AssertionError("funnel entered under rollback"),
    ):
        _, result = _run_step(tool, engine_kwargs={"user_scope_id": SCOPE, "user_role": "user"})

    assert result.success, result.error
    assert len(tool.calls) == 1, "legacy mode no longer restores the pre-C2 dispatch"


# ── the funnel is configured for THIS lane ───────────────────────────────────────────

def test_the_engine_configures_the_funnel_for_this_lane():
    """Every dropped constructor kwarg is its own red (librarian _Capturing pattern).

    Patched on vaf.core.tool_dispatch, not on the engine module: execute() imports
    ToolCaller function-locally at call time, so the registry module is the seam."""
    import vaf.core.tool_dispatch as td
    from vaf.core.bounded_run import SELF_SUPERVISED_TOOLS
    from vaf.core.tool_dispatch import ToolCaller
    from vaf.workflows.engine import _workflow_step_timeout

    captured = {}

    class _Capturing(ToolCaller):
        def __init__(self, tools, **kwargs):
            captured.update(kwargs)
            super().__init__(tools, **kwargs)

    def _authorize(req):
        pass

    def _stop():
        return False

    tool = _Probe()
    engine = WorkflowEngine(tools={"probe": tool}, callback=lambda *a, **k: None,
                            user_scope_id=SCOPE, username="tenant", user_role="user",
                            authorize=_authorize)
    engine._session_id = "web_1234"
    with patch.object(td, "ToolCaller", _Capturing):
        result = engine.execute(
            [WorkflowStep(tool="probe", input_template="x", output_name="out1")],
            variables={}, check_stop=_stop,
        )

    assert result.success, result.error
    assert captured["gate_enabled"] is False
    assert captured["max_result_chars"] is None
    assert captured["timeout_for"] is _workflow_step_timeout
    assert captured["self_supervised"] == SELF_SUPERVISED_TOOLS - {"browser_agent"}
    assert captured["stop_check"] is _stop
    assert captured["user_scope_id"] == SCOPE
    assert captured["username"] == "tenant"
    assert captured["user_role"] == "user"
    assert captured["session_id"] == "web_1234"
    assert captured["authorize"] is _authorize
    assert "on_event" not in captured, "this lane reports through its own callback protocol"


def test_a_step_result_is_not_truncated_on_its_way_to_the_next_step():
    """The funnel's default cut is 2000 chars; this lane chains step outputs and must
    pass max_result_chars=None. A digest-level cap test would not catch a 2000 cut."""
    class _Long(_Probe):
        name = "long_tool"

        def run(self, **kwargs):
            self.calls.append(kwargs)
            return "y" * 4000

    long_tool = _Long()
    receiver = _Probe()
    engine = WorkflowEngine(tools={"long_tool": long_tool, "probe": receiver},
                            callback=lambda *a, **k: None)
    result = engine.execute([
        WorkflowStep(tool="long_tool", input_template="x", output_name="out1"),
        WorkflowStep(tool="probe", input_template="{out1}", output_name="out2"),
    ], variables={})

    assert result.success, result.error
    received = str(receiver.calls[0])
    assert "y" * 4000 in received, "the step output was truncated on its way to the next step"


# ── structural guards (AST, never substring) ─────────────────────────────────────────

def _execute_ast():
    import ast
    import inspect

    src = inspect.getsource(WorkflowEngine.execute)
    import textwrap
    return ast.parse(textwrap.dedent(src))


def test_exactly_one_toolcaller_construction_site():
    import ast

    calls = [
        node for node in ast.walk(_execute_ast())
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == "ToolCaller"
    ]
    assert len(calls) == 1, (
        f"expected exactly ONE ToolCaller construction in WorkflowEngine.execute, "
        f"found {len(calls)} - a second one is a second place to forget an argument"
    )


def test_the_only_raw_tool_run_in_execute_is_the_spawn_branch():
    import ast

    raw_runs = [
        node for node in ast.walk(_execute_ast())
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute) and node.func.attr == "run"
        and isinstance(node.func.value, ast.Name) and node.func.value.id == "tool"
    ]
    assert len(raw_runs) == 1, (
        f"expected exactly one raw tool.run() in execute (the spawn branch), "
        f"found {len(raw_runs)} - a new one is a funnel bypass"
    )
