'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
//
// The calendar window: the VAF calendar (appointments, internal and synced from Google
// or Outlook) and the scheduled automations side by side, with the per-user planner
// (to-dos, notes). Design: docs/integrations/CALENDAR_INTEGRATION.md and
// docs/platform/AUTOMATIONS.md. Events come from GET /api/calendar/events for the
// visible month and are refetched after every write here and on every
// `calendar_changed` frame (the `calendarVersion` prop); automations, notes and to-dos
// arrive over the WebSocket as before.

import { useState, useEffect, useMemo, useCallback, useRef } from 'react';
import { useTranslations } from 'next-intl';
import { X, ChevronRight, Zap, Trash2, Plus, ExternalLink, Loader2, MapPin, Users } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useIsMobile } from '@/hooks/useIsMobile';
import { useEscapeLayer } from '@/hooks/useEscapeLayer';
import ConfirmDialog from '@/components/ui/ConfirmDialog';
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

/** One event of the VAF calendar as GET /api/calendar/events returns it. */
export type CalendarEventItem = {
    id: string;
    title: string;
    description?: string | null;
    location?: string | null;
    start: string;
    end: string;
    start_ts: number;
    end_ts: number;
    all_day: boolean;
    start_date?: string | null;
    end_date?: string | null;
    tz?: string | null;
    status: string;
    source: string;
    account_id?: string | null;
    sync_state: string;
    last_error?: string | null;
    link?: string | null;
    contact_ids: string[];
    reminder_minutes?: number | null;
    created_by?: string;
};

type CalendarStatus = {
    has_calendar: boolean;
    accounts: Array<{ account_id: string; email: string; provider: string; enabled: boolean }>;
    settings: { push_target: string | null; default_reminder_minutes: number };
    sync: { interval_minutes: number; push_enabled: boolean; supervisor_running: boolean };
};

type ContactOption = { id: string; name: string };

type EventDraft = {
    mode: 'new' | 'edit';
    source?: CalendarEventItem;
    title: string;
    date: string;        // YYYY-MM-DD
    start: string;       // HH:MM
    end: string;         // HH:MM
    allDay: boolean;
    location: string;
    description: string;
    contactIds: string[];
    reminder: number;    // minutes before; 0 = none
    mirror: boolean;     // create only: also write into the connected calendar
};

const WEEKDAYS = ['sunday', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday'] as const;
const REMINDER_CHOICES = [0, 5, 15, 30, 60, 1440];
const HOUR_REM = 3;   // one hour row in the day view is 3rem tall

const api = (path: string) => (path.startsWith('/') ? path : `/${path}`);

function parseTimeHH(timeStr: string): number {
    const parts = (timeStr || '0:0').split(':');
    return Math.max(0, Math.min(23, parseInt(parts[0] || '0', 10) || 0));
}

function pad2(n: number): string {
    return String(n).padStart(2, '0');
}

function dateKey(d: Date): string {
    return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

function sameDay(a: Date, b: Date): boolean {
    return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

/** Return automations that run at the given date and hour slot. */
function automationsAtSlot(automations: CalendarAutomationItem[], date: Date, hour: number): CalendarAutomationItem[] {
    const dayOfMonth = date.getDate();
    const weekdayStr = WEEKDAYS[date.getDay()];
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
                return sameDay(next, date) && next.getHours() === hour;
            } catch {
                return false;
            }
        }
        return false;
    });
}

/** Automations that run on the given calendar day. Only today and the future: an automation
 *  has no past, its runs are not records. Events (below) are shown on past days too. */
function automationsOnDay(automations: CalendarAutomationItem[], year: number, month: number, day: number): CalendarAutomationItem[] {
    const cellDate = new Date(year, month, day);
    const todayStart = new Date();
    todayStart.setHours(0, 0, 0, 0);
    if (cellDate.getTime() < todayStart.getTime()) return [];
    const weekdayStr = WEEKDAYS[cellDate.getDay()];
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

/** The events that touch a calendar day (an all-day event by its dates, a timed one by its instants). */
function eventsOnDay(events: CalendarEventItem[], year: number, month: number, day: number): CalendarEventItem[] {
    const key = `${year}-${pad2(month + 1)}-${pad2(day)}`;
    const dayStart = new Date(year, month, day).getTime() / 1000;
    const dayEnd = dayStart + 86400;
    return events.filter((ev) => {
        if (ev.all_day) {
            const sd = ev.start_date || ev.start.slice(0, 10);
            const ed = ev.end_date || sd;
            return sd <= key && key < ed;
        }
        return ev.start_ts < dayEnd && ev.end_ts > dayStart;
    });
}

type Placed = { ev: CalendarEventItem; top: number; height: number; col: number; cols: number };

/** Lay the day's timed events out: top and height in hour rows, overlapping ones side by side. */
function layoutDay(events: CalendarEventItem[], day: Date): Placed[] {
    const dayStart = new Date(day.getFullYear(), day.getMonth(), day.getDate()).getTime() / 1000;
    const items = events
        .filter((ev) => !ev.all_day)
        .map((ev) => {
            const s = Math.max(0, (ev.start_ts - dayStart) / 3600);
            const e = Math.min(24, (ev.end_ts - dayStart) / 3600);
            return { ev, top: s, height: Math.max(0.5, e - s) };
        })
        .sort((a, b) => a.top - b.top || b.height - a.height);
    const placed: Placed[] = [];
    let cluster: Placed[] = [];
    let clusterEnd = -1;
    const flush = () => {
        const cols = cluster.reduce((m, p) => Math.max(m, p.col + 1), 1);
        for (const p of cluster) p.cols = cols;
        placed.push(...cluster);
        cluster = [];
    };
    for (const item of items) {
        if (cluster.length && item.top >= clusterEnd) flush();
        const taken = new Set(cluster.filter((p) => p.top + p.height > item.top).map((p) => p.col));
        let col = 0;
        while (taken.has(col)) col += 1;
        cluster.push({ ...item, col, cols: 1 });
        clusterEnd = Math.max(clusterEnd, item.top + item.height);
    }
    if (cluster.length) flush();
    return placed;
}

function timeLabel(d: Date, userTimeFormat?: '24h' | '12h'): string {
    return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', hour12: userTimeFormat === '12h' });
}

function formatPlannerDate(iso: string, userTimeFormat?: '24h' | '12h'): string {
    try {
        const d = new Date(iso);
        return d.toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'short', hour12: userTimeFormat === '12h' });
    } catch {
        return iso;
    }
}

