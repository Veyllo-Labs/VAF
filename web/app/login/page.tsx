'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import React, { useState, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { motion, AnimatePresence } from 'framer-motion';
import {
    User, Lock, Eye, EyeOff, ArrowRight, ShieldCheck,
    Smartphone, CheckCircle, Check, Copy, Link2
} from 'lucide-react';
import SoulWizard from '@/components/SoulWizard';
import { cn } from '@/lib/utils';
import { CONNECTION_APPS, CATEGORIES, DiscordSetupWizard, TelegramSetupWizard, EmailSetupWizard } from '@/components/connections';
import type { DiscordConfig, TelegramConfig } from '@/components/connections';
import { getApiBase } from '@/lib/utils';
import { AgentAvatar } from '@/components/AgentAvatar';

// On the 2FA step, the agent "wakes up" (materialises + the eye opens) where the logo would be, then
// settles into the patient `waiting` loop. `lite` keeps it leak-safe (no border-radius morph).
function WakingAvatar() {
    const [mode, setMode] = useState<'waking' | 'waiting'>('waking');
    useEffect(() => {
        const t = setTimeout(() => setMode('waiting'), 1500);
        return () => clearTimeout(t);
    }, []);
    return (
        <div className="w-20 h-20 mx-auto mb-4 flex items-center justify-center">
            <div style={{ transform: 'scale(2)' }}><AgentAvatar mode={mode} lite /></div>
        </div>
    );
}

