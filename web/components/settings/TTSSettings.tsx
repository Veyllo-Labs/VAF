'use client';

/**
 * TTS Settings Component with Drag-and-Drop Language Selection
 *
 * Features:
 * - View available TTS languages
 * - Install/uninstall language models
 * - Drag-and-drop to reorder language priority
 * - Real-time download progress
 * - Auto-detect language toggle
 */

import React, { useState, useEffect, useCallback } from 'react';
import {
    Volume2, Download, Trash2, GripVertical, Check, Loader2,
    AlertCircle, RefreshCw, Globe, ChevronDown, ChevronUp
} from 'lucide-react';
import { cn } from '@/lib/utils';

interface Language {
    code: string;
    name: string;
    voice: string;
    quality: string;
    installed: boolean;
    priority: number;
    download_status?: {
        status: string;
        progress?: number;
        error?: string;
    };
}

interface TTSConfig {
    language_priority: string[];
    auto_detect: boolean;
    default_language: string;
    installed_languages: string[];
}

interface TTSSettingsProps {
    ttsEnabled: boolean;
    ttsUrl: string;
    autoSpeak: boolean;
    onTtsEnabledChange: (enabled: boolean) => void;
    onTtsUrlChange: (url: string) => void;
    onAutoSpeakChange: (enabled: boolean) => void;
}