function eventTimeRange(ev: CalendarEventItem, userTimeFormat?: '24h' | '12h', allDayLabel?: string): string {
    if (ev.all_day) return allDayLabel || '';
    const s = new Date(ev.start);
    const e = new Date(ev.end);
    return `${timeLabel(s, userTimeFormat)} - ${timeLabel(e, userTimeFormat)}`;
}

const inputClass = 'w-full px-3 py-2 text-sm border border-gray-300 rounded-lg focus:ring-2 focus:ring-gray-400 focus:border-gray-400 bg-white text-gray-900';
const primaryButtonClass = 'px-3 py-1.5 text-sm font-medium bg-gray-800 text-white rounded-lg hover:bg-gray-700 dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-[#f5f5f5] dark:shadow-none disabled:opacity-50';
const ghostButtonClass = 'px-3 py-1.5 text-sm font-medium text-gray-600 hover:text-gray-800';

export interface AutomationCalendarModalProps {
    isOpen: boolean;
    onClose: () => void;
    currentUser?: { username?: string };
    /** List of automations to show in the calendar (agent-created and manual). */
    automations?: CalendarAutomationItem[];
    /** Notes for the planner (per user). */
    automationNotes?: AutomationNoteItem[];
    /** To-dos for the planner (per user). */
    automationTodos?: AutomationTodoItem[];
    /** Send WebSocket message for planner (notes/todos create/update/delete). */
    onSendPlannerMessage?: (msg: object) => void;
    /** User's time format from settings for timestamp display. */
    userTimeFormat?: '24h' | '12h';
    /** When provided, clicking an hour slot in the automations lane opens CreateAutomationPopup and this is used to submit. */
    onSubmitCreateAutomation?: (payload: CreateAutomationPayload) => Promise<{ ok: boolean; error?: string }>;
    /** Called after an automation was created (e.g. to refresh list). */
    onAutomationCreated?: () => void;
    /** When provided, clicking an automation chip in the day view opens edit for that automation. */
    onEditAutomation?: (automation: CalendarAutomationItem) => void;
    /** Bumped by the page on every `calendar_changed` frame: the visible month is refetched. */
    calendarVersion?: number;
    /** Open the window on this day (unix seconds), e.g. from a contact's upcoming event. */
    initialDayTs?: number | null;
}

