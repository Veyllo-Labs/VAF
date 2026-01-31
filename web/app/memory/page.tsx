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
    Plus, RefreshCw, Database, Brain, Link2, 
    ChevronLeft, Settings, AlertTriangle, CheckCircle,
    FileText, Sparkles
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
        <div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4">
            <div className="bg-white rounded-xl shadow-xl max-w-2xl w-full max-h-[90vh] overflow-hidden">
                <div className="px-6 py-4 border-b border-gray-200 flex items-center justify-between">
                    <div className="flex items-center gap-2">
                        <Plus className="w-5 h-5 text-indigo-600" />
                        <h2 className="text-lg font-semibold text-gray-800">Create Memory</h2>
                    </div>
                    <button
                        onClick={onClose}
                        className="p-2 hover:bg-gray-100 rounded-lg text-gray-500"
                    >
                        <span className="sr-only">Close</span>
                        ×
                    </button>
                </div>
                
                <form onSubmit={handleSubmit} className="p-6 space-y-4">
                    <div>
                        <label className="block text-sm font-medium text-gray-700 mb-1">
                            Title
                        </label>
                        <input
                            type="text"
                            value={title}
                            onChange={(e) => setTitle(e.target.value)}
                            placeholder="Memory title (optional)"
                            className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500"
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
                            className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500"
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
                                className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500"
                            />
                        </div>
                        <div>
                            <label className="block text-sm font-medium text-gray-700 mb-1">
                                Type
                            </label>
                            <select
                                value={type}
                                onChange={(e) => setType(e.target.value)}
                                className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500"
                            >
                                <option value="note">Note</option>
                                <option value="document">Document</option>
                                <option value="code">Code</option>
                                <option value="conversation">Conversation</option>
                            </select>
                        </div>
                    </div>
                    
                    <div className="flex justify-end gap-3 pt-4">
                        <button
                            type="button"
                            onClick={onClose}
                            className="px-4 py-2 text-gray-700 bg-gray-100 rounded-lg hover:bg-gray-200"
                        >
                            Cancel
                        </button>
                        <button
                            type="submit"
                            disabled={!content.trim() || isCreating}
                            className="px-4 py-2 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 disabled:opacity-50 flex items-center gap-2"
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

// Stats Badge Component
function StatsBadge({ label, value, icon: Icon }: { label: string; value: number | string; icon: React.ElementType }) {
    return (
        <div className="flex items-center gap-2 px-3 py-1.5 bg-white rounded-lg border border-gray-200">
            <Icon className="w-4 h-4 text-gray-400" />
            <span className="text-xs text-gray-500">{label}:</span>
            <span className="text-sm font-medium text-gray-800">{value}</span>
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
        <div className="min-h-screen bg-gray-50">
            {/* Header */}
            <header className="bg-white border-b border-gray-200 sticky top-0 z-40">
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
                            <div className="h-6 w-px bg-gray-300" />
                            <div className="flex items-center gap-2">
                                <Brain className="w-6 h-6 text-indigo-600" />
                                <h1 className="text-xl font-semibold text-gray-800">
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
                                    <div className={cn(
                                        'flex items-center gap-1.5 px-2 py-1 rounded-full text-xs',
                                        stats.db_connected 
                                            ? 'bg-green-100 text-green-700'
                                            : 'bg-red-100 text-red-700'
                                    )}>
                                        {stats.db_connected ? (
                                            <CheckCircle className="w-3 h-3" />
                                        ) : (
                                            <AlertTriangle className="w-3 h-3" />
                                        )}
                                        {stats.db_connected ? 'Connected' : 'Disconnected'}
                                    </div>
                                </div>
                            )}
                            
                            {/* Actions */}
                            <button
                                onClick={handleRefresh}
                                disabled={isLoading}
                                className="p-2 text-gray-600 hover:text-gray-900 hover:bg-gray-100 rounded-lg disabled:opacity-50"
                                title="Refresh"
                            >
                                <RefreshCw className={cn('w-5 h-5', isLoading && 'animate-spin')} />
                            </button>
                            
                            <button
                                onClick={() => setShowCreateModal(true)}
                                className="flex items-center gap-2 px-4 py-2 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 transition-colors"
                            >
                                <Plus className="w-4 h-4" />
                                <span className="hidden sm:inline">New Memory</span>
                            </button>
                        </div>
                    </div>
                </div>
            </header>
            
            {/* Error Banner */}
            {error && (
                <div className="bg-red-50 border-b border-red-200 px-4 py-3">
                    <div className="max-w-screen-2xl mx-auto flex items-center justify-between">
                        <div className="flex items-center gap-2 text-red-700">
                            <AlertTriangle className="w-5 h-5" />
                            <span className="text-sm">{error}</span>
                        </div>
                        <button
                            onClick={clearError}
                            className="text-red-600 hover:text-red-800 text-sm"
                        >
                            Dismiss
                        </button>
                    </div>
                </div>
            )}
            
            {/* Main Content */}
            <main className="max-w-screen-2xl mx-auto p-4">
                <div className="grid grid-cols-1 lg:grid-cols-5 gap-4 h-[calc(100vh-140px)]">
                    {/* Graph - 3 columns */}
                    <div className="lg:col-span-3 h-full">
                        <MemoryGraph 
                            className="h-full"
                            onNodeSelect={(id) => {
                                // Selection handled by store
                            }}
                        />
                    </div>
                    
                    {/* Right Panel - 2 columns */}
                    <div className="lg:col-span-2 flex flex-col gap-4 h-full overflow-hidden">
                        {/* RAG Query Panel */}
                        <RagQueryPanel 
                            className="flex-1 min-h-[300px]"
                            onSourceClick={(memoryId) => {
                                // Highlight and select in graph
                            }}
                        />
                        
                        {/* Memory Details */}
                        {selectedNodeId && (
                            <MemoryDetailPanel 
                                className="max-h-[40%]"
                                onClose={() => {}}
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
