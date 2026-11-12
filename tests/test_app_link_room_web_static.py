# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The Veyllo App room in the browser (web/app/page.tsx): the wiring of the pinned row, the
room view in its Veyllo App mode, the composer without a chooser, the drafts of both sessions
and the inbox. The rules themselves are plain functions in web/components/chat/appRoom.ts,
which tests/test_app_link_room_web.py runs in node; here each assertion names the page line
that calls them, and deleting it turns the test red."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
INBOX = (ROOT / "web" / "components" / "inbox" / "InboxWindow.tsx").read_text(encoding="utf-8")


def _between(text: str, start: str, end: str) -> str:
    assert start in text, start
    body = text.split(start, 1)[1]
    assert end in body, end
    return body.split(end, 1)[0]


def _row() -> str:
    return _between(PAGE, "{sessions.filter(isAppRoom).map(s => {",
                    "{sessions.filter(s => !isAppRoom(s)).map(s => isRoom(s) ? (")


def test_the_row_is_pinned_with_the_app_symbol_and_its_name():
    """MUTATION: draw the row with the chat icon, or drop the pinned block - red."""
    row = _row()
    assert "<Smartphone size={16}" in row and "{APP_ROOM_TITLE}" in row
    assert "onClick={() => { setDrawerOpen(false); openAppRoom(); }}" in row
    assert "handleSessionSwitch" not in row and "delete_session" not in row
    assert "s.appRoom?.connected" in row
    assert PAGE.index("{sessions.filter(isAppRoom).map(s => {") < PAGE.index(
        "{sessions.filter(s => !isAppRoom(s)).map(s => isRoom(s) ? (")


def test_the_row_sticks_to_the_top_and_takes_the_keyboard():
    """MUTATION: drop `sticky top-0` from the row's wrapper, or make the row a div again -
    red."""
    row = _row()
    assert 'className="sticky top-0 z-10 -mx-2 px-2"' in row
    assert "style={{ background: 'rgb(var(--chat-fog))' }}" in row
    assert '<button type="button" data-app-room="veyllo_app"' in row


def test_the_unread_dot_follows_the_rule():
    """MUTATION: draw the dot from `s.unread` alone - red (it would also burn on the open room,
    which the node test of `showUnreadDot` refuses)."""
    row = _row()
    assert "const dot = showUnreadDot(s.unread, open);" in row
    assert "{dot && (" in row and "(s.unread || 0) > 0" not in row


def test_no_self_pick_lands_on_the_row():
    """MUTATION: drop `!isAppRoom(s)` from `conversationsOnly` - red."""
    assert ("const conversationsOnly = (list: Session[]) => list.filter(s => !isRoom(s) && !isAppRoom(s));"
            in PAGE)
    assert "isAppRoom," in _between(PAGE, "import {\n    APP_ROOM_ID", "from '@/components/chat/appRoom';")


def test_the_room_opens_and_refreshes_through_its_own_command():
    """MUTATION: drop `pendingRoomOpenRef.current = APP_ROOM_ID` from `openAppRoom` - red (the
    answer is never adopted and the click shows nothing); poll without `roomOpenCommand` -
    red."""
    opener = _between(PAGE, "const openAppRoom = () => {", "\n    };")
    assert "pendingRoomOpenRef.current = APP_ROOM_ID;" in opener
    assert "roomOpenCommand(APP_ROOM_ID)" in opener
    adopt = _between(PAGE, "else if (data.type === 'room_transcript') {",
                     "else if (data.type === 'room_invite_candidates') {")
    assert "|| pendingRoomOpenRef.current === rid;" in adopt
    poll = _between(PAGE, "roomPollRef.current = setInterval(() => {", "}, 3000);")
    assert "roomOpenCommand(roomId)" in poll


def test_the_composer_posts_the_line_without_a_chooser():
    """MUTATION: send the Veyllo App room's line as `room_say`, add an addressee to the body,
    or clear the box without `clearAfterSend` - red."""
    branch = _between(PAGE, "if (roomView.room.roomKind === 'veyllo_app') {", "}")
    assert "void sendToAppRoom(roomText);" in branch
    sender = _between(PAGE, "const sendToAppRoom = async (text: string, resendOf?: string) => {",
                      "\n    };")
    assert "/api/app-link/room" in sender and "method: 'POST'" in sender
    assert ("body: JSON.stringify({ text: next.text, ...(next.resendOf ? { resend_of: next.resendOf } : {}) }),"
            in sender), "the body carries the line (and the line it sends again) only"
    assert "const sentFrom = roomViewRef.current?.room.roomId ?? null;" in sender
    refused = _between(sender, "if (!res.ok) {", "}")
    assert "setAppRoomError(String(data.code || 'failed'));" in refused and "setInput" not in refused
    assert "clearAfterSend(sentFrom, roomViewRef.current?.room.roomId ?? null, text, prev)" in sender
    assert "tMain(placeholderKey(!!roomView?.room.appRoom?.canWrite), appRoomNames)" in PAGE