export default function LoginPage() {
    const router = useRouter();
    // Default to login; only show wizard when API explicitly says needs_setup: true (no admin yet)
    const [step, setStep] = useState<'login' | '2fa' | 'create_admin' | 'soul_wizard' | 'connections' | 'setup_2fa'>('login');
    const [username, setUsername] = useState('');
    const [password, setPassword] = useState('');
    const [confirmPassword, setConfirmPassword] = useState('');
    const [showPassword, setShowPassword] = useState(false);
    const [rememberMe, setRememberMe] = useState(false);
    const [isLoading, setIsLoading] = useState(false);
    const [checkingSetup, setCheckingSetup] = useState(true);
    const [tempToken, setTempToken] = useState<string | null>(null);
    const [twoFACode, setTwoFACode] = useState('');
    const [qrCodeBase64, setQrCodeBase64] = useState<string | null>(null);
    const [twoFASecret, setTwoFASecret] = useState<string | null>(null);
    const [secretCopied, setSecretCopied] = useState(false);
    const [loginError, setLoginError] = useState<string | null>(null);
    const [twoFAError, setTwoFAError] = useState<string | null>(null);
    const [bootstrapError, setBootstrapError] = useState<string | null>(null);
    const [createAdminSubStep, setCreateAdminSubStep] = useState<'username' | 'password'>('username');
    const [pendingSoul, setPendingSoul] = useState<string | null>(null);
    const [onboardingConfig, setOnboardingConfig] = useState<Record<string, unknown>>({});
    const [showDiscordWizard, setShowDiscordWizard] = useState(false);
    const [showTelegramWizard, setShowTelegramWizard] = useState(false);
    const [showEmailWizard, setShowEmailWizard] = useState(false);
    const [backendUnreachable, setBackendUnreachable] = useState(false);

    // If network TLS/proxy mode is active, avoid showing login on :3000.
    // Redirect to HTTPS access port so login/session use the correct origin.
    useEffect(() => {
        if (typeof window === 'undefined') return;
        if (window.location.port !== '3000') return;
        const ac = new AbortController();
        fetch(`${getApiBase()}/api/network/ws-config`, { signal: ac.signal, cache: 'no-store' })
            .then((r) => (r.ok ? r.json() : null))
            .then((cfg) => {
                const useWss = !!cfg?.useWss;
                const targetPort = String(cfg?.port || '');
                if (!useWss || !targetPort || targetPort === '3000') return;
                const targetUrl = `https://${window.location.hostname}:${targetPort}${window.location.pathname}${window.location.search}${window.location.hash}`;
                window.location.replace(targetUrl);
            })
            .catch(() => {});
        return () => ac.abort();
    }, []);

    useEffect(() => {
        // ── Test/preview hook ──────────────────────────────────────────────────────
        // /login?preview=soul_wizard  (also: connections | create_admin | 2fa | setup_2fa)
        // forces the REAL onboarding step to render on demand — no admin reset, no backend
        // mutation — so the setup wizard's (mobile) layout can be verified without a real
        // first run. A final submit still hits the auth-protected backend, so interact for
        // layout and avoid the last "Finish" unless you want the real action.
        if (typeof window !== 'undefined') {
            const preview = new URLSearchParams(window.location.search).get('preview');
            const PREVIEW_STEPS = ['login', '2fa', 'create_admin', 'soul_wizard', 'connections', 'setup_2fa'];
            if (preview && PREVIEW_STEPS.includes(preview)) {
                setStep(preview as typeof step);
                setCheckingSetup(false);
                return;
            }
        }
        // Empty string = same-origin /api via Next proxy or HTTPS proxy (e.g. https://localhost:8443).
        const apiPrefix = getApiBase() || '';
        setBackendUnreachable(false);
        const authHeaders: Record<string, string> = {
            'Cache-Control': 'no-cache',
        };
        if (typeof window !== 'undefined') {
            const token = localStorage.getItem('vaf_token');
            if (token) authHeaders.Authorization = `Bearer ${token}`;
        }

        // Check if already authenticated (cookie and/or Bearer); do not skip when apiPrefix is ''.
        fetch(`${apiPrefix}/api/auth/me`, {
            credentials: 'include',
            cache: 'no-store',
            headers: authHeaders,
        })
            .then((res) => {
                if (res.ok) {
                    // If we're in the middle of onboarding (e.g. refresh after 2FA), stay and show Soul/Connections
                    if (typeof window !== 'undefined' && sessionStorage.getItem('vaf_onboarding') === 'true') {
                        const savedStep = sessionStorage.getItem('vaf_onboarding_step') as 'soul_wizard' | 'connections' | null;
                        setStep(savedStep === 'connections' ? 'connections' : 'soul_wizard');
                        setCheckingSetup(false);
                        return;
                    }
                    // Full navigation so dashboard mounts with the same cookie/Bearer semantics as a refresh (avoids client-only transition loops).
                    if (typeof window !== 'undefined') {
                        window.location.replace(`${window.location.origin}/`);
                    } else {
                        router.replace('/');
                    }
                    return; // Stop further checks
                }

                // If not authenticated, check setup status
                if (typeof window !== 'undefined') {
                    localStorage.removeItem('vaf_token');
                }
                fetch(`${apiPrefix}/api/auth/needs-setup`, { credentials: 'include' })
                    .then((res) => {
                        if (res.status === 404) return null;
                        if (res.ok) return res.json();
                        return null;
                    })
                    .then((data) => {
                        if (data?.needs_setup === true) {
                            if (typeof window !== 'undefined') sessionStorage.setItem('vaf_onboarding', 'true');
                            setStep('create_admin');
                        }
                    })
                    .catch(() => {
                        setStep('login');
                    })
                    .finally(() => setCheckingSetup(false));
            })
            .catch(() => {
                setCheckingSetup(false);
                setBackendUnreachable(true);
            });
    }, []);

    // During onboarding, onboardingConfig is built up in state (no API call without auth).

    const handleBootstrapPasswordStep = (e: React.FormEvent) => {
        e.preventDefault();
        if (!password || password !== confirmPassword) {
            setBootstrapError(password !== confirmPassword ? 'Passwords do not match' : 'Enter password');
            return;
        }
        if (password.length < 8) {
            setBootstrapError('Password must be at least 8 characters');
            return;
        }
        // No API call yet — collect credentials locally and continue to soul setup.
        setBootstrapError(null);
        setStep('soul_wizard');
    };

    // Called from the 2FA step (step 4): bootstrap + setup-2fa + verify, then save pending data.
    const handleStartSetup2FA = async () => {
        setIsLoading(true);
        setBootstrapError(null);
        try {
            // 1. Create the admin account now (first API call of the wizard).
            const bootstrapRes = await fetch(`${getApiBase()}/api/auth/bootstrap`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({ username: username.trim(), password }),
            });
            const bootstrapData = await bootstrapRes.json().catch(() => ({}));
            if (!bootstrapRes.ok) {
                setBootstrapError((bootstrapData?.detail as string) || 'Account creation failed');
                setIsLoading(false);
                return;
            }
            const accessToken: string = bootstrapData.access_token || '';
            setTempToken(accessToken);

            // 2. Save pending soul content with Bearer token (no cookie needed).
            if (pendingSoul && accessToken) {
                await fetch(`${getApiBase()}/api/user/soul`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${accessToken}` },
                    body: JSON.stringify({ content: pendingSoul }),
                }).catch(() => {});
            }

            // 3. Save pending connection configs with Bearer token.
            if (Object.keys(onboardingConfig).length > 0 && accessToken) {
                await fetch(`${getApiBase()}/api/config`, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${accessToken}` },
                    body: JSON.stringify(onboardingConfig),
                }).catch(() => {});
            }

            // 4. Fetch the 2FA QR code.
            if (accessToken) {
                try {
                    const setupRes = await fetch(`${getApiBase()}/api/auth/setup-2fa`, {
                        method: 'POST',
                        headers: { Authorization: `Bearer ${accessToken}` },
                        credentials: 'include',
                    });
                    if (setupRes.ok) {
                        const setupData = await setupRes.json();
                        setQrCodeBase64(setupData.qr_code_base64 || null);
                        setTwoFASecret(setupData.secret || null);
                    }
                } catch { /* optional QR */ }
            }
        } catch (err) {
            const msg = typeof err === 'object' && err && 'message' in err ? String((err as Error).message) : '';
            setBootstrapError(`Connection failed. Is the backend reachable?${msg ? ` (${msg})` : ''}`);
        }
        setIsLoading(false);
    };

    const handleWizard2FAVerify = async () => {
        if (!tempToken || !twoFACode.trim()) return;
        setIsLoading(true);
        setTwoFAError(null);
        try {
            const res = await fetch(`${getApiBase()}/api/auth/verify-2fa`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({ code: twoFACode.trim().replace(/\s/g, ''), temp_token: tempToken }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                setTwoFAError((data?.detail as string) || 'Invalid code');
                setIsLoading(false);
                return;
            }
            if (typeof window !== 'undefined') {
                sessionStorage.removeItem('vaf_onboarding');
                sessionStorage.removeItem('vaf_onboarding_step');
                if (data.access_token) localStorage.setItem('vaf_token', data.access_token);
            }
            window.location.replace(`${window.location.origin}/`);
            return;
        } catch {
            setTwoFAError('Network error');
        }
        setIsLoading(false);
    };

    const handleLogin = async (e: React.FormEvent) => {
        e.preventDefault();
        if (!username || !password) return;
        setIsLoading(true);
        setLoginError(null);
        try {
            const res = await fetch(`${getApiBase()}/api/auth/login`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({ username: username.trim(), password, remember_me: rememberMe }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                setLoginError((data?.detail as string) || 'Login failed');
                setIsLoading(false);
                return;
            }
            if (data.requires_2fa) {
                setTempToken(data.temp_token || null);
                setQrCodeBase64(null); // Clear any previous QR code first
                setStep('2fa');
                // Only fetch QR code if user needs to SET UP 2FA (first time)
                // If 2FA is already configured, just show code input field
                if (data.needs_2fa_setup === true) {
                    try {
                        const setupRes = await fetch(`${getApiBase()}/api/auth/setup-2fa`, {
                            method: 'POST',
                            headers: { Authorization: `Bearer ${data.temp_token}` },
                            credentials: 'include',
                        });
                        if (setupRes.ok) {
                            const setupData = await setupRes.json();
                            setQrCodeBase64(setupData.qr_code_base64 || null);
                            setTwoFASecret(setupData.secret || null);
                        }
                    } catch {
                        // optional QR
                    }
                } else {
                    // 2FA already configured - clear any old QR code
                    setQrCodeBase64(null);
                }
            } else {
                if (typeof window !== 'undefined' && data.access_token) {
                    localStorage.setItem('vaf_token', data.access_token);
                }
                router.push('/');
                return;
            }
        } catch {
            setLoginError('Network error');
            setBackendUnreachable(true);
        }
        setIsLoading(false);
    };

    const handle2FAComplete = async (e: React.FormEvent) => {
        e.preventDefault();
        if (!twoFACode.trim()) {
            setTwoFAError('Enter code');
            return;
        }
        setIsLoading(true);
        setTwoFAError(null);
        try {
            const res = await fetch(`${getApiBase()}/api/auth/verify-2fa`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({ code: twoFACode.trim().replace(/\s/g, ''), temp_token: tempToken }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                setTwoFAError((data?.detail as string) || 'Invalid code');
                setIsLoading(false);
                return;
            }
            if (typeof window !== 'undefined' && data.access_token) {
                localStorage.setItem('vaf_token', data.access_token);
            }
            router.push('/');
        } catch {
            setTwoFAError('Network error');
            setBackendUnreachable(true);
        }
        setIsLoading(false);
    };

    const onboardingSteps = [
        { id: 1, label: 'Create Admin' },
        { id: 2, label: 'Soul' },
        { id: 3, label: 'Connections' },
        { id: 4, label: '2FA' },
    ];
    const onboardingCurrentStep =
        step === 'create_admin' ? 1 : step === 'soul_wizard' ? 2 : step === 'connections' ? 3 : step === 'setup_2fa' ? 4 : 0;
    const showOnboardingProgress = onboardingCurrentStep >= 1;

    return (
        <div className="min-h-screen bg-gray-50 flex flex-col items-center justify-center p-4">
            {step !== 'connections' && step !== 'setup_2fa' && (
                <div className="mb-8 text-center">
                    {step === '2fa' ? <WakingAvatar /> : <img src="/logo.png" alt="Veyllo Logo" className="w-20 h-20 mx-auto mb-4 object-contain" />}
                    <h1 className="text-2xl font-bold text-gray-900">Veyllo Agentic Framework</h1>
                    <p className="text-sm text-gray-500 mt-1">User Login</p>
                </div>
            )}
            {showOnboardingProgress && (
                <div className="w-full max-w-md mb-6 grid grid-cols-[2rem_1fr_2rem_1fr_2rem_1fr_2rem] gap-y-2 gap-x-0 items-center">
                    {onboardingSteps.map((s, idx) => (
                        <React.Fragment key={s.id}>
                            <div
                                className={cn(
                                    'w-8 h-8 rounded-full flex items-center justify-center text-sm font-medium justify-self-center',
                                    idx < onboardingCurrentStep
                                        ? 'bg-green-500 text-white'
                                        : idx === onboardingCurrentStep - 1
                                            ? 'bg-gray-900 text-white'
                                            : 'bg-gray-200 text-gray-500'
                                )}
                            >
                                {idx < onboardingCurrentStep - 1 ? <Check className="w-4 h-4" /> : idx + 1}
                            </div>
                            {idx < onboardingSteps.length - 1 && (
                                <div
                                    className={cn(
                                        'h-1 rounded-full w-full',
                                        idx < onboardingCurrentStep - 1 ? 'bg-green-500' : 'bg-gray-200'
                                    )}
                                />
                            )}
                        </React.Fragment>
                    ))}
                    {onboardingSteps.map((s, idx) => (
                        <div
                            key={`label-${s.id}`}
                            className={cn(
                                'text-xs text-center',
                                idx === 0 ? 'col-start-1 col-span-1' : idx === 1 ? 'col-start-3 col-span-1' : idx === 2 ? 'col-start-5 col-span-1' : 'col-start-7 col-span-1'
                            )}
                        >
                            <span className={idx === onboardingCurrentStep - 1 ? 'text-gray-900 font-medium' : 'text-gray-500'}>
                                {s.label}
                            </span>
                        </div>
                    ))}
                </div>
            )}

            {backendUnreachable && (
                <div className="w-full max-w-md mb-4 p-4 bg-amber-50 border border-amber-200 rounded-xl text-sm text-amber-800">
                    <p className="font-medium mb-1">Backend unreachable</p>
                    <p className="mb-2">
                        If you just disabled Local Network: wait a few seconds for restart, then reload the page or open <strong>http://localhost:3000</strong> on this PC.
                    </p>
                    <p className="mb-3 text-amber-700">You can also restart your VAF app (tray or desktop).</p>
                    <button
                        type="button"
                        title="Reload this page, or restart the VAF app from the tray/desktop"
                        onClick={() => window.location.reload()}
                        className="px-3 py-1.5 bg-amber-600 text-white rounded-lg hover:bg-amber-700 text-sm font-medium"
                    >
                        Reload page
                    </button>
                </div>
            )}

            <AnimatePresence mode="wait">
                {checkingSetup && (
                    <motion.div
                        key="checking"
                        initial={{ opacity: 0 }}
                        animate={{ opacity: 1 }}
                        exit={{ opacity: 0 }}
                        className="w-full max-w-md flex flex-col items-center justify-center py-12"
                    >
                        <div className="w-10 h-10 border-2 border-gray-300 border-t-gray-900 rounded-full animate-spin mb-4" />
                        <p className="text-sm text-gray-500">Checking setup…</p>
                    </motion.div>
                )}

                {!checkingSetup && step === 'login' && (
                    <motion.div
                        key="login"
                        initial={{ opacity: 0, y: 20 }}
                        animate={{ opacity: 1, y: 0 }}
                        exit={{ opacity: 0, y: -20 }}
                        className="w-full max-w-md"
                    >
                        <div className="bg-white rounded-2xl shadow-xl border border-gray-200 overflow-hidden">
                            <div className="p-8">
                                <h2 className="text-lg font-semibold text-gray-900 mb-6">Sign in to your account</h2>
                                <form onSubmit={handleLogin} className="space-y-5">
                                    <div className="space-y-1.5">
                                        <label className="text-sm font-medium text-gray-700 ml-1">Username</label>
                                        <div className="relative">
                                            <User className="absolute left-3.5 top-1/2 -translate-y-1/2 text-gray-400 w-5 h-5" />
                                            <input
                                                type="text"
                                                value={username}
                                                onChange={(e) => setUsername(e.target.value)}
                                                className="w-full pl-11 pr-4 py-3 bg-white border border-gray-200 rounded-xl text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-gray-500 transition-all"
                                                placeholder="Enter your username"
                                            />
                                        </div>
                                    </div>
                                    <div className="space-y-1.5">
                                        <label className="text-sm font-medium text-gray-700 ml-1">Password</label>
                                        <div className="relative">
                                            <Lock className="absolute left-3.5 top-1/2 -translate-y-1/2 text-gray-400 w-5 h-5" />
                                            <input
                                                type={showPassword ? 'text' : 'password'}
                                                value={password}
                                                onChange={(e) => setPassword(e.target.value)}
                                                className="w-full pl-11 pr-11 py-3 bg-white border border-gray-200 rounded-xl text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-gray-500 transition-all"
                                                placeholder="••••••••"
                                            />
                                            <button
                                                type="button"
                                                onClick={() => setShowPassword(!showPassword)}
                                                className="absolute right-3.5 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"
                                            >
                                                {showPassword ? <EyeOff size={20} /> : <Eye size={20} />}
                                            </button>
                                        </div>
                                    </div>
                                    <label className="flex items-center gap-2 cursor-pointer">
                                        <input
                                            type="checkbox"
                                            checked={rememberMe}
                                            onChange={(e) => setRememberMe(e.target.checked)}
                                            className="rounded border-gray-300 text-gray-900 focus:ring-gray-400 w-4 h-4 accent-gray-900"
                                        />
                                        <span className="text-sm text-gray-700">Remember me</span>
                                    </label>
                                    {loginError && (
                                        <p className="text-sm text-red-600 bg-red-50 px-3 py-2 rounded-lg">{loginError}</p>
                                    )}
                                    <button
                                        type="submit"
                                        disabled={!username || !password || isLoading}
                                        className="w-full bg-gray-900 hover:bg-black text-white font-medium py-3 rounded-xl shadow-lg transition-all flex items-center justify-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed"
                                    >
                                        {isLoading ? (
                                            <div className="w-5 h-5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                                        ) : (
                                            <>Sign In <ArrowRight size={18} /></>
                                        )}
                                    </button>
                                </form>
                            </div>
                            <div className="bg-gray-50 px-8 py-4 border-t border-gray-100 flex items-center justify-center">
                                <span className="text-sm text-gray-500">Need an account? Contact Admin</span>
                            </div>
                        </div>
                    </motion.div>
                )}

                {!checkingSetup && step === 'create_admin' && (
                    <motion.div
                        key={`create_admin_${createAdminSubStep}`}
                        initial={{ opacity: 0, y: 20 }}
                        animate={{ opacity: 1, y: 0 }}
                        exit={{ opacity: 0, y: -20 }}
                        className="w-full max-w-md"
                    >
                        <div className="bg-white rounded-2xl shadow-xl border border-gray-200 overflow-hidden">
                            <div className="bg-gray-50 px-8 py-3 flex items-center gap-2 border-b border-gray-100">
                                <ShieldCheck size={18} className="text-gray-700" />
                                <span className="text-sm font-medium text-gray-700">First-time setup – Create Admin</span>
                            </div>
                            <div className="p-8">
                                {createAdminSubStep === 'username' && (
                                    <form
                                        onSubmit={(e) => {
                                            e.preventDefault();
                                            if (username.trim().length < 2) {
                                                setBootstrapError('Username must be at least 2 characters');
                                                return;
                                            }
                                            setBootstrapError(null);
                                            setCreateAdminSubStep('password');
                                        }}
                                        className="space-y-5"
                                    >
                                        <h2 className="text-lg font-semibold text-gray-900 mb-1">Admin account</h2>
                                        <p className="text-sm text-gray-500 mb-6">No admin exists yet. Enter the username for the first administrator.</p>
                                        <div className="space-y-5">
                                            <div className="space-y-1.5">
                                                <label className="text-sm font-medium text-gray-700 ml-1">Username *</label>
                                                <div className="relative">
                                                    <User className="absolute left-3.5 top-1/2 -translate-y-1/2 text-gray-400 w-5 h-5" />
                                                    <input
                                                        type="text"
                                                        value={username}
                                                        onChange={(e) => { setUsername(e.target.value); setBootstrapError(null); }}
                                                        className="w-full pl-11 pr-4 py-3 bg-white border border-gray-200 rounded-xl text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-gray-500 transition-all"
                                                        placeholder="admin"
                                                        minLength={2}
                                                        autoFocus
                                                    />
                                                </div>
                                            </div>
                                            <button
                                                type="submit"
                                                disabled={username.trim().length < 2}
                                                className="w-full bg-gray-900 hover:bg-gray-800 text-white font-medium py-3 rounded-xl shadow-sm transition-all flex items-center justify-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed"
                                            >
                                                Weiter <ArrowRight size={18} />
                                            </button>
                                        </div>
                                    </form>
                                )}

                                {createAdminSubStep === 'password' && (
                                    <>
                                        <h2 className="text-lg font-semibold text-gray-900 mb-1">Password</h2>
                                        <p className="text-sm text-gray-500 mb-6">Choose a password for <span className="font-medium text-gray-700">{username || 'admin'}</span> (at least 8 characters).</p>
                                        <form onSubmit={handleBootstrapPasswordStep} className="space-y-5">
                                            <div className="space-y-1.5">
                                                <label className="text-sm font-medium text-gray-700 ml-1">Password *</label>
                                                <div className="relative">
                                                    <Lock className="absolute left-3.5 top-1/2 -translate-y-1/2 text-gray-400 w-5 h-5" />
                                                    <input
                                                        type={showPassword ? 'text' : 'password'}
                                                        value={password}
                                                        onChange={(e) => setPassword(e.target.value)}
                                                        className="w-full pl-11 pr-11 py-3 bg-white border border-gray-200 rounded-xl text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-gray-500 transition-all"
                                                        placeholder="••••••••"
                                                        minLength={8}
                                                    />
                                                    <button type="button" onClick={() => setShowPassword(!showPassword)} className="absolute right-3.5 top-1/2 -translate-y-1/2 text-gray-400">
                                                        {showPassword ? <EyeOff size={20} /> : <Eye size={20} />}
                                                    </button>
                                                </div>
                                            </div>
                                            <div className="space-y-1.5">
                                                <label className="text-sm font-medium text-gray-700 ml-1">Confirm password *</label>
                                                <div className="relative">
                                                    <Lock className="absolute left-3.5 top-1/2 -translate-y-1/2 text-gray-400 w-5 h-5" />
                                                    <input
                                                        type={showPassword ? 'text' : 'password'}
                                                        value={confirmPassword}
                                                        onChange={(e) => setConfirmPassword(e.target.value)}
                                                        className="w-full pl-11 pr-4 py-3 bg-white border border-gray-200 rounded-xl text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-gray-500 transition-all"
                                                        placeholder="••••••••"
                                                    />
                                                </div>
                                            </div>
                                            {bootstrapError && (
                                                <p className="text-sm text-red-600 bg-red-50 px-3 py-2 rounded-lg">{bootstrapError}</p>
                                            )}
                                            <div className="flex gap-3">
                                                <button
                                                    type="button"
                                                    onClick={() => setCreateAdminSubStep('username')}
                                                    className="px-4 py-3 text-gray-600 hover:text-gray-900 border border-gray-200 rounded-xl"
                                                >
                                                    Back
                                                </button>
                                                <button
                                                    type="submit"
                                                    disabled={!password || password !== confirmPassword || password.length < 8 || isLoading}
                                                    className="flex-1 bg-gray-900 hover:bg-gray-800 text-white font-medium py-3 rounded-xl shadow-sm transition-all flex items-center justify-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed"
                                                >
                                                    {isLoading ? (
                                                        <div className="w-5 h-5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                                                    ) : (
                                                        <>Continue <ArrowRight size={18} /></>
                                                    )}
                                                </button>
                                            </div>
                                        </form>
                                    </>
                                )}

                            </div>
                        </div>
                    </motion.div>
                )}

                {!checkingSetup && step === '2fa' && (
                    <motion.div
                        key="2fa"
                        initial={{ opacity: 0, scale: 0.95 }}
                        animate={{ opacity: 1, scale: 1 }}
                        exit={{ opacity: 0, scale: 1.05 }}
                        className="w-full max-w-md"
                    >
                        <div className="bg-white rounded-2xl shadow-xl border border-gray-200 overflow-hidden">
                            <div className="bg-gray-50 px-8 py-3 flex items-center gap-2 border-b border-gray-100">
                                <Smartphone size={18} className="text-gray-600" />
                                <span className="text-sm font-medium text-gray-700">Two-Factor Authentication</span>
                            </div>
                            <div className="p-8">
                                {qrCodeBase64 && (
                                    <div className="flex justify-center mb-6">
                                        <img src={`data:image/png;base64,${qrCodeBase64}`} alt="2FA QR" className="w-40 h-40" />
                                    </div>
                                )}
                                <form onSubmit={handle2FAComplete} className="space-y-5">
                                    <div className="space-y-1.5">
                                        <label className="text-sm font-medium text-gray-700 ml-1">Authenticator code</label>
                                        <input
                                            type="text"
                                            value={twoFACode}
                                            onChange={(e) => { setTwoFACode(e.target.value.replace(/\D/g, '').slice(0, 6)); setTwoFAError(null); }}
                                            className="w-full px-4 py-3 bg-white border border-gray-200 rounded-xl text-gray-900 font-mono text-center text-lg tracking-widest focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-gray-500"
                                            placeholder="000000"
                                            maxLength={6}
                                        />
                                    </div>
                                    {twoFAError && (
                                        <p className="text-sm text-red-600 bg-red-50 px-3 py-2 rounded-lg">{twoFAError}</p>
                                    )}
                                    <button
                                        type="submit"
                                        disabled={twoFACode.length < 6 || isLoading}
                                        className="w-full bg-gray-900 hover:bg-black text-white font-medium py-3 rounded-xl shadow-lg transition-all flex items-center justify-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed"
                                    >
                                        {isLoading ? (
                                            <div className="w-5 h-5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                                        ) : (
                                            <>Verify and continue <ArrowRight size={18} /></>
                                        )}
                                    </button>
                                </form>
                                <button
                                    type="button"
                                    onClick={() => { setStep('login'); setTwoFACode(''); setTwoFAError(null); }}
                                    className="mt-4 w-full text-sm text-gray-500 hover:text-gray-900"
                                >
                                    Back to login
                                </button>
                            </div>
                        </div>
                    </motion.div>
                )}

                {!checkingSetup && step === 'soul_wizard' && (
                    <SoulWizard
                        isOpen={true}
                        onClose={() => {
                            if (typeof window !== 'undefined') sessionStorage.setItem('vaf_onboarding_step', 'connections');
                            setStep('connections');
                        }}
                        username={username || 'Admin'}
                        onComplete={(content) => {
                            setPendingSoul(content);
                            if (typeof window !== 'undefined') sessionStorage.setItem('vaf_onboarding_step', 'connections');
                            setStep('connections');
                        }}
                    />
                )}

                {!checkingSetup && step === 'connections' && (
                    <motion.div
                        key="connections"
                        initial={{ opacity: 0, y: 20 }}
                        animate={{ opacity: 1, y: 0 }}
                        exit={{ opacity: 0, y: -20 }}
                        className="w-full max-w-2xl max-h-[85vh] overflow-y-auto"
                    >
                        <div className="bg-white rounded-2xl shadow-xl border border-gray-200 overflow-hidden">
                            <div className="bg-gray-50 px-8 py-3 flex items-center gap-2 border-b border-gray-100">
                                <Link2 size={18} className="text-gray-700" />
                                <span className="text-sm font-medium text-gray-700">Connections (optional)</span>
                            </div>
                            <div className="p-6">
                                <p className="text-sm text-gray-500 mb-4">
                                    Connect external apps to interact with your VAF agent. You can set these up later in Settings.
                                </p>
                                <div className="space-y-6">
                                    {CATEGORIES.filter((cat) => CONNECTION_APPS.some((app) => app.category === cat.id)).map((category) => (
                                        <div key={category.id} className="space-y-3">
                                            <div>
                                                <h4 className="text-sm font-medium text-gray-700">{category.label}</h4>
                                                <p className="text-xs text-gray-400">{category.description}</p>
                                            </div>
                                            <div className="space-y-2">
                                                {CONNECTION_APPS.filter((app) => app.category === category.id).map((app) => {
                                                    const Icon = app.icon;
                                                    const configured = (onboardingConfig[app.configKey] as { verified?: boolean })?.verified === true;
                                                    const canSetup = (app.id === 'discord' || app.id === 'telegram' || app.id === 'email') && !app.comingSoon;
                                                    return (
                                                        <div
                                                            key={app.id}
                                                            className={cn(
                                                                'p-4 rounded-xl border transition-all',
                                                                configured ? 'bg-white border-gray-200 shadow-sm' : 'bg-gray-50 border-gray-200',
                                                                app.comingSoon && 'opacity-60'
                                                            )}
                                                        >
                                                            <div className="flex items-center justify-between">
                                                                <div className="flex items-center gap-3 min-w-0 flex-1">
                                                                    <div
                                                                        className={cn(
                                                                            'w-10 h-10 rounded-xl flex items-center justify-center text-white',
                                                                            configured ? (app.iconColor || 'bg-gray-900') : 'bg-gray-300 text-gray-500'
                                                                        )}
                                                                    >
                                                                        <Icon className="w-5 h-5" />
                                                                    </div>
                                                                    <div>
                                                                        <div className="flex items-center gap-2">
                                                                            <span className="font-medium text-gray-900">{app.name}</span>
                                                                            {app.comingSoon && (
                                                                                <span className="text-xs px-2 py-0.5 rounded-full bg-gray-100 text-gray-500">Coming Soon</span>
                                                                            )}
                                                                            {configured && !app.comingSoon && (
                                                                                <span className="text-xs px-2 py-0.5 rounded-full bg-green-100 text-green-700">Configured</span>
                                                                            )}
                                                                        </div>
                                                                        <p className="text-sm text-gray-500">{app.description}</p>
                                                                    </div>
                                                                </div>
                                                                {canSetup ? (
                                                                    <button
                                                                        onClick={() => {
                                                                            if (app.id === 'discord') setShowDiscordWizard(true);
                                                                            if (app.id === 'telegram') setShowTelegramWizard(true);
                                                                            if (app.id === 'email') setShowEmailWizard(true);
                                                                        }}
                                                                        className={cn(
                                                                            'flex items-center gap-2 px-4 py-2 rounded-lg font-medium transition-colors shrink-0 whitespace-nowrap max-md:text-sm',
                                                                            configured ? 'bg-gray-100 hover:bg-gray-200 text-gray-700' : 'bg-gray-900 hover:bg-gray-800 text-white'
                                                                        )}
                                                                    >
                                                                        {configured ? 'Edit' : 'Set up'}
                                                                    </button>
                                                                ) : (
                                                                    <span className="text-xs px-3 py-1.5 rounded-lg bg-gray-100 text-gray-400 font-medium">Coming Soon</span>
                                                                )}
                                                            </div>
                                                        </div>
                                                    );
                                                })}
                                            </div>
                                        </div>
                                    ))}
                                </div>
                                <div className="flex gap-3 mt-6">
                                    <button
                                        type="button"
                                        onClick={() => {
                                            setStep('setup_2fa');
                                            handleStartSetup2FA();
                                        }}
                                        className="flex-1 bg-gray-900 hover:bg-gray-800 text-white font-medium py-3 rounded-xl shadow-sm transition-all flex items-center justify-center gap-2"
                                    >
                                        Continue to 2FA Setup <ArrowRight size={18} />
                                    </button>
                                </div>
                            </div>
                        </div>
                    </motion.div>
                )}
                {!checkingSetup && step === 'setup_2fa' && (
                    <motion.div
                        key="setup_2fa"
                        initial={{ opacity: 0, y: 20 }}
                        animate={{ opacity: 1, y: 0 }}
                        exit={{ opacity: 0, y: -20 }}
                        className="w-full max-w-md"
                    >
                        <div className="bg-white rounded-2xl shadow-xl border border-gray-200 overflow-hidden">
                            <div className="bg-gray-50 px-8 py-3 flex items-center gap-2 border-b border-gray-100">
                                <Smartphone size={18} className="text-gray-600" />
                                <span className="text-sm font-medium text-gray-700">Two-Factor Authentication</span>
                            </div>
                            <div className="p-8">
                                {bootstrapError && (
                                    <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded-xl text-sm text-red-700">
                                        {bootstrapError}
                                    </div>
                                )}
                                {isLoading && !qrCodeBase64 && (
                                    <div className="flex flex-col items-center justify-center py-8">
                                        <div className="w-10 h-10 border-2 border-gray-300 border-t-gray-900 rounded-full animate-spin mb-4" />
                                        <p className="text-sm text-gray-500">Creating account…</p>
                                    </div>
                                )}
                                {!isLoading && !bootstrapError && (
                                    <>
                                        <h2 className="text-lg font-semibold text-gray-900 mb-1">Scan QR Code</h2>
                                        <p className="text-sm text-gray-500 mb-6">Scan the code with your authenticator app (e.g. Google Authenticator), then enter the 6-digit code below.</p>
                                        {qrCodeBase64 && (
                                            <div className="flex justify-center mb-6">
                                                <img src={`data:image/png;base64,${qrCodeBase64}`} alt="2FA QR" className="w-44 h-44 border border-gray-200 rounded-xl p-2" />
                                            </div>
                                        )}
                                        {twoFASecret && (
                                            <div className="mb-6">
                                                <p className="text-xs text-gray-500 text-center mb-1.5">Or enter this key manually in your app:</p>
                                                <button
                                                    type="button"
                                                    onClick={() => { try { navigator.clipboard?.writeText(twoFASecret); setSecretCopied(true); setTimeout(() => setSecretCopied(false), 1500); } catch { /* clipboard unavailable */ } }}
                                                    className="w-full flex items-center justify-center gap-2 px-3 py-2.5 rounded-xl bg-gray-50 border border-gray-200 hover:bg-gray-100 active:bg-gray-200 transition-colors font-mono text-sm tracking-wider text-gray-800"
                                                    title="Copy to clipboard"
                                                >
                                                    <span className="break-all">{twoFASecret}</span>
                                                    {secretCopied ? <Check size={15} className="shrink-0 text-green-600" /> : <Copy size={15} className="shrink-0 text-gray-400" />}
                                                </button>
                                            </div>
                                        )}
                                        <div className="space-y-1.5 mb-5">
                                            <label className="text-sm font-medium text-gray-700 ml-1">Authenticator code</label>
                                            <input
                                                type="text"
                                                value={twoFACode}
                                                onChange={(e) => { setTwoFACode(e.target.value.replace(/\D/g, '').slice(0, 6)); setTwoFAError(null); }}
                                                className="w-full px-4 py-3 bg-white border border-gray-200 rounded-xl text-gray-900 font-mono text-center text-lg tracking-widest focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-gray-500"
                                                placeholder="000000"
                                                maxLength={6}
                                                autoFocus
                                            />
                                        </div>
                                        {twoFAError && (
                                            <p className="text-sm text-red-600 bg-red-50 px-3 py-2 rounded-lg mb-4">{twoFAError}</p>
                                        )}
                                        <button
                                            type="button"
                                            onClick={handleWizard2FAVerify}
                                            disabled={twoFACode.length < 6 || isLoading}
                                            className="w-full bg-gray-900 hover:bg-gray-800 text-white font-medium py-3 rounded-xl shadow-sm transition-all flex items-center justify-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed"
                                        >
                                            {isLoading ? (
                                                <div className="w-5 h-5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                                            ) : (
                                                <><CheckCircle size={18} /> Finish Setup</>
                                            )}
                                        </button>
                                    </>
                                )}
                                {!isLoading && bootstrapError && (
                                    <button
                                        type="button"
                                        onClick={() => { setBootstrapError(null); handleStartSetup2FA(); }}
                                        className="w-full mt-4 bg-gray-900 hover:bg-gray-800 text-white font-medium py-3 rounded-xl shadow-sm transition-all"
                                    >
                                        Retry
                                    </button>
                                )}
                            </div>
                        </div>
                    </motion.div>
                )}
            </AnimatePresence>

            <DiscordSetupWizard
                isOpen={showDiscordWizard}
                onClose={() => setShowDiscordWizard(false)}
                onComplete={(config: DiscordConfig) => {
                    setOnboardingConfig((prev) => ({ ...prev, discord_config: config }));
                    setShowDiscordWizard(false);
                }}
                existingConfig={onboardingConfig.discord_config as DiscordConfig | undefined}
            />

            <TelegramSetupWizard
                isOpen={showTelegramWizard}
                onClose={() => setShowTelegramWizard(false)}
                onComplete={(config: TelegramConfig) => {
                    setOnboardingConfig((prev) => ({ ...prev, telegram_config: config }));
                    setShowTelegramWizard(false);
                }}
                existingConfig={onboardingConfig.telegram_config as TelegramConfig | undefined}
            />

            <EmailSetupWizard
                isOpen={showEmailWizard}
                onClose={() => setShowEmailWizard(false)}
                onComplete={() => {
                    // Email wizard handles its own config persistence
                    setShowEmailWizard(false);
                    // Reload config to show updated email accounts
                    fetch(`${getApiBase()}/api/config`, { credentials: 'include' })
                        .then((res) => res.ok ? res.json() : {})
                        .then((data) => setOnboardingConfig(data))
                        .catch(() => { });
                }}
            />
        </div>
    );
}
