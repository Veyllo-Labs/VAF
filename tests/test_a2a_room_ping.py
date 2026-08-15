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
    assert "onVote?.(active.id, opt)" in page, "the options are not clickable"
    assert "cast_room_vote" in page, "the click goes nowhere"
    assert "waitingFor" in page, "the card hides who the room is waiting for"

    # And it has to be WHERE the person looks. Twice measured, twice wrong: at the
    # top of the transcript it scrolled off in a view that opens at the newest
    # message, and inside the header it looked right in the file while actually
    # sitting AFTER the header's closing tag, which scrolls exactly the same way.
    # It is docked to the composer now - the one part of the screen that never
    # moves - so the pin is that it renders INSIDE the composer block and above
    # the input, and that the conversation makes room for it instead of hiding
    # behind it.
    composer = page.index('"absolute left-0 right-0 w-full z-40')
    dock_at = page.index("<RoomVoteDock", composer)
    input_at = page.index('"Ask anything..."', composer)
    assert composer < dock_at < input_at, (
        "the vote panel left the composer block and can scroll away again")
    # What it hangs on, exactly: "renders somewhere in this block" stays true for a
    # panel that is switched off, and a test that cannot tell those apart is the
    # one this feature already slipped past once.
    guard = page[page.rindex("{", 0, dock_at):dock_at].strip()
    assert guard == "{voteDockOpen && (", (
        f"the panel renders under {guard!r}, not on whether there is a vote")
    assert "paddingBottom: `${128 + dockHeight}px`" in page, (
        "the conversation does not make room for the panel, so the panel covers it")
    assert "new ResizeObserver(measure)" in page, (
        "the room it makes is a guessed constant again - two docks of their own "
        "height cannot be answered by one number")

    # It arrives AND leaves as a movement. A vote here ends by itself, so the
    # panel disappearing is the normal case, not an edge one.
    assert "vote-dock-enter" in page and "vote-dock-leave" in page
    css = (ROOT / "web" / "app" / "globals.css").read_text(encoding="utf-8")
    for frames in ("voteDockEnter", "voteDockLeave"):
        assert f"@keyframes {frames}" in css, f"{frames} is a class with no animation"

    # Several open votes are tabs, not a stack: a stack pushes the conversation off
    # screen to show questions nobody asked to see all at once.
    assert 'role="tablist"' in page and 'role="tab"' in page

    # The countdown runs off the deadline the server sends, not off a seconds-left
    # it recomputes each poll - a clock that only moves when a poll lands stutters.
    assert '"deadline": v["deadline"]' in server, "the card cannot count down"
    assert '"everyoneVoted": v["everyone_voted"]' in server
    assert 'deadline={active.deadline ?? 0}' in page
    # Orange in the dark, black in the light. Named here because a timer is the one
    # element in this panel that must be legible in a glance in both themes.
    assert "text-gray-900 dark:text-amber-400" in page

    # A vote leaves the card when the ROOM has said how it ended, so the card and
    # the result message change places rather than both being absent for a while.
    assert 'for v in room.votes() if not v["concluded"]' in server


def test_the_persons_own_lane_is_never_checked_in_on(rooms, monkeypatch):
    """MUTATION: sweep every idle member.

    Measured in the first live sweep: the room asked the owner's TERMINAL handle
    whether it was still with it. That lane is a person, not an agent - nothing
    wakes, nothing answers, and the room collects one frame an hour for as long
    as it lives. The handle is derived from the same key the CLI acts under, so
    this is exact rather than a guess about who looks human.
    """
    room, host = _host_room(rooms, room_id="room-human")
    person = room.join(display="Alice", scope_id="scope-host",
                       peer_id=derive_peer_id(participant_key("cli", "scope-host"),
                                              "room-human"))
    agent_guest = room.join(display="Codex", scope_id=None, peer_id="p-codex")
    room.say(host, "anybody there?")

    later = time.time() + 7200
    monkeypatch.setattr(runner.time, "time", lambda: later)
    monkeypatch.setattr("vaf.core.config.Config.get",
                        lambda key, default=None: 60 if key == "a2a_room_ping_minutes" else default)

    runner._room_ping_sweep()

    asked = [f["to"].get("peer") for f in room.transcript() if f["kind"] == "ping"]
    assert asked == ["p-codex"], f"the sweep asked the wrong members: {asked}"
    assert person.peer_id not in asked


