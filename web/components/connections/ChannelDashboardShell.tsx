'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
//
// The one window every messaging channel dashboard is built on, laid out like the
// mail client: chats on the left, the active conversation in the middle, a gear in
// the header that opens a settings overlay. A channel hands in its chats (already
// judged: label, preview, badge, what the agent does with that chat), where the
// conversation history comes from, and the cards for its settings. Everything the
// three channels used to copy from each other (list, bubbles, in-chat search, day
// separators, Memory Learning counter, keyboard handling) lives here once.

import React, { useState, useEffect, useMemo, useRef } from 'react';
import { useTranslations } from 'next-intl';
import { X, Search, RefreshCw, Settings, ChevronUp, ChevronDown, Bot, User } from 'lucide-react';
import { cn, stripThinkBlocks } from '@/lib/utils';
import { useEscapeLayer } from '@/hooks/useEscapeLayer';
import HighlightedText from './HighlightedText';

const api = (path: string) => path.startsWith('/') ? path : `/${path}`;

export interface ShellBadge {
    label: string;
    cls: string;
}

export interface ShellChat {
    id: string;
    /** What the conversation pane loads through `historyUrl` (a session id, a chat id); null when there is nothing to show. */
    historyKey?: string | null;
    label: string;
    /** Profile picture, when the channel can show one; the initials stay as the fallback. */
    avatarUrl?: string | null;
    preview?: string;
    ts?: number;
    badge: ShellBadge;
    /** One line under the name in the conversation header: number, what the agent does here. */
    subline: string;
    /** The footer line of the conversation: which mode answers in this chat. */
    footer: string;
}

export interface ChannelDashboardShellProps {
    isOpen: boolean;
    onClose: () => void;
    icon: React.ReactNode;
    iconClass: string;
    title: string;
    subtitle?: React.ReactNode;
    dot: 'green' | 'amber' | 'gray';
    dotTitle?: string;
    chats: ShellChat[];
    loading: boolean;
    loadFailed: boolean;
    onRefresh: () => void;
    historyUrl: (historyKey: string) => string;
    /** Bump to reload the conversation without changing the selected chat (older messages arrived). */
    historyVersion?: number;
    selectedId: string | null;
    onSelect: (id: string | null) => void;
    banner?: React.ReactNode;
    /** Extra controls in the conversation header for the selected chat (assign a number, add as contact). */
    conversationExtra?: (chat: ShellChat) => React.ReactNode;
    conversationNote?: string | null;
    settingsTitle: string;
    settingsContent: React.ReactNode;
    settingsOpen: boolean;
    onSettingsOpenChange: (open: boolean) => void;
}

export const BADGE_CLS = {
    owner: 'bg-[#1d3550] text-[#8ec3f0]',
    contact: 'bg-[#1e3a24] text-[#8fd39a]',
    conversation: 'bg-[#3a2f16] text-[#e0b866]',
    assign: 'bg-[#2b2417] text-[#d4a24e]',
    readOnly: 'bg-[#262626] text-[#b0b0b0]',
} as const;

