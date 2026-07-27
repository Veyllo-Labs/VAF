# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The workflow lane can distribute identity by declaration instead of by a name list.

The chat dispatcher asks each tool what it needs (``BaseTool.identity_kwargs``). The engine
carried its own hardcoded list of tool names instead - a second registry copy, and the exact
shape that drifts: a tool added to one list and not the other. Worse than drift, it could not
express one thing at all. The list has no branch that sets ``user_role``, so a workflow step
was never role-aware, while the same tool in chat was.

MEASURED before the switch was built, over all 132 tool classes - list versus declaration:

    41 identical      48 gain identity      0 LOSE identity

That zero is what makes this defensible: the change is purely additive. Among the gains are
every filesystem tool, the GitHub tools, ``browser_agent``, the skill and automation tools -
and ``send_mail``, which today receives scope and username but never the role, so its jail
resolves admin-ness from the scope half alone.

It is still a behaviour change, so it rides the same key that decides whether this lane has
an identity at all. One key for both halves on purpose: separate keys would allow a
combination nobody designed - a real identity distributed by the old list.

The legacy branch is kept byte-for-byte, and not as courtesy. It is the rollback, it is the
default, and five existing source-anchored guards still read it - verified by breaking it on
purpose, which turns two of them red.
"""
from unittest.mock import patch

import pytest

from vaf.workflows.engine import WorkflowEngine, WorkflowStep

SCOPE = "deadbeef-0000-0000-0000-000000000000"   # synthetic; never a real scope UUID
IDENTITY = ("user_scope_id", "username", "user_role")


def _mode(mode):
    return patch("vaf.core.config.Config.get",
                 side_effect=lambda k, d=None: mode if k == "workflow_identity_injection" else d)


def _step(tool_name, declares, mode, *, args_template=None, role="user"):
    """Run one real workflow step and report the identity the tool received."""
    seen = {}

    class _Recording:
        name = tool_name
        identity_kwargs = declares

        def run(self, **kwargs):
            seen.update(kwargs)
            return "ok"

    with _mode(mode):
        engine = WorkflowEngine(
            tools={tool_name: _Recording()}, callback=lambda *a, **k: None,
            user_scope_id=SCOPE, username="tenant", user_role=role,
        )
        engine.execute(
            [WorkflowStep(tool=tool_name, input_template="x", output_name="o",
                          args_template=args_template)],
            variables={},
        )
    return {k: v for k, v in seen.items() if k in IDENTITY}


# ── the default is unchanged ─────────────────────────────────────────────────

def test_a_tool_off_the_name_list_gets_nothing_under_legacy():
    """The old behaviour, stated plainly: read_file ran in a workflow with no identity at
    all, which is why write_file was never jailed there."""
    assert _step("read_file", ("user_role", "user_scope_id"), "legacy") == {}


def test_a_tool_on_the_name_list_is_unchanged_under_legacy():
    assert _step("memory_save", ("user_scope_id",), "legacy") == {"user_scope_id": SCOPE}


@pytest.mark.parametrize("mode", ["legacy", "off", "", None, "something-else"])
def test_only_declared_switches_the_distribution(mode):
    assert _step("read_file", ("user_role", "user_scope_id"), mode) == {}


# ── what declared changes ────────────────────────────────────────────────────

def test_a_tool_off_the_list_now_receives_what_it_declares():
    """THE point. 48 tools are in this position, including every filesystem tool."""
    assert _step("read_file", ("user_role", "user_scope_id"), "declared") == {
        "user_role": "user", "user_scope_id": SCOPE}


def test_the_role_arrives_for_the_first_time():
    """The name list has no branch that sets user_role - grep it. So a workflow step could
    not be role-aware, and send_mail's jail decided admin-ness from the scope half alone
    while the same tool in chat had both."""
    legacy = _step("send_mail", ("user_role", "user_scope_id", "username"), "legacy")
    declared = _step("send_mail", ("user_role", "user_scope_id", "username"), "declared")
    assert "user_role" not in legacy
    assert declared["user_role"] == "user"
    assert set(declared) - set(legacy) == {"user_role"}


def test_a_tool_that_declares_nothing_still_gets_nothing():
    """The safe direction survives the switch: not declaring means not receiving."""
    assert _step("some_quiet_tool", (), "declared") == {}


def test_agreement_stays_agreement():
    """Where the list and the declaration already agreed, nothing moves - that is 41 of the
    132 classes, and a difference there would mean the measurement was wrong."""
    for mode in ("legacy", "declared"):
        assert _step("memory_save", ("user_scope_id",), mode) == {"user_scope_id": SCOPE}


def test_a_model_authored_step_arg_cannot_spoof_the_identity():
    """Step args come from a MODEL in the run_temp lane, so the identity must be ASSIGNED
    over them - the same guarantee the chat dispatcher gives."""
    seen = _step("read_file", ("user_role", "user_scope_id"), "declared",
                 args_template={"user_role": "admin",
                                "user_scope_id": "ffffffff-0000-0000-0000-000000000000"})
    assert seen == {"user_role": "user", "user_scope_id": SCOPE}


def test_an_absent_role_stays_absent_rather_than_becoming_admin():
    """Four of the seven consumers still carry no role. None must keep the previous answer -
    the jail then resolves admin-ness from the scope half - and must never be filled in with
    a default that would widen access."""
    seen = _step("read_file", ("user_role", "user_scope_id"), "declared", role=None)
    assert seen["user_role"] is None
    assert seen["user_scope_id"] == SCOPE


# ── the two halves share one key, deliberately ───────────────────────────────

def test_one_key_governs_both_halves():
    """Whether the four identity-less consumers pass one, and how the engine distributes it,
    are the same question. Two keys would allow a real identity distributed by the old list -
    a combination nobody designed and nobody would test."""
    import inspect

    from vaf.workflows.engine import _identity_mode, identity_for_engine

    assert "_identity_mode()" in inspect.getsource(identity_for_engine)
    assert "workflow_identity_injection" in inspect.getsource(_identity_mode)


def test_the_legacy_branch_is_still_present():
    """It is the rollback and the default, and five source-anchored guards read it. Deleting
    it early would make the switch a one-way door."""
    import inspect

    src = inspect.getsource(WorkflowEngine.execute)
    assert '"memory_save", "memory_search"' in src
    assert "schedule_reminder" in src
