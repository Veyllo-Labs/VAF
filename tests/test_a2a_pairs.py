# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Who belongs to whom in a room, and why it cannot be claimed.

A room holds a person and their agent as two separate members. With several
households in one room the useful question stops being "who is here" and becomes
"who speaks for whom" - and that answer must be derived, never asserted, or the
first agent to claim somebody else's user gets believed.

What is pinned here is the derivation and its limits: it proves what it can prove,
says nothing where it cannot, and never treats a member's own file as evidence
about anybody.
"""
from pathlib import Path

import pytest

import vaf.core.a2a.store as store_mod
from vaf.core.a2a.room import Room, derive_peer_id, participant_key

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def rooms(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path)
    return tmp_path


def _household(room, tenant, *, human="", agent=""):
    """Seat one account's lanes exactly the way the product does."""
    out = {}
    if agent:
        out["agent"] = room.join(
            display=agent, scope_id=tenant,
            peer_id=derive_peer_id(participant_key("agent", tenant), room.room_id))
    if human:
        out["cli"] = room.join(
            display=human, scope_id=tenant,
            peer_id=derive_peer_id(participant_key("cli", tenant), room.room_id))
    return out


def test_a_lone_agent_has_no_partner_and_that_is_normal(rooms):
    """MUTATION: report a partner for a household whose person never joined.

    A person becomes a member the moment they first act in the room and not before,
    so an agent sitting there alone is the ORDINARY starting state. A surface that
    treats it as a fault would flag every fresh room.
    """
    room = Room.create(kind="round", owner_scope="tenant-a", base=rooms, room_id="room-solo")
    seats = _household(room, "tenant-a", agent="Nobel")

    pairs = room.pairs()
    mine = pairs[seats["agent"].peer_id]
    assert mine["kind"] == "agent", "the lane says what this seat is"
    assert mine["partner"] == "", "there is nobody to pair with yet"
    assert mine["proof"] == "derived"

    # The person speaks for the first time - the pair appears by itself, because it
    # was never stored anywhere to be updated.
    person = room.join(display="Alice", scope_id="tenant-a",
                       peer_id=derive_peer_id(participant_key("cli", "tenant-a"), "room-solo"))
    after = room.pairs()
    assert after[seats["agent"].peer_id]["partner"] == person.peer_id
    assert after[person.peer_id]["partner"] == seats["agent"].peer_id
    assert after[person.peer_id]["kind"] == "human"
    assert after[seats["agent"].peer_id]["partner_label"] == "Alice"


def test_two_households_are_not_mixed_up(rooms):
    """MUTATION: pair by display name, or by join order.

    Both would work in a two-member room and fall apart in the room this exists for.
    The handle is the evidence: it is blake2s over lane, tenant and room, so only the
    account that owns it can produce it.
    """
    room = Room.create(kind="round", owner_scope="tenant-a", base=rooms,
                       room_id="room-two", multi_scope=True, tenants=["tenant-b"])
    first = _household(room, "tenant-a", human="Alice", agent="Nobel")
    second = _household(room, "tenant-b", human="Ana", agent="Iris")

    pairs = room.pairs(tenants=["tenant-a", "tenant-b"])
    assert pairs[first["cli"].peer_id]["partner"] == first["agent"].peer_id
    assert pairs[second["cli"].peer_id]["partner"] == second["agent"].peer_id
    assert pairs[first["agent"].peer_id]["partner_label"] == "Alice"
    assert pairs[second["agent"].peer_id]["partner_label"] == "Ana"
    # And nobody is paired across the line.
    assert pairs[first["agent"].peer_id]["partner"] != second["cli"].peer_id


