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


def test_the_badge_counts_only_what_the_view_would_show(rooms):
    """MUTATION: filter the badge on BOOKKEEPING_KINDS instead of NON_CONVERSATION_KINDS.

    The browser's room projection deliberately hides `ping` frames - a check-in is
    the room talking to ONE member about its own attention, not something anybody
    said. A badge that counts them lights the sidebar for a frame no view shows:
    the person opens the room, finds nothing new, and the dot comes back with the
    next check-in. Measured live on a day of app restarts, that was a phantom
    notification every few minutes.
    """
    host = rooms.identity_for(participant_key("agent", SCOPE))
    rooms.ping(host, "p-codex")

    assert _room_rows(SCOPE)[0]["unread"] == 1, "the say counts, the check-in must not"


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
    # Tree-wide, because the eighth copy DID appear and it appeared where a
    # guard reading only web_server.py could not see it: the classic CLI pushed
    # its own session_list after every turn, with id/title/date and nothing else.
    # Every browser row then read messageCount as undefined - which the trash
    # icon took for "empty chat, delete it without asking" - and lost the `kind`
    # that keeps a room row out of the session loader.
    copies = sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "vaf").rglob("*.py")
        if '"title": s["name"]' in path.read_text(encoding="utf-8")
    )
    assert copies == ["vaf/core/session.py"], (
        f"a session list is being built by hand again: {copies}")

    source = (ROOT / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
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
    # Both are unique here, so both keep their names as they are. A number appears when
    # two people share a name and not before - it answers a collision.
    assert sorted(listed) == ["Codex", "VAF"]


def test_the_header_renders_the_members_and_marks_our_own(rooms):
    """MUTATION: render members_list without telling our agent apart.

    An agent that is not ours is a full agent of its own. Drawing it the same as ours
    is the one thing this view must never do, because everything a reader concludes
    about who said what depends on it.
    """
    source = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    # The member chips live in RoomIdentity, the one component that draws the
    # room's identity wherever the header band is.
    block = source.split("(room.members_list || []).map(m => (")[1].split("</div>")[0]

    assert "m.peer === room.me" in block, "our own agent is not marked"
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
    # Anchored on the column's own width class rather than on its padding: the
    # padding is a value now (the conversation makes room for the vote panel), and
    # a guard that breaks when a number changes is a guard people learn to edit
    # rather than to read.
    container = source.split("cn(messagesAreaWidthClass,")[1][:1600]

    assert "{roomView ? (" in container, "the room is not branched inside the chat area"
    assert "<RoomConversation" in container


def test_choosing_a_conversation_puts_the_room_away():
    """MUTATION: leave roomView set when a session is picked.

    Both occupy the same place. A chat loaded underneath a room that is still showing
    is a user clicking a conversation and getting no conversation.
    """
    source = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    body = source.split("const handleSessionSwitch = (id: string) => {")[1][:800]

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

    # The surface is named at the call site: only a rename begun in the SIDEBAR may
    # pin the sidebar open, now that the chat header renames through the same lane.
    assert "Edit2" in branch and "startEditing(s, 'sidebar')" in branch
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

    assert "startEditing(s, 'sidebar')" in branch, "the pencil no longer edits in place"
    assert "editingId === s.id && editingWhere === 'sidebar'" in branch, \
        "the row would mount its field while a header rename is open, with two carets"
    assert "submitRename" in branch
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
    # Window sized to hold BOTH markers: the room branch grew (trimmed send +
    # the blend-up bookkeeping), which pushed the chat path's marker past the
    # old 3400 and made index() throw without the invariant ever breaking.
    body = source.split("const sendMessage = async (")[1][:6000]

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
    block = source.split('elif type in ("room_say",')[1].split("elif type == \"load_session\"")[0]
    assert 'participant_key("cli", user_scope_id)' in block


def test_writing_and_closing_answer_from_the_store():
    """MUTATION: echo the message back optimistically instead of re-reading.

    A room has N writers. A browser that repainted from what it ASSUMED had happened
    would drift from the transcript the moment anybody else wrote at the same time,
    and the drift would be invisible until somebody compared two screens.
    """
    source = (ROOT / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    block = source.split('elif type in ("room_say",')[1].split("elif type == \"load_session\"")[0]

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
    header = source.split("function RoomIdentity(")[1].split("\nfunction ")[0]
    block = source.split("function RoomConversation(")[1].split("\nfunction ")[0]

    assert "onMembers" in header and "<Info size={16} />" in header
    assert "onClose" not in header and "onClose" not in block, (
        "the header still carries a way to close the view")


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
    """MUTATION: keep showing the conversation's workspace, or hide the token gauge.

    The workspace folder and the retrieval sources describe the CHAT. With a room
    open they described the one hidden behind it, so a user read another chat's
    workspace while typing into a room - the chat chips stay in the branch a room
    never takes, and the room's own chip opens the ROOM's folder instead.

    The token gauge is the deliberate exception, and it used to be inside the
    branch too: the agent answering in a room is the same main agent with the same
    context window, so hiding its gauge there hid the one number that explains a
    slow or clipped reply.
    """
    source = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    # Sliced to the next structural landmark, not to a character count: the RAG chip
    # between the two claims grows and shrinks with unrelated work, and a count that
    # once fit went stale the first time it did.
    strip = source.split("{/* Token Stats (Clickable) + RAG Badge */}")[1] \
                  .split("Stop button left of message box")[0]

    assert "{roomView ? (" in strip
    assert strip.index("{roomView ? (") < strip.index("workspaceInfo?.path"), (
        "the workspace chip is still rendered for a room")
    assert "refreshWorkspace(roomView.room.roomId" in strip, (
        "the room chip no longer opens the room's own folder")
    assert strip.index("</>)}") < strip.index("contextStats && ("), (
        "the context gauge is back inside the chat-only branch")


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
    block = source.split('elif type in ("room_say",')[1] \
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


def test_an_open_room_refetches_itself_while_it_is_on_screen():
    """MUTATION: rely on a push.

    A room has writers this process cannot see: another agent's CLI, a peer over the
    wire, the agent's own tool call in a different turn. There is nothing in the web
    process to hook, so no push covers all of them - and the symptom was exact: the
    conversation only moved when the person watching typed something themselves, which
    is the one moment a round trip already happens.

    Polling the store is the answer that holds for every writer. It runs only while
    somebody is looking, and stops when the view closes or the room is closed.
    """
    source = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    block = source.split("const roomPollRef")[1].split("const handleSessionSwitch")[0]

    assert "setInterval" in block and "type: 'open_room'" in block
    assert "clearInterval" in block, "the poll outlives the view it belongs to"
    assert "roomView?.room.closed" in block, "a closed room is still being polled"
    assert "wsSocketRef.current" in block, (
        "the captured socket is null on a reconnect; the ref is the documented fix")


def test_a_room_scrolls_with_the_chat_s_own_autoscroll():
    """MUTATION: leave the bottom anchor inside the chat branch.

    It used to live there, so with a room open there was nothing for the autoscroll to
    aim at and the conversation ran off the bottom of the screen. The anchor and the
    effect are the CHAT's - a room does not get a scroll of its own, it gets the one
    that already works.
    """
    source = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")

    # the anchor sits after the branch closes, so both halves render it
    branch_end = source.index("</>)}")
    anchor_at = source.index("<div ref={scrollRef} />", branch_end)
    assert anchor_at > branch_end, "the scroll anchor is inside the chat branch again"

    # and the effect fires for a room's messages too
    assert "}, [messages, loading, roomView?.messages.length]);" in source, (
        "new room messages do not trigger the scroll")


def test_the_room_header_stays_on_screen():
    """MUTATION: let it scroll away with the messages.

    It is the only thing that says WHICH room this is and who is in it, and it was the
    first thing gone in a conversation of any length. Frosted rather than opaque so the
    messages sliding under it stay visible: it belongs to the conversation rather than
    sitting on top of one.
    """
    source = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    block = source.split("function RoomConversation(")[1].split("\nfunction ")[0]

    assert "sticky top-0" in block
    assert "backdrop-blur" in block
    assert "supports-[backdrop-filter]" in block, (
        "no fallback where the browser cannot blur, so the header would be see-through")


def test_at_completes_room_members_while_a_room_is_open():
    """MUTATION: keep offering workflows.

    In a room "@" means a PERSON - it is the addressing rule, a leading mention wakes
    exactly that participant. Offering workflows there answered a question nobody asked
    while hiding the only list that matters, so the name had to be typed exactly by
    hand. Whatever is open decides what "@" completes.
    """
    source = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    block = source.split("} else if (lastWord.startsWith('@')) {")[1].split("} else {")[0]

    assert "if (roomView) {" in block
    assert "members_list" in block
    # Against the CODE, not against the prose: the word "workflows" appears in the
    # comment above the branch, so anchoring on the bare word measured my own sentence.
    assert block.index("if (roomView) {") < block.index("const filtered = workflows"), (
        "the workflow list is still consulted first")
    assert "m.peer !== roomView.room.me" in block, "it offers to address yourself"


def test_the_browser_corrects_a_lane_name_to_the_account_name():
    """MUTATION: leave whatever the terminal wrote - or heal it only when the person ACTS.

    "terminal" is a LANE, not a person, so somebody who joined from a shell sat in the
    room named after the thing they typed into. Their own member file is the one file
    they are the authoritative writer for, so it is theirs to correct.

    The heal lived in the command branch first, and the owner stayed "terminal"
    through two rounds of fixing it, because reading a room is what a person does
    most and the command branch only runs when they act. So the heal lives in the
    transcript builder, which every room command answers through, and the command
    branch must NOT grow a second copy that would drift from it.
    """
    source = (ROOT / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    builder = source.split("async def _send_room_transcript")[1].split("\ndef ")[0]

    assert "room.introduce(healed, display=wanted_name)" in builder
    assert '("terminal", "guest", "")' in builder, (
        "a name the person chose would be overwritten too")

    commands = source.split('elif type in ("room_say",')[1] \
                     .split('elif type == "load_session"')[0]
    assert "wanted_name" not in commands, (
        "a second copy of the heal is growing in the command branch")


def test_looking_at_a_room_heals_a_lane_literal_name(tmp_path, monkeypatch):
    """MUTATION: move the heal back into the command branch.

    The member record for the person was written by an older CLI as the lane literal
    "terminal". The owner of that room only ever READ it in the browser - reading is
    what a person does most - and the name stayed wrong through two rounds of fixing
    the write paths, because every fix sat on a path that only runs when they ACT.
    The transcript builder is the one place every room command answers through, so
    the heal lives there and nowhere else.
    """
    import asyncio

    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path)
    import vaf.core.config as config_mod
    monkeypatch.setattr(config_mod, "get_local_admin_scope_id", lambda: "scope-a")
    monkeypatch.setattr(config_mod, "get_local_admin_username", lambda: "Alice")

    room = Room.create(kind="round", owner_scope="scope-a", base=tmp_path,
                       room_id="room-heal")
    key = participant_key("cli", "scope-a")
    stale = derive_peer_id(key, "room-heal")
    room.join(display="terminal", scope_id="scope-a", peer_id=stale)

    from vaf.core.web_server import _send_room_transcript

    class _WS:
        def __init__(self):
            self.sent = []

        async def send_json(self, payload):
            self.sent.append(payload)

    ws = _WS()
    asyncio.run(_send_room_transcript(ws, Room.open("room-heal", base=tmp_path),
                                      "scope-a"))

    assert (room.store.member(stale) or {}).get("display") == "Alice"
    listed = ws.sent[0]["room"]["members_list"]
    assert any(m["label"] == "Alice" for m in listed), (
        "healed on disk but the payload still paints the stale name")


def test_the_heal_never_writes_someone_elses_name(tmp_path, monkeypatch):
    """MUTATION: drop the admin-scope check from the heal.

    The only name the harness knows is the local admin's. Writing it onto a member
    acting for ANOTHER account would rename that person to somebody they are not -
    so the heal stops at the one account whose name it actually has, and the
    boundary moves the day cross-account rooms bring a per-user name lookup.
    """
    import asyncio

    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path)
    import vaf.core.config as config_mod
    monkeypatch.setattr(config_mod, "get_local_admin_scope_id", lambda: "scope-a")
    monkeypatch.setattr(config_mod, "get_local_admin_username", lambda: "Alice")

    room = Room.create(kind="round", owner_scope="scope-b", base=tmp_path,
                       room_id="room-heal2")
    key = participant_key("cli", "scope-b")
    stale = derive_peer_id(key, "room-heal2")
    room.join(display="terminal", scope_id="scope-b", peer_id=stale)

    from vaf.core.web_server import _send_room_transcript

    class _WS:
        async def send_json(self, payload):
            pass

    asyncio.run(_send_room_transcript(_WS(), Room.open("room-heal2", base=tmp_path),
                                      "scope-b"))

    assert (room.store.member(stale) or {}).get("display") == "terminal", (
        "another account's member was renamed to the admin")


def test_reading_is_a_receipt_and_only_keys_or_turn_compose(tmp_path, monkeypatch):
    """MUTATION: paint a reader as typing again, drop the receipt, drop the
    viewer exclusion, or let a keypress live past its window.

    The old rule derived "took the newest message recently, answered nothing"
    into the typing list for two minutes at a time, so an agent that merely
    monitors its room looked permanently busy - watched live for hours. Reading
    is a READ RECEIPT now (the view stacks it under the last message the
    reader's cursor covers), and composing is exactly two things: a keypress a
    browser reported, or the agent's live turn (pinned by the test below).
    """
    import asyncio
    import time as time_mod

    import vaf.core.web_server as web_server_mod
    from vaf.core.web_server import _send_room_transcript

    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path)
    monkeypatch.setattr(web_server_mod, "_ROOM_KEYS_TYPING", {})
    room = Room.create(kind="round", owner_scope="scope-a", base=tmp_path,
                       room_id="room-typing")
    me_key = participant_key("cli", "scope-a")
    me = derive_peer_id(me_key, "room-typing")
    mine = room.join(display="Alice", scope_id="scope-a", peer_id=me)
    room.join(display="Codex", scope_id=None, peer_id="p-codex")

    latest = room.say(mine, "anyone there?")

    class _WS:
        def __init__(self):
            self.sent = []

        async def send_json(self, payload):
            self.sent.append(payload)

    def projection():
        ws = _WS()
        asyncio.run(_send_room_transcript(ws, Room.open("room-typing", base=tmp_path),
                                          "scope-a"))
        payload = ws.sent[0]["room"]
        return ({t["peer"]: t for t in payload["typing"]},
                {r["peer"]: r for r in payload["readPositions"]})

    # Nobody has read anything: no bubbles, no receipts.
    typing, receipts = projection()
    assert typing == {} and receipts == {}

    # Codex takes the newest message the way every reader does - cursor AFTER
    # the frame is in hand. That is a receipt saying where it stands, and it is
    # NEVER a typing bubble again.
    room.store.set_cursor("p-codex", latest.lamport)
    typing, receipts = projection()
    assert typing == {}, "a mere reader was painted as composing"
    assert receipts["p-codex"]["readTo"] == latest.lamport
    assert me not in receipts, ("the viewer's own position is noise - they know "
                                "what they have read")

    # A keypress composes, and dies by the clock seconds after the last key.
    web_server_mod._ROOM_KEYS_TYPING.setdefault("room-typing", {})["p-codex"] = time_mod.time()
    typing, _ = projection()
    assert typing["p-codex"]["kind"] == "keys"
    real_time = time_mod.time
    monkeypatch.setattr(time_mod, "time", lambda: real_time() + 30.0)
    typing, _ = projection()
    assert "p-codex" not in typing, "the dots outlived the typing"


