// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useLocale, useTranslations } from 'next-intl';
import { Check, Copy, KeyRound, Laptop, Monitor, Smartphone } from 'lucide-react';
import { copyText } from '@/lib/clipboard';
import { retryAfterSeconds, retryWhen } from '@/lib/retryAfter';

/** One live session of the signed-in account, as GET /api/auth/sessions answers it. */
interface SessionRow {
    id: string;
    kind: 'browser' | 'this_machine' | 'program';
    name: string;
    browser: string;
    os: string;
    ip: string;
    created_at: string | null;
    last_used_at: string | null;
    expires_at: string | null;
    idle_until: string | null;
    current: boolean;
}

/** The lifetimes a token for programs may have (PROGRAM_TOKEN_DAYS in vaf/auth/sessions.py). */
const TOKEN_DAYS = [7, 30, 90, 365];

/** The header a program sends the token in: typed back verbatim, so never translated. */
const BEARER_HEADER = 'Authorization: Bearer <token>';

/**
 * The account's own sessions (vaf/auth/sessions.py): the devices signed in to it and its tokens
 * for programs. A device is signed out here (DELETE /api/auth/sessions/<id>), every other one at
 * once (POST /api/auth/sessions/end-others), and a token for a script is made with the 2FA code
 * (POST /api/auth/tokens) and shown exactly once. The browser itself holds no token a script
 * could read; this is where the account sees what can act as it.
 */
