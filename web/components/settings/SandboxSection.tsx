// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import { Box, Square, Trash2 } from 'lucide-react';

interface Environment {
    id: string;
    name: string;
    kind: 'temporary' | 'project' | 'scratch';
    network: 'none' | 'registries' | 'open';
    state: string;
    created: number;
    expires: number;
    project_path: string;
    degraded: string;
    memory_mb: number;
    owner?: string;
}

interface Process {
    handle: string;
    env: string;
    command: string;
    state: string;
}

interface Overview {
    available: boolean;
    reason?: string;
    environments: Environment[];
    processes: Process[];
    is_admin: boolean;
    /** Whether this list is everybody's: the actions on its rows ask with the same scope. */
    all: boolean;
}

/**
 * The caller's sandbox environments (vaf/core/environments.py, /api/sandbox): containers the
 * agent runs, installs and tests code in. Listed with what they are and when they go away;
 * stopping keeps the files, deleting removes container, files and network - so it asks twice
 * on the same button. An admin may switch to everybody's environments, to stop or delete one;
 * nothing here runs code anywhere.
 */
export default function SandboxSection() {
    const t = useTranslations('sandboxEnv');
    const apiBase = typeof window !== 'undefined' ? (document.location.origin || '') : '';
    const [data, setData] = useState<Overview | null>(null);
    const [everyone, setEveryone] = useState(false);
    const [confirming, setConfirming] = useState<string | null>(null);
    const [busy, setBusy] = useState<string | null>(null);
    const [note, setNote] = useState<string | null>(null);
    const [loadFailed, setLoadFailed] = useState(false);
    // Only the latest request may write: a slow answer for the other "All accounts"
    // setting would otherwise show one list while the checkbox says the other.
    const latest = useRef(0);

    const load = useCallback(async () => {
        const seq = ++latest.current;
        try {
            const res = await fetch(`${apiBase}/api/sandbox${everyone ? '?all=1' : ''}`, { credentials: 'include' });
            const body = res.ok ? await res.json() : null;
            if (seq !== latest.current) return;
            if (!body) { setLoadFailed(true); return; }
            setData({ ...body, all: everyone });
            setLoadFailed(false);
        } catch {
            if (seq === latest.current) setLoadFailed(true);
        }
    }, [apiBase, everyone]);

    useEffect(() => { void load(); }, [load]);

    const act = async (env: Environment, action: 'stop' | 'delete') => {
        if (!data || data.all !== everyone) return;      // a row of the other setting's list
        if (action === 'delete' && confirming !== env.id) { setConfirming(env.id); return; }
        setConfirming(null);
        setBusy(env.id);
        setNote(null);
        try {
            const url = `${apiBase}/api/sandbox/${encodeURIComponent(env.id)}${action === 'stop' ? '/stop' : ''}${data?.all ? '?all=1' : ''}`;
            const res = await fetch(url, { method: action === 'stop' ? 'POST' : 'DELETE', credentials: 'include' });
            if (!res.ok) {
                const body = await res.json().catch(() => ({}));
                setNote(String(body.detail || t('failed')));
            }
        } catch {
            setNote(t('failed'));
        } finally {
            setBusy(null);
        }
        // Either way the list shows what is really there.
        void load();
    };

    if (!data && !loadFailed) return null;
    // The list on screen belongs to the other "All accounts" setting until the new one has
    // arrived (or failed to): shown dimmed, and nothing on it can be acted on.
    const stale = !!data && data.all !== everyone;
    const kindLabel = (k: Environment['kind']) =>
        k === 'project' ? t('kindProject') : k === 'scratch' ? t('kindScratch') : t('kindTemporary');
    const networkLabel = (n: Environment['network']) =>
        n === 'open' ? t('networkOpen') : n === 'registries' ? t('networkRegistries') : t('networkNone');
    const hoursLeft = (env: Environment) =>
        env.expires ? Math.max(0, Math.round((env.expires * 1000 - Date.now()) / 3600000)) : null;

    return (
        <div className="bg-gray-50/50 p-6 rounded-xl border border-gray-100 mt-6">
            <div className="flex items-start justify-between gap-3 mb-2">
                <h3 className="text-sm font-bold text-gray-900 uppercase tracking-wide">{t('title')}</h3>
                {data?.is_admin && (
                    <label className="flex items-center gap-2 text-xs text-gray-600 cursor-pointer select-none">
                        <input type="checkbox" checked={everyone} onChange={e => setEveryone(e.target.checked)} />
                        {t('showAll')}
                    </label>
                )}
            </div>
            <p className="text-xs text-gray-600 mb-4">{t('intro')}</p>
            {loadFailed && (
                <div className="flex items-center gap-3 mb-2">
                    <p className="text-sm text-red-600">{t('loadFailed')}</p>
                    <button type="button" onClick={() => void load()}
                        className="px-2 py-1 text-xs font-medium rounded-md border border-gray-200 text-gray-700 hover:bg-gray-100">
                        {t('retry')}
                    </button>
                </div>
            )}
            {!data ? null : !data.available ? (
                <p className="text-sm text-gray-500">
                    {data.reason === 'docker_unavailable' ? t('dockerUnavailable') : t('unavailable', { reason: data.reason || '' })}
                </p>
            ) : data.environments.length === 0 ? (
                <p className="text-sm text-gray-500">{t('none')}</p>
            ) : (
                <ul className={`flex flex-col gap-2 ${stale ? 'opacity-50' : ''}`} aria-busy={stale}>
                    {data.environments.map(env => {
                        const running = env.state === 'running';
                        const left = hoursLeft(env);
                        const procs = data.processes.filter(p => p.env === env.id && p.state === 'running');
                        return (
                            <li key={env.id} className="px-3 py-2 rounded-lg border border-gray-200 bg-white">
                                <div className="flex items-center gap-3">
                                    <Box className="w-4 h-4 text-gray-500 shrink-0" />
                                    <span className="text-sm font-mono text-gray-800 shrink-0">{env.id}</span>
                                    <span className="text-sm text-gray-700 flex-1 min-w-0 truncate">{env.name}</span>
                                    <span className={`text-xs shrink-0 whitespace-nowrap ${running ? 'text-emerald-600' : 'text-gray-500'}`}>
                                        {running ? t('running') : t('stopped')}
                                    </span>
                                    {running && (
                                        <button type="button" disabled={stale || busy === env.id} onClick={() => void act(env, 'stop')}
                                            title={t('stop')} aria-label={t('stop')}
                                            className="p-1.5 rounded-md text-gray-500 hover:text-gray-900 hover:bg-gray-100 disabled:opacity-50">
                                            <Square className="w-4 h-4" />
                                        </button>
                                    )}
                                    {confirming === env.id ? (
                                        <button type="button" disabled={stale || busy === env.id} onClick={() => void act(env, 'delete')}
                                            className="px-2 py-1 text-xs font-medium rounded-md bg-red-600 hover:bg-red-700 text-white disabled:opacity-50 shrink-0 whitespace-nowrap">
                                            {t('confirmDelete')}
                                        </button>
                                    ) : (
                                        <button type="button" disabled={stale || busy === env.id} onClick={() => void act(env, 'delete')}
                                            title={t('delete')} aria-label={t('delete')}
                                            className="p-1.5 rounded-md text-gray-500 hover:text-red-600 hover:bg-gray-100 disabled:opacity-50">
                                            <Trash2 className="w-4 h-4" />
                                        </button>
                                    )}
                                </div>
                                <div className="mt-1 pl-7 text-xs text-gray-500 flex flex-wrap items-center gap-x-3 gap-y-1">
                                    <span className="text-[11px] px-1.5 py-0.5 rounded border border-gray-200 text-gray-600 whitespace-nowrap">{kindLabel(env.kind)}</span>
                                    <span className="text-[11px] px-1.5 py-0.5 rounded border border-gray-200 text-gray-600 whitespace-nowrap">{networkLabel(env.network)}</span>
                                    {env.project_path && <span className="font-mono min-w-0 truncate" title={env.project_path}>{env.project_path}</span>}
                                    {left !== null && <span>{t('expiresIn', { hours: left })}</span>}
                                    {procs.length > 0 && <span>{t('processes', { count: procs.length })}</span>}
                                    {data.all && env.owner && <span className="font-mono">{t('owner', { owner: env.owner })}</span>}
                                </div>
                                {env.degraded && <p className="mt-1 pl-7 text-xs text-amber-700">{env.degraded}</p>}
                            </li>
                        );
                    })}
                </ul>
            )}
            {note && <p className="text-xs mt-2 text-red-600">{note}</p>}
        </div>
    );
}
