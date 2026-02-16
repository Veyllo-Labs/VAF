'use client';

import React, { useState, useEffect, useCallback, lazy, Suspense, useMemo, useRef } from 'react';
import { useTranslations } from 'next-intl';
// PERFORMANCE: Lazy load ReactFlow - it's heavy and only needed for specific modals
const ReactFlow = lazy(() => import('reactflow').then(mod => ({ default: mod.default })));
import {
    Background,
    Controls,
    MiniMap,
    useNodesState,
    useEdgesState,
    Position,
    MarkerType
} from 'reactflow';
import 'reactflow/dist/style.css';
import TTSSettings from './settings/TTSSettings';

// Loading fallback for lazy-loaded ReactFlow
const ReactFlowFallback = () => {
    const t = useTranslations('settings');
    return (
        <div className="flex items-center justify-center h-full bg-gray-50">
            <div className="animate-pulse text-gray-400">{t('loadingVisualization')}</div>
        </div>
    );
};

// Constants for collision detection
const NODE_WIDTH = 220;
const NODE_HEIGHT = 100;
const TAG_NODE_WIDTH = 150;
const TAG_NODE_HEIGHT = 60;
const COLLISION_PADDING = 25;

// Helper: Check if two rectangles overlap
function rectsOverlap(
    r1: { x: number; y: number; width: number; height: number },
    r2: { x: number; y: number; width: number; height: number }
): boolean {
    return !(
        r1.x + r1.width + COLLISION_PADDING < r2.x ||
        r2.x + r2.width + COLLISION_PADDING < r1.x ||
        r1.y + r1.height + COLLISION_PADDING < r2.y ||
        r2.y + r2.height + COLLISION_PADDING < r1.y
    );
}

// Helper: Apply collision detection to prevent overlapping nodes
function applyCollisionDetection(nodes: any[]): any[] {
    const result = nodes.map(n => ({ ...n, position: { ...n.position } }));
    const maxIterations = 50;

    for (let iter = 0; iter < maxIterations; iter++) {
        let hasCollision = false;

        for (let i = 0; i < result.length; i++) {
            const nodeA = result[i];
            const widthA = nodeA.type === 'tagNode' ? TAG_NODE_WIDTH : NODE_WIDTH;
            const heightA = nodeA.type === 'tagNode' ? TAG_NODE_HEIGHT : NODE_HEIGHT;

            for (let j = i + 1; j < result.length; j++) {
                const nodeB = result[j];
                const widthB = nodeB.type === 'tagNode' ? TAG_NODE_WIDTH : NODE_WIDTH;
                const heightB = nodeB.type === 'tagNode' ? TAG_NODE_HEIGHT : NODE_HEIGHT;

                const rectA = { x: nodeA.position.x, y: nodeA.position.y, width: widthA, height: heightA };
                const rectB = { x: nodeB.position.x, y: nodeB.position.y, width: widthB, height: heightB };

                if (rectsOverlap(rectA, rectB)) {
                    hasCollision = true;

                    // Calculate push direction
                    const centerAX = rectA.x + rectA.width / 2;
                    const centerAY = rectA.y + rectA.height / 2;
                    const centerBX = rectB.x + rectB.width / 2;
                    const centerBY = rectB.y + rectB.height / 2;

                    const dx = centerBX - centerAX;
                    const dy = centerBY - centerAY;
                    const dist = Math.sqrt(dx * dx + dy * dy) || 1;

                    // Push apart
                    const pushForce = 35;
                    const pushX = (dx / dist) * pushForce;
                    const pushY = (dy / dist) * pushForce;

                    result[i].position = {
                        x: result[i].position.x - pushX,
                        y: result[i].position.y - pushY
                    };
                    result[j].position = {
                        x: result[j].position.x + pushX,
                        y: result[j].position.y + pushY
                    };
                }
            }
        }

        if (!hasCollision) break;
    }

    return result;
}
import {
    X, Globe, Cpu, Volume2, Monitor, Shield, Save, RotateCcw,
    Check, ChevronRight, Zap, Search, Download, RefreshCw, Workflow, GitBranch,
    Brain, Database, Link2, MessageSquare, Network, Users, User, Lock, Server, Laptop, Smartphone,
    Edit, Trash2, Plus, Filter, MoreHorizontal, CheckCircle, XCircle, ShieldAlert, Copy, Wand2, LogOut, Calendar
} from 'lucide-react';
import { useTranslations } from 'next-intl';
import { cn } from '@/lib/utils';
import { displayOAuthValue, BUILTIN_GOOGLE_CLIENT_ID } from '@/lib/oauth_defaults';
import { useLocaleStore } from '@/lib/localeStore';
import { languages } from '@/lib/languages';
import { ConnectionsPanel, DiscordSetupWizard, DiscordConfig, TelegramSetupWizard, TelegramConfig, TelegramDashboard, DiscordDashboard, EmailSetupWizard, MailDashboard, CloudDashboard, CloudSetupWizard, WhatsAppSetupWizard, WhatsAppDashboard, ContactsDashboard } from './connections';
import SoulWizard from './SoulWizard';
import AutomationCalendarModal from './AutomationCalendarModal';

export interface SettingsModalProps {
    isOpen: boolean;
    onClose: () => void;
    config: any;
    onSave: (newConfig: any) => void;
    availableModels: string[];
    apiModels: Record<string, string[]>;
    onFetchApiModels: (provider: string, apiKey: string) => void;
    onRefreshLocalModels: () => void;
    tools?: Array<{ name: string; description: string; category: string }>;
    workflows?: Array<{ id: string; name: string; description: string; steps: number }>;
    trustedSources?: { categories: Array<{ id: string; name: string; description: string; is_custom?: boolean; sources: Array<{ name: string; url: string; domains: string[]; trust_score: number; is_custom: boolean }> }> };
    onAddTrustedSource?: (categoryId: string, name: string, url: string) => void;
    onRemoveTrustedSource?: (domain: string, is_custom: boolean) => void;
    onDeleteTrustedCategory?: (categoryId: string) => void;
    onRequestTrustedSources?: () => void;
    onCreateTrustedCategory?: (name: string) => void;
    trustedSourcesError?: string | null;
    automations?: Array<{ id: string; name: string; description: string; frequency: string; time: string; enabled: boolean }>;
    currentUser?: { id: string; username: string; role: string };
    onLogout?: () => void;
    apiBase?: string;
    /** When set, open the modal with this tab active (e.g. 'automations'). */
    initialTab?: string;
    /** Callback to refresh config (e.g. after OAuth/cloud connection) */
    onRefreshConfig?: () => void;
    /** Connection status for indicator above Logout in sidebar */
    connectionLabel?: string;
    isConnected?: boolean;
    showIdleState?: boolean;
    onReconnect?: () => void;
}

const CATEGORIES = [
    { id: 'general', labelKey: 'general', icon: Globe },
    { id: 'persona', labelKey: 'persona', icon: Users, adminOnly: true },
    { id: 'ai', labelKey: 'ai', icon: Cpu },
    { id: 'voice', labelKey: 'voice', icon: Volume2 },
    { id: 'interface', labelKey: 'interface', icon: Monitor },
    { id: 'connections', labelKey: 'connections', icon: MessageSquare },
    { id: 'advanced', labelKey: 'advanced', icon: Zap },
    { id: 'automations', labelKey: 'automations', icon: Check },
    { id: 'local_network', labelKey: 'localNetwork', icon: Network },
    { id: 'about', labelKey: 'about', icon: Globe },
];

const PROVIDERS = [
    { id: 'openai', label: 'OpenAI', defaultModel: 'gpt-4o' },
    { id: 'anthropic', label: 'Anthropic', defaultModel: 'claude-3-5-sonnet-20241022' },
    { id: 'deepseek', label: 'DeepSeek', defaultModel: 'deepseek-chat' },
    { id: 'google', label: 'Google', defaultModel: 'gemini-1.5-flash-latest' },
    { id: 'openrouter', label: 'OpenRouter', defaultModel: 'anthropic/claude-3.5-sonnet' },
];

// Common IANA timezones for Date & Time (Interface). Used in system prompt and user context.
const DATE_TIME_TIMEZONES: { value: string; label: string }[] = [
    { value: '', label: 'Server default' },
    { value: 'UTC', label: 'UTC' },
    { value: 'Europe/Berlin', label: 'Europe/Berlin' },
    { value: 'Europe/London', label: 'Europe/London' },
    { value: 'Europe/Paris', label: 'Europe/Paris' },
    { value: 'Europe/Vienna', label: 'Europe/Vienna' },
    { value: 'Europe/Zurich', label: 'Europe/Zurich' },
    { value: 'America/New_York', label: 'America/New_York' },
    { value: 'America/Chicago', label: 'America/Chicago' },
    { value: 'America/Los_Angeles', label: 'America/Los_Angeles' },
    { value: 'America/Denver', label: 'America/Denver' },
    { value: 'Asia/Tokyo', label: 'Asia/Tokyo' },
    { value: 'Asia/Shanghai', label: 'Asia/Shanghai' },
    { value: 'Asia/Singapore', label: 'Asia/Singapore' },
    { value: 'Australia/Sydney', label: 'Australia/Sydney' },
];

const DATE_TIME_DATE_FORMATS: { value: string; label: string }[] = [
    { value: '', label: 'Default (language-based)' },
    { value: 'dd.mm.yyyy', label: 'DD.MM.YYYY (e.g. 10.02.2026)' },
    { value: 'yyyy-mm-dd', label: 'YYYY-MM-DD (e.g. 2026-02-10)' },
    { value: 'mm/dd/yyyy', label: 'MM/DD/YYYY (e.g. 02/10/2026)' },
    { value: 'dd.mm.yy', label: 'DD.MM.YY (e.g. 10.02.26)' },
];

const DATE_TIME_TIME_FORMATS: { value: string; label: string }[] = [
    { value: '', label: 'Default (24h)' },
    { value: '24h', label: '24-hour' },
    { value: '12h', label: '12-hour (AM/PM)' },
];


