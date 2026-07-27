# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Regression: the tool gate answered "is this an admin" differently from everything else.

VAF has one definition of admin - ``config.is_admin_identity`` - with two halves: the DB
role, or the configured local-admin scope for the machine owner who carries no role claim.
It tolerates case and padding on the role, deliberately, because the role travels through
JWT claims and session metadata and the roughly thirty gates that read it all lowercase it.

The tool dispatcher did not. It rebuilt the check inline with an exact ``== "admin"``
comparison, which meant a role spelled "Admin" lifted the file jail while leaving every
``admin_only`` tool blocked. Same person, same request, two answers - in the one place that
decides whether a tool may run at all.

An earlier round gave the file gates the shared rule precisely to end this class of
divergence (``tests/test_admin_identity_is_role_aware.py``). This spot was missed because it
sat inline in a 600-line method instead of behind a name; extracting it for the dispatch
funnel is what made it visible.

The direction is unusual for a security change - it GRANTS access - so the trust chain
matters and is pinned below: the role only ever arrives as a claim from a signature-verified
JWT issued from ``LocalUser.role``, and the API lowercases it on create and on update, so no
API-created account can reach the tolerant branch at all. The column itself constrains
nothing, which is why the rule and not the storage has to be the guarantee.
"""
import pytest

from vaf.core.config import get_local_admin_scope_id, is_admin_identity
from vaf.core.tool_dispatch import policy_admin_flag

# Synthetic scopes (public-repo hygiene: never a real scope UUID).
SECOND_ADMIN = "abcdef12-0000-0000-0000-000000000000"
PLAIN_USER = "12345678-1234-1234-1234-123456789abc"


@pytest.mark.parametrize("role", ["admin", "Admin", "ADMIN", " admin ", "user", "", None])
@pytest.mark.parametrize("scope", [None, SECOND_ADMIN, PLAIN_USER])
def test_the_gate_and_the_shared_rule_never_disagree(role, scope):
    """THE regression, as a total function rather than a handful of examples: every
    combination must produce the same answer on both sides."""
    assert policy_admin_flag(role, scope) is is_admin_identity(role, scope), (
        f"role={role!r} scope={scope!r}: the tool gate and the file gates disagree about "
        f"who is an admin"
    )


@pytest.mark.parametrize("role", ["Admin", "ADMIN", " admin "])
def test_a_case_variant_role_is_admin_at_the_tool_gate_too(role):
    """The concrete divergence that existed: these lifted the file jail and were refused
    admin_only tools."""
    assert policy_admin_flag(role, PLAIN_USER) is True


def test_the_machine_owner_without_a_role_stays_admin():
    """The scope half is not redundant - the tokenless desktop, the CLI and automations
    resolve to the local-admin scope and carry no role at all."""
    assert policy_admin_flag(None, get_local_admin_scope_id()) is True


def test_a_second_admin_account_is_admin():
    """User management supports more than one admin; a second one carries its OWN scope, so
    a scope-only check would demote it."""
    assert policy_admin_flag("admin", SECOND_ADMIN) is True


@pytest.mark.parametrize("role", ["administrator", "superadmin", "adm", "user", None, ""])
def test_nothing_else_becomes_admin(role):
    """Tolerating case must not have widened into prefix or substring matching."""
    assert policy_admin_flag(role, PLAIN_USER) is False


def test_it_fails_closed_rather_than_raising():
    """execute_tool promises never to raise for tool failures, and this runs before the
    policy decision - an exception here would take the whole dispatch with it."""
    assert policy_admin_flag(object(), object()) is False  # type: ignore[arg-type]


def test_the_dispatcher_has_no_second_admin_definition_left():
    """The deletion this change is for: an inline reconstruction would drift again, and it
    is invisible in review because it reads exactly like the shared rule."""
    import inspect

    from vaf.core.agent import Agent

    src = inspect.getsource(Agent.execute_tool)
    assert 'get_local_admin_scope_id' not in src, (
        "execute_tool resolves the local-admin scope itself again - that is how the two "
        "definitions drifted apart the first time"
    )
    assert '== "admin"' not in src, "execute_tool compares a role by hand again"
