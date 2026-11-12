// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

/**
 * How a chat is drawn when the person comes back to it.
 *
 * The server is the truth for a chat's view. `history_update` brings the stored transcript
 * (with the prompt of a running turn and the files of every answer), and the `live_replay`
 * right behind it brings what the running turn and its workers did while this browser was
 * looking at another chat (vaf/core/live_view.py). What the browser kept of the chat is only
 * a first picture for the moment before that arrives: it never adds a row the server did not
 * send, except its own system log lines, and a kept "generating" flag never outvotes the
 * server's "not active" (one short window after a LOCAL send aside, the enqueue race).
 */

import type { FileChip } from './workingShots';

/** One message of a `history_update`, as the server sends it (`_history_projection`). */
export type HistoryWireMessage = {
    role: string;
    content?: unknown;
    timestamp?: string | number | null;
    kind?: string;
    images?: { url: string; name: string }[];
    toolId?: string;
    toolName?: string;
    toolStatus?: string;
    round?: unknown;
    roundAnswer?: unknown;
    turnId?: unknown;
    files?: unknown;
};

/** The chat row the page builds from it. A subset of the page's Message type. */
export type HistoryMessage = {
    role: 'user' | 'assistant' | 'system' | 'tool' | 'workflow';
    content: string;
    timestamp: number;
    kind?: string;
    images?: { url: string; name: string }[];
    toolId?: string;
    toolName?: string;
    toolStatus?: 'running' | 'completed' | 'error' | 'unknown';
    round?: number;
    roundAnswer?: boolean;
    turnId?: string;
    downloadFiles?: FileChip[];
    _order: number;
};

/** The files of a stored answer as chips, keeping the `purpose` and `toolId` fields of the file event. */
export function historyFiles(files: unknown): FileChip[] | undefined {
    if (!Array.isArray(files)) return undefined;
    const out: FileChip[] = [];
    for (const f of files) {
        if (!f || typeof f !== 'object') continue;
        const { path, name, purpose, toolId } = f as Record<string, unknown>;
        if (typeof path !== 'string' || !path) continue;
        out.push({
            path,
            name: typeof name === 'string' && name ? name : (path.split('/').pop() || 'file'),
            purpose: typeof purpose === 'string' ? purpose : undefined,
            toolId: typeof toolId === 'string' ? toolId : undefined,
        });
    }
    return out.length > 0 ? out : undefined;
}

/**
 * The field-by-field rebuild of one history message. This object is rebuilt field by field,
 * so a field not named here is silently dropped (the class that cost `diffs`, `activity` and
 * the images before): every field the server projects is named here, and nowhere else.
 */
export function historyMessage(m: HistoryWireMessage, idx: number, nowMs: number): HistoryMessage {
    return {
        role: m.role as HistoryMessage['role'],
        content: m.content as string,
        timestamp: m.timestamp ? new Date(m.timestamp).getTime() : nowMs,
        kind: m.kind,   // proactive bubble tag -> re-plays the avatar animation on reload/chat-switch
        // The backend has always sent these for a user message that carried an attachment.
        images: m.images,
        _order: idx,
        toolId: m.toolId,
        toolName: m.toolName,
        toolStatus: m.toolStatus as HistoryMessage['toolStatus'] | undefined,
        // A turn stored per generation round: the same placement and answer rule as while it
        // streamed.
        round: typeof m.round === 'number' ? m.round : undefined,
        roundAnswer: typeof m.roundAnswer === 'boolean' ? m.roundAnswer : undefined,
        // Which exchange produced the answer: a file announced later for the same turn (an
        // async coder) finds this bubble by it.
        turnId: typeof m.turnId === 'string' && m.turnId ? m.turnId : undefined,
        // The files the answer came with, stored on the server, so a chat switch or a reload
        // keeps them.
        downloadFiles: historyFiles(m.files),
    };
}

/** How long a LOCAL send lets the kept "generating" flag stand against the server. */
export const LOCAL_SEND_WINDOW_MS = 15000;

/**
 * Is the chat active, as far as the view is concerned? The server's `isActive` is the queue's
 * answer and the truth. The one honest gap is the enqueue race: the person just pressed send
 * and the server computed its answer before the chat frame reached the queue. So the kept flag
 * counts only within a short window after a local send (the page zeroes that time on every
 * chat switch), and never otherwise.
 */
export function entryActivity(serverActive: boolean, kept: { isGenerating?: boolean } | undefined,
                              lastLocalSendMs: number, nowMs: number): { active: boolean; hadLocalGenerating: boolean } {
    const recentLocalSend = lastLocalSendMs > 0 && (nowMs - lastLocalSendMs) < LOCAL_SEND_WINDOW_MS;
    const hadLocalGenerating = recentLocalSend && !!kept?.isGenerating;
    return { active: serverActive || hadLocalGenerating, hadLocalGenerating };
}

/** How close to the local send a kept prompt has to be to count as the one just sent. */
export const LOCAL_PROMPT_SLACK_MS = 2000;

/**
 * Which kept row of an active chat may stand next to the server's transcript: the browser's
 * own system log lines, which the server never sends, and the person's own prompt sent in
 * this tab moments ago when the server's answer was computed before that chat frame reached
 * it (the enqueue race; the server sends the prompt in every later answer). A kept tool card,
 * assistant bubble or older prompt is not re-added: the server sends the running turn's
 * prompt and the replay its cards and its streamed text, so a kept copy could only be stale.
 */
