'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import React, { useState, useEffect } from 'react';
import { createPortal } from 'react-dom';
import { X, MessageSquare, UserCheck, UserPlus, Trash2, Bot, User, RefreshCw } from 'lucide-react';
import { cn } from '@/lib/utils';
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
    const [sessionHistoryPopoutChatId, setSessionHistoryPopoutChatId] = useState<string | null>(null);
    const [sessionHistory, setSessionHistory] = useState<Array<{ role: string; content: string; timestamp?: string }>>([]);
    const [historyCompaction, setHistoryCompaction] = useState<{ user_turn_count: number; compaction_interval: number; last_compaction_at_turn: number } | null>(null);
    const [historyLoading, setHistoryLoading] = useState(false);
    const [relayAddId, setRelayAddId] = useState('');
    const [relayAddUsername, setRelayAddUsername] = useState('');
    const [restarting, setRestarting] = useState(false);
    const [restartError, setRestartError] = useState<string | null>(null);

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

    const getSessionForChat = (chatId: string) => data?.sessions?.find((s) => s.chat_id === chatId);
    const selectedSession = sessionHistoryPopoutChatId ? getSessionForChat(sessionHistoryPopoutChatId) : null;
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
                                title={data?.connected ? 'Alle Chats von WhatsApp laden & Aktualisieren' : 'Aktualisieren; startet Bridge falls nötig'}
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
                                                    onClick={() => {
                                                        setSelectedChatId(s.chat_id);
                                                        setSessionHistoryPopoutChatId(s.chat_id);
                                                    }}
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
                                                        LID-Chat: Die Linked-Device-API liefert oft keine Nummer (auch bei gespeichertem Kontakt auf dem Handy). Nur Lesen. Für Agent-Antwort: whatsapp_config.lid_to_e164 in der Config.
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
                    <div className="flex-1 overflow-y-auto px-5 py-4 space-y-5 min-w-0 max-md:min-h-0 max-md:shrink-0">
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
                                            className="px-3 py-1.5 rounded bg-gray-900 text-white text-sm font-medium disabled:opacity-50 disabled:cursor-not-allowed dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-white dark:shadow-none"
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
            selectedSession &&
            createPortal(
                <div
                    className="fixed inset-0 z-[100] flex items-center justify-center p-4 bg-black/50 max-md:p-0"
                    onClick={() => setSessionHistoryPopoutChatId(null)}
                >
                    <div
                        className="bg-white rounded-xl shadow-2xl border border-gray-200 flex flex-col overflow-hidden w-[210mm] min-h-[320mm] h-[95vh] max-w-[96vw] max-md:max-w-none max-md:h-[100dvh] max-md:rounded-none max-md:border-0 max-md:min-h-0"
                        onClick={e => e.stopPropagation()}
                    >
                        <div className="flex items-center justify-between px-4 py-3 border-b border-gray-200 shrink-0">
                            <h4 className="text-sm font-semibold text-gray-900">
                                {selectedSession.phone_number}
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
                                <span>
                                    <span className="font-medium text-violet-700">{Math.max(0, historyCompaction.user_turn_count - historyCompaction.last_compaction_at_turn)}</span>
                                    <span className="text-gray-500"> / </span>
                                    <span className="font-medium">{historyCompaction.compaction_interval}</span>
                                    {' '}Nachrichten bis Memory Learning
                                </span>
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
                                        .map((msg, i) => {
                                            const isBot = msg.role === 'assistant';
                                            const text = msg.content || '—';
                                            return (
                                                <div
                                                    key={i}
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
