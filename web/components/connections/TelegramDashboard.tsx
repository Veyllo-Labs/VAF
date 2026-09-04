'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
//
// The Telegram window on the shared channel shell: one chat per paired user or relay
// contact, badges for full access / relay / read-only, settings with the bot, the
// paired users, the relay list and the activity chart.

import React, { useState, useEffect, useCallback, useMemo } from 'react';
import { useTranslations } from 'next-intl';
import { Send, ExternalLink, Trash2 } from 'lucide-react';
import { cn } from '@/lib/utils';
import MessagesChart from './MessagesChart';
import ChannelDashboardShell, { BADGE_CLS, BTN, BTN_PRIMARY, INPUT, KvRow, SettingsCard, ShellChat } from './ChannelDashboardShell';

const api = (path: string) => path.startsWith('/') ? path : `/${path}`;

export interface TelegramDashboardProps {
    isOpen: boolean;
    onClose: () => void;
    config: any;
    onConfigChange: (key: string, value: any) => void;
}

interface TelegramSession {
    chat_id: string;
    telegram_user_id: string;
    telegram_username?: string | null;
    vaf_username?: string | null;
    type: 'admin' | 'relay' | 'unknown';
    last_ts: number;
    message_count: number;
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
}

export default function TelegramDashboard({ isOpen, onClose, config, onConfigChange }: TelegramDashboardProps) {
    const t = useTranslations('settings.telegramDashboard');
    const [data, setData] = useState<DashboardData | null>(null);
    const [loading, setLoading] = useState(false);
    const [loadFailed, setLoadFailed] = useState(false);
    const [selectedChatId, setSelectedChatId] = useState<string | null>(null);
    const [showSettings, setShowSettings] = useState(false);
    const [relayAddId, setRelayAddId] = useState('');
    const [relayAddUsername, setRelayAddUsername] = useState('');
    const [relayError, setRelayError] = useState<string | null>(null);

    const fetchDashboard = useCallback(async () => {
        setLoading(true);
        setLoadFailed(false);
        try {
            const [res, statusRes] = await Promise.all([
                fetch(api('api/telegram/dashboard'), { credentials: 'include' }),
                fetch(api('api/telegram/status'), { credentials: 'include' }).catch(() => null),
            ]);
            const json = await res.json();
            if (!res.ok) { setLoadFailed(true); return; }
            const status = statusRes && statusRes.ok ? await statusRes.json().catch(() => ({})) : {};
            const sessions: TelegramSession[] = Array.isArray(json.sessions) ? json.sessions : [];
            setData({
                bot_username: json.bot_username ?? null,
                bot_link: json.bot_link ?? null,
                sessions,
                stats_4h: Array.isArray(json.stats_4h) ? json.stats_4h : [],
                admin_whitelist: Array.isArray(json.admin_whitelist) ? json.admin_whitelist : [],
                relay_whitelist: Array.isArray(json.relay_whitelist) ? json.relay_whitelist : [],
                running: status?.running === true,
            });
            setSelectedChatId(prev => prev ?? (sessions[0]?.chat_id ?? null));
        } catch {
            setLoadFailed(true);
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => { if (isOpen) fetchDashboard(); }, [isOpen, fetchDashboard]);

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
                ? { label: t('badgeRelay'), cls: BADGE_CLS.conversation }
                : { label: t('badgeReadOnly'), cls: BADGE_CLS.readOnly };
        const sub = s.type === 'admin' ? t('subFull') : s.type === 'relay' ? t('subRelay') : t('subReadOnly');
        const foot = s.type === 'admin' ? t('footFull') : s.type === 'relay' ? t('footRelay') : t('footReadOnly');
        return {
            id: s.chat_id,
            historyKey: `telegram_${s.chat_id}`,
            label: label(s),
            preview: s.vaf_username ? `${s.telegram_user_id} · ${s.vaf_username}` : s.telegram_user_id,
            ts: s.last_ts,
            badge,
            subline: `${s.telegram_user_id} · ${sub}`,
            footer: foot,
        };
    }), [data, t]);

    const stateText = data?.running ? t('stateRunning') : t('stateStopped');

    const settingsContent = (
        <>
            <SettingsCard title={t('cardBotTitle')} desc={t('cardBotDesc')}>
                <KvRow
                    left={<><span className={cn('w-2 h-2 rounded-full', data?.running ? 'bg-[#3fbf5f]' : 'bg-[#555]')} />{data?.bot_username ? `@${data.bot_username}` : t('noBot')}</>}
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
                {(!data?.admin_whitelist || data.admin_whitelist.length === 0) && <p className="text-[12.5px] text-[#9a9a9a]">{t('noneFull')}</p>}
            </SettingsCard>

            <SettingsCard title={t('cardRelayTitle')} desc={t('cardRelayDesc')}>
                {(data?.relay_whitelist || []).map((e, i) => (
                    <KvRow key={i} left={label(e)} right={<>
                        <span>{e.telegram_user_id}</span>
                        <button type="button" title={t('remove')} onClick={() => { if (confirm(t('removeRelayConfirm'))) handleRelayRemove(e.telegram_user_id); }}
                            className="p-1 rounded hover:bg-[#3a1d1d] text-[#9a9a9a] hover:text-[#e08c8c]"><Trash2 className="w-3.5 h-3.5" /></button>
                    </>} />
                ))}
                {(!data?.relay_whitelist || data.relay_whitelist.length === 0) && <p className="text-[12.5px] text-[#9a9a9a] mb-2">{t('noneRelay')}</p>}
                <div className="flex gap-2 flex-wrap">
                    <input type="text" placeholder={t('relayIdPlaceholder')} value={relayAddId} onChange={e => setRelayAddId(e.target.value)} className={cn('flex-1 min-w-[10rem]', INPUT)} />
                    <input type="text" placeholder={t('relayUserPlaceholder')} value={relayAddUsername} onChange={e => setRelayAddUsername(e.target.value)} className={cn('flex-1 min-w-[10rem]', INPUT)} />
                    <button type="button" onClick={handleRelayAdd} disabled={!relayAddId.trim()} className={BTN_PRIMARY}>{t('add')}</button>
                </div>
                {relayError && <p className="mt-2 text-xs text-[#e08c8c]">{relayError}</p>}
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
            subtitle={<>{t('bot')} <span className="text-[#d0d0d0]">{data?.bot_username ? `@${data.bot_username}` : t('noBot')}</span></>}
            dot={data?.running ? 'green' : 'gray'}
            dotTitle={stateText}
            chats={chats}
            loading={loading}
            loadFailed={loadFailed}
            onRefresh={fetchDashboard}
            historyUrl={(sid) => `api/telegram/session/${encodeURIComponent(sid)}/history`}
            selectedId={selectedChatId}
            onSelect={setSelectedChatId}
            settingsTitle={t('settingsTitle')}
            settingsContent={settingsContent}
            settingsOpen={showSettings}
            onSettingsOpenChange={setShowSettings}
        />
    );
}
