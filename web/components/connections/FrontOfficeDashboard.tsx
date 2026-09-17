'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
/**
 * Front Office: how the agent answers the people the owner lets in.
 *
 * The window behind the Front Office card in Settings, Connections. Left: the owner's
 * instructions for those turns and the knowledge, documents learned into a lane of the
 * memory store that only a contact's turn reads. Right: the channels, each with its own
 * switch (PUT /api/front-office {channel, enabled}): on means everyone who writes there is
 * answered, the contacts already in the book are granted at that moment and a new sender
 * is enrolled by the bridge; one person is kept out by switching them off in the channel
 * window or the contact book. Switching a channel on asks once.
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { BookOpen, Headphones, Loader2, Mail, MessageCircle, Phone, Plus, Trash2, Users, X } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { cn } from '@/lib/utils';
import { useEscapeLayer } from '@/hooks/useEscapeLayer';
import ConfirmDialog from '@/components/ui/ConfirmDialog';

const api = (path: string) => path.startsWith('/') ? path : `/${path}`;

export interface FrontOfficeKnowledge {
    doc_tag: string;
    title: string;
    status: string;
    sections: number;
    learned_pages: number | null;
    total_pages: number | null;
    learned_at: string | null;
    batches_done: number;
    batches_total: number;
    error: string | null;
}

export interface FrontOfficeState {
    enabled: boolean;
    channels: Record<string, boolean>;
    contacts_only: Record<string, boolean>;
    channels_connected: Record<string, boolean>;
    channel_contacts: Record<string, { total: number; allowed: number }>;
    whatsapp_inbound_to_agent: boolean;
    reply_window_hours: number;
    reachable_contacts: number;
    admin: boolean;
    profile: { briefing: string; use_general_memory: boolean };
    briefing_max_chars: number;
    memory_enabled: boolean;
    knowledge: FrontOfficeKnowledge[];
    knowledge_error: string | null;
}

export interface FrontOfficeDashboardProps {
    isOpen: boolean;
    onClose: () => void;
    /** Open the contact book (the window closes first). */
    onOpenContacts?: () => void;
    /** Called after every saved change so the card in Connections shows the same state. */
    onChanged?: (state: FrontOfficeState) => void;
}

const SWITCH_TRACK = 'relative w-11 h-6 rounded-full transition-colors shrink-0';
const SWITCH_KNOB = 'absolute top-1 w-4 h-4 rounded-full bg-white shadow transition-transform';
const CARD = 'rounded-xl border border-gray-200 bg-gray-50 p-4 space-y-3';
const BTN = 'px-3 py-2 rounded-lg text-sm font-medium border border-gray-200 bg-white hover:bg-gray-100 text-gray-900 transition-colors shrink-0 disabled:opacity-50';

type FrontOfficeChannel = 'whatsapp' | 'telegram';
type Confirm = { kind: 'open'; channel: FrontOfficeChannel } | { kind: 'remove'; doc: FrontOfficeKnowledge } | null;

/** The channels the panel lists: the two with a Front Office lane, then the ones without,
 *  so the reader sees at a glance where the agent can answer for them and where not. */
const CHANNEL_ROWS: Array<{ id: string; label: string; icon: React.ElementType; color: string; frontOffice: boolean }> = [
    { id: 'whatsapp', label: 'WhatsApp', icon: Phone, color: 'bg-green-600', frontOffice: true },
    { id: 'telegram', label: 'Telegram', icon: MessageCircle, color: 'bg-sky-500', frontOffice: true },
    { id: 'discord', label: 'Discord', icon: MessageCircle, color: 'bg-indigo-600', frontOffice: false },
    { id: 'email', label: 'E-Mail', icon: Mail, color: 'bg-amber-500', frontOffice: false },
];

