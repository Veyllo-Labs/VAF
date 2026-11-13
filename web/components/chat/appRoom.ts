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

/** The composer's hint: while no line can reach the app's agent, it shows the mention that
 *  reaches the VAF agent, because a line without it goes to the app's agent and is refused. */
export const placeholderKey = (canWrite: boolean) => (canWrite ? 'appRoomPlaceholder' : 'appRoomPlaceholderAgentOnly');

/** How wide, in lowercase Latin letters (`hintWidth`), the handle in the composer's hint may
 *  be. At 390 px the tightest form, Turkish "@\u2026 ile sor\u2026", leaves about 97 px for the handle,
 *  and one unit of `hintWidth` is at most about 8 px at the box's 14 px (Roboto, measured). */
export const HINT_HANDLE_MAX = 12;

/** A grapheme a font draws about twice as wide as a Latin letter: Han, kana, Hangul, the
 *  full-width forms and CJK punctuation, an emoji, a flag. */
const WIDE = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}\p{Extended_Pictographic}\p{Regional_Indicator}\u3000-\u303f\uff01-\uff60\uffe0-\uffe6]/u;
/** Latin letters a font draws about one and a half lowercase letters wide (10.5-12.4 px at 14 px
 *  against 7.1-8 px for an ordinary lowercase letter), so "@MMMMMMMMMMMM" does not pass as 12. */
