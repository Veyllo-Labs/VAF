'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
//
// The Telegram window on the shared channel shell: one chat per paired user or relay
// contact, badges for full access / relay / read-only, settings with the bot, the
// paired users, the relay list and the activity chart.
//
// A relay contact is invited by a one-time link the person opens in Telegram
// (POST/GET api/telegram/relay-pair; vaf/core/channel_pairing.py), so the person chooses
// the account. The add by Telegram id is an admin's (`can_add_by_id`).

import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { useTranslations } from 'next-intl';
import { Send, ExternalLink, Trash2 } from 'lucide-react';
import { cn } from '@/lib/utils';
import { copyText } from '@/lib/clipboard';
import MessagesChart from './MessagesChart';
import ChannelDashboardShell, { BADGE_CLS, BTN, BTN_PRIMARY, INPUT, KvRow, SFC_FILL, SettingsCard, ShellChat } from './ChannelDashboardShell';

const api = (path: string) => path.startsWith('/') ? path : `/${path}`;

export interface TelegramDashboardProps {
    isOpen: boolean;
    onClose: () => void;
    config: any;
    onConfigChange: (key: string, value: any) => void;
    /** Chat to open on arrival (a jump from the contact book); null leaves the selection alone. */
    initialChatId?: string | null;
}

interface TelegramSession {
    chat_id: string;
    telegram_user_id: string;
    telegram_username?: string | null;
    vaf_username?: string | null;
    type: 'admin' | 'relay' | 'unknown';
    last_ts: number;
    message_count: number;
    name?: string | null;
    last_preview?: string;
    unread?: number;
    waits?: boolean;
    waits_reason?: string;
    answered_by_agent?: boolean;
    done?: boolean;
}

interface WhitelistEntry {
    telegram_user_id: string;
    telegram_username?: string | null;
    vaf_username?: string | null;
}

interface DashboardData {
    bot_username: string | null;
    bot_link: string | null;
    sessions: TelegramSession[];
    stats_4h: Array<{ bucket_ts: number; count: number }>;
    admin_whitelist: WhitelistEntry[];
    relay_whitelist: WhitelistEntry[];
    running: boolean;
    /** Whether this account may add a relay contact by its Telegram id (admins). */
    can_add_by_id: boolean;
}

interface RelayInvite {
    command: string;
    link: string | null;
    expiresIn: number;
    /** The relay contacts listed when the invite was made: one not among them is the new one. */
    knownIds: string[];
}

