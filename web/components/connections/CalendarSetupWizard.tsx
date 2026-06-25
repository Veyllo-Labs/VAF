'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import React, { useState, useEffect } from 'react';
import { X, Calendar, Loader2, CheckCircle2, ExternalLink } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { cn } from '@/lib/utils';

const api = (path: string) => path.startsWith('/') ? path : `/${path}`;

interface CalendarSetupWizardProps {
    isOpen: boolean;
    onClose: () => void;
    onComplete?: () => void;
    /** When set, show only this provider (e.g. opened from Google Calendar card). */
    initialProvider?: 'google_calendar' | 'outlook_calendar';
}

export default function CalendarSetupWizard({ isOpen, onClose, onComplete, initialProvider }: CalendarSetupWizardProps) {
    const t = useTranslations('settings.calendar');
    const [loading, setLoading] = useState<string | null>(null);
    const [error, setError] = useState('');
    const [status, setStatus] = useState<{ google_available: boolean; microsoft_available: boolean }>({ google_available: false, microsoft_available: false });
    const [refreshing, setRefreshing] = useState(false);
    const [showSuccess, setShowSuccess] = useState(false);

    const fetchStatus = async () => {
        try {
            const res = await fetch(api('api/calendar/status'), { credentials: 'include' });
            if (res.ok) {
                const data = await res.json();
                setStatus({
                    google_available: !!data.google_available,
                    microsoft_available: !!data.microsoft_available,
                });
                if (data.google_available || data.microsoft_available) setShowSuccess(true);
            }
        } catch {
            setStatus({ google_available: false, microsoft_available: false });
        }
    };

    useEffect(() => {
        if (isOpen) {
            setError('');
            setShowSuccess(false);
            fetchStatus();
        }
    }, [isOpen]);

    useEffect(() => {
        if (typeof window !== 'undefined') {
            const params = new URLSearchParams(window.location.search);
            if (params.get('email_oauth') === 'success' && isOpen) {
                setRefreshing(true);
                fetchStatus().finally(() => setRefreshing(false));
            }
        }
    }, [isOpen]);

    const startOAuth = async (provider: 'gmail' | 'microsoft') => {
        setLoading(provider);
        setError('');
        try {
            const res = await fetch(api(`api/email/oauth/start?provider=${provider}`), { credentials: 'include' });
            if (!res.ok) throw new Error(res.status === 400 ? 'Sign-in could not be started. An admin may need to configure OAuth in Settings.' : `Request failed: ${res.status}`);
            const data = await res.json();
            const url = data.authorization_url || '';
            if (url && typeof window !== 'undefined') {
                window.open(url, '_blank', 'noopener,noreferrer');
            } else {
                setError('No sign-in URL returned. Check OAuth client in Settings (Email / Central credentials).');
            }
        } catch (e) {
            setError(e instanceof Error ? e.message : 'Failed to start sign-in');
        } finally {
            setLoading(null);
        }
    };

    const handleRefresh = async () => {
        setRefreshing(true);
        await fetchStatus();
        setRefreshing(false);
    };

    const handleDone = () => {
        onComplete?.();
        onClose();
    };

    if (!isOpen) return null;

    const showGoogle = initialProvider !== 'outlook_calendar';
    const showMicrosoft = initialProvider !== 'google_calendar';

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
            <div className="bg-white rounded-2xl border border-gray-200 shadow-xl max-w-md w-full max-h-[90vh] overflow-y-auto">
                <div className="p-6">
                    <div className="flex items-center justify-between mb-4">
                        <div className="flex items-center gap-2">
                            <div className="w-10 h-10 rounded-xl bg-blue-500 flex items-center justify-center text-white">
                                <Calendar className="w-5 h-5" />
                            </div>
                            <div>
                                <h2 className="text-lg font-semibold text-gray-900">Connect Calendar</h2>
                                <p className="text-xs text-gray-500">Scheduling & event management</p>
                            </div>
                        </div>
                        <button
                            type="button"
                            onClick={onClose}
                            className="p-2 hover:bg-gray-100 rounded-lg transition-colors text-gray-500"
                            aria-label="Close"
                        >
                            <X className="w-5 h-5" />
                        </button>
                    </div>

                    {showSuccess ? (
                        <div className="space-y-4">
                            <div className="flex items-center gap-3 p-3 rounded-xl bg-green-50 border border-green-200">
                                <CheckCircle2 className="w-6 h-6 text-green-600 shrink-0" />
                                <div>
                                    <p className="font-medium text-green-800">Calendar connected</p>
                                    <p className="text-sm text-green-700">
                                        {status.google_available && status.microsoft_available
                                            ? 'Gmail and Outlook are connected. The agent can list and create events.'
                                            : status.google_available
                                                ? 'Gmail is connected. The agent can list and create events.'
                                                : 'Outlook is connected. The agent can list and create events.'}
                                    </p>
                                </div>
                            </div>
                            <button
                                type="button"
                                onClick={handleDone}
                                className="w-full py-2.5 px-4 rounded-lg font-medium bg-gray-900 hover:bg-gray-800 text-white transition-colors"
                            >
                                Done
                            </button>
                        </div>
                    ) : (
                        <>
                            <p className="text-sm text-gray-600 mb-4">
                                {t('calendarUsesEmailAccount')}
                            </p>
                            {error && (
                                <div className="mb-4 p-3 rounded-lg bg-red-50 border border-red-200 text-sm text-red-700">
                                    {error}
                                </div>
                            )}
                            <div className="space-y-2">
                                {showGoogle && (
                                    <button
                                        type="button"
                                        onClick={() => startOAuth('gmail')}
                                        disabled={!!loading}
                                        className={cn(
                                            "w-full flex items-center justify-center gap-2 py-3 px-4 rounded-xl border transition-colors",
                                            "bg-white border-gray-200 hover:bg-gray-50 text-gray-800"
                                        )}
                                    >
                                        {loading === 'gmail' ? (
                                            <Loader2 className="w-5 h-5 animate-spin text-gray-500" />
                                        ) : (
                                            <>
                                                <ExternalLink className="w-4 h-4" />
                                                Sign in with Google
                                            </>
                                        )}
                                    </button>
                                )}
                                {showMicrosoft && (
                                    <button
                                        type="button"
                                        onClick={() => startOAuth('microsoft')}
                                        disabled={!!loading}
                                        className={cn(
                                            "w-full flex items-center justify-center gap-2 py-3 px-4 rounded-xl border transition-colors",
                                            "bg-white border-gray-200 hover:bg-gray-50 text-gray-800"
                                        )}
                                    >
                                        {loading === 'microsoft' ? (
                                            <Loader2 className="w-5 h-5 animate-spin text-gray-500" />
                                        ) : (
                                            <>
                                                <ExternalLink className="w-4 h-4" />
                                                Sign in with Microsoft
                                            </>
                                        )}
                                    </button>
                                )}
                            </div>
                            <p className="text-xs text-gray-500 mt-4">
                                A new tab will open for sign-in. After completing sign-in, click below to refresh.
                            </p>
                            <button
                                type="button"
                                onClick={handleRefresh}
                                disabled={refreshing}
                                className="mt-2 w-full py-2 px-4 rounded-lg border border-gray-200 bg-gray-50 hover:bg-gray-100 text-sm font-medium text-gray-700 disabled:opacity-50"
                            >
                                {refreshing ? 'Checking...' : "I've signed in – refresh status"}
                            </button>
                        </>
                    )}
                </div>
            </div>
        </div>
    );
}
