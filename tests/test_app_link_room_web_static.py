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


def _button(marker: str) -> str:
    """The whole `<button ...>` opening tag around a marker of the composer."""
    at = PAGE.index(marker)
    start = PAGE.rindex("<button", 0, at)
    return PAGE[start:PAGE.index("\n                                        >", at)]


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
    assert ("body: JSON.stringify({ text: next.text, ...(next.resendOf ? { resend_of: next.resendOf } : {}),\n"
            "                    ...(lineId ? { client_id: lineId } : {}) }),"
            in sender), "the body carries the line, the line it sends again and its own id only"
    assert "const sentFrom = roomViewRef.current?.room.roomId ?? null;" in sender
    refused = _between(sender, "} else if (outcome.status === 'refused') {", "}")
    assert "setAppRoomError(String(outcome.data.code || 'failed'));" in refused
    assert "setInput" not in refused
    assert "clearAfterSend(sentFrom, roomViewRef.current?.room.roomId ?? null, text, prev)" in sender
    # The hint shows the handle only as `hintHandle` lets it (whole, or "VAF"), and the chat's
    # own hint stays. MUTATION: pass the raw handle or `appRoomNames` again, or change the
    # chat's hint - red.
    hint = _between(PAGE, "placeholder={isIndexing ?", "\n")
    assert ("appRoomWords ? tMain(placeholderKey(!!roomView?.room.appRoom?.canWrite),"
            " { handle: hintHandle(appRoomHandle) }) : \"Ask anything...\"}" in hint)
    assert hint.rstrip().endswith(': "Ask anything..."}')


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
    # The attachment and the call button stay in their places in the room, visible but
    # switched off (the page's disabled look, no hover), out of the keyboard's way and saying
    # why, so the box is the chat's own box with two controls off. MUTATION: unmount either one
    # in the room again, hide it, drop the disabled look, or drop the reason - red.
    attach = _button("onClick={() => { if (!appRoomWords) fileInputRef.current?.click(); }}")
    call = _button("useVoiceCallStore.getState().start();")
    for button in (attach, call):
        start = PAGE.index(button)
        assert "{!appRoomWords && (" not in PAGE[start - 900:start], button[:60]
        classes = _between(button, "className={cn(", ")}")
        assert "appRoomWords && OFF_IN_APP_ROOM" in classes, button[:60]
        assert "invisible" not in button and "aria-hidden" not in button
        assert "tabIndex={appRoomWords ? -1 : undefined}" in button
    # Only in the room: the definition and exactly the two uses, each behind the room's gate, so
    # the chat's buttons cannot pick up the switched-off look. MUTATION: add an ungated
    # `OFF_IN_APP_ROOM` to either button - red.
    assert PAGE.count("OFF_IN_APP_ROOM") == 3
    uses = [i for i in range(len(PAGE)) if PAGE.startswith("OFF_IN_APP_ROOM", i)]
    assert PAGE.startswith("const OFF_IN_APP_ROOM = ", uses[0] - len("const "))
    for at in uses[1:]:
        assert PAGE[:at].endswith("appRoomWords && "), PAGE[at - 40:at + 20]
    off = _between(PAGE, "const OFF_IN_APP_ROOM = \"", "\";")
    for cls in ("disabled:opacity-40", "disabled:cursor-not-allowed", "disabled:hover:bg-transparent"):
        assert cls in off.split(), cls
    assert "if (appRoomWords) return;" in call and "|| !!appRoomWords}" in call
    assert "disabled={!!appRoomWords}" in attach
    assert "title={appRoomWords ? tMain('appRoomTextOnly') : tMain('attachmentsDocumentViewer')}" in attach
    assert "title={appRoomWords ? tMain('appRoomNoCalls') : \"Live-Call\"}" in call
    # "@" completes the VAF agent only, with the handle that is sure to reach it.
    assert "(roomView.room.roomKind !== 'veyllo_app' || m.peer === roomView.room.agentPeer)" in PAGE
    assert "(roomView.room.appRoom?.handle || m.label) : m.label," in PAGE


