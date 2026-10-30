// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
'use client';

import { useEffect, useId, useState } from 'react';
import { useTranslations } from 'next-intl';
import { cn } from '@/lib/utils';

/** One row of POST /api/config/model-roles (`RoleAssignment.as_dict` in vaf/core/model_roles.py). */
export interface ModelRoleRow {
    role: string;
    provider: string | null;
    model: string | null;
    source: string;
    reason: string;
    setting: string;
    tab: string;
    also: string[];
    also_reasons: string[];
}

// Reasons that mean a job does not run the way the person expects. A row carrying one has
// its job name in the warning colour, its reason written out under the grid, and it stays
// visible when the card is collapsed on a phone. The other reasons explain a working row (a
// model that cannot read images gets its screenshots described) and are shown on hover only.
export const WARNING_REASONS = new Set([
    'no_api_key', 'no_local_models', 'choice_inactive', 'config_unreadable',
    'speech_provider_unusable', 'browser_dom_only',
]);

// Placeholder lines while the first answer is on its way: as many as the resolver has jobs
// today, so the settings below do not jump when the rows arrive.
const SKELETON_ROWS = 10;

// The phone shows only the rows that need attention until the person asks for all of them;
// the choice holds for the browser session.
const SHOW_ALL_KEY = 'vaf_model_roles_show_all';

/**
 * "Who does what" at the top of Settings > AI & Model: which provider and model serves each
 * job, one dense line per job. It renders the rows it is given, in their order, and decides
 * nothing: the provider, the source and the reason are the backend resolver's
 * (vaf/core/model_roles.py), the same function every lane asks, so the summary cannot
 * disagree with what runs. A row is a button that takes the person to the setting that
 * changes it.
 */
