# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A vote that ends by itself: deadline, one reminder, abstention, result.

The half that is easy to get wrong is not the counting - it is WHEN. A vote that
never ends leaves a room full of questions nobody reads; a vote that ends twice
puts two results in a transcript that cannot take either back; a reminder that
repeats nags an agent every tick and spends a model turn each time.

So what is pinned here is the timing and the once-ness, both of which are derived
from the log rather than remembered, and the rule that only the room itself may
say how a vote ended.
"""
import time
from pathlib import Path
from typing import List

import pytest

import vaf.core.a2a.store as store_mod
from vaf.core.a2a.room import (Room, RoomError, derive_peer_id, describe,
                               fold_votes, participant_key)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def rooms(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path)
    return tmp_path


def _room(base, scope="scope-host", room_id="room-vote"):
    """A room this machine hosts, plus its host lane and two members."""
    room = Room.create(kind="round", owner_scope=scope, base=base, room_id=room_id)
    host = room.join(display="Nobel", scope_id=scope,
                     peer_id=derive_peer_id(participant_key("agent", scope), room_id))
    alice = room.join(display="Alice", scope_id=None, peer_id="p-alice")
    bob = room.join(display="Bob", scope_id=None, peer_id="p-bob")
    return room, host, alice, bob


def _entry(room, vote_id, *, now=None):
    return next(v for v in fold_votes(room.store.frames(), labels=room.labels(),
                                      members=list(room.members().keys()), now=now)
                if v["id"] == vote_id)


def test_a_vote_that_named_no_deadline_still_ends(rooms):
    """MUTATION: leave `deadline` at closes_at, so a vote without one never ends.

    Every live vote in this house was opened without a deadline, because the flag
    is optional and nobody passes it. If "no deadline" meant "open forever", the
    reminder and the abstention would be features that in practice never fire.
    """
    room, host, alice, bob = _room(rooms)
    vote = room.open_vote(host, "Pizza?", options=["yes", "no"])

    entry = _entry(room, vote.id)
    assert entry["closes_at"] == 0.0, "nothing was written into the frame"
    assert entry["deadline"] == pytest.approx(vote.ts + 180.0, abs=1.0)
    assert entry["remind_at"] == pytest.approx(vote.ts + 60.0, abs=1.0)

    # And a vote that DID name one keeps it, with the reminder two minutes before.
    timed = room.open_vote(host, "Now?", options=["yes"], closes_in_s=600.0)
    late = _entry(room, timed.id)
    assert late["deadline"] == pytest.approx(timed.ts + 600.0, abs=1.0)
    assert late["remind_at"] == pytest.approx(timed.ts + 480.0, abs=1.0)


def test_everybody_voting_ends_it_without_waiting_for_the_clock(rooms):
    """MUTATION: require `now >= deadline` for `due`.

    Waiting out a three-minute timer after the last member has answered is three
    minutes of a room showing a question that is already decided.
    """
    room, host, alice, bob = _room(rooms)
    vote = room.open_vote(host, "Pizza?", options=["yes", "no"])

    room.cast(alice, vote.id, "yes")
    half = _entry(room, vote.id)
    assert half["due"] is False and half["everyone_voted"] is False
    assert half["waiting_for"] == ["Bob"], "the asker is not waited for"

    room.cast(bob, vote.id, "no")
    full = _entry(room, vote.id)
    assert full["everyone_voted"] is True
    assert full["due"] is True, "the last ballot ends it"
    assert full["closed"] is True


def test_the_room_writes_one_result_and_only_once(rooms):
    """MUTATION: drop the existence check in conclude_votes.

    The store keys a frame on (sender, seq), never on what it answers, so nothing
    below this method refuses a second result. Two results for one vote is a
    transcript saying the room decided twice.
    """
    room, host, alice, bob = _room(rooms)
    vote = room.open_vote(host, "Pizza?", options=["yes", "no"])
    room.cast(alice, vote.id, "yes")
    room.cast(bob, vote.id, "yes")

    written = room.conclude_votes(host)
    assert len(written) == 1 and written[0].kind == "tally"
    assert written[0].reply_to == vote.id

    again = room.conclude_votes(host)
    assert again == [], "a concluded vote is concluded"
    tallies = [f for f in room.store.frames() if f.kind == "tally"]
    assert len(tallies) == 1

    entry = _entry(room, vote.id)
    assert entry["concluded"] is True
    assert entry["result"]["tally"] == {"yes": 2}
    assert entry["result"]["winner"] == "yes"
    assert entry["result"]["everyone_voted"] is True
    assert "Everybody voted" in entry["result"]["text"]
    # And it reads as a message, because it IS one - the room saying how it ended.
    row = next(e for e in room.transcript() if e["id"] == written[0].id)
    assert describe(row) == entry["result"]["text"]


def test_a_result_written_while_the_loop_runs_is_not_written_twice(rooms):
    """MUTATION: drop the host-lane re-read in conclude_votes, keeping only the fold.

    The fold happens ONCE at the top of the loop, and the loop then writes one
    result per due vote. By the time it reaches the second one, that fold is as old
    as the first write - so a second host process that concluded the second vote in
    between is invisible to it. The re-read is what closes that window, and it is
    cheap because one writer owns one lane.

    This is the honest scope of the guarantee: single-write INTENT, not
    exactly-once. Two processes writing in the same instant still produce two
    results, which is why the fold takes the last one.
    """
    room, host, alice, bob = _room(rooms)
    first = room.open_vote(host, "One?", options=["yes", "no"])
    second = room.open_vote(host, "Two?", options=["yes", "no"])
    for vote in (first, second):
        room.cast(alice, vote.id, "yes")
        room.cast(bob, vote.id, "yes")

    real_ingest = room.ingest
    raced: List[str] = []

    def racing(data, *, identity):
        frame = real_ingest(data, identity=identity)
        if frame.kind == "tally" and not raced:
            # Another host process concludes the OTHER vote while this loop is
            # still on its first one. Which vote that is depends on the fold's
            # order (newest first), so it is taken from the write itself.
            other = second.id if frame.reply_to == first.id else first.id
            raced.append(other)
            Room.open(room.room_id, base=rooms).ingest(
                {"kind": "tally", "reply_to": other, "to": {"room": True},
                 "body": {"text": "raced", "vote": other, "abstained": []}},
                identity=host)
        return frame

    room.ingest = racing
    try:
        written = room.conclude_votes(host)
    finally:
        room.ingest = real_ingest

    assert len(written) == 1, "the raced vote was concluded a second time"
    assert written[0].reply_to not in raced
    for vote in (first, second):
        results = [f for f in room.store.frames()
                   if f.kind == "tally" and f.reply_to == vote.id]
        assert len(results) == 1, f"{vote.body['text']} was concluded twice"


def test_a_member_that_never_answered_is_named_as_abstaining(rooms):
    """MUTATION: leave `abstained` empty, or recompute it after the fact.

    A vote that simply evaporates tells nobody anything, and the recompute is the
    subtler bug: a ballot cast after the room closed the vote would retroactively
    un-abstain somebody the transcript already named.
    """
    room, host, alice, bob = _room(rooms)
    vote = room.open_vote(host, "Pizza?", options=["yes", "no"])
    room.cast(alice, vote.id, "yes")

    after = time.time() + 200.0
    entry = _entry(room, vote.id, now=after)
    assert entry["due"] is True and entry["abstained"] == ["Bob"]

    written = room.conclude_votes(host, now=after)
    assert len(written) == 1
    assert written[0].body["abstained"] == ["Bob"]
    assert "counted as abstaining: Bob" in written[0].body["text"]

    # Bob turns up late. The log takes his ballot - it cannot refuse the past -
    # but the result already written is what every reader folds.
    room.cast(bob, vote.id, "no")
    late = _entry(room, vote.id)
    assert late["abstained"] == ["Bob"], "the result frame is the record, not a recount"
    assert late["concluded"] is True


def test_only_the_room_itself_says_how_a_vote_ended(rooms):
    """MUTATION: add `tally` to CAPABILITIES, or to the role gate.

    A result a member can write is a result a member can invent - and in a room of
    strangers that is the whole point of the kind being host-only.
    """
    room, host, alice, bob = _room(rooms)
    vote = room.open_vote(host, "Pizza?", options=["yes", "no"])

    with pytest.raises(RoomError):
        room.ingest({"kind": "tally", "reply_to": vote.id,
                     "body": {"text": "I won", "tally": {"yes": 99}}}, identity=alice)
    with pytest.raises(RoomError):
        room.conclude_votes(alice)
    with pytest.raises(RoomError):
        room.remind_vote(alice, "p-bob", _entry(room, vote.id))


def test_the_reminder_is_sent_once_and_never_to_the_asker(rooms):
    """MUTATION: drop the `already` set, or stop excluding the asker.

    Without the first, every sweep tick reminds the same member again and each
    reminder spends a model turn. Without the second, the room nags the peer that
    put the question for not answering itself.
    """
    room, host, alice, bob = _room(rooms)
    vote = room.open_vote(host, "Pizza?", options=["yes", "no"])
    room.cast(alice, vote.id, "yes")

    assert room.vote_reminders(now=vote.ts + 10) == [], "too early to nag"

    due = room.vote_reminders(now=vote.ts + 70)
    assert [peer for peer, _ in due] == ["p-bob"], "alice voted, the host asked"

    room.remind_vote(host, "p-bob", due[0][1])
    assert room.vote_reminders(now=vote.ts + 80) == [], "once per member per vote"

    # A second vote is a second question, so it earns its own reminder.
    other = room.open_vote(host, "Beer?", options=["yes", "no"])
    again = room.vote_reminders(now=other.ts + 70)
    assert sorted(peer for peer, _ in again) == ["p-alice", "p-bob"]


def test_the_reminder_carries_everything_needed_to_answer_it(rooms):
    """MUTATION: shorten the body to "you have not voted".

    The member being reminded may be a foreign agent that sees none of our
    surfaces: it has the frame and nothing else. A nudge without the question, the
    options and a way to cast is a nudge it cannot act on.
    """
    room, host, alice, bob = _room(rooms)
    vote = room.open_vote(host, "Pizza tonight?", options=["yes", "no"])
    entry = _entry(room, vote.id)

    frame = room.remind_vote(host, "p-bob", entry)
    assert frame.kind == "ping", "the hidden lane already exists"
    assert frame.to == {"peer": "p-bob"}, "one member, not the room"
    assert frame.body["vote"] == vote.id

    text = frame.body["text"]
    assert "Pizza tonight?" in text
    assert "yes | no" in text
    assert vote.id in text, "the id it must answer"
    assert "room_send" in text and "vaf a2a ballot" in text, "both kinds of agent"
    assert "abstaining" in text, "what silence will mean"
    state = frame.body["state"]
    assert state["kind"] == "vote_reminder" and state["options"] == ["yes", "no"]


def test_a_ballot_is_resolved_wherever_it_arrives_from(rooms):
    """MUTATION: resolve the choice in `cast` only, the way it was.

    `cast` is one of four ways a ballot reaches a room - the others are a shell on
    this machine, a peer over the wire, and any third-party implementation, and
    none of them goes through it. Measured live: a remote 'ja' against an option
    called 'ja, weiter so' became its own column in the tally, so the room counted
    three answers to a two-way question.

    Resolving in ingest is what makes the shortening safe for all four at once,
    and refusing an invention there is what lets a machine peer retry instead of
    quietly skewing a count nobody rechecks.
    """
    room, host, alice, bob = _room(rooms)
    vote = room.open_vote(host, "Weiter?", options=["ja, weiter so", "erst schlafen"])

    # Exactly what the wire hands the host: a bare answer frame, no `cast`.
    room.ingest({"kind": "answer", "reply_to": vote.id,
                 "body": {"text": "votes: ja", "choice": "ja"}}, identity=alice)
    entry = _entry(room, vote.id)
    assert entry["tally"] == {"ja, weiter so": 1}, "a shortened answer is still that answer"

    with pytest.raises(RoomError) as refused:
        room.ingest({"kind": "answer", "reply_to": vote.id,
                     "body": {"text": "maybe", "choice": "vielleicht"}}, identity=bob)
    assert "erst schlafen" in str(refused.value), "a refusal names the options"

    # An ordinary answer is untouched - it is a message, not a ballot.
    plain = room.ingest({"kind": "answer", "reply_to": vote.id,
                         "body": {"text": "I have no opinion"}}, identity=bob)
    assert "choice" not in plain.body
    assert _entry(room, vote.id)["voted"] == 1


def test_a_concluded_vote_is_the_same_for_a_reader_with_no_store(rooms):
    """MUTATION: fold votes from the store instead of from frames.

    A peer reading a room over the wire has frames and no store. Two folds would
    be two opinions about who abstained, which is the one part of a vote nobody
    may recompute differently.
    """
    room, host, alice, bob = _room(rooms)
    vote = room.open_vote(host, "Pizza?", options=["yes", "no"])
    room.cast(alice, vote.id, "yes")
    room.conclude_votes(host, now=time.time() + 200.0)

    # Exactly what the wire hands a remote peer: frames, and names from the log.
    frames = room.store.frames()
    labels = room.labels()
    members = sorted({f.sender for f in frames if f.kind == "join"})
    remote = fold_votes(frames, labels=labels, members=members)
    local = room.votes()
    assert [v["id"] for v in remote] == [v["id"] for v in local]
    assert remote[0]["abstained"] == local[0]["abstained"] == ["Bob"]
    assert remote[0]["tally"] == local[0]["tally"] == {"yes": 1}


def test_the_sweep_ends_a_vote_even_with_check_ins_turned_off(rooms, monkeypatch):
    """MUTATION: gate the vote half on `a2a_room_ping_minutes` too.

    The check-in is an hourly courtesy a person may switch off; a deadline is part
    of the protocol. Sharing one setting would mean that turning off "are you still
    there?" also left every vote open forever, waiting for a result nobody writes -
    and the surfaces would keep showing a countdown that had stopped meaning
    anything.
    """
    import vaf.core.headless_runner as runner

    room, host, alice, bob = _room(rooms, scope="scope-sweep", room_id="room-sweep-vote")
    vote = room.open_vote(alice, "Pizza?", options=["yes", "no"])
    room.cast(bob, vote.id, "yes")

    monkeypatch.setattr("vaf.core.config.Config.get",
                        lambda key, default=None: 0 if key == "a2a_room_ping_minutes" else default)
    monkeypatch.setattr(runner.time, "time", lambda: vote.ts + 200.0)
    runner._room_ping_sweep()

    entry = _entry(room, vote.id)
    assert entry["concluded"] is True, "the room never said how it ended"
    assert entry["result"]["tally"] == {"yes": 1}
    # Nobel is the host and did not vote, but the host is a member like any other:
    # what it did not answer, it abstained from.
    assert entry["abstained"] == ["Nobel"]


def test_the_sweep_reminds_the_agent_and_leaves_the_person_alone(rooms, monkeypatch):
    """MUTATION: sweep every waiting member, or drop the human-lane skip.

    A reminder is a frame that wakes an agent and spends a model turn. The person's
    own terminal lane is not an agent: nudging it reaches somebody who is looking at
    the vote card in their browser, and the same mistake was already measured once
    on the idle check-in.
    """
    import vaf.core.headless_runner as runner

    scope = "scope-remind"
    room = Room.create(kind="round", owner_scope=scope, base=rooms, room_id="room-remind")
    host = room.join(display="Nobel", scope_id=scope,
                     peer_id=derive_peer_id(participant_key("agent", scope), "room-remind"))
    person = room.join(display="Alice", scope_id=scope,
                       peer_id=derive_peer_id(participant_key("cli", scope), "room-remind"))
    guest = room.join(display="Codex", scope_id=None, peer_id="p-codex")
    vote = room.open_vote(host, "Pizza?", options=["yes", "no"])

    monkeypatch.setattr(runner.time, "time", lambda: vote.ts + 70.0)
    monkeypatch.setattr("vaf.core.config.Config.get",
                        lambda key, default=None: 0 if key == "a2a_room_ping_minutes" else default)
    runner._room_ping_sweep()

    reminders = [f for f in room.store.frames()
                 if f.kind == "ping" and (f.body or {}).get("vote") == vote.id]
    assert [f.to.get("peer") for f in reminders] == [guest.peer_id], (
        "the agent is reminded; the person at the keyboard is not")

    # Twice through the sweep is still one reminder - it is derived from the log.
    runner._room_ping_sweep()
    assert len([f for f in room.store.frames()
                if f.kind == "ping" and (f.body or {}).get("vote") == vote.id]) == 1

    # The person's silence still counts, though: a deadline that only applied to
    # agents would be a different rule for the one member who can see the timer.
    monkeypatch.setattr(runner.time, "time", lambda: vote.ts + 200.0)
    runner._room_ping_sweep()
    entry = _entry(room, vote.id)
    assert entry["concluded"] is True
    assert sorted(entry["abstained"]) == ["Alice", "Codex"]


def test_the_result_quotes_the_question_instead_of_repeating_it(rooms):
    """MUTATION: put the whole question in the result line.

    Measured in a live vote: an agent wrote its eight options INTO the question
    text, so the result read as a wall with the counts hiding at the end of it.
    The question itself is two lines up in the same transcript; this line exists to
    say how it went, and a summary nobody finishes reading is not one.
    """
    room, host, alice, bob = _room(rooms)
    long_question = ("Welcher Teil war der beste? Jede:r stimmt einmal ab, Antwort "
                     "per Option, ich zaehle am Ende aus. Optionen: eins, zwei, drei, "
                     "vier, fuenf, sechs, sieben, acht.")
    vote = room.open_vote(host, long_question, options=["eins", "zwei"])
    room.cast(alice, vote.id, "eins")
    room.cast(bob, vote.id, "eins")

    text = room.conclude_votes(host)[0].body["text"]
    assert len(text) < 220, f"the result line is a wall of text ({len(text)} chars)"
    assert text.startswith('Vote closed: "Welcher Teil war der beste?')
    assert "..." in text, "the question was cut without saying so"
    assert text.rstrip().endswith("Everybody voted."), (
        "the counts must survive the trim - they are the point of the line")
    assert "eins 2" in text


def test_the_result_says_who_voted_for_what(rooms):
    """MUTATION: print the counts alone, the way the first version did.

    Ballots are public in this protocol for one reason: a tally nobody can check is
    a number somebody made up. A result line that says "2, 1" and not who takes that
    back at the one moment it matters - and it was the one thing a person in a live
    room noticed was missing, next to an agent's hand-written summary.

    The names are capped, because a room of twenty turns one line into a roll call.
    """
    room, host, alice, bob = _room(rooms)
    vote = room.open_vote(host, "Best one?", options=["three", "reach"])
    room.cast(alice, vote.id, "three")
    room.cast(bob, vote.id, "reach")
    room.cast(host, vote.id, "three")

    text = room.conclude_votes(host)[0].body["text"]
    assert "three 2 (" in text and "reach 1 (Bob)" in text
    assert "Alice" in text and "Nobel" in text
    assert "Result: three." in text

    # And the data stays beside the prose, so a surface counts without parsing it.
    body = [f for f in room.store.frames() if f.kind == "tally"][0].body
    assert body["tally"] == {"three": 2, "reach": 1}
    assert {b["label"] for b in body["ballots"]} == {"Alice", "Bob", "Nobel"}
