'use client';

import React, { useState, useEffect, useCallback } from 'react';
import ReactFlow, { 
    Background, 
    Controls, 
    MiniMap, 
    useNodesState, 
    useEdgesState,
    Position,
    MarkerType
} from 'reactflow';
import 'reactflow/dist/style.css';
import {
    X, Globe, Cpu, Volume2, Monitor, Shield, Save, RotateCcw,
    Check, ChevronRight, Zap, Search, Download, RefreshCw, Workflow, GitBranch,
    Brain, Database, Link2, MessageSquare, Network, Users, Lock, Server, Laptop, Smartphone,
    Edit, Trash2, Plus, Filter, MoreHorizontal, CheckCircle, XCircle, ShieldAlert
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { ConnectionsPanel, DiscordSetupWizard, DiscordConfig } from './connections';

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
    automations?: Array<{ id: string; name: string; description: string; frequency: string; time: string; enabled: boolean }>;
}

const CATEGORIES = [
    { id: 'general', label: 'General', icon: Globe },
    { id: 'ai', label: 'AI & Model', icon: Cpu },
    { id: 'voice', label: 'Voice & Speech', icon: Volume2 },
    { id: 'interface', label: 'Interface', icon: Monitor },
    { id: 'connections', label: 'Connections', icon: MessageSquare },
    { id: 'advanced', label: 'Advanced', icon: Zap },
    { id: 'automations', label: 'Automations', icon: Check },
    { id: 'local_network', label: 'Local Network', icon: Network },
    { id: 'about', label: 'About', icon: Globe },
];

const PROVIDERS = [
    { id: 'openai', label: 'OpenAI', defaultModel: 'gpt-4o' },
    { id: 'anthropic', label: 'Anthropic', defaultModel: 'claude-3-5-sonnet-20241022' },
    { id: 'deepseek', label: 'DeepSeek', defaultModel: 'deepseek-chat' },
    { id: 'google', label: 'Google', defaultModel: 'gemini-1.5-flash-latest' },
    { id: 'openrouter', label: 'OpenRouter', defaultModel: 'anthropic/claude-3.5-sonnet' },
];

const WAKE_WORDS = [
    { value: 'hey_jarvis', label: 'Hey Jarvis' },
    { value: 'alexa', label: 'Alexa' },
    { value: 'hey_mycroft', label: 'Hey Mycroft' },
    { value: 'hey_rhasspy', label: 'Hey Rhasspy' },
];

