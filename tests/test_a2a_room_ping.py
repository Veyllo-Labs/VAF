# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The room checking in on a member that drifted off.

The sweep is the half that costs money if it is wrong: it runs on a timer, in a
process that serves every tenant, and each check-in it sends may wake an agent
and spend a model turn. So the rules it enforces are the ones tested here - only
the host asks, only about peers that are actually idle, at most once per
interval, and never about itself.
"""
import time
from pathlib import Path

import pytest

import vaf.core.a2a.store as store_mod
import vaf.core.headless_runner as runner
from vaf.core.a2a.room import Room, derive_peer_id, participant_key

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def rooms(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path)
    runner._PING_SENT.clear()
    yield tmp_path
    runner._PING_SENT.clear()


def _host_room(base, scope="scope-host", room_id="room-sweep"):
    """A room this machine hosts, with the host's AGENT lane in it - the lane the
    sweep acts as, because it is the one that is present while VAF runs."""
    room = Room.create(kind="round", owner_scope=scope, base=base, room_id=room_id)
    host = room.join(display="Nobel", scope_id=scope,
                     peer_id=derive_peer_id(participant_key("agent", scope), room_id))
    return room, host


def test_the_sweep_asks_only_idle_peers_and_never_itself(rooms, monkeypatch):
    """MUTATION: sweep every member, or drop the idleness check.

    A check-in for a peer that is right here costs a model turn to tell it what
    it already knows, and in a room of twenty that is nineteen wasted turns an
    hour. The host is skipped for the same reason plus a better one: it is the
    one doing the asking.
    """
    room, host = _host_room(rooms)
    quiet = room.join(display="Quiet", scope_id=None, peer_id="p-quiet")
    room.say(host, "anybody there?")

    # `quiet` last did something (its join) two hours ago as far as the sweep is
    # concerned; the host wrote just now.
    later = time.time() + 7200
    monkeypatch.setattr(runner.time, "time", lambda: later)
    monkeypatch.setattr("vaf.core.config.Config.get",
                        lambda key, default=None: 60 if key == "a2a_room_ping_minutes" else default)

    runner._room_ping_sweep()

    pings = [f for f in room.transcript() if f["kind"] == "ping"]
    assert len(pings) == 1, f"expected one check-in, got {len(pings)}"
    assert pings[0]["to"] == {"peer": "p-quiet"}
    assert pings[0]["peer"] == host.peer_id, "the host is the one that asks"


def test_a_peer_is_asked_at_most_once_per_interval(rooms, monkeypatch):
    """MUTATION: forget which peers were already asked.

    The sweep runs every minute and the interval is an hour: without the memory
    a peer that stays idle is woken sixty times, which is the exact failure a
    check-in is supposed to prevent.
    """
    room, host = _host_room(rooms, room_id="room-once")
    room.join(display="Quiet", scope_id=None, peer_id="p-quiet")
    room.say(host, "hello")

    later = time.time() + 7200
    monkeypatch.setattr(runner.time, "time", lambda: later)
    monkeypatch.setattr("vaf.core.config.Config.get",
                        lambda key, default=None: 60 if key == "a2a_room_ping_minutes" else default)

    for _ in range(5):
        runner._room_ping_sweep()

    assert len([f for f in room.transcript() if f["kind"] == "ping"]) == 1


def test_zero_minutes_turns_the_whole_thing_off(rooms, monkeypatch):
    """A switch that cannot be switched off is not a setting."""
    room, host = _host_room(rooms, room_id="room-off")
    room.join(display="Quiet", scope_id=None, peer_id="p-quiet")
    room.say(host, "hello")

    later = time.time() + 7200
    monkeypatch.setattr(runner.time, "time", lambda: later)
    monkeypatch.setattr("vaf.core.config.Config.get",
                        lambda key, default=None: 0 if key == "a2a_room_ping_minutes" else default)

    runner._room_ping_sweep()
    assert [f for f in room.transcript() if f["kind"] == "ping"] == []


def test_a_guests_room_is_not_swept_by_this_machine(rooms, monkeypatch):
    """MUTATION: sweep every room on disk.

    A room whose host is somebody else may be on this disk (a guest keeps its own
    copy of what it read), and pinging its members would be this machine asking
    questions on another host's behalf - which the room would refuse anyway,
    since is_host is keyed on the tenant. Skipped before the refusal, so a
    guest's rooms cost nothing every minute.
    """
    room = Room.create(kind="round", owner_scope="somebody-else", base=rooms,
                       room_id="room-foreign")
    room.join(display="Them", scope_id="somebody-else", peer_id="p-them")
    room.join(display="Quiet", scope_id=None, peer_id="p-quiet")

    later = time.time() + 7200
    monkeypatch.setattr(runner.time, "time", lambda: later)
    monkeypatch.setattr("vaf.core.config.Config.get",
                        lambda key, default=None: 60 if key == "a2a_room_ping_minutes" else default)

    runner._room_ping_sweep()
    assert [f for f in room.transcript() if f["kind"] == "ping"] == []


def test_the_wake_frames_a_check_in_as_a_look_around():
    """MUTATION: let a ping fall through to the ordinary room opening.

    Then the agent reads "somebody wrote to you" for a frame nobody wrote, and
    answers a status probe as if it were a question - which is the thank-you loop
    with extra steps.
    """
    src = (ROOT / "vaf" / "core" / "agent.py").read_text(encoding="utf-8")
    assert 'all(f.kind == "ping" for f in frames)' in src, (
        "the wake no longer recognises a check-in")
    block = src.split('all(f.kind == "ping" for f in frames)', 1)[1][:1200]
    assert "CHECK-IN" in block and "not something somebody said" in block.lower()
    assert "invitation" in block.lower(), "a check-in must not read as an order"
    assert "saying nothing" in block.lower() or "say nothing" in block.lower()


def test_a_check_in_is_not_drawn_as_a_message():
    """MUTATION: leave pings in the browser transcript.

    It is the room talking to ONE agent about its own attention. Drawn as a
    bubble it would put words in a member's mouth that no member wrote - and it
    would do so once an hour, forever.
    """
    src = (ROOT / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    block = src.split('"messages": [', 1)[1][:800]
    assert 'if e["kind"] != "ping"' in block, (
        "the room transcript draws check-ins as messages")


def test_the_wake_shows_open_votes_and_the_mission():
    """MUTATION: leave votes or the mission out of the room turn.

    Both fail the same way and silently. A vote scrolls out of a transcript like
    anything else, and a decision nobody was asked about is one made by whoever
    happened to be awake. A purpose stated once at creation is gone from the
    context by the time it would have decided anything.
    """
    src = (ROOT / "vaf" / "core" / "agent.py").read_text(encoding="utf-8")
    assert "OPEN VOTES YOU HAVE NOT ANSWERED" in src, (
        "the room turn never mentions a vote this agent still owes an answer to")
    block = src.split("OPEN VOTES YOU HAVE NOT ANSWERED", 1)[1][:700]
    assert "reply_to" in block and "choice" in block, (
        "the agent is told a vote exists but not how to answer it")
    assert "WHAT THIS ROOM IS FOR" in src, "the room turn forgot the mission"
    assert "LEADS THIS ROOM" in src, (
        "a role tag says what an agent may send, not who to ask")


def test_the_browser_can_see_and_cast_a_vote():
    """MUTATION: send the tally without a way to answer it, or drop `mine`.

    The person in the room is a member like any other. A card that shows a
    question and cannot take an answer sends them to a terminal to vote in their
    own room, and without `mine` the card cannot show what they already chose -
    so they vote twice and wonder why nothing changed.
    """
    server = (ROOT / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    assert '"votes": [' in server, "the room payload carries no votes"
    assert '"mine": next((b["choice"]' in server, (
        "the card cannot tell the viewer what they already voted for")
    assert 'elif type == "cast_room_vote":' in server, (
        "the browser has no way to cast a ballot")

    page = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    assert "onVote?.(v.id, opt)" in page, "the options are not clickable"
    assert "cast_room_vote" in page, "the click goes nowhere"
    assert "waitingFor" in page, "the card hides who the room is waiting for"
