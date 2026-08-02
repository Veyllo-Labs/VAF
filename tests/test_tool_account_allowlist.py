# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The account-level tool allowlist: a framework primitive, enforced where every lane runs.

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
account may run, without re-asking the resolver mid-run.

WHERE THE LIST COMES FROM: the process-wide resolver an application registers via
`set_account_allowlist_resolver` (the framework primitive; the harness registers its
auth-DB resolver in vaf/main.py, pinned by tests/test_account_allowlist_wiring.py).
Framework contract, each choice against its failure mode: unregistered = unrestricted (a
bare library embedder has not opted in and must not be locked out by a default); None =
unrestricted; any other answer is normalized to a frozenset and an EMPTY answer allows
nothing; a RAISING registered resolver is a refusal, the authorizer's polarity - a broken
guard must not quietly become no guard.

HARNESS SEMANTICS, pinned one layer down in `vaf.auth.permissions` (each choice has a
failure mode on the other side): the stored list is an ALLOWLIST; absent row / absent key /
EMPTY stored list = unrestricted, because `[]` is the API model's creation default and
would otherwise lock out every route-created user; admins are never restricted; an
unreachable DB resolves unrestricted - the harness resolver catches its own DB errors and
returns None, because on the desktop the auth DB being down also means no tenant can
authenticate, so the only person present is the owner. That fail-open trade lives INSIDE
the harness resolver now, not in the funnel.
"""
import pytest

from vaf.core.tool_dispatch import ToolCaller, set_account_allowlist_resolver

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


@pytest.fixture(autouse=True)
def _registry_isolated():
    """Save/RESTORE the process-global registry, never bare-clear it.

    Restoring matters: test_integrity.py imports vaf.main in-process, which registers the
    REAL harness resolver for the rest of a single-process suite run - parity with the old
    hard import, and clearing it here would make suite behavior depend on file order.
    Each test in this file starts from an empty registry and states its own answer.
    """
    from vaf.core.tool_dispatch import get_account_allowlist_resolver
    previous = get_account_allowlist_resolver()
    set_account_allowlist_resolver(None)
    yield
    set_account_allowlist_resolver(previous)


def _resolver(answer):
    if isinstance(answer, Exception):
        def _raise(scope):
            raise answer
        set_account_allowlist_resolver(_raise)
    else:
        set_account_allowlist_resolver(lambda scope: answer)


# ── the funnel refusal ──────────────────────────────────────────────────────────────

def test_a_blocked_tool_is_refused_for_a_scoped_user():
    _resolver(frozenset({"something_else"}))

    out = _caller(user_scope_id=SCOPE, user_role="user").execute("probe", {})

    assert out.startswith("Security Error:"), out
    assert "not enabled for your account" in out


def test_an_allowed_tool_runs():
    _resolver(frozenset({"probe"}))

    assert _caller(user_scope_id=SCOPE, user_role="user").execute("probe", {}) == "RAN"


def test_unrestricted_accounts_run_everything():
    _resolver(None)

    assert _caller(user_scope_id=SCOPE, user_role="user").execute("probe", {}) == "RAN"


def test_no_registered_resolver_means_unrestricted():
    """The embedded-library default: an application that registered nothing has not opted
    into account policy, and a scoped caller must not be locked out by a default."""
    assert _caller(user_scope_id=SCOPE, user_role="user").execute("probe", {}) == "RAN"


def test_an_admin_is_never_restricted():
    """Same rule as every other gate - and the reason the check consults the shared
    `policy_admin_flag` instead of growing its own idea of admin."""
    _resolver(frozenset({"something_else"}))

    assert _caller(user_scope_id=SCOPE, user_role="admin").execute("probe", {}) == "RAN"


def test_admins_and_scopeless_callers_never_consult_the_resolver():
    """Exemptions sit BEFORE the resolver, framework-side: the resolver answers "which
    list for this scope", never "who is exempt" - a resolver that crashed on the admin's
    scope must not be able to lock the admin out either."""
    called = []
    set_account_allowlist_resolver(lambda scope: called.append(scope) or frozenset())

    assert _caller().execute("probe", {}) == "RAN"
    assert _caller(user_scope_id=SCOPE, user_role="admin").execute("probe", {}) == "RAN"
    assert called == [], "the resolver was consulted for an exempt caller"


def test_a_raising_registered_resolver_fails_closed():
    """The authorizer's polarity, not the event sink's: a registered guard that crashed
    must refuse, because a crash is indistinguishable from a guard that never ran. The
    fail-open trade for an unreachable backend lives INSIDE the harness resolver, which
    catches its own DB errors - pinned by test_the_harness_resolver_never_raises."""
    _resolver(RuntimeError("backend exploded"))

    out = _caller(user_scope_id=SCOPE, user_role="user").execute("probe", {})
    assert out.startswith("Security Error:"), out
    assert out != "RAN"
    assert "resolver failed" in out


def test_the_harness_resolver_never_raises(monkeypatch):
    """The lever must not become a tripwire: the harness resolver resolves an unreachable
    DB to None (unrestricted), because on the desktop a stopped auth DB also means no
    tenant can authenticate. Under the fail-closed funnel this catch is what keeps the
    product's behavior identical - deleting it would take every tool down with the DB."""
    import vaf.auth.permissions as perms

    def _boom(scope):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(perms, "_lookup_allowed_tools", _boom)
    assert perms.resolve_allowed_tools(SCOPE) is None


