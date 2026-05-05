'use client';

import React, { useState, useEffect, useCallback } from 'react';
import { X, Zap, Trash2 } from 'lucide-react';
import { useTranslations } from 'next-intl';

export interface CreateAutomationPayload {
    prompt: string;
    frequency: 'once' | 'daily' | 'weekly' | 'monthly' | 'hourly';
    time: string;
    name?: string;
    description?: string;
    weekday?: string;
    day?: number;
}

export type EditAutomationTask = {
    id: string;
    name?: string;
    prompt: string;
    frequency: string;
    time: string;
    weekday?: string | null;
    day?: number | null;
};

export interface CreateAutomationPopupProps {
    isOpen: boolean;
    onClose: () => void;
    initialDate: Date;
    initialHour: number;
    /** Minute for initial time when not in edit mode (0-59). Default 0. */
    initialMinute?: number;
    /** When set, popup is in edit mode: prefill form and submit sends task_id for update. */
    editTask?: EditAutomationTask | null;
    onCreated?: () => void;
    onSubmit?: (payload: CreateAutomationPayload & { task_id?: string }) => Promise<{ ok: boolean; error?: string }>;
    /** When provided and in edit mode, a Delete button is shown; called with task id on confirm. */
    onDelete?: (taskId: string) => void;
}

