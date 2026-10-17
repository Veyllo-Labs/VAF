// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
'use client';

import { useCallback, useEffect, useState } from 'react';
import { useTranslations } from 'next-intl';
import { Cpu, Cloud } from 'lucide-react';

interface ProfileState {
    profile: 'local' | 'api';
    services: string[];
    left_out: string[];
    conflicts: string[];
}

/**
 * The installation's inference profile (vaf/core/inference_profile.py, /api/system/inference-
 * profile): whether this machine runs local models at all. The switch is the same
 * implementation as `vaf profile` - Lean stops the speech containers, Full starts them - so it
 * asks once on the button before it does anything. Admin only, like the tab it sits on.
 */
export default function InferenceProfileSection({ onChanged }: { onChanged?: () => void }) {
    const t = useTranslations('inferenceProfile');
    const apiBase = typeof window !== 'undefined' ? (document.location.origin || '') : '';
    const [data, setData] = useState<ProfileState | null>(null);
    const [loadFailed, setLoadFailed] = useState(false);
    const [confirming, setConfirming] = useState(false);
    const [prune, setPrune] = useState(false);
    const [busy, setBusy] = useState(false);
    const [note, setNote] = useState<{ text: string; error: boolean } | null>(null);

    const load = useCallback(async () => {
        try {
            const res = await fetch(`${apiBase}/api/system/inference-profile`, { credentials: 'include' });
            if (!res.ok) { setLoadFailed(true); return; }
            setData(await res.json());
            setLoadFailed(false);
        } catch {
            setLoadFailed(true);
        }
    }, [apiBase]);

    useEffect(() => { void load(); }, [load]);

    const target: 'local' | 'api' = data?.profile === 'api' ? 'local' : 'api';

    const doSwitch = async () => {
        if (!confirming) { setConfirming(true); setNote(null); return; }
        setConfirming(false);
        setBusy(true);
        setNote(null);
        try {
            const res = await fetch(`${apiBase}/api/system/inference-profile`, {
                method: 'POST',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ profile: target, prune: target === 'api' && prune }),
            });
            const body = await res.json().catch(() => ({}));
            if (!res.ok) {
                setNote({ text: String(body.detail || t('failed')), error: true });
            } else {
                const errors: string[] = body.errors || [];
                setNote(errors.length
                    ? { text: errors.join(' '), error: true }
                    : { text: t('done', { profile: target === 'api' ? t('api') : t('local') }), error: false });
                onChanged?.();
            }
        } catch {
            setNote({ text: t('failed'), error: true });
        } finally {
            setBusy(false);
            setPrune(false);
        }
        void load();
    };

    if (!data && !loadFailed) return null;
    const lean = data?.profile === 'api';
    const Icon = lean ? Cloud : Cpu;

    return (
        <div className="bg-gray-50/50 p-6 rounded-xl border border-gray-100">
            <h3 className="text-sm font-bold text-gray-900 uppercase tracking-wide mb-3">{t('title')}</h3>
            {loadFailed && (
                <div className="flex items-center gap-3 mb-2">
                    <p className="text-sm text-red-600">{t('loadFailed')}</p>
                    <button type="button" onClick={() => void load()}
                        className="px-2 py-1 text-xs font-medium rounded-md border border-gray-200 text-gray-700 hover:bg-gray-100">
                        {t('retry')}
                    </button>
                </div>
            )}
            {data && (
                <>
                    <div className="flex items-start gap-3">
                        <Icon className="w-5 h-5 text-gray-500 shrink-0 mt-0.5" />
                        <div className="min-w-0 flex-1">
                            <p className="text-sm font-medium text-gray-800">{lean ? t('api') : t('local')}</p>
                            <p className="text-xs text-gray-600 mt-0.5">{lean ? t('apiDesc') : t('localDesc')}</p>
                            {data.left_out.length > 0 && (
                                <p className="text-xs text-gray-500 mt-1">{t('leftOut', { names: data.left_out.join(', ') })}</p>
                            )}
                        </div>
                    </div>
                    {data.conflicts.length > 0 && (
                        <ul className="mt-3 flex flex-col gap-1">
                            {data.conflicts.map((c) => (
                                <li key={c} className="text-xs text-amber-700">{c}</li>
                            ))}
                        </ul>
                    )}
                    <div className="mt-4 flex flex-wrap items-center gap-3">
                        <button type="button" disabled={busy} onClick={() => void doSwitch()}
                            className={confirming
                                ? 'px-3 py-1.5 text-xs font-medium rounded-md bg-amber-600 text-white hover:bg-amber-700 disabled:opacity-50'
                                : 'px-3 py-1.5 text-xs font-medium rounded-md border border-gray-200 text-gray-700 hover:bg-gray-100 disabled:opacity-50'}>
                            {busy ? t('switching')
                                : confirming ? (target === 'api' ? t('confirmApi') : t('confirmLocal'))
                                : (target === 'api' ? t('switchToApi') : t('switchToLocal'))}
                        </button>
                        {confirming && target === 'api' && (
                            <label className="flex items-center gap-2 text-xs text-gray-600 cursor-pointer select-none">
                                <input type="checkbox" checked={prune} onChange={(e) => setPrune(e.target.checked)} />
                                {t('prune')}
                            </label>
                        )}
                    </div>
                    <p className="text-xs text-gray-500 mt-2">{target === 'api' ? t('apiHint') : t('localHint')}</p>
                </>
            )}
            {note && <p className={`text-xs mt-2 ${note.error ? 'text-red-600' : 'text-emerald-700'}`}>{note.text}</p>}
        </div>
    );
}