const WIDE_LATIN = /^[mwMW]$/;
/** The narrow ones, about half a lowercase letter (3.4-4.9 px). */
const NARROW = /^[ijlrtfI.,:;'!|-]$/;
/** Any other capital, about a quarter wider than a lowercase letter (8.3-10 px). */
const CAPITAL = /^\p{Lu}$/u;

/** One grapheme's width in lowercase Latin letters; an accented letter weighs as its base. */
function graphemeWidth(g: string): number {
    if (WIDE.test(g)) return 2;
    const base = g.normalize('NFD').charAt(0);
    if (WIDE_LATIN.test(base)) return 1.6;
    if (CAPITAL.test(base)) return 1.25;
    if (NARROW.test(base)) return 0.6;
    return 1;
}

/** What a reader sees as one character: a grapheme (a family emoji, a Thai consonant with its
 *  vowel and tone marks), or a code point where the browser has no `Intl.Segmenter`. */
function graphemes(text: string): string[] {
    if (typeof Intl !== 'undefined' && typeof Intl.Segmenter === 'function') {
        return Array.from(new Intl.Segmenter(undefined, { granularity: 'grapheme' }).segment(text), s => s.segment);
    }
    return Array.from(text);
}

/** A text's width in lowercase Latin letters, grapheme by grapheme (`graphemeWidth`). */
export function hintWidth(text: string): number {
    return graphemes(text).reduce((sum, g) => sum + graphemeWidth(g), 0);
}

/** The handle the composer's hint shows after "@" ("Frag @Nobel …"). A textarea's placeholder
 *  can neither wrap nor end in an ellipsis (Chromium clips it hard, and a second line scrolls
 *  the box), and at 390 px the box leaves about 159 px of text: "@Bartholomäus ile sor…"
 *  measures 150 px (width 11.65), "Frag @MMMMMMMMMMMM …" 203 px (width 19.2, so it falls back).
 *  A handle wider than `max` is not cut, because
 *  a cut handle is no mention ("@Hubertus-Max" reaches nobody): the hint shows "VAF" then, which
 *  the server always takes for the VAF agent (`room.addressee`). */
export function hintHandle(handle: string, max: number = HINT_HANDLE_MAX): string {
    const h = (handle || '').trim();
    return h && hintWidth(h) <= max ? h : 'VAF';
}

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

/** The id the page gives a line it sends (`client_id`), so it can recognise the line in the
 *  room if it stops waiting for the answer: a version 4 UUID. `randomUUID` exists only in a
 *  secure context (HTTPS or localhost); on a plain-HTTP address in the local network the id is
 *  built from `getRandomValues`, which every context has. */
export function newLineId(): string {
    if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
    const b = crypto.getRandomValues(new Uint8Array(16));
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    const h = Array.from(b, x => x.toString(16).padStart(2, '0')).join('');
    return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

/** A line of the room as the check reads it: the person's line keeps the page's id as its
 *  `id` (to the app's agent) or as `msg` (to the VAF agent, waiting and stored alike). */
export type CheckedLine = { id: string; peer: string; msg?: string };

/** Whether a transcript holds the line the page sent under `lineId`. Only that line counts: an
 *  earlier line with the same words has another id. */
export function holdsLine(lines: ReadonlyArray<CheckedLine>, lineId: string): boolean {
    return !!lineId && lines.some(l => l.peer === 'person' && (l.id === lineId || l.msg === lineId));
}

/** How long the page asks the room after a give-up before it shows the notice. */
export const CONFIRM_WAIT_MS = 5000;

/** The socket as the check needs it. */
export type CheckSocket = { readyState: number; send(data: string): void } | null | undefined;
/** Hands every Veyllo App room transcript that arrives to `cb`; returns the unsubscribe. */
export type TranscriptListen = (cb: (lines: ReadonlyArray<CheckedLine>) => void) => () => void;
type Timers = { set: (fn: () => void, ms: number) => unknown; clear: (handle: unknown) => void };

/** After a send gave up: ask the room once, without marking anything read (`seen: false`), and
 *  read every transcript that arrives until one holds the line or `waitMs` passed. A poll
 *  answer from before the line was stored ends nothing. True: the line is there. False: not
 *  seen in time, or the room could not be asked (the socket is not open, or sending threw);
 *  the timer and the listener are gone in every case. */
export function confirmAfterGiveUp(socket: CheckSocket, listen: TranscriptListen, lineId: string,
    waitMs: number = CONFIRM_WAIT_MS,
    timers: Timers = { set: (fn, ms) => setTimeout(fn, ms), clear: h => clearTimeout(h as ReturnType<typeof setTimeout>) },
): Promise<boolean> {
    return new Promise(resolve => {
        if (!lineId || !socket || socket.readyState !== 1) {
            resolve(false);
            return;
        }
        let done = false;
        let stop: () => void = () => undefined;
        let handle: unknown = null;
        const finish = (found: boolean) => {
            if (done) return;
            done = true;
            stop();
            timers.clear(handle);
            resolve(found);
        };
        stop = listen(lines => { if (holdsLine(lines, lineId)) finish(true); });
        handle = timers.set(() => finish(false), waitMs);
        try {
            socket.send(JSON.stringify({ type: 'open_app_room', seen: false }));
        } catch {
            finish(false);
        }
    });
}

/** What a transcript means for a line the page gave up on and showed the notice for: `clear`
 *  when the line has arrived, the view it was written in is still on screen and the box still
 *  holds exactly its text (`clearAfterSend`; clear the box and the notice: a second Enter would
 *  send it twice); `wait` while another view owns the box (its draft is not this line, even
 *  with the same words); `drop` when the line arrived but the box holds something else (stop
 *  watching, leave the box); `wait` before the line arrived. */
export function lateLine(lines: ReadonlyArray<CheckedLine>, lineId: string, sentFrom: string | null,
    onScreen: string | null, text: string, box: string): 'clear' | 'drop' | 'wait' {
    if (!holdsLine(lines, lineId)) return 'wait';
    if (clearAfterSend(sentFrom, onScreen, text, box)) return 'clear';
    return sentFrom !== onScreen ? 'wait' : 'drop';
}

/** What the page does with a `lateLine` verdict: the line is in the room for `clear` and `drop`,
 *  so the notice ("send it again if it does not appear") is no longer true and goes in both;
 *  the box is emptied only for `clear` (it still holds exactly the line), a box the person
 *  changed is kept. */
export function lateEffect(verdict: 'clear' | 'drop' | 'wait'): { box: boolean; notice: boolean } {
    return { box: verdict === 'clear', notice: verdict !== 'wait' };
}

/** Whether the view goes to the person's line once the room took it: only while the room is
 *  still on screen (another chat must not jump to its bottom) and the view is at the bottom.
 *  A reader who scrolled up while the line was on its way stays where they are, the rule of
 *  every chat on the page. */
export function goToTakenLine(onScreen: string | null, atBottom: boolean): boolean {
    return onScreen === APP_ROOM_ID && atBottom;
}

/** Whether the composer is cleared when a line was taken: only while the view it was written in
 *  is still the one on screen and the box still holds that line. Typing the same words in
 *  another chat afterwards must not lose them. */
export function clearAfterSend(sentFrom: string | null, onScreen: string | null, sent: string, box: string): boolean {
    return !!sentFrom && sentFrom === onScreen && box.trim() === sent.trim();
}

/** The red dot on the row: new lines, and the room is not the one on screen. */
export const showUnreadDot = (unread: number | undefined, open: boolean) => (unread || 0) > 0 && !open;
