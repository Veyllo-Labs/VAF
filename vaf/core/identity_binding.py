# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Who this turn runs as, bound onto the engine the same way from every door.

VAF says whose turn this is with three attributes on the agent object:
``_current_user_scope_id``, ``_current_username`` and ``_current_user_role``. Ten places
write them by hand - the public facade, the thinking lane, two automation lanes, the
gateway, two TUI lanes, the CLI agent factory and the headless runner's two halves - and
an eleventh, ``load_session_context``, overwrites two of the three from session metadata
in the middle of several of those. The attributes are read in roughly thirty places that
decide which file jail applies, which memories are searched, which workspace directory is
opened and whose messenger a result is delivered to. So "which door did this turn come
through" is currently part of the answer to "whose data is this", and that is the same
defect the tool dispatcher was built to end.

MEASURED BEFORE BUILDING. Every clause below has a number behind it, taken from the tree
as it stood:

- 10 hand-rolled bind sites across 7 files.
- 6 of those bind ``uuid.UUID``; 4 bind the raw string. ONE attribute, TWO types, chosen
  by the door. The stores on the other end do not agree either: ``skills_registry`` tests
  membership with a raw ``in`` against a list of JSON strings, and ``SessionManager.save``
  is ``json.dumps`` with no ``default=``, so a UUID there is a crash.
- The reason written at 3 of those 6 sites, "the memory tools expect the UUID", is STALE.
  ``run_memory_search_sync`` normalises the scope itself and denies an unparseable one.
  The coercion buys nothing and costs the two breakages above.
- 3 verbatim copies of the same six-line cross-user-leak comment, plus 2 prose
  restatements of the same rule in two docstrings. A rule stated five times has no home.
- 5 hand-rolled copies of the synthetic ``scope_<8hex>`` fallback beside the canonical one
  in ``config.resolve_caller_username``.
- ``_current_user_role`` has exactly 2 writers in the whole tree, both in the headless
  runner, and 5 readers, every one of them ``getattr(obj, "_current_user_role", None)``.
  So eight of the ten binders can never clear a role, and no reader can tell an absent
  attribute from a None one.

WHY THE ASYMMETRY. ``bind_identity`` writes all three fields UNCONDITIONALLY, so a missing
field CLEARS the previous turn's value. ``reassert_identity`` writes only the fields the
Identity actually carries a value for. They are not two spellings of one idea; they answer
two different questions about the same object.

One agent object serves many queued turns. When the next turn's identity is missing a
field, the honest answer is "nobody", not "whoever was here last": leaving the previous
tenant's username in place is how their workspace, their mail store and their messenger
end up serving the next person. That is why the bind clears. But between the bind and the
turn, ``load_session_context`` may run, and it assigns the SESSION owner's scope and
username unconditionally, including None. Re-asserting the caller's identity afterwards
must therefore put back what the caller knows and must NOT blank what the session
legitimately supplied. That is why the re-assert is conditional on VALUE, never on key
presence: a Discord task carries ``{"user_scope_id": None, "username": "admin"}`` with the
key present and the value absent, and treating that as "carried" would blank a hydrated
session owner.

The re-assert also asserts FORWARD. It does not roll back to a previous identity, because
there is none worth restoring: the agent object is per-lane and the next turn opens with a
full bind anyway. That is why this module ships two functions and NO context manager. A
``with`` block cannot express "run the second half only when the body succeeded" without a
flag, and at the one site with a real clobberer that distinction is the whole point - a
failed session load must leave the session's own identity standing, which is exactly what
a ``finally`` would destroy. A flag there would be a security parameter with two values
and no name.