function Switch({ on, disabled, label, onClick }: { on: boolean; disabled?: boolean; label: string; onClick: () => void }) {
    return (
        <button
            type="button"
            role="switch"
            aria-checked={on}
            aria-label={label}
            disabled={disabled}
            onClick={onClick}
            className={cn(SWITCH_TRACK, on ? 'bg-gray-800 dark:bg-[#d9d9d9]' : 'bg-gray-300 dark:bg-[#333333]', disabled && 'opacity-60')}
        >
            <div className={cn(SWITCH_KNOB, on ? 'translate-x-6 dark:bg-[#1a1a1a]' : 'translate-x-1 dark:bg-[#e8e8e8]')} />
        </button>
    );
}

export default function FrontOfficeDashboard({ isOpen, onClose, onOpenContacts, onChanged }: FrontOfficeDashboardProps) {
    const t = useTranslations('settings.frontOffice');
    const tcm = useTranslations('common');
    const [data, setData] = useState<FrontOfficeState | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [busy, setBusy] = useState(false);
    const [confirm, setConfirm] = useState<Confirm>(null);
    const [briefing, setBriefing] = useState('');
    const [briefingSaved, setBriefingSaved] = useState(false);
    const [uploading, setUploading] = useState(false);
    const fileInput = useRef<HTMLInputElement>(null);

    // The window is a z-[60] sheet; its confirm sits at z-[70] and answers Escape first.
    useEscapeLayer({ active: isOpen && confirm === null, level: 60, onEscape: onClose });

    const apply = useCallback((next: FrontOfficeState) => {
        setData(next);
        setBriefing(next.profile?.briefing ?? '');
        onChanged?.(next);
    }, [onChanged]);

    const load = useCallback(async (quiet = false) => {
        try {
            const res = await fetch(api('api/front-office'), { credentials: 'include' });
            if (!res.ok) throw new Error(String(res.status));
            const next: FrontOfficeState = await res.json();
            setData(next);
            if (!quiet) setBriefing(next.profile?.briefing ?? '');
            setError(null);
        } catch {
            if (!quiet) setError(t('loadFailed'));
        }
    }, [t]);

    useEffect(() => {
        if (isOpen) { load(); return; }
        setConfirm(null); setError(null); setBriefingSaved(false);
    }, [isOpen, load]);

    // A learn in flight: poll the state until every document has settled.
    const learning = !!data?.knowledge.some(k => k.status === 'running');
    useEffect(() => {
        if (!isOpen || !learning) return;
        const id = setInterval(() => load(true), 3000);
        return () => clearInterval(id);
    }, [isOpen, learning, load]);

    const put = async (path: string, body: unknown, method = 'PUT') => {
        setBusy(true);
        try {
            const res = await fetch(api(path), {
                method,
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify(body),
            });
            if (!res.ok) throw new Error(String(res.status));
            setError(null);
            return await res.json();
        } catch {
            setError(t('saveFailed'));
            return null;
        } finally {
            setBusy(false);
        }
    };

    const setChannel = async (channel: FrontOfficeChannel, enabled: boolean) => {
        const next = await put('api/front-office', { channel, enabled });
        if (next) apply(next);
    };

    const saveBriefing = async () => {
        const out = await put('api/front-office/profile', { briefing });
        if (out?.profile && data) {
            const next = { ...data, profile: out.profile };
            setData(next);
            setBriefingSaved(true);
            onChanged?.(next);
            setTimeout(() => setBriefingSaved(false), 2500);
        }
    };

    const setGeneralMemory = async (on: boolean) => {
        const out = await put('api/front-office/profile', { use_general_memory: on });
        if (out?.profile && data) {
            const next = { ...data, profile: out.profile };
            setData(next);
            onChanged?.(next);
        }
    };

    // Several files at once: each one is its own upload and its own learn, one after the
    // other, so a bad file in the middle stops nothing but itself. The list below shows
    // every document with its state (learning, learned, failed) as soon as it is stored.
    const onPickFiles = async (e: React.ChangeEvent<HTMLInputElement>) => {
        const files = Array.from(e.target.files ?? []);
        e.target.value = '';
        if (files.length === 0) return;
        setUploading(true);
        try {
            for (const file of files) {
                if (!/\.(pdf|txt|md)$/i.test(file.name)) { setError(t('uploadWrongType')); continue; }
                if (file.size > 40 * 1024 * 1024) { setError(t('uploadTooLarge')); continue; }
                const buf = await file.arrayBuffer();
                let binary = '';
                const bytes = new Uint8Array(buf);
                for (let i = 0; i < bytes.length; i += 0x8000) {
                    binary += String.fromCharCode.apply(null, Array.from(bytes.subarray(i, i + 0x8000)));
                }
                const started = await put('api/front-office/knowledge', { filename: file.name, content_base64: btoa(binary) }, 'POST');
                if (started) await load(true);
            }
        } finally {
            setUploading(false);
        }
    };

    const removeDoc = async (doc: FrontOfficeKnowledge) => {
        const out = await put(`api/front-office/knowledge/${encodeURIComponent(doc.doc_tag)}`, {}, 'DELETE');
        if (out) await load(true);
    };

    if (!isOpen) return null;

    const hours = data ? Math.round(data.reply_window_hours) : 0;
    const docStatus = (k: FrontOfficeKnowledge) => {
        if (k.status === 'running') return t('learning', { done: k.batches_done, total: k.batches_total });
        if (k.status === 'complete' || k.status === 'partial') return t('learned', { sections: k.sections });
        if (k.status === 'stopped' || k.status === 'capped') return t('learnStopped');
        return t('learnFailed');
    };

    return (
        <>
            <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 backdrop-blur-sm p-4 max-md:p-0" onClick={onClose}>
                <div
                    className="bg-white rounded-2xl shadow-2xl w-full max-w-6xl mx-4 overflow-hidden border border-gray-200 max-h-[90vh] flex flex-col max-md:max-w-none max-md:h-[100dvh] max-md:max-h-none max-md:mx-0 max-md:rounded-none max-md:border-0 max-md:min-h-0"
                    onClick={e => e.stopPropagation()}
                >
                    <div className="flex items-center justify-between p-6 border-b border-gray-200 bg-gray-50 shrink-0 max-md:p-4">
                        <div className="flex items-center gap-3 min-w-0">
                            <div className="w-10 h-10 rounded-xl bg-gray-600 flex items-center justify-center text-white shrink-0">
                                <Headphones className="w-5 h-5" />
                            </div>
                            <div className="min-w-0">
                                <h2 className="text-xl font-bold text-gray-900 max-md:text-lg truncate">{t('title')}</h2>
                                <p className="text-sm text-gray-500 max-md:text-xs truncate">{t('subtitle')}</p>
                            </div>
                        </div>
                        <button type="button" onClick={onClose} title={tcm('close')} className="p-2 hover:bg-gray-200 rounded-lg transition-colors">
                            <X className="w-5 h-5 text-gray-500" />
                        </button>
                    </div>

                    <div className="p-6 overflow-y-auto max-md:p-4 max-md:flex-1">
                      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
                      <div className="space-y-6 min-w-0 md:col-span-2">
                        <div className="text-sm text-gray-600 space-y-2">
                            <p>{t('intro')}</p>
                            <p><span className="font-medium text-gray-900">{t('introActivationLabel')}{tcm('labelSeparator')}</span>{t('introActivation')}</p>
                            <p><span className="font-medium text-gray-900">{t('introExceptionsLabel')}{tcm('labelSeparator')}</span>{t('introExceptions')}</p>
                        </div>

                        {error && (
                            <div className="px-3 py-2 rounded-lg bg-red-50 border border-red-200 text-sm text-red-700">{error}</div>
                        )}

                        {/* the owner's instructions */}
                        <div className={CARD}>
                            <div>
                                <h3 className="text-sm font-semibold text-gray-900">{t('briefingTitle')}</h3>
                                <p className="text-xs text-gray-500">{t('briefingDesc')}</p>
                            </div>
                            <textarea
                                value={briefing}
                                onChange={e => { setBriefing(e.target.value.slice(0, data?.briefing_max_chars ?? 8000)); setBriefingSaved(false); }}
                                placeholder={t('briefingPlaceholder')}
                                rows={8}
                                className="w-full text-sm rounded-lg border border-gray-200 bg-white px-3 py-2 text-gray-900 focus:outline-none focus:ring-2 focus:ring-gray-200 focus:border-gray-300 vaf-scroll"
                            />
                            <div className="flex items-center justify-between gap-3">
                                <span className="text-xs text-gray-500">{briefingSaved ? t('saved') : ''}</span>
                                <button
                                    type="button"
                                    onClick={saveBriefing}
                                    disabled={busy || !data || briefing === (data.profile?.briefing ?? '')}
                                    className={BTN}
                                >
                                    {t('save')}
                                </button>
                            </div>
                        </div>

                        {/* the knowledge */}
                        <div className={CARD}>
                            <div className="flex items-center justify-between gap-4 max-md:flex-col max-md:items-start">
                                <div className="min-w-0">
                                    <h3 className="text-sm font-semibold text-gray-900">{t('knowledgeTitle')}</h3>
                                    <p className="text-xs text-gray-500">{t('knowledgeDesc')}</p>
                                </div>
                                <input ref={fileInput} type="file" accept=".pdf,.txt,.md" multiple className="hidden" onChange={onPickFiles} />
                                <button
                                    type="button"
                                    onClick={() => fileInput.current?.click()}
                                    disabled={busy || uploading || !data || !data.memory_enabled}
                                    className={cn(BTN, 'flex items-center gap-2')}
                                >
                                    {uploading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
                                    <span>{t('addDocument')}</span>
                                </button>
                            </div>
                            {data && !data.memory_enabled && (
                                <p className="text-xs text-gray-500">{t('memoryOff')}</p>
                            )}
                            {data && data.memory_enabled && data.knowledge.length === 0 && (
                                <p className="text-xs text-gray-500">{data.knowledge_error ? t('loadFailed') : t('noKnowledge')}</p>
                            )}
                            {data && data.knowledge.length > 0 && (
                                <div className="divide-y divide-gray-200 rounded-lg border border-gray-200 bg-white">
                                    {data.knowledge.map(k => (
                                        <div key={k.doc_tag} className="flex items-center justify-between gap-4 px-4 py-3">
                                            <div className="flex items-center gap-3 min-w-0">
                                                <div className="w-8 h-8 rounded-lg bg-gray-100 flex items-center justify-center text-gray-600 shrink-0">
                                                    {k.status === 'running' ? <Loader2 className="w-4 h-4 animate-spin" /> : <BookOpen className="w-4 h-4" />}
                                                </div>
                                                <div className="min-w-0">
                                                    <div className="text-sm font-medium text-gray-900 truncate">{k.title}</div>
                                                    <div className={cn('text-xs', k.status === 'failed' ? 'text-red-600' : 'text-gray-500')}>{docStatus(k)}</div>
                                                </div>
                                            </div>
                                            <button
                                                type="button"
                                                onClick={() => setConfirm({ kind: 'remove', doc: k })}
                                                disabled={busy}
                                                title={t('remove')}
                                                className="p-2 hover:bg-red-50 rounded-lg transition-colors group shrink-0"
                                            >
                                                <Trash2 className="w-4 h-4 text-gray-400 group-hover:text-red-500" />
                                            </button>
                                        </div>
                                    ))}
                                </div>
                            )}
                            <div className="flex items-center justify-between gap-4 pt-2 border-t border-gray-200">
                                <div className="min-w-0">
                                    <div className="text-sm text-gray-900">{t('generalMemoryLabel')}</div>
                                    <p className="text-xs text-gray-500">{t('generalMemoryHint')}</p>
                                </div>
                                <Switch
                                    on={!!data?.profile?.use_general_memory}
                                    disabled={busy || !data}
                                    label={t('generalMemoryLabel')}
                                    onClick={() => setGeneralMemory(!data?.profile?.use_general_memory)}
                                />
                            </div>
                        </div>

                      </div>

                      {/* the channels */}
                      <div className="space-y-3 min-w-0">
                        <div>
                            <h3 className="text-sm font-semibold text-gray-900">{t('channelsTitle')}</h3>
                            <p className="text-xs text-gray-500">{t('channelsDesc')}</p>
                        </div>
                        <div className="space-y-2">
                            {CHANNEL_ROWS.map(row => {
                                const Icon = row.icon;
                                const on = !!data?.channels[row.id];
                                const connected = !!data?.channels_connected[row.id];
                                const counts = data?.channel_contacts[row.id];
                                const note = !row.frontOffice
                                    ? t('channelNoFrontOffice')
                                    : !connected
                                        ? t('channelNotConnected')
                                        : on
                                            ? t('channelOn', { count: counts?.allowed ?? 0 })
                                            : t('channelOff', { count: counts?.total ?? 0 });
                                return (
                                    <div key={row.id} className={cn('rounded-xl border border-gray-200 bg-white p-4 flex items-start justify-between gap-4', !row.frontOffice && 'opacity-60')}>
                                        <div className="flex items-start gap-3 min-w-0">
                                            <div className={cn('w-9 h-9 rounded-lg flex items-center justify-center text-white shrink-0', row.color)}>
                                                <Icon className="w-4 h-4" />
                                            </div>
                                            <div className="min-w-0">
                                                <div className="text-sm font-medium text-gray-900">{row.label}</div>
                                                <div className="text-xs text-gray-500">{note}</div>
                                                {row.frontOffice && row.id === 'whatsapp' && connected && data && !data.whatsapp_inbound_to_agent && (
                                                    <div className="text-xs text-gray-500 mt-1">{t('inboundOff')}</div>
                                                )}
                                            </div>
                                        </div>
                                        {row.frontOffice && (
                                            <Switch
                                                on={on}
                                                disabled={busy || !data || !data.admin}
                                                label={row.label}
                                                onClick={() => on ? setChannel(row.id as FrontOfficeChannel, false) : setConfirm({ kind: 'open', channel: row.id as FrontOfficeChannel })}
                                            />
                                        )}
                                    </div>
                                );
                            })}
                        </div>
                        <div className="rounded-xl border border-gray-200 bg-gray-50 p-3 space-y-3">
                            <div className="flex items-center gap-2 min-w-0">
                                <Users className="w-4 h-4 text-gray-600 shrink-0" />
                                <p className="text-sm text-gray-700">{t('contactsCount', { count: data?.reachable_contacts ?? 0 })}</p>
                            </div>
                            {onOpenContacts && (
                                <button type="button" onClick={onOpenContacts} className={cn(BTN, 'w-full')}>{t('openContacts')}</button>
                            )}
                        </div>
                        <p className="text-xs text-gray-500 leading-relaxed">{t('optOutHint')}</p>
                        {data && (
                            <p className="text-xs text-gray-500 leading-relaxed">
                                {data.enabled
                                    ? t('onNote', { hours })
                                    : (hours > 0 ? t('offNote', { hours }) : t('offNoteClosed'))}
                            </p>
                        )}
                      </div>
                      </div>
                    </div>
                </div>
            </div>

            <ConfirmDialog
                open={confirm !== null}
                title={confirm?.kind === 'remove'
                    ? t('removeConfirmTitle')
                    : t('confirmTitle', { channel: confirm?.kind === 'open' ? (CHANNEL_ROWS.find(r => r.id === confirm.channel)?.label ?? '') : '' })}
                body={confirm?.kind === 'remove'
                    ? t('removeConfirmBody', { title: confirm.doc.title })
                    : t('confirmBody', {
                        channel: confirm?.kind === 'open' ? (CHANNEL_ROWS.find(r => r.id === confirm.channel)?.label ?? '') : '',
                        count: confirm?.kind === 'open' ? (data?.channel_contacts[confirm.channel]?.total ?? 0) : 0,
                    })}
                confirmLabel={confirm?.kind === 'remove' ? t('remove') : t('confirmYes')}
                cancelLabel={t('confirmNo')}
                onConfirm={() => {
                    const c = confirm;
                    setConfirm(null);
                    if (c?.kind === 'open') setChannel(c.channel, true);
                    if (c?.kind === 'remove') removeDoc(c.doc);
                }}
                onCancel={() => setConfirm(null)}
                zIndexClass="z-[70]"
                escapeLevel={70}
                destructive={confirm?.kind === 'remove'}
            />
        </>
    );
}