def test_the_agents_running_room_turn_is_the_precise_signal(tmp_path, monkeypatch):
    """MUTATION: derive the own agent from cursors too.

    The agent's cursor only advances AFTER its turn, so a cursor-derived bubble for
    it would appear exactly when it stopped being true. The runner's turn marker is
    live while the turn runs, and only the marker may put the agent in the list.
    """
    import asyncio

    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path)
    room = Room.create(kind="round", owner_scope="scope-a", base=tmp_path,
                       room_id="room-turnsig")
    agent_peer = derive_peer_id(participant_key("agent", "scope-a"), "room-turnsig")
    room.join(display="Nobel", scope_id="scope-a", peer_id=agent_peer)
    guest = room.join(display="Codex", scope_id=None, peer_id="p-codex")
    room.say(guest, "Nobel?")

    import vaf.core.web_server as web_server_mod

    class _Agent:
        _room_turn = {"room_id": "room-turnsig", "mode": "assist"}

    # On the INSTANCE, not the class: the WebInterface is a singleton and patching
    # anywhere else has bitten this suite before.
    monkeypatch.setattr(web_server_mod.manager, "agent_instance", _Agent(),
                        raising=False)

    class _WS:
        def __init__(self):
            self.sent = []

        async def send_json(self, payload):
            self.sent.append(payload)

    ws = _WS()
    asyncio.run(web_server_mod._send_room_transcript(
        ws, Room.open("room-turnsig", base=tmp_path), "scope-a"))
    listed = {t["peer"]: t for t in ws.sent[0]["room"]["typing"]}
    assert agent_peer in listed and listed[agent_peer]["kind"] == "turn"

    # Marker for ANOTHER room: this room shows nothing.
    _Agent._room_turn = {"room_id": "room-elsewhere", "mode": "assist"}
    ws2 = _WS()
    asyncio.run(web_server_mod._send_room_transcript(
        ws2, Room.open("room-turnsig", base=tmp_path), "scope-a"))
    assert agent_peer not in {t["peer"] for t in ws2.sent[0]["room"]["typing"]}

    # The half the first version of this test missed, found live: right after a
    # turn the runner advances the agent's cursor - including a turn in which it
    # DELIBERATELY said nothing (the thank-you brake). Marker gone plus cursor at
    # the newest is exactly the derived "is typing" shape, so without its own
    # exclusion the agent showed as composing for the whole window at precisely
    # the moment it had decided to stay quiet.
    _Agent._room_turn = None
    room.store.set_cursor(agent_peer, room.store.highest_lamport())
    ws3 = _WS()
    asyncio.run(web_server_mod._send_room_transcript(
        ws3, Room.open("room-turnsig", base=tmp_path), "scope-a"))
    assert agent_peer not in {t["peer"] for t in ws3.sent[0]["room"]["typing"]}, (
        "the agent's own silence is painted as composing")