def test_the_room_label_is_the_workspace_chip():
    """The room's label on the strip above the box has the chat's workspace chip's look, from
    one constant, and is not clickable. MUTATION: write the room's span by hand again, or give
    it `cursor-pointer` - red."""
    assert PAGE.count("const STRIP_CHIP = ") == 1
    label = _between(PAGE, "{roomView ? (appRoomWords ? (", "</span>\n")
    assert '<span className={cn(STRIP_CHIP, "mr-auto")} data-app-room-strip>' in label
    assert "cursor-pointer" not in label and "hover:" not in label and "onClick" not in label
    assert "<Smartphone size={10}" in label
    chip = _between(PAGE, "{workspaceInfo?.path && (", "<Folder size={10}")
    assert "className={cn(\n                                                STRIP_CHIP," in chip
    assert "cursor-pointer" in chip
    # The agent room's folder chip beside it takes the same look, plus its dark border.
    # MUTATION: write the agent room's chip by hand again - red.
    title = PAGE.index("title={tMain('roomWorkspaceTitle')}")
    agent_room = PAGE[PAGE.rindex("<span", 0, title):title]
    assert ('className={cn(STRIP_CHIP, "mr-auto cursor-pointer dark:border-[#2a2a2a] '
            in agent_room), agent_room
    assert "text-[10px] font-mono text-gray-400 opacity-80 px-2 py-0.5 rounded cursor-pointer" not in PAGE


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


def test_the_follow_signature_moves_on_every_pass():
    """The signature of the follow moves also when a parked restore takes the pass. Skipped
    there, it kept "a line is sending" from the first send, and every later send, which sets
    that same state again, read as no change: the view stayed 86 px short of the waiting row
    (measured on the page: 0 of 4 later sends followed with the signature inside the branch,
    every one of 24 after). MUTATION: ask `_chatGrew()` only in the follow branch - red."""
    effect = _code_only(_between(PAGE, "    useEffect(() => {\n        // The signature moves on EVERY pass",
                                 "appRoomDrafts.length, dockHeight, appRoomSending.busy]);"))
    grew_at = effect.index("const grew = _chatGrew();")
    assert grew_at < effect.index("if (pendingScrollRestore.current !== null) {")
    assert "} else if (isAtBottomRef.current && grew) {" in effect
    assert effect.count("_chatGrew()") == 1


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


def _code_only(src):
    """`src` without its // comments, so a check never matches a line that was commented out."""
    import re
    return re.sub(r"(?m)^\s*//.*$|\s//[^'\"`\n]*$", "", src)


