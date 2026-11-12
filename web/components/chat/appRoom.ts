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
        case 'text_only': return 'appRoomTextOnly';
        case 'already_sent_again': return 'appRoomAlreadySentAgain';
        // The browser stopped waiting and the room did not show the line: it may still come.
        case 'timeout': return 'appRoomSendUnconfirmed';
        default: return 'appRoomFailed';
    }
}

/** The empty room's explanation: while no line can reach the app's agent it says what IS
 *  possible (asking the VAF agent with its handle). */
export const emptyKey = (canWrite: boolean) => (canWrite ? 'appRoomEmpty' : 'appRoomEmptyAgentOnly');

/** The composer's hint: while no line can reach the app's agent, only the VAF agent's form. */
export const placeholderKey = (canWrite: boolean) => (canWrite ? 'appRoomPlaceholder' : 'appRoomPlaceholderAgentOnly');

/** "to <name>" under a line: the person addressed gets their own form ("an dich", "sana"). */
export const toKey = (peer: string) => (peer === 'person' ? 'appRoomToYou' : 'appRoomTo');

/** The catalogue key of how a line to the agent in the Veyllo App stands, and whether the
 *  person is offered to send it again (only a line that will not arrive any more). */
export function deliveryOf(state: string, reason?: string, devices?: number, device?: string,
    resent?: boolean): { key: string; values?: Record<string, string>; resend: boolean; quiet?: boolean } {
    // A line the person already sent again offers no second resend and is no longer a
    // warning: it reads, quietly, that it was sent again.
    if (resent) return { key: 'appRoomSentAgain', resend: false, quiet: true };
    return deliveryState(state, reason, devices, device);
}

function deliveryState(state: string, reason?: string, devices?: number, device?: string):
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
        case 'timer': return 'appRoomWakeTimer';
        case 'process': return 'appRoomWakeProcess';
        case 'draft': return 'appRoomWakeDraft';
        case 'confirm': return 'appRoomConfirmed';
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

/** One line on its way at a time: Enter, the send button and every "Send again" wait until
 *  the open request answered, so a double Enter or a second click never sends the line twice
 *  (each "@" line is a model call, each line to the app a billed relay message). */
export type RoomSend = { busy: boolean; text: string; resendOf: string | null };
export const ROOM_SEND_IDLE: RoomSend = { busy: false, text: '', resendOf: null };

/** The send that starts now, or null while another is open or there is nothing to send. */
export function beginSend(current: RoomSend, text: string, resendOf?: string | null): RoomSend | null {
    if (current.busy || !text.trim()) return null;
    return { busy: true, text: text.trim(), resendOf: resendOf || null };
}

/** How long a line may be on its way before the composer gives up on it (the request is
 *  aborted and the room is asked once whether the line arrived anyway). */
export const SEND_TIMEOUT_MS = 30000;

/** How a room send ended: `ok` with the server's answer, `refused` with its code, `timeout`
 *  when no complete answer came within the time (also when the headers came and the body did
 *  not), `error` when the request failed outright. */
export type SendOutcome = { status: 'ok' | 'refused' | 'timeout' | 'error'; data: Record<string, unknown> };

/** One room send with its time limit. The timer covers the whole answer, the body included: a
 *  body that stalls is aborted too, and an aborted body never reads as a success. */
export async function sendWithTimeout(fetchFn: typeof fetch, url: string, init: RequestInit,
    timeoutMs: number = SEND_TIMEOUT_MS): Promise<SendOutcome> {
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), timeoutMs);
    try {
        const res = await fetchFn(url, { ...init, signal: abort.signal });
        let data: Record<string, unknown> = {};
        try {
            data = await res.json();
        } catch {
            if (abort.signal.aborted) return { status: 'timeout', data: {} };
        }
        if (abort.signal.aborted) return { status: 'timeout', data: {} };
        return res.ok ? { status: 'ok', data } : { status: 'refused', data };
    } catch {
        return { status: abort.signal.aborted ? 'timeout' : 'error', data: {} };
    } finally {
        clearTimeout(timer);
    }
}

/** After a send gave up: whether the room now holds the line anyway (stored and sent after the
 *  browser stopped waiting). It does when a line of the person that was not there before the
 *  send carries the same text. */
export function arrivedAfterAll(before: ReadonlySet<string>,
    lines: ReadonlyArray<{ id: string; peer: string; text: string }>, text: string): boolean {
    const wanted = text.trim();
    return lines.some(l => l.peer === 'person' && !before.has(l.id) && (l.text || '').trim() === wanted);
}

/** Whether the composer is cleared when a line was taken: only while the view it was written in
 *  is still the one on screen and the box still holds that line. Typing the same words in
 *  another chat afterwards must not lose them. */
export function clearAfterSend(sentFrom: string | null, onScreen: string | null, sent: string, box: string): boolean {
    return !!sentFrom && sentFrom === onScreen && box.trim() === sent.trim();
}

/** The red dot on the row: new lines, and the room is not the one on screen. */
export const showUnreadDot = (unread: number | undefined, open: boolean) => (unread || 0) > 0 && !open;