def test_a_member_cannot_write_itself_a_partner(rooms):
    """MUTATION: read the pairing out of the member record (a `speaks_for` field).

    The member file is written BY the member. Anything in it about who somebody
    belongs to is that peer naming its own partner, and in a room with strangers
    that is exactly the claim nobody may be allowed to make. The room recomputes the
    handle instead, and a claim that does not match one is simply not a pair.
    """
    room = Room.create(kind="round", owner_scope="tenant-a", base=rooms, room_id="room-liar")
    seats = _household(room, "tenant-a", human="Alice", agent="Nobel")
    guest = room.join(display="Codex", scope_id=None, peer_id="p-guest")

    # The guest writes every claim it can into its own record.
    record = dict(room.store.member(guest.peer_id) or {})
    record.update({"speaks_for": seats["cli"].peer_id, "kind": "agent",
                   "partner": seats["cli"].peer_id, "scope": "tenant-a",
                   "card": {"speaks_for": seats["cli"].peer_id}})
    room.store.put_member(guest.peer_id, record)

    pairs = room.pairs()
    assert pairs[guest.peer_id]["kind"] == "unknown", "a claim is not a derivation"
    assert pairs[guest.peer_id]["partner"] == ""
    assert pairs[guest.peer_id]["proof"] == ""
    # And the household it tried to attach itself to is untouched.
    assert pairs[seats["cli"].peer_id]["partner"] == seats["agent"].peer_id


def test_a_guest_is_left_unanswered_rather_than_guessed(rooms):
    """MUTATION: fall back to "probably an agent" for anybody unrecognised.

    A ticket joins with no tenant at all, so no derivation reaches it. Saying
    "unknown" is the honest answer and the one the invitation path later replaces
    with something proven; a guess here would be a wrong answer that nothing
    downstream could tell apart from a right one.
    """
    room = Room.create(kind="round", owner_scope="tenant-a", base=rooms, room_id="room-guest")
    _household(room, "tenant-a", human="Alice", agent="Nobel")
    ticket = room.mint_ticket(
        room.identity_for(participant_key("cli", "tenant-a")), display="Codex")
    guest = room.redeem_ticket(ticket, display="Codex")

    entry = room.pairs()[guest.peer_id]
    assert entry["kind"] == "unknown" and entry["partner"] == "" and entry["proof"] == ""


def test_the_pairs_come_from_the_rooms_own_guest_list(rooms):
    """MUTATION: read the accounts from a user store, or from the member files.

    The room's manifest is the one place a member cannot write, so it is the only
    honest source for "which accounts are in here". Taking the list from a user store
    would make a pure derivation depend on who happens to have an account on the
    machine; taking it from the member files would let a member nominate itself.
    """
    room = Room.create(kind="round", owner_scope="tenant-a", base=rooms,
                       room_id="room-scoped", multi_scope=True, tenants=["tenant-b"])
    _household(room, "tenant-a", human="Alice", agent="Nobel")
    other = _household(room, "tenant-b", human="Ana", agent="Iris")

    # No argument: the room answers for every account it admitted, so every member
    # can be shown every pair.
    everyone = room.pairs()
    assert everyone[other["cli"].peer_id]["partner"] == other["agent"].peer_id
    assert everyone[other["cli"].peer_id]["kind"] == "human"

    # An account the room never admitted is not resolved, even when asked for by name.
    outsider = derive_peer_id(participant_key("cli", "tenant-c"), "room-scoped")
    assert outsider not in room.pairs(tenants=["tenant-c"])

    # And a caller may still narrow it deliberately.
    narrowed = room.pairs(tenants=["tenant-a"])
    assert narrowed[other["cli"].peer_id]["kind"] == "unknown"


def test_a_cross_account_room_admits_only_the_accounts_it_named(rooms):
    """MUTATION: let `multi_scope` mean "anybody who knows the room id".

    A room id is not a secret and was never designed to be one: it travels in
    invitations, in prompts, in log lines, and an agent can be TOLD one inside a room
    message. If the flag alone opened the door, that agent could walk into a
    conversation belonging to five other people, and the only thing standing in front
    of it would be that nobody had mentioned the id yet.
    """
    from vaf.core.a2a.room import NotAMember

    room = Room.create(kind="round", owner_scope="tenant-a", base=rooms,
                       room_id="room-door", multi_scope=True, tenants=["tenant-b"])
    assert room.tenants() == ["tenant-a", "tenant-b"], "the owner is always admitted"

    _household(room, "tenant-b", human="Ana", agent="Iris")   # invited: goes through

    with pytest.raises(NotAMember) as refused:
        room.join(display="Eve", scope_id="tenant-c",
                  peer_id=derive_peer_id(participant_key("cli", "tenant-c"), "room-door"))
    assert "did not admit" in str(refused.value)

    # A room that never opened its doors takes nobody but its owner, flag or no flag.
    plain = Room.create(kind="round", owner_scope="tenant-a", base=rooms, room_id="room-shut")
    with pytest.raises(NotAMember):
        plain.join(display="Ana", scope_id="tenant-b",
                   peer_id=derive_peer_id(participant_key("cli", "tenant-b"), "room-shut"))


