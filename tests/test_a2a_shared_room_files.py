# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The one folder several accounts share, and the invariant it has to pass.

A room's shared folder lives in the tree of the account that opened it. For every
other member that path looks exactly like somebody else's data - which it is, and
which is why the file jail's hard cross-account invariant denies it before any
allow-list is consulted. Admitting the members of a shared room is therefore a
clause INSIDE that invariant, not a root beside it.

What is pinned here: the exception reaches exactly the rooms an account was admitted
to, it does not widen anything else by a single path, and an account that is in no
shared room is affected in no way at all.
"""
from pathlib import Path

import pytest

import vaf.core.a2a.store as store_mod
import vaf.tools.filesystem as fs
from vaf.core.a2a.room import Room, derive_peer_id, participant_key


@pytest.fixture()
def rooms(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path)
    fs._shared_room_roots_cache.clear()
    yield tmp_path
    fs._shared_room_roots_cache.clear()


# Real scope shapes, because the hard invariant only fires on a path segment that
# looks like one: `uid8` is the first eight characters of the account id with the
# dashes stripped, and a made-up name like "tenant-a" never produces eight hex
# digits - so a synthetic id would walk past the very rule under test.
OWNER = "aaaaaaaa-1111-4222-8333-444444444444"
GUEST = "bbbbbbbb-1111-4222-8333-444444444444"
OUTSIDER = "cccccccc-1111-4222-8333-444444444444"


def _shared_room(base, owner=OWNER, guest=GUEST, room_id="room-shared"):
    room = Room.create(kind="round", owner_scope=owner, base=base, room_id=room_id,
                       multi_scope=True, tenants=[guest])
    room.join(display="Nobel", scope_id=owner,
              peer_id=derive_peer_id(participant_key("agent", owner), room_id))
    room.join(display="Iris", scope_id=guest,
              peer_id=derive_peer_id(participant_key("agent", guest), room_id))
    return room


def test_a_member_of_a_shared_room_reaches_its_folder(rooms, monkeypatch, tmp_path):
    """MUTATION: add the folder to allowed_roots and stop there.

    The hard invariant returns BEFORE the allow-list is read, so a root alone changes
    nothing: the folder sits under `VAF_Projects/<the owner's uid8>/`, and to any other
    account that is the shape of somebody else's data. The exception has to be inside
    the invariant, and this is the test that tells the two apart.
    """
    room = _shared_room(rooms)
    folder = room.workspace_dir(create=True)
    assert folder is not None

    jail = fs.compute_user_jail(GUEST, None, mode="write")
    assert jail["is_admin"] is False
    assert folder in jail["shared_roots"], "the room the account was admitted to"
    assert folder in jail["allowed_roots"], "and it has to pass the positive list too"

    token = fs.set_librarian_scope(jail)
    try:
        assert fs._librarian_jail_ok(folder / "notes.md") is True
        # Everything else in the owner's tree stays exactly as closed as before.
        assert fs._librarian_jail_ok(folder.parent / "some-other-room" / "x.md") is False
        assert fs._librarian_jail_ok(folder.parent.parent / "ffffffff" / "x.md") is False
    finally:
        fs.reset_librarian_scope(token)


def test_an_account_in_no_shared_room_gains_nothing(rooms):
    """MUTATION: hand out every room's folder, or every folder of a room on disk.

    A room that belongs to one account keeps its folder inside that account's own tree,
    where the ordinary jail already reaches it - there is nothing to grant. An account
    that was never admitted anywhere must come out of this exactly as it went in.
    """
    private = Room.create(kind="round", owner_scope=OWNER, base=rooms,
                          room_id="room-private")
    # The owner is really IN it - a room nobody joined would come out empty whatever
    # the rule says, and prove nothing.
    private.join(display="Nobel", scope_id=OWNER,
                 peer_id=derive_peer_id(participant_key("agent", OWNER), "room-private"))
    _shared_room(rooms, room_id="room-shared-2")

    outsider = fs.compute_user_jail(OUTSIDER, None, mode="write")
    assert outsider["shared_roots"] == [], "an account nobody admitted was let in"

    owner = fs.compute_user_jail(OWNER, None, mode="write")
    assert [r for r in owner["shared_roots"] if "room-private" in str(r)] == [], (
        "a room with one account needs no exception at all")
    assert any("room-shared-2" in str(r) for r in owner["shared_roots"]), (
        "the shared room it opened is still shared")


def test_the_lookup_is_not_repeated_on_every_tool_call(rooms, monkeypatch):
    """MUTATION: drop the cache.

    Answering this means opening every room on the machine and decrypting each
    manifest, and it is asked once per tool call. The window is short and deliberate:
    an account removed from a shared room keeps the folder for at most that long, which
    is stated rather than hidden.
    """
    _shared_room(rooms)
    calls = []
    real = fs._shared_room_roots.__wrapped__ if hasattr(fs._shared_room_roots, "__wrapped__") else None
    assert real is None, "the cache is hand-rolled on purpose; see the TTL constant"

    import vaf.core.a2a.room as room_mod
    original = room_mod.joined_rooms

    def counting(key, base=None):
        calls.append(key)
        return original(key, base=base)

    monkeypatch.setattr(room_mod, "joined_rooms", counting)
    fs._shared_room_roots_cache.clear()
    for _ in range(5):
        fs.compute_user_jail(GUEST, None, mode="write")
    assert len(calls) == 2, (
        f"expected one scan per lane, cached after that; got {len(calls)}")


def test_the_download_lane_lets_a_member_fetch_what_it_put_there():
    """MUTATION: leave /api/file keying on the account prefix alone.

    Then a member of a shared room can WRITE a file into the folder with its own
    tools and be refused when it clicks the link to it - the two halves of the same
    permission answered by two different rules. The file lane keeps its fail-closed
    shape; the exception is the same one the tools got, taken from the same function
    so the two cannot drift.
    """
    from pathlib import Path as _P
    src = (_P(__file__).resolve().parents[1] / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    block = src.split('_re_iso.fullmatch(r"[0-9a-f]{8}", _first_seg)', 1)[1][:1400]
    assert "_shared_room_roots" in block, (
        "the download lane cannot see a room this account was admitted to")
    assert "is_relative_to" in block, "membership must be checked against the folder"
    assert block.index("_allowed = _is_admin") < block.index("_shared_room_roots"), (
        "the ordinary ownership answer has to be tried first")
    assert "_allowed = False" in block, "the lane must still fail closed on an error"