def test_the_payload_names_the_viewers_own_agent_and_nobody_else(tmp_path, monkeypatch):
    """MUTATION: hand the agent treatment to any agent-shaped member.

    The view draws the viewer's own agent with the living avatar. That face is an
    identity: a foreign agent wearing it would put our face on somebody else's
    words, so the payload only ever names the handle derived from the VIEWER'S
    scope, and only when that handle is actually in the room.
    """
    import asyncio

    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path)
    room = Room.create(kind="round", owner_scope="scope-a", base=tmp_path,
                       room_id="room-face")
    agent_peer = derive_peer_id(participant_key("agent", "scope-a"), "room-face")
    room.join(display="Nobel", scope_id="scope-a", peer_id=agent_peer)
    room.join(display="Codex", scope_id=None, peer_id="p-codex")

    from vaf.core.web_server import _send_room_transcript

    class _WS:
        def __init__(self):
            self.sent = []

        async def send_json(self, payload):
            self.sent.append(payload)

    ws = _WS()
    asyncio.run(_send_room_transcript(ws, Room.open("room-face", base=tmp_path),
                                      "scope-a"))
    assert ws.sent[0]["room"]["agentPeer"] == agent_peer

    # Another account looking at a room their agent is NOT in: no face for anybody.
    ws2 = _WS()
    asyncio.run(_send_room_transcript(ws2, Room.open("room-face", base=tmp_path),
                                      "scope-b"))
    assert ws2.sent[0]["room"]["agentPeer"] == ""