def test_the_refusal_and_the_older_app_notice_sit_in_the_dock():
    """MUTATION: move the notice out of the dock (its height no longer counts in the
    transcript's padding) - red; map the refusal by hand - red."""
    dock = _between(PAGE, "<div ref={dockRef}>", "{voteDockOpen && (")
    assert "{appRoomWords && (appRoomError || roomView?.room.appRoom?.reason) && (" in dock
    assert "appRoomRefusal(appRoomError || roomView?.room.appRoom?.reason || '')" in dock
    assert "const appRoomRefusal = (code: string) => tMain(refusalKey(code), appRoomNames);" in PAGE


def test_what_an_agent_room_offers_is_hidden_in_the_veyllo_app_room():
    """MUTATION: show the member chips, the attachment or the call button in the Veyllo App
    room - red."""
    identity = _between(PAGE, "function RoomIdentity(", "function RoomConversation(")
    assert "const appRoom = room.roomKind === 'veyllo_app';" in identity
    assert identity.index("{!appRoom && (<>") < identity.index("<Info size={16} />")
    assert "{tMain('roomReconnecting')}" in identity and "wiederhergestellt" not in identity
    assert "onReact={roomView.room.me && !roomView.room.closed && !appRoomWords ?" in PAGE
    assert "{roomView ? (appRoomWords ? (" in PAGE and "data-app-room-strip" in PAGE
    assert "&& roomView.room.roomKind !== 'veyllo_app'" in PAGE
    attach = PAGE.index("onClick={() => fileInputRef.current?.click()}")
    assert PAGE.rindex("{!appRoomWords && (", 0, attach) > attach - 400
    call = PAGE.index('title="Live-Call"')
    assert PAGE.rindex("{!appRoomWords && (", 0, call) > call - 1200
    # "@" completes the VAF agent only, with the handle that is sure to reach it.
    assert "(roomView.room.roomKind !== 'veyllo_app' || m.peer === roomView.room.agentPeer)" in PAGE
    assert "(roomView.room.appRoom?.handle || m.label) : m.label," in PAGE


def test_a_line_shows_its_speaker_addressee_delivery_and_its_words():
    """MUTATION: drop the delivery state, the resend, the coded text or the empty state -
    red."""
    view = _between(PAGE, "function RoomConversation(", "function VAFDashboardContent(")
    assert "const d = appRoom.delivery(m.state, m.reason, m.device, m.resent);" in view
    assert "onClick={() => appRoom.onSendAgain(m.text, m.id)}" in view
    assert "{(appRoom && appRoom.lineText(m.code, m.seconds)) || m.text}" in view
    assert "{appRoom.to(m.to.peer, m.toLabel || '')}" in view
    assert "{appRoom && m.kind === 'note' ? (appRoom.lineText(m.code) || appRoom.confirmed) : m.text}" in view
    assert "{appRoom.empty}" in view
    assert "const isAppAgent = !!appRoom && m.peer === 'app_agent';" in view
    words = _between(PAGE, "const appRoomWords = roomView?.room.roomKind === 'veyllo_app' ? {",
                     "} : undefined;")
    assert "tMain(toKey(peer), { name })" in words
    assert "deliveryOf(state, reason, roomView.room.appRoom?.devices, device, resent)" in words
    assert "lineTextKey(code)" in words
    assert "tMain(emptyKey(!!roomView.room.appRoom?.canWrite), appRoomNames)" in words
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
    assert "type: 'get_sessions'" in changed and "roomOpenCommand(APP_ROOM_ID)" in changed


def test_the_drafts_of_both_sessions_show_and_pin_the_view():
    """MUTATION: list the drafts of the app chat only, or leave the drafts out of the
    autoscroll - red (a waiting draft opened behind the composer)."""
    assert "useChatDrafts(getApiBase(), appRoomSessions[0] || '', heldVersion)" in PAGE
    assert "useChatDrafts(getApiBase(), appRoomSessions[1] || '', heldVersion)" in PAGE
    assert "[...appRoomChatDrafts.rows, ...appRoomAskDrafts.rows]" in PAGE
    assert "<UnplacedDrafts rows={appRoomDrafts}" in PAGE
    assert "}, [messages, loading, roomView?.messages.length, appRoomDrafts.length, dockHeight, appRoomSending.busy]);" in PAGE
    grew = _between(PAGE, "const _chatGrew = () => {", "\n    };")
    assert ":${appRoomDrafts.length}:${dockHeight}:${appRoomSending.busy}`;" in grew


