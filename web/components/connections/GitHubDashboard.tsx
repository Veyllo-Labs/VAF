'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import React, { useState, useEffect } from 'react';
import { useTranslations } from 'next-intl';
import { 
    X, Github, Loader2, UserPlus, RefreshCw,
    Shield, ShieldCheck, Trash2,
    Clock, BookOpen, Pencil, GitCommit, Upload, Activity,
    FolderGit2, Star, ExternalLink
} from 'lucide-react';
import { cn, getApiBase } from '@/lib/utils';

const api = (path: string) => {
    const p = path.startsWith('/') ? path : `/${path}`;
    return `${getApiBase()}${p}`;
};

export interface GitHubDashboardProps {
    isOpen: boolean;
    onClose: () => void;
    onOpenAddWizard: () => void;
    refreshTrigger?: number;
}

interface GitHubAccount {
    account_id: string;
    login: string;
    scopes: string;
    allow_write: boolean;
    enabled: boolean;
}

interface GitHubActivity {
    timestamp: number;
    action: string;
    details: string;
    account_id?: string;
    success: boolean;
    error?: string;
}

interface GitHubRepo {
    name: string;
    full_name: string;
    description: string;
    private: boolean;
    stargazers_count: number;
    html_url: string;
    updated_at: string | null;
}

