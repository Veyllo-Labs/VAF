# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""An admin must not be able to saw off the branch they sit on - and nobody the last one.

FOUND LIVE (2026-08-01), as the second half of the presence/account confusion: the user
manager offered every dangerous self-edit without a guard anywhere. An admin could
deactivate their own account or remove their own admin role - and if they were the last
admin, nothing left in the system could repair it.

THREE DOORS TO THE SAME LOCKOUT, which is why one function serves both routes:
deactivate, demote, delete. The delete route DID have a guard, and it counted admin
ROWS - so with a deactivated admin row present, the last ACTIVE admin could be deleted.
That leaves an instance where no admin can sign in, which flips `needs_setup` back on and
re-opens the unauthenticated bootstrap endpoint to the LAN. "Active" is therefore the
operative word in every rule here: an existing-but-deactivated admin cannot log in to
repair anything, so counting rows would call a system repairable that is not.

THE WIRING IS PART OF THE TEST, not an afterthought. The first version of this guard read
the caller's id from `admin["sub"]`, a key the auth middleware does not publish (it maps
the JWT `sub` claim onto `user_id`), so the id half of the self-check was dead on every
request and every test here stayed green - the guard was fed its arguments directly.
`test_the_route_reads_the_id_key_the_middleware_actually_publishes` closes exactly that,
by driving the identity helper with the dict shape the middleware really builds.
"""
import ast
import inspect
import textwrap

from vaf.api.user_routes import (
    caller_identity,
    count_other_active_admins,
    delete_user,
    refuse_dangerous_user_change,
    update_user,
)

ME = "11111111-0000-4000-8000-000000000001"
OTHER = "22222222-0000-4000-8000-000000000002"


def _refuse(**kw):
    defaults = dict(
        caller_id=ME, caller_scope="scope-me",
        target_id=OTHER, target_scope="scope-other",
        target_role="user", target_active=True,
        new_role=None, new_is_active=None, deleting=False,
        other_active_admins=1,
    )
    defaults.update(kw)
    return refuse_dangerous_user_change(**defaults)


# ── self-protection ─────────────────────────────────────────────────────────────────

def test_self_deactivation_is_refused_by_id():
    reason = _refuse(target_id=ME, target_scope="scope-x", target_role="admin", new_is_active=False)
    assert reason and "your own account" in reason


def test_self_deactivation_is_refused_by_scope_for_the_tokenless_admin():
    """The machine owner's requests carry no id at all - the scope is the identity."""
    reason = _refuse(caller_id=None, caller_scope="scope-x",
                     target_scope="scope-x", target_role="admin", new_is_active=False)
    assert reason and "your own account" in reason


def test_self_demotion_is_refused():
    reason = _refuse(target_id=ME, target_scope="scope-x", target_role="admin", new_role="user")
    assert reason and "own admin role" in reason


def test_self_deletion_is_refused():
    reason = _refuse(target_id=ME, target_scope="scope-x", target_role="admin", deleting=True)
    assert reason and "delete your own account" in reason


def test_another_admin_may_deactivate_me_when_admins_remain():
    """Self-protection is not target-protection: the OTHER admin is the repair path."""
    assert _refuse(target_role="admin", new_is_active=False, other_active_admins=1) is None


# ── last-active-admin protection ────────────────────────────────────────────────────

def test_deactivating_the_last_active_admin_is_refused_even_for_another_caller():
    reason = _refuse(target_role="admin", new_is_active=False, other_active_admins=0)
    assert reason and "last active admin" in reason


def test_demoting_the_last_active_admin_is_refused():
    reason = _refuse(target_role="admin", new_role="user", other_active_admins=0)
    assert reason and "last active admin" in reason


def test_deleting_the_last_active_admin_is_refused():
    """The measured hole: with a deactivated admin row present the old row-count let this
    through, and an instance with no signed-in-able admin re-opens public bootstrap."""
    reason = _refuse(target_role="admin", deleting=True, other_active_admins=0)
    assert reason and "last active admin" in reason


def test_deleting_a_deactivated_admin_is_allowed_while_an_active_one_remains():
    """The counterpart the row-count got wrong in the other direction: a leftover inactive
    admin is not a repair path, and removing it changes nothing about who can sign in."""
    assert _refuse(target_role="admin", target_active=False,
                   deleting=True, other_active_admins=1) is None


