'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import React, { useState, useEffect } from 'react';
import {
    X, ChevronRight, ChevronLeft, Mail, Loader2, AlertCircle,
    ExternalLink, CheckCircle2, Shield, ChevronDown, ChevronUp, Settings2
} from 'lucide-react';
import { cn } from '@/lib/utils';

const api = (path: string) => path.startsWith('/') ? path : `/${path}`;

interface EmailSetupWizardProps {
    isOpen: boolean;
    onClose: () => void;
    onComplete?: () => void;
    existingAccounts?: Array<{ account_id: string; email: string; provider: string; enabled?: boolean; last_verified_at?: string }>;
    /** When set, the "For admins: OAuth client" section is only shown for role === 'admin'. */
    currentUser?: { role?: string };
}

const STEPS = [
    { id: 'intro', title: 'Email Setup', subtitle: 'Connect your mailbox' },
    { id: 'choose', title: 'Choose Provider', subtitle: 'How do you want to connect?' },
    { id: 'connect', title: 'Connect', subtitle: 'Sign in or enter credentials' },
    { id: 'complete', title: 'Complete', subtitle: 'Manage your accounts' },
];

const PROVIDER_OPTIONS: Array<{ id: string; name: string; icon: string; desc: string; oauth: boolean }> = [
    { id: 'gmail', name: 'Google (Gmail)', icon: 'G', desc: 'Sign in with your Google account', oauth: true },
    { id: 'microsoft', name: 'Microsoft (Outlook)', icon: 'M', desc: 'Sign in with your Microsoft account', oauth: true },
    { id: 'apple', name: 'Apple (iCloud Mail)', icon: 'A', desc: 'Use IMAP with app-specific password', oauth: false },
    { id: 'imap', name: 'Other (IMAP/SMTP)', icon: 'O', desc: 'Email and app password', oauth: false },
];

