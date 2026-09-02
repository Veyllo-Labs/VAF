# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Who an agent belongs to, provably: the owner's attestation on an agent's join.

`Room.pairs` derives "which agent is whose" by recomputing handles from the accounts
the room admits, which reaches nobody who arrived on a ticket and nobody reading the
transcript on another machine. An agent's `join` may therefore carry its OWNER's
attestation - a signature by the owner's room key over the agent's handle and key -
and a reader anywhere folds the pair off the log (`fold_owners`).

The half worth being strict about is what a block is NOT allowed to do. It rides only
inside an attested join; it names an owner key that must be the one the owner's own
attested join bound, or a fresh keypair could vouch for anybody; and it follows the
C15 rule for what a later join does to it. Every one of those is a guard here, and
each was proven by taking the rule out and watching the test go red.
"""
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import vaf.core.a2a.store as store_mod
from vaf.core import data_files
from vaf.core.a2a import signing
from vaf.core.a2a.room import Room, derive_peer_id, fold_owners, participant_key

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def rooms(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path)
    return tmp_path


# ── raw keys, the way a guest on the wire holds them ───────────────────────

def _raw_pair(seed: bytes):
    """A keypair that is nobody's: this file pins rules, not real identities."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.from_private_bytes(seed)
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    return private, public


def _presented(room, identity, pair, payload):
    """Ingest a payload signed with a RAW key, the way a peer on the wire presents one."""
    private, public = pair
    content = room.compose(payload)
    message = signing.canonical_bytes(
        signing.covered_payload(room.room_id, identity.peer_id, content))
    sig = {"alg": "ed25519", "v": signing.VERSION, "key": public,
           "sig": private.sign(message).hex()}
    return room.ingest(dict(payload) | {"sig": sig}, identity=identity)


def _announce(room, identity, pair, display, owner=None):
    """A self-attested join, optionally carrying the owner's block."""
    body = {"display": display, "card": {}, "sign_key": pair[1]}
    if owner:
        body["owner"] = owner
    return _presented(room, identity, pair, {"kind": "join", "body": body})


def _block(room_id, owner_pair, owner_peer, agent_peer, agent_key):
    private, public = owner_pair
    message = signing.owner_bytes(
        signing.owner_payload(room_id, owner_peer, agent_peer, agent_key))
    return {"v": signing.OWNER_VERSION, "peer": owner_peer, "key": public,
            "sig": private.sign(message).hex()}


ANA, IRIS = bytes(range(32)), bytes(range(1, 33))


def _household(room, *, attest=True, owner_announces=True):
    """A guest household: a person and their agent, both on tickets with their own
    keys, the agent's announcement carrying the person's attestation."""
    ana_pair, iris_pair = _raw_pair(ANA), _raw_pair(IRIS)
    ana = room.join(display="Ana", scope_id=None, peer_id="p-ana")
    iris = room.join(display="Iris", scope_id=None, peer_id="p-iris")
    if owner_announces:
        _announce(room, ana, ana_pair, "Ana")
    block = (_block(room.room_id, ana_pair, ana.peer_id, iris.peer_id, iris_pair[1])
             if attest else None)
    _announce(room, iris, iris_pair, "Iris", owner=block)
    return ana, iris, ana_pair, iris_pair


# ── the primitive ───────────────────────────────────────────────────────────