def test_the_ban_cannot_be_overridden_by_an_authorizer_allow():
    """Order is the contract: the account ban sits BEFORE the authorizer, so an embedder's
    blanket allow() cannot lift what the admin revoked."""
    _resolver(frozenset({"something_else"}))

    caller = _caller(user_scope_id=SCOPE, user_role="user",
                     authorize=lambda req: req.allow("yes, everything"))

    out = caller.execute("probe", {})
    assert "not enabled for your account" in out


# ── the framework contract of the resolver primitive ──────────────────────────────────

def test_the_answer_is_normalized_to_a_frozenset():
    """A list answer would pass a membership check by accident; the contract says the
    framework normalizes, so the TYPE is asserted, not just the behavior. Blank names are
    dropped - a resolver that pads with empty strings has not allowed anything by it."""
    from vaf.core.tool_dispatch import resolve_account_allowlist

    set_account_allowlist_resolver(lambda scope: ["probe", " ", ""])
    assert resolve_account_allowlist(SCOPE) == frozenset({"probe"})
    assert isinstance(resolve_account_allowlist(SCOPE), frozenset)

    set_account_allowlist_resolver(lambda scope: (t for t in ("a", "b")))
    assert resolve_account_allowlist(SCOPE) == frozenset({"a", "b"})


def test_an_empty_answer_allows_nothing():
    """Framework contract: EMPTY means nothing allowed. The harness's "empty stored list =
    unrestricted" rule is a statement about ITS API-model default and lives one layer down,
    in _tools_from_permissions - it must not creep into the framework primitive."""
    _resolver(frozenset())

    out = _caller(user_scope_id=SCOPE, user_role="user").execute("probe", {})
    assert "not enabled for your account" in out


def test_detaching_the_resolver_restores_unrestricted():
    _resolver(frozenset({"something_else"}))
    set_account_allowlist_resolver(None)

    assert _caller(user_scope_id=SCOPE, user_role="user").execute("probe", {}) == "RAN"


def test_the_framework_never_imports_the_harness_auth():
    """The conversion's success measure: the funnel and the coder consult the registered
    resolver and import nothing from vaf.auth. Source-text scan on purpose - it also sees
    function-local imports, which is exactly where the two deleted ones sat."""
    import vaf.core.tool_dispatch as td
    import vaf.tools.coder as coder

    for mod in (td, coder):
        src = open(mod.__file__, "rb").read().decode("utf-8", errors="replace")
        assert "vaf.auth" not in src, (
            f"{mod.__name__} reaches into the harness auth layer again - consult "
            f"get_account_allowlist_resolver(); the harness registers in vaf/main.py"
        )


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


def test_the_parent_asks_the_registered_resolver(monkeypatch):
    """Parent side, env absent: the coder consults the SAME registry as the funnel, so
    the two lanes cannot disagree about what an account may run."""
    from vaf.tools.coder import _caller_allowed_tools

    monkeypatch.delenv("VAF_ALLOWED_TOOLS", raising=False)
    _resolver(["read_file"])
    assert _caller_allowed_tools(SCOPE, "user") == frozenset({"read_file"})
    assert _caller_allowed_tools(SCOPE, "admin") is None   # shared exemption rule


def test_a_raising_resolver_propagates_out_of_the_coder_helper(monkeypatch):
    """No silent fallback to unrestricted: run() resolves once at the top, so a raising
    registered resolver fails the whole coder call closed instead of handing the child a
    full registry."""
    from vaf.tools.coder import _caller_allowed_tools

    monkeypatch.delenv("VAF_ALLOWED_TOOLS", raising=False)
    _resolver(RuntimeError("backend exploded"))
    with pytest.raises(RuntimeError):
        _caller_allowed_tools(SCOPE, "user")


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
