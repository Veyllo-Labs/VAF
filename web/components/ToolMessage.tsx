'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import React, { useState, useEffect, useRef, useMemo } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Check, AlertCircle, Terminal, ChevronRight, Activity, Skull, Loader2 } from 'lucide-react';
import { cn, getApiBase } from '@/lib/utils';

/** A sub-agent (librarian/research/document/coding/browser) — these run as supervised units. */
const SUBAGENT_RE = /(?:^|[^a-z])(librarian|research|document|coding|browser)_agent(?:$|[^a-z])/;

interface SupervisorUnit {
    task_id: string;
    agent_type: string;
    status: string;
    runtime_s?: number | null;
    heartbeat_age_s?: number | null;
    stale?: boolean;
}

function fmtDuration(s?: number | null): string {
    if (s == null) return '—';
    if (s < 60) return `${Math.round(s)}s`;
    const m = Math.floor(s / 60);
    const sec = Math.round(s % 60);
    return `${m}m ${sec}s`;
}

/** Short right-aligned result counter (mockup ".stat"): line count, else KB, else a neutral label. */
function resultStat(result?: string): string {
    if (!result || !result.trim()) return 'Fertig';
    const lines = result.trim().split('\n').filter(l => l.trim()).length;
    if (lines > 1) return `${lines} Zeilen`;
    if (result.length >= 1024) return `${(result.length / 1024).toFixed(1)} KB`;
    return 'Fertig';
}

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

/** Compact one-value rendering for the structured-args fallback. Arrays/objects are summarized so a
 *  big payload never floods the card; strings are truncated. */
function compactValue(v: unknown): string {
    if (v === null || v === undefined) return '';
    if (typeof v === 'string') return v.length > 80 ? v.slice(0, 80) + '…' : v;
    if (typeof v === 'number' || typeof v === 'boolean') return String(v);
    if (Array.isArray(v)) return `[${v.length} item${v.length === 1 ? '' : 's'}]`;
    if (typeof v === 'object') return '{…}';
    return '';
}

