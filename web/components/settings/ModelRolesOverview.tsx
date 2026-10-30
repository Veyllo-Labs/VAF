// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
'use client';

import { useId, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';
import { useTranslations } from 'next-intl';
import { cn } from '@/lib/utils';
import Skeleton, { SkeletonBar, SkeletonRows } from '@/components/ui/Skeleton';

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

// Reasons that mean a job does not run the way the person expects. A row carrying one says its
// reason in place of the provider, in amber, the only colour on the card. The other reason
// (a model that cannot read images gets its screenshots described) explains a working row and
// stays in the row's hover title and screen-reader text.
export const WARNING_REASONS = new Set([
    'no_api_key', 'no_local_models', 'choice_inactive', 'config_unreadable',
    'speech_provider_unusable', 'browser_dom_only', 'thinking_model_ignored',
]);

// The areas the card is grouped by, in order, and the jobs in each. The only place the
// component names a job: everything a row shows comes from the row. A job the resolver adds
// and no area names joins the last area, so it is never dropped.
export const ROLE_GROUPS: [string, string[]][] = [
    ['work', ['chat', 'subagents', 'browser_agent', 'thinking']],
    ['images', ['vision', 'ocr']],
    ['speech', ['voice_call', 'speech_to_text', 'text_to_speech']],
    ['failover', ['failover']],
];

// A placeholder row on a phone stacks its bars the way a real row stacks job, model and note.
const ROW_MOBILE = 'max-md:flex-col max-md:items-start max-md:justify-center max-md:gap-2';

/**
 * "Who does what" at the top of Settings > AI & Model: which provider and model serves each
 * job, grouped by area. It renders the rows it is given and decides nothing: the provider, the
 * source and the reason are the backend resolver's (vaf/core/model_roles.py), the same function
 * every lane asks, so the summary cannot disagree with what runs. A row opens in place and
 * shows the choice `renderChoice` returns for it, the very elements of the section below (same
 * draft, saved by the normal Save), with a link to all of that job's options; a row with no
 * such choice jumps to its setting.
 */
export default function ModelRolesOverview({ rows, loading, error, onRetry, providerLabel, modelLabel, onPick, renderChoice }: {
    rows: ModelRoleRow[] | null;
    loading: boolean;
    error: boolean;
    onRetry: () => void;
    /** The display name of `provider` on `row` (an engine row has no provider). */
    providerLabel: (provider: string | null, row: ModelRoleRow) => string;
    /** The model as the row shows it (an engine row names its engine). */
    modelLabel: (row: ModelRoleRow) => string | null;
    /** Jump to the row's setting in the section below and light it up. */
    onPick: (row: ModelRoleRow) => void;
    /** The fields the open row offers, or null when the row only jumps. */
    renderChoice: (row: ModelRoleRow) => ReactNode;
}) {
    const t = useTranslations('settings.ai');
    const tCommon = useTranslations('common');
    const headingId = useId();
    const baseId = useId();
    const cardRef = useRef<HTMLDivElement>(null);
    // One row open at a time. The rows stay mounted when it changes, so the panel can move.
    const [open, setOpen] = useState<string | null>(null);

    const isWarning = (row: ModelRoleRow) =>
        row.source === 'unavailable' || [row.reason, ...row.also_reasons].some(r => WARNING_REASONS.has(r));

    const chat = rows?.find(r => r.source === 'main') ?? null;
    const mainName = chat?.provider ? providerLabel(chat.provider, chat) : '';

    // Each row in exactly one area, in the order of ROLE_GROUPS; unknown jobs join the last.
    const known = new Set(ROLE_GROUPS.flatMap(([, roles]) => roles));
    const groups = rows ? ROLE_GROUPS.map(([id, roles], i) => ({
        id,
        rows: [
            ...roles.flatMap(role => rows.filter(r => r.role === role)),
            ...(i === ROLE_GROUPS.length - 1 ? rows.filter(r => !known.has(r.role)) : []),
        ],
    })).filter(g => g.rows.length > 0) : [];

    const closeOpen = (e: KeyboardEvent) => {
        if (e.key !== 'Escape' || !open) return;
        // Escape closes the row, not the settings window around it.
        e.stopPropagation();
        const role = open;
        setOpen(null);
        cardRef.current?.querySelector<HTMLButtonElement>(`[data-role-row="${CSS.escape(role)}"]`)?.focus();
    };

    const value = (row: ModelRoleRow, reasons: string[]) => {
        if (isWarning(row)) {
            return <span className="min-w-0 text-amber-700">{reasons.join(' ')}</span>;
        }
        if (row.source === 'off') {
            return <span className="min-w-0 text-gray-400">{t('rolesOff')}</span>;
        }
        const name = (row.provider || row.source === 'local_engine') ? providerLabel(row.provider, row) : '';
        if (row.source === 'follows_main') {
            return <span className="min-w-0 truncate text-gray-500 max-md:whitespace-normal">{name}</span>;
        }
        const shown = modelLabel(row);
        const model = shown && shown.toLowerCase() !== name.toLowerCase() ? shown : '';
        return (
            <span className="min-w-0 truncate max-md:whitespace-normal" title={[name, model].filter(Boolean).join(' ')}>
                <span data-role-provider className="font-semibold text-gray-900">{name}</span>
                {model && <> <span className="font-mono text-xs text-gray-500">{model}</span></>}
            </span>
        );
    };

    const note = (row: ModelRoleRow) => {
        if (isWarning(row)) return '';
        if (row.source === 'main') return t('rolesNoteMain');
        if (row.source === 'via_vision') return t('rolesNoteViaVision');
        if (row.also.length > 0) return t('rolesNoteThen', { chain: row.also.map(p => providerLabel(p, row)).join(', ') });
        return '';
    };

    return (
        <div ref={cardRef} onKeyDown={closeOpen} className="bg-gray-50/50 px-5 py-4 rounded-xl border border-gray-100" data-testid="model-roles-overview">
            <h3 id={headingId} className="text-sm font-bold text-gray-900 uppercase tracking-wide">{t('rolesTitle')}</h3>
            {!(loading && !rows) && (
                <p className="mt-0.5 text-xs text-gray-500">
                    {mainName
                        ? t.rich('rolesIntro', { provider: mainName, b: (c) => <b className="font-semibold text-gray-900">{c}</b> })
                        : t('rolesIntroNoMain')}
                </p>
            )}
            {error && (
                <div className="mt-2 flex items-center gap-3">
                    <p className="text-sm text-red-600">{t('rolesError')}</p>
                    <button type="button" onClick={onRetry}
                        className="shrink-0 whitespace-nowrap px-2 py-1 text-xs font-medium rounded-md border border-gray-200 text-gray-700 hover:bg-gray-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gray-400">
                        {t('rolesRetry')}
                    </button>
                </div>
            )}
            {loading && !rows && (
                // While the first answer is on its way: the intro naming the primary provider (one
                // line, three on a phone), the real area headings and one line per job in each
                // (ROLE_GROUPS names every job the resolver has), at the row's height, so the
                // settings below do not jump when the rows arrive. The first job is the primary
                // provider's, whose row always carries a note: a third bar, a third line on a phone.
                <Skeleton label={t('rolesLoading')}>
                    <div className="mt-0.5 h-4 flex flex-col justify-center gap-[5.5px] max-md:h-12">
                        <SkeletonBar className="h-2.5 w-4/5 max-md:w-full" />
                        <SkeletonBar className="h-2.5 w-full hidden max-md:block" />
                        <SkeletonBar className="h-2.5 w-1/2 hidden max-md:block" />
                    </div>
                    {ROLE_GROUPS.map(([id, roles], g) => (
                        <section key={id}>
                            <div className="mt-3 mb-0.5 text-[11px] font-semibold uppercase tracking-wide text-gray-500">
                                {t(`rolesGroup.${id}`)}
                            </div>
                            {g === 0 && (
                                <SkeletonRows count={1} widths={[['7rem', '10rem', '6rem']]} rowClassName={`h-8 max-md:h-[71px] ${ROW_MOBILE}`} />
                            )}
                            <SkeletonRows count={g === 0 ? roles.length - 1 : roles.length}
                                widths={[['7rem', '8rem'], ['7rem', '12rem'], ['7rem', '10rem']]}
                                rowClassName={`h-8 max-md:h-[52px] ${ROW_MOBILE}`} />
                        </section>
                    ))}
                </Skeleton>
            )}
            {rows && groups.map(group => (
                <section key={group.id}>
                    <h4 id={`${baseId}-${group.id}`} className="mt-3 mb-0.5 text-[11px] font-semibold uppercase tracking-wide text-gray-500">
                        {t(`rolesGroup.${group.id}`)}
                    </h4>
                    <ul aria-labelledby={`${baseId}-${group.id}`}>
                        {group.rows.map(row => {
                            const warn = isWarning(row);
                            const reasons = [row.reason, ...row.also_reasons].filter(Boolean)
                                .map(r => t(`roleReason.${r}`, { provider: mainName }));
                            const choice = renderChoice(row);
                            const expandable = choice !== null && choice !== undefined && choice !== false;
                            const isOpen = expandable && open === row.role;
                            const panelId = `${baseId}-${row.role}-panel`;
                            const noteText = note(row);
                            return (
                                <li key={row.role}>
                                    <button
                                        type="button"
                                        data-role-row={row.role}
                                        aria-expanded={expandable ? isOpen : undefined}
                                        aria-controls={expandable ? panelId : undefined}
                                        onClick={() => (expandable ? setOpen(isOpen ? null : row.role) : onPick(row))}
                                        title={!warn && reasons.length > 0 ? reasons.join(' ') : undefined}
                                        className="vaf-role-row -mx-2 grid w-[calc(100%+1rem)] grid-cols-[9.5rem_minmax(0,1fr)_auto_0.75rem] items-baseline gap-x-3 rounded-lg px-2 py-1.5 text-left text-[13px] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gray-400 max-md:grid-cols-[minmax(0,1fr)_0.75rem] max-md:gap-y-0.5"
                                    >
                                        <span data-role-label className={cn('min-w-0 max-md:col-start-1 max-md:row-start-1 max-md:font-medium', warn ? 'text-amber-700' : 'text-gray-700')}>
                                            {t(`role.${row.role}`)}
                                        </span>
                                        <span className="flex min-w-0 max-md:col-start-1 max-md:row-start-2">{value(row, reasons)}</span>
                                        <span className={cn('whitespace-nowrap text-xs text-gray-400 max-md:col-start-1 max-md:row-start-3', !noteText && 'max-md:hidden')}>{noteText}</span>
                                        <span aria-hidden="true" className="vaf-role-caret h-3 w-3 self-center text-gray-400 max-md:col-start-2 max-md:row-start-1 max-md:row-span-2">
                                            <svg viewBox="0 0 12 12" className="h-3 w-3" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M7.5 2.5 4 6l3.5 3.5" /></svg>
                                        </span>
                                        {!warn && reasons.length > 0 && <span className="sr-only">{reasons.join(' ')}</span>}
                                        {!expandable && <span className="sr-only">{t('rolesJumpHint')}</span>}
                                    </button>
                                    {expandable && (
                                        // `inert` is a boolean to React 18.3 (an empty string reads as false) and is
                                        // not in its types yet, hence the spread.
                                        <div id={panelId} role="region" aria-label={t(`role.${row.role}`)}
                                            className="vaf-role-panel" data-open={isOpen ? 'true' : 'false'}
                                            {...({ inert: isOpen ? undefined : true } as Record<string, boolean | undefined>)}>
                                            <div>
                                                <div className="mt-0.5 mb-2 rounded-lg border border-gray-200 bg-white p-3">
                                                    <div className="grid grid-cols-2 gap-3 max-md:grid-cols-1">{choice}</div>
                                                    <div className="mt-3 flex items-center justify-between gap-3">
                                                        <span className="text-xs text-gray-400">{t('rolesSaveHint', { save: tCommon('saveChanges') })}</span>
                                                        <button type="button" onClick={() => onPick(row)}
                                                            className="shrink-0 rounded text-xs text-gray-900 underline underline-offset-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gray-400">
                                                            {t('rolesAllOptions')}
                                                        </button>
                                                    </div>
                                                </div>
                                            </div>
                                        </div>
                                    )}
                                </li>
                            );
                        })}
                    </ul>
                </section>
            ))}
        </div>
    );
}