def test_the_attestation_bytes_are_pinned():
    """A test vector, so a change to the form is a decision. A stranger implementing
    this from the document reproduces these bytes or their attestations bind nowhere."""
    payload = signing.owner_payload("room-abc", "p-owner", "p-agent", "ab" * 32)
    assert payload == {"v": 1, "room": "room-abc", "owner": "p-owner",
                       "agent": "p-agent", "agent_key": "ab" * 32}
    raw = signing.owner_bytes(payload)
    assert raw.startswith(b"vaf-a2a-owner/v1\n")
    assert raw != signing.DOMAIN + raw[len(signing.OWNER_DOMAIN):], \
        "the domain separates it from a frame signature's input"
    assert raw == b"vaf-a2a-owner/v1\n" + json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def test_an_attestation_verifies_and_every_covered_field_breaks_it(rooms):
    owner_key = participant_key("cli", "scope-a")
    block = signing.attest("room-x", owner_key=owner_key, owner_peer="p-owner",
                           agent_peer="p-agent", agent_key="ab" * 32)
    assert block["key"] == signing.public_key(owner_key, "room-x")
    assert signing.verify_attestation("room-x", "p-agent", "ab" * 32, block) is True
    assert signing.verify_attestation("room-y", "p-agent", "ab" * 32, block) is False
    assert signing.verify_attestation("room-x", "p-other", "ab" * 32, block) is False
    assert signing.verify_attestation("room-x", "p-agent", "cd" * 32, block) is False
    assert signing.verify_attestation("room-x", "p-agent", "ab" * 32,
                                      {**block, "peer": "p-somebody"}) is False
    other = signing.public_key(participant_key("cli", "scope-b"), "room-x")
    assert signing.verify_attestation("room-x", "p-agent", "ab" * 32,
                                      {**block, "key": other}) is False


@pytest.mark.parametrize("block", [
    None, "", {}, [],
    {"peer": "p-o", "key": "a" * 63, "sig": "b" * 128},
    {"peer": "p-o", "key": "z" * 64, "sig": "b" * 128},
    {"peer": "", "key": "a" * 64, "sig": "b" * 128},
    {"v": 2, "peer": "p-o", "key": "a" * 64, "sig": "b" * 128},
])
def test_a_block_this_reader_cannot_use_is_nothing_to_check(block):
    """A shape or a version this reader cannot parse binds nothing and accuses
    nobody - the same reading a frame signature gets."""
    assert signing.read_attestation(block) is None
    assert signing.verify_attestation("room-x", "p-agent", "a" * 64, block) is False


# ── the host's own household, made portable ────────────────────────────────

def test_the_hosts_agent_join_carries_its_persons_attestation(rooms):
    """MUTATION: publish the key without the block.

    The derivation `pairs` makes reaches only this machine. The block rides inside
    the agent's signed join, so a reader anywhere folds the same pair - and it can be
    made at the agent's join, before the person has ever spoken here, because the
    person's key comes out of the same account.
    """
    room = Room.create(kind="round", owner_scope="scope-a", base=rooms, room_id="room-host")
    agent_key, person_key = participant_key("agent", "scope-a"), participant_key("cli", "scope-a")
    agent = room.join(display="Nobel", scope_id="scope-a",
                      peer_id=derive_peer_id(agent_key, "room-host"), participant_key=agent_key)

    body = next(f for f in room.store.frames() if f.kind == "join").body
    block = body["owner"]
    assert block["peer"] == derive_peer_id(person_key, "room-host")
    assert block["key"] == signing.public_key(person_key, "room-host")
    assert signing.verify_attestation("room-host", agent.peer_id, body["sign_key"], block)

    # Nobody has spoken with the person's key yet, so the claim is a key nobody has
    # corroborated - and a reader off the host must not pair on it.
    assert fold_owners(room.store.frames(), "room-host") == {}

    person = room.join(display="Alice", scope_id="scope-a",
                       peer_id=derive_peer_id(person_key, "room-host"),
                       participant_key=person_key)
    assert fold_owners(room.store.frames(), "room-host") == {agent.peer_id: person.peer_id}
    # On the host itself the derivation still answers, and the two agree.
    assert room.pairs()[agent.peer_id]["proof"] == "derived"
    assert room.pairs()[agent.peer_id]["partner"] == person.peer_id


def test_a_person_carries_no_attestation_and_a_remote_lane_none_either(rooms):
    """A person attests an agent; nothing attests a person. And the remote lane is
    never signed for by the host, so it cannot be attested by the host either."""
    room = Room.create(kind="round", owner_scope="scope-a", base=rooms, room_id="room-lane")
    for lane in ("cli", "remote"):
        key = participant_key(lane, "scope-a")
        room.join(display=lane, scope_id="scope-a",
                  peer_id=derive_peer_id(key, "room-lane"), participant_key=key)
    for frame in room.store.frames():
        assert "owner" not in (frame.body or {}), frame.sender


# ── a guest household, paired from the transcript alone ────────────────────

