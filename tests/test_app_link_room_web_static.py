# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The Veyllo App room in the browser (web/app/page.tsx): the pinned row with the app's
symbol, the room view in its Veyllo App mode with a composer that has no chooser, the
delivery state of a line, the drafts of both sessions, and the inbox that opens the room.
Each assertion names the line that carries the behaviour; deleting it turns the test red."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
INBOX = (ROOT / "web" / "components" / "inbox" / "InboxWindow.tsx").read_text(encoding="utf-8")


def _between(text: str, start: str, end: str) -> str:
    assert start in text, start
    body = text.split(start, 1)[1]
    assert end in body, end
    return body.split(end, 1)[0]


def test_the_row_is_pinned_with_the_app_symbol_and_its_name():
    """MUTATION: draw the row with the chat icon, or drop the pinned block - red."""
    row = _between(PAGE, "{sessions.filter(isAppRoom).map(s => {",
                   "{sessions.filter(s => !isAppRoom(s)).map(s => isRoom(s) ? (")
    assert "<Smartphone size={16}" in row
    assert "{APP_ROOM_TITLE}" in row and "const APP_ROOM_TITLE = 'Veyllo App';" in PAGE
    assert "onClick={() => { setDrawerOpen(false); openAppRoom(); }}" in row
    assert "handleSessionSwitch" not in row and "delete_session" not in row
    assert "s.appRoom?.connected" in row and "s.unread" in row
    # The pinned block comes BEFORE the agent rooms and the chats.
    assert PAGE.index("{sessions.filter(isAppRoom).map(s => {") < PAGE.index(
        "{sessions.filter(s => !isAppRoom(s)).map(s => isRoom(s) ? (")


def test_no_self_pick_lands_on_the_row():
    """MUTATION: drop `!isAppRoom(s)` from `conversationsOnly` - red."""
    assert "const isAppRoom = (s: Session) => s.kind === 'app_room';" in PAGE
    assert ("const conversationsOnly = (list: Session[]) => list.filter(s => !isRoom(s) && !isAppRoom(s));"
            in PAGE)


def test_the_room_opens_and_refreshes_through_its_own_command():
    """MUTATION: poll the Veyllo App room with `open_room` - red."""
    opener = _between(PAGE, "const openAppRoom = () => {", "};")
    assert "type: 'open_app_room'" in opener and "room_id" not in opener
    poll = _between(PAGE, "roomPollRef.current = setInterval(() => {", "}, 3000);")
    assert "roomId === APP_ROOM_ID" in poll and "{ type: 'open_app_room' }" in poll


def test_the_composer_posts_the_line_without_a_chooser():
    """MUTATION: send the Veyllo App room's line as `room_say`, or add an addressee to the
    body - red."""
    branch = _between(PAGE, "if (roomView.room.roomKind === 'veyllo_app') {", "}")
    assert "void sendToAppRoom(roomText);" in branch
    sender = _between(PAGE, "const sendToAppRoom = async (text: string) => {", "\n    };")
    assert "/api/app-link/room" in sender and "method: 'POST'" in sender
    assert "body: JSON.stringify({ text })," in sender, "the body carries the line only"
    # Cleared only when the line was taken; a refusal keeps it and names the reason.
    assert sender.index("if (!res.ok) {") < sender.index("setInput(")
    assert "setAppRoomError(String(data.code || 'failed'));" in sender
    assert "tMain('appRoomPlaceholder', { name: appRoomAgentName })" in PAGE


def test_the_refusal_and_the_older_app_notice_stand_above_the_box():
    """MUTATION: drop the notice block - red."""
    notice = _between(PAGE, "{appRoomWords && (appRoomError || roomView?.room.appRoom?.reason) && (",
                      "{/* Memory Learning Banner */}")
    assert "appRoomRefusal(appRoomError || roomView?.room.appRoom?.reason || '')" in notice
    refusal = _between(PAGE, "const appRoomRefusal = (code: string) =>", ";\n")
    assert "tMain('appRoomNeedsNewerApp', { name: appRoomAgentName })" in refusal


def test_what_an_agent_room_offers_is_hidden_in_the_veyllo_app_room():
    """MUTATION: show the member chips and the info button for the Veyllo App room - red."""
    identity = _between(PAGE, "function RoomIdentity(", "function RoomConversation(")
    assert "const appRoom = room.roomKind === 'veyllo_app';" in identity
    assert "{!appRoom && (<>" in identity
    assert identity.index("{!appRoom && (<>") < identity.index("<Info size={16} />")
    assert "onReact={roomView.room.me && !roomView.room.closed && !appRoomWords ?" in PAGE
    # Neither the room's shared folder nor the typing signal exists for it.
    assert "{roomView ? (appRoomWords ? (" in PAGE and "data-app-room-strip" in PAGE
    assert "&& roomView.room.roomKind !== 'veyllo_app'" in PAGE
    # "@" completes the VAF agent only.
    assert "(roomView.room.roomKind !== 'veyllo_app' || m.peer === roomView.room.agentPeer)" in PAGE


def test_a_line_shows_its_speaker_addressee_and_delivery():
    """MUTATION: drop the delivery state under a line to the app's agent - red."""
    view = _between(PAGE, "function RoomConversation(", "function VAFDashboardContent(")
    assert "{appRoom && m.state && (" in view and "{appRoom.state(m.state)}" in view
    assert "const isAppAgent = !!appRoom && m.peer === 'app_agent';" in view
    assert "<Smartphone size={15} />" in view
    assert "appRoom.to(m.to.peer === 'person' ? appRoom.you : (m.toLabel || ''))" in view
    assert "appRoom={appRoomWords}" in PAGE


def test_the_payload_fields_reach_the_view():
    """MUTATION: rebuild the room from a field list that leaves `appRoom` out - red."""
    handler = _between(PAGE, "else if (data.type === 'room_transcript') {",
                       "else if (data.type === 'room_invite_candidates') {")
    assert "setRoomView({ room: data.room, messages: data.messages || [] });" in handler
    assert "appRoom?: AppRoomState;" in _between(PAGE, "type RoomView = {", "};")
    assert "appRoom?: AppRoomState;" in _between(PAGE, "type Session = {", "};")
    changed = _between(PAGE, "else if (data.type === 'app_room_changed') {",
                       "else if (data.type === 'rooms_changed') {")
    assert "type: 'get_sessions'" in changed and "type: 'open_app_room'" in changed


def test_the_drafts_of_both_sessions_show_in_the_room():
    """MUTATION: list the drafts of the app chat only - red."""
    assert ("useChatDrafts(getApiBase(), appRoomSessions[0] || '', heldVersion)" in PAGE)
    assert ("useChatDrafts(getApiBase(), appRoomSessions[1] || '', heldVersion)" in PAGE)
    assert "[...appRoomChatDrafts.rows, ...appRoomAskDrafts.rows]" in PAGE
    assert "<UnplacedDrafts rows={appRoomDrafts}" in PAGE


def test_the_inbox_opens_the_room():
    """MUTATION: send a Veyllo App row of the inbox to Settings again - red."""
    assert "onOpenAppRoom={openAppRoom}" in PAGE
    assert ("if ((r.channel as string) === 'veyllo_app' && onOpenAppRoom) { onClose(); "
            "onOpenAppRoom(); return; }") in INBOX
