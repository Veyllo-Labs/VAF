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

import React, { useState } from 'react';
import { useMemoryStore, RagSource } from './stores/memoryStore';
import {
    Search, Loader2, X,
    FileText, ExternalLink, AlertCircle
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
        highlightNodes,
        selectMemory,
    } = useMemoryStore();

    const [localQuery, setLocalQuery] = useState('');

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        if (!localQuery.trim() || isQuerying) return;

        await searchMemories(localQuery.trim(), 10);
    };

    const handleClear = () => {
        setLocalQuery('');
        clearRagResult();
    };

    const handleSourceClick = (source: RagSource) => {
        // Highlight this source in the graph
        highlightNodes([source.memory_id]);
        selectMemory(source.memory_id);
        onSourceClick?.(source.memory_id);
    };

    const sources = ragResult?.sources || [];

    return (
        <div className={cn('bg-white rounded-xl border border-gray-200 overflow-hidden flex flex-col', className)}>
            {/* Header */}
            <div className="px-4 py-3 border-b border-gray-200 bg-gray-50">
                <div className="flex items-center gap-2">
                    <div className="w-10 h-10 rounded-xl bg-gray-900 flex items-center justify-center flex-shrink-0">
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
                        className="px-4 py-2 bg-gray-900 hover:bg-gray-800 text-white font-medium rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2 dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-white dark:shadow-none"
                    >
                        {isQuerying ? (
                            <Loader2 className="w-4 h-4 animate-spin" />
                        ) : (
                            <Search className="w-4 h-4" />
                        )}
                    </button>
                    {sources.length > 0 && (
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

            {/* Results */}
            {!isQuerying && sources.length > 0 && (
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
                                onClick={() => handleSourceClick(source)}
                                className="w-full px-4 py-3 text-left hover:bg-gray-100 border-b border-gray-100 last:border-b-0 transition-colors"
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
            {!isQuerying && sources.length === 0 && !error && (
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
