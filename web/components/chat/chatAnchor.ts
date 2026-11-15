// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
//
// Where a chat left scrolled up comes back: at the same message, not at the same number.
// A raw scrollTop only lands on the same content when the column has the same width as
// when it was saved, and it often does not: the agent window reopens after the return
// (narrowing the column over 300 ms) or stays closed because its worker finished meanwhile.
// The chat therefore remembers its first visible bubble and that bubble's distance from the
// top edge, and places it there again (`scrollChatToMessage(idx, offset)` in page.tsx).
// tests/test_chat_scroll_anchor_web.py runs this file's code in node.

/**
 * Which message an anchor names, in a form that survives the server's transcript replacing
 * the kept picture. A message has no id, and its index is a position in the array it was
 * read from: the kept picture holds the browser's own `system` log lines, which the server
 * never sends (the page drops its `system` rows), so a log line above the message shifts its
 * index by one after the swap. Its place among the other rows does not move, and its role and
 * the start of its text tell a hit from a neighbour.
 */
export type ChatAnchorKey = { role: string; ordinal: number; head: string };

/** The first visible bubble (its `data-msg-idx`) and its top's distance below the view's top. */
export type ChatAnchor = { idx: number; offset: number; key?: ChatAnchorKey };

/** The fields of a chat message the key reads. */
export type AnchorMessage = { role: string; content?: unknown };

const HEAD_CHARS = 200;

function headOf(m: AnchorMessage): string {
    return String(m.content ?? '').slice(0, HEAD_CHARS);
}

/** The key of `messages[idx]`, or null when there is no such message. */
export function anchorKey(messages: AnchorMessage[], idx: number): ChatAnchorKey | null {
    const m = messages[idx];
    if (!m) return null;
    let ordinal = 0;
    for (let i = 0; i < idx; i++) if (messages[i].role !== 'system') ordinal++;
    return { role: m.role, ordinal, head: headOf(m) };
}

/**
 * Where the anchor's message is in `messages` now, or null when it is not there (the caller
 * then falls back to the saved number). The saved index wins when it still holds the same
 * message; otherwise the message is found by its place among the non-`system` rows, which a
 * transcript without the browser's log lines keeps. A `system` row exists only in the kept
 * picture: it is found by its text, nearest to where it was, or not at all. Without a key
 * (an anchor read where no messages were at hand) the index is taken as it is.
 */
export function resolveAnchor(messages: AnchorMessage[], anchor: ChatAnchor): number | null {
    const key = anchor.key;
    if (!key) return anchor.idx >= 0 && anchor.idx < messages.length ? anchor.idx : null;
    const at = messages[anchor.idx];
    if (at && at.role === key.role && headOf(at) === key.head) return anchor.idx;
    if (key.role === 'system') {
        let best = -1;
        for (let i = 0; i < messages.length; i++) {
            const m = messages[i];
            if (m.role === 'system' && headOf(m) === key.head
                && (best < 0 || Math.abs(i - anchor.idx) < Math.abs(best - anchor.idx))) best = i;
        }
        return best < 0 ? null : best;
    }
    let ordinal = 0;
    for (let i = 0; i < messages.length; i++) {
        if (messages[i].role === 'system') continue;
        if (ordinal === key.ordinal) return messages[i].role === key.role ? i : null;
        ordinal++;
    }
    return null;
}

/** One rendered bubble: its index and its top and bottom on the same axis as `viewTop`. */
export type ChatAnchorRow = { idx: number; top: number; bottom: number };

/**
 * The first row, in the order given (the DOM order), whose bottom lies below the view's top
 * edge. The offset is negative when the bubble starts above that edge. A row that ends at the
 * edge, within a pixel of rounding, is not visible and is skipped. `null` for no such row.
 */
export function pickAnchor(rows: ChatAnchorRow[], viewTop: number): ChatAnchor | null {
    for (const row of rows) {
        if (row.bottom > viewTop + 1) return { idx: row.idx, offset: row.top - viewTop };
    }
    return null;
}

/**
 * The scrollTop that puts a row `offset` px below the view's top edge, given where the row
 * is now (`rowTop`, on the same axis as `viewTop`) at the current `scrollTop`.
 */
export function anchoredScrollTop(scrollTop: number, rowTop: number, viewTop: number, offset: number): number {
    return scrollTop + (rowTop - viewTop) - offset;
}
