# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Who a turn runs as: one primitive, and the two questions it keeps apart.

MEASURED BEFORE BUILDING (2026-08-04, on the live tree). Ten places wrote
``_current_user_scope_id`` / ``_current_username`` / ``_current_user_role`` by
hand across seven files. Six bound a ``uuid.UUID``, four bound the raw string -
one attribute, two types, chosen by which door the turn came through. Three
carried a verbatim copy of the same six-line cross-user-leak comment and five
re-implemented the synthetic ``scope_<8hex>`` fallback that
``config.resolve_caller_username`` already owns.

WHY THE COERCION IS GONE RATHER THAN UNIFIED. The reason written at three of the
six coercion sites, "the memory tools expect the UUID", was stale:
``run_memory_search_sync`` normalises the scope itself and denies an unparseable
one. Meanwhile the same value is a raw ``in`` against a list of JSON strings in
``skills_registry`` (a UUID object never matches, so a skill shared with a user
became invisible to that user) and a ``json.dumps`` argument with no ``default=``
in ``SessionManager.save`` (a UUID object raises). The coercion bought nothing
and cost those two.

THE TWO QUESTIONS. ``bind_identity`` answers "who is calling" and writes all
three fields unconditionally, so a field the new turn does not carry CLEARS the
previous one - one agent object serves many queued turns, and inheriting a
missing field is how one tenant's workspace serves the next person.
``reassert_identity`` answers "put mine back after the session load overwrote
it" and writes only fields that carry a VALUE, because the session may
legitimately supply a name the caller does not. Collapsing either into the other
is a leak in one direction and a blanked session owner in the other, so both
directions are pinned below.

