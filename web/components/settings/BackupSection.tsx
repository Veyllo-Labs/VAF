// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useLocale, useTranslations } from 'next-intl';
import { Archive, Loader2 } from 'lucide-react';

interface BackupRow {
    name: string;
    size: number;
    created: string;
}

interface Overview {
    schedule: 'off' | 'daily' | 'weekly';
    time: string;
    keep: { daily: number; weekly: number; monthly: number };
    check: { status: 'ok' | 'degraded' | 'down'; reason: string };
    last_run: { last_success_at?: string | null; last_attempt_at?: string | null; last_error?: string | null };
    folder: string;
    backups: BackupRow[];
    count: number;
    job: {
        running: boolean;
        result: { name: string; size: number; warnings?: string[] } | null;
        error: string | null;
    };
}

/** "20261019T140000Z", the time in a backup's name (vaf/core/backup.py), as a Date. */
function stampDate(stamp: string): Date | null {
    const m = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/.exec(stamp);
    return m ? new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6])) : null;
}

function mb(size: number): string {
    return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * The backups in Settings > Advanced (vaf/core/backup.py, vaf/core/backup_schedule.py,
 * /api/system/backups): the schedule and the retention, which are config keys saved with the
 * page like every other setting here, and from the server the state of the backups, a
 * "Back up now" that runs as a job (it takes from seconds to minutes) and the newest backups.
 * Admin only, like the tab it sits on.
 */
export default function BackupSection({ config, onChange }: {
    config: Record<string, any>;
    onChange: (key: string, value: any) => void;
}) {
    const t = useTranslations('settings.advanced');
    const locale = useLocale();
    const apiBase = typeof window !== 'undefined' ? (document.location.origin || '') : '';
    const [data, setData] = useState<Overview | null>(null);
    const [loadFailed, setLoadFailed] = useState(false);
    const [note, setNote] = useState<{ ok: boolean; text: string; warnings?: string[] } | null>(null);
    const closed = useRef(false);
    const following = useRef(false);

    useEffect(() => {
        closed.current = false;
        return () => { closed.current = true; };
    }, []);

    const load = useCallback(async (): Promise<Overview | null> => {
        try {
            const res = await fetch(`${apiBase}/api/system/backups`, { credentials: 'include' });
            if (!res.ok) { setLoadFailed(true); return null; }
            const body: Overview = await res.json();
            if (!closed.current) { setData(body); setLoadFailed(false); }
            return body;
        } catch {
            setLoadFailed(true);
            return null;
        }
    }, [apiBase]);

    // Follows a running backup until it ends, then says how it ended.
    const follow = useCallback(async () => {
        if (following.current) return;
        following.current = true;
        try {
            for (;;) {
                await new Promise((r) => setTimeout(r, 2000));
                if (closed.current) return;
                const body = await load();
                if (body && !body.job.running) {
                    if (body.job.error) setNote({ ok: false, text: t('backupFailed', { error: body.job.error }) });
                    // The warnings say who can open it and what it lacks (a version 1 recovery key,
                    // keyring sign-ins): the same lines `vaf backup create` prints.
                    else if (body.job.result) setNote({ ok: true, text: t('backupDone', { name: body.job.result.name, size: mb(body.job.result.size) }), warnings: body.job.result.warnings });
                    return;
                }
            }
        } finally {
            following.current = false;
        }
    }, [load, t]);

    useEffect(() => {
        void load().then((body) => { if (body?.job.running) void follow(); });
    }, [load, follow]);

    const backupNow = async () => {
        setNote(null);
        try {
            const res = await fetch(`${apiBase}/api/system/backups/run`, { method: 'POST', credentials: 'include' });
            if (!res.ok) {
                const body = await res.json().catch(() => ({}));
                setNote({ ok: false, text: String(body.detail || t('backupLoadFailed')) });
                return;
            }
        } catch {
            setNote({ ok: false, text: t('backupLoadFailed') });
            return;
        }
        await load();
        void follow();
    };

    const when = (d: Date | null) => (d ? d.toLocaleString(locale, { dateStyle: 'medium', timeStyle: 'short' }) : '');
    const newest = data?.backups[0] ? stampDate(data.backups[0].created) : null;

    // The status sentence, from the same fields the health check reads (backup_check).
    let status: { color: string; text: string } | null = null;
    if (data) {
        const failed = !!data.last_run.last_error && data.check.status !== 'ok';
        if (data.schedule === 'off') {
            status = { color: 'bg-gray-400', text: newest ? t('backupStatusOffNewest', { when: when(newest) }) : t('backupStatusOff') };
        } else if (failed) {
            status = { color: 'bg-amber-500', text: t('backupStatusFailed', { error: data.last_run.last_error || '' }) };
        } else if (!newest) {
            status = { color: 'bg-amber-500', text: t('backupStatusNone') };
        } else {
            status = {
                color: data.check.status === 'ok' ? 'bg-emerald-500' : 'bg-amber-500',
                text: data.check.status === 'ok' ? t('backupStatusNewest', { when: when(newest) }) : t('backupStatusOld', { when: when(newest) }),
            };
        }
    }

    const schedule = String(config.backup_schedule || 'off').trim().toLowerCase();
    const running = !!data?.job.running;
    // A field the admin cleared stays empty (saved as no count, which the backend replaces
    // with the default); the default shows as its placeholder instead of filling it back in.
    const keep = (key: string, fallback: number) => {
        const value = config[key];
        return value === undefined || value === null ? fallback : value;
    };
    const keepInput = (key: string, label: string, fallback: number) => (
        <label className="flex flex-col gap-1.5">
            <span className="text-xs text-gray-600">{label}</span>
            <input
                type="number"
                min={0}
                value={keep(key, fallback)}
                placeholder={String(fallback)}
                onChange={(e) => {
                    // A cleared field stays empty, which the backend reads as "no count" and
                    // replaces with the default (keep_counts); 0 would delete that whole bucket.
                    const n = parseInt(e.target.value, 10);
                    onChange(key, Number.isNaN(n) ? '' : Math.max(0, n));
                }}
                className="w-full px-3 h-9 bg-white border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-gray-500"
            />
        </label>
    );

    return (
        <div>
            <p className="text-xs text-gray-500 mb-4">{t('backupsIntro')}</p>

            {loadFailed && <p className="text-sm text-red-600 mb-3">{t('backupLoadFailed')}</p>}
            {status && (
                <div className="flex items-start gap-2 mb-4">
                    <span className={`w-2 h-2 rounded-full mt-1.5 shrink-0 ${status.color}`} />
                    <span className="text-sm text-gray-700">{status.text}</span>
                </div>
            )}

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <label className="flex flex-col gap-1.5">
                    <span className="text-sm font-medium text-gray-700 ml-1">{t('backupSchedule')}</span>
                    <select
                        value={['daily', 'weekly'].includes(schedule) ? schedule : 'off'}
                        onChange={(e) => onChange('backup_schedule', e.target.value)}
                        className="w-full h-10 px-4 bg-white border border-gray-200 rounded-lg text-sm text-gray-700 focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-gray-500"
                    >
                        <option value="off">{t('backupScheduleOff')}</option>
                        <option value="daily">{t('backupScheduleDaily')}</option>
                        <option value="weekly">{t('backupScheduleWeekly')}</option>
                    </select>
                </label>
                <label className="flex flex-col gap-1.5">
                    <span className="text-sm font-medium text-gray-700 ml-1">{t('backupTime')}</span>
                    <input
                        type="time"
                        value={String(config.backup_time || '03:00')}
                        disabled={!['daily', 'weekly'].includes(schedule)}
                        onChange={(e) => onChange('backup_time', e.target.value)}
                        className="w-full h-10 px-4 bg-white border border-gray-200 rounded-lg text-sm text-gray-700 focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-gray-500 disabled:opacity-60"
                    />
                </label>
            </div>
            <p className="text-xs text-gray-500 mt-1.5">{t('backupTimeDesc')}</p>

            <div className="mt-4">
                <span className="text-sm font-medium text-gray-700 ml-1">{t('backupKeep')}</span>
                <div className="grid grid-cols-3 gap-3 mt-1.5">
                    {keepInput('backup_keep_daily', t('backupKeepDaily'), 7)}
                    {keepInput('backup_keep_weekly', t('backupKeepWeekly'), 4)}
                    {keepInput('backup_keep_monthly', t('backupKeepMonthly'), 6)}
                </div>
                <p className="text-xs text-gray-500 mt-1.5">{t('backupKeepDesc')}</p>
            </div>

            <div className="mt-5 flex flex-wrap items-center gap-3">
                <button
                    type="button"
                    disabled={running || !data}
                    onClick={() => void backupNow()}
                    className="inline-flex items-center gap-2 px-3 py-1.5 text-xs font-medium rounded-md border border-gray-200 text-gray-700 hover:bg-gray-100 disabled:opacity-50"
                >
                    {running ? <Loader2 size={13} className="animate-spin" /> : <Archive size={13} />}
                    {running ? t('backupRunning') : t('backupNow')}
                </button>
            </div>
            {note && <p className={`text-xs mt-2 ${note.ok ? 'text-emerald-700' : 'text-red-600'}`}>{note.text}</p>}
            {note?.warnings && note.warnings.length > 0 && (
                <ul className="mt-1 flex flex-col gap-1">
                    {note.warnings.map((w) => <li key={w} className="text-xs text-amber-700">{w}</li>)}
                </ul>
            )}

            {data && (
                <div className="mt-5">
                    <div className="text-xs font-bold text-gray-500 uppercase tracking-wide mb-2">
                        {t('backupList', { count: data.count })}
                    </div>
                    {data.backups.length === 0 ? (
                        <p className="text-xs text-gray-500">{t('backupNone')}</p>
                    ) : (
                        <ul className="divide-y divide-gray-100 border border-gray-100 rounded-lg bg-white">
                            {data.backups.slice(0, 10).map((row) => (
                                <li key={row.name} className="flex items-center justify-between gap-3 px-3 py-2 text-xs">
                                    <span className="text-gray-700">{when(stampDate(row.created)) || row.name}</span>
                                    <span className="text-gray-500 tabular-nums">{mb(row.size)}</span>
                                </li>
                            ))}
                        </ul>
                    )}
                    <p className="text-xs text-gray-500 mt-2 break-all">{t('backupFolder', { folder: data.folder })}</p>
                    <p className="text-xs text-gray-500 mt-1">{t('backupOffsite')}</p>
                </div>
            )}
        </div>
    );
}
