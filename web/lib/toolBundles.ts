// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

/**
 * Which bundle a tool belongs to in the tools window, and in which order the
 * bundles appear.
 *
 * The KEYS are not invented here. They come from TOOL_CATEGORIES in
 * vaf/core/tool_contract.py, every tool declares one on its class, and the
 * backend sends the resolved value on each tool entry. This file only decides
 * presentation: order, colour, icon. tests/test_tool_category_registry_sync.py
 * fails if this list and the Python vocabulary drift apart.
 *
 * The colours are the ones the product already uses for the same integrations
 * on the Connections cards, so GitHub is the same black in both places.
 */

/** Display order. A key the backend sends that is NOT listed here (a
 *  third-party tool or an MCP server naming its own bundle) is rendered after
 *  these, before "general". */
export const TOOL_BUNDLE_ORDER = [
    'custom', 'github', 'mail', 'whatsapp', 'telegram', 'discord', 'slack',
    'messaging', 'rooms', 'calendar', 'contacts', 'cloud', 'automations',
    'timers', 'workflows', 'skills', 'memory', 'context', 'code', 'git',
    'files', 'documents', 'web', 'tool_catalog', 'mcp', 'general',
] as const;

/** Accent colour per bundle. Used for the shelf frame, the notch and the
 *  active card border. GitHub's Connections tile is white, which would be an
 *  invisible frame, so the bundle takes the mark's ink instead. */
export const TOOL_BUNDLE_COLOR: Record<string, string> = {
    custom: '#9333ea', github: '#111827', mail: '#ef4444', whatsapp: '#16a34a',
    telegram: '#0ea5e9', discord: '#4f46e5', slack: '#9333ea', messaging: '#0891b2',
    rooms: '#059669', calendar: '#3b82f6', contacts: '#4b5563', cloud: '#eab308',
    automations: '#f59e0b', timers: '#f97316', workflows: '#0d9488', skills: '#db2777',
    memory: '#7c3aed', context: '#8b5cf6', code: '#1f2937', git: '#ea580c',
    files: '#d97706', documents: '#2563eb', web: '#0284c7', tool_catalog: '#6b7280',
    mcp: '#475569', general: '#6b7280',
};

/** Icon key per bundle; the modal maps these onto the lucide components it
 *  already imports. Anything unmapped falls back to the generic tool icon. */
export const TOOL_BUNDLE_ICON: Record<string, string> = {
    custom: 'sparkles', github: 'github', mail: 'mail', whatsapp: 'phone',
    telegram: 'chat', discord: 'chat', slack: 'chat', messaging: 'send',
    rooms: 'network', calendar: 'calendar', contacts: 'users', cloud: 'cloud',
    automations: 'zap', timers: 'clock', workflows: 'workflow', skills: 'sparkles',
    memory: 'database', context: 'brain', code: 'terminal', git: 'branch',
    files: 'folder', documents: 'file', web: 'globe', tool_catalog: 'list',
    mcp: 'plug', general: 'cpu',
};

/** Mirrors CUSTOM_CATEGORY_PREFIX in vaf/core/tool_contract.py. */
export const CUSTOM_PREFIX = 'custom';
export const isCustomBundle = (key: string) =>
    key === CUSTOM_PREFIX || key.startsWith(`${CUSTOM_PREFIX}_`);

/** The bundle a custom one mirrors: "custom_github" -> "github". */
export const mirroredBundle = (key: string) =>
    key.startsWith(`${CUSTOM_PREFIX}_`) ? key.slice(CUSTOM_PREFIX.length + 1) : '';

/** Accent colour. Everything a user uploaded keeps the custom purple, whichever
 *  bundle it mirrors - the colour is the "this one is yours" signal, and a
 *  custom GitHub bundle in GitHub's black would defeat the separation the
 *  namespace exists for. */
export const bundleColor = (key: string) => {
    if (isCustomBundle(key)) return TOOL_BUNDLE_COLOR.custom;
    return TOOL_BUNDLE_COLOR[key] || TOOL_BUNDLE_COLOR.general;
};

/** Icon key. A custom bundle borrows the icon of the bundle it mirrors, so it
 *  is recognisable at a glance while the colour keeps them apart. */
export const bundleIconKey = (key: string) => {
    if (key === CUSTOM_PREFIX) return TOOL_BUNDLE_ICON.custom;
    const mirrored = mirroredBundle(key);
    if (mirrored) return TOOL_BUNDLE_ICON[mirrored] || TOOL_BUNDLE_ICON.custom;
    return TOOL_BUNDLE_ICON[key] || TOOL_BUNDLE_ICON.general;
};

export interface BundledTool {
    name: string;
    description: string;
    category?: string;
    is_custom?: boolean;
}

export interface ToolBundle<T extends BundledTool> {
    key: string;
    tools: T[];
}

/**
 * Group tools into bundles, in display order.
 *
 * The bundle of a user-uploaded tool arrives already namespaced - the loader in
 * vaf/core/custom_tools_registry.py stamps "github" as "custom_github" - so
 * this file does NOT special-case is_custom. Doing it here would mean the CLI,
 * the TUI and list_tools each had to repeat the rule, which is how the same
 * tool came to sit in two different bundles depending on which surface you
 * looked at.
 */
export function groupToolsIntoBundles<T extends BundledTool>(tools: T[]): ToolBundle<T>[] {
    const byKey = new Map<string, T[]>();
    for (const tool of tools) {
        const key = tool.category || (tool.is_custom ? CUSTOM_PREFIX : 'general');
        const list = byKey.get(key);
        if (list) list.push(tool); else byKey.set(key, [tool]);
    }
    const present = [...byKey.keys()];
    const rank = (key: string) => {
        // A custom bundle sorts directly BEHIND the bundle it mirrors, so
        // "GitHub" and "Custom GitHub" stand next to each other and the
        // difference is impossible to miss.
        const mirrored = mirroredBundle(key);
        const base = mirrored || key;
        const index = (TOOL_BUNDLE_ORDER as readonly string[]).indexOf(base);
        // Unknown bundles (a third-party tool, an MCP server) go after the
        // known ones but before the "general" catch-all.
        const primary = index >= 0 ? index : TOOL_BUNDLE_ORDER.length;
        return [primary, mirrored || key === CUSTOM_PREFIX ? 1 : 0, key] as const;
    };
    const order = present.sort((a, b) => {
        const ra = rank(a), rb = rank(b);
        return ra[0] - rb[0] || ra[1] - rb[1] || ra[2].localeCompare(rb[2]);
    });
    // The catch-all stays last whatever else happens.
    const general = order.filter(k => k === 'general');
    const rest = order.filter(k => k !== 'general');
    return [...rest, ...general].map(key => ({ key, tools: byKey.get(key)! }));
}

/** Bundle label, translated when known; an unknown key is title-cased so an
 *  MCP server still gets a readable heading instead of a raw slug. A custom
 *  bundle is named after the bundle it mirrors. */
export function bundleLabel(
    key: string,
    t: (k: string, values?: Record<string, string>) => string,
): string {
    const known = (TOOL_BUNDLE_ORDER as readonly string[]).includes(key);
    if (known) return t(`tools.groups.${key}`);
    const mirrored = mirroredBundle(key);
    if (mirrored) return t('tools.customBundle', { name: bundleLabel(mirrored, t) });
    return key.replace(/[_-]+/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}
