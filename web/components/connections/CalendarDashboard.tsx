'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
//
// The calendar's sync settings: the connected Google and Outlook accounts with their
// sync state, the switch per account, which account new events are written into, the
// default reminder, "sync now", and the upcoming events of the VAF calendar (internal
// and mirrored alike). The appointments themselves live in the calendar window
// (AutomationCalendarModal); this is the place a connection is looked after.
// Design: docs/integrations/CALENDAR_INTEGRATION.md.

import React, { useState, useEffect, useCallback } from 'react';
import { useTranslations } from 'next-intl';
import { X, Calendar, Loader2, ExternalLink, RefreshCw, CalendarDays, AlertTriangle, MapPin } from 'lucide-react';
import { cn } from '@/lib/utils';

const api = (path: string) => path.startsWith('/') ? path : `/${path}`;

const GOOGLE_CALENDAR_URL = 'https://calendar.google.com';
const OUTLOOK_CALENDAR_URL = 'https://outlook.live.com/calendar/0/view/Month';
const REMINDER_CHOICES = [0, 5, 15, 30, 60, 1440];

export interface CalendarDashboardProps {
    isOpen: boolean;
    onClose: () => void;
    /** Open the calendar setup wizard (add account). */
    onOpenAddWizard?: (provider?: 'google_calendar' | 'outlook_calendar') => void;
    /** Bumped from outside (a closed wizard, a `calendar_changed` frame): status and events are refetched. */
    refreshTrigger?: number;
    /** Open the calendar window with the appointments. */
    onOpenCalendar?: () => void;
}

interface CalendarAccountState {
    account_id: string;
    email: string;
    provider: string;
    enabled: boolean;
    last_sync_at: number | null;
    last_error: string | null;
    needs_reconsent: boolean;
}

interface CalendarStatus {
    google_available: boolean;
    microsoft_available: boolean;
    has_calendar: boolean;
    accounts: CalendarAccountState[];
    settings: { push_target: string | null; default_reminder_minutes: number };
    sync: { interval_minutes: number; push_enabled: boolean; supervisor_running: boolean };
}

interface CalendarEvent {
    id: string;
    title?: string | null;
    start: string;
    end: string;
    all_day: boolean;
    location?: string | null;
    description?: string | null;
    link?: string | null;
    account_id?: string | null;
    source?: string;
}

type ErrorKind = 'accounts' | 'events' | 'settings' | null;

function formatWhen(ev: CalendarEvent): string {
    try {
        if (ev.all_day) {
            return new Date(`${ev.start}T00:00:00`).toLocaleDateString(undefined, { day: '2-digit', month: '2-digit', year: 'numeric' });
        }
        const s = new Date(ev.start);
        const e = new Date(ev.end);
        const date = s.toLocaleDateString(undefined, { day: '2-digit', month: '2-digit', year: 'numeric' });
        const from = s.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
        const to = e.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
        return `${date} ${from} - ${to}`;
    } catch {
        return ev.start;
    }
}

function formatSyncTime(ts: number): string {
    try {
        return new Date(ts * 1000).toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'short' });
    } catch {
        return String(ts);
    }
}