def test_the_badge_counts_what_the_person_has_not_seen_and_looking_clears_it(tmp_path, monkeypatch):
    """MUTATION: count the agent lane's backlog, or advance the cursor before sending.

    The badge stayed red after the person had read everything, because it counted
    the AGENT'S unread - and the agent's cursor only moves when its turn runs.
    The sidebar is the person's surface: its number counts from the person's own
    reading position, and looking at the room IS reading it, so the transcript
    builder moves that position - after the transcript went out, never before.
    """
    import asyncio

    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path)
    room = Room.create(kind="round", owner_scope=SCOPE, base=tmp_path,
                       room_id="room-badge")
    agent_peer = derive_peer_id(participant_key("agent", SCOPE), "room-badge")
    room.join(display="VAF", scope_id=SCOPE, peer_id=agent_peer)
    codex = room.join(display="Codex", scope_id=None, peer_id="p-codex")
    room.say(codex, "news for everyone")

    assert _room_rows(SCOPE)[0]["unread"] == 1

    from vaf.core.web_server import _send_room_transcript

    class _WS:
        async def send_json(self, payload):
            pass

    asyncio.run(_send_room_transcript(_WS(), Room.open("room-badge", base=tmp_path),
                                      SCOPE))

    # The person looked; the badge goes out - even though the AGENT's own cursor
    # never moved (its wake lane is untouched by a human reading along).
    assert _room_rows(SCOPE)[0]["unread"] == 0

    # The person's own words are not news to them either.
    human = derive_peer_id(participant_key("cli", SCOPE), "room-badge")
    me = room.join(display="Alice", scope_id=SCOPE, peer_id=human)
    room.say(me, "my own line")
    assert _room_rows(SCOPE)[0]["unread"] == 0

    # AFTER, never before: a transcript that never reached the browser was not
    # read, so a failed send must leave the badge standing. This is the half a
    # never-failing fake cannot see, and the half the mutation flips.
    room.say(codex, "more news")
    assert _room_rows(SCOPE)[0]["unread"] == 1

    class _DeadWS:
        async def send_json(self, payload):
            raise ConnectionError("socket gone")

    with pytest.raises(ConnectionError):
        asyncio.run(_send_room_transcript(_DeadWS(), Room.open("room-badge", base=tmp_path),
                                          SCOPE))
    assert _room_rows(SCOPE)[0]["unread"] == 1, (
        "the cursor moved past a transcript nobody received")


def test_the_task_board_travels_with_the_transcript(tmp_path, monkeypatch):
    """MUTATION: leave tasks out of the payload.

    The board is derived server-side; a browser cannot rebuild it (it has no
    reply_to fold). If the field is dropped here, the WebUI card renders never
    and silently - the exact class of loss the wholesale room assignment was
    chosen to prevent.
    """
    import asyncio

    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path)
    room = Room.create(kind="round", owner_scope="scope-a", base=tmp_path,
                       room_id="room-boardpay")
    alice = room.join(display="Alice", scope_id="scope-a", peer_id="p-alice")
    nobel = room.join(display="Nobel", scope_id=None, peer_id="p-nobel")
    ask = room.say(alice, "build the site")
    room.ingest({"kind": "report", "reply_to": ask.id,
                 "body": {"status": "working"}}, identity=nobel)

    from vaf.core.web_server import _send_room_transcript

    class _WS:
        def __init__(self):
            self.sent = []

        async def send_json(self, payload):
            self.sent.append(payload)

    ws = _WS()
    asyncio.run(_send_room_transcript(ws, Room.open("room-boardpay", base=tmp_path),
                                      "scope-a"))
    board = ws.sent[0]["room"]["tasks"]
    assert len(board) == 1
    assert board[0]["status"] == "working"
    assert board[0]["title"] == "build the site"
    assert board[0]["assignee"].startswith("Nobel")


def test_the_mode_switch_reaches_only_the_viewers_own_agent(tmp_path, monkeypatch):
    """MUTATION: take a peer id from the command instead of deriving it.

    The mode is the user's standing decision about THEIR agent, and this is its
    one control surface. The handle is derived from the caller's scope, so
    another account's agent is unreachable by construction - a command that
    accepted a peer id would undo exactly that.
    """
    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path)
    room = Room.create(kind="round", owner_scope="scope-a", base=tmp_path,
                       room_id="room-mode")
    agent_peer = derive_peer_id(participant_key("agent", "scope-a"), "room-mode")
    room.join(display="Nobel", scope_id="scope-a", peer_id=agent_peer)

    assert room.mode_of(agent_peer) == "assist"

    # The command's core, exercised at the same seam the handler uses.
    identity = room.identity_for(participant_key("agent", "scope-a"))
    assert identity is not None
    room.set_mode(identity, "autonomous")
    assert room.mode_of(agent_peer) == "autonomous"

    # Another account derives a DIFFERENT handle, so its lookup finds nothing
    # here - there is no way to name this agent from that side.
    assert room.identity_for(participant_key("agent", "scope-b")) is None

    # And the payload carries the decision to the panel that shows it.
    import asyncio
    from vaf.core.web_server import _send_room_transcript

    class _WS:
        def __init__(self):
            self.sent = []

        async def send_json(self, payload):
            self.sent.append(payload)

    ws = _WS()
    asyncio.run(_send_room_transcript(ws, Room.open("room-mode", base=tmp_path),
                                      "scope-a"))
    assert ws.sent[0]["room"]["agentMode"] == "autonomous"


