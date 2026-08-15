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
    The exclusive CLAIM is the gate, so exactly one caller can win it.
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


def test_the_claim_holds_even_where_a_rename_is_not_a_gate(tmp_path, monkeypatch):
    """MUTATION: make the rename the gate again.

    Windows CI measured three peers joining on one invitation while Linux and
    macOS held: os.replace is not the mutex that code assumed, and a race that
    only shows on one platform is the worst kind to rely on. The gate is an
    exclusive create now, which the kernel refuses to grant twice everywhere.

    The Windows failure is reproduced HERE, on any platform, by making every
    rename succeed - which is what that platform effectively did. With the old
    rename-as-gate this test yields several winners; with the claim, one.
    """
    import os as _os
    import threading

    room = Room.create(kind="round", owner_scope="s", base=tmp_path,
                       room_id="room-norename")
    owner = room.join(display="Owner", scope_id="s", peer_id="p-own")
    ticket = room.mint_ticket(owner, display="Guest")

    import vaf.core.a2a.store as store_mod
    real_replace = _os.replace

    def _everyone_wins(src, dst):
        """A rename that never refuses - the Windows behaviour, made portable."""
        try:
            real_replace(src, dst)
        except OSError:
            pass

    monkeypatch.setattr(store_mod.os, "replace", _everyone_wins)

    results, errors = [], []
    barrier = threading.Barrier(8)

    def _redeem():
        barrier.wait()
        try:
            results.append(Room.open("room-norename", base=tmp_path)
                           .redeem_ticket(ticket))
        except TicketInvalid as e:
            errors.append(e)

    threads = [threading.Thread(target=_redeem) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 1, (
        f"{len(results)} peers joined on one invitation - the gate depends on "
        "the rename again, which does not hold on every platform")
    assert len(errors) == 7


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


# ── the audit view ─────────────────────────────────────────────────────────

def test_the_audit_says_who_did_what_and_when(tmp_path):
    from vaf.core.a2a.room import audit

    room = Room.create(kind="chain", owner_scope=None, base=tmp_path, room_id="room-aud")
    leader = room.join(display="VAF", scope_id=None, peer_id="p-lead")
    worker = room.join(display="Codex", scope_id=None, peer_id="p-work")
    room.ingest({"kind": "directive", "body": {"text": "collect the logs"}}, identity=leader)
    room.ingest({"kind": "report", "body": {"text": "done", "status": "completed"}},
                identity=worker)
    room.leave(worker, reason="finished")

    events = [row["event"] for row in audit(room)]
    assert events == ["joined", "joined", "instruction sent", "report sent", "left"]

    rows = audit(room)
    assert rows[3]["detail"] == "completed", "a report's status is the point of it"
    assert rows[4]["detail"] == "finished"
    assert rows[1]["label"] == room.label_for("p-work")


def test_the_audit_carries_no_message_text(tmp_path):
    """MUTATION: put the body, or the text, into the audit row.

    An audit answers "did the worker report before the leader asked" and "when did
    this one leave". Reading what was actually said is the transcript's job and a
    different question - keeping them apart is what lets an audit be shown to somebody
    who has no business reading the conversation.
    """
    from vaf.core.a2a.room import audit

    room = Room.create(kind="round", owner_scope=None, base=tmp_path, room_id="room-quiet")
    one = room.join(display="Alice", scope_id=None, peer_id="p-a")
    room.say(one, "the password is hunter2")

    blob = repr(audit(room))
    assert "hunter2" not in blob
    assert all("text" not in row and "body" not in row for row in audit(room))


def test_an_unknown_kind_still_appears_in_the_audit(tmp_path):
    """MUTATION: skip a kind the table does not know.

    A gap in an audit is worse than a line nobody recognises: the first is invisible,
    the second asks a question. It is also the frame rule one level up - an unknown
    kind is shown, never dropped.
    """
    from vaf.core.a2a.room import audit

    room = Room.create(kind="round", owner_scope=None, base=tmp_path, room_id="room-odd")
    one = room.join(display="Alice", scope_id=None, peer_id="p-a")
    from vaf.core.a2a.frame import Frame
    room.store.append(Frame.new(
        room=room.room_id, sender=one.peer_id, role="peer", kind="telemetry",
        seq=room.store.next_seq(one.peer_id), lamport=room.store.next_lamport(),
        body={"cpu": 12}))

    rows = audit(room)
    assert any(row["kind"] == "telemetry" for row in rows), "an unknown act vanished"


def test_an_addressed_message_is_visible_as_a_fact_without_its_wording(tmp_path):
    from vaf.core.a2a.room import audit

    room = Room.create(kind="round", owner_scope=None, base=tmp_path, room_id="room-aim")
    one = room.join(display="Alice", scope_id=None, peer_id="p-a")
    two = room.join(display="Bob", scope_id=None, peer_id="p-b")
    room.ingest({"kind": "say", "body": {"text": "secret plan"},
                 "to": {"peer": two.peer_id}}, identity=one)

    row = audit(room)[-1]
    assert row["event"] == "message sent"
    assert row["detail"] == f"to {room.label_for('p-b')}"
    assert "secret plan" not in repr(row)


def test_the_audit_reads_the_log_and_stores_nothing(tmp_path):
    """MUTATION: keep a second record of events.

    Two records can disagree, and then somebody has to decide which one lied. This one
    cannot, because it IS the transcript read a different way - every row it returns
    has a frame behind it.
    """
    from vaf.core.a2a.room import audit

    room = Room.create(kind="round", owner_scope=None, base=tmp_path, room_id="room-one-truth")
    one = room.join(display="Alice", scope_id=None, peer_id="p-a")
    room.say(one, "hello")

    lamports = [row["lamport"] for row in audit(room)]
    assert lamports == [f.lamport for f in room.store.read_since(0)]

    before = sorted(p.name for p in tmp_path.rglob("*") if p.is_file())
    audit(room)
    assert sorted(p.name for p in tmp_path.rglob("*") if p.is_file()) == before


# ── the advisory clock belongs to the protocol ─────────────────────────────

def test_a_broken_timestamp_renders_as_nothing_not_as_a_wrong_time(tmp_path):
    """MUTATION: fall back to "now" when ts cannot be read.

    A missing timestamp in a transcript is a gap somebody notices. A wrong one is a
    fact somebody believes - and in a room whose whole ordering rule is "never trust
    the clock", inventing one is the worst available answer.
    """
    from vaf.core.a2a.room import frame_clock

    assert len(frame_clock(1765000000.0)) == 5
    for bad in (None, "", "nope", float("nan"), object()):
        assert frame_clock(bad) == ""


# ── the host may end a room whatever its role ──────────────────────────────

def test_the_host_can_close_a_round_it_has_no_role_to_close(tmp_path):
    """MUTATION: leave closing to the capability table alone.

    A round has NO leader by design, and only a leader may emit `close`. Without a
    host rule the person whose machine stores the room could never end a conversation
    living in their own files - which is exactly the state the first live round was
    in: "round, you are peer", and no way out.
    """
    room = Room.create(kind="round", owner_scope="scope-owner", base=tmp_path,
                       room_id="room-host")
    host = room.join(display="Me", scope_id="scope-owner", peer_id="p-host")

    assert room.role_of("p-host") == "peer"
    assert not room.may("peer", "close"), "a peer gained the capability instead"
    room.close(host, reason=Room.TERMINATED_BY_USER)
    assert room.closed


def test_a_guest_can_never_close_the_room_it_was_invited_into(tmp_path):
    """MUTATION: key the host rule on anything a guest controls.

    A redeemed ticket sets the guest's scope to None on purpose. The host check reads
    the TENANT, so nothing a stranger presents can make it true - and a guest that
    could close the room could end everybody else's work on its way out.
    """
    room = Room.create(kind="round", owner_scope="scope-owner", base=tmp_path,
                       room_id="room-guest")
    room.join(display="Me", scope_id="scope-owner", peer_id="p-host")
    guest = room.join(display="Codex", scope_id=None, peer_id="p-guest")

    assert room.is_host(guest) is False
    with pytest.raises(NotPermitted):
        room.close(guest, reason="bye")
    assert not room.closed


def test_a_room_with_no_owner_has_no_host(tmp_path):
    """Fail closed: an unowned room does not hand the host power to whoever asks."""
    room = Room.create(kind="round", owner_scope=None, base=tmp_path, room_id="room-anon")
    someone = room.join(display="A", scope_id="scope-a", peer_id="p-a")

    assert room.is_host(someone) is False


def test_a_leader_still_closes_its_own_chain(tmp_path):
    """The host rule is an addition, not a replacement."""
    room = Room.create(kind="chain", owner_scope=None, base=tmp_path, room_id="room-lead")
    leader = room.join(display="L", scope_id="scope-x", peer_id="p-l")
    room.close(leader, reason="done")
    assert room.closed


def test_everyone_reads_the_same_sentence_when_a_user_ends_it(tmp_path):
    """MUTATION: let each surface phrase the closing reason itself.

    It is the last line anybody reads in that transcript, and it reaches agents that
    are not on this machine. Three wordings would mean three different accounts of why
    their access stopped.
    """
    from vaf.core.a2a.room import describe

    room = Room.create(kind="round", owner_scope="scope-owner", base=tmp_path,
                       room_id="room-last-word")
    host = room.join(display="Me", scope_id="scope-owner", peer_id="p-host")
    room.close(host, reason=Room.TERMINATED_BY_USER)

    last = room.transcript()[-1]
    assert last["kind"] == "close"
    assert Room.TERMINATED_BY_USER in describe(last)
    assert "terminated by the user or Host AI system" in Room.TERMINATED_BY_USER


def test_a_closed_room_takes_nothing_more(tmp_path):
    """The point of closing, from the agents' side: their access to write is gone."""
    room = Room.create(kind="round", owner_scope="scope-owner", base=tmp_path,
                       room_id="room-shut")
    host = room.join(display="Me", scope_id="scope-owner", peer_id="p-host")
    guest = room.join(display="Codex", scope_id=None, peer_id="p-guest")
    room.close(host, reason=Room.TERMINATED_BY_USER)

    with pytest.raises(RoomError):
        room.say(guest, "still here?")


def test_closing_is_what_actually_revokes_the_ability_to_write(tmp_path):
    """MUTATION: compute `closed` and never check it.

    That was the real state until this test asked what closing STOPPED: the transcript
    said closed, every surface showed closed, the CLI help promised "nothing more can
    be written" - and writes were still accepted. A room that tells its participants
    their access is gone while leaving it exactly where it was is worse than one that
    never claimed to close at all.
    """
    from vaf.core.a2a.room import RoomClosed

    room = Room.create(kind="chain", owner_scope="scope-owner", base=tmp_path,
                       room_id="room-revoked")
    host = room.join(display="Me", scope_id="scope-owner", peer_id="p-host")
    worker = room.join(display="Codex", scope_id=None, peer_id="p-w")
    room.close(host, reason=Room.TERMINATED_BY_USER)

    for act in (lambda: room.say(worker, "still here?"),
                lambda: room.ingest({"kind": "report", "body": {"text": "done"}},
                                    identity=worker),
                lambda: room.say(host, "and me?")):
        with pytest.raises(RoomClosed):
            act()

    # and it stays READABLE, which is the other half of the promise
    assert [r["kind"] for r in room.transcript()][-1] == "close"


def test_a_closed_room_cannot_be_closed_twice(tmp_path):
    from vaf.core.a2a.room import RoomClosed

    room = Room.create(kind="round", owner_scope="scope-owner", base=tmp_path,
                       room_id="room-twice-shut")
    host = room.join(display="Me", scope_id="scope-owner", peer_id="p-host")
    room.close(host, reason=Room.TERMINATED_BY_USER)

    with pytest.raises(RoomClosed):
        room.close(host, reason="again")
    assert sum(1 for r in room.transcript() if r["kind"] == "close") == 1


# ── removing somebody, and who cannot be removed ───────────────────────────

def test_a_host_removes_a_guest_and_the_guest_is_out(tmp_path):
    """MUTATION: write the removal into the removed peer's lane.

    One writer per lane is the property the whole store rests on. A removal that wrote
    into somebody else's directory would trade it away for a convenience - and two
    writers in one lane is the lost update the design exists to avoid.
    """
    room = Room.create(kind="round", owner_scope="scope-owner", base=tmp_path,
                       room_id="room-kick")
    from vaf.core.a2a.room import derive_peer_id, participant_key
    host_peer = derive_peer_id(participant_key("cli", "scope-owner"), "room-kick")
    host = room.join(display="Me", scope_id="scope-owner", peer_id=host_peer)
    guest = room.join(display="Codex", scope_id=None, peer_id="p-guest")

    frame = room.kick(host, "p-guest", reason="done here")

    assert room.role_of("p-guest") is None
    assert frame.sender == host_peer, "the removal was written into another peer's lane"
    with pytest.raises(RoomError):
        room.say(guest, "am I still here?")


def test_the_rooms_own_host_can_never_be_kicked(tmp_path):
    """MUTATION: let the protection be read from anything a peer writes.

    The member file is written by the member, so a protection stored there would be a
    peer naming its own. These handles are DERIVED from the owner's scope and the room
    id, so nobody else can land on one.
    """
    from vaf.core.a2a.room import derive_peer_id, participant_key

    room = Room.create(kind="chain", owner_scope="scope-owner", base=tmp_path,
                       room_id="room-protect")
    agent_peer = derive_peer_id(participant_key("agent", "scope-owner"), "room-protect")
    room.join(display="VAF", scope_id="scope-owner", peer_id=agent_peer)
    leader = room.join(display="Boss", scope_id="scope-owner", peer_id="p-boss")

    assert agent_peer in room.host_peers()
    with pytest.raises(NotPermitted) as refusal:
        room.kick(leader, agent_peer)
    assert "closing the room" in str(refusal.value), (
        "the refusal does not say what to do instead")
    assert room.role_of(agent_peer) is not None


def test_a_peer_cannot_kick_anybody(tmp_path):
    room = Room.create(kind="round", owner_scope=None, base=tmp_path, room_id="room-nokick")
    one = room.join(display="A", scope_id=None, peer_id="p-a")
    room.join(display="B", scope_id=None, peer_id="p-b")

    with pytest.raises(NotPermitted):
        room.kick(one, "p-b")
    assert room.role_of("p-b") == "peer"


def test_kicking_yourself_and_kicking_a_stranger_are_both_refused(tmp_path):
    from vaf.core.a2a.room import derive_peer_id, participant_key

    room = Room.create(kind="round", owner_scope="scope-owner", base=tmp_path,
                       room_id="room-odd-kick")
    host_peer = derive_peer_id(participant_key("cli", "scope-owner"), "room-odd-kick")
    host = room.join(display="Me", scope_id="scope-owner", peer_id=host_peer)

    with pytest.raises(NotPermitted):
        room.kick(host, host_peer)
    with pytest.raises(NotAMember):
        room.kick(host, "p-nobody")


def test_a_removed_peer_can_be_invited_back(tmp_path):
    """Removal is not a ban. The fold is over the log, so a later join simply resolves
    again - which is what makes membership recomputable rather than stateful."""
    from vaf.core.a2a.room import derive_peer_id, participant_key

    room = Room.create(kind="round", owner_scope="scope-owner", base=tmp_path,
                       room_id="room-return")
    host_peer = derive_peer_id(participant_key("cli", "scope-owner"), "room-return")
    host = room.join(display="Me", scope_id="scope-owner", peer_id=host_peer)
    room.join(display="Codex", scope_id=None, peer_id="p-back")
    room.kick(host, "p-back")
    assert room.role_of("p-back") is None

    room.join(display="Codex", scope_id=None, peer_id="p-back")
    assert room.role_of("p-back") == "peer"


def test_a_removal_reads_as_a_removal_everywhere(tmp_path):
    from vaf.core.a2a.room import audit, derive_peer_id, describe, participant_key

    room = Room.create(kind="round", owner_scope="scope-owner", base=tmp_path,
                       room_id="room-said")
    host_peer = derive_peer_id(participant_key("cli", "scope-owner"), "room-said")
    host = room.join(display="Me", scope_id="scope-owner", peer_id=host_peer)
    room.join(display="Codex", scope_id=None, peer_id="p-gone")
    room.kick(host, "p-gone", reason="finished")

    last = room.transcript()[-1]
    assert "removed" in describe(last) and "finished" in describe(last)
    assert audit(room)[-1]["event"] == "removed somebody"


# ── a room's owner is a TENANT, not a participant key ──────────────────────

def test_a_room_recorded_with_a_participant_key_still_has_its_host(tmp_path):
    """MUTATION: read owner_scope raw.

    `vaf a2a create` recorded `participant_key("cli")` where a tenant belongs. One
    prefix apart, and invisible until something derived from it: the host handles came
    out belonging to nobody, so the room had NO host - its own opener could not close
    it and could not remove anybody, and the tenant check compared two strings that can
    never match. Found by reading a live room rather than a test fixture.

    Healed on READ, because the rooms already on disk hold conversations that are still
    going and a migration pass over somebody's live rooms is the worse answer.
    """
    from vaf.core.a2a.room import derive_peer_id, owner_tenant, participant_key

    room = Room.create(kind="round", owner_scope="cli:scope-owner", base=tmp_path,
                       room_id="room-legacy-owner")
    peer = derive_peer_id(participant_key("cli", "scope-owner"), "room-legacy-owner")
    host = room.join(display="Me", scope_id="scope-owner", peer_id=peer)

    assert owner_tenant("cli:scope-owner") == "scope-owner"
    assert peer in room.host_peers(), "the host handles belong to nobody"
    assert room.is_host(host) is True
    room.close(host, reason=Room.TERMINATED_BY_USER)
    assert room.closed


def test_the_command_records_the_tenant_and_not_the_lane():
    """MUTATION: pass the participant key again.

    The writer is fixed as well as the reader: a room opened from now on records what
    it should, and the healing above is for the ones already written.
    """
    source = (ROOT / "vaf" / "cli" / "cmd" / "a2a.py").read_text(encoding="utf-8")
    create = source.split("\ndef create(")[1].split("\n@app.command()")[0]

    assert "owner_scope=_scope()" in create
    assert "owner_scope=_key()" not in create, "the lane is being stored as the tenant"


def test_a_tenant_check_is_not_fooled_by_the_prefix(tmp_path):
    """The same confusion in the other direction: the owner's own agent could not join
    a room its own terminal had opened, because the check compared "scope" against
    "cli:scope" and refused."""
    room = Room.create(kind="round", owner_scope="cli:scope-owner", base=tmp_path,
                       room_id="room-legacy-join")
    identity = room.join(display="Agent", scope_id="scope-owner", peer_id="p-agent")
    assert identity.role == "peer"


# ── an agent says what it is good for ──────────────────────────────────────

def test_a_join_can_carry_what_the_joiner_is_good_for(tmp_path):
    """The protocol has carried this slot since the first release and nothing filled
    it, so every panel and every foreign agent saw a name, a role, and nothing about
    what the thing behind the name can DO. A room is agents deciding who to ask."""
    room = Room.create(kind="round", owner_scope=None, base=tmp_path, room_id="room-card")
    one = room.join(display="Codex", scope_id=None, peer_id="p-c",
                    card={"kind": "VAF agent", "skills": "writes and reviews Python"})

    assert room.members()["p-c"]["card"]["skills"] == "writes and reviews Python"
    joined = [e for e in room.transcript() if e["kind"] == "join"][0]
    assert joined["body"]["card"]["skills"] == "writes and reviews Python", (
        "the card never reached the transcript, so no other agent can read it")
    assert one.role == "peer"


def test_a_card_still_cannot_name_its_own_role(tmp_path):
    """MUTATION: read the role out of the card.

    Self-description is displayed as self-description. The moment it is read as
    permission, every foreign agent can promote itself by describing itself.
    """
    room = Room.create(kind="chain", owner_scope=None, base=tmp_path, room_id="room-claim")
    room.join(display="Boss", scope_id=None, peer_id="p-boss")
    liar = room.join(display="Codex", scope_id=None, peer_id="p-liar",
                     card={"kind": "agent", "role": "leader", "skills": "everything"})

    assert liar.role == "worker"
    assert room.role_of("p-liar") == "worker"


def test_the_host_is_recognised_from_a_looked_up_identity(tmp_path):
    """MUTATION: decide the host from the identity's scope alone.

    `identity_for` builds an Identity out of the LOG and leaves scope_id None, so every
    caller that looked a member up rather than joining them got False. The browser is
    one of those, which is how the host of a room stayed unable to close or clear it
    through two rounds of fixing exactly that. The handle answers the same question and
    is always present.
    """
    from vaf.core.a2a.room import derive_peer_id, participant_key

    room = Room.create(kind="round", owner_scope="scope-owner", base=tmp_path,
                       room_id="room-lookup")
    key = participant_key("cli", "scope-owner")
    room.join(display="Me", scope_id="scope-owner", peer_id=derive_peer_id(key, "room-lookup"))

    looked_up = room.identity_for(key)
    assert looked_up is not None
    assert looked_up.scope_id is None, "the premise of this test changed"
    assert room.is_host(looked_up) is True

    room.close(looked_up, reason=Room.TERMINATED_BY_USER)
    assert room.closed


def test_a_guest_is_never_recognised_by_a_handle_it_holds(tmp_path):
    """The handle is safe to decide on because nobody else can hold one: a guest's is
    minted at random, and a derived one needs the owner's own tenant."""
    room = Room.create(kind="round", owner_scope="scope-owner", base=tmp_path,
                       room_id="room-guest-handle")
    guest = room.join(display="Codex", scope_id=None, peer_id="p-guest")

    assert guest.peer_id not in room.host_peers()
    assert room.is_host(guest) is False


# ── deleting a room, the way a chat is deleted ─────────────────────────────

def test_the_host_deletes_a_room_and_it_is_gone(tmp_path):
    """A chat is deleted by removing its file. A room had no equivalent at all, so the
    only thing a bin could offer was CLOSING - which leaves it on disk and, before the
    list learned to skip closed rooms, on screen.
    """
    from vaf.core.a2a.room import derive_peer_id, participant_key
    from vaf.core.a2a.store import RoomStore, StoreError

    room = Room.create(kind="round", owner_scope="scope-owner", base=tmp_path,
                       room_id="room-gone")
    peer = derive_peer_id(participant_key("cli", "scope-owner"), "room-gone")
    host = room.join(display="Me", scope_id="scope-owner", peer_id=peer)
    room.join(display="Codex", scope_id=None, peer_id="p-guest")

    assert room.delete(host) is True
    assert not RoomStore("room-gone", base=tmp_path).exists()
    with pytest.raises(StoreError):
        Room.open("room-gone", base=tmp_path)


def test_deleting_closes_first_so_the_others_learn_why(tmp_path):
    """MUTATION: remove the files without closing.

    On one machine the two happen in the same breath. Over a wire they do not: a peer
    that reads the room in between is told the conversation ended rather than finding
    one that is simply not there, and the order is what makes that survivable.
    """
    import vaf.core.a2a.room as room_mod
    from vaf.core.a2a.room import derive_peer_id, participant_key

    room = Room.create(kind="round", owner_scope="scope-owner", base=tmp_path,
                       room_id="room-farewell")
    peer = derive_peer_id(participant_key("cli", "scope-owner"), "room-farewell")
    host = room.join(display="Me", scope_id="scope-owner", peer_id=peer)

    seen = {}
    original = room_mod.RoomStore.destroy

    def _spy(self):
        seen["closed_before_delete"] = any(
            f.kind == "close" for f in self.frames())
        return original(self)

    room_mod.RoomStore.destroy = _spy
    try:
        room.delete(host)
    finally:
        room_mod.RoomStore.destroy = original

    assert seen["closed_before_delete"] is True


def test_a_guest_can_never_delete_the_room_it_was_invited_into(tmp_path):
    """MUTATION: allow anybody in the room to delete it.

    Blunter than kicking: this removes somebody else's transcript as well as your own.
    A guest that could do it could end everybody's work on its way out and leave no
    record it had been there.
    """
    from vaf.core.a2a.store import RoomStore

    room = Room.create(kind="round", owner_scope="scope-owner", base=tmp_path,
                       room_id="room-safe")
    room.join(display="Me", scope_id="scope-owner", peer_id="p-host")
    guest = room.join(display="Codex", scope_id=None, peer_id="p-guest")

    with pytest.raises(NotPermitted) as refusal:
        room.delete(guest)
    assert "leaving is" in str(refusal.value), "the refusal does not say what a guest CAN do"
    assert RoomStore("room-safe", base=tmp_path).exists()


def test_deleting_an_already_closed_room_does_not_close_it_twice(tmp_path):
    from vaf.core.a2a.room import derive_peer_id, participant_key

    room = Room.create(kind="round", owner_scope="scope-owner", base=tmp_path,
                       room_id="room-shut-then-gone")
    peer = derive_peer_id(participant_key("cli", "scope-owner"), "room-shut-then-gone")
    host = room.join(display="Me", scope_id="scope-owner", peer_id=peer)
    room.close(host, reason=Room.TERMINATED_BY_USER)

    assert room.delete(host) is True


# ── a name only gets a number when it collides ─────────────────────────────

def test_a_unique_name_is_left_alone(tmp_path):
    """MUTATION: tag every name.

    That WAS the rule, and it made "Nobel" into "Nobel88" for no reason a reader could
    see - the name was already the only one in the room. A tag answers a COLLISION, so
    it shows up when there is one and not before.
    """
    room = Room.create(kind="round", owner_scope=None, base=tmp_path, room_id="room-solo")
    room.join(display="Nobel", scope_id=None, peer_id="p-n")
    room.join(display="Claude", scope_id=None, peer_id="p-c")

    assert room.label_for("p-n") == "Nobel"
    assert room.label_for("p-c") == "Claude"


def test_a_collision_is_numbered_from_one(tmp_path):
    """Small numbers rather than a digest: a person reads them back to somebody else,
    and "@Codex2" survives being said out loud."""
    room = Room.create(kind="round", owner_scope=None, base=tmp_path, room_id="room-two")
    room.join(display="Codex", scope_id=None, peer_id="p-aaa")
    room.join(display="Codex", scope_id=None, peer_id="p-bbb")
    room.join(display="Nobel", scope_id=None, peer_id="p-ccc")

    labels = room.labels()
    assert sorted(labels[p] for p in ("p-aaa", "p-bbb")) == ["Codex1", "Codex2"]
    assert labels["p-ccc"] == "Nobel", "an uninvolved name was renamed by somebody else's clash"


def test_a_numbered_name_still_resolves_a_mention(tmp_path):
    room = Room.create(kind="round", owner_scope=None, base=tmp_path, room_id="room-mention2")
    room.join(display="Codex", scope_id=None, peer_id="p-aaa")
    room.join(display="Codex", scope_id=None, peer_id="p-bbb")

    label = room.label_for("p-aaa")
    assert room.address_from_mention(f"@{label} look at this") == {"peer": "p-aaa"}


def test_a_later_arrival_does_not_rename_the_ones_already_talking(tmp_path):
    """Stable because the order comes from the handles, which do not move. Renaming a
    peer somebody is mid-conversation with would break every mention aimed at it."""
    room = Room.create(kind="round", owner_scope=None, base=tmp_path, room_id="room-grow")
    room.join(display="Codex", scope_id=None, peer_id="p-aaa")
    room.join(display="Codex", scope_id=None, peer_id="p-bbb")
    before = dict(room.labels())

    room.join(display="Codex", scope_id=None, peer_id="p-zzz")
    after = room.labels()
    assert after["p-aaa"] == before["p-aaa"] and after["p-bbb"] == before["p-bbb"]
    assert after["p-zzz"] == "Codex3"


def test_the_terminal_appears_under_the_account_name():
    """MUTATION: keep the literal "terminal".

    That is a LANE, not a person, so the machine owner sat in the room called
    "terminal" beside agents that had names. The account already knows what to call
    them.
    """
    source = (ROOT / "vaf" / "cli" / "cmd" / "a2a.py").read_text(encoding="utf-8")

    assert "def _display()" in source
    assert 'display=display or "terminal"' not in source
    assert source.count("_display()") >= 3


# ── saying what you can do, after you already joined ───────────────────────

def test_a_member_can_say_what_it_can_do_after_joining(tmp_path):
    """MUTATION: keep the card writable only at join.

    That was the state, so anybody who arrived without one read "said nothing about
    what it can do" forever - in a room whose whole point is agents deciding who to
    ask. The same held for the name: a peer that joined as "terminal" could never
    become the person behind it.
    """
    room = Room.create(kind="round", owner_scope=None, base=tmp_path, room_id="room-intro")
    peer = room.join(display="terminal", scope_id=None, peer_id="p-late")

    assert not (room.members()["p-late"].get("card") or {})
    room.introduce(peer, display="Alice",
                   card={"kind": "terminal", "skills": "reads logs, runs the deploy"})

    record = room.members()["p-late"]
    assert record["display"] == "Alice"
    assert record["card"]["skills"] == "reads logs, runs the deploy"
    assert room.label_for("p-late") == "Alice"


def test_introducing_writes_only_your_own_file(tmp_path):
    """One writer per lane is what the whole store rests on. Self-description that
    could touch somebody else's record would trade it away for a convenience."""
    room = Room.create(kind="round", owner_scope=None, base=tmp_path, room_id="room-own")
    one = room.join(display="A", scope_id=None, peer_id="p-a")
    room.join(display="B", scope_id=None, peer_id="p-b")

    room.introduce(one, display="Alice")
    assert room.members()["p-b"]["display"] == "B"


def test_a_stranger_to_the_room_cannot_introduce_itself_into_it(tmp_path):
    from vaf.core.a2a.room import Identity

    room = Room.create(kind="round", owner_scope=None, base=tmp_path, room_id="room-nomem")
    with pytest.raises(NotAMember):
        room.introduce(Identity("p-ghost", "Ghost", None, "peer"), display="Ghost")


def test_a_card_added_later_still_cannot_name_a_role(tmp_path):
    """MUTATION: read the role out of the card on update.

    The join path already refuses this. An update path that did not would be the same
    hole through a second door.
    """
    room = Room.create(kind="chain", owner_scope=None, base=tmp_path, room_id="room-late-claim")
    room.join(display="Boss", scope_id=None, peer_id="p-boss")
    worker = room.join(display="W", scope_id=None, peer_id="p-w")

    room.introduce(worker, card={"role": "leader", "skills": "everything"})
    assert room.role_of("p-w") == "worker"


# ── the shared folder ───────────────────────────────────────────────────────

def test_the_shared_folder_lives_where_chat_workspaces_live(tmp_path, monkeypatch):
    """MUTATION: anchor the folder anywhere else, or skip the label.

    The room's folder is a chat workspace to every other part of the product: the
    browser window, the upload lane and the delete lane all resolve it through the
    same code a chat's folder goes through. That only holds while it lives where
    chat workspaces live - under the owning account's projects root, named by the
    room id - and while its label carries the topic, so the workspace browser shows
    a conversation name rather than a hex id.
    """
    from vaf.core.platform import Platform
    monkeypatch.setattr(Platform, "documents_dir", staticmethod(lambda: tmp_path))

    room = Room.create(kind="round", owner_scope="scope-a", base=tmp_path,
                       room_id="room-ws", topic="Deploy talk")
    expected = tmp_path / "VAF_Projects" / "scopea" / "room-ws"
    assert room.workspace_dir() == expected
    assert not expected.exists(), "asking for the path must not create it"

    created = room.workspace_dir(create=True)
    assert created == expected and created.is_dir()

    from vaf.core.session import read_workspace_label
    assert read_workspace_label(created) == "Deploy talk"


def test_a_room_with_no_owner_tenant_has_no_folder(tmp_path, monkeypatch):
    """A guest-hosted legacy manifest has no account to anchor a path under - the
    honest answer is None, never a folder in nobody's tree."""
    from vaf.core.platform import Platform
    monkeypatch.setattr(Platform, "documents_dir", staticmethod(lambda: tmp_path))

    room = Room.create(kind="round", owner_scope=None, base=tmp_path, room_id="room-noowner")
    assert room.workspace_dir(create=True) is None
    assert not (tmp_path / "VAF_Projects").exists()


def test_deleting_the_room_takes_the_shared_folder_with_it(tmp_path, monkeypatch):
    """MUTATION: leave the folder behind on delete.

    Deleting the conversation is the statement that its files are no longer wanted -
    the same rule a chat's workspace follows (SessionManager.delete). A folder that
    outlived its room would be an orphan the owner can neither see nor reach from
    any surface that knows what it was.
    """
    from vaf.core.a2a.room import derive_peer_id, participant_key
    from vaf.core.platform import Platform
    monkeypatch.setattr(Platform, "documents_dir", staticmethod(lambda: tmp_path))

    room = Room.create(kind="round", owner_scope="scope-a", base=tmp_path,
                       room_id="room-wsdel")
    host = room.join(display="Me", scope_id="scope-a",
                     peer_id=derive_peer_id(participant_key("cli", "scope-a"), "room-wsdel"))
    folder = room.workspace_dir(create=True)
    (folder / "notes.txt").write_text("shared", encoding="utf-8")

    assert room.delete(host)
    assert not folder.exists()


def test_activity_reports_facts_not_verdicts(tmp_path):
    """MUTATION: fold the typing window or the viewer exclusion in here.

    activity() joins what the store already records - each reader's cursor and the
    last frame each sender wrote - and hands it over as facts. Whether "read the
    newest message two seconds ago and has not answered" deserves a typing bubble
    is presentation taste, and taste lives with the surface that has it.
    """
    room = Room.create(kind="round", owner_scope=None, base=tmp_path, room_id="room-act")
    alice = room.join(display="Alice", scope_id=None, peer_id="p-alice")
    room.join(display="Bob", scope_id=None, peer_id="p-bob")

    frame = room.say(alice, "anyone there?")
    room.store.set_cursor("p-bob", frame.lamport)

    facts = room.activity()
    assert facts["p-bob"]["read_to"] == frame.lamport
    assert facts["p-bob"]["read_at"] > 0, "when the cursor moved is the whole signal"
    assert facts["p-bob"]["last_wrote"] < frame.lamport
    assert facts["p-alice"]["last_wrote"] >= frame.lamport


# ── the task board ─────────────────────────────────────────────────────────

def test_a_task_is_born_from_a_report_chain_and_dies_by_its_last_status(tmp_path):
    """MUTATION: make every ask a task, or read the FIRST report's status.

    A task exists when somebody reports on something - that self-selection is what
    keeps "bist du da?" off the board without anybody classifying messages. And
    the LAST report decides: a board that read the first would show every finished
    task as freshly started.
    """
    room = Room.create(kind="round", owner_scope=None, base=tmp_path, room_id="room-board")
    alice = room.join(display="Alice", scope_id=None, peer_id="p-alice")
    nobel = room.join(display="Nobel", scope_id=None, peer_id="p-nobel")

    ask = room.say(alice, "Nobel, build the website")
    room.say(nobel, "sure")
    assert room.tasks() == [], "small talk became a task"

    first = room.ingest({"kind": "report", "reply_to": ask.id,
                         "body": {"text": "starting", "status": "working"}},
                        identity=nobel)
    board = room.tasks()
    assert len(board) == 1
    task = board[0]
    assert task["title"] == "Nobel, build the website"
    assert task["status"] == "working"
    assert task["requester_label"].startswith("Alice")
    assert task["assignee_label"].startswith("Nobel")

    room.ingest({"kind": "report", "reply_to": first.id,
                 "body": {"text": "done", "status": "completed"}}, identity=nobel)
    task = room.tasks()[0]
    assert task["status"] == "completed"
    assert task["reports"] == 2


def test_a_directive_is_a_task_before_anyone_reports(tmp_path):
    """In a chain, giving work is the point - the board must show it while it is
    still unanswered, or the leader reads an empty board as an idle team."""
    room = Room.create(kind="chain", owner_scope=None, base=tmp_path, room_id="room-chain-b")
    boss = room.join(display="Boss", scope_id=None, peer_id="p-boss")
    room.join(display="W", scope_id=None, peer_id="p-w")

    room.ingest({"kind": "directive", "to": {"peer": "p-w"},
                 "body": {"text": "ship it"}}, identity=boss)
    board = room.tasks()
    assert len(board) == 1
    assert board[0]["status"] == "submitted"
    assert board[0]["assignee"] == "p-w"


def test_open_work_sorts_before_finished_work(tmp_path):
    room = Room.create(kind="round", owner_scope=None, base=tmp_path, room_id="room-sort")
    alice = room.join(display="Alice", scope_id=None, peer_id="p-alice")
    bob = room.join(display="Bob", scope_id=None, peer_id="p-bob")

    a = room.say(alice, "task one")
    b = room.say(alice, "task two")
    room.ingest({"kind": "report", "reply_to": a.id,
                 "body": {"status": "completed"}}, identity=bob)
    room.ingest({"kind": "report", "reply_to": b.id,
                 "body": {"status": "working"}}, identity=bob)

    statuses = [t["status"] for t in room.tasks()]
    assert statuses == ["working", "completed"]


def test_a_report_without_a_status_still_says_i_am_on_it(tmp_path):
    room = Room.create(kind="round", owner_scope=None, base=tmp_path, room_id="room-nostatus")
    alice = room.join(display="Alice", scope_id=None, peer_id="p-alice")
    bob = room.join(display="Bob", scope_id=None, peer_id="p-bob")

    ask = room.say(alice, "do the thing")
    room.ingest({"kind": "report", "reply_to": ask.id, "body": {"text": "on it"}},
                identity=bob)
    assert room.tasks()[0]["status"] == "working"


def test_a_peer_may_report_in_a_round_now(tmp_path):
    """The protocol change this board forced, pinned on purpose: in a round nobody
    commands (directive stays refused), but reporting on one's own work is
    self-description, not command - without it no task in a round could ever
    move, because every member of a round is a peer."""
    room = Room.create(kind="round", owner_scope=None, base=tmp_path, room_id="room-peerrep")
    peer = room.join(display="P", scope_id=None, peer_id="p-p")

    frame = room.ingest({"kind": "report", "body": {"status": "working"}}, identity=peer)
    assert frame.kind == "report"
    with pytest.raises(WrongRoomKind):
        room.ingest({"kind": "directive", "body": {"text": "no"}}, identity=peer)


def test_a_reply_to_that_is_prose_is_refused_at_the_tool_and_survives_in_the_fold(tmp_path, monkeypatch):
    """MUTATION: store whatever reply_to arrives at the sending tool.

    Found on the first real collaboration: a model handed room_send the MESSAGE
    TEXT as reply_to. The room stored it faithfully - and must keep doing so (a
    reply to a frame that has not arrived yet is legal, and foreign ids have
    foreign shapes), so the refusal lives at the SENDING tool, and the fold has
    to stay standing when prose reaches it through the CLI of an older build.
    """
    import vaf.core.a2a.store as store_mod
    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path)
    from vaf.tools.room_tools import RoomJoinTool, RoomSendTool

    room = Room.create(kind="round", owner_scope=None, base=tmp_path, room_id="room-prose")
    RoomJoinTool().run(room_id="room-prose", user_scope_id=None)
    b = room.join(display="B", scope_id=None, peer_id="p-b")

    prose = "Kleines Gemeinschaftsprojekt, wir bauen zusammen eine Team-Seite. " * 3
    out = RoomSendTool().run(room_id="room-prose", kind="report", text="on it",
                             reply_to=prose, user_scope_id=None)
    assert "id" in out.lower() and "error" in out.lower(), out
    assert all(f.kind != "report" for f in room.store.frames()), (
        "the prose reply_to was stored anyway")

    # The fold survives prose that reached the store through an older door.
    room.ingest({"kind": "report", "reply_to": prose,
                 "body": {"status": "working"}}, identity=b)
    board = room.tasks()
    assert len(board) == 1 and board[0]["status"] == "working"


