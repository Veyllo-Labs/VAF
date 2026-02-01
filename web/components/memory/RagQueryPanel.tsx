'use client';

/**
 * RAG Query Panel - Query memories with AI-powered retrieval.
 * 
 * Features:
 * - Query input with streaming responses
 * - Source citations with relevance scores
 * - Click-to-highlight sources in graph
 * - Response formatting
 */

import React, { useState, useRef, useEffect } from 'react';
import { useMemoryStore, RagSource } from './stores/memoryStore';
import { 
    Search, Send, Loader2, X, ChevronDown, ChevronUp,
    FileText, ExternalLink, Sparkles, AlertCircle
} from 'lucide-react';
import { cn } from '@/lib/utils';

interface RagQueryPanelProps {
    className?: string;
    onSourceClick?: (memoryId: string) => void;
}

export default function RagQueryPanel({ className, onSourceClick }: RagQueryPanelProps) {
    const {
        ragQuery,
        setRagQuery,
        ragResult,
        ragSources,
        isQuerying,
        streamingAnswer,
        error,
        queryRagStream,
        clearRagResult,
        highlightNodes,
        selectMemory,
    } = useMemoryStore();
    
    const [localQuery, setLocalQuery] = useState('');
    const [showSources, setShowSources] = useState(true);
    const inputRef = useRef<HTMLInputElement>(null);
    const answerRef = useRef<HTMLDivElement>(null);
    
    // Auto-scroll answer as it streams
    useEffect(() => {
        if (answerRef.current && (streamingAnswer || ragResult?.answer)) {
            answerRef.current.scrollTop = answerRef.current.scrollHeight;
        }
    }, [streamingAnswer, ragResult?.answer]);
    
    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        if (!localQuery.trim() || isQuerying) return;
        
        await queryRagStream(localQuery.trim(), 5);
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
    
    const currentAnswer = streamingAnswer || ragResult?.answer || '';
    const sources = ragResult?.sources || [];
    
    return (
        <div className={cn('bg-white rounded-xl border border-gray-200 overflow-hidden flex flex-col', className)}>
            {/* Header (DESIGN: section header, no indigo) */}
            <div className="px-4 py-3 border-b border-gray-200 bg-gray-50">
                <div className="flex items-center gap-2">
                    <div className="w-10 h-10 rounded-xl bg-gray-900 flex items-center justify-center flex-shrink-0">
                        <Sparkles className="w-5 h-5 text-white" />
                    </div>
                    <div>
                        <h3 className="text-lg font-semibold text-gray-900">Memory Search</h3>
                        <p className="text-xs text-gray-500 mt-0.5">
                            Ask questions about your memories using AI-powered retrieval
                        </p>
                    </div>
                </div>
            </div>
            
            {/* Query Input */}
            <form onSubmit={handleSubmit} className="p-4 border-b border-gray-200">
                <div className="flex gap-2">
                    <div className="relative flex-1">
                        <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
                        <input
                            ref={inputRef}
                            type="text"
                            value={localQuery}
                            onChange={(e) => setLocalQuery(e.target.value)}
                            placeholder="Ask a question about your memories..."
                            disabled={isQuerying}
                            className="w-full pl-10 pr-4 py-2.5 rounded-xl bg-white border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-transparent disabled:bg-gray-50"
                        />
                    </div>
                    <button
                        type="submit"
                        disabled={!localQuery.trim() || isQuerying}
                        className="px-4 py-2 bg-gray-900 hover:bg-gray-800 text-white font-medium rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
                    >
                        {isQuerying ? (
                            <Loader2 className="w-4 h-4 animate-spin" />
                        ) : (
                            <Send className="w-4 h-4" />
                        )}
                    </button>
                    {(currentAnswer || sources.length > 0) && (
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
            
            {/* Results */}
            {(currentAnswer || isQuerying) && (
                <div className="flex-1 overflow-hidden flex flex-col">
                    {/* Answer */}
                    <div 
                        ref={answerRef}
                        className="flex-1 p-4 overflow-y-auto bg-gray-50"
                    >
                        <div className="prose prose-sm max-w-none">
                            {currentAnswer ? (
                                <div className="whitespace-pre-wrap text-gray-700">
                                    {currentAnswer}
                                    {isQuerying && (
                                        <span className="inline-block w-2 h-4 bg-gray-900 animate-pulse ml-1" />
                                    )}
                                </div>
                            ) : isQuerying ? (
                                <div className="flex items-center gap-2 text-gray-500">
                                    <Loader2 className="w-4 h-4 animate-spin" />
                                    <span>Searching memories...</span>
                                </div>
                            ) : null}
                        </div>
                    </div>
                    
                    {/* Sources */}
                    {sources.length > 0 && (
                        <div className="border-t border-gray-200">
                            <button
                                onClick={() => setShowSources(!showSources)}
                                className="w-full px-4 py-2 flex items-center justify-between bg-gray-100 hover:bg-gray-200 text-sm"
                            >
                                <span className="font-medium text-gray-700">
                                    Sources ({sources.length})
                                </span>
                                {showSources ? (
                                    <ChevronUp className="w-4 h-4 text-gray-500" />
                                ) : (
                                    <ChevronDown className="w-4 h-4 text-gray-500" />
                                )}
                            </button>
                            
                            {showSources && (
                                <div className="max-h-[200px] overflow-y-auto">
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
                                                        <FileText className="w-3 h-3 text-gray-400" />
                                                        <span className="text-sm font-medium text-gray-800 truncate">
                                                            {source.metadata?.title || 'Untitled'}
                                                        </span>
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
                            )}
                        </div>
                    )}
                </div>
            )}
            
            {/* Empty state */}
            {!currentAnswer && !isQuerying && sources.length === 0 && (
                <div className="flex-1 flex items-center justify-center p-8">
                    <div className="text-center">
                        <Search className="w-10 h-10 text-gray-300 mx-auto mb-3" />
                        <p className="text-sm text-gray-500">
                            Enter a question to search your memories
                        </p>
                        <p className="text-xs text-gray-400 mt-1">
                            AI will find relevant content and generate an answer
                        </p>
                    </div>
                </div>
            )}
        </div>
    );
}
