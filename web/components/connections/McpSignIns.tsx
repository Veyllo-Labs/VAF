'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

/**
 * McpSignIns
 * ==========
 * The MCP servers an admin set up with a sign-in per account (`auth: "oauth"`,
 * vaf/core/mcp_oauth.py), as the viewing account sees them: signed in or not, and the button that
 * signs this account in or out. Every account has its own row state; nobody sees another's.
 *
 * Signing in opens the service's page in a new tab (the system browser in the desktop app, the
 * same as the mail sign-in: services refuse embedded webviews). The tab is taken during the click
 * (lib/authTab.ts), because the start can outlast the moment a browser still allows one; if the
 * browser blocked it anyway, or the person closed it, the row offers the page as a link while the
 * sign-in waits. The service sends that tab to
 * VAF's callback, which lands on Settings > Connections there. This tab learns the outcome the
 * way the mail accounts do: it asks again every few seconds while a sign-in waits, and when the
 * window gets the focus back.
 *
 * Renders nothing while no server signs accounts in, or none matches the panel's search.
 */

import React, { useCallback, useEffect, useState } from 'react';
import { useTranslations } from 'next-intl';
import { KeyRound, Loader2 } from 'lucide-react';
import { cn, getApiBase } from '@/lib/utils';
import { reserveAuthTab } from '@/lib/authTab';

interface Row {
    name: string;
    url: string;
    enabled: boolean;
    signed_in: boolean;
    pending: boolean;
    error: string | null;
}

const api = (p: string) => `${getApiBase()}${p}`;

function host(url: string): string {
    try { return new URL(url).host; } catch { return url; }
}