def test_the_board_keeps_the_last_progress_anybody_reported(tmp_path):
    """MUTATION: overwrite progress with every report, or ignore it entirely.

    A status says WHETHER work runs; ten minutes of unchanged `working` is
    indistinguishable from a hang. Progress is what a reader can watch - and a
    worker that reports a count once and then only statuses must not lose it,
    while a fresh count always wins.
    """
    room = Room.create(kind="round", owner_scope="s", base=tmp_path, room_id="room-prog")
    asker = room.join(display="Asker", scope_id="s", peer_id="p-ask")
    worker = room.join(display="Worker", scope_id=None, peer_id="p-work")
    task = room.say(asker, "please build the thing")

    room.report(worker, "on it", status="working", reply_to=task.id,
                progress={"done": 1, "total": 4, "step": "reading"})
    entry = [t for t in room.tasks() if t["id"] == task.id][0]
    assert entry["progress"] == {"done": 1, "total": 4, "step": "reading"}

    # A later report that says nothing about progress keeps the last picture.
    room.report(worker, "still going", status="working", reply_to=task.id)
    entry = [t for t in room.tasks() if t["id"] == task.id][0]
    assert entry["progress"] == {"done": 1, "total": 4, "step": "reading"}

    # A fresh count replaces it.
    room.report(worker, "further", status="working", reply_to=task.id,
                progress={"done": 3, "total": 4, "step": "writing tests"})
    entry = [t for t in room.tasks() if t["id"] == task.id][0]
    assert entry["progress"]["done"] == 3 and entry["progress"]["step"] == "writing tests"

    # A task nobody reported progress on says so, rather than pretending zero.
    other = room.say(asker, "and this one too")
    room.report(worker, "took it", status="working", reply_to=other.id)
    assert [t for t in room.tasks() if t["id"] == other.id][0]["progress"] is None


def test_a_room_refuses_to_emit_progress_it_would_refuse_to_read(tmp_path):
    """MUTATION: write the caller's progress dict into the body unchecked.

    The room is a sender as well as a reader here, and a shape it would drop
    from a stranger must not be a shape it puts on the wire itself.
    """
    room = Room.create(kind="round", owner_scope="s", base=tmp_path, room_id="room-prog2")
    worker = room.join(display="Worker", scope_id="s", peer_id="p-w2")
    frame = room.report(worker, "going", status="working",
                        progress={"done": -3, "total": "lots", "step": "y" * 400})
    body_progress = (frame.body or {}).get("progress")
    assert body_progress is None or (
        body_progress.get("done", 0) >= 0 and len(body_progress.get("step", "")) <= 120)
