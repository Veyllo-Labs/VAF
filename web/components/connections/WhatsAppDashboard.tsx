'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import React, { useState, useEffect, useMemo, useRef } from 'react';
import { X, MessageSquare, UserCheck, UserPlus, Trash2, Bot, User, RefreshCw, Search, ChevronUp, ChevronDown } from 'lucide-react';
import { cn, stripThinkBlocks } from '@/lib/utils';
import HighlightedText from './HighlightedText';
import MessagesChart from './MessagesChart';

const api = (path: string) => path.startsWith('/') ? path : `/${path}`;

export interface WhatsAppDashboardProps {
    isOpen: boolean;
    onClose: () => void;
    config: any;
    onConfigChange: (key: string, value: any) => void;
    onOpenSetupWizard?: () => void;
}

interface WhatsAppSession {
    chat_id: string;
    phone_number: string;
    name?: string | null;
    vaf_username?: string | null;
    session_id?: string;
    type: string;
    last_ts: number;
    message_count: number;
    answerable?: boolean;
    needs_assign?: boolean;
    display_name?: string | null;
    resolved_e164?: string | null;
}

interface Stats4hBucket {
    bucket_ts: number;
    count: number;
}

interface LidChatToAssign {
    lid_jid: string;
    chat_id: string;
    name?: string | null;
    session_id?: string;
    resolved_e164_from_config?: string | null;
    resolved_e164_from_node?: string | null;
}

interface DashboardData {
    bot_link: string | null;
    linked?: boolean;
    sessions: WhatsAppSession[];
    stats_4h: Stats4hBucket[];
    admin_whitelist: Array<{ phone_number: string; vaf_username?: string | null }>;
    relay_whitelist: Array<{ phone_number: string; vaf_username?: string | null }>;
    front_office_contacts: Array<{ name: string | null; phone_number: string }>;
    lid_chats_to_assign: LidChatToAssign[];
    activity: Array<{ chat_id: string; user_scope_id: string | null; ts: number; direction: string }>;
    connected?: boolean;
    running?: boolean;
    log_path?: string | null;
}