NO COERCION, NO RESOLUTION, IN THE BINDERS. ``bind_identity`` and ``reassert_identity`` are
pure attribute writes. Everything that decides a VALUE - is this the owner, what is this
scope's account name, is this string a UUID - happens in a named producer above them, at a
site that knows which lane it is. The scope is bound BYTE-IDENTICAL to what the caller
supplied, including None, because it is simultaneously a directory name, a filename
component, a JSON value, a dict key and one half of an admin comparison, and every one of
those five uses breaks differently under normalisation. Canonicalising it for a comparison
is a real and separate question; it belongs in ``config.is_admin_identity``, on both sides
at once, in its own change, not smuggled in behind an extraction.

THE INCIDENT CLASS. Every one of these binders was written correctly, in isolation, by
someone who had read the previous one. The rule they all encode - a non-admin scope must
never resolve to the literal "admin", because the username keys ``~/.vaf/users/<name>``
and hands over a stranger's profile, mail and messenger - is right in the thinking lane
and right in both automation lanes and right in the facade. It was missing entirely in the
thinking lane's re-assert after a session load, where a session whose metadata carries no
username still falls through to ``or "admin"`` three times. A rule stated five times and
enforced in four of them is not a convention; it is a coin flip with good documentation.

NAMED BOUNDARY: deliberately NOT on the public facade. Identity is asserted by the
embedder at construction (``Agent(user_scope=...)``, validated there, documented in
docs/EMBEDDING.md) and applied by the library; a third party building on VAF binds nothing
by hand and needs no export. The measurement that would earn one is an embedder who has to
re-apply an identity around a call the facade does not wrap - none exists today, and the
facade's own binder is now a two-line consumer of this module. The export is one lazy
``__getattr__`` branch the day that measurement appears.

This module imports ``vaf.core.config`` and, through it and lazily, ``vaf.core.thinking_mode``.
It must never be imported BY ``vaf/core/config.py``: config is imported by ``vaf/auth``,
``vaf/api``, ``vaf/tools`` and ``vaf/core``, and inverting that arrow is the layer-order
defect this codebase has already paid for once.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

__all__ = [
    "Identity",
    "identity_from_metadata",
    "resolve_owner_identity",
    "resolve_scope_identity",
    "bind_identity",
    "reassert_identity",
]


@dataclass(frozen=True)
class Identity:
    """Whose turn this is: the three values the engine reads, carried together.

    A CARRIER, not a resolver. It normalises nothing and validates nothing - the producer
    that built it already decided, in the one place that knows which lane this is.

    Contract, each choice against its failure mode:

    - ``scope`` is whatever the producer supplied, byte-identical, and ``None`` stays
      ``None``. It reaches five kinds of consumer that disagree about types: a directory
      name (the handoff bundles, the thinking workspace, the per-scope contacts store), a
      filename component (the thinking session id), a JSON value (``SessionManager.save``
      is ``json.dumps`` with no ``default=``), a dict key (the thinking locks file), and
      one half of the admin comparison in ``config.is_admin_identity``. Coerce it and the
      first three move silently, the fourth crashes, and the fifth can demote the machine
      owner whose configured scope is stored in a different spelling.
    - ``None`` is a THIRD value, distinct from a scope and from the empty string.
      ``is_admin_identity`` short-circuits on ``None`` only; ``compute_user_jail`` reads a
      falsy scope as "direct consumer, no jail"; the unscoped attachments directory is
      keyed on it. A producer that substitutes the owner's scope for ``None`` moves every
      Discord and tokenless-desktop attachment and turns an unauthenticated caller into
      the owner.
    - ``username`` may be ``None``. It means "nobody said", and the dispatcher resolves it
      per call from the SCOPE. Defaulting it here to the owner's name is the exact leak
      the six-line comments were written against, and defaulting it to the literal
      ``"admin"`` is that leak plus a person who exists on no installation whose owner
      registered under another name.
    - ``role`` defaults to ``None`` because eight of the ten binders have never had one.
      ``None`` is not "unknown, be generous": ``is_admin_identity`` still grants the
      machine owner admin through the SCOPE half, and every role reader in the tree uses
      ``getattr(obj, "_current_user_role", None)``, so an absent attribute and a None one
      are indistinguishable. Restrictive, never permissive: a missing role frees nobody.
    """

    scope: Any
    username: Optional[str]
    role: Optional[str] = None


