# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A reaction: an emoji on one message, and nobody is woken by it.

The cheapest thing a member can say. It exists because "seen" written as a message
wakes every agent it is aimed at to read nothing, which is the first failure the
conduct rules name - and the rules could only forbid the message, not offer the
alternative. Everything pinned here is the alternative: shown everywhere, read along,
audited, and never a turn.

The template is the ballot, the one text-free frame the protocol already had: the
room writes the line at compose, so a lane that can send an emoji and an id has sent
a complete frame, and every reader that prints body.text shows the reaction.
"""
import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

import vaf.core.a2a.store as store_mod
from vaf.core.a2a.frame import KINDS
from vaf.core.a2a.room import (AUDIT_EVENTS, CAPABILITIES, NON_CONVERSATION_KINDS,
                               SILENT_KINDS, MalformedContent, Room, derive_peer_id,
                               describe, participant_key, unread_counts, unread_frames)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def rooms(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path)
    return tmp_path


def _round(base, room_id="room-react"):
    """The agent of one account and a stranger on a ticket, in a round."""
    room = Room.create(kind="round", owner_scope="scope-a", base=base, room_id=room_id)
    key = participant_key("agent", "scope-a")
    agent = room.join(display="Nobel", scope_id="scope-a",
                      peer_id=derive_peer_id(key, room_id), participant_key=key)
    other = room.join(display="Codex", scope_id=None, peer_id="p-codex")
    return room, key, agent, other


# ── the frame ──────────────────────────────────────────────────────────────

def test_a_reaction_is_an_emoji_on_one_message_and_nothing_else(rooms):
    """MUTATION: accept a reaction without reply_to, or without an emoji.

    An emoji on nothing is nothing, and a reaction with no emoji is a message with
    no words: both are refused at the door rather than stored and rendered blank.
    """
    room, _key, agent, other = _round(rooms)
    said = room.say(agent, "the logs are clean")

    frame = room.react(other, said.id, "+1")
    assert frame.kind == "reaction" and frame.reply_to == said.id
    assert frame.body == {"emoji": "+1", "text": "+1"}
    assert "reaction" in KINDS

    with pytest.raises(MalformedContent):
        room.compose({"kind": "reaction", "body": {"emoji": "+1"}})
    with pytest.raises(MalformedContent):
        room.compose({"kind": "reaction", "reply_to": said.id, "body": {}})


def test_compose_is_a_fixed_point_for_a_reaction_and_caps_it(rooms):
    """MUTATION: trim once but not twice, or let a paragraph through.

    C12 holds for this kind like every other: a sender may ask what will be stored
    and sign exactly that. And a "reaction" long enough to hold a sentence is a
    message wearing a costume - it would wake nobody, so it must not carry words.
    """
    room, _key, agent, _other = _round(rooms)
    said = room.say(agent, "x")

    once = room.compose({"kind": "reaction", "reply_to": said.id,
                         "body": {"emoji": " " + "y" * 40}})
    assert once["body"]["emoji"] == "y" * 16 and once["body"]["text"] == "y" * 16
    assert room.compose(once) == once

    # `text` alone is the emoji, for a lane that can only send text.
    plain = room.compose({"kind": "reaction", "reply_to": said.id, "body": {"text": "+1"}})
    assert plain["body"] == {"emoji": "+1", "text": "+1"}


def test_every_role_may_react(rooms):
    """MUTATION: leave `reaction` out of one role's CAPABILITIES.

    A kind in KINDS and missing from a role's set is refused for that role at
    ingest, runtime-proven, so the table is pinned for all three and one of them
    is driven.
    """
    for role in ("leader", "worker", "peer"):
        assert "reaction" in CAPABILITIES[role], role

    chain = Room.create(kind="chain", owner_scope="scope-a", base=rooms, room_id="room-chain-r")
    leader = chain.join(display="Lead", scope_id="scope-a", peer_id="p-lead")
    worker = chain.join(display="Work", scope_id=None, peer_id="p-work")
    assert chain.roles().get("p-work") == "worker"
    said = chain.say(leader, "please look at this")
    assert chain.react(worker, said.id, "+1").kind == "reaction"
    assert chain.react(leader, said.id, "+1").kind == "reaction"


# ── what it costs: nothing ─────────────────────────────────────────────────

def test_a_reaction_wakes_nobody_and_is_read_along(rooms):
    """MUTATION: drop SILENT_KINDS from the waking comprehension.

    The whole point. Addressed to the room, so the addressing rule alone would wake
    every member; the kind is what keeps it silent. And it is not dropped from the
    context either: an agent woken by something else sees what its report earned.
    """
    room, key, agent, other = _round(rooms)
    said = room.say(agent, "done, see the report")
    room.react(other, said.id, "+1")

    assert unread_frames(key, base=rooms) == [], "a reaction cost a turn"
    assert unread_counts(key, base=rooms) == {}

    room.say(other, "one more thing")
    pending = unread_frames(key, base=rooms)
    assert len(pending) == 1
    _room, _identity, waking, context = pending[0]
    assert [f.kind for f in waking] == ["say"]
    assert [f.kind for f in context] == ["reaction", "say"], "read along, in order"


def test_a_reaction_is_nothing_said_for_the_badge_and_the_corpus():
    """MUTATION: leave `reaction` out of NON_CONVERSATION_KINDS.

    The sidebar badge, the learning transcript and the cross-chat corpus all ask
    "was anything said" through that one set; a reaction answering differently on
    any of them is a notification for a line no view shows as new.
    """
    assert "reaction" in SILENT_KINDS
    assert "reaction" in NON_CONVERSATION_KINDS


# ── how it reads ───────────────────────────────────────────────────────────

def test_a_reaction_renders_as_its_emoji_and_audits_as_reacted(rooms):
    """MUTATION: stop compose from writing the emoji into body.text.

    A KNOWN kind that carries no text renders as an EMPTY line on every surface at
    once - describe() falls through to the text - which is the trap this kind was
    measured to fall into before it was written. The room writes the text at the
    door, so no renderer needs a branch: a describe() branch was tried and changed
    nothing observable, which is why there is none.
    """
    room, _key, agent, other = _round(rooms)
    said = room.say(agent, "x")
    room.react(other, said.id, "+1")

    rows = room.transcript()
    reaction = [r for r in rows if r["kind"] == "reaction"][0]
    assert describe(reaction) == "+1"
    assert reaction["reply_to"] == said.id and reaction["known"] is True
    assert AUDIT_EVENTS["reaction"] == "reacted"


def test_the_wake_prompt_names_the_message_a_reaction_landed_on():
    """The room turn reads body.text, which for a reaction is the emoji alone; the
    label has to say WHICH message was seen or the agent reads a bare "+1"."""
    source = (ROOT / "vaf" / "core" / "agent.py").read_text(encoding="utf-8")
    wake = source.split("def collect_room_wake")[1].split("\n    def ")[0]
    assert 'label += f" on {frame.reply_to}"' in wake


# ── every lane that can send one ───────────────────────────────────────────

def test_the_agents_tool_reacts_and_refuses_text_as_an_id(rooms):
    """MUTATION: accept the message's text as reply_to.

    Found live for room_send: a model handed the MESSAGE TEXT as the id and the
    room stored it faithfully. The reacting tool shares that one door.
    """
    from vaf.tools.room_tools import RoomJoinTool, RoomReactTool

    room = Room.create(kind="round", owner_scope=None, base=rooms, room_id="room-tool-r")
    other = room.join(display="Codex", scope_id=None, peer_id="p-codex")
    said = room.say(other, "anyone there?")
    RoomJoinTool().run(room_id="room-tool-r", display="VAF", user_scope_id="scope-a")

    out = RoomReactTool().run(room_id="room-tool-r", reply_to=said.id, emoji="+1",
                              user_scope_id="scope-a")
    assert "Reacted +1" in out and "Nobody was woken" in out
    last = room.store.frames()[-1]
    assert last.kind == "reaction" and last.reply_to == said.id

    bad = RoomReactTool().run(room_id="room-tool-r", reply_to="anyone there?", emoji="+1",
                              user_scope_id="scope-a")
    assert bad.startswith("Error") and "ID" in bad
    assert "user_scope_id" in RoomReactTool.identity_kwargs


def test_the_cli_verb_reacts(rooms):
    """MUTATION: build the frame by hand in the verb instead of calling Room.react."""
    from vaf.cli.cmd import a2a as a2a_cmd

    room, _key, agent, other = _round(rooms, "room-cli-r")
    said = room.say(agent, "x")
    runner = CliRunner()

    result = runner.invoke(a2a_cmd.app, ["react", "room-cli-r", said.id, "+1", "--as", other.peer_id])
    assert result.exit_code == 0, result.output
    row = json.loads(result.output.strip().splitlines()[-1])
    assert row["ok"] and row["on"] == said.id and row["emoji"] == "+1"
    assert room.store.frames()[-1].kind == "reaction"

    result = runner.invoke(a2a_cmd.app, ["react", "room-cli-r", "the text of x", "+1",
                                         "--as", other.peer_id])
    assert result.exit_code != 0


def test_the_browser_lane_carries_the_target_and_can_react():
    """Source guards, the house pattern for the rebuild that has dropped fields twice.

    The transcript projection forwards `reply_to`, without which the browser could
    draw a reaction only as a line of its own - the message it exists to replace -
    and the command lane accepts `room_react` on the person's own lane.
    """
    server = (ROOT / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    assert '"reply_to": e.get("reply_to")' in server, "the projection drops the target"
    assert '"room_react"' in server and "room.react(identity" in server
    page = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    assert "reply_to?: string" in page, "the RoomMessage type has no target"
    assert "'room_react'" in page, "the browser cannot send one"
    assert "m.kind === 'reaction'" in page, "a reaction is drawn as a message"


def _guest():
    spec = importlib.util.spec_from_file_location("a2a_wire_peer_react",
                                                  ROOT / "examples" / "12_a2a_wire_peer.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["a2a_wire_peer_react"] = module
    spec.loader.exec_module(module)
    return module


def test_the_guest_client_reacts_over_its_seat(monkeypatch, capsys):
    """Driving the VERB: the payload the guest sends is the one the room accepts."""
    guest = _guest()
    sent = []

    class Line:
        def submit(self, payload):
            sent.append(payload)
            return {"kind": "ack", "status": "committed", "id": "f-1"}

    monkeypatch.setattr(guest, "load_record", lambda room: {"room": room, "peer": "p-g"})
    monkeypatch.setattr(guest, "_line_for", lambda room, record: Line())
    guest.cmd_react(argparse.Namespace(room="room-g", frame_id="f-target", emoji="+1"))

    assert sent == [{"kind": "reaction", "reply_to": "f-target", "body": {"emoji": "+1"}}]
    assert json.loads(capsys.readouterr().out.strip())["status"] == "committed"
    assert "react <room> <id> <emoji>" in guest._HOWTO
