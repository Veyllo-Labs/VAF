'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
//
// Mail client page (engine v2). Design: EMAIL_CLIENT.md. Three-pane layout,
// conversation view, sanitized HTML in a sandboxed iframe (CSP script-src
// 'none'), remote images blocked with an explicit banner, local-first write
// actions (read/star/archive/trash) that replay to the server via the op
// queue, compose with reply/reply-all/forward prefill and undo-send.
// Strings come from the mailV2 next-intl catalog.

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import {
    AlertTriangle, Archive, ChevronRight, CornerUpLeft, CornerUpRight, Inbox, Loader2, Mail,
    MailOpen, Paperclip, PenSquare, RefreshCw, Reply, ReplyAll, Search, ShieldCheck, Star,
    Trash2, X,
} from 'lucide-react';
import { cn, getApiBase } from '@/lib/utils';

const api = (p: string) => `${getApiBase()}${p.startsWith('/') ? p : `/${p}`}`;
const jfetch = async (p: string, init?: RequestInit) => {
    const r = await fetch(api(p), { credentials: 'include', ...init });
    if (!r.ok) throw new Error(`${r.status}`);
    return r.json();
};
const jpost = (p: string, body?: unknown, method = 'POST') => jfetch(p, {
    method, headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
});

interface Account { account_id: string; provider: string; email: string; last_sync_at?: string }
interface Folder { id: number; name: string; special_use?: string }
interface ThreadRow {
    thread_id: number; message_count: number; unread_count: number; last_date_ts?: number;
    acct: string; newest_pk: number; subject: string; from_addr: string; snippet: string;
    has_attachments: number; flags: string[];
}
interface Msg {
    id: number; subject: string; from_addr: string; to_addrs: string; date_ts?: number;
    internaldate_ts?: number; snippet: string; flags: string[]; folder_name: string;
    has_attachments: number;
}
interface Body {
    html: string | null; text: string; blocked_remote: number; cached: boolean;
    attachments: { id: number; part_id: string; filename?: string; content_type?: string; size_bytes?: number; is_inline: number }[];
}
interface Prefill { account_id: string; to: string; cc: string; subject: string; body: string; in_reply_to: string; references: string }

const SPECIAL_KEYS: Record<string, string> = {
    '\\Inbox': 'inbox', '\\Sent': 'sent', '\\Drafts': 'drafts',
    '\\Trash': 'trash', '\\Junk': 'junk', '\\Archive': 'archive', '\\All': 'all',
};

function fmtWhen(ts?: number): string {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    const now = new Date();
    if (d.toDateString() === now.toDateString()) {
        return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }
    if (now.getTime() - d.getTime() < 6 * 86400_000) {
        return d.toLocaleDateString([], { weekday: 'short' });
    }
    return d.toLocaleDateString([], { day: '2-digit', month: '2-digit', year: '2-digit' });
}

