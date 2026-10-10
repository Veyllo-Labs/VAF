// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
'use client';

import { useCallback, useEffect, useState } from 'react';
import { useTranslations } from 'next-intl';
import { Server, Trash2 } from 'lucide-react';

interface FtpServer {
    name: string;
    trust: 'authority' | 'pinned' | 'none' | string;
    fingerprint: string;
    confirmed: string;
}

/**
 * The FTP servers this account confirmed for its agent (vaf/core/ftp.py, /api/ftp), each with
 * how its certificate is trusted: a certificate authority, a fingerprint remembered at the
 * first connection, or none for plain FTP. Hidden for an account the ftp tool is not enabled
 * for (the route answers 403); any other failed load shows itself with a retry, so a server
 * error does not read as "FTP is not for you". Removing a server makes the agent's next
 * connection to it ask again - what a server whose certificate changed needs.
 */
export default function FtpSection() {
    const t = useTranslations('ftp');
    const apiBase = typeof window !== 'undefined' ? (document.location.origin || '') : '';
    const [servers, setServers] = useState<FtpServer[] | null>(null);
    const [note, setNote] = useState<string | null>(null);
    const [loadFailed, setLoadFailed] = useState(false);

    const load = useCallback(async () => {
        try {
            const res = await fetch(`${apiBase}/api/ftp`, { credentials: 'include' });
            // 403: the tool is not enabled for this account - the section stays away.
            if (res.status === 403) { setServers(null); setLoadFailed(false); return; }
            if (!res.ok) { setLoadFailed(true); return; }
            const body = await res.json();
            setServers(Array.isArray(body.servers) ? body.servers : []);
            setLoadFailed(false);
        } catch {
            setLoadFailed(true);
        }
    }, [apiBase]);

    useEffect(() => { void load(); }, [load]);

    const forget = async (server: FtpServer) => {
        setNote(null);
        try {
            const res = await fetch(`${apiBase}/api/ftp/servers/${encodeURIComponent(server.name)}`,
                { method: 'DELETE', credentials: 'include' });
            if (!res.ok) {
                const body = await res.json().catch(() => ({}));
                setNote(String(body.detail || t('failed')));
            }
        } catch {
            setNote(t('failed'));
        }
        // Either way the list shows what is really stored.
        void load();
    };

    const trustLabel = (s: FtpServer) =>
        s.trust === 'authority' ? t('trustAuthority') : s.trust === 'pinned' ? t('trustPinned') : t('trustNone');

    if (servers === null && !loadFailed) return null;
    return (
        <div className="bg-gray-50/50 p-6 rounded-xl border border-gray-100 mt-6">
            <h3 className="text-sm font-bold text-gray-900 uppercase tracking-wide mb-2">{t('title')}</h3>
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
            {servers === null ? null : <>
            <p className="text-xs font-semibold text-gray-700 mb-1">{t('servers')}</p>
            {servers.length === 0 ? (
                <p className="text-sm text-gray-500">{t('noServers')}</p>
            ) : (
                <ul className="flex flex-col gap-2">
                    {servers.map(s => (
                        <li key={s.name} className="px-3 py-2 rounded-lg border border-gray-200 bg-white">
                            <div className="flex items-center gap-3">
                                <Server className="w-4 h-4 text-gray-500 shrink-0" />
                                <span className="text-sm font-mono text-gray-800 flex-1 min-w-0 truncate" title={s.name}>{s.name}</span>
                                <span className={`text-[11px] px-1.5 py-0.5 rounded border whitespace-nowrap shrink-0 ${s.trust === 'none'
                                    ? 'border-amber-300 text-amber-700'
                                    : 'border-gray-200 text-gray-600'}`}>
                                    {trustLabel(s)}
                                </span>
                                <button type="button" onClick={() => void forget(s)} title={t('forget')} aria-label={t('forget')}
                                    className="p-1.5 rounded-md text-gray-500 hover:text-red-600 hover:bg-gray-100 shrink-0">
                                    <Trash2 className="w-4 h-4" />
                                </button>
                            </div>
                            {s.fingerprint && (
                                <p className="mt-1 pl-7 text-xs font-mono text-gray-500 truncate" title={s.fingerprint}>{s.fingerprint}</p>
                            )}
                        </li>
                    ))}
                </ul>
            )}
            </>}
            {note && <p className="text-xs mt-2 text-red-600">{note}</p>}
        </div>
    );
}