export default function AutomationCalendarModal({
    isOpen, onClose, currentUser, automations = [], automationNotes = [], automationTodos = [], onSendPlannerMessage,
    userTimeFormat, onSubmitCreateAutomation, onAutomationCreated, onEditAutomation, calendarVersion = 0, initialDayTs = null,
}: AutomationCalendarModalProps) {
    const t = useTranslations('settings.automations');
    const isMobile = useIsMobile();
    const [viewDate, setViewDate] = useState(() => new Date());
    const [selectedDayForView, setSelectedDayForView] = useState<Date | null>(null);
    const [selectedSlot, setSelectedSlot] = useState<{ date: Date; hour: number } | null>(null);
    const [showAddTodoPopup, setShowAddTodoPopup] = useState(false);
    const [addTodoText, setAddTodoText] = useState('');
    const [addTodoDueAt, setAddTodoDueAt] = useState('');
    const [showAddNotePopup, setShowAddNotePopup] = useState(false);
    const [addNoteTitle, setAddNoteTitle] = useState('');
    const [addNoteContent, setAddNoteContent] = useState('');

    // ── the VAF calendar ─────────────────────────────────────────────────────
    const [events, setEvents] = useState<CalendarEventItem[]>([]);
    const [eventsLoading, setEventsLoading] = useState(false);
    const [eventsError, setEventsError] = useState(false);
    const [status, setStatus] = useState<CalendarStatus | null>(null);
    const [contacts, setContacts] = useState<ContactOption[]>([]);
    const [contactQuery, setContactQuery] = useState('');
    const [draft, setDraft] = useState<EventDraft | null>(null);
    const [saving, setSaving] = useState(false);
    const [saveError, setSaveError] = useState<'required' | 'time' | 'server' | null>(null);
    const [confirmDelete, setConfirmDelete] = useState<CalendarEventItem | null>(null);
    const fetchSeq = useRef(0);

    const fetchEvents = useCallback(async (y: number, m: number) => {
        const seq = ++fetchSeq.current;
        setEventsLoading(true);
        try {
            const timeMin = new Date(y, m, 1).toISOString();
            const timeMax = new Date(y, m + 1, 1).toISOString();
            const res = await fetch(api(`api/calendar/events?time_min=${encodeURIComponent(timeMin)}&time_max=${encodeURIComponent(timeMax)}`), { credentials: 'include' });
            if (!res.ok) throw new Error(String(res.status));
            const data = await res.json();
            if (seq !== fetchSeq.current) return;
            setEvents(Array.isArray(data?.events) ? data.events : []);
            setEventsError(false);
        } catch {
            if (seq !== fetchSeq.current) return;
            setEvents([]);
            setEventsError(true);
        } finally {
            if (seq === fetchSeq.current) setEventsLoading(false);
        }
    }, []);

    const fetchStatus = useCallback(async () => {
        try {
            const res = await fetch(api('api/calendar/status'), { credentials: 'include' });
            if (res.ok) setStatus(await res.json());
        } catch {
            // the status only decides the mirror toggle and the reminder default; the window works without it
        }
    }, []);

    useEffect(() => {
        if (!isOpen) return;
        fetchEvents(viewDate.getFullYear(), viewDate.getMonth());
    }, [isOpen, viewDate, calendarVersion, fetchEvents]);

    useEffect(() => {
        if (isOpen) fetchStatus();
    }, [isOpen, fetchStatus]);

    // A deep link (a contact's upcoming event) lands on its day.
    useEffect(() => {
        if (!isOpen || !initialDayTs) return;
        const d = new Date(initialDayTs * 1000);
        setViewDate(new Date(d.getFullYear(), d.getMonth(), 1));
        setSelectedDayForView(new Date(d.getFullYear(), d.getMonth(), d.getDate()));
    }, [isOpen, initialDayTs]);

    useEffect(() => {
        if (isOpen) return;
        setSelectedDayForView(null);
        setSelectedSlot(null);
        setDraft(null);
        setConfirmDelete(null);
        setSaveError(null);
    }, [isOpen]);

    useEffect(() => {
        if (!draft) return;
        let cancelled = false;
        (async () => {
            try {
                const res = await fetch(api('api/contacts'), { credentials: 'include' });
                const data = await res.json();
                const list = (Array.isArray(data) ? data : data?.contacts || []) as Array<{ id?: string; name?: string }>;
                if (!cancelled) setContacts(list.filter((c) => c.id && c.name).map((c) => ({ id: String(c.id), name: String(c.name) })));
            } catch {
                if (!cancelled) setContacts([]);
            }
        })();
        return () => { cancelled = true; };
    }, [draft !== null]);  // eslint-disable-line react-hooks/exhaustive-deps

    // ── memos ────────────────────────────────────────────────────────────────
    const dayViewSlots = useMemo(() => {
        if (!selectedDayForView) return null;
        return Array.from({ length: 24 }, (_, h) => ({ h, slotAutomations: automationsAtSlot(automations, selectedDayForView, h) }));
    }, [selectedDayForView, automations]);

    const dayEvents = useMemo(() => {
        if (!selectedDayForView) return { allDay: [] as CalendarEventItem[], placed: [] as Placed[] };
        const y = selectedDayForView.getFullYear(), m = selectedDayForView.getMonth(), d = selectedDayForView.getDate();
        const onDay = eventsOnDay(events, y, m, d).filter((ev) => ev.status !== 'cancelled');
        return { allDay: onDay.filter((ev) => ev.all_day), placed: layoutDay(onDay, selectedDayForView) };
    }, [selectedDayForView, events]);

    const monthCells = useMemo(() => {
        const y = viewDate.getFullYear();
        const m = viewDate.getMonth();
        const firstWeekday = (new Date(y, m, 1).getDay() + 6) % 7;
        const daysInMonth = new Date(y, m + 1, 0).getDate();
        const today = new Date();
        const isCurrentMonth = today.getFullYear() === y && today.getMonth() === m;
        const cells: (number | null)[] = [...Array(firstWeekday).fill(null), ...Array.from({ length: daysInMonth }, (_, i) => i + 1)];
        while (cells.length % 7 !== 0) cells.push(null);
        return { y, m, isCurrentMonth, today, cells, numRows: Math.ceil(cells.length / 7) };
    }, [viewDate]);

    const cellAutomations = useMemo(() => {
        const { y, m, cells } = monthCells;
        return cells.map((day) => (day !== null ? automationsOnDay(automations, y, m, day) : []));
    }, [monthCells, automations]);

    const cellEvents = useMemo(() => {
        const { y, m, cells } = monthCells;
        return cells.map((day) => (day !== null ? eventsOnDay(events, y, m, day).filter((ev) => ev.status !== 'cancelled') : []));
    }, [monthCells, events]);

    const monthLabels = useMemo(() => Array.from({ length: 12 }, (_, i) => new Date(2024, i, 1).toLocaleDateString(undefined, { month: 'short' })), []);
    // 2024-01-01 is a Monday: the week starts there, as the grid does.
    const weekdayLabels = useMemo(() => Array.from({ length: 7 }, (_, i) => new Date(2024, 0, 1 + i).toLocaleDateString(undefined, { weekday: 'short' })), []);

    const mirrorAccount = useMemo(() => {
        if (!status) return null;
        const enabled = status.accounts.filter((a) => a.enabled);
        if (!enabled.length) return null;
        return enabled.find((a) => a.account_id === status.settings.push_target) || enabled[0];
    }, [status]);

    // ── escape ladder: window, then what covers it ───────────────────────────
    const closeNotePopup = useCallback(() => { setShowAddNotePopup(false); setAddNoteTitle(''); setAddNoteContent(''); }, []);
    const closeTodoPopup = useCallback(() => { setShowAddTodoPopup(false); setAddTodoText(''); setAddTodoDueAt(''); }, []);
    const closeDraft = useCallback(() => { setDraft(null); setSaveError(null); setContactQuery(''); }, []);

    useEscapeLayer({ active: isOpen, level: 60, onEscape: () => { if (selectedDayForView) setSelectedDayForView(null); else onClose(); } });
    useEscapeLayer({ active: isOpen && showAddNotePopup, level: 71, onEscape: closeNotePopup });
    useEscapeLayer({ active: isOpen && showAddTodoPopup, level: 71, onEscape: closeTodoPopup });
    useEscapeLayer({ active: isOpen && selectedSlot !== null, level: 71, onEscape: () => setSelectedSlot(null) });
    useEscapeLayer({ active: isOpen && draft !== null, level: 72, onEscape: saving ? null : closeDraft });

    // ── drafts ───────────────────────────────────────────────────────────────
    const openNewDraft = useCallback((day: Date, hour: number, allDay = false) => {
        setSaveError(null);
        setDraft({
            mode: 'new',
            title: '',
            date: dateKey(day),
            start: `${pad2(hour)}:00`,
            end: `${pad2(Math.min(23, hour + 1))}:${hour >= 23 ? '59' : '00'}`,
            allDay,
            location: '',
            description: '',
            contactIds: [],
            reminder: status?.settings.default_reminder_minutes ?? 15,
            mirror: !!mirrorAccount,
        });
    }, [status, mirrorAccount]);

    const openEditDraft = useCallback((ev: CalendarEventItem) => {
        const s = new Date(ev.start);
        const e = new Date(ev.end);
        setSaveError(null);
        setDraft({
            mode: 'edit',
            source: ev,
            title: ev.title || '',
            date: ev.all_day ? (ev.start_date || ev.start.slice(0, 10)) : dateKey(s),
            start: ev.all_day ? '09:00' : `${pad2(s.getHours())}:${pad2(s.getMinutes())}`,
            end: ev.all_day ? '10:00' : `${pad2(e.getHours())}:${pad2(e.getMinutes())}`,
            allDay: !!ev.all_day,
            location: ev.location || '',
            description: ev.description || '',
            contactIds: [...(ev.contact_ids || [])],
            reminder: ev.reminder_minutes ?? 0,
            mirror: !!ev.account_id,
        });
    }, []);

    const saveDraft = useCallback(async () => {
        if (!draft) return;
        const title = draft.title.trim();
        if (!title) { setSaveError('required'); return; }
        if (!/^\d{4}-\d{2}-\d{2}$/.test(draft.date) || (!draft.allDay && (!/^\d{1,2}:\d{2}$/.test(draft.start) || !/^\d{1,2}:\d{2}$/.test(draft.end)))) {
            setSaveError('time');
            return;
        }
        const body: Record<string, unknown> = {
            title,
            start: draft.allDay ? draft.date : `${draft.date}T${draft.start}`,
            end: draft.allDay ? undefined : `${draft.date}T${draft.end}`,
            all_day: draft.allDay,
            description: draft.description,
            location: draft.location,
            contact_ids: draft.contactIds,
            reminder_minutes: draft.reminder,
        };
        if (draft.mode === 'new') body.internal_only = !draft.mirror;
        setSaving(true);
        setSaveError(null);
        try {
            const res = await fetch(
                api(draft.mode === 'new' ? 'api/calendar/events' : `api/calendar/events/${encodeURIComponent(draft.source!.id)}`),
                { method: draft.mode === 'new' ? 'POST' : 'PATCH', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) },
            );
            if (!res.ok) {
                setSaveError(res.status === 400 ? 'time' : 'server');
                return;
            }
            closeDraft();
            fetchEvents(viewDate.getFullYear(), viewDate.getMonth());
        } catch {
            setSaveError('server');
        } finally {
            setSaving(false);
        }
    }, [draft, closeDraft, fetchEvents, viewDate]);

    const deleteEvent = useCallback(async (ev: CalendarEventItem) => {
        setConfirmDelete(null);
        try {
            await fetch(api(`api/calendar/events/${encodeURIComponent(ev.id)}`), { method: 'DELETE', credentials: 'include' });
        } catch {
            // the refetch below shows the truth either way
        }
        closeDraft();
        fetchEvents(viewDate.getFullYear(), viewDate.getMonth());
    }, [closeDraft, fetchEvents, viewDate]);

    if (!isOpen) return null;

    const reminderLabel = (minutes: number) => {
        if (minutes === 0) return t('eventPopup.reminderNone');
        if (minutes === 60) return t('eventPopup.reminderHour');
        if (minutes === 1440) return t('eventPopup.reminderDay');
        return t('eventPopup.reminderMinutes', { count: minutes });
    };

    const filteredContacts = contactQuery.trim()
        ? contacts.filter((c) => c.name.toLowerCase().includes(contactQuery.trim().toLowerCase()))
        : contacts;

    // The notes panel renders in two places: on desktop as a full-width strip BELOW the body
    // (compact=false), on mobile stacked between the to-do list and the calendar (compact=true).
    const notesPanel = (compact: boolean) => (
        <div
            className={compact
                ? 'rounded-xl border-2 border-dashed border-gray-200 bg-gray-50 p-4 flex flex-col gap-3'
                : 'shrink-0 mx-4 mb-4 rounded-xl border-2 border-dashed border-gray-200 bg-gray-50 p-4 flex flex-row gap-4 h-[160px]'}
            style={{ backgroundImage: 'radial-gradient(circle, #d1d5db 1.5px, transparent 1.5px)', backgroundSize: '10px 10px' }}
        >
            <div className="shrink-0 flex flex-col">
                <h3 className="text-sm font-medium text-gray-700 mb-1.5 shrink-0">{t('notes.title')}</h3>
                <button type="button" onClick={() => setShowAddNotePopup(true)} className="shrink-0 flex items-center gap-1 text-xs font-medium text-gray-600 hover:text-gray-800 mb-0">
                    <Plus className="w-3.5 h-3.5" /> {t('notes.add')}
                </button>
            </div>
            <div className="flex-1 min-w-0 overflow-x-auto overflow-y-hidden flex flex-nowrap gap-2 items-center">
                {automationNotes.map((note) => (
                    <div key={note.id} className="flex flex-col p-3 rounded-lg bg-white border border-gray-200 min-w-[180px] max-w-[280px] max-h-[120px] shrink-0 overflow-hidden">
                        <div className="flex items-start justify-between gap-2 shrink-0">
                            {note.title ? <span className="text-sm font-medium text-gray-800 min-w-0 flex-1 truncate">{note.title}</span> : <span className="flex-1" />}
                            <button type="button" onClick={() => onSendPlannerMessage?.({ type: 'delete_automation_note', id: note.id })} className="p-1 text-gray-400 hover:text-red-600 shrink-0" title={t('deleteTooltip')}>
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

    const eventChip = (ev: CalendarEventItem, extraClass?: string, style?: React.CSSProperties) => (
        <div
            key={ev.id}
            role="button"
            tabIndex={0}
            onClick={(e) => { e.stopPropagation(); openEditDraft(ev); }}
            onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); openEditDraft(ev); } }}
            style={style}
            className={cn(
                'rounded-md px-2 py-1 text-xs leading-tight cursor-pointer overflow-hidden border transition-colors',
                ev.account_id
                    ? 'bg-blue-50 border-blue-200 text-blue-900 hover:bg-blue-100 dark:bg-[#1f2a3a] dark:border-[#31415a] dark:text-blue-100'
                    : 'bg-gray-100 border-gray-200 text-gray-800 hover:bg-gray-200',
                ev.sync_state === 'push_failed' && 'border-red-300',
                extraClass,
            )}
            title={`${ev.title}${ev.location ? `, ${ev.location}` : ''}`}
        >
            <div className="font-medium truncate">{ev.title || t('eventPopup.titleLabel')}</div>
            {!ev.all_day && <div className="opacity-80 truncate">{eventTimeRange(ev, userTimeFormat)}</div>}
            {ev.location && <div className="opacity-80 truncate flex items-center gap-1"><MapPin className="w-3 h-3 shrink-0" />{ev.location}</div>}
        </div>
    );

    return (
        <div className="fixed inset-0 z-[60] flex items-center justify-center p-4 max-md:p-0" onClick={onClose}>
            <div className="absolute inset-0 bg-black/50" />
            <div
                className="relative w-full max-w-[95vw] h-[90vh] rounded-2xl shadow-2xl border border-gray-200 flex flex-col animate-in fade-in zoom-in-95 duration-200 overflow-hidden bg-white max-md:max-w-none max-md:h-[100dvh] max-md:rounded-none max-md:border-0 max-md:overflow-y-auto"
                onClick={(e) => e.stopPropagation()}
            >
                {/* Header: user, months, title and close */}
                <div className="flex items-center shrink-0 px-4 py-3 border-b border-gray-200 gap-4 max-md:flex-wrap max-md:gap-2 max-md:px-3 max-md:sticky max-md:top-0 max-md:z-20 max-md:bg-white">
                    <div className="flex items-center gap-2 shrink-0">
                        <div className="w-9 h-9 rounded-full bg-gray-200 flex items-center justify-center text-gray-700 font-semibold text-sm">
                            {(currentUser?.username ?? 'U').slice(0, 1).toUpperCase()}
                        </div>
                        <span className="text-sm font-medium text-gray-900 truncate">{currentUser?.username ?? ''}</span>
                    </div>
                    <div className="flex flex-wrap items-center justify-center gap-1.5 flex-1 min-w-0 max-md:order-last max-md:w-full max-md:flex-none max-md:flex-nowrap max-md:overflow-x-auto max-md:justify-start max-md:pb-1">
                        {monthLabels.map((label, i) => {
                            const isSelected = viewDate.getMonth() === i;
                            const isActualMonth = viewDate.getFullYear() === new Date().getFullYear() && new Date().getMonth() === i;
                            return (
                                <button
                                    key={label + i}
                                    type="button"
                                    onClick={() => { setSelectedDayForView(null); setViewDate((d) => new Date(d.getFullYear(), i)); }}
                                    className={cn(
                                        'px-3.5 py-2 rounded-lg text-xs font-medium transition-colors shrink-0',
                                        isSelected ? 'bg-gray-900 text-white dark:bg-[#3a3a3a] dark:text-white' : 'bg-gray-100 text-gray-700 hover:bg-gray-200',
                                        isActualMonth && 'ring-2 ring-red-500',
                                    )}
                                >
                                    {label}
                                </button>
                            );
                        })}
                    </div>
                    <div className="flex items-center gap-2 shrink-0 max-md:ml-auto">
                        {eventsLoading && <Loader2 className="w-4 h-4 animate-spin text-gray-400" />}
                        <h2 className="text-lg font-bold text-gray-900 truncate max-md:text-base">{t('windowTitle', { year: viewDate.getFullYear() })}</h2>
                        <button onClick={onClose} className="p-2 hover:bg-gray-100 rounded-lg transition-colors text-gray-500 hover:text-gray-700" title={t('close')}>
                            <X size={18} />
                        </button>
                    </div>
                </div>

                {eventsError && (
                    <div className="mx-4 mt-3 p-2 rounded-lg bg-red-50 border border-red-200 text-xs text-red-700 shrink-0">{t('loadError')}</div>
                )}

                {/* Body: to-do (left) + calendar (center) */}
                <div className="flex-1 flex gap-4 min-h-0 p-4 overflow-auto max-md:flex-col max-md:p-3 max-md:flex-none max-md:overflow-visible">
                    <div
                        className="min-w-[220px] w-[220px] shrink-0 rounded-xl border-2 border-dashed border-gray-200 bg-gray-50 p-4 flex flex-col min-h-[200px] max-md:w-full max-md:min-w-0"
                        style={{ backgroundImage: 'radial-gradient(circle, #d1d5db 1.5px, transparent 1.5px)', backgroundSize: '10px 10px' }}
                    >
                        <h3 className="text-sm font-medium text-gray-700 mb-1.5 shrink-0">{t('todo.title')}</h3>
                        <p className="text-xs text-gray-500 shrink-0 mb-2">{t('todo.hint')}</p>
                        <button type="button" onClick={() => setShowAddTodoPopup(true)} className="shrink-0 flex items-center gap-1 text-xs font-medium text-gray-600 hover:text-gray-800 mb-2">
                            <Plus className="w-3.5 h-3.5" /> {t('todo.add')}
                        </button>
                        <div className="flex-1 min-h-0 overflow-auto space-y-2">
                            {automationTodos.map((todo) => (
                                <div key={todo.id} className="flex items-start gap-2 p-2 rounded-lg bg-white border border-gray-200">
                                    <input
                                        type="checkbox"
                                        checked={!!todo.done}
                                        onChange={() => onSendPlannerMessage?.({ type: 'update_automation_todo', id: todo.id, done: !todo.done })}
                                        className="mt-0.5 shrink-0 rounded border-gray-300 dark:accent-[#d9d9d9]"
                                    />
                                    <div className="flex-1 min-w-0">
                                        <span className={cn('text-sm', todo.done && 'line-through text-gray-500')}>{todo.text}</span>
                                        <p className="text-xs text-gray-500 mt-0.5">
                                            {formatPlannerDate(todo.created_at, userTimeFormat)}
                                            {todo.due_at ? <span className="block">{t('todo.due', { when: formatPlannerDate(todo.due_at, userTimeFormat) })}</span> : null}
                                        </p>
                                    </div>
                                    <button type="button" onClick={() => onSendPlannerMessage?.({ type: 'delete_automation_todo', id: todo.id })} className="p-1 text-gray-400 hover:text-red-600 shrink-0" title={t('deleteTooltip')}>
                                        <Trash2 className="w-3.5 h-3.5" />
                                    </button>
                                </div>
                            ))}
                        </div>
                    </div>
                    {isMobile && notesPanel(true)}
                    <div className="flex-1 flex flex-col min-w-0 min-h-0 rounded-xl border-2 border-dashed border-gray-200 p-3 overflow-hidden max-md:flex-none max-md:min-h-[400px]">
                        {selectedDayForView ? (() => {
                            const now = new Date();
                            const isToday = sameDay(selectedDayForView, now);
                            const currentHour = now.getHours();
                            return (
                                <>
                                    <div className="flex items-center justify-between gap-2 shrink-0 mb-3">
                                        <button type="button" onClick={() => setSelectedDayForView(null)} className="flex items-center gap-1.5 px-2.5 py-1.5 text-sm font-medium text-gray-600 hover:bg-gray-100 rounded-lg transition-colors">
                                            <ChevronRight size={16} className="rotate-180" />
                                            {t('backToMonth')}
                                        </button>
                                        <h3 className="text-sm font-semibold text-gray-900">
                                            {selectedDayForView.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' })}
                                        </h3>
                                        <button type="button" onClick={() => openNewDraft(selectedDayForView, Math.min(23, isToday ? currentHour + 1 : 9))} className={cn(primaryButtonClass, 'inline-flex items-center gap-1')}>
                                            <Plus className="w-3.5 h-3.5" /> {t('newEvent')}
                                        </button>
                                    </div>
                                    {/* the all-day row */}
                                    <div
                                        role="button"
                                        tabIndex={0}
                                        onClick={() => openNewDraft(selectedDayForView, 9, true)}
                                        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openNewDraft(selectedDayForView, 9, true); } }}
                                        className="shrink-0 flex items-start gap-3 px-3 py-2 mb-2 border border-gray-200 rounded-lg bg-white min-h-[40px] cursor-pointer hover:bg-gray-100"
                                    >
                                        <span className="text-xs font-medium text-gray-500 w-14 shrink-0 pt-1">{t('allDay')}</span>
                                        <div className="flex-1 flex flex-wrap gap-1.5 min-w-0">
                                            {dayEvents.allDay.map((ev) => eventChip(ev, 'max-w-[240px]'))}
                                        </div>
                                    </div>
                                    {/* 24 fixed rows: hour labels, the events lane, the automations lane */}
                                    <div className="flex-1 overflow-auto min-h-0 border border-gray-200 rounded-lg bg-white">
                                        <div className="grid" style={{ gridTemplateColumns: '3.5rem minmax(0, 1fr) 12rem', gridTemplateRows: `repeat(24, ${HOUR_REM}rem)` }}>
                                            {(dayViewSlots ?? []).map(({ h, slotAutomations }) => {
                                                const isCurrentHourSlot = isToday && h === currentHour;
                                                return (
                                                    <>
                                                        <div key={`label-${h}`} style={{ gridColumn: 1, gridRow: h + 1 }} className={cn('border-b border-gray-100 px-2 pt-1 text-xs font-medium', isCurrentHourSlot ? 'text-red-600' : 'text-gray-500')}>
                                                            {pad2(h)}:00
                                                        </div>
                                                        <div
                                                            key={`events-${h}`}
                                                            style={{ gridColumn: 2, gridRow: h + 1 }}
                                                            role="button"
                                                            tabIndex={0}
                                                            onClick={() => openNewDraft(selectedDayForView, h)}
                                                            onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openNewDraft(selectedDayForView, h); } }}
                                                            className={cn('border-b border-l border-gray-100 cursor-pointer transition-colors', isCurrentHourSlot ? 'bg-red-50/30' : 'hover:bg-gray-100')}
                                                        />
                                                        <div
                                                            key={`auto-${h}`}
                                                            style={{ gridColumn: 3, gridRow: h + 1 }}
                                                            role="button"
                                                            tabIndex={0}
                                                            onClick={() => setSelectedSlot({ date: selectedDayForView, hour: h })}
                                                            onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setSelectedSlot({ date: selectedDayForView, hour: h }); } }}
                                                            className={cn('border-b border-l border-dashed border-gray-200 p-1 flex flex-wrap content-start gap-1 overflow-hidden cursor-pointer transition-colors', isCurrentHourSlot ? 'bg-red-50/30' : 'bg-gray-50/50 hover:bg-gray-100')}
                                                        >
                                                            {slotAutomations.map((auto) => (
                                                                <span
                                                                    key={auto.id}
                                                                    role="button"
                                                                    tabIndex={0}
                                                                    onClick={(e) => { e.stopPropagation(); onEditAutomation?.(auto); }}
                                                                    onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); onEditAutomation?.(auto); } }}
                                                                    className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md bg-gray-800 text-white text-xs font-medium truncate max-w-full cursor-pointer hover:bg-gray-700 transition-colors dark:bg-[#3a3a3a] dark:text-gray-100"
                                                                    title={t('editSlotTooltip', { name: auto.name })}
                                                                >
                                                                    <Zap className="w-3 h-3 shrink-0" />
                                                                    <span className="truncate">{auto.name}</span>
                                                                </span>
                                                            ))}
                                                        </div>
                                                    </>
                                                );
                                            })}
                                            {/* the events lane's content spans all rows and positions its blocks by time */}
                                            <div style={{ gridColumn: 2, gridRow: '1 / span 24' }} className="relative pointer-events-none">
                                                {dayEvents.placed.map((p) => eventChip(p.ev, 'absolute pointer-events-auto shadow-sm', {
                                                    top: `${p.top * HOUR_REM}rem`,
                                                    height: `calc(${p.height * HOUR_REM}rem - 2px)`,
                                                    left: `calc(${(p.col / p.cols) * 100}% + 2px)`,
                                                    width: `calc(${100 / p.cols}% - 4px)`,
                                                }))}
                                            </div>
                                        </div>
                                    </div>
                                </>
                            );
                        })() : (
                            <div className="grid grid-cols-7 gap-px bg-gray-400 rounded-xl flex-1 min-h-0 w-full" style={{ gridTemplateRows: `auto repeat(${monthCells.numRows}, minmax(0, 1fr))` }}>
                                {weekdayLabels.map((day, i) => (
                                    <div key={day + i} className="bg-gray-50 flex items-center justify-center text-xs font-medium text-gray-500 uppercase tracking-wide py-1.5">{day}</div>
                                ))}
                                {monthCells.cells.map((day, i) => {
                                    const { y, m, isCurrentMonth, today } = monthCells;
                                    const isSunday = (i % 7) === 6;
                                    const autos = cellAutomations[i];
                                    const evs = cellEvents[i];
                                    const tooltip = [
                                        evs.length ? (evs.length === 1 ? evs[0].title : t('eventsOnDay', { count: evs.length })) : '',
                                        autos.length ? (autos.length === 1 ? autos[0].name : t('automationsOnDay', { count: autos.length })) : '',
                                    ].filter(Boolean).join(', ');
                                    return (
                                        <div
                                            key={i}
                                            role="button"
                                            tabIndex={0}
                                            onClick={day !== null ? () => setSelectedDayForView(new Date(y, m, day)) : undefined}
                                            onKeyDown={day !== null ? (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setSelectedDayForView(new Date(y, m, day!)); } } : undefined}
                                            className={cn(
                                                'min-h-0 flex flex-col items-center justify-center text-sm rounded-lg transition-colors duration-150 relative',
                                                day === null ? 'bg-gray-200 text-gray-400 cursor-default' : 'bg-white text-gray-700 cursor-pointer hover:bg-gray-100',
                                                day !== null && isSunday && 'text-red-600',
                                                day !== null && isCurrentMonth && day === today.getDate() && 'font-semibold text-gray-900 ring-2 ring-red-500 ring-inset',
                                            )}
                                            title={tooltip || undefined}
                                        >
                                            <span>{day ?? ''}</span>
                                            {(evs.length > 0 || autos.length > 0) && (
                                                <span className="mt-0.5 flex gap-0.5 flex-wrap justify-center max-w-full">
                                                    {evs.slice(0, 4).map((ev) => <span key={ev.id} className="w-1.5 h-1.5 rounded-full bg-blue-500 shrink-0" />)}
                                                    {autos.slice(0, 4).map((a) => <span key={a.id} className="w-1.5 h-1.5 rounded-full bg-gray-500 dark:bg-[#bdbdbd] shrink-0" />)}
                                                </span>
                                            )}
                                        </div>
                                    );
                                })}
                            </div>
                        )}
                    </div>
                </div>
                {!isMobile && notesPanel(false)}
            </div>

            {showAddNotePopup && (
                <div className="fixed inset-0 z-[70] flex items-center justify-center p-4" onClick={closeNotePopup}>
                    <div className="absolute inset-0 bg-black/50" />
                    <div className="relative w-full max-w-md rounded-xl bg-white shadow-xl border border-gray-200 p-4" onClick={(e) => e.stopPropagation()}>
                        <h3 className="text-sm font-semibold text-gray-900 mb-3">{t('notes.add')}</h3>
                        <input type="text" value={addNoteTitle} onChange={(e) => setAddNoteTitle(e.target.value)} placeholder={t('notes.titlePlaceholder')} className={cn(inputClass, 'mb-2')} />
                        <textarea value={addNoteContent} onChange={(e) => setAddNoteContent(e.target.value)} placeholder={t('notes.contentPlaceholder')} rows={4} className={cn(inputClass, 'resize-none mb-4')} />
                        <div className="flex justify-end gap-2">
                            <button type="button" onClick={closeNotePopup} className={ghostButtonClass}>{t('createPopup.cancel')}</button>
                            <button
                                type="button"
                                onClick={() => {
                                    const content = addNoteContent.trim();
                                    if (content && onSendPlannerMessage) {
                                        onSendPlannerMessage({ type: 'create_automation_note', title: addNoteTitle.trim() || undefined, content });
                                        closeNotePopup();
                                    }
                                }}
                                className={primaryButtonClass}
                            >
                                {t('add')}
                            </button>
                        </div>
                    </div>
                </div>
            )}

            {showAddTodoPopup && (
                <div className="fixed inset-0 z-[70] flex items-center justify-center p-4" onClick={closeTodoPopup}>
                    <div className="absolute inset-0 bg-black/50" />
                    <div className="relative w-full max-w-md rounded-xl bg-white shadow-xl border border-gray-200 p-4" onClick={(e) => e.stopPropagation()}>
                        <h3 className="text-sm font-semibold text-gray-900 mb-3">{t('todo.add')}</h3>
                        <input type="text" value={addTodoText} onChange={(e) => setAddTodoText(e.target.value)} placeholder={t('todo.textPlaceholder')} className={cn(inputClass, 'mb-2')} />
                        <input type="text" value={addTodoDueAt} onChange={(e) => setAddTodoDueAt(e.target.value)} placeholder={t('todo.duePlaceholder')} className={cn(inputClass, 'mb-4')} />
                        <div className="flex justify-end gap-2">
                            <button type="button" onClick={closeTodoPopup} className={ghostButtonClass}>{t('createPopup.cancel')}</button>
                            <button
                                type="button"
                                onClick={() => {
                                    const text = addTodoText.trim();
                                    if (text && onSendPlannerMessage) {
                                        onSendPlannerMessage({ type: 'create_automation_todo', text, ...(addTodoDueAt.trim() ? { due_at: addTodoDueAt.trim() } : {}) });
                                        closeTodoPopup();
                                    }
                                }}
                                className={primaryButtonClass}
                            >
                                {t('add')}
                            </button>
                        </div>
                    </div>
                </div>
            )}

            {draft && (
                <div className="fixed inset-0 z-[72] flex items-center justify-center p-4" onClick={() => { if (!saving) closeDraft(); }}>
                    <div className="absolute inset-0 bg-black/50" />
                    <div className="relative w-full max-w-lg rounded-xl bg-white shadow-xl border border-gray-200 p-5 max-h-[90vh] overflow-y-auto" onClick={(e) => e.stopPropagation()}>
                        <div className="flex items-start justify-between gap-3 mb-4">
                            <h3 className="text-base font-semibold text-gray-900">{draft.mode === 'new' ? t('eventPopup.titleNew') : t('eventPopup.titleEdit')}</h3>
                            <button type="button" onClick={() => { if (!saving) closeDraft(); }} className="p-1.5 hover:bg-gray-100 rounded-lg text-gray-500" title={t('close')}>
                                <X size={16} />
                            </button>
                        </div>
                        {draft.mode === 'edit' && draft.source && (
                            <div className="mb-3 flex flex-wrap items-center gap-2 text-xs text-gray-500">
                                <span className="px-2 py-0.5 rounded-full bg-gray-100 text-gray-700">
                                    {draft.source.account_id ? t('eventPopup.sourceMirrored', { account: draft.source.account_id }) : t('eventPopup.sourceInternal')}
                                </span>
                                {draft.source.sync_state === 'pending_push' && <span>{t('eventPopup.pendingPush')}</span>}
                                {draft.source.sync_state === 'push_failed' && <span className="text-red-600">{t('eventPopup.pushFailed', { error: draft.source.last_error || '' })}</span>}
                                {draft.source.link && (
                                    <a href={draft.source.link} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-blue-600 hover:text-blue-800">
                                        <ExternalLink className="w-3.5 h-3.5" /> {t('eventPopup.openInProvider')}
                                    </a>
                                )}
                            </div>
                        )}
                        <label className="block text-xs font-medium text-gray-600 mb-1">{t('eventPopup.titleLabel')}</label>
                        <input
                            type="text"
                            value={draft.title}
                            autoFocus
                            onChange={(e) => setDraft({ ...draft, title: e.target.value })}
                            onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); saveDraft(); } }}
                            placeholder={t('eventPopup.titlePlaceholder')}
                            className={cn(inputClass, 'mb-3')}
                        />
                        <div className="grid grid-cols-3 gap-2 mb-3 max-md:grid-cols-1">
                            <div>
                                <label className="block text-xs font-medium text-gray-600 mb-1">{t('eventPopup.dateLabel')}</label>
                                <input type="date" value={draft.date} onChange={(e) => setDraft({ ...draft, date: e.target.value })} className={inputClass} />
                            </div>
                            <div>
                                <label className="block text-xs font-medium text-gray-600 mb-1">{t('eventPopup.startLabel')}</label>
                                <input type="time" value={draft.start} disabled={draft.allDay} onChange={(e) => setDraft({ ...draft, start: e.target.value })} className={cn(inputClass, 'disabled:opacity-50')} />
                            </div>
                            <div>
                                <label className="block text-xs font-medium text-gray-600 mb-1">{t('eventPopup.endLabel')}</label>
                                <input type="time" value={draft.end} disabled={draft.allDay} onChange={(e) => setDraft({ ...draft, end: e.target.value })} className={cn(inputClass, 'disabled:opacity-50')} />
                            </div>
                        </div>
                        <label className="flex items-center gap-2 text-sm text-gray-700 mb-3 cursor-pointer">
                            <input type="checkbox" checked={draft.allDay} onChange={(e) => setDraft({ ...draft, allDay: e.target.checked })} className="rounded border-gray-300 dark:accent-[#d9d9d9]" />
                            {t('eventPopup.allDayLabel')}
                        </label>
                        <label className="block text-xs font-medium text-gray-600 mb-1">{t('eventPopup.locationLabel')}</label>
                        <input type="text" value={draft.location} onChange={(e) => setDraft({ ...draft, location: e.target.value })} placeholder={t('eventPopup.locationPlaceholder')} className={cn(inputClass, 'mb-3')} />
                        <label className="block text-xs font-medium text-gray-600 mb-1">{t('eventPopup.descriptionLabel')}</label>
                        <textarea value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })} rows={3} className={cn(inputClass, 'resize-none mb-3')} />
                        <label className="block text-xs font-medium text-gray-600 mb-1 flex items-center gap-1"><Users className="w-3.5 h-3.5" /> {t('eventPopup.contactsLabel')}</label>
                        <input type="text" value={contactQuery} onChange={(e) => setContactQuery(e.target.value)} placeholder={t('eventPopup.contactsPlaceholder')} className={cn(inputClass, 'mb-1')} />
                        <div className="max-h-28 overflow-y-auto rounded-lg border border-gray-200 mb-3 divide-y divide-gray-100">
                            {filteredContacts.length === 0 ? (
                                <p className="px-3 py-2 text-xs text-gray-500">{t('eventPopup.noContacts')}</p>
                            ) : filteredContacts.slice(0, 50).map((c) => {
                                const checked = draft.contactIds.includes(c.id);
                                return (
                                    <label key={c.id} className="flex items-center gap-2 px-3 py-1.5 text-sm text-gray-700 cursor-pointer hover:bg-gray-100">
                                        <input
                                            type="checkbox"
                                            checked={checked}
                                            onChange={() => setDraft({ ...draft, contactIds: checked ? draft.contactIds.filter((id) => id !== c.id) : [...draft.contactIds, c.id] })}
                                            className="rounded border-gray-300 dark:accent-[#d9d9d9]"
                                        />
                                        <span className="truncate">{c.name}</span>
                                    </label>
                                );
                            })}
                        </div>
                        <div className="grid grid-cols-2 gap-2 mb-4 max-md:grid-cols-1">
                            <div>
                                <label className="block text-xs font-medium text-gray-600 mb-1">{t('eventPopup.reminderLabel')}</label>
                                <select value={draft.reminder} onChange={(e) => setDraft({ ...draft, reminder: Number(e.target.value) })} className={inputClass}>
                                    {REMINDER_CHOICES.map((m) => <option key={m} value={m}>{reminderLabel(m)}</option>)}
                                </select>
                            </div>
                            {draft.mode === 'new' && mirrorAccount && (
                                <label className="flex items-end gap-2 text-sm text-gray-700 pb-2 cursor-pointer">
                                    <input type="checkbox" checked={draft.mirror} onChange={(e) => setDraft({ ...draft, mirror: e.target.checked })} className="rounded border-gray-300 dark:accent-[#d9d9d9]" />
                                    <span className="leading-tight">{t('eventPopup.mirrorLabel', { account: mirrorAccount.email || mirrorAccount.account_id })}</span>
                                </label>
                            )}
                        </div>
                        {saveError && (
                            <p className="text-xs text-red-600 mb-3">
                                {saveError === 'required' ? t('eventPopup.errorRequired') : saveError === 'time' ? t('eventPopup.errorTime') : t('eventPopup.errorServer')}
                            </p>
                        )}
                        <div className="flex items-center justify-between gap-2">
                            <div>
                                {draft.mode === 'edit' && draft.source && (
                                    <button type="button" onClick={() => setConfirmDelete(draft.source!)} className="inline-flex items-center gap-1 text-sm text-gray-500 hover:text-red-600 px-2 py-1.5 rounded-lg hover:bg-red-50">
                                        <Trash2 className="w-3.5 h-3.5" /> {t('eventPopup.delete')}
                                    </button>
                                )}
                            </div>
                            <div className="flex gap-2">
                                <button type="button" onClick={closeDraft} disabled={saving} className={ghostButtonClass}>{t('eventPopup.cancel')}</button>
                                <button type="button" onClick={saveDraft} disabled={saving} className={primaryButtonClass}>
                                    {saving ? t('eventPopup.saving') : draft.mode === 'new' ? t('eventPopup.create') : t('eventPopup.save')}
                                </button>
                            </div>
                        </div>
                    </div>
                </div>
            )}

            <ConfirmDialog
                open={confirmDelete !== null}
                title={t('eventPopup.deleteConfirmTitle')}
                body={confirmDelete?.account_id
                    ? t('eventPopup.deleteConfirmBodyMirrored', { title: confirmDelete?.title || '' })
                    : t('eventPopup.deleteConfirmBody', { title: confirmDelete?.title || '' })}
                confirmLabel={t('eventPopup.delete')}
                cancelLabel={t('eventPopup.cancel')}
                onConfirm={() => { if (confirmDelete) deleteEvent(confirmDelete); }}
                onCancel={() => setConfirmDelete(null)}
                destructive
            />

            {selectedSlot && (
                <CreateAutomationPopup
                    isOpen={true}
                    onClose={() => setSelectedSlot(null)}
                    initialDate={selectedSlot.date}
                    initialHour={selectedSlot.hour}
                    onCreated={() => { setSelectedSlot(null); onAutomationCreated?.(); }}
                    onSubmit={onSubmitCreateAutomation}
                />
            )}
        </div>
    );
}