export default function ModelRolesOverview({ rows, loading, error, onRetry, providerLabel, onPick }: {
    rows: ModelRoleRow[] | null;
    loading: boolean;
    error: boolean;
    onRetry: () => void;
    /** The display name of `provider` on `row` (an engine row has no provider). */
    providerLabel: (provider: string | null, row: ModelRoleRow) => string;
    onPick: (row: ModelRoleRow) => void;
}) {
    const t = useTranslations('settings.ai');
    const headingId = useId();
    const [showAll, setShowAll] = useState(false);
    useEffect(() => {
        try { setShowAll(sessionStorage.getItem(SHOW_ALL_KEY) === '1'); } catch { /* private mode */ }
    }, []);
    const toggleShowAll = () => {
        const next = !showAll;
        setShowAll(next);
        try { sessionStorage.setItem(SHOW_ALL_KEY, next ? '1' : '0'); } catch { /* private mode */ }
    };

    const isWarning = (row: ModelRoleRow) =>
        row.source === 'unavailable' || [row.reason, ...row.also_reasons].some(r => WARNING_REASONS.has(r));
    const warningCount = rows ? rows.filter(isWarning).length : 0;

    return (
        <div className="bg-gray-50/50 px-5 py-3 rounded-xl border border-gray-100" data-testid="model-roles-overview">
            <h3 id={headingId} className="text-sm font-bold text-gray-900 uppercase tracking-wide">{t('rolesTitle')}</h3>
            <p className="text-xs text-gray-500 mb-2">{t('rolesIntro')}</p>
            {error && (
                <div className="flex items-center gap-3">
                    <p className="text-sm text-red-600">{t('rolesError')}</p>
                    <button type="button" onClick={onRetry}
                        className="shrink-0 whitespace-nowrap px-2 py-1 text-xs font-medium rounded-md border border-gray-200 text-gray-700 hover:bg-gray-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gray-400">
                        {t('rolesRetry')}
                    </button>
                </div>
            )}
            {loading && !rows && (
                <div className="grid grid-cols-2 gap-x-6 max-md:grid-cols-1" role="status">
                    <span className="sr-only">{t('rolesLoading')}</span>
                    {Array.from({ length: SKELETON_ROWS }).map((_, i) => (
                        <div key={i} aria-hidden="true" className={cn('flex h-6 items-center gap-3 px-2 max-md:h-8', i >= 3 && 'max-md:hidden')}
                            style={{ animation: 'skelPulse 1.6s ease-in-out infinite', animationDelay: `${i * 0.08}s` }}>
                            <div className="h-2.5 w-24 rounded bg-gray-200" />
                            <div className="h-2.5 flex-1 rounded bg-gray-100" />
                        </div>
                    ))}
                </div>
            )}
            {rows && (
                <>
                    <ul aria-labelledby={headingId} className="grid grid-cols-2 gap-x-6 max-md:grid-cols-1">
                        {rows.map(row => {
                            const unavailable = row.source === 'unavailable';
                            const warn = isWarning(row);
                            const name = (row.provider || row.source === 'local_engine') ? providerLabel(row.provider, row) : '';
                            const model = row.model && row.model.toLowerCase() !== name.toLowerCase() ? row.model : '';
                            const chain = row.also.map(p => providerLabel(p, row));
                            const reasons = [row.reason, ...row.also_reasons].filter(Boolean).map(r => t(`roleReason.${r}`));
                            return (
                                <li key={row.role} className={cn('min-w-0', !warn && !showAll && 'max-md:hidden')}>
                                    <button
                                        type="button"
                                        onClick={() => onPick(row)}
                                        title={reasons.join(' ') || undefined}
                                        className="w-full min-w-0 text-left rounded-md px-2 py-0.5 max-md:py-1.5 transition-colors hover:bg-gray-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gray-400"
                                    >
                                        <span className="flex min-w-0 items-center gap-2">
                                            <span className={cn('w-[46%] shrink-0 truncate text-[13px]', warn ? 'text-amber-700' : 'text-gray-800')} title={t(`role.${row.role}`)}>
                                                {t(`role.${row.role}`)}
                                            </span>
                                            {/* Only the model gives way: the provider is what the line is for, and
                                                the full model is in the title. */}
                                            <span className="flex min-w-0 flex-1 items-baseline gap-1 overflow-hidden text-xs text-gray-600"
                                                title={[name, model, ...chain].filter(Boolean).join(' · ') || undefined}>
                                                {name && <span className="max-w-full shrink-0 truncate">{name}</span>}
                                                {model && (
                                                    <>
                                                        <span className="shrink-0 text-gray-400" aria-hidden="true">·</span>
                                                        <span className="min-w-0 truncate font-mono text-[11px] text-gray-500">{model}</span>
                                                    </>
                                                )}
                                                {chain.length > 0 && (
                                                    <span className="shrink-0 text-gray-500">{chain.map(c => `→ ${c}`).join(' ')}</span>
                                                )}
                                            </span>
                                            {/* Following the primary provider is the default and needs no mark
                                                (the intro says so); it stays readable for screen readers. */}
                                            {row.source === 'follows_main' ? (
                                                <span className="sr-only">{t(`roleSource.${row.source}`)}</span>
                                            ) : <span className={cn(
                                                'shrink-0 whitespace-nowrap rounded px-1.5 py-0.5 text-[11px] font-medium leading-none border',
                                                unavailable ? 'bg-amber-50 border-amber-200 text-amber-700'
                                                    : row.source === 'set' ? 'bg-gray-200 border-gray-300 text-gray-800'
                                                    : row.source === 'main' ? 'bg-gray-100 border-gray-200 text-gray-700'
                                                    : 'bg-transparent border-gray-200 text-gray-500'
                                            )}>
                                                {t(`roleSource.${row.source}`)}
                                            </span>}
                                        </span>
                                        {reasons.length > 0 && <span className="sr-only">{reasons.join(' ')}</span>}
                                        <span className="sr-only">{t('rolesJumpHint')}</span>
                                    </button>
                                </li>
                            );
                        })}
                    </ul>
                    {/* What needs attention, written out under the grid at its full width: a
                        sentence squeezed into a half-width column took two or three lines per
                        row. Screen readers get each reason with its row already. */}
                    {warningCount > 0 && (
                        <ul aria-hidden="true" className="mt-1 space-y-0.5 px-2">
                            {rows.filter(isWarning).map(row => (
                                <li key={row.role} className="text-xs leading-snug text-amber-700">
                                    <span className="font-medium">{t(`role.${row.role}`)}:</span>{' '}
                                    {[row.reason, ...row.also_reasons].filter(Boolean).map(r => t(`roleReason.${r}`)).join(' ')}
                                </li>
                            ))}
                        </ul>
                    )}
                    {warningCount === 0 && !showAll && (
                        <p className="hidden px-2 py-1 text-xs text-gray-500 max-md:block">{t('rolesAllFine')}</p>
                    )}
                    <button type="button" onClick={toggleShowAll} aria-expanded={showAll}
                        className="mt-1 hidden rounded-md px-2 py-1 text-xs font-medium text-gray-600 hover:bg-gray-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gray-400 max-md:inline-block">
                        {showAll ? t('rolesShowLess') : t('rolesShowAll', { count: rows.length })}
                    </button>
                </>
            )}
        </div>
    );
}
