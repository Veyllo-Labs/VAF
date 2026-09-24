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


def test_every_admin_check_in_the_tree_asks_the_shared_rule():
    """The same class one layer up: the messenger routes carried three admin checks of their
    own (`_is_telegram_admin`, `_is_whatsapp_admin`, `_is_discord_admin`), the supervisor
    routes a fourth, and `require_admin` knew only the role. They disagreed about a second
    admin account. Every function that answers "is this an admin" now asks
    `is_admin_identity`, and a new one that does not fails here. Names about the LOCAL admin
    (`is_local_admin_lane`, `is_local_admin_caller`) answer a different question - whose
    lane, whose files - and are not admin checks."""
    import ast
    import re
    from pathlib import Path

    name = re.compile(r"^_?(is_\w*admin|require_admin|caller_is_admin)$")
    root = Path(__file__).resolve().parent.parent / "vaf"
    offenders = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not name.match(node.name) or "local_admin" in node.name or node.name == "is_admin_identity":
                continue
            calls = {getattr(c.func, "id", None) or getattr(c.func, "attr", None)
                     for c in ast.walk(node) if isinstance(c, ast.Call)}
            if "is_admin_identity" not in calls:
                offenders.append(f"{path.relative_to(root.parent).as_posix()}:{node.lineno} {node.name}")
    assert not offenders, "an admin check with a rule of its own:\n" + "\n".join(offenders)


# Comparisons of a role to "admin" that are NOT an answer to "does the caller have admin
# rights", each with the reason it stays. Keyed by file and the comparison's own source, so
# a new one fails the test below and has to be either the shared rule or a line here.
_ROLE_COMPARISONS_THAT_ARE_NOT_THE_RULE = {
    ("vaf/core/config.py", 'str(role or "").strip().lower() == "admin"'): "the rule itself",
    ("vaf/core/config.py", '(role or "").lower() == "admin"'):
        "config_for_user's role parameter; its callers pass 'admin' after asking the rule",
    ("vaf/api/auth_routes.py", 'LocalUser.role == "admin"'): "a query over account rows",
    ("vaf/api/user_routes.py", 'LocalUser.role == "admin"'): "a query over account rows",
    ("vaf/auth/user_admin.py", 'LocalUser.role == "admin"'): "a query over account rows",
    ("vaf/core/web_server.py", 'u.role != "admin"'): "listing account rows, not the caller",
    ("vaf/api/user_routes.py", '(target_role or "").lower() == "admin"'):
        "the TARGET account's stored role (last-admin protection), not the caller's rights",
    ("vaf/api/user_routes.py", 'str(new_role).lower() != "admin"'):
        "the role being assigned (a demotion), not the caller's rights",
    ("vaf/api/auth_routes.py", 'user.role != "admin"'):
        "the localhost 2FA exemption: an authentication exemption decides on the stored role "
        "alone and is never widened by a scope match",
    ("vaf/core/web_server.py", 'payload.get("role") != "admin"'):
        "the same localhost 2FA exemption on the WebSocket",
    ("vaf/tools/automation.py", '(user_role or "").strip().lower() == "admin"'):
        "the role half on its own: the local-admin scope is answered by the branch before it, "
        "which picks a different store",
}


def test_no_role_is_compared_to_admin_by_hand():
    """The inline copies: 26 in the WebSocket handlers and two more elsewhere spelled the rule
    by hand, with a case-exact role comparison. A comparison of anything role-shaped to
    "admin" is now either the shared rule or a listed exception with its reason."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    found = []
    for path in sorted((root / "vaf").rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        rel = path.relative_to(root).as_posix()
        for node in ast.walk(ast.parse(src, filename=rel)):
            if not (isinstance(node, ast.Compare) and len(node.ops) == 1
                    and isinstance(node.ops[0], (ast.Eq, ast.NotEq))):
                continue
            sides = [node.left, *node.comparators]
            if not any(isinstance(s, ast.Constant) and s.value == "admin" for s in sides):
                continue
            other = next(s for s in sides if not (isinstance(s, ast.Constant) and s.value == "admin"))
            if "role" not in (ast.get_source_segment(src, other) or "").lower():
                continue
            key = (rel, ast.get_source_segment(src, node))
            if key not in _ROLE_COMPARISONS_THAT_ARE_NOT_THE_RULE:
                found.append(f"{rel}:{node.lineno}: {key[1]}")
    assert not found, ("a role compared to 'admin' by hand; ask config.is_admin_identity, or list "
                       "it with its reason:\n" + "\n".join(found))
