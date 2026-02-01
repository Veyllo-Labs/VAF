'use client';

import React, { useState, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { motion, AnimatePresence } from 'framer-motion';
import {
    User, Lock, Eye, EyeOff, ArrowRight, ShieldCheck,
    Smartphone, CheckCircle
} from 'lucide-react';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8001';

export default function LoginPage() {
    const router = useRouter();
    // Default to login; only show wizard when API explicitly says needs_setup: true (no admin yet)
    const [step, setStep] = useState<'login' | '2fa' | 'create_admin' | 'dashboard'>('login');
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
    const [loginError, setLoginError] = useState<string | null>(null);
    const [twoFAError, setTwoFAError] = useState<string | null>(null);
    const [bootstrapError, setBootstrapError] = useState<string | null>(null);
    const [createAdminSubStep, setCreateAdminSubStep] = useState<'username' | 'password' | '2fa'>('username');

    useEffect(() => {
        if (!API_BASE) {
            setCheckingSetup(false);
            return;
        }

        // Check if already authenticated
        fetch(`${API_BASE}/api/auth/me`, { credentials: 'include' })
            .then((res) => {
                if (res.ok) {
                    router.replace('/');
                    return; // Stop further checks
                }
                
                // If not authenticated, check setup status
                fetch(`${API_BASE}/api/auth/needs-setup`, { credentials: 'include' })
                    .then((res) => {
                        if (res.status === 404) return null;
                        if (res.ok) return res.json();
                        return null;
                    })
                    .then((data) => {
                        if (data?.needs_setup === true) {
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
            });
    }, []);

    const handleBootstrapPasswordStep = async (e: React.FormEvent) => {
        e.preventDefault();
        if (!password || password !== confirmPassword) {
            setBootstrapError(password !== confirmPassword ? 'Passwords do not match' : 'Enter password');
            return;
        }
        if (password.length < 8) {
            setBootstrapError('Password must be at least 8 characters');
            return;
        }
        setIsLoading(true);
        setBootstrapError(null);
        try {
            const res = await fetch(`${API_BASE}/api/auth/bootstrap`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({ username: username.trim(), password }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                setBootstrapError((data?.detail as string) || 'Setup failed');
                setIsLoading(false);
                return;
            }
            setTempToken(data.access_token || null);
            if (data.user?.requires_2fa_setup && data.access_token) {
                try {
                    const setupRes = await fetch(`${API_BASE}/api/auth/setup-2fa`, {
                        method: 'POST',
                        headers: { Authorization: `Bearer ${data.access_token}` },
                        credentials: 'include',
                    });
                    if (setupRes.ok) {
                        const setupData = await setupRes.json();
                        setQrCodeBase64(setupData.qr_code_base64 || null);
                    }
                } catch {
                    // optional QR
                }
                setCreateAdminSubStep('2fa');
            } else {
                router.push('/');
                return;
            }
        } catch (err) {
            const msg = typeof err === 'object' && err && 'message' in err ? String((err as Error).message) : '';
            setBootstrapError(
                `Connection failed. Is the backend at ${API_BASE} reachable?${msg ? ` (${msg})` : ''}`
            );
        }
        setIsLoading(false);
    };

    const handleCreateAdmin2FAComplete = async () => {
        if (!tempToken || !twoFACode.trim()) return;
        setIsLoading(true);
        setTwoFAError(null);
        try {
            const res = await fetch(`${API_BASE}/api/auth/verify-2fa`, {
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
            router.push('/');
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
            const res = await fetch(`${API_BASE}/api/auth/login`, {
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
                        const setupRes = await fetch(`${API_BASE}/api/auth/setup-2fa`, {
                            method: 'POST',
                            headers: { Authorization: `Bearer ${data.temp_token}` },
                            credentials: 'include',
                        });
                        if (setupRes.ok) {
                            const setupData = await setupRes.json();
                            setQrCodeBase64(setupData.qr_code_base64 || null);
                        }
                    } catch {
                        // optional QR
                    }
                } else {
                    // 2FA already configured - clear any old QR code
                    setQrCodeBase64(null);
                }
            } else {
                router.push('/');
                return;
            }
        } catch {
            setLoginError('Network error');
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
            const res = await fetch(`${API_BASE}/api/auth/verify-2fa`, {
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
            router.push('/');
        } catch {
            setTwoFAError('Network error');
        }
        setIsLoading(false);
    };

    return (
        <div className="min-h-screen bg-gray-50 flex flex-col items-center justify-center p-4">
            {step !== 'dashboard' && (
                <div className="mb-8 text-center">
                    <img src="/logo.png" alt="Veyllo Logo" className="w-20 h-20 mx-auto mb-4 object-contain" />
                    <h1 className="text-2xl font-bold text-gray-900">Veyllo Agentic Framework</h1>
                    <p className="text-sm text-gray-500 mt-1">User Login</p>
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
                                    <>
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
                                                type="button"
                                                onClick={() => {
                                                    if (username.trim().length < 2) {
                                                        setBootstrapError('Username must be at least 2 characters');
                                                        return;
                                                    }
                                                    setBootstrapError(null);
                                                    setCreateAdminSubStep('password');
                                                }}
                                                disabled={username.trim().length < 2}
                                                className="w-full bg-gray-900 hover:bg-gray-800 text-white font-medium py-3 rounded-xl shadow-sm transition-all flex items-center justify-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed"
                                            >
                                                Weiter <ArrowRight size={18} />
                                            </button>
                                        </div>
                                    </>
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
                                                        <>Continue to 2FA <ArrowRight size={18} /></>
                                                    )}
                                                </button>
                                            </div>
                                        </form>
                                    </>
                                )}

                                {createAdminSubStep === '2fa' && (
                                    <>
                                        <h2 className="text-lg font-semibold text-gray-900 mb-1">Two-Factor Authentication</h2>
                                        <p className="text-sm text-gray-500 mb-6">Scan the QR code with your authenticator app, then enter the code below.</p>
                                        {qrCodeBase64 && (
                                            <div className="flex justify-center mb-6">
                                                <img src={`data:image/png;base64,${qrCodeBase64}`} alt="2FA QR" className="w-40 h-40" />
                                            </div>
                                        )}
                                        <form onSubmit={handleCreateAdmin2FAComplete} className="space-y-5">
                                            <div className="space-y-1.5">
                                                <label className="text-sm font-medium text-gray-700 ml-1">Code</label>
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
                                                className="w-full bg-gray-900 hover:bg-gray-800 text-white font-medium py-3 rounded-xl shadow-sm transition-all flex items-center justify-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed"
                                            >
                                                {isLoading ? (
                                                    <div className="w-5 h-5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                                                ) : (
                                                    <>Finish setup <ArrowRight size={18} /></>
                                                )}
                                            </button>
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
            </AnimatePresence>
        </div>
    );
}
