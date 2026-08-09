# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Contract: `set_account_allowlist_resolver` (docs/EMBEDDING.md, "Which tools
an account may use").

The account allowlist is the one funnel question answered from the ACCOUNT
rather than from the tool's own declarations. Everything pinned here is a
promise an embedder's plans table or admin panel is built on: the resolver
signature, the None/empty semantics, the exemptions, the fail-closed polarity,
and the rank of the check (after hard policy, BEFORE the authorizer). Error
strings are pinned by prefix or short substring only - prose may be reworded.

Every test that depends on a clean baseline calls
set_account_allowlist_resolver(None) itself first: an in-repo full-suite run
may have the product's real resolver registered process-wide. The autouse
conftest fixture restores whatever was registered before each test.
"""
from vaf import ToolCaller, set_account_allowlist_resolver

# Synthetic tenant scope (never a real UUID): a scoped, non-admin caller is
# the only identity the allowlist constrains.
SCOPE = "deadbeef-0000-0000-0000-000000000000"


class _ReadNote:
    """A trivial read tool; duck-typed on purpose - BaseTool is not required."""
    name = "read_note"
    description = "returns a fixed note"
    parameters = {"type": "object", "properties": {}}
    identity_kwargs = ()

    def run(self, **kwargs):
        return "NOTE: ok"


def _caller(**kw):
    return ToolCaller({"read_note": _ReadNote()}, **kw)


def _tenant(**kw):
    return _caller(user_scope_id=SCOPE, user_role="user", **kw)


def test_no_registered_resolver_means_unrestricted():
    """The embedded-library default: an application that registered nothing
    has not opted into account policy, and a scoped caller must not be locked
    out by a default."""
    set_account_allowlist_resolver(None)

    assert _tenant().execute("read_note", {}) == "NOTE: ok"


def test_a_resolver_answering_none_means_unrestricted():
    """None is the documented "no restriction for this scope" answer, distinct
    from an empty list."""
    set_account_allowlist_resolver(lambda scope: None)

    assert _tenant().execute("read_note", {}) == "NOTE: ok"


def test_an_empty_answer_allows_nothing_and_emits_no_events():
    """An EMPTY answer allows nothing (an embedder mapping "empty = no
    restriction" must do so in their own resolver), and the block behaves
    exactly like a policy block: refusal string, zero events on the sink."""
    set_account_allowlist_resolver(lambda scope: [])
    events = []

    out = _tenant(on_event=events.append).execute("read_note", {})

    assert out.startswith("Security Error:"), out
    assert "not enabled for your account" in out
    assert events == [], "an allowlist block must emit NO events"


def test_the_resolver_is_consulted_per_call_with_the_callers_scope():
    """Consulted per call, not cached by the framework: flipping the answer
    between two calls of the SAME ToolCaller flips the outcome. Revocation
    latency is the resolver's own business. Also pins the documented
    signature: the resolver receives the caller's user_scope_id."""
    answer = {"allowed": ["other_tool"]}
    scopes_seen = []

    def resolver(scope):
        scopes_seen.append(scope)
        return answer["allowed"]

    set_account_allowlist_resolver(resolver)
    caller = _tenant()

    blocked = caller.execute("read_note", {})
    answer["allowed"] = ["read_note"]
    allowed = caller.execute("read_note", {})

    assert blocked.startswith("Security Error:"), blocked
    assert allowed == "NOTE: ok"
    assert scopes_seen == [SCOPE, SCOPE]


def test_a_scopeless_caller_never_consults_the_resolver():
    """Callers with no scope are exempt BEFORE the resolver: it answers
    "which list for this scope", never "who is exempt". Counting proves the
    exemption is a short-circuit, not a permissive answer."""
    calls = []

    def resolver(scope):
        calls.append(scope)
        return []  # would block everything if consulted

    set_account_allowlist_resolver(resolver)

    assert _caller().execute("read_note", {}) == "NOTE: ok"
    assert calls == []


def test_an_admin_identity_never_consults_the_resolver():
    """Admin identities are exempt the same way: a resolver that crashed on
    the admin's own scope must not be able to lock the admin out."""
    calls = []

    def resolver(scope):
        calls.append(scope)
        return []  # would block everything if consulted

    set_account_allowlist_resolver(resolver)

    out = _caller(user_scope_id=SCOPE, user_role="admin").execute("read_note", {})

    assert out == "NOTE: ok"
    assert calls == []


def test_a_raising_resolver_refuses_the_call():
    """Fail-closed, the authorizer's polarity: a broken guard must not quietly
    become no guard. Fail-open belongs INSIDE a resolver that knows its
    backend."""
    def resolver(scope):
        raise RuntimeError("backend down")

    set_account_allowlist_resolver(resolver)

    out = _tenant().execute("read_note", {})

    assert out.startswith("Security Error:"), out
    assert "resolver failed" in out


def test_the_last_registration_wins():
    """One resolver per process; re-registering replaces the previous one."""
    set_account_allowlist_resolver(lambda scope: [])  # blocks everything
    set_account_allowlist_resolver(lambda scope: ["read_note"])

    assert _tenant().execute("read_note", {}) == "NOTE: ok"


def test_registering_none_deregisters():
    """set_account_allowlist_resolver(None) removes the guard entirely: the
    scoped caller is unrestricted again, as if nothing was ever registered."""
    set_account_allowlist_resolver(lambda scope: [])
    blocked = _tenant().execute("read_note", {})

    set_account_allowlist_resolver(None)
    unrestricted = _tenant().execute("read_note", {})

    assert blocked.startswith("Security Error:"), blocked
    assert unrestricted == "NOTE: ok"


def test_an_authorizer_allow_cannot_lift_an_account_ban():
    """The allowlist is checked after the hard policy block and BEFORE the
    authorizer: an account-level ban is not overridable by an embedder's
    allow(). The authorizer is not even consulted for a banned call."""
    set_account_allowlist_resolver(lambda scope: [])
    authorizer_calls = []

    def authorize(req):
        authorizer_calls.append(req.tool_name)
        req.allow()

    out = _tenant(authorize=authorize).execute("read_note", {})

    assert out.startswith("Security Error:"), out
    assert "not enabled for your account" in out
    assert authorizer_calls == [], "the allowlist must rank before the authorizer"


def test_a_generator_answer_is_accepted():
    """Any iterable of names is a valid answer - the framework normalizes it;
    the resolver returns a fresh generator per call because it is consulted
    per call."""
    set_account_allowlist_resolver(lambda scope: (n for n in ("read_note",)))

    assert _tenant().execute("read_note", {}) == "NOTE: ok"
