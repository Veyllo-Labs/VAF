// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

// The tool card's right-aligned counter and its durations, as catalogue keys with their values.
// Nothing here is visible text: the component looks each key up under `main`, so every locale
// gets its own words, units and plural forms. Kept free of React so node can run it in tests.

export type ToolStat =
    | { key: 'toolStatDone' }
    | { key: 'toolStatLines'; count: number }
    | { key: 'toolStatKb'; kb: number };

const utf8 = new TextEncoder();

/** The result counter (mockup ".stat"): the line count, else the size in KB, else a neutral "done".
 *  The size is UTF-8 bytes, not `length`: a string counts UTF-16 units, so 600 Japanese characters
 *  (1800 bytes) used to stay under the 1 KB threshold and read "done". */
export function toolResultStat(result?: string): ToolStat {
    if (!result || !result.trim()) return { key: 'toolStatDone' };
    const lines = result.trim().split('\n').filter(l => l.trim()).length;
    if (lines > 1) return { key: 'toolStatLines', count: lines };
    const bytes = utf8.encode(result).length;
    if (bytes >= 1024) return { key: 'toolStatKb', kb: bytes / 1024 };
    return { key: 'toolStatDone' };
}

export type DurationText =
    | { key: 'durationSeconds'; seconds: number }
    | { key: 'durationMinutesSeconds'; minutes: number; seconds: number };

/** A compact elapsed time: whole seconds under a minute, else minutes and seconds. The total is
 *  rounded before it is split, so 119.6 s reads 2m 0s rather than 1m 60s, and 59.6 s reads 1m 0s. */
export function durationText(s: number): DurationText {
    const total = Math.round(s);
    if (total < 60) return { key: 'durationSeconds', seconds: total };
    return { key: 'durationMinutesSeconds', minutes: Math.floor(total / 60), seconds: total % 60 };
}
