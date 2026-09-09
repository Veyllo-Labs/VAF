'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
//
// The inbox: one window in the sidebar footer for every conversation across every
// channel (WhatsApp, Telegram, Discord, mail, agent rooms), newest first, with the same
// four states the channel windows and the agent's `inbox` tool show, because all of
// them read the rows vaf/core/inbox.py builds (docs/integrations/INBOX.md). A rail with
// the views, the channels and the three toggles, the list, and a preview with the
// conversation and the actions. No input field and no done or read button: the inbox is
// the place to read and to decide. Opening a row reads it, and a read row no longer waits
// for you (the person read it and decides for themselves whether to answer), so the
// opened row outlives the list it may leave. Writing happens in the channel window with
// the Composer beside it, and "write a draft" opens that window on the chat and starts
// the Composer. It refetches on the
// `inbox_changed` signal, never on a timer. The bubbles, the history hook and the chips
// come from the channel shell; nothing here is a fourth copy. Escape: 65 closes the
// window, 66 clears a running search first, 67 steps back from the preview on a phone.

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import { X, Search, RefreshCw, Inbox, ArrowLeft, Sparkles, ExternalLink, CheckCheck } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useEscapeLayer } from '@/hooks/useEscapeLayer';
import {
    BTN, BTN_PRIMARY, INPUT, ConversationBubbles, StateChips, WaitsChip, fmtWhen, initials,
    useConversationHistory, type InboxChannel,
} from '@/components/connections/ChannelDashboardShell';

const api = (path: string) => path.startsWith('/') ? path : `/${path}`;

export interface InboxRow {
    key: string;
    channel: InboxChannel;
    id: string;
    name: string;
    preview: string;
    preview_from: string;
    last_ts: number;
    message_count: number;
    unread: number;
    waits: boolean;
    waits_reason: string;
    answered_by_agent: boolean;
    done: boolean;
    is_group: boolean;
    mode: string;
    reply_window_until: number | null;
    can_compose: boolean;
    session_id: string;
    subject?: string;
    members?: number;
    invited?: boolean;
    jump?: Record<string, unknown>;
}

interface Counts {
    all: number; waits: number; unread: number; agent: number;
    per_channel: Record<string, number>; waits_per_channel: Record<string, number>;
    unread_per_channel?: Record<string, number>; invitations?: number;
}

interface Status {
    whatsapp?: { linked: boolean; running: boolean };
    telegram?: { configured: boolean; running: boolean };
    discord?: { configured: boolean; running: boolean };
    mail?: { accounts: number; last_sync_at: string | null };
}

/** Where "open in the channel window" lands: a chat in a channel window, or a mail thread. */
export type InboxJump =
    | { channel: 'whatsapp' | 'telegram' | 'discord'; chatId: string; draft?: boolean }
    | { channel: 'mail'; threadId: number; draft?: boolean };

export interface InboxWindowProps {
    isOpen: boolean;
    onClose: () => void;
    /** Bumped by the page on every `inbox_changed` and `rooms_changed` frame; the window refetches, debounced. */
    version: number;
    onOpenInChannel: (jump: InboxJump) => void;
    onOpenRoom: (roomId: string, name: string) => void;
}

const CHANNELS: InboxChannel[] = ['whatsapp', 'telegram', 'discord', 'mail', 'room'];
const CHANNEL_SQUARE: Record<InboxChannel, string> = {
    whatsapp: 'bg-[#25a244]', telegram: 'bg-[#2aabee]', discord: 'bg-[#5865f2]', mail: 'bg-[#e0a03c]', room: 'bg-[#a78bfa]',
};
const VIEWS = ['all', 'waits', 'unread', 'agent'] as const;
type View = typeof VIEWS[number];
const MODE_CHIP = 'text-[11px] px-1.5 rounded-md bg-[#262626] text-[#9a9a9a] border border-[#2e2e2e] whitespace-nowrap';
const RAIL_BTN = 'mx-2 px-3 py-1.5 rounded-lg flex items-center justify-between gap-2 text-left max-md:mx-0 max-md:px-2 max-md:py-1 max-md:text-xs max-md:border max-md:border-[#2e2e2e] max-md:shrink-0 max-md:whitespace-nowrap';
const RAIL_HEAD = 'px-4 pt-4 pb-1 text-[11px] uppercase tracking-wide text-[#8a8a8a] max-md:hidden';

