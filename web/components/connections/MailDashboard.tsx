'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import React, { useState, useEffect, useRef } from 'react';
import { X, Mail, Loader2, UserPlus, RefreshCw, Inbox, Search, AlertTriangle } from 'lucide-react';
import { cn, getApiBase } from '@/lib/utils';

/** Use direct backend (port 8001) to bypass Next.js proxy which can return 500 on sync/body. */
const api = (path: string) => {
    const p = path.startsWith('/') ? path : `/${path}`;
    return `${getApiBase()}${p}`;
};

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
    /** Optional label/purpose (e.g. support, outreach, sending) - visible to agent in list_email_accounts */
    label?: string;
}

interface SyncedMessage {
    account_id: string;
    folder: string;
    message_id?: string;
    category?: string;
    provider_message_id?: string;
    subject: string;
    from: string;
    date: string;
    /** ISO date (UTC) when available; use for reliable relative time. */
    message_date_iso?: string | null;
    body_snippet: string;
    synced_at: string;
    answered_at?: string;
    suspicious_for_agent?: boolean;
    suspicious_reasons?: string[];
    suspicious_score?: number;
}

/** Provider auto-detected categories (e.g. Gmail: Primary, Social, Promotions). Always shown in filter bar. */
const STANDARD_CATEGORIES = ['primary', 'social', 'promotions'] as const;
const CATEGORY_DISPLAY: Record<string, string> = {
    all: 'All',
    primary: 'Primary',
    social: 'Social',
    promotions: 'Promotions',
};
function categoryDisplay(cat: string): string {
    return CATEGORY_DISPLAY[cat] ?? (cat.charAt(0).toUpperCase() + cat.slice(1).toLowerCase().replace(/_/g, ' '));
}

/** Format answered_at ISO to "DD.MM.YYYY um HH:MM" for display. */
function formatAnsweredAt(iso: string | undefined): string {
    if (!iso?.trim()) return '';
    try {
        const d = new Date(iso);
        if (Number.isNaN(d.getTime())) return '';
        const dd = String(d.getDate()).padStart(2, '0');
        const mm = String(d.getMonth() + 1).padStart(2, '0');
        const yyyy = d.getFullYear();
        const hh = String(d.getHours()).padStart(2, '0');
        const min = String(d.getMinutes()).padStart(2, '0');
        return `${dd}.${mm}.${yyyy} um ${hh}:${min}`;
    } catch {
        return '';
    }
}

/** Format message date for list: use message_date_iso for correct relative time, else raw date string. */
function formatMessageDate(m: SyncedMessage): string {
    const iso = m.message_date_iso?.trim();
    if (iso) {
        try {
            const d = new Date(iso);
            if (!Number.isNaN(d.getTime())) {
                const diffMs = Date.now() - d.getTime();
                if (diffMs < 60_000) return 'Gerade eben';
                if (diffMs < 3600_000) return `vor ${Math.floor(diffMs / 60_000)} Min.`;
                if (diffMs < 86400_000) return `vor ${Math.floor(diffMs / 3600_000)} Std.`;
                const dd = String(d.getDate()).padStart(2, '0');
                const mm = String(d.getMonth() + 1).padStart(2, '0');
                const yyyy = d.getFullYear();
                return `${dd}.${mm}.${yyyy}`;
            }
        } catch {
            /* fall through to raw date */
        }
    }
    return m.date || '';
}

function suspiciousReasonLabel(reason: string): string {
    const map: Record<string, string> = {
        provider_spam_category: 'Provider marked as spam/junk',
        punycode_domain: 'Internationalized/punycode sender domain',
        social_engineering_language: 'Urgency or social-engineering language detected',
        exec_impersonation_free_mail: 'Possible executive impersonation via free-mail domain',
        phishing_pattern: 'Known phishing-like wording pattern detected',
    };
    return map[reason] || reason;
}

