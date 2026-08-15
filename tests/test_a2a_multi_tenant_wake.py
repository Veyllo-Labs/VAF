# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""One process, several accounts, and the question of whose agent gets woken.

The runner is a single loop serving every tenant on the machine, and the agent
object it drives carries whatever identity the last queued turn left on it. That is
fine for a chat turn, which binds its own identity before it runs - and it was not
fine for rooms, which polled "the current scope" and therefore woke exactly one
account's agent, decided by whoever happened to chat last.

What is pinned here: the poll asks for the accounts it was GIVEN, one directory walk
covers all of them, and the answer says which account the room belongs to so the
caller can run the turn as that person rather than as the last one.
"""
from pathlib import Path

import pytest

import vaf.core.a2a.store as store_mod
from vaf.core.a2a.room import (Room, derive_peer_id, local_room_tenants,
                               participant_key, unread_frames)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def rooms(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path)
    return tmp_path


def _room_for(tenant, room_id, base, *, guest="Codex"):
    """A room owned by `tenant`, with that account's agent in it and a stranger
    who has just said something - the shape that should cost a turn."""
    room = Room.create(kind="round", owner_scope=tenant, base=base, room_id=room_id)
    agent = room.join(display=f"Agent-{tenant}", scope_id=tenant,
                      peer_id=derive_peer_id(participant_key("agent", tenant), room_id))
    other = room.join(display=guest, scope_id=None, peer_id=f"p-{room_id}")
    room.say(other, f"hello {tenant}")
    return room, agent


def test_every_account_that_holds_a_room_is_polled(rooms):
    """MUTATION: ask with one key - the account that happens to be bound.

    This is the defect the round starts from: with one key, exactly one tenant's
    agent is ever woken, and which one is decided by whoever chatted last. The other
    accounts' agents sit in their rooms and never answer anything.
    """
    _room_for("tenant-a", "room-a", rooms)
    _room_for("tenant-b", "room-b", rooms)

    keys = [participant_key("agent", "tenant-a"), participant_key("agent", "tenant-b")]
    pending = unread_frames(keys)
    assert {room.room_id for room, _i, _w, _c in pending} == {"room-a", "room-b"}

    # And a single key still answers exactly for that one account.
    alone = unread_frames(participant_key("agent", "tenant-a"))
    assert {room.room_id for room, _i, _w, _c in alone} == {"room-a"}


def test_the_accounts_come_from_the_rooms_on_disk(rooms, monkeypatch):
    """MUTATION: read the tenant list from the user store.

    The question is not "who has an account here" but "whose rooms is this machine
    holding" - the only accounts that can have an agent waiting. Deriving it from the
    manifests keeps this package thin (it reaches for no database) and keeps the poll
    honest on a machine where most accounts have no room at all.
    """
    _room_for("tenant-a", "room-a", rooms)
    _room_for("tenant-b", "room-b", rooms)
    monkeypatch.setattr("vaf.core.config.get_local_admin_scope_id", lambda: "tenant-admin")

    found = local_room_tenants()
    assert set(found) >= {"tenant-a", "tenant-b"}
    assert "tenant-admin" in found, "a machine with no room yet still has one account"
    assert len(found) == len(set(found)), "the same account twice would poll it twice"


def test_the_wake_says_which_account_the_room_belongs_to(rooms):
    """MUTATION: return the bound scope instead of the room's.

    The caller binds this before running the turn. Getting it wrong does not fail
    loudly - it builds the prompt, the memory seed, the workspace and the tool set for
    the wrong person, which is the quiet cross-user leak the binding contract exists
    to prevent.
    """
    _room_for("tenant-a", "room-a", rooms)
    _room_for("tenant-b", "room-b", rooms)

    class _Waker:
        # Borrowed from the real agent, the way the other room tests do it: both
        # methods, because collect_room_wake calls the reporter and swallows every
        # exception - an incomplete stand-in returns None and looks like a defect.
        from vaf.core.agent import Agent as _Real
        collect_room_wake = _Real.collect_room_wake
        _room_unattended_report = _Real._room_unattended_report

        def __init__(self, scope):
            self._current_user_scope_id = scope
            self._current_username = "owner"
            self._room_reply_streak = {}

    # Bound to A, asked for both: it must still be able to answer for B.
    seen = {}
    waker = _Waker("tenant-a")
    for _ in range(2):
        wake = waker.collect_room_wake(scopes=["tenant-a", "tenant-b"])
        if wake is None:
            break
        seen[wake["room_id"]] = wake["scope"]
        wake["advance"]()          # so the next call moves on to the other room

    assert seen == {"room-a": "tenant-a", "room-b": "tenant-b"}, (
        "a room turn must name the account whose room it is")


def test_the_room_turn_is_bound_and_restored(rooms):
    """MUTATION: run the turn without binding, or bind without restoring.

    Without the bind, the turn runs as whoever was there before. Without the restore,
    the NEXT thing this loop does - a sub-agent drain, a summary, an automation -
    inherits a tenant it was never given, which is the same leak one step later.
    """
    src = (ROOT / "vaf" / "core" / "headless_runner.py").read_text(encoding="utf-8")
    branch = src.split("_room_wake = agent.collect_room_wake", 1)[1].split(
        "_pushed = False", 1)[0]

    assert "bind_identity(agent, room_identities[_room_scope])" in branch, (
        "the room turn does not run as the account whose room it is")
    assert branch.index("bind_identity(agent, room_identities[_room_scope])") \
        < branch.index("agent.chat_step("), "bound after the turn is bound too late"
    restore = branch.split("finally:", 1)[1]
    assert "bind_identity(agent, _prev_identity)" in restore, (
        "the previous identity is never put back")
    # The resolve is a database round trip; its own docstring says one per run is
    # affordable and one per call is not.
    assert "if _room_scope not in room_identities:" in branch, (
        "a resolve on every poll would be an uncached lookup every two seconds")


def test_no_account_s_person_is_ever_checked_in_on(rooms, monkeypatch):
    """MUTATION: skip only the ROOM OWNER's person, the way it was.

    A check-in wakes an agent and spends a model turn. A person's lane wakes nothing
    and answers nothing, so a check-in there is a message into a void, hourly, for as
    long as the room exists. That was measured once for the owner and fixed; in a room
    across tenant lines the other four people are ordinary idle peers to this loop and
    would get exactly the same treatment.
    """
    import time as _time

    import vaf.core.headless_runner as runner

    room = Room.create(kind="round", owner_scope="tenant-a", base=rooms,
                       room_id="room-people", multi_scope=True)
    # The second account was admitted to this room. The manifest is the door and the
    # only writer of it is the room itself - a member file could not carry this,
    # because a member writes its own.
    room.store.update_manifest(tenants=["tenant-b"])
    room = Room.open("room-people", base=rooms)
    host = room.join(display="Nobel", scope_id="tenant-a",
                     peer_id=derive_peer_id(participant_key("agent", "tenant-a"), "room-people"))
    people = {}
    for tenant, name in (("tenant-a", "Alice"), ("tenant-b", "Ana")):
        people[name] = room.join(
            display=name, scope_id=tenant,
            peer_id=derive_peer_id(participant_key("cli", tenant), "room-people"))
    stranger = room.join(display="Codex", scope_id=None, peer_id="p-codex")
    room.say(host, "anybody there?")

    later = _time.time() + 7200
    monkeypatch.setattr(runner.time, "time", lambda: later)
    monkeypatch.setattr("vaf.core.config.Config.get",
                        lambda key, default=None: 60 if key == "a2a_room_ping_minutes" else default)
    monkeypatch.setattr("vaf.core.config.get_local_admin_scope_id", lambda: "tenant-a")
    runner._PING_SENT.clear()
    try:
        runner._room_ping_sweep()
    finally:
        runner._PING_SENT.clear()

    asked = [f["to"].get("peer") for f in room.transcript() if f["kind"] == "ping"]
    assert asked == [stranger.peer_id], (
        f"a person was checked in on: {[p for p in asked if p != stranger.peer_id]}")
    for name, seat in people.items():
        assert seat.peer_id not in asked, f"{name} is not an agent to wake"