export default function WhatsAppDashboard({ isOpen, onClose, config, onConfigChange, onOpenSetupWizard }: WhatsAppDashboardProps) {
    const [data, setData] = useState<DashboardData | null>(null);
    const [loading, setLoading] = useState(false);
    const [selectedChatId, setSelectedChatId] = useState<string | null>(null);
    const [sessionHistory, setSessionHistory] = useState<Array<{ role: string; content: string; timestamp?: string }>>([]);
    const [historyCompaction, setHistoryCompaction] = useState<{ user_turn_count: number; compaction_interval: number; last_compaction_at_turn: number } | null>(null);
    const [historyLoading, setHistoryLoading] = useState(false);
    const [relayAddId, setRelayAddId] = useState('');
    const [relayAddUsername, setRelayAddUsername] = useState('');
    const [restarting, setRestarting] = useState(false);
    const [restartError, setRestartError] = useState<string | null>(null);
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
    }, [isOpen, config?.whatsapp_config]);

    const handleRelink = async () => {
        await fetch(api('api/whatsapp/qr/reset'), { method: 'POST', credentials: 'include' });
        onClose();
        onOpenSetupWizard?.();
    };

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

    const getSessionForChat = (chatId: string) => data?.sessions?.find((s) => s.chat_id === chatId);
    const selectedSession = selectedChatId ? getSessionForChat(selectedChatId) : null;
    const historySessionId = selectedSession?.session_id;

    useEffect(() => {
        if (!historySessionId || !isOpen) {
            setSessionHistory([]);
            setHistoryCompaction(null);
            return;
        }
        setHistoryLoading(true);
        fetch(api(`api/whatsapp/session/${encodeURIComponent(historySessionId)}/history`), { credentials: 'include' })
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
    }, [historySessionId, isOpen]);

    const setDataFromJson = (json: any) => {
        const whitelist = Array.isArray(json?.whitelist) ? json.whitelist : [];
        const sessions = Array.isArray(json?.sessions) ? json.sessions : [];
        setData({
            bot_link: json?.linked ? 'https://web.whatsapp.com' : null,
            linked: json?.linked === true,
            sessions,
            stats_4h: Array.isArray(json?.stats_4h) ? json.stats_4h : [],
            admin_whitelist: whitelist,
            relay_whitelist: [],
            front_office_contacts: Array.isArray(json?.front_office_contacts) ? json.front_office_contacts : [],
            lid_chats_to_assign: Array.isArray(json?.lid_chats_to_assign) ? json.lid_chats_to_assign : [],
            activity: Array.isArray(json?.activity) ? json.activity : [],
            connected: json?.connected === true,
            running: json?.running === true,
            log_path: json?.log_path || null,
        });
        if (!selectedChatId && sessions.length > 0) {
            setSelectedChatId(sessions[0]?.chat_id ?? null);
        }
    };

    const fetchDashboard = async () => {
        setLoading(true);
        try {
            const res = await fetch(api('api/whatsapp/dashboard'), { credentials: 'include' });
            const json = await res.json();
            if (!res.ok) {
                console.warn('WhatsApp dashboard API error:', res.status, json);
                setData(null);
                return;
            }
            setDataFromJson(json);
        } catch {
            setData(null);
        } finally {
            setLoading(false);
        }
    };

    const handleRefresh = async () => {
        setLoading(true);
        try {
            const res = await fetch(api('api/whatsapp/dashboard'), { credentials: 'include' });
            const json = await res.json();
            if (!res.ok) {
                setData(null);
                return;
            }
            setDataFromJson(json);
            if (json.connected) {
                await fetch(api('api/whatsapp/sync-chats'), { method: 'POST', credentials: 'include' });
                const res2 = await fetch(api('api/whatsapp/dashboard'), { credentials: 'include' });
                const json2 = await res2.json();
                if (res2.ok) setDataFromJson(json2);
            } else if (!json.running && json.enabled) {
                await fetch(api('api/whatsapp/start'), { method: 'POST', credentials: 'include' });
                await new Promise(r => setTimeout(r, 2000));
                const res2 = await fetch(api('api/whatsapp/dashboard'), { credentials: 'include' });
                const json2 = await res2.json();
                if (res2.ok) setDataFromJson(json2);
            }
        } catch {
            setData(null);
        } finally {
            setLoading(false);
        }
    };

    const handleRestartBridge = async () => {
        setRestarting(true);
        setRestartError(null);
        try {
            const wc = config?.whatsapp_config || {};
            await fetch(api('api/config'), {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ whatsapp_config: { ...wc, enabled: true } }),
                credentials: 'include',
            });
            onConfigChange('whatsapp_config', { ...wc, enabled: true });
            const res = await fetch(api('api/whatsapp/restart'), { method: 'POST', credentials: 'include' });
            const json = await res.json().catch(() => ({}));
            if (!res.ok) {
                setRestartError(json?.detail || json?.message || `Failed (${res.status})`);
                return;
            }
            await new Promise(r => setTimeout(r, 3000));
            await fetchDashboard();
        } catch (e) {
            setRestartError(e instanceof Error ? e.message : 'Failed. Check console.');
        } finally {
            setRestarting(false);
        }
    };

    const handleRelayAdd = async () => {
        const phone = relayAddId.trim();
        if (!phone) return;
        try {
            await fetch(api('api/whatsapp/whitelist/add'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({ phone_number: phone, vaf_username: relayAddUsername.trim() || undefined }),
            });
            setRelayAddId('');
            setRelayAddUsername('');
            onConfigChange('whatsapp_config', { ...config.whatsapp_config, whitelist: [...(config.whatsapp_config?.whitelist || []), { phone_number: phone, vaf_username: relayAddUsername.trim() || null }] });
            fetchDashboard();
        } catch (e) {
            console.error(e);
        }
    };

    const handleRelayRemove = async (phone_number: string) => {
        try {
            await fetch(api('api/whatsapp/whitelist/remove'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({ phone_number }),
            });
            const current = config.whatsapp_config?.whitelist || [];
            onConfigChange('whatsapp_config', { ...config.whatsapp_config, whitelist: current.filter((e: any) => String(e.phone_number) !== phone_number) });
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
                    <h3 className="text-lg font-semibold text-gray-900 max-md:text-lg truncate">WhatsApp</h3>
                    <button type="button" onClick={onClose} className="p-2 hover:bg-gray-100 rounded-lg transition-colors">
                        <X className="w-5 h-5 text-gray-500" />
                    </button>
                </div>

                <div className="flex-1 flex min-h-0 max-md:flex-col">
                    {/* Left sidebar: session list (chats for this bot only) */}
                    <div className="w-56 shrink-0 border-r border-gray-200 flex flex-col bg-gray-50/50 max-md:w-full max-md:max-h-[38vh] max-md:border-r-0 max-md:border-b">
                        <div className="px-3 py-2 border-b border-gray-200 flex items-center justify-between gap-2">
                            <div className="flex items-center gap-2 min-w-0">
                                <p className="text-xs font-medium text-gray-500 uppercase tracking-wide">Chats</p>
                                {data && (
                                    <span
                                        className={cn(
                                            'shrink-0 w-2 h-2 rounded-full',
                                            data.connected ? 'bg-green-500' : data.running ? 'bg-amber-500' : 'bg-gray-400'
                                        )}
                                        title={
                                            data.connected
                                                ? 'WhatsApp connected'
                                                : data.running
                                                    ? 'Bridge running, WhatsApp not connected. Check logs/whatsapp_qr.log for connection=close and status code (401 → Reset + QR; 515/516 → wait or Restart bridge).'
                                                    : 'Bridge not started'
                                        }
                                    />
                                )}
                            </div>
                            <button
                                type="button"
                                onClick={(e) => { e.stopPropagation(); handleRefresh(); }}
                                disabled={loading}
                                className="p-1.5 rounded hover:bg-gray-200 text-gray-500 hover:text-gray-700 transition-colors disabled:opacity-50"
                                title={data?.connected ? 'Load all chats from WhatsApp & refresh' : 'Refresh; starts bridge if needed'}
                            >
                                <RefreshCw className={cn('w-4 h-4', loading && 'animate-spin')} />
                            </button>
                        </div>
                        <div className="flex-1 overflow-y-auto">
                            {loading ? (
                                <div className="p-3 text-sm text-gray-500">Loading…</div>
                            ) : data?.sessions && data.sessions.length > 0 ? (
                                <ul className="py-1">
                                    {data.sessions.map((s) => {
                                        const title = s.display_name ?? s.name ?? s.phone_number ?? s.chat_id;
                                        const lidJid = (s.chat_id && String(s.chat_id).includes('@lid')) ? s.chat_id : null;
                                        const showAssign = s.needs_assign && lidJid;
                                        return (
                                        <li key={s.chat_id} className="border-b border-gray-100 last:border-0">
                                            <div className="flex flex-col gap-1">
                                                <button
                                                    type="button"
                                                    onClick={() => setSelectedChatId(s.chat_id)}
                                                    className={cn(
                                                        'w-full text-left px-3 py-2 flex flex-col gap-0.5 transition-colors border-l-2',
                                                        selectedChatId === s.chat_id
                                                            ? 'bg-sky-100 text-sky-900 border-sky-500'
                                                            : 'border-transparent hover:bg-gray-100 text-gray-700'
                                                    )}
                                                >
                                                    <span className="text-sm font-medium truncate" title={s.chat_id}>
                                                        {title}
                                                    </span>
                                                    <span className="flex items-center gap-1.5 text-xs text-gray-500 flex-wrap">
                                                        {s.answerable ? (
                                                            <span className="px-1.5 py-0.5 rounded bg-green-100 text-green-700">Agent</span>
                                                        ) : (
                                                            <span className="px-1.5 py-0.5 rounded bg-gray-200 text-gray-600">Read-only</span>
                                                        )}
                                                        {s.message_count > 0 && <span>{s.message_count} msgs</span>}
                                                    </span>
                                                    {s.last_ts > 0 && (
                                                        <span className="text-xs text-gray-400">{formatActivityTime(s.last_ts)}</span>
                                                    )}
                                                </button>
                                                {showAssign && (
                                                    <p className="px-3 py-0.5 text-[10px] text-gray-500 leading-none">
                                                        LID chat: The linked-device API often does not return a phone number (even for a contact saved on the phone). Read-only. For agent replies, set whatsapp_config.lid_to_e164 in the config.
                                                    </p>
                                                )}
                                            </div>
                                        </li>
                                    );})}
                                </ul>
                            ) : (
                                <p className="p-3 text-sm text-gray-500">No chats. Restart bridge (Settings → Connections) and wait 1–2 min.</p>
                            )}
                        </div>
                    </div>

                    {/* Main content */}
                    <div className="flex-1 overflow-y-auto px-5 py-4 space-y-5 min-w-0 flex flex-col max-md:min-h-0 max-md:shrink-0">
                    {data && !data.running && !data.linked && (
                        <div className="rounded-lg border border-gray-200 bg-gray-50 p-4 text-sm text-gray-700">
                            <p className="font-medium mb-1">Bridge not started</p>
                            <p className="mb-3">Turn the WhatsApp toggle ON (Connections), or Start bridge below.</p>
                            {restartError && <p className="mb-3 text-red-600 font-medium">{restartError}</p>}
                            <button
                                type="button"
                                onClick={handleRestartBridge}
                                disabled={restarting || loading}
                                className="px-4 py-2 rounded-lg bg-green-600 text-white font-medium hover:bg-green-700 disabled:opacity-50 transition-colors"
                            >
                                {restarting ? 'Starting…' : 'Start bridge'}
                            </button>
                        </div>
                    )}
                    {data && !data.running && data.linked && (
                        <div className="rounded-lg border border-gray-200 bg-gray-50 p-4 text-sm text-gray-700">
                            <p className="font-medium mb-3">Session expired.</p>
                            <button
                                type="button"
                                onClick={handleRelink}
                                className="px-4 py-2 rounded-lg bg-green-600 text-white font-medium hover:bg-green-700"
                            >
                                Re-link (opens setup)
                            </button>
                        </div>
                    )}
                    {loading ? (
                        <div className="py-8 text-center text-gray-500">Loading…</div>
                    ) : data ? (
                        <>
                            {/* Line chart: messages per 4-hour interval */}
                            <MessagesChart buckets={data?.stats_4h ?? []} chartId="whatsapp-messages-chart" />

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
                                    <p className="text-xs text-gray-500 mb-3">These users can use the full agent (tools, memory) via WhatsApp.</p>
                                    <ul className="space-y-2">
                                        {(data.admin_whitelist || []).map((e, i) => (
                                            <li key={i} className="flex items-center justify-between text-sm py-1.5 px-2 rounded bg-gray-50">
                                                <span className="text-gray-700">{e.phone_number}</span>
                                                {e.vaf_username && <span className="text-gray-500 text-xs">{e.vaf_username}</span>}
                                            </li>
                                        ))}
                                        {(!data.admin_whitelist || data.admin_whitelist.length === 0) && (
                                            <li className="text-sm text-gray-500">None. Add yourself in the setup wizard.</li>
                                        )}
                                    </ul>
                                </div>

                                {/* Front Office contacts (Can reach your assistant – full agent via WhatsApp) */}
                                {(data.front_office_contacts?.length ?? 0) > 0 && (
                                    <div className="rounded-lg border border-gray-200 p-4">
                                        <div className="flex items-center gap-2 mb-3">
                                            <UserCheck className="w-4 h-4 text-gray-600" />
                                            <p className="text-sm font-medium text-gray-800">Front Office contacts (WhatsApp)</p>
                                        </div>
                                        <p className="text-xs text-gray-500 mb-3">These contacts can message you with the full assistant (Settings → Connections → Contacts, &quot;Can reach your assistant&quot;).</p>
                                        <ul className="space-y-2">
                                            {data.front_office_contacts.map((c, i) => (
                                                <li key={i} className="text-sm py-1.5 px-2 rounded bg-gray-50 text-gray-700">
                                                    {c.name ? <span className="font-medium">{c.name}</span> : null}
                                                    {c.name ? ' · ' : null}
                                                    <span className="text-gray-600">{c.phone_number}</span>
                                                </li>
                                            ))}
                                        </ul>
                                    </div>
                                )}
                                {/* Relay whitelist (message-only, no tools) – add phone to main whitelist */}
                                <div className="rounded-lg border border-gray-200 p-4">
                                    <div className="flex items-center gap-2 mb-3">
                                        <UserPlus className="w-4 h-4 text-gray-600" />
                                        <p className="text-sm font-medium text-gray-800">Add to whitelist</p>
                                    </div>
                                    <p className="text-xs text-gray-500 mb-3">Add a phone number so they can send and receive messages. For relay-only (fixed reply, no tools) use Telegram relay in Settings → Telegram.</p>
                                    <ul className="space-y-2 mb-3">
                                        {(data.relay_whitelist || []).map((e, i) => (
                                            <li key={i} className="flex items-center justify-between text-sm py-1.5 px-2 rounded bg-gray-50">
                                                <span className="text-gray-700">{e.phone_number}</span>
                                                <button
                                                    type="button"
                                                    onClick={() => {
                                                        if (!confirm('Are you sure you want to remove this relay contact?')) return;
                                                        handleRelayRemove(e.phone_number);
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
                                            placeholder="Phone number"
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