def test_worker_cards_show_only_the_viewers_own_workers(monkeypatch):
    """MUTATION: show every active task, or trust a task with no session.

    The IPC active file is global to every user and a task's description is user
    content, so the room's worker cards are a cross-user leak the moment the
    filter softens. A task is shown only when its session PROVABLY belongs to
    the viewer; no session id means no card, whoever is asking.
    """
    import vaf.core.web_server as ws_mod
    from vaf.core.web_server import _viewer_agent_workers

    class _Task:
        def __init__(self, sid, desc):
            self.session_id = sid
            self.agent_type = "coding_agent"
            self.status = "running"
            self.task_description = desc
            self.progress_done = 1
            self.progress_total = 3

    class _Ipc:
        def get_active_tasks(self, _sid):
            return [_Task("sess-mine", "my private build"),
                    _Task("sess-theirs", "their private build"),
                    _Task(None, "orphaned build")]

    import vaf.core.subagent_ipc as ipc_mod
    monkeypatch.setattr(ipc_mod, "get_ipc", lambda: _Ipc())

    class _Session:
        def __init__(self, scope):
            self.metadata = {"user_scope_id": scope}

    def _load(sid):
        return {"sess-mine": _Session("scope-a"),
                "sess-theirs": _Session("scope-b")}[sid]

    monkeypatch.setattr(ws_mod.session_mgr, "load", _load)

    cards = _viewer_agent_workers("scope-a")
    assert [c["task"] for c in cards] == ["my private build"]
    assert cards[0]["type"] == "coding_agent"
    assert cards[0]["done"] == 1 and cards[0]["total"] == 3

    assert _viewer_agent_workers("scope-b") and \
        _viewer_agent_workers("scope-b")[0]["task"] == "their private build"


def test_a_room_turns_live_feed_travels_per_user_with_the_room_stamp(monkeypatch):
    """MUTATION: keep broadcasting per session while a room turn runs.

    Measured live: a real coder run looked like a hung one, because its updates
    went to the turn's SESSION subscribers and the person was watching the ROOM.
    While the agent's room-turn marker is up, the event is stamped with the room
    and sent per USER - which reaches the session subscribers too, reaches the
    room watcher, and never crosses an account boundary. Patched ON THE INSTANCE:
    the WebInterface is a singleton, and patching anywhere else has bitten this
    suite before.
    """
    import vaf.core.web_interface as wi_mod
    wi = wi_mod.get_web_interface()

    class _Agent:
        _room_turn = {"room_id": "room-live", "mode": "autonomous"}
        _current_user_scope_id = "scope-a"

    scheduled = []

    def _fake_sched(coro, loop):
        scheduled.append(coro.__qualname__)
        coro.close()
        return object()

    monkeypatch.setattr(wi, "agent_instance", _Agent(), raising=False)
    monkeypatch.setattr(wi, "_get_dispatch_loop", lambda: object())
    monkeypatch.setattr(wi_mod.asyncio, "run_coroutine_threadsafe", _fake_sched)

    data = {"type": "subagent_update", "status": "running"}
    wi._push_session_update("sess-1", data)
    assert data.get("roomId") == "room-live", "the room stamp never made it on"
    assert scheduled and "broadcast_to_user" in scheduled[-1], (
        "the feed still travels per session during a room turn")

    # No room turn: the session lane stays exactly what it was.
    monkeypatch.setattr(wi.agent_instance, "_room_turn", None, raising=False)
    data2 = {"type": "subagent_update", "status": "running"}
    wi._push_session_update("sess-1", data2)
    assert "roomId" not in data2
    assert "broadcast_to_session" in scheduled[-1]


def test_the_context_gauge_and_the_room_messages_follow_the_open_view():
    """MUTATION: merge context_status ungated again, or drop the room's entry
    animation.

    Two things a room lacked next to the chat. The gauge took whatever context
    report arrived last, from any session, so an open room showed some other
    conversation's numbers. And the room's messages appeared from nowhere while
    the view scrolled, because the chat's entry animation had never been
    carried over - the room is not a lesser surface, it gets the same one.
    """
    source = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    gauge = source.split("data.type === 'context_status'", 1)[1][:700]
    assert "eventBelongsHere(data, activeSessionId, 'worker')" in gauge, (
        "the context gauge accepts reports from any conversation again")
    # The entry animation is conditional now: a message whose text stood here
    # as the person's own pending copy a second ago BLENDS UP in place
    # (room-msg-confirm) instead of drifting in; everything else still enters.
    assert '"room-msg-confirm" : "room-msg-enter"' in source, (
        "the room's messages lost their entry/blend-up animation pair")
    # The app's OTHER animations (58 sites) come from the plugin, which was
    # missing for so long that every one of them was inert. A plugin list that
    # loses it again takes them all down silently - Tailwind drops an unknown
    # utility without a word, which is exactly why nobody noticed.
    cfg = (ROOT / "web" / "tailwind.config.ts").read_text(encoding="utf-8")
    assert 'require("tailwindcss-animate")' in cfg, (
        "the animate plugin is gone; every animate-in / fade-in / zoom-in "
        "class in the app silently stops animating")
    pkg = (ROOT / "web" / "package.json").read_text(encoding="utf-8")
    assert "tailwindcss-animate" in pkg, "the plugin is configured but not a dependency"
    licenses = (ROOT / "web" / "lib" / "licenses_data.ts").read_text(encoding="utf-8")
    assert "tailwindcss-animate" in licenses, (
        "a bundled third-party component is missing from the in-app licence list")
    css = (ROOT / "web" / "app" / "globals.css").read_text(encoding="utf-8")
    assert "@keyframes roomMessageEnter" in css and ".room-msg-enter" in css, (
        "the animation must be a REAL keyframe: the app's animate-in / "
        "fade-in / slide-in-from-* utilities do nothing at all, because "
        "tailwindcss-animate is not installed and the plugin list is empty")
    frames = css.split("@keyframes roomMessageEnter", 1)[1][:200]
    assert "opacity" in frames and "transform" in frames, "the drift is gone"
    for costly in ("box-shadow", "width", "height", "top:", "left:"):
        assert costly not in frames, (
            f"{costly} in a keyframe repaints every frame - transform and "
            "opacity only (the avatar leak's lesson)")


