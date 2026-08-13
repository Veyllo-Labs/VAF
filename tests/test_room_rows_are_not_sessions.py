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


def _sidebar_branches():
    """The room row and the conversation row, split on the OUTER ternary.

    Anchored on the exact indentation of the branch separator: the room row now holds a
    ternary of its own (the inline rename), so a naive split on ") : (" lands inside it
    and every assertion afterwards is made against half a row.
    """
    source = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    body = source.split("{sessions.map(s => isRoom(s) ? (")[1]
    room, chat = body.split("\n                            ) : (\n", 1)
    return room, chat[:8000]



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


# ── the browser gets the fields, and gets them from one place ──────────────

def test_the_browser_payload_carries_what_a_room_row_is(rooms):
    """MUTATION: keep the five-key projection the seven call sites used to write out.

    The room row already travelled in the list; every field that says it IS a room was
    dropped on the way to the browser, so the sidebar could only have rendered it as a
    conversation with a strange name.
    """
    from vaf.core.session import SessionManager, _room_rows
    from vaf.core.web_server import session_list_payload

    payload = session_list_payload(_room_rows(SCOPE))

    assert len(payload) == 1
    row = payload[0]
    assert row["kind"] == "room"
    assert row["roomId"] == "room-visible"
    assert row["unread"] == 1
    assert row["members"] == 2
    assert row["closed"] is False
    assert row["id"].startswith("room:"), "the row id must stay one no loader accepts"


def test_an_ordinary_conversation_keeps_the_keys_it_always_had():
    """The room fields are additive. A chat row that lost or renamed a key would break
    a sidebar that has nothing to do with rooms."""
    from vaf.core.web_server import session_list_payload

    payload = session_list_payload([{
        "id": "chat-1", "name": "an ordinary chat", "updated_at": "2026-01-01",
        "message_count": 4, "metadata": {"source": "thinking"},
    }])

    assert payload == [{
        "id": "chat-1", "title": "an ordinary chat", "date": "2026-01-01",
        "messageCount": 4, "source": "thinking", "kind": "chat",
    }]