export default function TTSSettings({
    ttsEnabled,
    ttsUrl,
    autoSpeak,
    onTtsEnabledChange,
    onTtsUrlChange,
    onAutoSpeakChange
}: TTSSettingsProps) {
    const [languages, setLanguages] = useState<Language[]>([]);
    const [config, setConfig] = useState<TTSConfig | null>(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [expanded, setExpanded] = useState(false);
    const [draggedIndex, setDraggedIndex] = useState<number | null>(null);
    const [ttsHealthy, setTtsHealthy] = useState<boolean | null>(null);

    const baseUrl = ttsUrl || 'http://localhost:5002';

    // Fetch languages and config
    const fetchLanguages = useCallback(async () => {
        setLoading(true);
        setError(null);
        try {
            const response = await fetch('/api/tts/languages');
            if (!response.ok) throw new Error('Failed to fetch languages');
            const data = await response.json();
            setLanguages(data.languages || []);
            setConfig(data.config || null);
            setTtsHealthy(true);
        } catch (err) {
            setError((err as Error).message);
            setTtsHealthy(false);
        } finally {
            setLoading(false);
        }
    }, []);

    // Check health on mount
    useEffect(() => {
        if (ttsEnabled) {
            fetchLanguages();
        }
    }, [ttsEnabled, fetchLanguages]);

    // Poll for download progress
    useEffect(() => {
        if (!ttsEnabled) return;

        const downloading = languages.filter(
            l => l.download_status?.status === 'downloading'
        );
        if (downloading.length === 0) return;

        const interval = setInterval(async () => {
            for (const lang of downloading) {
                try {
                    const response = await fetch(`/api/tts/install/status/${lang.code}`);
                    if (response.ok) {
                        const status = await response.json();
                        setLanguages(prev =>
                            prev.map(l =>
                                l.code === lang.code
                                    ? { ...l, download_status: status, installed: status.status === 'completed' }
                                    : l
                            )
                        );
                        if (status.status === 'completed' || status.status === 'error') {
                            fetchLanguages(); // Refresh full list
                        }
                    }
                } catch {
                    // Ignore polling errors
                }
            }
        }, 1000);

        return () => clearInterval(interval);
    }, [languages, ttsEnabled, fetchLanguages]);

    // Install a language
    const installLanguage = async (code: string) => {
        try {
            setLanguages(prev =>
                prev.map(l =>
                    l.code === code
                        ? { ...l, download_status: { status: 'downloading', progress: 0 } }
                        : l
                )
            );
            const response = await fetch('/api/tts/install', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ language: code })
            });
            if (!response.ok) throw new Error('Install failed');
        } catch (err) {
            setError((err as Error).message);
            fetchLanguages();
        }
    };

    // Uninstall a language
    const uninstallLanguage = async (code: string) => {
        if (!confirm(`Remove ${code.toUpperCase()} language model?`)) return;
        try {
            const response = await fetch('/api/tts/uninstall', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ language: code })
            });
            if (!response.ok) throw new Error('Uninstall failed');
            fetchLanguages();
        } catch (err) {
            setError((err as Error).message);
        }
    };

    // Update config (priority, auto-detect)
    const updateConfig = async (updates: Partial<TTSConfig>) => {
        try {
            const response = await fetch('/api/tts/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(updates)
            });
            if (!response.ok) throw new Error('Config update failed');
            const newConfig = await response.json();
            setConfig(newConfig);
        } catch (err) {
            setError((err as Error).message);
        }
    };

    // Drag and drop handlers
    const handleDragStart = (index: number) => {
        setDraggedIndex(index);
    };

    const handleDragOver = (e: React.DragEvent, index: number) => {
        e.preventDefault();
        if (draggedIndex === null || draggedIndex === index) return;

        const installedLangs = languages.filter(l => l.installed);
        const newOrder = [...installedLangs];
        const [dragged] = newOrder.splice(draggedIndex, 1);
        newOrder.splice(index, 0, dragged);

        // Update local state
        const newPriority = newOrder.map(l => l.code);
        setLanguages(prev => {
            const notInstalled = prev.filter(l => !l.installed);
            return [
                ...newOrder.map((l, i) => ({ ...l, priority: i })),
                ...notInstalled
            ];
        });
        setDraggedIndex(index);
    };

    const handleDragEnd = () => {
        if (draggedIndex !== null) {
            const installedLangs = languages.filter(l => l.installed);
            const newPriority = installedLangs.map(l => l.code);
            updateConfig({ language_priority: newPriority });
        }
        setDraggedIndex(null);
    };

    // Test TTS
    const testTTS = async (lang?: string) => {
        try {
            const text = lang === 'de' ? 'Hallo, ich bin dein Sprachassistent.' :
                        lang === 'en' ? 'Hello, I am your voice assistant.' :
                        lang === 'fr' ? 'Bonjour, je suis votre assistant vocal.' :
                        'Test speech synthesis.';

            const response = await fetch('/api/tts/synthesize', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ text, language: lang })
            });

            if (!response.ok) throw new Error('Synthesis failed');

            const audioBlob = await response.blob();
            const audioUrl = URL.createObjectURL(audioBlob);
            const audio = new Audio(audioUrl);
            audio.play();
        } catch (err) {
            setError((err as Error).message);
        }
    };

    const installedLanguages = languages.filter(l => l.installed);
    const availableLanguages = languages.filter(l => !l.installed);

    return (
        <div className="space-y-4">
            {/* Enable TTS Toggle */}
            <div className="flex items-center justify-between py-2">
                <div className="flex items-center gap-3">
                    <div className="w-10 h-10 rounded-xl bg-gray-900 flex items-center justify-center">
                        <Volume2 className="w-5 h-5 text-white" />
                    </div>
                    <div>
                        <div className="font-medium text-gray-900">Enable Voice Output (TTS)</div>
                        <div className="text-sm text-gray-500">Agent speaks responses aloud in your browser</div>
                    </div>
                </div>
                <button
                    onClick={() => onTtsEnabledChange(!ttsEnabled)}
                    className={cn(
                        "w-11 h-6 rounded-full transition-colors relative",
                        ttsEnabled ? "bg-gray-800" : "bg-gray-300"
                    )}
                >
                    <div className={cn(
                        "w-5 h-5 rounded-full bg-white shadow absolute top-0.5 transition-transform",
                        ttsEnabled ? "translate-x-5" : "translate-x-0.5"
                    )} />
                </button>
            </div>

            {ttsEnabled && (
                <div className="pl-4 border-l-2 border-gray-200 space-y-4">
                    {/* Docker URL */}
                    <div>
                        <label className="block text-sm font-medium text-gray-700 mb-1">
                            Docker TTS URL
                        </label>
                        <div className="flex gap-2">
                            <input
                                type="text"
                                value={ttsUrl}
                                onChange={(e) => onTtsUrlChange(e.target.value)}
                                placeholder="http://localhost:5002"
                                className="flex-1 px-4 py-3 rounded-xl bg-white border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-transparent"
                            />
                            <button
                                onClick={() => fetchLanguages()}
                                className="p-2 hover:bg-gray-100 rounded-lg transition-colors text-gray-500 hover:text-gray-700"
                                title="Refresh"
                            >
                                <RefreshCw className={cn("w-4 h-4", loading && "animate-spin")} />
                            </button>
                        </div>
                        <div className="flex items-center gap-2 mt-1">
                            <div className={cn(
                                "w-2 h-2 rounded-full",
                                ttsHealthy === true ? "bg-green-500" :
                                ttsHealthy === false ? "bg-red-500" :
                                "bg-gray-400"
                            )} />
                            <span className={cn(
                                "text-xs",
                                ttsHealthy === true ? "text-green-700" :
                                ttsHealthy === false ? "text-red-600" :
                                "text-gray-500"
                            )}>
                                {ttsHealthy === true ? "Connected" :
                                 ttsHealthy === false ? "Not available" :
                                 "Checking..."}
                            </span>
                        </div>
                    </div>

                    {/* Error Display */}
                    {error && (
                        <div className="p-3 bg-red-100 rounded-lg flex items-center gap-2 text-red-600 border border-red-500">
                            <AlertCircle className="w-4 h-4 flex-shrink-0" />
                            <span className="text-sm">{error}</span>
                            <button onClick={() => setError(null)} className="ml-auto text-red-500 hover:text-red-700">×</button>
                        </div>
                    )}

                    {/* Language Management */}
                    {ttsHealthy && (
                        <div className="space-y-4">
                            {/* Installed Languages (Drag & Drop) */}
                            <div>
                                <div className="flex items-center justify-between mb-2">
                                    <h4 className="text-sm font-medium text-gray-700">
                                        Installed Languages ({installedLanguages.length})
                                    </h4>
                                    <span className="text-xs text-gray-500">Drag to reorder priority</span>
                                </div>

                                {installedLanguages.length === 0 ? (
                                    <div className="p-4 bg-gray-50 rounded-xl border border-gray-200 text-center text-sm text-gray-500">
                                        No languages installed. Install one below.
                                    </div>
                                ) : (
                                    <div className="space-y-1">
                                        {installedLanguages.map((lang, index) => (
                                            <div
                                                key={lang.code}
                                                draggable
                                                onDragStart={() => handleDragStart(index)}
                                                onDragOver={(e) => handleDragOver(e, index)}
                                                onDragEnd={handleDragEnd}
                                                className={cn(
                                                    "flex items-center gap-3 p-3 bg-white rounded-xl border border-gray-200 transition-all cursor-move hover:shadow-sm",
                                                    draggedIndex === index
                                                        ? "border-gray-400 shadow-md scale-[1.02] ring-2 ring-gray-400/20"
                                                        : "hover:border-gray-300"
                                                )}
                                            >
                                                <GripVertical className="w-4 h-4 text-gray-400" />
                                                <div className="w-8 h-8 rounded-lg bg-gray-100 flex items-center justify-center text-xs font-bold text-gray-700 border border-gray-300">
                                                    {lang.code.toUpperCase()}
                                                </div>
                                                <div className="flex-1 min-w-0">
                                                    <div className="font-medium text-gray-900 text-sm">
                                                        {lang.name}
                                                    </div>
                                                    <div className="text-xs text-gray-500">
                                                        Voice: {lang.voice} • {lang.quality}
                                                    </div>
                                                </div>
                                                <div className="flex items-center gap-1">
                                                    <button
                                                        onClick={() => testTTS(lang.code)}
                                                        className="p-1.5 hover:bg-gray-100 rounded-lg transition-colors text-gray-500 hover:text-gray-700"
                                                        title="Test voice"
                                                    >
                                                        <Volume2 className="w-4 h-4" />
                                                    </button>
                                                    <button
                                                        onClick={() => {
                                                            if (!confirm('Are you sure you want to remove this language? You can install it again later from available languages.')) return;
                                                            uninstallLanguage(lang.code);
                                                        }}
                                                        className="p-1.5 hover:bg-red-100 rounded-lg transition-colors"
                                                        title="Remove"
                                                    >
                                                        <Trash2 className="w-4 h-4 text-red-500" />
                                                    </button>
                                                </div>
                                                {index === 0 && (
                                                    <span className="px-2 py-0.5 bg-gray-100 text-gray-700 text-xs rounded-full border border-gray-300">
                                                        Default
                                                    </span>
                                                )}
                                            </div>
                                        ))}
                                    </div>
                                )}
                            </div>

                            {/* Available Languages */}
                            <div>
                                <button
                                    onClick={() => setExpanded(!expanded)}
                                    className="flex items-center gap-2 text-sm font-medium text-gray-700 hover:text-gray-900 transition-colors"
                                >
                                    {expanded ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
                                    Available Languages ({availableLanguages.length})
                                </button>

                                {expanded && (
                                    <div className="mt-2 grid grid-cols-2 gap-2">
                                        {availableLanguages.map(lang => (
                                            <div
                                                key={lang.code}
                                                className="flex items-center gap-2 p-2 bg-gray-50 rounded-xl border border-gray-200"
                                            >
                                                <div className="w-8 h-8 rounded-lg bg-gray-200 flex items-center justify-center text-xs font-bold text-gray-500">
                                                    {lang.code.toUpperCase()}
                                                </div>
                                                <div className="flex-1 min-w-0">
                                                    <div className="font-medium text-gray-700 text-sm truncate">
                                                        {lang.name}
                                                    </div>
                                                </div>
                                                {lang.download_status?.status === 'downloading' ? (
                                                    <div className="flex items-center gap-1">
                                                        <Loader2 className="w-4 h-4 animate-spin text-gray-500" />
                                                        <span className="text-xs text-gray-600">
                                                            {lang.download_status.progress || 0}%
                                                        </span>
                                                    </div>
                                                ) : (
                                                    <button
                                                        onClick={() => installLanguage(lang.code)}
                                                        className="p-1.5 hover:bg-gray-100 rounded-lg transition-colors text-gray-500 hover:text-gray-700"
                                                        title="Install"
                                                    >
                                                        <Download className="w-4 h-4" />
                                                    </button>
                                                )}
                                            </div>
                                        ))}
                                    </div>
                                )}
                            </div>

                            {/* Auto-detect & Auto-speak */}
                            <div className="space-y-3 pt-2 border-t border-gray-200">
                                <div className="flex items-center justify-between">
                                    <div>
                                        <div className="font-medium text-gray-900 text-sm">Auto-detect Language</div>
                                        <div className="text-xs text-gray-500">Automatically select voice based on text language</div>
                                    </div>
                                    <button
                                        onClick={() => updateConfig({ auto_detect: !config?.auto_detect })}
                                        className={cn(
                                            "w-10 h-5 rounded-full transition-colors relative",
                                            config?.auto_detect ? "bg-gray-800" : "bg-gray-300"
                                        )}
                                    >
                                        <div className={cn(
                                            "w-4 h-4 rounded-full bg-white shadow absolute top-0.5 transition-transform",
                                            config?.auto_detect ? "translate-x-5" : "translate-x-0.5"
                                        )} />
                                    </button>
                                </div>

                                <div className="flex items-center justify-between">
                                    <div>
                                        <div className="font-medium text-gray-900 text-sm">Auto-speak Responses</div>
                                        <div className="text-xs text-gray-500">Automatically speak agent responses</div>
                                    </div>
                                    <button
                                        onClick={() => onAutoSpeakChange(!autoSpeak)}
                                        className={cn(
                                            "w-10 h-5 rounded-full transition-colors relative",
                                            autoSpeak ? "bg-gray-800" : "bg-gray-300"
                                        )}
                                    >
                                        <div className={cn(
                                            "w-4 h-4 rounded-full bg-white shadow absolute top-0.5 transition-transform",
                                            autoSpeak ? "translate-x-5" : "translate-x-0.5"
                                        )} />
                                    </button>
                                </div>
                            </div>
                        </div>
                    )}
                </div>
            )}
        </div>
    );
}
