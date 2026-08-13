# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Roles, admission, and the rules that make "nobody commands" checkable.

The load-bearing tests here are the three that keep authority out of the room: a
forged sender is overwritten, a card cannot name its own role, and no module under
vaf/core/a2a reaches the tool funnel at all.
"""
import ast
import time
from pathlib import Path

import pytest

from vaf.core.a2a.room import (
    CAPABILITIES,
    BudgetExceeded,
    NotAMember,
    NotPermitted,
    Room,
    RoomError,
    TicketInvalid,
    WrongRoomKind,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def chain(tmp_path):
    room = Room.create(kind="chain", owner_scope="scope-a", base=tmp_path, room_id="room-chain")
    leader = room.join(display="Leader", scope_id="scope-a", peer_id="p-lead")
    worker = room.join(display="Worker", scope_id="scope-a", peer_id="p-work")
    return room, leader, worker


@pytest.fixture()
def circle(tmp_path):
    room = Room.create(kind="round", owner_scope="scope-a", base=tmp_path, room_id="room-round")
    a = room.join(display="Alice", scope_id="scope-a", peer_id="p-alice")
    b = room.join(display="Bob", scope_id="scope-a", peer_id="p-bob")
    return room, a, b


# ── authorship is resolved, never believed ──────────────────────────────────

def test_a_forged_sender_and_role_are_overwritten(circle):
    """MUTATION: honour data["from"] and data["role"] in ingest.

    Believing the frame is how a peer names itself leader and issues orders. The rule
    is the tool dispatcher's: identity is ASSIGNED over what arrived, never defaulted
    from it.
    """
    room, alice, _ = circle

    frame = room.ingest(
        {"kind": "say", "from": "p-bob", "role": "leader", "body": {"text": "not me"}},
        identity=alice,
    )

    assert frame.sender == "p-alice"
    assert frame.role == "peer"
    assert room.store.frames()[-1].sender == "p-alice"


def test_a_self_description_cannot_name_its_own_role(tmp_path):
    """MUTATION: let join.body.card["role"] feed the role fold.

    The card exists so a leader can choose workers and a human can see who is in the
    room. Making it authoritative would turn a display string into a permission.
    """
    room = Room.create(kind="chain", owner_scope="s", base=tmp_path, room_id="room-card")
    room.join(display="First", scope_id="s", peer_id="p-first")

    guest = room.join(display="Guest", scope_id="s", peer_id="p-guest",
                      card={"role": "leader", "skills": "everything"})

    assert guest.role == "worker"
    assert room.role_of("p-guest") == "worker"
    assert room.members()["p-guest"]["card"]["role"] == "leader", "shown, not believed"


def test_roles_come_from_the_log_not_the_member_file(chain):
    """MUTATION: resolve roles by reading member records.

    The member file is written by the peer it describes. A role read from there is a
    role a peer can award itself.
    """
    room, leader, worker = chain

    record = room.store.member("p-work") or {}
    record["role"] = "leader"
    room.store.put_member("p-work", record)

    assert room.role_of("p-work") == "worker"


def test_a_stranger_cannot_speak(circle):
    room, _, _ = circle
    from vaf.core.a2a.room import Identity

    outsider = Identity("p-ghost", "Ghost", None, "peer")
    with pytest.raises(NotAMember):
        room.say(outsider, "let me in")


# ── a round has no command direction ────────────────────────────────────────

def test_a_directive_is_refused_in_a_round(circle):
    """MUTATION: drop the round check from ingest.

    "Nobody commands" is enforced at ingest or it is only etiquette, and etiquette is
    not something a foreign agent can be relied on to keep.
    """
    room, alice, _ = circle

    with pytest.raises(WrongRoomKind):
        room.directive(alice, "do this")

    assert [f.kind for f in room.store.frames() if f.kind == "directive"] == []


def test_a_leader_may_direct_in_a_chain(chain):
    room, leader, _ = chain
    frame = room.directive(leader, "collect the logs")
    assert frame.kind == "directive" and frame.role == "leader"


def test_a_worker_may_not_direct_or_recast_roles(chain):
    """MUTATION: give worker the directive or role capability.

    A worker that can direct makes the chain meaningless; one that can re-cast roles
    can promote itself.
    """
    room, _, worker = chain

    with pytest.raises(NotPermitted):
        room.directive(worker, "you work for me now")
    with pytest.raises(NotPermitted):
        room.grant_role(worker, "p-work", "leader")

    assert room.role_of("p-work") == "worker"


def test_a_role_frame_from_a_non_leader_is_ignored_by_the_fold(chain):
    """Belt and braces: even if such a frame reached the log, the fold refuses it.

    Two independent stops, because the capability gate is about what may be WRITTEN
    and the fold is about what may be BELIEVED, and only the second protects a reader
    of a transcript written by somebody else's implementation.
    """
    room, _, worker = chain
    from vaf.core.a2a.frame import Frame

    room.store.append(Frame.new(
        room=room.room_id, sender="p-work", role="worker", kind="role",
        seq=room.store.next_seq("p-work"), lamport=room.store.next_lamport(),
        body={"peer": "p-work", "role": "leader"}, ts=0.0,
    ))

    assert room.role_of("p-work") == "worker"


def test_a_leader_can_recast_a_role(chain):
    room, leader, _ = chain
    room.grant_role(leader, "p-work", "leader")
    assert room.role_of("p-work") == "leader"


def test_the_capability_table_is_the_only_copy():
    """A second copy of the truth table is how two lanes start disagreeing."""
    assert "directive" in CAPABILITIES["leader"]
    assert "directive" not in CAPABILITIES["worker"]
    assert "directive" not in CAPABILITIES["peer"]
    assert "hire" in CAPABILITIES["worker"], "the snowball needs workers to hire"
    assert "hire" not in CAPABILITIES["peer"]


# ── the snowball is a forest, not a promotion ───────────────────────────────

def test_hiring_opens_a_child_room_and_leaves_the_parent_role_alone(chain, tmp_path):
    room, _, worker = chain

    child, frame = room.hire(worker, purpose="read the logs", base=tmp_path)

    assert room.role_of("p-work") == "worker", "no promotion in the parent"
    assert child.manifest["parent_room"] == room.room_id
    assert child.manifest["parent_frame"] == frame.id
    assert child.manifest["depth"] == 1
    child_roles = child.roles()
    assert list(child_roles.values()) == ["leader"], "the hirer leads the child"


def test_the_parent_sees_the_hire_and_the_report_but_not_the_child_transcript(chain, tmp_path):
    """MUTATION: copy the child's frames into the parent.

    This containment is what lets a chain of command grow. Without it every ancestor
    drowns in its descendants' chatter, and a leader four levels up reads a
    conversation it has no way to act on.
    """
    room, _, worker = chain
    child, hire_frame = room.hire(worker, purpose="read the logs", base=tmp_path)
    child_lead = next(iter(child.roles()))
    from vaf.core.a2a.room import Identity
    child_identity = Identity(child_lead, "Worker", "scope-a", "leader")

    child.say(child_identity, "chatting inside the child")
    child.say(child_identity, "still chatting")
    room.report(worker, "logs collected", status="completed")

    parent_kinds = [f.kind for f in room.store.frames()]
    parent_texts = [str((f.body or {}).get("text") or "") for f in room.store.frames()]

    assert "hire" in parent_kinds and "report" in parent_kinds
    assert "chatting inside the child" not in parent_texts
    assert "still chatting" not in parent_texts
    assert len(child.store.frames()) == 3  # join + two says


def test_the_depth_budget_refuses_rather_than_hires_quietly(tmp_path):
    """MUTATION: skip the depth check.

    A snowball with no limit is a fork bomb with a nicer name. Refusing is loud on
    purpose: a silent stop looks like a worker that simply did nothing.
    """
    room = Room.create(kind="chain", owner_scope="s", base=tmp_path,
                       room_id="room-deep", max_depth=1)
    lead = room.join(display="Lead", scope_id="s", peer_id="p-1")
    child, _ = room.hire(lead, purpose="one level", base=tmp_path)

    child_lead_id = next(iter(child.roles()))
    from vaf.core.a2a.room import Identity
    deeper = Identity(child_lead_id, "Lead", "s", "leader")

    with pytest.raises(BudgetExceeded):
        child.hire(deeper, purpose="one level too far", base=tmp_path)


def test_the_children_budget_refuses_at_the_limit(tmp_path):
    room = Room.create(kind="chain", owner_scope="s", base=tmp_path,
                       room_id="room-wide", max_children=2)
    lead = room.join(display="Lead", scope_id="s", peer_id="p-1")

    room.hire(lead, purpose="a", base=tmp_path)
    room.hire(lead, purpose="b", base=tmp_path)
    with pytest.raises(BudgetExceeded):
        room.hire(lead, purpose="c", base=tmp_path)


# ── tenancy and tickets ─────────────────────────────────────────────────────

def test_another_accounts_agent_cannot_join(tmp_path):
    """MUTATION: drop the owner_scope comparison.

    Cross-tenant rooms are off by default because a shared record carrying model text
    across tenants is a leak class this tree has already paid for once.
    """
    room = Room.create(kind="round", owner_scope="scope-a", base=tmp_path, room_id="room-t")
    room.join(display="Owner", scope_id="scope-a", peer_id="p-own")

    with pytest.raises(NotAMember):
        room.join(display="Intruder", scope_id="scope-b", peer_id="p-int")


def test_a_foreign_agent_without_an_account_is_not_caught_by_tenancy(tmp_path):
    """A foreign agent carries no scope and is not a tenant. What bounds it is the
    ticket, which opens exactly one room."""
    room = Room.create(kind="round", owner_scope="scope-a", base=tmp_path, room_id="room-f")
    room.join(display="Owner", scope_id="scope-a", peer_id="p-own")

    guest = room.join(display="Codex", scope_id=None, peer_id="p-guest")
    assert room.role_of(guest.peer_id) == "peer"


def test_a_ticket_opens_one_room_only(tmp_path):
    """MUTATION: stop comparing the ticket's room to this room.

    A bearer credential that works anywhere is a master key. This comparison is the
    whole reason an invite can be pasted into a chat window.
    """
    base = tmp_path
    room_a = Room.create(kind="round", owner_scope="s", base=base, room_id="room-a")
    room_b = Room.create(kind="round", owner_scope="s", base=base, room_id="room-b")
    owner = room_a.join(display="Owner", scope_id="s", peer_id="p-own")
    room_b.join(display="Owner", scope_id="s", peer_id="p-own2")

    ticket = room_a.mint_ticket(owner, display="Guest")
    room_b.store.put_ticket(ticket, room_a.store.ticket(ticket))

    with pytest.raises(TicketInvalid):
        room_b.redeem_ticket(ticket, display="Guest")


def test_a_ticket_is_spent_on_first_use(tmp_path):
    room = Room.create(kind="round", owner_scope="s", base=tmp_path, room_id="room-tick")
    owner = room.join(display="Owner", scope_id="s", peer_id="p-own")
    ticket = room.mint_ticket(owner, display="Guest")

    guest = room.redeem_ticket(ticket)
    assert room.role_of(guest.peer_id) == "peer"

    with pytest.raises(TicketInvalid):
        room.redeem_ticket(ticket)


def test_an_expired_ticket_is_refused_and_removed(tmp_path):
    room = Room.create(kind="round", owner_scope="s", base=tmp_path, room_id="room-exp")
    owner = room.join(display="Owner", scope_id="s", peer_id="p-own")
    ticket = room.mint_ticket(owner, display="Guest", ttl_s=-1.0)

    with pytest.raises(TicketInvalid):
        room.redeem_ticket(ticket)
    assert room.store.ticket(ticket) is None


# ── leaving, staleness, closing ─────────────────────────────────────────────

def test_a_lapsed_lease_is_stale_not_gone(circle):
    """MUTATION: drop a peer whose lease expired.

    A sleeping laptop is not a departure. Only a leave frame removes somebody, and
    only the peer or a leader writes one.
    """
    room, alice, _ = circle
    record = room.store.member("p-alice")
    record["lease"] = time.time() - 10_000
    room.store.put_member("p-alice", record)

    members = room.members()
    assert members["p-alice"]["stale"] is True
    assert room.role_of("p-alice") == "peer"


def test_leaving_removes_a_peer_from_the_fold(circle):
    room, alice, _ = circle
    room.leave(alice, reason="done")
    assert "p-alice" not in room.roles()


def test_a_closed_room_takes_no_new_members(chain):
    room, leader, _ = chain
    room.close(leader, reason="finished")
    with pytest.raises(RoomError):
        room.join(display="Late", scope_id="scope-a", peer_id="p-late")


def test_the_transcript_keeps_the_speaker_apart_from_the_text(circle):
    """The rule voice_turn already follows: a renderer must never have to parse a
    name back out of a message."""
    room, alice, _ = circle
    room.say(alice, "hello everyone")

    row = [r for r in room.transcript() if r["kind"] == "say"][0]
    assert row["text"] == "hello everyone"
    assert row["display"] == "Alice"
    assert "Alice" not in row["text"]


def test_a_report_carries_its_status_and_artifacts(chain):
    room, _, worker = chain
    frame = room.report(worker, "done", status="completed",
                        artifacts=[{"name": "log.txt", "text": "..."}])
    assert frame.body["status"] == "completed"
    assert frame.body["artifacts"][0]["name"] == "log.txt"


# ── E1: the room never reaches the tool funnel ─────────────────────────────

_FORBIDDEN = ("execute_tool", "ToolCaller", "resolve_account_allowlist",
              "compute_user_jail", "user_jail")


def test_no_module_under_a2a_touches_the_tool_funnel():
    """A room is a message bus, not an authority. It hands out no tool.

    MUTATION: import execute_tool into any module under vaf/core/a2a.

    Scope note for whoever tightens this later: the guard covers vaf/core/a2a ONLY.
    The agent's own room tools live in vaf/tools and are the OPPOSITE direction - a
    tool that writes INTO a room under the agent's own bound identity, through the
    normal funnel. Widening this guard to cover them would forbid the feature it is
    meant to protect.
    """
    offenders = {}
    for path in sorted((ROOT / "vaf" / "core" / "a2a").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        hits = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in _FORBIDDEN:
                hits.add(node.id)
            elif isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN:
                hits.add(node.attr)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in _FORBIDDEN:
                        hits.add(alias.name)
        if hits:
            offenders[path.name] = sorted(hits)

    assert not offenders, (
        f"a2a reached into the tool funnel: {offenders}. A room assigns roles, not "
        f"capabilities; a peer that could call a tool through the room would bypass "
        f"the account allowlist and the file jail, both of which read 'no scope' as "
        f"unrestricted."
    )


# ── one wording, four surfaces ──────────────────────────────────────────────

def test_every_kind_reads_as_a_sentence(chain, tmp_path):
    """MUTATION: return entry["text"] for a bookkeeping kind.

    Four surfaces render this transcript - the a2a CLI, the terminal app, the classic
    lane and the agent's own room_read - and a phrase kept in four places is three
    chances to drift.
    """
    from vaf.core.a2a.room import describe

    room, leader, worker = chain
    room.grant_role(leader, "p-work", "worker")
    child, _ = room.hire(worker, purpose="log reading", base=tmp_path)
    room.report(worker, "done", status="completed")
    room.close(leader, reason="finished")

    lines = {r["kind"]: describe(r) for r in room.transcript()}
    assert lines["join"] == "joined"
    assert lines["role"] == "made p-work a worker"
    assert lines["hire"].startswith("opened ") and "log reading" in lines["hire"]
    assert lines["report"] == "[completed] done"
    assert lines["close"] == "closed the room - finished"
    assert all(line.strip() for line in lines.values()), lines


def test_an_unknown_kind_is_described_rather_than_shown_blank():
    from vaf.core.a2a.room import describe

    line = describe({"kind": "celebrate", "body": {}, "text": "hooray", "known": False})
    assert "celebrate" in line and "hooray" in line


def test_no_renderer_hand_rolls_the_wording():
    """The guard that keeps the four surfaces honest."""
    surfaces = [
        ROOT / "vaf" / "cli" / "cmd" / "a2a.py",
        ROOT / "vaf" / "cli" / "tui_app" / "app.py",
        ROOT / "vaf" / "cli" / "cmd" / "run.py",
        ROOT / "vaf" / "tools" / "room_tools.py",
    ]
    for path in surfaces:
        source = path.read_text(encoding="utf-8")
        assert "describe" in source, f"{path.name} renders a transcript without describe()"


# ── what an adversarial pass over the wire plan found ──────────────────────

def test_one_invitation_yields_exactly_one_member_under_concurrency(tmp_path):
    """MUTATION: read the ticket, check it, then delete it.

    Read-then-delete lets two handshakes arriving at the same moment both read a valid
    ticket, both delete it (one deletion silently failing), and both join. One
    invitation, N members - the one thing a single-use bearer credential must not do.
    The rename IS the gate, so exactly one caller can win it.
    """
    import threading

    room = Room.create(kind="round", owner_scope="s", base=tmp_path, room_id="room-race")
    owner = room.join(display="Owner", scope_id="s", peer_id="p-own")
    ticket = room.mint_ticket(owner, display="Guest")

    results, errors = [], []
    barrier = threading.Barrier(8)

    def _redeem():
        barrier.wait()
        try:
            results.append(Room.open("room-race", base=tmp_path).redeem_ticket(ticket))
        except TicketInvalid as e:
            errors.append(e)

    threads = [threading.Thread(target=_redeem) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 1, f"{len(results)} peers joined on one invitation"
    assert len(errors) == 7
    assert len([r for r in room.roles() if r != "p-own"]) == 1


def test_a_spent_ticket_is_kept_rather_than_erased(tmp_path):
    """The claim moves it aside instead of deleting it, so a room can still show that
    an invitation was used and by whom - and so a failed rename is unambiguous."""
    room = Room.create(kind="round", owner_scope="s", base=tmp_path, room_id="room-spent")
    owner = room.join(display="Owner", scope_id="s", peer_id="p-own")
    ticket = room.mint_ticket(owner, display="Guest")

    room.redeem_ticket(ticket)

    assert room.store.ticket(ticket) is None
    assert (room.store.tickets_dir / "spent" / f"{ticket}.json").exists()


def test_a_join_never_overwrites_an_existing_members_mode(tmp_path):
    """MUTATION: let join write the mode it was given, unconditionally.

    The mode is the local user's standing decision about how far their own agent may
    act, and a join is the ONE operation a remote party can cause. A join that carried
    a mode would hand a stranger the switch: rejoin as that peer with
    mode="autonomous", and room text becomes tool execution under the owner's identity.
    """
    room = Room.create(kind="round", owner_scope="s", base=tmp_path, room_id="room-mode2")
    mine = room.join(display="Mine", scope_id="s", peer_id="p-mine", mode="observe")
    assert room.mode_of("p-mine") == "observe"

    room.leave(mine)
    room.join(display="Mine", scope_id="s", peer_id="p-mine", mode="autonomous")

    assert room.mode_of("p-mine") == "observe", "a rejoin raised the local autonomy"


def test_a_connection_can_never_derive_a_local_handle():
    """MUTATION: drop the "remote" lane and let a connection use the agent's key.

    Landing on the local agent's seat would put a stranger's words where the owner's
    own agent speaks from, and every reader would attribute them to it.
    """
    from vaf.core.a2a.room import PARTICIPANT_LANES, derive_peer_id, participant_key

    assert "remote" in PARTICIPANT_LANES
    handles = {lane: derive_peer_id(participant_key(lane, "scope-a"), "room-x")
               for lane in PARTICIPANT_LANES}
    assert len(set(handles.values())) == len(PARTICIPANT_LANES), handles


# ── who is who, when three agents share a name ─────────────────────────────

def test_two_agents_with_the_same_name_are_told_apart(tmp_path):
    """MUTATION: return the display name alone.

    Three agents joining as "Codex" are indistinguishable in a transcript, and a person
    reading it cannot ask one of them anything. The tag is what makes a room legible.
    """
    room = Room.create(kind="round", owner_scope=None, base=tmp_path, room_id="room-lbl")
    room.join(display="Codex", scope_id=None, peer_id="p-aaa1")
    room.join(display="Codex", scope_id=None, peer_id="p-bbb2")
    room.join(display="VAF", scope_id=None, peer_id="p-ccc3")

    labels = room.labels()
    assert len(set(labels.values())) == 3, labels
    assert all(label.startswith(("Codex", "VAF")) for label in labels.values())
    assert labels["p-ccc3"].startswith("VAF")


def test_a_label_is_stable_and_needs_nothing_written_down(tmp_path):
    """MUTATION: hand out labels from a counter stored in the room.

    A counter is shared, incrementing state written on every join - the exact shape this
    store spends its design avoiding. Derived from the handle, the label is the same
    after a restart and after a rejoin, with nothing to keep in step.
    """
    from vaf.core.a2a.room import peer_tag

    room = Room.create(kind="round", owner_scope=None, base=tmp_path, room_id="room-stable")
    room.join(display="Codex", scope_id=None, peer_id="p-aaa1")

    first = room.label_for("p-aaa1")
    reopened = Room.open("room-stable", base=tmp_path)
    assert reopened.label_for("p-aaa1") == first
    assert peer_tag("p-aaa1") == peer_tag("p-aaa1")
    assert peer_tag("p-aaa1") != peer_tag("p-bbb2")


def test_a_clash_takes_more_digits_rather_than_giving_up(tmp_path):
    """Uniqueness inside the room is the property that matters: a room without name
    collisions has no undeliverable mentions, which is the ambiguity rule dissolving
    rather than being worked around."""
    from vaf.core.a2a import room as room_mod

    room = Room.create(kind="round", owner_scope=None, base=tmp_path, room_id="room-clash")
    room.join(display="Codex", scope_id=None, peer_id="p-one")
    room.join(display="Codex", scope_id=None, peer_id="p-two")

    # Force the short tag to collide, and demand the room widens instead of repeating.
    original = room_mod.peer_tag
    try:
        room_mod.peer_tag = lambda peer_id, *, width=2: ("07" if width == 2
                                                         else original(peer_id, width=width))
        labels = room.labels()
    finally:
        room_mod.peer_tag = original

    assert len(set(labels.values())) == 2, labels


# ── the label travels with the transcript ──────────────────────────────────

def test_the_transcript_carries_the_tagged_label_not_only_the_bare_name(tmp_path):
    """MUTATION: leave the label out and let each surface tag names for itself.

    Four surfaces render this transcript. A surface that built the label on its own
    would call the same peer something different from the next one, and a mention typed
    against one of those names would not resolve on the other.
    """
    room = Room.create(kind="round", owner_scope=None, base=tmp_path, room_id="room-tr")
    one = room.join(display="Codex", scope_id=None, peer_id="p-aaa1")
    room.join(display="Codex", scope_id=None, peer_id="p-bbb2")
    room.say(one, "hello")

    rows = [row for row in room.transcript() if row["kind"] == "say"]
    assert rows[0]["display"] == "Codex", "the bare join name is still available"
    assert rows[0]["label"] == room.label_for("p-aaa1")
    assert rows[0]["label"] != rows[0]["display"], "the label carries no tag"


def test_a_mention_resolves_against_the_name_a_reader_actually_sees(tmp_path):
    """MUTATION: match the bare display only.

    Every surface SHOWS the tagged label, so "Codex51" is what a reader has in front of
    them when they type a mention. Matching only the bare name makes the addressed
    message arrive at the ROOM instead of at the peer it named - and nothing reports it,
    because sending to the room is a perfectly valid thing to do.
    """
    room = Room.create(kind="round", owner_scope=None, base=tmp_path, room_id="room-mnt")
    room.join(display="Codex", scope_id=None, peer_id="p-aaa1")
    room.join(display="Codex", scope_id=None, peer_id="p-bbb2")

    label = room.label_for("p-aaa1")
    assert room.peer_by_display(label) == "p-aaa1"
    assert room.address_from_mention(f"@{label} can you look") == {"peer": "p-aaa1"}


def test_an_ambiguous_bare_name_is_still_refused(tmp_path):
    """The label lane must not soften the rule above it: two members called "Codex"
    and a message aimed at "@Codex" is still a message that must not be delivered to a
    coin toss."""
    room = Room.create(kind="round", owner_scope=None, base=tmp_path, room_id="room-amb")
    room.join(display="Codex", scope_id=None, peer_id="p-aaa1")
    room.join(display="Codex", scope_id=None, peer_id="p-bbb2")

    assert room.peer_by_display("Codex") is None
    assert room.address_from_mention("@Codex hello") is None
