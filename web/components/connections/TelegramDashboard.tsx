'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import React, { useState, useEffect } from 'react';
import { createPortal } from 'react-dom';
import { X, ExternalLink, MessageSquare, UserCheck, UserPlus, Trash2, Bot, User } from 'lucide-react';
import { cn } from '@/lib/utils';
import MessagesChart from './MessagesChart';

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
    const [sessionHistoryPopoutChatId, setSessionHistoryPopoutChatId] = useState<string | null>(null);
    const [sessionHistory, setSessionHistory] = useState<Array<{ role: string; content: string; timestamp?: string }>>([]);
    const [historyCompaction, setHistoryCompaction] = useState<{ user_turn_count: number; compaction_interval: number; last_compaction_at_turn: number } | null>(null);
    const [historyLoading, setHistoryLoading] = useState(false);
    const [relayAddId, setRelayAddId] = useState('');
    const [relayAddUsername, setRelayAddUsername] = useState('');

    useEffect(() => {
        if (isOpen) fetchDashboard();
    }, [isOpen]);

    useEffect(() => {
        if (!isOpen) return;
        const handleKeyDown = (e: KeyboardEvent) => {
            if (e.key === 'Escape') {
                e.preventDefault();
                e.stopPropagation();
                if (sessionHistoryPopoutChatId) {
                    setSessionHistoryPopoutChatId(null);
                } else {
                    onClose();
                }
            }
        };
        window.addEventListener('keydown', handleKeyDown, true);
        return () => window.removeEventListener('keydown', handleKeyDown, true);
    }, [isOpen, onClose, sessionHistoryPopoutChatId]);

    useEffect(() => {
        const chatId = sessionHistoryPopoutChatId ?? selectedChatId;
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
    }, [sessionHistoryPopoutChatId, selectedChatId, isOpen]);

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
                    <h3 className="text-lg font-semibold text-gray-900 max-md:text-lg truncate">Telegram</h3>
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
                                                onClick={() => {
                                                    setSelectedChatId(s.chat_id);
                                                    setSessionHistoryPopoutChatId(s.chat_id);
                                                }}
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
                    <div className="flex-1 overflow-y-auto px-5 py-4 space-y-5 min-w-0 max-md:min-h-0 max-md:shrink-0">
                    {loading ? (
                        <div className="py-8 text-center text-gray-500">Loading…</div>
                    ) : data ? (
                        <>
                            {/* Test link */}
                            <div>
                                <p className="text-sm font-medium text-gray-700 mb-2">Chat with bot</p>
                                {data.bot_link ? (
                                    <a
                                        href={data.bot_link}
                                        target="_blank"
                                        rel="noopener noreferrer"
                                        className="inline-flex items-center gap-2 px-3 py-2 rounded-lg bg-sky-50 text-sky-700 hover:bg-sky-100 transition-colors text-sm"
                                    >
                                        <ExternalLink className="w-4 h-4" />
                                        Open in Telegram
                                    </a>
                                ) : (
                                    <p className="text-sm text-gray-500">Bot link not available.</p>
                                )}
                            </div>

                            {/* Line chart: messages per 4-hour interval */}
                            <MessagesChart buckets={data?.stats_4h ?? []} chartId="telegram-messages-chart" />

                            {/* Activity: all or for selected session; when a chat is selected, whole block opens same DIN A4 Verlauf popup on click */}
                            <div
                                role={selectedChatId ? 'button' : undefined}
                                tabIndex={selectedChatId ? 0 : undefined}
                                onClick={selectedChatId ? () => setSessionHistoryPopoutChatId(selectedChatId) : undefined}
                                onKeyDown={selectedChatId ? (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setSessionHistoryPopoutChatId(selectedChatId); } } : undefined}
                                className={cn(selectedChatId && 'cursor-pointer hover:bg-gray-50/80 rounded-lg transition-colors -m-1 p-1')}
                            >
                                <p className="text-sm font-medium text-gray-700 mb-2">
                                    {selectedChatId ? 'Activity for this chat' : 'Recent activity'}
                                </p>
                                <div className="rounded-lg border border-gray-200 bg-gray-50/50 h-[12.5rem] overflow-y-auto">
                                    {data.activity.length === 0 ? (
                                        <p className="text-sm text-gray-500 p-3">No activity yet.</p>
                                    ) : (
                                        <ul className="divide-y divide-gray-200">
                                            {[...data.activity]
                                                .filter((a) => !selectedChatId || a.chat_id === selectedChatId)
                                                .reverse()
                                                .slice(0, 20)
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
                                            className="px-3 py-1.5 rounded bg-gray-900 text-white text-sm font-medium disabled:opacity-50 disabled:cursor-not-allowed dark:bg-[#e6e6e6] dark:text-gray-900 dark:hover:bg-white dark:shadow-none"
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

        {/* Session-Verlauf als eigenständiges Popup (Portal auf document.body), größer/höher als das Dashboard */}
        {typeof document !== 'undefined' &&
            sessionHistoryPopoutChatId &&
            createPortal(
                <div
                    className="fixed inset-0 z-[100] flex items-center justify-center p-4 bg-black/50 max-md:p-0"
                    onClick={() => setSessionHistoryPopoutChatId(null)}
                >
                    <div
                        className="bg-white rounded-xl shadow-2xl border border-gray-200 flex flex-col overflow-hidden w-[210mm] min-h-[320mm] h-[95vh] max-w-[96vw] max-md:max-w-none max-md:w-full max-md:h-[100dvh] max-md:rounded-none max-md:border-0 max-md:min-h-0"
                        onClick={e => e.stopPropagation()}
                    >
                        <div className="flex items-center justify-between px-4 py-3 border-b border-gray-200 shrink-0">
                            <h4 className="text-sm font-semibold text-gray-900">
                                @{data?.sessions?.find(s => s.chat_id === sessionHistoryPopoutChatId)?.telegram_username || sessionHistoryPopoutChatId}
                            </h4>
                            <button
                                type="button"
                                onClick={() => setSessionHistoryPopoutChatId(null)}
                                className="p-1.5 hover:bg-gray-100 rounded-lg transition-colors"
                            >
                                <X className="w-4 h-4 text-gray-500" />
                            </button>
                        </div>
                        {/* Memory Learning: X/Y bis nächstes, letztes Mal */}
                        {historyCompaction && (
                            <div className="shrink-0 px-4 py-2 bg-violet-50/80 border-b border-violet-100 text-xs text-gray-700 flex flex-wrap items-center gap-x-4 gap-y-1">
                                {(() => {
                                    const interval = Math.max(1, Number(historyCompaction.compaction_interval) || 15);
                                    const sinceLast = Math.max(
                                        0,
                                        Number(historyCompaction.user_turn_count || 0) - Number(historyCompaction.last_compaction_at_turn || 0)
                                    );
                                    const progress = sinceLast % interval;
                                    return (
                                        <span>
                                            <span className="font-medium text-violet-700">{progress}</span>
                                            <span className="text-gray-500"> / </span>
                                            <span className="font-medium">{interval}</span>
                                            {' '}Nachrichten bis Memory Learning
                                        </span>
                                    );
                                })()}
                                <span className="text-gray-500">
                                    {historyCompaction.last_compaction_at_turn === 0
                                        ? 'Letztes Memory Learning: noch keins'
                                        : `Letztes Memory Learning: nach Turn ${historyCompaction.last_compaction_at_turn}`}
                                </span>
                            </div>
                        )}
                        <div className="flex-1 min-h-0 overflow-y-auto p-4 bg-gray-50/50">
                            {historyLoading ? (
                                <p className="text-sm text-gray-500 py-4 text-center">Lade Verlauf…</p>
                            ) : sessionHistory.length === 0 ? (
                                <p className="text-sm text-gray-500 py-4 text-center">Noch keine Nachrichten in dieser Session.</p>
                            ) : (
                                <div className="space-y-2 max-w-2xl mx-auto">
                                    {sessionHistory
                                        .filter((m) => m.role === 'user' || m.role === 'assistant')
                                        .slice()
                                        .reverse()
                                        .map((msg, i) => {
                                            const isBot = msg.role === 'assistant';
                                            const text = msg.content || '—';
                                            return (
                                                <div
                                                    key={`${msg.timestamp || 'no-ts'}-${i}`}
                                                    className={cn('flex gap-3 pt-4', isBot ? 'justify-start' : 'justify-end')}
                                                >
                                                    {isBot && (
                                                        <div className="w-9 h-9 rounded-xl bg-gray-900 flex items-center justify-center text-white shadow-sm shrink-0">
                                                            <Bot className="w-[18px] h-[18px]" />
                                                        </div>
                                                    )}
                                                    <div className={cn('max-w-[85%] flex flex-col', isBot ? 'items-start' : 'items-end shrink-0')}>
                                                        <div
                                                            className={cn(
                                                                'px-5 py-3 rounded-2xl shadow-sm text-sm leading-relaxed',
                                                                isBot
                                                                    ? 'bg-white text-gray-800 rounded-tl-none border border-gray-200'
                                                                    : 'bg-gray-800 text-white rounded-tr-none'
                                                            )}
                                                        >
                                                            <p className="whitespace-pre-wrap break-words">{text}</p>
                                                        </div>
                                                        {msg.timestamp && (
                                                            <span className="text-[10px] text-gray-400 mt-1">{msg.timestamp}</span>
                                                        )}
                                                    </div>
                                                    {!isBot && (
                                                        <div className="w-9 h-9 rounded-xl bg-white border border-gray-200 flex items-center justify-center text-gray-500 shadow-sm shrink-0">
                                                            <User className="w-[18px] h-[18px]" />
                                                        </div>
                                                    )}
                                                </div>
                                            );
                                        })}
                                </div>
                            )}
                        </div>
                    </div>
                </div>,
                document.body
            )}
    </>
    );
}
