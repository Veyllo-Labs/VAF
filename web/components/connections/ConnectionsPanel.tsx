'use client';

import React, { useState, useEffect } from 'react';
import {
    MessageCircle, Phone, Mail, Slack, Plus, Settings,
    CheckCircle2, XCircle, Loader2, Trash2, Power,
    Calendar, Cloud, HardDrive, FolderSync, Users,
    Video, Gamepad2, Building2, ShoppingBag, Briefcase
} from 'lucide-react';
import { cn } from '@/lib/utils';

interface ConnectionsPanelProps {
    config: any;
    onConfigChange: (key: string, value: any) => void;
    /** Current user for per-user cloud config (admin uses cloud_config, others use cloud_config_by_user) */
    currentUser?: { username?: string } | null;
    /** Bump to refetch cloud accounts (e.g. when cloud wizard completes) */
    refreshTrigger?: number;
    onOpenDiscordWizard: () => void;
    onOpenDiscordDashboard?: () => void;
    onOpenTelegramWizard: () => void;
    onOpenWhatsAppWizard?: () => void;
    onOpenWhatsAppDashboard?: () => void;
    onOpenTelegramDashboard?: () => void;
    onOpenEmailDashboard?: () => void;
    onOpenEmailWizard?: () => void;
    onOpenCloudDashboard?: () => void;
    onOpenCloudWizard?: (provider?: string) => void;
    onOpenContactsDashboard?: () => void;
    /** Open calendar setup wizard (reuses Email OAuth). When provided, Connect on Google/Outlook calendar uses this; otherwise falls back to onOpenEmailWizard. */
    onOpenCalendarWizard?: (provider?: 'google_calendar' | 'outlook_calendar') => void;
    /** Open calendar dashboard (accounts left, events in the middle). When provided and calendar is configured, Settings opens this. */
    onOpenCalendarDashboard?: () => void;
}

export interface ConnectionApp {
    id: string;
    name: string;
    icon: React.ElementType;
    category: 'contacts' | 'communication' | 'calendar' | 'cloud' | 'productivity' | 'social';
    description: string;
    configKey: string;
    available: boolean;
    comingSoon?: boolean;
    iconColor?: string;
}