def test_a_guest_household_is_paired_from_the_transcript_alone(rooms):
    """THE feature. Two peers on tickets, whom no derivation reaches, and the room
    still says which is whose - because the log says so in a way anybody can check."""
    room = Room.create(kind="round", owner_scope="scope-a", base=rooms, room_id="room-guest")
    ana, iris, _, _ = _household(room)

    assert fold_owners(room.store.frames(), "room-guest") == {iris.peer_id: ana.peer_id}
    pairs = room.pairs()
    assert pairs[iris.peer_id] == {"peer": iris.peer_id, "kind": "agent",
                                   "partner": ana.peer_id, "partner_label": "Ana",
                                   "proof": "attested"}
    assert pairs[ana.peer_id] == {"peer": ana.peer_id, "kind": "human",
                                  "partner": iris.peer_id, "partner_label": "Iris",
                                  "proof": "attested"}


def test_a_claim_by_a_key_nobody_has_spoken_with_binds_nothing(rooms):
    """MUTATION: pair on any block that verifies with the key it names.

    A fresh keypair can make a block that verifies. What it cannot do is be the key
    that signed the owner's own words in this room - and that corroboration is the
    whole difference between "somebody vouched" and "somebody typed".
    """
    room = Room.create(kind="round", owner_scope="scope-a", base=rooms, room_id="room-fresh")
    # The owner never announced a key, so the block names a key nobody has spoken with.
    ana, iris, _, _ = _household(room, owner_announces=False)
    assert fold_owners(room.store.frames(), "room-fresh") == {}
    assert room.pairs()[iris.peer_id]["kind"] == "unknown"

    # The owner HAS announced - but the block was made with some other key.
    room2 = Room.create(kind="round", owner_scope="scope-a", base=rooms, room_id="room-fresh2")
    ana_pair, iris_pair, fresh = _raw_pair(ANA), _raw_pair(IRIS), _raw_pair(bytes(range(2, 34)))
    ana = room2.join(display="Ana", scope_id=None, peer_id="p-ana")
    iris = room2.join(display="Iris", scope_id=None, peer_id="p-iris")
    _announce(room2, ana, ana_pair, "Ana")
    _announce(room2, iris, iris_pair, "Iris",
              owner=_block("room-fresh2", fresh, ana.peer_id, iris.peer_id, iris_pair[1]))
    assert fold_owners(room2.store.frames(), "room-fresh2") == {}


@pytest.mark.parametrize("wrong", ["agent", "agent_key", "room"])
def test_changing_what_was_attested_breaks_it(rooms, wrong):
    """The block covers the agent's handle, its key and the room, so it can be lifted
    onto none of them."""
    room = Room.create(kind="round", owner_scope="scope-a", base=rooms, room_id="room-cov")
    ana_pair, iris_pair = _raw_pair(ANA), _raw_pair(IRIS)
    ana = room.join(display="Ana", scope_id=None, peer_id="p-ana")
    iris = room.join(display="Iris", scope_id=None, peer_id="p-iris")
    _announce(room, ana, ana_pair, "Ana")
    inputs = {"room": "room-cov", "agent": iris.peer_id, "key": iris_pair[1]}
    if wrong == "agent":
        inputs["agent"] = "p-somebody"
    elif wrong == "agent_key":
        inputs["key"] = _raw_pair(bytes(range(3, 35)))[1]
    else:
        inputs["room"] = "room-elsewhere"
    _announce(room, iris, iris_pair, "Iris",
              owner=_block(inputs["room"], ana_pair, ana.peer_id, inputs["agent"], inputs["key"]))
    assert fold_owners(room.store.frames(), "room-cov") == {}


def test_a_host_cannot_move_the_block_onto_another_agent(rooms):
    """The block rides inside the agent's signed join. A host that copies that join
    into another lane breaks the signature that binds the key, so the join is not
    attested at all and the block inside it is never read."""
    room = Room.create(kind="round", owner_scope="scope-a", base=rooms, room_id="room-move")
    ana, iris, _, _ = _household(room)
    room.join(display="Thief", scope_id=None, peer_id="p-thief")

    announcement = [f for f in room.store.frames()
                    if f.sender == iris.peer_id and f.kind == "join"][-1]
    stored = data_files.read_json(
        room.store.lane(iris.peer_id) / f"{announcement.seq:012d}.json", default=None)
    stored["from"], stored["seq"], stored["lamport"] = "p-thief", 2, 90
    stored["id"] = "00000000-0000-4000-8000-00000000ab11"
    data_files.write_json_atomic(room.store.lane("p-thief") / "000000000002.json", stored)

    owners = fold_owners(room.store.frames(), "room-move")
    assert owners == {iris.peer_id: ana.peer_id}, "the thief gained nothing"
    assert "p-thief" not in room.signing_keys()


