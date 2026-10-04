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
    const [data, setData] = useState<Grants | null>(null);
    const [failed, setFailed] = useState(false);

    // The list on screen is always THIS endpoint's: cleared when the account changes, on a
    // failed fetch, and an answer for a previous account is dropped - its revoke buttons
    // would otherwise post that account's grant names to the new one.
    const endpointRef = useRef(endpoint);
    endpointRef.current = endpoint;
    const load = useCallback(async () => {
        const requested = endpoint;
        let next: Grants | null = null;
        try {
            const res = await fetch(`${apiBase}${requested}`, { credentials: 'include' });
            next = res.ok ? await res.json() : null;
        } catch {
            next = null;
        }
        if (endpointRef.current === requested) setData(next);
    }, [apiBase, endpoint]);

    useEffect(() => { setData(null); void load(); }, [load]);

    const revoke = async (body: { tools?: string[]; dirs?: string[]; everything?: boolean }) => {
        setFailed(false);
        try {
            const res = await fetch(`${apiBase}${endpoint}/revoke`, {
                method: 'POST', credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (!res.ok) setFailed(true);
        } catch {
            setFailed(true);
        }
        // Either way the list shows what is really stored.
        void load();
    };

    if (!data) return null;
    const tools = Object.entries(data.tools);
    const empty = tools.length === 0 && data.dirs.length === 0;
    const rowClass = 'flex items-center gap-3 px-3 py-2 rounded-lg border border-gray-200 bg-white';
    const revokeButton = (onClick: () => void) => (
        <button type="button" onClick={onClick} title={t('revoke')} aria-label={t('revoke')}
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
                            {revokeButton(() => void revoke({ tools: [name] }))}
                        </li>
                    ))}
                    {data.dirs.map(dir => (
                        <li key={`d-${dir}`} className={rowClass}>
                            <Folder className="w-4 h-4 text-gray-500 shrink-0" />
                            <span className="text-sm font-mono text-gray-800 flex-1 min-w-0 truncate" title={dir}>{dir}</span>
                            <span className="text-xs text-gray-500">{t('folder')}</span>
                            {revokeButton(() => void revoke({ dirs: [dir] }))}
                        </li>
                    ))}
                </ul>
            )}
            {failed && <p className="text-xs mt-2 text-red-600">{t('failed')}</p>}
        </div>
    );
}
