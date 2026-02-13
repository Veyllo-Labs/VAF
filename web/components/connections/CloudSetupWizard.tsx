'use client';

import React, { useState, useEffect } from 'react';
import {
    X, ChevronRight, ChevronLeft, Cloud, Loader2, AlertCircle,
    ExternalLink, CheckCircle2, Shield, FolderSync, HardDrive,
    RefreshCw, Trash2
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { displayOAuthValue, BUILTIN_GOOGLE_CLIENT_ID } from '@/lib/oauth_defaults';

const api = (path: string) => path.startsWith('/') ? path : `/${path}`;

interface CloudSetupWizardProps {
    isOpen: boolean;
    onClose: () => void;
    onComplete?: () => void;
    initialProvider?: string;
    currentUser?: { role?: string };
}

const STEPS = [
    { id: 'intro', title: 'Cloud Storage', subtitle: 'Sync files with your cloud' },
    { id: 'connect', title: 'Connect', subtitle: 'Sign in or enter credentials' },
    { id: 'complete', title: 'Complete', subtitle: 'Manage your accounts' },
];

interface ProviderOption {
    id: string;
    name: string;
    icon: React.ElementType;
    desc: string;
    authType: 'oauth' | 'webdav' | 'local';
    iconColor: string;
}

const PROVIDER_OPTIONS: ProviderOption[] = [
    { id: 'google_drive', name: 'Google Drive', icon: Cloud, desc: 'Sign in with Google to sync files', authType: 'oauth', iconColor: 'bg-yellow-500' },
    { id: 'onedrive', name: 'Microsoft OneDrive', icon: Cloud, desc: 'Sign in with Microsoft to sync files', authType: 'oauth', iconColor: 'bg-blue-500' },
    { id: 'dropbox', name: 'Dropbox', icon: FolderSync, desc: 'Sign in with Dropbox to sync files', authType: 'oauth', iconColor: 'bg-blue-600' },
    { id: 'nextcloud', name: 'Nextcloud', icon: HardDrive, desc: 'Connect via WebDAV with app password', authType: 'webdav', iconColor: 'bg-cyan-600' },
    { id: 'icloud', name: 'Apple iCloud', icon: Cloud, desc: 'Sync via local iCloud Drive (macOS only)', authType: 'local', iconColor: 'bg-sky-400' },
];

interface CloudAccount {
    account_id: string;
    provider: string;
    display_name?: string;
    sync_enabled?: boolean;
    last_synced_at?: number | null;
}

export default function CloudSetupWizard({ isOpen, onClose, onComplete, initialProvider, currentUser }: CloudSetupWizardProps) {
    const [currentStep, setCurrentStep] = useState(0);
    const [provider, setProvider] = useState<string>('');
    const [authUrl, setAuthUrl] = useState('');
    const [accounts, setAccounts] = useState<CloudAccount[]>([]);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState('');
    const [syncLoading, setSyncLoading] = useState<string | null>(null);

    // WebDAV (Nextcloud) fields
    const [webdavUrl, setWebdavUrl] = useState('');
    const [webdavUser, setWebdavUser] = useState('');
    const [webdavPass, setWebdavPass] = useState('');

    // OAuth status
    const [oauthStatus, setOauthStatus] = useState<Record<string, boolean>>({});

    // Admin OAuth config
    const [oauthGoogleId, setOauthGoogleId] = useState('');
    const [oauthGoogleSecret, setOauthGoogleSecret] = useState('');
    const [oauthMicrosoftId, setOauthMicrosoftId] = useState('');
    const [oauthMicrosoftSecret, setOauthMicrosoftSecret] = useState('');
    const [oauthDropboxId, setOauthDropboxId] = useState('');
    const [oauthDropboxSecret, setOauthDropboxSecret] = useState('');
    const [adminSaveStatus, setAdminSaveStatus] = useState<'idle' | 'saving' | 'ok' | 'fail'>('idle');

    const fetchAccounts = async () => {
        try {
            const res = await fetch(api('api/cloud/accounts'), { credentials: 'include' });
            if (res.ok) {
                const data = await res.json();
                setAccounts(data.accounts || []);
            }
        } catch { setAccounts([]); }
    };

    const fetchOAuthStatus = async () => {
        try {
            const res = await fetch(api('api/cloud/providers'), { credentials: 'include' });
            if (res.ok) {
                const data = await res.json();
                const status: Record<string, boolean> = {};
                for (const p of (data.providers || [])) {
                    status[p.id] = p.oauth_configured;
                }
                setOauthStatus(status);
            }
        } catch { /* ignore */ }
    };

    const loadAdminConfig = async () => {
        try {
            const res = await fetch(api('api/config'), { credentials: 'include' });
            if (res.ok) {
                const c = await res.json();
                setOauthGoogleId(displayOAuthValue(c.cloud_oauth_google_client_id, BUILTIN_GOOGLE_CLIENT_ID));
                setOauthGoogleSecret((c.cloud_oauth_google_client_secret ?? '').trim());
                setOauthMicrosoftId((c.cloud_oauth_microsoft_client_id ?? '').trim());
                setOauthMicrosoftSecret((c.cloud_oauth_microsoft_client_secret ?? '').trim());
                setOauthDropboxId((c.cloud_oauth_dropbox_client_id ?? '').trim());
                setOauthDropboxSecret((c.cloud_oauth_dropbox_client_secret ?? '').trim());
            }
        } catch { /* ignore */ }
    };

    const initialProviderHandled = React.useRef(false);

    useEffect(() => {
        if (isOpen) {
            // Reset state on open
            setAuthUrl('');
            setError('');
            setLoading(false);
            initialProviderHandled.current = false;

            const params = typeof window !== 'undefined' ? new URLSearchParams(window.location.search) : null;
            const fromOAuthSuccess = params?.get('cloud_oauth') === 'success';

            fetchOAuthStatus();
            fetchAccounts().then(() => {
                if (fromOAuthSuccess) setCurrentStep(2);
            });
            if (currentUser?.role === 'admin') loadAdminConfig();
            if (initialProvider) {
                setProvider(initialProvider);
                // If returning from OAuth success, show completion step (2); else show Connect step (1)
                setCurrentStep(fromOAuthSuccess ? 2 : 1);
            } else {
                setProvider('');
                setCurrentStep(fromOAuthSuccess ? 2 : 0);
            }
        }
    }, [isOpen, currentUser?.role, initialProvider]);

    // When opened with a specific provider, skip intro + choose steps and go straight to connect
    // EXCEPT when returning from OAuth success – then we must NOT start OAuth again (would cause a loop)
    useEffect(() => {
        if (!isOpen || !initialProvider || initialProviderHandled.current) return;
        const params = typeof window !== 'undefined' ? new URLSearchParams(window.location.search) : null;
        if (params?.get('cloud_oauth') === 'success') return; // Coming back from callback: show completion, don't re-trigger OAuth
        initialProviderHandled.current = true;
        handleChooseProvider(initialProvider);
    }, [isOpen, initialProvider]);

    useEffect(() => {
        if (isOpen && accounts.length > 0 && currentStep === 0) setCurrentStep(2);
    }, [isOpen, accounts.length, currentStep]);

    const handleChooseProvider = (id: string) => {
        setProvider(id);
        setError('');
        const opt = PROVIDER_OPTIONS.find(p => p.id === id);
        if (!opt) return;

        if (opt.authType === 'webdav' || opt.authType === 'local') {
            setCurrentStep(1);
            return;
        }

        // OAuth flow – pass redirect_base so post-OAuth redirect matches the host (localhost vs 127.0.0.1)
        setLoading(true);
        const redirectBase = typeof window !== 'undefined' ? encodeURIComponent(window.location.origin) : '';
        const startUrl = redirectBase
            ? api(`api/cloud/oauth/start?provider=${id}&redirect_base=${redirectBase}`)
            : api(`api/cloud/oauth/start?provider=${id}`);
        fetch(startUrl, { credentials: 'include' })
            .then(r => {
                if (!r.ok) throw new Error('Could not start sign-in. Check OAuth settings.');
                return r.json();
            })
            .then(data => {
                const url = data.authorization_url || '';
                setAuthUrl(url);
                setCurrentStep(1);
                if (url && typeof window !== 'undefined') {
                    // Navigate in same tab so redirect back shares session/cookies (new tab can lose session)
                    window.location.href = url;
                } else if (!url) {
                    setError('No sign-in URL returned. Check OAuth client ID in Settings.');
                }
            })
            .catch(e => {
                setError(e?.message || 'Failed to start sign-in');
                setCurrentStep(1);
            })
            .finally(() => setLoading(false));
    };

    const handleConnectWebdav = async () => {
        if (!webdavUrl.trim() || !webdavUser.trim() || !webdavPass.trim()) {
            setError('All fields are required.');
            return;
        }
        setLoading(true);
        setError('');
        try {
            const res = await fetch(api('api/cloud/accounts/webdav'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({
                    url: webdavUrl.trim(),
                    username: webdavUser.trim(),
                    password: webdavPass,
                }),
            });
            if (!res.ok) {
                const err = await res.json();
                throw new Error(err.detail || 'Failed to connect');
            }
            await fetchAccounts();
            setWebdavUrl('');
            setWebdavUser('');
            setWebdavPass('');
            setCurrentStep(2);
        } catch (e) {
            setError(e instanceof Error ? e.message : 'Connection failed');
        } finally { setLoading(false); }
    };

    const handleConnectICloud = async () => {
        setLoading(true);
        setError('');
        try {
            const res = await fetch(api('api/cloud/accounts/webdav'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({ url: 'local://icloud', username: 'local', password: '' }),
            });
            if (!res.ok) {
                const err = await res.json();
                throw new Error(err.detail || 'iCloud Drive not available. Are you on macOS with iCloud Drive enabled?');
            }
            await fetchAccounts();
            setCurrentStep(2);
        } catch (e) {
            setError(e instanceof Error ? e.message : 'Failed to connect iCloud');
        } finally { setLoading(false); }
    };

    const handleRemoveAccount = async (accountId: string) => {
        try {
            const res = await fetch(api(`api/cloud/accounts/${encodeURIComponent(accountId)}`), {
                method: 'DELETE', credentials: 'include',
            });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err.detail || `Failed to remove account (${res.status})`);
            }
            await fetchAccounts();
        } catch (e) {
            setError(e instanceof Error ? e.message : 'Failed to remove account');
        }
    };

    const handleTriggerSync = async (accountId: string) => {
        setSyncLoading(accountId);
        try {
            const res = await fetch(api(`api/cloud/accounts/${encodeURIComponent(accountId)}/sync`), {
                method: 'POST', credentials: 'include',
            });
            if (res.ok) {
                await fetchAccounts();
            } else {
                const err = await res.json().catch(() => ({}));
                setError(err.detail || 'Sync failed');
            }
        } catch { setError('Sync request failed'); }
        finally { setSyncLoading(null); }
    };

    const handleSaveAdminOAuth = async () => {
        setAdminSaveStatus('saving');
        try {
            const res = await fetch(api('api/config'), {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({
                    cloud_oauth_google_client_id: oauthGoogleId.trim(),
                    cloud_oauth_google_client_secret: oauthGoogleSecret.trim(),
                    cloud_oauth_microsoft_client_id: oauthMicrosoftId.trim(),
                    cloud_oauth_microsoft_client_secret: oauthMicrosoftSecret.trim(),
                    cloud_oauth_dropbox_client_id: oauthDropboxId.trim(),
                    cloud_oauth_dropbox_client_secret: oauthDropboxSecret.trim(),
                }),
            });
            if (!res.ok) throw new Error('Failed to save');
            setAdminSaveStatus('ok');
            fetchOAuthStatus();
        } catch {
            setAdminSaveStatus('fail');
        }
    };

    const handleFinish = () => {
        onComplete?.();
        onClose();
    };

    const prevStep = () => {
        if (currentStep === 2) { setCurrentStep(1); setProvider(''); return; }
        if (currentStep > 0) { if (currentStep === 1) setProvider(''); setCurrentStep(currentStep - 1); }
    };

    const handleNextClick = () => {
        if (currentStep === 0) { setCurrentStep(1); return; }
        if (currentStep === 1 && provider === 'nextcloud') { handleConnectWebdav(); return; }
        if (currentStep === 1 && provider === 'icloud') { handleConnectICloud(); return; }
        if (currentStep < STEPS.length - 1) setCurrentStep(currentStep + 1);
    };

    const formatAgo = (ts?: number | null) => {
        if (!ts) return 'Never';
        const diff = Math.floor(Date.now() / 1000 - ts);
        if (diff < 60) return 'Just now';
        if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
        if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
        return new Date(ts * 1000).toLocaleDateString();
    };

    if (!isOpen) return null;

    const providerOpt = PROVIDER_OPTIONS.find(p => p.id === provider);
    const isNextDisabled =
        (currentStep === 1 && !provider) ||
        (currentStep === 1 && provider === 'nextcloud' && (!webdavUrl.trim() || !webdavUser.trim() || !webdavPass.trim())) ||
        loading;

    return (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 backdrop-blur-sm">
            <div className="bg-white rounded-2xl shadow-2xl w-full max-w-2xl mx-4 overflow-hidden border border-gray-200">
                {/* Header */}
                <div className="flex items-center justify-between p-6 border-b border-gray-200 bg-gray-50">
                    <div className="flex items-center gap-3">
                        <div className="w-10 h-10 rounded-xl bg-gray-900 flex items-center justify-center">
                            <Cloud className="w-5 h-5 text-white" />
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

                {/* Progress Steps */}
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
                                <div className="w-20 h-20 mx-auto rounded-2xl bg-gray-900 flex items-center justify-center mb-4">
                                    <Cloud className="w-10 h-10 text-white" />
                                </div>
                                <h3 className="text-2xl font-bold text-gray-900 mb-2">Connect Cloud Storage</h3>
                                <p className="text-gray-500 max-w-md mx-auto">
                                    Sync documents, research, and exports with your cloud storage. VAF creates
                                    a &quot;VAF Sync&quot; folder in your cloud — files you place there appear locally, and vice versa.
                                </p>
                            </div>
                            <div className="grid grid-cols-2 gap-4">
                                <div className="p-4 rounded-xl bg-gray-50 border border-gray-200">
                                    <Shield className="w-8 h-8 text-green-600 mb-2" />
                                    <h4 className="font-semibold text-gray-900">Secure</h4>
                                    <p className="text-sm text-gray-500">Tokens in OS keyring or encrypted storage</p>
                                </div>
                                <div className="p-4 rounded-xl bg-gray-50 border border-gray-200">
                                    <FolderSync className="w-8 h-8 text-gray-700 mb-2" />
                                    <h4 className="font-semibold text-gray-900">Bi-directional</h4>
                                    <p className="text-sm text-gray-500">Upload and download sync automatically</p>
                                </div>
                            </div>
                        </div>
                    )}

                    {/* Step 1: Connect */}
                    {currentStep === 1 && providerOpt?.authType === 'oauth' && (
                        <div className="space-y-6">
                            <h3 className="text-lg font-semibold text-gray-900">Sign in with {providerOpt.name}</h3>
                            {authUrl ? (
                                <>
                                    <p className="text-sm text-gray-600">
                                        A browser window was opened. Sign in and authorize VAF to access your files.
                                        When done, click the button below to refresh.
                                    </p>
                                    <a href={authUrl} target="_blank" rel="noopener noreferrer"
                                        className="inline-flex items-center gap-2 px-4 py-2 rounded-xl bg-gray-900 text-white text-sm font-medium hover:bg-gray-800">
                                        <ExternalLink className="w-4 h-4" />
                                        Open sign-in page
                                    </a>
                                    <button onClick={() => fetchAccounts()}
                                        className="block w-full py-2 text-sm text-gray-600 hover:text-gray-900 border border-gray-200 rounded-lg">
                                        I&apos;ve completed sign-in — refresh list
                                    </button>
                                </>
                            ) : currentUser?.role === 'admin' ? (
                                <>
                                    <p className="text-sm text-gray-600">
                                        OAuth is not configured for {providerOpt.name}. Enter Client ID and Client Secret below, then save and retry.
                                    </p>
                                    {provider === 'google_drive' && (
                                        <a href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noopener noreferrer"
                                            className="inline-flex items-center gap-2 text-sm text-blue-600 hover:text-blue-800">
                                            <ExternalLink className="w-4 h-4" />
                                            Where to get Google OAuth credentials
                                        </a>
                                    )}
                                    {provider === 'onedrive' && (
                                        <a href="https://portal.azure.com/#blade/Microsoft_AAD_RegisteredApps/ApplicationsListBlade" target="_blank" rel="noopener noreferrer"
                                            className="inline-flex items-center gap-2 text-sm text-blue-600 hover:text-blue-800">
                                            <ExternalLink className="w-4 h-4" />
                                            Where to get Microsoft OAuth credentials
                                        </a>
                                    )}
                                    {provider === 'dropbox' && (
                                        <a href="https://www.dropbox.com/developers/apps" target="_blank" rel="noopener noreferrer"
                                            className="inline-flex items-center gap-2 text-sm text-blue-600 hover:text-blue-800">
                                            <ExternalLink className="w-4 h-4" />
                                            Where to get Dropbox App credentials
                                        </a>
                                    )}
                                    <div className="space-y-3">
                                        {provider === 'google_drive' && (
                                            <>
                                                <div>
                                                    <label className="block text-sm font-medium text-gray-700 mb-1">Google Client ID</label>
                                                    <input type="text" value={oauthGoogleId} onChange={e => { setOauthGoogleId(e.target.value); setAdminSaveStatus('idle'); }}
                                                        placeholder="xxx.apps.googleusercontent.com"
                                                        className="w-full px-4 py-3 rounded-xl border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400" />
                                                </div>
                                                <div>
                                                    <label className="block text-sm font-medium text-gray-700 mb-1">Google Client Secret</label>
                                                    <input type="password" value={oauthGoogleSecret} onChange={e => { setOauthGoogleSecret(e.target.value); setAdminSaveStatus('idle'); }}
                                                        className="w-full px-4 py-3 rounded-xl border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400" />
                                                </div>
                                            </>
                                        )}
                                        {provider === 'onedrive' && (
                                            <>
                                                <div>
                                                    <label className="block text-sm font-medium text-gray-700 mb-1">Microsoft Client ID</label>
                                                    <input type="text" value={oauthMicrosoftId} onChange={e => { setOauthMicrosoftId(e.target.value); setAdminSaveStatus('idle'); }}
                                                        className="w-full px-4 py-3 rounded-xl border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400" />
                                                </div>
                                                <div>
                                                    <label className="block text-sm font-medium text-gray-700 mb-1">Microsoft Client Secret</label>
                                                    <input type="password" value={oauthMicrosoftSecret} onChange={e => { setOauthMicrosoftSecret(e.target.value); setAdminSaveStatus('idle'); }}
                                                        className="w-full px-4 py-3 rounded-xl border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400" />
                                                </div>
                                            </>
                                        )}
                                        {provider === 'dropbox' && (
                                            <>
                                                <div>
                                                    <label className="block text-sm font-medium text-gray-700 mb-1">Dropbox App Key</label>
                                                    <input type="text" value={oauthDropboxId} onChange={e => { setOauthDropboxId(e.target.value); setAdminSaveStatus('idle'); }}
                                                        className="w-full px-4 py-3 rounded-xl border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400" />
                                                </div>
                                                <div>
                                                    <label className="block text-sm font-medium text-gray-700 mb-1">Dropbox App Secret</label>
                                                    <input type="password" value={oauthDropboxSecret} onChange={e => { setOauthDropboxSecret(e.target.value); setAdminSaveStatus('idle'); }}
                                                        className="w-full px-4 py-3 rounded-xl border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400" />
                                                </div>
                                            </>
                                        )}
                                    </div>
                                    <div className="flex items-center gap-2 flex-wrap">
                                        <button onClick={handleSaveAdminOAuth} disabled={adminSaveStatus === 'saving'}
                                            className="px-4 py-2 rounded-xl text-sm font-medium bg-gray-900 text-white hover:bg-gray-800 disabled:opacity-50">
                                            {adminSaveStatus === 'saving' ? <Loader2 className="w-4 h-4 animate-spin inline" /> : 'Save'}
                                        </button>
                                        {adminSaveStatus === 'ok' && (
                                            <button onClick={() => { setError(''); handleChooseProvider(provider); }}
                                                className="px-4 py-2 rounded-xl text-sm font-medium border border-gray-300 text-gray-700 hover:bg-gray-50">
                                                Retry sign-in
                                            </button>
                                        )}
                                        {adminSaveStatus === 'ok' && <span className="text-sm text-green-600">Saved.</span>}
                                        {adminSaveStatus === 'fail' && <span className="text-sm text-red-600">Failed to save.</span>}
                                    </div>
                                </>
                            ) : (
                                <div className="flex flex-col gap-2 text-sm text-amber-700 bg-amber-50 border border-amber-200 rounded-xl p-4">
                                    <div className="flex items-center gap-2">
                                        <AlertCircle className="w-5 h-5 flex-shrink-0" />
                                        <span className="font-medium">OAuth not configured</span>
                                    </div>
                                    <p>
                                        Please ask your administrator to configure OAuth for {providerOpt.name}.
                                    </p>
                                </div>
                            )}
                        </div>
                    )}

                    {currentStep === 1 && provider === 'nextcloud' && (
                        <div className="space-y-6">
                            <h3 className="text-lg font-semibold text-gray-900">Connect Nextcloud</h3>
                            <p className="text-sm text-gray-600 bg-gray-50 p-3 rounded-lg border border-gray-200">
                                Enter your Nextcloud server URL and an <strong>app password</strong>.
                                Create one in Nextcloud under Settings &rarr; Security &rarr; Devices &amp; sessions.
                            </p>
                            <a href="https://docs.nextcloud.com/server/stable/user_manual/en/user_2fa.html#device-tokens" target="_blank" rel="noopener noreferrer"
                                className="inline-flex items-center gap-2 text-sm text-blue-600 hover:text-blue-800">
                                <ExternalLink className="w-4 h-4" />
                                Where to create an app password
                            </a>
                            <div className="space-y-3">
                                <div>
                                    <label className="block text-sm font-medium text-gray-700 mb-1">Server URL</label>
                                    <input type="url" value={webdavUrl} onChange={e => setWebdavUrl(e.target.value)}
                                        placeholder="https://cloud.example.com"
                                        className="w-full px-4 py-3 rounded-xl border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400" />
                                </div>
                                <div>
                                    <label className="block text-sm font-medium text-gray-700 mb-1">Username</label>
                                    <input type="text" value={webdavUser} onChange={e => setWebdavUser(e.target.value)}
                                        placeholder="your-username"
                                        className="w-full px-4 py-3 rounded-xl border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400" />
                                </div>
                                <div>
                                    <label className="block text-sm font-medium text-gray-700 mb-1">App Password</label>
                                    <input type="password" value={webdavPass} onChange={e => setWebdavPass(e.target.value)}
                                        placeholder="••••••••"
                                        className="w-full px-4 py-3 rounded-xl border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400" />
                                </div>
                            </div>
                        </div>
                    )}

                    {currentStep === 1 && provider === 'icloud' && (
                        <div className="space-y-6">
                            <h3 className="text-lg font-semibold text-gray-900">Apple iCloud Drive</h3>
                            <p className="text-sm text-gray-600 bg-amber-50 p-4 rounded-xl border border-amber-200">
                                iCloud Drive sync works by reading and writing directly to your local iCloud Drive folder on macOS.
                                Apple handles the cloud sync automatically. This only works on <strong>macOS</strong> with iCloud Drive enabled.
                            </p>
                            <p className="text-sm text-gray-500">
                                A &quot;VAF Sync&quot; folder will be created in your iCloud Drive.
                            </p>
                        </div>
                    )}

                    {currentStep === 1 && !provider && (
                        <div className="space-y-4">
                            <p className="text-sm text-gray-600">Choose a provider to connect.</p>
                            <div className="flex flex-wrap gap-2">
                                {PROVIDER_OPTIONS.map((p) => {
                                    const Icon = p.icon;
                                    return (
                                        <button
                                            key={p.id}
                                            onClick={() => handleChooseProvider(p.id)}
                                            disabled={loading}
                                            className={cn(
                                                "flex items-center gap-2 px-4 py-2 rounded-xl border text-sm font-medium transition-colors",
                                                "border-gray-200 hover:border-gray-300 hover:bg-gray-50",
                                                loading && "opacity-60 cursor-not-allowed"
                                            )}
                                        >
                                            <div className={cn("w-8 h-8 rounded-lg flex items-center justify-center text-white", p.iconColor)}>
                                                <Icon className="w-4 h-4" />
                                            </div>
                                            {p.name}
                                            {loading && provider === p.id && <Loader2 className="w-4 h-4 animate-spin" />}
                                        </button>
                                    );
                                })}
                            </div>
                        </div>
                    )}

                    {/* Step 2: Complete / Manage */}
                    {currentStep === 2 && (
                        <div className="space-y-6">
                            {accounts.length === 0 ? (
                                <p className="text-gray-500 text-sm">No cloud accounts connected yet. Use Back to add one.</p>
                            ) : (
                                <ul className="space-y-2">
                                    {accounts.map((a) => {
                                        const opt = PROVIDER_OPTIONS.find(p => p.id === a.provider);
                                        const Icon = opt?.icon || Cloud;
                                        return (
                                            <li key={a.account_id} className="p-4 rounded-xl border border-gray-200 bg-white shadow-sm">
                                                <div className="flex items-center justify-between">
                                                    <div className="flex items-center gap-3">
                                                        <div className={cn("w-10 h-10 rounded-lg flex items-center justify-center text-white", opt?.iconColor || "bg-gray-900")}>
                                                            <Icon className="w-5 h-5" />
                                                        </div>
                                                        <div>
                                                            <span className="font-medium text-gray-900">{a.display_name || a.account_id}</span>
                                                            <span className="ml-2 text-xs text-gray-500">{opt?.name || a.provider}</span>
                                                            <p className="text-xs text-gray-400">Last sync: {formatAgo(a.last_synced_at)}</p>
                                                        </div>
                                                    </div>
                                                    <div className="flex items-center gap-2">
                                                        <button onClick={() => handleTriggerSync(a.account_id)}
                                                            disabled={syncLoading === a.account_id}
                                                            className="p-2 hover:bg-gray-100 rounded-lg transition-colors"
                                                            title="Sync now">
                                                            {syncLoading === a.account_id
                                                                ? <Loader2 className="w-4 h-4 animate-spin text-gray-400" />
                                                                : <RefreshCw className="w-4 h-4 text-gray-500" />}
                                                        </button>
                                                        <button onClick={() => handleRemoveAccount(a.account_id)}
                                                            className="p-2 hover:bg-red-50 rounded-lg transition-colors group"
                                                            title="Disconnect">
                                                            <Trash2 className="w-4 h-4 text-gray-400 group-hover:text-red-500" />
                                                        </button>
                                                    </div>
                                                </div>
                                            </li>
                                        );
                                    })}
                                </ul>
                            )}
                            <button
                                onClick={() => { setCurrentStep(1); setError(''); setProvider(''); }}
                                className="w-full py-2 rounded-xl border border-gray-200 text-sm font-medium text-gray-700 hover:bg-gray-50">
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
                        )}>
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
                            )}>
                            {loading && <Loader2 className="w-4 h-4 animate-spin" />}
                            {currentStep === 1 && provider === 'nextcloud' ? 'Connect' :
                             currentStep === 1 && provider === 'icloud' ? 'Enable' : 'Next'}
                            <ChevronRight className="w-4 h-4" />
                        </button>
                    ) : (
                        <button
                            onClick={handleFinish}
                            className="flex items-center gap-2 px-6 py-2 rounded-lg font-medium bg-green-500 hover:bg-green-600 text-white transition-colors">
                            <CheckCircle2 className="w-4 h-4" />
                            Finish
                        </button>
                    )}
                </div>
            </div>
        </div>
    );
}
