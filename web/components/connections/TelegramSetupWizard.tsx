'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import React, { useState, useEffect } from 'react';
import {
    X, ChevronRight, ChevronLeft, Check, Copy, ExternalLink,
    MessageCircle, Shield, Loader2, AlertCircle, CheckCircle2, UserPlus
} from 'lucide-react';
import { cn } from '@/lib/utils';

/** Use relative /api/ so Next.js rewrites to backend (127.0.0.1:8001). */
const api = (path: string) => path.startsWith('/') ? path : `/${path}`;

interface TelegramSetupWizardProps {
    isOpen: boolean;
    onClose: () => void;
    onComplete: (config: TelegramConfig) => void;
    existingConfig?: TelegramConfig;
}

export interface TelegramConfig {
    bot_token: string;
    verified: boolean;
    enabled: boolean;
    whitelist?: Array<{ telegram_user_id: string; telegram_username?: string; user_scope_id?: string; vaf_username?: string }>;
}

const STEPS = [
    { id: 'intro', title: 'Telegram Bot Setup', subtitle: 'Connect VAF to Telegram' },
    { id: 'create', title: 'Create Bot', subtitle: 'Get your bot token' },
    { id: 'token', title: 'Enter Token', subtitle: 'Paste your bot token' },
    { id: 'verify', title: 'Verify Bot', subtitle: 'Confirm the bot works' },
    { id: 'whitelist', title: 'Whitelist', subtitle: 'Add your Telegram' },
    { id: 'complete', title: 'Complete', subtitle: 'Setup finished!' },
];