const WEEKDAYS = ['sunday', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday'] as const;

function formatTime(hour: number, minute: number): string {
    return `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`;
}

function parseTime(s: string): { hour: number; minute: number } {
    const parts = (s || '00:00').trim().split(':');
    const hour = Math.max(0, Math.min(23, parseInt(parts[0] || '0', 10) || 0));
    const minute = Math.max(0, Math.min(59, parseInt(parts[1] || '0', 10) || 0));
    return { hour, minute };
}

export default function CreateAutomationPopup({
    isOpen,
    onClose,
    initialDate,
    initialHour,
    initialMinute = 0,
    editTask,
    onCreated,
    onSubmit,
    onDelete,
}: CreateAutomationPopupProps) {
    const t = useTranslations('settings.automations.createPopup');
    const tAutomations = useTranslations('settings.automations');
    const [frequency, setFrequency] = useState<'once' | 'daily' | 'weekly' | 'monthly' | 'hourly'>('daily');
    const [selectedWeekday, setSelectedWeekday] = useState<string>(WEEKDAYS[new Date().getDay()]);
    const [timeStr, setTimeStr] = useState(() => formatTime(initialHour, initialMinute));
    const [prompt, setPrompt] = useState('');
    const [name, setName] = useState('');
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const isEditMode = Boolean(editTask?.id);

    const resetForm = useCallback(() => {
        if (editTask) {
            const freq = (editTask.frequency || 'daily') as 'once' | 'daily' | 'weekly' | 'monthly' | 'hourly';
            setFrequency(freq);
            setTimeStr(editTask.time || '06:00');
            setPrompt(editTask.prompt || '');
            setName(editTask.name || '');
            setSelectedWeekday((editTask.weekday || '').toLowerCase() || WEEKDAYS[initialDate.getDay()]);
        } else {
            setFrequency('daily');
            setTimeStr(formatTime(initialHour, initialMinute));
            setPrompt('');
            setName('');
            setSelectedWeekday(WEEKDAYS[initialDate.getDay()]);
        }
        setError(null);
    }, [initialHour, initialMinute, editTask]);

    useEffect(() => {
        if (isOpen) {
            if (editTask) {
                const freq = (editTask.frequency || 'daily') as 'once' | 'daily' | 'weekly' | 'monthly' | 'hourly';
                setFrequency(freq);
                setTimeStr(editTask.time || '06:00');
                setPrompt(editTask.prompt || '');
                setName(editTask.name || '');
                setSelectedWeekday((editTask.weekday || '').toLowerCase() || WEEKDAYS[initialDate.getDay()]);
                setError(null);
            } else {
                setTimeStr(formatTime(initialHour, initialMinute));
                setSelectedWeekday(WEEKDAYS[initialDate.getDay()]);
                resetForm();
            }
        }
    }, [isOpen, initialHour, initialMinute, editTask, resetForm]);

    useEffect(() => {
        if (!isOpen) return;
        const handleKeyDown = (e: KeyboardEvent) => {
            if (e.key === 'Escape') {
                e.preventDefault();
                onClose();
            }
        };
        window.addEventListener('keydown', handleKeyDown);
        return () => window.removeEventListener('keydown', handleKeyDown);
    }, [isOpen, onClose]);

    const weekdayFromDate = initialDate.getDay();
    const weekday = WEEKDAYS[weekdayFromDate];
    const dayOfMonth = initialDate.getDate();

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        setError(null);
        const promptTrimmed = prompt.trim();
        if (!promptTrimmed) {
            setError(t('errorRequired'));
            return;
        }
        const { hour, minute } = parseTime(timeStr);
        const time = formatTime(hour, minute);
        const payload: CreateAutomationPayload & { task_id?: string } = {
            prompt: promptTrimmed,
            frequency,
            time,
            name: name.trim() || undefined,
            description: promptTrimmed.slice(0, 200),
            weekday: frequency === 'weekly' ? selectedWeekday : undefined,
            day: frequency === 'monthly' ? (editTask?.day ?? dayOfMonth) : undefined,
        };
        if (editTask?.id) payload.task_id = editTask.id;
        if (!onSubmit) {
            setError(t('errorServer'));
            return;
        }
        setLoading(true);
        try {
            const result = await onSubmit(payload);
            if (result.ok) {
                onCreated?.();
                onClose();
            } else {
                setError(result.error || t('errorServer'));
            }
        } catch {
            setError(t('errorServer'));
        } finally {
            setLoading(false);
        }
    };

    if (!isOpen) return null;

    return (
        <div className="fixed inset-0 z-[70] flex items-center justify-center p-4" onClick={onClose}>
            <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" />
            <div
                className="relative bg-white rounded-2xl shadow-2xl w-full max-w-2xl mx-4 overflow-hidden border border-gray-200 animate-in fade-in zoom-in-95 duration-200"
                onClick={(e) => e.stopPropagation()}
            >
                <div className="flex items-center justify-between p-6 border-b border-gray-200 bg-gray-50">
                    <div className="flex items-center gap-3">
                        <div className="w-10 h-10 rounded-xl bg-gray-900 flex items-center justify-center">
                            <Zap className="w-5 h-5 text-white" />
                        </div>
                        <div>
                            <h2 className="text-xl font-bold text-gray-900">{t('title')}</h2>
                            <p className="text-sm text-gray-500">{t('subtitle')}</p>
                        </div>
                    </div>
                    <button
                        type="button"
                        onClick={onClose}
                        className="p-2 hover:bg-gray-200 rounded-lg transition-colors text-gray-500 hover:text-gray-700"
                        aria-label="Close"
                    >
                        <X className="w-5 h-5" />
                    </button>
                </div>

                <form onSubmit={handleSubmit}>
                    <div className="p-6 space-y-4">
                        <div>
                            <label htmlFor="create-automation-frequency" className="block text-sm font-medium text-gray-700 mb-1">
                                {t('frequencyLabel')}
                            </label>
                            <select
                                id="create-automation-frequency"
                                value={frequency}
                                onChange={(e) => setFrequency(e.target.value as CreateAutomationPayload['frequency'])}
                                className="w-full h-10 px-4 bg-white border border-gray-200 rounded-lg text-sm text-gray-700 focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500"
                            >
                                <option value="once">{t('frequencyOnce')}</option>
                                <option value="daily">{t('frequencyDaily')}</option>
                                <option value="weekly">{t('frequencyWeekly')}</option>
                                <option value="monthly">{t('frequencyMonthly')}</option>
                                <option value="hourly">{t('frequencyHourly')}</option>
                            </select>
                        </div>

                        {frequency === 'weekly' && (
                            <div>
                                <label htmlFor="create-automation-weekday" className="block text-sm font-medium text-gray-700 mb-1">
                                    {t('weekdayLabel')}
                                </label>
                                <select
                                    id="create-automation-weekday"
                                    value={selectedWeekday}
                                    onChange={(e) => setSelectedWeekday(e.target.value)}
                                    className="w-full h-10 px-4 bg-white border border-gray-200 rounded-lg text-sm text-gray-700 focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500"
                                >
                                    {WEEKDAYS.map((d) => (
                                        <option key={d} value={d}>{d.charAt(0).toUpperCase() + d.slice(1)}</option>
                                    ))}
                                </select>
                            </div>
                        )}

                        <div>
                            <label htmlFor="create-automation-time" className="block text-sm font-medium text-gray-700 mb-1">
                                {t('timeLabel')}
                            </label>
                            <input
                                id="create-automation-time"
                                type="text"
                                value={timeStr}
                                onChange={(e) => setTimeStr(e.target.value)}
                                placeholder="HH:MM"
                                className="w-full px-4 py-3 rounded-xl bg-white border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-transparent"
                            />
                        </div>

                        <div>
                            <label htmlFor="create-automation-prompt" className="block text-sm font-medium text-gray-700 mb-1">
                                {t('promptLabel')}
                            </label>
                            <textarea
                                id="create-automation-prompt"
                                value={prompt}
                                onChange={(e) => setPrompt(e.target.value)}
                                placeholder={t('promptPlaceholder')}
                                rows={5}
                                className="w-full px-4 py-3 rounded-xl bg-white border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-transparent resize-y min-h-[100px]"
                                required
                            />
                        </div>

                        <div>
                            <label htmlFor="create-automation-name" className="block text-sm font-medium text-gray-700 mb-1">
                                {t('nameLabel')}
                            </label>
                            <input
                                id="create-automation-name"
                                type="text"
                                value={name}
                                onChange={(e) => setName(e.target.value)}
                                placeholder={t('namePlaceholder')}
                                className="w-full px-4 py-3 rounded-xl bg-white border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-transparent"
                            />
                        </div>

                        {error && (
                            <div className="rounded-lg bg-red-100 text-red-600 text-sm px-4 py-2">
                                {error}
                            </div>
                        )}
                    </div>

                    <div className="flex items-center justify-between gap-3 p-6 border-t border-gray-200 bg-gray-50">
                        {isEditMode && onDelete ? (
                            <button
                                type="button"
                                onClick={() => { if (window.confirm(tAutomations('confirmDelete'))) { onDelete(editTask!.id); } }}
                                className="px-4 py-2 rounded-lg text-red-600 hover:bg-red-50 transition-colors font-medium inline-flex items-center gap-2"
                            >
                                <Trash2 className="w-4 h-4" />
                                {tAutomations('delete')}
                            </button>
                        ) : (
                            <div />
                        )}
                        <div className="flex-1" />
                        <button
                            type="button"
                            onClick={onClose}
                            className="px-4 py-2 rounded-lg text-gray-600 hover:bg-gray-200 transition-colors font-medium"
                        >
                            {t('cancel')}
                        </button>
                        <button
                            type="submit"
                            disabled={loading}
                            className="bg-gray-900 hover:bg-gray-800 text-white px-6 py-2 rounded-lg font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                            {loading ? t('creating') : isEditMode ? t('save') : t('create')}
                        </button>
                    </div>
                </form>
            </div>
        </div>
    );
}
