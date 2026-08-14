# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The room store: write-once frames, a directory that is its own counter, and
reads that take nothing away.

Each test names the mutation that must turn it red. The two load-bearing ones are
the concurrent writers (the shared-queue failure this store exists to avoid) and
the crash-continues-gaplessly test (the reason seq is not an in-memory counter).
"""
import os
import subprocess
import sys
import textwrap
import time

import pytest

from vaf.core import data_files
from vaf.core.a2a.frame import Frame
from vaf.core.a2a.store import (
    ROOM_FORMAT,
    FrameExists,
    RoomStore,
    StoreError,
    UnsafeName,
    list_rooms,
    new_peer_id,
    new_room_id,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture()
def store(tmp_path):
    s = RoomStore("room-test", base=tmp_path)
    s.create({"kind": "round", "owner_scope": "scope-a"})
    return s


def _frame(store, sender, *, seq=None, lamport=None, text="hi", kind="say", ts=0.0):
    return Frame.new(
        room=store.room_id, sender=sender, role="peer", kind=kind,
        seq=seq if seq is not None else store.next_seq(sender),
        lamport=lamport if lamport is not None else store.next_lamport(),
        body={"text": text}, ts=ts, frame_id=f"{sender}-{seq or 0}-{text}",
    )


# ── the shape that makes it lock-free ───────────────────────────────────────

def test_two_processes_writing_at_once_both_survive(tmp_path, monkeypatch):
    """MUTATION: put every peer's frames in one shared file.

    This is the whole reason the store exists. The sub-agent queue keeps all writers
    in one guarded JSON, and its own guard degrades to an UNLOCKED read-modify-write
    after five seconds - one writer's work is then lost. Separate lanes have no
    read-modify-write at all, so there is nothing to lose.

    Real OS processes, not threads: a lost update needs two address spaces to be the
    failure this test is about.

    Encryption is off in this test ALONE, and deliberately. A scratch HOME has no
    persisted machine KEK, so each fresh process bootstraps its own keyring and
    cannot read what another process wrote - an artifact of the throwaway home, not
    of this store, and one that would otherwise make the test measure the keyring
    instead of the concurrency. Encryption at rest has its own test below, which is
    where that property belongs.
    """
    monkeypatch.setattr(data_files, "encryption_enabled", lambda: False)
    RoomStore("room-race", base=tmp_path).create({"kind": "round"})

    script = textwrap.dedent(
        """
        import sys
        sys.path.insert(0, sys.argv[1])
        from vaf.core import data_files
        data_files.encryption_enabled = lambda: False
        from vaf.core.a2a.frame import Frame
        from vaf.core.a2a.store import RoomStore
        peer = sys.argv[3]
        store = RoomStore("room-race", base=sys.argv[2])
        for n in range(1, 6):
            store.append(Frame.new(room="room-race", sender=peer, role="peer",
                                   kind="say", seq=n, lamport=n,
                                   body={"text": f"{peer}-{n}"}, ts=0.0))
        """
    )
    env = dict(os.environ)
    procs = [
        subprocess.Popen([sys.executable, "-c", script, ROOT, str(tmp_path), peer],
                         env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for peer in ("alpha", "beta")
    ]
    for proc in procs:
        out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err.decode("utf-8", "replace")

    store = RoomStore("room-race", base=tmp_path)
    assert store.sequence_numbers("alpha") == [1, 2, 3, 4, 5]
    assert store.sequence_numbers("beta") == [1, 2, 3, 4, 5]
    assert len(store.frames()) == 10


def test_the_lane_is_the_authorship_record(store):
    """A frame's author is the directory it sits in, so no field can disagree with
    where the bytes actually are."""
    store.append(_frame(store, "p1", seq=1, lamport=1))
    written = list((store.log_dir / "p1").iterdir())
    assert [p.name for p in written] == ["000000000001.json"]


def test_a_frame_is_written_once_and_never_rewritten(store):
    """MUTATION: drop the exists() check in append.

    Reaching this means two writers acted as one peer. Overwriting would destroy a
    delivered message silently; refusing turns it into a visible bug.
    """
    store.append(_frame(store, "p1", seq=1, lamport=1))
    with pytest.raises(FrameExists):
        store.append(_frame(store, "p1", seq=1, lamport=2, text="clobber"))
    assert store.frames()[0].body["text"] == "hi"


# ── the directory is the counter ────────────────────────────────────────────

def test_a_restarted_writer_continues_the_sequence_without_a_gap(store):
    """MUTATION: keep seq in an in-memory counter on the store object.

    A counter in memory dies with the process. After a crash the next run would
    restart at 1 (overwriting) or carry on from a number it cannot know (a hole),
    and a file-only peer has no outbox to heal the hole from. The directory cannot
    disagree with itself, so it is the counter.
    """
    for n in (1, 2, 3):
        store.append(_frame(store, "p1", seq=n, lamport=n))

    # A brand-new store object is what a restarted process gets: no memory of the
    # three frames, only the directory they left behind.
    after_crash = RoomStore(store.room_id, base=store.root.parent)
    assert after_crash.next_seq("p1") == 4
    assert after_crash.gaps("p1") == []


def test_the_first_sequence_number_is_one(store):
    assert store.next_seq("nobody") == 1


def test_a_hole_in_the_sequence_is_reported_not_smoothed_over(store):
    """MUTATION: return [] from gaps().

    A torn write is invisible by construction: the reader never sees the temp file,
    so the frame simply is not there. The gap is the only evidence left, and a store
    that hides it reports a truncated conversation as a complete one.
    """
    store.append(_frame(store, "p1", seq=1, lamport=1))
    store.append(_frame(store, "p1", seq=3, lamport=3))

    assert store.gaps("p1") == [2]
    assert store.next_seq("p1") == 4


def test_a_crashed_write_leaves_nothing_a_reader_can_see(store):
    """A leftover temp file must not be read as a frame and must not be counted.

    Both halves matter: parsing it would surface half a message, and counting it
    would make next_seq skip a number nobody wrote.
    """
    store.append(_frame(store, "p1", seq=1, lamport=1))
    (store.lane("p1") / ".tmp-abcdef.part").write_bytes(b"half a fra")

    assert store.sequence_numbers("p1") == [1]
    assert store.next_seq("p1") == 2
    assert len(store.frames()) == 1


# ── reading takes nothing away ──────────────────────────────────────────────

def test_reading_is_not_a_pop(store):
    """MUTATION: delete the file after reading it, the way consume_result does.

    A room is N readers of the same message, each with their own position. A
    destructive read means the first reader wins and everybody else sees an empty
    conversation.
    """
    store.append(_frame(store, "p1", seq=1, lamport=1))

    first = store.frames()
    second = store.frames()

    assert len(first) == len(second) == 1
    assert first[0].id == second[0].id


def test_a_reader_carries_its_own_position(store):
    store.append(_frame(store, "p1", seq=1, lamport=1))
    store.append(_frame(store, "p1", seq=2, lamport=2))

    assert store.cursor("reader-a") == 0
    store.set_cursor("reader-a", 1)

    assert [f.lamport for f in store.read_since(store.cursor("reader-a"))] == [2]
    assert store.cursor("reader-b") == 0, "one reader's position never moves another's"


def test_one_unreadable_file_does_not_make_the_room_unreadable(store):
    store.append(_frame(store, "p1", seq=1, lamport=1))
    (store.lane("p1") / "000000000002.json").write_bytes(b"not json at all")
    store.append(_frame(store, "p1", seq=3, lamport=3))

    assert [f.seq for f in store.frames()] == [1, 3]


def test_frames_come_back_in_canonical_order(store):
    """Written out of order on purpose; the reader must not care."""
    store.append(_frame(store, "beta", seq=1, lamport=5, ts=1.0))
    store.append(_frame(store, "alpha", seq=1, lamport=5, ts=99.0))
    store.append(_frame(store, "alpha", seq=2, lamport=1, ts=50.0))

    assert [(f.lamport, f.sender) for f in store.frames()] == [
        (1, "alpha"), (5, "alpha"), (5, "beta"),
    ]


def test_next_lamport_is_one_past_the_room(store):
    assert store.next_lamport() == 1
    store.append(_frame(store, "p1", seq=1, lamport=7))
    assert store.next_lamport() == 8


# ── encrypted, atomic, owner-only: the same primitive sessions use ──────────

def test_a_frame_file_is_encrypted_at_rest(store, monkeypatch):
    """MUTATION: swap write_json_atomic for a plain write_text.

    A room transcript is a conversation; it gets the protection a conversation gets.
    Encryption is forced on here so the assertion holds regardless of the machine's
    configuration - the point under test is that the store goes through the
    encrypting primitive, not what this box happens to have enabled.
    """
    monkeypatch.setattr(data_files, "encryption_enabled", lambda: True)
    store.append(_frame(store, "p1", seq=1, lamport=1, text="secret"))

    raw = (store.lane("p1") / "000000000001.json").read_bytes()
    assert raw.startswith(data_files.FILE_MAGIC)
    assert b"secret" not in raw
    assert store.frames()[0].body["text"] == "secret"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_the_room_is_owner_only_on_disk(store):
    store.append(_frame(store, "p1", seq=1, lamport=1))

    assert (store.root.stat().st_mode & 0o777) == 0o700
    assert (store.lane("p1").stat().st_mode & 0o777) == 0o700
    assert ((store.lane("p1") / "000000000001.json").stat().st_mode & 0o777) == 0o600


# ── names arriving from outside ─────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["../escape", "a/b", "", ".", "..", "x" * 90, "-leading"])
def test_an_unsafe_identifier_is_refused_not_sanitised(bad, tmp_path):
    """MUTATION: strip the offending characters instead of raising.

    Room and peer ids arrive from outside - a foreign agent types them on a command
    line - and they become path components. Sanitising would let two different names
    collapse into one directory, which is worse than refusing: the second room would
    silently read and write the first room's transcript.
    """
    with pytest.raises(UnsafeName):
        RoomStore(bad, base=tmp_path)

    good = RoomStore("room-ok", base=tmp_path)
    good.create({"kind": "round"})
    with pytest.raises(UnsafeName):
        good.lane(bad)


def test_generated_identifiers_are_safe_by_construction(tmp_path):
    RoomStore(new_room_id(), base=tmp_path)
    RoomStore("room-ok", base=tmp_path).lane(new_peer_id())


# ── manifest and listing ────────────────────────────────────────────────────

def test_the_manifest_carries_the_format_tag_and_a_host_slot(store):
    record = store.manifest()
    assert record["format"] == ROOM_FORMAT
    assert record["room_id"] == "room-test"
    assert record["host"] == {}, "written from day one so cross-machine adds a member, not a field"


def test_a_room_is_not_created_twice(store):
    with pytest.raises(StoreError):
        store.create({"kind": "chain"})


def test_listing_finds_rooms_with_a_manifest_only(tmp_path):
    RoomStore("room-a", base=tmp_path).create({"kind": "round"})
    (tmp_path / "not-a-room").mkdir()
    assert list_rooms(tmp_path) == ["room-a"]


# ── members and tickets ─────────────────────────────────────────────────────

def test_a_member_record_is_written_by_its_own_peer(store):
    store.put_member("p1", {"display": "Alice", "mode": "assist"})
    store.put_member("p2", {"display": "Bob", "mode": "observe"})

    assert store.member("p1")["display"] == "Alice"
    assert set(store.members()) == {"p1", "p2"}
    assert store.members()["p2"]["mode"] == "observe"


def test_a_ticket_is_single_use(store):
    store.put_ticket("t1", {"room": store.room_id})
    assert store.ticket("t1")["room"] == store.room_id

    store.drop_ticket("t1")
    assert store.ticket("t1") is None


def test_a_frame_the_reader_cannot_fully_understand_stays_in_the_transcript(store):
    """MUTATION: enforce must_understand while READING a stored frame.

    Rule 5 governs whether a peer may ACT on a frame. Applying it to a reader deletes
    the frame from the room's history instead, which is rule 2's mistake wearing rule
    5's clothes: a frame removed from the log tears the lamport chain for everything
    after it, and every later reader silently sees a shorter conversation.

    Found by a surviving mutation in the hub tests, where "write first, screen after"
    stayed green because the frame it wrote had become invisible to the reader.
    """
    store.append(_frame(store, "p1", seq=1, lamport=1, text="ordinary"))
    demanding = Frame.new(
        room=store.room_id, sender="p1", role="peer", kind="say",
        seq=2, lamport=2, body={"text": "from a newer peer"},
        must_understand=["deadline"], ext={}, ts=0.0, frame_id="demanding",
    )
    payload = demanding.to_dict()
    payload["deadline"] = "soon"
    import vaf.core.data_files as _df
    _df.write_json_atomic(store.lane("p1") / "000000000002.json", payload)
    store.append(_frame(store, "p1", seq=3, lamport=3, text="after it"))

    seen = store.frames()
    assert [f.body.get("text") for f in seen] == ["ordinary", "from a newer peer", "after it"]
    assert seen[1].must_understand == ("deadline",)
    assert store.gaps("p1") == [], "the chain must be intact"


# ── every reader's position, for the engagement signal ─────────────────────

def test_cursors_reports_every_readers_position_and_when_it_moved(tmp_path):
    """MUTATION: return only the lamport, or glob the directory in a consumer.

    The cursor file's shape is the store's to know: `updated_at` is what makes an
    engagement signal possible at all (a cursor at the newest frame says a peer has
    SEEN it; the timestamp says how long ago), and a consumer that parsed the JSON
    itself would be a second copy of this format waiting to drift.
    """
    store = RoomStore("room-cursors", base=tmp_path)
    store.create({"room_id": "room-cursors", "kind": "round"})
    assert store.cursors() == {}

    before = time.time()
    store.set_cursor("p-a", 7)
    store.set_cursor("p-b", 3)

    positions = store.cursors()
    assert set(positions) == {"p-a", "p-b"}
    assert positions["p-a"]["lamport"] == 7
    assert positions["p-b"]["lamport"] == 3
    assert positions["p-a"]["updated_at"] >= before, (
        "the moment the cursor moved is missing - no engagement signal can exist")