def test_a_frozen_room_stops_claiming_somebody_is_typing():
    """MUTATION: render view.room.typing directly again.

    Presence is a claim about NOW; the transcript is the last poll's snapshot.
    With the socket down the payload freezes, and the room went on insisting an
    agent was composing - measured live after a restart, where the last payload
    happened to catch one mid-turn, so a typing bubble sat there for good. A
    conversation may go stale, a liveness signal may not, and the header says
    which of the two the reader is looking at.
    """
    source = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    assert "const typing = connected ? (view.room.typing ?? []) : [];" in source, (
        "the room's typing row is no longer gated on the connection")
    assert "view.room.typing!.map" not in source, (
        "a raw typing render is back - a frozen payload will lie again")
    assert "connected={isConnected}" in source, (
        "the room view is never told whether its socket is up")


def test_every_worker_feed_lights_the_rooms_card():
    """MUTATION: feed the room's live card from subagent_update alone again.

    A browser run in a room speaks browser_state and never a subagent_update -
    measured live: the window filled, the transcript stayed cardless. Research,
    document, librarian and learn runs have the same shape. One place notices
    that work is alive in the open room, whatever feed it speaks, and the card
    ages out on its own because those feeds have no 'idle' event to end them.
    """
    source = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    for feed in ("coder_state", "research_state", "document_state",
                 "librarian_state", "browser_state", "learn_state"):
        assert f"{feed}: '" in source, f"{feed} no longer lights the room card"
    assert "Date.now() - prev.at > 20000" in source, (
        "the card no longer ages out, so a feed that just stops leaves it "
        "pulsing forever")


def test_one_filter_decides_what_the_open_view_receives():
    """MUTATION: hand-roll the session comparison in any handler again.

    A room is a VIEW, not an exception to sixteen copies of the same condition.
    That is what the copies cost, measured: the socket's master filter and each
    handler carried their own `data.sessionId !== activeSessionId`, eight had
    grown a room clause and eight had not - so half the sub-agent surfaces
    (tool window, artifacts, console output) were dead in a room while the
    other half worked, and the master filter dropped everything whenever the
    chat behind the room was a different session, which it usually is. One
    filter answers it now, with the only distinction that is real: worker
    feeds are shown beside whatever is open, chat lanes belong to one
    conversation.
    """
    source = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    assert "data.sessionId !== activeSessionId" not in source, (
        "a hand-rolled session gate is back; every lane must call "
        "eventBelongsHere so the room stays a first-class view")
    # The same gate written against the STATE instead of the local const hid
    # five more copies from the first sweep - including the one that feeds the
    # context gauge, which is why a room showed another conversation's numbers.
    assert "data.sessionId !== currentSessionId" not in source, (
        "a hand-rolled gate under the other variable name is back")
    assert "const eventBelongsHere = (" in source, "the one filter is gone"
    assert source.count("eventBelongsHere(data, activeSessionId, 'worker')") >= 10, (
        "worker feeds no longer share the room-aware lane")
    assert "lane === 'chat'" in source, (
        "the chat lane no longer refuses a foreign conversation")


def test_the_docks_close_button_survives_a_streaming_run():
    """MUTATION: put the bare isOpen=false back into the dock's onClose.

    A close that does not set the user-closed flag is undone by the very next
    streamed event - measured live in a room run, where the coder never pauses
    long enough for a bare close to stick. The dock's X must go through
    closeSubAgentWindow(true), the one primitive that records the user's
    decision."""
    source = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    dock = source.split("<SubAgentWindow", 1)[1][:1200]
    assert "closeSubAgentWindow(true)" in dock, (
        "the dock's onClose no longer records the user's close")


def test_an_empty_new_chat_does_not_paint_its_hero_into_an_open_room():
    """MUTATION: drop the room guard from the empty-chat hero or its wrapper.

    `messages` is the CHAT's array and a room never fills it: a fresh chat has
    none, so with a room open the composer wrapper centered itself mid-screen
    and the welcome hero (avatar, greeting, "start a conversation") rendered on
    top of the room's transcript - measured live as two views blended into one.
    Both the wrapper's centering ternary and the hero's render gate must treat
    an open room as not-an-empty-chat.
    """
    source = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    assert source.count("messages.length === 0 && !roomView") >= 2, (
        "the empty-chat hero or its wrapper lost the open-room guard")


class _WatcherSocket:
    """A connected browser, reduced to what the broadcast maps read and write."""

    def __init__(self):
        self.received = []

    async def send_text(self, text):
        import json
        self.received.append(json.loads(text))


def _wire_room_task_world(monkeypatch, wi):
    """Three sockets, one room-ordered task, patched ON THE INSTANCE.

    The world the empty-window incident happened in: the coder subprocess's task
    is in the IPC active list carrying the room that ordered it, the person
    watching the room holds a user-registered socket with NO session
    subscription, and nobody at all is subscribed to the turn's session.
    """
    watcher = _WatcherSocket()   # the owner, looking at the room
    foreign = _WatcherSocket()   # another account, must never see anything
    plain = _WatcherSocket()     # same account, subscribed to an ordinary chat

    monkeypatch.setattr(wi, "active_connections", [watcher, foreign, plain])
    monkeypatch.setattr(wi, "connection_users",
                        {watcher: "scope-a", foreign: "scope-b", plain: "scope-a"})
    monkeypatch.setattr(wi, "connection_sessions", {plain: "sess-plain"})
    monkeypatch.setattr(wi, "connection_usernames", {})
    monkeypatch.setattr(wi, "connection_roles", {})
    monkeypatch.setattr(wi, "_room_route_cache", None, raising=False)

    class _Task:
        def __init__(self, sid, room):
            self.session_id = sid
            self.room_id = room

    class _Ipc:
        def get_active_tasks(self, _sid=None):
            return [_Task("sess-room-turn", "room-live-x"),
                    _Task("sess-plain", None)]

        def get_pending_tasks(self, _sid=None):
            return []

    import vaf.core.subagent_ipc as ipc_mod
    monkeypatch.setattr(ipc_mod, "get_ipc", lambda: _Ipc())

    class _Session:
        def __init__(self, scope):
            self.metadata = {"user_scope_id": scope}

    class _Sessions:
        def load(self, sid):
            return {"sess-room-turn": _Session("scope-a"),
                    "sess-plain": _Session("scope-a")}[sid]

    import vaf.core.session as session_mod
    monkeypatch.setattr(session_mod, "get_manager", lambda: _Sessions())
    return watcher, foreign, plain


