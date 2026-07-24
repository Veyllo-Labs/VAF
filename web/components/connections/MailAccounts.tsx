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
// the shared /api/email hub, so adding or upgrading an OAuth account delegates to
// the existing wizard via onAddOAuth.

import React, { useCallback, useEffect, useState } from 'react';
import { useTranslations } from 'next-intl';
import {
    AlertTriangle, Check, ChevronDown, Loader2, Mail, Pencil, Plus, RefreshCw, Trash2, X,
} from 'lucide-react';
import { getApiBase } from '@/lib/utils';

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
}

function providerName(p: string): string {
    const m: Record<string, string> = { gmail: 'Gmail', microsoft: 'Microsoft', imap: 'IMAP' };
    return m[p] || (p ? p.charAt(0).toUpperCase() + p.slice(1) : 'IMAP');
}

export function MailAccounts({ onClose, onAddOAuth }: { onClose: () => void; onAddOAuth?: () => void }) {
    const t = useTranslations('mailV2');
    const [accounts, setAccounts] = useState<Acct[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [busy, setBusy] = useState<string | null>(null);          // account_id currently acting on
    const [verify, setVerify] = useState<Record<string, 'ok' | 'fail' | 'checking'>>({});
    const [editLabel, setEditLabel] = useState<Record<string, string>>({});
    const [confirmDel, setConfirmDel] = useState<string | null>(null);
    const [showAdd, setShowAdd] = useState(false);

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
        <div className="absolute inset-0 z-20 bg-[#181818] flex flex-col">
            <div className="flex items-center justify-between px-5 py-3 border-b border-[#2e2e2e]">
                <h2 className="text-sm font-semibold flex items-center gap-2">
                    <Mail className="w-4 h-4" /> {t('accountsTitle')}
                </h2>
                <button type="button" onClick={onClose} title={t('close')}
                    className="p-1.5 rounded-md hover:bg-[#262626]"><X className="w-4 h-4" /></button>
            </div>

            <div className="flex-1 overflow-y-auto px-5 py-4 space-y-3">
                {error && (
                    <div className="px-3 py-2 rounded-lg bg-[#2b2417] border border-[#4a3b1e] text-[#d4a24e] text-[13px] flex items-center gap-2">
                        <AlertTriangle className="w-4 h-4 flex-shrink-0" />
                        <span className="flex-1">{error}</span>
                        <button type="button" onClick={() => setError(null)}><X className="w-3.5 h-3.5" /></button>
                    </div>
                )}

                {loading ? (
                    <div className="py-10 flex justify-center"><Loader2 className="w-5 h-5 animate-spin text-[#9a9a9a]" /></div>
                ) : accounts.length === 0 ? (
                    <p className="text-[#9a9a9a] text-sm py-6 text-center">{t('accountsEmpty')}</p>
                ) : accounts.map(a => {
                    const editing = a.account_id in editLabel;
                    const vs = verify[a.account_id];
                    return (
                        <div key={a.account_id} className="rounded-xl border border-[#2e2e2e] bg-[#1f1f1f] p-3">
                            <div className="flex items-center gap-3">
                                <div className="flex-1 min-w-0">
                                    <div className="flex items-center gap-2 text-sm">
                                        <span className="font-medium truncate">{a.email}</span>
                                        <span className="px-1.5 rounded-md bg-[#262626] text-[11px] text-[#9a9a9a] flex-shrink-0">
                                            {providerName(a.provider)}
                                        </span>
                                        {a.imap_ready
                                            ? <span className="px-1.5 rounded-md bg-[#17301f] text-[11px] text-[#7bbf7b] flex-shrink-0">{t('imapReady')}</span>
                                            : <span className="px-1.5 rounded-md bg-[#2b2417] text-[11px] text-[#d4a24e] flex-shrink-0">{t('imapNotReady')}</span>}
                                    </div>
                                    {editing ? (
                                        <div className="flex items-center gap-1 mt-1.5">
                                            <input autoFocus value={editLabel[a.account_id]}
                                                onChange={e => setEditLabel(p => ({ ...p, [a.account_id]: e.target.value }))}
                                                onKeyDown={e => { if (e.key === 'Enter') saveLabel(a); if (e.key === 'Escape') setEditLabel(p => { const n = { ...p }; delete n[a.account_id]; return n; }); }}
                                                placeholder={t('accountLabelPlaceholder')}
                                                className="bg-[#262626] border border-[#2e2e2e] rounded-md text-xs px-2 py-1 text-white focus:outline-none focus:border-[#444] w-40" />
                                            <button type="button" onClick={() => saveLabel(a)} disabled={busy === a.account_id}
                                                className="p-1 rounded-md hover:bg-[#262626] text-[#7bbf7b]"><Check className="w-3.5 h-3.5" /></button>
                                        </div>
                                    ) : (
                                        <button type="button" onClick={() => setEditLabel(p => ({ ...p, [a.account_id]: a.label }))}
                                            className="mt-0.5 text-xs text-[#9a9a9a] hover:text-[#c8c8c8] flex items-center gap-1">
                                            <Pencil className="w-3 h-3" /> {a.label || t('accountAddLabel')}
                                        </button>
                                    )}
                                </div>
                            </div>

                            <div className="flex items-center gap-2 mt-2.5 flex-wrap">
                                <label className="flex items-center gap-1.5 text-xs text-[#9a9a9a] cursor-pointer">
                                    <input type="checkbox" checked={a.auto_sync_enabled} onChange={() => toggleAutoSync(a)} />
                                    {t('autoSync')}
                                </label>
                                <div className="flex-1" />
                                {!a.imap_ready && (a.provider === 'gmail' || a.provider === 'microsoft') && onAddOAuth && (
                                    <button type="button" onClick={onAddOAuth}
                                        className="text-xs px-2 py-1 rounded-md bg-[#262626] border border-[#2e2e2e] hover:border-[#444]">
                                        {t('upgradeImap')}
                                    </button>
                                )}
                                <button type="button" onClick={() => doVerify(a)} disabled={vs === 'checking'}
                                    className="text-xs px-2 py-1 rounded-md bg-[#262626] border border-[#2e2e2e] hover:border-[#444] flex items-center gap-1">
                                    {vs === 'checking' ? <Loader2 className="w-3 h-3 animate-spin" /> : <RefreshCw className="w-3 h-3" />}
                                    {vs === 'ok' ? t('verifyOk') : vs === 'fail' ? t('verifyFail') : t('verify')}
                                </button>
                                {confirmDel === a.account_id ? (
                                    <span className="flex items-center gap-1">
                                        <button type="button" onClick={() => doRemove(a)} disabled={busy === a.account_id}
                                            className="text-xs px-2 py-1 rounded-md bg-[#3a1d1d] border border-[#5a2b2b] text-[#e08c8c] hover:bg-[#452020]">
                                            {busy === a.account_id ? <Loader2 className="w-3 h-3 animate-spin" /> : t('confirmRemove')}
                                        </button>
                                        <button type="button" onClick={() => setConfirmDel(null)}
                                            className="text-xs px-2 py-1 rounded-md hover:bg-[#262626]">{t('cancel')}</button>
                                    </span>
                                ) : (
                                    <button type="button" onClick={() => setConfirmDel(a.account_id)} title={t('remove')}
                                        className="text-xs px-2 py-1 rounded-md hover:bg-[#3a1d1d] text-[#e08c8c] flex items-center gap-1">
                                        <Trash2 className="w-3 h-3" /> {t('remove')}
                                    </button>
                                )}
                            </div>
                        </div>
                    );
                })}

                <AddImapForm onAdded={() => { setShowAdd(false); load(); }} open={showAdd} setOpen={setShowAdd} />

                {onAddOAuth && (
                    <button type="button" onClick={onAddOAuth}
                        className="w-full text-sm px-3 py-2 rounded-xl border border-dashed border-[#2e2e2e] text-[#9a9a9a] hover:border-[#444] hover:text-[#c8c8c8] flex items-center justify-center gap-2">
                        <Plus className="w-4 h-4" /> {t('addOAuthAccount')}
                    </button>
                )}
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
    const [state, setState] = useState<'idle' | 'testing' | 'saving'>('idle');
    const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);

    const payload = () => ({
        email: email.trim(), password,
        ...(advanced && imapHost.trim() ? { imap_host: imapHost.trim() } : {}),
        ...(advanced && imapPort.trim() ? { imap_port: Number(imapPort) } : {}),
    });

    const test = async () => {
        setState('testing'); setMsg(null);
        try {
            const r = await jsend('api/mail/accounts/test', payload());
            setMsg(r.ok ? { kind: 'ok', text: t('testOk') } : { kind: 'err', text: r.hint || r.error || t('testFail') });
        } catch { setMsg({ kind: 'err', text: t('testFail') }); }
        finally { setState('idle'); }
    };

    const add = async () => {
        setState('saving'); setMsg(null);
        try {
            const r = await jsend('api/mail/accounts', { ...payload(), label: label.trim() });
            if (r.ok) { setEmail(''); setPassword(''); setLabel(''); onAdded(); }
            else setMsg({ kind: 'err', text: r.hint || r.error || t('addFail') });
        } catch { setMsg({ kind: 'err', text: t('addFail') }); }
        finally { setState('idle'); }
    };

    if (!open) {
        return (
            <button type="button" onClick={() => setOpen(true)}
                className="w-full text-sm px-3 py-2 rounded-xl border border-dashed border-[#2e2e2e] text-[#9a9a9a] hover:border-[#444] hover:text-[#c8c8c8] flex items-center justify-center gap-2">
                <Plus className="w-4 h-4" /> {t('addImapAccount')}
            </button>
        );
    }
    const canSubmit = email.trim() && password && state === 'idle';
    return (
        <div className="rounded-xl border border-[#2e2e2e] bg-[#1f1f1f] p-3 space-y-2">
            <div className="flex items-center justify-between">
                <span className="text-sm font-medium">{t('addImapAccount')}</span>
                <button type="button" onClick={() => setOpen(false)} className="p-1 rounded-md hover:bg-[#262626]"><X className="w-3.5 h-3.5" /></button>
            </div>
            <input value={email} onChange={e => setEmail(e.target.value)} placeholder={t('emailPlaceholder')} type="email"
                className="w-full bg-[#262626] border border-[#2e2e2e] rounded-md text-sm px-2.5 py-1.5 text-white focus:outline-none focus:border-[#444]" />
            <input value={password} onChange={e => setPassword(e.target.value)} placeholder={t('passwordPlaceholder')} type="password"
                className="w-full bg-[#262626] border border-[#2e2e2e] rounded-md text-sm px-2.5 py-1.5 text-white focus:outline-none focus:border-[#444]" />
            <input value={label} onChange={e => setLabel(e.target.value)} placeholder={t('accountLabelPlaceholder')}
                className="w-full bg-[#262626] border border-[#2e2e2e] rounded-md text-sm px-2.5 py-1.5 text-white focus:outline-none focus:border-[#444]" />
            <button type="button" onClick={() => setAdvanced(v => !v)} className="text-xs text-[#9a9a9a] hover:text-[#c8c8c8] flex items-center gap-1">
                <ChevronDown className={`w-3 h-3 transition-transform ${advanced ? 'rotate-180' : ''}`} /> {t('advanced')}
            </button>
            {advanced && (
                <div className="flex gap-2">
                    <input value={imapHost} onChange={e => setImapHost(e.target.value)} placeholder={t('imapHostPlaceholder')}
                        className="flex-1 bg-[#262626] border border-[#2e2e2e] rounded-md text-sm px-2.5 py-1.5 text-white focus:outline-none focus:border-[#444]" />
                    <input value={imapPort} onChange={e => setImapPort(e.target.value)} placeholder="993" inputMode="numeric"
                        className="w-20 bg-[#262626] border border-[#2e2e2e] rounded-md text-sm px-2.5 py-1.5 text-white focus:outline-none focus:border-[#444]" />
                </div>
            )}
            {msg && (
                <p className={`text-xs ${msg.kind === 'ok' ? 'text-[#7bbf7b]' : 'text-[#e08c8c]'}`}>{msg.text}</p>
            )}
            <div className="flex gap-2 pt-1">
                <button type="button" onClick={test} disabled={!canSubmit}
                    className="text-sm px-3 py-1.5 rounded-md bg-[#262626] border border-[#2e2e2e] hover:border-[#444] disabled:opacity-40 flex items-center gap-1.5">
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
