'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import React, { useState, useEffect } from 'react';
import { useTranslations } from 'next-intl';
import { X, Calendar, Loader2, ExternalLink, RefreshCw, CalendarDays } from 'lucide-react';
import { cn } from '@/lib/utils';

const api = (path: string) => path.startsWith('/') ? path : `/${path}`;

const GOOGLE_CALENDAR_URL = 'https://calendar.google.com';
const OUTLOOK_CALENDAR_URL = 'https://outlook.live.com/calendar/0/view/Month';

export interface CalendarDashboardProps {
    isOpen: boolean;
    onClose: () => void;
    /** Open the calendar setup wizard (add account). */
    onOpenAddWizard?: (provider?: 'google_calendar' | 'outlook_calendar') => void;
    refreshTrigger?: number;
}

interface CalendarAccount {
    account_id: string;
    email: string;
    provider: string;
    enabled?: boolean;
}

interface CalendarEvent {
    id?: string;
    summary?: string;
    start?: string;
    end?: string;
    htmlLink?: string;
    webLink?: string;
    description?: string;
}

function formatEventDate(iso: string | undefined): string {
    if (!iso) return '—';
    try {
        const d = new Date(iso);
        if (Number.isNaN(d.getTime())) return iso;
        const date = d.toLocaleDateString(undefined, { day: '2-digit', month: '2-digit', year: 'numeric' });
        const time = iso.includes('T') ? d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' }) : '';
        return time ? `${date} ${time}` : date;
    } catch {
        return iso;
    }
}

