// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
//
// The chat's half of the draft contract (vaf/core/outbound_hold.py). Three literals cross the
// wire as TEXT, because they are what the history holds and a reloaded chat has nothing else:
// the tool result that parked a draft names it (`NOT SENT YET. Draft mail:12`), the turn that
// stopped at it ends with one fixed sentence, and the wake turn a sent draft queues starts
// with its own prefix. tests/test_outbound_hold_wiring.py pins all three against the Python
// constants, so neither side can rename one alone.

/** The turn's closing message when it stopped at a draft; the chat shows nothing for it. */
export const DRAFT_TURN_END = "Draft waiting for the user's decision; the turn ended here.";

/** The first line of the wake turn a sent draft queues; the rest is for the agent. */
export const DRAFT_WAKE_PREFIX = '✉ Draft sent:';

const CREATED = /^NOT SENT YET\. Draft (mail|call):(\d+)/;

/** The draft a tool result parked (`mail:12`, `call:7`), or null for any other result. */
export function draftRefOf(toolContent: string | null | undefined): string | null {
    const m = CREATED.exec(String(toolContent ?? ''));
    return m ? `${m[1]}:${m[2]}` : null;
}

/** Exactly the closing sentence, never a prefix: a real answer can never be taken for it. */
export function isDraftTurnEnd(content: string | null | undefined): boolean {
    return String(content ?? '').trim() === DRAFT_TURN_END;
}