export const CONNECTION_APPS: ConnectionApp[] = [
    // ============ Contacts (own category, top) ============
    {
        id: 'contacts',
        name: 'Contacts',
        icon: Users,
        category: 'contacts',
        description: 'Central contact list with personal file and assistant whitelist',
        configKey: 'contacts',
        available: true,
        comingSoon: false,
        iconColor: 'bg-gray-600',
    },

    // ============ Communication ============
    {
        id: 'discord',
        name: 'Discord',
        icon: MessageCircle,
        category: 'communication',
        description: 'Chat with your agent via Discord DMs or channels',
        configKey: 'discord_config',
        available: true,
        iconColor: 'bg-indigo-600',
    },
    {
        id: 'telegram',
        name: 'Telegram',
        icon: MessageCircle,
        category: 'communication',
        description: 'Message VAF from Telegram; VAF can reach you there',
        configKey: 'telegram_config',
        available: true,
        iconColor: 'bg-sky-500',
    },
    {
        id: 'slack',
        name: 'Slack',
        icon: Slack,
        category: 'communication',
        description: 'Integrate VAF into your Slack workspace',
        configKey: 'slack_config',
        available: false,
        comingSoon: true,
        iconColor: 'bg-purple-600',
    },
    {
        id: 'signal',
        name: 'Signal',
        icon: MessageCircle,
        category: 'communication',
        description: 'Chat with your agent via Signal',
        configKey: 'signal_config',
        available: false,
        comingSoon: true,
        iconColor: 'bg-cyan-600',
    },
    {
        id: 'whatsapp',
        name: 'WhatsApp',
        icon: Phone,
        category: 'communication',
        description: 'Chat with your agent on WhatsApp',
        configKey: 'whatsapp_config',
        available: true,
        comingSoon: false,
        iconColor: 'bg-green-600',
    },
    {
        id: 'email',
        name: 'Email',
        icon: Mail,
        category: 'communication',
        description: 'Receive and respond to emails automatically',
        configKey: 'email_config',
        available: true,
        comingSoon: false,
        iconColor: 'bg-red-500',
    },
    {
        id: 'microsoft_teams',
        name: 'Microsoft Teams',
        icon: MessageCircle,
        category: 'communication',
        description: 'Bot Framework integration for Teams',
        configKey: 'teams_config',
        available: false,
        comingSoon: true,
        iconColor: 'bg-indigo-700',
    },
    {
        id: 'matrix',
        name: 'Matrix (Element)',
        icon: MessageCircle,
        category: 'communication',
        description: 'Open-source chat protocol',
        configKey: 'matrix_config',
        available: false,
        comingSoon: true,
        iconColor: 'bg-green-700',
    },
    {
        id: 'irc',
        name: 'IRC',
        icon: MessageCircle,
        category: 'communication',
        description: 'Classic IRC for communities',
        configKey: 'irc_config',
        available: false,
        comingSoon: true,
        iconColor: 'bg-emerald-600',
    },
    // ============ Calendar ============
    {
        id: 'google_calendar',
        name: 'Google Calendar',
        icon: Calendar,
        category: 'calendar',
        description: 'Sync events, create reminders, and manage your Google Calendar',
        configKey: 'google_calendar_config',
        available: true,
        comingSoon: false,
        iconColor: 'bg-blue-500',
    },
    {
        id: 'outlook_calendar',
        name: 'Microsoft Outlook',
        icon: Calendar,
        category: 'calendar',
        description: 'Connect to Outlook/Microsoft 365 calendar',
        configKey: 'outlook_calendar_config',
        available: true,
        comingSoon: false,
        iconColor: 'bg-blue-600',
    },
    {
        id: 'apple_calendar',
        name: 'Apple Calendar',
        icon: Calendar,
        category: 'calendar',
        description: 'Sync with iCloud Calendar on macOS',
        configKey: 'apple_calendar_config',
        available: false,
        comingSoon: true,
        iconColor: 'bg-gray-600',
    },
    {
        id: 'caldav',
        name: 'CalDAV (Local)',
        icon: Calendar,
        category: 'calendar',
        description: 'Connect to any CalDAV server (Nextcloud, etc.)',
        configKey: 'caldav_config',
        available: false,
        comingSoon: true,
        iconColor: 'bg-orange-500',
    },
    {
        id: 'calendly',
        name: 'Calendly',
        icon: Calendar,
        category: 'calendar',
        description: 'Appointment scheduling and booking',
        configKey: 'calendly_config',
        available: false,
        comingSoon: true,
        iconColor: 'bg-blue-400',
    },

    // ============ Cloud Storage ============
    {
        id: 'google_drive',
        name: 'Google Drive',
        icon: Cloud,
        category: 'cloud',
        description: 'Sync files with Google Drive via VAF Sync folder',
        configKey: 'cloud_config',
        available: true,
        comingSoon: false,
        iconColor: 'bg-yellow-500',
    },
    {
        id: 'onedrive',
        name: 'Microsoft OneDrive',
        icon: Cloud,
        category: 'cloud',
        description: 'Sync files with OneDrive via VAF Sync folder',
        configKey: 'cloud_config',
        available: true,
        comingSoon: false,
        iconColor: 'bg-blue-500',
    },
    {
        id: 'icloud',
        name: 'Apple iCloud',
        icon: Cloud,
        category: 'cloud',
        description: 'Sync files via iCloud Drive (macOS only)',
        configKey: 'cloud_config',
        available: true,
        comingSoon: false,
        iconColor: 'bg-sky-400',
    },
    {
        id: 'dropbox',
        name: 'Dropbox',
        icon: FolderSync,
        category: 'cloud',
        description: 'Sync files with Dropbox via VAF Sync folder',
        configKey: 'cloud_config',
        available: true,
        comingSoon: false,
        iconColor: 'bg-blue-600',
    },
    {
        id: 'nextcloud',
        name: 'Nextcloud',
        icon: HardDrive,
        category: 'cloud',
        description: 'Sync files with self-hosted Nextcloud via WebDAV',
        configKey: 'cloud_config',
        available: true,
        comingSoon: false,
        iconColor: 'bg-cyan-600',
    },
    // ============ Social ============
    {
        id: 'linkedin',
        name: 'LinkedIn',
        icon: Briefcase,
        category: 'social',
        description: 'Messaging API for professional contacts and lead generation',
        configKey: 'linkedin_config',
        available: false,
        comingSoon: true,
        iconColor: 'bg-blue-700',
    },
    {
        id: 'x_twitter',
        name: 'X (Twitter)',
        icon: MessageCircle,
        category: 'social',
        description: 'Twitter API v2 for DMs, mentions, and tweet interactions',
        configKey: 'twitter_config',
        available: false,
        comingSoon: true,
        iconColor: 'bg-gray-800',
    },
    {
        id: 'facebook_messenger',
        name: 'Facebook Messenger',
        icon: MessageCircle,
        category: 'social',
        description: 'Messenger Platform (similar to Instagram)',
        configKey: 'facebook_messenger_config',
        available: false,
        comingSoon: true,
        iconColor: 'bg-blue-600',
    },
    {
        id: 'reddit',
        name: 'Reddit',
        icon: MessageCircle,
        category: 'social',
        description: 'Reddit API for PMs, comments, and subreddit moderation',
        configKey: 'reddit_config',
        available: false,
        comingSoon: true,
        iconColor: 'bg-orange-600',
    },
    {
        id: 'youtube',
        name: 'YouTube',
        icon: Video,
        category: 'social',
        description: 'YouTube Data API for comments and Community tab',
        configKey: 'youtube_config',
        available: false,
        comingSoon: true,
        iconColor: 'bg-red-600',
    },
    {
        id: 'twitch',
        name: 'Twitch',
        icon: Video,
        category: 'social',
        description: 'Twitch API for chat bots and subscriber messages',
        configKey: 'twitch_config',
        available: false,
        comingSoon: true,
        iconColor: 'bg-purple-600',
    },
    {
        id: 'steam',
        name: 'Steam',
        icon: Gamepad2,
        category: 'social',
        description: 'Steam Chat API for gaming community',
        configKey: 'steam_config',
        available: false,
        comingSoon: true,
        iconColor: 'bg-gray-700',
    },
    // ============ Productivity / Business ============
    {
        id: 'hubspot',
        name: 'HubSpot',
        icon: Building2,
        category: 'productivity',
        description: 'CRM integration via HubSpot API',
        configKey: 'hubspot_config',
        available: false,
        comingSoon: true,
        iconColor: 'bg-orange-500',
    },
    {
        id: 'salesforce',
        name: 'Salesforce',
        icon: Building2,
        category: 'productivity',
        description: 'CRM integration via Salesforce API',
        configKey: 'salesforce_config',
        available: false,
        comingSoon: true,
        iconColor: 'bg-blue-600',
    },
    {
        id: 'shopify',
        name: 'Shopify',
        icon: ShoppingBag,
        category: 'productivity',
        description: 'Customer support and order updates',
        configKey: 'shopify_config',
        available: false,
        comingSoon: true,
        iconColor: 'bg-green-600',
    },
];

