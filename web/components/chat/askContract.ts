// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
//
// The chat's half of the question contract (vaf/tools/ask_user.py). A question the agent asked
// in a chat ends its turn, and two things cross the wire as TEXT, because they are what the
// history holds and a reloaded chat has nothing else: the tool result names the question and
// its options (`ASKED THE USER. ...` then one JSON line), and the turn's answer is the question
// followed by the options as a numbered list - which every surface without buttons shows as it
// is. Here the list is cut off again and drawn as buttons. tests/test_ask_user_chat.py pins the
// prefix and the list format against the Python side, so neither can change alone.

export const ASKED_PREFIX = 'ASKED THE USER.';

export interface AskQuestion {
    question: string;
    options: string[];
}

/** The question a tool result carries, or null for any other result. */
export function askOf(toolContent: string | null | undefined): AskQuestion | null {
    const text = String(toolContent ?? '');
    if (!text.startsWith(ASKED_PREFIX)) return null;
    const nl = text.indexOf('\n');
    if (nl < 0) return null;
    try {
        const spec = JSON.parse(text.slice(nl + 1));
        const question = String(spec?.question ?? '').trim();
        const options = Array.isArray(spec?.options) ? spec.options.map((o: unknown) => String(o)) : [];
        return question ? { question, options } : null;
    } catch {
        return null;
    }
}

/** Every question a turn's tool results asked, in order. */
export function asksOf(tools: { content: string }[]): AskQuestion[] {
    return tools.map(m => askOf(m.content)).filter((q): q is AskQuestion => !!q && q.options.length > 0);
}

/** The numbered list the answer carries, built exactly as ask_user.options_block builds it. */
export function optionsBlock(options: string[]): string {
    return options.map((o, i) => `${i + 1}. ${o}`).join('\n');
}

/** The answer without the numbered lists the buttons replace. Only an exact list is cut, so
 *  a turn whose text is not the one the tool built keeps every word. */
export function withoutOptions(answer: string, asks: AskQuestion[]): string {
    let out = answer;
    for (const q of asks) {
        const block = optionsBlock(q.options);
        const at = out.lastIndexOf(block);
        if (at >= 0) out = out.slice(0, at) + out.slice(at + block.length);
    }
    return out === answer ? answer : out.replace(/\n{3,}/g, '\n\n').trim();
}

/** Which option a reply picked, or -1 when the person answered in their own words. */
export function pickedIndex(options: string[], reply: string | null): number {
    if (reply == null) return -1;
    const r = reply.trim().toLowerCase();
    const byText = options.findIndex(o => o.trim().toLowerCase() === r);
    if (byText >= 0) return byText;
    const n = Number(r);
    return Number.isInteger(n) && n >= 1 && n <= options.length ? n - 1 : -1;
}
