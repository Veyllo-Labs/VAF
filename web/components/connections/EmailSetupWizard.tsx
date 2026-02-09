'use client';

import React, { useState, useEffect } from 'react';
import {
    X, ChevronRight, ChevronLeft, Mail, Loader2, AlertCircle,
    ExternalLink, CheckCircle2, Shield
} from 'lucide-react';
import { cn } from '@/lib/utils';

const api = (path: string) => path.startsWith('/') ? path : `/${path}`;

interface EmailSetupWizardProps {
    isOpen: boolean;
    onClose: () => void;
    onComplete?: () => void;
    existingAccounts?: Array<{ account_id: string; email: string; provider: string; enabled?: boolean; last_verified_at?: string }>;
}

const STEPS = [
    { id: 'intro', title: 'Email Setup', subtitle: 'Connect your mailbox' },
    { id: 'choose', title: 'Choose Provider', subtitle: 'How do you want to connect?' },
    { id: 'connect', title: 'Connect', subtitle: 'Sign in or enter credentials' },
    { id: 'complete', title: 'Complete', subtitle: 'Manage your accounts' },
];

const PROVIDERS = [
    { id: 'gmail', name: 'Google (Gmail)', icon: 'G', desc: 'Sign in with your Google account', oauth: true },
    { id: 'microsoft', name: 'Microsoft (Outlook)', icon: 'M', desc: 'Sign in with your Microsoft account', oauth: true },
    { id: 'apple', name: 'Apple (iCloud Mail)', icon: 'A', desc: 'Sign in with Apple', oauth: true },
    { id: 'imap', name: 'Other (IMAP/SMTP)', icon: 'O', desc: 'Email and app password', oauth: false },
];

export default function EmailSetupWizard({ isOpen, onClose, onComplete, existingAccounts = [] }: EmailSetupWizardProps) {
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

    useEffect(() => {
        if (isOpen) {
            fetchAccounts().then(() => {
                const params = new URLSearchParams(typeof window !== 'undefined' ? window.location.search : '');
                if (params.get('email_oauth') === 'success') setCurrentStep(3);
            });
        }
    }, [isOpen]);

    useEffect(() => {
        if (isOpen && accounts.length > 0 && currentStep === 0) setCurrentStep(3);
    }, [isOpen, accounts.length, currentStep]);

    const handleChooseProvider = (id: string) => {
        setProvider(id);
        setError('');
        if (id === 'imap') {
            setCurrentStep(2);
            return;
        }
        setLoading(true);
        fetch(api(`api/email/oauth/start?provider=${id}`), { credentials: 'include' })
            .then(r => r.json())
            .then(data => {
                setAuthUrl(data.authorization_url || '');
                setCurrentStep(2);
                if (data.authorization_url && typeof window !== 'undefined') {
                    window.open(data.authorization_url, '_blank', 'noopener,noreferrer');
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

    const isNextDisabled = (currentStep === 1 && !provider) || (currentStep === 2 && provider === 'imap' && (testResult !== 'ok' || loading));

    return (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 backdrop-blur-sm">
            <div className="bg-white rounded-2xl shadow-2xl w-full max-w-2xl mx-4 overflow-hidden border border-gray-200">
                {/* Header */}
                <div className="flex items-center justify-between p-6 border-b border-gray-200 bg-gray-50">
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
                                    idx === currentStep ? "bg-gray-900 text-white" :
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
                <div className="p-6 min-h-[350px]">
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
                                {PROVIDERS.map((p) => (
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
                        </div>
                    )}

                    {/* Step 2: Connect (OAuth or IMAP) */}
                    {currentStep === 2 && provider && provider !== 'imap' && (
                        <div className="space-y-6">
                            <h3 className="text-lg font-semibold text-gray-900">Sign in with {PROVIDERS.find(p => p.id === provider)?.name}</h3>
                            <p className="text-sm text-gray-600">
                                A browser window was opened. Sign in there and authorize VAF. When done, click &quot;I&apos;ve completed sign-in&quot; below to refresh, then Next.
                            </p>
                            <a
                                href={authUrl}
                                target="_blank"
                                rel="noopener noreferrer"
                                className="inline-flex items-center gap-2 px-4 py-2 rounded-xl bg-gray-900 text-white text-sm font-medium hover:bg-gray-800"
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
                                For providers that don’t support “Sign in with Google” (or if you prefer not to use it), use your email and password here. For Gmail, Google recommends using <strong>“Google (Gmail)”</strong> in the previous step instead of IMAP. If you use IMAP with 2FA enabled, you’ll need an app password (Google Account → Security → 2-Step Verification → App passwords).
                            </p>
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
                                                        {a.provider === 'imap' && (
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
                <div className="flex items-center justify-between p-6 border-t border-gray-200 bg-gray-50">
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
                                !isNextDisabled ? "bg-gray-900 hover:bg-gray-800 text-white" : "bg-gray-200 text-gray-400 cursor-not-allowed"
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
