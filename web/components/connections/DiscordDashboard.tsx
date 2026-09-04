'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
//
// The Discord window on the shared channel shell. Discord is a single-admin
// integration: one chat, the paired admin's direct message; settings with the bot
// (Developer Portal), the bridge switch, the admin and the recent activity.

import React, { useState, useEffect, useCallback, useMemo } from 'react';
import { useTranslations } from 'next-intl';
import { MessageCircle, ExternalLink, Power, Loader2 } from 'lucide-react';
import { cn } from '@/lib/utils';
import ChannelDashboardShell, { BADGE_CLS, BTN, KvRow, SettingsCard, ShellChat, fmtWhen } from './ChannelDashboardShell';

const api = (path: string) => path.startsWith('/') ? path : `/${path}`;

export interface DiscordDashboardProps {
    isOpen: boolean;
    onClose: () => void;
    config: any;
    onConfigChange: (key: string, value: any) => void;
}

interface ActivityItem {
    chat_id: string;
    ts: number;
    direction: string;
}

interface DashboardData {
    configured: boolean;
    running: boolean;
    admin_username?: string | null;
    admin_user_id?: string | null;
    enabled: boolean;
    activity: ActivityItem[];
}

export default function DiscordDashboard({ isOpen, onClose, config, onConfigChange }: DiscordDashboardProps) {
    const t = useTranslations('settings.discordDashboard');
    const [data, setData] = useState<DashboardData | null>(null);
    const [loading, setLoading] = useState(false);
    const [loadFailed, setLoadFailed] = useState(false);
    const [toggling, setToggling] = useState(false);
    const [selectedChatId, setSelectedChatId] = useState<string | null>(null);
    const [showSettings, setShowSettings] = useState(false);

    const fetchDashboard = useCallback(async () => {
        setLoading(true);
        setLoadFailed(false);
        try {
            const res = await fetch(api('api/discord/dashboard'), { credentials: 'include' });
            const json = await res.json();
            if (!res.ok) { setLoadFailed(true); return; }
            setData({
                configured: json.configured ?? false,
                running: json.running ?? false,
                admin_username: json.admin_username ?? null,
                admin_user_id: json.admin_user_id ?? null,
                enabled: json.enabled ?? false,
                activity: Array.isArray(json.activity) ? json.activity : [],
            });
            if (json.admin_user_id) setSelectedChatId(prev => prev ?? String(json.admin_user_id));
        } catch {
            setLoadFailed(true);
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => { if (isOpen) fetchDashboard(); }, [isOpen, config?.discord_config, fetchDashboard]);

    const handleToggle = async () => {
        if (!data) return;
        setToggling(true);
        try {
            const enable = !data.running;
            await fetch(api(enable ? 'api/discord/start' : 'api/discord/stop'), { method: 'POST', credentials: 'include' });
            const dc = config?.discord_config || {};
            onConfigChange('discord_config', { ...dc, enabled: enable });
            await fetchDashboard();
        } finally {
            setToggling(false);
        }
    };

    const chats: ShellChat[] = useMemo(() => {
        if (!data?.admin_user_id) return [];
        const newest = data.activity.length ? data.activity[data.activity.length - 1] : null;
        return [{
            id: String(data.admin_user_id),
            historyKey: `discord_${data.admin_user_id}`,
            label: data.admin_username ? `@${data.admin_username}` : String(data.admin_user_id),
            preview: newest ? (newest.direction === 'in' ? t('incoming') : t('outgoing')) : '',
            ts: newest?.ts,
            badge: { label: t('badgeAdmin'), cls: BADGE_CLS.owner },
            subline: `${data.admin_user_id} · ${t('subAdmin')}`,
            footer: t('footAdmin'),
        }];
    }, [data, t]);

    const stateText = data?.running ? t('stateRunning') : t('stateStopped');

    const settingsContent = (
        <>
            <SettingsCard title={t('cardBotTitle')} desc={t('cardBotDesc')}>
                <a href="https://discord.com/developers/applications/" target="_blank" rel="noopener noreferrer" className={cn('inline-flex items-center gap-1.5', BTN)}>
                    <ExternalLink className="w-4 h-4" />{t('developerPortal')}
                </a>
            </SettingsCard>

            <SettingsCard title={t('cardBridgeTitle')}>
                <KvRow left={<><span className={cn('w-2 h-2 rounded-full', data?.running ? 'bg-[#3fbf5f]' : 'bg-[#555]')} />{stateText}</>} />
                <button type="button" onClick={handleToggle} disabled={toggling || !data?.configured} className={cn('flex items-center gap-1.5', BTN)}>
                    {toggling ? <Loader2 className="w-4 h-4 animate-spin" /> : <Power className="w-4 h-4" />}
                    {toggling ? t('working') : data?.running ? t('stopBridge') : t('startBridge')}
                </button>
            </SettingsCard>

            <SettingsCard title={t('cardAdminTitle')} desc={t('cardAdminDesc')}>
                {data?.admin_user_id
                    ? <KvRow left={data.admin_username ? `@${data.admin_username}` : String(data.admin_user_id)} right={String(data.admin_user_id)} />
                    : <p className="text-[12.5px] text-[#9a9a9a]">{t('noAdmin')}</p>}
            </SettingsCard>

            <SettingsCard title={t('cardActivityTitle')}>
                {(data?.activity?.length ?? 0) === 0 ? (
                    <p className="text-[12.5px] text-[#9a9a9a]">{t('noActivity')}</p>
                ) : [...(data?.activity ?? [])].reverse().slice(0, 7).map((a, i) => (
                    <KvRow key={i} left={<><MessageCircle className="w-3.5 h-3.5 text-[#9a9a9a]" />{a.direction === 'in' ? t('incoming') : t('outgoing')}</>} right={fmtWhen(a.ts)} />
                ))}
            </SettingsCard>
        </>
    );

    return (
        <ChannelDashboardShell
            isOpen={isOpen}
            onClose={onClose}
            icon={<MessageCircle className="w-4 h-4 text-white" />}
            iconClass="bg-[#5865f2]"
            title={t('title')}
            subtitle={<>{t('cardAdminTitle')} <span className="text-[#d0d0d0]">{data?.admin_username ? `@${data.admin_username}` : t('noAdmin')}</span></>}
            dot={data?.running ? 'green' : 'gray'}
            dotTitle={stateText}
            chats={chats}
            loading={loading}
            loadFailed={loadFailed}
            onRefresh={fetchDashboard}
            historyUrl={(sid) => `api/discord/session/${encodeURIComponent(sid)}/history`}
            selectedId={selectedChatId}
            onSelect={setSelectedChatId}
            settingsTitle={t('settingsTitle')}
            settingsContent={settingsContent}
            settingsOpen={showSettings}
            onSettingsOpenChange={setShowSettings}
        />
    );
}