export default function TelegramDashboard({ isOpen, onClose, config, onConfigChange, initialChatId }: TelegramDashboardProps) {
    const t = useTranslations('settings.telegramDashboard');
    const tCommon = useTranslations('common');
    const [data, setData] = useState<DashboardData | null>(null);
    const [loading, setLoading] = useState(false);
    const [loadFailed, setLoadFailed] = useState(false);
    const [selectedChatId, setSelectedChatId] = useState<string | null>(null);
    const [showSettings, setShowSettings] = useState(false);
    const [relayAddId, setRelayAddId] = useState('');
    const [relayAddUsername, setRelayAddUsername] = useState('');
    const [relayError, setRelayError] = useState<string | null>(null);
    const [relayInvite, setRelayInvite] = useState<RelayInvite | null>(null);
    const [relayInviteCopied, setRelayInviteCopied] = useState(false);
    // The invite ended (redeemed for nobody new, refused by the bot, or run out) and no
    // contact was added: said once, instead of the box just turning back into the button.
    const [relayInviteEnded, setRelayInviteEnded] = useState(false);
    // Set the moment Cancel is pressed, before its DELETE goes out: a poll answer that arrives
    // after the withdrawal ("pending: false") must not read as an invite that ended by itself.
    const relayCancelRef = useRef(false);
    // One number per invite flow, raised when an invite starts, is cancelled or the window
    // closes: a reload that finishes after that belongs to a flow nobody looks at any more.
    const relayGenRef = useRef(0);
    const [note, setNote] = useState<string | null>(null);
    // The jump id waiting to be checked against the loaded sessions. A ref, consumed once,
    // so the check runs on the load that follows the jump and not on every later refresh
    // (a refresh would otherwise yank the selection back to the first chat).
    const jumpPendingRef = useRef<string | null>(null);

    /** Reloads the window; resolves to the relay contacts' ids as loaded, or null on a failure. */
    const fetchDashboard = useCallback(async (): Promise<string[] | null> => {
        setLoading(true);
        setLoadFailed(false);
        try {
            const [res, statusRes] = await Promise.all([
                fetch(api('api/telegram/dashboard'), { credentials: 'include' }),
                fetch(api('api/telegram/status'), { credentials: 'include' }).catch(() => null),
            ]);
            const json = await res.json();
            if (!res.ok) { setLoadFailed(true); return null; }
            const status = statusRes && statusRes.ok ? await statusRes.json().catch(() => ({})) : {};
            const sessions: TelegramSession[] = Array.isArray(json.sessions) ? json.sessions : [];
            const relayList: WhitelistEntry[] = Array.isArray(json.relay_whitelist) ? json.relay_whitelist : [];
            setData({
                bot_username: json.bot_username ?? null,
                bot_link: json.bot_link ?? null,
                sessions,
                stats_4h: Array.isArray(json.stats_4h) ? json.stats_4h : [],
                admin_whitelist: Array.isArray(json.admin_whitelist) ? json.admin_whitelist : [],
                relay_whitelist: relayList,
                running: status?.running === true,
                can_add_by_id: json.can_add_by_id === true,
            });
            setSelectedChatId(prev => prev ?? (sessions[0]?.chat_id ?? null));
            const pendingJump = jumpPendingRef.current;
            if (pendingJump) {
                jumpPendingRef.current = null;
                if (!sessions.some(s => s.chat_id === pendingJump)) {
                    setSelectedChatId(sessions[0]?.chat_id ?? null);
                    setNote(t('jumpChatNotFound'));
                }
            }
            return relayList.map(e => String(e.telegram_user_id));
        } catch {
            setLoadFailed(true);
            return null;
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
        setSelectedChatId(initialChatId);
    }, [initialChatId, isOpen]);

    useEffect(() => { if (isOpen) fetchDashboard(); }, [isOpen, fetchDashboard]);
    // A closed window stops waiting but leaves the link live: closing it is what one does after
    // sending the link, and the person may open it later. Cancel is what takes a link back.
    useEffect(() => { if (!isOpen) { relayGenRef.current += 1; setRelayInvite(null); setRelayInviteEnded(false); } }, [isOpen]);

    const startRelayInvite = async () => {
        const relayContacts = data?.relay_whitelist;
        relayGenRef.current += 1;
        relayCancelRef.current = false;
        setRelayError(null);
        setRelayInviteCopied(false);
        setRelayInviteEnded(false);
        try {
            const res = await fetch(api('api/telegram/relay-pair'), { method: 'POST', credentials: 'include' });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                setRelayError(data?.detail || t('relayInviteFailed'));
                return;
            }
            setRelayInvite({ command: data.command, link: data.link ?? null, expiresIn: Number(data.expires_in) || 600,
                knownIds: (relayContacts ?? []).map(e => String(e.telegram_user_id)) });
        } catch {
            setRelayError(t('relayInviteFailed'));
        }
    };

    // While the link is out, ask every few seconds whether the bot has redeemed it; the code
    // runs out on the server, and the wait stops with it.
    useEffect(() => {
        if (!relayInvite) return;
        const started = Date.now();
        let closed = false;
        // The invite is over (redeemed, refused or run out): reload, and say so only when no
        // new contact came with it. Not after Cancel, and not once the window closed.
        const finishInvite = async () => {
            if (closed || relayCancelRef.current) return;
            const gen = relayGenRef.current;
            setRelayInvite(null);
            setRelayError(null);
            const ids = await fetchDashboard();
            if (gen !== relayGenRef.current) return;
            if (ids && !ids.some(id => !relayInvite.knownIds.includes(id))) setRelayInviteEnded(true);
        };
        const timer = setInterval(async () => {
            // The client timer starts after the server's, so a link opened in the last seconds
            // is checked against the reloaded list here as well.
            if (Date.now() - started > relayInvite.expiresIn * 1000) {
                await finishInvite();
                return;
            }
            try {
                const res = await fetch(api('api/telegram/relay-pair'), { credentials: 'include' });
                const data = await res.json();
                if (res.ok && data?.pending === false) await finishInvite();
            } catch { /* keep waiting */ }
        }, 3000);
        return () => { closed = true; clearInterval(timer); };
    }, [relayInvite, fetchDashboard]);

    // Cancel takes the link back on the server (DELETE api/telegram/relay-pair): a link sent to
    // the wrong chat stops working at once instead of at the end of its lifetime.
    const cancelRelayInvite = async () => {
        setRelayError(null);
        relayCancelRef.current = true;
        relayGenRef.current += 1;
        try {
            const res = await fetch(api('api/telegram/relay-pair'), { method: 'DELETE', credentials: 'include' });
            if (!res.ok) {
                const data = await res.json().catch(() => ({}));
                relayCancelRef.current = false;
                setRelayError(data?.detail || t('relayInviteCancelFailed'));
                return;
            }
            setRelayInvite(null);
        } catch {
            relayCancelRef.current = false;
            setRelayError(t('relayInviteCancelFailed'));
        }
    };

    const copyRelayInvite = async () => {
        if (!relayInvite) return;
        setRelayInviteCopied(await copyText(relayInvite.link || relayInvite.command));
    };

    const handleRelayAdd = async () => {
        const id = relayAddId.trim();
        if (!id) return;
        setRelayError(null);
        try {
            const res = await fetch(api('api/telegram/relay-whitelist-add'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({ telegram_user_id: id, telegram_username: relayAddUsername.trim() || undefined }),
            });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                setRelayError(err?.detail || t('addFailed'));
                return;
            }
            setRelayAddId('');
            setRelayAddUsername('');
            onConfigChange('telegram_config', { ...config.telegram_config, relay_whitelist: [...(config.telegram_config?.relay_whitelist || []), { telegram_user_id: id, telegram_username: relayAddUsername.trim() || null }] });
            fetchDashboard();
        } catch {
            setRelayError(t('addFailed'));
        }
    };

    const handleRelayRemove = async (telegram_user_id: string) => {
        try {
            await fetch(api('api/telegram/relay-whitelist-remove'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({ telegram_user_id }),
            });
            const current = config.telegram_config?.relay_whitelist || [];
            onConfigChange('telegram_config', { ...config.telegram_config, relay_whitelist: current.filter((e: any) => String(e.telegram_user_id) !== telegram_user_id) });
            fetchDashboard();
        } catch {
            setRelayError(t('addFailed'));
        }
    };

    const label = (u: { telegram_username?: string | null; telegram_user_id: string }) => u.telegram_username ? `@${u.telegram_username}` : u.telegram_user_id;

    const chats: ShellChat[] = useMemo(() => (data?.sessions || []).map(s => {
        const badge = s.type === 'admin'
            ? { label: t('badgeFull'), cls: BADGE_CLS.owner }
            : s.type === 'relay'
                ? { label: t('badgeRelay'), cls: BADGE_CLS.relay }
                : { label: t('badgeReadOnly'), cls: BADGE_CLS.readOnly };
        const sub = s.type === 'admin' ? t('subFull') : s.type === 'relay' ? t('subRelay') : t('subReadOnly');
        const foot = s.type === 'admin' ? t('footFull') : s.type === 'relay' ? t('footRelay') : t('footReadOnly');
        return {
            id: s.chat_id,
            historyKey: `telegram_${s.chat_id}`,
            label: s.telegram_username ? label(s) : (s.name || s.telegram_user_id),
            preview: s.last_preview || (s.vaf_username ? `${s.telegram_user_id} · ${s.vaf_username}` : s.telegram_user_id),
            ts: s.last_ts,
            badge,
            subline: `${s.telegram_user_id} · ${sub}`,
            footer: foot,
            unread: s.unread,
            waits: s.waits,
            waitsReason: s.waits_reason,
            answeredByAgent: s.answered_by_agent,
            done: s.done,
        };
    }), [data, t]);

    const stateText = data?.running ? t('stateRunning') : t('stateStopped');

    const settingsContent = (
        <>
            <SettingsCard title={t('cardBotTitle')} desc={t('cardBotDesc')}>
                <KvRow
                    left={<><span className={cn('w-2 h-2 rounded-full', data?.running ? 'bg-[#3fbf5f]' : 'bg-gray-400')} />{data?.bot_username ? `@${data.bot_username}` : t('noBot')}</>}
                    right={stateText}
                />
                {data?.bot_link && (
                    <a href={data.bot_link} target="_blank" rel="noopener noreferrer" className={cn('inline-flex items-center gap-1.5', BTN)}>
                        <ExternalLink className="w-4 h-4" />{t('openBot')}
                    </a>
                )}
            </SettingsCard>

            <SettingsCard title={t('cardFullTitle')} desc={t('cardFullDesc')}>
                {(data?.admin_whitelist || []).map((e, i) => (
                    <KvRow key={i} left={label(e)} right={e.vaf_username || e.telegram_user_id} />
                ))}
                {(!data?.admin_whitelist || data.admin_whitelist.length === 0) && <p className="text-[12.5px] text-gray-500">{t('noneFull')}</p>}
            </SettingsCard>

            <SettingsCard title={t('cardRelayTitle')} desc={t('cardRelayDesc')}>
                {(data?.relay_whitelist || []).map((e, i) => (
                    <KvRow key={i} left={label(e)} right={<>
                        {/* An admin's list holds every account's relay contacts: say whose each one is. */}
                        {data?.can_add_by_id && e.vaf_username && (
                            <span className="max-w-[7rem] max-md:max-w-[4.5rem] truncate" title={t('relayAccount', { name: e.vaf_username })}>{t('relayAccount', { name: e.vaf_username })}</span>
                        )}
                        <span>{e.telegram_user_id}</span>
                        <button type="button" title={t('remove')} onClick={() => { if (confirm(t('removeRelayConfirm'))) handleRelayRemove(e.telegram_user_id); }}
                            className="p-1 rounded hover:bg-red-100 text-gray-500 hover:text-red-600"><Trash2 className="w-3.5 h-3.5" /></button>
                    </>} />
                ))}
                {(!data?.relay_whitelist || data.relay_whitelist.length === 0) && <p className="text-[12.5px] text-gray-500 mb-2">{t('noneRelay')}</p>}
                {!relayInvite ? (
                    <>
                        {relayInviteEnded && <p className="text-xs text-gray-600 mb-2">{t('relayInviteEnded')}</p>}
                        <button type="button" onClick={startRelayInvite} className={cn(BTN_PRIMARY, 'mb-2')}>{t('relayInvite')}</button>
                    </>
                ) : (
                    <div className={cn(SFC_FILL, 'rounded-lg p-2.5 mb-2 space-y-2 text-[13px]')}>
                        <p className="text-gray-700">{t('relayInviteHint', { minutes: Math.round(relayInvite.expiresIn / 60) })}</p>
                        {relayInvite.link && (
                            <code className="block font-mono text-xs break-all select-all">{relayInvite.link}</code>
                        )}
                        <code className="block font-mono text-xs break-all select-all text-gray-600">{relayInvite.command}</code>
                        <div className="flex flex-wrap items-center gap-2">
                            <button type="button" onClick={copyRelayInvite} className={BTN}>{relayInviteCopied ? t('relayInviteCopied') : t('relayInviteCopy')}</button>
                            <button type="button" onClick={cancelRelayInvite} className={BTN}>{tCommon('cancel')}</button>
                        </div>
                        <p className="text-xs text-gray-600">{t('relayInviteWaiting')}</p>
                    </div>
                )}
                {data?.can_add_by_id && (
                    <div className="flex gap-2 flex-wrap">
                        <input type="text" placeholder={t('relayIdPlaceholder')} value={relayAddId} onChange={e => setRelayAddId(e.target.value)} className={cn('flex-1 min-w-[10rem]', INPUT)} />
                        <input type="text" placeholder={t('relayUserPlaceholder')} value={relayAddUsername} onChange={e => setRelayAddUsername(e.target.value)} className={cn('flex-1 min-w-[10rem]', INPUT)} />
                        <button type="button" onClick={handleRelayAdd} disabled={!relayAddId.trim()} className={BTN_PRIMARY}>{t('add')}</button>
                    </div>
                )}
                {relayError && <p className="mt-2 text-xs text-red-600">{relayError}</p>}
            </SettingsCard>

            <SettingsCard title={t('cardActivityTitle')} full>
                <MessagesChart buckets={data?.stats_4h ?? []} chartId="telegram-messages-chart" />
            </SettingsCard>
        </>
    );

    return (
        <ChannelDashboardShell
            isOpen={isOpen}
            onClose={onClose}
            icon={<Send className="w-4 h-4 text-white" />}
            iconClass="bg-[#2aabee]"
            title={t('title')}
            subtitle={<>{t('bot')} <span className="text-gray-800">{data?.bot_username ? `@${data.bot_username}` : t('noBot')}</span></>}
            dot={data?.running ? 'green' : 'gray'}
            dotTitle={stateText}
            chats={chats}
            loading={loading}
            loadFailed={loadFailed}
            onRefresh={async () => { await fetchDashboard(); }}
            historyUrl={(sid) => `api/telegram/session/${encodeURIComponent(sid)}/history`}
            selectedId={selectedChatId}
            onSelect={(id) => { setSelectedChatId(id); setNote(null); }}
            conversationNote={note}
            settingsTitle={t('settingsTitle')}
            settingsContent={settingsContent}
            settingsOpen={showSettings}
            onSettingsOpenChange={setShowSettings}
            channel="telegram"
        />
    );
}
