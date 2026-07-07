'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import { useState, useEffect, useMemo } from 'react';
import { useTranslations } from 'next-intl';
import { X, ChevronRight, Zap, Trash2, Plus } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useIsMobile } from '@/hooks/useIsMobile';
import CreateAutomationPopup, { type CreateAutomationPayload } from './CreateAutomationPopup';

export type AutomationNoteItem = { id: string; title?: string | null; content: string; created_at: string };
export type AutomationTodoItem = { id: string; text: string; created_at: string; due_at?: string | null; done: boolean };

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
    /** Notes for the automation planner (per user). */
    automationNotes?: AutomationNoteItem[];
    /** To-dos for the automation planner (per user). */
    automationTodos?: AutomationTodoItem[];
    /** Send WebSocket message for planner (notes/todos create/update/delete). */
    onSendPlannerMessage?: (msg: object) => void;
    /** User's time format from settings for timestamp display. */
    userTimeFormat?: '24h' | '12h';
    /** When provided, clicking an hour slot opens CreateAutomationPopup and this is used to submit. */
    onSubmitCreateAutomation?: (payload: CreateAutomationPayload) => Promise<{ ok: boolean; error?: string }>;
    /** Called after an automation was created (e.g. to refresh list). */
    onAutomationCreated?: () => void;
    /** When provided, clicking an automation chip in the day view opens edit for that automation. */
    onEditAutomation?: (automation: CalendarAutomationItem) => void;
}

function formatPlannerDate(iso: string, userTimeFormat?: '24h' | '12h'): string {
    try {
        const d = new Date(iso);
        const hour12 = userTimeFormat === '12h';
        return d.toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'short', hour12 });
    } catch {
        return iso;
    }
}

