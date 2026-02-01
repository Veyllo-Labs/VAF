'use client';

/**
 * VAF Memory System - Main Page
 * 
 * Features:
 * - Interactive memory graph visualization
 * - RAG-powered query interface
 * - Memory creation and management
 * - Real-time graph updates
 */

import React, { useEffect, useState } from 'react';
import { useMemoryStore } from '@/components/memory/stores/memoryStore';
import MemoryGraph from '@/components/memory/MemoryGraph';
import MemoryDetailPanel from '@/components/memory/MemoryDetailPanel';
import RagQueryPanel from '@/components/memory/RagQueryPanel';
import { 
    Plus, RefreshCw, Brain, Link2, 
    ChevronLeft, AlertTriangle, CheckCircle,
    FileText, Sparkles, X
} from 'lucide-react';
import { cn } from '@/lib/utils';
import Link from 'next/link';

// Create Memory Modal
function CreateMemoryModal({ 
    isOpen, 
    onClose, 
    onCreate 
}: { 
    isOpen: boolean; 
    onClose: () => void;
    onCreate: (content: string, metadata: { title: string; tags: string[]; type: string }) => Promise<void>;
}) {
    const [content, setContent] = useState('');
    const [title, setTitle] = useState('');
    const [tags, setTags] = useState('');
    const [type, setType] = useState('note');
    const [isCreating, setIsCreating] = useState(false);
    
    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        if (!content.trim()) return;
        
        setIsCreating(true);
        await onCreate(content, {
            title: title || content.slice(0, 50),
            tags: tags.split(',').map(t => t.trim()).filter(Boolean),
            type
        });
        setIsCreating(false);
        setContent('');
        setTitle('');
        setTags('');
        setType('note');
        onClose();
    };
    
    if (!isOpen) return null;
    
    return (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 backdrop-blur-sm p-4">
            <div className="bg-white rounded-2xl shadow-2xl w-full max-w-2xl mx-4 overflow-hidden border border-gray-200 max-h-[90vh] flex flex-col">
                {/* Header per DESIGN 4.4 */}
                <div className="flex items-center justify-between p-6 border-b border-gray-200 bg-gray-50">
                    <div className="flex items-center gap-3">
                        <div className="w-10 h-10 rounded-xl bg-gray-900 flex items-center justify-center">
                            <Plus className="w-5 h-5 text-white" />
                        </div>
                        <div>
                            <h2 className="text-xl font-bold text-gray-900">Create Memory</h2>
                            <p className="text-sm text-gray-500">Add a new memory to your graph</p>
                        </div>
                    </div>
                    <button
                        onClick={onClose}
                        className="p-2 hover:bg-gray-200 rounded-lg transition-colors"
                        title="Close"
                    >
                        <X className="w-5 h-5 text-gray-500" />
                    </button>
                </div>
                
                <form onSubmit={handleSubmit} className="flex flex-col flex-1 overflow-hidden">
                    <div className="p-6 space-y-4 overflow-y-auto">
                        <div>
                            <label className="block text-sm font-medium text-gray-700 mb-1">
                                Title
                            </label>
                            <input
                                type="text"
                                value={title}
                                onChange={(e) => setTitle(e.target.value)}
                                placeholder="Memory title (optional)"
                                className="w-full px-4 py-3 rounded-xl bg-white border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-transparent"
                            />
                        </div>
                        
                        <div>
                            <label className="block text-sm font-medium text-gray-700 mb-1">
                                Content <span className="text-red-500">*</span>
                            </label>
                            <textarea
                                value={content}
                                onChange={(e) => setContent(e.target.value)}
                                placeholder="Enter the memory content..."
                                rows={8}
                                required
                                className="w-full px-4 py-3 rounded-xl bg-white border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-transparent"
                            />
                        </div>
                        
                        <div className="grid grid-cols-2 gap-4">
                            <div>
                                <label className="block text-sm font-medium text-gray-700 mb-1">
                                    Tags
                                </label>
                                <input
                                    type="text"
                                    value={tags}
                                    onChange={(e) => setTags(e.target.value)}
                                    placeholder="tag1, tag2, tag3"
                                    className="w-full px-4 py-3 rounded-xl bg-white border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-transparent"
                                />
                            </div>
                            <div>
                                <label className="block text-sm font-medium text-gray-700 mb-1">
                                    Type
                                </label>
                                <select
                                    value={type}
                                    onChange={(e) => setType(e.target.value)}
                                    className="w-full h-10 px-4 bg-white border border-gray-200 rounded-lg text-sm text-gray-700 focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500"
                                >
                                    <option value="note">Note</option>
                                    <option value="document">Document</option>
                                    <option value="code">Code</option>
                                    <option value="conversation">Conversation</option>
                                </select>
                            </div>
                        </div>
                    </div>
                    
                    {/* Footer per DESIGN 4.4 */}
                    <div className="flex items-center justify-between p-6 border-t border-gray-200 bg-gray-50">
                        <button
                            type="button"
                            onClick={onClose}
                            className="text-gray-600 hover:bg-gray-200 px-4 py-2 rounded-lg transition-colors"
                        >
                            Cancel
                        </button>
                        <button
                            type="submit"
                            disabled={!content.trim() || isCreating}
                            className="bg-gray-900 hover:bg-gray-800 text-white px-6 py-2 rounded-lg font-medium disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2 transition-colors"
                        >
                            {isCreating ? (
                                <>
                                    <RefreshCw className="w-4 h-4 animate-spin" />
                                    Creating...
                                </>
                            ) : (
                                <>
                                    <Plus className="w-4 h-4" />
                                    Create Memory
                                </>
                            )}
                        </button>
                    </div>
                </form>
            </div>
        </div>
    );
}