def test_there_is_exactly_one_projection_left(rooms):
    """MUTATION: hand-write an eighth copy at the next call site that needs one.

    Seven identical copies is how the room fields came to be dropped in the first
    place, and the eighth would be the eighth place to forget.
    """
    source = (ROOT / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")

    assert source.count('"title": s["name"]') == 1, (
        "a session list is being built by hand again")
    assert source.count("session_list_payload(web_sessions)") >= 7, (
        "a call site stopped going through the one projection")


# ── the browser must not treat a room as a session either ──────────────────

def test_the_frontend_never_picks_a_room_when_it_picks_a_session():
    """MUTATION: drop the conversationsOnly filter in page.tsx.

    Rooms ride at the TOP of this list, so `sessions[0]` is a room for any user who is
    in one. The frontend auto-loads that first entry on connect - it would hand a room
    id to load_session, which the backend refuses, and the user would open to an error
    instead of their last chat. This is the same defect the Python side already refuses
    loudly, arriving through the browser.
    """
    source = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")

    # The IMPLEMENTATIONS, word for word, not merely that the names exist. A filter
    # gutted to `(list) => list` keeps every call site intact and every other assertion
    # here green while protecting nothing - which is the same defect as a test that
    # asserts a guard is called without asserting the guard does anything.
    assert "const isRoom = (s: Session) => s.kind === 'room';" in source
    assert "const conversationsOnly = (list: Session[]) => list.filter(s => !isRoom(s));" in source
    # the auto-select on connect
    assert "wsSocketRef.current?.send(JSON.stringify({ type: 'load_session', id: chats[0].id }))" in source
    assert "id: data.sessions[0].id" not in source, "the auto-load can still pick a room"
    # the fallback after a delete
    assert "conversationsOnly(sessions).filter" in source


def test_a_room_row_in_the_browser_opens_a_room_and_not_a_session():
    """MUTATION: give the room row the same onClick as the chat row.

    handleSessionSwitch is the session loader. A room reaching it is the lost update
    the store exists to avoid, reached through a mouse click.
    """
    branch, _chat = _sidebar_branches()

    assert "type: 'open_room'" in branch
    assert "handleSessionSwitch" not in branch, "the room row still switches sessions"
    # Renaming and ending ARE offered here, and they are the room's own versions of
    # both. What must never appear is the SESSION deleter, which would be handed an id
    # it cannot resolve and would answer for the wrong thing if it ever could.
    assert "delete_session" not in branch


# ── the room header names who is in it ─────────────────────────────────────

def test_the_room_payload_names_its_members(rooms):
    """A group chat header that says "2 agents" and not who they are is the one
    question a reader has when several agents share a room."""
    from vaf.core.a2a.room import Room

    room = Room.open("room-visible")
    listed = sorted(room.labels().values())

    assert len(listed) == 2
    assert all(label != label.rstrip("0123456789") for label in listed), (
        "a member is listed without its tag, so two agents could share a name")


def test_the_header_renders_the_members_and_marks_our_own(rooms):
    """MUTATION: render members_list without telling our agent apart.

    An agent that is not ours is a full agent of its own. Drawing it the same as ours
    is the one thing this view must never do, because everything a reader concludes
    about who said what depends on it.
    """
    source = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    block = source.split("(view.room.members_list || []).map(m => (")[1].split("</div>")[0]

    assert "m.peer === view.room.me" in block, "our own agent is not marked"
    assert "{m.label}" in block, "the header shows something other than the resolved name"


def test_a_room_is_rendered_inside_the_ordinary_chat_area():
    """MUTATION: give the room a surface of its own again.

    Twice already: first a narrow dialog, then a full-screen layer that covered the
    sidebar. Both were the same mistake - a room is not a different screen, it is the
    same screen with several speakers in it. The chat's frame (sidebar, header,
    composer, scroll area, column width) stays untouched and only the placing of the
    content differs, so the room component renders CONTENT and nothing around it.
    """
    source = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    block = source.split("function RoomConversation(")[1].split("\nfunction ")[0]

    for surface in ("fixed inset-0", "max-w-2xl", "bg-black/60", "z-[80]"):
        assert surface not in block, f"the room built a surface of its own again: {surface}"
    # it is placed by the chat, so it must not set the column width or the scrolling
    for chrome in ("messagesAreaWidthClass", "overflow-y-auto", "h-screen"):
        assert chrome not in block, f"the room is laying out the chat's frame: {chrome}"


def test_the_room_is_branched_inside_the_message_container():
    """MUTATION: mount the room next to the chat instead of inside it.

    Rendered as a sibling it would appear beside or over the conversation. Inside the
    message container it lands exactly where messages land, which is what makes it
    look like the chat rather than like something covering it.
    """
    source = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    container = source.split('mx-auto space-y-2 pb-32")}>')[1][:1200]

    assert "{roomView ? (" in container, "the room is not branched inside the chat area"
    assert "<RoomConversation" in container


def test_choosing_a_conversation_puts_the_room_away():
    """MUTATION: leave roomView set when a session is picked.

    Both occupy the same place. A chat loaded underneath a room that is still showing
    is a user clicking a conversation and getting no conversation.
    """
    source = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    body = source.split("const handleSessionSwitch = (id: string) => {")[1][:400]

    assert "setRoomView(null)" in body


def test_bookkeeping_is_not_rendered_as_something_somebody_said():
    """MUTATION: render join and leave as ordinary messages.

    A join has no words in it. Drawing it with an avatar and a name above a sentence
    puts a line in an agent's mouth that the agent never wrote, on the one kind of
    frame where that is guaranteed to be a fabrication.
    """
    source = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    block = source.split("function RoomConversation(")[1].split("\nfunction ")[0]

    assert "const bookkeeping = m.kind === 'join'" in block
    assert "if (bookkeeping) {" in block


# ── closing a room, and writing into one ───────────────────────────────────

def test_a_room_row_carries_the_same_pair_a_conversation_does():
    """A room row offers a pencil and a bin in the place a conversation offers them,
    doing the room's version of each: rename the topic, END the room. Ending is not
    deleting - the transcript stays readable - but it is the act a person reaches for
    in that spot, and a different icon there made them hunt for it.
    """
    branch, _chat = _sidebar_branches()

    assert "Edit2" in branch and "startEditing(s)" in branch
    assert "Trash2" in branch and "setRoomToClose(s)" in branch
    assert "delete_session" not in branch, (
        "a room row reached the session deleter, which would not know what to delete")


def test_a_room_renames_in_the_sidebar_the_way_a_conversation_does():
    """MUTATION: put the rename back in a dialog.

    A conversation renames in place, in the row, and that is where a person looks for
    it. A room that opened a modal for the same act was a different interaction for the
    same gesture, one row apart. What differs is only WHERE the new name goes.
    """
    source = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    branch, _chat = _sidebar_branches()

    assert "startEditing(s)" in branch, "the pencil no longer edits in place"
    assert "editingId === s.id" in branch and "submitRename()" in branch
    assert "setRoomToRename" not in branch, "the room row still opens a rename dialog"

    body = source.split("const submitRename = () => {")[1][:900]
    assert "type: 'rename_room'" in body, "a room rename would go to the session renamer"
    assert "isRoom(s)" in body


def test_closing_never_happens_on_one_click():
    """MUTATION: send close_room straight from the icon.

    Closing cannot be undone and it takes access away from agents that are not in
    front of this screen to object. It asks first, in the user's own language.
    """
    source = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    dialog = source.split("{roomToClose && (")[1].split("{/* Trust Gate Dialog")[0]

    assert "type: 'delete_room'" in dialog, (
        "the bin closes instead of deleting, which is not what a bin means anywhere "
        "else in this product")
    for key in ("roomCloseTitle", "roomCloseBody", "roomCloseConfirm", "roomCloseCancel"):
        assert f"tMain('{key}')" in dialog, f"the dialog hardcodes {key} instead of translating it"


def test_the_confirmation_is_translated_in_every_language_we_ship():
    """MUTATION: add the English strings and forget the catalogue.

    next-intl falls back to the key name, so a missing translation renders as
    "roomCloseBody" - visible, but only to somebody running that language.
    """
    import json

    keys = {"roomCloseTitle", "roomCloseBody", "roomCloseConfirm", "roomCloseCancel",
            "roomClosedNote"}
    for name in ("en", "de"):
        catalogue = json.loads((ROOT / "web" / "messages" / f"{name}.json").read_text(encoding="utf-8"))
        missing = keys - set(catalogue.get("main", {}))
        assert not missing, f"{name}.json is missing {sorted(missing)}"


def test_the_composer_writes_into_the_room_it_is_showing():
    """MUTATION: let the box send into the conversation underneath.

    The room is where the composer visibly sits, so a message typed there must land
    there. Sending it to the chat behind the room is the worst of the three possible
    behaviours - worse than refusing, because nothing tells the user it happened.
    """
    source = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    body = source.split("const sendMessage = async (")[1][:2500]

    assert "if (roomView) {" in body
    assert "type: 'room_say'" in body
    assert body.index("if (roomView) {") < body.index("expectNewAssistantRef"), (
        "the room branch runs after the message was already added to the chat")


def test_the_browser_acts_on_the_human_lane_and_not_a_lane_of_its_own():
    """MUTATION: add a "web" participant lane.

    The lanes separate the HUMAN from the AGENT. A browser and a terminal in front of
    the same person are the same actor, and a lane of its own would derive a second
    handle - splitting one person into two members of one room, so that whoever spoke
    last would look like somebody else.
    """
    from vaf.core.a2a.room import PARTICIPANT_LANES

    assert "web" not in PARTICIPANT_LANES
    source = (ROOT / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    block = source.split('elif type in ("room_say", "close_room", "delete_room", "kick_peer", "rename_room"):')[1].split("elif type == \"load_session\"")[0]
    assert 'participant_key("cli", user_scope_id)' in block


def test_writing_and_closing_answer_from_the_store():
    """MUTATION: echo the message back optimistically instead of re-reading.

    A room has N writers. A browser that repainted from what it ASSUMED had happened
    would drift from the transcript the moment anybody else wrote at the same time,
    and the drift would be invisible until somebody compared two screens.
    """
    source = (ROOT / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    block = source.split('elif type in ("room_say", "close_room", "delete_room", "kick_peer", "rename_room"):')[1].split("elif type == \"load_session\"")[0]

    assert "_send_room_transcript" in block
    # one builder for all three commands, so they cannot describe the room differently
    assert source.count("async def _send_room_transcript") == 1
    assert source.count("_send_room_transcript(") >= 3


# ── the group-chat header, and removing somebody ───────────────────────────

def test_the_header_asks_who_is_here_rather_than_offering_a_third_way_out():
    """MUTATION: put the close button back in the header.

    A click on any conversation already leaves the room, and the sidebar bin ends it.
    A third exit in the header crowded out the one thing a group-chat header is
    actually asked for: who is in this room.
    """
    source = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    block = source.split("function RoomConversation(")[1].split("\nfunction ")[0]

    assert "onMembers" in block and "<Info size={16} />" in block
    assert "onClose" not in block, "the header still carries a way to close the view"


def test_removing_somebody_asks_first_and_says_something_different_from_ending_the_room():
    """One takes a participant out, the other takes everybody out. A dialog that used
    the same sentence for both would be answered by habit."""
    source = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    dialog = source.split("{peerToKick && roomView && (")[1].split("{/* Renaming a room")[0]

    assert "type: 'kick_peer'" in dialog
    assert "tMain('roomKickBody')" in dialog
    assert "roomCloseBody" not in dialog, "the removal dialog reuses the closing wording"


def test_no_remove_button_is_offered_for_a_peer_that_cannot_be_removed():
    """MUTATION: render the button and let the backend refuse.

    An action offered and then denied reads as a fault. This is a deliberate rule with
    a different answer - end the room - so the button is ABSENT rather than disabled.
    """
    source = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    members = source.split("{roomMembersOpen && roomView && (")[1].split("{/* Removing one agent")[0]

    assert "roomView.room.canManage && !m.protected" in members
    assert "m.peer !== roomView.room.me" in members, "the viewer is offered a way to kick itself"


def test_the_payload_marks_the_peers_that_cannot_be_removed():
    """MUTATION: leave `protected` out and let the browser work it out.

    The rule is derived from the owner's scope and the room id. A browser deciding it
    for itself would be a second answer to a question the room already answers, and it
    is the answer that keeps somebody from being locked out of their own room.
    """
    source = (ROOT / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    block = source.split("async def _send_room_transcript")[1].split("\ndef ")[0]

    assert '"protected": peer in hosts' in block
    assert '"canManage"' in block


def test_renaming_a_room_changes_the_manifest_and_never_becomes_a_message():
    """MUTATION: write the new title as a frame.

    A topic is a property of the room, not something somebody said. As a frame it would
    appear in the transcript as a line nobody wrote, and every reader would render it.
    """
    source = (ROOT / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    block = source.split('elif type == "rename_room":')[1].split("else:")[0]

    assert "update_manifest(topic=" in block
    assert "ingest" not in block and "room.say" not in block
    assert "is_host" in block, "anybody in the room can rename it"


def test_the_new_dialogs_are_translated_in_every_language_we_ship():
    import json

    keys = {"roomMembersTitle", "roomMembersHost", "roomKickTitle", "roomKickBody",
            "roomKickConfirm", "roomRenameTitle", "roomRenameSave", "roomStale"}
    for name in ("en", "de"):
        catalogue = json.loads((ROOT / "web" / "messages" / f"{name}.json").read_text(encoding="utf-8"))
        missing = keys - set(catalogue.get("main", {}))
        assert not missing, f"{name}.json is missing {sorted(missing)}"


# ── the two things the owner found by using it ─────────────────────────────

def test_the_strip_over_the_composer_belongs_to_whatever_is_open():
    """MUTATION: keep showing the conversation's workspace and token budget.

    The workspace folder, the retrieval sources and the token budget all describe the
    CHAT. With a room open they described the one hidden behind it, so a user read
    another chat's workspace and another chat's numbers while typing into a room.
    """
    source = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    strip = source.split("{/* Token Stats (Clickable) + RAG Badge */}")[1][:3000]

    assert "{roomView ? (" in strip
    assert strip.index("{roomView ? (") < strip.index("workspaceInfo?.path"), (
        "the workspace chip is still rendered for a room")


# ── the panel, and the marking that was still wrong ────────────────────────

def test_the_open_room_is_the_highlighted_row_and_the_chat_is_not():
    """MUTATION: mark only the dot and leave the title weight alone.

    That was the state after the first attempt: the little indicator moved, and the
    conversation's title stayed bold with its background lit, so the sidebar still
    read as though the chat were open. A person looks at the WEIGHT, not the dot.
    """
    room_branch, chat_branch = _sidebar_branches()

    assert 'roomView?.room.roomId === s.roomId ? "font-medium text-gray-900"' in room_branch, (
        "an open room's title is never emphasised")
    assert 'currentSessionId === s.id && !roomView ? "font-medium text-gray-900"' in chat_branch, (
        "a conversation's title stays emphasised while a room is open")
    assert "currentSessionId === s.id && !roomView ? 'bg-transparent'" in chat_branch, (
        "a conversation keeps its active background while a room is open")


def test_the_panel_answers_what_the_room_is_and_not_only_who_is_in_it():
    """MUTATION: keep the narrow list.

    A group chat's panel that answers only "who" leaves out the two things asked next -
    what kind of room this is and since when - and a member row without a role, its
    abilities or a way to remove it is a tooltip that got away.
    """
    source = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    panel = source.split("{roomMembersOpen && roomView && (")[1].split("{/* Removing one agent")[0]

    assert "max-w-3xl" in panel and "h-[80vh]" in panel, "the panel is still a small box"
    for key in ("roomInfoKind", "roomInfoCreated", "roomInfoMembers", "roomInfoYou"):
        assert f"tMain('{key}')" in panel, f"the panel does not say {key}"
    assert "m.can || []" in panel, "a member is listed without what it may send"
    assert "UserMinus" in panel, "there is no way to remove anybody from the panel"
    assert "roomInfoLastSeen" in panel, "liveness is shown as a verdict rather than a time"


def test_the_abilities_come_from_the_table_that_enforces_them():
    """MUTATION: write the ability list into the payload by hand.

    Shown next to a name, an ability list is read as a promise. Written out separately
    it would keep promising something the room refuses, and the reader would only find
    out by watching it fail.
    """
    source = (ROOT / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    block = source.split("async def _send_room_transcript")[1].split("\ndef ")[0]

    assert '"can": sorted(CAPABILITIES.get(record["role"], ()))' in block


def test_the_viewer_is_identified_by_the_lane_it_acts_on():
    """MUTATION: answer canManage from whichever lane the sidebar row resolved.

    The row resolves the AGENT lane first, and the browser acts as the person on the
    CLI lane. Asking about the wrong one is why the remove buttons stayed hidden from
    the host of their own room.
    """
    source = (ROOT / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    block = source.split("async def _send_room_transcript")[1].split("\ndef ")[0]

    assert 'derive_peer_id(participant_key("cli", user_scope_id)' in block
    assert '"canManage": bool(acting)' in block
    assert 'row.get("role") == "leader"' not in block, "the row's role still decides"


def test_the_panel_strings_are_translated_in_every_language_we_ship():
    import json

    keys = {"roomInfoKind", "roomInfoYou", "roomInfoMembers", "roomInfoCreated",
            "roomInfoHostBadge", "roomInfoYouBadge", "roomInfoLastSeen",
            "roomInfoNeverSeen"}
    for name in ("en", "de"):
        catalogue = json.loads((ROOT / "web" / "messages" / f"{name}.json").read_text(encoding="utf-8"))
        missing = keys - set(catalogue.get("main", {}))
        assert not missing, f"{name}.json is missing {sorted(missing)}"


# ── closing is the only way to clear the list ──────────────────────────────

def test_a_closed_room_leaves_the_sidebar(rooms, tmp_path):
    """MUTATION: keep listing it with a closed flag.

    That was the state, and it is why "I deleted the group chats and they will not go
    away" was a correct report: the bin CLOSED the room and left the row standing, so
    the only icon offering removal removed nothing. A person had no way to clear this
    list at all.
    """
    from vaf.core.a2a.room import Room, derive_peer_id, participant_key

    assert len(_room_rows(SCOPE)) == 1
    room = Room.open("room-visible")
    host = room.identity_for(participant_key("agent", SCOPE))
    assert host is not None
    room.close(host, reason=Room.TERMINATED_BY_USER)

    assert _room_rows(SCOPE) == [], "a closed room is still offered as somewhere to talk"


def test_the_transcript_survives_the_row_going_away(rooms):
    """The other half of the promise: closing takes the conversation out of the live
    list, never out of existence. A room whose transcript vanished with its row would
    make "it stays readable forever" false the moment somebody used the bin."""
    from vaf.core.a2a.room import Room, participant_key

    room = Room.open("room-visible")
    host = room.identity_for(participant_key("agent", SCOPE))
    room.close(host, reason=Room.TERMINATED_BY_USER)

    reopened = Room.open("room-visible")
    assert reopened.closed
    assert [e["kind"] for e in reopened.transcript()].count("say") == 1


# ── the sidebar has to be told, not only the store ─────────────────────────

def test_closing_or_renaming_pushes_a_fresh_sidebar():
    """MUTATION: answer with the transcript alone.

    That was the state, and it is why a closed room kept sitting in the list with its
    own farewell in it. The store had already dropped it; the browser was still holding
    the list it was sent before. Anything that changes what the sidebar SAYS has to
    push the sidebar, and it broadcasts rather than replies, because the same person
    may have the app open twice.
    """
    source = (ROOT / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    block = source.split('elif type in ("room_say", "close_room", "delete_room", "kick_peer", "rename_room"):')[1] \
                  .split('elif type == "load_session"')[0]

    assert 'if type in ("close_room", "delete_room", "rename_room"):' in block
    assert "broadcast_to_user" in block and '"type": "session_list"' in block
    assert "session_list_payload" in block, "the push would drop the room fields again"


def test_a_room_that_leaves_the_list_closes_its_view():
    """MUTATION: leave the view open and wait for a "closed" message.

    Keyed on the LIST rather than on any one event, so it holds however the room went
    away - closed, left, or removed by somebody else. Otherwise the user reads a
    conversation the sidebar says does not exist and types into a composer whose
    messages are refused.
    """
    source = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    block = source.split("else if (data.type === 'session_list') {")[1][:2200]

    assert "setRoomView(prev =>" in block
    assert "s.roomId === prev.room.roomId" in block
    assert "setRoomMembersOpen(false)" in block, "the members panel outlives its room"


def test_the_bin_deletes_the_room_the_way_it_deletes_a_chat():
    """MUTATION: point the bin back at close_room.

    A bin means "gone" everywhere else in this product, and a room that only closed
    stayed on disk - which is why "I deleted it and it is still there" was reported
    twice from two different causes. Closing remains available as a protocol act
    (`vaf a2a close`): it ends the conversation and KEEPS the transcript. The bin does
    the other thing.
    """
    source = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    dialog = source.split("{roomToClose && (")[1].split("{/* Trust Gate Dialog")[0]

    assert "type: 'delete_room'" in dialog
    assert "close_room" not in dialog


def test_the_confirmation_says_it_cannot_be_undone():
    """Deleting a room removes somebody else's transcript as well as your own, so the
    sentence in front of it has to say what it costs and where to keep a copy."""
    import json

    for name, phrase in (("en", "cannot be undone"), ("de", "rueckgaengig")):
        catalogue = json.loads((ROOT / "web" / "messages" / f"{name}.json").read_text(encoding="utf-8"))
        body = catalogue["main"]["roomCloseBody"]
        assert phrase in body, f"{name}: the warning does not say it is permanent"
        assert "vaf a2a export" in body, f"{name}: no way to keep a copy is offered"


# ── a room the AGENT opens has to appear without a reload ──────────────────

def test_opening_or_joining_a_room_tells_the_browser():
    """MUTATION: leave the tools silent.

    Everything else that changes a room happens inside a WebSocket command, which can
    answer on the spot. A room OPENED by the agent has no socket command in flight, so
    nothing looked at the store and the row was missing until the entire interface was
    reloaded by hand - reported exactly that way, and the third time in this round that
    a change to the STORE was not a change to what somebody sees.
    """
    source = (ROOT / "vaf" / "tools" / "room_tools.py").read_text(encoding="utf-8")

    assert "def _announce(" in source
    assert "notify_rooms_changed" in source
    # every path that makes a row appear or change
    assert source.count("_announce(") >= 4, (
        "a tool that changes the room list is not announcing it")


def test_the_signal_carries_no_sidebar_payload():
    """MUTATION: build the session list inside the engine's notifier.

    That would point the dependency the wrong way round - the engine importing the web
    server's projection - to save one round trip. The browser already knows how to ask.
    """
    source = (ROOT / "vaf" / "core" / "web_interface.py").read_text(encoding="utf-8")
    block = source.split("def notify_rooms_changed")[1].split("\ndef ")[0]

    assert '"type": "rooms_changed"' in block
    assert "session_list_payload" not in block and "list_ui" not in block
    assert "web_server" not in block, "the engine is importing the harness"


def test_the_browser_answers_the_signal_by_asking():
    source = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    block = source.split("else if (data.type === 'rooms_changed') {")[1].split("else if")[0]

    assert "type: 'get_sessions'" in block
    assert "wsSocketRef.current" in block, (
        "the captured ws state is null on the first connect; that trap is documented "
        "in this file and cost a silent failure once already")


def test_a_signal_without_a_user_goes_nowhere():
    """The terminal, an automation and a channel run through the same tools. A notifier
    that raised, or broadcast to everybody, would turn a sidebar nicety into a failed
    tool call for something the user never asked about."""
    from vaf.core.web_interface import notify_rooms_changed

    notify_rooms_changed(None)
    notify_rooms_changed("")
