// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
'use client';

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

// Reasons that mean the job does not run the way the person expects, so they are
// written in the warning colour even on a row that still has a provider. The others
// explain a working row (a model that cannot see gets its screenshots described).
const WARNING_REASONS = new Set(['no_api_key', 'no_local_models', 'choice_inactive', 'config_unreadable', 'speech_provider_unusable']);

/**
 * "Who does what" at the top of Settings > AI & Model: which provider and model serves each
 * job. It renders the rows it is given, in their order, and decides nothing: the provider,
 * the source and the reason are the backend resolver's (vaf/core/model_roles.py), the same
 * function every lane asks, so the summary cannot disagree with what runs. A row is a
 * button that takes the person to the setting that changes it.
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

    return (
        <div className="bg-gray-50/50 p-6 rounded-xl border border-gray-100 max-md:p-4" data-testid="model-roles-overview">
            <h3 className="text-sm font-bold text-gray-900 uppercase tracking-wide mb-1">{t('rolesTitle')}</h3>
            <p className="text-xs text-gray-500 mb-3">{t('rolesIntro')}</p>
            {error && (
                <div className="flex items-center gap-3 mb-2">
                    <p className="text-sm text-red-600">{t('rolesError')}</p>
                    <button type="button" onClick={onRetry}
                        className="shrink-0 whitespace-nowrap px-2 py-1 text-xs font-medium rounded-md border border-gray-200 text-gray-700 hover:bg-gray-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gray-400">
                        {t('rolesRetry')}
                    </button>
                </div>
            )}
            {loading && !rows && (
                <div className="grid grid-cols-2 gap-x-6 gap-y-1 max-md:grid-cols-1" aria-busy="true" aria-label={t('rolesLoading')}>
                    {Array.from({ length: 6 }).map((_, i) => (
                        <div key={i} className="px-3 py-2"
                            style={{ animation: 'skelPulse 1.6s ease-in-out infinite', animationDelay: `${i * 0.12}s` }}>
                            <div className="h-3 w-24 rounded bg-gray-200" />
                            <div className="mt-2 h-3 w-40 rounded bg-gray-100" />
                        </div>
                    ))}
                </div>
            )}
            {rows && (
                <ul className="grid grid-cols-2 gap-x-6 gap-y-1 max-md:grid-cols-1">
                    {rows.map(row => {
                        const unavailable = row.source === 'unavailable';
                        const name = (row.provider || row.source === 'local_engine') ? providerLabel(row.provider, row) : '';
                        const model = row.model && row.model.toLowerCase() !== name.toLowerCase() ? row.model : '';
                        const chain = row.also.map(p => providerLabel(p, row));
                        const reasons = [row.reason, ...row.also_reasons].filter(Boolean);
                        const warn = unavailable || reasons.some(r => WARNING_REASONS.has(r));
                        return (
                            <li key={row.role} className="min-w-0">
                                <button
                                    type="button"
                                    onClick={() => onPick(row)}
                                    className="group w-full min-w-0 text-left rounded-lg px-3 py-2 transition-colors hover:bg-gray-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gray-400"
                                >
                                    <span className="flex items-start justify-between gap-2">
                                        <span className="min-w-0 text-sm font-medium text-gray-800">{t(`role.${row.role}`)}</span>
                                        <span className={cn(
                                            'mt-0.5 shrink-0 rounded px-1.5 py-0.5 text-[11px] font-medium leading-none border',
                                            unavailable ? 'bg-amber-50 border-amber-200 text-amber-700'
                                                : row.source === 'set' ? 'bg-gray-900 border-gray-900 text-white dark:bg-[#3a3a3a] dark:border-[#3a3a3a] dark:text-gray-100'
                                                : row.source === 'main' ? 'bg-gray-100 border-gray-200 text-gray-700'
                                                : 'bg-transparent border-gray-200 text-gray-500'
                                        )}>
                                            {t(`roleSource.${row.source}`)}
                                        </span>
                                    </span>
                                    {name && (
                                        <span className="mt-0.5 flex min-w-0 items-baseline gap-1 text-xs text-gray-600">
                                            <span className="shrink-0">{name}</span>
                                            {model && (
                                                <>
                                                    <span className="shrink-0 text-gray-400" aria-hidden="true">·</span>
                                                    <span className="truncate font-mono text-[11px] text-gray-500" title={model}>{model}</span>
                                                </>
                                            )}
                                            {chain.length > 0 && (
                                                <span className="shrink-0 text-gray-500">{chain.map(c => `→ ${c}`).join(' ')}</span>
                                            )}
                                        </span>
                                    )}
                                    {reasons.map(r => (
                                        <span key={r} className={cn('mt-0.5 block text-xs', warn ? 'text-amber-700' : 'text-gray-500')}>
                                            {t(`roleReason.${r}`)}
                                        </span>
                                    ))}
                                    <span className="sr-only">{t('rolesJumpHint')}</span>
                                </button>
                            </li>
                        );
                    })}
                </ul>
            )}
        </div>
    );
}
