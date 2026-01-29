'use client';

import React, { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Loader2, CheckCircle, AlertCircle, Terminal, ChevronDown, ChevronUp } from 'lucide-react';
import { cn } from '@/lib/utils';

export interface ToolMessageProps {
    id: string;
    name: string;
    result?: string;
    status: 'running' | 'completed' | 'error';
    startTime?: number;
    endTime?: number;
    args?: string; // Arguments passed to the tool
    onToggle?: (nextExpanded: boolean) => void;
}

export const ToolMessage: React.FC<ToolMessageProps> = ({
    name,
    result,
    status,
    startTime,
    endTime,
    args,
    onToggle
}) => {
    const [isExpanded, setIsExpanded] = useState(false);

    // Auto-expand if running
    // useEffect(() => { if (status === 'running') setIsExpanded(true); }, [status]);

    return (
        <div className="w-full max-w-[85%] ml-[3.25rem] my-2">
            <motion.div
                initial={{ opacity: 0, scale: 0.95 }}
                animate={{ opacity: 1, scale: 1 }}
                layout
                className={cn(
                    "overflow-hidden rounded-lg border bg-background/95 backdrop-blur shadow-sm transition-colors",
                    status === 'running' ? "border-primary/50 shadow-primary/5" : "border-border",
                    status === 'error' ? "border-destructive/50" : ""
                )}
            >
                {/* Header */}
                <div
                    className="flex items-center justify-between p-3 cursor-pointer hover:bg-accent/50 transition-colors"
                    onClick={() => {
                        const nextExpanded = !isExpanded;
                        if (onToggle) onToggle(nextExpanded);
                        setIsExpanded(nextExpanded);
                    }}
                >
                    <div className="flex items-center gap-3">
                        <div className="relative flex h-8 w-8 items-center justify-center rounded-full border bg-muted/50 shrink-0">
                            {status === 'running' && (
                                <Loader2 className="h-4 w-4 animate-spin text-primary" />
                            )}
                            {status === 'completed' && (
                                <CheckCircle className="h-4 w-4 text-green-500" />
                            )}
                            {status === 'error' && (
                                <AlertCircle className="h-4 w-4 text-destructive" />
                            )}
                        </div>
                        <div className="flex flex-col min-w-0">
                            <span className="text-sm font-medium leading-none truncate pr-2">
                                {name}
                            </span>
                            <span className="text-xs text-muted-foreground truncate">
                                {status === 'running' ? 'Executing...' :
                                    status === 'completed' ? 'Completed' : 'Failed'}
                                {endTime && startTime && ` (${((endTime - startTime) / 1000).toFixed(1)}s)`}
                            </span>
                        </div>
                    </div>
                    <div className="flex items-center gap-1">
                        <button className="rounded-md p-1 hover:bg-background text-muted-foreground">
                            {isExpanded ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
                        </button>
                    </div>
                </div>

                {/* Details */}
                <AnimatePresence>
                    {isExpanded && (
                        <motion.div
                            initial={{ height: 0 }}
                            animate={{ height: "auto" }}
                            exit={{ height: 0 }}
                            className="overflow-hidden bg-muted/30"
                        >
                            <div className="p-3 pt-0 text-xs font-mono text-muted-foreground break-all">
                                {/* Arguments */}
                                {args && (
                                    <div className="mb-2">
                                        <div className="flex items-center gap-1 opacity-70 mb-1 font-semibold">
                                            <span>Input</span>
                                        </div>
                                        <div className="bg-background/50 p-2 rounded border border-border/50 whitespace-pre-wrap">
                                            {args}
                                        </div>
                                    </div>
                                )}

                                {/* Output */}
                                <div className="flex items-center gap-1 opacity-70 mb-1 font-semibold">
                                    <Terminal className="h-3 w-3" />
                                    <span>Output</span>
                                </div>
                                {result ? (
                                    <div className="max-h-60 overflow-y-auto whitespace-pre-wrap rounded bg-background p-2 border">
                                        {result}
                                    </div>
                                ) : (
                                    <span className="italic">Waiting for output...</span>
                                )}
                            </div>
                        </motion.div>
                    )}
                </AnimatePresence>
            </motion.div>
        </div>
    );
};
