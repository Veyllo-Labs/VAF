# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The invitation, and the briefing that travels with it.

An invitation is the only thing that crosses between two machines that have never met,
so the properties that matter are the ones nobody can check afterwards: the address has
to be one the certificate covers, and the instructions have to describe the role the
room will actually enforce. Both are pinned here.

The briefing is the part with the most leverage and the least ceremony. A foreign agent
that reads it wrongly does not crash - it sits in the room being polite, which looks
exactly like a room nobody wanted to use.
"""
from pathlib import Path

import pytest

import vaf.core.a2a.store as store_mod
from vaf.core.a2a.invite import briefing, invitation, lan_endpoint
from vaf.core.a2a.room import CAPABILITIES, Room

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def circle(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path)
    room = Room.create(kind="round", owner_scope="scope-a", base=tmp_path,
                       room_id="room-inv", topic="Deploy talk")
    owner = room.join(display="VAF", scope_id="scope-a", peer_id="p-owner")
    return room, owner


@pytest.fixture()
def chain(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path)
    room = Room.create(kind="chain", owner_scope="scope-a", base=tmp_path,
                       room_id="room-chain-inv")
    owner = room.join(display="VAF", scope_id="scope-a", peer_id="p-owner")
    return room, owner


# ── one invitation, whoever hands it out ───────────────────────────────────

def test_an_invitation_carries_the_credential_and_the_instructions_together(circle):
    """MUTATION: return the ticket without the briefing.

    Half an invitation is the failure that looks like nothing: the ticket was sent, the
    other agent joined, and then it waited - because nobody told it that a line out of
    `wait` is a request to act.
    """
    room, owner = circle
    row = invitation(room, owner, display="Codex")

    assert row["ticket"]
    assert row["join"] == f"vaf a2a join room-inv --ticket {row['ticket']}"
    assert row["briefing"], "an invitation with no instructions in it"
    assert row["ticket"] in row["briefing"], "the briefing does not carry the credential"


def test_the_command_and_the_agent_hand_out_the_same_thing():
    """MUTATION: build the invitation in the CLI again.

    Two inviters existed the moment the agent could open a room on request. Two
    assemblies of the same string would mean a foreign agent is told different things
    depending on who invited it, and the one that got the shorter version is the one
    that sits there waiting.
    """
    source = (ROOT / "vaf" / "cli" / "cmd" / "a2a.py").read_text(encoding="utf-8")
    # The invite command only. `vaf a2a trust` legitimately handles a fingerprint - it
    # PINS a CA, which is a different job from assembling an invitation, and a guard
    # that swept the whole file would have to be loosened the first time it fired.
    body = source.split("\ndef invite(")[1].split("\n@app.command()")[0]

    assert "from vaf.core.a2a.invite import invitation" in body
    for gone in ("get_local_network_ip", "ca_fingerprint", "join_remote", "wss://"):
        assert gone not in body, f"the CLI is building the invitation by hand again: {gone}"


# ── the address has to be one the certificate covers ───────────────────────

def test_the_printed_address_comes_from_the_certificate_s_own_source(monkeypatch):
    """MUTATION: read the address from any other source.

    The certificate's subject names are built from `get_local_network_ip`. An address
    printed from somewhere else is an address a strictly verifying client refuses, and
    the refusal reads like a client bug on the other machine - the hardest kind of
    failure to attribute when it happens over a LAN.
    """
    import vaf.core.a2a.invite as invite_mod

    monkeypatch.setattr("vaf.network.binding.get_local_network_ip", lambda: "192.168.7.7")
    monkeypatch.setattr("vaf.network.ssl_utils.ca_fingerprint", lambda: "ab" * 32)
    monkeypatch.setattr("vaf.core.config.Config.get",
                        staticmethod(lambda key, default=None: {
                            "local_network_tls_enabled": True,
                            "local_network_https_port": 8443,
                        }.get(key, default)))

    endpoint = invite_mod.lan_endpoint("room-x")
    assert endpoint["url"] == "wss://192.168.7.7:8443/ws/a2a/room-x"
    assert endpoint["ca_fingerprint"] == "ab" * 32

    source = (ROOT / "vaf" / "core" / "a2a" / "invite.py").read_text(encoding="utf-8")
    assert "get_local_network_ip" in source
    for other in ("gethostbyname", "socket.gethostname", "0.0.0.0"):
        assert other not in source, f"a second address source appeared: {other}"


def test_a_machine_with_no_lan_identity_still_invites(monkeypatch):
    """MUTATION: raise when the LAN half is unavailable.

    Most installations never turn network mode on. An exception here would take the
    local invitation away over a feature the user does not use.
    """
    monkeypatch.setattr("vaf.core.config.Config.get",
                        staticmethod(lambda key, default=None: False))
    assert lan_endpoint("room-x") == {}


def test_the_briefing_never_promises_a_lane_that_does_not_exist(circle, monkeypatch):
    """MUTATION: print `vaf a2a join --url ...` for a peer on another machine.

    That line was written before the client behind it was, and it survived because
    nothing ever ran it: `vaf a2a join` has no --url, the terminal lane joins through
    the room's own files, and no CLI client speaks the socket. An invitation that
    prints it hands a stranger a command that fails on a machine nobody here can see.

    The network address is still handed over as DATA - it is true, and an agent that
    implements the protocol directly needs it - but no command is built around it.
    """
    monkeypatch.setattr("vaf.core.a2a.invite.lan_endpoint",
                        lambda room_id: {"origin": "wss://h:8443",
                                         "url": f"wss://h:8443/ws/a2a/{room_id}",
                                         "ca_fingerprint": "cd" * 32})
    room, owner = circle
    row = invitation(room, owner)

    assert row["url"] == "wss://h:8443/ws/a2a/room-inv"
    assert "--url" not in row["briefing"], "the briefing names a flag that does not exist"
    assert "--url" not in row.get("join_remote", ""), "so does the machine-readable half"
    assert "vaf a2a join room-inv --ticket" in row["briefing"]


# ── the briefing describes the role the room will enforce ──────────────────

def test_the_capability_lines_are_read_off_the_enforcement_table(chain):
    """MUTATION: write the capabilities into the briefing by hand.

    The briefing tells an agent what it may send and the room refuses what it may not.
    Those are one fact. A hand-written list would keep promising `directive` to a worker
    long after the table stopped allowing it, and the agent would find out by being
    refused - in front of everybody, in a room it was invited into.
    """
    room, owner = chain
    text = invitation(room, owner, display="Codex")["briefing"]

    assert "`worker`" in text, "an invited agent in a chain is a worker"
    for allowed in sorted(CAPABILITIES["worker"]):
        assert allowed in text
    # and the three a worker must not send are named as refused
    refused_line = text.split("You may not send:")[1].split("\n")[0]
    for forbidden in ("close", "directive", "role"):
        assert forbidden in refused_line


def test_a_round_invites_peers_and_a_chain_invites_workers(circle, chain):
    assert invitation(circle[0], circle[1])["role"] == "peer"
    assert invitation(chain[0], chain[1])["role"] == "worker"


def test_the_briefing_says_the_one_thing_that_makes_a_room_work(circle):
    """MUTATION: soften the acting instruction into a description.

    An agent that reads `wait` output as text to look at will sit in the room forever.
    Nothing crashes, nothing is logged, and from the outside it is indistinguishable
    from a room nobody wanted.
    """
    room, owner = circle
    text = invitation(room, owner)["briefing"]

    assert "REQUEST TO ACT" in text
    assert "vaf a2a wait" in text
    # and it has to close the loop, or the agent acts exactly once
    assert "GO BACK TO STEP 2" in text


def test_the_briefing_tells_a_guest_to_claim_its_own_handle(circle):
    """MUTATION: leave VAF_A2A_PEER out.

    A guest that redeemed a ticket has a handle of its own, and every later command
    needs to know it. Without the export its messages are attributed to whoever owns
    the machine or refused outright - the CLI's own docstring records that the first
    live run hit exactly this, and a briefing that omits it hands a stranger the same
    dead end with more confidence.
    """
    room, owner = circle
    text = invitation(room, owner, display="Codex")["briefing"]

    assert "VAF_A2A_PEER" in text
    assert "peer" in text.split("VAF_A2A_PEER")[0][-400:], (
        "the briefing exports the variable without saying where the value comes from")


def test_every_command_the_briefing_names_exists_with_the_flags_it_uses():
    """MUTATION: rename a flag in the CLI and leave the briefing alone.

    This text is run by strangers on other machines. A flag that drifted would fail on
    somebody else's terminal, where nobody can see why, and the room would look broken
    rather than the instructions.
    """
    from typer.main import get_command

    from vaf.cli.cmd import a2a as a2a_cmd

    text = briefing(room_id="r1", ticket="t1", role="peer", display="Codex",
                    endpoint={"origin": "wss://h:1", "url": "wss://h:1/ws/a2a/r1",
                              "ca_fingerprint": "ff"})
    group = get_command(a2a_cmd.app)
    commands = group.commands

    used = {
        "join": ["--ticket"],
        "wait": [],
        "say": [],
        "answer": ["--reply-to"],
        "report": ["--status"],
        "leave": [],
    }
    for name, flags in used.items():
        assert f"vaf a2a {name}" in text, f"the briefing stopped naming {name}"
        assert name in commands, f"the briefing names a command that does not exist: {name}"
        options = {opt for param in commands[name].params for opt in param.opts}
        for flag in flags:
            assert flag in options, f"`vaf a2a {name}` has no {flag}"
            assert flag in text


def test_the_briefing_never_tells_a_stranger_it_gained_anything(circle):
    """The sentence the whole security model rests on has to survive into the text a
    foreign agent actually reads: a room hands out a role, never a tool and never a
    warrant."""
    room, owner = circle
    text = invitation(room, owner)["briefing"]

    assert "INPUT, never" in text
    assert "nothing here gives you access" in text.lower()


def test_the_briefing_is_paste_ready_plain_text(circle):
    """No markup, no colour, no shell quoting a paste would break: it goes into another
    agent's prompt box verbatim."""
    room, owner = circle
    text = invitation(room, owner)["briefing"]

    assert "\x1b[" not in text, "an ANSI escape would arrive as noise"
    assert "```" not in text, "a fenced block inside a pasted prompt breaks the paste"
    assert text.strip(), "empty briefing"


def test_a_briefing_can_be_built_without_a_room_at_all():
    """It is a pure function of what it is told, so a caller that already knows the role
    - the wire lane, a future bridge - does not have to open a room to produce one."""
    text = briefing(room_id="r1", ticket="t1", role="peer", display="Codex")
    assert "r1" in text and "t1" in text and "`peer`" in text