def test_a_person_joins_a_room_under_their_own_name():
    """MUTATION: fall back to the machine owner's name, the way it was.

    A person becomes a member of a room the first time they speak in it, and the name
    they get is the one the room shows everybody from then on. Taking the LOCAL
    ADMIN's name for whoever happens to be speaking is harmless on a one-person
    machine and a lie on any other - and it stops being cosmetic the moment a surface
    says who belongs to whom, because the room then asserts, with its own authority,
    that the admin is somebody else's user.
    """
    src = (ROOT / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    block = src.split("Speaking for themselves makes them a member too", 1)[1][:1400]
    assert "resolve_caller_username(" in block, (
        "a person joins under a name that is not theirs")
    assert "get_local_admin_username" not in block, (
        "the machine owner's name is still the fallback for every account")
    assert "allow_lookup=True" in block, (
        "without the lookup the resolver answers with the synthetic bucket")


def test_the_room_turn_names_the_agents_own_person(rooms):
    """MUTATION: leave the pairing out of the roster, or claim the nearest human.

    The derivation existed four lines above this roster and was used for a security
    decision - whether a write may ride on the user's authority - while the roster
    itself listed only role, host and you. In a room with one household an agent could
    guess; in a room with five, guessing means answering for somebody it does not work
    for.
    """
    room = Room.create(kind="round", owner_scope="tenant-a", base=rooms,
                       room_id="room-turn", multi_scope=True, tenants=["tenant-b"])
    mine = _household(room, "tenant-a", human="Alice", agent="Nobel")
    theirs = _household(room, "tenant-b", human="Ana", agent="Iris")
    room.say(theirs["cli"], "hello everyone")

    class _Waker:
        from vaf.core.agent import Agent as _Real
        collect_room_wake = _Real.collect_room_wake
        _room_unattended_report = _Real._room_unattended_report

        def __init__(self):
            self._current_user_scope_id = "tenant-a"
            self._current_username = "Alice"
            self._room_reply_streak = {}

    wake = _Waker().collect_room_wake(scopes=["tenant-a", "tenant-b"])
    assert wake is not None and wake["peer_id"] == mine["agent"].peer_id
    prompt = wake["prompt"]
    lines = [line for line in prompt.splitlines() if line.startswith("- ")]

    def _tags(name):
        return next((line for line in lines if line.startswith(f"- {name} [")), "")

    assert "YOUR USER" in _tags("Alice"), "the agent is not told which one is its user"
    assert "Iris's user" in _tags("Ana"), "the other household is not named"
    assert "Nobel" not in _tags("Ana"), "Ana belongs to Iris, not to Nobel"
    assert "YOUR USER" not in _tags("Ana"), "only one person here is this agent's"
    # And answering is not restricted - only the authority is.
    assert "STILL A PERSON, and you may answer" in prompt
    assert "does not carry your user's authority" in prompt


def test_an_agent_alone_is_told_that_nobody_here_is_its_user(rooms):
    """MUTATION: say nothing when the agent's person has not joined.

    Silence reads as "not mentioned", and the nearest human in the room is then the
    obvious candidate. Since a person becomes a member only when they first speak,
    this is the state every room starts in.
    """
    room = Room.create(kind="round", owner_scope="tenant-a", base=rooms, room_id="room-alone")
    _household(room, "tenant-a", agent="Nobel")
    guest = room.join(display="Codex", scope_id=None, peer_id="p-codex")
    room.say(guest, "anybody there?")

    class _Waker:
        from vaf.core.agent import Agent as _Real
        collect_room_wake = _Real.collect_room_wake
        _room_unattended_report = _Real._room_unattended_report

        def __init__(self):
            self._current_user_scope_id = "tenant-a"
            self._current_username = "Alice"
            self._room_reply_streak = {}

    prompt = _Waker().collect_room_wake(scopes=["tenant-a"])["prompt"]
    assert "your user is not in this room" in prompt
    roster = [line for line in prompt.splitlines() if line.startswith("- ")]
    assert not any("YOUR USER" in line for line in roster), (
        "nobody here is this agent's person, and marking one invites exactly the guess "
        "this line exists to prevent")