export const CATEGORIES = [
    { id: 'contacts', label: 'Contacts', description: 'Central contact list with personal file' },
    { id: 'communication', label: 'Communication', description: 'Messaging & chat platforms' },
    { id: 'calendar', label: 'Calendar', description: 'Scheduling & event management' },
    { id: 'cloud', label: 'Cloud Storage', description: 'File sync & cloud drives' },
    { id: 'productivity', label: 'Productivity', description: 'Work tools & integrations' },
    { id: 'social', label: 'Social', description: 'Social media platforms' },
];

/** Use relative /api/ so Next.js rewrites to backend. */
const api = (path: string) => path.startsWith('/') ? path : `/${path}`;

export default function ConnectionsPanel({ config, onConfigChange, currentUser, refreshTrigger = 0, onOpenDiscordWizard, onOpenDiscordDashboard, onOpenTelegramWizard, onOpenWhatsAppWizard, onOpenWhatsAppDashboard, onOpenTelegramDashboard, onOpenEmailDashboard, onOpenEmailWizard, onOpenCloudDashboard, onOpenCloudWizard, onOpenContactsDashboard, onOpenCalendarWizard, onOpenCalendarDashboard }: ConnectionsPanelProps) {
    const [connectionStatus, setConnectionStatus] = useState<Record<string, 'connected' | 'linked' | 'disconnected' | 'checking'>>({});
    /** Cloud accounts from API (source of truth; config can be stale after OAuth) */
    const [cloudAccountsFromApi, setCloudAccountsFromApi] = useState<any[]>([]);
    /** Email accounts from API (source of truth; config only has legacy email_config, not email_config_by_user) */
    const [emailAccountsFromApi, setEmailAccountsFromApi] = useState<any[]>([]);
    /** Calendar status from API (google_available, microsoft_available from connected email accounts) */
    const [calendarStatus, setCalendarStatus] = useState<{ google_available: boolean; microsoft_available: boolean }>({ google_available: false, microsoft_available: false });

    useEffect(() => {
        checkConnectionStatus();
    }, [config, refreshTrigger]);

    const fetchEmailAccounts = async () => {
        try {
            const res = await fetch(api('api/email/accounts'), { credentials: 'include' });
            const data = await res.json();
            const accounts = data?.accounts ?? [];
            setEmailAccountsFromApi(Array.isArray(accounts) ? accounts : []);
            setConnectionStatus(prev => ({
                ...prev,
                email: (Array.isArray(accounts) && accounts.length > 0) ? 'connected' : 'disconnected'
            }));
        } catch {
            setEmailAccountsFromApi([]);
            setConnectionStatus(prev => ({ ...prev, email: 'disconnected' }));
        }
    };

    const fetchCloudAccounts = async () => {
        try {
            const res = await fetch(api('api/cloud/accounts'), { credentials: 'include' });
            if (res.ok) {
                const data = await res.json();
                setCloudAccountsFromApi(data.accounts || []);
            }
            // On error: keep previous list so a transient API/network failure doesn't hide existing connections
        } catch {
            // Keep previous cloudAccountsFromApi; do not set to []
        }
    };

    const fetchCalendarStatus = async () => {
        try {
            const res = await fetch(api('api/calendar/status'), { credentials: 'include' });
            if (res.ok) {
                const data = await res.json();
                setCalendarStatus({
                    google_available: !!data.google_available,
                    microsoft_available: !!data.microsoft_available,
                });
            }
        } catch {
            setCalendarStatus({ google_available: false, microsoft_available: false });
        }
    };

    const checkConnectionStatus = async () => {
        if (config.discord_config?.verified) {
            setConnectionStatus(prev => ({ ...prev, discord: 'checking' }));
            try {
                const res = await fetch(api('api/discord/status'), { credentials: 'include' });
                const status = await res.json();
                setConnectionStatus(prev => ({ ...prev, discord: status.running ? 'connected' : 'disconnected' }));
            } catch {
                setConnectionStatus(prev => ({ ...prev, discord: 'disconnected' }));
            }
        } else {
            setConnectionStatus(prev => ({ ...prev, discord: 'disconnected' }));
        }
        if (config.telegram_config?.verified) {
            setConnectionStatus(prev => ({ ...prev, telegram: 'checking' }));
            try {
                const res = await fetch(api('api/telegram/status'), { credentials: 'include' });
                const status = await res.json();
                setConnectionStatus(prev => ({ ...prev, telegram: status.running ? 'connected' : 'disconnected' }));
            } catch {
                setConnectionStatus(prev => ({ ...prev, telegram: 'disconnected' }));
            }
        } else {
            setConnectionStatus(prev => ({ ...prev, telegram: 'disconnected' }));
        }
        if (config.whatsapp_config?.enabled) {
            setConnectionStatus(prev => ({ ...prev, whatsapp: 'checking' }));
            try {
                const res = await fetch(api('api/whatsapp/status'), { credentials: 'include' });
                const status = await res.json();
                setConnectionStatus(prev => ({
                    ...prev,
                    whatsapp: status.linked && status.running ? 'connected' : status.linked ? 'linked' : 'disconnected',
                }));
            } catch {
                setConnectionStatus(prev => ({ ...prev, whatsapp: 'disconnected' }));
            }
        } else {
            setConnectionStatus(prev => ({ ...prev, whatsapp: 'disconnected' }));
        }
        await fetchEmailAccounts();
        await fetchCloudAccounts();
        await fetchCalendarStatus();
    };

    const handleToggleConnection = async (appId: string, enabled: boolean) => {
        if (appId === 'discord') {
            const currentConfig = config.discord_config || {};
            onConfigChange('discord_config', { ...currentConfig, enabled });
            try {
                if (enabled) {
                    await fetch(api('api/discord/start'), { method: 'POST', credentials: 'include' });
                } else {
                    await fetch(api('api/discord/stop'), { method: 'POST', credentials: 'include' });
                }
                setConnectionStatus(prev => ({ ...prev, discord: enabled ? 'connected' : 'disconnected' }));
            } catch (e) {
                console.error('Failed to toggle Discord:', e);
            }
        }
        if (appId === 'telegram') {
            const currentConfig = config.telegram_config || {};
            onConfigChange('telegram_config', { ...currentConfig, enabled });
            try {
                if (enabled) {
                    await fetch(api('api/telegram/start'), { method: 'POST', credentials: 'include' });
                } else {
                    await fetch(api('api/telegram/stop'), { method: 'POST', credentials: 'include' });
                }
                setConnectionStatus(prev => ({ ...prev, telegram: enabled ? 'connected' : 'disconnected' }));
            } catch (e) {
                console.error('Failed to toggle Telegram:', e);
            }
        }
        if (appId === 'whatsapp') {
            const currentConfig = config.whatsapp_config || {};
            onConfigChange('whatsapp_config', { ...currentConfig, enabled });
            try {
                // Persist enabled so backend has correct state (start checks it; stop avoids auto-start on restart)
                await fetch(api('api/config'), {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ whatsapp_config: { ...currentConfig, enabled } }),
                    credentials: 'include',
                });
                if (enabled) {
                    await fetch(api('api/whatsapp/start'), { method: 'POST', credentials: 'include' });
                } else {
                    await fetch(api('api/whatsapp/stop'), { method: 'POST', credentials: 'include' });
                }
                setConnectionStatus(prev => ({ ...prev, whatsapp: enabled ? 'connected' : 'disconnected' }));
                await checkConnectionStatus();
            } catch (e) {
                console.error('Failed to toggle WhatsApp:', e);
            }
        }
    };

    const handleDisconnect = async (appId: string) => {
        if (appId === 'google_calendar' || appId === 'outlook_calendar') {
            const msg = appId === 'google_calendar'
                ? 'Calendar uses your Gmail account. To disconnect, remove the Gmail account under Email.'
                : 'Calendar uses your Outlook account. To disconnect, remove the Outlook account under Email.';
            alert(msg);
            return;
        }
        const app = CONNECTION_APPS.find(a => a.id === appId);
        const appName = app?.name ?? appId;
        if (!confirm(`Are you sure you want to disconnect ${appName}? This will remove the connection and associated data.`)) return;
        if (appId === 'discord') {
            onConfigChange('discord_config', null);
            setConnectionStatus(prev => ({ ...prev, discord: 'disconnected' }));
        }
        if (appId === 'telegram') {
            onConfigChange('telegram_config', null);
            setConnectionStatus(prev => ({ ...prev, telegram: 'disconnected' }));
        }
        if (appId === 'whatsapp') {
            try {
                await fetch(api('api/whatsapp/stop'), { method: 'POST', credentials: 'include' });
                await fetch(api('api/whatsapp/qr/reset'), { method: 'POST', credentials: 'include' });
            } catch (e) {
                console.error('Failed to disconnect WhatsApp:', e);
            }
            onConfigChange('whatsapp_config', null);
            setConnectionStatus(prev => ({ ...prev, whatsapp: 'disconnected' }));
        }
        if (appId === 'email') {
            try {
                const accounts = config?.email_config?.accounts || [];
                for (const a of accounts) {
                    const id = a.account_id || a.email;
                    if (id) {
                        await fetch(api(`api/email/accounts/${encodeURIComponent(id)}`), { method: 'DELETE', credentials: 'include' });
                    }
                }
                onConfigChange('email_config', { accounts: [] });
            } catch (e) {
                console.error('Failed to disconnect email accounts', e);
            }
        }
        if (isCloudApp(appId)) {
            try {
                const res = await fetch(api('api/cloud/accounts'), { credentials: 'include' });
                if (res.ok) {
                    const data = await res.json();
                    const accounts = data.accounts || [];
                    const providerAccounts = accounts.filter((a: any) => a.provider === appId);
                    for (const a of providerAccounts) {
                        const id = a.account_id;
                        if (id) {
                            const delRes = await fetch(api(`api/cloud/accounts/${encodeURIComponent(id)}`), { method: 'DELETE', credentials: 'include' });
                            if (!delRes.ok) {
                                console.error(`Failed to delete cloud account ${id}:`, delRes.status);
                            }
                        }
                    }
                    // Refresh accounts and update config so UI reflects the change (match user scope)
                    const refreshRes = await fetch(api('api/cloud/accounts'), { credentials: 'include' });
                    if (refreshRes.ok) {
                        const refreshed = await refreshRes.json();
                        const remaining = refreshed.accounts || [];
                        const localAdmin = ((config?.local_admin_username || 'admin') as string).trim().toLowerCase();
                        const username = (currentUser?.username || '').trim().toLowerCase();
                        if (!username || username === localAdmin) {
                            onConfigChange('cloud_config', { ...(config?.cloud_config || {}), accounts: remaining });
                        } else {
                            const byUser = { ...(config?.cloud_config_by_user || {}) };
                            byUser[username] = { ...(byUser[username] || {}), accounts: remaining };
                            onConfigChange('cloud_config_by_user', byUser);
                        }
                    }
                }
                setConnectionStatus(prev => ({ ...prev, [appId]: 'disconnected' }));
            } catch (e) {
                console.error(`Failed to disconnect ${appId}`, e);
            }
        }
    };

    const getAppsByCategory = (category: string) => {
        return CONNECTION_APPS.filter(app => app.category === category);
    };

    const CLOUD_IDS = ['google_drive', 'onedrive', 'icloud', 'dropbox', 'nextcloud'];
    const isCloudApp = (id: string) => CLOUD_IDS.includes(id);

    /** Cloud accounts from config (fallback when API fails or before first load), scoped to current user */
    const cloudAccountsFromConfig = (() => {
        const localAdmin = ((config?.local_admin_username ?? 'admin') as string).trim().toLowerCase();
        const username = (currentUser?.username ?? '').trim().toLowerCase();
        if (!username || username === localAdmin) {
            return Array.isArray(config?.cloud_config?.accounts) ? config.cloud_config.accounts : [];
        }
        const byUser = config?.cloud_config_by_user as Record<string, { accounts?: unknown[] }> | undefined;
        const userAccounts = byUser?.[username]?.accounts;
        return Array.isArray(userAccounts) ? userAccounts : [];
    })();

    const isConfigured = (app: ConnectionApp) => {
        if (app.id === 'contacts') return true;
        if (app.id === 'email') {
            const fromApi = emailAccountsFromApi.length > 0;
            const fromConfig = Array.isArray(config?.email_config?.accounts) && config.email_config.accounts.length > 0;
            return fromApi || fromConfig;
        }
        if (app.id === 'google_calendar') {
            if (calendarStatus.google_available) return true;
            return emailAccountsFromApi.some((a: any) => (a.provider || '').toLowerCase() === 'gmail' && a.enabled !== false);
        }
        if (app.id === 'outlook_calendar') {
            if (calendarStatus.microsoft_available) return true;
            return emailAccountsFromApi.some((a: any) => (a.provider || '').toLowerCase() === 'microsoft' && a.enabled !== false);
        }
        if (app.id === 'whatsapp') {
            const wc = config?.whatsapp_config;
            if (!wc) return false;
            const whitelist = wc.whitelist || [];
            return whitelist.some((e: any) => e?.phone_number);
        }
        if (isCloudApp(app.id)) {
            const fromApi = cloudAccountsFromApi.some((a: any) => a.provider === app.id);
            const fromConfig = cloudAccountsFromConfig.some((a: any) => a.provider === app.id);
            return fromApi || fromConfig;
        }
        const appConfig = config[app.configKey];
        return appConfig?.verified === true;
    };

    const isEnabled = (app: ConnectionApp) => {
        if (app.id === 'contacts') return true;
        if (app.id === 'email') {
            const accounts = emailAccountsFromApi.length > 0 ? emailAccountsFromApi : (config?.email_config?.accounts ?? []);
            return Array.isArray(accounts) && accounts.length > 0 && (accounts as any[]).some((a: any) => a.enabled !== false);
        }
        if (app.id === 'google_calendar') {
            if (calendarStatus.google_available) return true;
            return emailAccountsFromApi.some((a: any) => (a.provider || '').toLowerCase() === 'gmail' && a.enabled !== false);
        }
        if (app.id === 'outlook_calendar') {
            if (calendarStatus.microsoft_available) return true;
            return emailAccountsFromApi.some((a: any) => (a.provider || '').toLowerCase() === 'microsoft' && a.enabled !== false);
        }
        if (app.id === 'whatsapp') {
            return config?.whatsapp_config?.enabled === true;
        }
        if (isCloudApp(app.id)) {
            const fromApi = cloudAccountsFromApi.some((a: any) => a.provider === app.id && a.sync_enabled !== false);
            const fromConfig = cloudAccountsFromConfig.some((a: any) => a.provider === app.id && a.sync_enabled !== false);
            return fromApi || fromConfig;
        }
        const appConfig = config[app.configKey];
        return appConfig?.enabled === true;
    };

    return (
        <div className="space-y-6">
            <p className="text-sm text-gray-500">
                Connect external apps and services to interact with your VAF agent.
            </p>

            {CATEGORIES.filter(cat => getAppsByCategory(cat.id).length > 0).map(category => (
                <div key={category.id} className="space-y-3">
                    <div>
                        <h4 className="text-sm font-medium text-gray-700">{category.label}</h4>
                        <p className="text-xs text-gray-400">{category.description}</p>
                    </div>

                    <div className="space-y-2">
                        {getAppsByCategory(category.id).map(app => {
                            const configured = isConfigured(app);
                            const enabled = isEnabled(app);
                            const status = app.id === 'email'
                                ? (connectionStatus.email ?? ((config?.email_config?.accounts?.length ?? 0) > 0 ? 'connected' : 'disconnected'))
                                : isCloudApp(app.id)
                                    ? (configured ? 'connected' : 'disconnected')
                                    : (app.id === 'google_calendar' || app.id === 'outlook_calendar')
                                        ? (configured ? 'connected' : 'disconnected')
                                        : connectionStatus[app.id];
                            const Icon = app.icon;

                            return (
                                <div
                                    key={app.id}
                                    className={cn(
                                        "p-4 rounded-xl border transition-all",
                                        configured
                                            ? "bg-white border-gray-200 shadow-sm"
                                            : "bg-gray-50 border-gray-200",
                                        app.comingSoon && "opacity-60"
                                    )}
                                >
                                    <div className="flex items-center justify-between">
                                        <div className="flex items-center gap-3">
                                            <div className={cn(
                                                "w-10 h-10 rounded-xl flex items-center justify-center text-white",
                                                configured
                                                    ? (app.iconColor || "bg-gray-900")
                                                    : "bg-gray-300 text-gray-500"
                                            )}>
                                                <Icon className="w-5 h-5" />
                                            </div>
                                            <div>
                                                <div className="flex items-center gap-2">
                                                    <span className="font-medium text-gray-900">{app.name}</span>
                                                    {app.comingSoon && (
                                                        <span className="text-xs px-2 py-0.5 rounded-full bg-gray-100 text-gray-500">
                                                            Coming Soon
                                                        </span>
                                                    )}
                                                    {configured && app.id !== 'contacts' && (
                                                        <span className={cn(
                                                            "text-xs px-2 py-0.5 rounded-full",
                                                            status === 'connected' ? "bg-green-100 text-green-700" :
                                                            status === 'linked' ? "bg-amber-100 text-amber-700" :
                                                            status === 'checking' ? "bg-yellow-100 text-yellow-700" :
                                                            "bg-gray-100 text-gray-500"
                                                        )}>
                                                            {status === 'connected' ? 'Connected' :
                                                             status === 'linked' ? 'Linked' :
                                                             status === 'checking' ? 'Checking...' :
                                                             'Disconnected'}
                                                        </span>
                                                    )}
                                                </div>
                                                <p className="text-sm text-gray-500">{app.description}</p>
                                                {configured && app.id === 'discord' && config[app.configKey]?.admin_username && (
                                                    <p className="text-xs text-gray-600 mt-1">
                                                        Admin: @{config[app.configKey].admin_username}
                                                    </p>
                                                )}
                                                {configured && app.id === 'telegram' && (
                                                    <p className="text-xs text-gray-600 mt-1">
                                                        {(config[app.configKey]?.whitelist?.length ?? 0) > 0
                                                            ? 'You can message from Telegram; VAF can reach you there.'
                                                            : 'Add your Telegram in Settings to message and be reached.'}
                                                    </p>
                                                )}
                                                {configured && app.id === 'whatsapp' && (
                                                    <p className="text-xs text-gray-600 mt-1">
                                                        You can message from WhatsApp; VAF can reach you there.
                                                    </p>
                                                )}
                                                {configured && app.id === 'email' && (
                                                    <p className="text-xs text-gray-600 mt-1">
                                                        {(emailAccountsFromApi.length || (config?.email_config?.accounts?.length ?? 0))} account(s) connected. Open Settings to add or remove.
                                                    </p>
                                                )}
                                                {configured && (app.id === 'google_calendar' || app.id === 'outlook_calendar') && (
                                                    <p className="text-xs text-gray-600 mt-1">
                                                        Calendar uses your {app.id === 'google_calendar' ? 'Gmail' : 'Outlook'} account. The agent can list and create events.
                                                    </p>
                                                )}
                                            </div>
                                        </div>

                                        <div className="flex items-center gap-2">
                                            {app.id === 'contacts' ? (
                                                <button
                                                    onClick={() => onOpenContactsDashboard?.()}
                                                    className="p-2 hover:bg-gray-100 rounded-lg transition-colors"
                                                    title="Open contacts"
                                                >
                                                    <Settings className="w-4 h-4 text-gray-500" />
                                                </button>
                                            ) : configured ? (
                                                <>
                                                    {/* Toggle Switch (not for calendar: uses email account) */}
                                                    {(app.id !== 'google_calendar' && app.id !== 'outlook_calendar') && (
                                                        <button
                                                            onClick={() => handleToggleConnection(app.id, !enabled)}
                                                            className={cn(
                                                                "relative w-11 h-6 rounded-full transition-colors",
                                                                enabled ? "bg-gray-800" : "bg-gray-300"
                                                            )}
                                                        >
                                                            <div className={cn(
                                                                "absolute top-1 w-4 h-4 rounded-full bg-white shadow transition-transform",
                                                                enabled ? "translate-x-6" : "translate-x-1"
                                                            )} />
                                                        </button>
                                                    )}

                                                    {/* Settings */}
                                                    <button
                                                        onClick={() => {
                                                            if (app.id === 'discord') {
                                                                if (onOpenDiscordDashboard && configured) onOpenDiscordDashboard();
                                                                else onOpenDiscordWizard();
                                                            }
                                                            if (app.id === 'telegram') {
                                                                if (onOpenTelegramDashboard && configured) onOpenTelegramDashboard();
                                                                else onOpenTelegramWizard();
                                                            }
                                                            if (app.id === 'whatsapp') (onOpenWhatsAppDashboard && configured ? onOpenWhatsAppDashboard() : onOpenWhatsAppWizard?.());
                                                            if (app.id === 'email') {
                                                                if (onOpenEmailDashboard) onOpenEmailDashboard();
                                                                else if (onOpenEmailWizard) onOpenEmailWizard();
                                                            }
                                                            if (app.id === 'google_calendar' || app.id === 'outlook_calendar') {
                                                                if (onOpenCalendarDashboard && configured) onOpenCalendarDashboard();
                                                                else if (onOpenCalendarWizard) onOpenCalendarWizard(app.id);
                                                                else onOpenEmailWizard?.();
                                                            }
                                                            if (isCloudApp(app.id)) {
                                                                if (onOpenCloudDashboard && configured) onOpenCloudDashboard();
                                                                else if (onOpenCloudWizard) onOpenCloudWizard(app.id);
                                                            }
                                                        }}
                                                        className="p-2 hover:bg-gray-100 rounded-lg transition-colors"
                                                        title={
                                                            (app.id === 'google_calendar' || app.id === 'outlook_calendar') && configured
                                                                ? 'Open calendar'
                                                                : isCloudApp(app.id) && configured
                                                                    ? 'Open cloud dashboard'
                                                                    : 'Add account'
                                                        }
                                                    >
                                                        <Settings className="w-4 h-4 text-gray-500" />
                                                    </button>

                                                    {/* Disconnect */}
                                                    <button
                                                        onClick={() => handleDisconnect(app.id)}
                                                        className="p-2 hover:bg-red-50 rounded-lg transition-colors group"
                                                        title="Disconnect"
                                                    >
                                                        <Trash2 className="w-4 h-4 text-gray-400 group-hover:text-red-500" />
                                                    </button>
                                                </>
                                            ) : (
                                                <button
                                                    onClick={() => {
                                                        if (app.id === 'discord') onOpenDiscordWizard();
                                                        if (app.id === 'telegram') onOpenTelegramWizard();
                                                        if (app.id === 'whatsapp') onOpenWhatsAppWizard?.();
                                                        if (app.id === 'email') {
                                                            if (onOpenEmailDashboard) onOpenEmailDashboard();
                                                            else if (onOpenEmailWizard) onOpenEmailWizard();
                                                        }
                                                        if (app.id === 'google_calendar' || app.id === 'outlook_calendar') {
                                                            if (onOpenCalendarWizard) onOpenCalendarWizard(app.id);
                                                            else onOpenEmailWizard?.();
                                                        }
                                                        if (isCloudApp(app.id) && onOpenCloudWizard) onOpenCloudWizard(app.id);
                                                    }}
                                                    disabled={app.comingSoon || !app.available}
                                                    className={cn(
                                                        "flex items-center gap-2 px-4 py-2 rounded-lg font-medium transition-colors",
                                                        app.comingSoon || !app.available
                                                            ? "bg-gray-100 text-gray-400 cursor-not-allowed"
                                                            : "bg-gray-900 hover:bg-gray-800 text-white"
                                                    )}
                                                >
                                                    <Plus className="w-4 h-4" />
                                                    Connect
                                                </button>
                                            )}
                                        </div>
                                    </div>
                                </div>
                            );
                        })}
                    </div>
                </div>
            ))}
        </div>
    );
}