function extractMainInput(argsJson: string | undefined): string {
    if (!argsJson) return '';
    try {
        const obj = JSON.parse(argsJson) as Record<string, unknown>;
        for (const key of INPUT_PRIORITY) {
            const v = obj[key];
            if (typeof v === 'string' && v.trim()) return v.trim();
        }
        for (const v of Object.values(obj)) {
            if (typeof v === 'string' && v.trim()) return v.trim();
        }
        // No string value (structured args, e.g. update_working_memory: arrays / numbers / booleans).
        // Show a compact key: value summary so the call's input is still visible instead of blank.
        const summary = Object.entries(obj)
            .filter(([, v]) => v !== null && v !== undefined)
            .map(([k, v]) => `${k}: ${compactValue(v)}`)
            .filter(s => !s.endsWith(': '))
            .join(', ');
        if (summary) return summary;
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

function workflowBadge(args: string | undefined): { label: string; color: string } | null {
    if (!args) return null;
    try {
        const obj = JSON.parse(args) as Record<string, unknown>;
        if (!('action' in obj)) return null;
        switch (obj.action) {
            case 'run_temp':  return { label: 'Temporary',            color: 'bg-blue-100 text-blue-700 border-blue-200' };
            case 'create':    return { label: 'Persistent Workflow',  color: 'bg-green-100 text-green-700 border-green-200' };
            case 'delete':    return { label: 'Delete',               color: 'bg-red-100 text-red-700 border-red-200' };
            case 'list':      return { label: 'List',                 color: 'bg-gray-100 text-gray-600 border-gray-200' };
            default:          return null;
        }
    } catch { return null; }
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
    const wfBadge = name === 'create_agent_workflow' ? workflowBadge(args) : null;

    // ── Live watchdog: surface a running sub-agent's supervised unit (heartbeat, runtime, kill)
    //    INSIDE its own tool bubble. A spawned sub-agent delegates immediately, so this bubble
    //    flips to "completed" while the subprocess keeps running — therefore this is gated on a
    //    LIVE supervisor unit existing, NOT on the bubble's own status. Matched by task id (from
    //    the delegation marker in the result) when available, else by agent type.
    const isSubAgent = SUBAGENT_RE.test(name.toLowerCase());
    const subAgentTaskId = useMemo(() => {
        const text = String(result || '');
        const m = text.match(/\[SUBAGENT_ASYNC:([^:\]]+)/) || text.match(/Task-?ID:\s*([A-Za-z0-9_-]+)/i);
        return m ? m[1].trim() : null;
    }, [result]);
    const [liveUnit, setLiveUnit] = useState<SupervisorUnit | null>(null);
    const [killing, setKilling] = useState(false);
    // An async sub-agent delegates instantly, so the tool call's own duration is ~0.0s.
    // Capture the supervised unit's real runtime while it is alive, then keep showing it
    // after the unit is gone — otherwise the bubble would read a misleading "(0.0s)".
    const lastRuntimeRef = useRef<number | null>(null);
    useEffect(() => {
        if (liveUnit?.runtime_s != null) lastRuntimeRef.current = liveUnit.runtime_s;
    }, [liveUnit?.runtime_s]);

    useEffect(() => {
        if (!isSubAgent) { setLiveUnit(null); return; }
        let stopped = false;
        let sawUnit = false;
        let polls = 0;
        let id: ReturnType<typeof setInterval> | null = null;
        const finish = () => { stopped = true; if (id) clearInterval(id); };
        const poll = async () => {
            polls += 1;
            try {
                const r = await fetch(`${getApiBase()}/api/supervisor/status`, { credentials: 'include' });
                if (!r.ok) return;
                const d = await r.json();
                const units: SupervisorUnit[] = Array.isArray(d.units) ? d.units : [];
                // Match precisely by task id (from an async delegation marker) when present.
                // The agent-type fallback is for SYNC sub-agents that block this tool call —
                // so only apply it while THIS bubble is still running. Otherwise a completed
                // call (e.g. one bounced by the plan gate, which carries no task id) would
                // adopt a concurrently-running unit of the same type and falsely show as
                // "running" with that unit's watchdog.
                const match = (subAgentTaskId
                    ? units.find((u) => u.task_id === subAgentTaskId)
                    : (status === 'running'
                        ? units.find((u) => (u.agent_type || '').toLowerCase() === name.toLowerCase())
                        : undefined)) || null;
                if (stopped) return;
                setLiveUnit(match);
                if (match) sawUnit = true;
                else if (sawUnit) finish();      // unit was live and is now gone → sub-agent finished
                else if (polls >= 6) finish();    // never appeared (~12 s) → not a tracked unit
            } catch {
                /* transient — keep last */
            }
        };
        poll();
        id = setInterval(poll, 2000);
        return () => { stopped = true; if (id) clearInterval(id); };
    }, [isSubAgent, name, subAgentTaskId, status]);

    const killUnit = async () => {
        if (!liveUnit?.task_id) return;
        setKilling(true);
        try {
            await fetch(`${getApiBase()}/api/supervisor/cancel`, {
                method: 'POST',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ task_id: liveUnit.task_id }),
            });
        } catch {
            /* next poll reflects reality */
        }
        setLiveUnit(null);
        setKilling(false);
    };

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
        // Keep the bubble open while the sub-agent's subprocess is still alive, so its watchdog
        // row stays visible even though the tool call itself already "completed" (delegated).
        if (liveUnit) { setIsExpanded(true); return; }
        if (visualStatus === 'completed' || visualStatus === 'error') {
            const t = setTimeout(() => setIsExpanded(false), 1500);
            return () => clearTimeout(t);
        }
    }, [visualStatus, liveUnit]);

    // While a live sub-agent unit exists, present the bubble as "running" (the delegated tool call
    // reads as completed, but the actual work is still going).
    const headerStatus: 'running' | 'completed' | 'error' = liveUnit ? 'running' : visualStatus;

    // Right-aligned status/result label (mockup ".stat"): "running…" while running, the sub-agent
    // runtime, or a result counter once done. Keeps the existing duration logic, just presented
    // like the mockup instead of a "(0.0s)" subtitle.
    const rt = liveUnit?.runtime_s ?? lastRuntimeRef.current ?? (endTime && startTime ? (endTime - startTime) / 1000 : null);
    const statText =
        headerStatus === 'running'   ? (rt != null ? `running… ${fmtDuration(rt)}` : 'running…')
        : headerStatus === 'error'   ? 'Error'
        : (isSubAgent && rt != null  ? fmtDuration(rt) : resultStat(result));

    return (
        <div className="w-full">
            <style>{`
                @keyframes agentDotPulse {
                    0%,100% { transform: scale(1);    opacity: 1; }
                    50%     { transform: scale(1.35); opacity: 0.7; }
                }
            `}</style>

            <motion.div
                initial={{ opacity: 0, y: 4 }}
                animate={{ opacity: 1, y: 0 }}
                className={cn(
                    "tool-message-card relative overflow-hidden rounded-[11px] border bg-white transition-colors",
                    headerStatus === 'running' ? "border-[#dbe6ff]"
                        : headerStatus === 'error' ? "border-destructive/50"
                        : "border-border"
                )}
            >
                {/* success flash over the whole card on completion (mockup .resultflash) */}
                {visualStatus === 'completed' && (
                    <span
                        aria-hidden
                        className="pointer-events-none absolute inset-0 rounded-[11px]"
                        style={{ boxShadow: '0 0 0 2px rgba(22,163,74,0.5)', animation: 'chatSuccessFlash 0.7s ease-out forwards' }}
                    />
                )}

                {/* Header — single row: dot · name · arg · result-counter (mockup .th) */}
                <div
                    className="flex items-center gap-[9px] px-[11px] py-2 cursor-pointer hover:bg-accent/40 transition-colors"
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
                    <span className={cn(
                        "relative flex h-[22px] w-[22px] shrink-0 items-center justify-center rounded-full border transition-colors",
                        headerStatus === 'completed' ? "border-green-200 bg-green-50" : "border-border bg-muted/50"
                    )}>
                        {headerStatus === 'running' && (
                            <span data-agent-tool-dot className="rounded-full"
                                style={{ width: 7, height: 7, backgroundColor: 'hsl(var(--foreground))', animation: 'agentDotPulse 1.2s ease-in-out infinite' }} />
                        )}
                        {headerStatus === 'completed' && (
                            <span className="absolute inset-0 flex items-center justify-center rounded-full bg-green-500"
                                style={{ animation: 'chatCheckPop 0.5s cubic-bezier(.34,1.56,.64,1) both' }}>
                                <Check className="h-3 w-3 text-white" strokeWidth={3} />
                            </span>
                        )}
                        {headerStatus === 'error' && <AlertCircle className="h-[15px] w-[15px] text-destructive" />}
                    </span>

                    <span className="shrink-0 text-[12.5px] font-semibold leading-none text-[#2b303b] dark:text-gray-100">{name}</span>
                    {wfBadge && (
                        <span className={`shrink-0 rounded border px-1.5 py-0.5 text-[10px] font-medium leading-none ${wfBadge.color}`}>
                            {wfBadge.label}
                        </span>
                    )}
                    {animInput && (
                        <span className="min-w-0 flex-1 truncate text-[11.5px] text-muted-foreground/70">{animInput}</span>
                    )}
                    <span className={cn(
                        "ml-auto shrink-0 text-[11px]",
                        headerStatus === 'completed' ? "font-semibold text-green-600"
                            : headerStatus === 'error' ? "text-destructive"
                            : "text-muted-foreground"
                    )}>
                        {statText}
                    </span>
                    <ChevronRight className={cn("h-3.5 w-3.5 shrink-0 text-muted-foreground/50 transition-transform duration-200", isExpanded && "rotate-90")} />
                </div>

                {/* indeterminate progress bar while running (mockup .bar) */}
                <div className={cn("h-[2px] overflow-hidden", headerStatus === 'running' ? "bg-[#eef3ff]" : "bg-transparent")}>
                    {headerStatus === 'running' && (
                        <span className="block h-full w-2/5"
                            style={{ background: 'linear-gradient(90deg,transparent,#1d4ed8,transparent)', animation: 'chatIndet 1.1s ease-in-out infinite' }} />
                    )}
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

                                {/* Input: typewriter while running, same styled text when done */}
                                {animInput && (
                                    <div className="mb-2">
                                        {status === 'running'
                                            ? <TypewriterText input={animInput} />
                                            : (
                                                <div className="px-2 py-1.5 rounded border border-border/40 bg-background/60">
                                                    <span className="font-mono text-[11px] text-foreground break-all leading-relaxed">
                                                        {animInput}
                                                    </span>
                                                </div>
                                            )
                                        }
                                    </div>
                                )}

                                {/* Watchdog: live heartbeat + runtime + kill, between input and output.
                                    Gated on a live supervised unit (NOT the bubble status), so it shows
                                    while a delegated sub-agent's subprocess is still running. */}
                                {liveUnit && (
                                    <div className="mb-2">
                                        <div className="flex items-center gap-1 opacity-70 mb-1 font-semibold">
                                            <Activity className="h-3 w-3" />
                                            <span>Watchdog</span>
                                        </div>
                                        <div className="flex items-center gap-2 rounded border border-border/40 bg-background/60 px-2 py-1.5">
                                            <span
                                                className={cn(
                                                    'h-2 w-2 rounded-full shrink-0',
                                                    liveUnit.stale
                                                        ? 'bg-red-500'
                                                        : (liveUnit.heartbeat_age_s != null && liveUnit.heartbeat_age_s < 10)
                                                            ? 'bg-emerald-500 animate-pulse'
                                                            : 'bg-amber-400 animate-pulse'
                                                )}
                                            />
                                            <span className="text-[11px] text-foreground">
                                                {liveUnit.stale ? 'No heartbeat — may be stuck' : 'Active'}
                                                {liveUnit.runtime_s != null && (
                                                    <span className="text-muted-foreground"> · {fmtDuration(liveUnit.runtime_s)}</span>
                                                )}
                                                {liveUnit.heartbeat_age_s != null && (
                                                    <span className="text-muted-foreground"> · ♥ {fmtDuration(liveUnit.heartbeat_age_s)}</span>
                                                )}
                                            </span>
                                            <button
                                                onClick={(e) => { e.stopPropagation(); killUnit(); }}
                                                disabled={killing}
                                                title="Diesen Sub-Agent killen"
                                                className="ml-auto inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium text-red-600 hover:bg-red-50 disabled:opacity-40 transition-colors"
                                            >
                                                {killing ? <Loader2 className="h-3 w-3 animate-spin" /> : <Skull className="h-3 w-3" />}
                                                Kill
                                            </button>
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