function fmtSize(n?: number): string {
    if (!n) return '';
    if (n > 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
    return `${Math.max(1, Math.round(n / 1024))} KB`;
}

/** Sanitized HTML in a sandboxed iframe: content is nh3-cleaned server-side,
 * the iframe adds sandbox + CSP script-src 'none' (defense in layers,
 * Close.com pattern). allow-same-origin only for height measurement. */
function BodyFrame({ html }: { html: string }) {
    const ref = useRef<HTMLIFrameElement>(null);
    const [height, setHeight] = useState(320);
    const doc = useMemo(() =>
        `<!DOCTYPE html><html><head><meta charset="utf-8">` +
        `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data: ${typeof window !== 'undefined' ? window.location.origin : ''}; style-src 'unsafe-inline'">` +
        `<base target="_blank">` +
        `<style>body{font:14px/1.5 system-ui,sans-serif;color:#222;background:#fff;margin:16px;word-break:break-word}` +
        `img{max-width:100%}blockquote{border-left:3px solid #ccc;margin-left:0;padding-left:10px;color:#666}</style>` +
        `</head><body>${html}</body></html>`, [html]);
    const measure = useCallback(() => {
        try {
            const h = ref.current?.contentDocument?.body?.scrollHeight;
            if (h && h > 40) setHeight(Math.min(h + 40, 20000));
        } catch { /* sandboxed */ }
    }, []);
    return (
        <iframe ref={ref} title="message" srcDoc={doc} onLoad={measure} style={{ height }}
            sandbox="allow-same-origin allow-popups allow-popups-to-escape-sandbox"
            className="w-full rounded-lg bg-white border-0" />
    );
}

function ComposeModal({ prefill, accounts, onClose, onQueued }: {
    prefill: Partial<Prefill> | null; accounts: Account[];
    onClose: () => void; onQueued: (opId: number, undoUntil: number) => void;
}) {
    const t = useTranslations('mailV2');
    const [account, setAccount] = useState(prefill?.account_id || accounts[0]?.account_id || '');
    const [to, setTo] = useState(prefill?.to || '');
    const [cc, setCc] = useState(prefill?.cc || '');
    const [subject, setSubject] = useState(prefill?.subject || '');
    const [body, setBody] = useState(prefill?.body || '');
    const [sending, setSending] = useState(false);
    const [error, setError] = useState('');
    const send = useCallback(async () => {
        setSending(true);
        setError('');
        try {
            const out = await jpost('api/mail/send', {
                account_id: account, to, cc, subject, body,
                in_reply_to: prefill?.in_reply_to || '', references: prefill?.references || '',
                undo_seconds: 15,
            });
            onQueued(out.op_id, out.undo_until_ts);
            onClose();
        } catch { setError(t('compose.sendFailed')); }
        finally { setSending(false); }
    }, [account, to, cc, subject, body, prefill, onClose, onQueued, t]);
    return (
        <div className="fixed inset-0 z-50 bg-black/50 grid place-items-center p-4">
            <div className="w-full max-w-2xl bg-[#1f1f1f] border border-[#2e2e2e] rounded-xl shadow-2xl flex flex-col max-h-[90vh]">
                <div className="flex items-center justify-between px-4 py-3 border-b border-[#2e2e2e]">
                    <h2 className="font-semibold text-[15px]">{t('compose.title')}</h2>
                    <button type="button" onClick={onClose} className="text-[#9a9a9a] hover:text-white"><X className="w-4 h-4" /></button>
                </div>
                <div className="p-4 space-y-2.5 overflow-y-auto">
                    {accounts.length > 1 && (
                        <select value={account} onChange={e => setAccount(e.target.value)}
                            className="w-full bg-[#262626] border border-[#2e2e2e] rounded-lg px-3 py-1.5 text-sm">
                            {accounts.map(a => <option key={a.account_id} value={a.account_id}>{a.email}</option>)}
                        </select>
                    )}
                    <input value={to} onChange={e => setTo(e.target.value)} placeholder={t('compose.to')}
                        className="w-full bg-[#262626] border border-[#2e2e2e] rounded-lg px-3 py-1.5 text-sm outline-none focus:border-[#444]" />
                    <input value={cc} onChange={e => setCc(e.target.value)} placeholder={t('compose.cc')}
                        className="w-full bg-[#262626] border border-[#2e2e2e] rounded-lg px-3 py-1.5 text-sm outline-none focus:border-[#444]" />
                    <input value={subject} onChange={e => setSubject(e.target.value)} placeholder={t('compose.subject')}
                        className="w-full bg-[#262626] border border-[#2e2e2e] rounded-lg px-3 py-1.5 text-sm outline-none focus:border-[#444]" />
                    <textarea value={body} onChange={e => setBody(e.target.value)} placeholder={t('compose.body')} rows={12}
                        className="w-full bg-[#262626] border border-[#2e2e2e] rounded-lg px-3 py-2 text-sm outline-none focus:border-[#444] font-mono" />
                    {error && <div className="text-[#e08c8c] text-sm">{error}</div>}
                </div>
                <div className="flex justify-end gap-2 px-4 py-3 border-t border-[#2e2e2e]">
                    <button type="button" onClick={onClose}
                        className="px-3 py-1.5 rounded-lg text-sm text-[#9a9a9a] hover:text-white">{t('compose.cancel')}</button>
                    <button type="button" onClick={send} disabled={sending || !to.trim() || !account}
                        className="px-4 py-1.5 rounded-lg text-sm bg-[#e05d44] text-white disabled:opacity-50">
                        {sending ? t('compose.sending') : t('compose.send')}
                    </button>
                </div>
            </div>
        </div>
    );
}

function MessageView({ msg, expanded, onToggle }: { msg: Msg; expanded: boolean; onToggle: () => void }) {
    const t = useTranslations('mailV2');
    const [body, setBody] = useState<Body | null>(null);
    const [loading, setLoading] = useState(false);
    const [allowRemote, setAllowRemote] = useState(false);
    useEffect(() => {
        if (!expanded) return;
        if (body && !allowRemote) return;
        if (body && allowRemote && body.blocked_remote === 0) return;
        setLoading(true);
        jfetch(`api/mail/messages/${msg.id}/body${allowRemote ? '?allow_remote=true' : ''}`)
            .then(setBody).catch(() => setBody(null)).finally(() => setLoading(false));
    }, [expanded, body, allowRemote, msg.id]);

    if (!expanded) {
        return (
            <button type="button" onClick={onToggle}
                className="w-full text-left px-5 py-2.5 border-b border-[#2e2e2e] text-[#9a9a9a] text-sm hover:bg-[#1f1f1f] flex items-center gap-2">
                <ChevronRight className="w-3.5 h-3.5" />
                <span className="font-medium text-[#c8c8c8] truncate max-w-[220px]">{msg.from_addr.split('<')[0].trim() || msg.from_addr}</span>
                <span className="truncate flex-1">{msg.snippet}</span>
                <span className="flex-shrink-0">{fmtWhen(msg.date_ts || msg.internaldate_ts)}</span>
            </button>
        );
    }
    const regularAtts = (body?.attachments || []).filter(a => !a.is_inline);
    return (
        <div className="border-b border-[#2e2e2e]">
            <button type="button" onClick={onToggle} className="w-full text-left px-5 pt-4 pb-1">
                <div className="text-sm">
                    <span className="font-semibold">{msg.from_addr.split('<')[0].trim() || msg.from_addr}</span>
                    <span className="text-[#9a9a9a]"> · {t('toMe', { name: msg.to_addrs.split('<')[0].trim() || '' })} · {fmtWhen(msg.date_ts || msg.internaldate_ts)} · {msg.folder_name}</span>
                </div>
            </button>
            {loading && <div className="px-5 py-6"><Loader2 className="w-5 h-5 animate-spin text-[#9a9a9a]" /></div>}
            {body && body.blocked_remote > 0 && (
                <div className="mx-5 my-2 px-3 py-2 rounded-lg bg-[#2b2417] border border-[#4a3b1e] text-[#d4a24e] text-[13px] flex items-center gap-2">
                    <ShieldCheck className="w-4 h-4 flex-shrink-0" />
                    <span className="flex-1">{t('remoteBlocked', { count: body.blocked_remote })}</span>
                    <button type="button" onClick={() => setAllowRemote(true)}
                        className="px-2 py-1 rounded-md border border-[#4a3b1e] hover:bg-[#332a1a] flex-shrink-0">
                        {t('loadImages')}
                    </button>
                </div>
            )}
            {body && !body.cached && (
                <div className="mx-5 my-2 px-3 py-2 rounded-lg bg-[#262626] text-[#9a9a9a] text-[13px]">{t('notCached')}</div>
            )}
            <div className="px-5 pb-3 pt-1">
                {body?.html
                    ? <BodyFrame html={body.html} />
                    : <pre className="whitespace-pre-wrap font-sans text-sm text-[#e8e8e8] bg-[#1f1f1f] rounded-lg p-4">{body?.text || msg.snippet || t('noContent')}</pre>}
            </div>
            {regularAtts.length > 0 && (
                <div className="flex flex-wrap gap-2 px-5 pb-4">
                    {regularAtts.map(a => (
                        <a key={a.id} href={api(`api/mail/messages/${msg.id}/parts/${a.part_id}`)}
                            className="flex items-center gap-1.5 text-xs px-2.5 py-1.5 rounded-lg bg-[#262626] border border-[#2e2e2e] hover:border-[#444]">
                            <Paperclip className="w-3.5 h-3.5" />
                            {a.filename || t('attachment')} {a.size_bytes ? `· ${fmtSize(a.size_bytes)}` : ''}
                        </a>
                    ))}
                </div>
            )}
        </div>
    );
}

export default function MailPage() {
    const t = useTranslations('mailV2');
    const [status, setStatus] = useState<{ v2_enabled: boolean; accounts?: Account[] } | null>(null);
    const [folders, setFolders] = useState<Record<string, Folder[]>>({});
    const [sel, setSel] = useState<{ account: string | null; folder: string }>({ account: null, folder: 'INBOX' });
    const [threads, setThreads] = useState<ThreadRow[]>([]);
    const [listLoading, setListLoading] = useState(false);
    const [activeThread, setActiveThread] = useState<number | null>(null);
    const [threadMsgs, setThreadMsgs] = useState<Msg[]>([]);
    const [expandedIds, setExpandedIds] = useState<Set<number>>(new Set());
    const [showOlder, setShowOlder] = useState(false);
    const [query, setQuery] = useState('');
    const [searchRows, setSearchRows] = useState<Msg[] | null>(null);
    const [syncing, setSyncing] = useState(false);
    const [error, setError] = useState('');
    const [compose, setCompose] = useState<Partial<Prefill> | null | false>(false);
    const [undoState, setUndoState] = useState<{ opId: number; until: number } | null>(null);

    const loadStatus = useCallback(async () => {
        try {
            const s = await jfetch('api/mail/status');
            setStatus(s);
            for (const a of s.accounts || []) {
                jfetch(`api/mail/folders?account_id=${encodeURIComponent(a.account_id)}`)
                    .then(f => setFolders(prev => ({ ...prev, [a.account_id]: f.folders })))
                    .catch(() => undefined);
            }
        } catch { setStatus({ v2_enabled: false }); }
    }, []);
    useEffect(() => { loadStatus(); }, [loadStatus]);

    const loadThreads = useCallback(async () => {
        setListLoading(true);
        setSearchRows(null);
        try {
            const params = new URLSearchParams({ folder: sel.folder, limit: '50' });
            if (sel.account) params.set('account_id', sel.account);
            const data = await jfetch(`api/mail/threads?${params}`);
            setThreads(data.threads || []);
            setError('');
        } catch { setError(t('loadError')); }
        finally { setListLoading(false); }
    }, [sel, t]);
    useEffect(() => { if (status?.v2_enabled) loadThreads(); }, [status?.v2_enabled, loadThreads]);
    // light refresh so new mail appears without manual sync (WS deltas: phase 2.5)
    useEffect(() => {
        if (!status?.v2_enabled) return;
        const timer = setInterval(loadThreads, 60_000);
        return () => clearInterval(timer);
    }, [status?.v2_enabled, loadThreads]);

    const openThread = useCallback(async (row: ThreadRow) => {
        setActiveThread(row.thread_id);
        setShowOlder(false);
        try {
            const data = await jfetch(`api/mail/threads/${row.thread_id}`);
            const msgs: Msg[] = data.messages || [];
            setThreadMsgs(msgs);
            const unread = msgs.filter(m => !m.flags.includes('\\Seen'));
            const initial = new Set<number>(unread.length ? unread.map(m => m.id) : msgs.slice(-1).map(m => m.id));
            setExpandedIds(initial);
            // local-first read marking; server replay rides the op queue
            for (const m of unread) {
                jpost(`api/mail/messages/${m.id}/flags`, { read: true }, 'PATCH').catch(() => undefined);
            }
            if (unread.length) {
                setThreads(prev => prev.map(tr => tr.thread_id === row.thread_id ? { ...tr, unread_count: 0 } : tr));
            }
        } catch { setThreadMsgs([]); }
    }, []);

    const threadAction = useCallback(async (row: ThreadRow, action: 'archive' | 'trash') => {
        setThreads(prev => prev.filter(tr => tr.thread_id !== row.thread_id));
        if (activeThread === row.thread_id) { setActiveThread(null); setThreadMsgs([]); }
        try {
            await jpost(`api/mail/messages/${row.newest_pk}/${action}`);
        } catch { setError(t('actionFailed')); loadThreads(); }
    }, [activeThread, loadThreads, t]);

    const openCompose = useCallback(async (mode: 'new' | 'reply' | 'replyAll' | 'forward') => {
        if (mode === 'new') { setCompose(null); return; }
        const newest = threadMsgs[threadMsgs.length - 1];
        if (!newest) return;
        try {
            const params = mode === 'forward' ? 'forward=true' : (mode === 'replyAll' ? 'reply_all=true' : '');
            const pre = await jfetch(`api/mail/messages/${newest.id}/reply-prefill?${params}`);
            setCompose(pre);
        } catch { setError(t('actionFailed')); }
    }, [threadMsgs, t]);

    const runSearch = useCallback(async () => {
        const q = query.trim();
        if (!q) { setSearchRows(null); return; }
        setListLoading(true);
        try {
            const params = new URLSearchParams({ q, limit: '50' });
            if (sel.account) params.set('account_id', sel.account);
            const data = await jfetch(`api/mail/search?${params}`);
            setSearchRows(data.messages || []);
        } catch { setError(t('searchError')); }
        finally { setListLoading(false); }
    }, [query, sel.account, t]);

    const runSync = useCallback(async () => {
        const accounts = status?.accounts || [];
        if (!accounts.length) return;
        setSyncing(true);
        try {
            for (const a of (sel.account ? accounts.filter(x => x.account_id === sel.account) : accounts)) {
                await jpost(`api/mail/sync/${encodeURIComponent(a.account_id)}`);
            }
            await loadThreads();
            await loadStatus();
        } catch { setError(t('syncError')); }
        finally { setSyncing(false); }
    }, [status?.accounts, sel.account, loadThreads, loadStatus, t]);

    const undoSend = useCallback(async () => {
        if (!undoState) return;
        try {
            await jpost(`api/mail/send/${undoState.opId}`, undefined, 'DELETE');
            setUndoState(null);
        } catch { setUndoState(null); }
    }, [undoState]);
    useEffect(() => {
        if (!undoState) return;
        const timer = setTimeout(() => setUndoState(null),
            Math.max(1000, undoState.until * 1000 - Date.now()));
        return () => clearTimeout(timer);
    }, [undoState]);

    if (status === null) {
        return <div className="h-screen grid place-items-center bg-[#181818] text-[#9a9a9a]"><Loader2 className="w-6 h-6 animate-spin" /></div>;
    }
    if (!status.v2_enabled) {
        return (
            <div className="h-screen grid place-items-center bg-[#181818] text-[#e8e8e8]">
                <div className="max-w-md text-center space-y-3 p-6">
                    <Mail className="w-10 h-10 mx-auto text-[#e05d44]" />
                    <h1 className="text-lg font-semibold">{t('flagOffTitle')}</h1>
                    <p className="text-sm text-[#9a9a9a]">{t('flagOffBody')}</p>
                </div>
            </div>
        );
    }

    const visibleMsgs = showOlder ? threadMsgs : threadMsgs.slice(Math.max(0, threadMsgs.length - 6));
    const hiddenCount = threadMsgs.length - visibleMsgs.length;
    const newestMsg = threadMsgs[threadMsgs.length - 1];

    return (
        <div className="h-screen flex flex-col bg-[#181818] text-[#e8e8e8]">
            <header className="flex items-center gap-3 px-4 py-2.5 border-b border-[#2e2e2e] bg-[#1f1f1f]">
                <div className="w-8 h-8 rounded-lg bg-[#e05d44] grid place-items-center"><Mail className="w-4 h-4 text-white" /></div>
                <h1 className="font-semibold text-[15px]">{t('title')}</h1>
                <span className="text-xs text-[#9a9a9a]">{t('engineBadge', { count: (status.accounts || []).length })}</span>
                <button type="button" onClick={() => openCompose('new')}
                    className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-[#e05d44] text-white text-sm">
                    <PenSquare className="w-4 h-4" /> {t('compose.title')}
                </button>
                <div className="flex-1 max-w-xl ml-auto flex gap-2">
                    <div className="flex-1 relative">
                        <Search className="w-4 h-4 absolute left-3 top-2.5 text-[#9a9a9a]" />
                        <input value={query} onChange={e => setQuery(e.target.value)}
                            onKeyDown={e => { if (e.key === 'Enter') runSearch(); if (e.key === 'Escape') { setQuery(''); setSearchRows(null); } }}
                            placeholder={t('searchPlaceholder')}
                            className="w-full bg-[#262626] border border-[#2e2e2e] rounded-lg pl-9 pr-3 py-1.5 text-sm outline-none focus:border-[#444]" />
                    </div>
                    <button type="button" onClick={runSync} disabled={syncing}
                        className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-[#262626] border border-[#2e2e2e] text-sm hover:border-[#444] disabled:opacity-50">
                        <RefreshCw className={cn('w-4 h-4', syncing && 'animate-spin')} /> {t('sync')}
                    </button>
                </div>
            </header>

            <main className="flex-1 grid min-h-0" style={{ gridTemplateColumns: '220px 380px 1fr' }}>
                <nav className="border-r border-[#2e2e2e] bg-[#1f1f1f] overflow-y-auto p-2">
                    <button type="button" onClick={() => setSel({ account: null, folder: 'INBOX' })}
                        className={cn('w-full text-left flex items-center gap-2 px-3 py-2 rounded-lg text-sm',
                            sel.account === null ? 'bg-[#2a2a2a] font-semibold' : 'hover:bg-[#262626]')}>
                        <Inbox className="w-4 h-4" /> {t('allInboxes')}
                    </button>
                    {(status.accounts || []).map(a => (
                        <div key={a.account_id} className="mt-3">
                            <div className="px-3 text-xs text-[#9a9a9a] truncate">{a.email} ({a.provider})</div>
                            {(folders[a.account_id] || [{ id: 0, name: 'INBOX', special_use: '\\Inbox' } as Folder]).map(f => (
                                <button key={`${a.account_id}:${f.name}`} type="button"
                                    onClick={() => setSel({ account: a.account_id, folder: f.name })}
                                    className={cn('w-full text-left px-3 py-1.5 rounded-lg text-sm truncate',
                                        sel.account === a.account_id && sel.folder === f.name
                                            ? 'bg-[#2a2a2a] font-semibold' : 'hover:bg-[#262626]')}>
                                    {SPECIAL_KEYS[f.special_use || ''] ? t(`folders.${SPECIAL_KEYS[f.special_use || '']}`) : f.name}
                                </button>
                            ))}
                        </div>
                    ))}
                </nav>

                <section className="border-r border-[#2e2e2e] overflow-y-auto">
                    {error && (
                        <div className="m-3 px-3 py-2 rounded-lg bg-[#2b1a1a] border border-[#4a2222] text-[#e08c8c] text-[13px] flex items-center gap-2">
                            <AlertTriangle className="w-4 h-4" /> {error}
                        </div>
                    )}
                    {listLoading && <div className="p-6 grid place-items-center"><Loader2 className="w-5 h-5 animate-spin text-[#9a9a9a]" /></div>}
                    {!listLoading && searchRows !== null && (
                        searchRows.length === 0
                            ? <div className="p-6 text-sm text-[#9a9a9a]">{t('noResults')}</div>
                            : searchRows.map(m => (
                                <button key={m.id} type="button"
                                    onClick={() => { setActiveThread(null); setThreadMsgs([m]); setExpandedIds(new Set([m.id])); }}
                                    className="w-full text-left px-4 py-2.5 border-b border-[#2e2e2e] hover:bg-[#1f1f1f]">
                                    <div className="flex justify-between gap-2 text-[13px]">
                                        <span className="font-semibold truncate">{m.from_addr.split('<')[0].trim() || m.from_addr}</span>
                                        <span className="text-[#9a9a9a] flex-shrink-0">{fmtWhen(m.date_ts || m.internaldate_ts)}</span>
                                    </div>
                                    <div className="text-[13px] truncate">{m.subject || t('noSubject')}</div>
                                    <div className="text-xs text-[#9a9a9a] truncate">{m.snippet}</div>
                                </button>
                            ))
                    )}
                    {!listLoading && searchRows === null && threads.length === 0 && (
                        <div className="p-6 text-sm text-[#9a9a9a]">{t('noMessages')}</div>
                    )}
                    {!listLoading && searchRows === null && threads.map(row => (
                        <div key={row.thread_id}
                            className={cn('relative border-b border-[#2e2e2e] group',
                                activeThread === row.thread_id ? 'bg-[#2a2a2a]' : 'hover:bg-[#1f1f1f]')}>
                            <button type="button" onClick={() => openThread(row)} className="w-full text-left px-4 py-2.5">
                                {row.unread_count > 0 && <span className="absolute left-1.5 top-4 w-2 h-2 rounded-full bg-[#e05d44]" />}
                                <div className="flex justify-between gap-2 text-[13px]">
                                    <span className={cn('truncate', row.unread_count > 0 ? 'font-bold text-white' : 'font-semibold')}>
                                        {row.from_addr.split('<')[0].trim() || row.from_addr}
                                    </span>
                                    <span className="text-[#9a9a9a] flex-shrink-0">{fmtWhen(row.last_date_ts)}</span>
                                </div>
                                <div className={cn('text-[13px] truncate', row.unread_count > 0 && 'text-white')}>{row.subject || t('noSubject')}</div>
                                <div className="text-xs text-[#9a9a9a] truncate pr-14">{row.snippet}</div>
                            </button>
                            <div className="absolute right-3 bottom-2 flex items-center gap-1.5 text-[11px] text-[#9a9a9a] group-hover:hidden">
                                {row.has_attachments ? <Paperclip className="w-3 h-3" /> : null}
                                {row.message_count > 1 && (
                                    <span className={cn('px-1.5 rounded-md', row.unread_count > 0 ? 'bg-[#e05d44] text-white' : 'bg-[#262626]')}>
                                        {row.message_count}
                                    </span>
                                )}
                            </div>
                            <div className="absolute right-2 top-1/2 -translate-y-1/2 hidden group-hover:flex items-center gap-1">
                                <button type="button" title={t('archive')} onClick={() => threadAction(row, 'archive')}
                                    className="p-1.5 rounded-md bg-[#262626] border border-[#2e2e2e] hover:border-[#444]">
                                    <Archive className="w-3.5 h-3.5" />
                                </button>
                                <button type="button" title={t('trash')} onClick={() => threadAction(row, 'trash')}
                                    className="p-1.5 rounded-md bg-[#262626] border border-[#2e2e2e] hover:border-[#444]">
                                    <Trash2 className="w-3.5 h-3.5" />
                                </button>
                            </div>
                        </div>
                    ))}
                </section>

                <section className="overflow-y-auto">
                    {threadMsgs.length === 0 && (
                        <div className="h-full grid place-items-center text-[#9a9a9a] text-sm">{t('selectConversation')}</div>
                    )}
                    {threadMsgs.length > 0 && (
                        <>
                            <div className="px-5 pt-4 pb-2 border-b border-[#2e2e2e] flex items-center gap-2">
                                <h2 className="text-[17px] font-semibold flex-1 truncate">{newestMsg?.subject || t('noSubject')}</h2>
                                <button type="button" title={t('reply')} onClick={() => openCompose('reply')}
                                    className="p-2 rounded-lg bg-[#262626] border border-[#2e2e2e] hover:border-[#444]"><Reply className="w-4 h-4" /></button>
                                <button type="button" title={t('replyAll')} onClick={() => openCompose('replyAll')}
                                    className="p-2 rounded-lg bg-[#262626] border border-[#2e2e2e] hover:border-[#444]"><ReplyAll className="w-4 h-4" /></button>
                                <button type="button" title={t('forward')} onClick={() => openCompose('forward')}
                                    className="p-2 rounded-lg bg-[#262626] border border-[#2e2e2e] hover:border-[#444]"><CornerUpRight className="w-4 h-4" /></button>
                            </div>
                            {hiddenCount > 0 && !showOlder && (
                                <button type="button" onClick={() => setShowOlder(true)}
                                    className="w-full text-left px-5 py-2.5 text-sm text-[#9a9a9a] border-b border-[#2e2e2e] hover:bg-[#1f1f1f]">
                                    ▸ {t('showOlder', { count: hiddenCount })}
                                </button>
                            )}
                            {visibleMsgs.map(m => (
                                <MessageView key={m.id} msg={m}
                                    expanded={expandedIds.has(m.id)}
                                    onToggle={() => setExpandedIds(prev => {
                                        const next = new Set(prev);
                                        if (next.has(m.id)) next.delete(m.id); else next.add(m.id);
                                        return next;
                                    })} />
                            ))}
                        </>
                    )}
                </section>
            </main>

            {compose !== false && (
                <ComposeModal prefill={compose} accounts={status.accounts || []}
                    onClose={() => setCompose(false)}
                    onQueued={(opId, until) => setUndoState({ opId, until })} />
            )}
            {undoState && (
                <div className="fixed bottom-5 left-1/2 -translate-x-1/2 z-50 flex items-center gap-3 px-4 py-2.5 rounded-xl bg-[#262626] border border-[#2e2e2e] shadow-xl text-sm">
                    <MailOpen className="w-4 h-4 text-[#9a9a9a]" />
                    {t('undoSent')}
                    <button type="button" onClick={undoSend}
                        className="flex items-center gap-1 text-[#d4a24e] font-medium hover:underline">
                        <CornerUpLeft className="w-3.5 h-3.5" /> {t('undo')}
                    </button>
                </div>
            )}
        </div>
    );
}
