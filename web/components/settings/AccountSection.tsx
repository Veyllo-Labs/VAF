// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
'use client';

import { useState } from 'react';
import { useLocale, useTranslations } from 'next-intl';
import { retryAfterSeconds, retryWhen } from '@/lib/retryAfter';
import { MIN_PASSWORD_LEN } from '@/lib/passwordPolicy';

/**
 * The caller changes their OWN password (POST /api/auth/change-password, vaf/auth/user_admin.py
 * change_own_password): the current password, and the 2FA code when 2FA is set up, so a session
 * left open on someone else's screen is not enough. The fields are emptied after a change, and
 * nothing on the page keeps a password any longer than the typing does. A forgotten password is
 * not handled here: an admin resets an account, and the admin's own in a terminal.
 */
export default function AccountSection() {
    const t = useTranslations('account');
    const tCommon = useTranslations('common');
    const locale = useLocale();
    const apiBase = typeof window !== 'undefined' ? (document.location.origin || '') : '';
    const [current, setCurrent] = useState('');
    const [next, setNext] = useState('');
    const [repeat, setRepeat] = useState('');
    const [code, setCode] = useState('');
    const [busy, setBusy] = useState(false);
    const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);

    const change = async () => {
        setNote(null);
        if (next.length < MIN_PASSWORD_LEN) {
            setNote({ ok: false, text: t('tooShort', { n: MIN_PASSWORD_LEN }) });
            return;
        }
        if (next !== repeat) {
            setNote({ ok: false, text: t('mismatch') });
            return;
        }
        setBusy(true);
        try {
            const res = await fetch(`${apiBase}/api/auth/change-password`, {
                method: 'POST', credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ current_password: current, new_password: next, code: code.trim() }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                // A known refusal is said in the reader's language; anything else as sent. A
                // hold (429) names its wait: the second factor's, or the address window's.
                const when = retryWhen(retryAfterSeconds(res), locale);
                const known: Record<string, string> = {
                    wrong_password: t('errWrongPassword'), wrong_code: t('errWrongCode'),
                    same_password: t('errSamePassword'), record_changed: t('errRecordChanged'),
                    twofa_unreadable: t('errTwofaUnreadable'),
                    throttled: t('errThrottled', { when }),
                    code_spent: t('errCodeSpent'),
                    password_too_short: t('tooShort', { n: MIN_PASSWORD_LEN }),
                    password_common: t('errCommonPassword'),
                };
                const held = res.status === 429 && !data.code ? tCommon('tooManyAttempts', { when }) : '';
                setNote({ ok: false, text: known[String(data.code || '')] || held || String(data.detail || t('failed')) });
                return;
            }
            setCurrent('');
            setNext('');
            setRepeat('');
            setCode('');
            setNote({ ok: true, text: t('changed') });
        } catch {
            setNote({ ok: false, text: t('failed') });
        } finally {
            setBusy(false);
        }
    };

    const field = 'w-full px-3 py-2 border border-gray-300 rounded-lg text-sm bg-white focus:outline-none focus:ring-2 focus:ring-gray-400';
    const label = 'block text-xs font-semibold text-gray-700 mb-1';
    return (
        <div className="bg-gray-50/50 p-6 rounded-xl border border-gray-100">
            <h3 className="text-sm font-bold text-gray-900 uppercase tracking-wide mb-2">{t('passwordTitle')}</h3>
            <p className="text-xs text-gray-600 mb-4">{t('passwordIntro')}</p>
            <form className="flex flex-col gap-3 max-w-md" onSubmit={e => { e.preventDefault(); void change(); }}>
                <div>
                    <label className={label} htmlFor="acct-current">{t('current')}</label>
                    <input id="acct-current" type="password" value={current} onChange={e => setCurrent(e.target.value)}
                        autoComplete="current-password" className={field} />
                </div>
                <div>
                    <label className={label} htmlFor="acct-new">{t('new')}</label>
                    <input id="acct-new" type="password" value={next} onChange={e => setNext(e.target.value)}
                        autoComplete="new-password" className={field} />
                </div>
                <div>
                    <label className={label} htmlFor="acct-repeat">{t('repeat')}</label>
                    <input id="acct-repeat" type="password" value={repeat} onChange={e => setRepeat(e.target.value)}
                        autoComplete="new-password" className={field} />
                </div>
                <div>
                    <label className={label} htmlFor="acct-code">{t('code')}</label>
                    <input id="acct-code" value={code} onChange={e => setCode(e.target.value)} inputMode="numeric"
                        autoComplete="one-time-code" placeholder="123456" className={`${field} font-mono w-40`} />
                    <p className="text-xs text-gray-500 mt-1">{t('codeHint')}</p>
                </div>
                <div>
                    <button type="submit" disabled={busy || !current || !next || !repeat}
                        className="px-4 py-2 text-sm font-medium rounded-lg bg-gray-900 hover:bg-black text-white dark:bg-[#e6e6e6] dark:hover:bg-[#f5f5f5] dark:text-[#181818] transition-colors disabled:opacity-50">
                        {t('submit')}
                    </button>
                </div>
            </form>
            {note && (
                <p className={`text-xs mt-3 ${note.ok ? 'text-gray-600' : 'text-red-600'}`}>{note.text}</p>
            )}
            <p className="text-xs text-gray-500 mt-4">{t('forgotten')}</p>
        </div>
    );
}
