'use client';

import React, { useState, useEffect } from 'react';
import {
    MessageCircle, Phone, Mail, Slack, Plus, Settings,
    CheckCircle2, XCircle, Loader2, Trash2, Power,
    Calendar, Cloud, HardDrive, FolderSync
} from 'lucide-react';
import { cn } from '@/lib/utils';

interface ConnectionsPanelProps {
    config: any;
    onConfigChange: (key: string, value: any) => void;
    onOpenDiscordWizard: () => void;
}

interface ConnectionApp {
    id: string;
    name: string;
    icon: React.ElementType;
    category: 'communication' | 'calendar' | 'cloud' | 'productivity' | 'social';
    description: string;
    configKey: string;
    available: boolean;
    comingSoon?: boolean;
    iconColor?: string;
}

const CONNECTION_APPS: ConnectionApp[] = [
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
        description: 'Control your agent through Telegram bot',
        configKey: 'telegram_config',
        available: false,
        comingSoon: true,
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
        available: false,
        comingSoon: true,
        iconColor: 'bg-green-600',
    },
    {
        id: 'email',
        name: 'Email',
        icon: Mail,
        category: 'communication',
        description: 'Receive and respond to emails automatically',
        configKey: 'email_config',
        available: false,
        comingSoon: true,
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
        description: 'Access and manage files on Google Drive',
        configKey: 'google_drive_config',
        available: false,
        comingSoon: true,
        iconColor: 'bg-yellow-500',
    },
    {
        id: 'onedrive',
        name: 'Microsoft OneDrive',
        icon: Cloud,
        category: 'cloud',
        description: 'Sync files with OneDrive / SharePoint',
        configKey: 'onedrive_config',
        available: false,
        comingSoon: true,
        iconColor: 'bg-blue-500',
    },
    {
        id: 'icloud',
        name: 'Apple iCloud',
        icon: Cloud,
        category: 'cloud',
        description: 'Access iCloud Drive files on macOS',
        configKey: 'icloud_config',
        available: false,
        comingSoon: true,
        iconColor: 'bg-sky-400',
    },
    {
        id: 'dropbox',
        name: 'Dropbox',
        icon: FolderSync,
        category: 'cloud',
        description: 'Sync and access Dropbox files',
        configKey: 'dropbox_config',
        available: false,
        comingSoon: true,
        iconColor: 'bg-blue-600',
    },
    {
        id: 'nextcloud',
        name: 'Nextcloud',
        icon: HardDrive,
        category: 'cloud',
        description: 'Connect to self-hosted Nextcloud instance',
        configKey: 'nextcloud_config',
        available: false,
        comingSoon: true,
        iconColor: 'bg-cyan-600',
    },
];

const CATEGORIES = [
    { id: 'communication', label: 'Communication', description: 'Messaging & chat platforms' },
    { id: 'calendar', label: 'Calendar', description: 'Scheduling & event management' },
    { id: 'cloud', label: 'Cloud Storage', description: 'File sync & cloud drives' },
    { id: 'productivity', label: 'Productivity', description: 'Work tools & integrations' },
    { id: 'social', label: 'Social', description: 'Social media platforms' },
];

export default function ConnectionsPanel({ config, onConfigChange, onOpenDiscordWizard }: ConnectionsPanelProps) {
    const [connectionStatus, setConnectionStatus] = useState<Record<string, 'connected' | 'disconnected' | 'checking'>>({});

    // Check connection status on mount
    useEffect(() => {
        checkConnectionStatus();
    }, [config]);

    const checkConnectionStatus = async () => {
        // Check Discord status
        if (config.discord_config?.verified) {
            setConnectionStatus(prev => ({ ...prev, discord: 'checking' }));
            try {
                const res = await fetch('http://localhost:8001/api/discord/status');
                const status = await res.json();
                setConnectionStatus(prev => ({ ...prev, discord: status.running ? 'connected' : 'disconnected' }));
            } catch {
                setConnectionStatus(prev => ({ ...prev, discord: 'disconnected' }));
            }
        } else {
            setConnectionStatus(prev => ({ ...prev, discord: 'disconnected' }));
        }
    };

    const handleToggleConnection = async (appId: string, enabled: boolean) => {
        if (appId === 'discord') {
            const currentConfig = config.discord_config || {};
            onConfigChange('discord_config', { ...currentConfig, enabled });

            // Start/stop the Discord bridge
            try {
                if (enabled) {
                    await fetch('http://localhost:8001/api/discord/start', { method: 'POST' });
                } else {
                    await fetch('http://localhost:8001/api/discord/stop', { method: 'POST' });
                }
                setConnectionStatus(prev => ({ ...prev, discord: enabled ? 'connected' : 'disconnected' }));
            } catch (e) {
                console.error('Failed to toggle Discord:', e);
            }
        }
    };

    const handleDisconnect = (appId: string) => {
        if (appId === 'discord') {
            onConfigChange('discord_config', null);
            setConnectionStatus(prev => ({ ...prev, discord: 'disconnected' }));
        }
    };

    const getAppsByCategory = (category: string) => {
        return CONNECTION_APPS.filter(app => app.category === category);
    };

    const isConfigured = (app: ConnectionApp) => {
        const appConfig = config[app.configKey];
        return appConfig?.verified === true;
    };

    const isEnabled = (app: ConnectionApp) => {
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
                            const status = connectionStatus[app.id];
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
                                                    {configured && (
                                                        <span className={cn(
                                                            "text-xs px-2 py-0.5 rounded-full",
                                                            status === 'connected' ? "bg-green-100 text-green-700" :
                                                            status === 'checking' ? "bg-yellow-100 text-yellow-700" :
                                                            "bg-gray-100 text-gray-500"
                                                        )}>
                                                            {status === 'connected' ? 'Connected' :
                                                             status === 'checking' ? 'Checking...' :
                                                             'Disconnected'}
                                                        </span>
                                                    )}
                                                </div>
                                                <p className="text-sm text-gray-500">{app.description}</p>
                                                {configured && config[app.configKey]?.admin_username && (
                                                    <p className="text-xs text-gray-600 mt-1">
                                                        Admin: @{config[app.configKey].admin_username}
                                                    </p>
                                                )}
                                            </div>
                                        </div>

                                        <div className="flex items-center gap-2">
                                            {configured ? (
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
                                                        onClick={() => app.id === 'discord' && onOpenDiscordWizard()}
                                                        className="p-2 hover:bg-gray-100 rounded-lg transition-colors"
                                                        title="Settings"
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
                                                    onClick={() => app.id === 'discord' && !app.comingSoon && onOpenDiscordWizard()}
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