export default function SettingsModal({ isOpen, onClose, config, onSave, availableModels, apiModels, onFetchApiModels, onRefreshLocalModels, tools = [], workflows = [], trustedSources = { categories: [] }, onAddTrustedSource, onRemoveTrustedSource, onDeleteTrustedCategory, onRequestTrustedSources, onCreateTrustedCategory, trustedSourcesError, automations = [], currentUser, onLogout, apiBase, initialTab: initialTabProp, onRefreshConfig, connectionLabel = 'Connected', isConnected = true, showIdleState = false, onReconnect }: SettingsModalProps) {
    const t = useTranslations();
    const tTabs = useTranslations('settings.tabs');
    const tCommon = useTranslations('common');
    const tGeneral = useTranslations('settings.general');
    const tPersona = useTranslations('settings.persona');
    const tAi = useTranslations('settings.ai');
    const tVoice = useTranslations('settings.voice');
    const tInterface = useTranslations('settings.interface');
    const tAdvanced = useTranslations('settings.advanced');
    const tLocalNet = useTranslations('settings.localNetwork');
    const tAutomations = useTranslations('settings.automations');
    const tAbout = useTranslations('settings.about');
    const tModals = useTranslations('modals');
    const uiLocale = useLocaleStore((s) => s.locale);
    const setUiLocale = useLocaleStore((s) => s.setLocale);
    const [localConfig, setLocalConfig] = useState<any>(config || {});
    const [activeTab, setActiveTab] = useState('general');
    const [changed, setChanged] = useState(false);
    const [hfQuery, setHfQuery] = useState('');
    const [fetchingProvider, setFetchingProvider] = useState<string | null>(null);
    
    // Modals State
    const [showToolsModal, setShowToolsModal] = useState(false);
    const [showWorkflowsModal, setShowWorkflowsModal] = useState(false);
    const [showTrustedSourcesModal, setShowTrustedSourcesModal] = useState(false);
    const [showNetworkModal, setShowNetworkModal] = useState(false);
    const [showMemoryModal, setShowMemoryModal] = useState(false);
    const [showUserIdentityModal, setShowUserIdentityModal] = useState(false);
    const [showDiscordWizard, setShowDiscordWizard] = useState(false);
    const [showTelegramWizard, setShowTelegramWizard] = useState(false);
    const [showWhatsAppWizard, setShowWhatsAppWizard] = useState(false);
    const [showWhatsAppDashboard, setShowWhatsAppDashboard] = useState(false);
    const [showTelegramDashboard, setShowTelegramDashboard] = useState(false);
    const [showContactsDashboard, setShowContactsDashboard] = useState(false);
    const [showDiscordDashboard, setShowDiscordDashboard] = useState(false);
    const [showMailDashboard, setShowMailDashboard] = useState(false);
    const [showEmailWizard, setShowEmailWizard] = useState(false);
    const [showCloudWizard, setShowCloudWizard] = useState(false);
    const [showCloudDashboard, setShowCloudDashboard] = useState(false);
    const [cloudDashboardRefresh, setCloudDashboardRefresh] = useState(0);
    const [cloudWizardProvider, setCloudWizardProvider] = useState<string | undefined>(undefined);
    const [mailDashboardRefresh, setMailDashboardRefresh] = useState(0);
    const [showCreateAutomationModal, setShowCreateAutomationModal] = useState(false);

    const [toolsSearch, setToolsSearch] = useState('');
    const [workflowsSearch, setWorkflowsSearch] = useState('');
    const [trustedSourceForm, setTrustedSourceForm] = useState<{ categoryId: string; name: string; url: string }>({ categoryId: '', name: '', url: '' });
    const [addFormCategoryId, setAddFormCategoryId] = useState<string | null>(null);
    const [newCategoryName, setNewCategoryName] = useState('');
    const [showCreateCategoryForm, setShowCreateCategoryForm] = useState(false);
    const [codeModal, setCodeModal] = useState<{name: string, code: string} | null>(null);
    
    // Memory System State
    const [memoryStats, setMemoryStats] = useState<{ memories: number; chunks: number; connections: number; db_connected: boolean } | null>(null);
    const [memoryNodes, setMemoryNodes] = useState<any[]>([]);
    const [memoryEdges, setMemoryEdges] = useState<any[]>([]);
    const [memoryLoading, setMemoryLoading] = useState(false);
    const [memoryGraphError, setMemoryGraphError] = useState<string | null>(null);
    const [selectedMemoryNodeId, setSelectedMemoryNodeId] = useState<string | null>(null);

    // Calculate connected nodes for highlighting
    const connectedMemoryNodeIds = useMemo(() => {
        if (!selectedMemoryNodeId) return new Set<string>();

        const connected = new Set<string>();
        connected.add(selectedMemoryNodeId);

        // Find all edges connected to selected node
        memoryEdges.forEach(edge => {
            if (edge.source === selectedMemoryNodeId) {
                connected.add(edge.target);
            }
            if (edge.target === selectedMemoryNodeId) {
                connected.add(edge.source);
            }
        });

        // If a tag is selected, include all memories connected to that tag
        const selectedNode = memoryNodes.find(n => n.id === selectedMemoryNodeId);
        if (selectedNode?.type === 'tagNode' || selectedNode?.data?.isTagNode) {
            memoryEdges.forEach(edge => {
                if (edge.target === selectedMemoryNodeId || edge.source === selectedMemoryNodeId) {
                    connected.add(edge.source);
                    connected.add(edge.target);
                }
            });
        }

        // If a memory is selected, include its connected tags and other memories via those tags
        if (selectedNode?.type === 'memoryNode' || (!selectedNode?.data?.isTagNode && selectedNode)) {
            const connectedTags = new Set<string>();
            memoryEdges.forEach(edge => {
                if (edge.source === selectedMemoryNodeId && edge.target?.startsWith('tag-')) {
                    connectedTags.add(edge.target);
                    connected.add(edge.target);
                }
                if (edge.target === selectedMemoryNodeId && edge.source?.startsWith('tag-')) {
                    connectedTags.add(edge.source);
                    connected.add(edge.source);
                }
            });

            // Get all memories connected to those tags
            connectedTags.forEach(tagId => {
                memoryEdges.forEach(edge => {
                    if (edge.target === tagId) connected.add(edge.source);
                    if (edge.source === tagId) connected.add(edge.target);
                });
            });
        }

        return connected;
    }, [selectedMemoryNodeId, memoryEdges, memoryNodes]);
    
    // Workflow Visualization State
    const [workflowModal, setWorkflowModal] = useState<any>(null);
    const [selectedUser, setSelectedUser] = useState<any>(null);
    const [nodes, setNodes, onNodesChange] = useNodesState([]);
    const [edges, setEdges, onEdgesChange] = useEdgesState([]);
    
    // Local network: real host/port from browser (no dummy)
    const [displayHost, setDisplayHost] = useState('');
    const [displayPort, setDisplayPort] = useState('3000');

    // Logout flow
    const [showLogoutConfirm, setShowLogoutConfirm] = useState(false);
    const [isLoggingOut, setIsLoggingOut] = useState(false);
    const [logoutBarProgress, setLogoutBarProgress] = useState(0);

    // Network Topology: server node only; devices from API when available
    const [networkNodes, setNetworkNodes, onNetworkNodesChange] = useNodesState([
        { 
            id: 'server', 
            type: 'input', 
            data: { 
                label: (
                    <div className="flex flex-col items-center">
                        <div className="w-12 h-12 bg-gray-900 rounded-xl flex items-center justify-center mb-2 shadow-lg shadow-gray-200">
                            <Server size={24} className="text-white" />
                        </div>
                        <div className="font-bold text-gray-900">VAF Host</div>
                        <div className="text-[10px] text-gray-500 font-mono">—</div>
                    </div>
                ) 
            }, 
            position: { x: 300, y: 150 },
            style: { border: 'none', background: 'transparent' }
        },
    ]);
    
    const [networkEdges, setNetworkEdges, onNetworkEdgesChange] = useEdgesState([]);

    // User Management: loaded from API when Local Network tab is active (no dummy list)
    const [users, setUsers] = useState<Array<{ id: number; username: string; email?: string; role: string; lastActive: string; status: string; tools: string[]; workflows: string[]; access: string }>>([]);
    const [usersLoading, setUsersLoading] = useState(false);
    const [networkLinkCopied, setNetworkLinkCopied] = useState(false);
    /** LAN URL for other devices (from backend); e.g. http://192.168.1.100:3000 */
    const [networkAccessUrl, setNetworkAccessUrl] = useState<string | null>(null);
    const [userSearch, setUserSearch] = useState('');
    const [showAddUserModal, setShowAddUserModal] = useState(false);
    const [editingUser, setEditingUser] = useState<any>(null);
    const [newUser, setNewUser] = useState({ username: '', email: '', role: 'User', password: '', tools: [] as string[], workflows: [] as string[], createDb: true });

    // Security Warning & Restart Animation
    const [showNetworkWarning, setShowNetworkWarning] = useState(false);
    const [isRestarting, setIsRestarting] = useState(false);
    const [showSoulWizard, setShowSoulWizard] = useState(false);

    // Persona State
    const [personaData, setPersonaData] = useState<{identity: any, user_identity?: any, soul: string} | null>(null);
    const [personaLoading, setPersonaLoading] = useState(false);
    // Date & Time (Interface tab) – synced from user_identity when on interface tab
    const [dateTimeTimezone, setDateTimeTimezone] = useState<string>('');
    const [dateTimeDateFormat, setDateTimeDateFormat] = useState<string>('');
    const [dateTimeTimeFormat, setDateTimeTimeFormat] = useState<string>('');
    const [dateTimeSaving, setDateTimeSaving] = useState(false);

    // User Identity Edit State - Text-based editing
    const [isEditingUserIdentity, setIsEditingUserIdentity] = useState(false);
    const [userIdentityDraft, setUserIdentityDraft] = useState<{
        name: string;
        preferred_language: string;
        city: string;
        country: string;
        main_messenger: string;
        preferences: string;
        dos: string;
        donts: string;
    } | null>(null);
    const [newTimelineEntryCount, setNewTimelineEntryCount] = useState(0); // Track new entries for animation
    const timelineRef = useRef<HTMLDivElement>(null);

    useEffect(() => {
        if (activeTab === 'persona' || activeTab === 'interface') {
            setPersonaLoading(true);
            fetch('/api/user/persona')
                .then(res => res.json())
                .then(data => setPersonaData(data))
                .catch(e => console.error("Failed to load persona", e))
                .finally(() => setPersonaLoading(false));
        }
    }, [activeTab]);

    // Sync Date & Time fields from user_identity when Interface tab has persona data
    useEffect(() => {
        if (activeTab !== 'interface' || !personaData?.user_identity) return;
        const ui = personaData.user_identity as { timezone?: string; date_format?: string; time_format?: string };
        setDateTimeTimezone(ui.timezone || '');
        setDateTimeDateFormat(ui.date_format || '');
        setDateTimeTimeFormat(ui.time_format || '');
    }, [activeTab, personaData?.user_identity]);

    useEffect(() => {
        setLocalConfig(config || {});
        setChanged(false);
    }, [config, isOpen]);

    // Open with a specific tab when initialTab is provided
    useEffect(() => {
        if (isOpen && initialTabProp) {
            setActiveTab(initialTabProp);
        }
    }, [isOpen, initialTabProp]);

    // OAuth callback: auto-open Cloud or Email wizard when returning from OAuth with success
    useEffect(() => {
        if (!isOpen || activeTab !== 'connections') return;
        const params = typeof window !== 'undefined' ? new URLSearchParams(window.location.search) : null;
        if (!params) return;
        if (params.get('cloud_oauth') === 'success') {
            setCloudWizardProvider(params.get('provider') || 'google_drive');
            setShowCloudWizard(true);
        } else if (params.get('email_oauth') === 'success') {
            setShowEmailWizard(true);
        }
    }, [isOpen, activeTab]);

    // Reset user identity editing state when modal closes
    useEffect(() => {
        if (!isOpen) {
            setIsEditingUserIdentity(false);
            setUserIdentityDraft(null);
        }
    }, [isOpen]);

    // User Identity: Start editing - convert arrays to newline-separated text
    const startEditingUserIdentity = () => {
        if (!personaData?.user_identity) return;
        const ui = personaData.user_identity;
        setUserIdentityDraft({
            name: ui.name || '',
            preferred_language: ui.preferred_language || '',
            city: ui.city || '',
            country: ui.country || '',
            main_messenger: ui.main_messenger || '',
            preferences: (ui.preferences || []).join('\n'),
            dos: (ui.dos || []).join('\n'),
            donts: (ui.donts || []).join('\n'),
        });
        setIsEditingUserIdentity(true);
    };

    // User Identity: Save changes - convert text back to arrays
    const saveUserIdentity = async () => {
        if (!userIdentityDraft) return;

        // Parse text back to arrays (split by newline, filter empty)
        const parseList = (text: string) => text.split('\n').map(s => s.trim()).filter(s => s.length > 0);

        const rawMain = (userIdentityDraft.main_messenger || '').trim().toLowerCase();
        const main_messenger = ['telegram', 'discord', 'slack', 'whatsapp', 'email'].includes(rawMain) ? rawMain : null;
        const updateData = {
            name: userIdentityDraft.name.trim() || undefined,
            preferred_language: userIdentityDraft.preferred_language.trim() || undefined,
            city: userIdentityDraft.city.trim() || undefined,
            country: userIdentityDraft.country.trim() || undefined,
            main_messenger: main_messenger as string | null,
            preferences: parseList(userIdentityDraft.preferences),
            dos: parseList(userIdentityDraft.dos),
            donts: parseList(userIdentityDraft.donts),
        };

        // Get current change_log length to calculate how many new entries were added
        const prevChangeLogLength = personaData?.user_identity?.change_log?.length ?? 0;

        try {
            const res = await fetch('/api/user/user-identity', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(updateData)
            });
            if (res.ok) {
                const data = await res.json();
                setPersonaData(prev => prev ? { ...prev, user_identity: data.user_identity } : prev);

                // Calculate new entries and trigger animation
                const newLength = data.user_identity?.change_log?.length ?? 0;
                const addedCount = newLength - prevChangeLogLength;
                if (addedCount > 0) {
                    setNewTimelineEntryCount(addedCount);
                    // Scroll timeline to top after a brief delay to let React render
                    setTimeout(() => {
                        timelineRef.current?.scrollTo({ top: 0, behavior: 'smooth' });
                    }, 50);
                    // Clear animation flag after animation completes
                    setTimeout(() => setNewTimelineEntryCount(0), 1500);
                }
            }
        } catch (e) {
            console.error("Failed to save user identity", e);
        }
        setIsEditingUserIdentity(false);
        setUserIdentityDraft(null);
    };

    // User Identity: Cancel editing
    const cancelEditingUserIdentity = () => {
        setIsEditingUserIdentity(false);
        setUserIdentityDraft(null);
    };

    // Date & Time (Interface tab): save timezone, date_format, time_format to user_identity
    const saveDateTimeSettings = async () => {
        setDateTimeSaving(true);
        try {
            const res = await fetch('/api/user/user-identity', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    timezone: dateTimeTimezone.trim() || undefined,
                    date_format: dateTimeDateFormat.trim() || undefined,
                    time_format: dateTimeTimeFormat.trim() || undefined,
                }),
            });
            if (res.ok) {
                const data = await res.json();
                setPersonaData(prev => prev ? { ...prev, user_identity: data.user_identity } : prev);
            }
        } catch (e) {
            console.error('Failed to save date/time settings', e);
        } finally {
            setDateTimeSaving(false);
        }
    };

    // Local network: real host/port from browser
    useEffect(() => {
        if (typeof window === 'undefined') return;
        setDisplayHost(window.location.hostname || '');
        setDisplayPort(window.location.port || '3000');
    }, [isOpen]);

    // Update network map server node label with real host:port
    useEffect(() => {
        if (!displayHost) return;
        setNetworkNodes((nds) =>
            nds.map((n) =>
                n.id === 'server'
                    ? {
                          ...n,
                          data: {
                              label: (
                                  <div className="flex flex-col items-center">
                                      <div className="w-12 h-12 bg-gray-900 rounded-xl flex items-center justify-center mb-2 shadow-lg shadow-gray-200">
                                          <Server size={24} className="text-white" />
                                      </div>
                                      <div className="font-bold text-gray-900">VAF Host</div>
                                      <div className="text-[10px] text-gray-500 font-mono">{displayHost}{displayPort && displayPort !== '80' && displayPort !== '443' ? `:${displayPort}` : ''}</div>
                                  </div>
                              ),
                          },
                      }
                    : n
            )
        );
    }, [displayHost, displayPort]);

    // Fetch local network users when tab is active
    useEffect(() => {
        if (!isOpen || activeTab !== 'local_network') return;
        setUsersLoading(true);
        fetch('/api/users')
            .then((res) => (res.ok ? res.json() : []))
            .then((data) => (Array.isArray(data) ? setUsers(data) : setUsers([])))
            .catch(() => setUsers([]))
            .finally(() => setUsersLoading(false));
    }, [isOpen, activeTab]);

    // Fetch LAN access URL from backend (IP for other devices)
    useEffect(() => {
        if (!isOpen || activeTab !== 'local_network') return;
        fetch('http://localhost:8001/api/network/access-url')
            .then((res) => (res.ok ? res.json() : {}))
            .then((data: { url?: string | null }) => setNetworkAccessUrl(data.url ?? null))
            .catch(() => setNetworkAccessUrl(null));
    }, [isOpen, activeTab]);

    // Reset fetching state when apiModels update
    useEffect(() => {
        setFetchingProvider(null);
    }, [apiModels]);

    // Same-origin /api so Next.js rewrite proxies to backend (no CORS)
    const memoryApiBase = typeof window !== 'undefined' ? '' : (process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8001');

    // Fetch memory stats when modal opens
    useEffect(() => {
        if (isOpen && localConfig.memory_enabled) {
            fetch(`${memoryApiBase}/api/memory/stats`)
                .then(res => res.json())
                .then(data => setMemoryStats(data))
                .catch(() => setMemoryStats(null));
        }
    }, [isOpen, localConfig.memory_enabled, memoryApiBase]);

    // Fetch memory graph when memory modal opens
    const fetchMemoryGraph = useCallback(async () => {
        setMemoryLoading(true);
        setMemoryGraphError(null);
        try {
            const res = await fetch(`${memoryApiBase}/api/memory/graph?limit=100`);
            if (!res.ok) {
                let detail = `HTTP ${res.status}`;
                try {
                    const errBody = await res.json();
                    if (errBody && typeof errBody.detail === 'string') detail = errBody.detail;
                    else if (errBody && typeof errBody.detail === 'object') detail = (errBody.detail as unknown[]).map((d: unknown) => (d as { msg?: string }).msg ?? String(d)).join('; ');
                } catch {
                    // ignore parse error
                }
                setMemoryGraphError(detail);
                throw new Error(`Failed to fetch graph: ${detail}`);
            }
            const data = await res.json();
            // Apply collision detection to prevent overlapping nodes
            const nodesWithCollisionFix = applyCollisionDetection(data.nodes || []);
            setMemoryNodes(nodesWithCollisionFix);
            setMemoryEdges(data.edges || []);
        } catch (e) {
            console.error('Failed to fetch memory graph:', e);
            setMemoryNodes([]);
            setMemoryEdges([]);
            setMemoryGraphError((e as Error).message);
        }
        setMemoryLoading(false);
    }, [memoryApiBase]);

    useEffect(() => {
        if (showMemoryModal) {
            fetchMemoryGraph();
        }
    }, [showMemoryModal, fetchMemoryGraph]);

    // Stacked Escape Key Handling
    useEffect(() => {
        const handleKeyDown = (e: KeyboardEvent) => {
            if (e.key === 'Escape') {
                // Check topmost modal first
                if (codeModal) {
                    setCodeModal(null);
                    e.stopPropagation();
                    return;
                }
                if (workflowModal) {
                    setWorkflowModal(null);
                    e.stopPropagation();
                    return;
                }
                if (showMemoryModal) {
                    setShowMemoryModal(false);
                    e.stopPropagation();
                    return;
                }
                if (showCreateAutomationModal) {
                    setShowCreateAutomationModal(false);
                    e.stopPropagation();
                    return;
                }
                if (showUserIdentityModal) {
                    setShowUserIdentityModal(false);
                    setIsEditingUserIdentity(false);
                    setUserIdentityDraft(null);
                    e.stopPropagation();
                    return;
                }
                if (showToolsModal) {
                    setShowToolsModal(false);
                    e.stopPropagation();
                    return;
                }
                if (showWorkflowsModal) {
                    setShowWorkflowsModal(false);
                    e.stopPropagation();
                    return;
                }
                if (showTrustedSourcesModal) {
                    setShowTrustedSourcesModal(false);
                    e.stopPropagation();
                    return;
                }
                if (showCloudDashboard) {
                    setShowCloudDashboard(false);
                    e.stopPropagation();
                    return;
                }
                // Finally close settings
                if (isOpen) {
                    onClose();
                }
            }
        };

        if (isOpen) {
            window.addEventListener('keydown', handleKeyDown);
        }
        return () => window.removeEventListener('keydown', handleKeyDown);
    }, [isOpen, codeModal, workflowModal, showMemoryModal, showCreateAutomationModal, showUserIdentityModal, showToolsModal, showWorkflowsModal, showTrustedSourcesModal, showCloudDashboard, onClose]);

    const handleLogoutYes = useCallback(() => {
        setShowLogoutConfirm(false);
        setLogoutBarProgress(0);
        setIsLoggingOut(true);
        const base = apiBase || (typeof window !== 'undefined' ? '' : 'http://localhost:8001');
        fetch(`${base}/api/auth/logout`, { method: 'POST', credentials: 'include' })
            .then(() => {
                if (typeof window !== 'undefined') sessionStorage.removeItem('vaf_token');
                setTimeout(() => {
                    setIsLoggingOut(false);
                    setLogoutBarProgress(0);
                    onLogout?.();
                }, 1500);
            })
            .catch(() => {
                if (typeof window !== 'undefined') sessionStorage.removeItem('vaf_token');
                setIsLoggingOut(false);
                setLogoutBarProgress(0);
                onLogout?.();
            });
    }, [apiBase, onLogout]);

    // Animate logout progress bar 0 → 100% over 1500ms while token is invalidated
    useEffect(() => {
        if (!isLoggingOut) return;
        setLogoutBarProgress(0);
        const start = requestAnimationFrame(() => {
            setLogoutBarProgress(100);
        });
        return () => cancelAnimationFrame(start);
    }, [isLoggingOut]);

    if (!isOpen) return null;

    const handleChange = (key: string, value: any) => {
        setLocalConfig((prev: any) => ({ ...prev, [key]: value }));
        setChanged(true);
    };

    const handleDiscordComplete = async (discordConfig: DiscordConfig) => {
        handleChange('discord_config', discordConfig);
        setShowDiscordWizard(false);
        // Persist immediately and start the bridge so it shows as connected
        const merged = { ...config, discord_config: discordConfig };
        onSave(merged);
        // Brief delay so save_config is processed before we start the bridge
        await new Promise(r => setTimeout(r, 400));
        try {
            const base = apiBase || '';
            await fetch(`${base}/api/discord/start`, { method: 'POST', credentials: 'include' });
        } catch (e) {
            console.error('Discord bridge start failed:', e);
        }
    };

    const handleTelegramComplete = (telegramConfig: TelegramConfig) => {
        const merged = { ...(localConfig.telegram_config || {}), ...telegramConfig };
        handleChange('telegram_config', merged);
        setShowTelegramWizard(false);
        // Persist immediately so the connection stays after closing/reopening the modal
        onSave({ ...config, telegram_config: merged });
        // Refetch to get latest whitelist from API; keep merged so connection stays visible
        fetch(apiBase ? `${apiBase}/api/config` : '/api/config', { credentials: 'include' })
            .then((r) => r.ok ? r.json() : null)
            .then((full) => {
                setLocalConfig((prev: any) => ({
                    ...prev,
                    telegram_config: {
                        ...(prev.telegram_config || {}),
                        whitelist: full?.telegram_config?.whitelist ?? prev.telegram_config?.whitelist,
                    },
                }));
            })
            .catch(() => {});
    };

    const handleViewCode = async (name: string) => {
        try {
            const res = await fetch(`/api/tools/${encodeURIComponent(name)}/source`);
            if (!res.ok) {
                const text = await res.text();
                alert(`API Error ${res.status}: ${text}`);
                return;
            }
            const data = await res.json();
            if (data.code) {
                setCodeModal({ name, code: data.code });
            } else {
                alert("Could not load code: " + (data.error || "Unknown error"));
            }
        } catch (e) {
            console.error(e);
            alert("Failed to fetch code: " + String(e));
        }
    };

    const handleViewWorkflow = async (id: string) => {
        try {
            // Using ID (filename) is safer than name
            const res = await fetch(`/api/workflows/${encodeURIComponent(id)}`);
            if (!res.ok) throw new Error(await res.text());
            const data = await res.json();
            
            // Build ReactFlow Nodes
            const newNodes = (data.steps || []).map((step: any, idx: number) => ({
                id: step.id,
                type: 'default', // Built-in node type
                data: { label: step.name, code: step.code },
                position: { x: 250, y: idx * 120 + 50 },
                style: { 
                    background: '#fff', 
                    // Highlight first node by default
                    border: idx === 0 ? '2px solid #9333ea' : '1px solid #e5e7eb', 
                    borderRadius: '12px', 
                    padding: '12px',
                    width: 250,
                    fontSize: '13px',
                    fontWeight: 500,
                    // Add glow to first node
                    boxShadow: idx === 0 ? '0 0 0 4px rgba(147, 51, 234, 0.2)' : '0 4px 6px -1px rgb(0 0 0 / 0.1)'
                },
                // Vertical layout
                sourcePosition: Position.Bottom,
                targetPosition: Position.Top,
            }));
            
            // Build Edges
            const newEdges = (data.steps || []).slice(0, -1).map((step: any, idx: number) => ({
                id: `e${idx}-${idx+1}`,
                source: step.id,
                target: data.steps[idx+1].id,
                animated: true,
                style: { stroke: '#9333ea', strokeWidth: 2 }, // Purple
                markerEnd: { type: MarkerType.ArrowClosed, color: '#9333ea' },
            }));

            setNodes(newNodes);
            setEdges(newEdges);
            setWorkflowModal({ ...data, selectedCode: data.steps[0]?.code || "// Select a step to view details" });
        } catch (e) {
            console.error(e);
            alert("Failed to load workflow: " + String(e));
        }
    };
    
    const onNodeClick = (_: any, clickedNode: any) => {
        setWorkflowModal((prev: any) => ({ ...prev, selectedCode: clickedNode.data.code }));
        
        // Highlight selected node
        setNodes((nds) =>
            nds.map((node) => {
                const isSelected = node.id === clickedNode.id;
                return {
                    ...node,
                    style: {
                        ...node.style,
                        border: isSelected ? '2px solid #9333ea' : '1px solid #e5e7eb',
                        boxShadow: isSelected ? '0 0 0 4px rgba(147, 51, 234, 0.2)' : '0 4px 6px -1px rgb(0 0 0 / 0.1)'
                    },
                };
            })
        );
    };

    const handleSave = () => {
        const networkChanged = localConfig.local_network_enabled !== (config?.local_network_enabled || false);
        onSave(localConfig);
        
        if (networkChanged) {
            setIsRestarting(true);
            setTimeout(() => {
                setIsRestarting(false);
                onClose();
            }, 5000);
        } else {
            onClose();
        }
    };

    const handleSearchHF = () => {
        const query = hfQuery.trim() || "text-generation";
        window.open(`https://huggingface.co/models?pipeline_tag=text-generation&sort=downloads&search=${encodeURIComponent(query)}`, '_blank');
    };

    const handleFetchModels = (provider: string) => {
        const apiKey = localConfig[`api_key_${provider}`];
        if (!apiKey) {
            alert(`Please enter an API Key for ${provider} first.`);
            return;
        }
        setFetchingProvider(provider);
        onFetchApiModels(provider, apiKey);
    };

    const handleCreateUser = async () => {
        if (!newUser.username) {
            alert('Username is required');
            return;
        }

        try {
            const res = await fetch('/api/users', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    username: newUser.username,
                    password: newUser.password,
                    email: newUser.email,
                    role: newUser.role,
                    tools: newUser.tools,
                    workflows: newUser.workflows,
                    create_db: newUser.createDb
                })
            });

            if (res.ok) {
                setShowAddUserModal(false);
                // Refresh users list
                setUsersLoading(true);
                fetch('/api/users')
                    .then((res) => (res.ok ? res.json() : []))
                    .then((data) => (Array.isArray(data) ? setUsers(data) : setUsers([])))
                    .catch(() => setUsers([]))
                    .finally(() => setUsersLoading(false));
                
                // Reset form
                setNewUser({ username: '', email: '', role: 'User', password: '', tools: [], workflows: [], createDb: true });
            } else {
                const err = await res.json();
                alert(`Failed to create user: ${err.detail || 'Unknown error'}`);
            }
        } catch (error) {
            console.error('Error creating user:', error);
            alert('Error creating user');
        }
    };

    const handleUpdateUser = async () => {
        if (!editingUser) return;

        try {
            const res = await fetch(`/api/users/${editingUser.id}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    email: editingUser.email,
                    role: editingUser.role,
                    is_active: editingUser.status === 'active',
                })
            });

            if (res.ok) {
                setEditingUser(null);
                // Refresh users list
                setUsersLoading(true);
                fetch('/api/users')
                    .then((res) => (res.ok ? res.json() : []))
                    .then((data) => (Array.isArray(data) ? setUsers(data) : setUsers([])))
                    .catch(() => setUsers([]))
                    .finally(() => setUsersLoading(false));
            } else {
                const err = await res.json();
                alert(`Failed to update user: ${err.detail || 'Unknown error'}`);
            }
        } catch (error) {
            console.error('Error updating user:', error);
            alert('Error updating user');
        }
    };

    const handleDeleteUser = async () => {
        if (!editingUser) return;
        if (!confirm(`Are you sure you want to delete user ${editingUser.username}?`)) return;

        try {
            const res = await fetch(`/api/users/${editingUser.id}`, {
                method: 'DELETE'
            });

            if (res.ok) {
                setEditingUser(null);
                // Refresh users list
                setUsersLoading(true);
                fetch('/api/users')
                    .then((res) => (res.ok ? res.json() : []))
                    .then((data) => (Array.isArray(data) ? setUsers(data) : setUsers([])))
                    .catch(() => setUsers([]))
                    .finally(() => setUsersLoading(false));
            } else {
                const err = await res.json();
                alert(`Failed to delete user: ${err.detail || 'Unknown error'}`);
            }
        } catch (error) {
            console.error('Error deleting user:', error);
            alert('Error deleting user');
        }
    };

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
            {/* Backdrop */}
            <div
                className="absolute inset-0 bg-black/20 backdrop-blur-sm transition-opacity"
                onClick={onClose}
            />

            {/* Modal Window */}
            <div className="relative bg-white/95 backdrop-blur-xl w-full max-w-4xl h-[650px] rounded-2xl shadow-2xl border border-white/20 flex overflow-hidden animate-in fade-in zoom-in-95 duration-200">

                {/* Sidebar */}
                <div className="w-64 bg-gray-50/50 border-r border-gray-200 flex flex-col pt-6 pb-4 px-3 gap-1">
                    <div className="px-3 mb-4">
                        <h2 className="text-sm font-bold text-gray-400 uppercase tracking-wider">{t('settings.title')}</h2>
                    </div>

                    {CATEGORIES.map(cat => {
                        if (cat.adminOnly && currentUser?.role !== 'admin') return null;
                        return (
                        <button
                            key={cat.id}
                            onClick={() => setActiveTab(cat.id)}
                            className={cn(
                                "flex items-center gap-3 px-3 py-2 text-sm font-medium rounded-lg transition-all",
                                activeTab === cat.id
                                    ? "bg-gray-900 text-white shadow-md"
                                    : "text-gray-600 hover:bg-gray-200/50"
                            )}
                        >
                            <cat.icon size={18} />
                            {tTabs(cat.labelKey)}
                        </button>
                    )})}

                    {/* Connection-Indikator – über dem Trennstrich */}
                    <div
                        className={cn(
                            "flex items-center gap-3 px-3 py-2 rounded-lg w-full transition-all mt-auto",
                            !isConnected && onReconnect && "cursor-pointer hover:bg-gray-100"
                        )}
                        onClick={() => { if (!isConnected && onReconnect) onReconnect(); }}
                        title={!isConnected && onReconnect ? 'Click to reconnect' : undefined}
                    >
                        <div
                            className={cn(
                                "w-2.5 h-2.5 rounded-full shrink-0 transition-colors",
                                showIdleState
                                    ? "bg-yellow-400 shadow-[0_0_6px_rgba(234,179,8,0.5)]"
                                    : isConnected
                                        ? "bg-green-500 shadow-[0_0_6px_rgba(34,197,94,0.5)]"
                                        : "bg-red-500 shadow-[0_0_6px_rgba(239,68,68,0.5)]"
                            )}
                        />
                        <span className="text-sm font-medium text-gray-600 truncate">
                            {connectionLabel}
                        </span>
                    </div>

                    {/* Trennstrich, darunter Log out */}
                    <div className="pt-2 border-t border-gray-200">
                        {currentUser && (
                            <button
                                type="button"
                                onClick={() => setShowLogoutConfirm(true)}
                                className="flex items-center gap-3 px-3 py-2 text-sm font-medium rounded-lg transition-all text-gray-600 hover:bg-red-50 hover:text-red-600 w-full"
                            >
                                <LogOut size={18} />
                                {tCommon('logOut')}
                            </button>
                        )}
                    </div>
                </div>

                {/* Content Area */}
                <div className="flex-1 flex flex-col bg-white">
                    {/* Header */}
                    <div className="h-16 border-b border-gray-100 flex items-center justify-between px-8 shrink-0">
                        <h1 className="text-xl font-bold text-gray-800">
                            {(() => { const cat = CATEGORIES.find(c => c.id === activeTab); return cat ? tTabs(cat.labelKey) : ''; })()}
                        </h1>
                        <button onClick={onClose} className="p-2 -mr-2 text-gray-400 hover:text-gray-600 rounded-full hover:bg-gray-100 transition-colors">
                            <X size={20} />
                        </button>
                    </div>

                    {/* Scrollable Form */}
                    <div className="flex-1 overflow-y-auto p-8 space-y-8">

                        {activeTab === 'general' && (
                            <div className="space-y-6">
                                <Section title={tGeneral('apiKeys')}>
                                    <Input
                                        label="Veyllo Key"
                                        value={localConfig.api_key_veyllo || ''}
                                        onChange={(v: string) => handleChange('api_key_veyllo', v)}
                                        type="password"
                                        placeholder="Veyllo API key (server coming soon)"
                                    />
                                    <Input
                                        label={tGeneral('openaiKey')}
                                        value={localConfig.api_key_openai || ''}
                                        onChange={(v: string) => handleChange('api_key_openai', v)}
                                        type="password" placeholder="sk-..."
                                    />
                                    <Input
                                        label={tGeneral('anthropicKey')}
                                        value={localConfig.api_key_anthropic || ''}
                                        onChange={(v: string) => handleChange('api_key_anthropic', v)}
                                        type="password" placeholder="sk-ant-..."
                                    />
                                    <Input
                                        label={tGeneral('deepseekKey')}
                                        value={localConfig.api_key_deepseek || ''}
                                        onChange={(v: string) => handleChange('api_key_deepseek', v)}
                                        type="password"
                                    />
                                    <Input
                                        label={tGeneral('googleKey')}
                                        value={localConfig.api_key_google || ''}
                                        onChange={(v: string) => handleChange('api_key_google', v)}
                                        type="password"
                                    />
                                    <Input
                                        label={tGeneral('openrouterKey')}
                                        value={localConfig.api_key_openrouter || ''}
                                        onChange={(v: string) => handleChange('api_key_openrouter', v)}
                                        type="password"
                                    />
                                </Section>
                                <Section title={tGeneral('webSearch')}>
                                    <p className="text-xs text-gray-500 mb-3">{tGeneral('webSearchDesc')}</p>
                                    <Input
                                        label={tGeneral('braveSearchKey')}
                                        value={localConfig.api_key_brave_search || ''}
                                        onChange={(v: string) => handleChange('api_key_brave_search', v)}
                                        type="password"
                                        placeholder="From api-dashboard.search.brave.com"
                                    />
                                    <Input
                                        label={tGeneral('googleSearchKey')}
                                        value={localConfig.api_key_google_search || ''}
                                        onChange={(v: string) => handleChange('api_key_google_search', v)}
                                        type="password"
                                        placeholder="Cloud Console – Custom Search API"
                                    />
                                    <Input
                                        label={tGeneral('googleSearchEngineId')}
                                        value={localConfig.google_search_engine_id || ''}
                                        onChange={(v: string) => handleChange('google_search_engine_id', v)}
                                        type="text"
                                        placeholder="From Programmable Search Engine control panel"
                                    />
                                </Section>

                                {currentUser?.role === 'admin' && (
                                    <Section title={tGeneral('centralCredentials')}>
                                        <p className="text-xs text-gray-500 mb-4">
                                            {tGeneral('centralCredentialsDesc')}
                                        </p>
                                        <div className="space-y-6">
                                            <div>
                                                <h4 className="text-xs font-semibold text-gray-700 uppercase tracking-wide mb-3">{tGeneral('googleProvider')}</h4>
                                                <div className="grid grid-cols-1 gap-3">
                                                    <Input
                                                        label={tGeneral('googleClientId')}
                                                        value={displayOAuthValue(localConfig.email_oauth_google_client_id || localConfig.cloud_oauth_google_client_id, BUILTIN_GOOGLE_CLIENT_ID)}
                                                        onChange={(v: string) => {
                                                            setLocalConfig((prev: any) => ({
                                                                ...prev,
                                                                email_oauth_google_client_id: v,
                                                                cloud_oauth_google_client_id: v,
                                                            }));
                                                            setChanged(true);
                                                        }}
                                                        type="text"
                                                        placeholder="xxx.apps.googleusercontent.com"
                                                    />
                                                    <Input
                                                        label={tGeneral('googleClientSecret')}
                                                        value={localConfig.email_oauth_google_client_secret || localConfig.cloud_oauth_google_client_secret || ''}
                                                        onChange={(v: string) => {
                                                            setLocalConfig((prev: any) => ({
                                                                ...prev,
                                                                email_oauth_google_client_secret: v,
                                                                cloud_oauth_google_client_secret: v,
                                                            }));
                                                            setChanged(true);
                                                        }}
                                                        type="password"
                                                        placeholder="Optional for desktop apps"
                                                    />
                                                </div>
                                            </div>
                                            <div>
                                                <h4 className="text-xs font-semibold text-gray-700 uppercase tracking-wide mb-3">{tGeneral('microsoftProvider')}</h4>
                                                <div className="grid grid-cols-1 gap-3">
                                                    <Input
                                                        label={tGeneral('microsoftClientId')}
                                                        value={localConfig.email_oauth_microsoft_client_id || localConfig.cloud_oauth_microsoft_client_id || ''}
                                                        onChange={(v: string) => {
                                                            setLocalConfig((prev: any) => ({
                                                                ...prev,
                                                                email_oauth_microsoft_client_id: v,
                                                                cloud_oauth_microsoft_client_id: v,
                                                            }));
                                                            setChanged(true);
                                                        }}
                                                        type="text"
                                                        placeholder="Azure App Registration"
                                                    />
                                                    <Input
                                                        label={tGeneral('microsoftClientSecret')}
                                                        value={localConfig.email_oauth_microsoft_client_secret || localConfig.cloud_oauth_microsoft_client_secret || ''}
                                                        onChange={(v: string) => {
                                                            setLocalConfig((prev: any) => ({
                                                                ...prev,
                                                                email_oauth_microsoft_client_secret: v,
                                                                cloud_oauth_microsoft_client_secret: v,
                                                            }));
                                                            setChanged(true);
                                                        }}
                                                        type="password"
                                                    />
                                                </div>
                                            </div>
                                            <div>
                                                <h4 className="text-xs font-semibold text-gray-700 uppercase tracking-wide mb-3">{tGeneral('appleProvider')}</h4>
                                                <div className="grid grid-cols-1 gap-3">
                                                    <Input label={tGeneral('appleClientId')} value={localConfig.email_oauth_apple_client_id || ''} onChange={(v: string) => handleChange('email_oauth_apple_client_id', v)} type="text" placeholder="iCloud Mail OAuth (if available)" />
                                                    <Input label={tGeneral('appleClientSecret')} value={localConfig.email_oauth_apple_client_secret || ''} onChange={(v: string) => handleChange('email_oauth_apple_client_secret', v)} type="password" />
                                                </div>
                                            </div>
                                            <div>
                                                <h4 className="text-xs font-semibold text-gray-700 uppercase tracking-wide mb-3">{tGeneral('dropboxProvider')}</h4>
                                                <div className="grid grid-cols-1 gap-3">
                                                    <Input label={tGeneral('dropboxAppKey')} value={localConfig.cloud_oauth_dropbox_client_id || ''} onChange={(v: string) => handleChange('cloud_oauth_dropbox_client_id', v)} type="text" placeholder="Dropbox App Console" />
                                                    <Input label={tGeneral('dropboxAppSecret')} value={localConfig.cloud_oauth_dropbox_client_secret || ''} onChange={(v: string) => handleChange('cloud_oauth_dropbox_client_secret', v)} type="password" />
                                                </div>
                                            </div>
                                        </div>
                                    </Section>
                                )}
                            </div>
                        )}

                        {activeTab === 'persona' && (
                            <div className="space-y-6 animate-in fade-in duration-300">
                                {personaLoading ? (
                                    <div className="flex justify-center py-12"><div className="w-8 h-8 border-2 border-gray-200 border-t-gray-900 rounded-full animate-spin" /></div>
                                ) : (
                                    <>
                                        <Section title={tPersona('identity')}>
                                            <div className="grid grid-cols-2 gap-4">
                                                <Input
                                                    label={tPersona('agentName')}
                                                    value={personaData?.identity?.name || ''}
                                                    onChange={(v) => {
                                                        const newIdentity = { ...personaData?.identity, name: v };
                                                        setPersonaData({ ...personaData!, identity: newIdentity });
                                                        fetch('/api/user/identity', {
                                                            method: 'PUT',
                                                            headers: { 'Content-Type': 'application/json' },
                                                            body: JSON.stringify(newIdentity)
                                                        });
                                                    }}
                                                />
                                                <Input
                                                    label={tPersona('emojiSymbol')}
                                                    value={personaData?.identity?.emoji || ''}
                                                    onChange={(v) => {
                                                        const newIdentity = { ...personaData?.identity, emoji: v };
                                                        setPersonaData({ ...personaData!, identity: newIdentity });
                                                        fetch('/api/user/identity', {
                                                            method: 'PUT',
                                                            headers: { 'Content-Type': 'application/json' },
                                                            body: JSON.stringify(newIdentity)
                                                        });
                                                    }}
                                                />
                                            </div>
                                        </Section>

                                        <Section title={tPersona('soul')}>
                                            <div className="flex justify-between items-center mb-2">
                                                <p className="text-xs text-gray-500">{tPersona('soulDesc')}</p>
                                                <button
                                                    onClick={() => setShowSoulWizard(true)}
                                                    className="text-xs px-2 py-1 bg-purple-50 text-purple-600 rounded-lg hover:bg-purple-100 transition-colors flex items-center gap-1 font-medium"
                                                >
                                                    <Wand2 size={12} /> {tPersona('createWithWizard')}
                                                </button>
                                            </div>
                                            <textarea
                                                className="w-full h-64 p-4 bg-gray-50 border border-gray-200 rounded-xl font-mono text-sm focus:outline-none focus:ring-2 focus:ring-gray-400 resize-none"
                                                value={personaData?.soul || ''}
                                                onChange={(e) => setPersonaData({ ...personaData!, soul: e.target.value })}
                                                onBlur={() => fetch('/api/user/soul', {
                                                    method: 'PUT',
                                                    headers: { 'Content-Type': 'application/json' },
                                                    body: JSON.stringify({ content: personaData?.soul })
                                                })}
                                            />
                                        </Section>

                                        <Section title={tPersona('longTermMemory')}>
                                            <div className="flex flex-wrap items-center gap-4">
                                                <div className="flex items-center gap-2">
                                                    <span className="text-sm font-medium text-gray-700">{tPersona('maxRagSnippets')}</span>
                                                    <input
                                                        type="number"
                                                        min={1}
                                                        max={20}
                                                        value={localConfig.memory_rag_k ?? 5}
                                                        onChange={(e) => handleChange('memory_rag_k', Math.max(1, Math.min(20, parseInt(e.target.value) || 5)))}
                                                        className="w-14 px-2 h-9 bg-white border border-gray-200 rounded-lg text-sm text-center focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-gray-500"
                                                    />
                                                </div>
                                                <div className="flex items-center gap-2">
                                                    <span className="text-sm font-medium text-gray-700">{tPersona('minRelevance')}</span>
                                                    <input
                                                        type="number"
                                                        min={0}
                                                        max={100}
                                                        value={Math.round((localConfig.memory_rag_threshold ?? 0.3) * 100)}
                                                        onChange={(e) => {
                                                            const pct = Math.max(0, Math.min(100, parseInt(e.target.value) || 0));
                                                            handleChange('memory_rag_threshold', pct / 100);
                                                        }}
                                                        className="w-14 px-2 h-9 bg-white border border-gray-200 rounded-lg text-sm text-center focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-gray-500"
                                                    />
                                                </div>
                                            </div>
                                            <p className="text-xs text-gray-400 mt-1">{tPersona('ragDesc')}</p>
                                            <div className="flex flex-wrap gap-2 mt-3">
                                                <button
                                                    onClick={() => setShowMemoryModal(true)}
                                                    disabled={!localConfig.memory_enabled}
                                                    className={cn(
                                                        "text-sm px-3 py-2 rounded-lg transition-colors flex items-center gap-2",
                                                        localConfig.memory_enabled
                                                            ? "bg-purple-50 text-purple-600 hover:bg-purple-100"
                                                            : "bg-gray-100 text-gray-400 cursor-not-allowed"
                                                    )}
                                                >
                                                    <Brain size={16} /> {tPersona('viewGraph')}
                                                </button>
                                                <button
                                                    type="button"
                                                    onClick={() => setShowUserIdentityModal(true)}
                                                    className="text-sm px-3 py-2 rounded-lg transition-colors flex items-center gap-2 bg-amber-100 text-amber-700 hover:bg-amber-200"
                                                >
                                                    <User size={16} /> {tPersona('userIdentity')}
                                                </button>
                                            </div>
                                        </Section>
                                    </>
                                )}
                            </div>
                        )}

                        {activeTab === 'ai' && (
                            <div className="space-y-6">
                                <Section title={tAi('provider')}>
                                    <Select
                                        label={tAi('primaryProvider')}
                                        value={localConfig.provider || 'local'}
                                        onChange={(v: string) => handleChange('provider', v)}
                                        options={[
                                            { value: 'local', label: tAi('localLlama') },
                                            ...PROVIDERS.map(p => ({ value: p.id, label: p.label }))
                                        ]}
                                    />
                                </Section>

                                {(!localConfig.provider || localConfig.provider === 'local') && (
                                    <Section title={tAi('localModelSettings')}>
                                        <div className="flex gap-2 items-end">
                                            <div className="flex-1">
                                                <Select
                                                    label={tAi('localModelFile')}
                                                    value={localConfig.model || ''}
                                                    onChange={(v: string) => handleChange('model', v)}
                                                    options={[
                                                        { value: '', label: tAi('selectModel') },
                                                        ...availableModels.map(m => ({ value: m, label: m }))
                                                    ]}
                                                />
                                            </div>
                                            <button
                                                onClick={onRefreshLocalModels}
                                                className="px-3 bg-gray-100 text-gray-600 hover:bg-gray-200 rounded-lg transition-colors h-10 flex items-center justify-center"
                                                title={tAi('refreshLocalModels')}
                                            >
                                                <RefreshCw size={18} />
                                            </button>
                                        </div>
                                        <p className="text-xs text-gray-400 mt-1 mb-4">{tAi('modelsDir')}</p>

                                        <div className="grid grid-cols-2 gap-4 mt-4">
                                            <Input
                                                label={tAi('contextWindow')}
                                                value={localConfig.n_ctx || 8192}
                                                onChange={(v: string) => handleChange('n_ctx', parseInt(v))}
                                                type="number"
                                            />
                                            <Input
                                                label={tAi('gpuLayers')}
                                                value={localConfig.gpu_layers ?? -1}
                                                onChange={(v: string) => handleChange('gpu_layers', parseInt(v))}
                                                type="number"
                                            />
                                        </div>
                                        <div className="mt-4">
                                            <Switch
                                                label={tAi('promptCacheAuto')}
                                                description={tAi('promptCacheAutoDesc')}
                                                checked={localConfig.llama_cache_ram === -1}
                                                onChange={(v: boolean) => handleChange('llama_cache_ram', v ? -1 : 4096)}
                                            />
                                            {localConfig.llama_cache_ram !== -1 && (
                                                <div className="mt-3">
                                                    <Input
                                                        label={tAi('promptCacheRam')}
                                                        value={localConfig.llama_cache_ram ?? 4096}
                                                        onChange={(v: string) => {
                                                            const n = parseInt(v, 10);
                                                            if (!Number.isNaN(n)) handleChange('llama_cache_ram', Math.max(0, Math.min(16384, n)));
                                                        }}
                                                        type="number"
                                                    />
                                                    <p className="text-xs text-gray-400 mt-1">{tAi('promptCacheRamDesc')}</p>
                                                </div>
                                            )}
                                            {localConfig.llama_cache_ram === -1 && (
                                                <p className="text-xs text-gray-400 mt-2">{tAi('promptCacheAutoInfo')}</p>
                                            )}
                                        </div>
                                    </Section>
                                )}

                                {PROVIDERS.map(p => {
                                    if (localConfig.provider !== p.id) return null;
                                    const hasKey = !!localConfig[`api_key_${p.id}`];
                                    
                                    return (
                                        <Section key={p.id} title={`${p.label} – ${tAi('provider')}`}>
                                            {!hasKey && (
                                                <div className="p-3 bg-yellow-50 text-yellow-700 text-sm rounded-lg mb-4 flex items-center gap-2">
                                                    <Shield size={16} />
                                                    <span>{tAi('apiKeyMissing')}</span>
                                                </div>
                                            )}
                                            <div className="flex gap-2 items-end">
                                                <div className="flex-1">
                                                    <Select
                                                        label={`${p.label} Model`}
                                                        value={localConfig[`api_model_${p.id}`] || p.defaultModel}
                                                        onChange={(v: string) => handleChange(`api_model_${p.id}`, v)}
                                                        options={[
                                                            { value: p.defaultModel, label: `${p.defaultModel} (Default)` },
                                                            ...(apiModels[p.id] ? apiModels[p.id].map(m => ({ value: m, label: m })) : [])
                                                        ]}
                                                    />
                                                </div>
                                                <button
                                                    onClick={() => handleFetchModels(p.id)}
                                                    className={cn(
                                                        "px-3 bg-gray-100 text-gray-600 hover:bg-gray-200 rounded-lg transition-colors h-10 flex items-center justify-center",
                                                        fetchingProvider === p.id && "animate-pulse"
                                                    )}
                                                    title={tAi('fetchModels')}
                                                    disabled={!hasKey}
                                                >
                                                    <RefreshCw size={18} className={cn(fetchingProvider === p.id && "animate-spin")} />
                                                </button>
                                            </div>
                                        </Section>
                                    );
                                })}

                                <div className="mt-4 p-4 bg-gray-50 rounded-lg border border-gray-100">
                                    <div className="flex items-center justify-between mb-3">
                                        <label className="text-sm font-medium text-gray-700">{tAi('temperature')}</label>
                                        <div className="flex items-center gap-2">
                                            <span className="text-xs text-gray-500">{tAi('auto')}</span>
                                            <button
                                                onClick={() => handleChange('temperature_auto', !localConfig.temperature_auto)}
                                                className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors ${
                                                    localConfig.temperature_auto ? 'bg-blue-500' : 'bg-gray-300'
                                                }`}
                                            >
                                                <span
                                                    className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                                                        localConfig.temperature_auto ? 'translate-x-4' : 'translate-x-1'
                                                    }`}
                                                />
                                            </button>
                                        </div>
                                    </div>
                                    {localConfig.temperature_auto ? (
                                        <div className="bg-blue-50 border border-blue-200 rounded-lg p-3">
                                            <div className="flex items-center gap-2 text-blue-700">
                                                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" />
                                                </svg>
                                                <span className="text-sm font-medium">{tAi('adaptiveTemperature')}</span>
                                            </div>
                                            <p className="text-xs text-blue-600 mt-1">
                                                {tAi('adaptiveDesc')}
                                            </p>
                                            <ul className="text-xs text-blue-600 mt-1 space-y-0.5 ml-4 list-disc">
                                                <li><strong>0.1-0.3:</strong> {tAi('tempMath')}</li>
                                                <li><strong>0.4-0.6:</strong> {tAi('tempGeneral')}</li>
                                                <li><strong>0.7-0.9:</strong> {tAi('tempCreative')}</li>
                                            </ul>
                                        </div>
                                    ) : (
                                        <>
                                            <div className="flex items-center justify-between mb-1">
                                                <span className="text-xs text-gray-500">Manual: {localConfig.temperature ?? 0.7}</span>
                                            </div>
                                            <input
                                                type="range" min="0" max="2" step="0.1"
                                                value={localConfig.temperature ?? 0.7}
                                                onChange={(e) => handleChange('temperature', parseFloat(e.target.value))}
                                                className="w-full accent-blue-500"
                                            />
                                            <div className="flex justify-between text-xs text-gray-400 mt-1">
                                                <span>{tAi('strict')}</span>
                                                <span>{tAi('balanced')}</span>
                                                <span>{tAi('creative')}</span>
                                            </div>
                                        </>
                                    )}
                                </div>
                            </div>
                        )}

                        {activeTab === 'voice' && (
                            <div className="space-y-6">
                                <Section title={tVoice('stt')}>
                                    <Switch
                                        label={tVoice('enableVoiceInput')}
                                        checked={localConfig.stt_enabled || false}
                                        onChange={(v: boolean) => handleChange('stt_enabled', v)}
                                    />
                                    {localConfig.stt_enabled && (
                                        <div className="mt-4 space-y-4">
                                            <Select
                                                label={tVoice('sttEngine')}
                                                value={localConfig.speech_stt_engine ?? 'docker'}
                                                onChange={(v: string) => handleChange('speech_stt_engine', v)}
                                                options={[
                                                    { value: 'docker', label: tVoice('dockerStt') },
                                                    { value: 'local', label: tVoice('localStt') },
                                                ]}
                                            />
                                            {(localConfig.speech_stt_engine ?? 'docker') === 'docker' && (
                                                <Input
                                                    label={tVoice('dockerSttUrl')}
                                                    placeholder="http://localhost:5003"
                                                    value={localConfig.speech_stt_docker_url || 'http://localhost:5003'}
                                                    onChange={(v: string) => handleChange('speech_stt_docker_url', v)}
                                                />
                                            )}
                                            <p className="text-xs text-gray-500">
                                                {tVoice('sttDesc')}
                                            </p>
                                        </div>
                                    )}
                                </Section>

                                <Section title={tVoice('tts')}>
                                    <TTSSettings
                                        ttsEnabled={localConfig.speech_tts_enabled || false}
                                        ttsUrl={localConfig.speech_tts_docker_url || 'http://localhost:5002'}
                                        autoSpeak={localConfig.tts_auto_speak || false}
                                        onTtsEnabledChange={(v: boolean) => {
                                            handleChange('speech_tts_enabled', v);
                                            if (v) handleChange('speech_tts_engine', 'docker');
                                        }}
                                        onTtsUrlChange={(v: string) => {
                                            handleChange('speech_tts_docker_url', v);
                                            handleChange('speech_tts_engine', 'docker');
                                        }}
                                        onAutoSpeakChange={(v: boolean) => handleChange('tts_auto_speak', v)}
                                    />
                                </Section>
                            </div>
                        )}

                        {activeTab === 'interface' && (
                            <div className="space-y-6">
                                <Section title={tInterface('language')}>
                                    <p className="text-xs text-gray-500 mb-3">{tInterface('preferredLanguage')} (UI)</p>
                                    <select
                                        value={uiLocale}
                                        onChange={(e) => setUiLocale(e.target.value)}
                                        className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-amber-500 focus:border-transparent text-sm"
                                    >
                                        {languages.map((lang) => (
                                            <option key={lang.code} value={lang.code}>{lang.flag} {lang.name}</option>
                                        ))}
                                    </select>
                                </Section>
                                <Section title={tInterface('dateTime')}>
                                    <p className="text-xs text-gray-500 mb-4">{tInterface('dateTimeDesc')}</p>
                                    {personaLoading ? (
                                        <p className="text-sm text-gray-500">Loading…</p>
                                    ) : (
                                        <>
                                            <div className="grid grid-cols-1 gap-4">
                                                <Select
                                                    label={tInterface('timezone')}
                                                    value={dateTimeTimezone}
                                                    onChange={(v: string) => setDateTimeTimezone(v)}
                                                    options={DATE_TIME_TIMEZONES}
                                                />
                                                <Select
                                                    label={tInterface('dateFormat')}
                                                    value={dateTimeDateFormat}
                                                    onChange={(v: string) => setDateTimeDateFormat(v)}
                                                    options={DATE_TIME_DATE_FORMATS}
                                                />
                                                <Select
                                                    label={tInterface('timeFormat')}
                                                    value={dateTimeTimeFormat}
                                                    onChange={(v: string) => setDateTimeTimeFormat(v)}
                                                    options={DATE_TIME_TIME_FORMATS}
                                                />
                                            </div>
                                            <button
                                                type="button"
                                                onClick={saveDateTimeSettings}
                                                disabled={dateTimeSaving}
                                                className="mt-3 px-4 py-2 bg-gray-800 text-white rounded-lg hover:bg-gray-700 disabled:opacity-50 text-sm font-medium"
                                            >
                                                {dateTimeSaving ? tCommon('saving') : tCommon('save')}
                                            </button>
                                        </>
                                    )}
                                </Section>
                                <Section title={tInterface('automation')}>
                                    <Switch
                                        label={tInterface('autoOpenLinks')}
                                        description={tInterface('autoOpenLinksDesc')}
                                        checked={localConfig.ux_auto_open_links ?? false}
                                        onChange={(v: boolean) => handleChange('ux_auto_open_links', v)}
                                    />
                                    <div className="h-4" />
                                    <Switch
                                        label={tInterface('autoOpenOutputs')}
                                        description={tInterface('autoOpenOutputsDesc')}
                                        checked={localConfig.ux_auto_open_outputs ?? true}
                                        onChange={(v: boolean) => handleChange('ux_auto_open_outputs', v)}
                                    />
                                    {localConfig.ux_auto_open_outputs && (
                                        <div className="mt-2 pl-4 border-l-2 border-gray-100 animate-in slide-in-from-top-1 fade-in">
                                            <Input
                                                label={tInterface('maxLimit')}
                                                value={localConfig.ux_auto_open_max || 20}
                                                onChange={(v: string) => handleChange('ux_auto_open_max', parseInt(v))}
                                                type="number"
                                            />
                                        </div>
                                    )}
                                    <div className="h-4" />
                                    <Switch
                                        label={tInterface('separateTerminals')}
                                        description={tInterface('separateTerminalsDesc')}
                                        checked={localConfig.sub_agents_in_separate_terminals ?? true}
                                        onChange={(v: boolean) => handleChange('sub_agents_in_separate_terminals', v)}
                                    />
                                </Section>
                            </div>
                        )}

                        {activeTab === 'connections' && (
                            <ConnectionsPanel
                                config={localConfig}
                                onConfigChange={handleChange}
                                currentUser={currentUser}
                                refreshTrigger={cloudDashboardRefresh}
                                onOpenDiscordWizard={() => setShowDiscordWizard(true)}
                                onOpenDiscordDashboard={() => setShowDiscordDashboard(true)}
                                onOpenTelegramWizard={() => setShowTelegramWizard(true)}
                                onOpenWhatsAppWizard={() => setShowWhatsAppWizard(true)}
                                onOpenWhatsAppDashboard={() => setShowWhatsAppDashboard(true)}
                                onOpenTelegramDashboard={() => setShowTelegramDashboard(true)}
                                onOpenEmailDashboard={() => setShowMailDashboard(true)}
                                onOpenEmailWizard={() => setShowEmailWizard(true)}
                                onOpenCloudDashboard={() => setShowCloudDashboard(true)}
                                onOpenCloudWizard={(provider?: string) => {
                                    setCloudWizardProvider(provider);
                                    setShowCloudWizard(true);
                                }}
                                onOpenContactsDashboard={() => setShowContactsDashboard(true)}
                            />
                        )}

                        {activeTab === 'local_network' && (
                            <div className="space-y-6 animate-in fade-in slide-in-from-right-4 duration-300">
                                <Section title={tLocalNet('networkSettings')}>
                                     <Switch
                                        label={tLocalNet('enableHosting')}
                                        description={tLocalNet('enableHostingDesc')}
                                        checked={localConfig.local_network_enabled || false}
                                        onChange={(v: boolean) => {
                                            if (v) {
                                                setShowNetworkWarning(true);
                                            } else {
                                                handleChange('local_network_enabled', false);
                                            }
                                        }}
                                    />
                                    {/* Access URL – LAN IP link so other devices can reach VAF (localhost only works on this PC) */}
                                    {(() => {
                                        const protocol = typeof window !== 'undefined' ? window.location.protocol : 'http:';
                                        const host = displayHost || '';
                                        const port = displayPort && displayPort !== '80' && displayPort !== '443' ? displayPort : '';
                                        const fallbackUrl = host ? `${protocol}//${host}${port ? `:${port}` : ''}` : '';
                                        const accessUrl = networkAccessUrl || fallbackUrl;
                                        const isLanUrl = !!networkAccessUrl;
                                        if (!accessUrl && !fallbackUrl) return null;
                                        return (
                                            <div className={cn(
                                                "mt-4 p-4 rounded-xl border flex flex-col gap-2",
                                                localConfig.local_network_enabled
                                                    ? "bg-green-50/50 border-green-200"
                                                    : "bg-gray-50 border-gray-200"
                                            )}>
                                                <div className="flex items-center justify-between gap-3">
                                                    <div className="flex-1 min-w-0">
                                                        <div className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-1">
                                                            {isLanUrl ? tLocalNet('networkAccessLinkLan') : tLocalNet('networkAccessLink')}
                                                        </div>
                                                        <div className="font-mono text-sm text-gray-900 break-all">{accessUrl}</div>
                                                        {!localConfig.local_network_enabled && (
                                                            <div className="text-xs text-gray-500 mt-1">{tLocalNet('enableHostingHint')}</div>
                                                        )}
                                                        {localConfig.local_network_enabled && isLanUrl && (
                                                            <div className="text-xs text-green-700 mt-1">{tLocalNet('lanUrlHint')}</div>
                                                        )}
                                                        {localConfig.local_network_enabled && !isLanUrl && (
                                                            <div className="text-xs text-amber-700 mt-1">{tLocalNet('localhostHint')}</div>
                                                        )}
                                                    </div>
                                                    <button
                                                        type="button"
                                                        onClick={() => {
                                                            if (accessUrl && navigator.clipboard) {
                                                                navigator.clipboard.writeText(accessUrl);
                                                                setNetworkLinkCopied(true);
                                                                setTimeout(() => setNetworkLinkCopied(false), 2000);
                                                            }
                                                        }}
                                                        disabled={!localConfig.local_network_enabled}
                                                        className={cn(
                                                            "shrink-0 p-2.5 rounded-lg border transition-colors flex items-center gap-2",
                                                            localConfig.local_network_enabled
                                                                ? "bg-white border-gray-200 text-gray-700 hover:bg-gray-50 hover:border-gray-300"
                                                                : "bg-gray-100 border-gray-100 text-gray-400 cursor-not-allowed"
                                                        )}
                                                        title="Copy link"
                                                    >
                                                        {networkLinkCopied ? (
                                                            <Check size={16} className="text-green-600" />
                                                        ) : (
                                                            <Copy size={16} />
                                                        )}
                                                        <span className="text-xs font-medium hidden sm:inline">{networkLinkCopied ? tCommon('copied') : tCommon('copy')}</span>
                                                    </button>
                                                </div>
                                            </div>
                                        );
                                    })()}
                                </Section>
                                
                                <div className={cn("space-y-6 transition-all duration-300", !localConfig.local_network_enabled && "opacity-50 pointer-events-none grayscale-[0.5]")}>
                                    <Section title={tLocalNet('userManagement')}>
                                        <div className="flex flex-col gap-4">
                                            {/* Toolbar */}
                                            <div className="flex items-center justify-between">
                                                <div className="relative max-w-xs w-full">
                                                    <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
                                                    <input 
                                                        type="text" 
                                                        placeholder={tLocalNet('searchUsers')}
                                                        value={userSearch}
                                                        onChange={(e) => setUserSearch(e.target.value)}
                                                        className="w-full pl-9 pr-4 py-2 bg-white border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-gray-500 transition-all"
                                                    />
                                                </div>
                                                <button 
                                                    onClick={() => setShowAddUserModal(true)}
                                                    className="px-4 py-2 bg-green-500 hover:bg-green-600 text-white font-medium rounded-lg text-sm shadow-sm hover:shadow transition-all flex items-center gap-2"
                                                >
                                                    <Plus size={16} /> {tLocalNet('addNewUser')}
                                                </button>
                                            </div>

                                            {/* Table */}
                                            <div className="bg-white border border-gray-200 rounded-lg overflow-hidden shadow-sm">
                                                <table className="w-full text-sm text-left">
                                                    <thead className="bg-gray-50 text-gray-500 font-medium border-b border-gray-200">
                                                        <tr>
                                                            <th className="px-4 py-3 font-semibold">{tLocalNet('username')}</th>
                                                            <th className="px-4 py-3 font-semibold">{tLocalNet('role')}</th>
                                                            <th className="px-4 py-3 font-semibold">{tLocalNet('lastActive')}</th>
                                                            <th className="px-4 py-3 font-semibold">{tLocalNet('status')}</th>
                                                            <th className="px-4 py-3 font-semibold text-right">{tLocalNet('actions')}</th>
                                                        </tr>
                                                    </thead>
                                                    <tbody className="divide-y divide-gray-100">
                                                        {usersLoading ? (
                                                            <tr>
                                                                <td colSpan={5} className="px-4 py-8 text-center text-gray-500">
                                                                    {tLocalNet('loadingUsers')}
                                                                </td>
                                                            </tr>
                                                        ) : users.filter(u => 
                                                            u.username.toLowerCase().includes(userSearch.toLowerCase()) || 
                                                            (u.role && u.role.toLowerCase().includes(userSearch.toLowerCase()))
                                                        ).map((user, i) => (
                                                            <tr key={i} className="hover:bg-gray-50 transition-colors group">
                                                                <td onClick={() => setSelectedUser(user)} className="px-4 py-3 font-medium text-gray-900 flex items-center gap-2 cursor-pointer">
                                                                    <div className="w-8 h-8 rounded-full bg-gray-100 flex items-center justify-center text-xs text-gray-600 font-bold border border-gray-200">
                                                                        {user.username[0].toUpperCase()}
                                                                    </div>
                                                                    {user.username}
                                                                </td>
                                                                <td className="px-4 py-3 text-gray-600">{user.role}</td>
                                                                <td className="px-4 py-3 text-gray-500">{user.lastActive}</td>
                                                                <td className="px-4 py-3">
                                                                    <span className={cn(
                                                                        "px-2 py-1 rounded-full text-xs font-medium border flex items-center w-fit gap-1.5",
                                                                        user.status === 'active' 
                                                                            ? "bg-green-50 text-green-700 border-green-200" 
                                                                            : "bg-gray-50 text-gray-600 border-gray-200"
                                                                    )}>
                                                                        <div className={cn("w-1.5 h-1.5 rounded-full", user.status === 'active' ? "bg-green-500" : "bg-gray-400")} />
                                                                        {user.status === 'active' ? tCommon('active') : tCommon('inactive')}
                                                                    </span>
                                                                </td>
                                                                <td className="px-4 py-3 text-right">
                                                                    <div className="flex items-center justify-end gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                                                                        <button 
                                                                            onClick={() => setEditingUser(user)}
                                                                            className="p-1.5 text-gray-500 hover:text-gray-800 hover:bg-gray-100 rounded-lg transition-colors"
                                                                            title="Edit User"
                                                                        >
                                                                            <Edit size={16} />
                                                                        </button>
                                                                        <button 
                                                                            className="p-1.5 text-gray-500 hover:text-red-600 hover:bg-red-50 rounded-lg transition-colors"
                                                                            title="Delete User"
                                                                        >
                                                                            <Trash2 size={16} />
                                                                        </button>
                                                                    </div>
                                                                </td>
                                                            </tr>
                                                        ))}
                                                    </tbody>
                                                </table>
                                                {!usersLoading && users.filter(u => 
                                                    u.username.toLowerCase().includes(userSearch.toLowerCase()) || 
                                                    (u.role && u.role.toLowerCase().includes(userSearch.toLowerCase()))
                                                ).length === 0 && (
                                                    <div className="p-8 text-center text-gray-500">
                                                        {users.length === 0
                                                            ? tLocalNet('noUsersYet')
                                                            : tLocalNet('noUsersFound', { query: userSearch })}
                                                    </div>
                                                )}
                                            </div>
                                        </div>
                                    </Section>

                                    <Section title={tLocalNet('connectionDetails')}>
                                        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                                            <div className="flex flex-col gap-1.5 w-full">
                                                <label className="text-sm font-medium text-gray-700 ml-1">{tLocalNet('hostIp')}</label>
                                                <div className="px-4 h-10 flex items-center bg-gray-50 border border-gray-200 rounded-lg text-sm text-gray-700 font-mono select-all">
                                                    {networkAccessUrl ? new URL(networkAccessUrl).hostname : (displayHost || 'localhost')}
                                                </div>
                                            </div>
                                            <div className="flex flex-col gap-1.5 w-full">
                                                <label className="text-sm font-medium text-gray-700 ml-1">{tLocalNet('port')}</label>
                                                <input
                                                    type="number"
                                                    value={localConfig.local_network_port_frontend || 3000}
                                                    onChange={(e) => handleChange('local_network_port_frontend', parseInt(e.target.value))}
                                                    className="px-4 h-10 bg-white border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-gray-500 transition-all text-gray-700 font-mono"
                                                />
                                            </div>
                                        </div>
                                        
                                        {/* Security Status Info */}
                                        <div className="mt-4 p-3 bg-blue-50 border border-blue-100 rounded-lg flex flex-col gap-2">
                                            <div className="flex items-center gap-2 text-blue-800 font-medium text-sm">
                                                <Shield size={16} />
                                                {tLocalNet('securityStatus')}
                                            </div>
                                            <div className="grid grid-cols-1 gap-2 pl-6">
                                                <div className="flex items-center gap-2 text-xs text-blue-700">
                                                    <CheckCircle size={12} className="text-blue-600" />
                                                    <span>{tLocalNet('firewallActive')}</span>
                                                </div>
                                                <div className="flex items-center gap-2 text-xs text-blue-700">
                                                    <CheckCircle size={12} className="text-blue-600" />
                                                    <span>{tLocalNet('authRequired')}</span>
                                                </div>
                                                <div className="flex items-center gap-2 text-xs text-blue-700">
                                                    <CheckCircle size={12} className="text-blue-600" />
                                                    <span>{tLocalNet('noPublicAccess')}</span>
                                                </div>
                                            </div>
                                        </div>
                                    </Section>

                                    <Section title={tLocalNet('networkTopology')}>
                                        <div className="space-y-3">
                                            <p className="text-xs text-gray-500 mb-2">{tLocalNet('networkTopologyDesc')}</p>
                                            
                                            <button 
                                                onClick={() => setShowNetworkModal(true)}
                                                className="w-full h-48 bg-gray-50 hover:bg-gray-100 border border-gray-200 hover:border-gray-300 rounded-xl transition-all flex flex-col items-center justify-center gap-4 group relative overflow-hidden"
                                            >
                                                {/* Visual hint of a map/grid */}
                                                <div className="absolute inset-0 opacity-[0.03] pointer-events-none" style={{ backgroundImage: 'radial-gradient(#000 1px, transparent 1px)', backgroundSize: '20px 20px' }} />
                                                
                                                <div className="w-16 h-16 rounded-full bg-white border border-gray-200 flex items-center justify-center shadow-sm group-hover:scale-110 transition-transform z-10">
                                                    <Network size={32} className="text-gray-600" />
                                                </div>
                                                <div className="text-center z-10">
                                                    <div className="font-bold text-gray-900 text-lg">{tLocalNet('viewNetworkMap')}</div>
                                                    <div className="text-sm text-gray-500 mt-1">{tLocalNet('activeDevices', { count: networkNodes.length })}</div>
                                                </div>
                                                
                                                <div className="absolute bottom-4 right-4 flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-wider text-blue-600 bg-blue-50 px-2 py-1 rounded-md border border-blue-100">
                                                    <div className="w-1.5 h-1.5 rounded-full bg-blue-500 animate-pulse" />
                                                    {tLocalNet('liveView')}
                                                </div>
                                            </button>
                                        </div>
                                    </Section>
                                </div>
                            </div>
                        )}

                        {activeTab === 'advanced' && (
                            <div className="space-y-6">
                                <Section title={tAdvanced('subAgents')}>
                                    <Switch
                                        label={tInterface('separateTerminals')}
                                        description={tInterface('separateTerminalsDesc')}
                                        checked={localConfig.sub_agents_in_separate_terminals ?? true}
                                        onChange={(v: boolean) => handleChange('sub_agents_in_separate_terminals', v)}
                                    />
                                    <div className="mt-4">
                                        <Select
                                            label={tAdvanced('subAgentProvider')}
                                            value={localConfig.subagent_provider || 'inherit'}
                                            onChange={(v: string) => handleChange('subagent_provider', v)}
                                            options={[
                                                { value: 'inherit', label: tAdvanced('sameAsMain') },
                                                { value: 'openai', label: 'OpenAI' },
                                                { value: 'anthropic', label: 'Anthropic' },
                                                { value: 'deepseek', label: 'DeepSeek' },
                                                { value: 'local', label: 'Local' },
                                            ]}
                                        />
                                    </div>
                                    <div className="h-4" />
                                    <Switch
                                        label={tAdvanced('subAgentTimeout')}
                                        description={tAdvanced('subAgentTimeoutDesc')}
                                        checked={localConfig.subagent_timeout_enabled ?? true}
                                        onChange={(v: boolean) => handleChange('subagent_timeout_enabled', v)}
                                    />
                                    {localConfig.subagent_timeout_enabled && (
                                        <div className="mt-2 pl-4 border-l-2 border-gray-100">
                                            <Input
                                                label={tAdvanced('timeoutMinutes')}
                                                value={localConfig.subagent_timeout_minutes || 120}
                                                onChange={(v: string) => handleChange('subagent_timeout_minutes', parseInt(v))}
                                                type="number"
                                            />
                                        </div>
                                    )}
                                </Section>

                                <Section title={tAdvanced('system')}>
                                    <Switch
                                        label={tAdvanced('webUiDashboard')}
                                        description={tAdvanced('webUiDashboardDesc')}
                                        checked={localConfig.web_ui_enabled ?? true}
                                        onChange={(v: boolean) => handleChange('web_ui_enabled', v)}
                                    />
                                    <div className="h-4" />
                                    <Switch
                                        label={tAdvanced('startTrayOnLogin')}
                                        description={tAdvanced('startTrayOnLoginDesc')}
                                        checked={localConfig.tray_autostart ?? false}
                                        onChange={(v: boolean) => handleChange('tray_autostart', v)}
                                    />
                                    <div className="h-4" />
                                    <Switch
                                        label={tAdvanced('serverPersistence')}
                                        description={tAdvanced('serverPersistenceDesc')}
                                        checked={localConfig.server_persistence_enabled ?? false}
                                        onChange={(v: boolean) => handleChange('server_persistence_enabled', v)}
                                    />
                                    <div className="h-4" />
                                    <Switch
                                        label={tAdvanced('memorySystem')}
                                        description={tAdvanced('memorySystemDesc')}
                                        checked={localConfig.memory_enabled ?? true}
                                        onChange={(v: boolean) => handleChange('memory_enabled', v)}
                                    />
                                    <div className="h-4" />
                                    <Switch
                                        label={tAdvanced('debugLogs')}
                                        description={tAdvanced('debugLogsDesc')}
                                        checked={localConfig.debug_logs_enabled ?? false}
                                        onChange={(v: boolean) => handleChange('debug_logs_enabled', v)}
                                    />
                                    <div className="h-4" />
                                    <button
                                        onClick={() => setShowToolsModal(true)}
                                        className="w-full flex items-center justify-between p-3 bg-gray-50 hover:bg-gray-100 rounded-lg border border-gray-100 transition-colors"
                                    >
                                        <div className="flex flex-col items-start">
                                            <span className="text-sm font-medium text-gray-700">{tAdvanced('tools')}</span>
                                            <span className="text-xs text-gray-500">{tAdvanced('toolsLoaded', { count: tools.length })}</span>
                                        </div>
                                        <ChevronRight size={16} className="text-gray-400" />
                                    </button>
                                    <div className="h-4" />
                                    <button
                                        onClick={() => setShowWorkflowsModal(true)}
                                        className="w-full flex items-center justify-between p-3 bg-gray-50 hover:bg-gray-100 rounded-lg border border-gray-100 transition-colors"
                                    >
                                        <div className="flex flex-col items-start">
                                            <span className="text-sm font-medium text-gray-700">{tAdvanced('workflows')}</span>
                                            <span className="text-xs text-gray-500">{tAdvanced('workflowsAvailable', { count: workflows.length })}</span>
                                        </div>
                                        <ChevronRight size={16} className="text-gray-400" />
                                    </button>
                                    <div className="h-4" />
                                    <button
                                        onClick={() => {
                                            onRequestTrustedSources?.();
                                            setShowTrustedSourcesModal(true);
                                        }}
                                        className="w-full flex items-center justify-between p-3 bg-gray-50 hover:bg-gray-100 rounded-lg border border-gray-100 transition-colors"
                                    >
                                        <div className="flex flex-col items-start">
                                            <span className="text-sm font-medium text-gray-700">{tAdvanced('trustedSources')}</span>
                                            <span className="text-xs text-gray-500">{tAdvanced('trustedSourcesCategories', { count: trustedSources.categories?.length ?? 0 })}</span>
                                        </div>
                                        <ChevronRight size={16} className="text-gray-400" />
                                    </button>
                                </Section>
                            </div>
                        )}

                        {activeTab === 'automations' && (
                            <div className="space-y-6">
                                {automations.length === 0 ? (
                                    <div className="flex flex-col items-center justify-center py-12 text-center space-y-4">
                                        <div className="p-4 bg-gray-50 rounded-full">
                                            <Zap size={32} className="text-gray-400" />
                                        </div>
                                        <div>
                                            <h3 className="text-lg font-medium text-gray-900">{tAutomations('noAutomationsTitle')}</h3>
                                            <p className="text-sm text-gray-500 max-w-xs mx-auto mt-1">
                                                {tAutomations('noAutomationsDesc')}
                                            </p>
                                        </div>
                                        <button
                                            onClick={() => setShowCreateAutomationModal(true)}
                                            className="px-4 py-2 bg-gray-900 hover:bg-gray-800 text-white font-medium rounded-lg text-sm transition-colors"
                                        >
                                            {tAutomations('createNew')}
                                        </button>
                                    </div>
                                ) : (
                                    <Section title={tAutomations('scheduled')}>
                                        <div className="space-y-3">
                                            {automations.map((auto, idx) => (
                                                <div key={idx} className="p-4 bg-white border border-gray-200 rounded-lg hover:border-gray-300 transition-colors">
                                                    <div className="flex items-start justify-between">
                                                        <div className="flex-1">
                                                            <div className="flex items-center gap-2">
                                                                <div className="font-medium text-gray-900">{auto.name}</div>
                                                                <div className={cn(
                                                                    "px-2 py-0.5 rounded text-xs font-medium",
                                                                    auto.enabled ? "bg-green-100 text-green-700" : "bg-gray-100 text-gray-500"
                                                                )}>
                                                                    {auto.enabled ? tCommon('active') : tCommon('disabled')}
                                                                </div>
                                                            </div>
                                                            <div className="text-sm text-gray-600 mt-1">{auto.description}</div>
                                                            <div className="flex items-center gap-4 mt-2 text-xs text-gray-500">
                                                                <div className="flex items-center gap-1">
                                                                    <span className="font-medium">{tAutomations('frequency')}:</span>
                                                                    <span>{auto.frequency}</span>
                                                                </div>
                                                                <div className="flex items-center gap-1">
                                                                    <span className="font-medium">{tAutomations('time')}:</span>
                                                                    <span>{auto.time}</span>
                                                                </div>
                                                            </div>
                                                        </div>
                                                    </div>
                                                </div>
                                            ))}
                                        </div>
                                        <div className="mt-4">
                                            <button
                                                onClick={() => setShowCreateAutomationModal(true)}
                                                className="w-full px-4 py-2 bg-gray-900 hover:bg-gray-800 text-white font-medium rounded-lg text-sm transition-colors"
                                            >
                                                {tAutomations('createNew')}
                                            </button>
                                        </div>
                                    </Section>
                                )}
                            </div>
                        )}

                        {activeTab === 'about' && (
                            <div className="space-y-6">
                                <div className="text-center py-6">
                                    <div className="w-16 h-16 bg-gray-900 rounded-2xl mx-auto flex items-center justify-center mb-4 shadow-xl">
                                        <span className="text-2xl font-bold text-white">V</span>
                                    </div>
                                    <h2 className="text-2xl font-bold text-gray-900">VAF</h2>
                                    <p className="text-gray-500">Veyllo Agent Framework</p>
                                    <p className="text-xs text-gray-400 mt-1">v2.4.0 (Mac/Metal Optimized)</p>
                                </div>

                                <Section title={tAbout('credits')}>
                                    <div className="space-y-3 text-sm text-gray-600">
                                        <div className="flex justify-between">
                                            <span>{tAbout('coreEngine')}</span>
                                            <span className="font-medium">Python 3.11 + Llama.cpp</span>
                                        </div>
                                        <div className="flex justify-between">
                                            <span>{tAbout('frontend')}</span>
                                            <span className="font-medium">Next.js + Tailwind</span>
                                        </div>
                                        <div className="flex justify-between">
                                            <span>{tAbout('developedBy')}</span>
                                            <span className="font-medium">Veyllo Labs</span>
                                        </div>
                                    </div>
                                </Section>
                            </div>
                        )}

                    </div>

                    {/* Footer */}
                    <div className="h-20 border-t border-gray-100 flex items-center justify-end px-8 gap-4 bg-gray-50/50 shrink-0">
                        <button
                            onClick={onClose}
                            className="px-6 py-2.5 rounded-xl font-medium text-gray-600 hover:bg-gray-200 transition-colors"
                        >
                            {tCommon('cancel')}
                        </button>
                        <button
                            onClick={handleSave}
                            disabled={!changed}
                            className="px-8 py-2.5 rounded-xl font-medium bg-gray-900 text-white hover:bg-black shadow-lg shadow-gray-200 disabled:opacity-50 disabled:cursor-not-allowed transition-all flex items-center gap-2"
                        >
                            <Save size={18} />
                            {tCommon('saveChanges')}
                        </button>
                    </div>
                </div>

                {/* Logout confirm / Have a nice day overlay */}
                {(showLogoutConfirm || isLoggingOut) && (
                    <div className="absolute inset-0 z-10 flex items-center justify-center bg-white/95 backdrop-blur-sm rounded-2xl">
                        {showLogoutConfirm && (
                            <div className="bg-white rounded-2xl border border-gray-200 shadow-xl p-6 w-full max-w-sm">
                                <p className="text-sm font-medium text-gray-800 text-center mb-6">
                                    {tCommon('logoutConfirm')}
                                </p>
                                <div className="flex gap-3">
                                    <button
                                        type="button"
                                        onClick={() => setShowLogoutConfirm(false)}
                                        className="flex-1 py-2.5 rounded-xl font-medium bg-gray-100 hover:bg-gray-200 text-gray-700 transition-colors"
                                    >
                                        {tCommon('no')}
                                    </button>
                                    <button
                                        type="button"
                                        onClick={handleLogoutYes}
                                        className="flex-1 py-2.5 rounded-xl font-medium bg-gray-900 hover:bg-gray-800 text-white transition-colors"
                                    >
                                        {tCommon('yes')}
                                    </button>
                                </div>
                            </div>
                        )}
                        {isLoggingOut && (
                            <div className="bg-white rounded-2xl border border-gray-200 shadow-xl p-8 w-full max-w-sm flex flex-col items-center gap-4">
                                <p className="text-lg font-medium text-gray-800">{tCommon('haveANiceDay')}</p>
                                <div className="w-full h-1.5 bg-gray-100 rounded-full overflow-hidden">
                                    <div
                                        className="h-full bg-gray-900 rounded-full transition-[width] duration-[1500ms] ease-out"
                                        style={{ width: `${logoutBarProgress}%` }}
                                    />
                                </div>
                            </div>
                        )}
                    </div>
                )}
            </div>

            {/* Tools Modal */}
            {showToolsModal && (
                <div className="fixed inset-0 z-[60] flex items-center justify-center p-4" onClick={() => setShowToolsModal(false)}>
                    <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" />
                    <div
                        className="relative bg-white w-full max-w-[90vw] h-[85vh] rounded-2xl shadow-2xl border border-gray-200 flex flex-col animate-in fade-in zoom-in-95 duration-200 overflow-hidden"
                        onClick={(e) => e.stopPropagation()}
                    >
                        {/* Header */}
                        <div className="h-20 border-b border-gray-100 flex items-center justify-between px-8 shrink-0 bg-white z-10">
                            <div>
                                <h2 className="text-2xl font-bold text-gray-800">{tModals('tools.title')}</h2>
                                <p className="text-sm text-gray-500">{tModals('tools.modulesInstalled', { count: tools.length })}</p>
                            </div>
                            <button onClick={() => setShowToolsModal(false)} className="p-2 text-gray-400 hover:text-gray-600 rounded-full hover:bg-gray-100 transition-colors">
                                <X size={24} />
                            </button>
                        </div>
                        
                        {/* Search Bar */}
                        <div className="p-6 border-b border-gray-100 bg-gray-50/50">
                            <div className="relative max-w-md">
                                <Search size={20} className="absolute left-4 top-1/2 -translate-y-1/2 text-gray-400" />
                                <input
                                    type="text"
                                    placeholder={tModals('tools.searchPlaceholder')}
                                    value={toolsSearch}
                                    onChange={(e) => setToolsSearch(e.target.value)}
                                    className="w-full pl-12 pr-4 h-12 bg-white border border-gray-200 rounded-xl text-base shadow-sm focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-gray-500 transition-all"
                                />
                            </div>
                        </div>

                        {/* Tools Grid */}
                        <div className="flex-1 overflow-y-auto p-6 bg-gray-50/30">
                            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5 gap-6">
                                {tools
                                    .filter(tool =>
                                        toolsSearch === '' ||
                                        tool.name.toLowerCase().includes(toolsSearch.toLowerCase()) ||
                                        tool.description.toLowerCase().includes(toolsSearch.toLowerCase())
                                    )
                                    .map((tool, idx) => (
                                        <div 
                                            key={idx} 
                                            onClick={() => handleViewCode(tool.name)}
                                            className="group relative aspect-square bg-white rounded-2xl border-2 border-gray-200 hover:border-blue-500 hover:shadow-xl hover:-translate-y-1 transition-all cursor-pointer overflow-hidden flex flex-col"
                                        >
                                            {/* Decoration: Floppy Disk Icon Background */}
                                            <div className="absolute -right-4 -top-4 opacity-[0.03] group-hover:opacity-[0.08] transition-opacity rotate-12">
                                                <Save size={160} />
                                            </div>
                                            
                                            {/* Content */}
                                            <div className="p-5 flex-1 flex flex-col relative z-10">
                                                <div className="flex items-start justify-between mb-2">
                                                    <div className="w-10 h-10 rounded-lg bg-blue-50 text-blue-600 flex items-center justify-center shadow-sm group-hover:bg-blue-600 group-hover:text-white transition-colors">
                                                        <Cpu size={20} />
                                                    </div>
                                                    {tool.category && (
                                                        <span className="px-2 py-1 bg-gray-100 text-gray-600 text-[10px] font-bold uppercase tracking-wider rounded-md">
                                                            {tool.category}
                                                        </span>
                                                    )}
                                                </div>
                                                
                                                <h3 className="text-lg font-bold text-gray-900 mb-1 group-hover:text-blue-600 transition-colors line-clamp-1">{tool.name}</h3>
                                                
                                                <div className="flex-1">
                                                    <p className="text-xs text-gray-500 line-clamp-4 leading-relaxed">
                                                        {tool.description}
                                                    </p>
                                                </div>

                                                <div className="mt-4 pt-3 border-t border-gray-100 flex items-center justify-between text-xs text-gray-400 group-hover:text-gray-600">
                                                    <span className="font-mono">v1.0.0</span>
                                                    <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity text-blue-600 font-medium">
                                                        {tModals('tools.viewCode')} <ChevronRight size={12} />
                                                    </div>
                                                </div>
                                            </div>
                                            
                                            {/* Bottom Bar (Floppy style) */}
                                            <div className="h-2 bg-gray-100 border-t border-gray-200 group-hover:bg-blue-50 group-hover:border-blue-100 transition-colors" />
                                        </div>
                                    ))}
                            </div>

                            {/* Empty State */}
                            {tools.length > 0 && tools.filter(tool =>
                                toolsSearch === '' ||
                                tool.name.toLowerCase().includes(toolsSearch.toLowerCase()) ||
                                tool.description.toLowerCase().includes(toolsSearch.toLowerCase())
                            ).length === 0 && (
                                <div className="flex flex-col items-center justify-center py-20 text-center">
                                    <div className="w-16 h-16 bg-gray-100 rounded-full flex items-center justify-center mb-4 text-gray-400">
                                        <Search size={32} />
                                    </div>
                                    <h3 className="text-lg font-medium text-gray-900">{tModals('tools.noToolsFound')}</h3>
                                    <p className="text-sm text-gray-500 mt-1">{tModals('tools.noToolsFoundHint')}</p>
                                </div>
                            )}
                        </div>
                    </div>
                </div>
            )}

            {/* Code Viewer Modal */}
            {codeModal && (
                <div className="fixed inset-0 z-[70] flex items-center justify-center p-4" onClick={() => setCodeModal(null)}>
                    <div className="absolute inset-0 bg-black/50 backdrop-blur-md" />
                    <div
                        className="relative bg-[#1e1e1e] w-full max-w-[90vw] h-[90vh] rounded-2xl shadow-2xl flex flex-col animate-in fade-in zoom-in-95 duration-200 overflow-hidden"
                        onClick={(e) => e.stopPropagation()}
                    >
                        {/* Header */}
                        <div className="h-14 border-b border-gray-700 flex items-center justify-between px-6 shrink-0 bg-[#252526]">
                            <div className="flex items-center gap-3">
                                <Cpu size={18} className="text-blue-400" />
                                <span className="font-mono text-sm font-medium text-gray-200">{codeModal.name}.py</span>
                            </div>
                            <button onClick={() => setCodeModal(null)} className="p-1.5 text-gray-400 hover:text-white rounded-md hover:bg-gray-700 transition-colors">
                                <X size={18} />
                            </button>
                        </div>
                        
                        {/* Code Content */}
                        <div className="flex-1 overflow-auto p-4 font-mono text-sm text-[#d4d4d4] leading-relaxed selection:bg-blue-500/30">
                            <pre>{codeModal.code}</pre>
                        </div>
                    </div>
                </div>
            )}

            {/* Workflows Modal */}
            {showWorkflowsModal && (
                <div className="fixed inset-0 z-[60] flex items-center justify-center p-4" onClick={() => setShowWorkflowsModal(false)}>
                    <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" />
                    <div
                        className="relative bg-white w-full max-w-[90vw] h-[85vh] rounded-2xl shadow-2xl border border-gray-200 flex flex-col animate-in fade-in zoom-in-95 duration-200 overflow-hidden"
                        onClick={(e) => e.stopPropagation()}
                    >
                        {/* Header */}
                        <div className="h-20 border-b border-gray-100 flex items-center justify-between px-8 shrink-0 bg-white z-10">
                            <div>
                                <h2 className="text-2xl font-bold text-gray-800">{tModals('workflows.title')}</h2>
                                <p className="text-sm text-gray-500">{tModals('workflows.templatesAvailable', { count: workflows.length })}</p>
                            </div>
                            <button onClick={() => setShowWorkflowsModal(false)} className="p-2 text-gray-400 hover:text-gray-600 rounded-full hover:bg-gray-100 transition-colors">
                                <X size={24} />
                            </button>
                        </div>
                        
                        {/* Search Bar */}
                        <div className="p-6 border-b border-gray-100 bg-gray-50/50">
                            <div className="relative max-w-md">
                                <Search size={20} className="absolute left-4 top-1/2 -translate-y-1/2 text-gray-400" />
                                <input
                                    type="text"
                                    placeholder={tModals('workflows.searchPlaceholder')}
                                    value={workflowsSearch}
                                    onChange={(e) => setWorkflowsSearch(e.target.value)}
                                    className="w-full pl-12 pr-4 h-12 bg-white border border-gray-200 rounded-xl text-base shadow-sm focus:outline-none focus:ring-2 focus:ring-purple-500/20 focus:border-purple-500 transition-all"
                                />
                            </div>
                        </div>

                        {/* Workflows Grid */}
                        <div className="flex-1 overflow-y-auto p-6 bg-gray-50/30">
                            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5 gap-6">
                                {workflows
                                    .filter(wf =>
                                        workflowsSearch === '' ||
                                        wf.name.toLowerCase().includes(workflowsSearch.toLowerCase()) ||
                                        wf.description.toLowerCase().includes(workflowsSearch.toLowerCase())
                                    )
                                    .map((wf, idx) => (
                                        <div 
                                            key={idx} 
                                            onClick={() => handleViewWorkflow(wf.id)}
                                            className="group relative aspect-square bg-white rounded-2xl border-2 border-gray-200 hover:border-purple-500 hover:shadow-xl hover:-translate-y-1 transition-all cursor-pointer overflow-hidden flex flex-col"
                                        >
                                            {/* Decoration: Workflow Icon Background */}
                                            <div className="absolute -right-4 -top-4 opacity-[0.03] group-hover:opacity-[0.08] transition-opacity rotate-12">
                                                <Workflow size={160} />
                                            </div>
                                            
                                            {/* Content */}
                                            <div className="p-5 flex-1 flex flex-col relative z-10">
                                                <div className="flex items-start justify-between mb-2">
                                                    <div className="w-10 h-10 rounded-lg bg-purple-50 text-purple-600 flex items-center justify-center shadow-sm group-hover:bg-purple-600 group-hover:text-white transition-colors">
                                                        <GitBranch size={20} />
                                                    </div>
                                                    <span className="px-2 py-1 bg-gray-100 text-gray-600 text-[10px] font-bold uppercase tracking-wider rounded-md">
                                                        {tModals('workflows.steps', { count: wf.steps })}
                                                    </span>
                                                </div>
                                                
                                                <h3 className="text-lg font-bold text-gray-900 mb-1 group-hover:text-purple-600 transition-colors line-clamp-1">{wf.name}</h3>
                                                
                                                <div className="flex-1">
                                                    <p className="text-xs text-gray-500 line-clamp-4 leading-relaxed">
                                                        {wf.description}
                                                    </p>
                                                </div>

                                                <div className="mt-4 pt-3 border-t border-gray-100 flex items-center justify-between text-xs text-gray-400 group-hover:text-gray-600">
                                                    <span className="font-mono">{tModals('workflows.template')}</span>
                                                    <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity text-purple-600 font-medium">
                                                        {tModals('workflows.details')} <ChevronRight size={12} />
                                                    </div>
                                                </div>
                                            </div>
                                            
                                            {/* Bottom Bar (Purple style) */}
                                            <div className="h-2 bg-gray-100 border-t border-gray-200 group-hover:bg-purple-50 group-hover:border-purple-100 transition-colors" />
                                        </div>
                                    ))}
                            </div>

                            {/* Empty State */}
                            {workflows.length > 0 && workflows.filter(wf =>
                                workflowsSearch === '' ||
                                wf.name.toLowerCase().includes(workflowsSearch.toLowerCase()) ||
                                wf.description.toLowerCase().includes(workflowsSearch.toLowerCase())
                            ).length === 0 && (
                                <div className="flex flex-col items-center justify-center py-20 text-center">
                                    <div className="w-16 h-16 bg-gray-100 rounded-full flex items-center justify-center mb-4 text-gray-400">
                                        <Search size={32} />
                                    </div>
                                    <h3 className="text-lg font-medium text-gray-900">{tModals('workflows.noWorkflowsFound')}</h3>
                                    <p className="text-sm text-gray-500 mt-1">{tModals('workflows.noWorkflowsFoundHint')}</p>
                                </div>
                            )}
                        </div>
                    </div>
                </div>
            )}

            {/* Trusted Sources Modal - same size as Tools/Workflows */}
            {showTrustedSourcesModal && (
                <div className="fixed inset-0 z-[60] flex items-center justify-center p-4" onClick={() => setShowTrustedSourcesModal(false)}>
                    <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" />
                    <div
                        className="relative bg-white w-full max-w-[90vw] h-[85vh] rounded-2xl shadow-2xl border border-gray-200 flex flex-col animate-in fade-in zoom-in-95 duration-200 overflow-hidden"
                        onClick={(e) => e.stopPropagation()}
                    >
                        <div className="h-20 border-b border-gray-100 flex items-center justify-between px-8 shrink-0 bg-white z-10">
                            <div>
                                <h2 className="text-2xl font-bold text-gray-800">{tModals('trustedSources.title')}</h2>
                                <p className="text-sm text-gray-500">{tModals('trustedSources.subtitle')}</p>
                            </div>
                            <button onClick={() => setShowTrustedSourcesModal(false)} className="p-2 text-gray-400 hover:text-gray-600 rounded-full hover:bg-gray-100 transition-colors">
                                <X size={24} />
                            </button>
                        </div>
                        <div className="flex-1 overflow-y-auto px-4 pt-2 pb-4 bg-gray-50/30 space-y-3 min-h-0">
                            {trustedSourcesError && (
                                <div className="rounded-xl bg-red-50 border border-red-200 px-4 py-3 text-sm text-red-700">
                                    {trustedSourcesError}
                                </div>
                            )}
                            {(trustedSources.categories ?? []).length === 0 && !onCreateTrustedCategory ? (
                                <div className="flex flex-col items-center justify-center py-12 text-center">
                                    <div className="w-14 h-14 rounded-xl bg-gray-100 flex items-center justify-center mb-3 text-gray-400">
                                        <Link2 size={28} />
                                    </div>
                                    <p className="text-sm text-gray-600">{tModals('trustedSources.noCategories')}</p>
                                </div>
                            ) : (
                            <div className="grid grid-cols-[repeat(auto-fill,minmax(280px,320px))] gap-x-3 gap-y-2 justify-center justify-items-stretch items-start content-start">
                                {onCreateTrustedCategory && (
                                    <div className="w-full min-w-0 bg-white rounded-xl border border-gray-200 shadow-sm p-4 flex flex-col min-h-[180px]">
                                        {!showCreateCategoryForm ? (
                                            <button
                                                type="button"
                                                onClick={() => setShowCreateCategoryForm(true)}
                                                className="flex-1 flex flex-col items-center justify-center gap-2 py-6 rounded-lg border-2 border-dashed border-gray-200 text-gray-400 hover:border-gray-300 hover:text-gray-600 hover:bg-gray-50/50 transition-colors w-full"
                                            >
                                                <Plus size={40} className="shrink-0" />
                                                <span className="text-sm font-medium">{tModals('trustedSources.newCategory')}</span>
                                            </button>
                                        ) : (
                                            <div className="space-y-3">
                                                <p className="text-sm font-medium text-gray-700">{tModals('trustedSources.categoryNameUnique')}</p>
                                                <input
                                                    type="text"
                                                    placeholder={tModals('trustedSources.namePlaceholder')}
                                                    value={newCategoryName}
                                                    onChange={(e) => setNewCategoryName(e.target.value)}
                                                    className="w-full px-4 py-3 rounded-xl bg-white border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400 text-sm"
                                                    autoFocus
                                                />
                                                <div className="flex gap-2">
                                                    <button
                                                        onClick={() => {
                                                            const name = newCategoryName.trim();
                                                            if (name) {
                                                                onCreateTrustedCategory(name);
                                                                setNewCategoryName('');
                                                                setShowCreateCategoryForm(false);
                                                            }
                                                        }}
                                                        className="px-4 py-2 rounded-lg font-medium bg-gray-900 text-white hover:bg-gray-800 transition-colors"
                                                    >
                                                        {tCommon('create')}
                                                    </button>
                                                    <button
                                                        onClick={() => { setShowCreateCategoryForm(false); setNewCategoryName(''); }}
                                                        className="px-4 py-2 rounded-lg font-medium bg-gray-100 text-gray-700 hover:bg-gray-200 transition-colors"
                                                    >
                                                        {tCommon('cancel')}
                                                    </button>
                                                </div>
                                            </div>
                                        )}
                                    </div>
                                )}
                                {([...(trustedSources.categories ?? [])]
                                    .sort((a, b) => {
                                        const aCustom = a.is_custom === true || a.description === 'Custom category';
                                        const bCustom = b.is_custom === true || b.description === 'Custom category';
                                        if (aCustom && !bCustom) return -1;
                                        if (!aCustom && bCustom) return 1;
                                        return 0;
                                    })
                                    .map((cat) => (
                                        <div key={cat.id} className="w-full min-w-0 bg-white rounded-xl border border-gray-200 shadow-sm p-4 flex flex-col">
                                            <div className="flex items-start justify-between gap-2">
                                                <div>
                                                    <h3 className="text-lg font-semibold text-gray-900">{cat.name}</h3>
                                                    {cat.description && <p className="text-xs text-gray-500 mt-0.5">{cat.description}</p>}
                                                </div>
                                                <div className="flex items-center gap-1 shrink-0">
                                                    {onAddTrustedSource && (
                                                        <button
                                                            onClick={() => { setAddFormCategoryId(cat.id); setTrustedSourceForm({ categoryId: cat.id, name: '', url: '' }); }}
                                                            className="p-2 text-gray-400 hover:text-gray-700 hover:bg-gray-100 rounded-lg transition-colors"
                                                            title={tModals('trustedSources.addLink', { name: cat.name })}
                                                        >
                                                            <Plus size={18} />
                                                        </button>
                                                    )}
                                                    {(Boolean(cat.is_custom || cat.description === 'Custom category') && onDeleteTrustedCategory) ? (
                                                        <button
                                                            type="button"
                                                            onClick={(e) => {
                                                                e.preventDefault();
                                                                e.stopPropagation();
                                                                if (!confirm(tModals('trustedSources.deleteCategoryConfirm'))) return;
                                                                onDeleteTrustedCategory(cat.id);
                                                            }}
                                                            className="p-2 cursor-pointer relative z-10 text-gray-400 hover:text-red-600 hover:bg-red-50 rounded-lg transition-colors"
                                                            title={tCommon('delete')}
                                                        >
                                                            <Trash2 size={18} />
                                                        </button>
                                                    ) : null}
                                                </div>
                                            </div>
                                            <ul className="mt-3 space-y-2 flex-1">
                                                {(cat.sources ?? []).map((src, idx) => (
                                                    <li key={idx} className="flex items-center justify-between gap-2 py-2 px-3 rounded-lg bg-gray-50 border border-gray-100">
                                                        <div className="min-w-0 flex-1">
                                                            <a href={src.url} target="_blank" rel="noopener noreferrer" className="text-sm font-medium text-gray-900 hover:text-blue-600 truncate block">{src.name}</a>
                                                            <span className="text-xs text-gray-500 truncate block">{src.url}</span>
                                                        </div>
                                                        <div className="flex items-center gap-2 shrink-0">
                                                            <span className="text-xs px-2 py-0.5 rounded-full bg-gray-200 text-gray-600">{tModals('trustedSources.trustScore', { score: src.trust_score })}</span>
                                                            {onRemoveTrustedSource && (
                                                                <button
                                                                    onClick={() => {
                                                                        if (!confirm(tModals('trustedSources.removeSourceConfirm'))) return;
                                                                        onRemoveTrustedSource((src.domains && src.domains[0]) || '', src.is_custom);
                                                                    }}
                                                                    className="p-2 text-gray-400 hover:text-red-600 hover:bg-red-50 rounded-lg transition-colors"
                                                                    title={tCommon('delete')}
                                                                >
                                                                    <Trash2 size={16} />
                                                                </button>
                                                            )}
                                                        </div>
                                                    </li>
                                                ))}
                                            </ul>
                                            {/* Inline add form for this category */}
                                            {onAddTrustedSource && addFormCategoryId === cat.id && (
                                                <div className="mt-4 pt-4 border-t border-gray-100 space-y-3">
                                                    <p className="text-sm font-medium text-gray-700">{tModals('trustedSources.addLink', { name: cat.name })}</p>
                                                    <input
                                                        type="text"
                                                        placeholder={tModals('trustedSources.namePlaceholder')}
                                                        value={trustedSourceForm.categoryId === cat.id ? trustedSourceForm.name : ''}
                                                        onChange={(e) => setTrustedSourceForm((f) => ({ ...f, name: e.target.value }))}
                                                        className="w-full px-4 py-3 rounded-xl bg-white border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400 text-sm"
                                                    />
                                                    <input
                                                        type="url"
                                                        placeholder="https://..."
                                                        value={trustedSourceForm.categoryId === cat.id ? trustedSourceForm.url : ''}
                                                        onChange={(e) => setTrustedSourceForm((f) => ({ ...f, url: e.target.value }))}
                                                        className="w-full px-4 py-3 rounded-xl bg-white border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400 text-sm"
                                                    />
                                                    <div className="flex gap-2">
                                                        <button
                                                            onClick={() => {
                                                                if (trustedSourceForm.name.trim() && trustedSourceForm.url.trim()) {
                                                                    onAddTrustedSource(cat.id, trustedSourceForm.name.trim(), trustedSourceForm.url.trim());
                                                                    setTrustedSourceForm({ categoryId: '', name: '', url: '' });
                                                                    setAddFormCategoryId(null);
                                                                }
                                                            }}
                                                            className="px-4 py-2 rounded-lg font-medium bg-gray-900 text-white hover:bg-gray-800 transition-colors"
                                                        >
                                                            {tCommon('save')}
                                                        </button>
                                                        <button
                                                            onClick={() => { setAddFormCategoryId(null); setTrustedSourceForm({ categoryId: '', name: '', url: '' }); }}
                                                            className="px-4 py-2 rounded-lg font-medium bg-gray-100 text-gray-700 hover:bg-gray-200 transition-colors"
                                                        >
                                                            {tCommon('cancel')}
                                                        </button>
                                                    </div>
                                                </div>
                                            )}
                                        </div>
                                    )))}
                                </div>
                            )}
                        </div>
                    </div>
                </div>
            )}

            {/* Workflow Visualizer Modal */}
            {workflowModal && (
                <div className="fixed inset-0 z-[70] flex items-center justify-center p-4" onClick={() => setWorkflowModal(null)}>
                    <div className="absolute inset-0 bg-black/50 backdrop-blur-md" />
                    <div
                        className="relative bg-white w-full max-w-[90vw] h-[90vh] rounded-2xl shadow-2xl flex flex-col animate-in fade-in zoom-in-95 duration-200 overflow-hidden"
                        onClick={(e) => e.stopPropagation()}
                    >
                        {/* Header */}
                        <div className="h-16 border-b border-gray-100 flex items-center justify-between px-6 shrink-0 bg-white z-10">
                            <div className="flex items-center gap-3">
                                <div className="w-8 h-8 rounded-lg bg-purple-50 text-purple-600 flex items-center justify-center">
                                    <GitBranch size={18} />
                                </div>
                                <div>
                                    <h2 className="text-lg font-bold text-gray-800">{workflowModal.name}</h2>
                                    <p className="text-xs text-gray-500">{tModals('workflows.flowVisualization')}</p>
                                </div>
                            </div>
                            <button onClick={() => setWorkflowModal(null)} className="p-2 text-gray-400 hover:text-gray-600 rounded-full hover:bg-gray-100 transition-colors">
                                <X size={20} />
                            </button>
                        </div>
                        
                        {/* Content Split View */}
                        <div className="flex-1 flex overflow-hidden">
                            {/* Left: ReactFlow Canvas - Lazy loaded */}
                            <div className="flex-1 bg-gray-50 relative border-r border-gray-200">
                                <Suspense fallback={<ReactFlowFallback />}>
                                    <ReactFlow
                                        nodes={nodes}
                                        edges={edges}
                                        onNodesChange={onNodesChange}
                                        onEdgesChange={onEdgesChange}
                                        onNodeClick={onNodeClick}
                                        nodesDraggable={false}
                                        fitView
                                        fitViewOptions={{ padding: 0.2 }}
                                        proOptions={{ hideAttribution: true }}
                                    >
                                        <Background color="#ccc" gap={20} />
                                        <Controls />
                                    </ReactFlow>
                                </Suspense>
                            </div>

                            {/* Right: Code Viewer (Fixed 30%) */}
                            <div className="w-[30%] shrink-0 bg-[#1e1e1e] flex flex-col border-l border-gray-800">
                                <div className="h-10 border-b border-gray-700 flex items-center px-4 bg-[#252526] shrink-0">
                                    <span className="text-xs font-mono font-medium text-gray-400 uppercase tracking-wide">{tModals('workflows.stepDefinition')}</span>
                                </div>
                                <div className="flex-1 overflow-auto p-4 font-mono text-xs text-[#d4d4d4] leading-relaxed selection:bg-purple-500/30">
                                    <pre>{workflowModal.selectedCode}</pre>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            )}

            {/* Memory System Modal */}
            {showMemoryModal && (
                <div className="fixed inset-0 z-[60] flex items-center justify-center p-4" onClick={() => setShowMemoryModal(false)}>
                    <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" />
                    <div
                        className="relative bg-white w-full max-w-[95vw] h-[90vh] rounded-2xl shadow-2xl border border-gray-200 flex flex-col animate-in fade-in zoom-in-95 duration-200 overflow-hidden"
                        onClick={(e) => e.stopPropagation()}
                    >
                        {/* Header (DESIGN: modal header, gray palette) */}
                        <div className="flex items-center justify-between p-6 border-b border-gray-200 shrink-0 bg-gray-50 z-10">
                            <div className="flex items-center gap-3">
                                <div className="w-10 h-10 rounded-xl bg-gray-900 flex items-center justify-center">
                                    <Brain size={20} className="text-white" />
                                </div>
                                <div>
                                    <h2 className="text-xl font-bold text-gray-900">{tModals('memory.title')}</h2>
                                    <p className="text-sm text-gray-500">
                                        {memoryStats?.memories ?? 0} memories • {memoryStats?.chunks ?? 0} chunks • {memoryEdges.length} connections
                                    </p>
                                </div>
                            </div>
                            <div className="flex items-center gap-3">
                                <button 
                                    onClick={fetchMemoryGraph}
                                    disabled={memoryLoading}
                                    className="flex items-center gap-2 px-4 py-2 bg-gray-100 hover:bg-gray-200 text-gray-700 font-medium rounded-lg transition-colors text-sm disabled:opacity-50"
                                >
                                    <RefreshCw size={16} className={memoryLoading ? 'animate-spin' : ''} />
                                    {tCommon('refresh')}
                                </button>
                                <a
                                    href="/memory"
                                    target="_blank"
                                    className="flex items-center gap-2 px-4 py-2 bg-gray-900 hover:bg-gray-800 text-white font-medium rounded-lg transition-colors text-sm"
                                >
                                    {tModals('memory.openFullView')}
                                    <ChevronRight size={16} />
                                </a>
                                <button onClick={() => setShowMemoryModal(false)} className="p-2 hover:bg-gray-200 rounded-lg transition-colors text-gray-500 hover:text-gray-700" title={tCommon('close')}>
                                    <X size={20} />
                                </button>
                            </div>
                        </div>
                        
                        {/* Graph Content */}
                        <div className="flex-1 overflow-hidden bg-gray-50">
                            {memoryLoading ? (
                                <div className="flex items-center justify-center h-full">
                                    <div className="text-center">
                                        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-gray-400 mx-auto mb-4" />
                                        <p className="text-sm text-gray-500">{tModals('memory.loadingGraph')}</p>
                                    </div>
                                </div>
                            ) : memoryNodes.length === 0 ? (
                                <div className="flex items-center justify-center h-full">
                                    <div className="text-center">
                                        <div className="w-20 h-20 rounded-xl bg-gray-200 flex items-center justify-center mx-auto mb-4">
                                            <Brain size={40} className="text-gray-500" />
                                        </div>
                                        {(memoryStats?.memories ?? 0) > 0 ? (
                                            <>
                                                <h3 className="text-lg font-semibold text-gray-900 mb-1">{tModals('memory.graphError')}</h3>
                                                {memoryGraphError && (
                                                    <p className="text-sm text-red-600 max-w-sm mb-2 font-mono">
                                                        {memoryGraphError}
                                                    </p>
                                                )}
                                                <p className="text-sm text-gray-500 max-w-sm mb-4">
                                                    {tModals('memory.graphErrorHint')}
                                                </p>
                                                <button
                                                    type="button"
                                                    onClick={fetchMemoryGraph}
                                                    disabled={memoryLoading}
                                                    className="inline-flex items-center gap-2 px-4 py-2 bg-gray-900 hover:bg-gray-800 disabled:opacity-50 text-white font-medium rounded-lg transition-colors text-sm"
                                                >
                                                    <RefreshCw size={16} className={memoryLoading ? 'animate-spin' : ''} />
                                                    {tCommon('refresh')}
                                                </button>
                                            </>
                                        ) : (
                                            <>
                                                <h3 className="text-lg font-semibold text-gray-900 mb-1">{tModals('memory.noMemoriesTitle')}</h3>
                                                <p className="text-sm text-gray-500 max-w-sm">
                                                    {tModals('memory.noMemoriesDesc')}
                                                </p>
                                                <a 
                                                    href="/memory"
                                                    target="_blank"
                                                    className="inline-flex items-center gap-2 mt-4 px-4 py-2 bg-gray-900 hover:bg-gray-800 text-white font-medium rounded-lg transition-colors text-sm"
                                                >
                                                    {tModals('memory.createMemory')}
                                                    <ChevronRight size={16} />
                                                </a>
                                            </>
                                        )}
                                    </div>
                                </div>
                            ) : (
                                <Suspense fallback={<ReactFlowFallback />}>
                                    <ReactFlow
                                        nodes={memoryNodes.map((node, i) => {
                                            const isTagNode = node.type === 'tagNode' || node.data?.isTagNode;
                                            const isFaded = selectedMemoryNodeId !== null && !connectedMemoryNodeIds.has(node.id);
                                            const isSelected = node.id === selectedMemoryNodeId;
                                            return {
                                            id: node.id,
                                            type: 'default',
                                            position: node.position || { x: (i % 5) * 280, y: Math.floor(i / 5) * 180 },
                                            data: {
                                                label: (
                                                    <div className="text-left p-1">
                                                        <div className={cn(
                                                            "font-medium text-sm truncate max-w-[180px]",
                                                            isTagNode ? "text-purple-700" : "text-gray-800"
                                                        )}>
                                                            {node.data?.label || 'Untitled'}
                                                        </div>
                                                        {!isTagNode && node.data?.preview && (
                                                            <div className="text-xs text-gray-500 truncate max-w-[180px] mt-0.5">
                                                                {node.data.preview}
                                                            </div>
                                                        )}
                                                        <div className="flex items-center gap-2 mt-1 text-[10px] text-gray-400">
                                                            {isTagNode ? (
                                                                <span>{node.data?.memoryCount || 0} memories</span>
                                                            ) : (
                                                                <>
                                                                    <span>{node.data?.chunkCount || 0} chunks</span>
                                                                    {node.data?.tags?.length > 0 && (
                                                                        <span className="px-1.5 py-0.5 bg-gray-100 rounded">
                                                                            {node.data.tags[0]}
                                                                        </span>
                                                                    )}
                                                                </>
                                                            )}
                                                        </div>
                                                    </div>
                                                )
                                            },
                                            selected: isSelected,
                                            style: {
                                                background: isTagNode
                                                    ? '#f3e8ff'
                                                    : (node.data?.isHighlighted ? '#fef3c7' : 'white'),
                                                border: isSelected
                                                    ? '2px solid #374151'
                                                    : (isTagNode
                                                        ? '2px solid #a855f7'
                                                        : (node.data?.isHighlighted ? '2px solid #f59e0b' : '1px solid #e5e7eb')),
                                                borderRadius: '12px',
                                                padding: '8px',
                                                minWidth: isTagNode ? '140px' : '200px',
                                                opacity: isFaded ? 0.4 : 1,
                                                transition: 'opacity 0.3s ease',
                                            }
                                        }})}
                                        edges={memoryEdges
                                            .filter(edge => edge.data?.connectionType === 'tag')
                                            .map(edge => {
                                            const isFaded = selectedMemoryNodeId !== null &&
                                                !connectedMemoryNodeIds.has(edge.source) &&
                                                !connectedMemoryNodeIds.has(edge.target);
                                            return {
                                            id: edge.id,
                                            source: edge.source,
                                            target: edge.target,
                                            type: 'smoothstep',
                                            animated: false,
                                            style: {
                                                stroke: '#a855f7',
                                                strokeWidth: Math.max(1, (edge.data?.strength || 0.5) * 3),
                                                opacity: isFaded ? 0.15 : 0.5,
                                                strokeDasharray: '5,5',
                                                transition: 'opacity 0.3s ease',
                                            },
                                            markerEnd: undefined,
                                        }})}
                                        onNodeClick={(_, node) => setSelectedMemoryNodeId(node.id)}
                                        onPaneClick={() => setSelectedMemoryNodeId(null)}
                                        fitView
                                        fitViewOptions={{ padding: 0.3 }}
                                        proOptions={{ hideAttribution: true }}
                                        defaultEdgeOptions={{ type: 'smoothstep' }}
                                    >
                                        <Background color="#e5e7eb" gap={24} />
                                        <Controls className="bg-white rounded-lg shadow-lg" />
                                        <MiniMap
                                            nodeColor={(node) => node.style?.background === '#fef3c7' ? '#eab308' : '#374151'}
                                            maskColor="rgba(0, 0, 0, 0.1)"
                                            className="bg-white rounded-lg shadow-lg"
                                        />
                                    </ReactFlow>
                                </Suspense>
                            )}
                        </div>

                        {/* Footer Stats (DESIGN: meta text, status badges) */}
                        <div className="border-t border-gray-200 flex items-center justify-between px-6 py-3 bg-gray-50">
                            <div className="flex items-center gap-6 text-xs text-gray-500">
                                <div className="flex items-center gap-2">
                                    <div className="w-3 h-3 rounded bg-white border-2 border-gray-300" />
                                    <span>{tModals('memory.legendMemory')}</span>
                                </div>
                                <div className="flex items-center gap-2">
                                    <div className="w-3 h-3 rounded bg-purple-100 border-2 border-purple-400" />
                                    <span>{tModals('memory.legendTag')}</span>
                                </div>
                                <div className="flex items-center gap-2">
                                    <div className="w-6 h-0.5 bg-purple-400" style={{ borderTop: '2px dashed #a855f7' }} />
                                    <span>{tModals('memory.legendTagConnection')}</span>
                                </div>
                            </div>
                            <div className="flex items-center gap-2">
                                {memoryStats?.db_connected ? (
                                    <span className="text-xs px-2 py-0.5 rounded-full bg-green-100 text-green-700">
                                        {tModals('memory.connected')}
                                    </span>
                                ) : (
                                    <span className="text-xs px-2 py-0.5 rounded-full bg-red-100 text-red-600">
                                        {tModals('memory.disconnected')}
                                    </span>
                                )}
                            </div>
                        </div>
                    </div>
                </div>
            )}

            <AutomationCalendarModal
                isOpen={showCreateAutomationModal}
                onClose={() => setShowCreateAutomationModal(false)}
                currentUser={currentUser}
            />

            {/* User Identity Modal */}
            {showUserIdentityModal && (
                <div className="fixed inset-0 z-[60] flex items-center justify-center p-4" onClick={() => { setShowUserIdentityModal(false); setIsEditingUserIdentity(false); setUserIdentityDraft(null); }}>
                    <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" />
                    <div
                        className="relative bg-white w-full max-w-[95vw] h-[90vh] rounded-2xl shadow-2xl border border-gray-200 flex flex-col animate-in fade-in zoom-in-95 duration-200 overflow-hidden"
                        onClick={(e) => e.stopPropagation()}
                    >
                        <div className="flex items-center justify-between p-4 border-b border-gray-200 shrink-0 bg-amber-50">
                            <div className="flex items-center gap-3">
                                <div className="w-10 h-10 rounded-xl bg-amber-200 flex items-center justify-center">
                                    <User size={20} className="text-amber-800" />
                                </div>
                                <div>
                                    <h2 className="text-xl font-bold text-gray-900">{tModals('userIdentity.title')}</h2>
                                    <p className="text-sm text-gray-500">{tModals('userIdentity.subtitle')}</p>
                                </div>
                            </div>
                            <button onClick={() => setShowUserIdentityModal(false)} className="p-2 hover:bg-amber-100 rounded-lg transition-colors text-gray-500 hover:text-gray-700" title={tCommon('close')}>
                                <X size={20} />
                            </button>
                        </div>
                        <div className="flex-1 overflow-hidden flex min-h-0">
                            {/* Left: User identity (human) – user_identity.json */}
                            <div className="flex-1 min-w-0 border-r border-gray-200 overflow-y-auto p-5 bg-gray-50">
                                <div className="flex items-center justify-between mb-3">
                                    <h3 className="text-sm font-semibold text-gray-700">{tModals('userIdentity.identityHeading')}</h3>
                                    {personaData?.user_identity && !isEditingUserIdentity && (
                                        <button
                                            onClick={startEditingUserIdentity}
                                            className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-amber-700 bg-amber-100 hover:bg-amber-200 rounded-lg transition-colors"
                                            title={tCommon('edit')}
                                        >
                                            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z" />
                                            </svg>
                                            {tCommon('edit')}
                                        </button>
                                    )}
                                    {isEditingUserIdentity && (
                                        <div className="flex gap-2">
                                            <button
                                                onClick={saveUserIdentity}
                                                className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-green-500 hover:bg-green-600 rounded-lg transition-colors"
                                            >
                                                <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                                                </svg>
                                                {tCommon('save')}
                                            </button>
                                            <button
                                                onClick={cancelEditingUserIdentity}
                                                className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-gray-700 bg-gray-200 hover:bg-gray-300 rounded-lg transition-colors"
                                            >
                                                {tCommon('cancel')}
                                            </button>
                                        </div>
                                    )}
                                </div>
                                <p className="text-xs text-gray-500 mb-4">
                                    {isEditingUserIdentity
                                        ? tModals('userIdentity.editHint')
                                        : tModals('userIdentity.viewHint')}
                                </p>

                                {personaData?.user_identity ? (
                                    isEditingUserIdentity && userIdentityDraft ? (
                                        /* Edit Mode - Text inputs */
                                        <div className="space-y-4 text-sm">
                                            <div>
                                                <label className="text-gray-500 font-medium text-xs uppercase tracking-wide">{tModals('userIdentity.name')}</label>
                                                <input
                                                    type="text"
                                                    value={userIdentityDraft.name}
                                                    onChange={(e) => setUserIdentityDraft({ ...userIdentityDraft, name: e.target.value })}
                                                    className="mt-1 w-full px-3 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-amber-500 focus:border-transparent text-sm"
                                                />
                                            </div>
                                            <div>
                                                <label className="text-gray-500 font-medium text-xs uppercase tracking-wide">{tModals('userIdentity.language')}</label>
                                                <input
                                                    type="text"
                                                    value={userIdentityDraft.preferred_language}
                                                    onChange={(e) => setUserIdentityDraft({ ...userIdentityDraft, preferred_language: e.target.value })}
                                                    className="mt-1 w-full px-3 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-amber-500 focus:border-transparent text-sm"
                                                    placeholder="z.B. de, en, tr"
                                                />
                                            </div>
                                            <div className="grid grid-cols-2 gap-3">
                                                <div>
                                                    <label className="text-gray-500 font-medium text-xs uppercase tracking-wide">{tModals('userIdentity.city')}</label>
                                                    <input
                                                        type="text"
                                                        value={userIdentityDraft.city}
                                                        onChange={(e) => setUserIdentityDraft({ ...userIdentityDraft, city: e.target.value })}
                                                        className="mt-1 w-full px-3 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-amber-500 focus:border-transparent text-sm"
                                                        placeholder="z.B. Berlin, München"
                                                    />
                                                </div>
                                                <div>
                                                    <label className="text-gray-500 font-medium text-xs uppercase tracking-wide">{tModals('userIdentity.country')}</label>
                                                    <input
                                                        type="text"
                                                        value={userIdentityDraft.country}
                                                        onChange={(e) => setUserIdentityDraft({ ...userIdentityDraft, country: e.target.value })}
                                                        className="mt-1 w-full px-3 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-amber-500 focus:border-transparent text-sm"
                                                        placeholder="z.B. Deutschland, DE"
                                                    />
                                                </div>
                                            </div>
                                            <p className="text-xs text-gray-400 -mt-1">{tModals('userIdentity.locationHint')}</p>
                                            <div>
                                                <label className="text-gray-500 font-medium text-xs uppercase tracking-wide">{tModals('userIdentity.mainMessenger')}</label>
                                                <select
                                                    value={userIdentityDraft.main_messenger || ''}
                                                    onChange={(e) => setUserIdentityDraft({ ...userIdentityDraft, main_messenger: e.target.value })}
                                                    className="mt-1 w-full px-3 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-amber-500 focus:border-transparent text-sm"
                                                >
                                                    <option value="">{tModals('userIdentity.notSet')}</option>
                                                    <option value="telegram">Telegram</option>
                                                    <option value="discord">Discord</option>
                                                    <option value="slack">Slack</option>
                                                    <option value="whatsapp">WhatsApp</option>
                                                    <option value="email">Mail</option>
                                                </select>
                                                <p className="text-xs text-gray-400 mt-0.5">{tModals('userIdentity.mainMessengerHint')}</p>
                                            </div>
                                            <div>
                                                <label className="text-gray-500 font-medium text-xs uppercase tracking-wide">{tModals('userIdentity.preferences')} <span className="text-gray-400 font-normal">{tModals('userIdentity.preferencesHint')}</span></label>
                                                <textarea
                                                    value={userIdentityDraft.preferences}
                                                    onChange={(e) => {
                                                        setUserIdentityDraft({ ...userIdentityDraft, preferences: e.target.value });
                                                        e.target.style.height = 'auto';
                                                        e.target.style.height = e.target.scrollHeight + 'px';
                                                    }}
                                                    ref={(el) => { if (el) { el.style.height = 'auto'; el.style.height = el.scrollHeight + 'px'; } }}
                                                    className="mt-1 w-full px-3 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-amber-500 focus:border-transparent text-sm font-mono resize-none overflow-hidden"
                                                    style={{ minHeight: '2.5rem' }}
                                                    placeholder="always address me as 'du'&#10;I prefer dark mode"
                                                />
                                            </div>
                                            <div>
                                                <label className="text-gray-500 font-medium text-xs uppercase tracking-wide">{tModals('userIdentity.dos')} <span className="text-gray-400 font-normal">{tModals('userIdentity.preferencesHint')}</span></label>
                                                <textarea
                                                    value={userIdentityDraft.dos}
                                                    onChange={(e) => {
                                                        setUserIdentityDraft({ ...userIdentityDraft, dos: e.target.value });
                                                        e.target.style.height = 'auto';
                                                        e.target.style.height = e.target.scrollHeight + 'px';
                                                    }}
                                                    ref={(el) => { if (el) { el.style.height = 'auto'; el.style.height = el.scrollHeight + 'px'; } }}
                                                    className="mt-1 w-full px-3 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-amber-500 focus:border-transparent text-sm font-mono resize-none overflow-hidden"
                                                    style={{ minHeight: '2.5rem' }}
                                                    placeholder="Use German for responses&#10;Be concise"
                                                />
                                            </div>
                                            <div>
                                                <label className="text-gray-500 font-medium text-xs uppercase tracking-wide">{tModals('userIdentity.donts')} <span className="text-gray-400 font-normal">{tModals('userIdentity.preferencesHint')}</span></label>
                                                <textarea
                                                    value={userIdentityDraft.donts}
                                                    onChange={(e) => {
                                                        setUserIdentityDraft({ ...userIdentityDraft, donts: e.target.value });
                                                        e.target.style.height = 'auto';
                                                        e.target.style.height = e.target.scrollHeight + 'px';
                                                    }}
                                                    ref={(el) => { if (el) { el.style.height = 'auto'; el.style.height = el.scrollHeight + 'px'; } }}
                                                    className="mt-1 w-full px-3 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-amber-500 focus:border-transparent text-sm font-mono resize-none overflow-hidden"
                                                    style={{ minHeight: '2.5rem' }}
                                                    placeholder="Don't use formal address&#10;Don't add emojis"
                                                />
                                            </div>
                                        </div>
                                    ) : (
                                        /* View Mode - Display values */
                                        <div className="space-y-3 text-sm">
                                            <div className="bg-white rounded-lg p-3 border border-gray-100">
                                                <span className="text-gray-500 font-medium text-xs uppercase tracking-wide">{tModals('userIdentity.name')}</span>
                                                <p className="text-gray-900 mt-1">{personaData.user_identity.name || '—'}</p>
                                            </div>
                                            <div className="bg-white rounded-lg p-3 border border-gray-100">
                                                <span className="text-gray-500 font-medium text-xs uppercase tracking-wide">{tModals('userIdentity.language')}</span>
                                                <p className="text-gray-900 mt-1">{personaData.user_identity.preferred_language || '—'}</p>
                                            </div>
                                            <div className="bg-white rounded-lg p-3 border border-gray-100">
                                                <span className="text-gray-500 font-medium text-xs uppercase tracking-wide">{tModals('userIdentity.location')}</span>
                                                <p className="text-gray-900 mt-1">
                                                    {[personaData.user_identity.city, personaData.user_identity.country].filter(Boolean).join(', ') || '—'}
                                                </p>
                                            </div>
                                            <div className="bg-white rounded-lg p-3 border border-gray-100">
                                                <span className="text-gray-500 font-medium text-xs uppercase tracking-wide">{tModals('userIdentity.mainMessenger')}</span>
                                                <p className="text-gray-900 mt-1">{personaData.user_identity.main_messenger ? String(personaData.user_identity.main_messenger).charAt(0).toUpperCase() + String(personaData.user_identity.main_messenger).slice(1) : '—'}</p>
                                            </div>
                                            <div className="bg-white rounded-lg p-3 border border-gray-100">
                                                <span className="text-gray-500 font-medium text-xs uppercase tracking-wide">{tModals('userIdentity.preferences')}</span>
                                                {(personaData.user_identity.preferences?.length ?? 0) > 0 ? (
                                                    <ul className="mt-1 space-y-1">
                                                        {personaData.user_identity.preferences.map((p: string, i: number) => (
                                                            <li key={i} className="text-gray-900 flex items-start gap-2">
                                                                <span className="text-amber-500 mt-0.5">•</span>
                                                                <span>{p}</span>
                                                            </li>
                                                        ))}
                                                    </ul>
                                                ) : (
                                                    <p className="text-gray-400 mt-1">—</p>
                                                )}
                                            </div>
                                            <div className="bg-white rounded-lg p-3 border border-gray-100">
                                                <span className="text-gray-500 font-medium text-xs uppercase tracking-wide">{tModals('userIdentity.dos')}</span>
                                                {(personaData.user_identity.dos?.length ?? 0) > 0 ? (
                                                    <ul className="mt-1 space-y-1">
                                                        {personaData.user_identity.dos.map((d: string, i: number) => (
                                                            <li key={i} className="text-gray-900 flex items-start gap-2">
                                                                <span className="text-green-500 mt-0.5">✓</span>
                                                                <span>{d}</span>
                                                            </li>
                                                        ))}
                                                    </ul>
                                                ) : (
                                                    <p className="text-gray-400 mt-1">—</p>
                                                )}
                                            </div>
                                            <div className="bg-white rounded-lg p-3 border border-gray-100">
                                                <span className="text-gray-500 font-medium text-xs uppercase tracking-wide">{tModals('userIdentity.donts')}</span>
                                                {(personaData.user_identity.donts?.length ?? 0) > 0 ? (
                                                    <ul className="mt-1 space-y-1">
                                                        {personaData.user_identity.donts.map((d: string, i: number) => (
                                                            <li key={i} className="text-gray-900 flex items-start gap-2">
                                                                <span className="text-red-500 mt-0.5">✗</span>
                                                                <span>{d}</span>
                                                            </li>
                                                        ))}
                                                    </ul>
                                                ) : (
                                                    <p className="text-gray-400 mt-1">—</p>
                                                )}
                                            </div>
                                        </div>
                                    )
                                ) : (
                                    <p className="text-gray-500 text-sm">{tModals('userIdentity.noDataYet')}</p>
                                )}
                            </div>
                            {/* Right: Timeline (change_log) – schmal, scrollbar bei vielen Einträgen */}
                            <div className="w-80 shrink-0 min-h-0 flex flex-col border-l border-gray-100">
                                <h3 className="text-sm font-semibold text-gray-700 mb-3 shrink-0 p-4 pb-0">{tModals('userIdentity.timeline')}</h3>
                                {personaData?.user_identity?.change_log?.length > 0 ? (
                                    <div ref={timelineRef} className="flex-1 min-h-0 overflow-y-auto p-4 pt-3">
                                        <div className="relative">
                                            <div className="absolute left-3 top-2 bottom-2 w-0.5 bg-gradient-to-b from-amber-200 via-amber-300 to-amber-200" />
                                            <ul className="space-y-0">
                                                {[...(personaData?.user_identity?.change_log ?? []) as Array<{ at: string; action: string; source?: string }>].reverse().map((entry, i) => {
                                                    const isManual = entry.source === 'settings_ui';
                                                    const isNewEntry = i < newTimelineEntryCount;
                                                    return (
                                                        <li
                                                            key={i}
                                                            className={`relative flex gap-3 pb-4 last:pb-0 transition-all duration-500 ${
                                                                isNewEntry ? 'animate-pulse bg-blue-50/50 -mx-2 px-2 rounded-lg' : ''
                                                            }`}
                                                        >
                                                            <div className={`relative z-10 w-6 h-6 rounded-full shrink-0 mt-0.5 flex items-center justify-center transition-transform duration-300 ${
                                                                isNewEntry ? 'scale-110' : ''
                                                            } ${
                                                                isManual
                                                                    ? 'bg-blue-100 border-2 border-blue-400'
                                                                    : 'bg-amber-200 border-2 border-amber-500'
                                                            }`}>
                                                                {isManual ? (
                                                                    <svg className="w-3 h-3 text-blue-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                                                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z" />
                                                                    </svg>
                                                                ) : (
                                                                    <svg className="w-3 h-3 text-amber-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                                                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
                                                                    </svg>
                                                                )}
                                                            </div>
                                                            <div className="flex-1 min-w-0">
                                                                <div className="flex items-center gap-2">
                                                                    <p className="text-xs text-gray-500 font-mono">{typeof entry.at === 'string' ? new Date(entry.at).toLocaleString() : entry.at}</p>
                                                                    {isManual && (
                                                                        <span className="text-[10px] px-1.5 py-0.5 bg-blue-100 text-blue-700 rounded-full">{tModals('userIdentity.manual')}</span>
                                                                    )}
                                                                    {isNewEntry && (
                                                                        <span className="text-[10px] px-1.5 py-0.5 bg-green-100 text-green-700 rounded-full animate-bounce">{tModals('userIdentity.new')}</span>
                                                                    )}
                                                                </div>
                                                                <p className="text-sm text-gray-900 font-medium mt-0.5">{entry.action || 'update'}</p>
                                                            </div>
                                                        </li>
                                                    );
                                                })}
                                            </ul>
                                        </div>
                                    </div>
                                ) : (
                                    <div className="flex-1 min-h-0 overflow-y-auto p-4 pt-0">
                                        <p className="text-gray-500 text-sm">{tModals('userIdentity.noUpdatesYet')}</p>
                                    </div>
                                )}
                            </div>
                        </div>
                    </div>
                </div>
            )}

            {/* Add User Modal */}
            {showAddUserModal && (
                <div className="fixed inset-0 z-[70] flex items-center justify-center p-4" onClick={() => setShowAddUserModal(false)}>
                    <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" />
                    <div
                        className="relative bg-white w-full max-w-2xl rounded-2xl shadow-2xl border border-gray-200 flex flex-col animate-in fade-in zoom-in-95 duration-200 max-h-[90vh] overflow-hidden"
                        onClick={(e) => e.stopPropagation()}
                    >
                        {/* Header */}
                        <div className="flex items-center justify-between p-6 border-b border-gray-200 bg-gray-50">
                            <div className="flex items-center gap-3">
                                <div className="w-10 h-10 rounded-xl bg-gray-900 flex items-center justify-center">
                                    <Users className="w-5 h-5 text-white" />
                                </div>
                                <div>
                                    <h2 className="text-xl font-bold text-gray-900">{tModals('addUser.title')}</h2>
                                    <p className="text-sm text-gray-500">{tModals('addUser.subtitle')}</p>
                                </div>
                            </div>
                            <button onClick={() => setShowAddUserModal(false)} className="p-2 hover:bg-gray-200 rounded-lg transition-colors">
                                <X className="w-5 h-5 text-gray-500" />
                            </button>
                        </div>

                        {/* Content */}
                        <div className="p-6 space-y-6 overflow-y-auto">
                            {/* Basic Info */}
                            <div className="grid grid-cols-2 gap-4">
                                <Input
                                    label={tModals('addUser.usernameRequired')}
                                    value={newUser.username}
                                    onChange={(v) => setNewUser({...newUser, username: v})}
                                    placeholder="johndoe"
                                />
                                <Input
                                    label={tModals('addUser.emailRequired')}
                                    value={newUser.email}
                                    onChange={(v) => setNewUser({...newUser, email: v})}
                                    placeholder="john@example.com"
                                />
                                <Select
                                    label={tModals('addUser.roleRequired')}
                                    value={newUser.role}
                                    onChange={(v) => setNewUser({...newUser, role: v})}
                                    options={[
                                        { value: 'User', label: tModals('addUser.roleUser') },
                                        { value: 'Admin', label: tModals('addUser.roleAdmin') },
                                        { value: 'Guest', label: tModals('addUser.roleGuest') }
                                    ]}
                                />
                                <Input
                                    label={tModals('addUser.initialPassword')}
                                    value={newUser.password}
                                    onChange={(v) => setNewUser({...newUser, password: v})}
                                    type="password"
                                    placeholder={tModals('addUser.autoGenerated')}
                                />
                            </div>

                            {/* Tools Section */}
                            <div className="space-y-3">
                                <div className="flex items-center justify-between">
                                    <div>
                                        <h4 className="text-sm font-medium text-gray-700">{tModals('addUser.availableTools')}</h4>
                                        <p className="text-xs text-gray-400">{tModals('addUser.availableToolsDesc')}</p>
                                    </div>
                                    <button
                                        type="button"
                                        onClick={() => {
                                            const allToolNames = tools.map(t => t.name);
                                            const allSelected = allToolNames.every(name => newUser.tools.includes(name));
                                            setNewUser({...newUser, tools: allSelected ? [] : allToolNames});
                                        }}
                                        className="text-xs px-3 py-1 rounded-lg bg-gray-100 hover:bg-gray-200 text-gray-600 transition-colors"
                                    >
                                        {tools.length > 0 && tools.every(t => newUser.tools.includes(t.name)) ? tCommon('deselectAll') : tCommon('selectAll')}
                                    </button>
                                </div>
                                <div className="max-h-36 overflow-y-auto border border-gray-200 rounded-xl p-3 bg-gray-50 grid grid-cols-3 gap-2">
                                    {tools.length > 0 ? tools.map(tool => (
                                        <label key={tool.name} className="flex items-center gap-2 p-2 rounded-lg hover:bg-white cursor-pointer transition-colors border border-transparent hover:border-gray-200">
                                            <input
                                                type="checkbox"
                                                checked={newUser.tools.includes(tool.name)}
                                                onChange={(e) => {
                                                    if (e.target.checked) {
                                                        setNewUser({...newUser, tools: [...newUser.tools, tool.name]});
                                                    } else {
                                                        setNewUser({...newUser, tools: newUser.tools.filter(t => t !== tool.name)});
                                                    }
                                                }}
                                                className="rounded border-gray-300 text-gray-900 focus:ring-gray-400 accent-gray-900"
                                            />
                                            <span className="text-sm text-gray-700 truncate" title={tool.description}>{tool.name}</span>
                                        </label>
                                    )) : (
                                        <div className="col-span-3 text-center py-4 text-sm text-gray-400">{tModals('addUser.noToolsAvailable')}</div>
                                    )}
                                </div>
                                <p className="text-xs text-gray-400">{tModals('addUser.toolsSelected', { selected: newUser.tools.length, total: tools.length })}</p>
                            </div>

                            {/* Workflows Section */}
                            <div className="space-y-3">
                                <div className="flex items-center justify-between">
                                    <div>
                                        <h4 className="text-sm font-medium text-gray-700">{tModals('addUser.availableWorkflows')}</h4>
                                        <p className="text-xs text-gray-400">{tModals('addUser.availableWorkflowsDesc')}</p>
                                    </div>
                                    <button
                                        type="button"
                                        onClick={() => {
                                            const allWorkflowIds = workflows.map(w => w.id);
                                            const allSelected = allWorkflowIds.every(id => newUser.workflows.includes(id));
                                            setNewUser({...newUser, workflows: allSelected ? [] : allWorkflowIds});
                                        }}
                                        className="text-xs px-3 py-1 rounded-lg bg-gray-100 hover:bg-gray-200 text-gray-600 transition-colors"
                                    >
                                        {workflows.length > 0 && workflows.every(w => newUser.workflows.includes(w.id)) ? tCommon('deselectAll') : tCommon('selectAll')}
                                    </button>
                                </div>
                                <div className="max-h-36 overflow-y-auto border border-gray-200 rounded-xl p-3 bg-gray-50 grid grid-cols-2 gap-2">
                                    {workflows.length > 0 ? workflows.map(workflow => (
                                        <label key={workflow.id} className="flex items-center gap-2 p-2 rounded-lg hover:bg-white cursor-pointer transition-colors border border-transparent hover:border-gray-200">
                                            <input
                                                type="checkbox"
                                                checked={newUser.workflows.includes(workflow.id)}
                                                onChange={(e) => {
                                                    if (e.target.checked) {
                                                        setNewUser({...newUser, workflows: [...newUser.workflows, workflow.id]});
                                                    } else {
                                                        setNewUser({...newUser, workflows: newUser.workflows.filter(w => w !== workflow.id)});
                                                    }
                                                }}
                                                className="rounded border-gray-300 text-gray-900 focus:ring-gray-400 accent-gray-900"
                                            />
                                            <div className="flex-1 min-w-0">
                                                <span className="text-sm text-gray-700 truncate block">{workflow.name}</span>
                                                <span className="text-xs text-gray-400">{workflow.steps} steps</span>
                                            </div>
                                        </label>
                                    )) : (
                                        <div className="col-span-2 text-center py-4 text-sm text-gray-400">{tModals('addUser.noWorkflowsAvailable')}</div>
                                    )}
                                </div>
                                <p className="text-xs text-gray-400">{tModals('addUser.workflowsSelected', { selected: newUser.workflows.length, total: workflows.length })}</p>
                            </div>

                            {/* Memory Database Toggle */}
                            <div className="flex items-center gap-4 p-4 bg-white border border-gray-200 rounded-xl shadow-sm">
                                <div className="w-10 h-10 rounded-xl bg-gray-100 flex items-center justify-center">
                                    <Database size={20} className="text-gray-600" />
                                </div>
                                <div className="flex-1">
                                    <div className="text-sm font-medium text-gray-900">{tModals('addUser.enableMemory')}</div>
                                    <div className="text-xs text-gray-500">
                                        {newUser.createDb
                                            ? tModals('addUser.enableMemoryOn')
                                            : tModals('addUser.enableMemoryOff')}
                                    </div>
                                </div>
                                <button
                                    type="button"
                                    onClick={() => setNewUser({...newUser, createDb: !newUser.createDb})}
                                    className={cn(
                                        "relative w-11 h-6 rounded-full transition-colors",
                                        newUser.createDb ? "bg-green-500" : "bg-gray-300"
                                    )}
                                >
                                    <div className={cn(
                                        "absolute top-1 w-4 h-4 rounded-full bg-white shadow transition-transform",
                                        newUser.createDb ? "translate-x-6" : "translate-x-1"
                                    )} />
                                </button>
                            </div>
                        </div>

                        {/* Footer */}
                        <div className="flex items-center justify-between p-6 border-t border-gray-200 bg-gray-50">
                            <button
                                onClick={() => setShowAddUserModal(false)}
                                className="text-gray-600 hover:bg-gray-200 px-4 py-2 rounded-lg transition-colors"
                            >
                                {tCommon('cancel')}
                            </button>
                            <button onClick={handleCreateUser} className="bg-gray-900 hover:bg-gray-800 text-white px-6 py-2 rounded-lg font-medium flex items-center gap-2 transition-colors">
                                <Plus size={18} />
                                {tModals('addUser.createUser')}
                            </button>
                        </div>
                    </div>
                </div>
            )}

            {/* Edit User Modal */}
            {editingUser && (
                <div className="fixed inset-0 z-[70] flex items-center justify-center p-4" onClick={() => setEditingUser(null)}>
                    <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" />
                    <div
                        className="relative bg-white w-full max-w-lg rounded-2xl shadow-2xl border border-gray-200 flex flex-col animate-in fade-in zoom-in-95 duration-200"
                        onClick={(e) => e.stopPropagation()}
                    >
                        <div className="flex items-center justify-between p-6 border-b border-gray-100 bg-gray-50/50 rounded-t-2xl">
                            <div className="flex items-center gap-3">
                                <div className="w-10 h-10 rounded-full bg-blue-100 flex items-center justify-center text-blue-600 font-bold border border-blue-200">
                                    {editingUser.username[0].toUpperCase()}
                                </div>
                                <div>
                                    <h2 className="text-xl font-bold text-gray-900">{tModals('editUser.title')}</h2>
                                    <p className="text-sm text-gray-500">{editingUser.username}</p>
                                </div>
                            </div>
                            <button onClick={() => setEditingUser(null)} className="p-2 text-gray-400 hover:text-gray-600 rounded-lg hover:bg-gray-100 transition-colors">
                                <X size={20} />
                            </button>
                        </div>
                        
                        <div className="p-6 space-y-6">
                            <div className="space-y-4">
                                <div className="grid grid-cols-2 gap-4">
                                    <Input
                                        label={tLocalNet('username')}
                                        value={editingUser.username}
                                        onChange={(v) => setEditingUser({...editingUser, username: v})}
                                    />
                                    <Select
                                        label={tLocalNet('role')}
                                        value={editingUser.role}
                                        onChange={(v) => setEditingUser({...editingUser, role: v})}
                                        options={[
                                            { value: 'User', label: tModals('addUser.roleUser') },
                                            { value: 'Admin', label: tModals('addUser.roleAdmin') },
                                            { value: 'Guest', label: tModals('addUser.roleGuest') }
                                        ]}
                                    />
                                </div>
                                <Input
                                    label="E-Mail"
                                    value={editingUser.email}
                                    onChange={(v) => setEditingUser({...editingUser, email: v})}
                                />
                            </div>

                            <Section title={tModals('editUser.securityAccess')}>
                                <div className="space-y-3">
                                    <div className="flex items-center justify-between p-3 border border-gray-200 rounded-lg hover:border-gray-300 transition-colors">
                                        <div className="flex items-center gap-3">
                                            <div className="p-2 bg-yellow-50 text-yellow-600 rounded-lg">
                                                <Lock size={16} />
                                            </div>
                                            <span className="text-sm font-medium text-gray-700">{tModals('editUser.password')}</span>
                                        </div>
                                        <button className="text-xs font-medium text-blue-600 hover:text-blue-700 hover:underline">{tCommon('reset')}</button>
                                    </div>
                                    
                                    <div className="flex items-center justify-between p-3 border border-gray-200 rounded-lg hover:border-gray-300 transition-colors">
                                        <div className="flex items-center gap-3">
                                            <div className="p-2 bg-purple-50 text-purple-600 rounded-lg">
                                                <Shield size={16} />
                                            </div>
                                            <span className="text-sm font-medium text-gray-700">{tModals('editUser.twoFaStatus')}</span>
                                        </div>
                                        <button className="text-xs font-medium text-blue-600 hover:text-blue-700 hover:underline">{tModals('editUser.resetTwoFa')}</button>
                                    </div>

                                    <div className="flex items-center justify-between p-3 border border-gray-200 rounded-lg hover:border-gray-300 transition-colors">
                                        <div className="flex items-center gap-3">
                                            <div className={cn("p-2 rounded-lg transition-colors", editingUser.status === 'active' ? "bg-green-50 text-green-600" : "bg-gray-100 text-gray-500")}>
                                                {editingUser.status === 'active' ? <CheckCircle size={16} /> : <XCircle size={16} />}
                                            </div>
                                            <span className="text-sm font-medium text-gray-700">{tModals('editUser.accountStatus')}</span>
                                        </div>
                                        <Switch 
                                            label="" 
                                            checked={editingUser.status === 'active'} 
                                            onChange={(v) => setEditingUser({...editingUser, status: v ? 'active' : 'inactive'})} 
                                        />
                                    </div>
                                </div>
                            </Section>
                        </div>

                        <div className="flex items-center justify-between p-6 border-t border-gray-100 bg-gray-50/50 rounded-b-2xl">
                            <button onClick={handleDeleteUser} className="px-4 py-2 text-red-600 hover:bg-red-50 font-medium rounded-lg transition-colors flex items-center gap-2">
                                <Trash2 size={16} /> {tModals('editUser.deleteUser')}
                            </button>
                            <div className="flex gap-3">
                                <button onClick={() => setEditingUser(null)} className="px-4 py-2 text-gray-600 hover:bg-gray-200 font-medium rounded-lg transition-colors">
                                    {tCommon('cancel')}
                                </button>
                                <button onClick={handleUpdateUser} className="px-6 py-2 bg-gray-900 text-white hover:bg-black font-medium rounded-lg shadow-sm hover:shadow transition-all flex items-center gap-2">
                                    <Save size={16} /> {tCommon('saveChanges')}
                                </button>
                            </div>
                        </div>
                    </div>
                </div>
            )}

            {/* User Detail Modal */}
            {selectedUser && (
                <div className="fixed inset-0 z-[80] flex items-center justify-center p-4" onClick={() => setSelectedUser(null)}>
                    <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" />
                    <div
                        className="relative bg-white w-full max-w-2xl max-h-[90vh] rounded-2xl shadow-2xl border border-gray-200 flex flex-col animate-in fade-in zoom-in-95 duration-200 overflow-hidden"
                        onClick={(e) => e.stopPropagation()}
                    >
                        {/* Header */}
                        <div className="h-20 border-b border-gray-100 flex items-center justify-between px-8 shrink-0 bg-gray-50/50">
                            <div className="flex items-center gap-4">
                                <div className="w-12 h-12 rounded-full bg-blue-100 text-blue-600 flex items-center justify-center text-xl font-bold">
                                    {selectedUser.username[0].toUpperCase()}
                                </div>
                                <div>
                                    <h2 className="text-xl font-bold text-gray-900">{selectedUser.username}</h2>
                                    <div className="flex items-center gap-2 mt-0.5">
                                        <span className="px-2 py-0.5 bg-gray-200 text-gray-700 text-[10px] font-bold uppercase tracking-wider rounded">
                                            {selectedUser.role}
                                        </span>
                                        <span className={cn("text-xs flex items-center gap-1", selectedUser.status === 'active' ? "text-green-600" : "text-gray-400")}>
                                            <div className={cn("w-1.5 h-1.5 rounded-full", selectedUser.status === 'active' ? "bg-green-500" : "bg-gray-400")} />
                                            {selectedUser.status === 'active' ? tModals('userDetail.activeAccount') : tCommon('inactive')}
                                        </span>
                                    </div>
                                </div>
                            </div>
                            <button onClick={() => setSelectedUser(null)} className="p-2 text-gray-400 hover:text-gray-600 rounded-full hover:bg-gray-200 transition-colors">
                                <X size={24} />
                            </button>
                        </div>
                        
                        {/* Content */}
                        <div className="flex-1 overflow-y-auto p-8 space-y-8">
                            
                            {/* Stats Grid */}
                            <div className="grid grid-cols-3 gap-4">
                                <div className="p-4 bg-gray-50 rounded-xl border border-gray-100">
                                    <div className="text-xs text-gray-500 uppercase tracking-wide font-semibold mb-1">{tModals('userDetail.lastActive')}</div>
                                    <div className="text-lg font-mono font-medium text-gray-900">{selectedUser.lastActive}</div>
                                </div>
                                <div className="p-4 bg-gray-50 rounded-xl border border-gray-100">
                                    <div className="text-xs text-gray-500 uppercase tracking-wide font-semibold mb-1">{tModals('userDetail.accessLevel')}</div>
                                    <div className="text-lg font-medium text-gray-900 capitalize">{selectedUser.access}</div>
                                </div>
                                <div className="p-4 bg-gray-50 rounded-xl border border-gray-100">
                                    <div className="text-xs text-gray-500 uppercase tracking-wide font-semibold mb-1">{tModals('userDetail.memoryUsage')}</div>
                                    <div className="text-lg font-mono font-medium text-gray-900">24.5 MB</div>
                                </div>
                            </div>

                            {/* Permissions */}
                            <div className="space-y-6">
                                <div>
                                    <h4 className="text-sm font-bold text-gray-900 mb-3 flex items-center gap-2">
                                        <Cpu size={16} /> {tModals('userDetail.authorizedTools')}
                                    </h4>
                                    <div className="grid grid-cols-2 gap-2">
                                        {['Web Search', 'File System', 'Code Interpreter', 'Memory System', 'Data Analysis', 'Image Gen'].map(tool => {
                                            const isEnabled = selectedUser.tools.includes('all') || selectedUser.tools.includes(tool.toLowerCase().replace(' ', '_'));
                                            return (
                                                <div key={tool} className={cn(
                                                    "flex items-center justify-between p-3 rounded-lg border transition-all",
                                                    isEnabled ? "bg-white border-green-200 shadow-sm" : "bg-gray-50 border-gray-100 opacity-60"
                                                )}>
                                                    <span className={cn("text-sm", isEnabled ? "text-gray-900 font-medium" : "text-gray-500")}>{tool}</span>
                                                    {isEnabled && <CheckCircle size={16} className="text-green-500" />}
                                                </div>
                                            );
                                        })}
                                    </div>
                                </div>

                                <div>
                                    <h4 className="text-sm font-bold text-gray-900 mb-3 flex items-center gap-2">
                                        <Workflow size={16} /> {tModals('userDetail.activeWorkflows')}
                                    </h4>
                                    <div className="space-y-2">
                                        {['Daily Summary', 'Code Review', 'Data Sync'].map(wf => {
                                            const isEnabled = selectedUser.workflows.includes('all');
                                            return (
                                                <div key={wf} className="flex items-center justify-between p-3 bg-white border border-gray-200 rounded-lg">
                                                    <span className="text-sm text-gray-700">{wf}</span>
                                                    <Switch label="" checked={isEnabled} onChange={() => {}} />
                                                </div>
                                            );
                                        })}
                                    </div>
                                </div>
                            </div>
                        </div>

                        {/* Footer */}
                        <div className="h-20 border-t border-gray-100 flex items-center justify-end px-8 gap-4 bg-gray-50/50 shrink-0">
                            <button
                                onClick={() => setSelectedUser(null)}
                                className="px-6 py-2.5 rounded-xl font-medium text-gray-600 hover:bg-gray-200 transition-colors"
                            >
                                {tCommon('close')}
                            </button>
                            <button
                                onClick={() => {
                                    setEditingUser(selectedUser);
                                    setSelectedUser(null);
                                }}
                                className="px-6 py-2.5 rounded-xl font-medium bg-blue-600 text-white hover:bg-blue-700 shadow-lg shadow-blue-200 transition-all flex items-center gap-2"
                            >
                                <Edit size={16} /> {tModals('userDetail.editUser')}
                            </button>
                        </div>
                    </div>
                </div>
            )}

            {/* Network Topology Modal */}
            {showNetworkModal && (
                <div className="fixed inset-0 z-[80] flex items-center justify-center p-4" onClick={() => setShowNetworkModal(false)}>
                    <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" />
                    <div
                        className="relative bg-white w-full max-w-[90vw] h-[90vh] rounded-2xl shadow-2xl border border-gray-200 flex flex-col animate-in fade-in zoom-in-95 duration-200 overflow-hidden"
                        onClick={(e) => e.stopPropagation()}
                    >
                        {/* Header */}
                        <div className="h-20 border-b border-gray-100 flex items-center justify-between px-8 shrink-0 bg-white z-10">
                            <div className="flex items-center gap-4">
                                <div className="w-12 h-12 rounded-xl bg-gray-100 text-gray-700 flex items-center justify-center shadow-sm">
                                    <Network size={24} />
                                </div>
                                <div>
                                    <h2 className="text-2xl font-bold text-gray-800">{tModals('networkMap.title')}</h2>
                                    <p className="text-sm text-gray-500">
                                        {tModals('networkMap.realtimeTopology')}{networkNodes.length > 1 ? ` • ${tModals('networkMap.devices', { count: networkNodes.length - 1 })}` : ` • ${tModals('networkMap.noActiveDevices')}`}
                                    </p>
                                </div>
                            </div>
                            <div className="flex items-center gap-3">
                                <div className="px-3 py-1.5 bg-green-50 text-green-700 text-xs font-medium rounded-full border border-green-100 flex items-center gap-2">
                                    <div className="w-2 h-2 rounded-full bg-green-500 animate-pulse" />
                                    {tModals('networkMap.systemOnline')}
                                </div>
                                <button onClick={() => setShowNetworkModal(false)} className="p-2 text-gray-400 hover:text-gray-600 rounded-full hover:bg-gray-100 transition-colors">
                                    <X size={24} />
                                </button>
                            </div>
                        </div>
                        
                        {/* Graph Content - Lazy loaded */}
                        <div className="flex-1 overflow-hidden bg-gray-50 relative">
                            <Suspense fallback={<ReactFlowFallback />}>
                                <ReactFlow
                                    nodes={networkNodes}
                                    edges={networkEdges}
                                    onNodesChange={onNetworkNodesChange}
                                    onEdgesChange={onNetworkEdgesChange}
                                    fitView
                                    fitViewOptions={{ padding: 0.2 }}
                                    proOptions={{ hideAttribution: true }}
                                >
                                    <Background color="#e5e7eb" gap={20} />
                                    <Controls className="bg-white border-gray-200 shadow-sm text-gray-500" />
                                    <MiniMap
                                        nodeColor={() => '#3b82f6'}
                                        maskColor="rgba(243, 244, 246, 0.7)"
                                        className="bg-white border-gray-200 shadow-sm"
                                    />
                                </ReactFlow>
                            </Suspense>

                            {/* Legend Overlay */}
                            <div className="absolute top-6 left-6 p-4 bg-white/90 backdrop-blur rounded-xl border border-gray-200 shadow-lg space-y-3 z-10">
                                <h4 className="text-xs font-bold text-gray-500 uppercase tracking-wide">{tModals('networkMap.deviceTypes')}</h4>
                                <div className="space-y-2">
                                    <div className="flex items-center gap-2 text-sm text-gray-600">
                                        <div className="w-6 h-6 rounded bg-gray-900 flex items-center justify-center text-white"><Server size={12} /></div>
                                        <span>{tModals('networkMap.vafHost')}</span>
                                    </div>
                                    <div className="flex items-center gap-2 text-sm text-gray-600">
                                        <div className="w-6 h-6 rounded bg-green-100 text-green-600 flex items-center justify-center border border-green-200"><Monitor size={12} /></div>
                                        <span>{tModals('networkMap.desktop')}</span>
                                    </div>
                                    <div className="flex items-center gap-2 text-sm text-gray-600">
                                        <div className="w-6 h-6 rounded bg-purple-100 text-purple-600 flex items-center justify-center border border-purple-200"><Laptop size={12} /></div>
                                        <span>{tModals('networkMap.laptop')}</span>
                                    </div>
                                    <div className="flex items-center gap-2 text-sm text-gray-600">
                                        <div className="w-6 h-6 rounded bg-pink-100 text-pink-600 flex items-center justify-center border border-pink-200"><Smartphone size={12} /></div>
                                        <span>{tModals('networkMap.mobile')}</span>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            )}

            {/* Network Security Warning Modal */}
            {showNetworkWarning && (
                <div className="fixed inset-0 z-[80] flex items-center justify-center p-4">
                    <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={() => setShowNetworkWarning(false)} />
                    <div className="relative bg-white rounded-xl shadow-xl w-full max-w-md overflow-hidden animate-in fade-in zoom-in-95 duration-200">
                        <div className="p-6 text-center space-y-4">
                            <div className="w-16 h-16 bg-yellow-100 rounded-full flex items-center justify-center mx-auto mb-2">
                                <ShieldAlert size={32} className="text-yellow-600" />
                            </div>
                            <h3 className="text-xl font-bold text-gray-900">{tLocalNet('securityWarningTitle')}</h3>
                            <p className="text-sm text-gray-600 leading-relaxed">
                                {tLocalNet('securityWarningText')}
                                <br/><br/>
                                <strong className="text-gray-800">{tLocalNet('securityWarningStrong')}</strong>
                            </p>
                        </div>
                        <div className="flex items-center border-t border-gray-100 bg-gray-50/50 p-4 gap-3">
                            <button 
                                onClick={() => setShowNetworkWarning(false)}
                                className="flex-1 py-2.5 bg-white border border-gray-200 text-gray-700 font-medium rounded-lg hover:bg-gray-50 transition-colors"
                            >
                                {tCommon('cancel')}
                            </button>
                            <button
                                onClick={() => {
                                    handleChange('local_network_enabled', true);
                                    setShowNetworkWarning(false);
                                }}
                                className="flex-1 py-2.5 bg-gray-900 text-white font-medium rounded-lg hover:bg-black transition-colors"
                            >
                                {tLocalNet('enableHosting')}
                            </button>
                        </div>
                    </div>
                </div>
            )}

            {/* Restarting Animation Modal */}
            {isRestarting && (
                <div className="fixed inset-0 z-[90] flex items-center justify-center p-4">
                    <div className="absolute inset-0 bg-black/60 backdrop-blur-sm cursor-wait" />
                    <div className="relative bg-white rounded-2xl shadow-2xl p-8 flex flex-col items-center gap-4 animate-in fade-in zoom-in-95 duration-300">
                        <div className="relative">
                            <div className="w-16 h-16 border-4 border-gray-100 border-t-gray-900 rounded-full animate-spin" />
                            <div className="absolute inset-0 flex items-center justify-center">
                                <Network size={24} className="text-gray-900" />
                            </div>
                        </div>
                        <div className="text-center">
                            <h3 className="text-lg font-bold text-gray-900">{tLocalNet('applyingNetworkSettings')}</h3>
                            <p className="text-sm text-gray-500 mt-1">{tLocalNet('restartingServer')}</p>
                        </div>
                    </div>
                </div>
            )}

            {/* Discord Setup Wizard - renders as full-screen modal */}
            <DiscordSetupWizard
                isOpen={showDiscordWizard}
                onClose={() => setShowDiscordWizard(false)}
                onComplete={handleDiscordComplete}
                existingConfig={localConfig.discord_config}
            />

            {/* Telegram Setup Wizard */}
            <TelegramSetupWizard
                isOpen={showTelegramWizard}
                onClose={() => setShowTelegramWizard(false)}
                onComplete={handleTelegramComplete}
                existingConfig={localConfig.telegram_config}
            />

            {/* WhatsApp Setup Wizard */}
            <WhatsAppSetupWizard
                isOpen={showWhatsAppWizard}
                onClose={() => setShowWhatsAppWizard(false)}
                onComplete={() => {
                    setShowWhatsAppWizard(false);
                    onRefreshConfig?.();
                }}
            />

            {/* WhatsApp Dashboard (when configured, Settings opens this) */}
            <WhatsAppDashboard
                isOpen={showWhatsAppDashboard}
                onClose={() => setShowWhatsAppDashboard(false)}
                config={localConfig}
                onConfigChange={handleChange}
                onOpenSetupWizard={() => {
                    setShowWhatsAppDashboard(false);
                    setShowWhatsAppWizard(true);
                }}
            />

            {/* Telegram Dashboard (when configured, Settings opens this) */}
            <TelegramDashboard
                isOpen={showTelegramDashboard}
                onClose={() => setShowTelegramDashboard(false)}
                config={localConfig}
                onConfigChange={handleChange}
            />

            {/* Contacts (list + personal file, from Connections) */}
            <ContactsDashboard
                isOpen={showContactsDashboard}
                onClose={() => setShowContactsDashboard(false)}
            />

            {/* Discord Dashboard (when configured, Settings opens this) */}
            <DiscordDashboard
                isOpen={showDiscordDashboard}
                onClose={() => setShowDiscordDashboard(false)}
                config={localConfig}
                onConfigChange={handleChange}
            />

            {/* Mail Dashboard (same size as Telegram Dashboard) */}
            <MailDashboard
                isOpen={showMailDashboard}
                onClose={() => setShowMailDashboard(false)}
                onOpenAddWizard={() => setShowEmailWizard(true)}
                refreshTrigger={mailDashboardRefresh}
            />

            {/* Email Setup Wizard (opened from Mail Dashboard "Add account") */}
            <EmailSetupWizard
                isOpen={showEmailWizard}
                onClose={() => {
                    setShowEmailWizard(false);
                    setMailDashboardRefresh(r => r + 1);
                }}
                onComplete={() => {
                    setShowEmailWizard(false);
                    setMailDashboardRefresh(r => r + 1);
                }}
                existingAccounts={localConfig?.email_config?.accounts || []}
                currentUser={currentUser}
            />

            {/* Cloud Storage Dashboard (Mail-style: accounts left, files middle) */}
            <CloudDashboard
                isOpen={showCloudDashboard}
                onClose={() => setShowCloudDashboard(false)}
                onOpenAddWizard={(provider?: string) => {
                    setShowCloudDashboard(false);
                    setCloudWizardProvider(provider);
                    setShowCloudWizard(true);
                }}
                refreshTrigger={cloudDashboardRefresh}
            />

            {/* Cloud Storage Setup Wizard */}
            <CloudSetupWizard
                isOpen={showCloudWizard}
                onClose={() => {
                    setShowCloudWizard(false);
                    setCloudWizardProvider(undefined);
                }}
                onComplete={() => {
                    setShowCloudWizard(false);
                    setCloudWizardProvider(undefined);
                    setCloudDashboardRefresh(r => r + 1);
                    onRefreshConfig?.();
                }}
                initialProvider={cloudWizardProvider}
                currentUser={currentUser}
            />

            {/* Soul Wizard Modal */}
            <SoulWizard
                isOpen={showSoulWizard}
                onClose={() => setShowSoulWizard(false)}
                username={currentUser?.username || 'Admin'}
                onComplete={(content) => {
                    if (personaData) {
                        setPersonaData({ ...personaData, soul: content });
                    }
                    fetch('/api/user/soul', {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ content })
                    });
                }}
            />
        </div>
    );
}