export default function SessionsSection() {
    const t = useTranslations('account');
    const tCommon = useTranslations('common');
    const locale = useLocale();
    const apiBase = typeof window !== 'undefined' ? (document.location.origin || '') : '';
    const [rows, setRows] = useState<SessionRow[] | null>(null);
    const [idleDays, setIdleDays] = useState(7);
    const [loadFailed, setLoadFailed] = useState(false);
    const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);
    const [busy, setBusy] = useState<string | null>(null);
    const [name, setName] = useState('');
    const [days, setDays] = useState(30);
    const [code, setCode] = useState('');
    const [tokenNote, setTokenNote] = useState<string | null>(null);
    const [created, setCreated] = useState<string | null>(null);
    const [copied, setCopied] = useState(false);

    // Only the latest load writes: a slow answer from before a sign-out must not put an ended
    // session back on screen.
    const loadSeq = useRef(0);
    const load = useCallback(async () => {
        const seq = ++loadSeq.current;
        try {
            const res = await fetch(`${apiBase}/api/auth/sessions`, { credentials: 'include', cache: 'no-store' });
            if (!res.ok) throw new Error(String(res.status));
            const data = await res.json();
            if (seq !== loadSeq.current) return;
            setRows(Array.isArray(data.sessions) ? data.sessions : []);
            if (typeof data.idle_days === 'number') setIdleDays(data.idle_days);
            setLoadFailed(false);
        } catch {
            if (seq !== loadSeq.current) return;
            setLoadFailed(true);
        }
    }, [apiBase]);

    useEffect(() => { void load(); }, [load]);

    // The last use with its time (it is what tells a device apart), every other moment as a day.
    // The spaces inside a date do not break, so a narrow row never splits "4:00" from "PM".
    const format = (iso: string | null, withTime: boolean) => {
        if (!iso) return '-';
        try {
            return new Intl.DateTimeFormat(locale, withTime ? { dateStyle: 'medium', timeStyle: 'short' }
                : { dateStyle: 'medium' }).format(new Date(iso)).replace(/\s/g, '\u00a0');
        } catch {
            return iso;
        }
    };
    const when = (iso: string | null) => format(iso, true);
    const day = (iso: string | null) => format(iso, false);

    const deviceName = (row: SessionRow) => {
        if (!row.browser) return t('unknownDevice');
        return row.os ? t('deviceOn', { browser: row.browser, os: row.os }) : row.browser;
    };

    const endOne = async (row: SessionRow) => {
        setBusy(row.id);
        setNote(null);
        try {
            const res = await fetch(`${apiBase}/api/auth/sessions/${encodeURIComponent(row.id)}`, {
                method: 'DELETE', credentials: 'include',
            });
            // 404: it ended meanwhile (another tab, its own expiry); the reload shows the truth.
            if (!res.ok && res.status !== 404) setNote({ ok: false, text: t('actionFailed') });
        } catch {
            setNote({ ok: false, text: t('actionFailed') });
        } finally {
            setBusy(null);
            void load();
        }
    };

    const endOthers = async () => {
        if (!confirm(t('confirmSignOutOthers'))) return;
        setBusy('others');
        setNote(null);
        try {
            const res = await fetch(`${apiBase}/api/auth/sessions/end-others`, { method: 'POST', credentials: 'include' });
            const data = await res.json().catch(() => ({}));
            setNote(res.ok ? { ok: true, text: t('signedOutOthers', { count: Number(data.ended) || 0 }) }
                : { ok: false, text: t('actionFailed') });
        } catch {
            setNote({ ok: false, text: t('actionFailed') });
        } finally {
            setBusy(null);
            void load();
        }
    };

    const createToken = async () => {
        setTokenNote(null);
        setBusy('create');
        try {
            const res = await fetch(`${apiBase}/api/auth/tokens`, {
                method: 'POST', credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: name.trim(), days, code: code.trim() }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                // A known refusal is said in the reader's language; anything else as sent.
                const wait = retryWhen(retryAfterSeconds(res), locale);
                const known: Record<string, string> = {
                    invalid: t('errTokenInvalid'), wrong_code: t('errWrongCode'),
                    code_spent: t('errCodeSpent'), twofa_not_set_up: t('errTwofaNotSetUp'),
                    twofa_unreadable: t('errTwofaUnreadable'), record_changed: t('errRecordChanged'),
                };
                const held = res.status === 429 ? tCommon('tooManyAttempts', { when: wait }) : '';
                setTokenNote(known[String(data.code || '')] || held || String(data.detail || t('actionFailed')));
                return;
            }
            setCreated(String(data.token || ''));
            setCopied(false);
            setName('');
            setCode('');
            void load();
        } catch {
            setTokenNote(t('actionFailed'));
        } finally {
            setBusy(null);
        }
    };

    const copyCreated = async () => {
        if (created && await copyText(created)) setCopied(true);
    };

    const card = 'bg-gray-50/50 p-6 rounded-xl border border-gray-100 mt-6 max-md:p-4';
    const title = 'text-sm font-bold text-gray-900 uppercase tracking-wide';
    // Below md the action takes a row of its own under the meta, aligned with the text.
    const rowClass = 'flex items-start gap-3 px-3 py-2.5 rounded-lg border border-gray-200 bg-white max-md:flex-wrap';
    const meta = 'text-xs text-gray-500 mt-0.5 break-words';
    const linkButton = 'text-xs font-medium text-red-600 hover:text-red-700 hover:underline disabled:opacity-50 shrink-0';
    const rowAction = `${linkButton} max-md:basis-full max-md:text-left max-md:pl-7`;
    const badge = 'text-[11px] font-semibold px-1.5 py-0.5 rounded bg-gray-100 text-gray-700 border border-gray-200';
    const field = 'w-full px-3 py-2 border border-gray-300 rounded-lg text-sm bg-white focus:outline-none focus:ring-2 focus:ring-gray-400';
    const label = 'block text-xs font-semibold text-gray-700 mb-1';

    if (rows === null) {
        if (!loadFailed) return null;
        return (
            <div className={card}>
                <h3 className={`${title} mb-2`}>{t('devicesTitle')}</h3>
                <div className="flex items-center gap-3">
                    <p className="text-sm text-red-600 flex-1">{t('sessionsFailed')}</p>
                    <button type="button" onClick={() => void load()}
                        className="text-xs font-medium text-gray-700 hover:text-gray-900 hover:underline">
                        {t('retry')}
                    </button>
                </div>
            </div>
        );
    }

    const devices = rows.filter(r => r.kind !== 'program');
    const tokens = rows.filter(r => r.kind === 'program');
    const others = rows.some(r => !r.current);
    const deviceIcon = (row: SessionRow) => {
        if (row.kind === 'this_machine') return <Monitor className="w-4 h-4 text-gray-500 shrink-0 mt-0.5" />;
        if (row.os === 'Android' || row.os === 'iOS') return <Smartphone className="w-4 h-4 text-gray-500 shrink-0 mt-0.5" />;
        return <Laptop className="w-4 h-4 text-gray-500 shrink-0 mt-0.5" />;
    };

    return (
        <>
            <div className={card}>
                <div className="flex items-center justify-between gap-3 mb-2 max-md:flex-wrap">
                    <h3 className={title}>{t('devicesTitle')}</h3>
                    {others && (
                        <button type="button" onClick={() => void endOthers()} disabled={busy !== null} className={linkButton}>
                            {t('signOutOthers')}
                        </button>
                    )}
                </div>
                <p className="text-xs text-gray-600 mb-4">{t('devicesIntro', { days: idleDays })}</p>
                <ul className="flex flex-col gap-2">
                    {devices.map(row => (
                        <li key={row.id} className={rowClass}>
                            {deviceIcon(row)}
                            <div className="flex-1 min-w-0">
                                <div className="flex items-center gap-2 flex-wrap">
                                    <span className="text-sm font-medium text-gray-800">{deviceName(row)}</span>
                                    {row.current && <span className={badge}>{t('thisDevice')}</span>}
                                    {row.kind === 'this_machine' && <span className={badge}>{t('thisComputer')}</span>}
                                </div>
                                <p className={meta}>
                                    {[row.ip, t('lastUsed', { when: when(row.last_used_at) })].filter(Boolean).join(' · ')}
                                </p>
                                <p className={meta}>
                                    {[t('signedIn', { when: day(row.created_at) }),
                                        row.idle_until ? t('endsUnused', { when: day(row.idle_until) }) : t('validUntil', { when: day(row.expires_at) })]
                                        .join(' · ')}
                                </p>
                            </div>
                            {!row.current && (
                                <button type="button" onClick={() => void endOne(row)} disabled={busy !== null} className={rowAction}>
                                    {t('signOut')}
                                </button>
                            )}
                        </li>
                    ))}
                </ul>
                {note && <p className={`text-xs mt-3 ${note.ok ? 'text-gray-600' : 'text-red-600'}`}>{note.text}</p>}
            </div>

            <div className={card}>
                <h3 className={`${title} mb-2`}>{t('tokensTitle')}</h3>
                <p className="text-xs text-gray-600 mb-4">{t('tokensIntro')}</p>
                {tokens.length === 0 ? (
                    <p className="text-sm text-gray-500 mb-4">{t('tokensNone')}</p>
                ) : (
                    <ul className="flex flex-col gap-2 mb-4">
                        {tokens.map(row => (
                            <li key={row.id} className={rowClass}>
                                <KeyRound className="w-4 h-4 text-gray-500 shrink-0 mt-0.5" />
                                <div className="flex-1 min-w-0">
                                    <span className="text-sm font-medium text-gray-800 break-words">{row.name}</span>
                                    <p className={meta}>{t('lastUsed', { when: when(row.last_used_at) })}</p>
                                    <p className={meta}>
                                        {[t('tokenCreated', { when: day(row.created_at) }), t('validUntil', { when: day(row.expires_at) })].join(' · ')}
                                    </p>
                                </div>
                                <button type="button" onClick={() => void endOne(row)} disabled={busy !== null} className={rowAction}>
                                    {t('revoke')}
                                </button>
                            </li>
                        ))}
                    </ul>
                )}

                {created ? (
                    <div className="rounded-lg border border-gray-200 bg-white p-3">
                        <p className="text-xs font-semibold text-amber-700 mb-2">{t('tokenShownOnce')}</p>
                        <div className="flex items-start gap-2 max-md:flex-col">
                            <code className="flex-1 min-w-0 font-mono text-xs text-gray-900 bg-gray-100 rounded px-2 py-1.5 break-all select-all">{created}</code>
                            <button type="button" onClick={() => void copyCreated()}
                                className="inline-flex items-center gap-1 px-3 py-1.5 text-xs font-medium rounded-lg border border-gray-300 text-gray-700 hover:bg-gray-50 shrink-0">
                                {copied ? <Check className="w-3.5 h-3.5" /> : <Copy className="w-3.5 h-3.5" />}
                                {copied ? t('copied') : t('copy')}
                            </button>
                        </div>
                        <p className="text-xs text-gray-500 mt-2 font-mono break-all">{BEARER_HEADER}</p>
                        <button type="button" onClick={() => setCreated(null)}
                            className="mt-3 px-4 py-2 text-sm font-medium rounded-lg bg-gray-900 hover:bg-black text-white dark:bg-[#e6e6e6] dark:hover:bg-[#f5f5f5] dark:text-[#181818] transition-colors">
                            {t('done')}
                        </button>
                    </div>
                ) : (
                    <form className="grid grid-cols-[minmax(0,1fr)_auto_auto_auto] gap-3 items-end max-md:grid-cols-1"
                        onSubmit={e => { e.preventDefault(); void createToken(); }}>
                        <div>
                            <label className={label} htmlFor="token-name">{t('tokenName')}</label>
                            <input id="token-name" value={name} onChange={e => setName(e.target.value)} maxLength={60}
                                placeholder={t('tokenNamePlaceholder')} className={field} />
                        </div>
                        <div>
                            <label className={label} htmlFor="token-days">{t('tokenLifetime')}</label>
                            <select id="token-days" value={days} onChange={e => setDays(Number(e.target.value))} className={field}>
                                {TOKEN_DAYS.map(d => <option key={d} value={d}>{t('tokenDays', { days: d })}</option>)}
                            </select>
                        </div>
                        <div>
                            <label className={label} htmlFor="token-code">{t('code')}</label>
                            <input id="token-code" value={code} onChange={e => setCode(e.target.value)} inputMode="numeric"
                                autoComplete="one-time-code" placeholder="123456" className={`${field} font-mono w-32 max-md:w-full`} />
                        </div>
                        <button type="submit" disabled={busy !== null || !name.trim() || !code.trim()}
                            className="px-4 py-2 text-sm font-medium rounded-lg bg-gray-900 hover:bg-black text-white dark:bg-[#e6e6e6] dark:hover:bg-[#f5f5f5] dark:text-[#181818] transition-colors disabled:opacity-50 dark:disabled:opacity-100 dark:disabled:bg-[#3a3a3a] dark:disabled:text-[#a3a3a3]">
                            {t('tokenCreate')}
                        </button>
                    </form>
                )}
                {tokenNote && <p className="text-xs mt-3 text-red-600">{tokenNote}</p>}
            </div>
        </>
    );
}
