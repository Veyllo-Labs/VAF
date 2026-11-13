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

/** The result counter (mockup ".stat"): the line count, else the size in KB, else a neutral "done". */
export function toolResultStat(result?: string): ToolStat {
    if (!result || !result.trim()) return { key: 'toolStatDone' };
    const lines = result.trim().split('\n').filter(l => l.trim()).length;
    if (lines > 1) return { key: 'toolStatLines', count: lines };
    if (result.length >= 1024) return { key: 'toolStatKb', kb: result.length / 1024 };
    return { key: 'toolStatDone' };
}

export type DurationText =
    | { key: 'durationSeconds'; seconds: number }
    | { key: 'durationMinutesSeconds'; minutes: number; seconds: number };

/** A compact elapsed time: whole seconds under a minute, else minutes and seconds. */
export function durationText(s: number): DurationText {
    if (s < 60) return { key: 'durationSeconds', seconds: Math.round(s) };
    return { key: 'durationMinutesSeconds', minutes: Math.floor(s / 60), seconds: Math.round(s % 60) };
}
