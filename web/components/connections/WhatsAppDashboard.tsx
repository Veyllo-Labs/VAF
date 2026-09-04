'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
//
// The WhatsApp window on the shared channel shell. The linked account is the
// agent's own number; each chat's badge says who it is to the agent (owner /
// contact / conversation inside the reply window / read-only), and the settings
// hold the agent number, the owner's registered number, who else may write, the
// reply window and the activity chart.

import { useState, useEffect, useCallback, useMemo } from 'react';
import { useTranslations } from 'next-intl';
import { Phone, UserPlus, Trash2, AlertTriangle } from 'lucide-react';
import { cn } from '@/lib/utils';
import MessagesChart from './MessagesChart';
import ChannelDashboardShell, { BADGE_CLS, BTN, BTN_PRIMARY, INPUT, KvRow, SettingsCard, ShellChat, fmtUntil } from './ChannelDashboardShell';

const api = (path: string) => path.startsWith('/') ? path : `/${path}`;

export interface WhatsAppDashboardProps {
    isOpen: boolean;
    onClose: () => void;
    config: any;
    onConfigChange: (key: string, value: any) => void;
    onOpenSetupWizard?: () => void;
    onOpenContacts?: () => void;
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
}

export default function WhatsAppDashboard({ isOpen, onClose, config, onConfigChange, onOpenSetupWizard, onOpenContacts }: WhatsAppDashboardProps) {
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
    const [windowInput, setWindowInput] = useState('');
    const [windowMsg, setWindowMsg] = useState<string | null>(null);
    const [olderBusy, setOlderBusy] = useState(false);
    const [historyVersion, setHistoryVersion] = useState(0);

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
            });
            if (typeof json?.reply_window_hours === 'number') setWindowInput(String(json.reply_window_hours));
            setSelectedChatId(prev => prev ?? (sessions[0]?.chat_id ?? null));
        } catch {
            setLoadFailed(true);
        } finally {
            setLoading(false);
        }
    }, []);

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
            setNote(loaded > 0 ? t('loadedOlder', { count: loaded }) : t('noOlder'));
            if (loaded > 0) setHistoryVersion(v => v + 1);
        } catch {
            setNote(t('loadOlderFailed'));
        } finally {
            setOlderBusy(false);
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
        const canAddContact = !s.needs_assign && (s.type === 'conversation' || s.type === 'unknown') && !!phone && !phone.includes('@');
        return (
            <>
                {s.needs_assign && (
                    <div className="flex items-center gap-2">
                        <input type="tel" value={assignPhone} onChange={e => setAssignPhone(e.target.value)} placeholder={t('numberPlaceholder')} className={cn('w-44', INPUT)} />
                        <button type="button" onClick={() => handleAssign(s.chat_id)} disabled={!assignPhone.trim()} className={BTN_PRIMARY}>{t('assign')}</button>
                    </div>
                )}
                {!s.needs_assign && (
                    <button type="button" onClick={() => handleLoadOlder(s.chat_id)} disabled={olderBusy} className={BTN}>
                        {olderBusy ? t('loadingOlder') : t('loadOlder')}
                    </button>
                )}
                {canAddContact && (
                    <button type="button" onClick={() => handleAddAsContact(s)} disabled={addingContact} className={cn('flex items-center gap-1.5', BTN)}>
                        <UserPlus className="w-4 h-4" />{t('addAsContact')}
                    </button>
                )}
            </>
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
                </div>
                {restartError && <p className="mt-2 text-xs text-[#e08c8c]">{restartError}</p>}
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
            onSelect={(id) => { setSelectedChatId(id); setNote(null); }}
            banner={banner}
            conversationExtra={conversationExtra}
            conversationNote={note}
            settingsTitle={t('settingsTitle')}
            settingsContent={settingsContent}
            settingsOpen={showSettings}
            onSettingsOpenChange={setShowSettings}
        />
    );
}
