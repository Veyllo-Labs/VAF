# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""An invitation is answered by the one it names, and its outcome is kept.

Two doors, one list. A foreign agent redeems a bearer ticket on the wire; an
account on this machine is invited by name and joins only when it says yes. Both
are tickets in the same store, so "who did I invite and who arrived" is one
question with one answer - and the account door is NOT a bearer credential: an
account invitation refused on the wire is the property that keeps a room id, which
travels in prompts and log lines, from being a way in.
"""
import json
import time
from pathlib import Path

import pytest

import vaf.core.a2a.store as store_mod
from vaf.core.a2a.room import (NotAMember, NotPermitted, Room, RoomError, TicketInvalid,
                               derive_peer_id, invited_rooms, participant_key)

ROOT = Path(__file__).resolve().parents[1]
HOST = "scope-host"
GUEST = "scope-guest"


@pytest.fixture()
def base(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path)
    return tmp_path


def _shared_room(base, room_id="room-inv"):
    room = Room.create(kind="round", owner_scope=HOST, base=base, room_id=room_id,
                       topic="Deploy talk", multi_scope=True)
    key = participant_key("cli", HOST)
    host = room.join(display="Alice", scope_id=HOST,
                     peer_id=derive_peer_id(key, room_id), participant_key=key)
    return room, host


# ── the account door ────────────────────────────────────────────────────────

def test_an_invited_account_sees_its_invitation_and_nobody_elses(base):
    room, host = _shared_room(base)
    row = room.invite_account(host, GUEST, display="bob")

    assert row["status"] == "pending" and row["kind"] == "account"
    assert row["minted_by_label"] == "Alice"
    assert room.invitation_for(GUEST)["id"] == row["id"]
    assert room.invitation_for("scope-stranger") is None
    found = invited_rooms(GUEST, base=base)
    assert [(r.room_id, inv["id"]) for r, inv in found] == [("room-inv", row["id"])]
    assert invited_rooms("scope-stranger", base=base) == []


def test_inviting_the_same_account_twice_returns_the_open_invitation(base):
    room, host = _shared_room(base)
    first = room.invite_account(host, GUEST)
    second = room.invite_account(host, GUEST)
    assert first["id"] == second["id"]
    assert len([r for r in room.invitations(host) if r["status"] == "pending"]) == 1


def test_accepting_admits_and_joins_the_person_and_keeps_who_arrived(base):
    """MUTATION: skip `_admit_tenant` before the join, or drop the settle.

    The join walks through the tenant door like every join; without the admission
    written first the invitee is refused by the very room that invited them.
    """
    room, host = _shared_room(base)
    row = room.invite_account(host, GUEST, display="bob")

    me = room.accept_invitation(GUEST, display="bob")

    assert GUEST in room.tenants()
    assert me.peer_id == derive_peer_id(participant_key("cli", GUEST), "room-inv")
    assert room.role_of(me.peer_id) == "peer"
    listed = {r["id"]: r for r in room.invitations(host)}
    assert listed[row["id"]]["status"] == "accepted"
    assert listed[row["id"]]["redeemed_by"] == me.peer_id
    assert listed[row["id"]]["redeemed_by_label"]
    assert room.invitation_for(GUEST) is None, "an accepted invitation is spent"
    assert invited_rooms(GUEST, base=base) == []


def test_only_the_named_account_can_accept(base):
    """MUTATION: drop the tenant comparison in accept_invitation."""
    room, host = _shared_room(base)
    row = room.invite_account(host, GUEST)

    with pytest.raises(TicketInvalid):
        room.accept_invitation("scope-stranger", display="Mallory")
    # Not consumed by the wrong caller: the right one still can.
    assert room.invitation_for(GUEST)["id"] == row["id"]
    room.accept_invitation(GUEST, display="bob")


def test_an_account_invitation_is_not_a_bearer_credential_on_the_wire(base):
    """MUTATION: remove the `tenant` peek from redeem_ticket.

    The invitation id is shown in a member panel and travels in a sidebar row. If
    the wire door redeemed it, anybody who read it could sit down as a guest.
    """
    room, host = _shared_room(base)
    row = room.invite_account(host, GUEST)

    with pytest.raises(TicketInvalid):
        room.redeem_ticket(row["id"], display="Mallory")
    assert room.invitation_for(GUEST) is not None, "refused without being consumed"
    assert room.role_of("p-mallory") is None


def test_declining_spends_the_invitation_and_keeps_the_answer(base):
    room, host = _shared_room(base)
    row = room.invite_account(host, GUEST)

    answered = room.decline_invitation(GUEST)

    assert answered["status"] == "declined"
    assert room.invitation_for(GUEST) is None
    assert {r["id"]: r["status"] for r in room.invitations(host)}[row["id"]] == "declined"
    assert GUEST not in room.tenants(), "declining admits nobody"
    with pytest.raises(TicketInvalid):
        room.accept_invitation(GUEST, display="bob")


def test_revoking_is_for_the_inviter_the_host_or_the_leader_and_only_while_open(base):
    room, host = _shared_room(base)
    peer = room.join(display="Codex", scope_id=None, peer_id="p-codex")
    row = room.invite_account(host, GUEST)

    with pytest.raises(NotPermitted):
        room.revoke_invitation(peer, row["id"])
    taken = room.revoke_invitation(host, row["id"])
    assert taken["status"] == "revoked"
    assert room.invitation_for(GUEST) is None
    with pytest.raises(TicketInvalid):
        room.revoke_invitation(host, row["id"])


def test_an_agent_ticket_records_who_redeemed_it(base):
    """MUTATION: drop the settle after the wire join."""
    room, host = _shared_room(base)
    ticket = room.mint_ticket(host, display="Codex")

    guest = room.redeem_ticket(ticket)

    rows = {r["id"]: r for r in room.invitations(host)}
    assert rows[ticket]["kind"] == "agent"
    assert rows[ticket]["status"] == "accepted"
    assert rows[ticket]["redeemed_by"] == guest.peer_id
    assert rows[ticket]["redeemed_by_label"] == room.label_for(guest.peer_id)


def test_a_pending_invitation_past_its_time_is_settled_as_expired_on_the_first_look(base):
    """MUTATION: show "expired" without claiming - the credential stays pending on
    disk and a later clock skew could bring it back."""
    room, host = _shared_room(base)
    ticket = room.mint_ticket(host, display="Late", ttl_s=-1)

    rows = {r["id"]: r for r in room.invitations(host)}
    assert rows[ticket]["status"] == "expired"
    assert room.store.ticket(ticket) is None, "left in the pending directory"
    with pytest.raises(TicketInvalid):
        room.redeem_ticket(ticket)


def test_the_list_is_for_members_and_the_invitation_needs_a_shared_room(base):
    room, host = _shared_room(base)
    stranger_room = Room.create(kind="round", owner_scope=HOST, base=base,
                                room_id="room-solo")
    key = participant_key("cli", HOST)
    solo_host = stranger_room.join(display="Alice", scope_id=HOST,
                                   peer_id=derive_peer_id(key, "room-solo"),
                                   participant_key=key)

    outsider = type("I", (), {"peer_id": "p-nobody", "scope_id": None, "role": ""})()
    with pytest.raises(NotAMember):
        room.invitations(outsider)
    with pytest.raises(RoomError):
        stranger_room.invite_account(solo_host, GUEST)


def test_opening_a_room_to_accounts_is_the_hosts_act_and_starts_newcomers_at_their_join(base):
    """MUTATION: flip multi_scope without touching backlog.

    A room created shared starts a newcomer at its own join; a room opened up later
    must decide the same thing, or the first invited account reads the whole history
    of a conversation it was never part of.
    """
    room = Room.create(kind="round", owner_scope=HOST, base=base, room_id="room-flip")
    key = participant_key("cli", HOST)
    host = room.join(display="Alice", scope_id=HOST,
                     peer_id=derive_peer_id(key, "room-flip"), participant_key=key)
    room.say(host, "before anybody else was here")
    peer = room.join(display="Codex", scope_id=None, peer_id="p-codex")

    with pytest.raises(NotPermitted):
        room.open_to_accounts(peer)
    room.open_to_accounts(host)
    assert room.manifest["multi_scope"] is True
    assert room.manifest["backlog"] == "since_join"

    room.invite_account(host, GUEST)
    me = room.accept_invitation(GUEST, display="bob")
    cursor = room.store.cursor(me.peer_id)
    assert cursor > 0, "the newcomer's cursor still stands at the start of the room"
    assert all(f.lamport <= cursor for f in room.store.frames() if f.kind == "say")


def test_a_closed_room_invites_nobody(base):
    room, host = _shared_room(base)
    room.invite_account(host, GUEST)
    room.close(host, reason="done")
    assert invited_rooms(GUEST, base=base) == []


def test_the_facade_exports_the_invited_lookup():
    import vaf
    assert vaf.invited_rooms is invited_rooms
    assert "invited_rooms" in vaf.__all__


# ── the sidebar, the door and the wire: what the harness built on it ───────────

def test_an_invited_account_gets_a_door_row_and_is_not_a_member(base):
    """MUTATION: mark the invited row without `invited`, or let member_room_ids
    include it - either way an invitee would be answered a transcript."""
    from vaf.core.session import _room_rows, member_room_ids, session_list_payload
    room, host = _shared_room(base)
    room.invite_account(host, GUEST, display="bob")

    rows = _room_rows(GUEST)
    assert [r["room_id"] for r in rows] == ["room-inv"]
    door = rows[0]
    assert door["invited"] is True and door["invited_by"] == "Alice"
    assert door["invitation_id"] and door["expires_at"] > 0
    assert door["message_count"] == 0 and door["unread"] == 0
    assert member_room_ids(GUEST) == set(), "an invitee is not a member"
    assert member_room_ids(HOST) == {"room-inv"}

    payload = session_list_payload(rows)[0]
    assert payload["kind"] == "room" and payload["invited"] is True
    assert payload["invitedBy"] == "Alice" and payload["invitationId"] == door["invitation_id"]
    assert payload["expiresAt"] == door["expires_at"]

    room.accept_invitation(GUEST, display="bob")
    rows = _room_rows(GUEST)
    assert rows and not rows[0].get("invited") and rows[0]["role"] == "peer"
    assert member_room_ids(GUEST) == {"room-inv"}


def test_the_door_carries_no_messages(base):
    """MUTATION: send the transcript to an invitee.

    Nothing said in a room reaches an account that has not accepted - not a line,
    not a file name. The door is the same message type with an empty list.
    """
    import asyncio
    from vaf.core.session import _room_rows
    from vaf.core.web_server import _send_room_door

    room, host = _shared_room(base)
    room.say(host, "the secret plan")
    room.invite_account(host, GUEST, display="bob")
    row = _room_rows(GUEST)[0]

    class _WS:
        def __init__(self):
            self.sent = []

        async def send_json(self, payload):
            self.sent.append(payload)

    ws = _WS()
    asyncio.run(_send_room_door(ws, room, row))
    sent = ws.sent[0]
    assert sent["type"] == "room_transcript"
    assert sent["messages"] == []
    assert sent["room"]["invited"]["by"] == "Alice"
    assert sent["room"]["invited"]["invitationId"] == row["invitation_id"]
    assert sent["room"]["role"] == "" and sent["room"]["canInvite"] is False
    assert "secret plan" not in str(sent)
    assert [m["label"] for m in sent["room"]["members_list"]] == ["Alice"]


def test_the_transcript_carries_the_invitations_and_who_may_invite(base):
    import asyncio
    from vaf.core.web_server import _send_room_transcript

    room, host = _shared_room(base)
    row = room.invite_account(host, GUEST, display="bob")
    ticket = room.mint_ticket(host, display="Codex")

    class _WS:
        def __init__(self):
            self.sent = []

        async def send_json(self, payload):
            self.sent.append(payload)

    ws = _WS()
    asyncio.run(_send_room_transcript(ws, Room.open("room-inv", base=base), HOST))
    payload = ws.sent[0]["room"]
    assert payload["canInvite"] is True and payload["shared"] is True
    listed = {i["id"]: i for i in payload["invitations"]}
    assert listed[row["id"]]["kind"] == "account" and listed[row["id"]]["status"] == "pending"
    assert listed[row["id"]]["invitedBy"] == "Alice"
    assert listed[ticket]["kind"] == "agent"
    for field in ("id", "kind", "display", "status", "invitedBy", "mintedAt",
                  "expiresAt", "decidedAt", "acceptedAs"):
        assert field in listed[ticket], f"{field} is not forwarded to the browser"


def test_every_membership_check_in_the_web_server_skips_the_door():
    """MUTATION: read membership from `_room_rows` in an acting command again.

    The invited row is IN `_room_rows` (that is how the sidebar shows the door), so
    every command that takes that list as "the rooms this user is in" would let an
    invitee speak, read the task record or open the shared folder. Only `open_room`
    and the door's own answer may read the raw rows.
    """
    source = (ROOT / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    raw_uses = [line for line in source.splitlines()
                if "_room_rows(user_scope_id)" in line and "member_room_ids" not in line]
    # open_room (needs the row), the door's own answer, the transcript builder's row
    # lookup, and the invitee's two commands - and nothing that ACTS in a room.
    assert len(raw_uses) <= 4, raw_uses
    acting = source.split('elif type in ("room_say",')[1].split('elif type == "load_session"')[0]
    assert "member_room_ids(user_scope_id)" in acting
    assert "{row[\"room_id\"] for row in _room_rows" not in acting
    invites = source.split('elif type in ("invite_account",')[1].split("elif type ==")[0]
    assert "member_room_ids(user_scope_id)" in invites
    assert "room.join(" not in invites, "looking at invitations must never join anybody"


def test_the_browser_forwards_the_door_and_draws_it():
    """Rule 2: a field the handler does not name is silently dropped."""
    page = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    assert "invited?: boolean;" in page and "invitedBy?: string;" in page
    assert "invited?: { by: string; expiresAt: number; invitationId: string };" in page
    room_row = page.split("{sessions.map(s => isRoom(s) ? (")[1].split("\n                            ) : (\n", 1)[0]
    assert "tMain('roomInviteBadge')" in room_row
    assert "!s.closed && !s.invited" in room_row, "the door offers rename or close"
    container = page.split("cn(messagesAreaWidthClass,")[1][:3200]
    assert "<RoomInvitationGate" in container and "<RoomConversation" in container
    gate = page.split("function RoomInvitationGate(")[1].split("\nfunction ")[0]
    assert "onAnswer(true)" in gate and "onAnswer(false)" in gate
    for cmd in ("accept_room_invite", "decline_room_invite", "invite_account",
                "revoke_room_invite", "room_invite_agent", "room_invitation_text",
                "room_invite_candidates"):
        assert f"'{cmd}'" in page, f"the browser never sends {cmd}"
    assert "roomView.room.invited) return;" in page, "the composer still sends from the door"


def test_every_locale_names_the_invitation_surface():
    import json
    for locale in ("en", "de", "tr", "zh", "ja", "ko", "th"):
        main = json.loads((ROOT / "web" / "messages" / f"{locale}.json").read_bytes())["main"]
        for key in ("roomInviteBadge", "roomTabInvite", "roomInviteGateTitle", "roomInviteAccept",
                    "roomInviteDecline", "roomInviteGenerate", "roomInviteStatusAccepted"):
            assert main.get(key), f"{locale}: {key}"


def test_the_agent_tool_and_the_cli_carry_the_account_lane():
    from vaf.tools.room_tools import RoomInviteTool
    assert "account" in RoomInviteTool.parameters["properties"]
    from vaf.cli.cmd.a2a import app
    names = {c.name or c.callback.__name__ for c in app.registered_commands}
    for name in ("invitations", "accept", "decline", "revoke"):
        assert name in names, name
    source = (ROOT / "vaf" / "cli" / "cmd" / "a2a.py").read_text(encoding="utf-8")
    assert '"--account"' in source and "invite_account(" in source


def test_the_event_and_the_config_key_are_registered():
    from vaf.core.config import Config
    from vaf.core.security_events import SECURITY_EVENT_KINDS
    assert "room_account_invited" in SECURITY_EVENT_KINDS
    assert Config.DEFAULTS["a2a_room_invite_directory"] is True
    assert "a2a_room_invite_directory" in Config.GLOBAL_CONFIG_KEYS
    doc = (ROOT / "docs" / "setup" / "CONFIG_SCHEMA.md").read_text(encoding="utf-8")
    assert "`a2a_room_invite_directory`" in doc
    assert f"({len(Config.DEFAULTS)} keys)" in doc


def test_the_harness_registers_the_account_directory_and_the_framework_reads_only_that():
    """MUTATION: read the auth store from config.py or the room commands again.

    The directory resolver exists so the framework never reaches into the harness's
    auth layer for a name; the shrink-only baseline in
    tests/test_framework_auth_layering.py holds the line, and this pins the wiring
    the same way the allowlist resolver's is pinned.
    """
    import os
    import subprocess
    import sys
    from vaf.core.tool_dispatch import (get_account_directory_resolver,
                                        resolve_account_directory,
                                        set_account_directory_resolver)

    main = (ROOT / "vaf" / "main.py").read_text(encoding="utf-8")
    assert "set_account_directory_resolver(_account_directory_resolver)" in main
    config = (ROOT / "vaf" / "core" / "config.py").read_text(encoding="utf-8")
    block = config.split("def scope_id_for_username")[1].split("\ndef ")[0]
    assert "vaf.auth" not in block and "resolve_account_directory" in block

    # The primitive itself: a lookup, not a guard - a raising resolver is an empty
    # directory, and rows that cannot be addressed or scoped are dropped.
    previous = get_account_directory_resolver()
    try:
        set_account_directory_resolver(lambda: [
            {"username": "bob", "user_scope_id": "scope-bob"},
            {"username": "", "user_scope_id": "scope-x"},
            {"username": "ghost", "user_scope_id": ""},
            {"username": "carol", "user_scope_id": "scope-carol", "active": False},
        ])
        rows = resolve_account_directory()
        assert [r["username"] for r in rows] == ["bob", "carol"]
        assert rows[1]["active"] is False
        from vaf.core.config import scope_id_for_username
        assert scope_id_for_username("BOB") == "scope-bob"
        assert scope_id_for_username("carol") is None, "an inactive account is not invited"
        assert scope_id_for_username("nobody") is None

        def _boom():
            raise RuntimeError("store down")
        set_account_directory_resolver(_boom)
        assert resolve_account_directory() == []
    finally:
        set_account_directory_resolver(previous)

    script = (
        "import vaf.main\n"
        "from vaf.core.tool_dispatch import get_account_directory_resolver\n"
        "from vaf.auth.permissions import list_accounts\n"
        "assert get_account_directory_resolver() is list_accounts\n"
        "print('REGISTERED_OK')\n"
    )
    env = dict(os.environ, VAF_SKIP_DEP_CHECK="1")
    result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                            text=True, env=env, cwd=str(ROOT), timeout=120)
    assert "REGISTERED_OK" in result.stdout, result.stderr[-2000:]