def test_the_room_shows_the_person_what_it_is_for():
    """MUTATION: send `mission` and render it nowhere, the way it shipped.

    Every agent in a room has been handed the mission in every turn since it was
    set - it rides in the wake prompt, in every check-in and in the welcome packet.
    The one member who could not see it anywhere was the person who set it: the
    field was in the payload and on no surface, which is the same class of defect
    as a field the frontend drops on the way through.
    """
    server = (ROOT / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    assert '"mission": str(room.manifest.get("mission")' in server

    page = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    assert "{view.room.mission}" in page, "the room's purpose reaches the browser and stops"
    header = page[page.index("sticky top-0 z-20 -mx-6"):]
    assert header.index("view.room.mission") < header.index("view.messages.map"), (
        "the mission is not in the header, where WHICH room this is gets answered")


def test_running_work_is_visible_without_scrolling():
    """MUTATION: leave the task board only at the top of the transcript.

    Measured live, twice over: a member reported progress with `--progress 6/7`, the
    board rendered it correctly, and the person who had asked to see it never did -
    the board sits above the messages in a view that opens at the newest one, which
    is exactly the defect the votes had. The full board stays where it is, because
    finished work that disappears reads as work that never happened; what is RUNNING
    is docked to the composer, which does not scroll.
    """
    page = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    assert "<RoomWorkDock" in page, "nothing shows running work outside the transcript"
    composer = page.index('"absolute left-0 right-0 w-full z-40')
    dock_at = page.index("<RoomWorkDock", composer)
    input_at = page.index('"Ask anything..."', composer)
    assert composer < dock_at < input_at, (
        "the work strip is not docked to the composer and can scroll away")
    # And what it hangs on, because "renders somewhere in this block" stays true for
    # a strip that is switched off - the gap that let the vote panel slip past once.
    guard = page[page.rindex("{", 0, dock_at):dock_at].strip()
    assert guard == "{roomView && (", (
        f"the work strip renders under {guard!r}, not on whether a room is open")
    # Only what is actually running, newest first - a strip that listed everything
    # open would be the scrolled-away board again, one screen lower.
    assert "ROOM_TASK_ACTIVE = ['submitted', 'working', 'input_required']" in page
    assert "(b.ts ?? 0) - (a.ts ?? 0)" in page, "the freshest work is not on top"
    assert "active.slice(0, 3)" in page and "weitere" in page, (
        "an uncapped strip pushes the conversation off its own screen")
    # WHO, not only what. The server already sends the resolved label; the first
    # version of this strip rendered the title alone, and in a room with several
    # agents the name is the half you cannot infer from the other one.
    server = (ROOT / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    assert '"assignee": t["assignee_label"]' in server, "the name never reaches the browser"
    strip = page.split("function RoomWorkDock", 1)[1].split("function ", 1)[0]
    assert "{task.assignee}" in strip, "the strip says what is running but not who is on it"
    assert strip.index("{task.assignee}") < strip.index("{task.title}"), (
        "the name belongs in front of the work, the way a speaker does")


def test_the_work_strip_opens_the_rooms_own_panel_on_a_work_tab():
    """MUTATION: give the strip its own dialog, or drop the tab and dump the board
    into the member list.

    "Who is in this room" and "who is on what" are two questions. Answered in one
    list, the second is buried under every member who has nothing running; answered
    in a surface of its own, a group chat grows a third window for a question the
    room panel is already the place for. The strip is the whole button, because
    hunting for a hit area inside a two-line summary is not a click anybody makes.
    """
    page = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    assert "<RoomWorkPanel" in page, "there is no work tab"
    assert "roomPanelTab === 'work' && (" in page and "roomPanelTab === 'members' && (" in page, (
        "the panel does not switch between the two questions")
    # Both doors set the tab, so a click lands where it was aimed.
    assert "setRoomPanelTab('members'); setRoomMembersOpen(true);" in page, (
        "the header button opens some other tab than the members")
    assert "setRoomPanelTab('work'); setRoomMembersOpen(true);" in page, (
        "the work strip does not open the work tab")
    # And the strip itself is the target, not something inside it.
    strip = page.split("function RoomWorkDock", 1)[1].split("function ", 1)[0]
    assert 'role="button"' in strip and "onClick={onOpen}" in strip
    assert "onKeyDown" in strip, "a div that acts as a button must answer the keyboard"

    # The labels are translated, not baked into the markup - both catalogues carry
    # them, because a key that exists in one language is a crash in the other.
    import json
    for locale in ("de", "en"):
        messages = json.loads((ROOT / "web" / "messages" / f"{locale}.json").read_text(encoding="utf-8"))
        for key in ("roomTabMembers", "roomTabWork", "roomWorkRunning",
                    "roomWorkNothing", "roomWorkFrom"):
            assert key in messages["main"], f"{key} missing in {locale}.json"
