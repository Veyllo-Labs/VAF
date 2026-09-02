# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The conformance list, run against TWO implementations that share no code.

VAF driven against VAF proves the code agrees with itself. It cannot tell you whether
the protocol is implementable, because every rule it exercises is enforced by the same
lines under test. So every check here is parametrised over two adapters: VAF's own
modules, and `examples/10_a2a_reference_peer.py`, which is written from the protocol
document and imports nothing from `vaf`.

A rule only one of them keeps fails here rather than becoming a footnote. That import
ban is what turns a self-test into a proof, so it is asserted first and by itself.

The list is C1-C11 from docs/agents/A2A_PROTOCOL.md. The items about a STORE (writing
only into your own lane, renewing a lease) and the one about tools are properties of an
implementation's storage and dispatch rather than of a frame receiver, and they are
checked where they live - named at the bottom of this file so the gap is deliberate
rather than assumed.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "examples" / "10_a2a_reference_peer.py"


def _load_reference():
    spec = importlib.util.spec_from_file_location("a2a_reference_peer", REFERENCE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["a2a_reference_peer"] = module
    spec.loader.exec_module(module)
    return module


# ── the property that makes this a proof ───────────────────────────────────

@pytest.mark.parametrize("path", [REFERENCE,
                                  ROOT / "examples" / "12_a2a_wire_peer.py"],
                         ids=["rules", "transport"])
def test_the_reference_peer_knows_nothing_about_vaf(path):
    """MUTATION: import anything from vaf in the reference peer or the wire peer.

    The moment one does, both sides of every check below run the same lines, and the
    suite goes from proving the protocol is implementable to proving VAF agrees with
    itself - which it would do whatever the protocol said. The wire peer is under the
    same ban for a second reason: it is DOWNLOADED and run on machines that have no
    VAF to import, so a vaf import would not merely weaken the proof, it would break
    every guest.
    """
    source = path.read_text(encoding="utf-8")
    body = "\n".join(line for line in source.split("\n")
                     if not line.lstrip().startswith("#"))
    for shape in ("import vaf", "from vaf", "vaf.core"):
        assert shape not in body, f"{path.name} reached into VAF: {shape}"


# ── the two implementations, behind one interface ──────────────────────────

class _VafPeer:
    """VAF's own modules, presented the way the reference peer presents itself."""
    name = "vaf"

    def __init__(self):
        from vaf.core.a2a import frame as frame_mod
        from vaf.core.a2a import room as room_mod
        self._frame, self._room = frame_mod, room_mod
        self.KINDS = frame_mod.KINDS
        self.REPORT_STATUSES = frame_mod.REPORT_STATUSES
        self.CAPABILITIES = room_mod.CAPABILITIES
        self.WIRE_KEYS = frame_mod.WIRE_KEYS
        self.VERSION = frame_mod.VERSION

    def screen(self, payload, understood=()):
        self._frame.screen_inbound(payload, understood=understood)

    def sort_key(self, frame):
        parsed = self._frame.Frame.from_dict(frame, enforce_requirements=False)
        return self._frame.canonical_sort_key(parsed)

    def relay(self, frame):
        return self._frame.Frame.from_dict(frame, enforce_requirements=False).to_dict()

    def is_actionable(self, frame):
        return str(frame.get("kind") or "") in self.KINDS

    def may_emit(self, role, kind, room_kind="round"):
        if room_kind == "round" and kind == "directive":
            return False
        if kind not in self.KINDS:
            return True
        return kind in self.CAPABILITIES.get(role, frozenset())


class _ReferencePeer:
    name = "reference"

    def __init__(self):
        self._m = _load_reference()
        self.KINDS = self._m.KINDS
        self.REPORT_STATUSES = self._m.REPORT_STATUSES
        self.CAPABILITIES = self._m.CAPABILITIES
        self.WIRE_KEYS = self._m.WIRE_KEYS
        self.VERSION = self._m.VERSION

    def screen(self, payload, understood=()):
        self._m.screen(payload, understood=understood)

    def sort_key(self, frame):
        return self._m.sort_key(frame)

    def relay(self, frame):
        return self._m.relay(frame)

    def is_actionable(self, frame):
        return self._m.is_actionable(frame)

    def may_emit(self, role, kind, room_kind="round"):
        return self._m.may_emit(role, kind, room_kind)


@pytest.fixture(params=[_VafPeer, _ReferencePeer], ids=["vaf", "reference"])
def peer(request):
    return request.param()


def _frame(**over):
    base = {"v": 1, "id": "f-1", "room": "r", "from": "p-a", "seq": 1, "lamport": 1,
            "ts": 1765000000.0, "role": "peer", "kind": "say", "to": {"room": True},
            "body": {"text": "hello"}}
    base.update(over)
    return base


# ── C1: the vocabulary is the same on both sides ───────────────────────────

def test_c1_both_agree_on_the_vocabulary(peer):
    reference, vaf = _ReferencePeer(), _VafPeer()
    assert set(reference.KINDS) == set(vaf.KINDS)
    assert set(reference.REPORT_STATUSES) == set(vaf.REPORT_STATUSES)
    assert set(reference.WIRE_KEYS) == set(vaf.WIRE_KEYS)
    assert reference.VERSION == vaf.VERSION
    assert {r: set(c) for r, c in reference.CAPABILITIES.items()} == \
           {r: set(c) for r, c in vaf.CAPABILITIES.items()}


# ── C3: an unknown field survives a relay ──────────────────────────────────

def test_c3_an_unknown_top_level_field_is_carried_through(peer):
    """MUTATION: filter a relayed frame to the known keys.

    Correct at a storage boundary, fatal at a relay one. The field a later version adds
    is exactly the field today's peer does not recognise, and dropping it means a room
    quietly loses information as it passes through the oldest participant.
    """
    carried = peer.relay(_frame(priority="high", mood={"tone": "curious"}))

    assert carried["priority"] == "high"
    assert carried["mood"] == {"tone": "curious"}


def test_c3_ext_is_the_only_region_that_may_be_ignored(peer):
    carried = peer.relay(_frame(ext={"vendor.thing": 1}))
    assert carried["ext"] == {"vendor.thing": 1}


# ── C4 and C5: the door ────────────────────────────────────────────────────

def test_c5_another_major_version_is_refused(peer):
    with pytest.raises(Exception):
        peer.screen(_frame(v=2))
    with pytest.raises(Exception):
        peer.screen(_frame(v="not a number"))


def test_c4_an_incomprehensible_requirement_is_refused(peer):
    with pytest.raises(Exception):
        peer.screen(_frame(must_understand=["priority"]))


def test_c4_a_requirement_the_peer_does_understand_passes(peer):
    peer.screen(_frame(must_understand=["priority"]), understood=["priority"])
    peer.screen(_frame(must_understand=["body", "kind"]))


def test_c4_a_frame_with_no_requirements_passes(peer):
    peer.screen(_frame())


# ── C2 and C7: ordering ────────────────────────────────────────────────────

def test_c7_the_order_is_lamport_then_sender_then_seq(peer):
    """MUTATION: sort by ts.

    The clocks of two machines in one room do not agree, so a reader that sorted by the
    advisory timestamp would see a different conversation from everybody else - and
    nothing would say so, because every frame is present in both.
    """
    frames = [
        _frame(id="c", **{"from": "p-b"}, seq=1, lamport=2, ts=1.0),
        _frame(id="a", **{"from": "p-a"}, seq=1, lamport=1, ts=9.0),
        _frame(id="b", **{"from": "p-a"}, seq=2, lamport=2, ts=5.0),
    ]
    ordered = [f["id"] for f in sorted(frames, key=peer.sort_key)]
    assert ordered == ["a", "b", "c"]


def test_c7_a_tie_on_lamport_is_broken_the_same_way_by_both(peer):
    left = _frame(id="l", **{"from": "p-a"}, seq=3, lamport=7)
    right = _frame(id="r", **{"from": "p-b"}, seq=1, lamport=7)
    assert peer.sort_key(left) < peer.sort_key(right)


def test_c7_identical_timestamps_do_not_disturb_the_order(peer):
    frames = [_frame(id=str(n), seq=n + 1, lamport=10 - n, ts=1765000000.0)
              for n in range(4)]
    ordered = [f["id"] for f in sorted(frames, key=peer.sort_key)]
    assert ordered == ["3", "2", "1", "0"]


def test_c1_seq_and_lamport_are_one_based_in_both(peer):
    """MUTATION: count from zero.

    Found by this file: the two implementations disagreed, and the DOCUMENT was the one
    that was wrong - it said `seq` started at 0 while every VAF frame starts at 1 and a
    zero is refused as malformed. A stranger building from that sentence would have
    emitted frames that were rejected, and the rejection would have arrived from a
    machine they could not inspect. That is precisely the failure a second
    implementation exists to catch before anybody ships it.
    """
    reference = _ReferencePeer()._m

    reference.validate(_frame())
    for bad in (0, -1):
        with pytest.raises(Exception):
            reference.validate(_frame(seq=bad))
        with pytest.raises(Exception):
            reference.validate(_frame(lamport=bad))

    from vaf.core.a2a.frame import Frame, MalformedFrame
    Frame.from_dict(_frame(), enforce_requirements=False)
    for bad in (0, -1):
        with pytest.raises(MalformedFrame):
            Frame.from_dict(_frame(seq=bad), enforce_requirements=False)
        with pytest.raises(MalformedFrame):
            Frame.from_dict(_frame(lamport=bad), enforce_requirements=False)


def test_c1_a_frame_missing_a_required_field_is_refused_by_both():
    reference = _ReferencePeer()._m
    from vaf.core.a2a.frame import Frame, MalformedFrame

    for field in reference.REQUIRED:
        broken = {k: v for k, v in _frame().items() if k != field}
        with pytest.raises(Exception):
            reference.validate(broken)
        with pytest.raises(MalformedFrame):
            Frame.from_dict(broken, enforce_requirements=False)


# ── C6: deduplication ──────────────────────────────────────────────────────

def test_c6_duplicates_are_dropped_on_the_id_and_nothing_else():
    """At-least-once is the promise, so a receiver deduplicates - and two frames that
    differ only in `id` are two frames, not one said twice."""
    reference = _ReferencePeer()._m

    twice = [_frame(id="same"), _frame(id="same", lamport=99), _frame(id="other")]
    assert [f["id"] for f in reference.dedupe(twice)] == ["same", "other"]


def test_a_gap_in_a_senders_sequence_is_detectable():
    """Per-sender FIFO is gapless, so holding 5 and 7 IS knowing 6 is missing."""
    reference = _ReferencePeer()._m

    assert reference.gaps([0, 1, 2]) == []
    assert reference.gaps([5, 7]) == [6]
    assert reference.gaps([]) == []


# ── C2: an unknown kind ────────────────────────────────────────────────────

def test_c2_an_unknown_kind_is_not_acted_on_and_not_thrown_away(peer):
    """MUTATION: drop it.

    Dropping tears the lamport chain for every later reader, and the tear is invisible:
    the frames that remain are all valid.
    """
    odd = _frame(kind="telemetry", body={"cpu": 12})

    assert peer.is_actionable(odd) is False
    assert peer.relay(odd)["kind"] == "telemetry"
    assert peer.relay(odd)["body"] == {"cpu": 12}
    peer.screen(odd)                       # and it is not refused at the door either


# ── C10: nobody commands in a round ────────────────────────────────────────

def test_c10_a_directive_is_never_obeyed_in_a_round(peer):
    for role in ("leader", "worker", "peer"):
        assert peer.may_emit(role, "directive", "round") is False


def test_the_capability_table_is_enforced_the_same_way_by_both(peer):
    for role, allowed in peer.CAPABILITIES.items():
        for kind in peer.KINDS:
            expected = kind in allowed
            assert peer.may_emit(role, kind, "chain") is expected, (role, kind)


def test_both_implementations_answer_identically_across_the_whole_matrix():
    """The strongest form of the whole file: not "each is self-consistent" but "they
    give the SAME answer", over every role and every kind in both sorts of room."""
    reference, vaf = _ReferencePeer(), _VafPeer()

    for room_kind in ("round", "chain"):
        for role in sorted(vaf.CAPABILITIES):
            for kind in sorted(vaf.KINDS) + ["telemetry"]:
                assert reference.may_emit(role, kind, room_kind) == \
                       vaf.may_emit(role, kind, room_kind), (room_kind, role, kind)


# ── C8, C9, C11, C12, C13, C14: checked where they live, named here ────────

def test_the_items_this_file_does_not_cover_are_covered_elsewhere():
    """C8 (write only into your own lane), C9 (renew the lease), C11 (no tool through
    the room surface), C12 (composing twice changes nothing) and C13/C14 (what a
    signature promises and what a verdict may never do) are properties of a
    STORE and of a HOST, not of a frame receiver, so a reference peer that is neither
    cannot demonstrate them. They are asserted in their own files, and this test fails
    if those files disappear - which is the difference between a named gap and one
    nobody noticed.
    """
    for name in ("test_a2a_store.py", "test_a2a_room.py", "test_a2a_compose.py",
                 "test_a2a_signed_frames.py"):
        assert (ROOT / "tests" / name).exists(), \
            f"{name} carried C8/C9/C11/C12/C13/C14 and is gone"

    room_tests = (ROOT / "tests" / "test_a2a_room.py").read_text(encoding="utf-8")
    assert "execute_tool" in room_tests, "the C11 source guard is no longer in place"

    compose_tests = (ROOT / "tests" / "test_a2a_compose.py").read_text(encoding="utf-8")
    assert "test_compose_is_a_fixed_point" in compose_tests, \
        "the C12 fixed-point guard is no longer in place"
    assert "test_ingest_stores_exactly_what_compose_promised" in compose_tests, \
        "the C12 promise guard is no longer in place"

    signed_tests = (ROOT / "tests" / "test_a2a_signed_frames.py").read_text(encoding="utf-8")
    assert "test_a_signature_over_something_else_is_refused" in signed_tests, \
        "the C13 guard is no longer in place"
    assert "test_a_verdict_never_raises_and_never_drops_a_frame" in signed_tests, \
        "the C14 guard is no longer in place"
