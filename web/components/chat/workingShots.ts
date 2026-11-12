// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

/**
 * Which of a turn's created files are working screenshots, and where they are drawn.
 *
 * A picture a tool took to check its own work (`render_check`, `sandbox_preview`) arrives
 * as a `file_created` event with `purpose: "check"` and the `toolId` of the step that took
 * it (vaf/core/web_interface.py, notify_file_created). Such a picture is part of its step,
 * not an attachment of the answer. The rule is the SOURCE, never a count: five charts the
 * person asked for stay attachments, and one check picture is still a step's picture.
 * Everything without that purpose comes back in `files`, the same objects in the same
 * order, so the attachment row of a turn without check pictures is unchanged.
 */

export type FileChip = { path: string; name: string; purpose?: string; toolId?: string };

export type TurnFiles = {
    /** Attachments: every chip that is not a check picture, untouched and in order. */
    files: FileChip[];
    /** Expanded timeline: the check pictures of each step, keyed by its tool-call id. */
    byStep: Record<string, FileChip[]>;
    /** One sideways strip for the turn: every check picture while collapsed, the
     *  unlinked ones while expanded. */
    strip: FileChip[];
};

// The image extensions of components/ImageViewer.tsx (isImageFile). A copy, because that
// module is a React component and this helper runs in node in the tests; the test suite
// pins the two lists equal.
export const SHOT_IMAGE_EXTS = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp', 'ico'];

export function isWorkingShot(chip: FileChip): boolean {
    if (chip.purpose !== 'check') return false;
    const ext = (chip.path.split('.').pop() || '').toLowerCase();
    return SHOT_IMAGE_EXTS.includes(ext);
}

/**
 * The tool-call ids of a turn's steps, the keys a check picture is matched on. Only tool
 * steps have one (thinking and spoken lines do not), and a step without an id matches
 * nothing, so its pictures go to the turn strip.
 */
export function stepToolIds(actions: { kind: string; msg: { toolId?: string } }[]): string[] {
    return actions.filter(a => a.kind === 'tool').map(a => a.msg.toolId || '').filter(id => id !== '');
}

export function splitTurnFiles(chips: FileChip[], stepIds: string[], expanded: boolean): TurnFiles {
    const files: FileChip[] = [];
    const shots: FileChip[] = [];
    for (const c of chips) (isWorkingShot(c) ? shots : files).push(c);
    // No prototype: the keys are tool-call ids from the provider, and an id such as
    // "constructor" or "__proto__" must be a key like any other, not an inherited member.
    const byStep: Record<string, FileChip[]> = Object.create(null);
    if (!expanded) return { files, byStep, strip: shots };
    const steps = new Set(stepIds.filter(id => !!id));
    const strip: FileChip[] = [];
    for (const s of shots) {
        if (s.toolId && steps.has(s.toolId)) (byStep[s.toolId] ||= []).push(s);
        else strip.push(s);
    }
    return { files, byStep, strip };
}

/** The check pictures of one step, or none. Two guards: an own key only (an inherited member is
 *  not this step's, even when it is an array), and only an array (an own key may still hold
 *  something else). Each has a case only it catches, pinned in the tests. */
export function stepShots(byStep: Record<string, FileChip[]>, toolId: string | undefined): FileChip[] {
    if (!toolId || !Object.prototype.hasOwnProperty.call(byStep, toolId)) return [];
    const shots = byStep[toolId];
    return Array.isArray(shots) ? shots : [];
}

// The faded edge of the screenshot strip, in px. Only the side where more pictures hide
// gets it, so a row that fits shows sharp edges and the fade itself says "there is more".
export const FADE_PX = 28;

/**
 * The soft edge is a gradient MASK on the scroller, not a blur: a filter over scrolling
 * content is rasterised again on every scroll frame, a mask is composited and costs
 * nothing while the row stands still. `undefined` when everything fits.
 */
export function edgeMask(atStart: boolean, atEnd: boolean): string | undefined {
    if (atStart && atEnd) return undefined;
    const left = atStart ? '#000 0' : `transparent 0, #000 ${FADE_PX}px`;
    const right = atEnd ? '#000 100%' : `#000 calc(100% - ${FADE_PX}px), transparent 100%`;
    return `linear-gradient(to right, ${left}, ${right})`;
}
