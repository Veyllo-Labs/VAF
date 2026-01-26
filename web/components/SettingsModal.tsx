'use client';

import React, { useState, useEffect } from 'react';
import {
    X, Globe, Cpu, Volume2, Monitor, Shield, Save, RotateCcw,
    Check, ChevronRight, Zap, Search, Download, RefreshCw
} from 'lucide-react';
import { cn } from '@/lib/utils';

export interface SettingsModalProps {
    isOpen: boolean;
    onClose: () => void;
    config: any;
    onSave: (newConfig: any) => void;
    availableModels: string[];
    apiModels: Record<string, string[]>;
    onFetchApiModels: (provider: string, apiKey: string) => void;
    onRefreshLocalModels: () => void;
}

const CATEGORIES = [
    { id: 'general', label: 'General', icon: Globe },
    { id: 'ai', label: 'AI & Model', icon: Cpu },
    { id: 'voice', label: 'Voice & Speech', icon: Volume2 },
    { id: 'interface', label: 'Interface', icon: Monitor },
    { id: 'advanced', label: 'Advanced', icon: Zap },
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
    { value: 'ok_google', label: 'Ok Google' },
    { value: 'computer', label: 'Computer' },
];

export default function SettingsModal({ isOpen, onClose, config, onSave, availableModels, apiModels, onFetchApiModels, onRefreshLocalModels }: SettingsModalProps) {
    const [localConfig, setLocalConfig] = useState<any>(config || {});
    const [activeTab, setActiveTab] = useState('general');
    const [changed, setChanged] = useState(false);
    const [hfQuery, setHfQuery] = useState('');
    const [fetchingProvider, setFetchingProvider] = useState<string | null>(null);

    useEffect(() => {
        setLocalConfig(config || {});
        setChanged(false);
    }, [config, isOpen]);

    // Reset fetching state when apiModels update
    useEffect(() => {
        setFetchingProvider(null);
    }, [apiModels]);

    if (!isOpen) return null;

    const handleChange = (key: string, value: any) => {
        setLocalConfig((prev: any) => ({ ...prev, [key]: value }));
        setChanged(true);
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
                                <Section title="Primary Provider">
                                    <Select
                                        label="AI Provider"
                                        value={localConfig.provider || 'local'}
                                        onChange={(v: string) => handleChange('provider', v)}
                                        options={[
                                            { value: 'local', label: 'Local (Llama.cpp)' },
                                            { value: 'openai', label: 'OpenAI' },
                                            { value: 'anthropic', label: 'Anthropic' },
                                            { value: 'deepseek', label: 'DeepSeek' },
                                            { value: 'google', label: 'Google Gemini' },
                                            { value: 'openrouter', label: 'OpenRouter' },
                                        ]}
                                    />
                                    <p className="text-xs text-gray-500 mt-2">Select the backend used for standard chat completions.</p>
                                </Section>

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
                                    <div className="mt-4">
                                        <label className="text-sm font-medium text-gray-700 mb-1 block">Temperature ({localConfig.temperature})</label>
                                        <input
                                            type="range" min="0" max="2" step="0.1"
                                            value={localConfig.temperature || 0.7}
                                            onChange={(e) => handleChange('temperature', parseFloat(e.target.value))}
                                            className="w-full accent-blue-500"
                                        />
                                    </div>
                                </Section>

                                <Section title="API Models Override">
                                    <div className="space-y-4">
                                        {PROVIDERS.map(p => {
                                            const hasKey = !!localConfig[`api_key_${p.id}`];
                                            if (!hasKey) return null;

                                            return (
                                                <div key={p.id} className="flex gap-2 items-end animate-in slide-in-from-left-2 fade-in duration-300">
                                                    <div className="flex-1">
                                                        <Select
                                                            label={`${p.label} Model`}
                                                            value={localConfig[`api_model_${p.id}`] || p.defaultModel}
                                                            onChange={(v: string) => handleChange(`api_model_${p.id}`, v)}
                                                            options={[
                                                                // Include default/current if not in list
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
                                                    >
                                                        <RefreshCw size={18} className={cn(fetchingProvider === p.id && "animate-spin")} />
                                                    </button>
                                                </div>
                                            );
                                        })}
                                        {PROVIDERS.every(p => !localConfig[`api_key_${p.id}`]) && (
                                            <p className="text-xs text-gray-400 italic">Add API keys in the General tab to configure model overrides.</p>
                                        )}
                                    </div>
                                </Section>

                                <Section title="Find Models">
                                    <div className="flex gap-2 items-end">
                                        <Input
                                            label="Search HuggingFace"
                                            value={hfQuery}
                                            onChange={setHfQuery}
                                            placeholder="e.g. Mistral-7B-Instruct..."
                                        />
                                        <button
                                            onClick={handleSearchHF}
                                            className="px-4 bg-gray-100 text-gray-700 hover:bg-gray-200 rounded-lg font-medium flex items-center gap-2 transition-colors h-10"
                                        >
                                            <Search size={18} /> Search
                                        </button>
                                    </div>
                                    <p className="text-xs text-gray-500 mt-2">Opens HuggingFace in a new tab. Download .gguf files to your <code>models/</code> folder.</p>
                                </Section>
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
                                </Section>

                                <Section title="Visuals">
                                    <Select
                                        label="Theme Variant"
                                        value={localConfig.theme || 'vaf'}
                                        onChange={(v: string) => handleChange('theme', v)}
                                        options={[
                                            { value: 'vaf', label: 'Default (VAF)' },
                                            { value: 'dark', label: 'Dark' },
                                            { value: 'light', label: 'Light' },
                                            { value: 'hacker', label: 'Hacker' },
                                        ]}
                                    />
                                </Section>
                            </div>
                        )}

                        {activeTab === 'advanced' && (
                            <div className="space-y-6">
                                <Section title="Sub-Agents">
                                    <Switch
                                        label="Separate Terminals"
                                        description="Launch sub-agents in new terminal windows"
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

const Select = ({ label, value, onChange, options }: SelectProps) => (
    <div className="flex flex-col gap-1.5 w-full">
        <label className="text-sm font-medium text-gray-700 ml-1">{label}</label>
        <div className="relative">
            <select
                value={value}
                onChange={(e) => onChange(e.target.value)}
                className="w-full h-10 appearance-none px-4 bg-white border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 transition-all text-gray-700 pr-10"
            >
                {/* Default option if current value is not in options (e.g. custom input previously saved) */}
                {!options.some(o => o.value === value) && value && (
                    <option value={value}>{value} (Current)</option>
                )}
                {options.map((o) => (
                    <option key={o.value} value={o.value}>{o.label}</option>
                ))}
            </select>
            <div className="absolute right-3 top-1/2 -translate-y-1/2 pointer-events-none text-gray-400">
                <ChevronRight size={16} className="rotate-90" />
            </div>
        </div>
    </div>
);

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
