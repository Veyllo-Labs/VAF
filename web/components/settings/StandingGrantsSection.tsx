// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import { Folder, Wrench, X } from 'lucide-react';

interface Grants {
    tools: Record<string, { always: boolean; chats: number }>;
    dirs: string[];
}

/**
 * What an account allowed beyond a single call (vaf/core/trust.py): tools set to "always" or
 * allowed for a chat, and trusted folders. While one stands, the confirmation dialog is
 * skipped for that tool, so a grant nobody can see or take back only ever grows. `endpoint`
 * is the account's own list (/api/security/grants) or, for an admin, another account's
 * (/api/users/<id>/grants); revoking posts to `<endpoint>/revoke`.
 */
export default function StandingGrantsSection({ endpoint, own }: { endpoint: string; own: boolean }) {
    const t = useTranslations('grants');
    const apiBase = typeof window !== 'undefined' ? (document.location.origin || '') : '';
    // The list with the endpoint it came from: on the render where the account changes the
    // previous list is not shown (its revoke buttons would post to the new account).
    const [loaded, setLoaded] = useState<{ endpoint: string; grants: Grants } | null>(null);
    const data = loaded && loaded.endpoint === endpoint ? loaded.grants : null;
    // The list could not be fetched: said, with a retry, instead of a section that vanishes.
    const [loadFailed, setLoadFailed] = useState(false);
    const [failed, setFailed] = useState(false);

    // The list on screen is always THIS endpoint's: cleared when the account changes, on a
    // failed fetch, and an answer for a previous account is dropped - its revoke buttons
    // would otherwise post that account's grant names to the new one.
    // Only the LATEST load writes: a slow answer from before a revoke must not put a revoked
    // grant back on screen.
    const endpointRef = useRef(endpoint);
    endpointRef.current = endpoint;
    const loadSeqRef = useRef(0);
    const load = useCallback(async () => {
        const requested = endpoint;
        const seq = ++loadSeqRef.current;
        let next: Grants | null = null;
        try {
            const res = await fetch(`${apiBase}${requested}`, { credentials: 'include' });
            next = res.ok ? await res.json() : null;
        } catch {
            next = null;
        }
        if (endpointRef.current === requested && seq === loadSeqRef.current) {
            setLoaded(next ? { endpoint: requested, grants: next } : null);
            setLoadFailed(next === null);
        }
    }, [apiBase, endpoint]);

    useEffect(() => { setLoaded(null); setLoadFailed(false); void load(); }, [load]);

    const revoke = async (body: { tools?: string[]; dirs?: string[]; everything?: boolean }) => {
        const requested = endpoint;
        setFailed(false);
        let ok = false;
        try {
            const res = await fetch(`${apiBase}${requested}/revoke`, {
                method: 'POST', credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            ok = res.ok;
        } catch {
            ok = false;
        }
        // The admin moved to another account meanwhile: this answer is about the previous
        // one, so it neither marks the new account's list as failed nor reloads it.
        if (endpointRef.current !== requested) return;
        if (!ok) setFailed(true);
        // Either way the list shows what is really stored.
        void load();
    };

    if (!data) {
        if (!loadFailed) return null;
        return (
            <div className="bg-gray-50/50 p-6 rounded-xl border border-gray-100 mt-6">
                <h3 className="text-sm font-bold text-gray-900 uppercase tracking-wide mb-2">{t('title')}</h3>
                <div className="flex items-center gap-3">
                    <p className="text-sm text-red-600 flex-1">{t('loadFailed')}</p>
                    <button type="button" onClick={() => void load()}
                        className="text-xs font-medium text-gray-700 hover:text-gray-900 hover:underline">
                        {t('retry')}
                    </button>
                </div>
            </div>
        );
    }
    const tools = Object.entries(data.tools);
    const empty = tools.length === 0 && data.dirs.length === 0;
    const rowClass = 'flex items-center gap-3 px-3 py-2 rounded-lg border border-gray-200 bg-white';
    const revokeButton = (onClick: () => void, name: string) => (
        <button type="button" onClick={onClick} title={t('revoke')} aria-label={t('revokeItem', { name })}
            className="p-1.5 rounded-md text-gray-500 hover:text-red-600 hover:bg-gray-100">
            <X className="w-4 h-4" />
        </button>
    );
    return (
        <div className="bg-gray-50/50 p-6 rounded-xl border border-gray-100 mt-6">
            <div className="flex items-center justify-between mb-2">
                <h3 className="text-sm font-bold text-gray-900 uppercase tracking-wide">{t('title')}</h3>
                {!empty && (
                    <button type="button" onClick={() => void revoke({ everything: true })}
                        className="text-xs font-medium text-red-600 hover:text-red-700 hover:underline">
                        {t('revokeAll')}
                    </button>
                )}
            </div>
            <p className="text-xs text-gray-600 mb-4">{own ? t('intro') : t('introOther')}</p>
            {empty ? (
                <p className="text-sm text-gray-500">{t('none')}</p>
            ) : (
                <ul className="flex flex-col gap-2">
                    {tools.map(([name, how]) => (
                        <li key={`t-${name}`} className={rowClass}>
                            <Wrench className="w-4 h-4 text-gray-500 shrink-0" />
                            <span className="text-sm font-mono text-gray-800 flex-1 min-w-0 truncate">{name}</span>
                            <span className="text-xs text-gray-500">
                                {[how.always ? t('always') : null, how.chats > 0 ? t('chats', { count: how.chats }) : null]
                                    .filter(Boolean).join(' · ')}
                            </span>
                            {revokeButton(() => void revoke({ tools: [name] }), name)}
                        </li>
                    ))}
                    {data.dirs.map(dir => (
                        <li key={`d-${dir}`} className={rowClass}>
                            <Folder className="w-4 h-4 text-gray-500 shrink-0" />
                            <span className="text-sm font-mono text-gray-800 flex-1 min-w-0 truncate" title={dir}>{dir}</span>
                            <span className="text-xs text-gray-500">{t('folder')}</span>
                            {revokeButton(() => void revoke({ dirs: [dir] }), dir)}
                        </li>
                    ))}
                </ul>
            )}
            {failed && <p className="text-xs mt-2 text-red-600">{t('failed')}</p>}
        </div>
    );
}