NO CONTEXT MANAGER, and the test for it is T_REASSERT_IS_NOT_A_FINALLY. At the
one site with a real clobberer the second half must run only when the body
SUCCEEDED: a failed session load has to leave the identity it left standing. A
``with`` cannot express that without a flag, and that flag would be a security
parameter with two values and no name.
"""
import ast
import uuid
from pathlib import Path

import pytest

from vaf.core.identity_binding import (
    Identity,
    bind_identity,
    identity_from_metadata,
    reassert_identity,
    resolve_owner_identity,
    resolve_scope_identity,
)

ROOT = Path(__file__).resolve().parents[1]

OWNER_SCOPE = "12345678-1234-5678-1234-567812345678"
TENANT_SCOPE = "ab12cd34-0000-4000-8000-000000000001"


class _Agent:
    """Duck-typed engine: the three attributes and nothing else.

    Deliberately does NOT predefine them. Half the lanes never had a role, so
    "the attribute does not exist yet" is a real starting state and the binder
    has to create it.
    """


# ── bind_identity: unconditional, because a missing field must clear ──────────

def test_bind_writes_all_three_fields_including_none() -> None:
    agent = _Agent()
    bind_identity(agent, Identity(scope=None, username=None, role=None))
    assert agent._current_user_scope_id is None
    assert agent._current_username is None
    assert agent._current_user_role is None


def test_bind_clears_the_previous_turns_identity() -> None:
    """The queue reuses one agent object. Inheritance here is a cross-user leak."""
    agent = _Agent()
    bind_identity(agent, Identity(scope=TENANT_SCOPE, username="alice", role="admin"))
    bind_identity(agent, Identity(scope=None, username=None, role=None))
    assert agent._current_user_scope_id is None
    assert agent._current_username is None, "the previous tenant's name survived the rebind"
    assert agent._current_user_role is None, "a stale admin role survived the rebind"


def test_bind_creates_the_role_attribute_on_a_lane_that_never_had_one() -> None:
    agent = _Agent()
    assert not hasattr(agent, "_current_user_role")
    bind_identity(agent, Identity(scope=OWNER_SCOPE, username="owner"))
    assert agent._current_user_role is None


def test_bind_stores_the_scope_byte_identical() -> None:
    """No coercion. The same value is a directory name, a JSON value and one
    half of a plain-string admin comparison."""
    agent = _Agent()
    bind_identity(agent, Identity(scope=OWNER_SCOPE, username="owner"))
    assert agent._current_user_scope_id == OWNER_SCOPE
    assert not isinstance(agent._current_user_scope_id, uuid.UUID)

    given = uuid.UUID(OWNER_SCOPE)
    bind_identity(agent, Identity(scope=given, username="owner"))
    assert agent._current_user_scope_id is given, "a UUID handed in must survive unchanged too"


# ── reassert_identity: value-based, forward only ─────────────────────────────

def test_reassert_leaves_a_hydrated_session_name_standing() -> None:
    """A task carrying username=None must not blank what the session supplied."""
    agent = _Agent()
    bind_identity(agent, Identity(scope=TENANT_SCOPE, username=None))
    agent._current_username = "from-session"          # what load_session_context did
    reassert_identity(agent, Identity(scope=TENANT_SCOPE, username=None))
    assert agent._current_username == "from-session"


def test_reassert_beats_the_session_when_the_caller_has_an_answer() -> None:
    agent = _Agent()
    agent._current_user_scope_id = "session-scope"
    agent._current_username = "from-session"
    reassert_identity(agent, Identity(scope=TENANT_SCOPE, username="alice", role="user"))
    assert agent._current_user_scope_id == TENANT_SCOPE
    assert agent._current_username == "alice"
    assert agent._current_user_role == "user"


def test_reassert_is_value_based_not_key_based() -> None:
    """The Discord bridge enqueues {"user_scope_id": None, "username": "admin"}
    with the key present and the value absent, and means it."""
    identity = identity_from_metadata({"user_scope_id": None, "username": "admin"})
    agent = _Agent()
    agent._current_user_scope_id = "session-scope"
    agent._current_username = "from-session"
    reassert_identity(agent, identity)
    assert agent._current_user_scope_id == "session-scope", (
        "a present-but-None key was treated as carried and blanked the session owner"
    )
    assert agent._current_username == "admin"


# ── the producers ────────────────────────────────────────────────────────────

def test_metadata_is_taken_raw_with_no_lookup_and_no_fallback() -> None:
    identity = identity_from_metadata({"user_scope_id": TENANT_SCOPE, "username": None})
    assert identity.scope == TENANT_SCOPE
    assert identity.username is None, (
        "resolving here would replace a real name with a synthetic bucket and lose "
        "the allowlist match that gates the messenger send tools"
    )
    assert identity.role is None


def test_metadata_survives_none_and_junk() -> None:
    for bad in (None, {}, {"unrelated": 1}):
        identity = identity_from_metadata(bad)
        assert (identity.scope, identity.username, identity.role) == (None, None, None)


def test_the_owner_scope_is_bound_exactly_as_configured(monkeypatch) -> None:
    """Not parsed. is_admin_identity compares this against the same config entry
    as plain text, so canonicalising one side only demotes the owner."""
    import vaf.core.config as cfg

    odd = "  ABCDEF01-0000-4000-8000-000000000009  ".strip().upper()
    monkeypatch.setattr(cfg, "get_local_admin_scope_id", lambda: odd)
    monkeypatch.setattr(cfg, "get_local_admin_username", lambda: "mert")

    identity = resolve_owner_identity()
    assert identity.scope == odd
    assert identity.username == "mert"
    assert cfg.is_admin_identity(None, identity.scope) is True, (
        "the owner no longer matches their own configured scope"
    )


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_an_unconfigured_owner_scope_binds_nothing(monkeypatch, blank) -> None:
    import vaf.core.config as cfg

    monkeypatch.setattr(cfg, "get_local_admin_scope_id", lambda: blank)
    monkeypatch.setattr(cfg, "get_local_admin_username", lambda: "mert")
    assert resolve_owner_identity().scope is None


def test_a_foreign_scope_never_resolves_to_the_owner(monkeypatch) -> None:
    """The rule this whole module exists for: the username keys the workspace
    directory, so the owner's name hands over the owner's profile and mail."""
    import vaf.core.config as cfg

    monkeypatch.setattr(cfg, "get_local_admin_scope_id", lambda: OWNER_SCOPE)
    monkeypatch.setattr(cfg, "get_local_admin_username", lambda: "mert")

    import vaf.core.thinking_mode as tm

    def _boom(_scope):
        raise RuntimeError("no database here")

    monkeypatch.setattr(tm, "_resolve_username_for_scope", _boom)

    identity = resolve_scope_identity(TENANT_SCOPE)
    assert identity.username not in ("admin", "mert")
    assert identity.username.startswith("scope_")
    assert identity.scope == TENANT_SCOPE


def test_a_whitespace_only_scope_is_isolated_not_owned(monkeypatch) -> None:
    """The shared resolver strips first, so "   " would take its no-scope branch
    and answer with the OWNER's name - a fail-toward-admin transition reachable
    from a hand-edited or restored task file."""
    import vaf.core.config as cfg

    monkeypatch.setattr(cfg, "get_local_admin_username", lambda: "mert")
    identity = resolve_scope_identity("   ")
    assert identity.username == "scope_unknown"
    assert identity.scope == "   ", "the scope itself must survive; only the NAME is resolved"


def test_the_owners_own_scope_still_resolves_to_the_owner(monkeypatch) -> None:
    import vaf.core.config as cfg

    monkeypatch.setattr(cfg, "get_local_admin_scope_id", lambda: OWNER_SCOPE)
    monkeypatch.setattr(cfg, "get_local_admin_username", lambda: "mert")
    assert resolve_scope_identity(OWNER_SCOPE).username == "mert"


# ── the ratchet: no eleventh hand-rolled binder ──────────────────────────────

# Every file allowed to assign the three identity attributes directly, with the
# reason it is exempt. Shrink-only: a new entry is a reviewed decision, not a
# rerun. Counts rather than line numbers, so a pure line shift does not freeze
# the guard and train people to update it without reading.
ALLOWED_DIRECT_WRITERS = {
    # The primitive itself.
    "vaf/core/identity_binding.py": 6,
    # The CLOBBERER, not a binder: it answers "who owns this session", which is a
    # different question from "who is calling". reassert_identity exists for it.
    "vaf/core/agent.py": 2,
}

_IDENTITY_ATTRS = {
    "_current_user_scope_id",
    "_current_username",
    "_current_user_role",
}


def _direct_identity_writes(path: Path) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    count = 0
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AugAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Attribute) and target.attr in _IDENTITY_ATTRS:
                count += 1
    return count


def test_identity_is_bound_through_the_primitive_everywhere_else() -> None:
    found = {}
    for path in sorted((ROOT / "vaf").rglob("*.py")):
        count = _direct_identity_writes(path)
        if count:
            found[str(path.relative_to(ROOT)).replace("\\", "/")] = count

    new = {f: n for f, n in found.items() if f not in ALLOWED_DIRECT_WRITERS}
    assert not new, (
        f"new hand-rolled identity binding: {new}. Use "
        "vaf.core.identity_binding.bind_identity / reassert_identity - the rule "
        "these sites encode was already stated five times and enforced in four."
    )

    grown = {f: (found.get(f, 0), n) for f, n in ALLOWED_DIRECT_WRITERS.items()
             if found.get(f, 0) > n}
    assert not grown, f"an exempt file grew new direct writes (found, allowed): {grown}"

    shrunk = {f: (found.get(f, 0), n) for f, n in ALLOWED_DIRECT_WRITERS.items()
              if found.get(f, 0) < n}
    assert not shrunk, (
        f"debt was paid down (found, allowed): {shrunk} - lower the baseline so it "
        "cannot creep back"
    )


# ── where the re-assert sits, which is the whole reason it is not a finally ───

def _reassert_call_lines(path: Path) -> list:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "reassert_identity"]


def _finally_spans(path: Path) -> list:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and node.finalbody:
            spans.append((node.finalbody[0].lineno, node.finalbody[-1].end_lineno))
    return spans


@pytest.mark.parametrize("module", [
    "vaf/core/headless_runner.py",
    "vaf/core/thinking_mode.py",
    "vaf/framework.py",
])
def test_the_reassert_never_runs_from_a_finally(module: str) -> None:
    """A failed session load must leave the identity the failure left standing.

    Source-level because the headless loop body is ~1700 lines with no seam a
    test can drive. This is also the property that decided the module ships two
    functions instead of a context manager: a `with` would have to run its exit
    on the exception path, which is exactly wrong here.
    """
    path = ROOT / module
    calls = _reassert_call_lines(path)
    assert calls, f"{module} lost its identity re-assert after a session load"
    for span_start, span_end in _finally_spans(path):
        inside = [ln for ln in calls if span_start <= ln <= span_end]
        assert not inside, (
            f"{module}:{inside} re-asserts identity from a finally; a failed session "
            "load would then be overwritten instead of left standing"
        )


def test_every_session_load_that_owns_its_identity_reasserts_after_it() -> None:
    """The gap this round closed: the thinking lane loaded the user's chat
    session and never put its own identity back, so a session with no stored
    username nulled it and the local-admin fallbacks below read the OWNER's
    workspace into a tenant's prompt."""
    src = (ROOT / "vaf" / "core" / "thinking_mode.py").read_text(encoding="utf-8")
    lines = src.splitlines()
    loads = [i for i, line in enumerate(lines) if "load_session_context(" in line]
    assert loads, "the thinking lane no longer loads a chat session"
    for index in loads:
        window = "\n".join(lines[index:index + 10])
        assert "reassert_identity(" in window, (
            f"thinking_mode.py:{index + 1} loads a session without re-asserting the "
            "run's identity afterwards"
        )
