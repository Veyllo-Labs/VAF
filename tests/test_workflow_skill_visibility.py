# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Regression: a workflow step must not read another tenant's private skill.

Skill visibility is one of the very few per-user gates in VAF that actually runs at tool
level: ``use_skill`` and ``read_skill`` call ``skills_registry.is_skill_visible_to_user``
before handing anything back, and a skill whose ``shared_with`` names other scopes is
invisible to everyone else.

The gate reads an ABSENT ``user_scope_id`` as admin (``get_visible_skill_ids_for_user``
returns everything for ``None`` - correct for the tokenless desktop, the CLI and
automations, which have no role claim). The agent dispatcher therefore ASSIGNS the scope
from the session, driven by each tool's ``identity_kwargs`` declaration.

The workflow engine does not read that declaration; it carries its own hardcoded list of
tool names, and ``use_skill``/``read_skill`` were simply never on it. So the same tool that
refuses a foreign skill in chat handed it over inside a workflow step - not because the gate
was weak, but because nobody told it who was asking.

Scope of the fix, stated honestly: this closes the hole wherever the engine HAS an identity
(run_temp carries the chat user, automations carry the task owner, resume carries the paused
record). Three construction sites pass no identity at all, ``execute_workflow`` among them,
and those still resolve to admin. That gap is not skill-specific and is fixed where the
identity is supplied, not here.

These are the first tests in the suite that actually EXECUTE the engine's injection rather
than grepping its source text.
"""
import pytest

from vaf.workflows.engine import WorkflowEngine, WorkflowStep

# Synthetic scopes (public-repo hygiene: never a real scope UUID).
TENANT = "deadbeef-0000-0000-0000-000000000000"
OTHER_TENANT = "cafe1234-0000-0000-0000-000000000000"


class _RecordingSkillTool:
    """Stands in for use_skill/read_skill: records the scope the engine handed it."""

    def __init__(self):
        self.seen = "<never called>"

    def run(self, **kwargs):
        self.seen = kwargs.get("user_scope_id", "<absent>")
        return "skill output"


def _run_step(tool_name, tool, **engine_kwargs):
    engine = WorkflowEngine(tools={tool_name: tool}, callback=lambda *a, **k: None,
                            **engine_kwargs)
    result = engine.execute(
        [WorkflowStep(tool=tool_name, input_template="do the thing", output_name="out")],
        variables={},
    )
    assert result.success, result.error
    return tool.seen


@pytest.mark.parametrize("tool_name", ["use_skill", "read_skill"])
def test_a_workflow_step_carries_the_callers_scope_into_the_skill_gate(tool_name):
    """THE regression: without this the gate saw None and treated the step as admin."""
    tool = _RecordingSkillTool()
    seen = _run_step(tool_name, tool, user_scope_id=TENANT, username="tenant")
    assert seen == TENANT, (
        f"{tool_name} ran with {seen!r} instead of the caller's scope - the skill "
        f"visibility gate would treat this step as admin"
    )


def test_read_skill_also_gets_the_username_it_declares():
    """read_skill declares ("user_scope_id", "username"); the engine must supply both or the
    editable-flag half of its answer is computed for the wrong person."""
    seen_kwargs = {}

    class _Tool:
        def run(self, **kwargs):
            seen_kwargs.update(kwargs)
            return "ok"

    engine = WorkflowEngine(tools={"read_skill": _Tool()}, callback=lambda *a, **k: None,
                            user_scope_id=TENANT, username="tenant")
    engine.execute([WorkflowStep(tool="read_skill", input_template="x", output_name="o")],
                   variables={})
    assert seen_kwargs.get("user_scope_id") == TENANT
    assert seen_kwargs.get("username") == "tenant"


@pytest.mark.parametrize("tool_name", ["use_skill", "read_skill"])
def test_a_model_authored_step_arg_cannot_spoof_the_scope(tool_name):
    """The step args come from a MODEL (run_temp authors them per turn), so the injection
    must ASSIGN over whatever the step template carried - never defer to it."""
    tool = _RecordingSkillTool()
    engine = WorkflowEngine(tools={tool_name: tool}, callback=lambda *a, **k: None,
                            user_scope_id=TENANT, username="tenant")
    engine.execute(
        [WorkflowStep(tool=tool_name, input_template="x", output_name="o",
                      args_template={"user_scope_id": OTHER_TENANT})],
        variables={},
    )
    assert tool.seen == TENANT, "a step arg overrode the caller's real scope"


@pytest.mark.parametrize("tool_name", ["use_skill", "read_skill"])
def test_a_direct_consumer_without_an_identity_is_unchanged(tool_name):
    """The CLI workflow subprocess and the @workflow_id lane construct the engine with no
    identity at all. Their behavior must be exactly what it was before - the admin reading
    documented in skills_registry.get_visible_skill_ids_for_user."""
    tool = _RecordingSkillTool()
    seen = _run_step(tool_name, tool)
    assert seen in (None, "<absent>"), (
        "an identity-less lane suddenly carries a scope; that is a behavior change, not "
        "this fix"
    )