def test_a_send_gives_up_after_its_timeout_and_scrolls_only_the_room():
    """The sender sends through `sendWithTimeout` (its behaviour is run in node, see
    test_app_link_room_web.py), asks the room once after a give-up before it shows anything,
    and scrolls only the room. MUTATION: fetch directly again - red; MUTATION: show the
    give-up before the room was asked - red; MUTATION: set the scroll target whatever is on
    screen - red; MUTATION: send no line id, hand the check no transcripts, or stop watching
    for a late line - red."""
    sender = _code_only(_between(PAGE, "const sendToAppRoom = async (text: string, resendOf?: string) => {",
                                 "\n    };\n"))
    assert "await sendWithTimeout(fetch, `${getApiBase()}/api/app-link/room`" in sender
    assert "await fetch(" not in sender
    # The line carries the page's own id, and a give-up hands the check (appRoom.ts
    # `confirmAfterGiveUp`, run in node) the socket, every transcript and that id.
    assert "...(lineId ? { client_id: lineId } : {})" in sender
    give_up = _between(sender, "} else if (outcome.status === 'timeout') {", "} else {\n                setAppRoomError('failed');")
    asked = give_up.index("await confirmAfterGiveUp(wsSocketRef.current, cb => {")
    decided = give_up.index("if (found) {")
    assert asked < decided < give_up.index("setAppRoomError('timeout');")
    assert "appRoomLateRef.current = { lineId, text: next.text, sentFrom };" in give_up
    assert "if (socket && socket.readyState === WebSocket.OPEN) {" in sender
    # The view goes to the taken line only by the decision of appRoom.ts `goToTakenLine` (run in
    # node), asked with the room on screen and whether the view is at the bottom NOW: the
    # parked restore is set inside that condition and nowhere else in the sender.
    taken = _code_only(_between(sender, "const taken = () => {", "\n        };"))
    decision = "if (goToTakenLine(roomViewRef.current?.room.roomId ?? null, isAtBottomRef.current)) {"
    assert taken.index(decision) < taken.index("pendingScrollRestore.current = 'bottom';")
    assert sender.count("pendingScrollRestore.current = 'bottom';") == 1
    handler = _code_only(_between(PAGE, "else if (data.type === 'room_transcript') {",
                                  "else if (data.type === 'room_invite_candidates')"))
    assert "appRoomListenersRef.current.forEach(cb => cb(lines));" in handler
    assert ("const verdict = lateLine(lines, late.lineId, late.sentFrom,\n"
            "                                    roomViewRef.current?.room.roomId ?? null, late.text, inputValueRef.current || '');"
            in handler)
    # What the verdict does is appRoom.ts `lateEffect` (run in node), and only that.
    effect = _between(handler, "const effect = lateEffect(verdict);", "}")
    assert "if (effect.box) setInput('');" in effect and "if (effect.notice) setAppRoomError('');" in effect
    assert handler.count("setAppRoomError(") == 1 and handler.count("setInput(") == 1
    # The check runs with the page's default wait (appRoom.ts `CONFIRM_WAIT_MS`).
    assert "return () => { appRoomListenersRef.current.delete(cb); };\n                }, lineId);" in give_up
    view = _between(PAGE, "function RoomConversation(", "function VAFDashboardContent(")
    assert "m.state === 'not_delivered' && !d.quiet ?" in view


def _block(src, opener):
    """The text of the block that `opener` (ending in "{") opens, braces counted."""
    start = src.index(opener) + len(opener)
    depth = 1
    for i in range(start, len(src)):
        depth += {"{": 1, "}": -1}.get(src[i], 0)
        if depth == 0:
            return src[start:i]
    raise AssertionError(f"unclosed block after {opener!r}")


def test_the_give_up_listeners_see_every_veyllo_app_transcript():
    """A person may switch chats during the 5 s check: the listeners and the late watch get
    EVERY Veyllo App transcript, also one the view does not adopt, and only the clearing is
    guarded (`lateLine` through `clearAfterSend`). MUTATION: hand them the transcript only
    inside `if (wanted)` - red."""
    handler = _code_only(_between(PAGE, "else if (data.type === 'room_transcript') {",
                                  "else if (data.type === 'room_invite_candidates')"))
    adopted = _block(handler, "if (wanted) {")
    assert "appRoomListenersRef" not in adopted and "appRoomLateRef" not in adopted
    listeners = _block(handler, "if (rid === APP_ROOM_ID) {")
    assert "appRoomListenersRef.current.forEach(cb => cb(lines));" in listeners
    assert "lateLine(" in listeners


def test_a_room_send_rearms_the_follow_like_a_chat_send():
    """Sending in the room is "take me to my line", as a chat send is (pinned for the chat in
    tests/test_chat_autoscroll_guard.py): a reader who scrolled up BEFORE typing sees their
    line and the sending row. The anchor is re-armed in the same block that puts the line on
    its way, before the sending state renders. MUTATION: drop the re-arm - red."""
    sender = _code_only(_between(PAGE, "const sendToAppRoom = async (text: string, resendOf?: string) => {",
                                 "\n    };\n"))
    rearm = sender.index("isAtBottomRef.current = true;")
    assert sender.index("appRoomSendRef.current = next;") < rearm < sender.index("setAppRoomSending(next);")


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
