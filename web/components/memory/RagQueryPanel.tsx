'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

/**
 * Memory Search Panel - Search and highlight memories.
 *
 * Features:
 * - Simple semantic search (no LLM required)
 * - Source citations with relevance scores
 * - Click-to-highlight sources in graph
 * - Click to select memory for editing/viewing
 */

import React, { useMemo, useState } from 'react';
import { connectedMemoriesForTag, useMemoryStore, TYPE_LABELS } from './stores/memoryStore';
import {
    Search, Loader2, X,
    FileText, ExternalLink, AlertCircle, Hash
} from 'lucide-react';
import { cn } from '@/lib/utils';


interface RagQueryPanelProps {
    className?: string;
    onSourceClick?: (memoryId: string) => void;
}

export default function RagQueryPanel({ className, onSourceClick }: RagQueryPanelProps) {
    const {
        ragResult,
        isQuerying,
        error,
        searchMemories,
        clearRagResult,
        selectMemory,
        nodes,
        edges,
        selectedNodeId,
        activeTagNodeId,
        activeTagLabel,
        clearTagResults,
    } = useMemoryStore();

    const [localQuery, setLocalQuery] = useState('');

    // A clicked tag fills this panel with its memories. The list is keyed on
    // the tag, NOT on the current selection, so clicking through its entries
    // keeps it on screen; only a new search or another tag replaces it.
    const tagMemories = useMemo(
        () => connectedMemoriesForTag(nodes, edges, activeTagNodeId),
        [nodes, edges, activeTagNodeId],
    );

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        if (!localQuery.trim() || isQuerying) return;

        await searchMemories(localQuery.trim(), 10);
    };

    const handleClear = () => {
        setLocalQuery('');
        if (activeTagNodeId) clearTagResults();
        else clearRagResult();
    };

    // One handler for both list kinds. Deliberately no highlightNodes([id]):
    // that overwrites ragSources with a single id, collapsing the highlighted
    // set to the row just opened (and narrowing the next fetchGraph highlight).
    // selectMemory sets selectedNodeId itself.
    const handleMemoryClick = (memoryId: string) => {
        selectMemory(memoryId);
        onSourceClick?.(memoryId);
    };

    const sources = ragResult?.sources || [];


    return (
        <div className={cn('bg-white rounded-xl border border-gray-200 overflow-hidden flex flex-col', className)}>
            {/* Header */}
            <div className="px-4 py-3 border-b border-gray-200 bg-gray-50">
                <div className="flex items-center gap-2">
                    <div className="w-10 h-10 rounded-xl bg-gray-900 dark:bg-[#2e2e2e] flex items-center justify-center flex-shrink-0">
                        <Search className="w-5 h-5 text-white" />
                    </div>
                    <div>
                        <h3 className="text-lg font-semibold text-gray-900">Memory Search</h3>
                        <p className="text-xs text-gray-500 mt-0.5">
                            Search your memories by keyword, tag or phrase
                        </p>
                    </div>
                </div>
            </div>

            {/* Search Input */}
            <form onSubmit={handleSubmit} className="p-4 border-b border-gray-200">
                <div className="flex gap-2">
                    <div className="relative flex-1">
                        <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
                        <input
                            type="text"
                            value={localQuery}
                            onChange={(e) => setLocalQuery(e.target.value)}
                            placeholder="Search memories... (e.g. vaf, project, meeting)"
                            disabled={isQuerying}
                            className="w-full pl-10 pr-4 py-2.5 rounded-xl bg-white border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-transparent disabled:bg-gray-50"
                        />
                    </div>
                    <button
                        type="submit"
                        disabled={!localQuery.trim() || isQuerying}
                        className="px-4 py-2 bg-gray-900 hover:bg-gray-800 text-white font-medium rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2 dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-[#f5f5f5] dark:shadow-none"
                    >
                        {isQuerying ? (
                            <Loader2 className="w-4 h-4 animate-spin" />
                        ) : (
                            <Search className="w-4 h-4" />
                        )}
                    </button>
                    {(sources.length > 0 || activeTagNodeId) && (
                        <button
                            type="button"
                            onClick={handleClear}
                            className="p-2 hover:bg-gray-100 rounded-lg transition-colors text-gray-500 hover:text-gray-700"
                            title="Clear"
                        >
                            <X className="w-4 h-4" />
                        </button>
                    )}
                </div>
            </form>

            {/* Error display */}
            {error && (
                <div className="px-4 py-3 bg-red-100 border-b border-red-500">
                    <div className="flex items-center gap-2 text-red-600">
                        <AlertCircle className="w-4 h-4 flex-shrink-0" />
                        <span className="text-sm">{error}</span>
                    </div>
                </div>
            )}

            {/* Loading */}
            {isQuerying && (
                <div className="p-4 flex items-center gap-2 text-gray-500">
                    <Loader2 className="w-4 h-4 animate-spin" />
                    <span>Searching memories...</span>
                </div>
            )}

            {/* Tag result set */}
            {!isQuerying && activeTagNodeId && (
                <div className="flex-1 overflow-hidden flex flex-col">
                    <div className="px-4 py-2 bg-gray-100 border-b border-gray-200 flex items-center gap-2">
                        <Hash className="w-3.5 h-3.5 text-purple-600 flex-shrink-0" />
                        <span className="font-medium text-gray-700 text-sm truncate min-w-0">
                            {activeTagLabel.replace(/^#/, '')}
                        </span>
                        <span className="text-gray-500 text-sm flex-shrink-0">
                            ({tagMemories.length})
                        </span>
                        <button
                            type="button"
                            onClick={clearTagResults}
                            aria-label="Clear tag selection"
                            title="Clear tag selection"
                            className="ml-auto p-1 rounded hover:bg-gray-200 text-gray-500 hover:text-gray-700 flex-shrink-0"
                        >
                            <X className="w-3.5 h-3.5" />
                        </button>
                    </div>

                    <div className="flex-1 overflow-y-auto">
                        {tagMemories.length > 0 ? tagMemories.map((memory) => (
                            <button
                                key={memory.id}
                                onClick={() => handleMemoryClick(memory.id)}
                                aria-current={memory.id === selectedNodeId ? 'true' : undefined}
                                className={cn(
                                    'w-full px-4 py-3 text-left border-b border-gray-100 last:border-b-0 transition-colors flex items-center gap-3 border-l-2',
                                    memory.id === selectedNodeId
                                        ? 'bg-gray-100 dark:bg-[#3a3a3a] border-l-purple-500'
                                        : 'border-l-transparent hover:bg-gray-100'
                                )}
                            >
                                <FileText className="w-4 h-4 text-gray-400 flex-shrink-0" />
                                <span className="text-sm text-gray-700 truncate flex-1 min-w-0">
                                    {memory.label || 'Untitled'}
                                </span>
                                {memory.type && (
                                    <span className="text-[10px] px-1.5 py-0.5 bg-gray-200 text-gray-600 rounded flex-shrink-0">
                                        {TYPE_LABELS[memory.type] ?? memory.type}
                                    </span>
                                )}
                            </button>
                        )) : (
                            <p className="text-xs text-gray-400 text-center py-6">
                                No memories carry this tag
                            </p>
                        )}
                    </div>
                </div>
            )}

            {/* Results */}
            {!isQuerying && !activeTagNodeId && sources.length > 0 && (
                <div className="flex-1 overflow-hidden flex flex-col">
                    <div className="px-4 py-2 bg-gray-100 border-b border-gray-200">
                        <span className="font-medium text-gray-700 text-sm">
                            Found {sources.length} matching memories
                        </span>
                    </div>

                    <div className="flex-1 overflow-y-auto">
                        {sources.map((source, idx) => (
                            <button
                                key={source.chunk_id}
                                onClick={() => handleMemoryClick(source.memory_id)}
                                aria-current={selectedNodeId === source.memory_id ? 'true' : undefined}
                                className={cn(
                                    'w-full px-4 py-3 text-left border-b border-gray-100 last:border-b-0 transition-colors border-l-2',
                                    // A search set lists CHUNKS, so two rows of the
                                    // same memory can both be marked - truthful.
                                    selectedNodeId === source.memory_id
                                        ? 'bg-gray-100 dark:bg-[#3a3a3a] border-l-purple-500'
                                        : 'border-l-transparent hover:bg-gray-100'
                                )}
                            >
                                <div className="flex items-start gap-3">
                                    <div className="flex-shrink-0 w-8 h-8 rounded-xl bg-gray-200 flex items-center justify-center">
                                        <span className="text-xs font-medium text-gray-700">
                                            {idx + 1}
                                        </span>
                                    </div>
                                    <div className="flex-1 min-w-0">
                                        <div className="flex items-center gap-2 mb-1">
                                            <span className={cn(
                                                'px-1.5 py-0.5 rounded text-[10px] font-medium',
                                                source.score >= 0.8
                                                    ? 'bg-green-100 text-green-700'
                                                    : source.score >= 0.6
                                                    ? 'bg-yellow-100 text-yellow-700'
                                                    : 'bg-gray-100 text-gray-600'
                                            )}>
                                                {Math.round(source.score * 100)}%
                                            </span>
                                            {source.metadata?.tags && source.metadata.tags.length > 0 && source.metadata.tags.slice(0, 3).map((tag: string) => (
                                                <span key={tag} className="text-[10px] px-1.5 py-0.5 bg-blue-50 text-blue-600 rounded dark:bg-[#3a3a3a] dark:text-gray-100">
                                                    #{tag}
                                                </span>
                                            ))}
                                        </div>
                                        <p className="text-xs text-gray-600 line-clamp-2">
                                            {source.text}
                                        </p>
                                    </div>
                                    <ExternalLink className="w-4 h-4 text-gray-400 flex-shrink-0" />
                                </div>
                            </button>
                        ))}
                    </div>
                </div>
            )}

            {/* Empty state */}
            {!isQuerying && !activeTagNodeId && sources.length === 0 && !error && (
                <div className="flex-1 flex items-center justify-center p-8">
                    <div className="text-center">
                        <Search className="w-10 h-10 text-gray-300 mx-auto mb-3" />
                        <p className="text-sm text-gray-500">
                            Enter a search term to find memories
                        </p>
                        <p className="text-xs text-gray-400 mt-1">
                            Results will be highlighted in the graph
                        </p>
                    </div>
                </div>
            )}
        </div>
    );
}
