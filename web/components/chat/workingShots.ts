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

export function splitTurnFiles(chips: FileChip[], stepToolIds: string[], expanded: boolean): TurnFiles {
    const files: FileChip[] = [];
    const shots: FileChip[] = [];
    for (const c of chips) (isWorkingShot(c) ? shots : files).push(c);
    const byStep: Record<string, FileChip[]> = {};
    if (!expanded) return { files, byStep, strip: shots };
    const steps = new Set(stepToolIds.filter(id => !!id));
    const strip: FileChip[] = [];
    for (const s of shots) {
        if (s.toolId && steps.has(s.toolId)) (byStep[s.toolId] ||= []).push(s);
        else strip.push(s);
    }
    return { files, byStep, strip };
}
