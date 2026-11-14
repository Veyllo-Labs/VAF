// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

/**
 * The per-user access picker's rules: which tools the picker lists, what each access preset
 * grants from that list, and the two hooks that keep a form's selection on that list. The user
 * manager and its tests run the same code (tests/test_user_access_picker_web.py).
 */

import { useCallback, useEffect, useRef, useState } from 'react';

export type AccessPreset = 'standard' | 'full' | 'readonly' | 'custom';

/** One row of the picker: a registry tool, or a name only the coding agent runs. */
export type PickerTool = { name: string; description?: string; account_opt_in?: boolean };

// Pattern-based so presets also cover the admin's CUSTOM tools (matched on the tool name, not a fixed list).
export const WW_DESTRUCTIVE_RE = /(^|_)(delete|remove|drop|clear|reset|uninstall|kill|destroy|wipe|purge|revoke)(_|$)/i;
export const WW_READABLE_RE = /(^|_)(list|read|get|search|view|show|fetch|find|query|describe|status|info|count)(_|$)/i;

/** Resolve a preset to concrete tool names + workflow ids from the live lists. Returns null for 'custom'
 *  (the caller keeps the manual selection). 'full' selects literally everything. A tool that
 *  declares `account_opt_in` (e.g. `ssh`) is granted only by name, so 'standard' and 'readonly'
 *  leave it out; 'full' and a custom selection can grant it. */
export function resolveAccessPreset(
    preset: AccessPreset,
    tools: { name: string; account_opt_in?: boolean }[],
    workflows: { id: string }[],
): { tools: string[]; workflows: string[] } | null {
    if (preset === 'full') return { tools: tools.map(t => t.name), workflows: workflows.map(w => w.id) };
    if (preset === 'readonly') return { tools: tools.filter(t => !t.account_opt_in && WW_READABLE_RE.test(t.name) && !WW_DESTRUCTIVE_RE.test(t.name)).map(t => t.name), workflows: [] };
    if (preset === 'standard') return { tools: tools.filter(t => !t.account_opt_in && !WW_DESTRUCTIVE_RE.test(t.name)).map(t => t.name), workflows: workflows.map(w => w.id) };
    return null;
}

/** The picker's tool list: the live registry, then the coding agent's own tool names
 *  (`GET /api/users/tool-universe`), each name exactly once.
 *
 *  The two lists overlap: the git tools, `update_codex` and `add_memory` are registered for
 *  the main agent AND run inside the coder. A plain concatenation listed them twice, so 'full'
 *  and "Select all" wrote every shared name twice, the grid carried duplicate React keys and the
 *  counter over-counted. The first entry wins, so a registry tool keeps its own description and
 *  its `account_opt_in` flag; a coder name the registry lacks (`bash`, `run_tests`, ...) gets a
 *  plain row. Entries from the endpoint that are not a non-empty string are dropped. */
export function mergePickerTools<T extends PickerTool>(
    tools: readonly T[],
    coderOnly: readonly unknown[],
): (T | PickerTool)[] {
    const seen = new Set<string>();
    const merged: (T | PickerTool)[] = [];
    for (const tool of tools) {
        if (seen.has(tool.name)) continue;
        seen.add(tool.name);
        merged.push(tool);
    }
    for (const name of coderOnly) {
        if (typeof name !== 'string' || !name.trim() || seen.has(name)) continue;
        seen.add(name);
        merged.push({ name, description: 'Coder sub-agent tool' });
    }
    return merged;
}

/** Where the coding agent's own tool names stand. 'idle' before the first request. */
export type ToolUniverseState = 'idle' | 'loading' | 'ready' | 'failed';

/** The coding agent's own tool names from `GET /api/users/tool-universe`, fetched once while
 *  `enabled`, with the request's state kept apart from its result: an empty or missing list
 *  is not a loaded one, and a preset resolved before the list arrived leaves `bash` and the
 *  test runner out of the account. A failed request stays 'failed' until `retry`. */
export function useToolUniverse(enabled: boolean): { names: string[]; state: ToolUniverseState; retry: () => void } {
    const [names, setNames] = useState<string[]>([]);
    const [state, setState] = useState<ToolUniverseState>('idle');
    const [attempt, setAttempt] = useState(0);
    const loaded = state === 'ready';
    useEffect(() => {
        if (!enabled || loaded) return;
        let current = true;
        setState('loading');
        fetch('/api/users/tool-universe', { credentials: 'include' })
            .then(r => (r.ok ? r.json() : Promise.reject(r.status)))
            .then(d => {
                if (!current) return;
                if (!Array.isArray(d?.coder_only)) throw new Error('tool-universe: no coder_only list');
                setNames(d.coder_only);
                setState('ready');
            })
            .catch(() => { if (current) setState('failed'); });
        return () => { current = false; };
    }, [enabled, loaded, attempt]);
    const retry = useCallback(() => setAttempt(a => a + 1), []);
    return { names, state, retry };
}

/** A preset (anything but 'custom') resolves over the picker list, so it can be saved only
 *  once the coding agent's names are in that list. A custom selection saves what was ticked. */
export function presetSaveBlocked(preset: AccessPreset, universe: ToolUniverseState): boolean {
    return preset !== 'custom' && universe !== 'ready';
}

/** Keeps a form's tool and workflow selection on `preset`, resolved over the LATEST lists:
 *  whenever the preset, either list or `key` (the account being edited) changes, `apply`
 *  receives the resolution; 'custom' leaves the selection alone. The returned `reapply`
 *  asks for the same again on the next render. It never resolves itself: a caller holding
 *  it across an await (the create handler) would otherwise resolve over the lists of the
 *  render that started the request, and a list that arrived meanwhile would be lost. */
export function useAccessPreset(
    preset: AccessPreset,
    tools: { name: string; account_opt_in?: boolean }[],
    workflows: { id: string }[],
    apply: (resolved: { tools: string[]; workflows: string[] }) => void,
    key?: unknown,
): () => void {
    const applyRef = useRef(apply);
    applyRef.current = apply;
    const [epoch, setEpoch] = useState(0);
    useEffect(() => {
        const resolved = resolveAccessPreset(preset, tools, workflows);
        if (resolved) applyRef.current(resolved);
    }, [preset, tools, workflows, key, epoch]);
    return useCallback(() => setEpoch(e => e + 1), []);
}