function suspiciousTooltip(m: SyncedMessage): string {
    const reasons = (m.suspicious_reasons || []).map(suspiciousReasonLabel);
    const reasonText = reasons.length > 0 ? reasons.join('; ') : 'Heuristic phishing filter matched';
    return `Suspicious email (${reasonText}). For safety, the agent does not see this message in mail tools.`;
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
    const [selectedCategory, setSelectedCategory] = useState<string>('all');
    const [categories, setCategories] = useState<string[]>(['primary', 'social', 'promotions']);
    const [customLabels, setCustomLabels] = useState<string[]>([]);
    const [selectedMessage, setSelectedMessage] = useState<SyncedMessage | null>(null);
    const [messageBody, setMessageBody] = useState<string | null>(null);
    const [messageBodyLoading, setMessageBodyLoading] = useState(false);
    const [messageBodyError, setMessageBodyError] = useState<string | null>(null);
    const [newLabelInput, setNewLabelInput] = useState('');
    const [patchLoading, setPatchLoading] = useState(false);
    const [searchQuery, setSearchQuery] = useState('');
    const [searchInputValue, setSearchInputValue] = useState('');
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

    const fetchCategories = async () => {
        try {
            const res = await fetch(api('api/email/categories'), { credentials: 'include' });
            if (res.ok) {
                const data = await res.json();
                setCategories(data.categories || ['primary', 'social', 'promotions']);
            }
        } catch {
            setCategories(['primary', 'social', 'promotions']);
        }
    };

    const autoSyncOnEmptyRef = useRef(false);

    const fetchMessages = async (accountId: string | null, offset: number, append: boolean, category: string = 'all') => {
        setMessagesLoading(true);
        try {
            const params = new URLSearchParams({ folder: 'INBOX', limit: '50', offset: String(offset) });
            if (accountId) params.set('account_id', accountId);
            if (category && category !== 'all') params.set('category', category);
            const res = await fetch(api(`api/email/messages?${params}`), { credentials: 'include' });
            if (res.ok) {
                const data = await res.json();
                const list = data.messages || [];
                setMessages(prev => {
                    if (append) return [...prev, ...list];
                    // On refresh: keep previous list if API returned empty (avoids spinner after sync/refetch glitch)
                    if (list.length === 0 && prev.length > 0) return prev;
                    return list;
                });
                // Auto-sync when store is empty on first load (e.g. after VAF restart) - sync repopulates the store
                if (!append && offset === 0 && category === 'all' && list.length === 0 && accounts.length > 0 && !autoSyncOnEmptyRef.current && syncLoading === null) {
                    autoSyncOnEmptyRef.current = true;
                    const toSync = accountId || accounts[0]?.account_id || accounts[0]?.email;
                    if (toSync) handleSyncAccount(toSync);
                }
            }
        } catch {
            // On error: keep previous list so user still sees mails instead of empty spinner
            setMessages(prev => (append ? prev : prev.length > 0 ? prev : []));
        } finally {
            setMessagesLoading(false);
        }
    };

    const fetchSearch = async (query: string) => {
        const q = (query || '').trim();
        if (!q) return;
        setMessagesLoading(true);
        try {
            const params = new URLSearchParams({ folder: 'INBOX', limit: '50' });
            params.set('query', q);
            const res = await fetch(api(`api/email/messages/search?${params}`), { credentials: 'include' });
            if (res.ok) {
                const data = await res.json();
                setMessages(data.messages || []);
                setSearchQuery(q);
            } else {
                setMessages([]);
            }
        } catch {
            setMessages([]);
        } finally {
            setMessagesLoading(false);
        }
    };

    const clearSearch = () => {
        setSearchQuery('');
        setSearchInputValue('');
        setMessagesOffset(0);
        fetchMessages(selectedAccountId, 0, false, selectedCategory);
    };

    useEffect(() => {
        if (isOpen) fetchAccounts();
    }, [isOpen, refreshTrigger]);

    useEffect(() => {
        if (isOpen && accounts.length > 0) {
            fetchCategories();
        }
    }, [isOpen, accounts.length]);

    useEffect(() => {
        if (isOpen && accounts.length > 0 && !searchQuery.trim()) {
            setMessagesOffset(0);
            fetchMessages(selectedAccountId, 0, false, selectedCategory);
        }
    }, [isOpen, selectedAccountId, selectedCategory, accounts.length, searchQuery]);

    useEffect(() => {
        if (!selectedMessage) {
            setMessageBody(null);
            setMessageBodyLoading(false);
            setMessageBodyError(null);
            return;
        }
        const mid = selectedMessage.message_id;
        if (!mid || !selectedMessage.account_id) {
            setMessageBody(null);
            setMessageBodyLoading(false);
            setMessageBodyError(null);
            return;
        }
        let cancelled = false;
        setMessageBody(null);
        setMessageBodyError(null);
        setMessageBodyLoading(true);
        const params = new URLSearchParams({
            account_id: selectedMessage.account_id,
            message_id: mid,
            folder: selectedMessage.folder || 'INBOX',
        });
        if (selectedMessage.provider_message_id) {
            params.set('provider_message_id', selectedMessage.provider_message_id);
        }
        fetch(api(`api/email/messages/body?${params}`), { credentials: 'include' })
            .then(async (res) => {
                if (cancelled) return;
                if (!res.ok) {
                    const data = await res.json().catch(() => ({}));
                    setMessageBodyError(data.detail || res.statusText || 'Failed to load body');
                    setMessageBody(null);
                    return;
                }
                const data = await res.json();
                setMessageBody(typeof data.body === 'string' ? data.body : '');
                setMessageBodyError(null);
            })
            .catch(() => {
                if (!cancelled) {
                    setMessageBodyError('Failed to load message body');
                    setMessageBody(null);
                }
            })
            .finally(() => {
                if (!cancelled) setMessageBodyLoading(false);
            });
        return () => { cancelled = true; };
    }, [selectedMessage]);

    useEffect(() => {
        if (!isOpen) return;
        const handleKeyDown = (e: KeyboardEvent) => {
            if (e.key === 'Escape') {
                e.preventDefault();
                if (selectedMessage) setSelectedMessage(null);
                else onClose();
            }
        };
        window.addEventListener('keydown', handleKeyDown, true);
        return () => window.removeEventListener('keydown', handleKeyDown, true);
    }, [isOpen, onClose, selectedMessage]);

    const handleSyncAccount = async (accountId: string) => {
        setSyncLoading(accountId);
        setError('');
        try {
            const res = await fetch(api(`api/email/accounts/${encodeURIComponent(accountId)}/sync?max_messages=100`), {
                method: 'POST',
                credentials: 'include',
            });
            const text = await res.text();
            let data: { ok?: boolean; error?: string; detail?: string };
            try {
                data = JSON.parse(text);
            } catch {
                // 500 may return HTML or plain text; extract "error"/"detail" if present
                const errMatch = text.match(/"error":\s*"((?:[^"\\]|\\.)*)"/) || text.match(/"detail":\s*"((?:[^"\\]|\\.)*)"/);
                const err = errMatch ? errMatch[1].replace(/\\"/g, '"') : (res.ok ? 'Invalid response' : `Sync failed (${res.status}). Response: ${text.slice(0, 150)}`);
                if (!res.ok) console.error('[MailDashboard] Sync 500 body:', text.slice(0, 800));
                setError(err);
                return;
            }
            if (data.ok) {
                await fetchAccounts();
                setMessagesOffset(0);
                fetchMessages(selectedAccountId, 0, false, selectedCategory);
            } else {
                setError((data as any).error || (data as any).detail || 'Sync failed');
            }
        } catch (e) {
            setError(e instanceof Error ? e.message : 'Sync request failed');
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
            handleSyncAccount(accountId); // run first sync immediately
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
            autoSyncOnEmptyRef.current = false;
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
                handleSyncAccount(id); // run first sync immediately when dashboard opens with auto-sync on
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

    const handleAccountLabelChange = async (accountId: string, label: string) => {
        try {
            await fetch(api(`api/email/accounts/${encodeURIComponent(accountId)}`), {
                method: 'PATCH',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ label: label.trim().slice(0, 64) }),
            });
            await fetchAccounts();
        } catch {
            setError('Failed to update label');
        }
    };

    const handleSetMessageCategory = async (message: SyncedMessage, category: string) => {
        const cat = category.trim().toLowerCase().replace(/\s+/g, '_').slice(0, 64) || 'primary';
        setPatchLoading(true);
        try {
            const res = await fetch(api('api/email/messages'), {
                method: 'PATCH',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    account_id: message.account_id,
                    folder: message.folder || 'INBOX',
                    message_id: message.message_id || '',
                    category: cat,
                }),
            });
            if (res.ok) {
                const data = await res.json().catch(() => ({}));
                const updatedCount = typeof data.updated === 'number' ? data.updated : 1;
                const updated = { ...message, category: cat };
                setSelectedMessage(prev => prev && prev.message_id === message.message_id ? updated : null);
                if (updatedCount > 0) {
                    setMessagesOffset(0);
                    fetchMessages(selectedAccountId, 0, false, selectedCategory);
                }
                setMessages(prev => {
                    const next = prev.map(m =>
                        (m.message_id === message.message_id && m.account_id === message.account_id) ? updated : m
                    );
                    if (selectedCategory !== 'all' && selectedCategory !== cat) {
                        return next.filter(m => !(m.message_id === message.message_id && m.account_id === message.account_id));
                    }
                    return next;
                });
                if (!categories.includes(cat) && !STANDARD_CATEGORIES.includes(cat as any)) {
                    setCustomLabels(prev => prev.includes(cat) ? prev : [...prev, cat]);
                }
                if (!categories.includes(cat)) {
                    setCategories(prev => prev.includes(cat) ? prev : [...prev, cat].sort());
                }
            }
        } finally {
            setPatchLoading(false);
        }
    };

    // Always show provider filter labels (Primary, Social, Promotions) then API/custom categories
    const allCategoriesForDisplay = [
        ...STANDARD_CATEGORIES,
        ...categories.filter(c => !STANDARD_CATEGORIES.includes(c as any)),
        ...customLabels.filter(c => !categories.includes(c) && !STANDARD_CATEGORIES.includes(c as any)),
    ];

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
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 max-md:p-0 bg-black/50" onClick={onClose}>
            <div
                className={cn(
                    'relative bg-white w-full max-w-[95vw] h-[90vh] rounded-2xl shadow-2xl border border-gray-200 flex flex-col overflow-hidden max-md:max-w-none max-md:h-[100dvh] max-md:rounded-none max-md:border-0'
                )}
                onClick={e => e.stopPropagation()}
            >
                <div className="flex items-center justify-between px-5 py-4 border-b border-gray-200 shrink-0 max-md:px-4 max-md:py-3">
                    <div className="flex items-center gap-3 max-md:gap-3 min-w-0">
                        <div className="w-10 h-10 rounded-xl bg-red-500 flex items-center justify-center shrink-0 max-md:w-10 max-md:h-10 max-md:rounded-xl max-md:shadow-none">
                            <Mail className="w-5 h-5 text-white max-md:w-5 max-md:h-5" />
                        </div>
                        <div className="min-w-0">
                            <h3 className="text-lg font-semibold text-gray-900 max-md:text-lg truncate">Email</h3>
                            <p className="text-xs text-gray-500 max-md:text-xs truncate">Manage your accounts</p>
                        </div>
                    </div>
                    <button type="button" onClick={onClose} className="p-2 hover:bg-gray-100 rounded-lg transition-colors">
                        <X className="w-5 h-5 text-gray-500" />
                    </button>
                </div>

                <div className="flex-1 flex min-h-0 overflow-hidden max-md:flex-col max-md:overflow-y-auto">
                    {/* Left sidebar: accounts + Add account. On mobile it stacks on top (capped height,
                        scrolls) and the mail list takes the rest. */}
                    <aside className="w-72 shrink-0 flex flex-col border-r border-gray-200 bg-gray-50/80 overflow-hidden max-md:w-full max-md:max-h-[38vh] max-md:border-r-0 max-md:border-b max-md:shrink-0">
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
                                        className="mt-4 w-full inline-flex items-center justify-center gap-2 px-3 py-2.5 rounded-xl bg-gray-900 text-white text-sm font-medium hover:bg-gray-800 transition-colors dark:bg-[#e6e6e6] dark:text-gray-900 dark:hover:bg-white dark:shadow-none"
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
                                                                <input
                                                                    type="text"
                                                                    placeholder="Label (e.g. support, outreach)"
                                                                    className="mt-1 w-full text-xs text-gray-600 border border-gray-200 rounded px-2 py-1 focus:outline-none focus:ring-1 focus:ring-gray-400 focus:border-gray-400"
                                                                    defaultValue={a.label ?? ''}
                                                                    onBlur={(e) => {
                                                                        const v = e.target.value.trim();
                                                                        if (v !== (a.label ?? '')) handleAccountLabelChange(id, v);
                                                                    }}
                                                                    onKeyDown={(e) => {
                                                                        if (e.key === 'Enter') (e.target as HTMLInputElement).blur();
                                                                    }}
                                                                    onClick={(e) => e.stopPropagation()}
                                                                />
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
                                                                    (a.auto_sync_enabled ?? false) ? 'bg-gray-900 border-gray-900 dark:bg-[#d9d9d9] dark:border-[#d9d9d9]' : 'bg-gray-200 border-gray-200 dark:bg-[#333333] dark:border-[#333333]'
                                                                )}
                                                                onClick={() => handleAutoSyncToggle(id, !(a.auto_sync_enabled ?? false))}
                                                            >
                                                                <span
                                                                    className={cn(
                                                                        'pointer-events-none inline-block h-4 w-4 rounded-full bg-white shadow ring-0 transition translate-y-0.5 dark:bg-[#e8e8e8]',
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
                    <main className="flex-1 min-w-0 flex flex-col overflow-hidden bg-white max-md:min-h-0 max-md:shrink-0">
                        {accounts.length === 0 && !loading && (
                            <div className="flex flex-col items-center justify-center flex-1 text-center max-w-sm mx-auto p-8 max-md:p-4">
                                <div className="w-14 h-14 rounded-2xl bg-gray-100 flex items-center justify-center mb-3">
                                    <Mail className="w-7 h-7 text-gray-400" />
                                </div>
                                <p className="text-gray-600 font-medium">Manage your email accounts</p>
                                <p className="text-sm text-gray-500 mt-1">Add an account in the sidebar to get started.</p>
                            </div>
                        )}
                        {accounts.length > 0 && (
                            <>
                                <div className="shrink-0 flex items-center justify-between gap-4 px-4 py-3 border-b border-gray-200 bg-gray-50/80 flex-wrap">
                                    <div className="flex items-center gap-2 min-w-0">
                                        <Inbox className="w-5 h-5 text-gray-500 shrink-0" />
                                        <span className="text-sm font-medium text-gray-700 truncate">
                                            {searchQuery
                                                ? `Search: "${searchQuery}"`
                                                : selectedAccountId
                                                    ? (accounts.find(a => (a.account_id || a.email) === selectedAccountId)?.email || selectedAccountId)
                                                    : 'All accounts'}
                                        </span>
                                    </div>
                                    <div className="flex items-center gap-2 flex-1 min-w-0 justify-end">
                                        <div className="flex items-center gap-1.5">
                                            <input
                                                type="text"
                                                placeholder="Search mail (e.g. lieferando)"
                                                value={searchInputValue}
                                                onChange={e => {
                                                    const v = e.target.value;
                                                    setSearchInputValue(v);
                                                    if (!v.trim()) clearSearch();
                                                }}
                                                onKeyDown={e => {
                                                    if (e.key === 'Enter') {
                                                        const q = searchInputValue.trim();
                                                        if (q) fetchSearch(q);
                                                    }
                                                }}
                                                className="w-40 px-2.5 py-1.5 rounded-lg text-sm border border-gray-200 focus:outline-none focus:ring-1 focus:ring-gray-400"
                                            />
                                            <button
                                                type="button"
                                                onClick={() => searchInputValue.trim() ? fetchSearch(searchInputValue.trim()) : clearSearch()}
                                                className="p-1.5 rounded-lg border border-gray-200 bg-white text-gray-600 hover:bg-gray-50"
                                                title="Search"
                                            >
                                                <Search className="w-4 h-4" />
                                            </button>
                                        </div>
                                        {(messagesLoading || syncLoading) && messages.length > 0 && (
                                            <span className="text-xs text-gray-500 inline-flex items-center gap-1">
                                                <Loader2 className="w-3.5 h-3.5 animate-spin" />
                                                Updating…
                                            </span>
                                        )}
                                        <button
                                            type="button"
                                            onClick={() => {
                                                const toSync = selectedAccountId || accounts[0]?.account_id || accounts[0]?.email;
                                                if (toSync) handleSyncAccount(toSync);
                                            }}
                                            disabled={accounts.length === 0 || syncLoading !== null}
                                            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium text-gray-700 bg-white border border-gray-200 hover:bg-gray-100 disabled:opacity-50 disabled:pointer-events-none"
                                        >
                                            {syncLoading !== null ? (
                                                <Loader2 className="w-4 h-4 animate-spin" />
                                            ) : (
                                                <RefreshCw className="w-4 h-4" />
                                            )}
                                            Sync now
                                        </button>
                                    </div>
                                </div>
                                <div className="shrink-0 flex flex-wrap items-center gap-1.5 px-4 py-2 border-b border-gray-100 bg-white">
                                    <button
                                        type="button"
                                        onClick={() => { setSelectedCategory('all'); setMessagesOffset(0); }}
                                        className={cn(
                                            'px-3 py-1.5 rounded-lg text-sm font-medium transition-colors',
                                            selectedCategory === 'all' ? 'bg-gray-900 text-white dark:bg-[#3a3a3a] dark:text-white' : 'text-gray-600 hover:bg-gray-100'
                                        )}
                                    >
                                        All
                                    </button>
                                    {allCategoriesForDisplay.map((cat) => (
                                        <button
                                            key={cat}
                                            type="button"
                                            onClick={() => { setSelectedCategory(cat); setMessagesOffset(0); }}
                                            className={cn(
                                                'px-3 py-1.5 rounded-lg text-sm font-medium transition-colors',
                                                selectedCategory === cat ? 'bg-gray-900 text-white dark:bg-[#3a3a3a] dark:text-white' : 'text-gray-600 hover:bg-gray-100'
                                            )}
                                        >
                                            {categoryDisplay(cat)}
                                        </button>
                                    ))}
                                    <div className="flex items-center gap-1">
                                        <input
                                            type="text"
                                            placeholder="+ New label"
                                            value={newLabelInput}
                                            onChange={(e) => setNewLabelInput(e.target.value)}
                                            onKeyDown={(e) => {
                                                if (e.key === 'Enter' && newLabelInput.trim()) {
                                                    const cat = newLabelInput.trim().toLowerCase().replace(/\s+/g, '_').slice(0, 64);
                                                    if (cat && !allCategoriesForDisplay.includes(cat)) {
                                                        setCustomLabels(prev => [...prev, cat]);
                                                        setSelectedCategory(cat);
                                                        setMessagesOffset(0);
                                                        setNewLabelInput('');
                                                    }
                                                }
                                            }}
                                            className="w-24 px-2 py-1 rounded-lg text-sm border border-gray-200 focus:outline-none focus:ring-1 focus:ring-gray-400"
                                        />
                                    </div>
                                </div>
                                <div className="flex-1 overflow-y-auto">
                                    {/* While loading messages, show spinner so first load can populate the list; only show "Syncing..." when not loading (sync ran but store was empty). When we have messages, always show them during sync. */}
                                    {messages.length === 0 && messagesLoading ? (
                                        <div className="flex items-center justify-center py-12">
                                            <Loader2 className="w-8 h-8 animate-spin text-gray-400" />
                                        </div>
                                    ) : messages.length === 0 && syncLoading ? (
                                        <div className="flex flex-col items-center justify-center py-12 text-center max-w-sm mx-auto px-4">
                                            <Loader2 className="w-10 h-10 animate-spin text-gray-400 mb-3" />
                                            <p className="text-gray-600 font-medium">Syncing your mailbox…</p>
                                            <p className="text-sm text-gray-500 mt-1">Your messages will appear here when sync finishes.</p>
                                        </div>
                                    ) : messages.length === 0 ? (
                                        <div className="flex flex-col items-center justify-center py-12 text-center max-w-sm mx-auto px-4">
                                            <Inbox className="w-12 h-12 text-gray-300 mb-3" />
                                            <p className="text-gray-600 font-medium">
                                                {selectedCategory !== 'all' ? 'No emails in this category' : 'No synced emails yet'}
                                            </p>
                                            <p className="text-sm text-gray-500 mt-1">
                                                {selectedCategory !== 'all'
                                                    ? 'Click Sync now to refresh – Gmail will assign Promotions and Social. You can also open a message and set a label.'
                                                    : 'Use Sync on an account in the sidebar to fetch your inbox.'}
                                            </p>
                                        </div>
                                    ) : (
                                        <ul className="divide-y divide-gray-100">
                                            {messages.map((m, i) => (
                                                <li
                                                    key={`${m.account_id}-${m.folder || 'INBOX'}-${m.provider_message_id || m.message_id || i}-${i}`}
                                                    onClick={() => setSelectedMessage(m)}
                                                    className="px-4 py-3 hover:bg-gray-50/80 transition-colors cursor-pointer"
                                                >
                                                    <div className="flex flex-col gap-0.5 min-w-0">
                                                        <div className="flex items-baseline justify-between gap-2">
                                                            <span className="font-medium text-gray-900 truncate text-sm flex items-center gap-1.5">
                                                                <span className="truncate">{m.subject || '(No subject)'}</span>
                                                                {m.suspicious_for_agent && (
                                                                    <span title={suspiciousTooltip(m)} className="inline-flex">
                                                                        <AlertTriangle
                                                                            className="w-3.5 h-3.5 text-amber-600 shrink-0"
                                                                        />
                                                                    </span>
                                                                )}
                                                            </span>
                                                            <span className="text-xs text-gray-500 shrink-0 flex items-center gap-1.5">
                                                                {m.answered_at && (
                                                                    <span className="text-green-600 font-medium" title={formatAnsweredAt(m.answered_at)}>Beantwortet</span>
                                                                )}
                                                                {formatMessageDate(m)}
                                                            </span>
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
                                                    fetchMessages(selectedAccountId, next, true, selectedCategory);
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

                {/* Mail detail popup – mail-client style: large, sender top, labels, body, AI reply */}
                {selectedMessage && (
                    <div
                        className="fixed inset-0 z-[60] flex items-center justify-center p-4 max-md:p-0 bg-black/50"
                        onClick={() => setSelectedMessage(null)}
                    >
                        <div
                            className="bg-white w-full max-w-4xl h-[90vh] min-h-[500px] rounded-2xl shadow-2xl border border-gray-200 flex flex-col overflow-hidden max-md:max-w-none max-md:h-[100dvh] max-md:min-h-0 max-md:rounded-none max-md:border-0"
                            onClick={e => e.stopPropagation()}
                        >
                            {/* Header: subject + close */}
                            <div className="flex items-start justify-between gap-4 px-5 py-4 border-b border-gray-200 shrink-0">
                                <h3 className="text-lg font-semibold text-gray-900 leading-snug pr-8">
                                    {selectedMessage.subject || '(No subject)'}
                                </h3>
                                <button type="button" onClick={() => setSelectedMessage(null)} className="p-2 hover:bg-gray-100 rounded-lg shrink-0">
                                    <X className="w-5 h-5 text-gray-500" />
                                </button>
                            </div>
                            {/* Absender + Datum + Benatwortet */}
                            <div className="px-5 py-2 border-b border-gray-100 shrink-0">
                                <p className="text-sm text-gray-700">
                                    <span className="font-medium text-gray-500">From:</span>{' '}
                                    <span className="text-gray-900">{selectedMessage.from}</span>
                                </p>
                                <p className="text-xs text-gray-500 mt-0.5">{formatMessageDate(selectedMessage)}</p>
                                {selectedMessage.answered_at && formatAnsweredAt(selectedMessage.answered_at) && (
                                    <p className="text-xs text-green-700 mt-1 font-medium">
                                        Benatwortet am {formatAnsweredAt(selectedMessage.answered_at)}
                                    </p>
                                )}
                                {selectedMessage.suspicious_for_agent && (
                                    <div
                                        className="mt-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800"
                                        title={suspiciousTooltip(selectedMessage)}
                                    >
                                        <span className="font-semibold">Security:</span> This email is flagged as suspicious and is hidden from agent mail tools.
                                    </div>
                                )}
                            </div>
                            {/* Labels — changing a label automatically applies to all mails from this sender */}
                            <div className="px-5 py-3 border-b border-gray-100 shrink-0 space-y-2">
                                <div className="flex flex-wrap items-center gap-2">
                                <span className="text-xs font-medium text-gray-500 mr-1">Label:</span>
                                {allCategoriesForDisplay.map((cat) => (
                                    <button
                                        key={cat}
                                        type="button"
                                        disabled={patchLoading}
                                        onClick={() => handleSetMessageCategory(selectedMessage, cat)}
                                        className={cn(
                                            'px-3 py-1.5 rounded-lg text-sm font-medium transition-colors',
                                            (selectedMessage.category || 'primary') === cat
                                                ? 'bg-gray-900 text-white dark:bg-[#3a3a3a] dark:text-white'
                                                : 'bg-gray-100 text-gray-700 hover:bg-gray-200'
                                        )}
                                    >
                                        {categoryDisplay(cat)}
                                    </button>
                                ))}
                                <input
                                    type="text"
                                    placeholder="New label"
                                    className="w-28 px-2 py-1.5 rounded-lg text-sm border border-gray-200 focus:outline-none focus:ring-1 focus:ring-gray-400"
                                    onKeyDown={(e) => {
                                        const t = e.currentTarget;
                                        if (e.key === 'Enter' && t.value.trim()) {
                                            handleSetMessageCategory(selectedMessage, t.value.trim());
                                            t.value = '';
                                        }
                                    }}
                                />
                                </div>
                            </div>
                            {/* Mail body – main scrollable area */}
                            <div className="flex-1 min-h-0 overflow-y-auto px-5 py-4">
                                {messageBodyLoading && (
                                    <div className="flex items-center justify-center py-12">
                                        <Loader2 className="w-8 h-8 animate-spin text-gray-400" />
                                    </div>
                                )}
                                {!messageBodyLoading && messageBodyError && (
                                    <p className="text-sm text-red-600">{messageBodyError}</p>
                                )}
                                {!messageBodyLoading && !messageBodyError && messageBody !== null && (
                                    <pre className="text-sm text-gray-800 whitespace-pre-wrap font-sans leading-relaxed">
                                        {messageBody || '(No content)'}
                                    </pre>
                                )}
                            </div>
                            {/* Footer: AI Antwort verfassen & senden */}
                            <div className="shrink-0 px-5 py-4 border-t border-gray-200 bg-gray-50/80">
                                <button
                                    type="button"
                                    className="w-full py-3 px-4 rounded-xl bg-gray-900 text-white text-sm font-medium hover:bg-gray-800 transition-colors flex items-center justify-center gap-2 dark:bg-[#e6e6e6] dark:text-gray-900 dark:hover:bg-white dark:shadow-none"
                                >
                                    <Mail className="w-4 h-4" />
                                    Reply with Agent
                                </button>
                            </div>
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
}