function keyParts(key: string): { channel: string; id: string } {
    const at = key.indexOf(':');
    return at < 0 ? { channel: key, id: '' } : { channel: key.slice(0, at), id: key.slice(at + 1) };
}

/** One switch row in the rail: the dark theme's toggle, a light track with a dark knob when on. */
function Toggle({ on, onChange, label }: { on: boolean; onChange: (v: boolean) => void; label: string }) {
    return (
        <button type="button" onClick={() => onChange(!on)} className="mx-4 py-1 flex items-center gap-2 text-[#c8c8c8] text-left max-md:mx-0 max-md:px-2 max-md:text-xs max-md:shrink-0 max-md:whitespace-nowrap">
            {/* The dark theme's toggle: a light track with a dark knob when on, a dark track with a light knob when off. */}
            <span className={cn('w-8 h-4 rounded-full relative shrink-0', on ? 'bg-[#d9d9d9]' : 'bg-[#333333]')}>
                <span className={cn('absolute top-0.5 w-3 h-3 rounded-full', on ? 'right-0.5 bg-[#1a1a1a]' : 'left-0.5 bg-[#e8e8e8]')} />
            </span>
            <span className="truncate">{label}</span>
        </button>
    );
}

export default function InboxWindow({ isOpen, onClose, version, onOpenInChannel, onOpenRoom }: InboxWindowProps) {
    const t = useTranslations('inbox');
    const [view, setView] = useState<View>('all');
    const [channel, setChannel] = useState<InboxChannel | null>(null);
    const [groups, setGroups] = useState(true);
    const [done, setDone] = useState(false);
    // Bulk mail (promotions, social, newsletters, notifications, junk) stays out of the
    // list unless asked: the inbox is for the mail a person answers.
    const [bulk, setBulk] = useState(false);
    const [queryInput, setQueryInput] = useState('');
    const [query, setQuery] = useState('');
    const [rows, setRows] = useState<InboxRow[]>([]);
    const [counts, setCounts] = useState<Counts | null>(null);
    const [status, setStatus] = useState<Status | null>(null);
    const [loading, setLoading] = useState(false);
    const [loadFailed, setLoadFailed] = useState(false);
    const [selectedKey, setSelectedKey] = useState<string | null>(null);
    const [mobilePane, setMobilePane] = useState<'list' | 'preview'>('list');

    // The search field debounces into the server query: the rows are the server's.
    useEffect(() => {
        const id = setTimeout(() => setQuery(queryInput.trim()), 300);
        return () => clearTimeout(id);
    }, [queryInput]);

    // Two requests per load: the rows of the selected view and channel, and the counts of
    // the WHOLE inbox under the same toggles. The narrowed result's own counts describe only
    // the selected channel, and a rail that reads them shows WhatsApp at 0 the moment
    // Telegram is selected.
    // A response that is not the newest request's is dropped: a channel switch and the
    // signal's debounce can leave two requests in flight, and the older answer must not
    // land on top of the newer list.
    const loadRequest = useRef(0);
    const load = useCallback(async () => {
        const requestNo = ++loadRequest.current;
        const params = new URLSearchParams({ view, groups: String(groups), done: String(done), bulk: String(bulk), limit: '200' });
        if (channel) params.set('channel', channel);
        if (query) params.set('q', query);
        const summaryParams = new URLSearchParams({ groups: String(groups), done: String(done), bulk: String(bulk) });
        setLoading(true);
        setLoadFailed(false);
        try {
            const [res, sum] = await Promise.all([
                fetch(api(`api/inbox?${params}`), { credentials: 'include' }),
                fetch(api(`api/inbox/summary?${summaryParams}`), { credentials: 'include' }).catch(() => null),
            ]);
            if (requestNo !== loadRequest.current) return;
            if (!res.ok) { setLoadFailed(true); return; }
            const json = await res.json();
            if (requestNo !== loadRequest.current) return;
            setRows(Array.isArray(json.rows) ? json.rows : []);
            setStatus(json.status ?? null);
            // The narrowed result's own counts stand in when the summary is unreachable.
            const countsJson = sum?.ok ? await sum.json() : (json.counts ?? null);
            if (requestNo !== loadRequest.current) return;
            setCounts(countsJson);
        } catch {
            setLoadFailed(true);
        } finally {
            if (requestNo === loadRequest.current) setLoading(false);
        }
    }, [view, channel, groups, done, bulk, query]);
    const loadRef = useRef(load);
    loadRef.current = load;

    useEffect(() => { if (isOpen) void load(); }, [isOpen, load]);

    // The signal, debounced: a history sync announces in bursts, and one fetch answers them.
    useEffect(() => {
        if (!isOpen || version === 0) return;
        const id = setTimeout(() => { void loadRef.current(); }, 400);
        return () => clearTimeout(id);
    }, [version, isOpen]);

    // The opened row and the reason it waited when it was opened. It outlives the list:
    // reading takes a row off "waits for you", so in that view the server's next answer no
    // longer holds it, while its conversation stays open in the preview until another row
    // is chosen (a filter change or a refetch cannot leave an empty pane either). The
    // amber sentence keeps the reason the row was opened with, so the reader still sees
    // why it was flagged; a fresh "waits" from the server replaces it, and a conversation
    // that moved on for another reason (a newer message, the agent's answer, the person's
    // own reply) drops it.
    const [opened, setOpened] = useState<{ row: InboxRow; reason: string } | null>(null);
    useEffect(() => {
        if (!isOpen) { setSelectedKey(null); setOpened(null); setMobilePane('list'); setQueryInput(''); setQuery(''); setChannel(null); setView('all'); }
    }, [isOpen]);

    const live = useMemo(() => (selectedKey ? rows.find(r => r.key === selectedKey) ?? null : null), [rows, selectedKey]);
    const selected = live ?? (opened && opened.row.key === selectedKey ? opened.row : null);
    const movedOn = !!(live && opened && (live.last_ts > opened.row.last_ts || live.answered_by_agent || live.done));
    const noteReason = live?.waits ? live.waits_reason : (opened && opened.row.key === selectedKey && !movedOn ? opened.reason : '');

    // Opening a row reads it, as in the channel windows: the seen mark goes to the store,
    // the pill and the "waits for you" chip go out at once, and they stay out while the
    // server reports what was marked; a different count, a fresh "waits" or a newer
    // message (the state carries the newest timestamp) is news. A mark the server refused is
    // remembered as refused for that state: the pill comes back once another row is chosen
    // and nothing retries until the row's state changes, so a failing server is asked once
    // per state. A room row's seen moves
    // the person's cursor as the room view does: reading the room here is reading it. An
    // invitation is read by answering it, so its row posts nothing and keeps its chip.
    const [marked, setMarked] = useState<Map<string, string>>(() => new Map());
    useEffect(() => { if (!isOpen) setMarked(new Map()); }, [isOpen]);
    const stateOf = (r: InboxRow) => `${r.unread}:${r.waits ? 1 : 0}:${r.last_ts}`;
    const selectedState = live ? stateOf(live) : '';
    const selectedNeedsMark = !!live && live.waits_reason !== 'invitation' && (live.unread > 0 || live.waits);
    useEffect(() => {
        if (!isOpen || !live || !selectedNeedsMark) return;
        const prior = marked.get(live.key);
        if (prior === selectedState || prior === `refused:${selectedState}`) return;
        const key = live.key;
        const state = selectedState;
        setMarked(prev => new Map(prev).set(key, state));
        const refused = () => setMarked(prev => (prev.get(key) === state ? new Map(prev).set(key, `refused:${state}`) : prev));
        fetch(api('api/inbox/marks'), {
            method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ channel: live.channel, id: live.id, seen: true }),
        }).then(res => { if (!res.ok) refused(); }).catch(refused);
    }, [isOpen, live, selectedState, selectedNeedsMark, marked]);
    const readOf = (r: InboxRow) => r.waits_reason !== 'invitation' && (r.key === selectedKey || marked.get(r.key) === stateOf(r));
    const unreadOf = (r: InboxRow) => readOf(r) ? 0 : r.unread;
    const waitsOf = (r: InboxRow) => !readOf(r) && r.waits;

    const { sessionHistory, historyLoading } = useConversationHistory(
        selected ? selected.key : null, isOpen,
        (key) => { const { channel: ch, id } = keyParts(key); return `api/inbox/history?channel=${encodeURIComponent(ch)}&id=${encodeURIComponent(id)}&limit=200`; },
        version,
    );
    const bubbles = useMemo(
        () => sessionHistory.filter(m => m.role === 'user' || m.role === 'assistant').map(m => ({ role: m.role, text: m.content || '', timestamp: m.timestamp })),
        [sessionHistory],
    );
    const bubblesRef = useRef<HTMLDivElement | null>(null);
    useEffect(() => { const el = bubblesRef.current; if (el) el.scrollTop = el.scrollHeight; }, [bubbles, selectedKey]);

    // Escape, one layer at a time: a running search clears first (66), then the phone's
    // preview steps back to the list (67 sits above 66 so a search under a preview is
    // untouched by the step back), then the window closes (65).
    useEscapeLayer({ active: isOpen && mobilePane === 'preview' && typeof window !== 'undefined' && window.innerWidth < 768, level: 67, onEscape: () => setMobilePane('list') });
    useEscapeLayer({ active: isOpen && queryInput !== '', level: 66, onEscape: () => { setQueryInput(''); setQuery(''); } });
    useEscapeLayer({ active: isOpen && queryInput === '' && !(mobilePane === 'preview' && typeof window !== 'undefined' && window.innerWidth < 768), level: 65, onEscape: onClose });

    const select = (r: InboxRow) => {
        setSelectedKey(r.key);
        setOpened({ row: r, reason: r.waits ? r.waits_reason : '' });
        setMobilePane('preview');
    };

    // "Mark all as read": every conversation of the selected channel (or of all of them),
    // groups and rooms as the toggle says, is read at once, server-side, so every surface
    // drops its pills and chips; the view and the search do not narrow it. The list reloads
    // straight away rather than waiting for the signal, and the button stays disabled until
    // that reload landed. It is offered only while the selection holds something a read can
    // clear: an invitation waits for a decision, not for reading.
    const [markingAll, setMarkingAll] = useState(false);
    const markAllRead = async () => {
        if (markingAll) return;
        setMarkingAll(true);
        try {
            await fetch(api('api/inbox/marks/all'), {
                method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ channels: channel ? [channel] : 'all', groups, bulk }),
            });
        } catch { /* the list reloads either way and shows what the server says */ }
        await load();
        setMarkingAll(false);
    };
    const anythingToRead = (() => {
        if (!counts) return false;
        const invitations = counts.invitations ?? 0;
        if (channel) {
            const waits = (counts.waits_per_channel?.[channel] ?? 0) - (channel === 'room' ? invitations : 0);
            return (counts.unread_per_channel?.[channel] ?? 0) > 0 || waits > 0;
        }
        return counts.unread > 0 || counts.waits - invitations > 0;
    })();

    const openElsewhere = (r: InboxRow, draft: boolean) => {
        if (r.channel === 'room') { onClose(); onOpenRoom(r.id, r.name); return; }
        onClose();
        if (r.channel === 'mail') onOpenInChannel({ channel: 'mail', threadId: Number(r.id), draft });
        else onOpenInChannel({ channel: r.channel, chatId: r.id, draft });
    };

    const previewLine = (r: InboxRow) => {
        const text = (r.preview || '').replace(/\s+/g, ' ').trim();
        if (!text) return '';
        if (r.preview_from === 'agent') return t('from.agent', { text });
        if (r.preview_from === 'you') return t('from.you', { text });
        if (r.preview_from && r.preview_from !== 'them') return t('from.other', { who: r.preview_from, text });
        return text;
    };

    const statusLine = (ch: Exclude<InboxChannel, 'room'>) => {
        if (!status) return null;
        if (ch === 'mail') {
            const n = status.mail?.accounts ?? 0;
            return { on: n > 0, text: t('status.mail', { count: n }) };
        }
        const s = status[ch];
        const on = ch === 'whatsapp' ? !!(s && 'linked' in s && s.linked && s.running) : !!(s && s.running);
        return { on, text: on ? t('status.on', { channel: t(`channel.${ch}`) }) : t('status.off', { channel: t(`channel.${ch}`) }) };
    };

    if (!isOpen) return null;

    const viewCount = (v: View) => !counts ? 0 : v === 'all' ? counts.all : v === 'waits' ? counts.waits : v === 'unread' ? counts.unread : counts.agent;
    const canDraft = (r: InboxRow) => (r.channel === 'whatsapp' && r.can_compose) || r.channel === 'mail';

    return (
        <div className="fixed inset-0 z-[65] flex items-center justify-center p-4 bg-black/50 max-md:p-0" onClick={onClose}>
            <div
                className="relative bg-[#181818] text-[#e8e8e8] w-full max-w-[95vw] h-[90vh] rounded-2xl shadow-2xl border border-[#2e2e2e] flex flex-col overflow-hidden max-md:max-w-none max-md:h-[100dvh] max-md:rounded-none max-md:border-0"
                onClick={e => e.stopPropagation()}
            >
                <header className="flex items-center gap-3 px-4 py-2.5 border-b border-[#2e2e2e] bg-[#1f1f1f] shrink-0">
                    <div className="w-8 h-8 rounded-lg grid place-items-center shrink-0 bg-[#e6e6e6] text-[#181818]"><Inbox className="w-4 h-4" /></div>
                    <h1 className="font-semibold text-[15px]">{t('title')}</h1>
                    <span className="text-[13px] text-[#9a9a9a] flex items-center gap-2 min-w-0 max-md:hidden">
                        <span className={cn('w-2 h-2 rounded-full shrink-0', counts && counts.waits > 0 ? 'bg-[#e0b866]' : 'bg-[#3fbf5f]')} />
                        <span className="truncate">{t('subtitle', { waits: counts?.waits ?? 0, unread: counts?.unread ?? 0 })}</span>
                    </span>
                    <button type="button" onClick={onClose} title={t('close')} className="ml-auto p-2 rounded-lg hover:bg-[#262626] text-[#9a9a9a] hover:text-white">
                        <X className="w-4 h-4" />
                    </button>
                </header>

                {/* The columns are a class, not an inline style: an inline style outranks the max-md
                    class and the phone would keep the three columns. */}
                <main className="flex-1 grid min-h-0 grid-cols-[210px_380px_1fr] max-md:grid-cols-1 max-md:grid-rows-[auto_1fr]">
                    {/* On a phone the rail is one row that scrolls sideways: a wrapping strip ate half the screen. */}
                    <nav className="border-r border-[#2e2e2e] bg-[#1a1a1a] flex flex-col text-sm overflow-y-auto max-md:flex-row max-md:flex-nowrap max-md:overflow-x-auto max-md:overflow-y-visible max-md:gap-1 max-md:p-2 max-md:border-r-0 max-md:border-b">
                        <div className={RAIL_HEAD}>{t('rail.view')}</div>
                        {VIEWS.map(v => (
                            <button key={v} type="button" onClick={() => setView(v)} className={cn(RAIL_BTN, view === v ? 'bg-[#2a2a2a]' : 'hover:bg-[#262626]')}>
                                <span className="flex items-center gap-2 min-w-0">
                                    {v === 'waits' && <span className="w-1.5 h-1.5 rounded-full bg-[#e0b866] shrink-0" />}
                                    <span className="truncate">{t(`view.${v}`)}</span>
                                </span>
                                <span className={cn('text-xs', v === 'waits' && viewCount(v) > 0 ? 'text-[#e0b866] font-medium' : 'text-[#9a9a9a]')}>{viewCount(v)}</span>
                            </button>
                        ))}
                        <div className={RAIL_HEAD}>{t('rail.channels')}</div>
                        {/* "All channels" is its own entry: the view's "All" above is a view, and a person who
                            narrowed the list to one channel looks here to widen it again. */}
                        <button type="button" onClick={() => setChannel(null)} className={cn(RAIL_BTN, channel === null ? 'bg-[#2a2a2a]' : 'hover:bg-[#262626]')}>
                            <span className="flex items-center gap-2 min-w-0"><span className="w-2 h-2 rounded-full shrink-0 bg-[#e6e6e6]" /><span className="truncate">{t('allChannels')}</span></span>
                            <span className={cn('text-xs', (counts?.waits ?? 0) > 0 ? 'text-[#e0b866] font-medium' : 'text-[#9a9a9a]')}>{counts?.all ?? 0}</span>
                        </button>
                        {CHANNELS.map(c => (
                            <button key={c} type="button" onClick={() => setChannel(prev => prev === c ? null : c)} className={cn(RAIL_BTN, channel === c ? 'bg-[#2a2a2a]' : 'hover:bg-[#262626]')}>
                                <span className="flex items-center gap-2 min-w-0"><span className={cn('w-2 h-2 rounded-sm shrink-0', CHANNEL_SQUARE[c])} /><span className="truncate">{t(`channel.${c}`)}</span></span>
                                <span className={cn('text-xs', (counts?.waits_per_channel?.[c] ?? 0) > 0 ? 'text-[#e0b866] font-medium' : 'text-[#9a9a9a]')}>{counts?.per_channel?.[c] ?? 0}</span>
                            </button>
                        ))}
                        <div className={RAIL_HEAD}>{t('rail.filters')}</div>
                        <Toggle on={groups} onChange={setGroups} label={t('showGroups')} />
                        <Toggle on={done} onChange={setDone} label={t('showDone')} />
                        <Toggle on={bulk} onChange={setBulk} label={t('showBulk')} />
                        <div className="mt-auto px-4 py-3 border-t border-[#2e2e2e] text-xs text-[#9a9a9a] space-y-1 max-md:hidden">
                            {(['whatsapp', 'telegram', 'discord', 'mail'] as const).map(ch => {
                                const line = statusLine(ch);
                                if (!line) return null;
                                return (
                                    <div key={ch} className="flex items-center gap-2">
                                        <span className={cn('w-2 h-2 rounded-full shrink-0', line.on ? 'bg-[#3fbf5f]' : 'bg-[#555]')} />
                                        <span className="truncate">{line.text}</span>
                                    </div>
                                );
                            })}
                        </div>
                    </nav>

                    <section className={cn('border-r border-[#2e2e2e] bg-[#1f1f1f] flex flex-col min-h-0 max-md:border-r-0', mobilePane === 'preview' && 'max-md:hidden')}>
                        <div className="sticky top-0 z-10 bg-[#1f1f1f] border-b border-[#2e2e2e] shrink-0">
                            {/* The list's own actions sit over the list they act on, in one row that shares
                                the search field's edges: the refresh as a symbol on the left, "mark all as
                                read" on the right. Nothing up here has to line up with the preview's buttons. */}
                            <div className="flex items-center gap-2 px-3 pt-3">
                                <button type="button" onClick={() => { void load(); }} disabled={loading} title={t('refresh')} className={cn('flex items-center', BTN)}>
                                    <RefreshCw className={cn('w-4 h-4', loading && 'animate-spin')} />
                                </button>
                                <button type="button" onClick={() => { void markAllRead(); }} disabled={markingAll || !anythingToRead} title={t('markAllReadHint')} className={cn('ml-auto flex items-center gap-1.5', BTN)}>
                                    <CheckCheck className="w-4 h-4" />{t('markAllRead')}
                                </button>
                            </div>
                            <div className="relative px-3 pt-2 pb-2">
                                <Search className="w-4 h-4 absolute left-6 top-1/2 -translate-y-1/2 text-[#9a9a9a] pointer-events-none" />
                                <input value={queryInput} onChange={e => setQueryInput(e.target.value)} placeholder={t('search')} className={cn(INPUT, 'w-full pl-9')} />
                            </div>
                            <div className="px-4 pb-2 text-xs text-[#9a9a9a] flex items-center justify-between gap-2">
                                <span>{t('listHeader', { count: rows.length })}</span>
                                <span>{t('newestFirst')}</span>
                            </div>
                        </div>
                        <div className="flex-1 overflow-y-auto min-h-0">
                            {loading && rows.length === 0 ? (
                                <div className="p-4 text-sm text-[#9a9a9a]">{t('loading')}</div>
                            ) : loadFailed ? (
                                <div className="p-4 text-sm text-[#e08c8c]">{t('couldNotLoad')}</div>
                            ) : rows.length === 0 ? (
                                <div className="p-4 text-sm text-[#9a9a9a]">{t('empty')}</div>
                            ) : rows.map(r => (
                                <button key={r.key} type="button" onClick={() => select(r)}
                                    className={cn('w-full text-left px-4 py-2.5 border-b border-[#2e2e2e]', selectedKey === r.key ? 'bg-[#2a2a2a]' : 'hover:bg-[#262626]')}>
                                    <div className="flex items-center gap-3">
                                        <div className="relative shrink-0">
                                            <div className="w-9 h-9 rounded-full bg-[#2e2e2e] grid place-items-center text-[#c8c8c8] text-xs font-medium">{initials(r.name || r.id)}</div>
                                            <span className={cn('absolute -right-0.5 -bottom-0.5 w-3.5 h-3.5 rounded-sm border-2 border-[#1f1f1f]', CHANNEL_SQUARE[r.channel])} />
                                        </div>
                                        <div className="min-w-0 flex-1">
                                            <div className="flex items-center gap-2 text-[13px]">
                                                <span className={cn('truncate', unreadOf(r) > 0 ? 'font-semibold text-white' : 'font-medium')}>{r.name || r.id}</span>
                                                {r.is_group && <span className="text-[10px] text-[#9a9a9a] shrink-0">{t(r.channel === 'room' ? 'kind.room' : 'kind.group')}</span>}
                                                <span className="ml-auto text-[11px] text-[#9a9a9a] shrink-0">{fmtWhen(r.last_ts)}</span>
                                            </div>
                                            <div className={cn('text-xs truncate', unreadOf(r) > 0 ? 'text-[#e8e8e8]' : 'text-[#9a9a9a]')}>{r.channel === 'mail' && r.subject ? r.subject : previewLine(r)}</div>
                                            <div className="mt-1 flex items-center gap-1.5 flex-wrap">
                                                <StateChips unread={unreadOf(r)} waits={waitsOf(r)} waitsReason={r.waits_reason} answeredByAgent={r.answered_by_agent} done={r.done} className="mt-0" />
                                                {r.channel !== 'mail' && <span className={MODE_CHIP}>{t(`modeChip.${r.mode}`)}</span>}
                                            </div>
                                        </div>
                                    </div>
                                </button>
                            ))}
                        </div>
                    </section>

                    <section className={cn('flex flex-col min-w-0 min-h-0', mobilePane === 'list' && 'max-md:hidden')}>
                        {!selected ? (
                            <div className="flex-1 grid place-items-center text-sm text-[#9a9a9a]">{t('selectRow')}</div>
                        ) : (
                            <>
                                <div className="px-5 py-3 border-b border-[#2e2e2e] flex items-center gap-3 flex-wrap shrink-0">
                                    <button type="button" onClick={() => setMobilePane('list')} title={t('back')} className="md:hidden p-1.5 rounded-md hover:bg-[#262626] text-[#9a9a9a]"><ArrowLeft className="w-4 h-4" /></button>
                                    <div className="w-9 h-9 rounded-full bg-[#2e2e2e] grid place-items-center text-[#c8c8c8] text-xs font-medium shrink-0">{initials(selected.name || selected.id)}</div>
                                    {/* The name block keeps a readable width; the actions wrap under it before they squeeze it. */}
                                    <div className="flex-1 min-w-[280px] max-md:min-w-0">
                                        <div className="flex items-center gap-2 min-w-0 flex-wrap">
                                            <span className="font-semibold truncate">{selected.name || selected.id}</span>
                                            <span className="text-[11px] px-1.5 rounded-md bg-[#262626] text-[#c8c8c8] flex items-center gap-1.5 whitespace-nowrap">
                                                <span className={cn('w-2 h-2 rounded-sm', CHANNEL_SQUARE[selected.channel])} />{t(`channel.${selected.channel}`)}
                                            </span>
                                            {waitsOf(selected) && <WaitsChip reason={selected.waits_reason} />}
                                        </div>
                                        <div className="text-xs text-[#9a9a9a] truncate">
                                            {t('subline', { mode: t(`mode.${selected.mode}`), id: selected.channel === 'mail' ? (selected.subject || selected.id) : selected.id, when: fmtWhen(selected.last_ts) })}
                                        </div>
                                    </div>
                                    <div className="flex items-center gap-2 flex-wrap">
                                        <button type="button" onClick={() => openElsewhere(selected, false)} className={cn('flex items-center gap-1.5', BTN)}>
                                            <ExternalLink className="w-3.5 h-3.5" />{selected.channel === 'room' ? t('openRoom') : t('openIn', { channel: t(`channel.${selected.channel}`) })}
                                        </button>
                                        {/* No done or read button: opening the row read it, and the reader decides. The one
                                            emphasis action takes the dark theme's light neutral, not a channel colour. */}
                                        {canDraft(selected) && (
                                            <button type="button" onClick={() => openElsewhere(selected, true)} className={cn('flex items-center gap-1.5', BTN_PRIMARY)}>
                                                <Sparkles className="w-4 h-4" />{t('writeDraft')}
                                            </button>
                                        )}
                                    </div>
                                </div>
                                <div ref={bubblesRef} className="flex-1 min-h-0 overflow-y-auto bg-[#151515] p-5 flex flex-col gap-2.5">
                                    {historyLoading && bubbles.length === 0 ? (
                                        <p className="text-sm text-[#9a9a9a]">{t('loading')}</p>
                                    ) : bubbles.length === 0 ? (
                                        <p className="text-sm text-[#9a9a9a] self-center">{t('noMessages')}</p>
                                    ) : (
                                        <ConversationBubbles messages={bubbles} iconClass={CHANNEL_SQUARE[selected.channel]} query="" currentMatch={null} mineClass="bg-[#3a3a3a]" />
                                    )}
                                    {noteReason === 'unanswered' && (
                                        <span className="self-center mt-2 text-[11px] text-[#e0b866] bg-[#2b2417] border border-[#4a3b1e] px-3 py-1 rounded-full text-center">{t('unanswered', { name: selected.name || selected.id })}</span>
                                    )}
                                    {noteReason === 'owner_asked' && (
                                        <span className="self-center mt-2 text-[11px] text-[#e0b866] bg-[#2b2417] border border-[#4a3b1e] px-3 py-1 rounded-full text-center">{t('ownerAsked')}</span>
                                    )}
                                    {noteReason === 'invitation' && (
                                        <span className="self-center mt-2 text-[11px] text-[#e0b866] bg-[#2b2417] border border-[#4a3b1e] px-3 py-1 rounded-full text-center">{t('invitation')}</span>
                                    )}
                                </div>
                                <div className="px-5 py-2 border-t border-[#2e2e2e] text-xs text-[#9a9a9a] flex justify-between gap-3 flex-wrap shrink-0">
                                    <span className="min-w-0 truncate">{t(`mode.${selected.mode}`)}</span>
                                    <span className="shrink-0">{selected.channel === 'room' ? t('members', { count: selected.members ?? 0 }) : t('messagesCount', { count: selected.message_count })}</span>
                                </div>
                            </>
                        )}
                    </section>
                </main>
            </div>
        </div>
    );
}