export default function AutomationCalendarModal({ isOpen, onClose, currentUser, automations = [], automationNotes = [], automationTodos = [], onSendPlannerMessage, userTimeFormat, onSubmitCreateAutomation, onAutomationCreated, onEditAutomation }: AutomationCalendarModalProps) {
    const t = useTranslations('settings.automations');
    const isMobile = useIsMobile();
    const [automationCalendarViewDate, setAutomationCalendarViewDate] = useState(() => new Date());
    const [selectedDayForView, setSelectedDayForView] = useState<Date | null>(null);

    // Pre-compute all 24 hour slots once — avoids re-running automationsAtSlot 24× on every render
    const dayViewSlots = useMemo(() => {
        if (!selectedDayForView) return null;
        return Array.from({ length: 24 }, (_, h) => ({
            h,
            slotAutomations: automationsAtSlot(automations, selectedDayForView, h),
        }));
    }, [selectedDayForView, automations]);

    // Pre-compute all month calendar cells once — avoids running automationsOnDay 35× on every render
    const monthCells = useMemo(() => {
        const y = automationCalendarViewDate.getFullYear();
        const m = automationCalendarViewDate.getMonth();
        const firstWeekday = (new Date(y, m, 1).getDay() + 6) % 7;
        const daysInMonth = new Date(y, m + 1, 0).getDate();
        const today = new Date();
        const isCurrentMonth = today.getFullYear() === y && today.getMonth() === m;
        const cells: (number | null)[] = [
            ...Array(firstWeekday).fill(null),
            ...Array.from({ length: daysInMonth }, (_, i) => i + 1),
        ];
        while (cells.length % 7 !== 0) cells.push(null);
        return { y, m, isCurrentMonth, today, cells, numRows: Math.ceil(cells.length / 7) };
    }, [automationCalendarViewDate]);

    // Per-cell automation dots — separate memo so cell list and automation data decouple
    const cellAutomations = useMemo(() => {
        const { y, m, cells } = monthCells;
        return cells.map(day =>
            day !== null ? automationsOnDay(automations, y, m, day) : []
        );
    }, [monthCells, automations]);
    const [selectedSlot, setSelectedSlot] = useState<{ date: Date; hour: number } | null>(null);
    const [showAddTodoPopup, setShowAddTodoPopup] = useState(false);
    const [addTodoText, setAddTodoText] = useState('');
    const [addTodoDueAt, setAddTodoDueAt] = useState('');
    const [showAddNotePopup, setShowAddNotePopup] = useState(false);
    const [addNoteTitle, setAddNoteTitle] = useState('');
    const [addNoteContent, setAddNoteContent] = useState('');

    useEffect(() => {
        if (!isOpen) return;
        const handleKeyDown = (e: KeyboardEvent) => {
            if (e.key !== 'Escape') return;
            if (showAddNotePopup) {
                setShowAddNotePopup(false);
                setAddNoteTitle('');
                setAddNoteContent('');
                e.stopPropagation();
                e.preventDefault();
                return;
            }
            if (showAddTodoPopup) {
                setShowAddTodoPopup(false);
                setAddTodoText('');
                setAddTodoDueAt('');
                e.stopPropagation();
                e.preventDefault();
                return;
            }
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
    }, [isOpen, showAddNotePopup, showAddTodoPopup, selectedDayForView, selectedSlot, onClose]);

    if (!isOpen) return null;

    // The notes panel renders in two places: on desktop as a full-width strip BELOW the body
    // (compact=false), on mobile stacked between the to-do list and the calendar (compact=true,
    // giving the requested To-do → Notes → Calendar order). Same inner content; only the wrapper's
    // direction/sizing differs.
    const notesPanel = (compact: boolean) => (
        <div
            className={compact
                ? "rounded-xl border-2 border-dashed border-gray-200 bg-gray-50 p-4 flex flex-col gap-3"
                : "shrink-0 mx-4 mb-4 rounded-xl border-2 border-dashed border-gray-200 bg-gray-50 p-4 flex flex-row gap-4 h-[160px]"}
            style={{ backgroundImage: 'radial-gradient(circle, #d1d5db 1.5px, transparent 1.5px)', backgroundSize: '10px 10px' }}
        >
            <div className="shrink-0 flex flex-col">
                <h3 className="text-sm font-medium text-gray-700 mb-1.5 shrink-0">Notes</h3>
                <button
                    type="button"
                    onClick={() => setShowAddNotePopup(true)}
                    className="shrink-0 flex items-center gap-1 text-xs font-medium text-gray-600 hover:text-gray-800 mb-0"
                >
                    <Plus className="w-3.5 h-3.5" /> Add note
                </button>
            </div>
            <div className="flex-1 min-w-0 overflow-x-auto overflow-y-hidden flex flex-nowrap gap-2 items-center">
                {automationNotes.map((note) => (
                    <div key={note.id} className="flex flex-col p-3 rounded-lg bg-white border border-gray-200 min-w-[180px] max-w-[280px] max-h-[120px] shrink-0 overflow-hidden">
                        <div className="flex items-start justify-between gap-2 shrink-0">
                            {note.title ? <span className="text-sm font-medium text-gray-800 min-w-0 flex-1 truncate">{note.title}</span> : <span className="flex-1" />}
                            <button
                                type="button"
                                onClick={() => onSendPlannerMessage?.({ type: 'delete_automation_note', id: note.id })}
                                className="p-1 text-gray-400 hover:text-red-600 shrink-0"
                                title="Delete"
                            >
                                <Trash2 className="w-3.5 h-3.5" />
                            </button>
                        </div>
                        <div className="flex-1 min-h-0 overflow-y-auto mt-0.5">
                            <p className="text-sm text-gray-700 break-words pr-0.5">{note.content}</p>
                            <p className="text-xs text-gray-500 mt-1 shrink-0">{formatPlannerDate(note.created_at, userTimeFormat)}</p>
                        </div>
                    </div>
                ))}
            </div>
        </div>
    );

    return (
        <div className="fixed inset-0 z-[60] flex items-center justify-center p-4 max-md:p-0" onClick={onClose}>
            <div className="absolute inset-0 bg-black/50" />
            <div
                className="relative w-full max-w-[95vw] h-[90vh] rounded-2xl shadow-2xl border border-gray-200 flex flex-col animate-in fade-in zoom-in-95 duration-200 overflow-hidden bg-white max-md:max-w-none max-md:h-[100dvh] max-md:rounded-none max-md:border-0 max-md:overflow-y-auto"
                onClick={(e) => e.stopPropagation()}
            >
                {/* Planner header: User, Months, Title + Close */}
                <div className="flex items-center shrink-0 px-4 py-3 border-b border-gray-200 gap-4 max-md:flex-wrap max-md:gap-2 max-md:px-3 max-md:sticky max-md:top-0 max-md:z-20 max-md:bg-white">
                    <div className="flex items-center gap-2 shrink-0">
                        <div className="w-9 h-9 rounded-full bg-gray-200 flex items-center justify-center text-gray-700 font-semibold text-sm">
                            {(currentUser?.username ?? 'User').slice(0, 1).toUpperCase()}
                        </div>
                        <span className="text-sm font-medium text-gray-900 truncate">{currentUser?.username ?? 'User'}</span>
                    </div>
                    <div className="flex flex-wrap items-center justify-center gap-1.5 flex-1 min-w-0 max-md:order-last max-md:w-full max-md:flex-none max-md:flex-nowrap max-md:overflow-x-auto max-md:justify-start max-md:pb-1">
                        {['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'].map((label, i) => {
                            const isSelected = automationCalendarViewDate.getMonth() === i;
                            const isActualMonth = automationCalendarViewDate.getFullYear() === new Date().getFullYear() && new Date().getMonth() === i;
                            return (
                                <button
                                    key={label}
                                    type="button"
                                    onClick={() => setAutomationCalendarViewDate(d => new Date(d.getFullYear(), i))}
                                    className={cn(
                                        'px-3.5 py-2 rounded-lg text-xs font-medium transition-colors shrink-0',
                                        isSelected ? 'bg-gray-900 text-white dark:bg-[#3a3a3a] dark:text-white' : 'bg-gray-100 text-gray-700 hover:bg-gray-200',
                                        isActualMonth && 'ring-2 ring-red-500'
                                    )}
                                >
                                    {label}
                                </button>
                            );
                        })}
                    </div>
                    <div className="flex items-center gap-2 shrink-0 max-md:ml-auto">
                        <h2 className="text-lg font-bold text-gray-900 truncate max-md:text-base">Automations {automationCalendarViewDate.getFullYear()}</h2>
                        <button onClick={onClose} className="p-2 hover:bg-gray-100 rounded-lg transition-colors text-gray-500 hover:text-gray-700" title="Close">
                            <X size={18} />
                        </button>
                    </div>
                </div>
                {/* Body: To-do (left) + Calendar (center) */}
                <div className="flex-1 flex gap-4 min-h-0 p-4 overflow-auto max-md:flex-col max-md:p-3 max-md:flex-none max-md:overflow-visible">
                    <div
                        className="min-w-[220px] w-[220px] shrink-0 rounded-xl border-2 border-dashed border-gray-200 bg-gray-50 p-4 flex flex-col min-h-[200px] max-md:w-full max-md:min-w-0"
                        style={{ backgroundImage: 'radial-gradient(circle, #d1d5db 1.5px, transparent 1.5px)', backgroundSize: '10px 10px' }}
                    >
                        <h3 className="text-sm font-medium text-gray-700 mb-1.5 shrink-0">To-do list</h3>
                        <p className="text-xs text-gray-500 shrink-0 mb-2">Quick tasks beside your calendar.</p>
                        <button
                            type="button"
                            onClick={() => setShowAddTodoPopup(true)}
                            className="shrink-0 flex items-center gap-1 text-xs font-medium text-gray-600 hover:text-gray-800 mb-2"
                        >
                            <Plus className="w-3.5 h-3.5" /> Add to-do
                        </button>
                        <div className="flex-1 min-h-0 overflow-auto space-y-2">
                            {automationTodos.map((todo) => (
                                <div key={todo.id} className="flex items-start gap-2 p-2 rounded-lg bg-white border border-gray-200">
                                    <input
                                        type="checkbox"
                                        checked={!!todo.done}
                                        onChange={() => onSendPlannerMessage?.({ type: 'update_automation_todo', id: todo.id, done: !todo.done })}
                                        className="mt-0.5 shrink-0 rounded border-gray-300"
                                    />
                                    <div className="flex-1 min-w-0">
                                        <span className={cn('text-sm', todo.done && 'line-through text-gray-500')}>{todo.text}</span>
                                        <p className="text-xs text-gray-500 mt-0.5">
                                            {formatPlannerDate(todo.created_at, userTimeFormat)}
                                            {todo.due_at ? ` · Due: ${formatPlannerDate(todo.due_at, userTimeFormat)}` : ''}
                                        </p>
                                    </div>
                                    <button
                                        type="button"
                                        onClick={() => onSendPlannerMessage?.({ type: 'delete_automation_todo', id: todo.id })}
                                        className="p-1 text-gray-400 hover:text-red-600 shrink-0"
                                        title="Delete"
                                    >
                                        <Trash2 className="w-3.5 h-3.5" />
                                    </button>
                                </div>
                            ))}
                        </div>
                    </div>
                    {/* mobile: Notes sit between the to-do list and the calendar (To-do → Notes → Calendar) */}
                    {isMobile && notesPanel(true)}
                    <div className="flex-1 flex flex-col min-w-0 min-h-0 rounded-xl border-2 border-dashed border-gray-200 p-3 overflow-hidden max-md:flex-none max-md:min-h-[400px]">
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
                                            {(dayViewSlots ?? []).map(({ h, slotAutomations }) => {
                                                const isCurrentHourSlot = isToday && h === currentHour;
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
                                                            'flex items-center gap-3 px-3 py-2.5 border-b border-gray-100 last:border-b-0 min-h-[48px] rounded-lg transition-colors duration-150 cursor-pointer',
                                                            !isCurrentHourSlot && 'hover:bg-gray-100',
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
                                                                    className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md bg-gray-800 text-white text-xs font-medium truncate max-w-[180px] cursor-pointer hover:bg-gray-700 transition-colors dark:bg-[#3a3a3a] dark:text-gray-100"
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
                            <div
                                className="grid grid-cols-7 gap-px bg-gray-400 rounded-xl flex-1 min-h-0 w-full"
                                style={{ gridTemplateRows: `auto repeat(${monthCells.numRows}, minmax(0, 1fr))` }}
                            >
                                {['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map(day => (
                                    <div key={day} className="bg-gray-50 flex items-center justify-center text-xs font-medium text-gray-500 uppercase tracking-wide py-1.5">
                                        {day}
                                    </div>
                                ))}
                                {monthCells.cells.map((day, i) => {
                                    const { y, m, isCurrentMonth, today } = monthCells;
                                    const isSunday = (i % 7) === 6;
                                    const onThisDay = cellAutomations[i];
                                    const count = onThisDay.length;
                                    return (
                                        <div
                                            key={i}
                                            role="button"
                                            tabIndex={0}
                                            onClick={day !== null ? () => setSelectedDayForView(new Date(y, m, day)) : undefined}
                                            onKeyDown={day !== null ? (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setSelectedDayForView(new Date(y, m, day!)); } } : undefined}
                                            className={cn(
                                                'min-h-0 flex flex-col items-center justify-center text-sm rounded-lg transition-colors duration-150 relative',
                                                day === null
                                                    ? 'bg-gray-200 text-gray-400 cursor-default'
                                                    : 'bg-white text-gray-700 cursor-pointer hover:bg-gray-100',
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
                                })}
                            </div>
                        )}
                    </div>
                </div>
                {/* desktop: Notes as a full-width strip below the body */}
                {!isMobile && notesPanel(false)}
            </div>

            {showAddNotePopup && (
                <div className="fixed inset-0 z-[70] flex items-center justify-center p-4" onClick={() => { setShowAddNotePopup(false); setAddNoteTitle(''); setAddNoteContent(''); }}>
                    <div className="absolute inset-0 bg-black/50" />
                    <div
                        className="relative w-full max-w-md rounded-xl bg-white shadow-xl border border-gray-200 p-4"
                        onClick={(e) => e.stopPropagation()}
                    >
                        <h3 className="text-sm font-semibold text-gray-900 mb-3">Add note</h3>
                        <input
                            type="text"
                            value={addNoteTitle}
                            onChange={(e) => setAddNoteTitle(e.target.value)}
                            placeholder="Title (optional)"
                            className="w-full px-3 py-2 text-sm border border-gray-300 rounded-lg focus:ring-2 focus:ring-gray-400 focus:border-gray-400 mb-2"
                        />
                        <textarea
                            value={addNoteContent}
                            onChange={(e) => setAddNoteContent(e.target.value)}
                            placeholder="Content"
                            rows={4}
                            className="w-full px-3 py-2 text-sm border border-gray-300 rounded-lg focus:ring-2 focus:ring-gray-400 resize-none mb-4"
                        />
                        <div className="flex justify-end gap-2">
                            <button
                                type="button"
                                onClick={() => { setShowAddNotePopup(false); setAddNoteTitle(''); setAddNoteContent(''); }}
                                className="px-3 py-1.5 text-sm font-medium text-gray-600 hover:text-gray-800"
                            >
                                Cancel
                            </button>
                            <button
                                type="button"
                                onClick={() => {
                                    const content = addNoteContent.trim();
                                    if (content && onSendPlannerMessage) {
                                        onSendPlannerMessage({ type: 'create_automation_note', title: addNoteTitle.trim() || undefined, content });
                                        setAddNoteTitle('');
                                        setAddNoteContent('');
                                        setShowAddNotePopup(false);
                                    }
                                }}
                                className="px-3 py-1.5 text-sm font-medium bg-gray-800 text-white rounded-lg hover:bg-gray-700 dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-[#f5f5f5] dark:shadow-none"
                            >
                                Add
                            </button>
                        </div>
                    </div>
                </div>
            )}

            {showAddTodoPopup && (
                <div className="fixed inset-0 z-[70] flex items-center justify-center p-4" onClick={() => { setShowAddTodoPopup(false); setAddTodoText(''); setAddTodoDueAt(''); }}>
                    <div className="absolute inset-0 bg-black/50" />
                    <div
                        className="relative w-full max-w-md rounded-xl bg-white shadow-xl border border-gray-200 p-4"
                        onClick={(e) => e.stopPropagation()}
                    >
                        <h3 className="text-sm font-semibold text-gray-900 mb-3">Add to-do</h3>
                        <input
                            type="text"
                            value={addTodoText}
                            onChange={(e) => setAddTodoText(e.target.value)}
                            placeholder="To-do text"
                            className="w-full px-3 py-2 text-sm border border-gray-300 rounded-lg focus:ring-2 focus:ring-gray-400 focus:border-gray-400 mb-2"
                        />
                        <input
                            type="text"
                            value={addTodoDueAt}
                            onChange={(e) => setAddTodoDueAt(e.target.value)}
                            placeholder="Due date (optional, e.g. YYYY-MM-DD)"
                            className="w-full px-3 py-2 text-sm border border-gray-300 rounded-lg focus:ring-2 focus:ring-gray-400 focus:border-gray-400 mb-4"
                        />
                        <div className="flex justify-end gap-2">
                            <button
                                type="button"
                                onClick={() => { setShowAddTodoPopup(false); setAddTodoText(''); setAddTodoDueAt(''); }}
                                className="px-3 py-1.5 text-sm font-medium text-gray-600 hover:text-gray-800"
                            >
                                Cancel
                            </button>
                            <button
                                type="button"
                                onClick={() => {
                                    const text = addTodoText.trim();
                                    if (text && onSendPlannerMessage) {
                                        onSendPlannerMessage({
                                            type: 'create_automation_todo',
                                            text,
                                            ...(addTodoDueAt.trim() ? { due_at: addTodoDueAt.trim() } : {}),
                                        });
                                        setAddTodoText('');
                                        setAddTodoDueAt('');
                                        setShowAddTodoPopup(false);
                                    }
                                }}
                                className="px-3 py-1.5 text-sm font-medium bg-gray-800 text-white rounded-lg hover:bg-gray-700 dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-[#f5f5f5] dark:shadow-none"
                            >
                                Add
                            </button>
                        </div>
                    </div>
                </div>
            )}

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
