'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import React, { useState, useEffect, useRef } from 'react';
import { X, Cloud, Loader2, UserPlus, RefreshCw, FolderOpen, HardDrive, FolderSync, File, ChevronRight, Home, Search } from 'lucide-react';
import { cn } from '@/lib/utils';

/** Use relative /api/ so Next.js rewrites to backend; cookies are sent (same-origin). */
const api = (path: string) => path.startsWith('/') ? path : `/${path}`;

const PROVIDER_META: Record<string, { name: string; icon: React.ElementType; color: string }> = {
    google_drive: { name: 'Google Drive', icon: Cloud, color: 'bg-yellow-500' },
    onedrive: { name: 'OneDrive', icon: Cloud, color: 'bg-blue-500' },
    dropbox: { name: 'Dropbox', icon: FolderSync, color: 'bg-blue-600' },
    nextcloud: { name: 'Nextcloud', icon: HardDrive, color: 'bg-cyan-600' },
    icloud: { name: 'iCloud', icon: Cloud, color: 'bg-sky-400' },
};

interface BrowseItem {
    file_id: string;
    name: string;
    path: string;
    size: number;
    modified_time: number;
    is_folder: boolean;
    mime_type?: string;
}

export interface CloudDashboardProps {
    isOpen: boolean;
    onClose: () => void;
    onOpenAddWizard: (provider?: string) => void;
    refreshTrigger?: number;
}

interface CloudAccount {
    account_id: string;
    provider: string;
    display_name?: string;
    label?: string;
    sync_enabled?: boolean;
    last_synced_at?: number | null;
    total_files?: number;
    conflicts?: number;
    pending_uploads?: number;
    pending_downloads?: number;
}

interface SearchResultItem extends BrowseItem {
    account_id: string;
    account_label?: string;
    account_display_name?: string;
}

interface BreadcrumbItem {
    id: string;
    name: string;
}