def test_rejoining_without_the_block_withdraws_and_an_unattested_rejoin_changes_nothing(rooms):
    """MUTATION: keep the claim across a join without a block, or let an unsigned
    join clear it.

    C15 applied to ownership. The last attested join decides, so the agent withdraws
    by announcing again without the block. An announcement this reader cannot check
    must not undo one it already checked - or a host could strip a stored join's
    signature and unpair an honest household.
    """
    room = Room.create(kind="round", owner_scope="scope-a", base=rooms, room_id="room-again")
    ana, iris, ana_pair, iris_pair = _household(room)
    assert fold_owners(room.store.frames(), "room-again") == {iris.peer_id: ana.peer_id}

    _announce(room, iris, iris_pair, "Iris")            # attested, no block: withdrawn
    assert fold_owners(room.store.frames(), "room-again") == {}

    _announce(room, iris, iris_pair, "Iris",
              owner=_block("room-again", ana_pair, ana.peer_id, iris.peer_id, iris_pair[1]))
    assert fold_owners(room.store.frames(), "room-again") == {iris.peer_id: ana.peer_id}

    # Unattested: a key announced with nothing to attest it. The host does not sign
    # for a guest, so this is what a stripped or a broken announcement looks like.
    room.ingest({"kind": "join", "body": {"display": "Iris", "card": {},
                                          "sign_key": iris_pair[1]}}, identity=iris)
    assert fold_owners(room.store.frames(), "room-again") == {iris.peer_id: ana.peer_id}


def test_the_fold_never_raises_on_a_malformed_block(rooms):
    room = Room.create(kind="round", owner_scope="scope-a", base=rooms, room_id="room-odd")
    ana_pair, iris_pair = _raw_pair(ANA), _raw_pair(IRIS)
    ana = room.join(display="Ana", scope_id=None, peer_id="p-ana")
    iris = room.join(display="Iris", scope_id=None, peer_id="p-iris")
    _announce(room, ana, ana_pair, "Ana")
    for odd in ({"peer": 5, "key": None, "sig": []}, {"v": "x"}, {"sig": "not hex" * 16}):
        _announce(room, iris, iris_pair, "Iris", owner=odd)
        assert fold_owners(room.store.frames(), "room-odd") == {}
    assert room.pairs()[iris.peer_id]["kind"] == "unknown"


# ── the surfaces that read it ───────────────────────────────────────────────

def test_the_room_turn_names_a_guest_household_from_the_attestation(rooms):
    """MUTATION: leave the attested pairs out of `pairs()`.

    The roster in the room turn is where the answer matters most: an agent that
    cannot tell whose user Ana is answers for somebody it does not work for. The
    derivation named the host's own household and left every guest `unknown`.
    """
    room = Room.create(kind="round", owner_scope="tenant-a", base=rooms, room_id="room-turn2")
    mine_key, person_key = participant_key("agent", "tenant-a"), participant_key("cli", "tenant-a")
    mine = room.join(display="Nobel", scope_id="tenant-a",
                     peer_id=derive_peer_id(mine_key, "room-turn2"), participant_key=mine_key)
    room.join(display="Alice", scope_id="tenant-a",
              peer_id=derive_peer_id(person_key, "room-turn2"), participant_key=person_key)
    ana, iris, ana_pair, _ = _household(room)
    _presented(room, ana, ana_pair, {"kind": "say", "body": {"text": "hello everyone"}})

    class _Waker:
        from vaf.core.agent import Agent as _Real
        collect_room_wake = _Real.collect_room_wake
        _room_unattended_report = _Real._room_unattended_report

        def __init__(self):
            self._current_user_scope_id = "tenant-a"
            self._current_username = "Alice"
            self._room_reply_streak = {}

    wake = _Waker().collect_room_wake(scopes=["tenant-a"])
    assert wake is not None and wake["peer_id"] == mine.peer_id
    lines = [line for line in wake["prompt"].splitlines() if line.startswith("- ")]

    def _tags(name):
        return next((line for line in lines if line.startswith(f"- {name} [")), "")

    assert "Iris's user" in _tags("Ana"), "the guest household is not named"
    assert "agent" in _tags("Iris")
    assert "YOUR USER" in _tags("Alice") and "YOUR USER" not in _tags("Ana")