export default function GitHubDashboard({ isOpen, onClose, onOpenAddWizard, refreshTrigger = 0 }: GitHubDashboardProps) {
    const t = useTranslations('githubDashboard');

    const [accounts, setAccounts] = useState<GitHubAccount[]>([]);
    const [activities, setActivities] = useState<GitHubActivity[]>([]);
    const [repos, setRepos] = useState<GitHubRepo[]>([]);
    const [loading, setLoading] = useState(true);
    const [loadingRepos, setLoadingRepos] = useState(false);
    const [reposError, setReposError] = useState<string | null>(null);
    const [updating, setUpdating] = useState<string | null>(null);
    const [reposAccountId, setReposAccountId] = useState<string | null>(null);

    const fetchData = async () => {
        setLoading(true);
        try {
            const [accRes, actRes] = await Promise.all([
                fetch(api('api/github/accounts'), { credentials: 'include' }),
                fetch(api('api/github/activity'), { credentials: 'include' })
            ]);
            
            if (accRes.ok) {
                const data = await accRes.json();
                const list = data.accounts || [];
                setAccounts(list);
                setReposAccountId(prev => {
                    if (list.length === 0) return null;
                    if (prev && list.some((a: GitHubAccount) => a.account_id === prev)) return prev;
                    return list[0].account_id;
                });
            }
            if (actRes.ok) {
                const data = await actRes.json();
                setActivities(data.activity || []);
            }
        } catch (err) {
            console.error('Failed to fetch GitHub dashboard data', err);
        } finally {
            setLoading(false);
        }
    };

    const fetchRepos = async (accountId: string) => {
        setLoadingRepos(true);
        setReposError(null);
        try {
            const res = await fetch(api(`api/github/repos?account_id=${encodeURIComponent(accountId)}&per_page=50`), { credentials: 'include' });
            if (res.ok) {
                const data = await res.json();
                setRepos(data.repos || []);
            } else {
                setRepos([]);
                const body = await res.json().catch(() => ({}));
                const msg = body?.detail || res.statusText;
                setReposError(msg === 'No token for this account' ? t('reposErrorNoToken') : t('reposErrorLoad'));
            }
        } catch (err) {
            console.error('Failed to fetch repos', err);
            setRepos([]);
            setReposError(t('reposErrorLoad'));
        } finally {
            setLoadingRepos(false);
        }
    };

    useEffect(() => {
        if (isOpen && reposAccountId) fetchRepos(reposAccountId);
    }, [isOpen, reposAccountId]);

    useEffect(() => {
        if (isOpen) fetchData();
    }, [isOpen, refreshTrigger]);

    useEffect(() => {
        if (!isOpen) return;
        const onKeyDown = (e: KeyboardEvent) => {
            if (e.key === 'Escape') {
                e.preventDefault();
                e.stopPropagation();
                onClose();
            }
        };
        window.addEventListener('keydown', onKeyDown, true);
        return () => window.removeEventListener('keydown', onKeyDown, true);
    }, [isOpen, onClose]);

    const handleTogglePermissions = async (accountId: string, currentAllowWrite: boolean) => {
        setUpdating(accountId);
        try {
            const res = await fetch(api(`api/github/accounts/${accountId}/permissions`), {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({ allow_write: !currentAllowWrite })
            });
            if (res.ok) {
                setAccounts(prev => prev.map(a => 
                    a.account_id === accountId ? { ...a, allow_write: !currentAllowWrite } : a
                ));
            }
        } catch (err) {
            console.error('Failed to update permissions', err);
        } finally {
            setUpdating(null);
        }
    };

    const handleDisconnect = async (accountId: string) => {
        if (!confirm(t('confirmDisconnect'))) return;
        setUpdating(accountId);
        try {
            const res = await fetch(api(`api/github/accounts/${accountId}`), {
                method: 'DELETE',
                credentials: 'include'
            });
            if (res.ok) {
                setAccounts(prev => prev.filter(a => a.account_id !== accountId));
            }
        } catch (err) {
            console.error('Failed to disconnect account', err);
        } finally {
            setUpdating(null);
        }
    };

    const formatTime = (ts: number) => {
        const date = new Date(ts * 1000);
        return date.toLocaleString([], { 
            day: '2-digit', 
            month: '2-digit', 
            year: 'numeric', 
            hour: '2-digit', 
            minute: '2-digit' 
        });
    };

    if (!isOpen) return null;

    return (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 backdrop-blur-sm p-4 md:p-8 max-md:p-0">
            <div className="bg-white rounded-3xl shadow-2xl w-full max-w-[95vw] h-[90vh] overflow-hidden flex flex-col border border-gray-200 animate-in fade-in zoom-in duration-300 max-md:max-w-none max-md:h-[100dvh] max-md:rounded-none max-md:border-0">
                
                {/* Header */}
                <div className="flex items-center justify-between px-8 py-6 border-b border-gray-100 bg-gray-50/50 max-md:px-4 max-md:py-3">
                    <div className="flex items-center gap-4 max-md:gap-3 min-w-0">
                        <div className="w-12 h-12 rounded-2xl bg-gray-900 flex items-center justify-center shadow-lg shadow-gray-200 shrink-0 max-md:w-10 max-md:h-10 max-md:rounded-xl max-md:shadow-none">
                            <Github className="w-6 h-6 text-white max-md:w-5 max-md:h-5" />
                        </div>
                        <div className="min-w-0">
                            <h2 className="text-2xl font-bold text-gray-900 max-md:text-lg truncate">{t('title')}</h2>
                            <p className="text-sm text-gray-500 max-md:text-xs truncate">{t('subtitle')}</p>
                        </div>
                    </div>
                    <div className="flex items-center gap-3">
                        <button 
                            onClick={fetchData}
                            className="p-2 hover:bg-gray-200 rounded-xl transition-colors text-gray-500"
                            title={t('refresh')}
                        >
                            <RefreshCw size={20} className={cn(loading && "animate-spin")} />
                        </button>
                        <button 
                            onClick={onClose} 
                            className="p-2 hover:bg-gray-200 rounded-xl transition-colors text-gray-500"
                        >
                            <X size={24} />
                        </button>
                    </div>
                </div>

                {/* Rights overview strip: quick toggle per account */}
                {accounts.length > 0 && (
                    <div className="px-6 py-3 border-b border-gray-100 bg-gray-50/70 flex flex-wrap items-center gap-3">
                        <span className="text-xs font-semibold text-gray-500 uppercase tracking-wider shrink-0">{t('rightsOverview')}</span>
                        {accounts.map(acc => (
                            <div key={acc.account_id} className="flex items-center gap-2 bg-white rounded-xl border border-gray-200 px-3 py-1.5 shadow-sm">
                                <span className="text-sm font-medium text-gray-700">@{acc.login}</span>
                                <div className="flex rounded-lg overflow-hidden border border-gray-200">
                                    <button
                                        onClick={() => acc.allow_write && handleTogglePermissions(acc.account_id, acc.allow_write)}
                                        disabled={updating === acc.account_id}
                                        className={cn(
                                            "px-2.5 py-1 text-[10px] font-bold transition-colors",
                                            !acc.allow_write ? "bg-blue-100 text-blue-700 dark:bg-[#3a3a3a] dark:text-gray-100" : "bg-gray-100 text-gray-500 hover:bg-gray-200",
                                            updating === acc.account_id && "opacity-50"
                                        )}
                                    >
                                        {t('readOnly')}
                                    </button>
                                    <button
                                        onClick={() => !acc.allow_write && handleTogglePermissions(acc.account_id, acc.allow_write)}
                                        disabled={updating === acc.account_id}
                                        className={cn(
                                            "px-2.5 py-1 text-[10px] font-bold transition-colors",
                                            acc.allow_write ? "bg-amber-100 text-amber-700 dark:bg-[#3a3a3a] dark:text-gray-100" : "bg-gray-100 text-gray-500 hover:bg-gray-200",
                                            updating === acc.account_id && "opacity-50"
                                        )}
                                    >
                                        {t('writeAccess')}
                                    </button>
                                </div>
                                {updating === acc.account_id && <Loader2 size={12} className="animate-spin text-gray-400" />}
                            </div>
                        ))}
                    </div>
                )}

                <div className="flex-1 overflow-hidden flex flex-col md:flex-row max-md:overflow-y-auto">

                    {/* Left: Accounts + Event timeline */}
                    <div className="w-full md:w-1/2 border-r border-gray-100 flex flex-col bg-white min-w-0 max-md:shrink-0">
                        <div className="p-4 border-b border-gray-50 flex justify-between items-center shrink-0">
                            <h3 className="font-bold text-gray-900 flex items-center gap-2">
                                <Shield size={18} className="text-blue-500" />
                                {t('connectedAccounts')}
                            </h3>
                            <button 
                                onClick={onOpenAddWizard}
                                className="p-1.5 hover:bg-blue-50 text-blue-600 rounded-lg transition-colors"
                                title="Add Account"
                            >
                                <UserPlus size={20} />
                            </button>
                        </div>
                        
                        <div className="flex-1 overflow-hidden flex flex-col md:flex-row min-h-0">
                            <div className="flex-1 min-h-0 overflow-auto p-4 space-y-4 border-b md:border-b-0 md:border-r border-gray-100">
                            {loading && accounts.length === 0 ? (
                                <div className="flex flex-col items-center justify-center py-12 text-gray-400">
                                    <Loader2 className="animate-spin mb-2" />
                                    <span className="text-sm">Loading accounts...</span>
                                </div>
                            ) : accounts.length === 0 ? (
                                <div className="text-center py-12 px-4">
                                    <p className="text-sm text-gray-400 italic">{t('noAccounts')}</p>
                                    <button 
                                        onClick={onOpenAddWizard}
                                        className="mt-4 px-4 py-2 bg-gray-900 text-white text-xs font-bold rounded-xl hover:bg-black transition-all dark:bg-[#e6e6e6] dark:text-gray-900 dark:hover:bg-white dark:shadow-none"
                                    >
                                        Connect GitHub
                                    </button>
                                </div>
                            ) : (
                                accounts.map(acc => (
                                    <div key={acc.account_id} className="p-4 rounded-2xl border border-gray-100 bg-gray-50/30 hover:shadow-md transition-all space-y-4">
                                        <div className="flex justify-between items-start">
                                            <div className="flex items-center gap-3">
                                                <div className="w-10 h-10 rounded-full bg-white border border-gray-200 flex items-center justify-center overflow-hidden">
                                                    <img 
                                                        src={`https://github.com/${acc.login}.png`} 
                                                        alt={acc.login}
                                                        className="w-full h-full object-cover"
                                                        onError={(e) => { (e.target as any).src = ''; }}
                                                    />
                                                </div>
                                                <div>
                                                    <div className="font-bold text-gray-900">@{acc.login}</div>
                                                    <div className="text-[10px] text-gray-400 uppercase tracking-wider font-semibold">
                                                        {acc.scopes ? acc.scopes.replace(/\s+/g, ', ') : 'Limited Access'}
                                                    </div>
                                                </div>
                                            </div>
                                            <button 
                                                onClick={() => handleDisconnect(acc.account_id)}
                                                className="p-1.5 text-gray-400 hover:text-red-500 hover:bg-red-50 rounded-lg transition-all"
                                            >
                                                <Trash2 size={16} />
                                            </button>
                                        </div>

                                        <div className="pt-2 border-t border-gray-100 flex flex-col gap-3">
                                            <div className="flex items-center justify-between">
                                                <div className="flex items-center gap-2">
                                                    {acc.allow_write ? (
                                                        <ShieldCheck size={16} className="text-green-500" />
                                                    ) : (
                                                        <Shield size={16} className="text-blue-500" />
                                                    )}
                                                    <span className="text-xs font-medium text-gray-700">
                                                        {acc.allow_write ? t('writeAccess') : t('readOnly')}
                                                    </span>
                                                </div>
                                                <button
                                                    onClick={() => handleTogglePermissions(acc.account_id, acc.allow_write)}
                                                    disabled={updating === acc.account_id}
                                                    className={cn(
                                                        "px-3 py-1 rounded-lg text-[10px] font-bold transition-all",
                                                        acc.allow_write
                                                            ? "bg-amber-50 text-amber-700 hover:bg-amber-100 dark:bg-[#3a3a3a] dark:text-gray-100"
                                                            : "bg-blue-50 text-blue-700 hover:bg-blue-100 dark:bg-[#3a3a3a] dark:text-gray-100",
                                                        updating === acc.account_id && "opacity-50 cursor-not-allowed"
                                                    )}
                                                >
                                                    {updating === acc.account_id ? (
                                                        <Loader2 size={12} className="animate-spin mx-auto" />
                                                    ) : (
                                                        acc.allow_write ? t('toggleReadAccess') : t('toggleWriteAccess')
                                                    )}
                                                </button>
                                            </div>
                                        </div>
                                    </div>
                                ))
                            )}
                            </div>

                            {/* Event timeline (newest first, like user identity) */}
                            <div className="w-full md:w-72 shrink-0 flex flex-col border-t md:border-t-0 border-gray-100 bg-gray-50/50">
                                <h3 className="text-sm font-semibold text-gray-700 shrink-0 p-4 pb-0">{t('eventTimeline')}</h3>
                                {activities.length === 0 ? (
                                    <div className="flex-1 min-h-0 overflow-auto p-4 pt-3">
                                        <p className="text-gray-500 text-sm">{t('noActivity')}</p>
                                    </div>
                                ) : (
                                    <div className="flex-1 min-h-0 overflow-y-auto p-4 pt-3">
                                        <div className="relative">
                                            <div className="absolute left-3 top-2 bottom-2 w-0.5 bg-gradient-to-b from-purple-200 via-purple-300 to-purple-200" />
                                            <ul className="space-y-0">
                                                {[...activities]
                                                    .sort((a, b) => (b.timestamp - a.timestamp))
                                                    .map((act, i) => {
                                                        const actionLower = (act.action || '').toLowerCase();
                                                        const isRead = actionLower.includes('read') || actionLower.includes('list');
                                                        const isEdit = actionLower.includes('edit') || actionLower.includes('update');
                                                        const isCommit = actionLower.includes('commit');
                                                        const isPush = actionLower.includes('push');
                                                        const Icon = isRead ? BookOpen : isEdit ? Pencil : isCommit ? GitCommit : isPush ? Upload : Activity;
                                                        const iconColor = act.success ? 'text-green-600 bg-green-100' : 'text-red-600 bg-red-100';
                                                        return (
                                                            <li key={i} className="relative flex gap-3 pb-4 last:pb-0">
                                                                <div className={`relative z-10 w-6 h-6 rounded-full shrink-0 mt-0.5 flex items-center justify-center ${iconColor}`}>
                                                                    <Icon className="w-3.5 h-3.5" />
                                                                </div>
                                                                <div className="flex-1 min-w-0">
                                                                    <p className="text-[10px] text-gray-500 font-mono">{formatTime(act.timestamp)}</p>
                                                                    <p className="text-xs font-medium text-gray-900 mt-0.5 capitalize">{act.action?.replace(/_/g, ' ') || t('action')}</p>
                                                                    {act.details && <p className="text-[11px] text-gray-600 truncate" title={act.details}>{act.details}</p>}
                                                                </div>
                                                            </li>
                                                        );
                                                    })}
                                            </ul>
                                        </div>
                                    </div>
                                )}
                            </div>
                        </div>
                    </div>

                    {/* Right: Repositories for selected account */}
                    <div className="w-full md:w-1/2 flex flex-col bg-gray-50/30 min-w-0">
                        <div className="p-4 border-b border-gray-100 flex flex-wrap items-center gap-2 bg-white shrink-0">
                            <h3 className="font-bold text-gray-900 flex items-center gap-2">
                                <FolderGit2 size={18} className="text-emerald-600" />
                                {t('repositories')}
                            </h3>
                            {accounts.length > 1 && (
                                <select
                                    value={reposAccountId ?? ''}
                                    onChange={(e) => setReposAccountId(e.target.value || null)}
                                    className="ml-2 text-xs font-medium text-gray-700 border border-gray-200 rounded-lg px-2 py-1.5 bg-white"
                                >
                                    {accounts.map(acc => (
                                        <option key={acc.account_id} value={acc.account_id}>@{acc.login}</option>
                                    ))}
                                </select>
                            )}
                        </div>
                        
                        <div className="flex-1 min-h-0 overflow-auto p-4 max-h-[min(50vh,360px)]">
                            {!reposAccountId ? (
                                <div className="flex flex-col items-center justify-center py-8 text-gray-500 text-sm">
                                    {t('noAccountForRepos')}
                                </div>
                            ) : loadingRepos ? (
                                <div className="flex flex-col items-center justify-center py-8 text-gray-400">
                                    <Loader2 className="animate-spin mb-2" size={24} />
                                    <p className="text-sm">{t('loadingRepos')}</p>
                                </div>
                            ) : reposError ? (
                                <div className="flex flex-col items-center justify-center py-8 text-amber-700 text-sm text-center px-4">
                                    <FolderGit2 size={32} className="mb-2 text-amber-400" />
                                    <p className="font-medium">{reposError}</p>
                                    <p className="text-xs text-gray-500 mt-1">{t('reposErrorHint')}</p>
                                </div>
                            ) : repos.length === 0 ? (
                                <div className="flex flex-col items-center justify-center py-8 text-gray-500 text-sm">
                                    <FolderGit2 size={32} className="mb-2 text-gray-300" />
                                    <p className="italic">{t('noRepos')}</p>
                                </div>
                            ) : (
                                <ul className="space-y-2">
                                    {repos.map((repo) => (
                                        <li key={repo.full_name} className="bg-white rounded-xl p-3 border border-gray-100 shadow-sm hover:shadow-md transition-shadow">
                                            <div className="flex items-start justify-between gap-2">
                                                <div className="flex-1 min-w-0">
                                                    <a
                                                        href={repo.html_url}
                                                        target="_blank"
                                                        rel="noopener noreferrer"
                                                        className="font-semibold text-gray-900 hover:text-emerald-600 text-sm flex items-center gap-1.5"
                                                    >
                                                        {repo.name}
                                                        <ExternalLink size={12} className="shrink-0 opacity-60" />
                                                    </a>
                                                    {repo.description && (
                                                        <p className="text-xs text-gray-600 mt-0.5 line-clamp-2">{repo.description}</p>
                                                    )}
                                                    <div className="flex items-center gap-3 mt-1.5 text-[10px] text-gray-400">
                                                        {repo.stargazers_count > 0 && (
                                                            <span className="flex items-center gap-0.5">
                                                                <Star size={10} className="fill-amber-400 text-amber-400" />
                                                                {repo.stargazers_count}
                                                            </span>
                                                        )}
                                                        {repo.updated_at && (
                                                            <span>{t('updated')}: {new Date(repo.updated_at).toLocaleDateString()}</span>
                                                        )}
                                                        {repo.private && <span className="text-amber-600">{t('private')}</span>}
                                                    </div>
                                                </div>
                                            </div>
                                        </li>
                                    ))}
                                </ul>
                            )}
                        </div>
                    </div>
                </div>
            </div>
        </div>
    );
}
