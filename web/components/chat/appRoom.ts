// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
//
// The browser's half of the Veyllo App room (vaf/core/app_link/room.py): which row it is, which
// command opens and refreshes it, which catalogue text says what, and when the composer may be
// cleared. Plain functions without React, so tests/test_app_link_room_web.py runs them in node.

/** The room id the Veyllo App room travels under in the room view (`room.ROW_ID`). */
export const APP_ROOM_ID = 'app:veyllo_app';
/** Its visible name: the product's name, the same in every language. */
export const APP_ROOM_TITLE = 'Veyllo App';
/** The registry's name of the Veyllo App channel (vaf/core/channels.py, `app_link.CHANNEL`),
 *  which the inbox's rows carry. */
export const VEYLLO_APP_CHANNEL: string = 'veyllo_app';

type Row = { kind?: string; roomId?: string };

/** The pinned Veyllo App row: never a conversation, never an agent room. */
export const isAppRoom = (s: Row) => s.kind === 'app_room';

/** The frame that opens a room and refreshes it while it is open: the Veyllo App room has its
 *  own command, which names no room (the server answers for the connection's own account). */
export function roomOpenCommand(roomId: string): Record<string, string> {
    return roomId === APP_ROOM_ID ? { type: 'open_app_room' } : { type: 'open_room', room_id: roomId };
}

/** Whether an open room is still in the list: an agent room by its id, the Veyllo App room by
 *  its row (it goes with the last paired device). A view whose room left the list closes. */
export function roomStillListed(openRoomId: string, rows: Row[]): boolean {
    if (openRoomId === APP_ROOM_ID) return rows.some(isAppRoom);
    return rows.some(s => s.kind === 'room' && s.roomId === openRoomId);
}

/** The catalogue key of why a line to the agent in the Veyllo App cannot go. */
export function refusalKey(code: string): string {
    switch (code) {
        case 'needs_newer_app': return 'appRoomNeedsNewerApp';
        case 'not_connected_yet': return 'appRoomNotConnectedYet';
        case 'channel_off': return 'appRoomChannelOff';
        case 'no_device': return 'appRoomNoDevice';
        case 'rate_limited': return 'appRoomRateLimited';
        default: return 'appRoomFailed';
    }
}

/** The composer's hint: while no line can reach the app's agent, only the VAF agent's form. */
export const placeholderKey = (canWrite: boolean) => (canWrite ? 'appRoomPlaceholder' : 'appRoomPlaceholderAgentOnly');

/** "to <name>" under a line: the person addressed gets their own form ("an dich", "sana"). */
export const toKey = (peer: string) => (peer === 'person' ? 'appRoomToYou' : 'appRoomTo');

/** The catalogue key of how a line to the agent in the Veyllo App stands, and whether the
 *  person is offered to send it again (only a line that will not arrive any more). */
export function deliveryOf(state: string, reason?: string, devices?: number, device?: string):
    { key: string; values?: Record<string, string>; resend: boolean } {
    if (state === 'delivered') {
        return (devices || 0) > 1 && device
            ? { key: 'appRoomDeliveredTo', values: { device }, resend: false }
            : { key: 'appRoomDelivered', resend: false };
    }
    if (state === 'waiting') return { key: 'appRoomWaiting', resend: false };
    switch (reason) {
        case 'removed': return { key: 'appRoomNotDeliveredRemoved', resend: true };
        case 'lane_off': return { key: 'appRoomNotDeliveredOff', resend: false };
        case 'expired': return { key: 'appRoomNotDeliveredExpired', resend: true };
        default: return { key: 'appRoomNotDelivered', resend: true };
    }
}

/** The catalogue key of a line's own text, when the server sent a code instead of words: how a
 *  task the VAF agent handed the app ended (`room.TASK_RESULT_CODES`), or an answer that
 *  failed. Null when the line's text is shown as it is. */
export function lineTextKey(code?: string): string | null {
    switch (code) {
        case 'turn_failed': return 'appRoomTurnFailed';
        case 'declined': return 'appRoomTaskDeclined';
        case 'unavailable': return 'appRoomTaskUnavailable';
        case 'timeout': return 'appRoomTaskTimeout';
        case 'failed': return 'appRoomTaskFailed';
        case 'cancelled': return 'appRoomTaskCancelled';
        case 'disconnected': return 'appRoomTaskDisconnected';
        case 'file_refused': return 'appRoomTaskFileRefused';
        default: return null;
    }
}

/** Whether the composer is cleared when a line was taken: only while the view it was written in
 *  is still the one on screen and the box still holds that line. Typing the same words in
 *  another chat afterwards must not lose them. */
export function clearAfterSend(sentFrom: string | null, onScreen: string | null, sent: string, box: string): boolean {
    return !!sentFrom && sentFrom === onScreen && box.trim() === sent.trim();
}

/** The red dot on the row: new lines, and the room is not the one on screen. */
export const showUnreadDot = (unread: number | undefined, open: boolean) => (unread || 0) > 0 && !open;