export default function EmailSetupWizard({ isOpen, onClose, onComplete, existingAccounts = [], currentUser }: EmailSetupWizardProps) {
    const [currentStep, setCurrentStep] = useState(0);
    const [provider, setProvider] = useState<string>('');
    const [authUrl, setAuthUrl] = useState('');
    const [accounts, setAccounts] = useState<typeof existingAccounts>(existingAccounts);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState('');
    const [imapEmail, setImapEmail] = useState('');
    const [imapPassword, setImapPassword] = useState('');
    const [imapHost, setImapHost] = useState('');
    const [imapPort, setImapPort] = useState('993');
    const [smtpHost, setSmtpHost] = useState('');
    const [smtpPort, setSmtpPort] = useState('587');
    const [testResult, setTestResult] = useState<'idle' | 'testing' | 'ok' | 'fail'>('idle');
    const [testError, setTestError] = useState('');
    const [testHint, setTestHint] = useState('');
    const [verifyLoading, setVerifyLoading] = useState<string | null>(null);
    // Admin OAuth (expandable): set once so all users get one-click Sign in with Google/Outlook
    const [adminOAuthOpen, setAdminOAuthOpen] = useState(false);
    const [oauthGoogleId, setOauthGoogleId] = useState('');
    const [oauthGoogleSecret, setOauthGoogleSecret] = useState('');
    const [oauthMicrosoftId, setOauthMicrosoftId] = useState('');
    const [oauthMicrosoftSecret, setOauthMicrosoftSecret] = useState('');
    const [adminSaveStatus, setAdminSaveStatus] = useState<'idle' | 'saving' | 'ok' | 'fail'>('idle');
    const [adminSaveError, setAdminSaveError] = useState('');
    // OAuth status: only show Gmail/Microsoft when admin has configured them
    const [oauthStatus, setOauthStatus] = useState<{ oauth_google_configured: boolean; oauth_microsoft_configured: boolean }>({ oauth_google_configured: false, oauth_microsoft_configured: false });

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

    const loadOAuthConfig = async () => {
        try {
            const res = await fetch(api('api/config'), { credentials: 'include' });
            if (res.ok) {
                const c = await res.json();
                setOauthGoogleId((c.email_oauth_google_client_id ?? '').trim());
                setOauthGoogleSecret((c.email_oauth_google_client_secret ?? '').trim());
                setOauthMicrosoftId((c.email_oauth_microsoft_client_id ?? '').trim());
                setOauthMicrosoftSecret((c.email_oauth_microsoft_client_secret ?? '').trim());
            }
        } catch {
            // ignore
        }
    };

    const fetchOAuthStatus = async () => {
        try {
            const res = await fetch(api('api/email/oauth-status'), { credentials: 'include' });
            if (res.ok) {
                const data = await res.json();
                setOauthStatus({
                    oauth_google_configured: !!data.oauth_google_configured,
                    oauth_microsoft_configured: !!data.oauth_microsoft_configured,
                });
            }
        } catch {
            setOauthStatus({ oauth_google_configured: false, oauth_microsoft_configured: false });
        }
    };

    useEffect(() => {
        if (isOpen) {
            if (currentUser?.role === 'admin') loadOAuthConfig();
            fetchOAuthStatus();
            fetchAccounts().then(() => {
                const params = new URLSearchParams(typeof window !== 'undefined' ? window.location.search : '');
                if (params.get('email_oauth') === 'success') setCurrentStep(3);
            });
        }
    }, [isOpen, currentUser?.role]);

    const visibleProviders = React.useMemo(() => {
        return PROVIDER_OPTIONS.filter((p) => {
            if (p.id === 'gmail') return true;  // Built-in client ID, always available
            if (p.id === 'microsoft') return oauthStatus.oauth_microsoft_configured;
            return true;
        });
    }, [oauthStatus.oauth_microsoft_configured]);

    useEffect(() => {
        if (isOpen && accounts.length > 0 && currentStep === 0) setCurrentStep(3);
    }, [isOpen, accounts.length, currentStep]);

    const handleSaveAdminOAuth = async () => {
        setAdminSaveStatus('saving');
        setAdminSaveError('');
        try {
            const res = await fetch(api('api/config'), {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({
                    email_oauth_google_client_id: oauthGoogleId.trim(),
                    email_oauth_google_client_secret: oauthGoogleSecret.trim(),
                    email_oauth_microsoft_client_id: oauthMicrosoftId.trim(),
                    email_oauth_microsoft_client_secret: oauthMicrosoftSecret.trim(),
                }),
            });
            if (!res.ok) {
                const err = await res.json();
                throw new Error(err.detail ?? 'Failed to save');
            }
            setAdminSaveStatus('ok');
            fetchOAuthStatus();
        } catch (e) {
            setAdminSaveStatus('fail');
            setAdminSaveError(e instanceof Error ? e.message : 'Failed to save');
        }
    };

    const handleChooseProvider = (id: string) => {
        setProvider(id);
        setError('');
        if (id === 'imap') {
            setCurrentStep(2);
            return;
        }
        if (id === 'apple') {
            setCurrentStep(2);
            return;
        }
        setLoading(true);
        fetch(api(`api/email/oauth/start?provider=${id}`), { credentials: 'include' })
            .then(r => {
                if (!r.ok) throw new Error(r.status === 400 ? 'Sign-in could not be started. Restart VAF and try again.' : `Request failed: ${r.status}`);
                return r.json();
            })
            .then(data => {
                const url = data.authorization_url || '';
                setAuthUrl(url);
                setCurrentStep(2);
                if (url && (url.startsWith('https://accounts.google.com') || url.startsWith('https://login.microsoftonline.com') || url.startsWith('https://appleid.apple.com')) && typeof window !== 'undefined') {
                    window.open(url, '_blank', 'noopener,noreferrer');
                } else if (url && typeof window !== 'undefined') {
                    window.open(url, '_blank', 'noopener,noreferrer');
                } else if (!url) {
                    setError('No sign-in URL returned. Check OAuth client ID in Settings.');
                }
            })
            .catch(e => {
                setError(e?.message || 'Failed to start sign-in');
            })
            .finally(() => setLoading(false));
    };

    const handleTestConnection = async () => {
        if (!imapEmail.trim() || !imapPassword.trim()) {
            setError('Email and password are required to test.');
            setTestResult('idle');
            return;
        }
        setTestResult('testing');
        setTestError('');
        setTestHint('');
        setError('');
        try {
            const res = await fetch(api('api/email/accounts/test'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({
                    email: imapEmail.trim(),
                    password: imapPassword,
                    imap_host: imapHost || undefined,
                    imap_port: imapPort ? parseInt(imapPort, 10) : undefined,
                }),
            });
            const data = await res.json();
            if (data.ok) setTestResult('ok');
            else {
                setTestResult('fail');
                setTestError(data.error || 'Connection failed');
                setTestHint(data.hint || '');
                if (data.hint) {
                    const urlMatch = data.hint.match(/https:\/\/[^\s)]+/);
                    if (urlMatch) window.open(urlMatch[0], '_blank', 'noopener,noreferrer');
                }
            }
        } catch (e) {
            setTestResult('fail');
            setTestError(e instanceof Error ? e.message : 'Test failed');
        }
    };

    const handleVerifyAccount = async (accountId: string) => {
        setVerifyLoading(accountId);
        setError('');
        try {
            const res = await fetch(api(`api/email/accounts/${encodeURIComponent(accountId)}/verify`), {
                method: 'POST',
                credentials: 'include',
            });
            const data = await res.json();
            if (data.ok) await fetchAccounts();
            else {
                setError(data.error || 'Verification failed');
                if (data.hint) setTestHint(data.hint);
            }
        } catch {
            setError('Verification request failed');
        } finally {
            setVerifyLoading(null);
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

    const handleFinish = () => {
        onComplete?.();
        onClose();
    };

    const canProceed = () => {
        switch (currentStep) {
            case 0: return true;
            case 1: return true;
            case 2:
                if (provider === 'imap') return testResult === 'ok';
                return true;
            case 3: return true;
            default: return false;
        }
    };

    const addAccountAndGoToComplete = async () => {
        if (!imapEmail.trim() || !imapPassword.trim()) return;
        setLoading(true);
        setError('');
        try {
            const res = await fetch(api('api/email/accounts'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({
                    email: imapEmail.trim(),
                    password: imapPassword,
                    imap_host: imapHost || undefined,
                    imap_port: imapPort ? parseInt(imapPort, 10) : undefined,
                    smtp_host: smtpHost || undefined,
                    smtp_port: smtpPort ? parseInt(smtpPort, 10) : undefined,
                }),
            });
            if (!res.ok) {
                const err = await res.json();
                throw new Error(err.detail || 'Failed to add account');
            }
            await fetchAccounts();
            setImapEmail('');
            setImapPassword('');
            setTestResult('idle');
            setCurrentStep(3);
        } catch (e) {
            setError(e instanceof Error ? e.message : 'Failed to add account');
        } finally {
            setLoading(false);
        }
    };

    const handleNextClick = () => {
        if (currentStep === 1 && !provider) return;
        if (currentStep === 2 && provider === 'imap') {
            addAccountAndGoToComplete();
            return;
        }
        if (currentStep < STEPS.length - 1) setCurrentStep(currentStep + 1);
    };

    const prevStep = () => {
        if (currentStep === 3) {
            setCurrentStep(1);
            setProvider('');
            return;
        }
        if (currentStep > 0) {
            if (currentStep === 2) setProvider('');
            setCurrentStep(currentStep - 1);
        }
    };

    if (!isOpen) return null;

    const isNextDisabled = (currentStep === 1 && !provider) || (currentStep === 2 && provider === 'apple') || (currentStep === 2 && provider === 'imap' && (testResult !== 'ok' || loading));

    return (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 backdrop-blur-sm max-md:p-0">
            <div className="bg-white rounded-2xl shadow-2xl w-full max-w-2xl mx-4 overflow-hidden border border-gray-200 max-md:max-w-none max-md:mx-0 max-md:h-[100dvh] max-md:rounded-none max-md:border-0 max-md:flex max-md:flex-col">
                {/* Header */}
                <div className="flex items-center justify-between p-6 border-b border-gray-200 bg-gray-50 max-md:p-4 max-md:shrink-0">
                    <div className="flex items-center gap-3">
                        <div className="w-10 h-10 rounded-xl bg-red-500 flex items-center justify-center">
                            <Mail className="w-5 h-5 text-white" />
                        </div>
                        <div>
                            <h2 className="text-xl font-bold text-gray-900">{STEPS[currentStep].title}</h2>
                            <p className="text-sm text-gray-500">{STEPS[currentStep].subtitle}</p>
                        </div>
                    </div>
                    <button onClick={onClose} className="p-2 hover:bg-gray-200 rounded-lg transition-colors">
                        <X className="w-5 h-5 text-gray-500" />
                    </button>
                </div>

                {/* Progress */}
                <div className="px-6 pt-4">
                    <div className="flex items-center gap-2">
                        {STEPS.map((step, idx) => (
                            <React.Fragment key={step.id}>
                                <div className={cn(
                                    "w-8 h-8 rounded-full flex items-center justify-center text-sm font-medium transition-all",
                                    idx < currentStep ? "bg-green-500 text-white" :
                                    idx === currentStep ? "bg-gray-900 text-white dark:bg-[#3a3a3a] dark:text-white" :
                                    "bg-gray-200 text-gray-500"
                                )}>
                                    {idx < currentStep ? <CheckCircle2 className="w-4 h-4" /> : idx + 1}
                                </div>
                                {idx < STEPS.length - 1 && (
                                    <div className={cn(
                                        "flex-1 h-1 rounded-full transition-all",
                                        idx < currentStep ? "bg-green-500" : "bg-gray-200"
                                    )} />
                                )}
                            </React.Fragment>
                        ))}
                    </div>
                </div>

                {/* Content */}
                <div className="p-6 min-h-[350px] max-md:min-h-0 max-md:flex-1 max-md:overflow-y-auto max-md:p-4">
                    {error && (
                        <div className="mb-4 p-3 rounded-lg bg-red-50 border border-red-200 flex items-center gap-2 text-red-700 text-sm">
                            <AlertCircle className="w-4 h-4 shrink-0" />
                            {error}
                        </div>
                    )}

                    {/* Step 0: Intro */}
                    {currentStep === 0 && (
                        <div className="space-y-6">
                            <div className="text-center py-8">
                                <div className="w-20 h-20 mx-auto rounded-2xl bg-red-500 flex items-center justify-center mb-4">
                                    <Mail className="w-10 h-10 text-white" />
                                </div>
                                <h3 className="text-2xl font-bold text-gray-900 mb-2">Connect Email to VAF</h3>
                                <p className="text-gray-500 max-w-md mx-auto">
                                    Let your agent read and send emails from your mailbox. Connect Gmail, Outlook, or any provider via IMAP. Only mail access is requested; credentials are stored securely.
                                </p>
                            </div>
                            <div className="grid grid-cols-2 gap-4">
                                <div className="p-4 rounded-xl bg-gray-50 border border-gray-200">
                                    <Shield className="w-8 h-8 text-green-600 mb-2" />
                                    <h4 className="font-semibold text-gray-900">Secure</h4>
                                    <p className="text-sm text-gray-500">Credentials in OS keyring or encrypted storage</p>
                                </div>
                                <div className="p-4 rounded-xl bg-gray-50 border border-gray-200">
                                    <Mail className="w-8 h-8 text-gray-700 mb-2" />
                                    <h4 className="font-semibold text-gray-900">Read & Send</h4>
                                    <p className="text-sm text-gray-500">Agent can check inbox and send on your behalf</p>
                                </div>
                            </div>
                        </div>
                    )}

                    {/* Step 1: Choose Provider */}
                    {currentStep === 1 && (
                        <div className="space-y-6">
                            <p className="text-sm text-gray-600">Only access to read and send email is requested.</p>
                            <div className="space-y-2">
                                {visibleProviders.map((p) => (
                                    <button
                                        key={p.id}
                                        onClick={() => handleChooseProvider(p.id)}
                                        disabled={loading}
                                        className={cn(
                                            "w-full p-4 rounded-xl border text-left flex items-center gap-3 transition-colors",
                                            "border-gray-200 hover:border-gray-300 hover:bg-gray-50",
                                            provider === p.id && "border-gray-900 bg-gray-50",
                                            loading && "opacity-60 cursor-not-allowed"
                                        )}
                                    >
                                        <div className="w-10 h-10 rounded-lg bg-gray-100 flex items-center justify-center text-lg font-semibold text-gray-700">
                                            {p.icon}
                                        </div>
                                        <div className="flex-1 min-w-0">
                                            <div className="font-medium text-gray-900">{p.name}</div>
                                            <div className="text-sm text-gray-500">{p.desc}</div>
                                        </div>
                                        {loading ? <Loader2 className="w-5 h-5 animate-spin text-gray-400" /> : <ChevronRight className="w-5 h-5 text-gray-400" />}
                                    </button>
                                ))}
                            </div>

                            {/* For admins only: OAuth client (one-click for all users) — expandable */}
                            {currentUser?.role === 'admin' && (
                                <div className="border border-gray-200 rounded-xl overflow-hidden">
                                    <button
                                        type="button"
                                        onClick={() => setAdminOAuthOpen(!adminOAuthOpen)}
                                        className="w-full flex items-center justify-between px-4 py-3 text-left text-sm font-medium text-gray-700 bg-gray-50 hover:bg-gray-100 transition-colors"
                                    >
                                        <span className="flex items-center gap-2">
                                            <Settings2 className="w-4 h-4 text-gray-500" />
                                            For admins: OAuth client (one-click Gmail/Outlook for all users)
                                        </span>
                                        {adminOAuthOpen ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
                                    </button>
                                    {adminOAuthOpen && (
                                        <div className="p-4 bg-white border-t border-gray-200 space-y-4">
                                            <p className="text-xs text-gray-600">
                                                Set these once so everyone on this VAF instance can use &quot;Sign in with Google&quot; or &quot;Sign in with Microsoft&quot; without further setup. Otherwise users can still connect Gmail/Outlook via <strong>Other (IMAP/SMTP)</strong> with an app password. Do not commit the client secret to source code.
                                            </p>
                                            <div className="grid grid-cols-1 gap-3">
                                                <div>
                                                    <label className="block text-xs font-medium text-gray-500 mb-1">Google Client ID</label>
                                                    <input
                                                        type="text"
                                                        value={oauthGoogleId}
                                                        onChange={e => { setOauthGoogleId(e.target.value); setAdminSaveStatus('idle'); }}
                                                        placeholder="xxx.apps.googleusercontent.com"
                                                        className="w-full px-3 py-2 text-sm rounded-lg border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400"
                                                    />
                                                </div>
                                                <div>
                                                    <label className="block text-xs font-medium text-gray-500 mb-1">Google Client secret</label>
                                                    <input
                                                        type="password"
                                                        value={oauthGoogleSecret}
                                                        onChange={e => { setOauthGoogleSecret(e.target.value); setAdminSaveStatus('idle'); }}
                                                        placeholder="Optional if using Web application client"
                                                        className="w-full px-3 py-2 text-sm rounded-lg border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400"
                                                    />
                                                </div>
                                                <div>
                                                    <label className="block text-xs font-medium text-gray-500 mb-1">Microsoft Client ID (optional)</label>
                                                    <input
                                                        type="text"
                                                        value={oauthMicrosoftId}
                                                        onChange={e => { setOauthMicrosoftId(e.target.value); setAdminSaveStatus('idle'); }}
                                                        placeholder="Application (client) ID from Entra"
                                                        className="w-full px-3 py-2 text-sm rounded-lg border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400"
                                                    />
                                                </div>
                                                <div>
                                                    <label className="block text-xs font-medium text-gray-500 mb-1">Microsoft Client secret (optional)</label>
                                                    <input
                                                        type="password"
                                                        value={oauthMicrosoftSecret}
                                                        onChange={e => { setOauthMicrosoftSecret(e.target.value); setAdminSaveStatus('idle'); }}
                                                        placeholder="Client secret from Entra"
                                                        className="w-full px-3 py-2 text-sm rounded-lg border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400"
                                                    />
                                                </div>
                                            </div>
                                            <div className="flex items-center gap-2">
                                                <button
                                                    type="button"
                                                    onClick={handleSaveAdminOAuth}
                                                    disabled={adminSaveStatus === 'saving'}
                                                    className="px-3 py-2 rounded-lg text-sm font-medium bg-gray-900 text-white hover:bg-gray-800 disabled:opacity-50 dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-white dark:shadow-none"
                                                >
                                                    {adminSaveStatus === 'saving' ? <Loader2 className="w-4 h-4 animate-spin inline" /> : 'Save'}
                                                </button>
                                                {adminSaveStatus === 'ok' && <span className="text-sm text-green-600">Saved.</span>}
                                                {adminSaveStatus === 'fail' && adminSaveError && (
                                                    <span className="text-sm text-red-600">{adminSaveError}</span>
                                                )}
                                            </div>
                                        </div>
                                    )}
                                </div>
                            )}
                        </div>
                    )}

                    {/* Step 2: Connect (OAuth or IMAP) */}
                    {currentStep === 2 && provider === 'apple' && (
                        <div className="space-y-6">
                            <h3 className="text-lg font-semibold text-gray-900">iCloud Mail</h3>
                            <p className="text-sm text-gray-600 bg-amber-50 p-4 rounded-xl border border-amber-200">
                                iCloud Mail does not offer OAuth access for third-party apps. To connect your iCloud mailbox, use <strong>Other (IMAP/SMTP)</strong> below: go Back, choose &quot;Other (IMAP/SMTP)&quot;, then enter your iCloud email and an <strong>app-specific password</strong> (Apple ID → Sign-In and Security → App-Specific Passwords).
                            </p>
                            <button
                                onClick={() => { setProvider('imap'); setError(''); }}
                                className="w-full py-3 rounded-xl border-2 border-gray-300 text-gray-700 font-medium hover:bg-gray-50"
                            >
                                Set up with IMAP instead
                            </button>
                        </div>
                    )}
                    {currentStep === 2 && provider && provider !== 'imap' && provider !== 'apple' && (
                        <div className="space-y-6">
                            <h3 className="text-lg font-semibold text-gray-900">Sign in with {PROVIDER_OPTIONS.find(p => p.id === provider)?.name}</h3>
                            <p className="text-sm text-gray-600">
                                A browser window was opened. Sign in there and authorize VAF. When done, click &quot;I&apos;ve completed sign-in&quot; below to refresh, then Next.
                            </p>
                            <a
                                href={authUrl}
                                target="_blank"
                                rel="noopener noreferrer"
                                className="inline-flex items-center gap-2 px-4 py-2 rounded-xl bg-gray-900 text-white text-sm font-medium hover:bg-gray-800 dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-white dark:shadow-none"
                            >
                                <ExternalLink className="w-4 h-4" />
                                Open sign-in page
                            </a>
                            <button
                                onClick={() => fetchAccounts()}
                                className="block w-full py-2 text-sm text-gray-600 hover:text-gray-900 border border-gray-200 rounded-lg"
                            >
                                I&apos;ve completed sign-in — refresh list
                            </button>
                        </div>
                    )}

                    {currentStep === 2 && provider === 'imap' && (
                        <div className="space-y-6">
                            <h3 className="text-lg font-semibold text-gray-900">Enter credentials</h3>
                            <p className="text-sm text-gray-600 bg-gray-50 p-3 rounded-lg border border-gray-200">
                                For providers that don’t support “Sign in with Google” (or if you prefer not to use it), use your email and password here. For Gmail, Google recommends using <strong>“Google (Gmail)”</strong> in the previous step instead of IMAP. If you use IMAP with 2FA enabled, you’ll need an app password. Create one at <a href="https://myaccount.google.com/apppasswords" target="_blank" rel="noopener noreferrer" className="text-blue-600 hover:underline">myaccount.google.com/apppasswords</a>.
                            </p>
                            {/^(outlook|hotmail|live|msn|outlook\.de|office365)\.(com|de)$/i.test((imapEmail.split('@')[1] || '').toLowerCase()) && (
                                <p className="text-sm text-amber-800 bg-amber-50 p-3 rounded-lg border border-amber-200">
                                    <strong>Microsoft/Outlook:</strong> Outlook.com no longer supports IMAP with password (Microsoft retired this in 2024). Use <strong>Sign in with Microsoft</strong> in the previous step instead; an admin must configure the OAuth client first (expand &quot;For admins&quot; in the email wizard).
                                </p>
                            )}
                            <div className="space-y-3">
                                <div>
                                    <label className="block text-sm font-medium text-gray-700 mb-1">Email</label>
                                    <input
                                        type="email"
                                        value={imapEmail}
                                        onChange={e => { setImapEmail(e.target.value); setTestResult('idle'); }}
                                        placeholder="you@gmail.com"
                                        className="w-full px-4 py-3 rounded-xl border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400"
                                    />
                                </div>
                                <div>
                                    <label className="block text-sm font-medium text-gray-700 mb-1">Password or app password</label>
                                    <input
                                        type="password"
                                        value={imapPassword}
                                        onChange={e => { setImapPassword(e.target.value); setTestResult('idle'); }}
                                        placeholder="••••••••"
                                        className="w-full px-4 py-3 rounded-xl border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400"
                                    />
                                </div>
                                {testResult === 'ok' && (
                                    <div className="p-3 rounded-lg bg-green-50 border border-green-200 flex items-center gap-2 text-green-800 text-sm">
                                        <CheckCircle2 className="w-4 h-4 shrink-0" />
                                        Connection successful. Click Next below to add the account.
                                    </div>
                                )}
                                {testResult === 'fail' && (
                                    <div className="p-3 rounded-lg bg-red-50 border border-red-200 text-sm">
                                        <p className="text-red-700">{testError}</p>
                                        {testHint && <p className="text-red-600 mt-1">{testHint}</p>}
                                    </div>
                                )}
                                <details className="text-sm">
                                    <summary className="cursor-pointer text-gray-600 hover:text-gray-900">Advanced (optional)</summary>
                                    <div className="mt-2 grid grid-cols-2 gap-2">
                                        <input value={imapHost} onChange={e => setImapHost(e.target.value)} placeholder="IMAP host" className="px-3 py-2 border rounded-lg" />
                                        <input value={imapPort} onChange={e => setImapPort(e.target.value)} placeholder="IMAP port" className="px-3 py-2 border rounded-lg" />
                                        <input value={smtpHost} onChange={e => setSmtpHost(e.target.value)} placeholder="SMTP host" className="px-3 py-2 border rounded-lg" />
                                        <input value={smtpPort} onChange={e => setSmtpPort(e.target.value)} placeholder="SMTP port" className="px-3 py-2 border rounded-lg" />
                                    </div>
                                </details>
                            </div>
                            <div className="flex flex-wrap gap-2">
                                <button
                                    onClick={handleTestConnection}
                                    disabled={loading || testResult === 'testing' || !imapEmail.trim() || !imapPassword.trim()}
                                    className="px-4 py-2 rounded-xl border border-gray-300 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
                                >
                                    {testResult === 'testing' ? <Loader2 className="w-4 h-4 animate-spin inline" /> : 'Test connection'}
                                </button>
                            </div>
                        </div>
                    )}

                    {/* Step 3: Complete / Manage */}
                    {currentStep === 3 && (
                        <div className="space-y-6">
                            {accounts.length === 0 ? (
                                <p className="text-gray-500 text-sm">No email accounts connected yet. Use Back to add one.</p>
                            ) : (
                                <ul className="space-y-2">
                                    {accounts.map((a) => {
                                        const id = a.account_id || a.email;
                                        const lastVerified = formatLastVerified((a as any).last_verified_at);
                                        return (
                                            <li key={id} className="p-3 rounded-xl border border-gray-200 bg-gray-50 space-y-2">
                                                <div className="flex items-center justify-between">
                                                    <div>
                                                        <span className="font-medium text-gray-900">{a.email || a.account_id}</span>
                                                        <span className="ml-2 text-xs text-gray-500">{a.provider}</span>
                                                    </div>
                                                    <div className="flex items-center gap-2">
                                                        {(a.provider === 'imap' || a.provider === 'gmail' || a.provider === 'microsoft') && (
                                                            <button
                                                                onClick={() => handleVerifyAccount(id)}
                                                                disabled={verifyLoading === id}
                                                                className="text-sm text-gray-600 hover:text-gray-900 disabled:opacity-50"
                                                            >
                                                                {verifyLoading === id ? <Loader2 className="w-4 h-4 animate-spin inline" /> : 'Verify'}
                                                            </button>
                                                        )}
                                                        <button onClick={() => handleRemoveAccount(id)} className="text-sm text-red-600 hover:text-red-700">Remove</button>
                                                    </div>
                                                </div>
                                                {lastVerified != null && <p className="text-xs text-gray-500">Last verified: {lastVerified}</p>}
                                            </li>
                                        );
                                    })}
                                </ul>
                            )}
                            <button
                                onClick={() => { setCurrentStep(1); setError(''); setProvider(''); setTestResult('idle'); }}
                                className="w-full py-2 rounded-xl border border-gray-200 text-sm font-medium text-gray-700 hover:bg-gray-50"
                            >
                                Add another account
                            </button>
                        </div>
                    )}
                </div>

                {/* Footer */}
                <div className="flex items-center justify-between p-6 border-t border-gray-200 bg-gray-50 max-md:p-4 max-md:shrink-0">
                    <button
                        onClick={prevStep}
                        disabled={currentStep === 0}
                        className={cn(
                            "flex items-center gap-2 px-4 py-2 rounded-lg transition-colors",
                            currentStep === 0 ? "text-gray-300 cursor-not-allowed" : "text-gray-600 hover:bg-gray-200"
                        )}
                    >
                        <ChevronLeft className="w-4 h-4" />
                        Back
                    </button>

                    {currentStep < STEPS.length - 1 ? (
                        <button
                            onClick={handleNextClick}
                            disabled={isNextDisabled}
                            className={cn(
                                "flex items-center gap-2 px-6 py-2 rounded-lg font-medium transition-colors",
                                !isNextDisabled ? "bg-gray-900 hover:bg-gray-800 text-white dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-white dark:shadow-none" : "bg-gray-200 text-gray-400 cursor-not-allowed"
                            )}
                        >
                            {currentStep === 2 && provider === 'imap' && loading && <Loader2 className="w-4 h-4 animate-spin" />}
                            Next
                            <ChevronRight className="w-4 h-4" />
                        </button>
                    ) : (
                        <button
                            onClick={handleFinish}
                            className="flex items-center gap-2 px-6 py-2 rounded-lg font-medium bg-green-500 hover:bg-green-600 text-white transition-colors"
                        >
                            <CheckCircle2 className="w-4 h-4" />
                            Finish
                        </button>
                    )}
                </div>
            </div>
        </div>
    );
}
