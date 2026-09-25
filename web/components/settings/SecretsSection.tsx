// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
'use client';

import { useCallback, useEffect, useState } from 'react';
import { useTranslations } from 'next-intl';
import { KeyRound, Trash2 } from 'lucide-react';

interface Row {
    name: string;
    env: string;
}

/**
 * The person's own credentials for their agent's commands (vaf/core/user_secrets.py). A value
 * is written and never read back: the list shows names and the variable the agent uses, and
 * the value field is emptied after a save, so nothing on the page holds it any longer than
 * the typing does.
 */
export default function SecretsSection() {
    const t = useTranslations('secrets');
    const apiBase = typeof window !== 'undefined' ? (document.location.origin || '') : '';
    const [rows, setRows] = useState<Row[]>([]);
    const [name, setName] = useState('');
    const [value, setValue] = useState('');
    const [busy, setBusy] = useState(false);
    const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);

    const load = useCallback(async () => {
        try {
            const res = await fetch(`${apiBase}/api/secrets`, { credentials: 'include' });
            if (!res.ok) return;
            const data = await res.json().catch(() => ({}));
            setRows(Array.isArray(data.names) ? data.names : []);
        } catch { /* a list that cannot be fetched stays as it was */ }
    }, [apiBase]);

    useEffect(() => { void load(); }, [load]);

    const save = async () => {
        if (!name.trim() || !value) return;
        setBusy(true);
        setNote(null);
        try {
            const res = await fetch(`${apiBase}/api/secrets/${encodeURIComponent(name.trim())}`, {
                method: 'PUT', credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ value }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                setNote({ ok: false, text: String(data.detail || t('failed')) });
                return;
            }
            setValue('');
            setName('');
            setNote({ ok: true, text: t('saved', { env: `$${data.env}` }) });
            void load();
        } catch {
            setNote({ ok: false, text: t('failed') });
        } finally {
            setBusy(false);
        }
    };

    const remove = async (row: Row) => {
        setNote(null);
        try {
            await fetch(`${apiBase}/api/secrets/${encodeURIComponent(row.name)}`,
                { method: 'DELETE', credentials: 'include' });
        } catch { /* the reload shows what is still there */ }
        void load();
    };

    const field = 'px-3 py-2 border border-gray-300 rounded-lg text-sm bg-white focus:outline-none focus:ring-2 focus:ring-gray-400';
    return (
        <div className="bg-gray-50/50 p-6 rounded-xl border border-gray-100 mt-6">
            <h3 className="text-sm font-bold text-gray-900 uppercase tracking-wide mb-2">{t('title')}</h3>
            <p className="text-xs text-gray-600 mb-4">{t('intro')}</p>
            {rows.length === 0 ? (
                <p className="text-sm text-gray-500 mb-4">{t('empty')}</p>
            ) : (
                <ul className="flex flex-col gap-2 mb-4">
                    {rows.map(r => (
                        <li key={r.name} className="flex items-center gap-3 px-3 py-2 rounded-lg border border-gray-200 bg-white">
                            <KeyRound className="w-4 h-4 text-gray-500 shrink-0" />
                            <span className="text-sm font-mono text-gray-800 flex-1 min-w-0 truncate">${r.env}</span>
                            <button type="button" onClick={() => void remove(r)} title={t('delete')} aria-label={t('delete')}
                                className="p-1.5 rounded-md text-gray-500 hover:text-red-600 hover:bg-gray-100">
                                <Trash2 className="w-4 h-4" />
                            </button>
                        </li>
                    ))}
                </ul>
            )}
            <form className="flex flex-wrap items-center gap-2" onSubmit={e => { e.preventDefault(); void save(); }}>
                <input value={name} onChange={e => setName(e.target.value)} placeholder={t('namePlaceholder')}
                    aria-label={t('name')} spellCheck={false} autoComplete="off" className={`${field} font-mono w-56`} />
                <input value={value} onChange={e => setValue(e.target.value)} placeholder={t('valuePlaceholder')}
                    aria-label={t('value')} type="password" autoComplete="new-password" className={`${field} flex-1 min-w-[12rem]`} />
                <button type="submit" disabled={busy || !name.trim() || !value}
                    className="px-4 py-2 text-sm font-medium rounded-lg bg-gray-900 hover:bg-black text-white dark:bg-[#e6e6e6] dark:hover:bg-[#f5f5f5] dark:text-[#181818] transition-colors disabled:opacity-50">
                    {t('save')}
                </button>
            </form>
            {note && (
                <p className={`text-xs mt-2 ${note.ok ? 'text-gray-600' : 'text-red-600'}`}>{note.text}</p>
            )}
        </div>
    );
}
