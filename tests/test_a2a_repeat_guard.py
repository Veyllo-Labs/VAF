# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Opening the same room twice by accident.

Measured live rather than imagined: an agent ran room_open, room_send, room_send and
room_invite, then ran the identical sequence again twenty-one seconds later inside the
SAME task. Two rooms, one request. It then told its user the cause was "a double
submission of your request" - the queue log shows exactly one queued input, so the
explanation was invented and the second room was its own doing.

What this guards is a REPEAT and deliberately not uniqueness. Two rooms may share a
topic and often should; a weekly standup is not a mistake. What is almost never meant
is the same participant opening the same topic twice within minutes.
"""
from pathlib import Path

import pytest

import vaf.core.a2a.store as store_mod
from vaf.core.a2a.room import (JUST_OPENED_S, Room, derive_peer_id, just_opened,
                               participant_key)


@pytest.fixture()
def rooms(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path)
    return tmp_path


def _opened(base, key, topic, room_id):
    room = Room.create(kind="round", owner_scope="scope-a", base=base,
                       topic=topic, room_id=room_id)
    room.join(display="Wer", scope_id="scope-a",
              peer_id=derive_peer_id(key, room_id), participant_key=key)
    return room


def test_a_room_opened_a_moment_ago_is_found(rooms):
    key = participant_key("agent", "scope-a")
    room = _opened(rooms, key, "Signaturtest", "room-one")
    assert just_opened(key, "Signaturtest", base=rooms) == room.room_id


def test_the_match_ignores_case_and_surrounding_space(rooms):
    """An agent that repeats itself rarely repeats itself byte for byte."""
    key = participant_key("agent", "scope-a")
    _opened(rooms, key, "Signaturtest", "room-one")
    assert just_opened(key, "  signaturtest ", base=rooms) == "room-one"


def test_an_older_room_is_not_a_repeat(rooms):
    """A weekly standup under the same topic is a different room, not a mistake.
    The window is what makes this a repeat detector instead of a uniqueness rule."""
    key = participant_key("agent", "scope-a")
    _opened(rooms, key, "Standup", "room-old")
    later = float(Room.open("room-old", base=rooms).manifest["created_at"]) + JUST_OPENED_S + 1
    assert just_opened(key, "Standup", base=rooms, now=later) is None


def test_a_closed_room_is_not_a_repeat(rooms):
    """Reopening a topic after closing it is a new conversation."""
    key = participant_key("agent", "scope-a")
    room = _opened(rooms, key, "Erledigt", "room-shut")
    room.close(room.identity_for(key), reason="fertig")
    assert just_opened(key, "Erledigt", base=rooms) is None


def test_another_topic_is_not_a_repeat(rooms):
    key = participant_key("agent", "scope-a")
    _opened(rooms, key, "Signaturtest", "room-one")
    assert just_opened(key, "Etwas anderes", base=rooms) is None


def test_somebody_elses_room_is_not_a_repeat_and_is_not_leaked(rooms):
    """It asks only about rooms this participant is IN, through the same walk
    joined_rooms does, so it can neither block nor reveal another tenant's room."""
    mine = participant_key("agent", "scope-a")
    theirs = participant_key("agent", "scope-b")
    _opened(rooms, theirs, "Signaturtest", "room-theirs")
    assert just_opened(mine, "Signaturtest", base=rooms) is None


def test_the_agents_tool_refuses_and_names_the_room(rooms, monkeypatch):
    """The refusal has to be actionable: an agent that is merely told "no" opens one
    with a different topic, which is the same duplicate wearing a new name."""
    from vaf.tools.room_tools import RoomOpenTool

    monkeypatch.setattr("vaf.tools.room_tools._announce", lambda *a, **k: None)
    tool = RoomOpenTool()
    first = tool.run(topic="Signaturtest", user_scope_id="scope-a")
    room_id = first.split("'")[1]

    again = tool.run(topic="Signaturtest", user_scope_id="scope-a")
    assert room_id in again
    assert "already opened" in again
    assert "room_send" in again, "the refusal names what to do instead"
    assert len(store_mod.list_rooms(rooms)) == 1, "no second room was created"