// UI Components with explicit types
interface InputProps {
    label: string;
    value: any;
    onChange: (value: string) => void;
    type?: string;
    placeholder?: string;
}

const Input = ({ label, value, onChange, type = "text", placeholder }: InputProps) => (
    <div className="flex flex-col gap-1.5 w-full">
        <label className="text-sm font-medium text-gray-700 ml-1">{label}</label>
        <input
            type={type}
            value={value}
            onChange={(e) => onChange(e.target.value)}
            placeholder={placeholder}
            className="px-4 h-10 bg-white border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-gray-500 transition-all placeholder:text-gray-400"
        />
    </div>
);

interface SelectProps {
    label: string;
    value: any;
    onChange: (value: string) => void;
    options: { value: string; label: string }[];
}

const Select = ({ label, value, onChange, options }: SelectProps) => {
    const uniqueOptions = options.filter((option, index) => {
        return options.findIndex((candidate) => candidate.value === option.value) === index;
    });

    return (
        <div className="flex flex-col gap-1.5 w-full">
            <label className="text-sm font-medium text-gray-700 ml-1">{label}</label>
            <div className="relative">
                <select
                    value={value}
                    onChange={(e) => onChange(e.target.value)}
                    className="w-full h-10 appearance-none px-4 bg-white border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-gray-500 transition-all text-gray-700 pr-10"
                >
                    {/* Default option if current value is not in options (e.g. custom input previously saved) */}
                    {!uniqueOptions.some(o => o.value === value) && value && (
                        <option value={value}>{value} (Current)</option>
                    )}
                    {uniqueOptions.map((o) => (
                        <option key={o.value} value={o.value}>{o.label}</option>
                    ))}
                </select>
                <div className="absolute right-3 top-1/2 -translate-y-1/2 pointer-events-none text-gray-400">
                    <ChevronRight size={16} className="rotate-90" />
                </div>
            </div>
        </div>
    );
};

interface SwitchProps {
    label: string;
    description?: string;
    checked: boolean;
    onChange: (checked: boolean) => void;
}

const Switch = ({ label, description, checked, onChange }: SwitchProps) => (
    <div className="flex items-start justify-between">
        <div className="flex flex-col gap-0.5">
            <span className="text-sm font-medium text-gray-700">{label}</span>
            {description && <span className="text-xs text-gray-400">{description}</span>}
        </div>
        <button
            type="button"
            onClick={() => onChange(!checked)}
            className={cn(
                "w-11 h-6 rounded-full transition-colors relative shrink-0",
                checked ? "bg-green-500" : "bg-gray-200"
            )}
        >
            <div className={cn(
                "absolute top-0.5 left-0.5 w-5 h-5 bg-white rounded-full shadow-sm transition-transform duration-200",
                checked ? "translate-x-5" : "translate-x-0"
            )} />
        </button>
    </div>
);

const Section = ({ title, children }: { title: string, children: React.ReactNode }) => (
    <div className="bg-gray-50/50 p-6 rounded-xl border border-gray-100">
        <h3 className="text-sm font-bold text-gray-900 uppercase tracking-wide mb-4">{title}</h3>
        {children}
    </div>
);
