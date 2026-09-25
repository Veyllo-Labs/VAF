'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
//
// In-client account panel for the v2 mail client (P5.5). Opened from the mail
// window's gear; replaces the setup-wizard crutch for day-to-day management.
// Builds entirely on the native /api/mail/accounts endpoints (P4.3): list, add an
// IMAP account (test + save), verify a saved account, edit its label, toggle
// auto-sync, and calendar-safe remove. OAuth sign-in (Gmail/Microsoft) stays on
// the shared /api/email hub, but the panel drives it itself: connecting a new
// account and reconnecting an existing one are the SAME call, differing only in
// the login_hint, so no setup wizard is involved.

import React, { useCallback, useEffect, useState } from 'react';
import { useTranslations } from 'next-intl';
import {
    AlertTriangle, Check, ChevronDown, Loader2, Mail, Pencil, Plus, RefreshCw, ShieldCheck, Trash2, X,
} from 'lucide-react';
import { cn, getApiBase } from '@/lib/utils';
import { SFC_CHROME, SFC_FILL, SFC_HOVER, SFC_WINDOW } from './ChannelDashboardShell';

// What a refused IMAP login comes back with. The guidance arrives in PARTS so
// the panel renders it in the reader's language; the backend also sends `hint`
// as English prose for callers without a message catalogue, and the panel uses
// that only when there are no parts to compose from.
type HintDetail = {
    provider: string | null;
    auth: string;
    enable_imap: boolean;
    help_url: string | null;
    text: string;
};
type LoginFail = { error?: string; hint?: string | null; hint_detail?: HintDetail | null };

const AUTH_MESSAGE: Record<string, string> = {
    password: 'authHintPassword',
    app_password: 'authHintAppPassword',
    mail_password: 'authHintMailPassword',
    oauth: 'authHintOauth',
    bridge: 'authHintBridge',
    none: 'authHintNoImap',
    unknown: 'authHintUnknown',
};

function AuthHint({ detail }: { detail: HintDetail }) {
    const t = useTranslations('mailV2');
    const key = AUTH_MESSAGE[detail.auth];
    const provider = detail.provider ?? '';
    return (
        <>
            {/* An auth kind this build has no wording for still says something:
                the backend's English sentence beats an empty line. */}
            <p className="text-gray-700">{key ? t(key, { provider }) : detail.text}</p>
            {detail.enable_imap && (
                <p className="text-gray-700">{t('authHintEnableImap', { provider })}</p>
            )}
            {detail.help_url && (
                <a href={detail.help_url} target="_blank" rel="noopener noreferrer"
                    className="inline-block text-blue-700 hover:underline">
                    {t('authHintHelp', { provider })}
                </a>
            )}
        </>
    );
}

const api = (p: string) => `${getApiBase()}${p.startsWith('/') ? p : `/${p}`}`;
const jfetch = async (p: string, init?: RequestInit) => {
    const r = await fetch(api(p), { credentials: 'include', ...init });
    if (!r.ok) throw new Error(`${r.status}`);
    return r.json();
};
const jsend = (p: string, body?: unknown, method = 'POST') => jfetch(p, {
    method, headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
});

interface Acct {
    account_id: string;
    email: string;
    provider: string;
    label: string;
    imap_ready: boolean;
    auto_sync_enabled: boolean;
    /** Sender verification (EMAIL_CLIENT.md, "Verification and cases"): the provider's
     *  Authentication-Results id the store trusts, how it was learned ("provider": known
     *  for the provider, named in authserv_provider), and whether the account is set up
     *  at all (a Microsoft account needs no id). */
    trusted_authserv_id?: string;
    auth_profile?: string;
    authserv_source?: string;
    authserv_provider?: string;
    authserv_samples?: number;
    auth_ready?: boolean;
}

type LearnState = { kind: 'idle' } | { kind: 'busy' } | { kind: 'done'; id: string; profile: string; count: number }
    | { kind: 'too_few'; count: number } | { kind: 'failed' };

function providerName(p: string): string {
    const m: Record<string, string> = { gmail: 'Gmail', microsoft: 'Microsoft', imap: 'IMAP' };
    return m[p] || (p ? p.charAt(0).toUpperCase() + p.slice(1) : 'IMAP');
}