export default function CloudDashboard({ isOpen, onClose, onOpenAddWizard, refreshTrigger = 0 }: CloudDashboardProps) {
    const [accounts, setAccounts] = useState<CloudAccount[]>([]);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState('');
    const [selectedAccountId, setSelectedAccountId] = useState<string | null>(null);
    const [browseItems, setBrowseItems] = useState<BrowseItem[]>([]);
    const [browseLoading, setBrowseLoading] = useState(false);
    const [currentFolderId, setCurrentFolderId] = useState('root');
    const [breadcrumb, setBreadcrumb] = useState<BreadcrumbItem[]>([]);
    const [syncLoading, setSyncLoading] = useState<string | null>(null);
    const [searchQuery, setSearchQuery] = useState('');
    const [searchResults, setSearchResults] = useState<SearchResultItem[]>([]);
    const [searchLoading, setSearchLoading] = useState(false);
    const [labelDrafts, setLabelDrafts] = useState<Record<string, string>>({});
    const pendingFolderRef = useRef<{ id: string; name: string } | null>(null);

    const fetchAccounts = async () => {
        try {
            const res = await fetch(api('api/cloud/accounts'), { credentials: 'include' });
            if (res.ok) {
                const data = await res.json();
                setAccounts(data.accounts || []);
            }
        } catch { setAccounts([]); }
    };

    const fetchAccountStatus = async (accountId: string) => {
        try {
            const res = await fetch(api(`api/cloud/accounts/${encodeURIComponent(accountId)}/status`), { credentials: 'include' });
            if (res.ok) {
                const data = await res.json();
                setAccounts(prev => prev.map(a =>
                    a.account_id === accountId
                        ? { ...a, ...data, total_files: data.total_files, conflicts: data.conflicts, pending_uploads: data.pending_uploads, pending_downloads: data.pending_downloads }
                        : a
                ));
            }
        } catch { /* ignore */ }
    };

    const fetchBrowse = async (accountId: string, folderId: string) => {
        setBrowseLoading(true);
        try {
            const res = await fetch(api(`api/cloud/accounts/${encodeURIComponent(accountId)}/browse?folder_id=${encodeURIComponent(folderId)}`), { credentials: 'include' });
            if (res.ok) {
                const data = await res.json();
                const items = (data.items || []) as BrowseItem[];
                setBrowseItems(items);
                setCurrentFolderId(folderId);
            } else {
                const err = await res.json().catch(() => ({}));
                setError(err.detail || 'Browse failed');
                setBrowseItems([]);
            }
        } catch {
            setBrowseItems([]);
            setError('Browse request failed');
        }
        finally { setBrowseLoading(false); }
    };

    useEffect(() => { if (isOpen) fetchAccounts(); }, [isOpen, refreshTrigger]);
    useEffect(() => {
        if (isOpen && selectedAccountId) {
            const pending = pendingFolderRef.current;
            pendingFolderRef.current = null;
            if (pending) {
                setCurrentFolderId(pending.id);
                setBreadcrumb([{ id: pending.id, name: pending.name }]);
                fetchBrowse(selectedAccountId, pending.id);
            } else {
                setCurrentFolderId('root');
                setBreadcrumb([]);
                fetchBrowse(selectedAccountId, 'root');
            }
            fetchAccountStatus(selectedAccountId);
        } else {
            setBrowseItems([]);
            setBreadcrumb([]);
            pendingFolderRef.current = null;
        }
    }, [isOpen, selectedAccountId]);

    const handleSyncAccount = async (accountId: string) => {
        setSyncLoading(accountId);
        setError('');
        try {
            const res = await fetch(api(`api/cloud/accounts/${encodeURIComponent(accountId)}/sync`), { method: 'POST', credentials: 'include' });
            if (res.ok) {
                await fetchAccounts();
                if (selectedAccountId === accountId) fetchAccountStatus(accountId);
            } else {
                const err = await res.json().catch(() => ({}));
                setError(err.detail || 'Sync failed');
            }
        } catch { setError('Sync request failed'); }
        finally { setSyncLoading(null); }
    };

    const handleFolderClick = (item: BrowseItem) => {
        if (!item.is_folder || !selectedAccountId) return;
        setBreadcrumb(prev => [...prev, { id: item.file_id, name: item.name }]);
        fetchBrowse(selectedAccountId, item.file_id);
    };

    const handleBreadcrumbClick = (index: number) => {
        if (!selectedAccountId) return;
        const item = breadcrumb[index];
        const newBreadcrumb = breadcrumb.slice(0, index + 1);
        setBreadcrumb(newBreadcrumb);
        fetchBrowse(selectedAccountId, item.id);
    };

    const handleGoToRoot = () => {
        if (!selectedAccountId) return;
        setBreadcrumb([]);
        fetchBrowse(selectedAccountId, 'root');
    };

    const handleSearchAll = async () => {
        const q = searchQuery.trim();
        if (!q || accounts.length === 0) {
            setSearchResults([]);
            return;
        }
        setSearchLoading(true);
        setError('');
        try {
            const urls = accounts.map((a) =>
                api(`api/cloud/accounts/${encodeURIComponent(a.account_id)}/search?q=${encodeURIComponent(q)}`)
            );
            const responses = await Promise.all(
                urls.map((url) => fetch(url, { credentials: 'include' }))
            );
            const results: SearchResultItem[] = [];
            for (let i = 0; i < responses.length; i++) {
                const res = responses[i];
                const acc = accounts[i];
                if (!res.ok) continue;
                const data = await res.json().catch(() => ({ items: [] }));
                const items = (data.items || []) as BrowseItem[];
                for (const it of items) {
                    results.push({
                        ...it,
                        account_id: acc.account_id,
                        account_label: acc.label,
                        account_display_name: acc.display_name || acc.account_id,
                    });
                }
            }
            setSearchResults(results);
        } catch {
            setError('Search failed');
            setSearchResults([]);
        } finally {
            setSearchLoading(false);
        }
    };

    const clearSearch = () => {
        setSearchQuery('');
        setSearchResults([]);
    };

    const handlePatchLabel = async (accountId: string, label: string) => {
        try {
            const res = await fetch(api(`api/cloud/accounts/${encodeURIComponent(accountId)}`), {
                method: 'PATCH',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ label: label.trim() || null }),
            });
            if (res.ok) {
                setAccounts(prev => prev.map(a => a.account_id === accountId ? { ...a, label: label.trim() || undefined } : a));
            }
        } catch { /* ignore */ }
    };

    const handleRemoveAccount = async (accountId: string) => {
        if (!confirm('Remove this cloud account? Sync data will be kept locally.')) return;
        try {
            await fetch(api(`api/cloud/accounts/${encodeURIComponent(accountId)}`), { method: 'DELETE', credentials: 'include' });
            await fetchAccounts();
            if (selectedAccountId === accountId) setSelectedAccountId(null);
        } catch { setError('Failed to remove account'); }
    };

    const formatAgo = (ts?: number | null) => {
        if (!ts) return 'Never';
        const diff = Math.floor(Date.now() / 1000 - ts);
        if (diff < 60) return 'Just now';
        if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
        if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
        return new Date(ts * 1000).toLocaleDateString();
    };

    const formatSize = (bytes?: number) => {
        if (!bytes) return '';
        if (bytes < 1024) return `${bytes} B`;
        if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
        return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
    };

    const accountsByProvider = accounts.reduce<Record<string, CloudAccount[]>>((acc, a) => {
        const p = a.provider || 'other';
        if (!acc[p]) acc[p] = [];
        acc[p].push(a);
        return acc;
    }, {});

    if (!isOpen) return null;

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50 max-md:p-0" onClick={onClose}>
            <div className={cn('relative bg-white w-full max-w-[95vw] h-[90vh] rounded-2xl shadow-2xl border border-gray-200 flex flex-col overflow-hidden max-md:max-w-none max-md:h-[100dvh] max-md:rounded-none max-md:border-0')} onClick={e => e.stopPropagation()}>
                <div className="flex items-center justify-between gap-4 px-5 py-4 border-b border-gray-200 shrink-0 max-md:px-4 max-md:py-3">
                    <div className="flex items-center gap-3 min-w-0">
                        <div className="w-10 h-10 rounded-xl bg-gray-900 flex items-center justify-center shrink-0 max-md:w-10 max-md:h-10 max-md:rounded-xl max-md:shadow-none">
                            <Cloud className="w-5 h-5 text-white max-md:w-5 max-md:h-5" />
                        </div>
                        <div className="min-w-0">
                            <h3 className="text-lg font-semibold text-gray-900 max-md:text-lg truncate">Cloud Storage</h3>
                            <p className="text-xs text-gray-500 max-md:text-xs truncate">Browse your cloud storage</p>
                        </div>
                    </div>
                    {accounts.length > 0 && (
                        <div className="flex items-center gap-2 flex-1 max-w-md min-w-0">
                            <div className="relative w-full">
                                <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400 pointer-events-none" />
                                <input
                                    type="text"
                                    placeholder="Search all clouds..."
                                    value={searchQuery}
                                    onChange={(e) => setSearchQuery(e.target.value)}
                                    onKeyDown={(e) => e.key === 'Enter' && handleSearchAll()}
                                    className="w-full pl-9 pr-3 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-gray-300 focus:border-transparent"
                                />
                            </div>
                            <button
                                type="button"
                                onClick={handleSearchAll}
                                disabled={searchLoading || !searchQuery.trim()}
                                className="shrink-0 px-3 py-2 rounded-lg bg-gray-900 text-white text-sm font-medium hover:bg-gray-800 disabled:opacity-50 disabled:cursor-not-allowed dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-white dark:shadow-none"
                            >
                                {searchLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Search className="w-4 h-4" />}
                            </button>
                            {(searchQuery || searchResults.length > 0) && (
                                <button type="button" onClick={clearSearch} className="text-xs text-gray-500 hover:text-gray-700 shrink-0">Clear</button>
                            )}
                        </div>
                    )}
                    <button type="button" onClick={onClose} className="p-2 hover:bg-gray-100 rounded-lg transition-colors shrink-0">
                        <X className="w-5 h-5 text-gray-500" />
                    </button>
                </div>

                <div className="flex-1 flex min-h-0 overflow-hidden max-md:flex-col max-md:overflow-y-auto">
                    {/* Left sidebar: accounts grouped by provider */}
                    <aside className="w-72 shrink-0 flex flex-col border-r border-gray-200 bg-gray-50/80 overflow-hidden max-md:w-full max-md:max-h-[38vh] max-md:border-r-0 max-md:border-b max-md:shrink-0">
                        {error && (
                            <div className="mx-3 mt-3 p-2 rounded-lg bg-red-50 border border-red-200 text-xs text-red-700">{error}</div>
                        )}
                        <div className="flex-1 overflow-y-auto p-3">
                            {loading ? (
                                <div className="flex items-center justify-center py-8"><Loader2 className="w-6 h-6 animate-spin text-gray-400" /></div>
                            ) : accounts.length === 0 ? (
                                <div className="flex flex-col items-center justify-center py-8 text-center">
                                    <p className="text-sm text-gray-600">No cloud accounts yet</p>
                                    <button type="button" onClick={() => onOpenAddWizard()} className="mt-4 w-full inline-flex items-center justify-center gap-2 px-3 py-2.5 rounded-xl bg-gray-900 text-white text-sm font-medium hover:bg-gray-800 dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-white dark:shadow-none">
                                        <UserPlus className="w-4 h-4" /> Add account
                                    </button>
                                </div>
                            ) : (
                                <>
                                    {Object.entries(accountsByProvider).map(([provider, list]) => {
                                        const meta = PROVIDER_META[provider] || { name: provider, icon: FolderOpen, color: 'bg-gray-500' };
                                        const Icon = meta.icon;
                                        return (
                                            <div key={provider} className="mb-6">
                                                <div className="flex items-center gap-2 mb-2 px-2">
                                                    <div className={cn('w-6 h-6 rounded flex items-center justify-center', meta.color)}>
                                                        <Icon className="w-3.5 h-3.5 text-white" />
                                                    </div>
                                                    <span className="text-xs font-semibold text-gray-600 uppercase tracking-wide">{meta.name}</span>
                                                </div>
                                                <ul className="space-y-2">
                                                    {list.map((a) => {
                                                        const id = a.account_id;
                                                        const isSelected = selectedAccountId === id;
                                                        return (
                                                            <li key={id} className={cn('p-3 rounded-xl border shadow-sm transition-colors', isSelected ? 'border-gray-400 bg-white ring-1 ring-gray-300' : 'border-gray-200 bg-white')}>
                                                                <div className="cursor-pointer" onClick={() => setSelectedAccountId(prev => prev === id ? null : id)} role="button" tabIndex={0} onKeyDown={(e) => e.key === 'Enter' && setSelectedAccountId(prev => prev === id ? null : id)}>
                                                                    <div className="flex items-start justify-between gap-2">
                                                                        <div className="min-w-0 flex-1">
                                                                            <span className="font-medium text-gray-900 text-sm truncate block">{a.display_name || a.account_id}</span>
                                                                            <span className="text-xs text-gray-500">{(a.total_files ?? 0)} files synced</span>
                                                                            <div className="mt-1.5">
                                                                                <input
                                                                                    type="text"
                                                                                    placeholder="Label (Privat, Arbeit...)"
                                                                                    value={labelDrafts[id] ?? a.label ?? ''}
                                                                                    onChange={(e) => { e.stopPropagation(); setLabelDrafts(prev => ({ ...prev, [id]: e.target.value })); }}
                                                                                    onBlur={(e) => {
                                                                                        e.stopPropagation();
                                                                                        const v = (e.target as HTMLInputElement).value;
                                                                                        handlePatchLabel(id, v);
                                                                                        setLabelDrafts(prev => { const p = { ...prev }; delete p[id]; return p; });
                                                                                    }}
                                                                                    onClick={(e) => e.stopPropagation()}
                                                                                    className="w-full text-xs px-2 py-1 border border-gray-200 rounded focus:outline-none focus:ring-1 focus:ring-gray-300 bg-white"
                                                                                />
                                                                            </div>
                                                                        </div>
                                                                        <div className="flex items-center gap-1 shrink-0">
                                                                            <button onClick={(e) => { e.stopPropagation(); handleSyncAccount(id); }} disabled={syncLoading === id}
                                                                                className="text-xs text-gray-600 hover:text-gray-900 disabled:opacity-50 px-1.5 py-1 rounded hover:bg-gray-200" title="Sync now">
                                                                                {syncLoading === id ? <Loader2 className="w-3.5 h-3.5 animate-spin inline" /> : <RefreshCw className="w-3.5 h-3.5 inline" />}
                                                                            </button>
                                                                            <button onClick={(e) => { e.stopPropagation(); handleRemoveAccount(id); }} className="text-xs text-red-600 hover:text-red-700 px-1.5 py-1 rounded hover:bg-red-50">Remove</button>
                                                                        </div>
                                                                    </div>
                                                                    {a.last_synced_at != null && <p className="text-xs text-gray-500 mt-1">Last synced: {formatAgo(a.last_synced_at)}</p>}
                                                                </div>
                                                            </li>
                                                        );
                                                    })}
                                                </ul>
                                            </div>
                                        );
                                    })}
                                    <button type="button" onClick={() => onOpenAddWizard()} className="mt-3 w-full py-2.5 rounded-xl border-2 border-dashed border-gray-200 text-sm font-medium text-gray-600 hover:border-gray-300 hover:bg-white inline-flex items-center justify-center gap-2">
                                        <UserPlus className="w-4 h-4" /> Add another account
                                    </button>
                                </>
                            )}
                        </div>
                    </aside>

                    {/* Main: files/folders for selected account */}
                    <main className="flex-1 min-w-0 flex flex-col overflow-hidden bg-white max-md:min-h-0 max-md:shrink-0">
                        {accounts.length === 0 && !loading && (
                            <div className="flex flex-col items-center justify-center flex-1 text-center max-w-sm mx-auto p-8 max-md:p-4">
                                <div className="w-14 h-14 rounded-2xl bg-gray-100 flex items-center justify-center mb-3"><Cloud className="w-7 h-7 text-gray-400" /></div>
                                <p className="text-gray-600 font-medium">Cloud accounts</p>
                                <p className="text-sm text-gray-500 mt-1">Add an account in the sidebar to sync files.</p>
                            </div>
                        )}
                        {accounts.length > 0 && !selectedAccountId && !searchQuery && searchResults.length === 0 && (
                            <div className="flex flex-col items-center justify-center flex-1 text-center max-w-sm mx-auto p-8 max-md:p-4">
                                <div className="w-14 h-14 rounded-2xl bg-gray-100 flex items-center justify-center mb-3"><FolderOpen className="w-7 h-7 text-gray-400" /></div>
                                <p className="text-gray-600 font-medium">Select an account</p>
                                <p className="text-sm text-gray-500 mt-1">Choose an account in the sidebar to view synced files.</p>
                            </div>
                        )}
                        {accounts.length > 0 && (searchQuery || searchResults.length > 0 || searchLoading) && (
                            <div className="flex flex-col flex-1 overflow-hidden">
                                <div className="shrink-0 px-4 py-3 border-b border-gray-200 bg-gray-50/80">
                                    <p className="text-sm text-gray-600">
                                        {searchLoading ? 'Searching all clouds...' : searchResults.length > 0 ? `Found ${searchResults.length} result(s) across all clouds` : 'No results'}
                                    </p>
                                </div>
                                <div className="flex-1 overflow-y-auto p-4">
                                    {searchLoading ? (
                                        <div className="flex justify-center py-12"><Loader2 className="w-8 h-8 animate-spin text-gray-400" /></div>
                                    ) : searchResults.length === 0 ? (
                                        <div className="flex flex-col items-center justify-center py-16 text-center">
                                            <Search className="w-16 h-16 text-gray-200 mb-4" />
                                            <p className="text-sm text-gray-600">No files matching &quot;{searchQuery}&quot;</p>
                                        </div>
                                    ) : (
                                        <div className="grid gap-1">
                                            {searchResults.map((item) => (
                                                <div
                                                    key={`${item.account_id}-${item.file_id}`}
                                                    className={cn(
                                                        'flex items-center gap-3 p-3 rounded-xl',
                                                        item.is_folder ? 'hover:bg-gray-50 cursor-pointer' : 'bg-gray-50/50'
                                                    )}
                                                >
                                                    <div className={cn('w-10 h-10 rounded-lg flex items-center justify-center shrink-0', item.is_folder ? 'bg-amber-100 text-amber-600' : 'bg-gray-100 text-gray-500')}>
                                                        {item.is_folder ? <FolderOpen className="w-5 h-5" /> : <File className="w-5 h-5" />}
                                                    </div>
                                                    <div className="min-w-0 flex-1">
                                                        <p className="text-sm font-medium text-gray-900 truncate">{item.name}</p>
                                                        <p className="text-xs text-gray-500">
                                                            {item.account_label ? `${item.account_label} • ` : ''}{item.account_display_name || item.account_id}
                                                            {!item.is_folder && ` • ${formatSize(item.size)} • ${item.modified_time ? new Date(item.modified_time * 1000).toLocaleDateString() : ''}`}
                                                        </p>
                                                    </div>
                                                    {item.is_folder && (
                                                        <button
                                                            type="button"
                                                            onClick={() => {
                                                                pendingFolderRef.current = { id: item.file_id, name: item.name };
                                                                setSelectedAccountId(item.account_id);
                                                                clearSearch();
                                                            }}
                                                            className="text-xs px-2 py-1 rounded bg-gray-200 hover:bg-gray-300 text-gray-700"
                                                        >
                                                            Open
                                                        </button>
                                                    )}
                                                </div>
                                            ))}
                                        </div>
                                    )}
                                </div>
                            </div>
                        )}
                        {accounts.length > 0 && selectedAccountId && !searchQuery && searchResults.length === 0 && (
                            <>
                                <div className="shrink-0 flex items-center justify-between gap-4 px-4 py-3 border-b border-gray-200 bg-gray-50/80">
                                    <div className="flex items-center gap-2 min-w-0 flex-1 overflow-x-auto">
                                        <button type="button" onClick={handleGoToRoot} className="flex items-center gap-1.5 px-2 py-1 rounded-lg hover:bg-gray-200 text-sm font-medium text-gray-700 shrink-0" title="My Drive">
                                            <Home className="w-4 h-4 text-gray-500" />
                                            <span className="hidden sm:inline">My Drive</span>
                                        </button>
                                        {breadcrumb.map((b, i) => (
                                            <span key={b.id} className="flex items-center gap-1 shrink-0">
                                                <ChevronRight className="w-4 h-4 text-gray-400" />
                                                <button type="button" onClick={() => handleBreadcrumbClick(i)} className="px-2 py-1 rounded-lg hover:bg-gray-200 text-sm font-medium text-gray-700 truncate max-w-[120px]" title={b.name}>
                                                    {b.name}
                                                </button>
                                            </span>
                                        ))}
                                    </div>
                                    <button type="button" onClick={() => handleSyncAccount(selectedAccountId)} disabled={syncLoading !== null}
                                        className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium text-gray-700 bg-white border border-gray-200 hover:bg-gray-100 disabled:opacity-50 shrink-0">
                                        {syncLoading === selectedAccountId ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
                                        Sync VAF Sync
                                    </button>
                                </div>
                                <div className="flex-1 overflow-y-auto p-4">
                                    {browseLoading ? (
                                        <div className="flex items-center justify-center py-12"><Loader2 className="w-8 h-8 animate-spin text-gray-400" /></div>
                                    ) : browseItems.length === 0 ? (
                                        <div className="flex flex-col items-center justify-center py-16 text-center">
                                            <FolderOpen className="w-16 h-16 text-gray-200 mb-4" />
                                            <p className="text-sm text-gray-600">This folder is empty</p>
                                        </div>
                                    ) : (
                                        <div className="grid gap-1">
                                            {[...browseItems]
                                                .sort((a, b) => {
                                                    if (a.is_folder !== b.is_folder) return a.is_folder ? -1 : 1;
                                                    return a.name.localeCompare(b.name, undefined, { sensitivity: 'base' });
                                                })
                                                .map((item) => (
                                                    <button
                                                        key={item.file_id}
                                                        type="button"
                                                        onClick={() => item.is_folder && handleFolderClick(item)}
                                                        disabled={!item.is_folder}
                                                        className={cn(
                                                            'flex items-center gap-3 p-3 rounded-xl text-left w-full transition-colors',
                                                            item.is_folder ? 'hover:bg-gray-100 cursor-pointer' : 'cursor-default opacity-90'
                                                        )}
                                                    >
                                                        <div className={cn('w-10 h-10 rounded-lg flex items-center justify-center shrink-0', item.is_folder ? 'bg-amber-100 text-amber-600' : 'bg-gray-100 text-gray-500')}>
                                                            {item.is_folder ? <FolderOpen className="w-5 h-5" /> : <File className="w-5 h-5" />}
                                                        </div>
                                                        <div className="min-w-0 flex-1">
                                                            <p className="text-sm font-medium text-gray-900 truncate">{item.name}</p>
                                                            <p className="text-xs text-gray-500">
                                                                {item.is_folder ? 'Folder' : `${formatSize(item.size)} • ${item.modified_time ? new Date(item.modified_time * 1000).toLocaleDateString() : ''}`}
                                                            </p>
                                                        </div>
                                                    </button>
                                                ))}
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
