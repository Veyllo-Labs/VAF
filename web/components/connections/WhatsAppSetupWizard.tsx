'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import React, { useState, useEffect, useCallback } from 'react';
import { useTranslations } from 'next-intl';
import { X, Loader2, CheckCircle2, AlertCircle, Phone, Bot, AlertTriangle } from 'lucide-react';

const api = (path: string) => path.startsWith('/') ? path : `/${path}`;

interface WhatsAppSetupWizardProps {
    isOpen: boolean;
    onClose: () => void;
    onComplete: () => void;
}

// The account scanned in step one is the AGENT's own WhatsApp number: the agent writes to
// contacts and other people from it, and nobody chats with the agent from that phone.
// Step two optionally registers the number the user chats FROM (their main-user number),
// which is what makes the owner reachable on WhatsApp. Nothing is added automatically.
export default function WhatsAppSetupWizard({ isOpen, onClose, onComplete }: WhatsAppSetupWizardProps) {
    const t = useTranslations('settings.whatsappWizard');
    const [step, setStep] = useState<'qr' | 'linked' | 'done' | 'error'>('qr');
    const [qrData, setQrData] = useState<string | null>(null);
    const [linkedPhone, setLinkedPhone] = useState<string | null>(null);
    const [ownerNumber, setOwnerNumber] = useState<string | null>(null);
    const [phoneNumber, setPhoneNumber] = useState('');
    const [error, setError] = useState('');
    const [waitingMsg, setWaitingMsg] = useState('');
    const [isAddingOwner, setIsAddingOwner] = useState(false);

    const finish = useCallback(() => {
        setStep('done');
        setTimeout(() => { onComplete(); onClose(); }, 1500);
    }, [onComplete, onClose]);

    const pollQr = useCallback(async () => {
        try {
            const res = await fetch(api('api/whatsapp/qr'), { credentials: 'include' });
            if (!res.ok) {
                setError(res.status >= 500 ? t('backendUnreachable') : t('serverError', { status: res.status }));
                return;
            }
            const data = await res.json();
            if (data.status === 'connected') {
                setQrData(null);
                setWaitingMsg('');
                setLinkedPhone(data.phone || null);
                // The link is live: start the bridge now so the agent number is reachable,
                // then let the user decide whether to register their own number.
                try {
                    await fetch(api('api/whatsapp/start'), { method: 'POST', credentials: 'include' });
                } catch (_) {}
                setStep('linked');
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
                setWaitingMsg(data.message || 'Connecting to WhatsApp server...');
            }
        } catch (e) {
            setError(String(e));
        }
    }, [t]);

    const startQrFlow = async (resetFirst = false) => {
        setStep('qr');
        setError('');
        setQrData(null);
        setWaitingMsg('');
        setLinkedPhone(null);
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
                const statusRes = await fetch(api('api/whatsapp/status'), { credentials: 'include' });
                if (cancelled) return;
                const status = await statusRes.json();
                if (status.linked) {
                    // Already linked: show the agent number and the owner-number step.
                    setLinkedPhone(status.linked_phone || null);
                    setOwnerNumber(status.owner_number || null);
                    setStep('linked');
                    setQrData(null);
                    setError('');
                    setWaitingMsg('');
                    return;
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
        // poll /api/whatsapp/qr every ~1.5s for the whole time Settings is open,
        // even when the user never opened WhatsApp setup.
        if (!isOpen || step !== 'qr') return;
        const t = setInterval(pollQr, qrData ? 2500 : 1500);
        return () => clearInterval(t);
    }, [isOpen, step, qrData, pollQr]);

    const handleAddOwnerNumber = async () => {
        const phone = phoneNumber.trim().replace(/\s/g, '');
        if (!phone || phone.length < 10) {
            setError(t('invalidPhone'));
            return;
        }
        setIsAddingOwner(true);
        setError('');
        try {
            const res = await fetch(api('api/whatsapp/whitelist/add'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({ phone_number: phone.startsWith('+') ? phone : `+${phone}` }),
            });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err.detail || t('registerFailed'));
            }
            finish();
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            setIsAddingOwner(false);
        }
    };

    if (!isOpen) return null;

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4 max-md:p-0">
            <div className="bg-white rounded-2xl shadow-xl max-w-md w-full p-6 max-md:max-w-none max-md:mx-0 max-md:h-[100dvh] max-md:rounded-none max-md:border-0 max-md:flex max-md:flex-col max-md:overflow-y-auto max-md:p-4">
                <div className="flex items-center justify-between mb-6">
                    <h3 className="text-lg font-semibold text-gray-900">{t('title')}</h3>
                    <button onClick={onClose} className="p-1 hover:bg-gray-100 rounded-lg">
                        <X className="w-5 h-5 text-gray-500" />
                    </button>
                </div>

                {step === 'qr' && (
                    <div className="space-y-4">
                        <div className="flex items-start gap-2 p-3 bg-gray-50 rounded-lg text-sm text-gray-700">
                            <Bot className="w-5 h-5 text-gray-500 shrink-0 mt-0.5" />
                            <p>{t('agentNumberIntro')}</p>
                        </div>
                        <div className="flex items-start gap-2 p-3 bg-amber-50 border border-amber-200 rounded-lg text-sm text-amber-800">
                            <AlertTriangle className="w-5 h-5 text-amber-600 shrink-0 mt-0.5" />
                            <p>{t('tosWarning')}</p>
                        </div>
                        <p className="text-sm text-gray-600">{t('scanInstructions')}</p>
                        <p className="text-xs text-gray-500">{t('loggingInHint')}</p>
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

                {step === 'linked' && (
                    <div className="space-y-4">
                        <div className="flex items-center gap-2 p-3 bg-green-50 text-green-700 rounded-lg">
                            <CheckCircle2 className="w-5 h-5 shrink-0" />
                            <span>
                                {linkedPhone
                                    ? t.rich('linkedWithNumber', { phone: () => <strong>{linkedPhone}</strong> })
                                    : t('linkedNoNumber')}
                            </span>
                        </div>
                        <p className="text-sm text-gray-600">{t('agentRole')}</p>
                        <p className="text-xs text-gray-500">
                            {t('wrongAccount')}{' '}
                            <button
                                type="button"
                                onClick={() => startQrFlow(true)}
                                className="text-blue-600 hover:underline"
                            >
                                {t('relink')}
                            </button>
                        </p>
                        <div className="pt-2 border-t border-gray-200">
                            <p className="text-sm font-medium text-gray-800">{t('ownNumberTitle')}</p>
                            <p className="text-sm text-gray-600 mt-1">{t('ownNumberDesc')}</p>
                            <div className="flex gap-2 mt-3">
                                <input
                                    type="tel"
                                    placeholder={ownerNumber || '+49 123 456789'}
                                    title={ownerNumber ? t('registeredHint', { phone: ownerNumber }) : undefined}
                                    value={phoneNumber}
                                    onChange={(e) => setPhoneNumber(e.target.value)}
                                    className="flex-1 px-3 py-2 border border-gray-200 rounded-lg focus:ring-2 focus:ring-green-500 focus:border-transparent"
                                />
                                <button
                                    onClick={handleAddOwnerNumber}
                                    disabled={isAddingOwner || !phoneNumber.trim()}
                                    className="px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 disabled:opacity-50 flex items-center gap-2"
                                >
                                    {isAddingOwner ? <Loader2 className="w-4 h-4 animate-spin" /> : <Phone className="w-4 h-4" />}<span>{t('register')}</span>
                                </button>
                            </div>
                        </div>
                        {error && (
                            <div className="flex items-center gap-2 p-3 bg-red-50 text-red-700 rounded-lg text-sm">
                                <AlertCircle className="w-4 h-4 flex-shrink-0" />
                                {error}
                            </div>
                        )}
                        <button
                            type="button"
                            onClick={finish}
                            className="w-full py-2 border border-gray-300 text-gray-700 rounded-lg hover:bg-gray-50"
                        >
                            {ownerNumber ? t('done') : t('skipOutboundOnly')}
                        </button>
                    </div>
                )}

                {step === 'done' && (
                    <div className="flex flex-col items-center gap-4 py-6">
                        <CheckCircle2 className="w-16 h-16 text-green-600" />
                        <p className="text-center text-gray-700">{t('setupComplete')}</p>
                    </div>
                )}

                {step === 'error' && (
                    <div className="space-y-4">
                        <div className="flex items-center gap-2 p-3 bg-red-50 text-red-700 rounded-lg">
                            <AlertCircle className="w-5 h-5 flex-shrink-0" />
                            {error}
                        </div>
                        <p className="text-xs text-gray-500">
                            {t('debugLog')} <code className="bg-gray-100 px-1 rounded">logs/whatsapp_qr.log</code>
                        </p>
                        <p className="text-sm text-gray-600">{t('loggedOutHint')}</p>
                        <button
                            onClick={() => startQrFlow(true)}
                            className="w-full py-2 bg-gray-900 text-white rounded-lg hover:bg-gray-800 dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-[#f5f5f5] dark:shadow-none"
                        >
                            {t('resetNewQr')}
                        </button>
                    </div>
                )}
            </div>
        </div>
    );
}