def test_the_terminal_roster_says_how_it_knows(rooms, monkeypatch):
    """MUTATION: drop `proof` from the row.

    "derived" and "attested" are two different kinds of evidence - one needs the
    account's secret and works only here, the other needs the transcript and works
    anywhere - and a foreign agent reading the roster is entitled to know which.
    """
    from vaf.cli.cmd import a2a as a2a_cmd

    monkeypatch.setattr(a2a_cmd, "_key", lambda room_id="": participant_key("cli", a2a_cmd._scope()))
    room = Room.create(kind="round", owner_scope=a2a_cmd._scope(), base=rooms, room_id="room-how")
    owner = a2a_cmd._scope()
    room.join(display="Nobel", scope_id=owner,
              peer_id=derive_peer_id(participant_key("agent", owner), "room-how"),
              participant_key=participant_key("agent", owner))
    room.join(display="Me", scope_id=owner,
              peer_id=derive_peer_id(participant_key("cli", owner), "room-how"),
              participant_key=participant_key("cli", owner))
    _household(room)
    room.join(display="Codex", scope_id=None, peer_id="p-codex")

    result = CliRunner().invoke(a2a_cmd.app, ["members", "room-how"])
    rows = {json.loads(line)["display"]: json.loads(line)
            for line in result.stdout.strip().splitlines() if line.strip()}
    assert rows["Nobel"]["proof"] == "derived"
    assert rows["Iris"]["proof"] == "attested" and rows["Iris"]["kind"] == "agent"
    assert rows["Ana"]["proof"] == "attested" and rows["Ana"]["partner_display"] == "Iris"
    assert rows["Codex"]["proof"] == "" and rows["Codex"]["kind"] == "unknown"


def test_a_roster_read_over_the_wire_folds_the_household_off_the_transcript(rooms, monkeypatch):
    """MUTATION: print `unknown` for everybody on the remote lane, the way it was.

    A reader on the wire has frames and no accounts, so derivation is impossible
    there - which was the whole reason the answer had to be in the transcript. The
    SAME fold the host runs, so the two rosters cannot disagree about a pair.
    """
    from vaf.cli.cmd import a2a as a2a_cmd

    room = Room.create(kind="round", owner_scope="scope-a", base=rooms, room_id="room-wire")
    ana, iris, _, _ = _household(room)
    frames = room.store.frames()

    monkeypatch.setattr(a2a_cmd, "_open_local", lambda room_id: None)
    monkeypatch.setattr(a2a_cmd, "_remote_record", lambda room_id: {"peer": "p-me"})
    monkeypatch.setattr(a2a_cmd, "_remote_read_frames", lambda room_id, record: frames)

    result = CliRunner().invoke(a2a_cmd.app, ["members", "room-wire"])
    rows = {json.loads(line)["display"]: json.loads(line)
            for line in result.stdout.strip().splitlines() if line.strip()}
    assert rows["Iris"]["kind"] == "agent" and rows["Iris"]["partner"] == ana.peer_id
    assert rows["Iris"]["proof"] == "attested" and rows["Iris"]["remote"] is True
    assert rows["Ana"]["kind"] == "human" and rows["Ana"]["partner_display"] == "Iris"


def test_the_fold_is_on_the_facade_for_a_reader_with_frames_and_no_store():
    """The only form the answer takes off the host, exported the way the other frame
    folds are - an embedder reading a room over the wire has exactly this input."""
    import vaf

    assert vaf.fold_room_owners is fold_owners
    assert "fold_room_owners" in vaf.__all__
    doc = (ROOT / "docs" / "EMBEDDING.md").read_text(encoding="utf-8")
    assert "fold_room_owners" in doc
