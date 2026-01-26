'use client';

import React, { useEffect, useState, useRef } from 'react';
import {
    Send, Menu, Plus, MessageSquare, Bot, User, Trash2, Edit2, Paperclip,
    Activity, GitBranch, Workflow, CheckCircle2, ShieldAlert, Loader2,
    Settings
} from 'lucide-react';
import { cn } from '@/lib/utils';
import SettingsModal from '@/components/SettingsModal';

// Types
type Message = {
    role: 'user' | 'assistant' | 'system';
    content: string;
    timestamp: number;
};

type Session = {
    id: string;
    title: string;
};

// Helper to parse and merge thinking blocks
const parseContent = (content: string) => {
    if (!content) return { thought: null, answer: "" };
    let clean = content.replace(/[\[][\/]?\w+\s*\w+[\]]/g, '').replace(/^resposta\s*/i, '');
    let merged = clean.replace(/<\/think>\s*<think>/g, ' ');
    const openTag = "<think>";
    const closeTag = "</think>";
    const openIndex = merged.indexOf(openTag);
    if (openIndex !== -1) {
        const closeIndex = merged.lastIndexOf(closeTag);
        if (closeIndex !== -1 && closeIndex > openIndex) {
            const thought = merged.substring(openIndex + openTag.length, closeIndex).trim();
            const answer = (merged.substring(0, openIndex) + merged.substring(closeIndex + closeTag.length)).trim();
            return { thought, answer };
        } else {
            const thought = merged.substring(openIndex + openTag.length).trim();
            const answer = merged.substring(0, openIndex).trim();
            return { thought, answer };
        }
    }
    return { thought: null, answer: merged };
};

// Component: Thinking Accordion
const ThinkingDetails = ({ thought }: { thought: string }) => {
    const [isOpen, setIsOpen] = useState(false);
    if (!thought) return null;
    return (
        <div className="mb-3 rounded-xl border border-gray-200 bg-gray-50/50 overflow-hidden w-full max-w-[95%] shadow-sm">
            <button
                type="button" onClick={() => setIsOpen(!isOpen)}
                className="w-full px-4 py-2.5 flex items-center justify-between text-[11px] uppercase tracking-wide font-semibold text-gray-500 hover:bg-gray-100 transition-colors"
            >
                <span className="flex items-center gap-2"><Activity size={14} /> Thinking Process</span>
                <div className={cn("transition-transform duration-300 text-gray-400", isOpen ? "rotate-180" : "")}>▼</div>
            </button>
            <div className={cn("px-4 text-xs text-slate-600 font-mono leading-relaxed border-t border-gray-200 transition-all bg-white/50", isOpen ? "max-h-[500px] opacity-100 py-3 overflow-y-auto" : "max-h-0 opacity-0 py-0 overflow-hidden")}>
                {thought}
            </div>
        </div>
    );
};

// Component: System Step Log
const SystemStep = ({ message, isLoading }: { message: string, isLoading?: boolean }) => {
    const isRouter = message.includes('Router');
    const isWorkflow = message.includes('Step') || message.includes('Workflow');
    const isSafety = message.includes('Safety');

    // Extract clean text
    const cleanText = message.replace(/^(Router|Step \d+\/\d+|System|Agent)\s*[:\|]?\s*/, '');
    const source = message.match(/^(Router|Step \d+\/\d+|System|Agent)/)?.[0] || "System";

    return (
        <div className="flex gap-4 w-full animate-in fade-in slide-in-from-left-2 duration-300 my-1">
            <div className="w-9 shrink-0 flex justify-center">
                <div className="w-0.5 h-full bg-gray-100 relative">
                    <div className={cn(
                        "absolute top-1/2 -translate-y-1/2 left-1/2 -translate-x-1/2 w-5 h-5 rounded-full border bg-white flex items-center justify-center z-10",
                        isLoading ? "border-gray-300 text-gray-700 shadow-sm" :
                            isRouter ? "border-orange-200 text-orange-500" :
                                isSafety ? "border-red-200 text-red-500" :
                                    isWorkflow ? "border-blue-200 text-blue-500" : "border-gray-200 text-gray-400"
                    )}>
                        {isLoading ? <Loader2 size={10} className="animate-spin" /> :
                            isRouter ? <GitBranch size={10} /> :
                                isSafety ? <ShieldAlert size={10} /> :
                                    isWorkflow ? <Workflow size={10} /> : <CheckCircle2 size={10} />}
                    </div>
                </div>
            </div>
            <div className="flex-1 py-1">
                <div className="text-xs text-gray-500 flex items-center gap-2">
                    <span className={cn("font-semibold uppercase tracking-wider text-[10px]", isLoading ? "text-gray-600" : "text-gray-400")}>{source}</span>
                    <span className={cn(isLoading ? "text-gray-900 font-medium" : "text-gray-600")}>{cleanText}</span>
                </div>
            </div>
        </div>
    );
};

