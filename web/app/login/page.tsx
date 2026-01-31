'use client';

import React, { useState } from 'react';
import { useRouter } from 'next/navigation';
import { motion, AnimatePresence } from 'framer-motion';
import { 
    User, Lock, Eye, EyeOff, ArrowRight, ShieldCheck, 
    Smartphone, CheckCircle, Copy, AlertTriangle, 
    QrCode, LogOut, LayoutDashboard, Database
} from 'lucide-react';
import { cn } from '@/lib/utils';

export default function LoginPage() {
    const router = useRouter();
    const [step, setStep] = useState<'login' | '2fa' | 'dashboard'>('login');
    const [username, setUsername] = useState('');
    const [password, setPassword] = useState('');
    const [showPassword, setShowPassword] = useState(false);
    const [isLoading, setIsLoading] = useState(false);

    const handleLogin = (e: React.FormEvent) => {
        e.preventDefault();
        if (!username || !password) return;
        
        setIsLoading(true);
        // Simulate network request
        setTimeout(() => {
            setIsLoading(false);
            setStep('2fa');
        }, 800);
    };

    const handle2FAComplete = () => {
        setIsLoading(true);
        setTimeout(() => {
            setIsLoading(false);
            // Redirect to the main UI environment
            router.push('/');
        }, 800);
    };

    return (
        <div className="min-h-screen bg-gray-50 flex flex-col items-center justify-center p-4">
            
            {/* Branding - Visible in Login/2FA */}
            {step !== 'dashboard' && (
                <div className="mb-8 text-center">
                    <img src="/logo.png" alt="Veyllo Logo" className="w-20 h-20 mx-auto mb-4 object-contain" />
                    <h1 className="text-2xl font-bold text-gray-900">Veyllo Agentic Framework</h1>
                    <p className="text-sm text-gray-500 mt-1">User Login</p>
                </div>
            )}

            <AnimatePresence mode="wait">
                
                {/* STEP 1: LOGIN */}
                {step === 'login' && (
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
                                                className="w-full pl-11 pr-4 py-3 bg-white border border-gray-200 rounded-xl text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 transition-all"
                                                placeholder="Enter your username"
                                            />
                                        </div>
                                    </div>

                                    <div className="space-y-1.5">
                                        <label className="text-sm font-medium text-gray-700 ml-1">Password</label>
                                        <div className="relative">
                                            <Lock className="absolute left-3.5 top-1/2 -translate-y-1/2 text-gray-400 w-5 h-5" />
                                            <input 
                                                type={showPassword ? "text" : "password"} 
                                                value={password}
                                                onChange={(e) => setPassword(e.target.value)}
                                                className="w-full pl-11 pr-11 py-3 bg-white border border-gray-200 rounded-xl text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 transition-all"
                                                placeholder="••••••••"
                                            />
                                            <button 
                                                type="button"
                                                onClick={() => setShowPassword(!showPassword)}
                                                className="absolute right-3.5 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600 transition-colors"
                                            >
                                                {showPassword ? <EyeOff size={20} /> : <Eye size={20} />}
                                            </button>
                                        </div>
                                    </div>

                                    <button 
                                        type="submit"
                                        disabled={!username || !password || isLoading}
                                        className="w-full bg-gray-900 hover:bg-black text-white font-medium py-3 rounded-xl shadow-lg shadow-gray-200 transition-all flex items-center justify-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed"
                                    >
                                        {isLoading ? (
                                            <div className="w-5 h-5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                                        ) : (
                                            <>
                                                Sign In <ArrowRight size={18} />
                                            </>
                                        )}
                                    </button>
                                </form>
                            </div>
                            <div className="bg-gray-50 px-8 py-4 border-t border-gray-100 flex justify-center">
                                <button className="text-sm text-gray-500 hover:text-blue-600 transition-colors">
                                    Need an account? Contact Admin
                                </button>
                            </div>
                        </div>
                    </motion.div>
                )}

                {/* STEP 2: 2FA SETUP */}
                {step === '2fa' && (
                    <motion.div 
                        key="2fa"
                        initial={{ opacity: 0, scale: 0.95 }}
                        animate={{ opacity: 1, scale: 1 }}
                        exit={{ opacity: 0, scale: 1.05 }}
                        className="w-full max-w-lg"
                    >
                        <div className="bg-white rounded-2xl shadow-xl border border-gray-200 overflow-hidden">
                            {/* Success Banner */}
                            <div className="bg-green-50 px-8 py-3 flex items-center gap-2 border-b border-green-100">
                                <CheckCircle size={18} className="text-green-600" />
                                <span className="text-sm font-medium text-green-700">Login Successful</span>
                            </div>

                            <div className="p-8">
                                <div className="text-center mb-8">
                                    <div className="w-14 h-14 bg-indigo-50 rounded-full flex items-center justify-center mx-auto mb-4 text-indigo-600">
                                        <Smartphone size={28} />
                                    </div>
                                    <h2 className="text-xl font-bold text-gray-900">Two-Factor Authentication</h2>
                                    <p className="text-gray-500 mt-1 max-w-xs mx-auto">
                                        To secure your local access, please set up 2FA using your authenticator app.
                                    </p>
                                </div>

                                <div className="grid grid-cols-1 md:grid-cols-2 gap-8 mb-8">
                                    {/* QR Code Placeholder */}
                                    <div className="flex flex-col items-center justify-center space-y-3">
                                        <div className="bg-white p-3 border-2 border-gray-100 rounded-xl shadow-inner">
                                            {/* Simulated QR Code SVG */}
                                            <svg width="140" height="140" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg" className="text-gray-900">
                                                <rect width="100" height="100" fill="white"/>
                                                <path d="M0 0h30v30H0V0zm10 10v10h10V10H10zM70 0h30v30H70V0zm10 10v10h10V10H80zM0 70h30v30H0V70zm10 10v10h10V80H10z" fill="currentColor"/>
                                                <rect x="40" y="10" width="10" height="10" fill="currentColor" rx="1"/>
                                                <rect x="60" y="20" width="10" height="10" fill="currentColor" rx="1"/>
                                                <rect x="40" y="40" width="10" height="10" fill="currentColor" rx="1"/>
                                                <rect x="10" y="40" width="10" height="10" fill="currentColor" rx="1"/>
                                                <rect x="80" y="40" width="10" height="10" fill="currentColor" rx="1"/>
                                                <rect x="50" y="50" width="10" height="10" fill="currentColor" rx="1"/>
                                                <rect x="70" y="60" width="10" height="10" fill="currentColor" rx="1"/>
                                                <rect x="40" y="70" width="10" height="10" fill="currentColor" rx="1"/>
                                                <rect x="60" y="80" width="10" height="10" fill="currentColor" rx="1"/>
                                                <rect x="80" y="90" width="10" height="10" fill="currentColor" rx="1"/>
                                                <rect x="40" y="90" width="10" height="10" fill="currentColor" rx="1"/>
                                            </svg>
                                        </div>
                                        <span className="text-xs font-mono text-gray-400 bg-gray-50 px-2 py-1 rounded">
                                            VAF-LOCAL-{Math.floor(Math.random()*9999)}
                                        </span>
                                    </div>

                                    {/* Instructions */}
                                    <div className="flex flex-col justify-center space-y-4">
                                        <div className="space-y-2">
                                            <h3 className="text-xs font-bold text-gray-500 uppercase tracking-wide">Compatible Apps</h3>
                                            <ul className="text-sm text-gray-600 space-y-1">
                                                <li className="flex items-center gap-2"><div className="w-1.5 h-1.5 rounded-full bg-gray-400" />Google Authenticator</li>
                                                <li className="flex items-center gap-2"><div className="w-1.5 h-1.5 rounded-full bg-gray-400" />Microsoft Authenticator</li>
                                                <li className="flex items-center gap-2"><div className="w-1.5 h-1.5 rounded-full bg-gray-400" />Authy / 1Password</li>
                                            </ul>
                                        </div>
                                        
                                        <div className="space-y-2">
                                            <h3 className="text-xs font-bold text-gray-500 uppercase tracking-wide">Backup Codes</h3>
                                            <div className="bg-gray-50 border border-gray-200 rounded-lg p-3 font-mono text-xs text-gray-600 grid grid-cols-2 gap-2 text-center">
                                                <span>1234 5678</span>
                                                <span>8765 4321</span>
                                                <span>1122 3344</span>
                                                <span>4455 6677</span>
                                            </div>
                                        </div>
                                    </div>
                                </div>

                                <div className="space-y-4">
                                    <div className="flex items-start gap-3 p-4 bg-amber-50 rounded-lg border border-amber-100">
                                        <AlertTriangle size={18} className="text-amber-600 shrink-0 mt-0.5" />
                                        <p className="text-xs text-amber-800 leading-relaxed">
                                            <strong>Security Notice:</strong> You must configure 2FA to prevent unauthorized access in your local network environment.
                                        </p>
                                    </div>

                                    <button 
                                        onClick={handle2FAComplete}
                                        disabled={isLoading}
                                        className="w-full bg-indigo-600 hover:bg-indigo-700 text-white font-medium py-3 rounded-xl shadow-lg shadow-indigo-200 transition-all flex items-center justify-center gap-2"
                                    >
                                        {isLoading ? (
                                            <div className="w-5 h-5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                                        ) : (
                                            <>
                                                I've Scanned the QR Code <ArrowRight size={18} />
                                            </>
                                        )}
                                    </button>
                                </div>
                            </div>
                        </div>
                    </motion.div>
                )}

                {/* STEP 3: DASHBOARD */}
                {step === 'dashboard' && (
                    <motion.div 
                        key="dashboard"
                        initial={{ opacity: 0, scale: 0.95 }}
                        animate={{ opacity: 1, scale: 1 }}
                        className="w-full max-w-5xl"
                    >
                        {/* Dashboard Navbar */}
                        <div className="bg-white rounded-2xl shadow-sm border border-gray-200 p-4 mb-6 flex items-center justify-between">
                            <div className="flex items-center gap-3 px-2">
                                <img src="/logo.png" alt="Veyllo Logo" className="w-10 h-10 object-contain" />
                                <span className="font-semibold text-gray-900">Veyllo Agentic Framework</span>
                            </div>
                            <div className="flex items-center gap-4">
                                <div className="flex items-center gap-3 px-4 py-2 bg-gray-50 rounded-lg border border-gray-100">
                                    <div className="w-8 h-8 rounded-full bg-blue-100 text-blue-600 flex items-center justify-center font-bold text-sm">
                                        {username[0]?.toUpperCase() || 'U'}
                                    </div>
                                    <div className="text-sm">
                                        <div className="font-medium text-gray-900">{username}</div>
                                        <div className="text-xs text-gray-500">Online • 2FA Active</div>
                                    </div>
                                </div>
                                <button onClick={() => setStep('login')} className="p-2 text-gray-500 hover:text-red-600 hover:bg-red-50 rounded-lg transition-colors">
                                    <LogOut size={20} />
                                </button>
                            </div>
                        </div>

                        {/* Dashboard Content */}
                        <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
                            {/* Welcome Card */}
                            <div className="col-span-1 md:col-span-2 bg-gradient-to-br from-indigo-500 to-purple-600 rounded-2xl shadow-xl p-8 text-white relative overflow-hidden">
                                <div className="relative z-10">
                                    <h2 className="text-3xl font-bold mb-2">Welcome back, {username}!</h2>
                                    <p className="text-indigo-100 text-lg mb-6 max-w-md">Your local environment is secure and ready. Access your tools and workflows below.</p>
                                    <button className="bg-white text-indigo-600 px-6 py-2.5 rounded-lg font-medium shadow-sm hover:shadow-md transition-all flex items-center gap-2">
                                        <LayoutDashboard size={18} /> Open Main Console
                                    </button>
                                </div>
                                <div className="absolute right-0 bottom-0 opacity-10">
                                    <ShieldCheck size={200} />
                                </div>
                            </div>

                            {/* Status Card */}
                            <div className="bg-white rounded-2xl shadow-sm border border-gray-200 p-6 flex flex-col justify-between">
                                <div>
                                    <h3 className="font-semibold text-gray-900 mb-4">System Status</h3>
                                    <div className="space-y-3">
                                        <div className="flex items-center justify-between text-sm">
                                            <span className="text-gray-600">Memory DB</span>
                                            <span className="text-green-600 font-medium flex items-center gap-1">
                                                <div className="w-2 h-2 rounded-full bg-green-500" /> Operational
                                            </span>
                                        </div>
                                        <div className="flex items-center justify-between text-sm">
                                            <span className="text-gray-600">Local LLM</span>
                                            <span className="text-green-600 font-medium flex items-center gap-1">
                                                <div className="w-2 h-2 rounded-full bg-green-500" /> Loaded
                                            </span>
                                        </div>
                                        <div className="flex items-center justify-between text-sm">
                                            <span className="text-gray-600">Network</span>
                                            <span className="text-blue-600 font-medium flex items-center gap-1">
                                                <div className="w-2 h-2 rounded-full bg-blue-500 animate-pulse" /> 192.168.1.100
                                            </span>
                                        </div>
                                    </div>
                                </div>
                                <div className="mt-6 pt-6 border-t border-gray-100">
                                    <div className="text-xs text-gray-500 mb-2">Storage Usage</div>
                                    <div className="w-full h-2 bg-gray-100 rounded-full overflow-hidden">
                                        <div className="h-full bg-gray-900 w-[35%]" />
                                    </div>
                                </div>
                            </div>
                        </div>

                        {/* Recent Activity */}
                        <div className="mt-6 bg-white rounded-2xl shadow-sm border border-gray-200 p-6">
                            <h3 className="font-semibold text-gray-900 mb-4">Recent Network Activity</h3>
                            <div className="space-y-0">
                                {[
                                    { user: username, action: 'Logged in via Local Network', time: 'Just now', icon: ShieldCheck, color: 'text-green-600', bg: 'bg-green-100' },
                                    { user: 'System', action: 'Memory Database optimization completed', time: '10 mins ago', icon: Database, color: 'text-blue-600', bg: 'bg-blue-100' },
                                    { user: 'admin', action: 'Updated security policies', time: '2 hours ago', icon: Lock, color: 'text-gray-600', bg: 'bg-gray-100' },
                                ].map((item, i) => (
                                    <div key={i} className="flex items-center gap-4 py-3 border-b border-gray-50 last:border-0 hover:bg-gray-50 px-2 -mx-2 rounded-lg transition-colors">
                                        <div className={`w-10 h-10 rounded-lg ${item.bg} ${item.color} flex items-center justify-center shrink-0`}>
                                            <item.icon size={20} />
                                        </div>
                                        <div className="flex-1">
                                            <div className="text-sm font-medium text-gray-900">{item.action}</div>
                                            <div className="text-xs text-gray-500">by {item.user}</div>
                                        </div>
                                        <div className="text-xs text-gray-400">{item.time}</div>
                                    </div>
                                ))}
                            </div>
                        </div>
                    </motion.div>
                )}

            </AnimatePresence>
        </div>
    );
}


