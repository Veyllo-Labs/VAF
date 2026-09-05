'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
//
// The contact book as a small CRM: a list with search, status chips and bulk
// actions on the left, one contact record on the right with status, tags, quick
// actions, a merged activity timeline (messages, mails, notes, events) and cards
// for channels, file, upcoming events and key figures.
//
// The window follows the theme through the folding palette (bg-white, gray and
// accent utilities are re-pointed under .dark, see docs/web-ui/DARKMODE.md). Raw
// hex appears only where the fold cannot express the state: the house switch,
// the primary button and the active tab/chip/row.

import React, { useState, useEffect, useMemo, useRef, useCallback } from 'react';
import {
    X, Users, Plus, Pencil, Trash2, Search, Phone, Mail, Send, Hash, MessageCircle,
    StickyNote, CalendarDays, MoreHorizontal, Copy, Check, ChevronDown, ArrowUpRight, UserPlus,
} from 'lucide-react';
import { useTranslations } from 'next-intl';
import { cn } from '@/lib/utils';
import { useEscapeLayer } from '@/hooks/useEscapeLayer';
import { copyText } from '@/lib/clipboard';
import ConfirmDialog from '@/components/ui/ConfirmDialog';
import { fmtWhen, initials } from './ChannelDashboardShell';

const api = (path: string) => path.startsWith('/') ? path : `/${path}`;

export interface ChannelEntry {
    type: string;
    value: string;
}

export interface Contact {
    id: string;
    name: string;
    channels?: ChannelEntry[];
    whatsapp_phone?: string | null;
    telegram_username?: string | null;
    telegram_user_id?: string | null;
    email?: string | null;
    preferred_language?: string | null;
    how_to_address?: string | null;
    birthday?: string | null;
    notes?: string | null;
    allow_as_assistant_user?: boolean;
    status?: string | null;
    company?: string | null;
    role?: string | null;
    tags?: string[];
    /** "manual", "agent" or the sync channel that created the record. */
    source?: string | null;
    created_at?: number | null;
    notes_log?: Array<{ id: string; ts: number; text: string; source?: string }>;
    events?: Array<{ id: string; ts: number; when_ts: number; title: string; source?: string; note?: string | null }>;
    /** Per-channel link written by the channel sync (WhatsApp today): the name shown there and the newest message time. */
    links?: Record<string, { endpoint?: string; display_name?: string; last_seen_ts?: number | null }>;
}

export interface ContactsDashboardProps {
    isOpen: boolean;
    onClose: () => void;
    /** Jump into the channel dashboard with this chat selected; the id is the backend endpoint value. */
    onOpenChat?: (channel: 'whatsapp' | 'telegram', chatId: string) => void;
}

interface Overview {
    status?: string | null;
    last_contact?: { channel: string; ts: number } | null;
    calendar_events?: Array<{ id?: string; summary?: string; start?: string; htmlLink?: string; webLink?: string }>;
    stats?: { messages: number; from_agent: number; first_ts: number | null; last_ts: number | null; by_channel?: Record<string, { count: number; out_count: number; first_ts: number | null; last_ts: number | null }> } | null;
    endpoints?: { whatsapp?: string[]; telegram?: string[]; discord?: string[]; email?: string[] };
    created?: { ts: number; source: string } | null;
}

type TimelineKind = 'message' | 'mail' | 'note' | 'event' | 'created';

interface TimelineItem {
    kind: TimelineKind;
    /** Unique per item (the store builds it from kind, key and time); the React key. */
    id: string;
    ts: number;
    channel: string | null;
    direction: 'in' | 'out' | null;
    title: string | null;
    body: string;
    source: string | null;
    ref: { chat_id?: string; message_id?: string; note_id?: string; event_id?: string; when_ts?: number; folder?: string; from?: string; to?: string };
}

type ActivityTab = 'all' | 'message' | 'note' | 'event' | 'mail';

type Confirm =
    | { kind: 'deleteContact'; contact: Contact }
    | { kind: 'deleteSelected'; ids: string[] }
    | { kind: 'removeNote'; contactId: string; noteId: string; text: string }
    | { kind: 'removeEvent'; contactId: string; eventId: string; title: string }
    | { kind: 'reach'; contact: Contact };

interface ContactForm {
    name: string;
    channels: ChannelEntry[];
    company: string;
    role: string;
    tagsText: string;
    preferred_language: string;
    how_to_address: string;
    birthday: string;
    notes: string;
}

const CHANNEL_TYPES = ['phone', 'whatsapp', 'email', 'telegram', 'discord'] as const;
const KNOWN_STATUSES = ['lead', 'in_contact', 'customer', 'archived'] as const;
const NO_STATUS = '__none__';

const STATUS_PILL: Record<string, string> = {
    lead: 'bg-amber-100 text-amber-800',
    in_contact: 'bg-sky-100 text-sky-800',
    customer: 'bg-green-100 text-green-800',
};
const PILL_DEFAULT = 'bg-gray-100 text-gray-700';
// Four accent pairs at the folding steps (surface 100, text 800); slate is not a house colour.
const AVATAR_FAMILIES = ['bg-blue-100 text-blue-800', 'bg-violet-100 text-violet-800', 'bg-teal-100 text-teal-800', 'bg-rose-100 text-rose-800'];

const PRIMARY = 'bg-gray-900 hover:bg-gray-800 text-white dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-[#f5f5f5] dark:shadow-none';
const ACTIVE = 'bg-gray-900 text-white dark:bg-[#3a3a3a] dark:text-white';
const BTN = 'inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-gray-100 hover:bg-gray-200 border border-gray-200 text-sm text-gray-900 whitespace-nowrap disabled:opacity-50';
const BTN_GHOST = 'inline-flex items-center gap-1 px-2 py-1 rounded-lg text-xs text-gray-600 hover:bg-gray-100 hover:text-gray-900';
const INPUT = 'bg-gray-100 border border-gray-200 rounded-lg px-3 py-1.5 text-sm text-gray-900 placeholder-gray-400 outline-none focus:border-gray-400';
const FIELD = 'w-full px-4 py-3 rounded-xl bg-white border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-transparent';
const CARD = 'rounded-xl border border-gray-200 bg-white';
const CARD_HEAD = 'flex items-center justify-between gap-2 px-3.5 py-3 border-b border-gray-200';
const CARD_TITLE = 'text-xs font-semibold uppercase tracking-wide text-gray-600';
const KV = 'flex items-center justify-between gap-2 py-1.5 border-b border-gray-200 last:border-b-0 text-[13px]';
const EMPTY_FIGURE = '-';

function hashIndex(s: string, n: number): number {
    let h = 0;
    for (const ch of s) h = (h * 31 + (ch.codePointAt(0) ?? 0)) >>> 0;
    return h % n;
}

function fmtDate(ts: number): string {
    return new Date(ts * 1000).toLocaleDateString([], { day: '2-digit', month: '2-digit', year: 'numeric' });
}

