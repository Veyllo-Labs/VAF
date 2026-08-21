'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import React, { useState, useEffect, useMemo, useRef } from 'react';
import { X, ExternalLink, MessageSquare, UserCheck, UserPlus, Trash2, Bot, User, Search, ChevronUp, ChevronDown } from 'lucide-react';
import { cn, stripThinkBlocks } from '@/lib/utils';
import MessagesChart from './MessagesChart';
import HighlightedText from './HighlightedText';

const api = (path: string) => path.startsWith('/') ? path : `/${path}`;

export interface TelegramDashboardProps {
    isOpen: boolean;
    onClose: () => void;
    config: any;
    onConfigChange: (key: string, value: any) => void;
}

interface TelegramSession {
    chat_id: string;
    telegram_user_id: string;
    telegram_username?: string | null;
    vaf_username?: string | null;
    type: 'admin' | 'relay' | 'unknown';
    last_ts: number;
    message_count: number;
}

interface Stats4hBucket {
    bucket_ts: number;
    count: number;
}

interface DashboardData {
    bot_username: string | null;
    bot_link: string | null;
    sessions: TelegramSession[];
    stats_4h: Stats4hBucket[];
    admin_whitelist: Array<{ telegram_user_id: string; telegram_username?: string | null; vaf_username?: string }>;
    relay_whitelist: Array<{ telegram_user_id: string; telegram_username?: string | null; vaf_username?: string }>;
    activity: Array<{ chat_id: string; user_scope_id: string | null; ts: number; direction: string }>;
}