export default function CalendarDashboard({ isOpen, onClose, onOpenAddWizard, refreshTrigger = 0, onOpenCalendar }: CalendarDashboardProps) {
    const t = useTranslations('settings.calendar');
    const [status, setStatus] = useState<CalendarStatus | null>(null);
    const [statusLoading, setStatusLoading] = useState(false);
    const [events, setEvents] = useState<CalendarEvent[]>([]);
    const [eventsLoading, setEventsLoading] = useState(false);
    const [error, setError] = useState<ErrorKind>(null);
    const [rangeDays, setRangeDays] = useState<number>(30);
    const [syncing, setSyncing] = useState(false);
    const [savingSettings, setSavingSettings] = useState(false);

    const fetchStatus = useCallback(async () => {
        setStatusLoading(true);
        try {
            const res = await fetch(api('api/calendar/status'), { credentials: 'include' });
            if (!res.ok) throw new Error(String(res.status));
            setStatus(await res.json());
            setError((e) => (e === 'accounts' ? null : e));
        } catch {
            setStatus(null);
            setError('accounts');
        } finally {
            setStatusLoading(false);
        }
    }, []);

    const fetchEvents = useCallback(async () => {
        setEventsLoading(true);
        try {
            const now = new Date();
            const end = new Date(now);
            end.setDate(end.getDate() + rangeDays);
            const res = await fetch(
                api(`api/calendar/events?time_min=${encodeURIComponent(now.toISOString())}&time_max=${encodeURIComponent(end.toISOString())}`),
                { credentials: 'include' },
            );
            if (!res.ok) throw new Error(String(res.status));
            const data = await res.json();
            setEvents(Array.isArray(data?.events) ? data.events : []);
            setError((e) => (e === 'events' ? null : e));
        } catch {
            setEvents([]);
            setError('events');
        } finally {
            setEventsLoading(false);
        }
    }, [rangeDays]);

    useEffect(() => {
        if (!isOpen) return;
        fetchStatus();
    }, [isOpen, refreshTrigger, fetchStatus]);

    useEffect(() => {
        if (!isOpen) return;
        fetchEvents();
    }, [isOpen, refreshTrigger, fetchEvents]);

    const syncNow = async () => {
        setSyncing(true);
        try {
            await fetch(api('api/calendar/sync'), { method: 'POST', credentials: 'include' });
        } catch {
            // the status below carries the account's own error text
        } finally {
            setSyncing(false);
            fetchStatus();
            fetchEvents();
        }
    };

    const updateSettings = async (patch: Record<string, unknown>) => {
        setSavingSettings(true);
        try {
            const res = await fetch(api('api/calendar/settings'), {
                method: 'PUT', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch),
            });
            if (!res.ok) throw new Error(String(res.status));
            setStatus(await res.json());
            setError((e) => (e === 'settings' ? null : e));
        } catch {
            setError('settings');
        } finally {
            setSavingSettings(false);
        }
    };

    if (!isOpen) return null;

    const accounts = status?.accounts ?? [];
    const reminderLabel = (minutes: number) => {
        if (minutes === 0) return t('reminderNone');
        if (minutes === 60) return t('reminderHour');
        if (minutes === 1440) return t('reminderDay');
        return t('reminderMinutes', { count: minutes });
    };
    const errorText = error === 'accounts' ? t('accountsLoadError') : error === 'events' ? t('eventsLoadError') : error === 'settings' ? t('settingsError') : '';

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50 max-md:p-0" onClick={onClose}>
            <div
                className={cn('relative bg-white w-full max-w-[95vw] h-[90vh] rounded-2xl shadow-2xl border border-gray-200 flex flex-col overflow-hidden max-md:max-w-none max-md:h-[100dvh] max-md:rounded-none max-md:border-0')}
                onClick={(e) => e.stopPropagation()}
            >
                <div className="flex items-center justify-between px-5 py-4 border-b border-gray-200 shrink-0 max-md:px-4 max-md:py-3">
                    <div className="flex items-center gap-3 min-w-0">
                        <div className="w-10 h-10 rounded-xl bg-blue-500 flex items-center justify-center shrink-0">
                            <Calendar className="w-5 h-5 text-white" />
                        </div>
                        <div className="min-w-0">
                            <h3 className="text-lg font-semibold text-gray-900 truncate">{t('dashboardTitle')}</h3>
                            <p className="text-xs text-gray-500 truncate">{t('dashboardSubtitle')}</p>
                        </div>
                    </div>
                    <div className="flex items-center gap-2">
                        {onOpenCalendar && (
                            <button
                                type="button"
                                onClick={onOpenCalendar}
                                className="inline-flex items-center gap-1.5 px-3 py-2 rounded-xl bg-gray-900 text-white text-sm font-medium hover:bg-gray-800 transition-colors dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-[#f5f5f5] dark:shadow-none"
                            >
                                <CalendarDays className="w-4 h-4" /> {t('openVafCalendar')}
                            </button>
                        )}
                        <button type="button" onClick={onClose} className="p-2 hover:bg-gray-100 rounded-lg transition-colors">
                            <X className="w-5 h-5 text-gray-500" />
                        </button>
                    </div>
                </div>

                <div className="flex-1 flex min-h-0 overflow-hidden max-md:flex-col max-md:overflow-y-auto">
                    {/* Left: accounts and the sync settings */}
                    <aside className="w-80 shrink-0 flex flex-col border-r border-gray-200 bg-gray-50/80 overflow-hidden max-md:w-full max-md:max-h-[45vh] max-md:border-r-0 max-md:border-b max-md:shrink-0">
                        {errorText && (
                            <div className="mx-3 mt-3 p-2 rounded-lg bg-red-50 border border-red-200 text-xs text-red-700">{errorText}</div>
                        )}
                        <div className="flex-1 overflow-y-auto p-3 space-y-4">
                            {statusLoading && !status ? (
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
                                            className="mt-4 w-full inline-flex items-center justify-center gap-2 px-3 py-2.5 rounded-xl bg-gray-900 text-white text-sm font-medium hover:bg-gray-800 transition-colors dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-[#f5f5f5] dark:shadow-none"
                                        >
                                            {t('openEmailToConnect')}
                                        </button>
                                    )}
                                </div>
                            ) : (
                                <>
                                    <section className="p-3 rounded-xl border border-gray-200 bg-white shadow-sm space-y-2">
                                        <div className="flex items-center justify-between gap-2">
                                            <span className="text-sm font-medium text-gray-900">{t('syncTitle')}</span>
                                            <button
                                                type="button"
                                                onClick={syncNow}
                                                disabled={syncing}
                                                className="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border border-gray-200 bg-white text-xs font-medium text-gray-700 hover:bg-gray-100 disabled:opacity-50"
                                            >
                                                {syncing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
                                                {syncing ? t('syncing') : t('syncNow')}
                                            </button>
                                        </div>
                                        <p className="text-xs text-gray-500">{t('syncEvery', { minutes: status?.sync.interval_minutes ?? 5 })}</p>
                                        {status && !status.sync.push_enabled && (
                                            <p className="text-xs text-amber-700 flex items-start gap-1"><AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />{t('pushDisabledByAdmin')}</p>
                                        )}
                                    </section>
                                    <ul className="space-y-2">
                                        {accounts.map((a) => {
                                            const isGmail = a.provider === 'gmail';
                                            return (
                                                <li key={a.account_id} className="p-3 rounded-xl border border-gray-200 bg-white shadow-sm space-y-2">
                                                    <div className="flex items-start justify-between gap-2">
                                                        <div className="min-w-0">
                                                            <span className="font-medium text-gray-900 text-sm truncate block">{a.email || a.account_id}</span>
                                                            <span className="text-xs text-gray-500">{isGmail ? 'Google' : 'Outlook'}</span>
                                                        </div>
                                                        <label className="flex items-center gap-1.5 text-xs text-gray-600 cursor-pointer shrink-0">
                                                            <input
                                                                type="checkbox"
                                                                checked={a.enabled}
                                                                disabled={savingSettings}
                                                                onChange={(e) => updateSettings({ accounts: [{ account_id: a.account_id, enabled: e.target.checked }] })}
                                                                className="rounded border-gray-300 dark:accent-[#d9d9d9]"
                                                            />
                                                            {t('syncEnabled')}
                                                        </label>
                                                    </div>
                                                    <p className="text-xs text-gray-500">
                                                        {a.last_sync_at ? t('lastSync', { when: formatSyncTime(a.last_sync_at) }) : t('neverSynced')}
                                                    </p>
                                                    {a.needs_reconsent ? (
                                                        <p className="text-xs text-amber-700 flex items-start gap-1"><AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />{t('reconnectNeeded')}</p>
                                                    ) : a.last_error ? (
                                                        <p className="text-xs text-red-600 break-words">{t('syncError', { error: a.last_error })}</p>
                                                    ) : null}
                                                    <a
                                                        href={isGmail ? GOOGLE_CALENDAR_URL : OUTLOOK_CALENDAR_URL}
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
                                    <section className="p-3 rounded-xl border border-gray-200 bg-white shadow-sm space-y-3">
                                        <div>
                                            <label className="block text-xs font-medium text-gray-600 mb-1">{t('pushTarget')}</label>
                                            <select
                                                value={status?.settings.push_target ?? ''}
                                                disabled={savingSettings}
                                                onChange={(e) => updateSettings({ push_target: e.target.value })}
                                                className="w-full text-sm border border-gray-200 rounded-lg px-2 py-1.5 bg-white text-gray-900 focus:outline-none focus:ring-1 focus:ring-gray-400"
                                            >
                                                <option value="">{t('pushTargetNone')}</option>
                                                {accounts.map((a) => <option key={a.account_id} value={a.account_id}>{a.email || a.account_id}</option>)}
                                            </select>
                                        </div>
                                        <div>
                                            <label className="block text-xs font-medium text-gray-600 mb-1">{t('defaultReminder')}</label>
                                            <select
                                                value={status?.settings.default_reminder_minutes ?? 15}
                                                disabled={savingSettings}
                                                onChange={(e) => updateSettings({ default_reminder_minutes: Number(e.target.value) })}
                                                className="w-full text-sm border border-gray-200 rounded-lg px-2 py-1.5 bg-white text-gray-900 focus:outline-none focus:ring-1 focus:ring-gray-400"
                                            >
                                                {REMINDER_CHOICES.map((m) => <option key={m} value={m}>{reminderLabel(m)}</option>)}
                                            </select>
                                        </div>
                                    </section>
                                    {onOpenAddWizard && (
                                        <button
                                            type="button"
                                            onClick={() => onOpenAddWizard()}
                                            className="w-full py-2.5 rounded-xl border-2 border-dashed border-gray-200 text-sm font-medium text-gray-600 hover:border-gray-300 hover:bg-white transition-colors"
                                        >
                                            {t('addAnotherAccountViaEmail')}
                                        </button>
                                    )}
                                </>
                            )}
                        </div>
                    </aside>

                    {/* Right: the upcoming events of the VAF calendar */}
                    <main className="flex-1 min-w-0 flex flex-col overflow-hidden bg-white max-md:min-h-0 max-md:shrink-0">
                        <div className="shrink-0 flex items-center justify-between gap-4 px-4 py-3 border-b border-gray-200 bg-gray-50/80 flex-wrap">
                            <div className="flex items-center gap-2 min-w-0">
                                <CalendarDays className="w-5 h-5 text-gray-500 shrink-0" />
                                <span className="text-sm font-medium text-gray-700">{t('upcomingEvents')}</span>
                            </div>
                            <div className="flex items-center gap-2">
                                <select
                                    value={rangeDays}
                                    onChange={(e) => setRangeDays(Number(e.target.value))}
                                    className="text-sm border border-gray-200 rounded-lg px-2 py-1.5 bg-white text-gray-900 focus:outline-none focus:ring-1 focus:ring-gray-400"
                                >
                                    <option value={7}>{t('next7Days')}</option>
                                    <option value={14}>{t('next14Days')}</option>
                                    <option value={30}>{t('next30Days')}</option>
                                    <option value={60}>{t('next60Days')}</option>
                                </select>
                                <button
                                    type="button"
                                    onClick={fetchEvents}
                                    disabled={eventsLoading}
                                    className="p-1.5 rounded-lg border border-gray-200 bg-white text-gray-600 hover:bg-gray-100 disabled:opacity-50"
                                    title={t('refreshEvents')}
                                >
                                    {eventsLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
                                </button>
                            </div>
                        </div>
                        <div className="flex-1 overflow-y-auto p-4">
                            {eventsLoading && events.length === 0 ? (
                                <div className="flex items-center justify-center py-12">
                                    <Loader2 className="w-8 h-8 animate-spin text-gray-400" />
                                </div>
                            ) : events.length === 0 ? (
                                <div className="text-center py-12 text-gray-500 text-sm">{t('noEventsInRange')}</div>
                            ) : (
                                <ul className="space-y-3">
                                    {events.map((ev) => (
                                        <li key={ev.id} className="p-4 rounded-xl border border-gray-200 bg-gray-50/50 hover:bg-gray-100 transition-colors">
                                            <div className="flex items-start justify-between gap-3">
                                                <div className="min-w-0 flex-1">
                                                    <p className="font-medium text-gray-900 truncate">{ev.title || t('noTitle')}</p>
                                                    <p className="text-xs text-gray-500 mt-0.5">{formatWhen(ev)}</p>
                                                    <div className="flex flex-wrap items-center gap-2 mt-1.5 text-xs text-gray-500">
                                                        <span className="px-2 py-0.5 rounded-full bg-gray-200 text-gray-700">{ev.account_id || t('internalEvent')}</span>
                                                        {ev.location && <span className="inline-flex items-center gap-1"><MapPin className="w-3 h-3" />{ev.location}</span>}
                                                    </div>
                                                    {ev.description && (
                                                        <p className="text-sm text-gray-600 mt-2 line-clamp-2">{ev.description.replace(/<[^>]*>/g, ' ').trim()}</p>
                                                    )}
                                                </div>
                                                {ev.link && (
                                                    <a
                                                        href={ev.link}
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
                                    ))}
                                </ul>
                            )}
                        </div>
                    </main>
                </div>
            </div>
        </div>
    );
}
