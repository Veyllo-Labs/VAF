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

/** The first visible bubble (its `data-msg-idx`) and its top's distance below the view's top. */
export type ChatAnchor = { idx: number; offset: number };

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