export default function TelegramDashboard({ isOpen, onClose, config, onConfigChange }: TelegramDashboardProps) {
    const [data, setData] = useState<DashboardData | null>(null);
    const [loading, setLoading] = useState(false);
    const [selectedChatId, setSelectedChatId] = useState<string | null>(null);
    const [sessionHistory, setSessionHistory] = useState<Array<{ role: string; content: string; timestamp?: string }>>([]);
    const [historyCompaction, setHistoryCompaction] = useState<{ user_turn_count: number; compaction_interval: number; last_compaction_at_turn: number } | null>(null);
    const [historyLoading, setHistoryLoading] = useState(false);
    const [relayAddId, setRelayAddId] = useState('');
    const [relayAddUsername, setRelayAddUsername] = useState('');
    const [chatSearch, setChatSearch] = useState('');
    const [chatSearchIdx, setChatSearchIdx] = useState(0);
    const inlineChatRef = useRef<HTMLDivElement | null>(null);

    const chatMessages = useMemo(
        () =>
            sessionHistory
                .filter((m) => m.role === 'user' || m.role === 'assistant')
                .map((m) => ({
                    role: m.role,
                    timestamp: m.timestamp,
                    text: (m.role === 'assistant' ? stripThinkBlocks(m.content || '') : m.content) || '—',
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

    useEffect(() => {
        setChatSearchIdx(0);
    }, [chatSearch, selectedChatId]);

    // Keep the inline conversation pinned to the newest message (unless a search is active).
    useEffect(() => {
        if (chatSearch.trim()) return;
        const el = inlineChatRef.current;
        if (el) el.scrollTop = el.scrollHeight;
    }, [sessionHistory, selectedChatId, chatSearch]);

    // Bring the current search match into view.
    useEffect(() => {
        if (!chatSearch.trim() || searchMatches.length === 0) return;
        const target = searchMatches[Math.min(chatSearchIdx, searchMatches.length - 1)];
        inlineChatRef.current
            ?.querySelector(`[data-msg-idx="${target}"]`)
            ?.scrollIntoView({ block: 'center' });
    }, [chatSearch, chatSearchIdx, searchMatches]);

    useEffect(() => {
        if (isOpen) fetchDashboard();
    }, [isOpen]);

    useEffect(() => {
        if (!isOpen) return;
        const handleKeyDown = (e: KeyboardEvent) => {
            if (e.key === 'Escape') {
                e.preventDefault();
                e.stopPropagation();
                if (chatSearch) {
                    setChatSearch('');
                } else {
                    onClose();
                }
            }
        };
        window.addEventListener('keydown', handleKeyDown, true);
        return () => window.removeEventListener('keydown', handleKeyDown, true);
    }, [isOpen, onClose, chatSearch]);

    useEffect(() => {
        const chatId = selectedChatId;
        if (!chatId || !isOpen) {
            setSessionHistory([]);
            setHistoryCompaction(null);
            return;
        }
        const sessionId = `telegram_${chatId}`;
        setHistoryLoading(true);
        fetch(api(`api/telegram/session/${encodeURIComponent(sessionId)}/history`), { credentials: 'include' })
            .then((r) => r.json())
            .then((json) => {
                setSessionHistory(Array.isArray(json.messages) ? json.messages : []);
                setHistoryCompaction(
                    typeof json.user_turn_count === 'number' && typeof json.compaction_interval === 'number' && typeof json.last_compaction_at_turn === 'number'
                        ? { user_turn_count: json.user_turn_count, compaction_interval: json.compaction_interval, last_compaction_at_turn: json.last_compaction_at_turn }
                        : null
                );
            })
            .catch(() => {
                setSessionHistory([]);
                setHistoryCompaction(null);
            })
            .finally(() => setHistoryLoading(false));
    }, [selectedChatId, isOpen]);

    const fetchDashboard = async () => {
        setLoading(true);
        try {
            const res = await fetch(api('api/telegram/dashboard'), { credentials: 'include' });
            const json = await res.json();
            setData({
                bot_username: json.bot_username ?? null,
                bot_link: json.bot_link ?? null,
                sessions: Array.isArray(json.sessions) ? json.sessions : [],
                stats_4h: Array.isArray(json.stats_4h) ? json.stats_4h : [],
                admin_whitelist: Array.isArray(json.admin_whitelist) ? json.admin_whitelist : [],
                relay_whitelist: Array.isArray(json.relay_whitelist) ? json.relay_whitelist : [],
                activity: Array.isArray(json.activity) ? json.activity : [],
            });
            if (!selectedChatId && Array.isArray(json.sessions) && json.sessions.length > 0) {
                setSelectedChatId(json.sessions[0]?.chat_id ?? null);
            }
        } catch {
            setData(null);
        } finally {
            setLoading(false);
        }
    };

    const handleRelayAdd = async () => {
        const id = relayAddId.trim();
        if (!id) return;
        try {
            await fetch(api('api/telegram/relay-whitelist-add'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({ telegram_user_id: id, telegram_username: relayAddUsername.trim() || undefined }),
            });
            setRelayAddId('');
            setRelayAddUsername('');
            onConfigChange('telegram_config', { ...config.telegram_config, relay_whitelist: [...(config.telegram_config?.relay_whitelist || []), { telegram_user_id: id, telegram_username: relayAddUsername.trim() || null }] });
            fetchDashboard();
        } catch (e) {
            console.error(e);
        }
    };

    const handleRelayRemove = async (telegram_user_id: string) => {
        try {
            await fetch(api('api/telegram/relay-whitelist-remove'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({ telegram_user_id }),
            });
            const current = config.telegram_config?.relay_whitelist || [];
            onConfigChange('telegram_config', { ...config.telegram_config, relay_whitelist: current.filter((e: any) => String(e.telegram_user_id) !== telegram_user_id) });
            fetchDashboard();
        } catch (e) {
            console.error(e);
        }
    };

    const formatActivityTime = (ts: number) => {
        const d = new Date(ts * 1000);
        const now = new Date();
        const sameDay = d.toDateString() === now.toDateString();
        return sameDay ? d.toLocaleTimeString() : d.toLocaleDateString() + ' ' + d.toLocaleTimeString();
    };

    if (!isOpen) return null;

    return (
        <>
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50 max-md:p-0" onClick={onClose}>
            <div
                className={cn(
                    'relative bg-white w-full max-w-[95vw] h-[90vh] rounded-2xl shadow-2xl border border-gray-200 flex flex-col overflow-hidden max-md:max-w-none max-md:h-[100dvh] max-md:rounded-none max-md:border-0'
                )}
                onClick={e => e.stopPropagation()}
            >
                <div className="flex items-center justify-between px-5 py-4 border-b border-gray-200 shrink-0 max-md:px-4 max-md:py-3">
                    <div className="flex items-center gap-3 min-w-0">
                        <h3 className="text-lg font-semibold text-gray-900 max-md:text-lg truncate">Telegram</h3>
                        {data?.bot_link && (
                            <a
                                href={data.bot_link}
                                target="_blank"
                                rel="noopener noreferrer"
                                className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-sky-50 text-sky-700 hover:bg-sky-100 transition-colors text-sm shrink-0"
                            >
                                <ExternalLink className="w-4 h-4" />
                                Open in Telegram
                            </a>
                        )}
                    </div>
                    <button type="button" onClick={onClose} className="p-2 hover:bg-gray-100 rounded-lg transition-colors">
                        <X className="w-5 h-5 text-gray-500" />
                    </button>
                </div>

                <div className="flex-1 flex min-h-0 max-md:flex-col">
                    {/* Left sidebar: session list (chats for this bot only) */}
                    <div className="w-56 shrink-0 border-r border-gray-200 flex flex-col bg-gray-50/50 max-md:w-full max-md:max-h-[38vh] max-md:border-r-0 max-md:border-b max-md:shrink-0">
                        <div className="px-3 py-2 border-b border-gray-200">
                            <p className="text-xs font-medium text-gray-500 uppercase tracking-wide">Sessions</p>
                            <p className="text-xs text-gray-400 mt-0.5">Chats with this bot</p>
                        </div>
                        <div className="flex-1 overflow-y-auto">
                            {loading ? (
                                <div className="p-3 text-sm text-gray-500">Loading…</div>
                            ) : data?.sessions && data.sessions.length > 0 ? (
                                <ul className="py-1">
                                    {data.sessions.map((s) => (
                                        <li key={s.chat_id}>
                                            <button
                                                type="button"
                                                onClick={() => setSelectedChatId(s.chat_id)}
                                                className={cn(
                                                    'w-full text-left px-3 py-2.5 flex flex-col gap-0.5 transition-colors border-l-2',
                                                    selectedChatId === s.chat_id
                                                        ? 'bg-sky-100 text-sky-900 border-sky-500'
                                                        : 'border-transparent hover:bg-gray-100 text-gray-700'
                                                )}
                                            >
                                                <span className="text-sm font-medium truncate">
                                                    @{s.telegram_username || s.telegram_user_id}
                                                </span>
                                                <span className="flex items-center gap-1.5 text-xs text-gray-500">
                                                    <span className={cn(
                                                        'px-1.5 py-0.5 rounded',
                                                        s.type === 'admin' ? 'bg-green-100 text-green-700' : s.type === 'relay' ? 'bg-amber-100 text-amber-700' : 'bg-gray-200 text-gray-600'
                                                    )}>
                                                        {s.type}
                                                    </span>
                                                    {s.message_count > 0 && <span>{s.message_count} msgs</span>}
                                                </span>
                                                {s.last_ts > 0 && (
                                                    <span className="text-xs text-gray-400">{formatActivityTime(s.last_ts)}</span>
                                                )}
                                            </button>
                                        </li>
                                    ))}
                                </ul>
                            ) : (
                                <p className="p-3 text-sm text-gray-500">No sessions yet. Add users in the wizard or relay list.</p>
                            )}
                        </div>
                    </div>

                    {/* Main content */}
                    <div className="flex-1 overflow-y-auto px-5 py-4 space-y-5 min-w-0 flex flex-col max-md:min-h-0 max-md:shrink-0">
                    {loading ? (
                        <div className="py-8 text-center text-gray-500">Loading…</div>
                    ) : data ? (
                        <>
                            {/* Line chart: messages per 4-hour interval; the bot link lives in the dialog header */}
                            <MessagesChart buckets={data?.stats_4h ?? []} chartId="telegram-messages-chart" />

                            {/* Conversation for the selected chat, inline (oldest at top, pinned to newest); the search field works like Ctrl+F over the chat */}
                            <div className="flex-1 min-h-0 flex flex-col">
                                <div className="flex items-center justify-between gap-3 mb-2 shrink-0">
                                    <div className="flex items-baseline gap-2 min-w-0">
                                        <p className="text-sm font-medium text-gray-700 shrink-0">
                                            {selectedChatId ? 'Conversation' : 'Recent activity'}
                                        </p>
                                        {selectedChatId && historyCompaction && (() => {
                                            const interval = Math.max(1, Number(historyCompaction.compaction_interval) || 15);
                                            const sinceLast = Math.max(
                                                0,
                                                Number(historyCompaction.user_turn_count || 0) - Number(historyCompaction.last_compaction_at_turn || 0)
                                            );
                                            const progress = sinceLast % interval;
                                            return (
                                                <span className="text-xs text-gray-500 truncate">
                                                    <span className="text-gray-400">- </span>
                                                    <span className="font-medium text-violet-700">{progress}</span>
                                                    <span> / {interval} messages until Memory Learning</span>
                                                    <span className="text-gray-400"> · </span>
                                                    <span>
                                                        {historyCompaction.last_compaction_at_turn === 0
                                                            ? 'Last Memory Learning: none yet'
                                                            : `Last Memory Learning: after turn ${historyCompaction.last_compaction_at_turn}`}
                                                    </span>
                                                </span>
                                            );
                                        })()}
                                    </div>
                                    {selectedChatId && (
                                        <div className="flex items-center gap-1.5">
                                            {chatSearch.trim() !== '' && (
                                                <>
                                                    <span className="text-xs text-gray-400 tabular-nums">
                                                        {searchMatches.length === 0
                                                            ? '0 / 0'
                                                            : `${Math.min(chatSearchIdx, searchMatches.length - 1) + 1} / ${searchMatches.length}`}
                                                    </span>
                                                    <button
                                                        type="button"
                                                        onClick={() => setChatSearchIdx((i) => (i - 1 + searchMatches.length) % searchMatches.length)}
                                                        disabled={searchMatches.length === 0}
                                                        className="p-1 rounded hover:bg-gray-100 text-gray-400 hover:text-gray-600 disabled:opacity-40 transition-colors"
                                                        title="Previous match"
                                                    >
                                                        <ChevronUp className="w-4 h-4" />
                                                    </button>
                                                    <button
                                                        type="button"
                                                        onClick={() => setChatSearchIdx((i) => (i + 1) % searchMatches.length)}
                                                        disabled={searchMatches.length === 0}
                                                        className="p-1 rounded hover:bg-gray-100 text-gray-400 hover:text-gray-600 disabled:opacity-40 transition-colors"
                                                        title="Next match"
                                                    >
                                                        <ChevronDown className="w-4 h-4" />
                                                    </button>
                                                </>
                                            )}
                                            <div className="relative">
                                                <Search className="w-4 h-4 text-gray-400 absolute left-2.5 top-1/2 -translate-y-1/2 pointer-events-none" />
                                                <input
                                                    type="text"
                                                    value={chatSearch}
                                                    onChange={(e) => setChatSearch(e.target.value)}
                                                    onKeyDown={(e) => {
                                                        if (e.key === 'Enter' && searchMatches.length > 0) {
                                                            e.preventDefault();
                                                            setChatSearchIdx((i) =>
                                                                e.shiftKey
                                                                    ? (i - 1 + searchMatches.length) % searchMatches.length
                                                                    : (i + 1) % searchMatches.length
                                                            );
                                                        }
                                                    }}
                                                    placeholder="Search chat"
                                                    className="w-80 max-w-[45vw] rounded border border-gray-300 pl-8 pr-2 py-1.5 text-sm"
                                                />
                                            </div>
                                        </div>
                                    )}
                                </div>
                                <div ref={inlineChatRef} className="rounded-lg border border-gray-200 bg-gray-50/50 flex-1 min-h-[12.5rem] overflow-y-auto">
                                    {selectedChatId ? (
                                        historyLoading && sessionHistory.length === 0 ? (
                                            <p className="text-sm text-gray-500 p-3">Loading history…</p>
                                        ) : chatMessages.length === 0 ? (
                                            <p className="text-sm text-gray-500 p-3">No messages in this session yet.</p>
                                        ) : (
                                            <div className="p-3 space-y-2">
                                                {chatMessages.map((msg, i) => {
                                                    const isBot = msg.role === 'assistant';
                                                    const isCurrentMatch =
                                                        searchMatches.length > 0 &&
                                                        searchMatches[Math.min(chatSearchIdx, searchMatches.length - 1)] === i;
                                                    return (
                                                        <div
                                                            key={`${msg.timestamp || 'no-ts'}-${i}`}
                                                            data-msg-idx={i}
                                                            className={cn('flex gap-2', isBot ? 'justify-start' : 'justify-end')}
                                                        >
                                                            {isBot && (
                                                                <div className="w-6 h-6 rounded-lg bg-gray-900 dark:bg-[#2e2e2e] flex items-center justify-center text-white shrink-0">
                                                                    <Bot className="w-3.5 h-3.5" />
                                                                </div>
                                                            )}
                                                            <div className={cn('max-w-[80%] flex flex-col', isBot ? 'items-start' : 'items-end')}>
                                                                <div
                                                                    className={cn(
                                                                        'px-3 py-1.5 rounded-xl text-sm leading-relaxed',
                                                                        isBot
                                                                            ? 'bg-white text-gray-800 rounded-tl-none border border-gray-200'
                                                                            : 'bg-gray-800 text-white rounded-tr-none',
                                                                        isCurrentMatch && 'ring-2 ring-amber-400'
                                                                    )}
                                                                >
                                                                    <p className="whitespace-pre-wrap break-words">
                                                                        <HighlightedText text={msg.text} query={chatSearch.trim()} />
                                                                    </p>
                                                                </div>
                                                                {msg.timestamp && (
                                                                    <span className="text-[10px] text-gray-400 mt-0.5">{msg.timestamp}</span>
                                                                )}
                                                            </div>
                                                            {!isBot && (
                                                                <div className="w-6 h-6 rounded-lg bg-white border border-gray-200 flex items-center justify-center text-gray-500 shrink-0">
                                                                    <User className="w-3.5 h-3.5" />
                                                                </div>
                                                            )}
                                                        </div>
                                                    );
                                                })}
                                            </div>
                                        )
                                    ) : data.activity.length === 0 ? (
                                        <p className="text-sm text-gray-500 p-3">No activity yet.</p>
                                    ) : (
                                        <ul className="divide-y divide-gray-200">
                                            {[...data.activity]
                                                .reverse()
                                                .slice(0, 50)
                                                .map((a, i) => (
                                                <li key={i} className="flex items-center gap-2 px-3 py-2 text-sm text-gray-600">
                                                    <MessageSquare className="w-4 h-4 text-gray-400 shrink-0" />
                                                    <span>{formatActivityTime(a.ts)}</span>
                                                    <span className="text-gray-400">·</span>
                                                    <span>{a.direction === 'in' ? 'Incoming' : 'Outgoing'}</span>
                                                </li>
                                            ))}
                                        </ul>
                                    )}
                                </div>
                            </div>

                            {/* Two columns: Admin whitelist | Relay whitelist */}
                            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                                {/* Admin whitelist (full agent) */}
                                <div className="rounded-lg border border-gray-200 p-4">
                                    <div className="flex items-center gap-2 mb-3">
                                        <UserCheck className="w-4 h-4 text-gray-600" />
                                        <p className="text-sm font-medium text-gray-800">Full access</p>
                                    </div>
                                    <p className="text-xs text-gray-500 mb-3">These users can use the full agent (tools, memory) via Telegram.</p>
                                    <ul className="space-y-2">
                                        {(data.admin_whitelist || []).map((e, i) => (
                                            <li key={i} className="flex items-center justify-between text-sm py-1.5 px-2 rounded bg-gray-50">
                                                <span className="text-gray-700">@{e.telegram_username || e.telegram_user_id}</span>
                                                {e.vaf_username && <span className="text-gray-500 text-xs">{e.vaf_username}</span>}
                                            </li>
                                        ))}
                                        {(!data.admin_whitelist || data.admin_whitelist.length === 0) && (
                                            <li className="text-sm text-gray-500">None. Add yourself in the setup wizard.</li>
                                        )}
                                    </ul>
                                </div>

                                {/* Relay whitelist (message-only, no tools) */}
                                <div className="rounded-lg border border-gray-200 p-4">
                                    <div className="flex items-center gap-2 mb-3">
                                        <UserPlus className="w-4 h-4 text-gray-600" />
                                        <p className="text-sm font-medium text-gray-800">Relay contacts</p>
                                    </div>
                                    <p className="text-xs text-gray-500 mb-3">These contacts can only send messages to you. No tools; replies are fixed (e.g. “I’ll pass that on”).</p>
                                    <ul className="space-y-2 mb-3">
                                        {(data.relay_whitelist || []).map((e, i) => (
                                            <li key={i} className="flex items-center justify-between text-sm py-1.5 px-2 rounded bg-gray-50">
                                                <span className="text-gray-700">@{e.telegram_username || e.telegram_user_id}</span>
                                                <button
                                                    type="button"
                                                    onClick={() => {
                                                        if (!confirm('Are you sure you want to remove this relay contact?')) return;
                                                        handleRelayRemove(e.telegram_user_id);
                                                    }}
                                                    className="p-1 hover:bg-red-50 rounded text-gray-400 hover:text-red-600"
                                                    title="Remove"
                                                >
                                                    <Trash2 className="w-4 h-4" />
                                                </button>
                                            </li>
                                        ))}
                                        {(!data.relay_whitelist || data.relay_whitelist.length === 0) && (
                                            <li className="text-sm text-gray-500">None.</li>
                                        )}
                                    </ul>
                                    <div className="flex gap-2 flex-wrap">
                                        <input
                                            type="text"
                                            placeholder="Telegram user ID"
                                            value={relayAddId}
                                            onChange={e => setRelayAddId(e.target.value)}
                                            className="flex-1 min-w-0 rounded border border-gray-300 px-2 py-1.5 text-sm"
                                        />
                                        <input
                                            type="text"
                                            placeholder="Username (optional)"
                                            value={relayAddUsername}
                                            onChange={e => setRelayAddUsername(e.target.value)}
                                            className="flex-1 min-w-0 rounded border border-gray-300 px-2 py-1.5 text-sm"
                                        />
                                        <button
                                            type="button"
                                            onClick={handleRelayAdd}
                                            disabled={!relayAddId.trim()}
                                            className="px-3 py-1.5 rounded bg-gray-900 text-white text-sm font-medium disabled:opacity-50 disabled:cursor-not-allowed dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-[#f5f5f5] dark:shadow-none"
                                        >
                                            Add
                                        </button>
                                    </div>
                                </div>
                            </div>
                        </>
                    ) : (
                        <div className="py-8 text-center text-gray-500">Could not load dashboard.</div>
                    )}
                    </div>
                </div>

            </div>
        </div>
    </>
    );
}
