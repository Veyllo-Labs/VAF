'use client';

import React, { useState, useEffect, useRef } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { CheckCircle, AlertCircle, Terminal, ChevronDown, ChevronRight } from 'lucide-react';
import { cn } from '@/lib/utils';

export interface ToolMessageProps {
    id: string;
    name: string;
    result?: string;
    status: 'running' | 'completed' | 'error';
    startTime?: number;
    endTime?: number;
    args?: string;
    onToggle?: (nextExpanded: boolean) => void;
    onToggleScroll?: (update: () => void) => void;
}

const INPUT_PRIORITY = [
    'query', 'q', 'search_query', 'search',
    'content', 'text', 'message', 'body',
    'prompt', 'instruction', 'input',
    'name', 'title', 'description',
    'path', 'file_path', 'url',
    'command', 'cmd', 'topic', 'subject',
];

function extractMainInput(argsJson: string | undefined): string {
    if (!argsJson) return '';
    try {
        const obj = JSON.parse(argsJson) as Record<string, unknown>;
        for (const key of INPUT_PRIORITY) {
            const v = obj[key];
            if (typeof v === 'string' && v.trim()) return v.trim().slice(0, 120);
        }
        for (const v of Object.values(obj)) {
            if (typeof v === 'string' && v.trim()) return v.trim().slice(0, 120);
        }
    } catch { /* empty */ }
    return '';
}

/** Typewriter text — no dot, just characters typing in, black text */
function TypewriterText({ input }: { input: string }) {
    const [typedLen, setTypedLen] = useState(0);
    const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

    useEffect(() => {
        setTypedLen(0);
        if (!input) return;
        let i = 0;
        const tick = () => {
            i++;
            setTypedLen(i);
            if (i < input.length) {
                timerRef.current = setTimeout(tick, 22 + Math.random() * 16);
            }
        };
        timerRef.current = setTimeout(tick, 60);
        return () => { if (timerRef.current) clearTimeout(timerRef.current); };
    }, [input]);

    return (
        <div className="px-2 py-1.5 rounded border border-border/40 bg-background/60">
            <span className="font-mono text-[11px] text-foreground break-all leading-relaxed">
                {input.slice(0, typedLen)}
                {typedLen < input.length && (
                    <span className="inline-block w-[1px] h-[11px] bg-foreground align-middle ml-[1px] animate-pulse" />
                )}
            </span>
        </div>
    );
}

export const ToolMessage: React.FC<ToolMessageProps> = ({
    name,
    result,
    status,
    startTime,
    endTime,
    args,
    onToggle,
    onToggleScroll
}) => {
    const [isExpanded, setIsExpanded] = useState(false);
    const animInput = extractMainInput(args);

    // visualStatus lags behind the real status by 450ms on completion so the
    // cursor return-to-avatar animation finishes before the green checkmark appears
    const [visualStatus, setVisualStatus] = useState<'running' | 'completed' | 'error'>(status);

    useEffect(() => {
        if (status === 'running') {
            setVisualStatus('running');
            setIsExpanded(true);
        } else {
            const t = setTimeout(() => setVisualStatus(status), 450);
            return () => clearTimeout(t);
        }
    }, [status]);

    useEffect(() => {
        if (visualStatus === 'completed' || visualStatus === 'error') {
            const t = setTimeout(() => setIsExpanded(false), 1500);
            return () => clearTimeout(t);
        }
    }, [visualStatus]);

    return (
        <div className="w-full my-2">
            <style>{`
                @keyframes agentDotPulse {
                    0%,100% { transform: scale(1);    opacity: 1; }
                    50%     { transform: scale(1.35); opacity: 0.7; }
                }
            `}</style>

            <motion.div
                initial={{ opacity: 0, scale: 0.95 }}
                animate={{ opacity: 1, scale: 1 }}
                className={cn(
                    "tool-message-card overflow-hidden rounded-lg border bg-background/95 backdrop-blur shadow-sm transition-colors border-border",
                    status === 'error' ? "border-destructive/50" : ""
                )}
            >
                {/* Header */}
                <div
                    className="flex items-center justify-between p-3 cursor-pointer hover:bg-accent/50 transition-colors"
                    onClick={() => {
                        const nextExpanded = !isExpanded;
                        if (onToggle) onToggle(nextExpanded);
                        if (onToggleScroll) {
                            onToggleScroll(() => setIsExpanded(nextExpanded));
                        } else {
                            setIsExpanded(nextExpanded);
                        }
                    }}
                >
                    <div className="flex items-center gap-3">
                        <div className="relative flex h-8 w-8 items-center justify-center rounded-full border bg-muted/50 shrink-0">
                            {visualStatus === 'running' && (
                                <span
                                    data-agent-tool-dot
                                    className="rounded-full"
                                    style={{
                                        width: 12, height: 12,
                                        backgroundColor: '#000000',
                                        boxShadow: '0 0 8px 3px rgba(0,0,0,0.3)',
                                        animation: 'agentDotPulse 1.1s ease-in-out infinite',
                                    }}
                                />
                            )}
                            {visualStatus === 'completed' && <CheckCircle className="h-4 w-4 text-green-500" />}
                            {visualStatus === 'error'     && <AlertCircle className="h-4 w-4 text-destructive" />}
                        </div>

                        <div className="flex flex-col min-w-0">
                            <span className="text-sm font-medium leading-none truncate pr-2">{name}</span>
                            <span className="text-xs text-muted-foreground truncate">
                                {visualStatus === 'running'   ? 'Running…' :
                                 visualStatus === 'completed' ? 'Completed' : 'Failed'}
                                {endTime && startTime && ` (${((endTime - startTime) / 1000).toFixed(1)}s)`}
                            </span>
                        </div>
                    </div>

                    <div className="flex items-center gap-1">
                        <button className="rounded-md p-1 hover:bg-background text-muted-foreground">
                            {isExpanded ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
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

                                {/* Typewriter animation while running */}
                                {status === 'running' && animInput && (
                                    <div className="mb-2">
                                        <TypewriterText input={animInput} />
                                    </div>
                                )}

                                {/* Static args once done */}
                                {status !== 'running' && args && (
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
                                    <span className="italic">Waiting for output…</span>
                                )}
                            </div>
                        </motion.div>
                    )}
                </AnimatePresence>
            </motion.div>
        </div>
    );
};
