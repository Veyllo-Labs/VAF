'use client';

import React, { useState, useEffect } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Loader2, CheckCircle, AlertCircle, Terminal, ChevronDown, ChevronRight, X } from 'lucide-react';
import { cn } from '@/lib/utils';

export interface ToolState {
    id: string;
    name: string;
    status: 'running' | 'completed' | 'error';
    result?: string;
    startTime: number;
    endTime?: number;
}

interface ActiveToolsPanelProps {
    tools: ToolState[];
    onClear?: (id: string) => void;
}

export const ActiveToolsPanel: React.FC<ActiveToolsPanelProps> = ({ tools, onClear }) => {
    const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());

    // Auto-expand new tools
    useEffect(() => {
        tools.forEach(tool => {
            if (tool.status === 'running') {
                // Optional: Auto-expand running tools? 
                // setExpandedIds(prev => new Set(prev).add(tool.id));
            }
        });
    }, [tools]);

    const toggleExpand = (id: string) => {
        setExpandedIds(prev => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
        });
    };

    if (tools.length === 0) return null;

    return (
        <div className="w-full max-w-[85%] ml-[3.25rem] space-y-2">
            <AnimatePresence mode="popLayout">
                {tools.map((tool) => (
                    <motion.div
                        key={tool.id}
                        initial={{ opacity: 0, x: 20, scale: 0.95 }}
                        animate={{ opacity: 1, x: 0, scale: 1 }}
                        exit={{ opacity: 0, x: 20, scale: 0.95 }}
                        transition={{ type: "spring", stiffness: 300, damping: 25 }}
                        layout
                        className={cn(
                            "overflow-hidden rounded-lg border bg-background/95 backdrop-blur shadow-sm transition-colors",
                            tool.status === 'running' ? "border-primary/50 shadow-primary/5" : "border-border",
                            tool.status === 'error' ? "border-destructive/50" : ""
                        )}
                    >
                        {/* Header */}
                        <div
                            className="flex items-center justify-between p-3 cursor-pointer hover:bg-accent/50 transition-colors"
                            onClick={() => toggleExpand(tool.id)}
                        >
                            <div className="flex items-center gap-3">
                                <div className="relative flex h-8 w-8 items-center justify-center rounded-full border bg-muted/50">
                                    {tool.status === 'running' && (
                                        <Loader2 className="h-4 w-4 animate-spin text-primary" />
                                    )}
                                    {tool.status === 'completed' && (
                                        <CheckCircle className="h-4 w-4 text-green-500" />
                                    )}
                                    {tool.status === 'error' && (
                                        <AlertCircle className="h-4 w-4 text-destructive" />
                                    )}
                                </div>
                                <div className="flex flex-col">
                                    <span className="text-sm font-medium leading-none">
                                        {tool.name}
                                    </span>
                                    <span className="text-xs text-muted-foreground">
                                        {tool.status === 'running' ? 'Executing...' :
                                            tool.status === 'completed' ? 'Completed' : 'Failed'}
                                        {tool.endTime && ` (${((tool.endTime - tool.startTime) / 1000).toFixed(1)}s)`}
                                    </span>
                                </div>
                            </div>
                            <div className="flex items-center gap-1">
                                <button
                                    onClick={(e) => { e.stopPropagation(); toggleExpand(tool.id); }}
                                    className="rounded-md p-1 hover:bg-background text-muted-foreground"
                                >
                                    {expandedIds.has(tool.id) ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
                                </button>
                                {onClear && tool.status !== 'running' && (
                                    <button
                                        onClick={(e) => { e.stopPropagation(); onClear(tool.id); }}
                                        className="rounded-md p-1 hover:bg-background text-muted-foreground hover:text-foreground"
                                    >
                                        <X className="h-3 w-3" />
                                    </button>
                                )}
                            </div>
                        </div>

                        {/* Details */}
                        <AnimatePresence>
                            {expandedIds.has(tool.id) && (
                                <motion.div
                                    initial={{ height: 0 }}
                                    animate={{ height: "auto" }}
                                    exit={{ height: 0 }}
                                    className="overflow-hidden bg-muted/30"
                                >
                                    <div className="p-3 pt-0 text-xs font-mono text-muted-foreground break-all">
                                        {/* Placeholder for args or partial result if we had them */}
                                        <div className="flex items-center gap-1 opacity-70 mb-1">
                                            <Terminal className="h-3 w-3" />
                                            <span>Output</span>
                                        </div>
                                        {tool.result ? (
                                            <div className="max-h-32 overflow-y-auto whitespace-pre-wrap rounded bg-background p-2 border">
                                                {tool.result}
                                            </div>
                                        ) : (
                                            <span className="italic">Waiting for output...</span>
                                        )}
                                    </div>
                                </motion.div>
                            )}
                        </AnimatePresence>
                    </motion.div>
                ))}
            </AnimatePresence>
        </div>
    );
};
