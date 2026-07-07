'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import React, { useState, useEffect, useMemo } from 'react';
import { X, Users, Plus, Pencil, Trash2, Search } from 'lucide-react';
import { cn } from '@/lib/utils';

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
}

const CHANNEL_TYPES = [
    { id: 'phone', label: 'Phone' },
    { id: 'whatsapp', label: 'WhatsApp' },
    { id: 'email', label: 'Email' },
    { id: 'telegram', label: 'Telegram (ID or @username)' },
    { id: 'discord', label: 'Discord' },
] as const;

export interface ContactsDashboardProps {
    isOpen: boolean;
    onClose: () => void;
}

export default function ContactsDashboard({ isOpen, onClose }: ContactsDashboardProps) {
    const [contacts, setContacts] = useState<Contact[]>([]);
    const [loading, setLoading] = useState(false);
    const [showFormModal, setShowFormModal] = useState(false);
    const [modalContact, setModalContact] = useState<Contact | null>(null);
    const [form, setForm] = useState<Partial<Contact>>({});
    const [saving, setSaving] = useState(false);
    const [deleteId, setDeleteId] = useState<string | null>(null);
    const [searchQuery, setSearchQuery] = useState('');
    const [selectedContactId, setSelectedContactId] = useState<string | null>(null);

    const fetchContacts = async () => {
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
    };

    useEffect(() => {
        if (isOpen) fetchContacts();
    }, [isOpen]);

    useEffect(() => {
        if (!isOpen) return;
        const handleKeyDown = (e: KeyboardEvent) => {
            if (e.key === 'Escape') {
                if (showFormModal) {
                    setShowFormModal(false);
                    setModalContact(null);
                    setForm({});
                } else {
                    onClose();
                }
            }
        };
        window.addEventListener('keydown', handleKeyDown, true);
        return () => window.removeEventListener('keydown', handleKeyDown, true);
    }, [isOpen, onClose, showFormModal]);

    const defaultChannelRows = (): ChannelEntry[] => [
        { type: 'phone', value: '' },
        { type: 'email', value: '' },
    ];

    const channelsFromContact = (c: Contact): ChannelEntry[] => {
        if (c.channels && Array.isArray(c.channels) && c.channels.length > 0) {
            return c.channels.map(ch => ({ type: ch.type || 'whatsapp', value: ch.value || '' }));
        }
        const out: ChannelEntry[] = [];
        if (c.whatsapp_phone) out.push({ type: 'phone', value: c.whatsapp_phone });
        if (c.telegram_user_id) out.push({ type: 'telegram', value: c.telegram_user_id });
        if (c.telegram_username) out.push({ type: 'telegram', value: c.telegram_username });
        if (c.email) out.push({ type: 'email', value: c.email });
        return out.length > 0 ? out : defaultChannelRows();
    };

    const openCreate = () => {
        setSelectedContactId(null);
        setModalContact(null);
        setForm({
            name: '',
            channels: defaultChannelRows(),
            allow_as_assistant_user: false,
        });
        setShowFormModal(true);
    };

    const openEdit = (c: Contact) => {
        setModalContact(c);
        setForm({
            name: c.name,
            channels: channelsFromContact(c),
            preferred_language: c.preferred_language ?? '',
            how_to_address: c.how_to_address ?? '',
            birthday: c.birthday ?? '',
            notes: c.notes ?? '',
            allow_as_assistant_user: c.allow_as_assistant_user ?? false,
        });
        setShowFormModal(true);
    };

    const buildChannels = (): ChannelEntry[] => {
        return (form.channels || []).filter(ch => (ch.value || '').trim()).map(ch => ({ type: ch.type, value: (ch.value || '').trim() }));
    };

    const handleSave = async () => {
        const name = (form.name ?? '').trim();
        if (!name) return;
        setSaving(true);
        const channels = buildChannels();
        const body = {
            name,
            channels,
            preferred_language: (form.preferred_language ?? '').trim() || undefined,
            how_to_address: (form.how_to_address ?? '').trim() || undefined,
            birthday: (form.birthday ?? '').trim() || undefined,
            notes: (form.notes ?? '').trim() || undefined,
            allow_as_assistant_user: form.allow_as_assistant_user ?? false,
        };
        try {
            if (modalContact?.id) {
                const res = await fetch(api(`api/contacts/${encodeURIComponent(modalContact.id)}`), {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    credentials: 'include',
                    body: JSON.stringify(body),
                });
                if (!res.ok) throw new Error(await res.text());
                await fetchContacts();
                setShowFormModal(false);
                setModalContact(null);
                setForm({});
                setSelectedContactId(modalContact.id);
            } else {
                const res = await fetch(api('api/contacts'), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    credentials: 'include',
                    body: JSON.stringify(body),
                });
                if (!res.ok) throw new Error(await res.text());
                await fetchContacts();
                setShowFormModal(false);
                setModalContact(null);
                setForm({});
                const created = await res.json();
                setSelectedContactId(created?.id ?? null);
            }
        } catch (e) {
            console.error(e);
        } finally {
            setSaving(false);
        }
    };

    const handleDelete = async (id: string) => {
        if (!confirm('Delete this contact?')) return;
        setDeleteId(id);
        try {
            const res = await fetch(api(`api/contacts/${encodeURIComponent(id)}`), {
                method: 'DELETE',
                credentials: 'include',
            });
            if (res.ok) {
                await fetchContacts();
                if (modalContact?.id === id) {
                    setShowFormModal(false);
                    setModalContact(null);
                    setForm({});
                }
                if (selectedContactId === id) setSelectedContactId(null);
            }
        } catch (e) {
            console.error(e);
        } finally {
            setDeleteId(null);
        }
    };

    const channelSummary = (c: Contact) => {
        const types = new Set<string>();
        if (c.channels && Array.isArray(c.channels)) {
            c.channels.forEach(ch => ch.type && types.add(ch.type));
        }
        if (!types.size && (c.whatsapp_phone || c.telegram_username || c.telegram_user_id || c.email)) {
            if (c.whatsapp_phone) types.add('whatsapp');
            if (c.telegram_username || c.telegram_user_id) types.add('telegram');
            if (c.email) types.add('email');
        }
        if (!types.size) return 'No channels';
        return Array.from(types).map(t => t.charAt(0).toUpperCase() + t.slice(1)).join(', ') + (c.channels && c.channels.length > types.size ? ` (${c.channels.length})` : '');
    };

    const sortedAndFilteredContacts = useMemo(() => {
        const q = (searchQuery || '').trim().toLowerCase();
        let list = [...contacts];
        if (q) {
            list = list.filter(
                c =>
                    (c.name || '').toLowerCase().includes(q) ||
                    (c.email || '').toLowerCase().includes(q) ||
                    (c.whatsapp_phone || '').toLowerCase().includes(q) ||
                    (c.telegram_username || '').toLowerCase().includes(q) ||
                    (c.notes || '').toLowerCase().includes(q)
            );
        }
        list.sort((a, b) => (a.name || '').localeCompare(b.name || '', undefined, { sensitivity: 'base' }));
        return list;
    }, [contacts, searchQuery]);

    const selectedContact = selectedContactId ? contacts.find(c => c.id === selectedContactId) : null;

    if (!isOpen) return null;

    return (
        <>
            <div
                className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50 max-md:p-0"
                onClick={onClose}
            >
                <div
                    className={cn(
                        'relative bg-white w-full max-w-6xl h-[85vh] rounded-2xl shadow-2xl border border-gray-200 flex flex-col overflow-hidden max-md:max-w-none max-md:h-[100dvh] max-md:rounded-none max-md:border-0'
                    )}
                    onClick={e => e.stopPropagation()}
                >
                    <div className="flex items-center justify-between px-5 py-4 border-b border-gray-200 shrink-0 bg-gray-50 max-md:px-4 max-md:py-3">
                        <div className="flex items-center gap-3 max-md:gap-3 min-w-0">
                            <div className="w-10 h-10 rounded-xl bg-gray-600 flex items-center justify-center text-white shrink-0 max-md:w-10 max-md:h-10 max-md:rounded-xl max-md:shadow-none">
                                <Users className="w-5 h-5 max-md:w-5 max-md:h-5" />
                            </div>
                            <div className="min-w-0">
                                <h2 className="text-xl font-bold text-gray-900 max-md:text-lg truncate">Contacts</h2>
                                <p className="text-sm text-gray-500 max-md:text-xs truncate">Central list with personal file and assistant whitelist</p>
                            </div>
                        </div>
                        <button
                            type="button"
                            onClick={onClose}
                            className="p-2 hover:bg-gray-200 rounded-lg transition-colors"
                        >
                            <X className="w-5 h-5 text-gray-500" />
                        </button>
                    </div>

                    <div className="flex flex-1 min-h-0 max-md:flex-col">
                        {/* Left: search + Add contact + contact list */}
                        <div className="w-72 shrink-0 border-r border-gray-200 flex flex-col bg-gray-50/80 max-md:w-full max-md:max-h-[38vh] max-md:border-r-0 max-md:border-b">
                            <div className="p-3 space-y-2 shrink-0">
                                <div className="relative">
                                    <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
                                    <input
                                        type="text"
                                        value={searchQuery}
                                        onChange={e => setSearchQuery(e.target.value)}
                                        placeholder="Search contacts"
                                        className="w-full pl-9 pr-3 py-2.5 rounded-xl bg-white border border-gray-200 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-transparent text-sm"
                                    />
                                </div>
                                <button
                                    type="button"
                                    onClick={openCreate}
                                    className="w-full flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl bg-gray-900 hover:bg-gray-800 text-white font-medium transition-colors text-sm dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-[#f5f5f5] dark:shadow-none"
                                >
                                    <Plus className="w-4 h-4" />
                                    Add contact
                                </button>
                            </div>
                            <div className="flex-1 overflow-y-auto min-h-0">
                                {loading ? (
                                    <p className="p-3 text-sm text-gray-500">Loading…</p>
                                ) : sortedAndFilteredContacts.length === 0 ? (
                                    <div className="p-3 text-sm text-gray-500">
                                        {contacts.length === 0
                                            ? 'No contacts yet. Add a contact above.'
                                            : 'No contacts match your search.'}
                                    </div>
                                ) : (
                                    <ul className="py-1">
                                        {sortedAndFilteredContacts.map(c => (
                                            <li key={c.id}>
                                                <button
                                                    type="button"
                                                    onClick={() => setSelectedContactId(c.id)}
                                                    className={cn(
                                                        'w-full text-left px-3 py-2.5 flex flex-col gap-0.5 border-l-2 transition-colors',
                                                        selectedContactId === c.id
                                                            ? 'bg-white border-gray-900 text-gray-900 shadow-sm'
                                                            : 'border-transparent hover:bg-white/70 text-gray-700'
                                                    )}
                                                >
                                                    <span className="font-medium text-sm truncate">{c.name}</span>
                                                    <span className="text-xs text-gray-500 truncate">{channelSummary(c)}</span>
                                                </button>
                                            </li>
                                        ))}
                                    </ul>
                                )}
                            </div>
                        </div>

                        {/* Right: detail view or empty state */}
                        <div className="flex-1 flex flex-col min-w-0 bg-white max-md:min-h-0">
                            {selectedContact ? (
                                <>
                                    <div className="p-4 border-b border-gray-200 flex items-center justify-between shrink-0">
                                        <div>
                                            <h3 className="text-lg font-semibold text-gray-900">{selectedContact.name}</h3>
                                            <p className="text-sm text-gray-500">{channelSummary(selectedContact)}</p>
                                        </div>
                                        <div className="flex items-center gap-2">
                                            <button
                                                type="button"
                                                onClick={() => openEdit(selectedContact)}
                                                className="flex items-center gap-2 px-3 py-2 rounded-lg bg-gray-100 hover:bg-gray-200 text-gray-700 font-medium transition-colors text-sm"
                                            >
                                                <Pencil className="w-4 h-4" />
                                                Edit
                                            </button>
                                            <button
                                                type="button"
                                                onClick={() => handleDelete(selectedContact.id)}
                                                disabled={deleteId === selectedContact.id}
                                                className="flex items-center gap-2 px-3 py-2 rounded-lg bg-red-50 hover:bg-red-100 text-red-600 font-medium transition-colors text-sm disabled:opacity-50"
                                            >
                                                <Trash2 className="w-4 h-4" />
                                                Delete
                                            </button>
                                        </div>
                                    </div>
                                    <div className="p-4 overflow-y-auto flex-1 text-sm">
                                        <div className="space-y-4">
                                            <div>
                                                <h4 className="text-xs font-medium uppercase tracking-wide text-gray-500 mb-1">Channels</h4>
                                                {(selectedContact.channels && selectedContact.channels.length > 0) ? (
                                                    <ul className="space-y-1">
                                                        {selectedContact.channels.map((ch, i) => (
                                                            <li key={i}>
                                                                <span className="text-gray-500">
                                                                    {ch.type === 'phone' ? 'Phone (used as WhatsApp)' : ch.type === 'whatsapp' ? 'WhatsApp' : ch.type.charAt(0).toUpperCase() + ch.type.slice(1)}:{' '}
                                                                </span>
                                                                <span className="text-gray-900">{ch.value}</span>
                                                            </li>
                                                        ))}
                                                    </ul>
                                                ) : (
                                                    <dl className="space-y-1">
                                                        {selectedContact.whatsapp_phone && <div><dt className="text-gray-500 inline">WhatsApp: </dt><dd className="inline text-gray-900">{selectedContact.whatsapp_phone}</dd></div>}
                                                        {(selectedContact.telegram_username || selectedContact.telegram_user_id) && <div><dt className="text-gray-500 inline">Telegram: </dt><dd className="inline text-gray-900">{selectedContact.telegram_username || selectedContact.telegram_user_id}</dd></div>}
                                                        {selectedContact.email && <div><dt className="text-gray-500 inline">Email: </dt><dd className="inline text-gray-900">{selectedContact.email}</dd></div>}
                                                        {!selectedContact.whatsapp_phone && !selectedContact.telegram_username && !selectedContact.telegram_user_id && !selectedContact.email && <p className="text-gray-500">No channels</p>}
                                                    </dl>
                                                )}
                                            </div>
                                            <div>
                                                <h4 className="text-xs font-medium uppercase tracking-wide text-gray-500 mb-1">Personal file</h4>
                                                <dl className="space-y-1">
                                                    {selectedContact.preferred_language && <div><dt className="text-gray-500 inline">Language: </dt><dd className="inline text-gray-900">{selectedContact.preferred_language}</dd></div>}
                                                    {selectedContact.how_to_address && <div><dt className="text-gray-500 inline">How to address: </dt><dd className="inline text-gray-900">{selectedContact.how_to_address}</dd></div>}
                                                    {selectedContact.birthday && <div><dt className="text-gray-500 inline">Birthday: </dt><dd className="inline text-gray-900">{selectedContact.birthday}</dd></div>}
                                                    {selectedContact.allow_as_assistant_user && (
                                                        <div><span className="text-xs px-2 py-0.5 rounded-full bg-green-100 text-green-700">Can reach your assistant</span></div>
                                                    )}
                                                    {selectedContact.notes && (
                                                        <div className="pt-1"><dt className="text-gray-500 block mb-0.5">Notes</dt><dd className="text-gray-900 whitespace-pre-wrap">{selectedContact.notes}</dd></div>
                                                    )}
                                                    {!selectedContact.preferred_language && !selectedContact.how_to_address && !selectedContact.birthday && !selectedContact.notes && !selectedContact.allow_as_assistant_user && (
                                                        <p className="text-gray-500">No personal file data</p>
                                                    )}
                                                </dl>
                                            </div>
                                        </div>
                                    </div>
                                </>
                            ) : (
                                <div className="flex-1 flex items-center justify-center p-8 text-center text-gray-500 max-md:p-4">
                                    <div>
                                        <Users className="w-12 h-12 mx-auto text-gray-300 mb-3" />
                                        <p className="font-medium text-gray-600">Select a contact or add one</p>
                                        <p className="text-sm mt-1">Choose a contact from the list on the left, or click &quot;Add contact&quot; to create a new one.</p>
                                    </div>
                                </div>
                            )}
                        </div>
                    </div>
                </div>
            </div>

            {/* Detail / Edit modal */}
            {showFormModal && (
                <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 backdrop-blur-sm p-4 max-md:p-0">
                    <div className="bg-white rounded-2xl shadow-2xl w-full max-w-2xl mx-4 overflow-hidden border border-gray-200 max-h-[90vh] flex flex-col max-md:max-w-none max-md:h-[100dvh] max-md:max-h-none max-md:mx-0 max-md:rounded-none max-md:border-0 max-md:min-h-0">
                        <div className="flex items-center justify-between p-6 border-b border-gray-200 bg-gray-50 shrink-0 max-md:p-4">
                            <div className="flex items-center gap-3 max-md:gap-3 min-w-0">
                                <div className="w-10 h-10 rounded-xl bg-gray-600 flex items-center justify-center text-white shrink-0 max-md:w-10 max-md:h-10 max-md:rounded-xl max-md:shadow-none">
                                    <Users className="w-5 h-5 max-md:w-5 max-md:h-5" />
                                </div>
                                <div className="min-w-0">
                                    <h2 className="text-xl font-bold text-gray-900 max-md:text-lg truncate">
                                        {modalContact?.id ? 'Edit contact' : 'Add contact'}
                                    </h2>
                                    <p className="text-sm text-gray-500 max-md:text-xs truncate">Channels and personal file</p>
                                </div>
                            </div>
                            <button
                                type="button"
                                onClick={() => { setShowFormModal(false); setModalContact(null); setForm({}); }}
                                className="p-2 hover:bg-gray-200 rounded-lg transition-colors"
                            >
                                <X className="w-5 h-5 text-gray-500" />
                            </button>
                        </div>

                        <div className="p-6 overflow-y-auto space-y-6 max-md:p-4">
                            <div>
                                <label className="block text-sm font-medium text-gray-700 mb-1">Name *</label>
                                <input
                                    type="text"
                                    value={form.name ?? ''}
                                    onChange={e => setForm(f => ({ ...f, name: e.target.value }))}
                                    className="w-full px-4 py-3 rounded-xl bg-white border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-transparent"
                                    placeholder="e.g. Max"
                                />
                            </div>

                            <div>
                                <h3 className="text-lg font-semibold text-gray-900 mb-2">Channels</h3>
                                <p className="text-sm text-gray-500 mb-3">Phone is automatically used as WhatsApp when the contact can reach your assistant. Add more channels with the button below.</p>
                                <div className="space-y-2">
                                    {(form.channels || []).map((ch, i) => (
                                        <div key={i} className="flex gap-2 items-center">
                                            <select
                                                value={ch.type}
                                                onChange={e => setForm(f => {
                                                    const chs = [...(f.channels || [])];
                                                    chs[i] = { ...chs[i], type: e.target.value };
                                                    return { ...f, channels: chs };
                                                })}
                                                className="w-44 shrink-0 px-3 py-2.5 rounded-xl bg-white border border-gray-300 text-gray-900 focus:outline-none focus:ring-2 focus:ring-gray-400 text-sm"
                                            >
                                                {CHANNEL_TYPES.map(opt => (
                                                    <option key={opt.id} value={opt.id}>{opt.label}</option>
                                                ))}
                                            </select>
                                            <input
                                                type="text"
                                                value={ch.value}
                                                onChange={e => setForm(f => {
                                                    const chs = [...(f.channels || [])];
                                                    chs[i] = { ...chs[i], value: e.target.value };
                                                    return { ...f, channels: chs };
                                                })}
                                                placeholder={ch.type === 'phone' ? '+1234567890 (used as WhatsApp)' : ch.type === 'whatsapp' ? '+1234567890' : ch.type === 'telegram' ? 'ID or @username' : ch.type === 'email' ? 'email@example.com' : 'Discord ID or handle'}
                                                className="flex-1 min-w-0 px-4 py-2.5 rounded-xl bg-white border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-transparent text-sm"
                                            />
                                            <button
                                                type="button"
                                                onClick={() => setForm(f => ({ ...f, channels: (f.channels || []).filter((_, j) => j !== i) }))}
                                                className="p-2 shrink-0 hover:bg-red-50 rounded-lg text-gray-400 hover:text-red-500 transition-colors"
                                                title="Remove"
                                            >
                                                <Trash2 className="w-4 h-4" />
                                            </button>
                                        </div>
                                    ))}
                                </div>
                                <button
                                    type="button"
                                    onClick={() => setForm(f => ({ ...f, channels: [...(f.channels || []), { type: 'whatsapp', value: '' }] }))}
                                    className="mt-2 flex items-center gap-2 text-sm font-medium text-gray-600 hover:text-gray-900"
                                >
                                    <Plus className="w-4 h-4" />
                                    Add channel
                                </button>
                            </div>

                            <div>
                                <h3 className="text-lg font-semibold text-gray-900 mb-2">Personal file</h3>
                                <p className="text-sm text-gray-500 mb-3">Preferred language, how to address, birthday, and notes.</p>
                                <div className="space-y-3">
                                    <div>
                                        <label className="block text-sm font-medium text-gray-700 mb-1">Preferred language</label>
                                        <input
                                            type="text"
                                            value={form.preferred_language ?? ''}
                                            onChange={e => setForm(f => ({ ...f, preferred_language: e.target.value }))}
                                            className="w-full px-4 py-3 rounded-xl bg-white border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-transparent"
                                            placeholder="e.g. de, en"
                                        />
                                    </div>
                                    <div>
                                        <label className="block text-sm font-medium text-gray-700 mb-1">How to address</label>
                                        <input
                                            type="text"
                                            value={form.how_to_address ?? ''}
                                            onChange={e => setForm(f => ({ ...f, how_to_address: e.target.value }))}
                                            className="w-full px-4 py-3 rounded-xl bg-white border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-transparent"
                                            placeholder="e.g. du, Sie, First name only"
                                        />
                                    </div>
                                    <div>
                                        <label className="block text-sm font-medium text-gray-700 mb-1">Birthday</label>
                                        <input
                                            type="text"
                                            value={form.birthday ?? ''}
                                            onChange={e => setForm(f => ({ ...f, birthday: e.target.value }))}
                                            className="w-full px-4 py-3 rounded-xl bg-white border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-transparent"
                                            placeholder="MM-DD or ISO date"
                                        />
                                    </div>
                                    <div>
                                        <label className="block text-sm font-medium text-gray-700 mb-1">Notes</label>
                                        <textarea
                                            value={form.notes ?? ''}
                                            onChange={e => setForm(f => ({ ...f, notes: e.target.value }))}
                                            rows={3}
                                            className="w-full px-4 py-3 rounded-xl bg-white border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-transparent resize-y"
                                            placeholder="Free-form notes about this contact"
                                        />
                                    </div>
                                    <div className="flex items-center justify-between pt-2">
                                        <label className="text-sm font-medium text-gray-700">Can reach your assistant (front office)</label>
                                        <button
                                            type="button"
                                            role="switch"
                                            aria-checked={form.allow_as_assistant_user ?? false}
                                            onClick={() => setForm(f => ({ ...f, allow_as_assistant_user: !(f.allow_as_assistant_user ?? false) }))}
                                            className={cn(
                                                'relative w-11 h-6 rounded-full transition-colors',
                                                (form.allow_as_assistant_user ?? false) ? 'bg-gray-800 dark:bg-[#d9d9d9]' : 'bg-gray-300 dark:bg-[#333333]'
                                            )}
                                        >
                                            <div
                                                className={cn(
                                                    'absolute top-1 w-4 h-4 rounded-full bg-white shadow transition-transform',
                                                    (form.allow_as_assistant_user ?? false) ? 'translate-x-6 dark:bg-[#1a1a1a]' : 'translate-x-1 dark:bg-[#e8e8e8]'
                                                )}
                                            />
                                        </button>
                                    </div>
                                    <p className="text-xs text-gray-500">
                                        When enabled, this contact can send messages to your assistant. The assistant handles them in your context (like a front office for you), not as a separate user account.
                                    </p>
                                </div>
                            </div>
                        </div>

                        <div className="flex items-center justify-between p-6 border-t border-gray-200 bg-gray-50 shrink-0 max-md:p-4">
                            <button
                                type="button"
                                onClick={() => { setShowFormModal(false); setModalContact(null); setForm({}); }}
                                className="text-gray-600 hover:bg-gray-200 px-4 py-2 rounded-lg transition-colors"
                            >
                                Cancel
                            </button>
                            <button
                                type="button"
                                onClick={handleSave}
                                disabled={saving || !(form.name ?? '').trim()}
                                className="bg-gray-900 hover:bg-gray-800 disabled:bg-gray-100 disabled:text-gray-400 text-white px-6 py-2 rounded-lg font-medium transition-colors disabled:cursor-not-allowed dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-[#f5f5f5] dark:shadow-none"
                            >
                                {saving ? 'Saving…' : 'Save'}
                            </button>
                        </div>
                    </div>
                </div>
            )}
        </>
    );
}
