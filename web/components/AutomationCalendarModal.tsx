'use client';

import React, { useState, useEffect } from 'react';
import { useTranslations } from 'next-intl';
import { X, ChevronRight, Zap } from 'lucide-react';
import { cn } from '@/lib/utils';
import CreateAutomationPopup, { type CreateAutomationPayload } from './CreateAutomationPopup';

export type CalendarAutomationItem = {
    id: string;
    name: string;
    frequency: string;
    time: string;
    weekday?: string | null;
    day?: number | null;
    enabled?: boolean;
    next_run?: string;
    prompt?: string;
    description?: string;
};

const WEEKDAYS = ['sunday', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday'] as const;

function parseTimeHH(timeStr: string): number {
    const parts = (timeStr || '0:0').split(':');
    return Math.max(0, Math.min(23, parseInt(parts[0] || '0', 10) || 0));
}

/** Return automations that run at the given date and hour slot. */
function automationsAtSlot(
    automations: CalendarAutomationItem[],
    date: Date,
    hour: number
): CalendarAutomationItem[] {
    const dayOfMonth = date.getDate();
    const weekdayIndex = date.getDay();
    const weekdayStr = WEEKDAYS[weekdayIndex];
    return automations.filter((task) => {
        if (task.enabled === false) return false;
        const taskHour = parseTimeHH(task.time);
        if (task.frequency === 'daily') return taskHour === hour;
        if (task.frequency === 'weekly') return (task.weekday || '').toLowerCase() === weekdayStr && taskHour === hour;
        if (task.frequency === 'monthly') return (task.day ?? 0) === dayOfMonth && taskHour === hour;
        if (task.frequency === 'hourly') return true;
        if (task.frequency === 'once' && task.next_run) {
            try {
                const next = new Date(task.next_run);
                return next.getFullYear() === date.getFullYear() &&
                    next.getMonth() === date.getMonth() &&
                    next.getDate() === date.getDate() &&
                    next.getHours() === hour;
            } catch {
                return false;
            }
        }
        return false;
    });
}

/** Automations that run on the given calendar day (year, month 0-11, day 1-31). Only shown for today and future (no dots on past dates). */
function automationsOnDay(
    automations: CalendarAutomationItem[],
    year: number,
    month: number,
    day: number
): CalendarAutomationItem[] {
    const cellDate = new Date(year, month, day);
    const todayStart = new Date();
    todayStart.setHours(0, 0, 0, 0);
    if (cellDate.getTime() < todayStart.getTime()) return [];

    const weekdayIndex = cellDate.getDay();
    const weekdayStr = WEEKDAYS[weekdayIndex];
    return automations.filter((task) => {
        if (task.enabled === false) return false;
        if (task.frequency === 'daily' || task.frequency === 'hourly') return true;
        if (task.frequency === 'weekly') return (task.weekday || '').toLowerCase() === weekdayStr;
        if (task.frequency === 'monthly') return (task.day ?? 0) === day;
        if (task.frequency === 'once' && task.next_run) {
            try {
                const next = new Date(task.next_run);
                return next.getFullYear() === year && next.getMonth() === month && next.getDate() === day;
            } catch {
                return false;
            }
        }
        return false;
    });
}

export interface AutomationCalendarModalProps {
    isOpen: boolean;
    onClose: () => void;
    currentUser?: { username?: string };
    /** List of automations to show in the calendar (agent-created and manual). */
    automations?: CalendarAutomationItem[];
    /** When provided, clicking an hour slot opens CreateAutomationPopup and this is used to submit. */
    onSubmitCreateAutomation?: (payload: CreateAutomationPayload) => Promise<{ ok: boolean; error?: string }>;
    /** Called after an automation was created (e.g. to refresh list). */
    onAutomationCreated?: () => void;
    /** When provided, clicking an automation chip in the day view opens edit for that automation. */
    onEditAutomation?: (automation: CalendarAutomationItem) => void;
}

export default function AutomationCalendarModal({ isOpen, onClose, currentUser, automations = [], onSubmitCreateAutomation, onAutomationCreated, onEditAutomation }: AutomationCalendarModalProps) {
    const t = useTranslations('settings.automations');
    const [automationCalendarViewDate, setAutomationCalendarViewDate] = useState(() => new Date());
    const [selectedDayForView, setSelectedDayForView] = useState<Date | null>(null);
    const [selectedSlot, setSelectedSlot] = useState<{ date: Date; hour: number } | null>(null);

    useEffect(() => {
        if (!isOpen) return;
        const handleKeyDown = (e: KeyboardEvent) => {
            if (e.key !== 'Escape') return;
            if (selectedSlot) {
                setSelectedSlot(null);
                e.stopPropagation();
                e.preventDefault();
                return;
            }
            if (selectedDayForView) {
                setSelectedDayForView(null);
                e.stopPropagation();
                e.preventDefault();
                return;
            }
            onClose();
        };
        window.addEventListener('keydown', handleKeyDown);
        return () => window.removeEventListener('keydown', handleKeyDown);
    }, [isOpen, selectedDayForView, selectedSlot, onClose]);

    if (!isOpen) return null;

    return (
        <div className="fixed inset-0 z-[60] flex items-center justify-center p-4" onClick={onClose}>
            <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" />
            <div
                className="relative w-full max-w-[95vw] h-[90vh] rounded-2xl shadow-2xl border border-gray-200 flex flex-col animate-in fade-in zoom-in-95 duration-200 overflow-hidden bg-white"
                onClick={(e) => e.stopPropagation()}
            >
                {/* Planner header: User, Months, Title + Close */}
                <div className="flex items-center shrink-0 px-4 py-3 border-b border-gray-200 gap-4">
                    <div className="flex items-center gap-2 shrink-0">
                        <div className="w-9 h-9 rounded-full bg-gray-200 flex items-center justify-center text-gray-700 font-semibold text-sm">
                            {(currentUser?.username ?? 'User').slice(0, 1).toUpperCase()}
                        </div>
                        <span className="text-sm font-medium text-gray-900 truncate">{currentUser?.username ?? 'User'}</span>
                    </div>
                    <div className="flex flex-wrap items-center justify-center gap-1.5 flex-1 min-w-0">
                        {['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'].map((label, i) => {
                            const isSelected = automationCalendarViewDate.getMonth() === i;
                            const isActualMonth = automationCalendarViewDate.getFullYear() === new Date().getFullYear() && new Date().getMonth() === i;
                            return (
                                <button
                                    key={label}
                                    type="button"
                                    onClick={() => setAutomationCalendarViewDate(d => new Date(d.getFullYear(), i))}
                                    className={cn(
                                        'px-3.5 py-2 rounded-lg text-xs font-medium transition-colors',
                                        isSelected ? 'bg-gray-900 text-white' : 'bg-gray-100 text-gray-700 hover:bg-gray-200',
                                        isActualMonth && 'ring-2 ring-red-500'
                                    )}
                                >
                                    {label}
                                </button>
                            );
                        })}
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                        <h2 className="text-lg font-bold text-gray-900 truncate">Automations {automationCalendarViewDate.getFullYear()}</h2>
                        <button onClick={onClose} className="p-2 hover:bg-gray-100 rounded-lg transition-colors text-gray-500 hover:text-gray-700" title="Close">
                            <X size={18} />
                        </button>
                    </div>
                </div>
                {/* Body: To-do (left) + Calendar (center) */}
                <div className="flex-1 flex gap-4 min-h-0 p-4 overflow-auto">
                    <div
                        className="min-w-[220px] w-[220px] shrink-0 rounded-xl border-2 border-dashed border-gray-200 bg-gray-50 p-4 flex flex-col min-h-[200px]"
                        style={{ backgroundImage: 'radial-gradient(circle, #d1d5db 1.5px, transparent 1.5px)', backgroundSize: '10px 10px' }}
                    >
                        <h3 className="text-sm font-medium text-gray-700 mb-1.5 shrink-0">To-do list</h3>
                        <p className="text-xs text-gray-500 shrink-0">Quick tasks beside your calendar.</p>
                        <div className="flex-1 min-h-[100px]" />
                    </div>
                    <div className="flex-1 flex flex-col min-w-0 min-h-0 rounded-xl border-2 border-dashed border-gray-200 p-3 overflow-hidden">
                        {selectedDayForView ? (
                            (() => {
                                const now = new Date();
                                const isToday = selectedDayForView.getFullYear() === now.getFullYear() &&
                                    selectedDayForView.getMonth() === now.getMonth() &&
                                    selectedDayForView.getDate() === now.getDate();
                                const currentHour = now.getHours();
                                const currentTimeStr = now.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', hour12: false });
                                return (
                                    <>
                                        <div className="flex items-center justify-between gap-2 shrink-0 mb-3">
                                            <button
                                                type="button"
                                                onClick={() => setSelectedDayForView(null)}
                                                className="flex items-center gap-1.5 px-2.5 py-1.5 text-sm font-medium text-gray-600 hover:bg-gray-100 rounded-lg transition-colors"
                                            >
                                                <ChevronRight size={16} className="rotate-180" />
                                                Monat
                                            </button>
                                            <h3 className="text-sm font-semibold text-gray-900">
                                                {selectedDayForView.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' })}
                                            </h3>
                                        </div>
                                        <div className="flex-1 overflow-auto min-h-0 border border-gray-200 rounded-lg bg-white">
                                            {Array.from({ length: 24 }, (_, h) => {
                                                const isCurrentHourSlot = isToday && h === currentHour;
                                                const slotAutomations = selectedDayForView ? automationsAtSlot(automations, selectedDayForView, h) : [];
                                                return (
                                                    <div
                                                        key={h}
                                                        role="button"
                                                        tabIndex={0}
                                                        onClick={() => selectedDayForView && setSelectedSlot({ date: selectedDayForView, hour: h })}
                                                        onKeyDown={(e) => {
                                                            if ((e.key === 'Enter' || e.key === ' ') && selectedDayForView) {
                                                                e.preventDefault();
                                                                setSelectedSlot({ date: selectedDayForView, hour: h });
                                                            }
                                                        }}
                                                        className={cn(
                                                            'flex items-center gap-3 px-3 py-2.5 border-b border-gray-100 last:border-b-0 min-h-[48px] rounded-lg transition-all duration-200 cursor-pointer',
                                                            'hover:shadow-[0_0_16px_4px_rgba(0,0,0,0.12)] hover:z-10',
                                                            !isCurrentHourSlot && 'hover:bg-gray-50/80',
                                                            isCurrentHourSlot && 'ring-2 ring-red-500 ring-inset bg-red-50/30'
                                                        )}
                                                    >
                                                        <span className={cn('text-sm font-medium w-12 shrink-0', isCurrentHourSlot ? 'text-red-600 font-semibold' : 'text-gray-500')}>
                                                            {String(h).padStart(2, '0')}:00
                                                            {isCurrentHourSlot && (
                                                                <span className="block text-xs font-normal text-red-600 mt-0.5">{currentTimeStr}</span>
                                                            )}
                                                        </span>
                                                        <div className={cn(
                                                            'flex-1 min-h-[32px] rounded border border-dashed flex flex-wrap items-center gap-1.5 p-1',
                                                            isCurrentHourSlot ? 'bg-red-50/50 border-red-200' : 'bg-gray-50/50 border-gray-200'
                                                        )}>
                                                            {slotAutomations.map((auto) => (
                                                                <span
                                                                    key={auto.id}
                                                                    role="button"
                                                                    tabIndex={0}
                                                                    onClick={(e) => { e.stopPropagation(); onEditAutomation?.(auto); }}
                                                                    onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); onEditAutomation?.(auto); } }}
                                                                    className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md bg-gray-800 text-white text-xs font-medium truncate max-w-[180px] cursor-pointer hover:bg-gray-700 transition-colors"
                                                                    title={t('editSlotTooltip', { name: auto.name })}
                                                                >
                                                                    <Zap className="w-3 h-3 shrink-0" />
                                                                    {auto.name}
                                                                </span>
                                                            ))}
                                                        </div>
                                                    </div>
                                                );
                                            })}
                                        </div>
                                    </>
                                );
                            })()
                        ) : (
                            (() => {
                                const y = automationCalendarViewDate.getFullYear();
                                const m = automationCalendarViewDate.getMonth();
                                const firstWeekday = (new Date(y, m, 1).getDay() + 6) % 7;
                                const daysInMonth = new Date(y, m + 1, 0).getDate();
                                const numRows = Math.ceil((firstWeekday + daysInMonth) / 7);
                                return (
                                    <div
                                        className="grid grid-cols-7 gap-px bg-gray-400 rounded-xl flex-1 min-h-0 w-full"
                                        style={{ gridTemplateRows: `auto repeat(${numRows}, minmax(0, 1fr))` }}
                                    >
                                        {['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map(day => (
                                            <div key={day} className="bg-gray-50 flex items-center justify-center text-xs font-medium text-gray-500 uppercase tracking-wide py-1.5">
                                                {day}
                                            </div>
                                        ))}
                                        {(() => {
                                            const today = new Date();
                                            const isCurrentMonth = today.getFullYear() === y && today.getMonth() === m;
                                            const cells: (number | null)[] = [
                                                ...Array(firstWeekday).fill(null),
                                                ...Array.from({ length: daysInMonth }, (_, i) => i + 1)
                                            ];
                                            while (cells.length % 7 !== 0) cells.push(null);
                                            return cells.map((day, i) => {
                                                const isSunday = (i % 7) === 6;
                                                const onThisDay = day !== null ? automationsOnDay(automations, y, m, day) : [];
                                                const count = onThisDay.length;
                                                return (
                                                    <div
                                                        key={i}
                                                        role="button"
                                                        tabIndex={0}
                                                        onClick={day !== null ? () => setSelectedDayForView(new Date(y, m, day)) : undefined}
                                                        onKeyDown={day !== null ? (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setSelectedDayForView(new Date(y, m, day!)); } } : undefined}
                                                        className={cn(
                                                            'min-h-0 flex flex-col items-center justify-center text-sm rounded-lg transition-all duration-200 relative',
                                                            day === null
                                                                ? 'bg-gray-200 text-gray-400 cursor-default'
                                                                : 'bg-white text-gray-700 cursor-pointer hover:bg-gray-50 hover:scale-105 hover:shadow-[0_0_16px_4px_rgba(0,0,0,0.12)] hover:z-10',
                                                            day !== null && isSunday && 'text-red-600',
                                                            day !== null && isCurrentMonth && day === today.getDate() && 'font-semibold text-gray-900 ring-2 ring-red-500 ring-inset'
                                                        )}
                                                    >
                                                        <span>{day ?? ''}</span>
                                                        {count > 0 && (
                                                            <span className="mt-0.5 flex gap-0.5 flex-wrap justify-center max-w-full" title={count === 1 ? onThisDay[0].name : `${count} automations`}>
                                                                {onThisDay.slice(0, 5).map((a) => (
                                                                    <span key={a.id} className="w-1.5 h-1.5 rounded-full bg-gray-700 shrink-0" />
                                                                ))}
                                                            </span>
                                                        )}
                                                    </div>
                                                );
                                            });
                                        })()}
                                    </div>
                                );
                            })()
                        )}
                    </div>
                </div>
                <div
                    className="shrink-0 mx-4 mb-4 rounded-xl border-2 border-dashed border-gray-200 bg-gray-50 flex flex-col items-center justify-center h-[150px] py-3"
                    style={{ backgroundImage: 'radial-gradient(circle, #d1d5db 1.5px, transparent 1.5px)', backgroundSize: '10px 10px' }}
                >
                    <span className="text-sm font-medium text-gray-600">Note</span>
                </div>
            </div>

            {selectedSlot && (
                <CreateAutomationPopup
                    isOpen={true}
                    onClose={() => setSelectedSlot(null)}
                    initialDate={selectedSlot.date}
                    initialHour={selectedSlot.hour}
                    onCreated={() => {
                        setSelectedSlot(null);
                        onAutomationCreated?.();
                    }}
                    onSubmit={onSubmitCreateAutomation}
                />
            )}
        </div>
    );
}