export default function TelegramSetupWizard({ isOpen, onClose, onComplete, existingConfig }: TelegramSetupWizardProps) {
    const [currentStep, setCurrentStep] = useState(0);
    const [botToken, setBotToken] = useState(existingConfig?.bot_token || '');
    const [verificationCode, setVerificationCode] = useState('');
    const [isVerifying, setIsVerifying] = useState(false);
    const [verificationStatus, setVerificationStatus] = useState<'pending' | 'waiting' | 'success' | 'error'>('pending');
    const [errorMessage, setErrorMessage] = useState('');
    const [telegramUser, setTelegramUser] = useState<{ id: string; username?: string } | null>(null);
    const [copied, setCopied] = useState(false);
    const [whitelistAdded, setWhitelistAdded] = useState(false);
    const [whitelistError, setWhitelistError] = useState('');
    const [isAddingWhitelist, setIsAddingWhitelist] = useState(false);

    useEffect(() => {
        if (currentStep === 3 && !verificationCode) {
            const code = Math.floor(100000 + Math.random() * 900000).toString();
            setVerificationCode(code);
        }
    }, [currentStep, verificationCode]);

    const handleCopyCode = () => {
        navigator.clipboard.writeText(verificationCode);
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
    };

    const handleStartVerification = async () => {
        const looksLikeInstructions =
            botToken.length > 60 ||
            /Waiting for verification|Send the code|to your bot in Telegram/i.test(botToken);
        if (looksLikeInstructions) {
            setVerificationStatus('error');
            setErrorMessage(
                'That looks like the verification instructions, not your bot token. Go back to step 2 (Enter Token) and paste only the token from BotFather (e.g. 123456789:ABC...).'
            );
            return;
        }

        setIsVerifying(true);
        setVerificationStatus('waiting');
        setErrorMessage('');

        try {
            const response = await fetch(api('api/telegram/start-verification'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({
                    bot_token: botToken,
                    verification_code: verificationCode
                }),
            });

            if (!response.ok) {
                const err = await response.json();
                throw new Error(err.detail || 'Failed to start verification');
            }

            const pollInterval = setInterval(async () => {
                try {
                    const statusRes = await fetch(api('api/telegram/verification-status'), { credentials: 'include' });
                    const status = await statusRes.json();

                    if (status.verified) {
                        clearInterval(pollInterval);
                        setVerificationStatus('success');
                        setTelegramUser({
                            id: status.telegram_user_id,
                            username: status.telegram_username
                        });
                        setIsVerifying(false);
                    } else if (status.error) {
                        clearInterval(pollInterval);
                        setVerificationStatus('error');
                        setErrorMessage(status.error);
                        setIsVerifying(false);
                    }
                } catch {
                    // continue polling
                }
            }, 2000);

            setTimeout(() => {
                clearInterval(pollInterval);
                if (verificationStatus === 'waiting') {
                    setVerificationStatus('error');
                    setErrorMessage('Verification timed out. Please try again.');
                    setIsVerifying(false);
                }
            }, 300000);

        } catch (error: unknown) {
            setVerificationStatus('error');
            setErrorMessage(error instanceof Error ? error.message : 'Failed to connect');
            setIsVerifying(false);
        }
    };

    const handleAddToWhitelist = async () => {
        if (!telegramUser?.id) return;
        setIsAddingWhitelist(true);
        setWhitelistError('');
        try {
            const response = await fetch(api('api/telegram/whitelist-add'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({
                    telegram_user_id: telegramUser.id,
                    telegram_username: telegramUser.username || undefined
                }),
            });
            if (!response.ok) {
                const err = await response.json();
                throw new Error(err.detail || 'Failed to add to whitelist');
            }
            setWhitelistAdded(true);
        } catch (error: unknown) {
            setWhitelistError(error instanceof Error ? error.message : 'Failed to add');
        } finally {
            setIsAddingWhitelist(false);
        }
    };

    const handleComplete = () => {
        onComplete({
            bot_token: botToken,
            verified: true,
            enabled: true,
        });
        onClose();
    };

    const canProceed = () => {
        switch (currentStep) {
            case 0: return true;
            case 1: return true;
            case 2: return botToken.length > 30;
            case 3: return verificationStatus === 'success';
            case 4: return whitelistAdded;
            case 5: return true;
            default: return false;
        }
    };

    const nextStep = () => {
        if (canProceed() && currentStep < STEPS.length - 1) setCurrentStep(currentStep + 1);
        else if (currentStep === 4 && !canProceed() && whitelistAdded) setCurrentStep(5);
        else if (currentStep < STEPS.length - 1) setCurrentStep(currentStep + 1);
    };

    const prevStep = () => {
        if (currentStep > 0) setCurrentStep(currentStep - 1);
    };

    if (!isOpen) return null;

    return (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 backdrop-blur-sm max-md:p-0">
            <div className="bg-white rounded-2xl shadow-2xl w-full max-w-2xl mx-4 overflow-hidden border border-gray-200 max-md:max-w-none max-md:mx-0 max-md:h-[100dvh] max-md:rounded-none max-md:border-0 max-md:flex max-md:flex-col">
                <div className="flex items-center justify-between p-6 border-b border-gray-200 bg-gray-50 max-md:p-4 max-md:shrink-0">
                    <div className="flex items-center gap-3">
                        <div className="w-10 h-10 rounded-xl bg-gray-900 dark:bg-[#2e2e2e] flex items-center justify-center">
                            <MessageCircle className="w-5 h-5 text-white" />
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
                                    {idx < currentStep ? <Check className="w-4 h-4" /> : idx + 1}
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

                <div className="p-6 min-h-[350px] max-md:min-h-0 max-md:flex-1 max-md:overflow-y-auto max-md:p-4">
                    {/* Step 0: Intro */}
                    {currentStep === 0 && (
                        <div className="space-y-6">
                            <div className="text-center py-8">
                                <div className="w-20 h-20 mx-auto rounded-2xl bg-gray-900 dark:bg-[#2e2e2e] flex items-center justify-center mb-4">
                                    <MessageCircle className="w-10 h-10 text-white" />
                                </div>
                                <h3 className="text-2xl font-bold text-gray-900 mb-2">Connect Telegram to VAF</h3>
                                <p className="text-gray-500 max-w-md mx-auto">
                                    Use VAF from Telegram and let VAF reach you there. Send messages, get responses in real time, and keep your data scoped to your account.
                                </p>
                            </div>
                            <div className="grid grid-cols-2 gap-4">
                                <div className="p-4 rounded-xl bg-gray-50 border border-gray-200">
                                    <Shield className="w-8 h-8 text-green-600 mb-2" />
                                    <h4 className="font-semibold text-gray-900">Secure</h4>
                                    <p className="text-sm text-gray-500">Only whitelisted users can use the bot</p>
                                </div>
                                <div className="p-4 rounded-xl bg-gray-50 border border-gray-200">
                                    <MessageCircle className="w-8 h-8 text-gray-700 mb-2" />
                                    <h4 className="font-semibold text-gray-900">Real-time</h4>
                                    <p className="text-sm text-gray-500">Same experience as the Web UI</p>
                                </div>
                            </div>
                        </div>
                    )}

                    {/* Step 1: Create Bot */}
                    {currentStep === 1 && (
                        <div className="space-y-6">
                            <h3 className="text-lg font-semibold text-gray-900">Create a Telegram Bot</h3>
                            <ol className="space-y-4">
                                <li className="flex gap-3">
                                    <span className="w-6 h-6 rounded-full bg-gray-900 dark:bg-[#2e2e2e] text-white text-sm flex items-center justify-center flex-shrink-0">1</span>
                                    <div>
                                        <p className="text-gray-900">Open Telegram and search for BotFather</p>
                                        <a href="https://t.me/BotFather" target="_blank" rel="noopener noreferrer" className="text-gray-600 hover:text-gray-900 flex items-center gap-1 text-sm underline">
                                            t.me/BotFather <ExternalLink className="w-3 h-3" />
                                        </a>
                                    </div>
                                </li>
                                <li className="flex gap-3">
                                    <span className="w-6 h-6 rounded-full bg-gray-900 dark:bg-[#2e2e2e] text-white text-sm flex items-center justify-center flex-shrink-0">2</span>
                                    <p className="text-gray-900">Send <code className="bg-gray-100 px-1 rounded">/newbot</code> and follow the prompts (name and username)</p>
                                </li>
                                <li className="flex gap-3">
                                    <span className="w-6 h-6 rounded-full bg-gray-900 dark:bg-[#2e2e2e] text-white text-sm flex items-center justify-center flex-shrink-0">3</span>
                                    <p className="text-gray-900">Copy the bot token BotFather gives you (keep it secret)</p>
                                </li>
                            </ol>
                        </div>
                    )}

                    {/* Step 2: Enter Token */}
                    {currentStep === 2 && (
                        <div className="space-y-6">
                            <h3 className="text-lg font-semibold text-gray-900">Enter Your Bot Token</h3>
                            <p className="text-gray-500">Paste the bot token from BotFather.</p>
                            <div className="space-y-2">
                                <label className="text-sm text-gray-600">Bot Token</label>
                                <input
                                    type="password"
                                    value={botToken}
                                    onChange={(e) => setBotToken(e.target.value)}
                                    placeholder="e.g. 123456789:ABCdefGHI..."
                                    className="w-full px-4 py-3 rounded-xl bg-white border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-transparent"
                                />
                                <p className="text-xs text-gray-400">Paste only the token from BotFather. Do not paste the verification text (&quot;Send the code...&quot;) from the next step.</p>
                            </div>
                            {botToken.length > 0 && botToken.length < 30 && (
                                <div className="flex items-center gap-2 text-amber-600 text-sm">
                                    <AlertCircle className="w-4 h-4" />
                                    Token seems too short. Paste the full token.
                                </div>
                            )}
                            {botToken.length >= 30 && (
                                <div className="flex items-center gap-2 text-green-600 text-sm">
                                    <CheckCircle2 className="w-4 h-4" />
                                    Token format looks correct.
                                </div>
                            )}
                        </div>
                    )}

                    {/* Step 3: Verify Bot */}
                    {currentStep === 3 && (
                        <div className="space-y-6">
                            <h3 className="text-lg font-semibold text-gray-900">Verify Your Bot</h3>
                            <p className="text-gray-500">Send only the 6-digit code below to your bot in a Telegram DM. Do not paste this page&apos;s text into the token field (step 2).</p>

                            {verificationStatus === 'pending' && (
                                <>
                                    <div className="p-6 rounded-xl bg-gray-50 border border-gray-200 text-center">
                                        <p className="text-sm text-gray-500 mb-2">Your Verification Code</p>
                                        <div className="flex items-center justify-center gap-2">
                                            <span className="text-4xl font-mono font-bold text-gray-900 tracking-widest">{verificationCode}</span>
                                            <button onClick={handleCopyCode} className="p-2 hover:bg-gray-200 rounded-lg transition-colors">
                                                {copied ? <Check className="w-5 h-5 text-green-600" /> : <Copy className="w-5 h-5 text-gray-500" />}
                                            </button>
                                        </div>
                                    </div>
                                    <ol className="space-y-2 text-sm text-gray-500">
                                        <li>1. Open Telegram and start a chat with your bot</li>
                                        <li>2. Send the verification code: <code className="text-gray-900 bg-gray-100 px-1 rounded">{verificationCode}</code></li>
                                        <li>3. Click &quot;Start Verification&quot; below</li>
                                    </ol>
                                    <button
                                        onClick={handleStartVerification}
                                        className="w-full py-3 rounded-xl bg-gray-900 hover:bg-gray-800 text-white font-medium transition-colors dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-[#f5f5f5] dark:shadow-none"
                                    >
                                        Start Verification
                                    </button>
                                </>
                            )}

                            {verificationStatus === 'waiting' && (
                                <div className="text-center space-y-6">
                                    <Loader2 className="w-12 h-12 text-gray-600 animate-spin mx-auto" />
                                    <p className="text-gray-900 font-medium">Waiting for verification…</p>
                                    <p className="text-sm text-gray-500">Send this code to your bot in Telegram:</p>
                                    <div className="p-6 rounded-xl bg-gray-50 border border-gray-200 text-center">
                                        <div className="flex items-center justify-center gap-2">
                                            <span className="text-4xl font-mono font-bold text-gray-900 tracking-widest">
                                                {verificationCode}
                                            </span>
                                            <button
                                                onClick={handleCopyCode}
                                                className="p-2 hover:bg-gray-200 rounded-lg transition-colors"
                                                title="Copy code"
                                            >
                                                {copied ? <Check className="w-5 h-5 text-green-600" /> : <Copy className="w-5 h-5 text-gray-500" />}
                                            </button>
                                        </div>
                                    </div>
                                </div>
                            )}

                            {verificationStatus === 'success' && telegramUser && (
                                <div className="text-center py-8">
                                    <div className="w-16 h-16 rounded-full bg-green-500 flex items-center justify-center mx-auto mb-4">
                                        <Check className="w-8 h-8 text-white" />
                                    </div>
                                    <p className="text-gray-900 font-medium text-lg">Verification successful</p>
                                    <p className="text-gray-500 mt-2">Telegram: {telegramUser.username ? `@${telegramUser.username}` : `ID ${telegramUser.id}`}</p>
                                </div>
                            )}

                            {verificationStatus === 'error' && (
                                <div className="text-center py-8">
                                    <div className="w-16 h-16 rounded-full bg-red-500 flex items-center justify-center mx-auto mb-4">
                                        <X className="w-8 h-8 text-white" />
                                    </div>
                                    <p className="text-gray-900 font-medium text-lg">Verification failed</p>
                                    <p className="text-red-600 mt-2">{errorMessage}</p>
                                    <button onClick={() => setVerificationStatus('pending')} className="mt-4 px-4 py-2 rounded-lg bg-gray-100 hover:bg-gray-200 text-gray-900 transition-colors">
                                        Try Again
                                    </button>
                                </div>
                            )}
                        </div>
                    )}

                    {/* Step 4: Whitelist */}
                    {currentStep === 4 && (
                        <div className="space-y-6">
                            <h3 className="text-lg font-semibold text-gray-900">Add Your Telegram to the Whitelist</h3>
                            <p className="text-gray-500">Add your Telegram so VAF can reach you and you can use VAF from Telegram.</p>

                            <div className="p-4 rounded-xl bg-amber-50 border border-amber-200">
                                <p className="text-sm font-medium text-amber-800">
                                    Please enter your own number or username, not someone else&apos;s!
                                </p>
                            </div>

                            {telegramUser && (
                                <div className="p-4 rounded-xl bg-gray-50 border border-gray-200">
                                    <p className="text-sm text-gray-500 mb-1">Verified Telegram account</p>
                                    <p className="text-gray-900 font-medium">{telegramUser.username ? `@${telegramUser.username}` : `ID ${telegramUser.id}`}</p>
                                    {!whitelistAdded ? (
                                        <button
                                            onClick={handleAddToWhitelist}
                                            disabled={isAddingWhitelist}
                                            className="mt-3 flex items-center gap-2 px-4 py-2 rounded-lg bg-gray-900 hover:bg-gray-800 text-white font-medium disabled:opacity-50 dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-[#f5f5f5] dark:shadow-none"
                                        >
                                            {isAddingWhitelist ? <Loader2 className="w-4 h-4 animate-spin" /> : <UserPlus className="w-4 h-4" />}
                                            Add to whitelist
                                        </button>
                                    ) : (
                                        <div className="mt-3 flex items-center gap-2 text-green-600">
                                            <CheckCircle2 className="w-5 h-5" />
                                            Added to whitelist
                                        </div>
                                    )}
                                    {whitelistError && <p className="text-red-600 text-sm mt-2">{whitelistError}</p>}
                                </div>
                            )}
                        </div>
                    )}

                    {/* Step 5: Complete */}
                    {currentStep === 5 && (
                        <div className="text-center py-8">
                            <div className="w-20 h-20 mx-auto rounded-2xl bg-green-500 flex items-center justify-center mb-4">
                                <Check className="w-10 h-10 text-white" />
                            </div>
                            <h3 className="text-2xl font-bold text-gray-900 mb-2">Setup complete!</h3>
                            <p className="text-gray-500 max-w-md mx-auto mb-6">
                                Your Telegram bot is connected to VAF. You can chat with your agent from Telegram and VAF can reach you there.
                            </p>
                            {telegramUser && (
                                <div className="p-4 rounded-xl bg-gray-50 border border-gray-200 inline-block">
                                    <p className="text-sm text-gray-500">Whitelisted</p>
                                    <p className="text-lg text-gray-900 font-medium">{telegramUser.username ? `@${telegramUser.username}` : `ID ${telegramUser.id}`}</p>
                                </div>
                            )}
                        </div>
                    )}
                </div>

                <div className="flex items-center justify-between p-6 border-t border-gray-200 bg-gray-50 max-md:p-4 max-md:shrink-0">
                    <button
                        onClick={prevStep}
                        disabled={currentStep === 0}
                        className={cn(
                            "flex items-center gap-2 px-4 py-2 rounded-lg transition-colors",
                            currentStep === 0 ? "text-gray-300 cursor-not-allowed" : "text-gray-600 hover:bg-gray-200"
                        )}
                    >
                        <ChevronLeft className="w-4 h-4" /> Back
                    </button>

                    {currentStep < STEPS.length - 1 ? (
                        <button
                            onClick={nextStep}
                            disabled={currentStep === 4 ? !whitelistAdded : !canProceed()}
                            className={cn(
                                "flex items-center gap-2 px-6 py-2 rounded-lg font-medium transition-colors",
                                (currentStep === 4 ? whitelistAdded : canProceed())
                                    ? "bg-gray-900 hover:bg-gray-800 text-white dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-[#f5f5f5] dark:shadow-none"
                                    : "bg-gray-200 text-gray-400 cursor-not-allowed"
                            )}
                        >
                            Next <ChevronRight className="w-4 h-4" />
                        </button>
                    ) : (
                        <button
                            onClick={handleComplete}
                            className="flex items-center gap-2 px-6 py-2 rounded-lg font-medium bg-green-500 hover:bg-green-600 text-white transition-colors"
                        >
                            <Check className="w-4 h-4" /> Finish Setup
                        </button>
                    )}
                </div>
            </div>
        </div>
    );
}
