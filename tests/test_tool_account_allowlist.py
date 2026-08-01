# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The admin's per-user tool selection is finally READ - and enforced where every lane runs.

MEASURED BEFORE BUILDING (2026-08-01, re-measured on the live code rather than recited from
a plan): `LocalUser.permissions["tools"]` was written by the admin routes, mirrored back for
display, and read by NOTHING else in the repository. The JWT carries no permissions, so the
value could not even reach the agent process. An admin unticked the coder for a user and
that user's chat still received the full registry; the UI said so honestly since a19.

WHERE ENFORCEMENT SITS, and why exactly there: in the funnel (`ToolCaller.execute`), after
the hard policy block and BEFORE the embedder's authorizer - an account-level ban must not
be overridable by an `allow()`. One check in the funnel covers every lane the funnel
serves; five per-lane checks is the shape where four are forgotten. Inside the coder the
allowlist crosses the process boundary as data (`VAF_ALLOWED_TOOLS`, tool NAMES only),
mirroring the identity vars - the child must agree with the chat lane about what an
account may run, without re-asking the DB mid-run.

PINNED SEMANTICS (each choice has a failure mode on the other side): the list is an
ALLOWLIST; absent row / absent key / EMPTY list = unrestricted, because `[]` is the API
model's creation default and would otherwise lock out every route-created user; admins are
never restricted; an unreachable DB resolves unrestricted - on the desktop the auth DB
being down also means no tenant can authenticate, so the only person present is the owner.
"""
import pytest

from vaf.core.tool_dispatch import ToolCaller

SCOPE = "ab12cd34-0000-4000-8000-000000000001"


class _Probe:
    name = "probe"
    description = "x"
    parameters = {"type": "object", "properties": {}}
    identity_kwargs = ()

    def run(self, **kwargs):
        return "RAN"


def _caller(**kw):
    return ToolCaller({"probe": _Probe()}, **kw)


@pytest.fixture(autouse=True)
def _no_db_cache(monkeypatch):
    """Each test states its own resolver answer; nothing here may touch a real DB."""
    import vaf.auth.permissions as perms
    monkeypatch.setattr(perms, "_cache", {})
    yield


def _resolver(monkeypatch, answer):
    import vaf.auth.permissions as perms
    if isinstance(answer, Exception):
        def _raise(scope):
            raise answer
        monkeypatch.setattr(perms, "resolve_allowed_tools", _raise)
    else:
        monkeypatch.setattr(perms, "resolve_allowed_tools", lambda scope: answer)


# ── the funnel refusal ──────────────────────────────────────────────────────────────

def test_a_blocked_tool_is_refused_for_a_scoped_user(monkeypatch):
    _resolver(monkeypatch, frozenset({"something_else"}))

    out = _caller(user_scope_id=SCOPE, user_role="user").execute("probe", {})

    assert out.startswith("Security Error:"), out
    assert "not enabled for your account" in out


def test_an_allowed_tool_runs(monkeypatch):
    _resolver(monkeypatch, frozenset({"probe"}))

    assert _caller(user_scope_id=SCOPE, user_role="user").execute("probe", {}) == "RAN"


def test_unrestricted_accounts_run_everything(monkeypatch):
    _resolver(monkeypatch, None)

    assert _caller(user_scope_id=SCOPE, user_role="user").execute("probe", {}) == "RAN"


def test_an_admin_is_never_restricted(monkeypatch):
    """Same rule as every other gate - and the reason the check consults the shared
    `policy_admin_flag` instead of growing its own idea of admin."""
    _resolver(monkeypatch, frozenset({"something_else"}))

    assert _caller(user_scope_id=SCOPE, user_role="admin").execute("probe", {}) == "RAN"


def test_a_caller_with_no_scope_is_unrestricted(monkeypatch):
    """The machine owner and every direct in-process lane: the allowlist constrains
    AUTHENTICATED tenants, and only they carry a scope."""
    called = []
    _resolver(monkeypatch, frozenset())
    import vaf.auth.permissions as perms
    monkeypatch.setattr(perms, "resolve_allowed_tools",
                        lambda scope: called.append(scope) or frozenset())

    assert _caller().execute("probe", {}) == "RAN"
    assert called == [], "the resolver was consulted for a scopeless caller"


def test_a_broken_resolver_never_breaks_a_turn(monkeypatch):
    """The lever must not become a tripwire: enforcement failing open is the documented
    trade, because failing closed would take every tool down with the auth DB."""
    _resolver(monkeypatch, RuntimeError("db exploded"))

    assert _caller(user_scope_id=SCOPE, user_role="user").execute("probe", {}) == "RAN"


def test_the_ban_cannot_be_overridden_by_an_authorizer_allow(monkeypatch):
    """Order is the contract: the account ban sits BEFORE the authorizer, so an embedder's
    blanket allow() cannot lift what the admin revoked."""
    _resolver(monkeypatch, frozenset({"something_else"}))

    caller = _caller(user_scope_id=SCOPE, user_role="user",
                     authorize=lambda req: req.allow("yes, everything"))

    out = caller.execute("probe", {})
    assert "not enabled for your account" in out


# ── the pinned resolver semantics ───────────────────────────────────────────────────

@pytest.mark.parametrize("perms_value", [None, {}, {"tools": None}, {"tools": []}, {"other": 1}])
def test_absent_or_empty_means_unrestricted(monkeypatch, perms_value):
    import vaf.auth.permissions as perms
    monkeypatch.setattr(perms, "_lookup_allowed_tools",
                        lambda scope: perms._tools_from_permissions(perms_value))

    assert perms.resolve_allowed_tools(SCOPE) is None


def test_a_real_list_becomes_the_allowlist(monkeypatch):
    import vaf.auth.permissions as perms
    monkeypatch.setattr(perms, "_lookup_allowed_tools",
                        lambda scope: perms._tools_from_permissions({"tools": ["a", "b", ""]}))

    assert perms.resolve_allowed_tools(SCOPE) == frozenset({"a", "b"})


def test_a_revocation_beats_the_cache(monkeypatch):
    import vaf.auth.permissions as perms

    answers = [frozenset({"a"}), frozenset()]
    monkeypatch.setattr(perms, "_lookup_allowed_tools", lambda scope: answers.pop(0))

    assert perms.resolve_allowed_tools(SCOPE) == frozenset({"a"})
    assert perms.resolve_allowed_tools(SCOPE) == frozenset({"a"})   # cached
    perms.invalidate_permissions_cache(SCOPE)
    assert perms.resolve_allowed_tools(SCOPE) == frozenset()


# ── the coder half ──────────────────────────────────────────────────────────────────

def test_the_child_reads_the_allowlist_from_the_env(monkeypatch):
    from vaf.tools.coder import _caller_allowed_tools

    monkeypatch.setenv("VAF_ALLOWED_TOOLS", "read_file,write_file")
    assert _caller_allowed_tools(SCOPE, "user") == frozenset({"read_file", "write_file"})


def test_env_absent_and_no_scope_means_unrestricted(monkeypatch):
    from vaf.tools.coder import _caller_allowed_tools

    monkeypatch.delenv("VAF_ALLOWED_TOOLS", raising=False)
    assert _caller_allowed_tools(None, None) is None


def test_the_spawn_env_carries_the_allowlist():
    """Same AST assertion shape as the identity transport: the wiring as code, not prose."""
    import ast

    import vaf.tools.coder as mod

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
        and n.slice.value == "VAF_ALLOWED_TOOLS"
    }
    assert assigned, "the spawn env no longer carries the account allowlist"


def test_coder_only_names_match_what_the_coder_actually_builds():
    """The picker universe cannot drift from the child's registry.

    Every literal key the coder assigns into `self.local_tools` is either a tool that also
    exists on the main registry (governable from the normal picker list) or is declared in
    CODER_ONLY_TOOL_NAMES (governable via the tool-universe endpoint). A key in neither is
    a tool the admin cannot express an opinion about - the exact gap the owner named for
    bash.
    """
    import ast

    import vaf.tools.coder as mod
    from vaf.tools.coder import CODER_ONLY_TOOL_NAMES

    shared_with_main = {
        "write_file", "edit_file", "read_file", "list_files",
        "python_sandbox", "linter", "codesearch",
    }
    tree = ast.parse(open(mod.__file__, "rb").read())
    assigned_keys = {
        node.targets[0].slice.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Subscript)
        and isinstance(node.targets[0].value, ast.Attribute)
        and node.targets[0].value.attr == "local_tools"
        and isinstance(node.targets[0].slice, ast.Constant)
        and isinstance(node.targets[0].slice.value, str)
    }
    ungoverned = assigned_keys - shared_with_main - set(CODER_ONLY_TOOL_NAMES)
    assert not ungoverned, (
        f"coder inner tools the admin cannot govern: {sorted(ungoverned)} - add them to "
        f"CODER_ONLY_TOOL_NAMES (picker) or to the shared set here (with the main tool)"
    )
    assert set(CODER_ONLY_TOOL_NAMES).isdisjoint(shared_with_main)


def test_the_universe_endpoint_offers_bash():
    import asyncio

    from vaf.api.user_routes import tool_universe

    out = asyncio.run(tool_universe(_={"role": "admin"}))
    assert "bash" in out["coder_only"]
