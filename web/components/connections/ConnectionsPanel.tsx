'use client';

import React, { useState, useEffect } from 'react';
import {
    MessageCircle, Phone, Mail, Slack, Plus, Settings,
    CheckCircle2, XCircle, Loader2, Trash2, Power,
    Calendar, Cloud, HardDrive, FolderSync, Users
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
    // ============ Calendar ============
    {
        id: 'google_calendar',
        name: 'Google Calendar',
        icon: Calendar,
        category: 'calendar',
        description: 'Sync events, create reminders, and manage your Google Calendar',
        configKey: 'google_calendar_config',
        available: false,
        comingSoon: true,
        iconColor: 'bg-blue-500',
    },
    {
        id: 'outlook_calendar',
        name: 'Microsoft Outlook',
        icon: Calendar,
        category: 'calendar',
        description: 'Connect to Outlook/Microsoft 365 calendar',
        configKey: 'outlook_calendar_config',
        available: false,
        comingSoon: true,
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

export default function ConnectionsPanel({ config, onConfigChange, currentUser, refreshTrigger = 0, onOpenDiscordWizard, onOpenDiscordDashboard, onOpenTelegramWizard, onOpenWhatsAppWizard, onOpenWhatsAppDashboard, onOpenTelegramDashboard, onOpenEmailDashboard, onOpenEmailWizard, onOpenCloudDashboard, onOpenCloudWizard, onOpenContactsDashboard }: ConnectionsPanelProps) {
    const [connectionStatus, setConnectionStatus] = useState<Record<string, 'connected' | 'linked' | 'disconnected' | 'checking'>>({});
    /** Cloud accounts from API (source of truth; config can be stale after OAuth) */
    const [cloudAccountsFromApi, setCloudAccountsFromApi] = useState<any[]>([]);
    /** Email accounts from API (source of truth; config only has legacy email_config, not email_config_by_user) */
    const [emailAccountsFromApi, setEmailAccountsFromApi] = useState<any[]>([]);

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
            } else {
                setCloudAccountsFromApi([]);
            }
        } catch {
            setCloudAccountsFromApi([]);
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

    const isConfigured = (app: ConnectionApp) => {
        if (app.id === 'contacts') return true;
        if (app.id === 'email') {
            const fromApi = emailAccountsFromApi.length > 0;
            const fromConfig = Array.isArray(config?.email_config?.accounts) && config.email_config.accounts.length > 0;
            return fromApi || fromConfig;
        }
        if (app.id === 'whatsapp') {
            const wc = config?.whatsapp_config;
            if (!wc) return false;
            const whitelist = wc.whitelist || [];
            return whitelist.some((e: any) => e?.phone_number);
        }
        if (isCloudApp(app.id)) {
            return cloudAccountsFromApi.some((a: any) => a.provider === app.id);
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
        if (app.id === 'whatsapp') {
            return config?.whatsapp_config?.enabled === true;
        }
        if (isCloudApp(app.id)) {
            return cloudAccountsFromApi.some((a: any) => a.provider === app.id && a.sync_enabled !== false);
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
                                                    {/* Toggle Switch */}
                                                    <button
                                                        onClick={() => handleToggleConnection(app.id, !enabled)}
                                                        className={cn(
                                                            "relative w-11 h-6 rounded-full transition-colors",
                                                            enabled ? "bg-green-500" : "bg-gray-300"
                                                        )}
                                                    >
                                                        <div className={cn(
                                                            "absolute top-1 w-4 h-4 rounded-full bg-white shadow transition-transform",
                                                            enabled ? "translate-x-6" : "translate-x-1"
                                                        )} />
                                                    </button>

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
                                                            if (isCloudApp(app.id)) {
                                                                if (onOpenCloudDashboard && configured) onOpenCloudDashboard();
                                                                else if (onOpenCloudWizard) onOpenCloudWizard(app.id);
                                                            }
                                                        }}
                                                        className="p-2 hover:bg-gray-100 rounded-lg transition-colors"
                                                        title={isCloudApp(app.id) && configured ? 'Open cloud dashboard' : 'Add account'}
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
