# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The coder's inner tools act as the CALLER, and the identity crosses the process as data.

WHAT WAS MEASURED BEFORE BUILDING (2026-08-01). `CodingAgentTool.identity_kwargs` was `()`,
so the dispatcher had nothing to hand over before any process boundary was involved; the
spawn env carried task, agent type, session and provider - no identity - and the child ran
every inner tool with `compute_user_jail(None, None)`, which answers is_admin=True with
zero roots. Meanwhile SEVEN of the eight inner tools already declare `identity_kwargs` and
`file_access`: the whole confinement machinery was attached and permanently resolving as
the machine owner. Phase 3 is therefore a WIRING, not a build - the librarian proved the
pattern (VAF_USER_SCOPE_ID / VAF_USER_ROLE in the child env, kwargs-or-env on the far
side), and this copies it rather than inventing a second one.

THE DESIGN RULE that shaped the cut: the coder uses its tools at FULL strength;
the containment for a user who should not have that power is access to the coder itself
(the per-user tool permission, enforced in the funnel and inside the child). So identity
travels so tools act AS the caller, not to cripple them - and `bash` stays broad as a
NAMED exception rather than a forgotten one.
"""
import pytest

from vaf.tools.coder import CodingAgentTool, _assign_caller_identity, _caller_identity

SCOPE = "ab12cd34-0000-4000-8000-000000000001"


# ── the declaration: the parent half ────────────────────────────────────────────────

def test_the_coder_declares_its_caller():
    """`()` was the defect: nothing to hand over, before any boundary was involved."""
    assert CodingAgentTool.identity_kwargs == ("user_scope_id", "user_role")


def test_the_coder_itself_carries_no_file_access():
    """The boundary belongs around the INNER tools, where it already is; a jail around the
    whole coder would stop it reading the very files it was asked to change."""
    assert getattr(CodingAgentTool, "file_access", None) is None


# ── the boundary: kwargs on one side, env on the other ─────────────────────────────

def test_identity_resolves_from_kwargs_in_the_parent(monkeypatch):
    monkeypatch.delenv("VAF_USER_SCOPE_ID", raising=False)
    monkeypatch.delenv("VAF_USER_ROLE", raising=False)

    assert _caller_identity({"user_scope_id": SCOPE, "user_role": "user"}) == (SCOPE, "user")


def test_identity_resolves_from_the_env_in_the_child(monkeypatch):
    """The child gets empty kwargs; the parent's _sub_env put the identity here."""
    monkeypatch.setenv("VAF_USER_SCOPE_ID", SCOPE)
    monkeypatch.setenv("VAF_USER_ROLE", "user")

    assert _caller_identity({}) == (SCOPE, "user")


def test_no_identity_anywhere_means_none_not_a_guess(monkeypatch):
    """The owner's local run: no scope, no role - and the jail resolves that as the owner,
    which is today's behaviour, unchanged. The rollback IS the default."""
    monkeypatch.delenv("VAF_USER_SCOPE_ID", raising=False)
    monkeypatch.delenv("VAF_USER_ROLE", raising=False)

    assert _caller_identity({}) == (None, None)


def test_the_spawn_env_carries_the_identity():
    """The wiring in run()'s spawn block, asserted as CODE rather than prose: the line that
    puts the scope into _sub_env has to exist on the spawn path. Docstrings are stripped so
    a comment about the mechanism can neither satisfy nor break this."""
    import ast

    import vaf.tools.coder as mod

    # The whole module, then the class, then run() - `inspect.getsource` + dedent breaks
    # here because run() contains strings with column-0 content, which defeats dedent.
    tree = ast.parse(open(mod.__file__, "rb").read())
    run_fn = next(
        fn
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "CodingAgentTool"
        for fn in node.body
        if isinstance(fn, ast.FunctionDef) and fn.name == "run"
    )
    assigned = {
        n.slice.value
        for n in ast.walk(run_fn)
        if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant)
        and n.slice.value in ("VAF_USER_SCOPE_ID", "VAF_USER_ROLE")
    }
    assert assigned == {"VAF_USER_SCOPE_ID", "VAF_USER_ROLE"}, (
        f"the spawn env no longer carries the caller's identity (found: {sorted(assigned)})"
    )


# ── the assignment in the child: the security half ─────────────────────────────────

class _Declared:
    identity_kwargs = ("user_scope_id", "user_role")

class _ScopeOnly:
    identity_kwargs = ("user_scope_id",)

class _Undeclared:
    identity_kwargs = ()


def test_a_declared_tool_gets_the_callers_identity():
    args = _assign_caller_identity(_Declared(), {"path": "x.py"}, SCOPE, "user")
    assert args["user_scope_id"] == SCOPE
    assert args["user_role"] == "user"


def test_a_model_supplied_admin_role_is_overwritten_not_honoured():
    """ASSIGN, never setdefault. `fn_args` is what the MODEL wrote; a prompt-injected
    `user_role: "admin"` would otherwise lift the file jail from inside a chat message -
    the exact escalation the main dispatcher's CI guard pins."""
    args = _assign_caller_identity(_Declared(), {"user_role": "admin", "user_scope_id": "stolen"},
                                   SCOPE, "user")
    assert args["user_role"] == "user"
    assert args["user_scope_id"] == SCOPE


def test_only_declared_keys_are_touched():
    args = _assign_caller_identity(_ScopeOnly(), {}, SCOPE, "user")
    assert args == {"user_scope_id": SCOPE}


def test_bash_is_the_named_exception_and_stays_untouched():
    """Deliberate, and frozen in BOTH directions: bash declares nothing (a shell confined
    to a per-user jail is not a shell - the containment is the per-user tool permission),
    and therefore the assignment must not hand it anything either. If either side changes,
    this fails and points at the class comment that says why it was this way."""
    from vaf.tools.bash import BashTool

    assert BashTool.identity_kwargs == ()
    assert getattr(BashTool, "file_access", None) is None
    assert _assign_caller_identity(BashTool, {"command": "ls"}, SCOPE, "user") == {"command": "ls"}


# ── the consequence: the jail the wiring feeds ─────────────────────────────────────

def test_the_identity_reaching_the_jail_actually_confines():
    """End to end at the decision point: the same call the inner tools make. Without the
    wiring the coder hands (None, None) and gets the first line; with a tenant's identity
    it gets the second. This is the difference between measured and described."""
    from vaf.tools.filesystem import compute_user_jail

    as_before = compute_user_jail(None, None, mode="write")
    as_tenant = compute_user_jail(SCOPE, "user", mode="write")

    assert as_before.get("is_admin") is True
    assert as_tenant.get("is_admin") is False
    assert as_tenant.get("allowed_roots"), "a tenant's jail has no roots - nothing is confined"