def test_a_subprocess_workers_bridge_events_reach_the_room_watcher(monkeypatch):
    """MUTATION: broadcast bridged sub-agent events per session only.

    The empty-window incident, measured live: a coder subprocess spawned by a
    room turn streams its ENTIRE run through POST /api/subagent/stream, and the
    endpoint broadcast per session - to a session no browser watches, because
    the person is watching the ROOM. The room-turn marker cannot help there: it
    dies with the turn, minutes before the subprocess finishes. The durable
    truth is the IPC task record, which carries the room that ordered the work,
    so the bridge resolves session -> active room task -> owner scope, stamps
    the room, and sends per user. Without a room task the session lane stays
    exactly what it was.
    """
    import asyncio

    import vaf.core.web_interface as wi_mod
    import vaf.core.web_server as ws_mod

    wi = wi_mod.get_web_interface()
    watcher, foreign, plain = _wire_room_task_world(monkeypatch, wi)

    update = ws_mod.SubAgentStreamUpdate(
        type="coder_state", sessionId="sess-room-turn",
        status="Editing room_stats.py")
    asyncio.run(ws_mod.receive_subagent_stream(update))

    assert watcher.received and watcher.received[-1]["type"] == "coder_state", (
        "the room watcher never saw the subprocess event - the window stays "
        "empty exactly like the live incident")
    assert watcher.received[-1].get("roomId") == "room-live-x", (
        "without the room stamp the browser's room gate drops the event")
    assert foreign.received == [], "another account saw a bridged event"
    # The same account's ordinary chat socket is reached too (per-user covers it).
    assert plain.received and plain.received[-1]["type"] == "coder_state"


def test_a_bridge_event_without_a_room_task_keeps_the_session_lane(monkeypatch):
    """MUTATION: stamp every bridged event, or route per user unconditionally.

    An ordinary chat's sub-agent run has a task with no room on it: its events
    must travel exactly as before - per session, unstamped - or every chat
    would paint into whatever room happens to be open.
    """
    import asyncio

    import vaf.core.web_interface as wi_mod
    import vaf.core.web_server as ws_mod

    wi = wi_mod.get_web_interface()
    watcher, foreign, plain = _wire_room_task_world(monkeypatch, wi)

    update = ws_mod.SubAgentStreamUpdate(
        type="subagent_update", sessionId="sess-plain", status="running")
    asyncio.run(ws_mod.receive_subagent_stream(update))

    assert watcher.received == [], "an unroomed event leaked to the room watcher"
    assert foreign.received == []
    assert plain.received and plain.received[-1]["type"] == "subagent_update"
    assert "roomId" not in plain.received[-1]


def test_a_sessionless_room_event_reaches_the_rooms_tenant(monkeypatch):
    """MUTATION: route a room-stamped event by session only, or trust an
    unresolvable room id.

    The decisive live incident: a room turn ran with NO session at all (the
    runner's room frame binds no chat), so its coder's events either died at a
    session gate or fell to the global lane unstamped - the window stayed
    empty however often the routing behind the session was repaired. The
    producers now stamp the ordering room on the event itself, and the bridge
    routes room-first: per user to the room's PROVEN tenant. An id whose room
    cannot be resolved loses the stamp and never widens delivery.
    """
    import asyncio

    import vaf.core.web_interface as wi_mod
    import vaf.core.web_server as ws_mod

    wi = wi_mod.get_web_interface()
    watcher, foreign, plain = _wire_room_task_world(monkeypatch, wi)
    monkeypatch.setattr(wi, "_room_owner_cache", {}, raising=False)
    monkeypatch.setattr(
        wi, "room_owner_scope",
        lambda rid: "scope-a" if str(rid) == "room-live-x" else None)

    # Sessionless, room-stamped: the shape a sessionless room turn produces.
    update = ws_mod.SubAgentStreamUpdate(
        type="coder_state", status="Editing", roomId="room-live-x")
    asyncio.run(ws_mod.receive_subagent_stream(update))

    assert watcher.received and watcher.received[-1]["type"] == "coder_state", (
        "the room's tenant never saw the sessionless event")
    assert watcher.received[-1].get("roomId") == "room-live-x"
    assert foreign.received == [], "another account saw a room event"

    # An unresolvable room: stamp dropped, delivery falls to the global lane
    # (everyone connected), never to a narrowed user it cannot prove.
    update2 = ws_mod.SubAgentStreamUpdate(
        type="coder_state", status="Editing", roomId="room-forged")
    asyncio.run(ws_mod.receive_subagent_stream(update2))
    assert watcher.received[-1].get("roomId") != "room-forged", (
        "a forged room id survived onto a delivered event")


def test_the_in_process_lane_falls_back_to_the_task_room_after_the_turn(monkeypatch):
    """MUTATION: rely on the live room-turn marker alone.

    Delegated in-process workers run AFTER the turn that ordered them - the
    marker is already gone while they stream. The task record is the durable
    source: with no marker up, _push_session_update resolves the room from the
    active task and routes per user, same as the bridge.
    """
    import vaf.core.web_interface as wi_mod

    wi = wi_mod.get_web_interface()
    watcher, foreign, plain = _wire_room_task_world(monkeypatch, wi)

    class _Agent:
        _room_turn = None
        _current_user_scope_id = "scope-a"

    scheduled = []

    def _fake_sched(coro, loop):
        scheduled.append(coro.__qualname__)
        coro.close()
        return object()

    monkeypatch.setattr(wi, "agent_instance", _Agent(), raising=False)
    monkeypatch.setattr(wi, "_get_dispatch_loop", lambda: object())
    monkeypatch.setattr(wi_mod.asyncio, "run_coroutine_threadsafe", _fake_sched)

    data = {"type": "research_state", "status": "searching"}
    wi._push_session_update("sess-room-turn", data)
    assert data.get("roomId") == "room-live-x", (
        "the post-turn in-process feed lost the room stamp")
    assert scheduled and "broadcast_to_user" in scheduled[-1]

    data2 = {"type": "research_state", "status": "searching"}
    wi._push_session_update("sess-plain", data2)
    assert "roomId" not in data2
    assert "broadcast_to_session" in scheduled[-1]


