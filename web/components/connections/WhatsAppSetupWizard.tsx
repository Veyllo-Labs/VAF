'use client';

import React, { useState, useEffect, useCallback } from 'react';
import { X, Loader2, CheckCircle2, AlertCircle, Phone } from 'lucide-react';
import { cn } from '@/lib/utils';

const api = (path: string) => path.startsWith('/') ? path : `/${path}`;

interface WhatsAppSetupWizardProps {
    isOpen: boolean;
    onClose: () => void;
    onComplete: () => void;
}

export default function WhatsAppSetupWizard({ isOpen, onClose, onComplete }: WhatsAppSetupWizardProps) {
    const [step, setStep] = useState<'qr' | 'phone' | 'done' | 'error'>('qr');
    const [qrData, setQrData] = useState<string | null>(null);
    const [phoneNumber, setPhoneNumber] = useState('');
    const [error, setError] = useState('');
    const [waitingMsg, setWaitingMsg] = useState('');
    const [isAddingWhitelist, setIsAddingWhitelist] = useState(false);

    const pollQr = useCallback(async () => {
        try {
            const res = await fetch(api('api/whatsapp/qr'), { credentials: 'include' });
            if (!res.ok) {
                setError(res.status >= 500 ? 'Backend not reachable – is VAF running?' : `Server error (${res.status})`);
                return;
            }
            const data = await res.json();
            if (data.status === 'connected') {
                setQrData(null);
                setWaitingMsg('');
                if (data.phone) {
                    setPhoneNumber(data.phone);
                    try {
                        const addRes = await fetch(api('api/whatsapp/whitelist/add'), {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            credentials: 'include',
                            body: JSON.stringify({ phone_number: data.phone }),
                        });
                        if (addRes.ok) {
                            setStep('done');
                            try {
                                await fetch(api('api/whatsapp/start'), { method: 'POST', credentials: 'include' });
                            } catch (_) {}
                            setTimeout(() => { onComplete(); onClose(); }, 1500);
                        } else {
                            setStep('phone');
                        }
                    } catch (_) {
                        setStep('phone');
                    }
                } else {
                    setStep('phone');
                }
                return;
            }
            if (data.status === 'qr' && data.qr) {
                setQrData(data.qr);
                setError('');
                setWaitingMsg('');
                return;
            }
            if (data.status === 'error' && data.error) {
                setError(data.error);
                setStep('error');
                setWaitingMsg('');
                return;
            }
            if (data.status === 'waiting') {
                setWaitingMsg(data.message || 'Connecting to WhatsApp server…');
            }
        } catch (e) {
            setError(String(e));
        }
    }, []);

    const startQrFlow = async (resetFirst = false) => {
        setStep('qr');
        setError('');
        setQrData(null);
        setWaitingMsg('');
        try {
            if (resetFirst) {
                await fetch(api('api/whatsapp/qr/reset'), { method: 'POST', credentials: 'include' });
            }
            await fetch(api('api/whatsapp/qr/start'), { method: 'POST', credentials: 'include' });
            pollQr();
        } catch (e) {
            setError(String(e));
        }
    };

    useEffect(() => {
        if (!isOpen) return;
        let cancelled = false;
        (async () => {
            try {
                const [statusRes, configRes] = await Promise.all([
                    fetch(api('api/whatsapp/status'), { credentials: 'include' }),
                    fetch(api('api/whatsapp/config'), { credentials: 'include' }),
                ]);
                if (cancelled) return;
                const status = await statusRes.json();
                const config = configRes.ok ? await configRes.json() : {};
                const whitelist = config?.whitelist || [];
                const hasWhitelist = Array.isArray(whitelist) && whitelist.some((e: any) => e?.phone_number);
                if (status.linked && hasWhitelist) {
                    setStep('phone');
                    setQrData(null);
                    setError('');
                    setWaitingMsg('');
                    return;
                }
                if (status.linked && !hasWhitelist) {
                    await fetch(api('api/whatsapp/qr/reset'), { method: 'POST', credentials: 'include' });
                }
            } catch (_) {}
            if (!cancelled) startQrFlow();
        })();
        return () => { cancelled = true; };
    }, [isOpen]);

    useEffect(() => {
        // Only poll while the wizard is actually open. The wizard is always mounted
        // inside SettingsModal (hidden via `if (!isOpen) return null`), and `step`
        // defaults to 'qr', so without the isOpen guard the hidden component would
        // poll /api/whatsapp/qr every ~1.5s for the whole time Settings is open —
        // even when the user never opened WhatsApp setup.
        if (!isOpen || step !== 'qr') return;
        const t = setInterval(pollQr, qrData ? 2500 : 1500);
        return () => clearInterval(t);
    }, [isOpen, step, qrData, pollQr]);

    const handleAddWhitelist = async () => {
        const phone = phoneNumber.trim().replace(/\s/g, '');
        if (!phone || phone.length < 10) {
            setError('Enter a valid phone number (e.g. +49123456789)');
            return;
        }
        setIsAddingWhitelist(true);
        setError('');
        try {
            const res = await fetch(api('api/whatsapp/whitelist/add'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({ phone_number: phone.startsWith('+') ? phone : `+${phone}` }),
            });
            if (!res.ok) {
                const err = await res.json();
                throw new Error(err.detail || 'Failed to add whitelist');
            }
            setStep('done');
            setTimeout(() => {
                onComplete();
                onClose();
            }, 1500);
        } catch (e) {
            setError(String(e));
        } finally {
            setIsAddingWhitelist(false);
        }
    };

    if (!isOpen) return null;

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
            <div className="bg-white rounded-2xl shadow-xl max-w-md w-full p-6">
                <div className="flex items-center justify-between mb-6">
                    <h3 className="text-lg font-semibold text-gray-900">WhatsApp Setup</h3>
                    <button onClick={onClose} className="p-1 hover:bg-gray-100 rounded-lg">
                        <X className="w-5 h-5 text-gray-500" />
                    </button>
                </div>

                {step === 'qr' && (
                    <div className="space-y-4">
                        <p className="text-sm text-gray-600">
                            Scan this QR code with WhatsApp on your phone: Open WhatsApp → Settings → Linked Devices → Link a Device.
                        </p>
                        <p className="text-xs text-gray-500">
                            If your phone shows &quot;logging in&quot; for a long time, close this window and reopen it – the link may have succeeded in the background.
                        </p>
                        {qrData ? (
                            <div className="flex justify-center p-4 bg-white border rounded-xl">
                                <img
                                    src={`https://api.qrserver.com/v1/create-qr-code/?size=256x256&data=${encodeURIComponent(qrData)}`}
                                    alt="WhatsApp QR Code"
                                    className="w-64 h-64"
                                />
                            </div>
                        ) : (
                            <div className="flex flex-col items-center justify-center p-8 gap-3">
                                <Loader2 className="w-12 h-12 text-gray-400 animate-spin" />
                                {waitingMsg && <p className="text-sm text-gray-500 text-center">{waitingMsg}</p>}
                            </div>
                        )}
                        {error && (
                            <div className="flex items-center gap-2 p-3 bg-red-50 text-red-700 rounded-lg text-sm">
                                <AlertCircle className="w-4 h-4 flex-shrink-0" />
                                {error}
                            </div>
                        )}
                    </div>
                )}

                {step === 'phone' && (
                    <div className="space-y-4">
                        <div className="flex items-center gap-2 p-3 bg-green-50 text-green-700 rounded-lg">
                            <CheckCircle2 className="w-5 h-5" />
                            <span>WhatsApp linked successfully.</span>
                        </div>
                        <p className="text-sm text-gray-600">
                            Add your phone number so VAF can route messages to you. Use E.164 format (e.g. +49123456789).
                        </p>
                        <p className="text-xs text-gray-500">
                            Unlinked on your phone?{' '}
                            <button
                                type="button"
                                onClick={() => startQrFlow(true)}
                                className="text-blue-600 hover:underline"
                            >
                                Re-link (reset & scan new QR)
                            </button>
                        </p>
                        <div className="p-3 rounded-lg bg-amber-50 border border-amber-200">
                            <p className="text-xs text-amber-800">
                                <strong>Read-only:</strong> VAF only replies to numbers in the whitelist. Your contacts will not be messaged.
                            </p>
                        </div>
                        <div className="flex gap-2">
                            <input
                                type="tel"
                                placeholder="+49 123 456789"
                                value={phoneNumber}
                                onChange={(e) => setPhoneNumber(e.target.value)}
                                className="flex-1 px-3 py-2 border border-gray-200 rounded-lg focus:ring-2 focus:ring-green-500 focus:border-transparent"
                            />
                            <button
                                onClick={handleAddWhitelist}
                                disabled={isAddingWhitelist}
                                className="px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 disabled:opacity-50 flex items-center gap-2"
                            >
                                {isAddingWhitelist ? <Loader2 className="w-4 h-4 animate-spin" /> : <Phone className="w-4 h-4" />}
                                Add
                            </button>
                        </div>
                        {error && (
                            <div className="flex items-center gap-2 p-3 bg-red-50 text-red-700 rounded-lg text-sm">
                                <AlertCircle className="w-4 h-4 flex-shrink-0" />
                                {error}
                            </div>
                        )}
                    </div>
                )}

                {step === 'done' && (
                    <div className="flex flex-col items-center gap-4 py-6">
                        <CheckCircle2 className="w-16 h-16 text-green-600" />
                        <p className="text-center text-gray-700">WhatsApp setup complete. You can now enable the connection.</p>
                    </div>
                )}

                {step === 'error' && (
                    <div className="space-y-4">
                        <div className="flex items-center gap-2 p-3 bg-red-50 text-red-700 rounded-lg">
                            <AlertCircle className="w-5 h-5 flex-shrink-0" />
                            {error}
                        </div>
                        <p className="text-xs text-gray-500">
                            Debug log: <code className="bg-gray-100 px-1 rounded">logs/whatsapp_qr.log</code> (in VAF folder)
                        </p>
                        <p className="text-sm text-gray-600">
                            When you see "Logged out": Clear auth data and display a new QR code.
                        </p>
                        <button
                            onClick={() => startQrFlow(true)}
                            className="w-full py-2 bg-gray-900 text-white rounded-lg hover:bg-gray-800"
                        >
                            Reset & get new QR code
                        </button>
                    </div>
                )}
            </div>
        </div>
    );
}
