'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import React, { useState, useEffect } from 'react';
import { createPortal } from 'react-dom';
import { X, ExternalLink, MessageCircle, UserCheck, Power, Loader2, Bot, User } from 'lucide-react';
import { cn } from '@/lib/utils';

const api = (path: string) => path.startsWith('/') ? path : `/${path}`;

export interface DiscordDashboardProps {
    isOpen: boolean;
    onClose: () => void;
    config: any;
    onConfigChange: (key: string, value: any) => void;
}

interface ActivityItem {
    chat_id: string;
    ts: number;
    direction: string;
}

interface DashboardData {
    configured: boolean;
    running: boolean;
    admin_username?: string;
    admin_user_id?: string;
    enabled: boolean;
    activity: ActivityItem[];
}

export default function DiscordDashboard({ isOpen, onClose, config, onConfigChange }: DiscordDashboardProps) {
    const [data, setData] = useState<DashboardData | null>(null);
    const [loading, setLoading] = useState(false);
    const [toggling, setToggling] = useState(false);
    const [historyPopoutOpen, setHistoryPopoutOpen] = useState(false);
    const [sessionHistory, setSessionHistory] = useState<Array<{ role: string; content: string; timestamp?: string }>>([]);
    const [historyLoading, setHistoryLoading] = useState(false);
    const [historyCompaction, setHistoryCompaction] = useState<{ user_turn_count: number; compaction_interval: number; last_compaction_at_turn: number } | null>(null);

    useEffect(() => {
        if (isOpen) fetchDashboard();
    }, [isOpen, config?.discord_config]);

    useEffect(() => {
        if (!isOpen) return;
        const handleKeyDown = (e: KeyboardEvent) => {
            if (e.key === 'Escape') {
                e.preventDefault();
                e.stopPropagation();
                if (historyPopoutOpen) setHistoryPopoutOpen(false);
                else onClose();
            }
        };
        window.addEventListener('keydown', handleKeyDown, true);
        return () => window.removeEventListener('keydown', handleKeyDown, true);
    }, [isOpen, onClose, historyPopoutOpen]);

    useEffect(() => {
        if (!historyPopoutOpen || !data?.admin_user_id) {
            setSessionHistory([]);
            setHistoryCompaction(null);
            return;
        }
        const sessionId = `discord_${data.admin_user_id}`;
        setHistoryLoading(true);
        fetch(api(`api/discord/session/${encodeURIComponent(sessionId)}/history`), { credentials: 'include' })
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
    }, [historyPopoutOpen, data?.admin_user_id]);

    const fetchDashboard = async () => {
        setLoading(true);
        try {
            const res = await fetch(api('api/discord/dashboard'), { credentials: 'include' });
            const json = await res.json();
            setData({
                configured: json.configured ?? false,
                running: json.running ?? false,
                admin_username: json.admin_username,
                admin_user_id: json.admin_user_id,
                enabled: json.enabled ?? false,
                activity: Array.isArray(json.activity) ? json.activity : [],
            });
        } catch {
            setData(null);
        } finally {
            setLoading(false);
        }
    };

    const formatActivityTime = (ts: number) => {
        const d = new Date(ts * 1000);
        const now = new Date();
        const sameDay = d.toDateString() === now.toDateString();
        return sameDay ? d.toLocaleTimeString() : d.toLocaleDateString() + ' ' + d.toLocaleTimeString();
    };

    const handleToggle = async () => {
        if (!data) return;
        setToggling(true);
        try {
            const enable = !data.running;
            if (enable) {
                await fetch(api('api/discord/start'), { method: 'POST', credentials: 'include' });
            } else {
                await fetch(api('api/discord/stop'), { method: 'POST', credentials: 'include' });
            }
            const dc = config?.discord_config || {};
            onConfigChange('discord_config', { ...dc, enabled: enable });
            await fetchDashboard();
        } catch (e) {
            console.error('Discord toggle failed:', e);
        } finally {
            setToggling(false);
        }
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
                    <h3 className="text-lg font-semibold text-gray-900 min-w-0 max-md:text-lg truncate">Discord</h3>
                    <button type="button" onClick={onClose} className="p-2 hover:bg-gray-100 rounded-lg transition-colors">
                        <X className="w-5 h-5 text-gray-500" />
                    </button>
                </div>

                <div className="flex-1 overflow-y-auto px-5 py-4 space-y-5 min-w-0">
                    {loading ? (
                        <div className="py-8 text-center text-gray-500 flex items-center justify-center gap-2">
                            <Loader2 className="w-5 h-5 animate-spin" />
                            Loading…
                        </div>
                    ) : data ? (
                        <>
                            {/* Top row side by side: Bot, Bridge status, Admin */}
                            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                                {/* Bot / Developer Portal */}
                                <div className="rounded-lg border border-gray-200 p-4">
                                    <p className="text-sm font-medium text-gray-700 mb-2">Bot &amp; Settings</p>
                                    <a
                                        href="https://discord.com/developers/applications/"
                                        target="_blank"
                                        rel="noopener noreferrer"
                                        className="inline-flex items-center gap-2 px-3 py-2 rounded-lg bg-indigo-50 text-indigo-700 hover:bg-indigo-100 transition-colors text-sm"
                                    >
                                        <ExternalLink className="w-4 h-4" />
                                        Discord Developer Portal
                                    </a>
                                    <p className="text-xs text-gray-500 mt-1">
                                        Manage the bot, reset the token, or adjust the invite URL.
                                    </p>
                                </div>

                                {/* Bridge status */}
                                <div className="rounded-lg border border-gray-200 p-4">
                                    <div className="flex items-center justify-between mb-3">
                                        <div className="flex items-center gap-2">
                                            <MessageCircle className="w-4 h-4 text-gray-600" />
                                            <span className="text-sm font-medium text-gray-800">Bridge status</span>
                                        </div>
                                        <span
                                            className={cn(
                                                'text-xs px-2 py-1 rounded-full',
                                                data.running ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-500'
                                            )}
                                        >
                                            {data.running ? 'Connected' : 'Disconnected'}
                                        </span>
                                    </div>
                                    <button
                                        type="button"
                                        onClick={handleToggle}
                                        disabled={toggling || !data.configured}
                                        className={cn(
                                            'w-full flex items-center justify-center gap-2 px-3 py-2 rounded-lg text-sm font-medium transition-colors',
                                            data.running
                                                ? 'bg-gray-100 text-gray-700 hover:bg-gray-200'
                                                : 'bg-indigo-600 text-white hover:bg-indigo-700',
                                            (toggling || !data.configured) && 'opacity-50 cursor-not-allowed'
                                        )}
                                    >
                                        {toggling ? (
                                            <Loader2 className="w-4 h-4 animate-spin" />
                                        ) : (
                                            <Power className="w-4 h-4" />
                                        )}
                                        {data.running ? 'Stop bridge' : 'Start bridge'}
                                    </button>
                                </div>

                                {/* Admin */}
                                <div className="rounded-lg border border-gray-200 p-4">
                                    <div className="flex items-center gap-2 mb-1">
                                        <UserCheck className="w-4 h-4 text-gray-600" />
                                        <p className="text-sm font-medium text-gray-800">Admin</p>
                                    </div>
                                    <p className="text-gray-700">@{data.admin_username ?? '—'}</p>
                                </div>
                            </div>

                            {/* Recent activity at the bottom: max 7 visible, max 20 stored, newest first */}
                            <div
                                role="button"
                                tabIndex={0}
                                onClick={() => data.admin_user_id && setHistoryPopoutOpen(true)}
                                onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); data?.admin_user_id && setHistoryPopoutOpen(true); } }}
                                className="cursor-pointer hover:bg-gray-50/80 rounded-lg transition-colors -m-1 p-1 mt-2"
                            >
                                <p className="text-sm font-medium text-gray-700 mb-2">Recent activity</p>
                                <div className="rounded-lg border border-gray-200 bg-gray-50/50 max-h-[11rem] overflow-y-auto">
                                    {(data.activity?.length ?? 0) === 0 ? (
                                        <p className="text-sm text-gray-500 p-3">No activity yet.</p>
                                    ) : (
                                        <ul className="divide-y divide-gray-200">
                                            {[...(data.activity ?? [])]
                                                .reverse()
                                                .slice(0, 7)
                                                .map((a, i) => (
                                                    <li key={i} className="flex items-center gap-2 px-3 py-2 text-sm text-gray-600">
                                                        <MessageCircle className="w-4 h-4 text-gray-400 shrink-0" />
                                                        <span>{formatActivityTime(a.ts)}</span>
                                                        <span className="text-gray-400">·</span>
                                                        <span>{a.direction === 'in' ? 'Incoming' : 'Outgoing'}</span>
                                                    </li>
                                                ))}
                                        </ul>
                                    )}
                                </div>
                                <p className="text-xs text-gray-500 mt-1">Click for chat history (max. 7 of 20 events)</p>
                            </div>
                        </>
                    ) : (
                        <div className="py-8 text-center text-gray-500">Could not load dashboard.</div>
                    )}
                </div>
            </div>
        </div>

        {/* History popup (same as Telegram) */}
        {typeof document !== 'undefined' &&
            historyPopoutOpen &&
            data?.admin_username &&
            createPortal(
                <div
                    className="fixed inset-0 z-[100] flex items-center justify-center p-4 bg-black/50 max-md:p-0"
                    onClick={() => setHistoryPopoutOpen(false)}
                >
                    <div
                        className="bg-white rounded-xl shadow-2xl border border-gray-200 flex flex-col overflow-hidden w-[210mm] min-h-[320mm] h-[95vh] max-w-[96vw] max-md:max-w-none max-md:h-[100dvh] max-md:rounded-none max-md:border-0 max-md:min-h-0"
                        onClick={(e) => e.stopPropagation()}
                    >
                        <div className="flex items-center justify-between px-4 py-3 border-b border-gray-200 shrink-0 max-md:px-4 max-md:py-3">
                            <h4 className="text-sm font-semibold text-gray-900 min-w-0 truncate">@{data.admin_username} – Discord</h4>
                            <button
                                type="button"
                                onClick={() => setHistoryPopoutOpen(false)}
                                className="p-1.5 hover:bg-gray-100 rounded-lg transition-colors"
                            >
                                <X className="w-4 h-4 text-gray-500" />
                            </button>
                        </div>
                        {historyCompaction && (
                            <div className="shrink-0 px-4 py-2 bg-violet-50/80 border-b border-violet-100 text-xs text-gray-700 flex flex-wrap items-center gap-x-4 gap-y-1">
                                <span>
                                    <span className="font-medium text-violet-700">{Math.max(0, historyCompaction.user_turn_count - historyCompaction.last_compaction_at_turn)}</span>
                                    <span className="text-gray-500"> / </span>
                                    <span className="font-medium">{historyCompaction.compaction_interval}</span>
                                    {' '}messages until Memory Learning
                                </span>
                                <span className="text-gray-500">
                                    {historyCompaction.last_compaction_at_turn === 0
                                        ? 'Last Memory Learning: none yet'
                                        : `Last Memory Learning: after turn ${historyCompaction.last_compaction_at_turn}`}
                                </span>
                            </div>
                        )}
                        <div className="flex-1 min-h-0 overflow-y-auto p-4 bg-gray-50/50">
                            {historyLoading ? (
                                <p className="text-sm text-gray-500 py-4 text-center">Loading history…</p>
                            ) : sessionHistory.length === 0 ? (
                                <p className="text-sm text-gray-500 py-4 text-center">No messages in this session yet.</p>
                            ) : (
                                <div className="space-y-2 max-w-2xl mx-auto">
                                    {sessionHistory
                                        .filter((m) => m.role === 'user' || m.role === 'assistant')
                                        .map((msg, i) => {
                                            const isBot = msg.role === 'assistant';
                                            const text = msg.content || '—';
                                            return (
                                                <div
                                                    key={i}
                                                    className={cn('flex gap-3 pt-4', isBot ? 'justify-start' : 'justify-end')}
                                                >
                                                    {isBot && (
                                                        <div className="w-9 h-9 rounded-xl bg-indigo-600 flex items-center justify-center text-white shadow-sm shrink-0">
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