def test_progress_travels_to_the_browser_and_draws_the_dots():
    """MUTATION: leave progress out of the transcript's task payload, or keep
    drawing three status dots when a count was reported.

    The payload is rebuilt field by field, which is exactly how `diffs` and
    `activity` were lost twice before - a new field has to be named or it
    vanishes without a word. And the card must draw what the worker SAID: three
    dots at two-thirds is this surface guessing, "3 of 5" is a fact.
    """
    server = (ROOT / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    # The WHOLE block, not a fixed window: a slice long enough today is a slice that
    # silently stops covering the field the moment a line is added above it, and this
    # guard exists precisely because fields go missing unnoticed.
    payload = server.split('"tasks": [', 1)[1].split("for t in room.tasks()", 1)[0]
    assert '"progress": t.get("progress")' in payload, (
        "the task payload drops progress before it reaches the browser")

    page = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    assert "progress?: { done?: number; total?: number; step?: string }" in page, (
        "the room view has no type for progress, so it cannot render it")
    card = page.split("view.room.tasks!.map", 1)[1][:3400]
    assert "const counted =" in card and "p!.total!" in card, (
        "the dots ignore a reported count")
    assert "p.step ?" in card or "p?.step" in card, (
        "the card never shows what the worker is doing right now")


def test_a_room_turn_reports_to_the_account_that_is_running_it(monkeypatch):
    """MUTATION: route a sessionless room event by the room's OWNER, the way it was.

    True while a room holds one household and false the moment it admits several: the
    events of an agent belonging to account B would land on account A's screen, because
    A happens to own the room. Model text and tool activity are exactly what must not
    cross that line, and no guard would have noticed - the fixtures had one tenant, so
    the property stopped being true while every assertion about it stayed green.

    The acting scope is trusted ONLY while that agent's own room turn is running. The
    bound scope at any other moment is whatever the last chat left behind, and routing
    by it then would be the same leak pointing the other way.
    """
    import asyncio

    import vaf.core.web_interface as wi_mod
    import vaf.core.web_server as ws_mod

    wi = wi_mod.get_web_interface()
    watcher, foreign, plain = _wire_room_task_world(monkeypatch, wi)
    monkeypatch.setattr(wi, "_room_owner_cache", {}, raising=False)
    monkeypatch.setattr(
        wi, "room_owner_scope",
        lambda rid: "scope-a" if str(rid) == "room-live-x" else None)

    class _ActingAgent:
        # Account B's agent, mid-turn in a room account A owns.
        _room_turn = {"room_id": "room-live-x", "mode": "assist"}
        _current_user_scope_id = "scope-b"

    monkeypatch.setattr(wi, "agent_instance", _ActingAgent(), raising=False)

    update = ws_mod.SubAgentStreamUpdate(
        type="coder_state", status="Editing", roomId="room-live-x")
    asyncio.run(ws_mod.receive_subagent_stream(update))

    assert foreign.received and foreign.received[-1]["type"] == "coder_state", (
        "the account whose agent is working never saw its own event")
    assert watcher.received == [], (
        "the room's owner saw another account's agent working")

    # No turn of its own running: the room's owner is the honest fallback again.
    class _Idle:
        _room_turn = None
        _current_user_scope_id = "scope-b"

    monkeypatch.setattr(wi, "agent_instance", _Idle(), raising=False)
    asyncio.run(ws_mod.receive_subagent_stream(ws_mod.SubAgentStreamUpdate(
        type="coder_state", status="Editing", roomId="room-live-x")))
    assert watcher.received and watcher.received[-1]["type"] == "coder_state"


def test_a_stale_transcript_cannot_reopen_a_room_the_person_left():
    """MUTATION: adopt every room_transcript into the view again.

    The 3s poll's last answer lands AFTER a switch to a chat, and unconditional
    adoption yanked the person straight back into the room - on a slow server,
    every attempt to leave bounced back within seconds. A transcript may only
    open the view when it answers the click the person just made (the pending
    mark) or refreshes the room already on screen; switching away clears the
    mark.
    """
    source = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    handler = source.split("data.type === 'room_transcript'")[1][:2200]
    adopt = handler.index("setRoomView({ room: data.room")
    assert "roomViewRef.current" in handler[:adopt] and "pendingRoomOpenRef" in handler[:adopt], \
        "the transcript handler must decide against the OPEN view before adopting"
    switch = source.split("const handleSessionSwitch")[1][:800]
    assert "pendingRoomOpenRef.current = null" in switch, \
        "switching away must clear the pending mark"


def test_a_room_message_is_not_swallowed_by_a_closing_socket():
    """MUTATION: send into the room branch without the readyState check.

    On a CLOSING socket the browser drops the frame with nothing but a console
    warning, so the message vanished while the input cleared - the person
    believed it was sent and it arrived minutes late, after retyping. The chat
    branch has this check further down; the room branch returns before ever
    reaching it, so it needs its own.
    """
    source = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    room_branch = source.split("type: 'room_say'")[0][-900:]
    assert "ws.readyState !== WebSocket.OPEN" in room_branch, \
        "the room send must refuse a socket that cannot carry it"


def test_loading_shows_the_shape_of_what_is_coming_in_both_lanes():
    """MUTATION: drop the skeleton branch of either lane, or the pending echo.

    Three loading illusions, asked for together and wired together: skeleton
    bubbles while a chat's history or a room's transcript is on its way, a
    progress bar that races to two thirds and then creeps (finishing is the
    content's job), and the person's own room message on screen at once -
    visibly pending, reconciled against the store, never given a position
    among the real messages, because the room trusts only the store for order.
    """
    page = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    assert page.count("<LoadingIllusion kind=") == 2, "both lanes share ONE component"
    assert '<LoadingIllusion kind="chat" />' in page
    assert '<LoadingIllusion kind="room" label={roomOpening.name} />' in page
    css = (ROOT / "web" / "app" / "globals.css").read_text(encoding="utf-8")
    assert "@keyframes loadCrawl" in css and "@keyframes skelPulse" in css
    assert "scaleX" in css.split("@keyframes loadCrawl")[1][:200], \
        "the bar animates transform only - the repaint rule"
    assert "setPendingRoomSays" in page
    assert "Date.now() - p.ts < 30000" in page, \
        "a pending echo must expire instead of accumulating"