def test_a_normal_user_can_always_be_deactivated_or_deleted():
    assert _refuse(target_role="user", new_is_active=False, other_active_admins=0) is None
    assert _refuse(target_role="user", deleting=True, other_active_admins=0) is None


def test_reactivation_is_never_refused():
    assert _refuse(target_id=ME, target_scope="scope-x", target_role="admin",
                   target_active=False, new_is_active=True,
                   other_active_admins=0) is None


def test_promotion_is_never_refused():
    assert _refuse(target_role="user", new_role="admin", other_active_admins=0) is None


def test_an_update_without_role_or_active_change_is_untouched():
    """Email/tools/workflows edits never hit the guard's rules."""
    assert _refuse(target_id=ME, target_scope="scope-x", target_role="admin") is None


# ── the caller identity, measured against the middleware's real dict ────────────────

def test_the_route_reads_the_id_key_the_middleware_actually_publishes():
    """The shape is not a guess: vaf/auth/middleware.py builds request.state.user with
    `user_id` (from the JWT `sub` claim), `username`, `role`, `user_scope_id`. A guard that
    reads `sub` gets None forever - which is what shipped until a review measured it, with
    all of this file green.
    """
    middleware_shape = {
        "user_id": ME,
        "username": "alice",
        "role": "admin",
        "user_scope_id": "scope-me",
    }
    assert caller_identity(middleware_shape) == (ME, "scope-me")


def test_the_middleware_really_builds_that_shape():
    """Pins the other end, so the pair above cannot drift into agreeing about a fiction."""
    import vaf.auth.middleware as mw

    src = inspect.getsource(mw)
    tree = ast.parse(src)
    published = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            target = node.targets[0]
            if isinstance(target, ast.Attribute) and target.attr == "user":
                published |= {
                    k.value for k in node.value.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                }
    assert "user_id" in published, f"middleware no longer publishes user_id: {published}"
    assert "user_scope_id" in published


def test_the_tokenless_local_admin_has_a_scope_but_no_id():
    scope_only = {"username": "admin", "role": "admin", "user_scope_id": "scope-owner"}
    assert caller_identity(scope_only) == (None, "scope-owner")


# ── the wiring: both routes ask, and the count is of ACTIVE admins ──────────────────

def _calls_guard(fn) -> list:
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    return [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", getattr(n.func, "attr", None)) == "refuse_dangerous_user_change"
    ]


def test_the_update_route_consults_the_guard():
    calls = _calls_guard(update_user)
    assert calls, "update_user no longer consults refuse_dangerous_user_change"
    kw = {k.arg for k in calls[0].keywords}
    assert {"caller_id", "caller_scope", "other_active_admins"} <= kw


def test_the_delete_route_consults_the_same_guard_and_declares_deletion():
    """Deleting is the third door; a delete route with its own private rule is how the
    hole existed in the first place."""
    calls = _calls_guard(delete_user)
    assert calls, "delete_user no longer consults refuse_dangerous_user_change"
    kw = {k.arg for k in calls[0].keywords}
    assert "deleting" in kw and "other_active_admins" in kw


def test_no_route_counts_admins_by_existence_anymore():
    """The shared counter filters on is_active; and no route may keep a private admin
    count beside it (the exact shape that let the delete route disagree).

    AST, not substring: the first version of this assertion looked for the text
    "is_active" in the source and stayed GREEN when the filter was deleted, because the
    docstring one line above says the word. Guard-reads-text-not-code, caught by its own
    mutation run - see tests/README.md.
    """
    counter_tree = ast.parse(textwrap.dedent(inspect.getsource(count_other_active_admins)))
    filters_on_active = [
        n for n in ast.walk(counter_tree)
        if isinstance(n, ast.Compare)
        and isinstance(n.left, ast.Attribute) and n.left.attr == "is_active"
        and any(isinstance(c, ast.Constant) and c.value is True for c in n.comparators)
    ]
    assert filters_on_active, (
        "count_other_active_admins no longer filters on is_active - it counts admin rows "
        "again, which is what let the last ACTIVE admin be deleted"
    )

    for fn in (update_user, delete_user):
        src = inspect.getsource(fn)
        assert 'LocalUser.role == "admin"' not in src, (
            f"{fn.__name__} counts admins itself again instead of using "
            f"count_other_active_admins - two counts is how they drifted apart"
        )
