'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import React, { useState, useEffect } from 'react';
import { useTranslations } from 'next-intl';
import {
    X, ChevronRight, ChevronLeft, Loader2, AlertCircle,
    ExternalLink, CheckCircle2, Shield, KeyRound, Smartphone,
    Copy, Check
} from 'lucide-react';
import { cn } from '@/lib/utils';

/** GitHub logo (official mark) as black SVG. */
function GitHubLogo({ className }: { className?: string }) {
    return (
        <svg className={className} viewBox="0 0 24 24" fill="currentColor" aria-hidden>
            <path fillRule="evenodd" clipRule="evenodd" d="M12 2C6.477 2 2 6.477 2 12c0 4.42 2.865 8.166 6.839 9.489.5.092.682-.217.682-.482 0-.237-.008-.866-.013-1.7-2.782.603-3.369-1.34-3.369-1.34-.454-1.156-1.11-1.464-1.11-1.464-.908-.62.069-.608.069-.608 1.003.07 1.531 1.03 1.531 1.03.892 1.529 2.341 1.087 2.91.831.092-.646.35-1.086.636-1.336-2.22-.253-4.555-1.11-4.555-4.943 0-1.091.39-1.984 1.029-2.683-.103-.253-.446-1.27.098-2.647 0 0 .84-.269 2.75 1.025A9.578 9.578 0 0112 6.836c.85.004 1.705.114 2.504.336 1.909-1.294 2.747-1.025 2.747-1.025.546 1.377.203 2.394.1 2.647.64.699 1.028 1.592 1.028 2.683 0 3.842-2.339 4.687-4.566 4.935.359.309.678.919.678 1.852 0 1.336-.012 2.415-.012 2.743 0 .267.18.578.688.48C19.138 20.163 22 16.418 22 12c0-5.523-4.477-10-10-10z" />
        </svg>
    );
}

const api = (path: string) => path.startsWith('/') ? path : `/${path}`;

interface GitHubSetupWizardProps {
    isOpen: boolean;
    onClose: () => void;
    onComplete?: () => void;
    currentUser?: { username?: string; role?: string };
}

