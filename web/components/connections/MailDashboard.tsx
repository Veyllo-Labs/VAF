'use client';

import React, { useState, useEffect, useRef } from 'react';
import { X, Mail, Loader2, UserPlus, RefreshCw, Inbox } from 'lucide-react';
import { cn } from '@/lib/utils';

const api = (path: string) => path.startsWith('/') ? path : `/${path}`;

const AUTO_SYNC_INTERVAL_MS = 30 * 60 * 1000; // 30 min

export interface MailDashboardProps {
    isOpen: boolean;
    onClose: () => void;
    /** Call to open the Add Account wizard (EmailSetupWizard). */
    onOpenAddWizard: () => void;
    /** Increment to force refetch of accounts (e.g. after wizard closes). */
    refreshTrigger?: number;
}

interface EmailAccount {
    account_id: string;
    email: string;
    provider: string;
    enabled?: boolean;
    last_verified_at?: string;
    auto_sync_enabled?: boolean;
}

interface SyncedMessage {
    account_id: string;
    folder: string;
    message_id: string;
    subject: string;
    from: string;
    date: string;
    body_snippet: string;
    synced_at: string;
}

export default function MailDashboard({ isOpen, onClose, onOpenAddWizard, refreshTrigger = 0 }: MailDashboardProps) {
    const [accounts, setAccounts] = useState<EmailAccount[]>([]);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState('');
    const [syncLoading, setSyncLoading] = useState<string | null>(null);
    const [messages, setMessages] = useState<SyncedMessage[]>([]);
    const [messagesLoading, setMessagesLoading] = useState(false);
    const [messagesOffset, setMessagesOffset] = useState(0);
    const [selectedAccountId, setSelectedAccountId] = useState<string | null>(null);
    const autoSyncTimersRef = useRef<Record<string, ReturnType<typeof setInterval>>>({});

    const fetchAccounts = async () => {
        try {
            const res = await fetch(api('api/email/accounts'), { credentials: 'include' });
            if (res.ok) {
                const data = await res.json();
                setAccounts(data.accounts || []);
            }
        } catch {
            setAccounts([]);
        }
    };

    const fetchMessages = async (accountId: string | null, offset: number, append: boolean) => {
        setMessagesLoading(true);
        try {
            const params = new URLSearchParams({ folder: 'INBOX', limit: '50', offset: String(offset) });
            if (accountId) params.set('account_id', accountId);
            const res = await fetch(api(`api/email/messages?${params}`), { credentials: 'include' });
            if (res.ok) {
                const data = await res.json();
                const list = data.messages || [];
                setMessages(prev => append ? [...prev, ...list] : list);
            }
        } catch {
            if (!append) setMessages([]);
        } finally {
            setMessagesLoading(false);
        }
    };

    useEffect(() => {
        if (isOpen) fetchAccounts();
    }, [isOpen, refreshTrigger]);

    useEffect(() => {
        if (isOpen && accounts.length > 0) {
            setMessagesOffset(0);
            fetchMessages(selectedAccountId, 0, false);
        }
    }, [isOpen, selectedAccountId, accounts.length]);

    useEffect(() => {
        if (!isOpen) return;
        const handleKeyDown = (e: KeyboardEvent) => {
            if (e.key === 'Escape') {
                e.preventDefault();
                onClose();
            }
        };
        window.addEventListener('keydown', handleKeyDown, true);
        return () => window.removeEventListener('keydown', handleKeyDown, true);
    }, [isOpen, onClose]);

    const handleSyncAccount = async (accountId: string) => {
        setSyncLoading(accountId);
        setError('');
        try {
            const res = await fetch(api(`api/email/accounts/${encodeURIComponent(accountId)}/sync?max_messages=100`), {
                method: 'POST',
                credentials: 'include',
            });
            const data = await res.json();
            if (data.ok) {
                await fetchAccounts();
                setMessagesOffset(0);
                fetchMessages(selectedAccountId, 0, false);
            } else {
                setError(data.error || 'Sync failed');
            }
        } catch {
            setError('Sync request failed');
        } finally {
            setSyncLoading(null);
        }
    };

    const handleAutoSyncToggle = async (accountId: string, enabled: boolean) => {
        try {
            await fetch(api(`api/email/accounts/${encodeURIComponent(accountId)}`), {
                method: 'PATCH',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ auto_sync_enabled: enabled }),
            });
            await fetchAccounts();
        } catch {
            setError('Failed to update auto-sync');
            return;
        }
        if (enabled) {
            const timer = setInterval(() => handleSyncAccount(accountId), AUTO_SYNC_INTERVAL_MS);
            autoSyncTimersRef.current[accountId] = timer;
        } else {
            const t = autoSyncTimersRef.current[accountId];
            if (t) {
                clearInterval(t);
                delete autoSyncTimersRef.current[accountId];
            }
        }
    };

    useEffect(() => {
        if (!isOpen) {
            Object.values(autoSyncTimersRef.current).forEach(clearInterval);
            autoSyncTimersRef.current = {};
        }
        return () => {
            Object.values(autoSyncTimersRef.current).forEach(clearInterval);
            autoSyncTimersRef.current = {};
        };
    }, [isOpen]);

    useEffect(() => {
        if (!isOpen || accounts.length === 0) return;
        accounts.forEach((a) => {
            const id = a.account_id || a.email;
            if (a.auto_sync_enabled && !autoSyncTimersRef.current[id]) {
                autoSyncTimersRef.current[id] = setInterval(() => handleSyncAccount(id), AUTO_SYNC_INTERVAL_MS);
            }
        });
    }, [isOpen, accounts]);

    const handleRemoveAccount = async (accountId: string) => {
        try {
            await fetch(api(`api/email/accounts/${encodeURIComponent(accountId)}`), {
                method: 'DELETE',
                credentials: 'include',
            });
            await fetchAccounts();
        } catch {
            setError('Failed to remove account');
        }
    };

    const formatLastVerified = (iso?: string) => {
        if (!iso) return null;
        try {
            const d = new Date(iso);
            if (Number.isNaN(d.getTime())) return null;
            const now = new Date();
            const diffMs = now.getTime() - d.getTime();
            if (diffMs < 60_000) return 'Just now';
            if (diffMs < 3600_000) return `${Math.floor(diffMs / 60_000)} min ago`;
            if (diffMs < 86400_000) return `${Math.floor(diffMs / 3600_000)} h ago`;
            return d.toLocaleDateString();
        } catch {
            return null;
        }
    };

    if (!isOpen) return null;

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50" onClick={onClose}>
            <div
                className={cn(
                    'relative bg-white w-full max-w-[95vw] h-[90vh] rounded-2xl shadow-2xl border border-gray-200 flex flex-col overflow-hidden'
                )}
                onClick={e => e.stopPropagation()}
            >
                <div className="flex items-center justify-between px-5 py-4 border-b border-gray-200 shrink-0">
                    <div className="flex items-center gap-3">
                        <div className="w-10 h-10 rounded-xl bg-red-500 flex items-center justify-center">
                            <Mail className="w-5 h-5 text-white" />
                        </div>
                        <div>
                            <h3 className="text-lg font-semibold text-gray-900">Email</h3>
                            <p className="text-xs text-gray-500">Manage your accounts</p>
                        </div>
                    </div>
                    <button type="button" onClick={onClose} className="p-2 hover:bg-gray-100 rounded-lg transition-colors">
                        <X className="w-5 h-5 text-gray-500" />
                    </button>
                </div>

                <div className="flex-1 flex min-h-0 overflow-hidden">
                    {/* Left sidebar: accounts + Add account */}
                    <aside className="w-72 shrink-0 flex flex-col border-r border-gray-200 bg-gray-50/80 overflow-hidden">
                        {error && (
                            <div className="mx-3 mt-3 p-2 rounded-lg bg-red-50 border border-red-200 text-xs text-red-700">
                                {error}
                            </div>
                        )}
                        <div className="flex-1 overflow-y-auto p-3">
                            {loading ? (
                                <div className="flex items-center justify-center py-8">
                                    <Loader2 className="w-6 h-6 animate-spin text-gray-400" />
                                </div>
                            ) : accounts.length === 0 ? (
                                <div className="flex flex-col items-center justify-center py-8 text-center">
                                    <p className="text-sm text-gray-600">No accounts yet</p>
                                    <button
                                        type="button"
                                        onClick={onOpenAddWizard}
                                        className="mt-4 w-full inline-flex items-center justify-center gap-2 px-3 py-2.5 rounded-xl bg-gray-900 text-white text-sm font-medium hover:bg-gray-800 transition-colors"
                                    >
                                        <UserPlus className="w-4 h-4" />
                                        Add account
                                    </button>
                                </div>
                            ) : (
                                <>
                                    <ul className="space-y-2">
                                        {accounts.map((a) => {
                                            const id = a.account_id || a.email;
                                            const lastSynced = formatLastVerified(a.last_verified_at);
                                            const isSelected = selectedAccountId === id;
                                            return (
                                                <li
                                                    key={id}
                                                    className={cn(
                                                        'p-3 rounded-xl border shadow-sm space-y-2 transition-colors',
                                                        isSelected ? 'border-gray-400 bg-white ring-1 ring-gray-300' : 'border-gray-200 bg-white'
                                                    )}
                                                >
                                                    <div
                                                        className="cursor-pointer min-w-0"
                                                        onClick={() => setSelectedAccountId(prev => prev === id ? null : id)}
                                                        role="button"
                                                        tabIndex={0}
                                                        onKeyDown={(e) => e.key === 'Enter' && setSelectedAccountId(prev => prev === id ? null : id)}
                                                    >
                                                        <div className="flex items-start justify-between gap-2">
                                                            <div className="min-w-0 flex-1">
                                                                <span className="font-medium text-gray-900 text-sm truncate block">{a.email || a.account_id}</span>
                                                                <span className="text-xs text-gray-500">{a.provider}</span>
                                                            </div>
                                                            <div className="flex items-center gap-1 shrink-0">
                                                                {(a.provider === 'imap' || a.provider === 'gmail' || a.provider === 'microsoft') && (
                                                                    <button
                                                                        onClick={(e) => { e.stopPropagation(); handleSyncAccount(id); }}
                                                                        disabled={syncLoading === id}
                                                                        className="text-xs text-gray-600 hover:text-gray-900 disabled:opacity-50 px-1.5 py-1 rounded hover:bg-gray-200"
                                                                        title="Sync now"
                                                                    >
                                                                        {syncLoading === id ? <Loader2 className="w-3.5 h-3.5 animate-spin inline" /> : <RefreshCw className="w-3.5 h-3.5 inline" />}
                                                                    </button>
                                                                )}
                                                                <button
                                                                    onClick={(e) => { e.stopPropagation(); handleRemoveAccount(id); }}
                                                                    className="text-xs text-red-600 hover:text-red-700 px-1.5 py-1 rounded hover:bg-red-50"
                                                                >
                                                                    Remove
                                                                </button>
                                                            </div>
                                                        </div>
                                                        {lastSynced != null && (
                                                            <p className="text-xs text-gray-500">Last synced: {lastSynced}</p>
                                                        )}
                                                        <div className="flex items-center justify-between gap-2 mt-1.5" onClick={e => e.stopPropagation()}>
                                                            <span className="text-xs text-gray-500">Auto-sync every 30 min</span>
                                                            <button
                                                                type="button"
                                                                role="switch"
                                                                aria-checked={a.auto_sync_enabled ?? false}
                                                                className={cn(
                                                                    'relative inline-flex h-5 w-9 shrink-0 rounded-full border transition-colors',
                                                                    (a.auto_sync_enabled ?? false) ? 'bg-gray-900 border-gray-900' : 'bg-gray-200 border-gray-200'
                                                                )}
                                                                onClick={() => handleAutoSyncToggle(id, !(a.auto_sync_enabled ?? false))}
                                                            >
                                                                <span
                                                                    className={cn(
                                                                        'pointer-events-none inline-block h-4 w-4 rounded-full bg-white shadow ring-0 transition translate-y-0.5',
                                                                        (a.auto_sync_enabled ?? false) ? 'translate-x-4' : 'translate-x-0.5'
                                                                    )}
                                                                />
                                                            </button>
                                                        </div>
                                                    </div>
                                                </li>
                                            );
                                        })}
                                    </ul>
                                    <button
                                        type="button"
                                        onClick={onOpenAddWizard}
                                        className="mt-3 w-full py-2.5 rounded-xl border-2 border-dashed border-gray-200 text-sm font-medium text-gray-600 hover:border-gray-300 hover:bg-white transition-colors inline-flex items-center justify-center gap-2"
                                    >
                                        <UserPlus className="w-4 h-4" />
                                        Add another account
                                    </button>
                                </>
                            )}
                        </div>
                    </aside>
                    {/* Main content area: synced mail list */}
                    <main className="flex-1 min-w-0 flex flex-col overflow-hidden bg-white">
                        {accounts.length === 0 && !loading && (
                            <div className="flex flex-col items-center justify-center flex-1 text-center max-w-sm mx-auto p-8">
                                <div className="w-14 h-14 rounded-2xl bg-gray-100 flex items-center justify-center mb-3">
                                    <Mail className="w-7 h-7 text-gray-400" />
                                </div>
                                <p className="text-gray-600 font-medium">Manage your email accounts</p>
                                <p className="text-sm text-gray-500 mt-1">Add an account in the sidebar to get started.</p>
                            </div>
                        )}
                        {accounts.length > 0 && (
                            <>
                                <div className="shrink-0 flex items-center justify-between gap-4 px-4 py-3 border-b border-gray-200 bg-gray-50/80">
                                    <div className="flex items-center gap-2 min-w-0">
                                        <Inbox className="w-5 h-5 text-gray-500 shrink-0" />
                                        <span className="text-sm font-medium text-gray-700 truncate">
                                            {selectedAccountId
                                                ? (accounts.find(a => (a.account_id || a.email) === selectedAccountId)?.email || selectedAccountId)
                                                : 'All accounts'}
                                        </span>
                                    </div>
                                    <button
                                        type="button"
                                        onClick={() => selectedAccountId && handleSyncAccount(selectedAccountId)}
                                        disabled={!selectedAccountId || syncLoading === selectedAccountId}
                                        className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium text-gray-700 bg-white border border-gray-200 hover:bg-gray-100 disabled:opacity-50 disabled:pointer-events-none"
                                    >
                                        {selectedAccountId && syncLoading === selectedAccountId ? (
                                            <Loader2 className="w-4 h-4 animate-spin" />
                                        ) : (
                                            <RefreshCw className="w-4 h-4" />
                                        )}
                                        Sync now
                                    </button>
                                </div>
                                <div className="flex-1 overflow-y-auto">
                                    {messagesLoading && messages.length === 0 ? (
                                        <div className="flex items-center justify-center py-12">
                                            <Loader2 className="w-8 h-8 animate-spin text-gray-400" />
                                        </div>
                                    ) : messages.length === 0 ? (
                                        <div className="flex flex-col items-center justify-center py-12 text-center max-w-sm mx-auto px-4">
                                            <Inbox className="w-12 h-12 text-gray-300 mb-3" />
                                            <p className="text-gray-600 font-medium">No synced emails yet</p>
                                            <p className="text-sm text-gray-500 mt-1">Use Sync on an account in the sidebar to fetch your inbox.</p>
                                        </div>
                                    ) : (
                                        <ul className="divide-y divide-gray-100">
                                            {messages.map((m) => (
                                                <li key={m.message_id || `${m.account_id}-${m.date}-${m.subject}`} className="px-4 py-3 hover:bg-gray-50/80 transition-colors">
                                                    <div className="flex flex-col gap-0.5 min-w-0">
                                                        <div className="flex items-baseline justify-between gap-2">
                                                            <span className="font-medium text-gray-900 truncate text-sm">{m.subject || '(No subject)'}</span>
                                                            <span className="text-xs text-gray-500 shrink-0">{m.date}</span>
                                                        </div>
                                                        <p className="text-xs text-gray-500 truncate">{m.from}</p>
                                                        {m.body_snippet && (
                                                            <p className="text-xs text-gray-600 mt-1 line-clamp-2">{m.body_snippet}</p>
                                                        )}
                                                    </div>
                                                </li>
                                            ))}
                                        </ul>
                                    )}
                                    {messages.length >= 50 && (
                                        <div className="px-4 py-3 border-t border-gray-100">
                                            <button
                                                type="button"
                                                disabled={messagesLoading}
                                                onClick={() => {
                                                    const next = messagesOffset + 50;
                                                    setMessagesOffset(next);
                                                    fetchMessages(selectedAccountId, next, true);
                                                }}
                                                className="text-sm text-gray-600 hover:text-gray-900 disabled:opacity-50"
                                            >
                                                {messagesLoading ? 'Loading…' : 'Load more'}
                                            </button>
                                        </div>
                                    )}
                                </div>
                            </>
                        )}
                    </main>
                </div>
            </div>
        </div>
    );
}