export function fmtWhen(ts?: number | null): string {
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

export function fmtUntil(ts?: number | null): string {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    return d.toLocaleDateString([], { weekday: 'short' }) + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

/** Round picture with the initials behind it: the image covers them when it loads and
 *  hides itself when the URL answers 404 (private profile, bridge down). */
function Avatar({ label, url, size }: { label: string; url?: string | null; size: 'sm' | 'md' }) {
    const [failed, setFailed] = useState(false);
    useEffect(() => { setFailed(false); }, [url]);
    const cls = size === 'sm' ? 'w-9 h-9 text-xs' : 'w-8 h-8 text-xs';
    return (
        <div className={cn('relative rounded-full bg-[#2e2e2e] grid place-items-center text-[#c8c8c8] shrink-0 overflow-hidden', cls)}>
            <span>{initials(label)}</span>
            {url && !failed && (
                // eslint-disable-next-line @next/next/no-img-element
                <img src={url} alt="" className="absolute inset-0 w-full h-full object-cover" onError={() => setFailed(true)} />
            )}
        </div>
    );
}

function initials(label: string): string {
    const words = label.replace(/[^\p{L}\p{N} ]/gu, '').trim().split(/\s+/).filter(Boolean);
    if (words.length === 0) return '#';
    return words.slice(0, 2).map(w => w[0]).join('').toUpperCase();
}

/** A settings card in the overlay: title, one explaining line, then whatever the channel puts in. */
export function SettingsCard({ title, desc, full, children }: { title: string; desc?: string; full?: boolean; children?: React.ReactNode }) {
    return (
        <div className={cn('rounded-xl border border-[#2e2e2e] bg-[#1c1c1c] p-4', full && 'md:col-span-2')}>
            <h3 className="text-[13px] font-semibold">{title}</h3>
            {desc && <p className="text-[12.5px] text-[#9a9a9a] mt-1 mb-2.5">{desc}</p>}
            {children}
        </div>
    );
}

/** One value row inside a card (a number, a user, a state), with an optional right-hand side. */
export function KvRow({ left, right }: { left: React.ReactNode; right?: React.ReactNode }) {
    return (
        <div className="flex items-center justify-between px-2.5 py-2 rounded-lg bg-[#232323] text-[13px] mb-1.5 gap-3">
            <span className="min-w-0 truncate flex items-center gap-2">{left}</span>
            {right !== undefined && <span className="flex items-center gap-2 text-xs text-[#9a9a9a] shrink-0">{right}</span>}
        </div>
    );
}

export const BTN = 'px-3 py-1.5 rounded-lg bg-[#262626] border border-[#2e2e2e] text-sm hover:border-[#444] disabled:opacity-50';
export const BTN_PRIMARY = 'px-3 py-1.5 rounded-lg bg-[#e6e6e6] text-[#181818] text-sm font-medium disabled:opacity-50';
export const INPUT = 'bg-[#262626] border border-[#2e2e2e] rounded-lg px-3 py-1.5 text-sm outline-none focus:border-[#444]';

export default function ChannelDashboardShell(props: ChannelDashboardShellProps) {
    const {
        isOpen, onClose, icon, iconClass, title, subtitle, dot, dotTitle, chats, loading, loadFailed, onRefresh,
        historyUrl, historyVersion, selectedId, onSelect, banner, conversationExtra, conversationNote,
        settingsTitle, settingsContent, settingsOpen, onSettingsOpenChange,
    } = props;
    const t = useTranslations('settings.channelDashboard');
    const [listFilter, setListFilter] = useState('');
    const [sessionHistory, setSessionHistory] = useState<Array<{ role: string; content: string; timestamp?: string }>>([]);
    const [historyCompaction, setHistoryCompaction] = useState<{ user_turn_count: number; compaction_interval: number; last_compaction_at_turn: number } | null>(null);
    const [historyLoading, setHistoryLoading] = useState(false);
    const [chatSearch, setChatSearch] = useState('');
    const [chatSearchIdx, setChatSearchIdx] = useState(0);
    const inlineChatRef = useRef<HTMLDivElement | null>(null);

    const selected = selectedId ? chats.find(c => c.id === selectedId) ?? null : null;
    const historyKey = selected?.historyKey ?? null;

    const chatMessages = useMemo(
        () => sessionHistory
            .filter((m) => m.role === 'user' || m.role === 'assistant')
            .map((m) => ({
                role: m.role,
                timestamp: m.timestamp,
                text: (m.role === 'assistant' ? stripThinkBlocks(m.content || '') : m.content) || '',
            })),
        [sessionHistory]
    );

    const searchMatches = useMemo(() => {
        const q = chatSearch.trim().toLowerCase();
        if (!q) return [] as number[];
        return chatMessages.reduce<number[]>((acc, m, i) => {
            if (m.text.toLowerCase().includes(q)) acc.push(i);
            return acc;
        }, []);
    }, [chatMessages, chatSearch]);

    useEffect(() => { setChatSearchIdx(0); }, [chatSearch, selectedId]);

    useEffect(() => {
        if (chatSearch.trim()) return;
        const el = inlineChatRef.current;
        if (el) el.scrollTop = el.scrollHeight;
    }, [sessionHistory, selectedId, chatSearch]);

    useEffect(() => {
        if (!chatSearch.trim() || searchMatches.length === 0) return;
        const target = searchMatches[Math.min(chatSearchIdx, searchMatches.length - 1)];
        inlineChatRef.current?.querySelector(`[data-msg-idx="${target}"]`)?.scrollIntoView({ block: 'center' });
    }, [chatSearch, chatSearchIdx, searchMatches]);

    // Escape, one layer at a time, through the shared registry: the settings overlay
    // covers the window (52), a running in-chat search is the next thing to clear (51),
    // and the window itself closes last (50, its z-index). Each rung is active only
    // while its UI is really on screen.
    useEscapeLayer({ active: isOpen && settingsOpen, level: 52, onEscape: () => onSettingsOpenChange(false) });
    useEscapeLayer({ active: isOpen && !settingsOpen && chatSearch !== '', level: 51, onEscape: () => setChatSearch('') });
    useEscapeLayer({ active: isOpen && !settingsOpen && chatSearch === '', level: 50, onEscape: onClose });

    // The URL builder is an inline arrow in every dashboard, so it is a new function on
    // each render; depending on it re-ran this effect after every response and the
    // conversation fetched itself in a loop (and flickered whenever one of the piled-up
    // requests failed). Read it through a ref; only the key and the version trigger.
    const historyUrlRef = useRef(historyUrl);
    historyUrlRef.current = historyUrl;
    const historyRequest = useRef(0);
    useEffect(() => {
        if (!historyKey || !isOpen) {
            setSessionHistory([]);
            setHistoryCompaction(null);
            return;
        }
        const requestNo = ++historyRequest.current;
        setHistoryLoading(true);
        fetch(api(historyUrlRef.current(historyKey)), { credentials: 'include' })
            .then((r) => r.json())
            .then((json) => {
                if (requestNo !== historyRequest.current) return;   // a newer chat was selected meanwhile
                setSessionHistory(Array.isArray(json.messages) ? json.messages : []);
                setHistoryCompaction(
                    typeof json.user_turn_count === 'number' && typeof json.compaction_interval === 'number' && typeof json.last_compaction_at_turn === 'number'
                        ? { user_turn_count: json.user_turn_count, compaction_interval: json.compaction_interval, last_compaction_at_turn: json.last_compaction_at_turn }
                        : null
                );
            })
            .catch(() => {
                if (requestNo !== historyRequest.current) return;
                setSessionHistory([]);
                setHistoryCompaction(null);
            })
            .finally(() => { if (requestNo === historyRequest.current) setHistoryLoading(false); });
    }, [historyKey, isOpen, historyVersion]);

    const filtered = useMemo(() => {
        const q = listFilter.trim().toLowerCase();
        if (!q) return chats;
        return chats.filter(c => [c.label, c.preview, c.id].some(v => (v || '').toLowerCase().includes(q)));
    }, [chats, listFilter]);

    const dayLabel = (iso?: string): string | null => {
        if (!iso) return null;
        const d = new Date(iso);
        if (Number.isNaN(d.getTime())) return null;
        const now = new Date();
        if (d.toDateString() === now.toDateString()) return t('today');
        const y = new Date(now.getTime() - 86400_000);
        if (d.toDateString() === y.toDateString()) return t('yesterday');
        return d.toLocaleDateString([], { day: '2-digit', month: 'short', year: 'numeric' });
    };

    if (!isOpen) return null;

    const dotCls = dot === 'green' ? 'bg-[#3fbf5f]' : dot === 'amber' ? 'bg-[#e0a030]' : 'bg-[#555]';

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50 max-md:p-0" onClick={onClose}>
            <div
                className="relative bg-[#181818] text-[#e8e8e8] w-full max-w-[95vw] h-[90vh] rounded-2xl shadow-2xl border border-[#2e2e2e] flex flex-col overflow-hidden max-md:max-w-none max-md:h-[100dvh] max-md:rounded-none max-md:border-0"
                onClick={e => e.stopPropagation()}
            >
                <header className="flex items-center gap-3 px-4 py-2.5 border-b border-[#2e2e2e] bg-[#1f1f1f] shrink-0">
                    <div className={cn('w-8 h-8 rounded-lg grid place-items-center shrink-0', iconClass)}>{icon}</div>
                    <h1 className="font-semibold text-[15px]">{title}</h1>
                    <span className="text-[13px] text-[#9a9a9a] flex items-center gap-2 min-w-0 max-md:hidden">
                        <span className={cn('w-2 h-2 rounded-full shrink-0', dotCls)} title={dotTitle} />
                        <span className="truncate">{subtitle}</span>
                    </span>
                    <div className="flex-1 max-w-xl ml-auto flex gap-2 min-w-0">
                        <div className="flex-1 relative min-w-0">
                            <Search className="w-4 h-4 absolute left-3 top-2.5 text-[#9a9a9a]" />
                            <input value={listFilter} onChange={e => setListFilter(e.target.value)} placeholder={t('searchChats')}
                                className={cn('w-full pl-9', INPUT)} />
                        </div>
                        <button type="button" onClick={onRefresh} disabled={loading} className={cn('flex items-center gap-1.5', BTN)}>
                            <RefreshCw className={cn('w-4 h-4', loading && 'animate-spin')} /><span className="max-md:hidden">{t('refresh')}</span>
                        </button>
                    </div>
                    <button type="button" onClick={() => onSettingsOpenChange(true)} title={t('settings')}
                        className="p-2 rounded-lg bg-[#262626] border border-[#2e2e2e] hover:border-[#444]">
                        <Settings className="w-4 h-4" />
                    </button>
                    <button type="button" onClick={onClose} title={t('close')} className="p-2 rounded-lg hover:bg-[#262626] text-[#9a9a9a] hover:text-white">
                        <X className="w-4 h-4" />
                    </button>
                </header>

                <main className="flex-1 grid min-h-0 max-md:grid-cols-1 max-md:grid-rows-[38vh_1fr]" style={{ gridTemplateColumns: '320px 1fr' }}>
                    <nav className="border-r border-[#2e2e2e] bg-[#1f1f1f] overflow-y-auto max-md:border-r-0 max-md:border-b">
                        <div className="sticky top-0 z-10 px-4 py-2 bg-[#1f1f1f] border-b border-[#2e2e2e] text-xs text-[#9a9a9a] flex items-center justify-between">
                            <span>{t('chatsHeader', { count: filtered.length })}</span>
                            <span>{t('newestFirst')}</span>
                        </div>
                        {loading && chats.length === 0 ? (
                            <div className="p-4 text-sm text-[#9a9a9a]">{t('loading')}</div>
                        ) : loadFailed ? (
                            <div className="p-4 text-sm text-[#e08c8c]">{t('couldNotLoad')}</div>
                        ) : filtered.length === 0 ? (
                            <div className="p-4 text-sm text-[#9a9a9a]">{t('noChats')}</div>
                        ) : filtered.map((c) => (
                            <button key={c.id} type="button" onClick={() => onSelect(c.id)}
                                className={cn('relative w-full text-left px-4 py-2.5 border-b border-[#2e2e2e]',
                                    selectedId === c.id ? 'bg-[#2a2a2a]' : 'hover:bg-[#262626]')}>
                                <div className="flex items-center gap-3">
                                    <Avatar label={c.label} url={c.avatarUrl} size="sm" />
                                    <div className="min-w-0 flex-1">
                                        <div className="flex justify-between gap-2 text-[13px]">
                                            <span className="font-semibold truncate" title={c.id}>{c.label}</span>
                                            <span className="text-[#9a9a9a] flex-shrink-0">{fmtWhen(c.ts)}</span>
                                        </div>
                                        <div className="text-xs text-[#9a9a9a] truncate pr-20 min-h-[1rem]">{c.preview || ''}</div>
                                    </div>
                                </div>
                                <span className={cn('absolute right-3 bottom-2 text-[11px] px-1.5 rounded-md', c.badge.cls)}>{c.badge.label}</span>
                            </button>
                        ))}
                    </nav>

                    <section className="flex flex-col min-w-0 min-h-0">
                        {banner}
                        {!selected ? (
                            <div className="flex-1 grid place-items-center text-sm text-[#9a9a9a]">{t('selectChat')}</div>
                        ) : (
                            <>
                                <div className="flex items-center gap-3 px-5 py-2.5 border-b border-[#2e2e2e] shrink-0 flex-wrap">
                                    <Avatar label={selected.label} url={selected.avatarUrl} size="md" />
                                    <div className="min-w-0 flex-1">
                                        <div className="font-semibold flex items-center gap-2 min-w-0">
                                            <span className="truncate">{selected.label}</span>
                                            <span className={cn('text-[11px] px-1.5 rounded-md font-normal', selected.badge.cls)}>{selected.badge.label}</span>
                                        </div>
                                        <div className="text-xs text-[#9a9a9a] truncate">{selected.subline}</div>
                                    </div>
                                    {conversationExtra?.(selected)}
                                    <div className="flex items-center gap-1.5">
                                        {chatSearch.trim() !== '' && (
                                            <>
                                                <span className="text-xs text-[#9a9a9a] tabular-nums">
                                                    {searchMatches.length === 0 ? '0 / 0' : `${Math.min(chatSearchIdx, searchMatches.length - 1) + 1} / ${searchMatches.length}`}
                                                </span>
                                                <button type="button" onClick={() => setChatSearchIdx((i) => (i - 1 + searchMatches.length) % searchMatches.length)} disabled={searchMatches.length === 0}
                                                    className="p-1 rounded hover:bg-[#262626] text-[#9a9a9a] disabled:opacity-40" title={t('prevMatch')}><ChevronUp className="w-4 h-4" /></button>
                                                <button type="button" onClick={() => setChatSearchIdx((i) => (i + 1) % searchMatches.length)} disabled={searchMatches.length === 0}
                                                    className="p-1 rounded hover:bg-[#262626] text-[#9a9a9a] disabled:opacity-40" title={t('nextMatch')}><ChevronDown className="w-4 h-4" /></button>
                                            </>
                                        )}
                                        <div className="relative">
                                            <Search className="w-4 h-4 text-[#9a9a9a] absolute left-2.5 top-1/2 -translate-y-1/2 pointer-events-none" />
                                            <input type="text" value={chatSearch} onChange={(e) => setChatSearch(e.target.value)}
                                                onKeyDown={(e) => {
                                                    if (e.key === 'Enter' && searchMatches.length > 0) {
                                                        e.preventDefault();
                                                        setChatSearchIdx((i) => e.shiftKey ? (i - 1 + searchMatches.length) % searchMatches.length : (i + 1) % searchMatches.length);
                                                    }
                                                }}
                                                placeholder={t('searchChat')}
                                                className={cn('w-48 pl-8 pr-2', INPUT)} />
                                        </div>
                                    </div>
                                    {conversationNote && <p className="w-full text-xs text-[#e08c8c]">{conversationNote}</p>}
                                </div>
                                <div ref={inlineChatRef} className="flex-1 min-h-0 overflow-y-auto bg-[#151515] p-5 flex flex-col gap-2.5">
                                    {historyLoading && sessionHistory.length === 0 ? (
                                        <p className="text-sm text-[#9a9a9a]">{t('loadingHistory')}</p>
                                    ) : chatMessages.length === 0 ? (
                                        <p className="text-sm text-[#9a9a9a]">{t('noMessagesInChat')}</p>
                                    ) : chatMessages.map((msg, i) => {
                                        const isBot = msg.role === 'assistant';
                                        const isCurrentMatch = searchMatches.length > 0 && searchMatches[Math.min(chatSearchIdx, searchMatches.length - 1)] === i;
                                        const day = dayLabel(msg.timestamp);
                                        const prevDay = i > 0 ? dayLabel(chatMessages[i - 1].timestamp) : null;
                                        return (
                                            <React.Fragment key={`${msg.timestamp || 'no-ts'}-${i}`}>
                                                {day && day !== prevDay && (
                                                    <span className="self-center text-[11px] text-[#8a8a8a] bg-[#1f1f1f] px-2.5 py-0.5 rounded-full">{day}</span>
                                                )}
                                                <div data-msg-idx={i} className={cn('flex gap-2', isBot ? 'justify-end' : 'justify-start')}>
                                                    {!isBot && (
                                                        <div className="w-6 h-6 rounded-full bg-[#2e2e2e] grid place-items-center text-[#c8c8c8] shrink-0"><User className="w-3.5 h-3.5" /></div>
                                                    )}
                                                    <div className={cn('max-w-[62%] px-3 py-2 rounded-2xl text-[13.5px] leading-relaxed',
                                                        isBot ? 'bg-[#1f4d2a] rounded-tr-sm' : 'bg-[#262626] rounded-tl-sm',
                                                        isCurrentMatch && 'ring-2 ring-[#e0b866]')}>
                                                        <p className="whitespace-pre-wrap break-words"><HighlightedText text={msg.text} query={chatSearch.trim()} /></p>
                                                        {msg.timestamp && <div className={cn('text-[10px] text-[#8a8a8a] mt-1', isBot && 'text-right')}>{msg.timestamp}</div>}
                                                    </div>
                                                    {isBot && (
                                                        <div className={cn('w-6 h-6 rounded-full grid place-items-center text-white shrink-0', iconClass)}><Bot className="w-3.5 h-3.5" /></div>
                                                    )}
                                                </div>
                                            </React.Fragment>
                                        );
                                    })}
                                </div>
                                <div className="px-5 py-2 border-t border-[#2e2e2e] text-xs text-[#9a9a9a] flex justify-between gap-3 flex-wrap shrink-0">
                                    <span className="min-w-0 truncate">{selected.footer}</span>
                                    <span className="shrink-0">
                                        {t('messagesCount', { count: chatMessages.length })}
                                        {historyCompaction && (() => {
                                            const interval = Math.max(1, Number(historyCompaction.compaction_interval) || 15);
                                            const sinceLast = Math.max(0, Number(historyCompaction.user_turn_count || 0) - Number(historyCompaction.last_compaction_at_turn || 0));
                                            return ` · ${t('untilLearning', { count: interval - (sinceLast % interval) })}`;
                                        })()}
                                    </span>
                                </div>
                            </>
                        )}
                    </section>
                </main>

                {settingsOpen && (
                    <div className="absolute inset-0 z-20 bg-[#181818] flex flex-col">
                        <div className="flex items-center justify-between px-5 py-3 border-b border-[#2e2e2e]">
                            <h2 className="text-sm font-semibold flex items-center gap-2"><Settings className="w-4 h-4" /> {settingsTitle}</h2>
                            <button type="button" onClick={() => onSettingsOpenChange(false)} title={t('close')} className="p-1.5 rounded-md hover:bg-[#262626]"><X className="w-4 h-4" /></button>
                        </div>
                        <div className="flex-1 overflow-y-auto px-5 py-4 grid grid-cols-1 md:grid-cols-2 gap-4 content-start">
                            {settingsContent}
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
}