def identity_from_metadata(meta: Optional[Mapping[str, Any]]) -> Identity:
    """The identity a queued task or a session carries, taken RAW.

    Contract, each choice against its failure mode:

    - No lookup, no fallback, no coercion. Task metadata is already authoritative - it was
      written by the enqueuing path from a verified token or from the owner's own session -
      and resolving it again would replace a real name with a synthetic bucket, which is a
      new on-disk workspace directory per nameless tenant and the loss of the ``"admin"``
      allowlist match that gates the messenger send tools.
    - A present key with a ``None`` value stays ``None``, not "absent". The Discord bridge
      enqueues ``{"user_scope_id": None, "username": "admin"}`` and means it.
    - ``meta`` may be ``None`` or any mapping; anything unusable yields an all-``None``
      Identity, which the binder then applies as a full clear. Fail-closed by construction.
    """
    meta = meta or {}
    return Identity(
        scope=meta.get("user_scope_id"),
        username=meta.get("username"),
        role=meta.get("role"),
    )


def resolve_owner_identity() -> Identity:
    """The machine owner, for the lanes that have no authentication at all.

    The CLI and the TUI run as the person at this machine. They have no token to consult
    and no tenant to distinguish, so the configured local admin IS the caller.

    Contract, each choice against its failure mode:

    - The configured scope is bound VERBATIM, with no UUID parse. Parsing was the bug, not
      the safeguard: ``is_admin_identity`` compares the bound scope against the same config
      value as a plain string, so a canonicalising parse on one side only demotes the
      machine owner on any installation whose ``local_admin_scope_id`` was hand-set or
      patched in a non-canonical spelling - the owner gets jailed out of their own files.
      Verbatim is self-consistent by construction.
    - An EMPTY or whitespace-only configured scope yields ``scope=None`` and the caller
      binds nothing rather than a blank string. ``""`` and ``None`` are read the same way
      by the jail but differently by ``is_admin_identity``, and a config value that says
      nothing must not become an assertion.
    - The username is the CONFIGURED admin name, never the literal ``"admin"``. The literal
      is a real person's directory on installations whose owner registered under another
      name.
    - Never raises for a missing or malformed config value. Callers wrap this in a bare
      ``except`` that must not become the reason the CLI fails to start, and a raising
      producer inside that swallow is indistinguishable from one that ran.
    """
    from vaf.core.config import get_local_admin_scope_id, get_local_admin_username

    scope = get_local_admin_scope_id()
    if not scope or not str(scope).strip():
        scope = None
    return Identity(scope=scope, username=get_local_admin_username(), role=None)


def resolve_scope_identity(scope: Any, *, role: Optional[str] = None) -> Identity:
    """A scope, and the ACCOUNT NAME that belongs to it. Never the literal "admin".

    For the lanes that are handed a scope by something outside the turn - an embedder's
    assertion, a saved automation, a thinking run - and must answer "and who is that?"
    before the system prompt is built.

    Contract, each choice against its failure mode:

    - The answer is ``config.resolve_caller_username(None, scope, allow_lookup=True)``, the
      one definition. The rule used to live here, and in the automation lane, and in the
      thinking lane, and in the facade. It has one home now, and this module is a consumer
      of it, not a fifth copy.
    - ``allow_lookup=True`` is deliberate HERE and must stay off on the per-dispatch path.
      It is an uncached database round trip (and, inside a running event loop, a thread
      with a ten-second join). One per run is affordable; one per tool call is not. Callers
      cache the Identity for the life of the run - see the facade, where ``run()``
      re-asserts on every turn and an uncached resolve would spend a fresh ``asyncio.run``
      against a pooled engine bound to a closed loop, silently degrading a real account
      name into the synthetic bucket somewhere around turn two.
    - A WHITESPACE-ONLY scope is a corrupt value, not an absent one, and resolves to the
      isolated ``"scope_unknown"`` bucket. The shared resolver strips first, so ``"   "``
      would otherwise take its no-scope branch and answer with the OWNER's name - a
      fail-toward-admin transition, reachable from a hand-edited or restored task file, and
      precisely the direction the cross-user-leak rule exists to forbid.
    - The scope itself is returned unchanged, ``None`` included. Only the NAME is resolved.
    - The database-backed helper stays where it lives and is reached through the shared
      resolver, which imports it LATE. A module-scope import here would both create an
      import cycle and silently un-test the never-"admin" guarantee: the facade's test
      proves it by monkeypatching that module attribute and making it raise, which only
      bites while the lookup is late-bound.
    """
    from vaf.core.config import resolve_caller_username

    scope_text = "" if scope is None else str(scope)
    if scope_text and not scope_text.strip():
        return Identity(scope=scope, username="scope_unknown", role=role)
    return Identity(
        scope=scope,
        username=resolve_caller_username(None, scope, allow_lookup=True),
        role=role,
    )