export function keepCachedOrphan(msg: { role: string; content?: unknown; timestamp?: number },
                                 serverMsgs: { role: string; content?: unknown }[] = [],
                                 lastLocalSendMs = 0,
                                 keptMsgs: { role: string; content?: unknown }[] = [msg]): boolean {
    if (msg.role === 'system') return true;
    if (msg.role !== 'user' || !(lastLocalSendMs > 0)) return false;
    if ((msg.timestamp ?? 0) < lastLocalSendMs - LOCAL_PROMPT_SLACK_MS) return false;
    // Counted, not matched: the same words sent twice are two prompts, and an earlier one the
    // server has stored must not stand in for the one it has not seen yet.
    const same = (m: { role: string; content?: unknown }) => m.role === 'user' && m.content === msg.content;
    return keptMsgs.filter(same).length > serverMsgs.filter(same).length;
}

/** What a replay asks of the page around the events themselves. */
export type ReplayPlan = {
    /** It carries the server's picture of a worker: the kept window is replaced by it. */
    workerEvents: boolean;
    /** The worker's last word was idle or error: its window stays closed. */
    finishedWorker: boolean;
    /** A workflow finished while the chat was not on screen: its panel stays closed. */
    workflowDone: boolean;
};

export const WORKER_EVENT = /^(subagent_|browser_)|_state$/;

/** Tools that open the specialist's window (the page's own test in its tool_update branch). */
const WORKER_TOOL = /(?:^|[^a-z])(librarian|research|document|coding|browser)_agent(?:$|[^a-z])/;

export function planReplay(events: { type?: unknown; presence?: unknown; subType?: unknown; name?: unknown }[]): ReplayPlan {
    const types = events.map(e => String(e?.type ?? ''));
    // The worker's last word, in the order things happened: a worker's own presence, or the
    // start of a specialist tool after it (a new worker that is running now).
    let last: string | undefined;
    for (const e of events) {
        if (e?.type === 'subagent_update' && e.presence) last = String(e.presence).trim().toLowerCase();
        else if (e?.type === 'tool_update' && e.subType === 'start'
                 && WORKER_TOOL.test(String(e.name ?? '').toLowerCase())) last = 'online';
    }
    return {
        workerEvents: types.some(t => WORKER_EVENT.test(t)),
        finishedWorker: last === 'idle' || last === 'error',
        workflowDone: types.includes('workflow_done'),
    };
}

type Timers = { set: (fn: () => void, ms: number) => unknown; clear: (handle: unknown) => void };

/**
 * A running chat's transcript, held until its live_replay arrives, so both are drawn in one
 * go: drawn alone, the transcript would replace the kept picture without the running reply
 * for a frame (the blink). A replay that does not come within `ms` still lets it through.
 */
export class LiveHistoryHold<M> {
    private held: { sessionId: string; messages: M[]; handle: unknown } | null = null;

    constructor(private readonly ms: number,
                private readonly timers: Timers = {
                    set: (fn, ms) => setTimeout(fn, ms),
                    clear: (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
                }) {}

    hold(sessionId: string, messages: M[], onTimeout: () => void): void {
        if (this.held) this.timers.clear(this.held.handle);
        this.held = { sessionId, messages, handle: this.timers.set(onTimeout, this.ms) };
    }

    /** The held transcript of this chat (any chat when none is named), released. */
    take(sessionId?: string): M[] | undefined {
        const held = this.held;
        if (!held || (sessionId && held.sessionId !== sessionId)) return undefined;
        this.timers.clear(held.handle);
        this.held = null;
        return held.messages;
    }
}

/** What the person had typed (and attached) in a chat, kept while they look at another. */
export type ComposerDraft<S = unknown> = {
    text: string;
    images: { id: string; url: string; name: string }[];
    selections: S[];
};

export const MAX_COMPOSER_DRAFTS = 20;

/**
 * The composer per chat, in memory only (never browser storage: it can hold anything the
 * person typed). At most `MAX_COMPOSER_DRAFTS` chats; the chat left longest ago goes first.
 * An empty draft is not kept.
 */
export class ComposerDrafts<S = unknown> {
    private drafts = new Map<string, ComposerDraft<S>>();

    constructor(private readonly max: number = MAX_COMPOSER_DRAFTS) {}

    save(chatId: string | null | undefined, draft: ComposerDraft<S>): void {
        if (!chatId) return;
        this.drafts.delete(chatId);
        if (!draft.text && draft.images.length === 0 && draft.selections.length === 0) return;
        this.drafts.set(chatId, draft);
        while (this.drafts.size > this.max) {
            const oldest = this.drafts.keys().next().value as string;
            this.drafts.delete(oldest);
        }
    }

    /** The chat's draft, removed from the store (it is in the composer again), or an empty one. */
    take(chatId: string | null | undefined): ComposerDraft<S> {
        const empty: ComposerDraft<S> = { text: '', images: [], selections: [] };
        if (!chatId) return empty;
        const draft = this.drafts.get(chatId);
        this.drafts.delete(chatId);
        return draft ?? empty;
    }

    get size(): number {
        return this.drafts.size;
    }
}