export default function McpSignIns({ query = '', onShownCount }: { query?: string; onShownCount?: (count: number) => void }) {
    const t = useTranslations('settings.connectionsPanel');
    const [rows, setRows] = useState<Row[]>([]);
    const [busy, setBusy] = useState<string | null>(null);
    /** A failure this tab saw itself (start or sign-out); a failed sign-in comes from the server's row. */
    const [problem, setProblem] = useState<{ name: string; text: string } | null>(null);
    /** The authorization address of a sign-in this tab started, per server, while it waits. */
    const [pages, setPages] = useState<Record<string, string>>({});
    const tCommon = useTranslations('common');

    const load = useCallback(async () => {
        try {
            const r = await fetch(api('/api/mcp/sign-in'), { credentials: 'include' });
            if (!r.ok) return;
            const d = await r.json();
            setRows(Array.isArray(d?.servers) ? d.servers : []);
        } catch { /* the list stays as it was */ }
    }, []);

    useEffect(() => { load(); }, [load]);

    const waiting = rows.some((row) => row.pending);
    useEffect(() => {
        if (!waiting) return;
        const timer = setInterval(load, 4000);
        const onFocus = () => load();
        window.addEventListener('focus', onFocus);
        return () => { clearInterval(timer); window.removeEventListener('focus', onFocus); };
    }, [waiting, load]);

    const q = query.trim().toLowerCase();
    const title = [t('mcpTitle'), t('mcpDescription'), 'mcp'].join('\n').toLowerCase();
    const shown = !q || title.includes(q) ? rows : rows.filter((row) => `${row.name} ${row.url}`.toLowerCase().includes(q));
    // The panel's "nothing matches" line has to know these rows exist.
    useEffect(() => { onShownCount?.(shown.length); }, [shown.length, onShownCount]);

    const signIn = async (name: string) => {
        // Before the first await: the click still counts for opening a tab.
        const tab = reserveAuthTab();
        setBusy(name);
        setProblem(null);
        // A link from an earlier attempt must not outlive it (a failed start).
        setPages((prev) => { const next = { ...prev }; delete next[name]; return next; });
        try {
            const r = await fetch(api(`/api/mcp/sign-in/${encodeURIComponent(name)}`), { method: 'POST', credentials: 'include' });
            const d = await r.json().catch(() => ({}));
            if (!r.ok || !d?.authorization_url) {
                tab.cancel();
                setProblem({ name, text: t('mcpStartFailed', { reason: String(d?.detail || r.status) }) });
            } else {
                const url = String(d.authorization_url);
                setPages((prev) => ({ ...prev, [name]: url }));
                tab.open(url);
            }
        } catch (e) {
            tab.cancel();
            setProblem({ name, text: t('mcpStartFailed', { reason: String(e instanceof Error ? e.message : e) }) });
        } finally {
            setBusy(null);
            load();
        }
    };

    const signOut = async (name: string) => {
        setBusy(name);
        setProblem(null);
        try {
            const r = await fetch(api(`/api/mcp/sign-in/${encodeURIComponent(name)}`), { method: 'DELETE', credentials: 'include' });
            if (!r.ok) setProblem({ name, text: t('mcpSignOutFailed') });
        } catch {
            setProblem({ name, text: t('mcpSignOutFailed') });
        } finally {
            setBusy(null);
            load();
        }
    };

    if (shown.length === 0) return null;

    return (
        <div className="space-y-3">
            <div>
                <h4 className="text-sm font-medium text-gray-700">{t('mcpTitle')}</h4>
                <p className="text-xs text-gray-400">{t('mcpDescription')}</p>
            </div>
            <div className="space-y-2">
                {shown.map((row) => {
                    const failure = problem?.name === row.name ? problem.text : (row.error ? t('mcpFailed', { reason: row.error }) : null);
                    // Offered while the sign-in this tab started still waits: a blocked or closed tab is not a dead end.
                    const page = row.pending && !row.signed_in ? pages[row.name] : undefined;
                    return (
                        <div
                            key={row.name}
                            className={cn(
                                'p-4 rounded-xl border transition-all',
                                row.signed_in ? 'bg-white border-gray-200 shadow-sm' : 'bg-gray-50 border-gray-200',
                            )}
                        >
                            <div className="flex items-center justify-between gap-3">
                                <div className="flex items-center gap-3 min-w-0 flex-1">
                                    <div className={cn(
                                        'w-10 h-10 rounded-xl flex items-center justify-center shrink-0',
                                        row.signed_in ? 'bg-amber-600 text-white' : 'bg-gray-300 text-gray-500',
                                    )}>
                                        <KeyRound className="w-5 h-5" />
                                    </div>
                                    <div className="min-w-0">
                                        <div className="flex items-center gap-2">
                                            <span className="font-medium text-gray-900 truncate">{row.name}</span>
                                            <span className={cn(
                                                'text-xs px-2 py-0.5 rounded-full shrink-0',
                                                !row.enabled ? 'bg-gray-100 text-gray-500'
                                                    : row.signed_in ? 'bg-green-100 text-green-700'
                                                        : row.pending ? 'bg-yellow-100 text-yellow-700'
                                                            : 'bg-gray-100 text-gray-500',
                                            )}>
                                                {!row.enabled ? t('mcpTurnedOff') : row.signed_in ? t('mcpSignedIn') : row.pending ? t('mcpWaiting') : t('mcpSignedOut')}
                                            </span>
                                        </div>
                                        <p className="text-sm text-gray-500 truncate">{host(row.url)}</p>
                                        <p className="text-xs text-gray-600 mt-1">{row.signed_in ? t('mcpUsesYourAccount') : t('mcpSignInFirst')}</p>
                                        {failure && <p className="text-xs text-red-600 mt-1 break-words">{failure}</p>}
                                        {page && (
                                            <a href={page} target="_blank" rel="noopener noreferrer" className="inline-block text-xs text-blue-700 hover:underline mt-1">
                                                {tCommon('openSignInPage')}
                                            </a>
                                        )}
                                    </div>
                                </div>
                                {row.enabled && (
                                    <button
                                        type="button"
                                        onClick={() => (row.signed_in ? signOut(row.name) : signIn(row.name))}
                                        disabled={busy === row.name}
                                        className={cn(
                                            'shrink-0 flex items-center gap-2 px-3 h-9 rounded-lg text-sm font-medium transition-colors disabled:opacity-50',
                                            row.signed_in
                                                ? 'text-gray-600 hover:bg-gray-100 border border-gray-200'
                                                : 'bg-gray-900 text-white hover:bg-gray-800 dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-[#f5f5f5]',
                                        )}
                                    >
                                        {busy === row.name && <Loader2 className="w-4 h-4 animate-spin" />}
                                        {row.signed_in ? t('mcpSignOut') : t('mcpSignIn')}
                                    </button>
                                )}
                            </div>
                        </div>
                    );
                })}
            </div>
        </div>
    );
}