def test_the_inbox_opens_the_room_by_the_registry_name():
    """MUTATION: send a Veyllo App row of the inbox to Settings again, or spell the channel by
    hand - red."""
    from vaf.core.app_link import CHANNEL
    from vaf.core.channels import CHAT_CHANNELS
    module = (ROOT / "web" / "components" / "chat" / "appRoom.ts").read_text(encoding="utf-8")
    assert f"export const VEYLLO_APP_CHANNEL: string = '{CHANNEL}';" in module and CHANNEL in CHAT_CHANNELS
    assert "onOpenAppRoom={openAppRoom}" in PAGE
    assert "if (r.channel === VEYLLO_APP_CHANNEL && onOpenAppRoom) { onClose(); onOpenAppRoom(); return; }" in INBOX
    assert "'veyllo_app'" not in INBOX


def test_one_line_is_on_its_way_at_a_time_and_shows_it():
    """MUTATION: drop the `beginSend` guard, the disabled resend or the pending copy while a
    line is on its way - red."""
    sender = _between(PAGE, "const sendToAppRoom = async (text: string, resendOf?: string) => {",
                      "\n    };")
    assert "const next = beginSend(appRoomSendRef.current, text, resendOf);\n        if (!next) return;" in sender
    assert "appRoomSendRef.current = ROOM_SEND_IDLE;" in _between(sender, "} finally {", "}")
    assert "pendingScrollRestore.current = 'bottom';" in sender
    view = _between(PAGE, "function RoomConversation(", "function VAFDashboardContent(")
    assert "disabled={appRoom.sending}" in view
    assert "(appRoomSending.busy ? [{ key: 1, text: appRoomSending.text }] : [])" in PAGE


def test_a_dropped_file_is_refused_in_the_room():
    """MUTATION: stage a dropped file in the Veyllo App room - red."""
    drop = _between(PAGE, "const handleDrop = useCallback((e: React.DragEvent) => {", "}, [")
    guard = drop.index("if (roomViewRef.current?.room.roomKind === 'veyllo_app') {")
    assert "setAppRoomError('text_only');" in drop
    assert guard < drop.index("addFilesAsAttachments(files)")


def test_a_send_gives_up_after_its_timeout_and_scrolls_only_the_room():
    """MUTATION: drop the abort signal, or set the scroll target whatever is on screen - red."""
    sender = _between(PAGE, "const sendToAppRoom = async (text: string, resendOf?: string) => {",
                      "\n    };")
    assert "const timer = setTimeout(() => abort.abort(), SEND_TIMEOUT_MS);" in sender
    assert "signal: abort.signal," in sender and "clearTimeout(timer);" in sender
    assert ("if (roomViewRef.current?.room.roomId === APP_ROOM_ID) {\n"
            "                pendingScrollRestore.current = 'bottom';") in sender
    view = _between(PAGE, "function RoomConversation(", "function VAFDashboardContent(")
    assert "m.state === 'not_delivered' && !d.quiet ?" in view


def test_the_room_details_read_on_a_phone():
    """MUTATION: let "Send again" wrap or shrink its hit area on a phone, drop the word or the
    status role of a waiting question, or put the draft cards back into the chat's gutter -
    red."""
    view = _between(PAGE, "function RoomConversation(", "function VAFDashboardContent(")
    resend = _between(view, "onClick={() => appRoom.onSendAgain(m.text, m.id)}", ">")
    assert "whitespace-nowrap shrink-0" in resend and "max-md:py-[15px] max-md:-my-[15px]" in resend
    assert '<div role="status" className="text-[10px] mt-0.5 text-gray-400 dark:text-[#7a7a7a] vaf-pulse-note">\n                                    {appRoom.awaiting}' in view
    assert "awaiting: tMain('appRoomAwaitingAnswer')," in PAGE
    drafts = _between(PAGE, "<div data-app-room-drafts className=\"", "\">")
    assert "[&_.vaf-msg-row>div>div:first-child]:hidden" in drafts
    assert "[&_.vaf-msg-row>div>div:last-child]:pl-11" in drafts