// Stats Badge Component (DESIGN: standard card / meta text)
function StatsBadge({ label, value, icon: Icon }: { label: string; value: number | string; icon: React.ElementType }) {
    return (
        <div className="flex items-center gap-2 px-3 py-1.5 bg-white rounded-xl border border-gray-200 shadow-sm">
            <Icon className="w-4 h-4 text-gray-500" />
            <span className="text-xs text-gray-500">{label}:</span>
            <span className="text-sm font-medium text-gray-900">{value}</span>
        </div>
    );
}

export default function MemoryPage() {
    const {
        nodes,
        stats,
        error,
        isLoading,
        fetchGraph,
        fetchStats,
        createMemory,
        selectedNodeId,
        clearError,
    } = useMemoryStore();
    
    const [showCreateModal, setShowCreateModal] = useState(false);
    const [isInitialized, setIsInitialized] = useState(false);
    const [detailsExpanded, setDetailsExpanded] = useState(true);
    
    // Initialize on mount
    useEffect(() => {
        const init = async () => {
            await Promise.all([fetchGraph(), fetchStats()]);
            setIsInitialized(true);
        };
        init();
    }, [fetchGraph, fetchStats]);
    
    const handleCreateMemory = async (
        content: string, 
        metadata: { title: string; tags: string[]; type: string }
    ) => {
        await createMemory(content, metadata);
    };
    
    const handleRefresh = async () => {
        await Promise.all([fetchGraph(), fetchStats()]);
    };
    
    return (
        <div className="h-screen bg-gray-50 flex flex-col overflow-hidden">
            {/* Header */}
            <header className="bg-white border-b border-gray-200 shrink-0 z-40">
                <div className="max-w-screen-2xl mx-auto px-4 py-3">
                    <div className="flex items-center justify-between">
                        <div className="flex items-center gap-4">
                            <Link 
                                href="/"
                                className="flex items-center gap-2 text-gray-600 hover:text-gray-900"
                            >
                                <ChevronLeft className="w-5 h-5" />
                                <span className="text-sm">Back</span>
                            </Link>
                            <div className="h-6 w-px bg-gray-200" />
                            <div className="flex items-center gap-2">
                                <div className="w-10 h-10 rounded-xl bg-gray-900 flex items-center justify-center">
                                    <Brain className="w-5 h-5 text-white" />
                                </div>
                                <h1 className="text-xl font-bold text-gray-800">
                                    Memory System
                                </h1>
                            </div>
                        </div>
                        
                        <div className="flex items-center gap-3">
                            {/* Stats */}
                            {stats && (
                                <div className="hidden md:flex items-center gap-2">
                                    <StatsBadge label="Memories" value={stats.memories} icon={FileText} />
                                    <StatsBadge label="Chunks" value={stats.chunks} icon={Sparkles} />
                                    <StatsBadge label="Connections" value={stats.connections} icon={Link2} />
                                    <span className={cn(
                                        'inline-flex items-center gap-1.5 text-xs px-2 py-0.5 rounded-full',
                                        stats.db_connected 
                                            ? 'bg-green-100 text-green-700'
                                            : 'bg-red-100 text-red-600'
                                    )}>
                                        {stats.db_connected ? (
                                            <CheckCircle className="w-3 h-3" />
                                        ) : (
                                            <AlertTriangle className="w-3 h-3" />
                                        )}
                                        {stats.db_connected ? 'Connected' : 'Disconnected'}
                                    </span>
                                </div>
                            )}
                            
                            {/* Actions */}
                            <button
                                onClick={handleRefresh}
                                disabled={isLoading}
                                className="p-2 hover:bg-gray-100 rounded-lg transition-colors text-gray-500 hover:text-gray-700 disabled:opacity-50"
                                title="Refresh"
                            >
                                <RefreshCw className={cn('w-5 h-5', isLoading && 'animate-spin')} />
                            </button>
                            
                            <button
                                onClick={() => setShowCreateModal(true)}
                                className="flex items-center gap-2 px-4 py-2 bg-gray-900 hover:bg-gray-800 text-white font-medium rounded-lg transition-colors"
                            >
                                <Plus className="w-4 h-4" />
                                <span className="hidden sm:inline">New Memory</span>
                            </button>
                        </div>
                    </div>
                </div>
            </header>
            
            {/* Error Banner (DESIGN: status error) */}
            {error && (
                <div className="bg-red-100 border-b border-red-500 px-4 py-3 shrink-0">
                    <div className="max-w-screen-2xl mx-auto flex items-center justify-between">
                        <div className="flex items-center gap-2 text-red-600">
                            <AlertTriangle className="w-5 h-5 flex-shrink-0" />
                            <span className="text-sm">{error}</span>
                        </div>
                        <button
                            onClick={clearError}
                            className="text-red-600 hover:text-red-800 text-sm font-medium"
                        >
                            Dismiss
                        </button>
                    </div>
                </div>
            )}
            
            {/* Main Content: Graph links, Memory Search komplett rechts */}
            <main className="flex-1 w-full p-4 overflow-hidden">
                <div className="flex flex-col lg:flex-row gap-4 h-full">
                    {/* Graph – nimmt restlichen Platz */}
                    <div className="flex-1 min-w-0 h-full">
                        <MemoryGraph 
                            className="h-full"
                            onNodeSelect={(id) => {
                                // Selection handled by store
                            }}
                        />
                    </div>
                    
                    {/* Right Panel – feste Breite, am rechten Rand */}
                    <div className="lg:w-[420px] lg:flex-shrink-0 flex flex-col gap-4 h-full overflow-hidden">
                        <RagQueryPanel 
                            className="flex-1 min-h-0"
                            onSourceClick={(memoryId) => {
                                // Highlight and select in graph
                            }}
                        />
                        {selectedNodeId && (
                            <MemoryDetailPanel 
                                className={cn(
                                    "shrink-0 flex flex-col transition-all duration-300",
                                    detailsExpanded ? "h-[60%]" : "h-[52px]"
                                )}
                                onToggleExpand={setDetailsExpanded}
                                onClose={() => setDetailsExpanded(true)}
                            />
                        )}
                    </div>
                </div>
            </main>
            
            {/* Create Memory Modal */}
            <CreateMemoryModal
                isOpen={showCreateModal}
                onClose={() => setShowCreateModal(false)}
                onCreate={handleCreateMemory}
            />
        </div>
    );
}