function fmtTime(ts: number): string {
    return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function fmtDateTime(ts: number): string {
    return fmtDate(ts) + ' ' + fmtTime(ts);
}

function fmtLongDay(ts: number): string {
    return new Date(ts * 1000).toLocaleDateString([], { weekday: 'long', day: 'numeric', month: 'long' });
}

/** Channel rows of a record; the legacy flat fields are read when the list is absent. */
function contactChannels(c: Contact): ChannelEntry[] {
    if (c.channels && Array.isArray(c.channels) && c.channels.length > 0) {
        return c.channels.map(ch => ({ type: ch.type || 'whatsapp', value: ch.value || '' }));
    }
    const out: ChannelEntry[] = [];
    if (c.whatsapp_phone) out.push({ type: 'phone', value: c.whatsapp_phone });
    if (c.telegram_user_id) out.push({ type: 'telegram', value: c.telegram_user_id });
    if (c.telegram_username) out.push({ type: 'telegram', value: c.telegram_username });
    if (c.email) out.push({ type: 'email', value: c.email });
    return out;
}

function channelTypes(c: Contact): string[] {
    const seen = new Set<string>();
    for (const ch of contactChannels(c)) seen.add(ch.type === 'phone' ? 'whatsapp' : ch.type);
    return Array.from(seen);
}

function lastSeen(c: Contact): { channel: string; ts: number } | null {
    let best: { channel: string; ts: number } | null = null;
    for (const [channel, link] of Object.entries(c.links || {})) {
        const ts = Number(link?.last_seen_ts || 0);
        if (ts && (!best || ts > best.ts)) best = { channel, ts };
    }
    return best;
}

/** Days until the next birthday for "MM-DD" or an ISO date; null when the value does not parse. */
function birthdayInDays(value: string): number | null {
    const m = /^(?:(\d{4})-)?(\d{1,2})-(\d{1,2})/.exec(value.trim());
    if (!m) return null;
    const month = Number(m[2]) - 1;
    const day = Number(m[3]);
    if (month < 0 || month > 11 || day < 1 || day > 31) return null;
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    let next = new Date(today.getFullYear(), month, day);
    if (next < today) next = new Date(today.getFullYear() + 1, month, day);
    return Math.round((next.getTime() - today.getTime()) / 86400_000);
}

function ChannelIcon({ type, className }: { type: string; className?: string }) {
    const cls = cn('shrink-0', className || 'w-3.5 h-3.5');
    switch (type) {
        case 'phone':
        case 'whatsapp': return <Phone className={cn(cls, 'text-green-600')} />;
        case 'email':
        case 'mail': return <Mail className={cn(cls, 'text-sky-600')} />;
        case 'telegram': return <Send className={cn(cls, 'text-sky-600')} />;
        case 'discord': return <Hash className={cn(cls, 'text-violet-600')} />;
        default: return <MessageCircle className={cn(cls, 'text-gray-500')} />;
    }
}

/** Initials in a colour picked from the name. The shell's Avatar is dark-only and carries a
 *  profile picture; a contact has neither, so only the initials helper is shared. */
function ContactAvatar({ name, size }: { name: string; size: 'sm' | 'lg' }) {
    const fam = AVATAR_FAMILIES[hashIndex(name || '', AVATAR_FAMILIES.length)];
    return (
        <div className={cn('rounded-full grid place-items-center font-semibold shrink-0', fam,
            size === 'sm' ? 'w-[34px] h-[34px] text-xs' : 'w-[52px] h-[52px] text-lg')}>
            {initials(name || '')}
        </div>
    );
}

export default function ContactsDashboard({ isOpen, onClose, onOpenChat }: ContactsDashboardProps) {
    const tc = useTranslations('settings.contactsDashboard');
    const tw = useTranslations('settings.whatsappDashboard');
    const td = useTranslations('settings.channelDashboard');
    const tcm = useTranslations('common');

    const [contacts, setContacts] = useState<Contact[]>([]);
    const [loading, setLoading] = useState(false);
    const [statusValues, setStatusValues] = useState<string[]>([]);
    const [tagValues, setTagValues] = useState<string[]>([]);

    const [searchQuery, setSearchQuery] = useState('');
    const [statusFilter, setStatusFilter] = useState('');
    const [sortBy, setSortBy] = useState<'last' | 'name'>('last');
    const [selectedContactId, setSelectedContactId] = useState<string | null>(null);

    const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
    const [bulkStatus, setBulkStatus] = useState('');
    const [bulkTag, setBulkTag] = useState('');
    const [bulkBusy, setBulkBusy] = useState(false);
    const [bulkError, setBulkError] = useState<string | null>(null);

    const [overview, setOverview] = useState<Overview | null>(null);
    const [timeline, setTimeline] = useState<TimelineItem[]>([]);
    const [nextCursor, setNextCursor] = useState<string | null>(null);
    const [timelineLoading, setTimelineLoading] = useState(false);
    const [timelineFailed, setTimelineFailed] = useState(false);
    const [timelineVersion, setTimelineVersion] = useState(0);
    const [tab, setTab] = useState<ActivityTab>('all');

    const [noteText, setNoteText] = useState('');
    const [eventTitle, setEventTitle] = useState('');
    const [eventWhen, setEventWhen] = useState('');
    const [showEventForm, setShowEventForm] = useState(false);
    const [statusEditing, setStatusEditing] = useState(false);
    const [statusDraft, setStatusDraft] = useState('');
    const [tagEditing, setTagEditing] = useState(false);
    const [tagDraft, setTagDraft] = useState('');
    const [menuOpen, setMenuOpen] = useState(false);
    const [copiedValue, setCopiedValue] = useState<string | null>(null);

    const [showFormModal, setShowFormModal] = useState(false);
    const [modalContact, setModalContact] = useState<Contact | null>(null);
    const [form, setForm] = useState<ContactForm | null>(null);
    const [saving, setSaving] = useState(false);
    const [confirm, setConfirm] = useState<Confirm | null>(null);
    const [fileError, setFileError] = useState<string | null>(null);

    const composerRef = useRef<HTMLInputElement>(null);
    const menuRef = useRef<HTMLDivElement>(null);
    const timelineRequest = useRef(0);
    // The contact the overview in state belongs to, so a switch blanks the cards at once
    // instead of showing the previous record's figures until the new fetch lands.
    const overviewFor = useRef<string | null>(null);
    // Set by the Escape rung before the editor unmounts, so a blur fired by the unmount does not commit.
    const editorCancelled = useRef(false);

    // ---- loading ---------------------------------------------------------------

    const fetchContacts = useCallback(async () => {
        setLoading(true);
        try {
            const res = await fetch(api('api/contacts'), { credentials: 'include' });
            const data = await res.json();
            setContacts(Array.isArray(data) ? data : []);
        } catch {
            setContacts([]);
        } finally {
            setLoading(false);
        }
    }, []);

    const fetchStatusValues = useCallback(async () => {
        try {
            const res = await fetch(api('api/contacts/statuses/values'), { credentials: 'include' });
            const json = await res.json();
            if (res.ok && Array.isArray(json?.values)) setStatusValues(json.values);
        } catch { /* suggestions only */ }
    }, []);

    const fetchTagValues = useCallback(async () => {
        try {
            const res = await fetch(api('api/contacts/tags/values'), { credentials: 'include' });
            const json = await res.json();
            if (res.ok && Array.isArray(json?.values)) setTagValues(json.values);
        } catch { /* suggestions only */ }
    }, []);

    useEffect(() => {
        if (isOpen) {
            fetchContacts(); fetchStatusValues(); fetchTagValues();
            setTimelineVersion(v => v + 1);           // a reopened window shows what arrived meanwhile
            return;
        }
        // Closing leaves nothing half-open for the next opening.
        setMenuOpen(false); setStatusEditing(false); setTagEditing(false); setShowEventForm(false);
        setSelectedIds(new Set()); setBulkStatus(''); setBulkTag(''); setBulkError(null);
        setConfirm(null); setFileError(null);
    }, [isOpen, fetchContacts, fetchStatusValues, fetchTagValues]);

    useEffect(() => {
        if (!menuOpen) return;
        const onDown = (e: MouseEvent) => {
            if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuOpen(false);
        };
        document.addEventListener('mousedown', onDown);
        return () => document.removeEventListener('mousedown', onDown);
    }, [menuOpen]);

    useEffect(() => {
        if (!selectedContactId) { setOverview(null); overviewFor.current = null; return; }
        if (overviewFor.current !== selectedContactId) { setOverview(null); overviewFor.current = selectedContactId; }
        let cancelled = false;
        fetch(api(`api/contacts/${encodeURIComponent(selectedContactId)}/overview`), { credentials: 'include' })
            .then(r => r.ok ? r.json() : null)
            .then(json => { if (!cancelled) setOverview(json); })
            .catch(() => { if (!cancelled) setOverview(null); });
        return () => { cancelled = true; };
    }, [selectedContactId, contacts]);

    const loadTimeline = useCallback(async (contactId: string, activeTab: ActivityTab, cursor: string | null) => {
        const request = ++timelineRequest.current;
        setTimelineLoading(true);
        if (!cursor) setTimelineFailed(false);
        try {
            const params = new URLSearchParams({ limit: '50' });
            if (cursor) params.set('cursor', cursor);
            if (activeTab !== 'all') params.set('kinds', activeTab);
            const res = await fetch(api(`api/contacts/${encodeURIComponent(contactId)}/timeline?${params.toString()}`), { credentials: 'include' });
            const json = await res.json();
            if (request !== timelineRequest.current) return;
            if (!res.ok) { setTimelineFailed(true); return; }
            const items: TimelineItem[] = Array.isArray(json?.items) ? json.items : [];
            setTimeline(prev => cursor ? [...prev, ...items] : items);
            setNextCursor(typeof json?.next_cursor === 'string' && json.next_cursor ? json.next_cursor : null);
            if (json?.timed_out) setTimelineFailed(true);
        } catch {
            if (request === timelineRequest.current) setTimelineFailed(true);
        } finally {
            if (request === timelineRequest.current) setTimelineLoading(false);
        }
    }, []);

    useEffect(() => {
        if (!selectedContactId) { setTimeline([]); setNextCursor(null); return; }
        setTimeline([]);
        setNextCursor(null);
        loadTimeline(selectedContactId, tab, null);
    }, [selectedContactId, tab, timelineVersion, loadTimeline]);

    // A contact switch clears everything that belonged to the previous record.
    useEffect(() => {
        setNoteText('');
        setEventTitle('');
        setEventWhen('');
        setShowEventForm(false);
        setStatusEditing(false);
        setTagEditing(false);
        setMenuOpen(false);
        setFileError(null);
    }, [selectedContactId]);

    // reloadTimeline: only a change that appears in the timeline (a note, an event) reloads
    // it; a status, tag or switch change keeps the pages the user has scrolled through.
    const refreshRecord = useCallback(async (reloadTimeline = true) => {
        await fetchContacts();
        fetchStatusValues();
        fetchTagValues();
        if (reloadTimeline) setTimelineVersion(v => v + 1);
    }, [fetchContacts, fetchStatusValues, fetchTagValues]);

    // ---- mutations -------------------------------------------------------------

    const patchContact = async (id: string, body: Record<string, unknown>) => {
        setFileError(null);
        try {
            const res = await fetch(api(`api/contacts/${encodeURIComponent(id)}`), {
                method: 'PATCH', headers: { 'Content-Type': 'application/json' }, credentials: 'include', body: JSON.stringify(body),
            });
            if (!res.ok) { setFileError(tc('saveFailed')); return; }
            await refreshRecord(false);
        } catch { setFileError(tc('saveFailed')); }
    };

    const handleAddNote = async (id: string) => {
        const text = noteText.trim();
        if (!text) return;
        setFileError(null);
        try {
            const res = await fetch(api(`api/contacts/${encodeURIComponent(id)}/notes`), {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'include', body: JSON.stringify({ text }),
            });
            if (!res.ok) { setFileError(tc('saveFailed')); return; }
            setNoteText('');
            await refreshRecord();
        } catch { setFileError(tc('saveFailed')); }
    };

    const removeNote = async (id: string, noteId: string) => {
        try {
            await fetch(api(`api/contacts/${encodeURIComponent(id)}/notes/${encodeURIComponent(noteId)}`), { method: 'DELETE', credentials: 'include' });
            await refreshRecord();
        } catch { setFileError(tc('saveFailed')); }
    };

    const handleAddEvent = async (id: string) => {
        const title = eventTitle.trim();
        if (!title || !eventWhen) return;
        setFileError(null);
        try {
            const res = await fetch(api(`api/contacts/${encodeURIComponent(id)}/events`), {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'include',
                body: JSON.stringify({ title, when: eventWhen.replace('T', ' ') }),
            });
            if (!res.ok) { setFileError(tc('saveFailed')); return; }
            setEventTitle('');
            setEventWhen('');
            setShowEventForm(false);
            await refreshRecord();
        } catch { setFileError(tc('saveFailed')); }
    };

    const removeEvent = async (id: string, eventId: string) => {
        try {
            await fetch(api(`api/contacts/${encodeURIComponent(id)}/events/${encodeURIComponent(eventId)}`), { method: 'DELETE', credentials: 'include' });
            await refreshRecord();
        } catch { setFileError(tc('saveFailed')); }
    };

    const deleteContact = async (id: string) => {
        setFileError(null);
        try {
            const res = await fetch(api(`api/contacts/${encodeURIComponent(id)}`), { method: 'DELETE', credentials: 'include' });
            if (!res.ok) { setFileError(tc('deleteFailed')); return; }
            if (selectedContactId === id) setSelectedContactId(null);
            setSelectedIds(prev => { const next = new Set(prev); next.delete(id); return next; });
            await fetchContacts();
        } catch { setFileError(tc('deleteFailed')); }
    };

    const applyBulk = async () => {
        const ids = Array.from(selectedIds);
        const status = bulkStatus.trim();
        const tag = bulkTag.trim();
        if (ids.length === 0 || (!status && !tag)) return;
        setBulkBusy(true);
        setBulkError(null);
        try {
            const body: Record<string, unknown> = { ids };
            if (status) body.status = status === NO_STATUS ? null : status;
            if (tag) body.add_tags = [tag];
            const res = await fetch(api('api/contacts/bulk'), {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'include', body: JSON.stringify(body),
            });
            if (!res.ok) { setBulkError(tc('saveFailed')); return; }
            setBulkStatus('');
            setBulkTag('');
            await refreshRecord(false);
        } catch { setBulkError(tc('saveFailed')); } finally { setBulkBusy(false); }
    };

    const deleteSelected = async (ids: string[]) => {
        if (ids.length === 0) return;
        setBulkBusy(true);
        setBulkError(null);
        try {
            const res = await fetch(api('api/contacts/bulk/delete'), {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'include', body: JSON.stringify({ ids }),
            });
            if (!res.ok) { setBulkError(tc('deleteFailed')); return; }
            if (selectedContactId && ids.includes(selectedContactId)) setSelectedContactId(null);
            setSelectedIds(new Set());
            await fetchContacts();
        } catch { setBulkError(tc('deleteFailed')); } finally { setBulkBusy(false); }
    };

    const copyValue = async (value: string) => {
        // The shared helper falls back to a text-area copy outside a secure context (LAN users on plain HTTP).
        if (!(await copyText(value))) return;
        setCopiedValue(value);
        window.setTimeout(() => setCopiedValue(prev => prev === value ? null : prev), 1500);
    };

    // ---- edit form -------------------------------------------------------------

    const emptyForm = (): ContactForm => ({
        name: '', channels: [{ type: 'phone', value: '' }, { type: 'email', value: '' }], company: '', role: '', tagsText: '',
        preferred_language: '', how_to_address: '', birthday: '', notes: '',
    });

    const openCreate = () => {
        setModalContact(null);
        setForm(emptyForm());
        setShowFormModal(true);
    };

    const openEdit = (c: Contact) => {
        setMenuOpen(false);
        setModalContact(c);
        const channels = contactChannels(c);
        setForm({
            name: c.name,
            channels: channels.length > 0 ? channels : emptyForm().channels,
            company: c.company ?? '',
            role: c.role ?? '',
            tagsText: (c.tags || []).join(', '),
            preferred_language: c.preferred_language ?? '',
            how_to_address: c.how_to_address ?? '',
            birthday: c.birthday ?? '',
            notes: c.notes ?? '',
        });
        setShowFormModal(true);
    };

    const closeForm = () => { setShowFormModal(false); setModalContact(null); setForm(null); };

    const handleSave = async () => {
        if (!form) return;
        const name = form.name.trim();
        if (!name) return;
        setSaving(true);
        const channels = form.channels.filter(ch => (ch.value || '').trim()).map(ch => ({ type: ch.type, value: (ch.value || '').trim() }));
        const tags = form.tagsText.split(',').map(t => t.trim()).filter(Boolean);
        const editing = !!modalContact?.id;
        // A cleared field is sent as null on PATCH so the store drops the old value; POST omits it.
        const text = (v: string) => v.trim() || (editing ? null : undefined);
        const body = {
            name,
            channels,
            company: text(form.company),
            role: text(form.role),
            tags,
            preferred_language: text(form.preferred_language),
            how_to_address: text(form.how_to_address),
            birthday: text(form.birthday),
            notes: text(form.notes),
            // Front Office is switched on only through the header switch and its confirmation;
            // a new record starts closed and an edit leaves the flag alone.
            ...(editing ? {} : { allow_as_assistant_user: false }),
        };
        try {
            if (editing && modalContact) {
                const res = await fetch(api(`api/contacts/${encodeURIComponent(modalContact.id)}`), {
                    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, credentials: 'include', body: JSON.stringify(body),
                });
                if (!res.ok) throw new Error(await res.text());
                await refreshRecord();
                closeForm();
                setSelectedContactId(modalContact.id);
            } else {
                const res = await fetch(api('api/contacts'), {
                    method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'include', body: JSON.stringify(body),
                });
                if (!res.ok) throw new Error(await res.text());
                const created = await res.json();
                await refreshRecord();
                closeForm();
                setSelectedContactId(created?.id ?? null);
            }
        } catch (e) {
            console.error(e);
            setFileError(tc('saveFailed'));
        } finally {
            setSaving(false);
        }
    };

    // ---- Escape ladder -----------------------------------------------------------
    // One rung per dismissable thing, counting up from the window's z-index (50):
    // the overflow menu (51), an inline editor (52), the edit form (60); the
    // ConfirmDialog registers its own rung at 70. Each rung is active only while
    // its UI is on screen and nothing above it is.

    // The menu and the inline editors are painted only inside the selected record, so a
    // rung counts as on screen only while that record is still in the list.
    const detailShown = !!selectedContactId && contacts.some(c => c.id === selectedContactId);
    const menuShown = menuOpen && detailShown;
    const editorShown = (statusEditing || tagEditing || showEventForm) && detailShown;
    const cancelInlineEditors = () => {
        editorCancelled.current = true;
        setStatusEditing(false);
        setTagEditing(false);
        setShowEventForm(false);
    };
    useEscapeLayer({ active: isOpen && confirm === null && !showFormModal && !menuShown && !editorShown, level: 50, onEscape: onClose });
    useEscapeLayer({ active: isOpen && confirm === null && !showFormModal && menuShown, level: 51, onEscape: () => setMenuOpen(false) });
    useEscapeLayer({ active: isOpen && confirm === null && !showFormModal && !menuShown && editorShown, level: 52, onEscape: cancelInlineEditors });
    useEscapeLayer({ active: isOpen && confirm === null && showFormModal, level: 60, onEscape: closeForm });

    // ---- derived ---------------------------------------------------------------

    const statusLabel = (value: string | null | undefined): string => {
        const v = (value || '').trim();
        if (!v) return tc('statusNone');
        switch (v) {
            case 'lead': return tc('statusLead');
            case 'in_contact': return tc('statusInContact');
            case 'customer': return tc('statusCustomer');
            case 'archived': return tc('statusArchived');
            default: return v;
        }
    };

    const channelLabel = (type: string | null | undefined): string => {
        switch (type) {
            case 'phone': return tc('channelPhone');
            case 'whatsapp': return tc('channelWhatsApp');
            case 'email':
            case 'mail': return tc('channelEmail');
            case 'telegram': return tc('channelTelegram');
            case 'discord': return tc('channelDiscord');
            default: return type || '';
        }
    };

    const channelPlaceholder = (type: string): string => {
        switch (type) {
            case 'phone': return tc('phPhone');
            case 'whatsapp': return tc('phWhatsApp');
            case 'telegram': return tc('phTelegram');
            case 'email': return tc('phEmail');
            default: return tc('phDiscord');
        }
    };

    const sourceLabel = (source: string | null | undefined): string => {
        if (!source || source === 'manual') return tc('sourceManual');
        if (source === 'agent') return tc('sourceAgentLabel');
        return channelLabel(source);
    };

    const bySource = (source: string | null | undefined) => tc('bySource', { source: source === 'agent' ? tc('sourceAgent') : tc('sourceUser') });

    const allStatusValues = useMemo(() => {
        const seen = new Set<string>(KNOWN_STATUSES);
        for (const s of statusValues) seen.add(s);
        for (const c of contacts) { const s = (c.status || '').trim(); if (s) seen.add(s); }
        return Array.from(seen);
    }, [statusValues, contacts]);

    const statusCounts = useMemo(() => {
        const counts = new Map<string, number>();
        let none = 0;
        for (const c of contacts) {
            const s = (c.status || '').trim();
            if (!s) { none += 1; continue; }
            counts.set(s, (counts.get(s) || 0) + 1);
        }
        return { counts, none };
    }, [contacts]);

    const statusChips = useMemo(() => {
        const known = KNOWN_STATUSES.filter(s => (statusCounts.counts.get(s) || 0) > 0);
        const custom = Array.from(statusCounts.counts.keys()).filter(s => !(KNOWN_STATUSES as readonly string[]).includes(s)).sort();
        return [...known, ...custom];
    }, [statusCounts]);

    const visibleContacts = useMemo(() => {
        const q = searchQuery.trim().toLowerCase();
        let list = [...contacts];
        if (statusFilter === NO_STATUS) list = list.filter(c => !(c.status || '').trim());
        else if (statusFilter) list = list.filter(c => (c.status || '').trim().toLowerCase() === statusFilter.toLowerCase());
        if (q) {
            list = list.filter(c =>
                (c.name || '').toLowerCase().includes(q)
                || (c.company || '').toLowerCase().includes(q)
                || (c.role || '').toLowerCase().includes(q)
                || (c.tags || []).some(t => t.toLowerCase().includes(q))
                || contactChannels(c).some(ch => ch.value.toLowerCase().includes(q))
                || Object.values(c.links || {}).some(l => (l?.display_name || '').toLowerCase().includes(q))
            );
        }
        const byName = (a: Contact, b: Contact) => (a.name || '').localeCompare(b.name || '', undefined, { sensitivity: 'base' });
        if (sortBy === 'name') list.sort(byName);
        else list.sort((a, b) => ((lastSeen(b)?.ts || 0) - (lastSeen(a)?.ts || 0)) || byName(a, b));
        return list;
    }, [contacts, searchQuery, statusFilter, sortBy]);

    const selectedContact = selectedContactId ? contacts.find(c => c.id === selectedContactId) ?? null : null;
    const reachCount = useMemo(() => contacts.filter(c => c.allow_as_assistant_user).length, [contacts]);

    const toggleSelected = (id: string) => {
        setSelectedIds(prev => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id); else next.add(id);
            return next;
        });
    };

    // Timeline grouped by calendar day, newest first (the server already sorts).
    const timelineDays = useMemo(() => {
        const groups: Array<{ key: string; ts: number; items: TimelineItem[] }> = [];
        for (const item of timeline) {
            const key = new Date(item.ts * 1000).toDateString();
            const last = groups[groups.length - 1];
            if (last && last.key === key) last.items.push(item);
            else groups.push({ key, ts: item.ts, items: [item] });
        }
        return groups;
    }, [timeline]);

    const dayLabel = (ts: number): string => {
        const d = new Date(ts * 1000);
        const now = new Date();
        if (d.toDateString() === now.toDateString()) return td('today');
        const y = new Date(now);
        y.setDate(now.getDate() - 1);
        if (d.toDateString() === y.toDateString()) return td('yesterday');
        return fmtLongDay(ts);
    };

    const mailCount = useMemo(() => timeline.filter(i => i.kind === 'mail').length, [timeline]);

    const commitStatus = (c: Contact) => {
        if (editorCancelled.current) { editorCancelled.current = false; return; }
        setStatusEditing(false);
        const value = statusDraft.trim();
        if (value !== (c.status || '').trim()) patchContact(c.id, { status: value || null });
    };

    const commitTag = (c: Contact) => {
        if (editorCancelled.current) { editorCancelled.current = false; return; }
        setTagEditing(false);
        const value = tagDraft.trim();
        setTagDraft('');
        if (!value) return;
        const existing = c.tags || [];
        if (existing.some(t => t.toLowerCase() === value.toLowerCase())) return;
        patchContact(c.id, { tags: [...existing, value] });
    };

    if (!isOpen) return null;

    // ---- confirm dialog wording --------------------------------------------------

    let confirmTitle = '';
    let confirmBody = '';
    let confirmYes = '';
    let confirmNo = tcm('cancel');
    if (confirm?.kind === 'deleteContact') {
        confirmTitle = tc('deleteContactConfirmTitle');
        confirmBody = tc('deleteContactConfirmBody', { name: confirm.contact.name });
        confirmYes = tc('confirmDelete');
    } else if (confirm?.kind === 'deleteSelected') {
        confirmTitle = tc('deleteSelectedConfirmTitle', { count: confirm.ids.length });
        confirmBody = tc('deleteSelectedConfirmBody');
        confirmYes = tc('confirmDelete');
    } else if (confirm?.kind === 'removeNote') {
        confirmTitle = tc('removeNoteConfirm');
        confirmBody = confirm.text;
        confirmYes = tc('remove');
    } else if (confirm?.kind === 'removeEvent') {
        confirmTitle = tc('removeEventConfirm');
        confirmBody = confirm.title;
        confirmYes = tc('remove');
    } else if (confirm?.kind === 'reach') {
        confirmTitle = tw('allowReachConfirmTitle');
        confirmBody = tw('allowReachConfirmBody', { name: confirm.contact.name });
        confirmYes = tw('allowReachConfirmYes');
        confirmNo = tw('allowReachConfirmNo');
    }
    const runConfirm = () => {
        const c = confirm;
        setConfirm(null);
        if (!c) return;
        if (c.kind === 'deleteContact') deleteContact(c.contact.id);
        else if (c.kind === 'deleteSelected') deleteSelected(c.ids);
        else if (c.kind === 'removeNote') removeNote(c.contactId, c.noteId);
        else if (c.kind === 'removeEvent') removeEvent(c.contactId, c.eventId);
        else if (c.kind === 'reach') patchContact(c.contact.id, { allow_as_assistant_user: true });
    };

    // ---- render helpers ----------------------------------------------------------

    const renderTimelineItem = (item: TimelineItem, c: Contact, isLast: boolean) => {
        let icon: React.ReactNode;
        let iconCls = 'bg-gray-100 border-gray-200';
        let heading = '';
        let who: React.ReactNode = null;
        switch (item.kind) {
            case 'message':
                icon = <ChannelIcon type={item.channel || ''} className="w-3.5 h-3.5" />;
                iconCls = item.channel === 'whatsapp' ? 'bg-green-100 border-green-200' : item.channel === 'discord' ? 'bg-violet-100 border-violet-200' : 'bg-sky-100 border-sky-200';
                heading = channelLabel(item.channel);
                who = item.direction === 'out' ? tc('fromAgentTo', { name: c.name }) : c.name;
                break;
            case 'mail':
                icon = <Mail className="w-3.5 h-3.5 text-sky-800" />;
                iconCls = 'bg-sky-100 border-sky-200';
                heading = tc('channelEmail');
                who = item.direction === 'out' ? bySource('user') : tc('bySource', { source: item.ref?.from || '' });
                break;
            case 'note':
                icon = <StickyNote className="w-3.5 h-3.5 text-amber-800" />;
                iconCls = 'bg-amber-100 border-amber-200';
                heading = tc('note');
                who = bySource(item.source);
                break;
            case 'event':
                icon = <CalendarDays className="w-3.5 h-3.5 text-violet-800" />;
                iconCls = 'bg-violet-100 border-violet-200';
                heading = tc('eventAdded');
                who = bySource(item.source);
                break;
            default:
                icon = <UserPlus className="w-3.5 h-3.5 text-gray-600" />;
                heading = tc('contactAdded');
                who = item.source && item.source !== 'manual' && item.source !== 'agent'
                    ? tc('importedFrom', { channel: channelLabel(item.source) })
                    : bySource(item.source);
        }
        const chatChannel = item.kind === 'message' && (item.channel === 'whatsapp' || item.channel === 'telegram') ? item.channel : null;
        const key = item.id || `${item.kind}-${item.ts}-${item.ref?.message_id || item.ref?.note_id || item.ref?.event_id || ''}`;
        return (
            <div key={key} className="relative grid grid-cols-[28px_1fr] gap-3 py-2">
                {!isLast && <span className="absolute left-[13px] top-9 -bottom-2 w-px bg-gray-200" />}
                <div className={cn('w-7 h-7 rounded-full border grid place-items-center', iconCls)}>{icon}</div>
                <div className="min-w-0">
                    <div className="flex items-center gap-2 text-xs text-gray-600 min-w-0">
                        <span className="font-semibold text-gray-900 shrink-0">{heading}</span>
                        <span className="truncate">{who}</span>
                        <span className="ml-auto shrink-0 text-gray-500">{fmtTime(item.ts)}</span>
                        {item.kind === 'note' && item.ref?.note_id && (
                            <button type="button" title={tc('remove')}
                                onClick={() => setConfirm({ kind: 'removeNote', contactId: c.id, noteId: item.ref.note_id as string, text: item.body })}
                                className="p-0.5 rounded text-gray-400 hover:text-red-600 shrink-0">
                                <Trash2 className="w-3.5 h-3.5" />
                            </button>
                        )}
                    </div>
                    {item.kind === 'message' ? (
                        <div className={cn('mt-1 inline-block max-w-[92%] rounded-xl px-3 py-2 text-[13px] text-gray-900 whitespace-pre-wrap break-words',
                            item.direction === 'out' ? 'bg-green-100 rounded-tr-sm' : 'bg-gray-100 rounded-tl-sm')}>
                            {item.body}
                        </div>
                    ) : item.kind === 'mail' ? (
                        <div className="mt-0.5 text-[13px] text-gray-900">
                            <span className="font-medium">{item.title || tc('noSubject')}</span>
                            {item.body && <span className="text-gray-600"> {item.body}</span>}
                        </div>
                    ) : item.kind === 'event' ? (
                        <div className="mt-0.5 text-[13px] text-gray-900">
                            {item.ref?.when_ts ? tc('withTime', { title: item.title || item.body, time: fmtDateTime(item.ref.when_ts) }) : (item.title || item.body)}
                        </div>
                    ) : item.kind === 'created' ? (
                        // The store puts the channel's display name in title and the source in body.
                        item.title ? <div className="mt-0.5 text-xs text-gray-600">{tc('shownThereAs', { name: item.title })}</div> : null
                    ) : (
                        <div className="mt-0.5 text-[13px] text-gray-900 whitespace-pre-wrap break-words">{item.body}</div>
                    )}
                    {chatChannel && onOpenChat && item.ref?.chat_id && (
                        <button type="button" onClick={() => onOpenChat(chatChannel, item.ref.chat_id as string)}
                            className="mt-1 inline-flex items-center gap-1 text-xs text-sky-700 hover:underline">
                            {tc('openInChat')}<ArrowUpRight className="w-3 h-3" />
                        </button>
                    )}
                </div>
            </div>
        );
    };

    const renderDetail = (c: Contact) => {
        const channels = contactChannels(c);
        const now = Date.now() / 1000;
        const upcoming = (c.events || []).filter(e => Number(e.when_ts) >= now).sort((a, b) => a.when_ts - b.when_ts);
        const past = (c.events || []).filter(e => now > Number(e.when_ts)).slice(-3);
        const cal = overview?.calendar_events || [];
        const stats = overview?.stats ?? null;
        const endpoints = overview?.endpoints || {};
        const waChat = endpoints.whatsapp?.[0];
        const tgChat = endpoints.telegram?.[0];
        const lastContact = overview?.last_contact ?? lastSeen(c);
        const created = overview?.created ?? (c.created_at ? { ts: c.created_at, source: c.source || 'manual' } : null);
        const bdays = c.birthday ? birthdayInDays(c.birthday) : null;
        const reachOn = !!c.allow_as_assistant_user;
        const statusValue = (c.status || '').trim();
        const notesCount = (c.notes_log || []).length;
        const eventsCount = (c.events || []).length;
        // Mails have no server-side total, so their badge shows the loaded count only while
        // mails are on screen, and the "all" tab carries no number at all.
        const tabs: Array<{ id: ActivityTab; label: string; count: number | null }> = [
            { id: 'all', label: tc('filterAll'), count: null },
            { id: 'message', label: tc('messages'), count: stats?.messages || 0 },
            { id: 'note', label: tc('notes'), count: notesCount },
            { id: 'event', label: tc('tabEvents'), count: eventsCount },
            { id: 'mail', label: tc('tabMails'), count: tab === 'mail' || tab === 'all' ? mailCount : null },
        ];
        const focusComposer = () => { setTab(t => t === 'mail' || t === 'event' ? 'all' : t); composerRef.current?.focus(); };

        return (
            <>
                {/* header */}
                <div className="flex items-start gap-4 px-6 py-4 border-b border-gray-200 bg-gray-50 shrink-0 max-md:px-4 max-md:py-3 max-md:flex-wrap">
                    <ContactAvatar name={c.name} size="lg" />
                    <div className="min-w-0 flex-1">
                        <h3 className="text-lg font-semibold text-gray-900 flex items-center gap-2 min-w-0">
                            <span className="truncate">{c.name}</span>
                            {reachOn && <span className="w-2 h-2 rounded-full bg-green-500 shrink-0" title={tc('canReachAgent')} />}
                        </h3>
                        {(c.company || c.role) && (
                            <p className="text-sm text-gray-600 truncate">
                                {c.company && c.role ? tc('companyRole', { company: c.company, role: c.role }) : (c.company || c.role)}
                            </p>
                        )}
                        <div className="mt-2 flex items-center gap-2 flex-wrap">
                            {statusEditing ? (
                                <input autoFocus list="contact-status-values" value={statusDraft}
                                    onChange={e => setStatusDraft(e.target.value)}
                                    onBlur={() => commitStatus(c)}
                                    onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); (e.target as HTMLInputElement).blur(); } }}
                                    placeholder={tc('statusNone')} title={tc('statusPlaceholder')}
                                    className={cn(INPUT, 'w-44 py-1 rounded-full')} />
                            ) : (
                                <button type="button" onClick={() => { editorCancelled.current = false; setStatusDraft(statusValue); setStatusEditing(true); }}
                                    title={tc('statusPlaceholder')}
                                    className={cn('inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-medium', STATUS_PILL[statusValue] || PILL_DEFAULT)}>
                                    {statusLabel(statusValue)}<ChevronDown className="w-3 h-3" />
                                </button>
                            )}
                            {(c.tags || []).map(tag => (
                                <span key={tag} className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-gray-100 border border-gray-200 text-gray-700">
                                    {tag}
                                    <button type="button" title={tc('removeTag')}
                                        onClick={() => patchContact(c.id, { tags: (c.tags || []).filter(t => t !== tag) })}
                                        className="text-gray-400 hover:text-gray-900"><X className="w-3 h-3" /></button>
                                </span>
                            ))}
                            {tagEditing ? (
                                <input autoFocus list="contact-tag-values" value={tagDraft}
                                    onChange={e => setTagDraft(e.target.value)}
                                    onBlur={() => commitTag(c)}
                                    onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); (e.target as HTMLInputElement).blur(); } }}
                                    placeholder={tc('tagPlaceholder')}
                                    className={cn(INPUT, 'w-44 py-0.5 rounded-full text-xs')} />
                            ) : (
                                <button type="button" onClick={() => { editorCancelled.current = false; setTagDraft(''); setTagEditing(true); }}
                                    className="inline-flex items-center gap-0.5 px-2 py-0.5 rounded-full text-xs border border-dashed border-gray-300 text-gray-600 hover:text-gray-900 hover:border-gray-400">
                                    <Plus className="w-3 h-3" />{tc('addTag')}
                                </button>
                            )}
                        </div>
                        {fileError && <p className="text-xs text-red-600 mt-1">{fileError}</p>}
                    </div>
                    <div className="flex flex-col items-end gap-2 shrink-0 max-md:w-full max-md:items-start">
                        <div className="flex items-center gap-2 flex-wrap max-md:justify-start">
                            {waChat && onOpenChat && (
                                <button type="button" onClick={() => onOpenChat('whatsapp', waChat)} className={BTN}>
                                    <MessageCircle className="w-3.5 h-3.5 text-green-600" />{tc('openWhatsAppChat')}
                                </button>
                            )}
                            {tgChat && onOpenChat && (
                                <button type="button" onClick={() => onOpenChat('telegram', tgChat)} className={BTN}>
                                    <Send className="w-3.5 h-3.5 text-sky-600" />{tc('openTelegramChat')}
                                </button>
                            )}
                            <button type="button" onClick={focusComposer} className={BTN}><Pencil className="w-3.5 h-3.5" />{tc('note')}</button>
                            <button type="button" onClick={() => { editorCancelled.current = false; setShowEventForm(true); }} className={BTN}>
                                <CalendarDays className="w-3.5 h-3.5" />{tc('event')}
                            </button>
                            <div className="relative" ref={menuRef}>
                                <button type="button" onClick={() => setMenuOpen(o => !o)} title={tc('moreActions')} className={cn(BTN, 'px-2')}>
                                    <MoreHorizontal className="w-4 h-4" />
                                </button>
                                {menuOpen && (
                                    <div className="absolute right-0 top-full mt-1 z-10 min-w-[10rem] rounded-xl border border-gray-200 bg-white shadow-lg py-1 text-sm">
                                        <button type="button" onClick={() => openEdit(c)} className="w-full text-left px-3 py-1.5 hover:bg-gray-100 text-gray-900 flex items-center gap-2">
                                            <Pencil className="w-3.5 h-3.5" />{tcm('edit')}
                                        </button>
                                        <button type="button" onClick={() => { setMenuOpen(false); setConfirm({ kind: 'deleteContact', contact: c }); }}
                                            className="w-full text-left px-3 py-1.5 hover:bg-gray-100 text-red-600 flex items-center gap-2">
                                            <Trash2 className="w-3.5 h-3.5" />{tc('deleteContact')}
                                        </button>
                                    </div>
                                )}
                            </div>
                        </div>
                        <label className="flex items-center gap-2 text-xs text-gray-700 cursor-pointer select-none">
                            <span>{tw('allowReach')}</span>
                            <button type="button" role="switch" aria-checked={reachOn}
                                onClick={() => reachOn ? patchContact(c.id, { allow_as_assistant_user: false }) : setConfirm({ kind: 'reach', contact: c })}
                                className={cn('relative w-11 h-6 rounded-full transition-colors', reachOn ? 'bg-gray-800 dark:bg-[#d9d9d9]' : 'bg-gray-300 dark:bg-[#333333]')}>
                                <div className={cn('absolute top-1 w-4 h-4 rounded-full bg-white shadow transition-transform', reachOn ? 'translate-x-6 dark:bg-[#1a1a1a]' : 'translate-x-1 dark:bg-[#e8e8e8]')} />
                            </button>
                        </label>
                        <p className="text-[11px] text-gray-500 text-right max-w-[300px] max-md:text-left">{reachOn ? tc('reachHintOn') : tc('reachHintOff')}</p>
                    </div>
                </div>

                {/* body */}
                <div className="flex-1 overflow-y-auto min-h-0 p-5 max-md:p-4">
                    <div className="grid grid-cols-[1fr_320px] gap-4 items-start max-md:grid-cols-1">
                        {/* activity */}
                        <div className={cn(CARD, 'min-w-0')}>
                            <div className={cn(CARD_HEAD, 'flex-wrap')}>
                                <h4 className={CARD_TITLE}>{tc('activity')}</h4>
                                <div className="flex gap-0.5 flex-wrap">
                                    {tabs.map(t => (
                                        <button key={t.id} type="button" onClick={() => setTab(t.id)}
                                            className={cn('px-2.5 py-1 rounded-lg text-xs', tab === t.id ? ACTIVE : 'text-gray-600 hover:bg-gray-100')}>
                                            {t.label}{t.count !== null && <b className={cn('ml-1 font-medium', tab === t.id ? 'opacity-80' : 'text-gray-500')}>{t.count}</b>}
                                        </button>
                                    ))}
                                </div>
                            </div>
                            <div className="flex gap-2 px-3.5 py-3 border-b border-gray-200 bg-gray-50">
                                <input ref={composerRef} type="text" value={noteText} onChange={e => setNoteText(e.target.value)}
                                    placeholder={tc('noteComposerPlaceholder', { name: c.name })}
                                    onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); handleAddNote(c.id); } }}
                                    className={cn(INPUT, 'flex-1 min-w-0')} />
                                <button type="button" onClick={() => handleAddNote(c.id)} disabled={!noteText.trim()}
                                    className={cn('px-3 py-1.5 rounded-lg text-sm font-medium disabled:opacity-50', PRIMARY)}>{tcm('save')}</button>
                            </div>
                            <div className="px-3.5 py-3">
                                {timelineDays.length === 0 && !timelineLoading && (
                                    <p className="text-sm text-gray-500">{timelineFailed ? tc('timelineFailed') : tc('timelineEmpty')}</p>
                                )}
                                {timelineDays.map((day, di) => (
                                    <div key={day.key}>
                                        <div className={cn('text-[11px] uppercase tracking-wide text-gray-500 ml-10 mb-1', di === 0 ? 'mt-0.5' : 'mt-3')}>{dayLabel(day.ts)}</div>
                                        {day.items.map((item, i) => renderTimelineItem(item, c, di === timelineDays.length - 1 && i === day.items.length - 1))}
                                    </div>
                                ))}
                                {timelineDays.length > 0 && timelineFailed && <p className="text-xs text-red-600 mt-2">{tc('timelineFailed')}</p>}
                                {timelineLoading && <p className="text-xs text-gray-500 mt-2">{tcm('loading')}</p>}
                                {nextCursor && !timelineLoading && (
                                    <button type="button" onClick={() => loadTimeline(c.id, tab, nextCursor)}
                                        className="block mx-auto mt-3 px-3 py-1 rounded-full border border-gray-200 bg-gray-50 text-[11px] text-gray-600 hover:text-gray-900 hover:bg-gray-100">
                                        {tc('loadOlder')}
                                    </button>
                                )}
                            </div>
                        </div>

                        {/* right cards */}
                        <div className="flex flex-col gap-4 min-w-0">
                            <div className={CARD}>
                                <div className={CARD_HEAD}>
                                    <h4 className={CARD_TITLE}>{tc('channels')}</h4>
                                    <button type="button" onClick={() => openEdit(c)} className={BTN_GHOST}><Plus className="w-3 h-3" />{tc('addChannel')}</button>
                                </div>
                                <div className="px-3.5 py-1">
                                    {channels.length === 0 && <p className="py-2 text-[13px] text-gray-500">{tc('notSet')}</p>}
                                    {channels.map((ch, i) => (
                                        <div key={i} className={KV}>
                                            <span className="flex items-center gap-2 text-gray-600 shrink-0">
                                                <ChannelIcon type={ch.type} />{ch.type === 'phone' ? tc('phoneUsedAsWhatsApp') : channelLabel(ch.type)}
                                            </span>
                                            <span className="flex items-center gap-1.5 text-gray-900 min-w-0">
                                                <span className="truncate">{ch.value}</span>
                                                <button type="button" onClick={() => copyValue(ch.value)} title={copiedValue === ch.value ? tcm('copied') : tcm('copy')}
                                                    className="p-0.5 rounded text-gray-400 hover:text-gray-900 shrink-0">
                                                    {copiedValue === ch.value ? <Check className="w-3 h-3 text-green-600" /> : <Copy className="w-3 h-3" />}
                                                </button>
                                            </span>
                                        </div>
                                    ))}
                                </div>
                            </div>

                            <div className={CARD}>
                                <div className={CARD_HEAD}>
                                    <h4 className={CARD_TITLE}>{tc('file')}</h4>
                                    <button type="button" onClick={() => openEdit(c)} className={BTN_GHOST}><Pencil className="w-3 h-3" />{tcm('edit')}</button>
                                </div>
                                <div className="px-3.5 py-1">
                                    {([
                                        [tc('company'), c.company],
                                        [tc('role'), c.role],
                                        [tc('language'), c.preferred_language],
                                        [tc('howToAddress'), c.how_to_address],
                                    ] as Array<[string, string | null | undefined]>).map(([k, v]) => (
                                        <div key={k} className={KV}>
                                            <span className="text-gray-600 shrink-0">{k}</span>
                                            <span className={cn('text-right truncate', v ? 'text-gray-900' : 'text-gray-500')}>{v || tc('notSet')}</span>
                                        </div>
                                    ))}
                                    <div className={KV}>
                                        <span className="text-gray-600 shrink-0">{tc('birthday')}</span>
                                        <span className={cn('text-right flex items-center gap-1.5 min-w-0', c.birthday ? 'text-gray-900' : 'text-gray-500')}>
                                            <span className="truncate">{c.birthday || tc('notSet')}</span>
                                            {bdays !== null && <small className="text-gray-500 shrink-0">{bdays === 0 ? td('today') : tc('inDays', { count: bdays })}</small>}
                                        </span>
                                    </div>
                                    <div className={KV}>
                                        <span className="text-gray-600 shrink-0">{tc('source')}</span>
                                        <span className="text-right flex items-center gap-1.5 text-gray-900 min-w-0">
                                            <span>{sourceLabel(created?.source ?? c.source)}</span>
                                            {created?.ts ? <small className="text-gray-500">{tc('sinceDate', { date: fmtDate(created.ts) })}</small> : null}
                                        </span>
                                    </div>
                                    {c.notes && <p className="py-2 text-[13px] text-gray-700 whitespace-pre-wrap">{c.notes}</p>}
                                </div>
                            </div>

                            <div className={CARD}>
                                <div className={CARD_HEAD}>
                                    <h4 className={CARD_TITLE}>{tc('events')}</h4>
                                    <button type="button" onClick={() => { editorCancelled.current = false; setShowEventForm(o => !o); }} className={BTN_GHOST}><Plus className="w-3 h-3" />{tc('event')}</button>
                                </div>
                                <div className="px-3.5 py-1">
                                    {upcoming.length === 0 && cal.length === 0 && !showEventForm && <p className="py-2 text-[13px] text-gray-500">{tc('eventsEmpty')}</p>}
                                    {upcoming.map(e => {
                                        const d = new Date(e.when_ts * 1000);
                                        return (
                                            <div key={e.id} className="flex items-start gap-2.5 py-2 border-b border-gray-200 last:border-b-0">
                                                <div className="w-11 shrink-0 text-center rounded-lg bg-gray-100 py-1">
                                                    <b className="block text-[15px] leading-tight text-gray-900">{d.getDate()}</b>
                                                    <small className="text-[10px] uppercase text-gray-500">{d.toLocaleDateString([], { month: 'short' })}</small>
                                                </div>
                                                <div className="flex-1 min-w-0 text-[13px]">
                                                    <div className="text-gray-900 truncate">{tc('withTime', { title: e.title, time: fmtTime(e.when_ts) })}</div>
                                                    <small className="block text-[11.5px] text-gray-500 truncate">{e.note ? e.note : tc('ownEvent')}</small>
                                                </div>
                                                <button type="button" onClick={() => setConfirm({ kind: 'removeEvent', contactId: c.id, eventId: e.id, title: e.title })} title={tc('remove')}
                                                    className="p-1 rounded text-gray-400 hover:text-red-600 shrink-0"><Trash2 className="w-3.5 h-3.5" /></button>
                                            </div>
                                        );
                                    })}
                                    {cal.map((e, i) => {
                                        const ts = e.start ? new Date(e.start).getTime() / 1000 : null;
                                        const allDay = !!e.start && /^\d{4}-\d{2}-\d{2}$/.test(e.start);
                                        const d = ts ? new Date(ts * 1000) : null;
                                        return (
                                            <div key={e.id || i} className="flex items-start gap-2.5 py-2 border-b border-gray-200 last:border-b-0">
                                                <div className="w-11 shrink-0 text-center rounded-lg bg-gray-100 py-1">
                                                    <b className="block text-[15px] leading-tight text-gray-900">{d ? d.getDate() : EMPTY_FIGURE}</b>
                                                    <small className="text-[10px] uppercase text-gray-500">{d ? d.toLocaleDateString([], { month: 'short' }) : ''}</small>
                                                </div>
                                                <div className="flex-1 min-w-0 text-[13px]">
                                                    <div className="text-gray-900 truncate">
                                                        {ts && !allDay ? tc('withTime', { title: e.summary || '', time: fmtTime(ts) }) : e.summary}
                                                    </div>
                                                    <small className="block text-[11.5px] text-gray-500">{tc('fromCalendar')}</small>
                                                </div>
                                                {(e.htmlLink || e.webLink) && (
                                                    <a href={e.htmlLink || e.webLink} target="_blank" rel="noopener noreferrer" className="p-1 text-gray-400 hover:text-gray-900 shrink-0">
                                                        <ArrowUpRight className="w-3.5 h-3.5" />
                                                    </a>
                                                )}
                                            </div>
                                        );
                                    })}
                                    {showEventForm && (
                                        <div className="flex gap-2 flex-wrap py-2">
                                            <input autoFocus type="text" value={eventTitle} onChange={e => setEventTitle(e.target.value)} placeholder={tc('eventTitlePlaceholder')}
                                                onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); handleAddEvent(c.id); } }}
                                                className={cn(INPUT, 'flex-1 min-w-[8rem]')} />
                                            <input type="datetime-local" value={eventWhen} onChange={e => setEventWhen(e.target.value)} aria-label={tc('eventWhenPlaceholder')}
                                                className={cn(INPUT, 'min-w-0')} />
                                            <button type="button" onClick={() => handleAddEvent(c.id)} disabled={!eventTitle.trim() || !eventWhen}
                                                className={cn('px-3 py-1.5 rounded-lg text-sm disabled:opacity-50', PRIMARY)}>{tc('addEvent')}</button>
                                        </div>
                                    )}
                                    {past.length > 0 && (
                                        <details className="py-2 text-xs text-gray-500">
                                            <summary className="cursor-pointer">{tc('pastEventsCount', { count: past.length })}</summary>
                                            {past.map(e => <div key={e.id} className="pl-2 py-0.5">{fmtDateTime(e.when_ts)} {e.title}</div>)}
                                        </details>
                                    )}
                                </div>
                            </div>

                            <div className={CARD}>
                                <div className={CARD_HEAD}><h4 className={CARD_TITLE}>{tc('stats')}</h4></div>
                                <div className="p-3.5 grid grid-cols-2 gap-2">
                                    {([
                                        [stats ? String(stats.messages) : EMPTY_FIGURE, tc('messages')],
                                        [stats ? String(stats.from_agent) : EMPTY_FIGURE, tc('statFromAgent')],
                                        [stats?.first_ts ? fmtDate(stats.first_ts) : EMPTY_FIGURE, tc('statOldestStored')],
                                        [lastContact?.ts ? fmtWhen(lastContact.ts) : EMPTY_FIGURE, tc('statLastContact')],
                                    ] as Array<[string, string]>).map(([n, l]) => (
                                        <div key={l} className="rounded-lg bg-gray-100 px-3 py-2">
                                            <div className="text-lg font-semibold text-gray-900 truncate">{n}</div>
                                            <div className="text-[11px] text-gray-600">{l}</div>
                                        </div>
                                    ))}
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </>
        );
    };

    const anySelected = selectedIds.size > 0;

    return (
        <>
            <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50 max-md:p-0" onClick={onClose}>
                <div
                    className="relative bg-gray-50 w-full max-w-[95vw] h-[90vh] rounded-2xl shadow-2xl border border-gray-200 flex flex-col overflow-hidden text-gray-900 max-md:max-w-none max-md:h-[100dvh] max-md:rounded-none max-md:border-0"
                    onClick={e => e.stopPropagation()}
                >
                    {/* window header */}
                    <div className="flex items-center justify-between gap-3 px-5 py-3 border-b border-gray-200 shrink-0 bg-white max-md:px-4 max-md:py-3">
                        <div className="flex items-center gap-3 max-md:gap-3 min-w-0">
                            <div className="w-10 h-10 rounded-xl bg-gray-600 flex items-center justify-center text-white shrink-0 max-md:w-10 max-md:h-10 max-md:rounded-xl max-md:shadow-none">
                                <Users className="w-5 h-5 max-md:w-5 max-md:h-5" />
                            </div>
                            <div className="min-w-0">
                                <h2 className="text-xl font-bold text-gray-900 max-md:text-lg truncate">{tc('title')}</h2>
                                <p className="text-sm text-gray-500 max-md:text-xs truncate">{tc('subtitle', { count: contacts.length, reach: reachCount })}</p>
                            </div>
                        </div>
                        <div className="flex items-center gap-2 shrink-0">
                            <button type="button" onClick={openCreate}
                                className={cn('flex items-center gap-2 px-3 py-2 rounded-lg font-medium text-sm', PRIMARY)}>
                                <Plus className="w-4 h-4" />
                                <span className="max-md:hidden">{tc('addContact')}</span>
                            </button>
                            <button type="button" onClick={onClose} title={tcm('close')} className="p-2 hover:bg-gray-200 rounded-lg transition-colors">
                                <X className="w-5 h-5 text-gray-500" />
                            </button>
                        </div>
                    </div>

                    <div className="flex flex-1 min-h-0 max-md:flex-col">
                        {/* list column */}
                        <div className="w-[300px] shrink-0 border-r border-gray-200 flex flex-col bg-white max-md:w-full max-md:max-h-[38vh] max-md:border-r-0 max-md:border-b max-md:shrink-0">
                            <div className="px-3 pt-3 pb-2 border-b border-gray-200 shrink-0">
                                <div className="relative">
                                    <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400 pointer-events-none" />
                                    <input type="text" value={searchQuery} onChange={e => setSearchQuery(e.target.value)} placeholder={tc('searchPlaceholder')}
                                        className={cn(INPUT, 'w-full pl-9')} />
                                </div>
                                <div className="flex flex-wrap gap-1.5 mt-2.5">
                                    {[{ id: '', label: tc('filterAll'), count: contacts.length },
                                    ...statusChips.map(s => ({ id: s, label: statusLabel(s), count: statusCounts.counts.get(s) || 0 })),
                                    ...(statusCounts.none > 0 ? [{ id: NO_STATUS, label: tc('statusNone'), count: statusCounts.none }] : [])].map(chip => (
                                        <button key={chip.id || 'all'} type="button" onClick={() => setStatusFilter(chip.id)}
                                            className={cn('px-2 py-0.5 rounded-full text-[11px] border border-transparent',
                                                statusFilter === chip.id ? ACTIVE : 'bg-gray-100 text-gray-600 hover:text-gray-900')}>
                                            {chip.label}<b className={cn('ml-1 font-medium', statusFilter === chip.id ? 'opacity-80' : 'text-gray-500')}>{chip.count}</b>
                                        </button>
                                    ))}
                                </div>
                                <button type="button" onClick={() => setSortBy(s => s === 'last' ? 'name' : 'last')}
                                    className="mt-2.5 w-full flex items-center justify-between text-[11px] text-gray-600 hover:text-gray-900">
                                    <span>{sortBy === 'last' ? tc('sortedByLastContact') : tc('sortedByName')}</span>
                                    <ChevronDown className="w-3.5 h-3.5" />
                                </button>
                            </div>
                            {anySelected && (
                                // Slides in under the filters; transform and opacity only, so the repaint stays cheap.
                                <div className="px-3 py-2 border-b border-gray-200 bg-gray-50 flex flex-col gap-1.5 shrink-0 text-xs animate-in fade-in slide-in-from-top-1 duration-150">
                                    <div className="flex items-center justify-between">
                                        <span className="font-medium text-gray-900">{tc('selected', { count: selectedIds.size })}</span>
                                        <button type="button" onClick={() => setSelectedIds(new Set())} className="text-gray-600 hover:text-gray-900">{tcm('deselectAll')}</button>
                                    </div>
                                    <div className="flex gap-1.5">
                                        <select value={bulkStatus} onChange={e => setBulkStatus(e.target.value)} aria-label={tc('bulkStatusPlaceholder')}
                                            className={cn(INPUT, 'flex-1 min-w-0 py-1 text-xs')}>
                                            <option value="">{tc('bulkStatusPlaceholder')}</option>
                                            <option value={NO_STATUS}>{tc('statusNone')}</option>
                                            {allStatusValues.map(s => <option key={s} value={s}>{statusLabel(s)}</option>)}
                                        </select>
                                        <input list="contact-tag-values" value={bulkTag} onChange={e => setBulkTag(e.target.value)} placeholder={tc('addTag')}
                                            className={cn(INPUT, 'flex-1 min-w-0 py-1 text-xs')} />
                                    </div>
                                    {bulkError && <p className="text-xs text-red-600">{bulkError}</p>}
                                    <div className="flex items-center gap-1.5">
                                        <button type="button" onClick={applyBulk} disabled={bulkBusy || (!bulkStatus.trim() && !bulkTag.trim())}
                                            className={cn('flex-1 px-2 py-1 rounded-lg text-xs font-medium disabled:opacity-50', PRIMARY)}>{tc('apply')}</button>
                                        {/* Quiet until hovered: deleting asks in the house dialog first, so the bar needs no red button. */}
                                        <button type="button" onClick={() => setConfirm({ kind: 'deleteSelected', ids: Array.from(selectedIds) })} disabled={bulkBusy}
                                            title={tc('deleteSelected')}
                                            className={cn(BTN_GHOST, 'px-2 py-1 hover:text-red-600 hover:bg-red-50 disabled:opacity-50')}>
                                            <Trash2 className="w-3.5 h-3.5" />{tcm('delete')}
                                        </button>
                                    </div>
                                </div>
                            )}
                            <div className="flex-1 overflow-y-auto min-h-0">
                                {loading && contacts.length === 0 ? (
                                    <p className="p-3 text-sm text-gray-500">{tcm('loading')}</p>
                                ) : visibleContacts.length === 0 ? (
                                    <p className="p-3 text-sm text-gray-500">{contacts.length === 0 ? tc('noContacts') : tc('noMatches')}</p>
                                ) : (
                                    <ul>
                                        {visibleContacts.map(c => {
                                            const isSelected = selectedIds.has(c.id);
                                            const showBox = anySelected || isSelected;
                                            const active = selectedContactId === c.id;
                                            const seen = lastSeen(c);
                                            const status = (c.status || '').trim();
                                            return (
                                                <li key={c.id} className="group">
                                                    <div role="button" tabIndex={0} onClick={() => setSelectedContactId(c.id)}
                                                        onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setSelectedContactId(c.id); } }}
                                                        className={cn('w-full text-left grid grid-cols-[34px_1fr_auto] gap-2.5 items-center px-3 py-2 border-b border-gray-200 border-l-2 transition-colors cursor-pointer select-none focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-gray-400',
                                                            active ? 'bg-gray-100 border-l-gray-900 dark:bg-[#3a3a3a] dark:text-white dark:border-l-[#e6e6e6]' : 'border-l-transparent hover:bg-gray-50')}>
                                                        {/* The avatar IS the checkbox: hovering the row (or having any selection) fades the
                                                            initials out and the box in, in the same 34px slot. A click on the slot selects
                                                            without opening the record; on a touch screen the avatar itself is the target. */}
                                                        <span className="relative w-[34px] h-[34px] shrink-0"
                                                            onClick={e => { e.stopPropagation(); if (!(e.target instanceof HTMLInputElement)) toggleSelected(c.id); }}>
                                                            <span className={cn('absolute inset-0 transition-[opacity,transform] duration-150 ease-out',
                                                                showBox ? 'opacity-0 scale-75' : 'opacity-100 scale-100 group-hover:opacity-0 group-hover:scale-75')}>
                                                                <ContactAvatar name={c.name} size="sm" />
                                                            </span>
                                                            <span className={cn('absolute inset-0 grid place-items-center transition-[opacity,transform] duration-150 ease-out',
                                                                showBox ? 'opacity-100 scale-100' : 'opacity-0 scale-75 group-hover:opacity-100 group-hover:scale-100')}>
                                                                <input type="checkbox" checked={isSelected} tabIndex={showBox ? 0 : -1} onChange={() => toggleSelected(c.id)}
                                                                    aria-label={tc('selectContact')} className="w-[18px] h-[18px] rounded cursor-pointer accent-gray-900 dark:accent-[#d9d9d9]" />
                                                            </span>
                                                        </span>
                                                        <div className="min-w-0">
                                                            <div className="text-[13px] font-semibold truncate flex items-center gap-1.5">
                                                                <span className="truncate">{c.name}</span>
                                                                {c.allow_as_assistant_user && <span className="w-[7px] h-[7px] rounded-full bg-green-500 shrink-0" title={tc('canReachAgent')} />}
                                                            </div>
                                                            <div className="text-[11.5px] text-gray-600 truncate flex items-center gap-1.5 mt-0.5">
                                                                {status && <span className={cn('px-1.5 rounded text-[10.5px] whitespace-nowrap', STATUS_PILL[status] || PILL_DEFAULT)}>{statusLabel(status)}</span>}
                                                                <span className="truncate">
                                                                    {c.company || (seen ? tc('lastContactVia', { channel: channelLabel(seen.channel), when: fmtWhen(seen.ts) }) : (status ? '' : tc('statusNone')))}
                                                                </span>
                                                            </div>
                                                        </div>
                                                        <div className="flex flex-col items-end gap-1 text-[11px] text-gray-500 shrink-0">
                                                            <span>{seen ? fmtWhen(seen.ts) : ''}</span>
                                                            <span className="flex gap-1">{channelTypes(c).map(t => <ChannelIcon key={t} type={t} className="w-3 h-3" />)}</span>
                                                        </div>
                                                    </div>
                                                </li>
                                            );
                                        })}
                                    </ul>
                                )}
                            </div>
                        </div>

                        {/* detail */}
                        <div className="flex-1 flex flex-col min-w-0 bg-gray-50 max-md:min-h-0">
                            {selectedContact ? renderDetail(selectedContact) : (
                                <div className="flex-1 flex items-center justify-center p-8 text-center text-gray-500 max-md:p-4">
                                    <div>
                                        <Users className="w-12 h-12 mx-auto text-gray-400 mb-3" />
                                        <p className="font-medium text-gray-600">{tc('emptyTitle')}</p>
                                        <p className="text-sm mt-1">{tc('emptyBody')}</p>
                                    </div>
                                </div>
                            )}
                        </div>
                    </div>
                    <datalist id="contact-status-values">
                        {allStatusValues.map(s => <option key={s} value={s}>{statusLabel(s)}</option>)}
                    </datalist>
                    <datalist id="contact-tag-values">
                        {tagValues.map(t => <option key={t} value={t} />)}
                    </datalist>
                </div>
            </div>

            {/* edit form */}
            {showFormModal && form && (
                <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 backdrop-blur-sm p-4 max-md:p-0">
                    <div className="bg-white rounded-2xl shadow-2xl w-full max-w-2xl mx-4 overflow-hidden border border-gray-200 max-h-[90vh] flex flex-col max-md:max-w-none max-md:h-[100dvh] max-md:max-h-none max-md:mx-0 max-md:rounded-none max-md:border-0 max-md:min-h-0">
                        <div className="flex items-center justify-between p-6 border-b border-gray-200 bg-gray-50 shrink-0 max-md:p-4">
                            <div className="flex items-center gap-3 max-md:gap-3 min-w-0">
                                <div className="w-10 h-10 rounded-xl bg-gray-600 flex items-center justify-center text-white shrink-0 max-md:w-10 max-md:h-10 max-md:rounded-xl max-md:shadow-none">
                                    <Users className="w-5 h-5 max-md:w-5 max-md:h-5" />
                                </div>
                                <div className="min-w-0">
                                    <h2 className="text-xl font-bold text-gray-900 max-md:text-lg truncate">{modalContact?.id ? tc('editContact') : tc('addContact')}</h2>
                                    <p className="text-sm text-gray-500 max-md:text-xs truncate">{tc('formSubtitle')}</p>
                                </div>
                            </div>
                            <button type="button" onClick={closeForm} title={tcm('close')} className="p-2 hover:bg-gray-200 rounded-lg transition-colors">
                                <X className="w-5 h-5 text-gray-500" />
                            </button>
                        </div>

                        <div className="p-6 overflow-y-auto space-y-6 max-md:p-4">
                            <div>
                                <label className="block text-sm font-medium text-gray-700 mb-1">{tc('name')}<span className="text-red-600"> *</span></label>
                                <input type="text" value={form.name} onChange={e => setForm(f => f && ({ ...f, name: e.target.value }))} className={FIELD} placeholder={tc('namePlaceholder')} />
                            </div>

                            <div>
                                <h3 className="text-lg font-semibold text-gray-900 mb-2">{tc('channels')}</h3>
                                <p className="text-sm text-gray-500 mb-3">{tc('channelsHint')}</p>
                                <div className="space-y-2">
                                    {form.channels.map((ch, i) => (
                                        <div key={i} className="flex gap-2 items-center">
                                            <select value={ch.type}
                                                onChange={e => setForm(f => {
                                                    if (!f) return f;
                                                    const chs = [...f.channels];
                                                    chs[i] = { ...chs[i], type: e.target.value };
                                                    return { ...f, channels: chs };
                                                })}
                                                className="w-44 shrink-0 px-3 py-2.5 rounded-xl bg-white border border-gray-300 text-gray-900 focus:outline-none focus:ring-2 focus:ring-gray-400 text-sm">
                                                {CHANNEL_TYPES.map(t => <option key={t} value={t}>{channelLabel(t)}</option>)}
                                            </select>
                                            <input type="text" value={ch.value}
                                                onChange={e => setForm(f => {
                                                    if (!f) return f;
                                                    const chs = [...f.channels];
                                                    chs[i] = { ...chs[i], value: e.target.value };
                                                    return { ...f, channels: chs };
                                                })}
                                                placeholder={channelPlaceholder(ch.type)}
                                                className={cn(FIELD, 'flex-1 min-w-0 py-2.5 text-sm')} />
                                            <button type="button" onClick={() => setForm(f => f && ({ ...f, channels: f.channels.filter((_, j) => j !== i) }))}
                                                className="p-2 shrink-0 hover:bg-red-50 rounded-lg text-gray-400 hover:text-red-500 transition-colors" title={tc('remove')}>
                                                <Trash2 className="w-4 h-4" />
                                            </button>
                                        </div>
                                    ))}
                                </div>
                                <button type="button" onClick={() => setForm(f => f && ({ ...f, channels: [...f.channels, { type: 'whatsapp', value: '' }] }))}
                                    className="mt-2 flex items-center gap-2 text-sm font-medium text-gray-600 hover:text-gray-900">
                                    <Plus className="w-4 h-4" />{tc('addChannel')}
                                </button>
                            </div>

                            <div className="grid grid-cols-2 gap-3 max-md:grid-cols-1">
                                <div>
                                    <label className="block text-sm font-medium text-gray-700 mb-1">{tc('company')}</label>
                                    <input type="text" value={form.company} onChange={e => setForm(f => f && ({ ...f, company: e.target.value }))} className={FIELD} placeholder={tc('companyPlaceholder')} />
                                </div>
                                <div>
                                    <label className="block text-sm font-medium text-gray-700 mb-1">{tc('role')}</label>
                                    <input type="text" value={form.role} onChange={e => setForm(f => f && ({ ...f, role: e.target.value }))} className={FIELD} placeholder={tc('rolePlaceholder')} />
                                </div>
                                <div className="col-span-2 max-md:col-span-1">
                                    <label className="block text-sm font-medium text-gray-700 mb-1">{tc('tags')}</label>
                                    <input type="text" value={form.tagsText} onChange={e => setForm(f => f && ({ ...f, tagsText: e.target.value }))} className={FIELD} placeholder={tc('tagsPlaceholder')} list="contact-tag-values" />
                                </div>
                            </div>

                            <div>
                                <h3 className="text-lg font-semibold text-gray-900 mb-2">{tc('file')}</h3>
                                <p className="text-sm text-gray-500 mb-3">{tc('fileHint')}</p>
                                <div className="space-y-3">
                                    <div>
                                        <label className="block text-sm font-medium text-gray-700 mb-1">{tc('language')}</label>
                                        <input type="text" value={form.preferred_language} onChange={e => setForm(f => f && ({ ...f, preferred_language: e.target.value }))} className={FIELD} placeholder={tc('languagePlaceholder')} />
                                    </div>
                                    <div>
                                        <label className="block text-sm font-medium text-gray-700 mb-1">{tc('howToAddress')}</label>
                                        <input type="text" value={form.how_to_address} onChange={e => setForm(f => f && ({ ...f, how_to_address: e.target.value }))} className={FIELD} placeholder={tc('howToAddressPlaceholder')} />
                                    </div>
                                    <div>
                                        <label className="block text-sm font-medium text-gray-700 mb-1">{tc('birthday')}</label>
                                        <input type="text" value={form.birthday} onChange={e => setForm(f => f && ({ ...f, birthday: e.target.value }))} className={FIELD} placeholder={tc('birthdayPlaceholder')} />
                                    </div>
                                    <div>
                                        <label className="block text-sm font-medium text-gray-700 mb-1">{tc('freeNotes')}</label>
                                        <textarea value={form.notes} onChange={e => setForm(f => f && ({ ...f, notes: e.target.value }))} rows={3} className={cn(FIELD, 'resize-y')} placeholder={tc('freeNotesPlaceholder')} />
                                    </div>
                                </div>
                            </div>
                        </div>

                        <div className="flex items-center justify-between p-6 border-t border-gray-200 bg-gray-50 shrink-0 max-md:p-4">
                            <button type="button" onClick={closeForm} className="text-gray-600 hover:bg-gray-200 px-4 py-2 rounded-lg transition-colors">{tcm('cancel')}</button>
                            <button type="button" onClick={handleSave} disabled={saving || !form.name.trim()}
                                className={cn('px-6 py-2 rounded-lg font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed', PRIMARY)}>
                                {saving ? tcm('saving') : tcm('save')}
                            </button>
                        </div>
                    </div>
                </div>
            )}

            <ConfirmDialog
                open={confirm !== null}
                title={confirmTitle}
                body={confirmBody}
                confirmLabel={confirmYes}
                cancelLabel={confirmNo}
                onConfirm={runConfirm}
                onCancel={() => setConfirm(null)}
                zIndexClass="z-[70]"
                escapeLevel={70}
            />
        </>
    );
}