export default function VAFDashboard() {
    const [input, setInput] = useState('');
    const [messages, setMessages] = useState<Message[]>([]);
    const [status, setStatus] = useState('connecting');
    const [sessions, setSessions] = useState<Session[]>([]);
    const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
    const [ws, setWs] = useState<WebSocket | null>(null);
    const [loading, setLoading] = useState(false);
    const [statusMessage, setStatusMessage] = useState(''); // RE-ADDED
    const [editingId, setEditingId] = useState<string | null>(null);
    const [editName, setEditName] = useState('');
    const [config, setConfig] = useState<any>({});
    const [availableModels, setAvailableModels] = useState<string[]>([]);
    const [apiModels, setApiModels] = useState<Record<string, string[]>>({});
    const [isSettingsOpen, setIsSettingsOpen] = useState(false);


    const scrollRef = useRef<HTMLDivElement>(null);
    const containerRef = useRef<HTMLDivElement>(null);

    useEffect(() => {
        if (typeof window === 'undefined') return;
        const socket = new WebSocket('ws://localhost:8001/ws');
        socket.onopen = () => {
            setStatus('connected');
            socket.send(JSON.stringify({ type: 'get_config' }));
            socket.send(JSON.stringify({ type: 'get_models' }));
        };
        socket.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                if (data.type === 'new_log') {
                    const src = data.entry.source || "";
                    const rawMsg = data.entry.message || "";

                    // Skip "Agent Thinking..." as requested
                    if (src === 'Agent' && rawMsg.toLowerCase().includes('thinking')) {
                        return;
                    }

                    if (src.includes('Step') || src.includes('Router') || src.includes('System') || src.includes('Agent')) {
                        const cleanMsg = rawMsg.replace(/^\|\s*/, '');

                        // Strip trailing dots/ellipsis/whitespace for cleaner display
                        const displayMsg = cleanMsg.replace(/[\.\u2026\s]+$/, '');

                        // Skip if result is empty (was just dots or empty)
                        if (!displayMsg) return;

                        // Set status message for the ghost loader
                        setStatusMessage(`${src}: ${displayMsg}`);

                        setMessages(prev => {
                            const last = prev[prev.length - 1];
                            const newContent = `${src}: ${cleanMsg}`;
                            if (last && last.role === 'system' && last.content === newContent) return prev;
                            return [...prev, { role: 'system', content: newContent, timestamp: Date.now() }];
                        });
                    }
                }
                else if (data.type === 'agent_message_update') {
                    // CRITICAL: Only update if this message belongs to the current session!
                    // If user switched chats while bot was typing, ignore this update.
                    if (data.sessionId && currentSessionId && data.sessionId !== currentSessionId) {
                        return;
                    }

                    setLoading(false);
                    setStatusMessage(''); // Clear status when answer starts
                    setMessages(prev => {
                        const last = prev[prev.length - 1];
                        if (last && last.role === 'assistant') {
                            const newMsgs = [...prev];
                            newMsgs[newMsgs.length - 1] = { ...last, content: data.content };
                            return newMsgs;
                        } else {
                            return [...prev, { role: 'assistant', content: data.content, timestamp: Date.now() }];
                        }
                    });
                }
                else if (data.type === 'session_list') {
                    setSessions(data.sessions);

                    // Only auto-create if we have NO sessions and NO active session selected
                    // This prevents creating duplicates during switching
                    if (data.sessions.length === 0 && !currentSessionId) {
                        ws?.send(JSON.stringify({ type: 'new_session' }));
                        return;
                    }

                    // Auto-select latest if none selected
                    if (!currentSessionId && data.sessions.length > 0) {
                        setCurrentSessionId(data.sessions[0].id);
                        ws?.send(JSON.stringify({ type: 'load_session', id: data.sessions[0].id }));
                    }
                }
                else if (data.type === 'history_update') {
                    setCurrentSessionId(data.sessionId);
                    // Clear logs from previous session immediately
                    setMessages(prev => {
                        // Keep only persistent messages, remove temporary system logs
                        return data.messages
                            .filter((m: any) => m.role !== 'system') // Hide raw system prompts
                            .map((m: any) => ({
                                role: m.role,
                                content: m.content,
                                timestamp: m.timestamp ? new Date(m.timestamp).getTime() : Date.now()
                            }));
                    });
                }
                else if (data.type === 'config_update') {
                    setConfig(data.config);
                }
                else if (data.type === 'config_saved') {
                    // Refresh config to confirm save
                    ws?.send(JSON.stringify({ type: 'get_config' }));
                }
            } catch (e) { console.error(e); }
        };
        socket.onclose = () => setStatus('disconnected');
        setWs(socket);
        return () => socket.close();
    }, []);

    useEffect(() => {
        if (scrollRef.current) scrollRef.current.scrollIntoView({ behavior: 'smooth' });
    }, [messages, loading]);

    const sendMessage = (e?: React.FormEvent) => {
        e?.preventDefault();
        if (!input.trim() || !ws) return;
        setMessages(prev => [...prev, { role: 'user', content: input, timestamp: Date.now() }]);
        setLoading(true);
        ws.send(JSON.stringify({ type: 'chat', content: input }));
        setInput('');
    };

    const startEditing = (s: Session) => {
        setEditingId(s.id);
        setEditName(s.title.replace(".json", ""));
    };

    const submitRename = () => {
        if (editingId && editName.trim()) {
            // Optimistic update to prevent flickering
            setSessions(prev => prev.map(s =>
                s.id === editingId ? { ...s, title: editName.trim() } : s
            ));

            ws?.send(JSON.stringify({ type: 'rename_session', id: editingId, newName: editName.trim() }));
            setEditingId(null);
        } else {
            setEditingId(null);
        }
    };

    const handleSaveConfig = (newConfig: any) => {
        ws?.send(JSON.stringify({ type: 'save_config', config: newConfig }));
        // Optimistically update
        setConfig(newConfig);
    };

    const fetchApiModels = (provider: string, apiKey: string) => {
        ws?.send(JSON.stringify({ type: 'get_api_models', provider, api_key: apiKey }));
    };

    const refreshLocalModels = () => {
        ws?.send(JSON.stringify({ type: 'get_models' }));
    };

    return (
        <main className="h-screen flex flex-col bg-gray-50 text-gray-900 font-sans overflow-hidden">
            <header className="bg-white border-b border-gray-200 px-6 py-4 z-30 relative shadow-sm flex justify-between items-center">
                <h1 className="text-xl font-bold text-gray-800 flex items-center gap-2">
                    <div className="w-8 h-8 rounded-lg bg-gray-900 flex items-center justify-center text-white text-xs font-bold">V</div>
                    Veyllo Agent Framework
                </h1>
            </header>

            <div className="flex-1 flex overflow-hidden">
                <aside className="group flex flex-col h-full bg-white border-r border-gray-200 w-16 hover:w-72 transition-all duration-300 z-20 shadow-lg overflow-hidden">
                    <div onClick={() => ws?.send(JSON.stringify({ type: 'new_session' }))} className="p-4 border-b border-gray-200 flex items-center gap-4 cursor-pointer hover:bg-gray-50">
                        <Plus size={24} className="text-gray-900 shrink-0" />
                        <span className="font-medium whitespace-nowrap opacity-0 group-hover:opacity-100 transition-opacity">New Chat</span>
                    </div>
                    <div className="flex-1 overflow-y-auto overflow-x-hidden p-2 space-y-1">
                        {sessions.map(s => (
                            <div key={s.id} onClick={() => ws?.send(JSON.stringify({ type: 'load_session', id: s.id }))}
                                className={cn("flex items-center gap-3 p-2 pl-3 rounded-lg cursor-pointer group/item relative", currentSessionId === s.id ? 'bg-transparent' : 'hover:bg-gray-100')}>

                                {/* Active Indicator (Dot) */}
                                {currentSessionId === s.id && (
                                    <div className="absolute left-1 my-auto w-1 h-1 bg-black rounded-full" />
                                )}

                                <MessageSquare size={16} className={cn("shrink-0", currentSessionId === s.id ? "text-gray-900" : "text-gray-400")} />

                                <div className="flex-1 flex justify-between items-center opacity-0 group-hover:opacity-100 transition-opacity min-w-0 pr-1">
                                    {editingId === s.id ? (
                                        <input
                                            autoFocus
                                            className="w-full text-xs border-b border-indigo-500 focus:outline-none bg-transparent"
                                            value={editName}
                                            onChange={e => setEditName(e.target.value)}
                                            onKeyDown={e => {
                                                if (e.key === 'Enter') submitRename();
                                                if (e.key === 'Escape') setEditingId(null);
                                            }}
                                            onBlur={submitRename}
                                            onClick={e => e.stopPropagation()}
                                        />
                                    ) : (
                                        <span className={cn("truncate text-sm transition-colors", currentSessionId === s.id ? "font-medium text-gray-900" : "text-gray-600")}>
                                            {s.title.replace(".json", "")}
                                        </span>
                                    )}

                                    {/* Action Icons (Hover Only) */}
                                    <div className="flex items-center gap-1.5 opacity-0 group-hover/item:opacity-100 transition-opacity">
                                        {!editingId && (
                                            <>
                                                <Edit2 size={12} className="text-gray-400 hover:text-gray-900" onClick={(e) => { e.stopPropagation(); startEditing(s); }} />
                                                <Trash2 size={12} className="text-gray-400 hover:text-red-600" onClick={(e) => { e.stopPropagation(); if (confirm('Delete?')) ws?.send(JSON.stringify({ type: 'delete_session', id: s.id })); }} />
                                            </>
                                        )}
                                    </div>
                                </div>
                            </div>
                        ))}
                    </div>

                    {/* Status Footer - Redesigned */}
                    <div className="p-3 mt-auto mb-2 flex flex-col gap-1 w-full overflow-hidden">

                        {/* Connection Indicator */}
                        <div className="flex items-center gap-3 p-2 rounded-lg justify-start transition-all duration-300">
                            <div className="w-6 flex justify-center shrink-0">
                                <div
                                    className={cn(
                                        "w-2.5 h-2.5 rounded-full shadow-[0_0_10px_rgba(0,0,0,0.2)] transition-colors",
                                        status === 'connected' ? "bg-green-500 shadow-green-400/50" : "bg-red-500 shadow-red-400/50"
                                    )}
                                />
                            </div>
                            <span className="max-w-0 group-hover:max-w-xs overflow-hidden opacity-0 group-hover:opacity-100 transition-all duration-300 whitespace-nowrap text-sm font-medium text-gray-500">{status === 'connected' ? 'Connected' : 'Disconnected'}</span>
                        </div>

                        <div
                            onClick={() => setIsSettingsOpen(true)}
                            className="flex items-center gap-3 p-2 rounded-xl cursor-pointer hover:bg-gray-100 text-gray-500 hover:text-gray-900 group/settings transition-all justify-start"
                            title="Settings"
                        >
                            <div className="w-6 flex justify-center shrink-0">
                                <Settings size={20} />
                            </div>
                            <span className="max-w-0 group-hover:max-w-xs overflow-hidden opacity-0 group-hover:opacity-100 transition-all duration-300 font-medium whitespace-nowrap">Settings</span>
                        </div>
                    </div>
                </aside>

                <div className="flex-1 flex flex-col relative bg-white overflow-hidden">
                    <div className="flex-1 overflow-y-auto p-6" ref={containerRef}>
                        <div className="max-w-4xl mx-auto space-y-2 pb-32">
                            {messages.length === 0 && (
                                <div className="h-full flex flex-col items-center justify-center pt-20 text-center">
                                    <Bot size={48} className="text-gray-300 mb-4" />
                                    <h2 className="text-2xl font-bold">How can I help you?</h2>
                                </div>
                            )}
                            {messages.filter(m => !m.content.includes('__CMD__')).map((msg, i) => { // Filter out internal commands
                                // Render System Steps (Timeline Style)
                                if (msg.role === 'system') {
                                    const isLast = i === messages.length - 1;
                                    return <SystemStep key={i} message={msg.content} isLoading={loading && isLast} />;
                                }

                                const { thought, answer } = parseContent(msg.content);
                                const isBot = msg.role === 'assistant';

                                // Add top margin if following a system step
                                const prevWasSystem = i > 0 && messages[i - 1].role === 'system';

                                return (
                                    <div key={i} className={cn("flex gap-4 pt-4", isBot ? "justify-start" : "justify-end", prevWasSystem ? "pt-2" : "pt-4")}>
                                        {isBot && <div className="w-9 h-9 rounded-xl bg-gray-900 flex items-center justify-center text-white shadow-sm shrink-0"><Bot size={18} /></div>}
                                        <div className={cn("max-w-[85%] flex flex-col w-full", isBot ? "items-start" : "items-end")}>

                                            {isBot && thought && <ThinkingDetails thought={thought} />}

                                            {(answer || !isBot) && (
                                                <div className={cn("px-5 py-3 rounded-2xl shadow-sm text-sm leading-relaxed",
                                                    isBot ? "bg-white text-gray-800 rounded-tl-none border border-gray-200" : "bg-gray-800 text-white rounded-tr-none")}>
                                                    <p className="whitespace-pre-wrap">{answer}</p>
                                                </div>
                                            )}

                                            {/* Show status steps below the active message if streaming */}
                                            {loading && isBot && i === messages.length - 1 && statusMessage && (
                                                <span className="text-[10px] text-gray-400 mt-1 ml-1 animate-in fade-in">{statusMessage}</span>
                                            )}
                                        </div>
                                        {!isBot && <div className="w-9 h-9 rounded-xl bg-white border border-gray-200 flex items-center justify-center text-gray-500 shadow-sm shrink-0"><User size={18} /></div>}
                                    </div>
                                );
                            })}

                            {loading && messages.length > 0 && messages[messages.length - 1].role === 'user' && (
                                <div className="flex gap-4 items-center animate-pulse pt-4">
                                    <div className="w-9 h-9 rounded-xl bg-gray-200 flex items-center justify-center text-gray-400"><Bot size={18} /></div>
                                    <div className="flex flex-col gap-1">
                                        <div className="bg-gray-100 px-4 py-2 rounded-2xl rounded-tl-none w-fit flex gap-1">
                                            <span className="w-1.5 h-1.5 bg-gray-400 rounded-full animate-bounce">.</span>
                                            <span className="w-1.5 h-1.5 bg-gray-400 rounded-full animate-bounce delay-75">.</span>
                                            <span className="w-1.5 h-1.5 bg-gray-400 rounded-full animate-bounce delay-150">.</span>
                                        </div>
                                        {statusMessage && <span className="text-[10px] text-gray-400 ml-2">{statusMessage}</span>}
                                    </div>
                                </div>
                            )}
                            <div ref={scrollRef} />
                        </div>
                    </div>

                    <div className="absolute bottom-0 w-full bg-gradient-to-t from-white via-white to-transparent pt-10 pb-8 px-6">
                        <form onSubmit={sendMessage} className="max-w-4xl mx-auto flex items-center bg-white rounded-2xl border border-gray-200 shadow-xl focus-within:border-gray-400 transition-all overflow-hidden">
                            <button type="button" className="p-4 text-gray-400 hover:text-gray-900"><Paperclip size={20} /></button>
                            <input type="text" value={input} onChange={e => setInput(e.target.value)} placeholder="Ask anything..." className="flex-1 py-4 bg-transparent border-none focus:ring-0 focus:outline-none text-sm" disabled={loading} />
                            <button type="submit" disabled={!input.trim() || loading} className="m-2 p-2 bg-gray-900 text-white rounded-xl hover:bg-black disabled:bg-gray-200 transition-all shadow-sm">
                                <Send size={18} className="mx-2" />
                            </button>
                        </form>
                    </div>
                </div>
            </div>
            <SettingsModal
                isOpen={isSettingsOpen}
                onClose={() => setIsSettingsOpen(false)}
                config={config}
                onSave={handleSaveConfig}
                availableModels={availableModels}
                apiModels={apiModels}
                onFetchApiModels={fetchApiModels}
                onRefreshLocalModels={refreshLocalModels}
            />
        </main>
    );
}
