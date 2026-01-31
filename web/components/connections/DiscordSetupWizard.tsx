'use client';

import React, { useState, useEffect } from 'react';
import {
    X, ChevronRight, ChevronLeft, Check, Copy, ExternalLink,
    MessageCircle, Shield, Loader2, AlertCircle, CheckCircle2
} from 'lucide-react';
import { cn } from '@/lib/utils';

interface DiscordSetupWizardProps {
    isOpen: boolean;
    onClose: () => void;
    onComplete: (config: DiscordConfig) => void;
    existingConfig?: DiscordConfig;
}

export interface DiscordConfig {
    bot_token: string;
    admin_user_id: string;
    admin_username: string;
    verified: boolean;
    enabled: boolean;
}

const STEPS = [
    { id: 'intro', title: 'Discord Bot Setup', subtitle: 'Connect VAF to Discord' },
    { id: 'create', title: 'Create Bot', subtitle: 'Get your bot token' },
    { id: 'token', title: 'Enter Token', subtitle: 'Paste your bot token' },
    { id: 'verify', title: 'Verify Admin', subtitle: 'Confirm your identity' },
    { id: 'complete', title: 'Complete', subtitle: 'Setup finished!' },
];

export default function DiscordSetupWizard({ isOpen, onClose, onComplete, existingConfig }: DiscordSetupWizardProps) {
    const [currentStep, setCurrentStep] = useState(0);
    const [botToken, setBotToken] = useState(existingConfig?.bot_token || '');
    const [verificationCode, setVerificationCode] = useState('');
    const [userInputCode, setUserInputCode] = useState('');
    const [isVerifying, setIsVerifying] = useState(false);
    const [verificationStatus, setVerificationStatus] = useState<'pending' | 'waiting' | 'success' | 'error'>('pending');
    const [errorMessage, setErrorMessage] = useState('');
    const [adminInfo, setAdminInfo] = useState<{ id: string; username: string } | null>(null);
    const [copied, setCopied] = useState(false);

    // Generate random 6-digit code
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
        setIsVerifying(true);
        setVerificationStatus('waiting');
        setErrorMessage('');

        try {
            // Start the bot and wait for verification message
            const response = await fetch('http://localhost:8001/api/discord/start-verification', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    bot_token: botToken,
                    verification_code: verificationCode
                }),
            });

            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.detail || 'Failed to start verification');
            }

            // Poll for verification status
            const pollInterval = setInterval(async () => {
                try {
                    const statusRes = await fetch('http://localhost:8001/api/discord/verification-status');
                    const status = await statusRes.json();

                    if (status.verified) {
                        clearInterval(pollInterval);
                        setVerificationStatus('success');
                        setAdminInfo({ id: status.admin_user_id, username: status.admin_username });
                        setIsVerifying(false);
                    } else if (status.error) {
                        clearInterval(pollInterval);
                        setVerificationStatus('error');
                        setErrorMessage(status.error);
                        setIsVerifying(false);
                    }
                } catch (e) {
                    // Continue polling
                }
            }, 2000);

            // Timeout after 5 minutes
            setTimeout(() => {
                clearInterval(pollInterval);
                if (verificationStatus === 'waiting') {
                    setVerificationStatus('error');
                    setErrorMessage('Verification timed out. Please try again.');
                    setIsVerifying(false);
                }
            }, 300000);

        } catch (error: any) {
            setVerificationStatus('error');
            setErrorMessage(error.message || 'Failed to connect to Discord');
            setIsVerifying(false);
        }
    };

    const handleComplete = () => {
        if (adminInfo) {
            onComplete({
                bot_token: botToken,
                admin_user_id: adminInfo.id,
                admin_username: adminInfo.username,
                verified: true,
                enabled: true,
            });
        }
        onClose();
    };

    const canProceed = () => {
        switch (currentStep) {
            case 0: return true;
            case 1: return true;
            case 2: return botToken.length > 50; // Discord tokens are long
            case 3: return verificationStatus === 'success';
            case 4: return true;
            default: return false;
        }
    };

    const nextStep = () => {
        if (canProceed() && currentStep < STEPS.length - 1) {
            setCurrentStep(currentStep + 1);
        }
    };

    const prevStep = () => {
        if (currentStep > 0) {
            setCurrentStep(currentStep - 1);
        }
    };

    if (!isOpen) return null;

    return (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 backdrop-blur-sm">
            <div className="bg-white rounded-2xl shadow-2xl w-full max-w-2xl mx-4 overflow-hidden border border-gray-200">
                {/* Header */}
                <div className="flex items-center justify-between p-6 border-b border-gray-200 bg-gray-50">
                    <div className="flex items-center gap-3">
                        <div className="w-10 h-10 rounded-xl bg-gray-900 flex items-center justify-center">
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

                {/* Progress Bar */}
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

                {/* Content */}
                <div className="p-6 min-h-[350px]">
                    {/* Step 0: Intro */}
                    {currentStep === 0 && (
                        <div className="space-y-6">
                            <div className="text-center py-8">
                                <div className="w-20 h-20 mx-auto rounded-2xl bg-gray-900 flex items-center justify-center mb-4">
                                    <MessageCircle className="w-10 h-10 text-white" />
                                </div>
                                <h3 className="text-2xl font-bold text-gray-900 mb-2">Connect Discord to VAF</h3>
                                <p className="text-gray-500 max-w-md mx-auto">
                                    Chat with your VAF agent directly through Discord. Send messages,
                                    execute commands, and get responses in real-time.
                                </p>
                            </div>
                            <div className="grid grid-cols-2 gap-4">
                                <div className="p-4 rounded-xl bg-gray-50 border border-gray-200">
                                    <Shield className="w-8 h-8 text-green-600 mb-2" />
                                    <h4 className="font-semibold text-gray-900">Secure</h4>
                                    <p className="text-sm text-gray-500">Only verified admins can control the bot</p>
                                </div>
                                <div className="p-4 rounded-xl bg-gray-50 border border-gray-200">
                                    <MessageCircle className="w-8 h-8 text-gray-700 mb-2" />
                                    <h4 className="font-semibold text-gray-900">Real-time</h4>
                                    <p className="text-sm text-gray-500">Instant responses via WebSocket</p>
                                </div>
                            </div>
                        </div>
                    )}

                    {/* Step 1: Create Bot */}
                    {currentStep === 1 && (
                        <div className="space-y-6">
                            <h3 className="text-lg font-semibold text-gray-900">Create a Discord Bot</h3>
                            <ol className="space-y-4">
                                <li className="flex gap-3">
                                    <span className="w-6 h-6 rounded-full bg-gray-900 text-white text-sm flex items-center justify-center flex-shrink-0">1</span>
                                    <div>
                                        <p className="text-gray-900">Go to Discord Developer Portal</p>
                                        <a
                                            href="https://discord.com/developers/applications"
                                            target="_blank"
                                            rel="noopener noreferrer"
                                            className="text-gray-600 hover:text-gray-900 flex items-center gap-1 text-sm underline"
                                        >
                                            discord.com/developers/applications <ExternalLink className="w-3 h-3" />
                                        </a>
                                    </div>
                                </li>
                                <li className="flex gap-3">
                                    <span className="w-6 h-6 rounded-full bg-gray-900 text-white text-sm flex items-center justify-center flex-shrink-0">2</span>
                                    <div>
                                        <p className="text-gray-900">Click "New Application" and give it a name (e.g., "VAF Agent")</p>
                                    </div>
                                </li>
                                <li className="flex gap-3">
                                    <span className="w-6 h-6 rounded-full bg-gray-900 text-white text-sm flex items-center justify-center flex-shrink-0">3</span>
                                    <div>
                                        <p className="text-gray-900">Go to "Bot" in the sidebar and click "Add Bot"</p>
                                    </div>
                                </li>
                                <li className="flex gap-3">
                                    <span className="w-6 h-6 rounded-full bg-gray-900 text-white text-sm flex items-center justify-center flex-shrink-0">4</span>
                                    <div>
                                        <p className="text-gray-900">Under "Privileged Gateway Intents", enable:</p>
                                        <ul className="text-sm text-gray-500 mt-1 space-y-1">
                                            <li>- Message Content Intent</li>
                                            <li>- Server Members Intent (optional)</li>
                                        </ul>
                                    </div>
                                </li>
                                <li className="flex gap-3">
                                    <span className="w-6 h-6 rounded-full bg-gray-900 text-white text-sm flex items-center justify-center flex-shrink-0">5</span>
                                    <div>
                                        <p className="text-gray-900">Click "Reset Token" and copy the token</p>
                                        <p className="text-sm text-amber-600 mt-1">Keep this token secret!</p>
                                    </div>
                                </li>
                            </ol>
                            <div className="p-4 rounded-xl bg-amber-50 border border-amber-200">
                                <p className="text-sm text-amber-800">
                                    <strong>Important:</strong> Also go to OAuth2 → URL Generator, select "bot" scope with
                                    "Send Messages" and "Read Message History" permissions, then use the generated URL to invite the bot to your server.
                                </p>
                            </div>
                        </div>
                    )}

                    {/* Step 2: Enter Token */}
                    {currentStep === 2 && (
                        <div className="space-y-6">
                            <h3 className="text-lg font-semibold text-gray-900">Enter Your Bot Token</h3>
                            <p className="text-gray-500">
                                Paste the bot token you copied from the Discord Developer Portal.
                            </p>
                            <div className="space-y-2">
                                <label className="text-sm text-gray-600">Bot Token</label>
                                <input
                                    type="password"
                                    value={botToken}
                                    onChange={(e) => setBotToken(e.target.value)}
                                    placeholder="Paste your Discord bot token here..."
                                    className="w-full px-4 py-3 rounded-xl bg-white border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-transparent"
                                />
                                <p className="text-xs text-gray-400">
                                    Your token is stored locally and never sent to external servers.
                                </p>
                            </div>
                            {botToken.length > 0 && botToken.length < 50 && (
                                <div className="flex items-center gap-2 text-amber-600 text-sm">
                                    <AlertCircle className="w-4 h-4" />
                                    Token seems too short. Please check and paste the complete token.
                                </div>
                            )}
                            {botToken.length >= 50 && (
                                <div className="flex items-center gap-2 text-green-600 text-sm">
                                    <CheckCircle2 className="w-4 h-4" />
                                    Token format looks correct!
                                </div>
                            )}
                        </div>
                    )}

                    {/* Step 3: Verify Admin */}
                    {currentStep === 3 && (
                        <div className="space-y-6">
                            <h3 className="text-lg font-semibold text-gray-900">Verify Your Identity</h3>
                            <p className="text-gray-500">
                                To ensure only you can control the bot, send the verification code to your bot via Discord DM.
                            </p>

                            {verificationStatus === 'pending' && (
                                <>
                                    <div className="p-6 rounded-xl bg-gray-50 border border-gray-200 text-center">
                                        <p className="text-sm text-gray-500 mb-2">Your Verification Code</p>
                                        <div className="flex items-center justify-center gap-2">
                                            <span className="text-4xl font-mono font-bold text-gray-900 tracking-widest">
                                                {verificationCode}
                                            </span>
                                            <button
                                                onClick={handleCopyCode}
                                                className="p-2 hover:bg-gray-200 rounded-lg transition-colors"
                                            >
                                                {copied ? <Check className="w-5 h-5 text-green-600" /> : <Copy className="w-5 h-5 text-gray-500" />}
                                            </button>
                                        </div>
                                    </div>
                                    <div className="space-y-3">
                                        <p className="text-gray-900 font-medium">Instructions:</p>
                                        <ol className="space-y-2 text-sm text-gray-500">
                                            <li>1. Open Discord and find your bot in your server</li>
                                            <li>2. Send a Direct Message (DM) to the bot</li>
                                            <li>3. Type the verification code: <code className="text-gray-900 bg-gray-100 px-1 rounded">{verificationCode}</code></li>
                                            <li>4. Click "Start Verification" below</li>
                                        </ol>
                                    </div>
                                    <button
                                        onClick={handleStartVerification}
                                        className="w-full py-3 rounded-xl bg-gray-900 hover:bg-gray-800 text-white font-medium transition-colors"
                                    >
                                        Start Verification
                                    </button>
                                </>
                            )}

                            {verificationStatus === 'waiting' && (
                                <div className="text-center py-8">
                                    <Loader2 className="w-12 h-12 text-gray-600 animate-spin mx-auto mb-4" />
                                    <p className="text-gray-900 font-medium">Waiting for verification...</p>
                                    <p className="text-sm text-gray-500 mt-2">
                                        Send the code <span className="text-gray-900 font-mono bg-gray-100 px-1 rounded">{verificationCode}</span> as a DM to your bot
                                    </p>
                                </div>
                            )}

                            {verificationStatus === 'success' && adminInfo && (
                                <div className="text-center py-8">
                                    <div className="w-16 h-16 rounded-full bg-green-500 flex items-center justify-center mx-auto mb-4">
                                        <Check className="w-8 h-8 text-white" />
                                    </div>
                                    <p className="text-gray-900 font-medium text-lg">Verification Successful!</p>
                                    <p className="text-gray-500 mt-2">
                                        Verified admin: <span className="text-gray-900 font-medium">@{adminInfo.username}</span>
                                    </p>
                                </div>
                            )}

                            {verificationStatus === 'error' && (
                                <div className="text-center py-8">
                                    <div className="w-16 h-16 rounded-full bg-red-500 flex items-center justify-center mx-auto mb-4">
                                        <X className="w-8 h-8 text-white" />
                                    </div>
                                    <p className="text-gray-900 font-medium text-lg">Verification Failed</p>
                                    <p className="text-red-600 mt-2">{errorMessage}</p>
                                    <button
                                        onClick={() => setVerificationStatus('pending')}
                                        className="mt-4 px-4 py-2 rounded-lg bg-gray-100 hover:bg-gray-200 text-gray-900 transition-colors"
                                    >
                                        Try Again
                                    </button>
                                </div>
                            )}
                        </div>
                    )}

                    {/* Step 4: Complete */}
                    {currentStep === 4 && (
                        <div className="text-center py-8">
                            <div className="w-20 h-20 mx-auto rounded-2xl bg-green-500 flex items-center justify-center mb-4">
                                <Check className="w-10 h-10 text-white" />
                            </div>
                            <h3 className="text-2xl font-bold text-gray-900 mb-2">Setup Complete!</h3>
                            <p className="text-gray-500 max-w-md mx-auto mb-6">
                                Your Discord bot is now connected to VAF. You can chat with your agent by sending messages to the bot.
                            </p>
                            {adminInfo && (
                                <div className="p-4 rounded-xl bg-gray-50 border border-gray-200 inline-block">
                                    <p className="text-sm text-gray-500">Authorized Admin</p>
                                    <p className="text-lg text-gray-900 font-medium">@{adminInfo.username}</p>
                                </div>
                            )}
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
                            onClick={nextStep}
                            disabled={!canProceed()}
                            className={cn(
                                "flex items-center gap-2 px-6 py-2 rounded-lg font-medium transition-colors",
                                canProceed()
                                    ? "bg-gray-900 hover:bg-gray-800 text-white"
                                    : "bg-gray-200 text-gray-400 cursor-not-allowed"
                            )}
                        >
                            Next
                            <ChevronRight className="w-4 h-4" />
                        </button>
                    ) : (
                        <button
                            onClick={handleComplete}
                            className="flex items-center gap-2 px-6 py-2 rounded-lg font-medium bg-green-500 hover:bg-green-600 text-white transition-colors"
                        >
                            <Check className="w-4 h-4" />
                            Finish Setup
                        </button>
                    )}
                </div>
            </div>
        </div>
    );
}
