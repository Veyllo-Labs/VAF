'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
//
// The WhatsApp window on the shared channel shell. The linked account is the
// agent's own number; each chat's badge says who it is to the agent (owner /
// contact / conversation inside the reply window / read-only), and the settings
// hold the agent number, the owner's registered number, who else may write, the
// reply window and the activity chart. Where the agent does NOT answer (a read-only
// sender, or the whole channel with inbound_to_agent off) the person answers
// themselves: a compose box under the chat sends from the agent's number, and the
// Composer on the right drafts into that box (the mail window's assistant on the
// shared lane, never sending anything itself).

import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { useTranslations } from 'next-intl';
import { Phone, UserPlus, Trash2, AlertTriangle, BookUser, Send, Sparkles, Loader2 } from 'lucide-react';
import ConfirmDialog from '@/components/ui/ConfirmDialog';
import { cn } from '@/lib/utils';
import MessagesChart from './MessagesChart';
import ChannelDashboardShell, { BADGE_CLS, BTN, BTN_PRIMARY, INPUT, KvRow, SettingsCard, ShellChat, fmtUntil } from './ChannelDashboardShell';

const api = (path: string) => path.startsWith('/') ? path : `/${path}`;

/** Size a textarea to its content, one line at minimum, `max` pixels at most. */
function growField(el: HTMLTextAreaElement | null, max: number) {
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, max)}px`;
}

const FIELD = 'bg-[#262626] border border-[#2e2e2e] rounded-2xl px-4 py-2 text-sm leading-5 outline-none focus:border-[#444] resize-none';
const ROUND_BTN = 'w-9 h-9 rounded-full grid place-items-center shrink-0 bg-[#25a244] text-white hover:bg-[#2db54e] disabled:opacity-40 disabled:hover:bg-[#25a244]';
const QUIET_BTN = 'text-xs text-[#9a9a9a] hover:text-white disabled:opacity-40 disabled:hover:text-[#9a9a9a]';

export interface WhatsAppDashboardProps {
    isOpen: boolean;
    onClose: () => void;
    config: any;
    onConfigChange: (key: string, value: any) => void;
    onOpenSetupWizard?: () => void;
    onOpenContacts?: () => void;
    /** Chat to open on arrival (a jump from the contact book); null leaves the selection alone. */
    initialChatId?: string | null;
}

interface WhatsAppSession {
    chat_id: string;
    phone_number: string;
    name?: string | null;
    session_id?: string;
    type: string;
    last_ts: number;
    message_count: number;
    needs_assign?: boolean;
    display_name?: string | null;
    resolved_e164?: string | null;
    contact_id?: string | null;
    contact_name?: string | null;
    reply_window_until?: number | null;
    last_preview?: string;
}

interface DashboardData {
    linked: boolean;
    sessions: WhatsAppSession[];
    stats_4h: Array<{ bucket_ts: number; count: number }>;
    linked_phone: string | null;
    reply_window_hours?: number;
    inbound_to_agent: boolean;
    owner_numbers: Array<{ phone_number: string; vaf_username?: string | null }>;
    front_office_contacts: Array<{ name: string | null; phone_number: string }>;
    connected: boolean;
    running: boolean;
    enabled: boolean;
    log_path: string | null;
    composer_enabled: boolean;
}

/** What the Composer read, so the panel can say so rather than imply it saw everything. */
interface ComposerMeta { included: number; total: number; truncated: boolean; dropped: number; own_included: number }
/** One exchange with the Composer. Assistant turns hold the draft they produced,
 *  replayed on the next request so a follow-up refines rather than restarts. */
interface ComposerTurn { role: 'user' | 'assistant'; content: string }
/** What a chat's compose box and Composer held when the person switched away. */
interface ComposeStash { composeText: string; turns: ComposerTurn[]; meta: ComposerMeta | null; instruction: string; beforeAssist: string | null }

export default function WhatsAppDashboard({ isOpen, onClose, config, onConfigChange, onOpenSetupWizard, onOpenContacts, initialChatId }: WhatsAppDashboardProps) {
    const t = useTranslations('settings.whatsappDashboard');
    const [data, setData] = useState<DashboardData | null>(null);
    const [loading, setLoading] = useState(false);
    const [loadFailed, setLoadFailed] = useState(false);
    const [selectedChatId, setSelectedChatId] = useState<string | null>(null);
    const [showSettings, setShowSettings] = useState(false);
    const [ownerAddPhone, setOwnerAddPhone] = useState('');
    const [ownerAddUsername, setOwnerAddUsername] = useState('');
    const [ownerAddError, setOwnerAddError] = useState<string | null>(null);
    const [restarting, setRestarting] = useState(false);
    const [restartError, setRestartError] = useState<string | null>(null);
    const [assignPhone, setAssignPhone] = useState('');
    const [note, setNote] = useState<string | null>(null);
    const [addingContact, setAddingContact] = useState(false);
    const [reachConfirm, setReachConfirm] = useState<WhatsAppSession | null>(null);
    const [windowInput, setWindowInput] = useState('');
    const [windowMsg, setWindowMsg] = useState<string | null>(null);
    const [olderBusy, setOlderBusy] = useState(false);
    const [namesBusy, setNamesBusy] = useState(false);
    const [namesMsg, setNamesMsg] = useState<string | null>(null);
    const [historyVersion, setHistoryVersion] = useState(0);
    // The compose box under the chat and the Composer beside it. `beforeAssist` backs
    // the Undo button, so one click always restores exactly what the person had typed.
    const [composeText, setComposeText] = useState('');
    const [sending, setSending] = useState(false);
    const [sendError, setSendError] = useState<string | null>(null);
    const [assistBusy, setAssistBusy] = useState(false);
    const [assistNote, setAssistNote] = useState('');
    const [assistInstruction, setAssistInstruction] = useState('');
    const [beforeAssist, setBeforeAssist] = useState<string | null>(null);
    const [assistMeta, setAssistMeta] = useState<ComposerMeta | null>(null);
    const [turns, setTurns] = useState<ComposerTurn[]>([]);
    const abortRef = useRef<AbortController | null>(null);
    const chatEndRef = useRef<HTMLDivElement>(null);
    const composeRef = useRef<HTMLTextAreaElement>(null);
    const instructionRef = useRef<HTMLTextAreaElement>(null);
    // The Composer exchange of every chat visited while the window is open, keyed by
    // chat. Kept in the window's memory only: restoring it on a chat switch is a state
    // swap, no request leaves the browser until the person clicks Draft or Send.
    const composeStashRef = useRef<Map<string, ComposeStash>>(new Map());
    useEffect(() => { chatEndRef.current?.scrollIntoView({ block: 'end' }); }, [turns, assistBusy]);
    // Both text fields start one line high and grow with their content, like a
    // messenger's input: a two-line box under a chat reads as a form, not a chat.
    useEffect(() => { growField(composeRef.current, 200); }, [composeText]);
    useEffect(() => { growField(instructionRef.current, 120); }, [assistInstruction]);
    // The jump id waiting to be checked against the loaded sessions. A ref, consumed once,
    // so the check runs on the load that follows the jump and not on every later refresh
    // (a refresh would otherwise yank the selection back to the first chat).
    const jumpPendingRef = useRef<string | null>(null);

    const fetchDashboard = useCallback(async () => {
        setLoading(true);
        setLoadFailed(false);
        try {
            const res = await fetch(api('api/whatsapp/dashboard'), { credentials: 'include' });
            const json = await res.json();
            if (!res.ok) { setLoadFailed(true); return; }
            const sessions: WhatsAppSession[] = Array.isArray(json?.sessions) ? json.sessions : [];
            setData({
                linked: json?.linked === true,
                sessions,
                stats_4h: Array.isArray(json?.stats_4h) ? json.stats_4h : [],
                linked_phone: json?.linked_phone || null,
                reply_window_hours: typeof json?.reply_window_hours === 'number' ? json.reply_window_hours : undefined,
                inbound_to_agent: json?.inbound_to_agent !== false,
                owner_numbers: Array.isArray(json?.owner_numbers) ? json.owner_numbers : [],
                front_office_contacts: Array.isArray(json?.front_office_contacts) ? json.front_office_contacts : [],
                connected: json?.connected === true,
                running: json?.running === true,
                enabled: json?.enabled === true,
                log_path: json?.log_path || null,
                composer_enabled: json?.composer_enabled !== false,
            });
            if (typeof json?.reply_window_hours === 'number') setWindowInput(String(json.reply_window_hours));
            setSelectedChatId(prev => prev ?? (sessions[0]?.chat_id ?? null));
            const pendingJump = jumpPendingRef.current;
            if (pendingJump) {
                jumpPendingRef.current = null;
                if (!sessions.some(s => s.chat_id === pendingJump)) {
                    setSelectedChatId(sessions[0]?.chat_id ?? null);
                    setNote(t('jumpChatNotFound'));
                }
            }
        } catch {
            setLoadFailed(true);
        } finally {
            setLoading(false);
        }
    }, [t]);

    useEffect(() => {
        // Closing runs this effect before the parent clears the jump; a closed window must
        // neither re-arm the pending id nor move the selection.
        if (!isOpen) { jumpPendingRef.current = null; return; }
        if (!initialChatId) return;
        jumpPendingRef.current = initialChatId;
        // A jump from the contact book bypasses the list's onSelect, so the compose
        // stash has to change hands here too, or the old chat's draft would travel along.
        if (initialChatId !== selectedChatId) switchCompose(selectedChatId, initialChatId);
        setSelectedChatId(initialChatId);
    }, [initialChatId, isOpen]); // eslint-disable-line react-hooks/exhaustive-deps

    useEffect(() => { if (isOpen) fetchDashboard(); }, [isOpen, config?.whatsapp_config, fetchDashboard]);

    const handleRefresh = async () => {
        await fetchDashboard();
        try {
            const res = await fetch(api('api/whatsapp/dashboard'), { credentials: 'include' });
            const json = await res.json();
            if (!res.ok) return;
            if (json.connected) {
                await fetch(api('api/whatsapp/sync-chats'), { method: 'POST', credentials: 'include' });
                await fetchDashboard();
            } else if (!json.running && json.enabled) {
                await fetch(api('api/whatsapp/start'), { method: 'POST', credentials: 'include' });
                await new Promise(r => setTimeout(r, 2000));
                await fetchDashboard();
            }
        } catch { /* the first fetch already reported */ }
    };

    const saveWhatsAppConfig = async (patch: Record<string, unknown>) => {
        const wc = config?.whatsapp_config || {};
        const next = { ...wc, ...patch };
        const res = await fetch(api('api/config'), {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ whatsapp_config: next }),
            credentials: 'include',
        });
        if (!res.ok) throw new Error(String(res.status));
        onConfigChange('whatsapp_config', next);
    };

    const handleRestartBridge = async () => {
        setRestarting(true);
        setRestartError(null);
        try {
            await saveWhatsAppConfig({ enabled: true });
            const res = await fetch(api('api/whatsapp/restart'), { method: 'POST', credentials: 'include' });
            const json = await res.json().catch(() => ({}));
            if (!res.ok) { setRestartError(json?.detail || json?.message || String(res.status)); return; }
            await new Promise(r => setTimeout(r, 3000));
            await fetchDashboard();
        } catch (e) {
            setRestartError(e instanceof Error ? e.message : String(e));
        } finally {
            setRestarting(false);
        }
    };

    const handleRelink = async () => {
        await fetch(api('api/whatsapp/qr/reset'), { method: 'POST', credentials: 'include' });
        onClose();
        onOpenSetupWizard?.();
    };

    const handleOwnerAdd = async () => {
        const raw = ownerAddPhone.trim().replace(/\s/g, '');
        if (!raw) return;
        const phone = raw.startsWith('+') ? raw : `+${raw}`;
        setOwnerAddError(null);
        try {
            const res = await fetch(api('api/whatsapp/whitelist/add'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({ phone_number: phone, vaf_username: ownerAddUsername.trim() || undefined }),
            });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                setOwnerAddError(err?.detail || res.statusText || String(res.status));
                return;
            }
            setOwnerAddPhone('');
            setOwnerAddUsername('');
            onConfigChange('whatsapp_config', { ...config.whatsapp_config, whitelist: [...(config.whatsapp_config?.whitelist || []), { phone_number: phone, vaf_username: ownerAddUsername.trim() || null }] });
            fetchDashboard();
        } catch (e) {
            setOwnerAddError(e instanceof Error ? e.message : String(e));
        }
    };

    const handleOwnerRemove = async (phone_number: string) => {
        try {
            await fetch(api('api/whatsapp/whitelist/remove'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({ phone_number }),
            });
            const current = config.whatsapp_config?.whitelist || [];
            onConfigChange('whatsapp_config', { ...config.whatsapp_config, whitelist: current.filter((e: any) => String(e.phone_number) !== phone_number) });
            fetchDashboard();
        } catch (e) {
            setOwnerAddError(e instanceof Error ? e.message : String(e));
        }
    };

    const handleAssign = async (chatId: string) => {
        const raw = assignPhone.trim().replace(/\s/g, '');
        if (!raw) return;
        setNote(null);
        try {
            const res = await fetch(api('api/whatsapp/lid-assign'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({ lid_jid: chatId, phone_number: raw.startsWith('+') ? raw : `+${raw}` }),
            });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                setNote(err?.detail || t('assignFailed'));
                return;
            }
            setAssignPhone('');
            setSelectedChatId(null);
            fetchDashboard();
        } catch {
            setNote(t('assignFailed'));
        }
    };

    const handleAddAsContact = async (s: WhatsAppSession) => {
        const phone = s.resolved_e164 || s.phone_number || s.chat_id;
        if (!phone || phone.includes('@')) return;
        setAddingContact(true);
        setNote(null);
        try {
            const res = await fetch(api('api/contacts'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({
                    name: (s.display_name || s.name || phone).trim(),
                    whatsapp_phone: phone,
                    allow_as_assistant_user: true,
                }),
            });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                setNote(err?.detail || t('addContactFailed'));
                return;
            }
            fetchDashboard();
        } catch {
            setNote(t('addContactFailed'));
        } finally {
            setAddingContact(false);
        }
    };

    const handleAllowReach = async (s: WhatsAppSession, allow: boolean) => {
        if (!s.contact_id) return;
        setAddingContact(true);
        setNote(null);
        try {
            const res = await fetch(api(`api/contacts/${encodeURIComponent(s.contact_id)}`), {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({ allow_as_assistant_user: allow }),
            });
            if (!res.ok) { setNote(t('addContactFailed')); return; }
            fetchDashboard();
        } catch {
            setNote(t('addContactFailed'));
        } finally {
            setAddingContact(false);
        }
    };

    const handleLoadOlder = async (chatId: string) => {
        setOlderBusy(true);
        setNote(null);
        try {
            const res = await fetch(api('api/whatsapp/chat-messages/older'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({ chat_id: chatId, count: 50 }),
            });
            const json = await res.json().catch(() => ({}));
            if (!res.ok) { setNote(json?.detail || t('loadOlderFailed')); return; }
            const loaded = Number(json?.loaded || 0);
            setNote(loaded > 0 ? t('loadedOlder', { count: loaded }) : json?.no_cursor ? t('olderNeedsMessage') : t('noOlder'));
            if (loaded > 0) setHistoryVersion(v => v + 1);
        } catch {
            setNote(t('loadOlderFailed'));
        } finally {
            setOlderBusy(false);
        }
    };

    const handleReloadNames = async () => {
        setNamesBusy(true);
        setNamesMsg(null);
        try {
            const res = await fetch(api('api/whatsapp/contacts/resync'), { method: 'POST', credentials: 'include' });
            const json = await res.json().catch(() => ({}));
            setNamesMsg(res.ok ? t('reloadNamesDone') : (json?.detail || t('reloadNamesFailed')));
            if (res.ok) setTimeout(() => fetchDashboard(), 3000);
        } catch {
            setNamesMsg(t('reloadNamesFailed'));
        } finally {
            setNamesBusy(false);
        }
    };

    const handleSaveWindow = async () => {
        const hours = Number(windowInput);
        if (!Number.isFinite(hours) || hours < 0) { setWindowMsg(t('saveFailed')); return; }
        try {
            await saveWhatsAppConfig({ reply_window_hours: hours });
            setWindowMsg(t('saved'));
            fetchDashboard();
        } catch {
            setWindowMsg(t('saveFailed'));
        }
    };

    const handleToggleInbound = async () => {
        if (!data) return;
        try {
            await saveWhatsAppConfig({ inbound_to_agent: !data.inbound_to_agent });
            fetchDashboard();
        } catch {
            setWindowMsg(t('saveFailed'));
        }
    };

    /** A draft typed for one chat must never be sent to another, but it must not be
     *  lost either: on a switch the leaving chat's box and exchange go into the stash
     *  under its id, and the arriving chat gets its own back (or a clean slate). A
     *  generation still running for the leaving chat is stopped; its partial draft
     *  stays in that chat's stash. */
    const switchCompose = (from: string | null, to: string | null) => {
        abortRef.current?.abort();
        if (from) {
            composeStashRef.current.set(from, {
                composeText, turns, meta: assistMeta, instruction: assistInstruction, beforeAssist,
            });
        }
        const next = (to && composeStashRef.current.get(to)) || null;
        setComposeText(next?.composeText ?? '');
        setTurns(next?.turns ?? []);
        setAssistMeta(next?.meta ?? null);
        setAssistInstruction(next?.instruction ?? '');
        setBeforeAssist(next?.beforeAssist ?? null);
        setSendError(null);
        setAssistNote('');
    };
    useEffect(() => {
        // Closing the window ends the day's exchanges: nothing is kept across opens.
        if (!isOpen) composeStashRef.current.clear();
    }, [isOpen]);

    /** The person writes here only where the agent does not: a read-only sender, or the
     *  whole channel with inbound_to_agent off. Owner, contact and conversation chats
     *  are the agent's to answer, and an unassigned LID has no address to send to. */
    const canCompose = (s: WhatsAppSession) =>
        !s.needs_assign && (data?.inbound_to_agent === false || s.type === 'unknown');

    const handleSend = async (s: WhatsAppSession) => {
        const text = composeText.trim();
        if (!text) return;
        setSending(true);
        setSendError(null);
        try {
            const res = await fetch(api('api/whatsapp/send'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({ chat_id: s.chat_id, text }),
            });
            const json = await res.json().catch(() => ({}));
            if (!res.ok) { setSendError(json?.detail || t('sendFailed')); return; }
            setComposeText('');
            setBeforeAssist(null);
            setHistoryVersion(v => v + 1);
            fetchDashboard();
        } catch {
            setSendError(t('sendFailed'));
        } finally {
            setSending(false);
        }
    };

    const runComposer = useCallback(async (s: WhatsAppSession, mode: 'draft' | 'rewrite') => {
        const said = assistInstruction.trim();
        // The conversation sent to the server is what happened BEFORE this turn;
        // the current instruction travels separately as the operator turn.
        const priorTurns = turns.map(x => ({ role: x.role, content: x.content }));
        setTurns(x => [...x, { role: 'user', content: said || (mode === 'draft' ? '\u2726' : '\u21bb') }]);
        setAssistInstruction('');
        setBeforeAssist(composeText);
        setAssistBusy(true);
        setAssistNote('');
        setAssistMeta(null);
        let produced = '';
        const ctrl = new AbortController();
        abortRef.current = ctrl;
        try {
            const res = await fetch(api('api/whatsapp/composer'), {
                method: 'POST', credentials: 'include', signal: ctrl.signal,
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    chat_id: s.chat_id, mode, instruction: said,
                    draft: mode === 'rewrite' ? composeText : '',
                    turns: priorTurns, chat_label: s.display_name || s.name || s.phone_number || '',
                }),
            });
            if (!res.ok || !res.body) { setAssistNote(t('composer.failed')); return; }
            // SSE: each data frame carries the FULL text so far (see the route),
            // so we replace rather than append and never show a partial <think>.
            const reader = res.body.getReader();
            const dec = new TextDecoder();
            let buf = '';
            for (;;) {
                const { done, value } = await reader.read();
                if (done) break;
                buf += dec.decode(value, { stream: true });
                const frames = buf.split('\n\n');
                buf = frames.pop() || '';
                for (const frame of frames) {
                    if (frame.startsWith('event: notice')) {
                        // Why nothing is happening yet: a cold local model maps from disk.
                        setAssistNote(t('composer.localLoading'));
                        continue;
                    }
                    if (frame.startsWith('event: error')) {
                        const l = frame.split('\n').find(x => x.startsWith('data: '));
                        let code = 'failed';
                        try { code = JSON.parse(l ? l.slice(6) : '""') || 'failed'; } catch { /* keep default */ }
                        setAssistNote(t(code === 'local_unavailable' ? 'composer.localUnavailable'
                            : code === 'local_loading' ? 'composer.localLoading'
                            : code === 'reasoning_only' ? 'composer.reasoningOnly' : 'composer.failed'));
                        continue;
                    }
                    const line = frame.split('\n').find(l => l.startsWith('data: '));
                    if (!line) continue;
                    if (frame.startsWith('event: meta')) {
                        try { setAssistMeta(JSON.parse(line.slice(6))); } catch { /* ignore */ }
                        continue;
                    }
                    if (frame.startsWith('event: end')) continue;
                    try {
                        const text = JSON.parse(line.slice(6));
                        if (typeof text === 'string') {
                            setAssistNote('');       // tokens arrive: the wait is over
                            produced = text;
                            setComposeText(text);
                        }
                    } catch { /* partial frame: the next read completes it */ }
                }
            }
        } catch (e) {
            if ((e as Error)?.name !== 'AbortError') setAssistNote(t('composer.failed'));
        } finally {
            // The assistant's turn IS the draft: replaying it lets the next
            // instruction ("shorter") refine what it just wrote instead of
            // starting over from the chat.
            if (produced) setTurns(x => [...x, { role: 'assistant', content: produced }]);
            setAssistBusy(false);
            abortRef.current = null;
        }
    }, [composeText, assistInstruction, turns, t]);

    const badgeFor = (s: WhatsAppSession) => {
        if (s.needs_assign) return { label: t('badgeAssign'), cls: BADGE_CLS.assign };
        if (s.type === 'owner') return { label: t('badgeOwner'), cls: BADGE_CLS.owner };
        if (s.type === 'contact') return { label: t('badgeContact'), cls: BADGE_CLS.contact };
        if (s.type === 'conversation') return { label: t('badgeConversation'), cls: BADGE_CLS.conversation };
        return { label: t('badgeReadOnly'), cls: BADGE_CLS.readOnly };
    };

    const sublineFor = (s: WhatsAppSession) => {
        const phone = (s.resolved_e164 || s.phone_number || '');
        const prefix = phone && !phone.includes('@') ? `${phone} · ` : '';
        if (s.needs_assign) return t('subAssign');
        if (s.type === 'owner') return prefix + t('subOwner');
        if (s.type === 'contact') return prefix + t('subContact');
        if (s.type === 'conversation') return prefix + (s.reply_window_until ? t('subConversation', { until: fmtUntil(s.reply_window_until) }) : t('subConversationOpen'));
        return prefix + t('subReadOnly');
    };

    const footerFor = (s: WhatsAppSession) => {
        if (s.type === 'owner') return t('footOwner');
        if (s.type === 'contact' || s.type === 'conversation') return t('footFrontOffice');
        return t('footReadOnly');
    };

    const sessionsById = useMemo(() => new Map((data?.sessions || []).map(s => [s.chat_id, s])), [data]);

    const chats: ShellChat[] = useMemo(() => (data?.sessions || []).map(s => ({
        id: s.chat_id,
        historyKey: s.chat_id,
        label: s.display_name || s.name || s.phone_number || t('unknownChat'),
        avatarUrl: (s.resolved_e164 || s.chat_id).startsWith('+') ? api(`api/whatsapp/avatar?chat_id=${encodeURIComponent(s.resolved_e164 || s.chat_id)}`) : null,
        preview: s.last_preview || '',
        ts: s.last_ts,
        badge: badgeFor(s),
        subline: sublineFor(s),
        footer: footerFor(s),
    })), [data, t]); // eslint-disable-line react-hooks/exhaustive-deps

    const stateText = data?.connected ? t('stateConnected') : data?.running ? t('stateRunning') : t('stateStopped');
    const dot = data?.connected ? 'green' : data?.running ? 'amber' : 'gray';

    const banner = data && !data.running ? (
        <div className="px-5 py-2 bg-[#2b2417] border-b border-[#4a3b1e] text-[#d4a24e] text-[13px] flex items-center gap-2 flex-wrap">
            <AlertTriangle className="w-4 h-4 flex-shrink-0" />
            <span className="flex-1 min-w-0">{data.linked ? t('sessionExpired') : t('bridgeNotStartedDesc')}</span>
            {restartError && <span className="text-[#e08c8c]">{restartError}</span>}
            <button type="button" onClick={handleRestartBridge} disabled={restarting}
                className="px-2 py-1 rounded-md border border-[#4a3b1e] hover:bg-[#3a2f16] disabled:opacity-50">{restarting ? t('starting') : t('startBridge')}</button>
            {data.linked && <button type="button" onClick={handleRelink} className="px-2 py-1 rounded-md hover:bg-[#3a2f16]">{t('relinkOpensSetup')}</button>}
        </div>
    ) : null;

    const conversationExtra = (chat: ShellChat) => {
        const s = sessionsById.get(chat.id);
        if (!s) return null;
        const phone = s.resolved_e164 || s.phone_number || '';
        const canAddContact = !s.needs_assign && !s.contact_id && (s.type === 'conversation' || s.type === 'unknown') && !!phone && !phone.includes('@');
        // A contact-book record (Front Office flag on or off) gets a switch; the owner's own number never does.
        const hasBookRecord = !s.needs_assign && !!s.contact_id && s.type !== 'owner';
        const reachOn = s.type === 'contact';
        return (
            <>
                {s.needs_assign && (
                    <div className="flex items-center gap-2">
                        <input type="tel" value={assignPhone} onChange={e => setAssignPhone(e.target.value)} placeholder={t('numberPlaceholder')} className={cn('w-44', INPUT)} />
                        <button type="button" onClick={() => handleAssign(s.chat_id)} disabled={!assignPhone.trim()} className={BTN_PRIMARY}>{t('assign')}</button>
                    </div>
                )}
                {canAddContact && (
                    <button type="button" onClick={() => handleAddAsContact(s)} disabled={addingContact} className={cn('flex items-center gap-1.5', BTN)}>
                        <UserPlus className="w-4 h-4" />{t('addAsContact')}
                    </button>
                )}
                {hasBookRecord && (
                    <>
                        <span className="text-xs text-[#9a9a9a] flex items-center gap-1.5" title={s.contact_name || undefined}>
                            <BookUser className="w-4 h-4" />{t('inContacts')}
                        </span>
                        <label className="flex items-center gap-2 text-xs text-[#d0d0d0] cursor-pointer select-none">
                            <button type="button" role="switch" aria-checked={reachOn} disabled={addingContact}
                                onClick={() => reachOn ? handleAllowReach(s, false) : setReachConfirm(s)}
                                // The house switch in its dark form (ConnectionsPanel, ContactsDashboard): light track and dark knob when on, dark track and light knob when off. The shell is dark-only, so the dark pair is used outright.
                                className={cn('relative w-11 h-6 rounded-full transition-colors', reachOn ? 'bg-[#d9d9d9]' : 'bg-[#333333]')}>
                                {/* left-0: an absolutely positioned SPAN inside a button starts at the button's centred static position, so without it the knob sat on the right while the switch was off. */}
                                <span className={cn('absolute left-0 top-1 w-4 h-4 rounded-full shadow transition-transform', reachOn ? 'translate-x-6 bg-[#1a1a1a]' : 'translate-x-1 bg-[#e8e8e8]')} />
                            </button>
                            {t('allowReach')}
                        </label>
                    </>
                )}
            </>
        );
    };

    const conversationTop = (chat: ShellChat) => {
        const s = sessionsById.get(chat.id);
        if (!s || s.needs_assign) return null;
        return (
            <button type="button" onClick={() => handleLoadOlder(s.chat_id)} disabled={olderBusy}
                className="text-[11px] text-[#9a9a9a] bg-[#1f1f1f] hover:text-[#e8e8e8] px-2.5 py-0.5 rounded-full disabled:opacity-50">
                {olderBusy ? t('loadingOlder') : t('loadOlder')}
            </button>
        );
    };

    const composeBar = (chat: ShellChat) => {
        const s = sessionsById.get(chat.id);
        if (!s || !canCompose(s)) return null;
        return (
            <div className="px-4 py-2.5 border-t border-[#2e2e2e] bg-[#1a1a1a] shrink-0 flex flex-col gap-1">
                <div className="flex items-end gap-2">
                    <textarea ref={composeRef} value={composeText} onChange={e => setComposeText(e.target.value)}
                        onKeyDown={e => {
                            if (e.key === 'Enter' && !e.shiftKey && !sending && composeText.trim()) {
                                e.preventDefault();
                                handleSend(s);
                            }
                        }}
                        placeholder={t('composePlaceholder')} rows={1} disabled={sending}
                        className={cn(FIELD, 'flex-1')} />
                    <button type="button" onClick={() => handleSend(s)} disabled={sending || !composeText.trim()}
                        title={sending ? t('sending') : t('send')} className={ROUND_BTN}>
                        {sending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4 -ml-0.5" />}
                    </button>
                </div>
                {sendError && <p className="text-xs text-[#e08c8c] px-1">{sendError}</p>}
            </div>
        );
    };

    const aside = (chat: ShellChat) => {
        const s = sessionsById.get(chat.id);
        if (!s || !canCompose(s) || data?.composer_enabled === false) return null;
        return (
            <div className="p-4 flex flex-col gap-3 flex-1 min-h-0">
                <div className="flex items-center gap-2 text-[13px] font-semibold shrink-0">
                    <Sparkles className="w-4 h-4 text-[#25a244]" />{t('composer.panelTitle')}
                    {turns.length > 0 && !assistBusy && (
                        <button type="button" onClick={() => { setTurns([]); setAssistMeta(null); }}
                            className="ml-auto text-[11px] font-normal text-[#7a7a7a] hover:text-white">
                            {t('composer.newChat')}
                        </button>
                    )}
                </div>

                {/* The exchange. Assistant turns are a short result line, not the draft
                    text: the draft is already in the compose box in full. */}
                <div className="flex-1 min-h-[6rem] overflow-y-auto space-y-2 pr-0.5">
                    {turns.length === 0 && !assistBusy && (
                        <p className="text-[#7a7a7a] text-xs leading-relaxed">{t('composer.panelHint')}</p>
                    )}
                    {turns.map((turn, i) => turn.role === 'user' ? (
                        <div key={i} className="ml-6 px-3 py-1.5 rounded-lg bg-[#2e2e2e] text-sm break-words">{turn.content}</div>
                    ) : (
                        <div key={i} className="mr-6 px-3 py-1.5 rounded-lg bg-[#262626] border border-[#2e2e2e] text-xs text-[#9a9a9a] leading-relaxed">
                            {t('composer.inserted')}
                        </div>
                    ))}
                    {assistBusy && (
                        <div className="mr-6 px-3 py-1.5 rounded-lg bg-[#262626] border border-[#2e2e2e] text-xs text-[#9a9a9a] flex items-center gap-1.5">
                            <Loader2 className="w-3 h-3 animate-spin" />{t('composer.working')}
                        </div>
                    )}
                    {assistNote && <div className="text-[#e0b84c] text-xs leading-relaxed">{assistNote}</div>}
                    {assistMeta && !assistNote && !assistBusy && (
                        <div className="text-[#7a7a7a] text-[11px] leading-relaxed">
                            {t('composer.readCount', { used: assistMeta.included, total: assistMeta.total })}
                            {(assistMeta.truncated || assistMeta.dropped > 0) && <> {t('composer.shortened')}</>}
                            {/* Whether it had a sample of the person's own writing at all.
                                Without one it falls back to a neutral register, and the
                                person should know that rather than wonder why it sounds off. */}
                            <> {assistMeta.own_included > 0
                                ? t('composer.matchedYourTone', { n: assistMeta.own_included })
                                : t('composer.noToneSample')}</>
                        </div>
                    )}
                    <div ref={chatEndRef} />
                </div>

                {/* One primary action, the rest quiet: the column is narrow, and two
                    labelled buttons side by side broke into four lines of text. */}
                <div className="shrink-0 space-y-2">
                    <textarea ref={instructionRef} value={assistInstruction} onChange={e => setAssistInstruction(e.target.value)}
                        onKeyDown={e => {
                            if (e.key === 'Enter' && !e.shiftKey && !assistBusy) {
                                e.preventDefault();
                                runComposer(s, 'draft');
                            }
                        }}
                        placeholder={turns.length ? t('composer.followUp') : t('composer.instruction')}
                        disabled={assistBusy} rows={1}
                        className={cn(FIELD, 'w-full')} />
                    {assistBusy ? (
                        <button type="button" onClick={() => abortRef.current?.abort()}
                            className={cn(BTN, 'w-full flex items-center justify-center gap-1.5')}>
                            <Loader2 className="w-3.5 h-3.5 animate-spin" />{t('composer.stop')}
                        </button>
                    ) : (
                        <button type="button" onClick={() => runComposer(s, 'draft')}
                            className="w-full px-3 py-2 rounded-xl text-sm font-medium bg-[#25a244] text-white hover:bg-[#2db54e] flex items-center justify-center gap-1.5">
                            <Sparkles className="w-4 h-4" />{t('composer.draft')}
                        </button>
                    )}
                    {!assistBusy && (composeText.trim() || (beforeAssist !== null && beforeAssist !== composeText)) && (
                        <div className="flex items-center justify-between gap-3 px-1">
                            <button type="button" onClick={() => runComposer(s, 'rewrite')} disabled={!composeText.trim()} className={QUIET_BTN}>
                                {t('composer.rewrite')}
                            </button>
                            {beforeAssist !== null && beforeAssist !== composeText && (
                                <button type="button" onClick={() => { setComposeText(beforeAssist); setBeforeAssist(null); }} className={QUIET_BTN}>
                                    {t('composer.undo')}
                                </button>
                            )}
                        </div>
                    )}
                </div>
            </div>
        );
    };

    const settingsContent = (
        <>
            <SettingsCard title={t('cardAgentTitle')} desc={t('cardAgentDesc')}>
                <KvRow
                    left={<><span className={cn('w-2 h-2 rounded-full', dot === 'green' ? 'bg-[#3fbf5f]' : dot === 'amber' ? 'bg-[#e0a030]' : 'bg-[#555]')} />{data?.linked_phone || t('notLinked')}</>}
                    right={stateText}
                />
                <div className="flex gap-2 flex-wrap">
                    <button type="button" onClick={handleRestartBridge} disabled={restarting} className={BTN}>{restarting ? t('restarting') : t('restartBridge')}</button>
                    <button type="button" onClick={handleRelink} className={BTN}>{t('relink')}</button>
                    <button type="button" onClick={handleReloadNames} disabled={namesBusy || !data?.connected} className={BTN}>{namesBusy ? t('reloadingNames') : t('reloadNames')}</button>
                </div>
                {restartError && <p className="mt-2 text-xs text-[#e08c8c]">{restartError}</p>}
                {namesMsg && <p className="mt-2 text-xs text-[#9a9a9a]">{namesMsg}</p>}
            </SettingsCard>

            <SettingsCard title={t('cardOwnerTitle')} desc={t('cardOwnerDesc')}>
                {(data?.owner_numbers || []).map((e, i) => (
                    <KvRow key={i} left={e.phone_number} right={<>
                        {e.vaf_username && <span>{e.vaf_username}</span>}
                        <button type="button" title={t('remove')} onClick={() => { if (confirm(t('removeOwnerConfirm'))) handleOwnerRemove(e.phone_number); }}
                            className="p-1 rounded hover:bg-[#3a1d1d] text-[#9a9a9a] hover:text-[#e08c8c]"><Trash2 className="w-3.5 h-3.5" /></button>
                    </>} />
                ))}
                {(!data?.owner_numbers || data.owner_numbers.length === 0) && <p className="text-[12.5px] text-[#9a9a9a] mb-2">{t('ownerNone')}</p>}
                <div className="flex gap-2 flex-wrap">
                    <input type="tel" placeholder={t('numberPlaceholder')} value={ownerAddPhone} onChange={e => setOwnerAddPhone(e.target.value)} className={cn('flex-1 min-w-[10rem]', INPUT)} />
                    <input type="text" placeholder={t('ownerUserPlaceholder')} value={ownerAddUsername} onChange={e => setOwnerAddUsername(e.target.value)} className={cn('flex-1 min-w-[10rem]', INPUT)} />
                    <button type="button" onClick={handleOwnerAdd} disabled={!ownerAddPhone.trim()} className={BTN_PRIMARY}>{t('register')}</button>
                </div>
                {ownerAddError && <p className="mt-2 text-xs text-[#e08c8c]">{ownerAddError}</p>}
            </SettingsCard>

            <SettingsCard title={t('cardWhoTitle')} desc={t('cardWhoDesc')}>
                {(data?.front_office_contacts || []).map((c, i) => (
                    <KvRow key={i} left={c.name || c.phone_number} right={c.name ? c.phone_number : undefined} />
                ))}
                {(!data?.front_office_contacts || data.front_office_contacts.length === 0) && <p className="text-[12.5px] text-[#9a9a9a] mb-2">{t('noFoContacts')}</p>}
                {onOpenContacts && <button type="button" onClick={onOpenContacts} className="text-[13px] text-[#6fb3ff] hover:underline">{t('manageContacts')}</button>}
            </SettingsCard>

            <SettingsCard title={t('cardWindowTitle')} desc={t('cardWindowDesc')}>
                <div className="flex gap-2 items-center flex-wrap">
                    <input type="number" min={0} value={windowInput} onChange={e => { setWindowInput(e.target.value); setWindowMsg(null); }} className={cn('w-24', INPUT)} />
                    <span className="text-sm text-[#9a9a9a]">{t('hours')}</span>
                    <button type="button" onClick={handleSaveWindow} className={BTN}>{t('save')}</button>
                    {windowMsg && <span className="text-xs text-[#9a9a9a]">{windowMsg}</span>}
                </div>
                <p className="text-[12.5px] text-[#9a9a9a] mt-3">
                    {data?.inbound_to_agent === false ? t('inboundOff') : t('inboundOn')}{' · '}
                    <button type="button" onClick={handleToggleInbound} className="text-[#6fb3ff] hover:underline">{data?.inbound_to_agent === false ? t('switchOn') : t('switchOff')}</button>
                </p>
            </SettingsCard>

            <SettingsCard title={t('cardActivityTitle')} full>
                <MessagesChart buckets={data?.stats_4h ?? []} chartId="whatsapp-messages-chart" />
                {data?.log_path && <p className="text-[12px] text-[#9a9a9a] mt-2">{t('logLabel')} <code className="bg-[#262626] px-1 rounded">{data.log_path}</code></p>}
            </SettingsCard>
        </>
    );

    return (
        <>
        <ChannelDashboardShell
            isOpen={isOpen}
            onClose={onClose}
            icon={<Phone className="w-4 h-4 text-white" />}
            iconClass="bg-[#25a244]"
            title={t('title')}
            subtitle={<>{t('agentNumber')} <span className="text-[#d0d0d0]">{data?.linked_phone || t('notLinked')}</span></>}
            dot={dot}
            dotTitle={stateText}
            chats={chats}
            loading={loading}
            loadFailed={loadFailed}
            onRefresh={handleRefresh}
            historyUrl={(cid) => `api/whatsapp/chat-messages?chat_id=${encodeURIComponent(cid)}`}
            historyVersion={historyVersion}
            selectedId={selectedChatId}
            onSelect={(id) => { if (id !== selectedChatId) switchCompose(selectedChatId, id); setSelectedChatId(id); setNote(null); }}
            banner={banner}
            conversationExtra={conversationExtra}
            conversationTop={conversationTop}
            composeBar={composeBar}
            aside={aside}
            conversationNote={note}
            settingsTitle={t('settingsTitle')}
            settingsContent={settingsContent}
            settingsOpen={showSettings}
            onSettingsOpenChange={setShowSettings}
        />
        <ConfirmDialog
            open={reachConfirm !== null}
            title={t('allowReachConfirmTitle')}
            body={t('allowReachConfirmBody', { name: reachConfirm?.contact_name || reachConfirm?.display_name || reachConfirm?.phone_number || '' })}
            confirmLabel={t('allowReachConfirmYes')}
            cancelLabel={t('allowReachConfirmNo')}
            onConfirm={() => { const s = reachConfirm; setReachConfirm(null); if (s) handleAllowReach(s, true); }}
            onCancel={() => setReachConfirm(null)}
            zIndexClass="z-[60]"
            escapeLevel={53}
        />
        </>
    );
}
