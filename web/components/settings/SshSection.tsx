// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
'use client';

import { useCallback, useEffect, useState } from 'react';
import { useTranslations } from 'next-intl';
import { Copy, Server, Trash2 } from 'lucide-react';
import { copyText } from '@/lib/clipboard';

interface Host {
    host: string;
    fingerprint: string;
    type: string;
}

interface Overview {
    available: boolean;
    reason?: string;
    public_key: string | null;
    hosts: Host[];
}

/**
 * This account's own SSH identity for the agent (vaf/core/ssh.py, /api/ssh): the public key to
 * put on a server or into a hoster's panel, and the servers this account confirmed. Hidden
 * for an account the ssh tool is not enabled for (the route answers 403). A key is created
 * only while none exists and never replaced: a new key would lock the account out of every
 * server the old one is installed on. Removing a server makes the agent's next connection to
 * it ask again - what a reinstalled server needs.
 */
export default function SshSection() {
    const t = useTranslations('ssh');
    const apiBase = typeof window !== 'undefined' ? (document.location.origin || '') : '';
    const [data, setData] = useState<Overview | null>(null);
    const [busy, setBusy] = useState(false);
    const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);

    const load = useCallback(async () => {
        try {
            const res = await fetch(`${apiBase}/api/ssh`, { credentials: 'include' });
            if (!res.ok) { setData(null); return; }
            setData(await res.json());
        } catch { /* a section that cannot be fetched stays as it was */ }
    }, [apiBase]);

    useEffect(() => { void load(); }, [load]);

    const createKey = async () => {
        setBusy(true);
        setNote(null);
        try {
            const res = await fetch(`${apiBase}/api/ssh/key`, { method: 'POST', credentials: 'include' });
            const body = await res.json().catch(() => ({}));
            if (!res.ok) { setNote({ ok: false, text: String(body.detail || t('failed')) }); return; }
            setData(body);
        } catch {
            setNote({ ok: false, text: t('failed') });
        } finally {
            setBusy(false);
        }
    };

    const copyKey = async () => {
        if (!data?.public_key) return;
        const ok = await copyText(data.public_key);
        setNote(ok ? { ok: true, text: t('copied') } : { ok: false, text: t('failed') });
    };

    const forget = async (host: Host) => {
        setNote(null);
        try {
            const res = await fetch(`${apiBase}/api/ssh/hosts/${encodeURIComponent(host.host)}`,
                { method: 'DELETE', credentials: 'include' });
            if (!res.ok) {
                const body = await res.json().catch(() => ({}));
                setNote({ ok: false, text: String(body.detail || t('failed')) });
            }
        } catch {
            setNote({ ok: false, text: t('failed') });
        }
        // Either way the list shows what is really stored.
        void load();
    };

    if (!data) return null;
    return (
        <div className="bg-gray-50/50 p-6 rounded-xl border border-gray-100 mt-6">
            <h3 className="text-sm font-bold text-gray-900 uppercase tracking-wide mb-2">{t('title')}</h3>
            <p className="text-xs text-gray-600 mb-4">{t('intro')}</p>
            {!data.available ? (
                <p className="text-sm text-gray-500">{t('unavailable', { reason: data.reason || '' })}</p>
            ) : (
                <>
                    <p className="text-xs font-semibold text-gray-700 mb-1">{t('publicKey')}</p>
                    {data.public_key ? (
                        <div className="flex items-start gap-2 mb-2">
                            <code className="flex-1 min-w-0 text-xs font-mono bg-white border border-gray-200 rounded-lg p-2 break-all text-gray-800">{data.public_key}</code>
                            <button type="button" onClick={() => void copyKey()} title={t('copy')} aria-label={t('copy')}
                                className="p-2 rounded-md text-gray-500 hover:text-gray-900 hover:bg-gray-100">
                                <Copy className="w-4 h-4" />
                            </button>
                        </div>
                    ) : (
                        <div className="flex items-center gap-3 mb-2">
                            <p className="text-sm text-gray-500 flex-1">{t('noKey')}</p>
                            <button type="button" disabled={busy} onClick={() => void createKey()}
                                className="px-4 py-2 text-sm font-medium rounded-lg bg-gray-900 hover:bg-black text-white dark:bg-[#e6e6e6] dark:hover:bg-[#f5f5f5] dark:text-[#181818] transition-colors disabled:opacity-50">
                                {t('createKey')}
                            </button>
                        </div>
                    )}
                    <p className="text-xs text-gray-500 mb-4">{t('keyHint')}</p>
                    <p className="text-xs font-semibold text-gray-700 mb-1">{t('servers')}</p>
                    {data.hosts.length === 0 ? (
                        <p className="text-sm text-gray-500">{t('noServers')}</p>
                    ) : (
                        <ul className="flex flex-col gap-2">
                            {data.hosts.map(h => (
                                <li key={h.host} className="flex items-center gap-3 px-3 py-2 rounded-lg border border-gray-200 bg-white">
                                    <Server className="w-4 h-4 text-gray-500 shrink-0" />
                                    <span className="text-sm font-mono text-gray-800 min-w-0 truncate">{h.host}</span>
                                    <span className="text-xs font-mono text-gray-500 flex-1 min-w-0 truncate" title={`${h.type} ${h.fingerprint}`}>{h.fingerprint}</span>
                                    <button type="button" onClick={() => void forget(h)} title={t('forget')} aria-label={t('forget')}
                                        className="p-1.5 rounded-md text-gray-500 hover:text-red-600 hover:bg-gray-100">
                                        <Trash2 className="w-4 h-4" />
                                    </button>
                                </li>
                            ))}
                        </ul>
                    )}
                </>
            )}
            {note && (
                <p className={`text-xs mt-2 ${note.ok ? 'text-gray-600' : 'text-red-600'}`}>{note.text}</p>
            )}
        </div>
    );
}