export default function GitHubSetupWizard({ isOpen, onClose, onComplete, currentUser }: GitHubSetupWizardProps) {
    const t = useTranslations('settings.githubWizard');
    const [currentStep, setCurrentStep] = useState(0);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState('');
    const [oauthConfigured, setOauthConfigured] = useState(false);
    const [accounts, setAccounts] = useState<Array<{ account_id: string; login?: string }>>([]);

    // Admin OAuth config (only when OAuth not configured)
    const [clientId, setClientId] = useState('');
    const [clientSecret, setClientSecret] = useState('');
    const [adminSaveStatus, setAdminSaveStatus] = useState<'idle' | 'saving' | 'ok' | 'fail'>('idle');

    // PAT path
    const [showTokenInput, setShowTokenInput] = useState(false);
    const [token, setToken] = useState('');
    const [tokenLoading, setTokenLoading] = useState(false);

    // Device Flow path
    const [deviceFlow, setDeviceFlow] = useState<{ device_code: string; user_code: string; verification_uri: string; interval: number } | null>(null);
    const [devicePolling, setDevicePolling] = useState(false);
    const [copied, setCopied] = useState(false);

    const fetchStatus = async () => {
        try {
            const res = await fetch(api('api/github/status'), { credentials: 'include' });
            if (res.ok) {
                const data = await res.json();
                setOauthConfigured(!!data.oauth_configured);
                setAccounts(data.accounts || []);
            }
        } catch {
            setOauthConfigured(false);
            setAccounts([]);
        }
    };

    const loadAdminConfig = async () => {
        try {
            const res = await fetch(api('api/config'), { credentials: 'include' });
            if (res.ok) {
                const c = await res.json();
                setClientId((c.github_oauth_client_id ?? '').trim());
                setClientSecret((c.github_oauth_client_secret ?? '').trim());
            }
        } catch {
            // ignore
        }
    };

    useEffect(() => {
        if (isOpen) {
            setError('');
            setShowTokenInput(false);
            setToken('');
            fetchStatus().then(() => {
                const params = typeof window !== 'undefined' ? new URLSearchParams(window.location.search) : null;
                if (params?.get('github_oauth') === 'success') {
                    onComplete?.();
                    onClose();
                }
            });
            if (currentUser?.role === 'admin') loadAdminConfig();
        }
    }, [isOpen, currentUser?.role]);

    const handleSaveAdminOAuth = async () => {
        setAdminSaveStatus('saving');
        setError('');
        try {
            const res = await fetch(api('api/config'), {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({
                    github_oauth_client_id: clientId.trim(),
                    github_oauth_client_secret: clientSecret.trim(),
                }),
            });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err.detail || t('saveError'));
            }
            setAdminSaveStatus('ok');
            await fetchStatus();
        } catch (e) {
            setAdminSaveStatus('fail');
            setError(e instanceof Error ? e.message : t('saveError'));
        }
    };

    const handleConnectOAuth = async () => {
        setError('');
        setLoading(true);
        try {
            const redirectBase = typeof window !== 'undefined' ? encodeURIComponent(window.location.origin) : '';
            const res = await fetch(api(`api/github/oauth/start?redirect_base=${redirectBase}`), { credentials: 'include' });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                setError(data?.detail || res.statusText || t('oauthStartError'));
                return;
            }
            if (data.authorization_url) {
                window.location.href = data.authorization_url;
                return;
            }
            setError(t('noAuthUrl'));
        } catch {
            setError(t('requestFailed'));
        } finally {
            setLoading(false);
        }
    };

    const handleStartDeviceFlow = async () => {
        setError('');
        setLoading(true);
        setDeviceFlow(null);
        try {
            const res = await fetch(api('api/github/device/start'), { credentials: 'include' });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                setError(data?.detail || res.statusText || t('oauthStartError'));
                return;
            }
            setDeviceFlow(data);
            startPolling(data.device_code, data.interval || 5);
        } catch {
            setError(t('requestFailed'));
        } finally {
            setLoading(false);
        }
    };

    const startPolling = async (deviceCode: string, interval: number) => {
        setDevicePolling(true);
        const poll = async () => {
            try {
                const res = await fetch(api('api/github/device/poll'), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    credentials: 'include',
                    body: JSON.stringify({ device_code: deviceCode }),
                });
                const data = await res.json().catch(() => ({}));
                
                if (data.status === 'success') {
                    setDevicePolling(false);
                    setDeviceFlow(null);
                    await fetchStatus();
                    onComplete?.();
                    onClose();
                    return true;
                } else if (data.status === 'error') {
                    if (data.error === 'authorization_pending') {
                        return false;
                    } else if (data.error === 'slow_down') {
                        // wait longer next time?
                        return false;
                    } else {
                        setError(data.error_description || t('deviceCodeError'));
                        setDevicePolling(false);
                        setDeviceFlow(null);
                        return true;
                    }
                }
                return false;
            } catch {
                return false;
            }
        };

        const timer = setInterval(async () => {
            const done = await poll();
            if (done) clearInterval(timer);
        }, (interval + 1) * 1000);

        // cleanup on unmount
        return () => clearInterval(timer);
    };

    const handleConnectToken = async () => {
        const trimmedToken = token.trim();
        if (!trimmedToken) {
            setError(t('tokenRequired'));
            return;
        }
        setError('');
        setTokenLoading(true);
        try {
            const res = await fetch(api('api/github/connect-token'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({ token: trimmedToken }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                setError(data?.detail || res.statusText || t('tokenConnectError'));
                return;
            }
            setToken('');
            setShowTokenInput(false);
            await fetchStatus();
            onComplete?.();
            onClose();
        } catch {
            setError(t('requestFailed'));
        } finally {
            setTokenLoading(false);
        }
    };

    const isAdmin = currentUser?.role === 'admin';

    const stepTitles = [t('stepIntroTitle'), t('stepConnectTitle')] as const;
    const stepSubtitles = [t('stepIntroSubtitle'), t('stepConnectSubtitle')] as const;

    if (!isOpen) return null;

    return (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 backdrop-blur-sm max-md:p-0">
            <div className="bg-white rounded-2xl shadow-2xl w-full max-w-2xl mx-4 overflow-hidden border border-gray-200 max-md:max-w-none max-md:mx-0 max-md:h-[100dvh] max-md:rounded-none max-md:border-0 max-md:flex max-md:flex-col">
                <div className="flex items-center justify-between p-6 border-b border-gray-200 bg-gray-50 max-md:p-4 max-md:shrink-0">
                    <div className="flex items-center gap-3">
                        <div className="w-10 h-10 rounded-xl bg-gray-900 flex items-center justify-center">
                            <GitHubLogo className="w-5 h-5 text-white" />
                        </div>
                        <div>
                            <h2 className="text-xl font-bold text-gray-900">{stepTitles[currentStep]}</h2>
                            <p className="text-sm text-gray-500">{stepSubtitles[currentStep]}</p>
                        </div>
                    </div>
                    <button onClick={onClose} className="p-2 hover:bg-gray-200 rounded-lg transition-colors">
                        <X className="w-5 h-5 text-gray-500" />
                    </button>
                </div>

                <div className="px-6 pt-4">
                    <div className="flex items-center gap-2">
                        {[0, 1].map((idx) => (
                            <React.Fragment key={idx}>
                                <div className={cn(
                                    'w-8 h-8 rounded-full flex items-center justify-center text-sm font-medium transition-all',
                                    idx < currentStep ? 'bg-green-500 text-white' : idx === currentStep ? 'bg-gray-900 text-white dark:bg-[#3a3a3a] dark:text-white' : 'bg-gray-200 text-gray-500'
                                )}>
                                    {idx < currentStep ? <CheckCircle2 className="w-4 h-4" /> : idx + 1}
                                </div>
                                {idx < 1 && (
                                    <div className={cn('flex-1 h-1 rounded-full transition-all', idx < currentStep ? 'bg-green-500' : 'bg-gray-200')} />
                                )}
                            </React.Fragment>
                        ))}
                    </div>
                </div>

                <div className="p-6 min-h-[320px] max-md:min-h-0 max-md:flex-1 max-md:overflow-y-auto max-md:p-4">
                    {error && (
                        <div className="mb-4 p-3 rounded-lg bg-red-50 border border-red-200 flex items-center gap-2 text-red-700 text-sm">
                            <AlertCircle className="w-4 h-4 shrink-0" />
                            {error}
                        </div>
                    )}

                    {currentStep === 0 && (
                        <div className="space-y-6">
                            <div className="text-center py-6">
                                <div className="w-20 h-20 mx-auto rounded-2xl bg-gray-900 flex items-center justify-center mb-4">
                                    <GitHubLogo className="w-10 h-10 text-white" />
                                </div>
                                <h3 className="text-2xl font-bold text-gray-900 mb-2">{t('connectGitHub')}</h3>
                                <p className="text-gray-500 max-w-md mx-auto">
                                    {t('connectGitHubDesc')}
                                </p>
                            </div>
                            <div className="grid grid-cols-2 gap-4 max-md:grid-cols-1">
                                <div className="p-4 rounded-xl bg-gray-50 border border-gray-200">
                                    <Shield className="w-8 h-8 text-green-600 mb-2" />
                                    <h4 className="font-semibold text-gray-900">{t('secure')}</h4>
                                    <p className="text-sm text-gray-500">{t('secureDesc')}</p>
                                </div>
                                <div className="p-4 rounded-xl bg-gray-50 border border-gray-200">
                                    <KeyRound className="w-8 h-8 text-gray-700 mb-2" />
                                    <h4 className="font-semibold text-gray-900">{t('twoOptions')}</h4>
                                    <p className="text-sm text-gray-500">{t('twoOptionsDesc')}</p>
                                </div>
                            </div>
                        </div>
                    )}

                    {currentStep === 1 && (
                        <div className="space-y-6">
                            {accounts.length > 0 && (
                                <div className="p-3 rounded-lg bg-green-50 border border-green-200 text-green-800 text-sm">
                                    {t('connectedAs', { login: accounts[0]?.login ?? accounts[0]?.account_id ?? '' })}
                                </div>
                            )}

                            {!oauthConfigured && isAdmin && (
                                <div className="p-4 rounded-xl bg-amber-50 border border-amber-200 space-y-3">
                                    <h4 className="font-semibold text-amber-900">{t('oauthNotConfigured')}</h4>
                                    <p className="text-sm text-amber-800">
                                        {t('oauthNotConfiguredDesc')}
                                    </p>
                                    <div className="grid grid-cols-1 gap-2">
                                        <div>
                                            <label className="block text-xs font-medium text-gray-600 mb-1">{t('clientId')}</label>
                                            <input
                                                type="text"
                                                value={clientId}
                                                onChange={e => { setClientId(e.target.value); setAdminSaveStatus('idle'); }}
                                                className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm"
                                                placeholder="Ov23li..."
                                            />
                                        </div>
                                        <button
                                            onClick={handleSaveAdminOAuth}
                                            disabled={adminSaveStatus === 'saving' || !clientId.trim()}
                                            className="inline-flex items-center gap-2 px-4 py-2 rounded-xl bg-amber-600 text-white text-sm font-medium hover:bg-amber-700 disabled:opacity-50 dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-white dark:shadow-none"
                                        >
                                            {adminSaveStatus === 'saving' && <Loader2 className="w-4 h-4 animate-spin" />}
                                            {adminSaveStatus === 'ok' ? t('saved') : adminSaveStatus === 'fail' ? t('saveFailed') : t('saveOAuthSettings')}
                                        </button>
                                    </div>
                                </div>
                            )}

                            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                                <div className={cn(
                                    "p-4 rounded-xl border shadow-sm space-y-3 transition-opacity relative",
                                    !oauthConfigured ? "bg-gray-50 border-gray-200 opacity-60" : "bg-white border-purple-200 ring-1 ring-purple-100"
                                )}>
                                    {oauthConfigured && (
                                        <div className="absolute -top-2 -right-2 bg-purple-600 text-white text-[10px] font-bold px-2 py-0.5 rounded-full shadow-sm">
                                            RECOMMENDED
                                        </div>
                                    )}
                                    <h4 className="font-semibold text-gray-900 flex items-center gap-1.5">
                                        <Smartphone size={16} className="text-purple-500" />
                                        {t('connectWithDevice')}
                                    </h4>
                                    <p className="text-xs text-gray-500 leading-relaxed">{t('connectWithDeviceDesc')}</p>
                                    <button
                                        onClick={handleStartDeviceFlow}
                                        disabled={loading || !!deviceFlow || !oauthConfigured}
                                        className="w-full inline-flex items-center justify-center gap-2 px-4 py-2 rounded-xl bg-purple-600 text-white text-sm font-medium hover:bg-purple-700 disabled:opacity-50"
                                    >
                                        {loading && !!deviceFlow ? <Loader2 className="w-4 h-4 animate-spin" /> : <Shield className="w-4 h-4" />}
                                        {t('connectWithDevice')}
                                    </button>
                                </div>

                                <div className={cn(
                                    "p-4 rounded-xl border shadow-sm space-y-3 transition-opacity",
                                    !oauthConfigured ? "bg-gray-50 border-gray-200 opacity-60" : "bg-white border-gray-100"
                                )}>
                                    <h4 className="font-semibold text-gray-900 flex items-center gap-1.5">
                                        <ExternalLink size={16} className="text-blue-500" />
                                        {t('connectWithGitHub')}
                                    </h4>
                                    <p className="text-xs text-gray-500 leading-relaxed">{t('connectWithGitHubDesc')}</p>
                                    <button
                                        onClick={handleConnectOAuth}
                                        disabled={loading || !!deviceFlow || !oauthConfigured}
                                        className="w-full inline-flex items-center justify-center gap-2 px-4 py-2 rounded-xl bg-gray-900 text-white text-sm font-medium hover:bg-gray-800 disabled:opacity-50 dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-white dark:shadow-none"
                                    >
                                        {loading && !deviceFlow ? <Loader2 className="w-4 h-4 animate-spin" /> : <ExternalLink className="w-4 h-4" />}
                                        {t('connectWithGitHub')}
                                    </button>
                                </div>
                            </div>

                            {deviceFlow && (
                                <div className="p-6 rounded-2xl bg-purple-50 border border-purple-100 text-center space-y-4 animate-in fade-in zoom-in duration-300">
                                    <div className="space-y-1">
                                        <p className="text-sm text-purple-800 font-medium">
                                            {t('deviceCodeInstructions', { url: deviceFlow.verification_uri })}
                                        </p>
                                        <div className="relative group mt-2">
                                            <div 
                                                onClick={() => {
                                                    navigator.clipboard.writeText(deviceFlow.user_code);
                                                    setCopied(true);
                                                    setTimeout(() => setCopied(false), 2000);
                                                    window.open(deviceFlow.verification_uri, '_blank');
                                                }}
                                                className="text-3xl font-mono font-bold tracking-[0.2em] text-purple-900 py-3 bg-white/50 rounded-xl border border-purple-200 cursor-pointer hover:bg-white/80 transition-all hover:border-purple-400 group"
                                            >
                                                {deviceFlow.user_code}
                                            </div>
                                            <div
                                                className="absolute right-2 top-1/2 -translate-y-1/2 p-2 rounded-lg text-purple-600 bg-white/80 pointer-events-none"
                                            >
                                                {copied ? <Check size={20} className="text-green-600 animate-in zoom-in" /> : <Copy size={20} className="group-hover:scale-110 transition-transform" />}
                                            </div>
                                        </div>
                                    </div>
                                    
                                    <div className="flex flex-col items-center gap-2">
                                        <div className="flex items-center gap-2 text-xs text-purple-600 font-medium">
                                            <Loader2 className="w-3 h-3 animate-spin" />
                                            {t('deviceCodePolling')}
                                        </div>
                                        <button 
                                            onClick={() => { setDeviceFlow(null); setDevicePolling(false); }}
                                            className="text-xs text-purple-700 hover:underline"
                                        >
                                            {t('cancel')}
                                        </button>
                                    </div>
                                    
                                    <a 
                                        href={deviceFlow.verification_uri} 
                                        target="_blank" 
                                        rel="noopener noreferrer"
                                        className="inline-flex items-center gap-1.5 text-xs text-purple-700 font-bold hover:text-purple-900"
                                    >
                                        {deviceFlow.verification_uri}
                                        <ExternalLink size={12} />
                                    </a>
                                </div>
                            )}

                            <div className="border-t border-gray-200 pt-4">
                                <button
                                    type="button"
                                    onClick={() => setShowTokenInput(!showTokenInput)}
                                    className="text-sm font-medium text-gray-700 hover:text-gray-900"
                                >
                                    {showTokenInput ? t('hideTokenOption') : t('useTokenInstead')}
                                </button>
                                {showTokenInput && (
                                    <div className="mt-3 space-y-2">
                                        <p className="text-sm text-gray-600">
                                            {t('tokenHint')}
                                        </p>
                                        <input
                                            type="password"
                                            value={token}
                                            onChange={e => setToken(e.target.value)}
                                            placeholder={t('tokenPlaceholder')}
                                            className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm"
                                        />
                                        <button
                                            onClick={handleConnectToken}
                                            disabled={tokenLoading || !token.trim()}
                                            className="inline-flex items-center gap-2 px-4 py-2 rounded-xl bg-gray-700 text-white text-sm font-medium hover:bg-gray-600 disabled:opacity-50 dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-white dark:shadow-none"
                                        >
                                            {tokenLoading && <Loader2 className="w-4 h-4 animate-spin" />}
                                            {t('connectWithToken')}
                                        </button>
                                    </div>
                                )}
                            </div>
                        </div>
                    )}

                    <div className="flex justify-between mt-6 pt-4 border-t border-gray-200">
                        <button
                            onClick={() => currentStep > 0 ? setCurrentStep(0) : onClose()}
                            className="inline-flex items-center gap-1 text-gray-600 hover:text-gray-900 text-sm font-medium"
                        >
                            <ChevronLeft className="w-4 h-4" />
                            {currentStep === 0 ? t('cancel') : t('back')}
                        </button>
                        {currentStep === 0 && (
                            <button
                                onClick={() => setCurrentStep(1)}
                                className="inline-flex items-center gap-1 px-4 py-2 rounded-xl bg-gray-900 text-white text-sm font-medium hover:bg-gray-800 dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-white dark:shadow-none"
                            >
                                {t('next')}
                                <ChevronRight className="w-4 h-4" />
                            </button>
                        )}
                    </div>
                </div>
            </div>
        </div>
    );
}
