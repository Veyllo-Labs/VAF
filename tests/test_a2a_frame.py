# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The five forward-compatibility rules of the A2A frame, and the ordering.

Every test names the mutation that must turn it red. A rule that survives its own
mutation is decoration, and these five ARE the protocol: a third-party peer agrees
with this module or it is not a peer.
"""
import pytest

from vaf.core.a2a.frame import (
    KINDS,
    REPORT_STATUSES,
    VERSION,
    Frame,
    MalformedFrame,
    UnsupportedRequirement,
    UnsupportedVersion,
    canonical_sort_key,
    in_canonical_order,
    next_lamport,
)


def _wire(**overrides):
    """A complete, valid frame as it arrives on the wire."""
    data = {
        "v": VERSION,
        "id": "f-1",
        "room": "r-1",
        "seq": 1,
        "lamport": 1,
        "ts": 1765000000.0,
        "from": "p1",
        "role": "peer",
        "kind": "say",
        "to": {"room": True},
        "body": {"text": "hello"},
    }
    data.update(overrides)
    return data


# ── rule 1: an unknown top-level field is preserved ─────────────────────────

def test_an_unknown_top_level_field_survives_the_round_trip():
    """MUTATION: filter from_dict to known fields, the way session.Message does.

    That filter is right at a storage boundary and fatal at a relay boundary: the
    unknown key is a newer peer's meaning passing through an older one.
    """
    wire = _wire(priority="high", trace={"span": "abc"})

    out = Frame.from_dict(wire).to_dict()

    assert out["priority"] == "high"
    assert out["trace"] == {"span": "abc"}


def test_the_unknown_fields_are_reported_rather_than_hidden():
    """MUTATION: return an empty tuple from unknown_fields.

    A renderer that cannot show "a newer peer said more than we understand" will
    silently present an incomplete frame as a complete one.
    """
    frame = Frame.from_dict(_wire(priority="high", trace={}))
    assert frame.unknown_fields == ("priority", "trace")
    assert Frame.from_dict(_wire()).unknown_fields == ()


def test_an_unknown_ext_key_is_carried_even_though_it_may_be_ignored():
    """ext is the only namespace a peer may ignore - ignoring is not dropping."""
    frame = Frame.from_dict(_wire(ext={"progress": 0.5}))
    assert frame.ext == {"progress": 0.5}
    assert frame.to_dict()["ext"] == {"progress": 0.5}


def test_an_overwritten_sender_beats_the_source(  ):
    """MUTATION: let _raw win in to_dict instead of the parsed fields.

    Room.ingest overwrites `from` and `role` with the admitted peer's values. If the
    source dict won, a forged `from` would be written back into the log unchanged,
    and preserving unknown fields would have quietly reintroduced spoofing.
    """
    frame = Frame.from_dict(_wire(**{"from": "liar", "role": "leader"}))
    frame.sender = "p7"
    frame.role = "worker"

    out = frame.to_dict()
    assert out["from"] == "p7"
    assert out["role"] == "worker"


# ── rule 2: an unknown kind is opaque, not fatal, not dropped ───────────────

def test_an_unknown_kind_parses_and_is_marked_unactionable():
    """MUTATION: raise on an unknown kind.

    Refusing the frame would remove it from the log, and removing a frame tears the
    lamport chain for every reader after it.
    """
    frame = Frame.from_dict(_wire(kind="celebrate"))
    assert frame.kind == "celebrate"
    assert frame.kind_known is False
    assert Frame.from_dict(_wire(kind="say")).kind_known is True


def test_the_kind_vocabulary_is_the_pinned_set():
    assert "directive" in KINDS and "hire" in KINDS and "ack" in KINDS
    assert "celebrate" not in KINDS


# ── rule 4: an unknown major version means leaving ──────────────────────────

def test_a_foreign_major_version_refuses():
    """MUTATION: accept any value of v.

    Half speaking a protocol you do not know is worse than being absent.
    """
    with pytest.raises(UnsupportedVersion):
        Frame.from_dict(_wire(v=2))
    with pytest.raises(UnsupportedVersion):
        Frame.from_dict(_wire(v="banana"))


def test_a_missing_version_is_read_as_this_version():
    """An early peer that omits v is speaking v1; only a DIFFERENT major refuses."""
    wire = _wire()
    wire.pop("v")
    assert Frame.from_dict(wire).v == VERSION


# ── rule 5: must_understand refuses, and refuses FIRST ──────────────────────

def test_must_understand_refuses_a_field_this_peer_does_not_know():
    """MUTATION: ignore must_understand.

    Ignoring it means acting on a frame whose sender said acting without that field
    would be wrong.
    """
    with pytest.raises(UnsupportedRequirement) as excinfo:
        Frame.from_dict(_wire(deadline="soon", must_understand=["deadline"]))
    assert excinfo.value.missing == ("deadline",)


def test_must_understand_is_satisfied_by_a_declared_extension():
    frame = Frame.from_dict(
        _wire(deadline="soon", must_understand=["deadline"]), understood=["deadline"]
    )
    assert frame.must_understand == ("deadline",)
    assert frame.to_dict()["deadline"] == "soon"


def test_must_understand_of_a_normal_field_is_always_satisfied():
    frame = Frame.from_dict(_wire(must_understand=["reply_to", "body"]))
    assert frame.must_understand == ("reply_to", "body")


def test_the_requirement_is_checked_before_the_frame_is_validated():
    """MUTATION: move the must_understand check below the required-field loop.

    A peer that cannot honour the requirement must take NO other action, and that
    includes complaining about a frame it may be misreading for exactly that reason.
    The frame here is BOTH unsatisfiable and malformed; the requirement must win.
    """
    broken = _wire(must_understand=["deadline"])
    broken.pop("room")

    with pytest.raises(UnsupportedRequirement):
        Frame.from_dict(broken)


# ── shape ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("missing", ["id", "room", "seq", "lamport", "from", "role", "kind", "to"])
def test_every_required_field_is_required(missing):
    wire = _wire()
    wire.pop(missing)
    with pytest.raises(MalformedFrame):
        Frame.from_dict(wire)


def test_sequence_numbers_start_at_one():
    """Zero would make "the first frame" ambiguous with "no frame", and the store
    derives the next seq from the highest file name."""
    with pytest.raises(MalformedFrame):
        Frame.from_dict(_wire(seq=0))
    with pytest.raises(MalformedFrame):
        Frame.from_dict(_wire(lamport=0))


def test_new_mints_a_frame_with_an_injectable_id_and_clock():
    frame = Frame.new(
        room="r-1", sender="p1", role="peer", kind="say", seq=3, lamport=9,
        body={"text": "hi"}, frame_id="fixed", ts=42.0,
    )
    assert (frame.id, frame.ts, frame.to) == ("fixed", 42.0, {"room": True})
    assert frame.to_dict()["from"] == "p1"


def test_two_minted_frames_do_not_share_an_id():
    a = Frame.new(room="r", sender="p", role="peer", kind="say", seq=1, lamport=1)
    b = Frame.new(room="r", sender="p", role="peer", kind="say", seq=2, lamport=2)
    assert a.id != b.id


# ── addressing is a hint, not a wall ────────────────────────────────────────

def test_addressing_covers_room_peer_role_and_list():
    room_wide = Frame.from_dict(_wire(to={"room": True}))
    assert room_wide.addresses("anyone") is True

    to_peer = Frame.from_dict(_wire(to={"peer": "p3"}))
    assert to_peer.addresses("p3") is True
    assert to_peer.addresses("p4") is False

    to_role = Frame.from_dict(_wire(to={"role": "leader"}))
    assert to_role.addresses("p4", role="leader") is True
    assert to_role.addresses("p4", role="worker") is False

    to_list = Frame.from_dict(_wire(to={"peers": ["p3", "p9"]}))
    assert to_list.addresses("p9") is True
    assert to_list.addresses("p1") is False


# ── ordering: logical, never chronological ─────────────────────────────────

def _f(lamport, sender, seq, ts):
    return Frame.new(room="r", sender=sender, role="peer", kind="say",
                     seq=seq, lamport=lamport, ts=ts, frame_id=f"{sender}-{seq}")


def test_canonical_order_ignores_the_wall_clock():
    """MUTATION: sort by ts.

    Wall clocks come from other machines. Ordering by them would make clock skew a
    correctness bug, and it is the single decision that lets the cross-machine step
    ship without touching the frame.
    """
    # ts runs backwards against lamport on purpose.
    later_cause = _f(lamport=1, sender="p1", seq=1, ts=9999.0)
    earlier_effect = _f(lamport=2, sender="p1", seq=2, ts=1.0)

    assert in_canonical_order([earlier_effect, later_cause]) == [later_cause, earlier_effect]


def test_concurrent_writers_are_ordered_deterministically_and_both_survive():
    """MUTATION: drop sender from the sort key.

    Two peers writing at the same lamport is the normal case, not the exception.
    Without the tie-break, two readers can render the same room in two orders.
    """
    a = _f(lamport=5, sender="alpha", seq=1, ts=100.0)
    b = _f(lamport=5, sender="beta", seq=1, ts=50.0)

    assert in_canonical_order([b, a]) == [a, b]
    assert in_canonical_order([a, b]) == [a, b]


def test_one_senders_frames_stay_in_sequence_within_a_lamport_tie():
    first = _f(lamport=7, sender="p1", seq=1, ts=0.0)
    second = _f(lamport=7, sender="p1", seq=2, ts=0.0)
    assert in_canonical_order([second, first]) == [first, second]


def test_the_sort_key_is_exactly_lamport_sender_seq():
    frame = _f(lamport=4, sender="p2", seq=6, ts=123.0)
    assert canonical_sort_key(frame) == (4, "p2", 6)


def test_next_lamport_is_one_past_the_highest_seen():
    assert next_lamport([]) == 1
    assert next_lamport([1, 7, 3]) == 8
    assert next_lamport([9]) == 10


# ── the borrowed task vocabulary ───────────────────────────────────────────

def test_the_report_status_vocabulary_matches_the_open_standard():
    """Kept identical to the open A2A task states so a bridge stays possible. It is
    a body field, so adding it costs no change to the frame."""
    assert REPORT_STATUSES == {
        "submitted", "working", "input_required",
        "completed", "failed", "rejected", "canceled",
    }


def test_an_unknown_report_status_is_carried_like_any_unknown_value():
    """MUTATION: validate body.status against the enum at parse time.

    Validating here would refuse a frame from a newer peer, which is rule 2's
    mistake wearing a different hat. The vocabulary is closed for what a reader may
    ACT on, not for what it may carry.
    """
    frame = Frame.from_dict(_wire(kind="report", body={"status": "reviewing"}))
    assert frame.body["status"] == "reviewing"
    assert frame.body["status"] not in REPORT_STATUSES
