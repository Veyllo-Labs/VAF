// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
//
// One bubble per generation round of a turn. The engine numbers every provider call of a
// turn (`round_start`, docs/OBSERVABILITY.md) and the chat lane tags each streamed update
// with its round (`agent_message_update.round`, docs/web-ui/WEBUI_WEBSOCKET_FLOW.md). These
// helpers place such an update by its round instead of by its position: a re-run's reasoning
// gets its own bubble instead of growing the first one, and a late throttled tail of an
// earlier round lands in that round's bubble even when a tool card already stands after it.
// A reloaded chat carries the same fields (`history_update`, `_history_projection`).
// tests/test_turn_rounds_web.py runs this file's code in node.

/** The fields of a chat message these helpers read and write. */
export interface RoundMessage {
    role: string;
    content: string;
    timestamp: number;
    kind?: string;
    turnId?: string;
    round?: number;
    roundAnswer?: boolean;
}

export interface RoundUpdate {
    round: number;
    content: string;
    /** Whether the round passed the final-answer path; known once the turn is over. */
    answer?: boolean;
    turnId?: string;
    now: number;
}

export interface Placed<M> {
    messages: M[];
    /** The index of the round's bubble, -1 when none was created. */
    index: number;
    created: boolean;
}

/** Where the current turn starts: after the last user message (a wake card is one too). */
const turnStart = (msgs: RoundMessage[]): number => {
    for (let i = msgs.length - 1; i >= 0; i--) {
        if (msgs[i].role === 'user') return i + 1;
    }
    return 0;
};

/** A streamed bubble of this turn. A proactive message (`kind`) belongs to no turn. */
const ofTurn = (m: RoundMessage, turnId?: string): boolean =>
    m.role === 'assistant' && !m.kind && typeof m.round === 'number' &&
    (turnId == null || m.turnId === turnId);

const findRound = (msgs: RoundMessage[], round: number, turnId?: string): number => {
    for (let i = turnStart(msgs); i < msgs.length; i++) {
        if (ofTurn(msgs[i], turnId) && msgs[i].round === round) return i;
    }
    return -1;
};

/**
 * Put one round's text where that round stands. The update carries the round's whole text so
 * far, never the turn's, so nothing is ever cut off a previous bubble. An existing bubble of
 * the round is updated in place; otherwise a new one goes in front of the first bubble of a
 * later round of this turn, or at the end. Text that is only whitespace creates nothing.
 */
export function placeRoundUpdate<M extends RoundMessage>(messages: M[], upd: RoundUpdate): Placed<M> {
    const content = String(upd.content ?? '');
    const at = findRound(messages, upd.round, upd.turnId);
    const flag = typeof upd.answer === 'boolean' ? { roundAnswer: upd.answer } : {};
    if (at !== -1) {
        const out = [...messages];
        out[at] = { ...out[at], content, turnId: upd.turnId ?? out[at].turnId, ...flag };
        return { messages: out, index: at, created: false };
    }
    if (content.trim() === '') return { messages, index: -1, created: false };
    let insertAt = messages.length;
    for (let i = turnStart(messages); i < messages.length; i++) {
        const m = messages[i];
        if (ofTurn(m, upd.turnId) && (m.round as number) > upd.round) { insertAt = i; break; }
    }
    // Distinct times inside the turn: a bubble's thinking duration and its expand state are
    // keyed by its timestamp.
    let timestamp = upd.now;
    const taken = new Set(messages.slice(turnStart(messages)).map(m => m.timestamp));
    while (taken.has(timestamp)) timestamp += 1;
    const bubble = {
        role: 'assistant', content, timestamp, turnId: upd.turnId, round: upd.round, ...flag,
    } as M;
    const out = [...messages.slice(0, insertAt), bubble, ...messages.slice(insertAt)];
    return { messages: out, index: insertAt, created: true };
}

/**
 * Take back the bubble of one round of the current turn (a retracted generation), and nothing
 * else: an earlier round's bubble is text the person already read.
 */
export function removeRoundBubble<M extends RoundMessage>(messages: M[], round: number, turnId?: string): M[] {
    const at = findRound(messages, round, turnId);
    if (at === -1) return messages;
    return [...messages.slice(0, at), ...messages.slice(at + 1)];
}

export interface RoundText {
    /** The visible answer text of the message (reasoning and tool-call markup removed). */
    text: string;
    roundAnswer?: boolean;
}

/**
 * Which assistant texts of a grouped turn form the answer below the rail, and which stand as
 * `'say'` steps in it. The body is the turn's validated answers: every earlier round that
 * passed the final-answer path (`roundAnswer`), in order, then the last assistant
 * (`answerIdx`, its text stays with that message). Every other non-empty text, an
 * announcement before a tool call or a reply a guard sent back, is a `'say'` step at its
 * place in time. Without round fields this is the old rule: everything but the last
 * assistant is a `'say'` step.
 */
export function bodyAndSay(items: RoundText[], answerIdx: number): { bodyLead: number[]; say: number[] } {
    const bodyLead: number[] = [];
    const say: number[] = [];
    items.forEach((it, i) => {
        if (i === answerIdx || String(it.text ?? '').trim() === '') return;
        if (it.roundAnswer === true && i < answerIdx) bodyLead.push(i);
        else say.push(i);
    });
    return { bodyLead, say };
}