def bind_identity(agent: Any, identity: Identity) -> None:
    """Make this agent run as this identity. Writes all three fields, always.

    Contract, each choice against its failure mode:

    - UNCONDITIONAL, including ``None``. One agent object serves many queued turns, so a
      field the new identity does not carry must CLEAR the previous turn's value, never
      inherit it. Inheritance here is how one tenant's workspace, mail store and messenger
      end up serving the next person in the queue.
    - Writes ``_current_user_role`` even on lanes that have never had one, creating the
      attribute as ``None``. Verified safe: every reader in the tree uses
      ``getattr(obj, "_current_user_role", None)``, there is no ``hasattr`` on it anywhere,
      and nothing serialises the agent's ``__dict__``. Creating it is also the point - it
      is the only way a stale role from an earlier task stops surviving a rebind.
    - A pure attribute write: no lookup, no coercion, no I/O, no exceptions of its own. It
      runs on the hot path of the public facade, once per turn.
    - Call it BEFORE ``init_chat()``. The system prompt's user context, the memory seed,
      the last-interaction line and the per-turn tool set are all built from these three
      attributes; binding afterwards produces a prompt addressed to the wrong person.
    """
    agent._current_user_scope_id = identity.scope
    agent._current_username = identity.username
    agent._current_user_role = identity.role


def reassert_identity(agent: Any, identity: Identity) -> None:
    """Put an identity back after something overwrote it. Forward only, value-based.

    ``load_session_context`` assigns the SESSION's scope and username unconditionally,
    including ``None``, on purpose: switching into a session with no owner must not let
    the previous session's owner bleed through. For a lane whose caller identity is
    authoritative (a queued task, an embedder's assertion, the single-user desktop) that
    load is a clobber, and this puts the authoritative answer back.

    Contract, each choice against its failure mode:

    - Writes only fields whose VALUE is not ``None``, never fields whose key was present.
      A task carrying an explicit ``username=None`` must not blank a username the session
      legitimately supplied; a task carrying a real username must win over the session's.
    - Asserts FORWARD. It restores nothing and rolls nothing back: there is no earlier
      identity worth returning to, and the next turn opens with a full ``bind_identity``.
    - Call it EXPLICITLY, on the success path of whatever did the clobbering, inside that
      caller's own error handling. It is NOT a ``finally``: when a session load fails, the
      turn must keep the identity the failure left standing, and a ``finally`` would
      overwrite it. That is also why this module ships no context manager.
    - Never clears a stale role, by construction. It cannot: a role it does not carry is a
      role it has no answer for. Clearing is ``bind_identity``'s job, at the top of the
      turn.
    """
    if identity.scope is not None:
        agent._current_user_scope_id = identity.scope
    if identity.username is not None:
        agent._current_username = identity.username
    if identity.role is not None:
        agent._current_user_role = identity.role
