# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Rooms appear in the session list. They are not sessions, and nothing may treat them
as one.

The danger is specific and measured: Session.save rewrites the ENTIRE message list on
every save. With one writer that is fine; with N peers it reproduces exactly the lost
update the room store was built to avoid - which is why a room is a directory of
write-once files and not a session in the first place.

So the rows share a list with sessions and share nothing else: a distinct kind, an id no
session loader would accept, and a guard here that says both out loud.
"""
from pathlib import Path

import pytest

import vaf.core.a2a.store as store_mod
from vaf.core.a2a.room import Room, derive_peer_id, participant_key
from vaf.core.session import SessionManager, _room_rows

ROOT = Path(__file__).resolve().parents[1]
SCOPE = "scope-owner"


@pytest.fixture()
def rooms(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path)
    room = Room.create(kind="round", owner_scope=SCOPE, base=tmp_path,
                       room_id="room-visible", topic="Deploy talk")
    key = participant_key("agent", SCOPE)
    room.join(display="VAF", scope_id=SCOPE, peer_id=derive_peer_id(key, "room-visible"))
    other = room.join(display="Codex", scope_id=None, peer_id="p-codex")
    room.say(other, "anyone there?")
    return room


# ── they show up ───────────────────────────────────────────────────────────

def test_a_joined_room_appears_with_what_a_surface_needs(rooms):
    rows = _room_rows(SCOPE)

    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "room"
    assert row["room_id"] == "room-visible"
    assert row["name"] == "Deploy talk"
    assert row["unread"] == 1
    assert row["members"] == 2
    assert row["closed"] is False


def test_both_local_lanes_are_looked_up_but_a_room_is_listed_once(tmp_path, monkeypatch):
    """A person does not think of "my agent's rooms" and "the rooms I joined from a
    terminal" as two lists, even though they are two participants by design."""
    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path)
    room = Room.create(kind="round", owner_scope=SCOPE, base=tmp_path, room_id="room-both")
    for lane in ("agent", "cli"):
        room.join(display=lane, scope_id=SCOPE,
                  peer_id=derive_peer_id(participant_key(lane, SCOPE), "room-both"))

    rows = _room_rows(SCOPE)
    assert [r["room_id"] for r in rows] == ["room-both"]


def test_rooms_come_before_conversations(rooms, monkeypatch):
    """MUTATION: append the rows instead of prepending them.

    The order lives in list_ui and nowhere else. Two surfaces sorting for themselves is
    exactly the divergence this function's docstring says already happened once.
    """
    manager = SessionManager()
    monkeypatch.setattr(manager, "list", lambda **kw: [
        {"id": "chat-1", "name": "an ordinary chat", "metadata": {}},
    ])

    listed = manager.list_ui(user_scope_id=SCOPE)
    assert listed[0]["kind"] == "room"
    assert listed[1]["id"] == "chat-1"


def test_a_damaged_room_never_breaks_the_sidebar(tmp_path, monkeypatch):
    """A chat list that cannot draw because one room directory is broken is a worse
    failure than a missing row."""
    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path)
    import vaf.core.a2a.room as room_mod
    monkeypatch.setattr(room_mod, "joined_rooms",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("disk")))

    assert _room_rows(SCOPE) == []


# ── and they are not sessions ──────────────────────────────────────────────

def test_a_room_row_carries_no_id_a_session_loader_would_take(rooms):
    """MUTATION: use the bare room id as the row id.

    A surface that passes the row's id to load_session would then open - and later SAVE
    - something that is not a session. Session.save rewrites the whole message list,
    which is the lost update the room store exists to avoid, arriving through the front
    door.
    """
    row = _room_rows(SCOPE)[0]

    assert row["id"].startswith("room:")
    assert row["id"] != row["room_id"]


def test_loading_a_room_row_as_a_session_refuses_loudly(rooms):
    """It RAISES rather than returning nothing, which is the stronger of the two: a
    surface that mistakenly passes a room row to the session loader finds out at once
    instead of rendering an empty conversation and calling it a room."""
    manager = SessionManager()
    row = _room_rows(SCOPE)[0]

    with pytest.raises(FileNotFoundError):
        manager.load(row["id"])
    with pytest.raises(FileNotFoundError):
        manager.load(row["room_id"])


def test_the_room_prefix_is_not_a_session_prefix():
    """MUTATION: add "room" to CHANNEL_SESSION_PREFIXES to "hide" them.

    That tuple decides which sessions a surface skips. A room is not a session being
    hidden; it is a different thing being shown, and conflating the two would make the
    rows vanish the moment somebody tidied up.
    """
    from vaf.core.tool_dispatch import CHANNEL_SESSION_PREFIXES

    assert not any(str(prefix).startswith("room") for prefix in CHANNEL_SESSION_PREFIXES)


def test_nothing_saves_a_room_row(rooms):
    """The one operation that would do real damage, refused by shape rather than by
    care: there is no Session to hand to save() in the first place."""
    manager = SessionManager()
    row = _room_rows(SCOPE)[0]

    with pytest.raises(FileNotFoundError):
        manager.load(row["id"])
    with pytest.raises(Exception):
        manager.save(row)          # a dict is not a Session, and must not become one