export default function CalendarDashboard({ isOpen, onClose, onOpenAddWizard, refreshTrigger = 0 }: CalendarDashboardProps) {
    const t = useTranslations('settings.calendar');
    const [accounts, setAccounts] = useState<CalendarAccount[]>([]);
    const [events, setEvents] = useState<CalendarEvent[]>([]);
    const [accountsLoading, setAccountsLoading] = useState(false);
    const [eventsLoading, setEventsLoading] = useState(false);
    const [error, setError] = useState('');
    const [rangeDays, setRangeDays] = useState<number>(30);

    const fetchAccounts = async () => {
        setAccountsLoading(true);
        setError('');
        try {
            const res = await fetch(api('api/email/accounts'), { credentials: 'include' });
            const data = await res.json();
            const list = (data?.accounts ?? []) as CalendarAccount[];
            const calendarAccounts = list.filter(
                (a) => (a.provider || '').toLowerCase() === 'gmail' || (a.provider || '').toLowerCase() === 'microsoft'
            );
            setAccounts(calendarAccounts);
        } catch {
            setAccounts([]);
            setError('Failed to load accounts');
        } finally {
            setAccountsLoading(false);
        }
    };

    const fetchEvents = async () => {
        setEventsLoading(true);
        setError('');
        try {
            const now = new Date();
            const end = new Date(now);
            end.setDate(end.getDate() + rangeDays);
            const timeMin = now.toISOString();
            const timeMax = end.toISOString();
            const res = await fetch(
                api(`api/calendar/events?time_min=${encodeURIComponent(timeMin)}&time_max=${encodeURIComponent(timeMax)}`),
                { credentials: 'include' }
            );
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                const detail = (err as any).detail;
                const errMsg = Array.isArray(detail)
                    ? detail.map((d: any) => d?.msg ?? String(d)).join('. ')
                    : typeof detail === 'string'
                        ? detail
                        : detail?.msg ?? 'Failed to load events';
                setError(errMsg);
                setEvents([]);
                return;
            }
            const data = await res.json();
            setEvents(Array.isArray(data?.events) ? data.events : []);
        } catch {
            setEvents([]);
            setError('Failed to load events');
        } finally {
            setEventsLoading(false);
        }
    };

    useEffect(() => {
        if (isOpen) {
            fetchAccounts();
        }
    }, [isOpen, refreshTrigger]);

    useEffect(() => {
        if (isOpen && accounts.length > 0) {
            fetchEvents();
        } else {
            setEvents([]);
        }
    }, [isOpen, accounts.length, rangeDays]);

    const handleRefreshEvents = () => {
        fetchEvents();
    };

    if (!isOpen) return null;

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50 max-md:p-0" onClick={onClose}>
            <div
                className={cn(
                    'relative bg-white w-full max-w-[95vw] h-[90vh] rounded-2xl shadow-2xl border border-gray-200 flex flex-col overflow-hidden max-md:max-w-none max-md:h-[100dvh] max-md:rounded-none max-md:border-0'
                )}
                onClick={(e) => e.stopPropagation()}
            >
                <div className="flex items-center justify-between px-5 py-4 border-b border-gray-200 shrink-0 max-md:px-4 max-md:py-3">
                    <div className="flex items-center gap-3 min-w-0">
                        <div className="w-10 h-10 rounded-xl bg-blue-500 flex items-center justify-center shrink-0 max-md:w-10 max-md:h-10 max-md:rounded-xl max-md:shadow-none">
                            <Calendar className="w-5 h-5 text-white max-md:w-5 max-md:h-5" />
                        </div>
                        <div className="min-w-0">
                            <h3 className="text-lg font-semibold text-gray-900 max-md:text-lg truncate">{t('dashboardTitle')}</h3>
                            <p className="text-xs text-gray-500 max-md:text-xs truncate">{t('dashboardSubtitle')}</p>
                        </div>
                    </div>
                    <button type="button" onClick={onClose} className="p-2 hover:bg-gray-100 rounded-lg transition-colors">
                        <X className="w-5 h-5 text-gray-500" />
                    </button>
                </div>

                <div className="flex-1 flex min-h-0 overflow-hidden max-md:flex-col max-md:overflow-y-auto">
                    {/* Left sidebar: calendar accounts + links */}
                    <aside className="w-72 shrink-0 flex flex-col border-r border-gray-200 bg-gray-50/80 overflow-hidden max-md:w-full max-md:max-h-[38vh] max-md:border-r-0 max-md:border-b max-md:shrink-0">
                        {error && typeof error === 'string' && (
                            <div className="mx-3 mt-3 p-2 rounded-lg bg-red-50 border border-red-200 text-xs text-red-700">
                                {error}
                            </div>
                        )}
                        <div className="flex-1 overflow-y-auto p-3">
                            {accountsLoading ? (
                                <div className="flex items-center justify-center py-8">
                                    <Loader2 className="w-6 h-6 animate-spin text-gray-400" />
                                </div>
                            ) : accounts.length === 0 ? (
                                <div className="flex flex-col items-center justify-center py-8 text-center">
                                    <p className="text-sm text-gray-600">{t('noCalendarAccount')}</p>
                                    <p className="text-xs text-gray-500 mt-1">{t('noCalendarAccountHint')}</p>
                                    {onOpenAddWizard && (
                                        <button
                                            type="button"
                                            onClick={() => onOpenAddWizard()}
                                            className="mt-4 w-full inline-flex items-center justify-center gap-2 px-3 py-2.5 rounded-xl bg-gray-900 text-white text-sm font-medium hover:bg-gray-800 transition-colors"
                                        >
                                            {t('openEmailToConnect')}
                                        </button>
                                    )}
                                </div>
                            ) : (
                                <>
                                    <ul className="space-y-2">
                                        {accounts.map((a) => {
                                            const id = a.account_id || a.email;
                                            const isGmail = (a.provider || '').toLowerCase() === 'gmail';
                                            const openUrl = isGmail ? GOOGLE_CALENDAR_URL : OUTLOOK_CALENDAR_URL;
                                            return (
                                                <li
                                                    key={id}
                                                    className="p-3 rounded-xl border border-gray-200 bg-white shadow-sm space-y-2"
                                                >
                                                    <div className="min-w-0">
                                                        <span className="font-medium text-gray-900 text-sm truncate block">
                                                            {a.email || a.account_id}
                                                        </span>
                                                        <span className="text-xs text-gray-500">{a.provider}</span>
                                                    </div>
                                                    <a
                                                        href={openUrl}
                                                        target="_blank"
                                                        rel="noopener noreferrer"
                                                        className="inline-flex items-center gap-1.5 text-xs text-blue-600 hover:text-blue-800"
                                                    >
                                                        <ExternalLink className="w-3.5 h-3.5" />
                                                        {isGmail ? t('openGoogleCalendar') : t('openOutlookCalendar')}
                                                    </a>
                                                </li>
                                            );
                                        })}
                                    </ul>
                                    {onOpenAddWizard && (
                                        <button
                                            type="button"
                                            onClick={() => onOpenAddWizard()}
                                            className="mt-3 w-full py-2.5 rounded-xl border-2 border-dashed border-gray-200 text-sm font-medium text-gray-600 hover:border-gray-300 hover:bg-white transition-colors"
                                        >
                                            {t('addAnotherAccountViaEmail')}
                                        </button>
                                    )}
                                </>
                            )}
                        </div>
                    </aside>

                    {/* Main content: events list */}
                    <main className="flex-1 min-w-0 flex flex-col overflow-hidden bg-white max-md:min-h-0 max-md:shrink-0">
                        {accounts.length === 0 && !accountsLoading && (
                            <div className="flex flex-col items-center justify-center flex-1 text-center max-w-sm mx-auto p-8 max-md:p-4">
                                <div className="w-14 h-14 rounded-2xl bg-gray-100 flex items-center justify-center mb-3">
                                    <CalendarDays className="w-7 h-7 text-gray-400" />
                                </div>
                                <p className="text-gray-600 font-medium">{t('yourCalendars')}</p>
                                <p className="text-sm text-gray-500 mt-1">{t('connectGmailOutlookSidebar')}</p>
                            </div>
                        )}
                        {accounts.length > 0 && (
                            <>
                                <div className="shrink-0 flex items-center justify-between gap-4 px-4 py-3 border-b border-gray-200 bg-gray-50/80 flex-wrap">
                                    <div className="flex items-center gap-2 min-w-0">
                                        <CalendarDays className="w-5 h-5 text-gray-500 shrink-0" />
                                        <span className="text-sm font-medium text-gray-700">{t('upcomingEvents')}</span>
                                    </div>
                                    <div className="flex items-center gap-2">
                                        <select
                                            value={rangeDays}
                                            onChange={(e) => setRangeDays(Number(e.target.value))}
                                            className="text-sm border border-gray-200 rounded-lg px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-gray-400"
                                        >
                                            <option value={7}>{t('next7Days')}</option>
                                            <option value={14}>{t('next14Days')}</option>
                                            <option value={30}>{t('next30Days')}</option>
                                            <option value={60}>{t('next60Days')}</option>
                                        </select>
                                        <button
                                            type="button"
                                            onClick={handleRefreshEvents}
                                            disabled={eventsLoading}
                                            className="p-1.5 rounded-lg border border-gray-200 bg-white text-gray-600 hover:bg-gray-50 disabled:opacity-50"
                                            title="Refresh events"
                                        >
                                            {eventsLoading ? (
                                                <Loader2 className="w-4 h-4 animate-spin" />
                                            ) : (
                                                <RefreshCw className="w-4 h-4" />
                                            )}
                                        </button>
                                    </div>
                                </div>
                                <div className="flex-1 overflow-y-auto p-4">
                                    {eventsLoading && events.length === 0 ? (
                                        <div className="flex items-center justify-center py-12">
                                            <Loader2 className="w-8 h-8 animate-spin text-gray-400" />
                                        </div>
                                    ) : events.length === 0 ? (
                                        <div className="text-center py-12 text-gray-500 text-sm">
                                            {t('noEventsInRange')}
                                        </div>
                                    ) : (
                                        <ul className="space-y-3">
                                            {events.map((ev) => {
                                                const link = ev.htmlLink || ev.webLink;
                                                return (
                                                    <li
                                                        key={ev.id || ev.start || ev.summary || Math.random()}
                                                        className="p-4 rounded-xl border border-gray-200 bg-gray-50/50 hover:bg-gray-50 transition-colors"
                                                    >
                                                        <div className="flex items-start justify-between gap-3">
                                                            <div className="min-w-0 flex-1">
                                                                <p className="font-medium text-gray-900 truncate">
                                                                    {ev.summary || '(No title)'}
                                                                </p>
                                                                <p className="text-xs text-gray-500 mt-0.5">
                                                                    {formatEventDate(ev.start)} → {formatEventDate(ev.end)}
                                                                </p>
                                                                {ev.description && (
                                                                    <p className="text-sm text-gray-600 mt-2 line-clamp-2">
                                                                        {ev.description.replace(/<[^>]*>/g, ' ').trim().slice(0, 200)}
                                                                        {(ev.description?.length ?? 0) > 200 ? '…' : ''}
                                                                    </p>
                                                                )}
                                                            </div>
                                                            {link && (
                                                                <a
                                                                    href={link}
                                                                    target="_blank"
                                                                    rel="noopener noreferrer"
                                                                    className="shrink-0 p-2 rounded-lg text-gray-500 hover:bg-gray-200 hover:text-gray-700"
                                                                    title={t('openInCalendar')}
                                                                >
                                                                    <ExternalLink className="w-4 h-4" />
                                                                </a>
                                                            )}
                                                        </div>
                                                    </li>
                                                );
                                            })}
                                        </ul>
                                    )}
                                </div>
                            </>
                        )}
                    </main>
                </div>
            </div>
        </div>
    );
}