export default function SettingsModal({ isOpen, onClose, config, onSave, availableModels, apiModels, onFetchApiModels, onRefreshLocalModels, tools = [], workflows = [], automations = [] }: SettingsModalProps) {
    const [localConfig, setLocalConfig] = useState<any>(config || {});
    const [activeTab, setActiveTab] = useState('general');
    const [changed, setChanged] = useState(false);
    const [hfQuery, setHfQuery] = useState('');
    const [fetchingProvider, setFetchingProvider] = useState<string | null>(null);
    
    // Modals State
    const [showToolsModal, setShowToolsModal] = useState(false);
    const [showWorkflowsModal, setShowWorkflowsModal] = useState(false);
    const [showNetworkModal, setShowNetworkModal] = useState(false);
    const [showMemoryModal, setShowMemoryModal] = useState(false);
    const [showDiscordWizard, setShowDiscordWizard] = useState(false);

    const [toolsSearch, setToolsSearch] = useState('');
    const [workflowsSearch, setWorkflowsSearch] = useState('');
    const [codeModal, setCodeModal] = useState<{name: string, code: string} | null>(null);
    
    // Memory System State
    const [memoryStats, setMemoryStats] = useState<{ memories: number; chunks: number; connections: number; db_connected: boolean } | null>(null);
    const [memoryNodes, setMemoryNodes] = useState<any[]>([]);
    const [memoryEdges, setMemoryEdges] = useState<any[]>([]);
    const [memoryLoading, setMemoryLoading] = useState(false);
    
    // Workflow Visualization State
    const [workflowModal, setWorkflowModal] = useState<any>(null);
    const [selectedUser, setSelectedUser] = useState<any>(null);
    const [nodes, setNodes, onNodesChange] = useNodesState([]);
    const [edges, setEdges, onEdgesChange] = useEdgesState([]);
    
    // Network Topology State
    const [networkNodes, setNetworkNodes, onNetworkNodesChange] = useNodesState([
        { 
            id: 'server', 
            type: 'input', 
            data: { 
                label: (
                    <div className="flex flex-col items-center">
                        <div className="w-12 h-12 bg-blue-600 rounded-xl flex items-center justify-center mb-2 shadow-lg shadow-blue-200">
                            <Server size={24} className="text-white" />
                        </div>
                        <div className="font-bold text-gray-900">VAF Host</div>
                        <div className="text-[10px] text-gray-500 font-mono">192.168.1.100</div>
                    </div>
                ) 
            }, 
            position: { x: 300, y: 150 },
            style: { border: 'none', background: 'transparent' }
        },
        { 
            id: 'dev1', 
            data: { 
                label: (
                    <div className="flex items-center gap-3 p-2 bg-white rounded-lg border-2 border-green-100 shadow-sm min-w-[180px]">
                        <div className="w-8 h-8 rounded bg-green-100 text-green-600 flex items-center justify-center">
                            <Monitor size={16} />
                        </div>
                        <div className="text-left">
                            <div className="font-bold text-xs text-gray-800">admin@Main-Station</div>
                            <div className="text-[10px] text-green-600 font-medium">● Online</div>
                        </div>
                    </div>
                ) 
            }, 
            position: { x: 50, y: 50 },
            style: { border: 'none', background: 'transparent', width: 'auto' }
        },
        { 
            id: 'dev2', 
            data: { 
                label: (
                    <div className="flex items-center gap-3 p-2 bg-white rounded-lg border-2 border-green-100 shadow-sm min-w-[180px]">
                        <div className="w-8 h-8 rounded bg-purple-100 text-purple-600 flex items-center justify-center">
                            <Laptop size={16} />
                        </div>
                        <div className="text-left">
                            <div className="font-bold text-xs text-gray-800">mert@MacBook-Pro</div>
                            <div className="text-[10px] text-green-600 font-medium">● Online</div>
                        </div>
                    </div>
                ) 
            }, 
            position: { x: 550, y: 50 },
            style: { border: 'none', background: 'transparent', width: 'auto' }
        },
        { 
            id: 'dev3', 
            data: { 
                label: (
                    <div className="flex items-center gap-3 p-2 bg-white rounded-lg border-2 border-green-100 shadow-sm min-w-[180px]">
                        <div className="w-8 h-8 rounded bg-pink-100 text-pink-600 flex items-center justify-center">
                            <Smartphone size={16} />
                        </div>
                        <div className="text-left">
                            <div className="font-bold text-xs text-gray-800">guest@iPhone-15</div>
                            <div className="text-[10px] text-green-600 font-medium">● Online</div>
                        </div>
                    </div>
                ) 
            }, 
            position: { x: 100, y: 300 },
            style: { border: 'none', background: 'transparent', width: 'auto' }
        },
        { 
            id: 'dev4', 
            data: { 
                label: (
                    <div className="flex items-center gap-3 p-2 bg-gray-50 rounded-lg border-2 border-gray-100 opacity-70 min-w-[180px]">
                        <div className="w-8 h-8 rounded bg-gray-200 text-gray-500 flex items-center justify-center">
                            <Smartphone size={16} />
                        </div>
                        <div className="text-left">
                            <div className="font-bold text-xs text-gray-600">ipad@Lounge</div>
                            <div className="text-[10px] text-gray-400 font-medium">Last seen: 2d ago</div>
                        </div>
                    </div>
                ) 
            }, 
            position: { x: 500, y: 300 },
            style: { border: 'none', background: 'transparent', width: 'auto' }
        },
    ]);
    
    const [networkEdges, setNetworkEdges, onNetworkEdgesChange] = useEdgesState([
        { id: 'e1', source: 'server', target: 'dev1', animated: true, style: { stroke: '#22c55e' } },
        { id: 'e2', source: 'server', target: 'dev2', animated: true, style: { stroke: '#a855f7' } },
        { id: 'e3', source: 'server', target: 'dev3', animated: true, style: { stroke: '#ec4899' } },
        { id: 'e4', source: 'server', target: 'dev4', animated: false, style: { stroke: '#9ca3af', strokeDasharray: '5,5' } },
    ]);

    // User Management State
    const [users, setUsers] = useState([
        { id: 1, username: 'john.doe', email: 'john@local', role: 'User', lastActive: '2min ago', status: 'active', tools: ['web_search'], workflows: [], access: 'read' },
        { id: 2, username: 'admin', email: 'admin@local', role: 'Admin', lastActive: '1h ago', status: 'active', tools: ['all'], workflows: ['all'], access: 'write' },
        { id: 3, username: 'guest', email: 'guest@local', role: 'Guest', lastActive: '3 days ago', status: 'inactive', tools: [], workflows: [], access: 'restricted' },
    ]);
    const [userSearch, setUserSearch] = useState('');
    const [showAddUserModal, setShowAddUserModal] = useState(false);
    const [editingUser, setEditingUser] = useState<any>(null);
    const [newUser, setNewUser] = useState({ username: '', email: '', role: 'User', password: '', tools: [], createDb: true });

    useEffect(() => {
        setLocalConfig(config || {});
        setChanged(false);
    }, [config, isOpen]);

    // Reset fetching state when apiModels update
    useEffect(() => {
        setFetchingProvider(null);
    }, [apiModels]);

    // Fetch memory stats when modal opens
    useEffect(() => {
        if (isOpen && localConfig.memory_enabled) {
            fetch('http://localhost:8001/api/memory/stats')
                .then(res => res.json())
                .then(data => setMemoryStats(data))
                .catch(() => setMemoryStats(null));
        }
    }, [isOpen, localConfig.memory_enabled]);

    // Fetch memory graph when memory modal opens
    const fetchMemoryGraph = useCallback(async () => {
        setMemoryLoading(true);
        try {
            const res = await fetch('http://localhost:8001/api/memory/graph?limit=100');
            const data = await res.json();
            setMemoryNodes(data.nodes || []);
            setMemoryEdges(data.edges || []);
        } catch (e) {
            console.error('Failed to fetch memory graph:', e);
        }
        setMemoryLoading(false);
    }, []);

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
    }, [isOpen, codeModal, workflowModal, showMemoryModal, showToolsModal, showWorkflowsModal, onClose]);

    if (!isOpen) return null;

    const handleChange = (key: string, value: any) => {
        setLocalConfig((prev: any) => ({ ...prev, [key]: value }));
        setChanged(true);
    };

    const handleDiscordComplete = (discordConfig: DiscordConfig) => {
        handleChange('discord_config', discordConfig);
        setShowDiscordWizard(false);
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
        onSave(localConfig);
        onClose();
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
                        <h2 className="text-sm font-bold text-gray-400 uppercase tracking-wider">Settings</h2>
                    </div>

                    {CATEGORIES.map(cat => (
                        <button
                            key={cat.id}
                            onClick={() => setActiveTab(cat.id)}
                            className={cn(
                                "flex items-center gap-3 px-3 py-2 text-sm font-medium rounded-lg transition-all",
                                activeTab === cat.id
                                    ? "bg-blue-500 text-white shadow-md"
                                    : "text-gray-600 hover:bg-gray-200/50"
                            )}
                        >
                            <cat.icon size={18} />
                            {cat.label}
                        </button>
                    ))}
                </div>

                {/* Content Area */}
                <div className="flex-1 flex flex-col bg-white">
                    {/* Header */}
                    <div className="h-16 border-b border-gray-100 flex items-center justify-between px-8 shrink-0">
                        <h1 className="text-xl font-bold text-gray-800">
                            {CATEGORIES.find(c => c.id === activeTab)?.label}
                        </h1>
                        <button onClick={onClose} className="p-2 -mr-2 text-gray-400 hover:text-gray-600 rounded-full hover:bg-gray-100 transition-colors">
                            <X size={20} />
                        </button>
                    </div>

                    {/* Scrollable Form */}
                    <div className="flex-1 overflow-y-auto p-8 space-y-8">

                        {activeTab === 'general' && (
                            <div className="space-y-6">
                                <Section title="API Keys">
                                    <Input
                                        label="OpenAI Key"
                                        value={localConfig.api_key_openai || ''}
                                        onChange={(v: string) => handleChange('api_key_openai', v)}
                                        type="password" placeholder="sk-..."
                                    />
                                    <Input
                                        label="Anthropic Key"
                                        value={localConfig.api_key_anthropic || ''}
                                        onChange={(v: string) => handleChange('api_key_anthropic', v)}
                                        type="password" placeholder="sk-ant-..."
                                    />
                                    <Input
                                        label="DeepSeek Key"
                                        value={localConfig.api_key_deepseek || ''}
                                        onChange={(v: string) => handleChange('api_key_deepseek', v)}
                                        type="password"
                                    />
                                    <Input
                                        label="Google Key"
                                        value={localConfig.api_key_google || ''}
                                        onChange={(v: string) => handleChange('api_key_google', v)}
                                        type="password"
                                    />
                                    <Input
                                        label="OpenRouter Key"
                                        value={localConfig.api_key_openrouter || ''}
                                        onChange={(v: string) => handleChange('api_key_openrouter', v)}
                                        type="password"
                                    />
                                </Section>
                            </div>
                        )}

                        {activeTab === 'ai' && (
                            <div className="space-y-6">
                                <Section title="Provider">
                                    <Select
                                        label="Primary AI Provider"
                                        value={localConfig.provider || 'local'}
                                        onChange={(v: string) => handleChange('provider', v)}
                                        options={[
                                            { value: 'local', label: 'Local (llama.cpp)' },
                                            ...PROVIDERS.map(p => ({ value: p.id, label: p.label }))
                                        ]}
                                    />
                                </Section>

                                {(!localConfig.provider || localConfig.provider === 'local') && (
                                    <Section title="Local Model Settings">
                                        <div className="flex gap-2 items-end">
                                            <div className="flex-1">
                                                <Select
                                                    label="Local Model File"
                                                    value={localConfig.model || ''}
                                                    onChange={(v: string) => handleChange('model', v)}
                                                    options={[
                                                        { value: '', label: 'Select a model...' },
                                                        ...availableModels.map(m => ({ value: m, label: m }))
                                                    ]}
                                                />
                                            </div>
                                            <button
                                                onClick={onRefreshLocalModels}
                                                className="px-3 bg-gray-100 text-gray-600 hover:bg-gray-200 rounded-lg transition-colors h-10 flex items-center justify-center"
                                                title="Refresh local models"
                                            >
                                                <RefreshCw size={18} />
                                            </button>
                                        </div>
                                        <p className="text-xs text-gray-400 mt-1 mb-4">Models must be placed in the <code>/models</code> directory.</p>

                                        <div className="grid grid-cols-2 gap-4 mt-4">
                                            <Input
                                                label="Context Window (n_ctx)"
                                                value={localConfig.n_ctx || 8192}
                                                onChange={(v: string) => handleChange('n_ctx', parseInt(v))}
                                                type="number"
                                            />
                                            <Input
                                                label="GPU Layers"
                                                value={localConfig.gpu_layers ?? -1}
                                                onChange={(v: string) => handleChange('gpu_layers', parseInt(v))}
                                                type="number"
                                            />
                                        </div>
                                    </Section>
                                )}

                                {PROVIDERS.map(p => {
                                    if (localConfig.provider !== p.id) return null;
                                    const hasKey = !!localConfig[`api_key_${p.id}`];
                                    
                                    return (
                                        <Section key={p.id} title={`${p.label} Settings`}>
                                            {!hasKey && (
                                                <div className="p-3 bg-yellow-50 text-yellow-700 text-sm rounded-lg mb-4 flex items-center gap-2">
                                                    <Shield size={16} />
                                                    <span>Please set the API Key in the <strong>General</strong> tab first.</span>
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
                                                    title="Fetch available models"
                                                    disabled={!hasKey}
                                                >
                                                    <RefreshCw size={18} className={cn(fetchingProvider === p.id && "animate-spin")} />
                                                </button>
                                            </div>
                                        </Section>
                                    );
                                })}

                                <div className="mt-4 p-4 bg-gray-50 rounded-lg border border-gray-100">
                                    <label className="text-sm font-medium text-gray-700 mb-1 block">Temperature ({localConfig.temperature || 0.7})</label>
                                    <input
                                        type="range" min="0" max="2" step="0.1"
                                        value={localConfig.temperature || 0.7}
                                        onChange={(e) => handleChange('temperature', parseFloat(e.target.value))}
                                        className="w-full accent-blue-500"
                                    />
                                    <p className="text-xs text-gray-400 mt-1">Controls creativity (0 = strict, 1 = creative).</p>
                                </div>
                            </div>
                        )}

                        {activeTab === 'voice' && (
                            <div className="space-y-6">
                                <Section title="Speech to Text">
                                    <Switch
                                        label="Enable Voice Input (STT)"
                                        checked={localConfig.stt_enabled || false}
                                        onChange={(v: boolean) => handleChange('stt_enabled', v)}
                                    />
                                    <p className="text-xs text-gray-500 mt-2">Requires faster-whisper and ffmpeg installed locally.</p>
                                </Section>

                                <Section title="Wake Word">
                                    <Switch
                                        label="Enable Wake Word"
                                        checked={localConfig.stt_wake_word_enabled || false}
                                        onChange={(v: boolean) => handleChange('stt_wake_word_enabled', v)}
                                    />
                                    <div className="mt-4">
                                        <Select
                                            label="Wake Word Pattern"
                                            value={localConfig.stt_wake_word || 'hey_jarvis'}
                                            onChange={(v: string) => handleChange('stt_wake_word', v)}
                                            options={WAKE_WORDS}
                                        />
                                    </div>
                                </Section>

                                <Section title="Text to Speech">
                                    <Switch
                                        label="Enable Voice Output (TTS)"
                                        description="Agent speaks responses aloud"
                                        checked={localConfig.speech_tts_enabled || false}
                                        onChange={(v: boolean) => handleChange('speech_tts_enabled', v)}
                                    />
                                    {localConfig.speech_tts_enabled && (
                                        <div className="mt-4">
                                            <Select
                                                label="TTS Engine"
                                                value={localConfig.speech_tts_engine || 'piper'}
                                                onChange={(v: string) => handleChange('speech_tts_engine', v)}
                                                options={[
                                                    { value: 'piper', label: 'Piper (Neural, High Quality)' },
                                                    { value: 'system', label: 'System (Native OS Voice)' },
                                                ]}
                                            />
                                            <p className="text-xs text-gray-500 mt-2">
                                                Piper provides natural-sounding neural voices. System uses your OS's built-in TTS.
                                            </p>
                                        </div>
                                    )}
                                </Section>
                            </div>
                        )}

                        {activeTab === 'interface' && (
                            <div className="space-y-6">
                                <Section title="Automation">
                                    <Switch
                                        label="Auto-open Links"
                                        description="Automatically open search result links in browser tabs"
                                        checked={localConfig.ux_auto_open_links ?? true}
                                        onChange={(v: boolean) => handleChange('ux_auto_open_links', v)}
                                    />
                                    <div className="h-4" />
                                    <Switch
                                        label="Auto-open Outputs"
                                        description="Open generated files/folders automatically"
                                        checked={localConfig.ux_auto_open_outputs ?? true}
                                        onChange={(v: boolean) => handleChange('ux_auto_open_outputs', v)}
                                    />
                                    {localConfig.ux_auto_open_outputs && (
                                        <div className="mt-2 pl-4 border-l-2 border-gray-100 animate-in slide-in-from-top-1 fade-in">
                                            <Input
                                                label="Max Limit (Items)"
                                                value={localConfig.ux_auto_open_max || 20}
                                                onChange={(v: string) => handleChange('ux_auto_open_max', parseInt(v))}
                                                type="number"
                                            />
                                        </div>
                                    )}
                                    <div className="h-4" />
                                    <Switch
                                        label="Separate Terminals (Global)"
                                        description="Applies to CLI/workflows. WebUI still runs sub-agents headless and streams output."
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
                                onOpenDiscordWizard={() => setShowDiscordWizard(true)}
                            />
                        )}

                        {activeTab === 'local_network' && (
                            <div className="space-y-6 animate-in fade-in slide-in-from-right-4 duration-300">
                                <Section title="Network Settings">
                                     <Switch
                                        label="Enable Local Network Hosting"
                                        description="Allow other devices on the network to access this agent"
                                        checked={localConfig.local_network_enabled || false}
                                        onChange={(v: boolean) => handleChange('local_network_enabled', v)}
                                    />
                                </Section>
                                
                                <div className={cn("space-y-6 transition-all duration-300", !localConfig.local_network_enabled && "opacity-50 pointer-events-none grayscale-[0.5]")}>
                                    <Section title="User Management">
                                        <div className="flex flex-col gap-4">
                                            {/* Toolbar */}
                                            <div className="flex items-center justify-between">
                                                <div className="relative max-w-xs w-full">
                                                    <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
                                                    <input 
                                                        type="text" 
                                                        placeholder="Search users..." 
                                                        value={userSearch}
                                                        onChange={(e) => setUserSearch(e.target.value)}
                                                        className="w-full pl-9 pr-4 py-2 bg-white border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 transition-all"
                                                    />
                                                </div>
                                                <button 
                                                    onClick={() => setShowAddUserModal(true)}
                                                    className="px-4 py-2 bg-green-500 hover:bg-green-600 text-white font-medium rounded-lg text-sm shadow-sm hover:shadow transition-all flex items-center gap-2"
                                                >
                                                    <Plus size={16} /> Add New User
                                                </button>
                                            </div>

                                            {/* Table */}
                                            <div className="bg-white border border-gray-200 rounded-lg overflow-hidden shadow-sm">
                                                <table className="w-full text-sm text-left">
                                                    <thead className="bg-gray-50 text-gray-500 font-medium border-b border-gray-200">
                                                        <tr>
                                                            <th className="px-4 py-3 font-semibold">Username</th>
                                                            <th className="px-4 py-3 font-semibold">Role</th>
                                                            <th className="px-4 py-3 font-semibold">Last Active</th>
                                                            <th className="px-4 py-3 font-semibold">Status</th>
                                                            <th className="px-4 py-3 font-semibold text-right">Actions</th>
                                                        </tr>
                                                    </thead>
                                                    <tbody className="divide-y divide-gray-100">
                                                        {users.filter(u => 
                                                            u.username.toLowerCase().includes(userSearch.toLowerCase()) || 
                                                            u.role.toLowerCase().includes(userSearch.toLowerCase())
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
                                                                        {user.status === 'active' ? 'Active' : 'Inactive'}
                                                                    </span>
                                                                </td>
                                                                <td className="px-4 py-3 text-right">
                                                                    <div className="flex items-center justify-end gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                                                                        <button 
                                                                            onClick={() => setEditingUser(user)}
                                                                            className="p-1.5 text-gray-500 hover:text-blue-600 hover:bg-blue-50 rounded-lg transition-colors"
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
                                                {users.filter(u => u.username.toLowerCase().includes(userSearch.toLowerCase())).length === 0 && (
                                                    <div className="p-8 text-center text-gray-500">
                                                        No users found matching "{userSearch}"
                                                    </div>
                                                )}
                                            </div>
                                        </div>
                                    </Section>

                                    <Section title="Access Configuration">
                                        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                                            <Input
                                                label="Host IP Address"
                                                value="192.168.1.100"
                                                onChange={() => {}}
                                                placeholder="0.0.0.0"
                                            />
                                            <Input
                                                label="Port"
                                                value="3000"
                                                onChange={() => {}}
                                                placeholder="3000"
                                            />
                                        </div>
                                        <div className="mt-4">
                                            <Select
                                                label="Security Level"
                                                value="standard"
                                                onChange={() => {}}
                                                options={[
                                                    { value: 'strict', label: 'Strict (Allowlist Only)' },
                                                    { value: 'standard', label: 'Standard (Password)' },
                                                    { value: 'open', label: 'Open (No Auth)' },
                                                ]}
                                            />
                                        </div>
                                    </Section>

                                    <Section title="Server Info">
                                        <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
                                            <div className="p-4 bg-white border border-gray-200 rounded-lg flex flex-col gap-1 shadow-sm">
                                                <span className="text-xs text-gray-500 uppercase tracking-wide font-semibold">Status</span>
                                                <div className="flex items-center gap-2 text-green-600 font-medium">
                                                    <div className="w-2 h-2 rounded-full bg-green-500 animate-pulse" />
                                                    Running
                                                </div>
                                            </div>
                                            <div className="p-4 bg-white border border-gray-200 rounded-lg flex flex-col gap-1 shadow-sm">
                                                <span className="text-xs text-gray-500 uppercase tracking-wide font-semibold">Container</span>
                                                <span className="text-gray-900 font-mono text-sm">vaf-web-1</span>
                                            </div>
                                            <div className="p-4 bg-white border border-gray-200 rounded-lg flex flex-col gap-1 shadow-sm">
                                                <span className="text-xs text-gray-500 uppercase tracking-wide font-semibold">Uptime</span>
                                                <span className="text-gray-900 font-mono text-sm">2d 4h 12m</span>
                                            </div>
                                        </div>

                                        <div className="space-y-3">
                                            <h4 className="text-sm font-medium text-gray-700 flex items-center gap-2">
                                                <Network size={16} /> Network Topology
                                            </h4>
                                            
                                            <button 
                                                onClick={() => setShowNetworkModal(true)}
                                                className="w-full h-32 bg-gray-50 hover:bg-gray-100 border border-gray-200 hover:border-blue-300 rounded-xl transition-all flex flex-col items-center justify-center gap-3 group"
                                            >
                                                <div className="w-12 h-12 rounded-full bg-white border border-gray-200 flex items-center justify-center shadow-sm group-hover:scale-110 transition-transform">
                                                    <Network size={24} className="text-blue-500" />
                                                </div>
                                                <div className="text-center">
                                                    <div className="font-medium text-gray-900">Open Network Map</div>
                                                    <div className="text-xs text-gray-500 mt-1">View active devices and connections</div>
                                                </div>
                                            </button>
                                        </div>
                                    </Section>
                                </div>
                            </div>
                        )}

                        {activeTab === 'advanced' && (
                            <div className="space-y-6">
                                <Section title="Sub-Agents">
                                    <Switch
                                        label="Separate Terminals (Global)"
                                        description="Applies to CLI/workflows. WebUI still runs sub-agents headless and streams output."
                                        checked={localConfig.sub_agents_in_separate_terminals ?? true}
                                        onChange={(v: boolean) => handleChange('sub_agents_in_separate_terminals', v)}
                                    />
                                    <div className="mt-4">
                                        <Select
                                            label="Sub-Agent Provider"
                                            value={localConfig.subagent_provider || 'inherit'}
                                            onChange={(v: string) => handleChange('subagent_provider', v)}
                                            options={[
                                                { value: 'inherit', label: 'Same as Main Agent' },
                                                { value: 'openai', label: 'OpenAI' },
                                                { value: 'anthropic', label: 'Anthropic' },
                                                { value: 'deepseek', label: 'DeepSeek' },
                                                { value: 'local', label: 'Local' },
                                            ]}
                                        />
                                    </div>
                                    <div className="h-4" />
                                    <Switch
                                        label="Sub-Agent Timeout"
                                        description="Limit execution time for sub-agents"
                                        checked={localConfig.subagent_timeout_enabled ?? true}
                                        onChange={(v: boolean) => handleChange('subagent_timeout_enabled', v)}
                                    />
                                    {localConfig.subagent_timeout_enabled && (
                                        <div className="mt-2 pl-4 border-l-2 border-gray-100">
                                            <Input
                                                label="Timeout (minutes)"
                                                value={localConfig.subagent_timeout_minutes || 120}
                                                onChange={(v: string) => handleChange('subagent_timeout_minutes', parseInt(v))}
                                                type="number"
                                            />
                                        </div>
                                    )}
                                </Section>

                                <Section title="System">
                                    <Switch
                                        label="Web UI Dashboard"
                                        description="Start Web UI automatically on launch"
                                        checked={localConfig.web_ui_enabled ?? true}
                                        onChange={(v: boolean) => handleChange('web_ui_enabled', v)}
                                    />
                                    <div className="h-4" />
                                    <Switch
                                        label="Start Tray on Login"
                                        description="Auto-start the tray app when your OS logs in"
                                        checked={localConfig.tray_autostart ?? false}
                                        onChange={(v: boolean) => handleChange('tray_autostart', v)}
                                    />
                                    <div className="h-4" />
                                    <Switch
                                        label="Server Persistence"
                                        description="Keep server running in background after exit"
                                        checked={localConfig.server_persistence_enabled ?? false}
                                        onChange={(v: boolean) => handleChange('server_persistence_enabled', v)}
                                    />
                                    <div className="h-4" />
                                    <Switch
                                        label="Memory System"
                                        description="Store and retrieve memories with AI-powered RAG"
                                        checked={localConfig.memory_enabled ?? true}
                                        onChange={(v: boolean) => handleChange('memory_enabled', v)}
                                    />
                                    <div className="h-4" />
                                    <button
                                        onClick={() => setShowToolsModal(true)}
                                        className="w-full flex items-center justify-between p-3 bg-gray-50 hover:bg-gray-100 rounded-lg border border-gray-100 transition-colors"
                                    >
                                        <div className="flex flex-col items-start">
                                            <span className="text-sm font-medium text-gray-700">Tools</span>
                                            <span className="text-xs text-gray-500">{tools.length} tools loaded</span>
                                        </div>
                                        <ChevronRight size={16} className="text-gray-400" />
                                    </button>
                                    <div className="h-4" />
                                    <button
                                        onClick={() => setShowWorkflowsModal(true)}
                                        className="w-full flex items-center justify-between p-3 bg-gray-50 hover:bg-gray-100 rounded-lg border border-gray-100 transition-colors"
                                    >
                                        <div className="flex flex-col items-start">
                                            <span className="text-sm font-medium text-gray-700">Workflows</span>
                                            <span className="text-xs text-gray-500">{workflows.length} workflows available</span>
                                        </div>
                                        <ChevronRight size={16} className="text-gray-400" />
                                    </button>
                                    <div className="h-4" />
                                    <button
                                        onClick={() => setShowMemoryModal(true)}
                                        disabled={!localConfig.memory_enabled}
                                        className={cn(
                                            "w-full flex items-center justify-between p-3 rounded-lg border transition-colors",
                                            localConfig.memory_enabled 
                                                ? "bg-indigo-50 hover:bg-indigo-100 border-indigo-200"
                                                : "bg-gray-50 border-gray-100 opacity-50 cursor-not-allowed"
                                        )}
                                    >
                                        <div className="flex items-center gap-3">
                                            <Brain size={20} className={localConfig.memory_enabled ? "text-indigo-600" : "text-gray-400"} />
                                            <div className="flex flex-col items-start">
                                                <span className={cn("text-sm font-medium", localConfig.memory_enabled ? "text-indigo-700" : "text-gray-600")}>
                                                    Memory System
                                                </span>
                                                <span className={cn("text-xs", localConfig.memory_enabled ? "text-indigo-500" : "text-gray-400")}>
                                                    {memoryStats?.memories ?? 0} memories stored
                                                    {memoryStats?.db_connected === false && " (disconnected)"}
                                                </span>
                                            </div>
                                        </div>
                                        <ChevronRight size={16} className={localConfig.memory_enabled ? "text-indigo-400" : "text-gray-300"} />
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
                                            <h3 className="text-lg font-medium text-gray-900">No Automations Yet</h3>
                                            <p className="text-sm text-gray-500 max-w-xs mx-auto mt-1">
                                                Create custom workflows and scheduled tasks to automate your daily work.
                                            </p>
                                        </div>
                                        <button className="px-4 py-2 bg-blue-50 text-blue-600 font-medium rounded-lg text-sm hover:bg-blue-100 transition-colors">
                                            Create New Automation
                                        </button>
                                    </div>
                                ) : (
                                    <Section title="Scheduled Automations">
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
                                                                    {auto.enabled ? "Active" : "Disabled"}
                                                                </div>
                                                            </div>
                                                            <div className="text-sm text-gray-600 mt-1">{auto.description}</div>
                                                            <div className="flex items-center gap-4 mt-2 text-xs text-gray-500">
                                                                <div className="flex items-center gap-1">
                                                                    <span className="font-medium">Frequency:</span>
                                                                    <span>{auto.frequency}</span>
                                                                </div>
                                                                <div className="flex items-center gap-1">
                                                                    <span className="font-medium">Time:</span>
                                                                    <span>{auto.time}</span>
                                                                </div>
                                                            </div>
                                                        </div>
                                                    </div>
                                                </div>
                                            ))}
                                        </div>
                                        <div className="mt-4">
                                            <button className="w-full px-4 py-2 bg-blue-50 text-blue-600 font-medium rounded-lg text-sm hover:bg-blue-100 transition-colors">
                                                Create New Automation
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

                                <Section title="Credits">
                                    <div className="space-y-3 text-sm text-gray-600">
                                        <div className="flex justify-between">
                                            <span>Core Engine</span>
                                            <span className="font-medium">Python 3.11 + Llama.cpp</span>
                                        </div>
                                        <div className="flex justify-between">
                                            <span>Frontend</span>
                                            <span className="font-medium">Next.js + Tailwind</span>
                                        </div>
                                        <div className="flex justify-between">
                                            <span>Developed by</span>
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
                            Cancel
                        </button>
                        <button
                            onClick={handleSave}
                            disabled={!changed}
                            className="px-8 py-2.5 rounded-xl font-medium bg-gray-900 text-white hover:bg-black shadow-lg shadow-gray-200 disabled:opacity-50 disabled:cursor-not-allowed transition-all flex items-center gap-2"
                        >
                            <Save size={18} />
                            Save Changes
                        </button>
                    </div>
                </div>
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
                                <h2 className="text-2xl font-bold text-gray-800">Available Tools</h2>
                                <p className="text-sm text-gray-500">{tools.length} modules installed</p>
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
                                    placeholder="Search tools by name or description..."
                                    value={toolsSearch}
                                    onChange={(e) => setToolsSearch(e.target.value)}
                                    className="w-full pl-12 pr-4 h-12 bg-white border border-gray-200 rounded-xl text-base shadow-sm focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 transition-all"
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
                                                        View Code <ChevronRight size={12} />
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
                                    <h3 className="text-lg font-medium text-gray-900">No tools found</h3>
                                    <p className="text-sm text-gray-500 mt-1">Try adjusting your search terms.</p>
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
                                <h2 className="text-2xl font-bold text-gray-800">Available Workflows</h2>
                                <p className="text-sm text-gray-500">{workflows.length} templates available</p>
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
                                    placeholder="Search workflows..."
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
                                                        {wf.steps} Steps
                                                    </span>
                                                </div>
                                                
                                                <h3 className="text-lg font-bold text-gray-900 mb-1 group-hover:text-purple-600 transition-colors line-clamp-1">{wf.name}</h3>
                                                
                                                <div className="flex-1">
                                                    <p className="text-xs text-gray-500 line-clamp-4 leading-relaxed">
                                                        {wf.description}
                                                    </p>
                                                </div>

                                                <div className="mt-4 pt-3 border-t border-gray-100 flex items-center justify-between text-xs text-gray-400 group-hover:text-gray-600">
                                                    <span className="font-mono">Template</span>
                                                    <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity text-purple-600 font-medium">
                                                        Details <ChevronRight size={12} />
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
                                    <h3 className="text-lg font-medium text-gray-900">No workflows found</h3>
                                    <p className="text-sm text-gray-500 mt-1">Try adjusting your search terms.</p>
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
                                    <p className="text-xs text-gray-500">Interactive Flow Visualization</p>
                                </div>
                            </div>
                            <button onClick={() => setWorkflowModal(null)} className="p-2 text-gray-400 hover:text-gray-600 rounded-full hover:bg-gray-100 transition-colors">
                                <X size={20} />
                            </button>
                        </div>
                        
                        {/* Content Split View */}
                        <div className="flex-1 flex overflow-hidden">
                            {/* Left: ReactFlow Canvas */}
                            <div className="flex-1 bg-gray-50 relative border-r border-gray-200">
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
                            </div>

                            {/* Right: Code Viewer (Fixed 30%) */}
                            <div className="w-[30%] shrink-0 bg-[#1e1e1e] flex flex-col border-l border-gray-800">
                                <div className="h-10 border-b border-gray-700 flex items-center px-4 bg-[#252526] shrink-0">
                                    <span className="text-xs font-mono font-medium text-gray-400 uppercase tracking-wide">Step Definition</span>
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
                        {/* Header */}
                        <div className="h-20 border-b border-gray-100 flex items-center justify-between px-8 shrink-0 bg-gradient-to-r from-indigo-50 to-purple-50 z-10">
                            <div className="flex items-center gap-4">
                                <div className="w-12 h-12 rounded-xl bg-indigo-600 flex items-center justify-center shadow-lg">
                                    <Brain size={24} className="text-white" />
                                </div>
                                <div>
                                    <h2 className="text-2xl font-bold text-gray-800">Memory Graph</h2>
                                    <p className="text-sm text-gray-500">
                                        {memoryStats?.memories ?? 0} memories • {memoryStats?.chunks ?? 0} chunks • {memoryStats?.connections ?? 0} connections
                                    </p>
                                </div>
                            </div>
                            <div className="flex items-center gap-3">
                                <button 
                                    onClick={fetchMemoryGraph}
                                    disabled={memoryLoading}
                                    className="flex items-center gap-2 px-4 py-2 bg-white border border-gray-200 rounded-lg hover:bg-gray-50 transition-colors text-sm font-medium text-gray-700"
                                >
                                    <RefreshCw size={16} className={memoryLoading ? 'animate-spin' : ''} />
                                    Refresh
                                </button>
                                <a 
                                    href="/memory"
                                    target="_blank"
                                    className="flex items-center gap-2 px-4 py-2 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 transition-colors text-sm font-medium"
                                >
                                    Open Full View
                                    <ChevronRight size={16} />
                                </a>
                                <button onClick={() => setShowMemoryModal(false)} className="p-2 text-gray-400 hover:text-gray-600 rounded-full hover:bg-white transition-colors">
                                    <X size={24} />
                                </button>
                            </div>
                        </div>
                        
                        {/* Graph Content */}
                        <div className="flex-1 overflow-hidden bg-gray-50">
                            {memoryLoading ? (
                                <div className="flex items-center justify-center h-full">
                                    <div className="text-center">
                                        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-indigo-600 mx-auto mb-4" />
                                        <p className="text-gray-500">Loading memory graph...</p>
                                    </div>
                                </div>
                            ) : memoryNodes.length === 0 ? (
                                <div className="flex items-center justify-center h-full">
                                    <div className="text-center">
                                        <div className="w-20 h-20 bg-gray-100 rounded-full flex items-center justify-center mx-auto mb-4">
                                            <Brain size={40} className="text-gray-300" />
                                        </div>
                                        <h3 className="text-xl font-semibold text-gray-700 mb-2">No memories yet</h3>
                                        <p className="text-gray-500 max-w-sm">
                                            Create your first memory to see the graph visualization.
                                            Memories are auto-connected based on semantic similarity.
                                        </p>
                                        <a 
                                            href="/memory"
                                            target="_blank"
                                            className="inline-flex items-center gap-2 mt-4 px-4 py-2 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 transition-colors text-sm font-medium"
                                        >
                                            Create Memory
                                            <ChevronRight size={16} />
                                        </a>
                                    </div>
                                </div>
                            ) : (
                                <ReactFlow
                                    nodes={memoryNodes.map((node, i) => ({
                                        id: node.id,
                                        type: 'default',
                                        position: node.position || { x: (i % 5) * 280, y: Math.floor(i / 5) * 180 },
                                        data: { 
                                            label: (
                                                <div className="text-left p-1">
                                                    <div className="font-medium text-gray-800 text-sm truncate max-w-[180px]">
                                                        {node.data?.label || 'Untitled'}
                                                    </div>
                                                    {node.data?.preview && (
                                                        <div className="text-xs text-gray-500 truncate max-w-[180px] mt-0.5">
                                                            {node.data.preview}
                                                        </div>
                                                    )}
                                                    <div className="flex items-center gap-2 mt-1 text-[10px] text-gray-400">
                                                        <span>{node.data?.chunkCount || 0} chunks</span>
                                                        {node.data?.tags?.length > 0 && (
                                                            <span className="px-1.5 py-0.5 bg-gray-100 rounded">
                                                                {node.data.tags[0]}
                                                            </span>
                                                        )}
                                                    </div>
                                                </div>
                                            )
                                        },
                                        style: {
                                            background: node.data?.isHighlighted ? '#fef3c7' : 'white',
                                            border: node.data?.isHighlighted ? '2px solid #f59e0b' : '1px solid #e5e7eb',
                                            borderRadius: '12px',
                                            padding: '8px',
                                            minWidth: '200px',
                                        }
                                    }))}
                                    edges={memoryEdges.map(edge => ({
                                        id: edge.id,
                                        source: edge.source,
                                        target: edge.target,
                                        type: 'smoothstep',
                                        animated: edge.data?.connectionType === 'semantic',
                                        style: {
                                            stroke: edge.data?.connectionType === 'semantic' ? '#6366f1' : '#9ca3af',
                                            strokeWidth: Math.max(1, (edge.data?.strength || 0.5) * 3),
                                            opacity: 0.6,
                                        },
                                        markerEnd: { type: MarkerType.ArrowClosed, width: 12, height: 12 },
                                    }))}
                                    fitView
                                    fitViewOptions={{ padding: 0.3 }}
                                    proOptions={{ hideAttribution: true }}
                                    defaultEdgeOptions={{ type: 'smoothstep' }}
                                >
                                    <Background color="#e5e7eb" gap={24} />
                                    <Controls className="bg-white rounded-lg shadow-lg" />
                                    <MiniMap 
                                        nodeColor={(node) => node.style?.background === '#fef3c7' ? '#f59e0b' : '#6366f1'}
                                        maskColor="rgba(0, 0, 0, 0.1)"
                                        className="bg-white rounded-lg shadow-lg"
                                    />
                                </ReactFlow>
                            )}
                        </div>

                        {/* Footer Stats */}
                        <div className="h-14 border-t border-gray-200 flex items-center justify-between px-8 bg-white">
                            <div className="flex items-center gap-6 text-sm text-gray-500">
                                <div className="flex items-center gap-2">
                                    <div className="w-3 h-3 rounded-full bg-indigo-500" />
                                    <span>Semantic Connection</span>
                                </div>
                                <div className="flex items-center gap-2">
                                    <div className="w-3 h-3 rounded-full bg-gray-400" />
                                    <span>Manual Connection</span>
                                </div>
                                <div className="flex items-center gap-2">
                                    <div className="w-3 h-3 rounded bg-yellow-200 border border-yellow-400" />
                                    <span>Highlighted (RAG Source)</span>
                                </div>
                            </div>
                            <div className="flex items-center gap-2">
                                {memoryStats?.db_connected ? (
                                    <span className="flex items-center gap-1.5 text-xs text-green-600">
                                        <div className="w-2 h-2 rounded-full bg-green-500" />
                                        Connected
                                    </span>
                                ) : (
                                    <span className="flex items-center gap-1.5 text-xs text-red-600">
                                        <div className="w-2 h-2 rounded-full bg-red-500" />
                                        Disconnected
                                    </span>
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
                        className="relative bg-white w-full max-w-lg rounded-2xl shadow-2xl border border-gray-200 flex flex-col animate-in fade-in zoom-in-95 duration-200"
                        onClick={(e) => e.stopPropagation()}
                    >
                        <div className="flex items-center justify-between p-6 border-b border-gray-100 bg-gray-50/50 rounded-t-2xl">
                            <div>
                                <h2 className="text-xl font-bold text-gray-900">Add New User</h2>
                                <p className="text-sm text-gray-500">Create a new local account</p>
                            </div>
                            <button onClick={() => setShowAddUserModal(false)} className="p-2 text-gray-400 hover:text-gray-600 rounded-lg hover:bg-gray-100 transition-colors">
                                <X size={20} />
                            </button>
                        </div>
                        
                        <div className="p-6 space-y-6">
                            <div className="space-y-4">
                                <Input 
                                    label="Username *" 
                                    value={newUser.username} 
                                    onChange={(v) => setNewUser({...newUser, username: v})} 
                                    placeholder="johndoe"
                                />
                                <Input 
                                    label="Email *" 
                                    value={newUser.email} 
                                    onChange={(v) => setNewUser({...newUser, email: v})} 
                                    placeholder="john@example.com"
                                />
                                <Select 
                                    label="Role *" 
                                    value={newUser.role} 
                                    onChange={(v) => setNewUser({...newUser, role: v})} 
                                    options={[
                                        { value: 'User', label: 'User' },
                                        { value: 'Admin', label: 'Administrator' },
                                        { value: 'Guest', label: 'Guest' }
                                    ]}
                                />
                                <Input 
                                    label="Initial Password" 
                                    value={newUser.password} 
                                    onChange={(v) => setNewUser({...newUser, password: v})} 
                                    type="password"
                                    placeholder="Auto-generated if empty"
                                />
                            </div>

                            <div className="space-y-3 pt-2 border-t border-gray-100">
                                <label className="text-sm font-medium text-gray-700">Tools & Workflows</label>
                                <div className="max-h-32 overflow-y-auto border border-gray-200 rounded-lg p-2 bg-gray-50/50 grid grid-cols-2 gap-2">
                                    {['Web Search', 'File System', 'Code Interpreter', 'Memory System', 'Data Analysis', 'Image Gen'].map(tool => (
                                        <label key={tool} className="flex items-center gap-2 p-2 rounded hover:bg-white cursor-pointer transition-colors">
                                            <input type="checkbox" className="rounded border-gray-300 text-blue-600 focus:ring-blue-500" />
                                            <span className="text-sm text-gray-600">{tool}</span>
                                        </label>
                                    ))}
                                </div>
                            </div>

                            <div className="flex items-center gap-3 p-3 bg-blue-50 border border-blue-100 rounded-lg">
                                <Database size={18} className="text-blue-600" />
                                <div className="flex-1">
                                    <div className="text-sm font-medium text-blue-900">Create Memory Database</div>
                                    <div className="text-xs text-blue-600">Dedicated vector store for this user</div>
                                </div>
                                <input type="checkbox" checked={newUser.createDb} onChange={(e) => setNewUser({...newUser, createDb: e.target.checked})} className="rounded border-blue-300 text-blue-600 focus:ring-blue-500 w-5 h-5" />
                            </div>
                        </div>

                        <div className="flex items-center justify-end gap-3 p-6 border-t border-gray-100 bg-gray-50/50 rounded-b-2xl">
                            <button onClick={() => setShowAddUserModal(false)} className="px-4 py-2 text-gray-600 hover:bg-gray-200 font-medium rounded-lg transition-colors">
                                Cancel
                            </button>
                            <button className="px-6 py-2 bg-green-500 hover:bg-green-600 text-white font-medium rounded-lg shadow-sm hover:shadow transition-all flex items-center gap-2">
                                <Plus size={18} />
                                Create User
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
                                    <h2 className="text-xl font-bold text-gray-900">Edit User</h2>
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
                                        label="Username" 
                                        value={editingUser.username} 
                                        onChange={(v) => setEditingUser({...editingUser, username: v})} 
                                    />
                                    <Select 
                                        label="Role" 
                                        value={editingUser.role} 
                                        onChange={(v) => setEditingUser({...editingUser, role: v})} 
                                        options={[
                                            { value: 'User', label: 'User' },
                                            { value: 'Admin', label: 'Administrator' },
                                            { value: 'Guest', label: 'Guest' }
                                        ]}
                                    />
                                </div>
                                <Input 
                                    label="Email" 
                                    value={editingUser.email} 
                                    onChange={(v) => setEditingUser({...editingUser, email: v})} 
                                />
                            </div>

                            <Section title="Security & Access">
                                <div className="space-y-3">
                                    <div className="flex items-center justify-between p-3 border border-gray-200 rounded-lg hover:border-gray-300 transition-colors">
                                        <div className="flex items-center gap-3">
                                            <div className="p-2 bg-yellow-50 text-yellow-600 rounded-lg">
                                                <Lock size={16} />
                                            </div>
                                            <span className="text-sm font-medium text-gray-700">Password</span>
                                        </div>
                                        <button className="text-xs font-medium text-blue-600 hover:text-blue-700 hover:underline">Reset</button>
                                    </div>
                                    
                                    <div className="flex items-center justify-between p-3 border border-gray-200 rounded-lg hover:border-gray-300 transition-colors">
                                        <div className="flex items-center gap-3">
                                            <div className="p-2 bg-purple-50 text-purple-600 rounded-lg">
                                                <Shield size={16} />
                                            </div>
                                            <span className="text-sm font-medium text-gray-700">2FA Status</span>
                                        </div>
                                        <button className="text-xs font-medium text-blue-600 hover:text-blue-700 hover:underline">Reset 2FA</button>
                                    </div>

                                    <div className="flex items-center justify-between p-3 border border-gray-200 rounded-lg hover:border-gray-300 transition-colors">
                                        <div className="flex items-center gap-3">
                                            <div className={cn("p-2 rounded-lg transition-colors", editingUser.status === 'active' ? "bg-green-50 text-green-600" : "bg-gray-100 text-gray-500")}>
                                                {editingUser.status === 'active' ? <CheckCircle size={16} /> : <XCircle size={16} />}
                                            </div>
                                            <span className="text-sm font-medium text-gray-700">Account Status</span>
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
                            <button className="px-4 py-2 text-red-600 hover:bg-red-50 font-medium rounded-lg transition-colors flex items-center gap-2">
                                <Trash2 size={16} /> Delete User
                            </button>
                            <div className="flex gap-3">
                                <button onClick={() => setEditingUser(null)} className="px-4 py-2 text-gray-600 hover:bg-gray-200 font-medium rounded-lg transition-colors">
                                    Cancel
                                </button>
                                <button onClick={() => setEditingUser(null)} className="px-6 py-2 bg-gray-900 text-white hover:bg-black font-medium rounded-lg shadow-sm hover:shadow transition-all flex items-center gap-2">
                                    <Save size={16} /> Save Changes
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
                                            {selectedUser.status === 'active' ? 'Active Account' : 'Inactive'}
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
                                    <div className="text-xs text-gray-500 uppercase tracking-wide font-semibold mb-1">Last Active</div>
                                    <div className="text-lg font-mono font-medium text-gray-900">{selectedUser.lastActive}</div>
                                </div>
                                <div className="p-4 bg-gray-50 rounded-xl border border-gray-100">
                                    <div className="text-xs text-gray-500 uppercase tracking-wide font-semibold mb-1">Access Level</div>
                                    <div className="text-lg font-medium text-gray-900 capitalize">{selectedUser.access}</div>
                                </div>
                                <div className="p-4 bg-gray-50 rounded-xl border border-gray-100">
                                    <div className="text-xs text-gray-500 uppercase tracking-wide font-semibold mb-1">Memory Usage</div>
                                    <div className="text-lg font-mono font-medium text-gray-900">24.5 MB</div>
                                </div>
                            </div>

                            {/* Permissions */}
                            <div className="space-y-6">
                                <div>
                                    <h4 className="text-sm font-bold text-gray-900 mb-3 flex items-center gap-2">
                                        <Cpu size={16} /> Authorized Tools
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
                                        <Workflow size={16} /> Active Workflows
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
                                Close
                            </button>
                            <button
                                onClick={() => {
                                    setEditingUser(selectedUser);
                                    setSelectedUser(null);
                                }}
                                className="px-6 py-2.5 rounded-xl font-medium bg-blue-600 text-white hover:bg-blue-700 shadow-lg shadow-blue-200 transition-all flex items-center gap-2"
                            >
                                <Edit size={16} /> Edit User
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
                                <div className="w-12 h-12 rounded-xl bg-blue-50 text-blue-600 flex items-center justify-center shadow-sm">
                                    <Network size={24} />
                                </div>
                                <div>
                                    <h2 className="text-2xl font-bold text-gray-800">Network Map</h2>
                                    <p className="text-sm text-gray-500">
                                        Real-time device topology • 4 Active Devices
                                    </p>
                                </div>
                            </div>
                            <div className="flex items-center gap-3">
                                <div className="px-3 py-1.5 bg-green-50 text-green-700 text-xs font-medium rounded-full border border-green-100 flex items-center gap-2">
                                    <div className="w-2 h-2 rounded-full bg-green-500 animate-pulse" />
                                    System Online
                                </div>
                                <button onClick={() => setShowNetworkModal(false)} className="p-2 text-gray-400 hover:text-gray-600 rounded-full hover:bg-gray-100 transition-colors">
                                    <X size={24} />
                                </button>
                            </div>
                        </div>
                        
                        {/* Graph Content */}
                        <div className="flex-1 overflow-hidden bg-gray-50 relative">
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

                            {/* Legend Overlay */}
                            <div className="absolute top-6 left-6 p-4 bg-white/90 backdrop-blur rounded-xl border border-gray-200 shadow-lg space-y-3 z-10">
                                <h4 className="text-xs font-bold text-gray-500 uppercase tracking-wide">Device Types</h4>
                                <div className="space-y-2">
                                    <div className="flex items-center gap-2 text-sm text-gray-600">
                                        <div className="w-6 h-6 rounded bg-blue-600 flex items-center justify-center text-white"><Server size={12} /></div>
                                        <span>VAF Host</span>
                                    </div>
                                    <div className="flex items-center gap-2 text-sm text-gray-600">
                                        <div className="w-6 h-6 rounded bg-green-100 text-green-600 flex items-center justify-center border border-green-200"><Monitor size={12} /></div>
                                        <span>Desktop</span>
                                    </div>
                                    <div className="flex items-center gap-2 text-sm text-gray-600">
                                        <div className="w-6 h-6 rounded bg-purple-100 text-purple-600 flex items-center justify-center border border-purple-200"><Laptop size={12} /></div>
                                        <span>Laptop</span>
                                    </div>
                                    <div className="flex items-center gap-2 text-sm text-gray-600">
                                        <div className="w-6 h-6 rounded bg-pink-100 text-pink-600 flex items-center justify-center border border-pink-200"><Smartphone size={12} /></div>
                                        <span>Mobile</span>
                                    </div>
                                </div>
                            </div>
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
            className="px-4 h-10 bg-white border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 transition-all placeholder:text-gray-400"
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
                    className="w-full h-10 appearance-none px-4 bg-white border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 transition-all text-gray-700 pr-10"
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