/** Password/app-password accounts speak IMAP by definition and never carry the
 *  imap_ready flag (it only marks an OAuth token that gained IMAP scope), so
 *  reading the flag alone showed a permanent "not ready" warning on healthy
 *  accounts - with no action the user could take. */
function isImapCapable(a: Acct): boolean {
    return a.imap_ready || (a.provider || 'imap').toLowerCase() === 'imap';
}

export function MailAccounts({ onClose }: { onClose: () => void }) {
    const t = useTranslations('mailV2');
    const [accounts, setAccounts] = useState<Acct[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [busy, setBusy] = useState<string | null>(null);          // account_id currently acting on
    const [verify, setVerify] = useState<Record<string, 'ok' | 'fail' | 'checking'>>({});
    const [editLabel, setEditLabel] = useState<Record<string, string>>({});
    const [confirmDel, setConfirmDel] = useState<string | null>(null);
    const [showAdd, setShowAdd] = useState(false);
    const [connecting, setConnecting] = useState<string | null>(null);
    // Which OAuth providers an admin has actually configured. VAF ships a client
    // id for Google only, so Microsoft is unusable on most instances - the wizard
    // hid the button entirely; without this the button would just 400.
    const [oauthReady, setOauthReady] = useState<{ google: boolean; microsoft: boolean }>(
        { google: true, microsoft: true });
    useEffect(() => {
        jfetch('api/email/oauth-status')
            .then(d => setOauthReady({
                google: !!(d.oauth_google_configured ?? true),
                microsoft: !!(d.oauth_microsoft_configured ?? true),
            }))
            .catch(() => undefined);   // unreachable status must not hide the buttons
    }, []);

    const load = useCallback(async () => {
        setLoading(true);
        try {
            const d = await jfetch('api/mail/accounts');
            setAccounts(d.accounts || []);
            setError(null);
        } catch { setError(t('accountsLoadFailed')); }
        finally { setLoading(false); }
    }, [t]);

    useEffect(() => { load(); }, [load]);

    // The consent screen runs in the system browser, so nothing in the app knows
    // when it finished. Poll while a reconnect is outstanding and re-check when the
    // window regains focus, otherwise the account keeps showing "not ready" after
    // the user has already granted access.
    useEffect(() => {
        if (!connecting) return;
        const done = accounts.find(a => a.account_id === connecting);
        if (done && isImapCapable(done)) { setConnecting(null); return; }
        const timer = setInterval(load, 4000);
        const onFocus = () => load();
        window.addEventListener('focus', onFocus);
        const giveUp = setTimeout(() => setConnecting(null), 5 * 60_000);
        return () => {
            clearInterval(timer);
            clearTimeout(giveUp);
            window.removeEventListener('focus', onFocus);
        };
    }, [connecting, accounts, load]);

    const doVerify = async (a: Acct) => {
        setVerify(v => ({ ...v, [a.account_id]: 'checking' }));
        try {
            const r = await jsend(`api/mail/accounts/${encodeURIComponent(a.account_id)}/verify`);
            setVerify(v => ({ ...v, [a.account_id]: r.ok ? 'ok' : 'fail' }));
        } catch { setVerify(v => ({ ...v, [a.account_id]: 'fail' })); }
    };

    const saveLabel = async (a: Acct) => {
        const label = (editLabel[a.account_id] ?? a.label).trim();
        setBusy(a.account_id);
        try {
            await jsend(`api/mail/accounts/${encodeURIComponent(a.account_id)}`, { label }, 'PATCH');
            setAccounts(prev => prev.map(x => x.account_id === a.account_id ? { ...x, label } : x));
            setEditLabel(prev => { const n = { ...prev }; delete n[a.account_id]; return n; });
        } catch { setError(t('accountsSaveFailed')); }
        finally { setBusy(null); }
    };

    const [learn, setLearn] = useState<Record<string, LearnState>>({});
    /** Learn the provider's Authentication-Results id from the account's own inbox: the
     *  route saves it on the account and recomputes every stored verdict, so the badges in
     *  the list change from nothing to verified or unverified after this call. */
    const learnAuth = async (a: Acct) => {
        setLearn(p => ({ ...p, [a.account_id]: { kind: 'busy' } }));
        try {
            const r = await jsend(`api/mail/accounts/${encodeURIComponent(a.account_id)}/learn-auth`, {});
            const learned = r?.learned || {};
            if (r?.saved) {
                setAccounts(prev => prev.map(x => x.account_id === a.account_id
                    ? { ...x, trusted_authserv_id: learned.authserv_id || '', auth_profile: learned.profile || 'rfc8601',
                        authserv_source: 'mailbox', authserv_samples: learned.count || 0, auth_ready: true }
                    : x));
                setLearn(p => ({ ...p, [a.account_id]: { kind: 'done', id: learned.authserv_id || '', profile: learned.profile || '', count: r.backfilled || 0 } }));
            } else {
                setLearn(p => ({ ...p, [a.account_id]: { kind: 'too_few', count: learned.total || 0 } }));
            }
        } catch {
            setLearn(p => ({ ...p, [a.account_id]: { kind: 'failed' } }));
        }
    };

    const toggleAutoSync = async (a: Acct) => {
        const next = !a.auto_sync_enabled;
        setAccounts(prev => prev.map(x => x.account_id === a.account_id ? { ...x, auto_sync_enabled: next } : x));
        try {
            await jsend(`api/mail/accounts/${encodeURIComponent(a.account_id)}`, { auto_sync_enabled: next }, 'PATCH');
        } catch {
            setAccounts(prev => prev.map(x => x.account_id === a.account_id ? { ...x, auto_sync_enabled: !next } : x));
            setError(t('accountsSaveFailed'));
        }
    };

    /** Start the shared OAuth sign-in. With `account` it reconnects that mailbox
     *  (login_hint), without it connects a new one - the SAME flow either way,
     *  which is why the panel no longer needs the setup wizard for OAuth at all. */
    const startOAuth = async (provider: string, account?: string) => {
        setBusy(account || provider);
        setError(null);
        try {
            const q = `provider=${encodeURIComponent(provider)}&imap=true`
                + (account ? `&account=${encodeURIComponent(account)}` : '');
            const d = await jfetch(`api/email/oauth/start?${q}`);
            if (d.authorization_url && typeof window !== 'undefined') {
                window.open(d.authorization_url, '_blank', 'noopener,noreferrer');
                setConnecting(account || provider);
            } else {
                setError(t('reconnectFailed'));
            }
        } catch {
            // 400 here usually means the provider has no client id configured
            // (VAF ships one for Google only), so name that instead of a generic fail.
            setError(provider.startsWith('microsoft') ? t('oauthNotConfigured') : t('reconnectFailed'));
        } finally { setBusy(null); }
    };

    // NOT an "upgrade" flow: the same sign-in that connecting an account runs,
    // which always requests the mail-engine scopes. An account predating that just
    // needs connecting once more. login_hint stops a multi-account user from
    // reconnecting whichever mailbox the browser happens to be signed in as.
    const reconnect = (a: Acct) => startOAuth(a.provider, a.email || a.account_id);

    const doRemove = async (a: Acct) => {
        setBusy(a.account_id);
        try {
            const r = await jsend(`api/mail/accounts/${encodeURIComponent(a.account_id)}`, undefined, 'DELETE');
            setAccounts(prev => prev.filter(x => x.account_id !== a.account_id));
            setConfirmDel(null);
            if (r.kept_for_calendar) setError(t('accountKeptForCalendar'));
        } catch { setError(t('accountsDeleteFailed')); }
        finally { setBusy(null); }
    };

    return (
        <div className={cn('absolute inset-0 z-20', SFC_WINDOW, 'flex flex-col text-gray-900')}>
            <div className="flex items-center justify-between px-5 py-3 border-b border-gray-200">
                <h2 className="text-sm font-semibold flex items-center gap-2">
                    <Mail className="w-4 h-4" /> {t('accountsTitle')}
                </h2>
                <button type="button" onClick={onClose} title={t('close')}
                    className={cn('p-1.5 rounded-md', SFC_HOVER)}><X className="w-4 h-4" /></button>
            </div>

            <div className="flex-1 overflow-y-auto px-5 py-4 space-y-3">
                {error && (
                    <div className="px-3 py-2 rounded-lg bg-amber-50 border border-amber-200 text-amber-600 text-[13px] flex items-center gap-2">
                        <AlertTriangle className="w-4 h-4 flex-shrink-0" />
                        <span className="flex-1">{error}</span>
                        <button type="button" onClick={() => setError(null)}><X className="w-3.5 h-3.5" /></button>
                    </div>
                )}

                {connecting && (
                    <div className="px-3 py-2 rounded-lg bg-blue-50 border border-blue-200 text-blue-700 text-[13px] flex items-center gap-2">
                        <Loader2 className="w-4 h-4 flex-shrink-0 animate-spin" />
                        <span className="flex-1">{t('reconnectWaiting')}</span>
                    </div>
                )}
                {loading ? (
                    <div className="py-10 flex justify-center"><Loader2 className="w-5 h-5 animate-spin text-gray-500" /></div>
                ) : accounts.length === 0 ? (
                    <p className="text-gray-500 text-sm py-6 text-center">{t('accountsEmpty')}</p>
                ) : accounts.map(a => {
                    const editing = a.account_id in editLabel;
                    const vs = verify[a.account_id];
                    return (
                        <div key={a.account_id} className={cn('rounded-xl border border-gray-200', SFC_CHROME, 'p-3')}>
                            <div className="flex items-center gap-3">
                                <div className="flex-1 min-w-0">
                                    <div className="flex items-center gap-2 text-sm">
                                        <span className="font-medium truncate">{a.email}</span>
                                        <span className={cn('px-1.5 rounded-md', SFC_FILL, 'text-[11px] text-gray-500 flex-shrink-0')}>
                                            {providerName(a.provider)}
                                        </span>
                                        {isImapCapable(a)
                                            ? <span className="px-1.5 rounded-md bg-green-50 text-[11px] text-green-600 flex-shrink-0">{t('imapReady')}</span>
                                            : <span className="px-1.5 rounded-md bg-amber-50 text-[11px] text-amber-600 flex-shrink-0">{t('imapNotReady')}</span>}
                                    </div>
                                    {editing ? (
                                        <div className="flex items-center gap-1 mt-1.5">
                                            <input autoFocus value={editLabel[a.account_id]}
                                                onChange={e => setEditLabel(p => ({ ...p, [a.account_id]: e.target.value }))}
                                                onKeyDown={e => { if (e.key === 'Enter') saveLabel(a); if (e.key === 'Escape') setEditLabel(p => { const n = { ...p }; delete n[a.account_id]; return n; }); }}
                                                placeholder={t('accountLabelPlaceholder')}
                                                className={cn(SFC_FILL, 'border border-gray-200 rounded-md text-xs px-2 py-1 text-gray-900 focus:outline-none focus:border-gray-400 w-40')} />
                                            <button type="button" onClick={() => saveLabel(a)} disabled={busy === a.account_id}
                                                className={cn('p-1 rounded-md', SFC_HOVER, 'text-green-600')}><Check className="w-3.5 h-3.5" /></button>
                                        </div>
                                    ) : (
                                        <button type="button" onClick={() => setEditLabel(p => ({ ...p, [a.account_id]: a.label }))}
                                            className="mt-0.5 text-xs text-gray-500 hover:text-gray-700 flex items-center gap-1">
                                            <Pencil className="w-3 h-3" /> {a.label || t('accountAddLabel')}
                                        </button>
                                    )}
                                </div>
                            </div>

                            <div className="mt-2 flex items-center gap-2 text-xs">
                                <ShieldCheck className={cn('w-3.5 h-3.5 flex-shrink-0', a.auth_ready ? 'text-green-600' : 'text-gray-500')} />
                                <span className={cn('flex-1 min-w-0', a.auth_ready ? 'text-gray-700' : 'text-gray-500')}>
                                    {a.auth_profile === 'microsoft'
                                        ? t('auth.accountMicrosoft')
                                        : a.trusted_authserv_id
                                            ? (a.authserv_source === 'provider'
                                                ? t('auth.accountProvider', { provider: a.authserv_provider || a.trusted_authserv_id })
                                                : a.authserv_source === 'mailbox'
                                                    ? t('auth.accountLearned', { id: a.trusted_authserv_id, count: a.authserv_samples || 0 })
                                                    : t('auth.accountManual', { id: a.trusted_authserv_id }))
                                            : t('auth.accountNone')}
                                </span>
                                <button type="button" onClick={() => learnAuth(a)} disabled={learn[a.account_id]?.kind === 'busy'}
                                    className={cn('text-xs px-2 py-1 rounded-md flex-shrink-0', SFC_FILL, 'border border-gray-200 hover:border-gray-400 flex items-center gap-1')}>
                                    {learn[a.account_id]?.kind === 'busy' ? <Loader2 className="w-3 h-3 animate-spin" /> : <RefreshCw className="w-3 h-3" />}
                                    <span>{t('auth.learn')}</span>
                                </button>
                            </div>
                            {(() => {
                                const st = learn[a.account_id];
                                if (!st || st.kind === 'idle' || st.kind === 'busy') return null;
                                const text = st.kind === 'done'
                                    ? (st.profile === 'microsoft' ? t('auth.learnDoneMicrosoft', { count: st.count }) : t('auth.learnDone', { id: st.id, count: st.count }))
                                    : st.kind === 'too_few' ? t('auth.learnTooFew', { count: st.count }) : t('auth.learnFailed');
                                return <div className={cn('mt-1 text-xs', st.kind === 'done' ? 'text-green-600' : 'text-amber-600')}>{text}</div>;
                            })()}

                            <div className="flex items-center gap-2 mt-2.5 flex-wrap">
                                <label className="flex items-center gap-1.5 text-xs text-gray-500 cursor-pointer">
                                    <input type="checkbox" checked={a.auto_sync_enabled} onChange={() => toggleAutoSync(a)} />
                                    {t('autoSync')}
                                </label>
                                <div className="flex-1" />
                                {!isImapCapable(a) && (a.provider === 'gmail' || a.provider === 'microsoft') && (
                                    <button type="button" onClick={() => reconnect(a)} disabled={busy === a.account_id}
                                        className="text-xs px-2 py-1 rounded-md bg-[#2b6cb0] hover:bg-[#2f7bc7] text-white flex items-center gap-1">
                                        {busy === a.account_id ? <Loader2 className="w-3 h-3 animate-spin" /> : null}
                                        {t('reconnect')}
                                    </button>
                                )}
                                <button type="button" onClick={() => doVerify(a)} disabled={vs === 'checking'}
                                    className={cn('text-xs px-2 py-1 rounded-md', SFC_FILL, 'border border-gray-200 hover:border-gray-400 flex items-center gap-1')}>
                                    {vs === 'checking' ? <Loader2 className="w-3 h-3 animate-spin" /> : <RefreshCw className="w-3 h-3" />}
                                    {vs === 'ok' ? t('verifyOk') : vs === 'fail' ? t('verifyFail') : t('verify')}
                                </button>
                                {confirmDel === a.account_id ? (
                                    <span className="flex items-center gap-1">
                                        <button type="button" onClick={() => doRemove(a)} disabled={busy === a.account_id}
                                            className="text-xs px-2 py-1 rounded-md bg-red-100 border border-red-200 text-red-600 hover:bg-red-200">
                                            {busy === a.account_id ? <Loader2 className="w-3 h-3 animate-spin" /> : t('confirmRemove')}
                                        </button>
                                        <button type="button" onClick={() => setConfirmDel(null)}
                                            className={cn('text-xs px-2 py-1 rounded-md', SFC_HOVER)}>{t('cancel')}</button>
                                    </span>
                                ) : (
                                    <button type="button" onClick={() => setConfirmDel(a.account_id)} title={t('remove')}
                                        className="text-xs px-2 py-1 rounded-md hover:bg-red-100 text-red-600 flex items-center gap-1">
                                        <Trash2 className="w-3 h-3" /> {t('remove')}
                                    </button>
                                )}
                            </div>
                        </div>
                    );
                })}

                <AddImapForm onAdded={() => { setShowAdd(false); load(); }} open={showAdd} setOpen={setShowAdd} />

                {/* Connecting a new OAuth account is the same sign-in as reconnecting
                    one, so the panel starts it itself instead of handing off to the
                    setup wizard (which the legacy teardown removes). */}
                <div className="flex gap-2">
                    {([['gmail', oauthReady.google, t('addGmail')],
                       ['microsoft', oauthReady.microsoft, t('addMicrosoft')]] as const).map(([p, ready, label]) => (
                        <button key={p} type="button" onClick={() => startOAuth(p)}
                            disabled={busy === p || !ready}
                            title={ready ? undefined : t('oauthNotConfigured')}
                            className="flex-1 text-sm px-3 py-2 rounded-xl border border-dashed border-gray-200 text-gray-500 hover:border-gray-400 hover:text-gray-700 disabled:opacity-40 disabled:hover:border-gray-200 flex items-center justify-center gap-2">
                            {busy === p ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
                            {label}
                        </button>
                    ))}
                </div>
            </div>
        </div>
    );
}

function AddImapForm({ open, setOpen, onAdded }: { open: boolean; setOpen: (v: boolean) => void; onAdded: () => void }) {
    const t = useTranslations('mailV2');
    const [email, setEmail] = useState('');
    const [password, setPassword] = useState('');
    const [label, setLabel] = useState('');
    const [advanced, setAdvanced] = useState(false);
    const [imapHost, setImapHost] = useState('');
    const [imapPort, setImapPort] = useState('');
    const [smtpHost, setSmtpHost] = useState('');
    const [smtpPort, setSmtpPort] = useState('');
    const [state, setState] = useState<'idle' | 'testing' | 'saving'>('idle');
    const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string; detail?: HintDetail } | null>(null);

    const payload = () => ({
        email: email.trim(), password,
        ...(advanced && imapHost.trim() ? { imap_host: imapHost.trim() } : {}),
        ...(advanced && imapPort.trim() ? { imap_port: Number(imapPort) } : {}),
        // SMTP too: a self-hosted server that is not in IMAP_SMTP_DEFAULTS has no
        // send host otherwise, and the account would receive but never send.
        ...(advanced && smtpHost.trim() ? { smtp_host: smtpHost.trim() } : {}),
        ...(advanced && smtpPort.trim() ? { smtp_port: Number(smtpPort) } : {}),
    });

    // The server's own words stay on the first line: they are the ground truth
    // for what happened. hint_detail turns that into something actionable, and
    // when the backend sent none (an OAuth collision, say) the plain hint is
    // appended instead of being swallowed.
    const failure = (r: LoginFail, fallback: string) => ({
        kind: 'err' as const,
        text: [r.error, r.hint_detail ? null : r.hint].filter(Boolean).join(' ') || fallback,
        detail: r.hint_detail ?? undefined,
    });

    const test = async () => {
        setState('testing'); setMsg(null);
        try {
            const r = await jsend('api/mail/accounts/test', payload());
            setMsg(r.ok ? { kind: 'ok', text: t('testOk') } : failure(r, t('testFail')));
        } catch { setMsg({ kind: 'err', text: t('testFail') }); }
        finally { setState('idle'); }
    };

    const add = async () => {
        setState('saving'); setMsg(null);
        try {
            const r = await jsend('api/mail/accounts', { ...payload(), label: label.trim() });
            if (r.ok) { setEmail(''); setPassword(''); setLabel(''); onAdded(); }
            else setMsg(failure(r, t('addFail')));
        } catch { setMsg({ kind: 'err', text: t('addFail') }); }
        finally { setState('idle'); }
    };

    if (!open) {
        return (
            <button type="button" onClick={() => setOpen(true)}
                className="w-full text-sm px-3 py-2 rounded-xl border border-dashed border-gray-200 text-gray-500 hover:border-gray-400 hover:text-gray-700 flex items-center justify-center gap-2">
                <Plus className="w-4 h-4" /> {t('addImapAccount')}
            </button>
        );
    }
    const canSubmit = email.trim() && password && state === 'idle';
    return (
        <div className={cn('rounded-xl border border-gray-200', SFC_CHROME, 'p-3 space-y-2')}>
            <div className="flex items-center justify-between">
                <span className="text-sm font-medium">{t('addImapAccount')}</span>
                <button type="button" onClick={() => setOpen(false)} className={cn('p-1 rounded-md', SFC_HOVER)}><X className="w-3.5 h-3.5" /></button>
            </div>
            <input value={email} onChange={e => setEmail(e.target.value)} placeholder={t('emailPlaceholder')} type="email"
                className={cn('w-full', SFC_FILL, 'border border-gray-200 rounded-md text-sm px-2.5 py-1.5 text-gray-900 focus:outline-none focus:border-gray-400')} />
            <input value={password} onChange={e => setPassword(e.target.value)} placeholder={t('passwordPlaceholder')} type="password"
                className={cn('w-full', SFC_FILL, 'border border-gray-200 rounded-md text-sm px-2.5 py-1.5 text-gray-900 focus:outline-none focus:border-gray-400')} />
            <input value={label} onChange={e => setLabel(e.target.value)} placeholder={t('accountLabelPlaceholder')}
                className={cn('w-full', SFC_FILL, 'border border-gray-200 rounded-md text-sm px-2.5 py-1.5 text-gray-900 focus:outline-none focus:border-gray-400')} />
            <button type="button" onClick={() => setAdvanced(v => !v)} className="text-xs text-gray-500 hover:text-gray-700 flex items-center gap-1">
                <ChevronDown className={`w-3 h-3 transition-transform ${advanced ? 'rotate-180' : ''}`} /> {t('advanced')}
            </button>
            {advanced && (
                <div className="space-y-2">
                    <div className="flex gap-2">
                        <input value={imapHost} onChange={e => setImapHost(e.target.value)} placeholder={t('imapHostPlaceholder')}
                            className={cn('flex-1', SFC_FILL, 'border border-gray-200 rounded-md text-sm px-2.5 py-1.5 text-gray-900 focus:outline-none focus:border-gray-400')} />
                        <input value={imapPort} onChange={e => setImapPort(e.target.value)} placeholder="993" inputMode="numeric"
                            className={cn('w-20', SFC_FILL, 'border border-gray-200 rounded-md text-sm px-2.5 py-1.5 text-gray-900 focus:outline-none focus:border-gray-400')} />
                    </div>
                    <div className="flex gap-2">
                        <input value={smtpHost} onChange={e => setSmtpHost(e.target.value)} placeholder={t('smtpHostPlaceholder')}
                            className={cn('flex-1', SFC_FILL, 'border border-gray-200 rounded-md text-sm px-2.5 py-1.5 text-gray-900 focus:outline-none focus:border-gray-400')} />
                        <input value={smtpPort} onChange={e => setSmtpPort(e.target.value)} placeholder="587" inputMode="numeric"
                            className={cn('w-20', SFC_FILL, 'border border-gray-200 rounded-md text-sm px-2.5 py-1.5 text-gray-900 focus:outline-none focus:border-gray-400')} />
                    </div>
                </div>
            )}
            {msg && (
                <div className={`text-xs space-y-1 ${msg.kind === 'ok' ? 'text-green-600' : 'text-red-600'}`}>
                    <p>{msg.text}</p>
                    {msg.detail && <AuthHint detail={msg.detail} />}
                </div>
            )}
            <div className="flex gap-2 pt-1">
                <button type="button" onClick={test} disabled={!canSubmit}
                    className={cn('text-sm px-3 py-1.5 rounded-md', SFC_FILL, 'border border-gray-200 hover:border-gray-400 disabled:opacity-40 flex items-center gap-1.5')}>
                    {state === 'testing' ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : null} {t('test')}
                </button>
                <button type="button" onClick={add} disabled={!canSubmit}
                    className="text-sm px-3 py-1.5 rounded-md bg-[#2b6cb0] hover:bg-[#2f7bc7] text-white disabled:opacity-40 flex items-center gap-1.5">
                    {state === 'saving' ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : null} {t('addAccount')}
                </button>
            </div>
        </div>
    );
}
