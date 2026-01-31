'use client';

import React, { useMemo } from 'react';
import { X, Terminal, FileCode, CheckCircle2, Circle, Loader2 } from 'lucide-react';
import { cn } from '@/lib/utils';

export type SubAgentWindowProps = {
    isOpen: boolean;
    onClose: () => void;
    canClose?: boolean;
    mode?: 'overlay' | 'dock';
    agentName: string;
    status: string;
    presence?: 'online' | 'idle' | 'error';  // Direct presence from backend
    currentFile: string;
    codeContent: string;
    artifactFile?: string;
    artifactCode?: string;
    artifactStatus?: string;
    onArtifactChange?: (nextValue: string) => void;
    consoleLines?: string[];
    steps: Array<{
        id: string;
        title: string;
        description?: string;
        status: 'pending' | 'running' | 'completed';
        actions: Array<{ type: string; details: string }>;
    }>;
    [key: string]: any;
};

const actionTone = (type: string) => {
    const normalized = type.toLowerCase();
    if (normalized === 'exec') return 'bg-gray-900 text-white';
    if (normalized === 'read') return 'bg-blue-100 text-blue-700';
    if (normalized === 'write') return 'bg-emerald-100 text-emerald-700';
    if (normalized === 'think') return 'bg-purple-100 text-purple-700';
    return 'bg-gray-100 text-gray-600';
};

export default function SubAgentWindow({
    isOpen,
    onClose,
    canClose = true,
    mode = 'overlay',
    agentName,
    status,
    presence,
    currentFile,
    codeContent,
    artifactFile,
    artifactCode,
    artifactStatus,
    onArtifactChange,
    consoleLines = [],
    steps,
}: SubAgentWindowProps) {
    const displayFile = artifactFile ?? currentFile;
    const displayCode = artifactCode ?? codeContent;
    const displayStatus = status;
    const artifactStateLabel = artifactStatus ?? '';

    // Use presence from backend if available, otherwise infer from status text
    const statusLower = (status || '').toLowerCase();
    const hasRunningStep = steps.some(step => step.status === 'running');
    const inferredPresence = presence
        ? presence  // Use backend presence directly
        : statusLower.includes('error') || statusLower.includes('fail') || statusLower.includes('timeout')
            ? 'error'
            : hasRunningStep || statusLower.includes('online') || statusLower.includes('running')
                ? 'online'
                : 'idle';
    const presenceLabel = inferredPresence === 'online' ? 'Running' : inferredPresence === 'error' ? 'Error' : 'Idle';
    const presenceTone = inferredPresence === 'online'
        ? 'bg-emerald-500'
        : inferredPresence === 'error'
            ? 'bg-red-500'
            : 'bg-gray-400';
    const hasWorkflow = false;
    const codeLines = useMemo(() => (displayCode ? displayCode.split('\n') : []), [displayCode]);

    if (!isOpen && mode === 'overlay') return null;

    if (mode === 'dock') {
        return (
            <div
                className={cn(
                    "relative h-full w-full overflow-hidden rounded-2xl border border-gray-200 bg-[#F7F8FA] transition-all duration-300 ease-out",
                    isOpen ? "translate-x-0 opacity-100" : "translate-x-8 opacity-0 pointer-events-none"
                )}
                aria-hidden={!isOpen}
            >
                <div className="flex h-full w-full">
                {hasWorkflow && (
                    <div className="flex w-[36%] min-w-[280px] flex-col border-r border-gray-200 bg-white">
                        <div className="flex h-12 items-center justify-between border-b border-gray-100 px-4">
                            <span className="text-xs font-semibold uppercase tracking-wide text-gray-500">Workflow</span>
                        </div>

                        <div className="relative flex-1 overflow-y-auto px-4 py-5">
                            <div className="absolute bottom-5 left-5 top-5 w-px bg-gray-200" />
                            <div className="space-y-4">
                                {steps.map((step) => (
                                    <div key={step.id} className="relative pl-7">
                                        <div
                                            className={cn(
                                                'absolute left-[2px] top-2 flex h-5 w-5 items-center justify-center rounded-full border border-white shadow-sm',
                                                step.status === 'running' && 'bg-blue-100 text-blue-600',
                                                step.status === 'completed' && 'bg-emerald-100 text-emerald-600',
                                                step.status === 'pending' && 'bg-gray-100 text-gray-400'
                                            )}
                                        >
                                            {step.status === 'running' && <Loader2 size={12} className="animate-spin" />}
                                            {step.status === 'completed' && <CheckCircle2 size={12} />}
                                            {step.status === 'pending' && <Circle size={10} />}
                                        </div>

                                        <div
                                            className={cn(
                                                'rounded-xl border px-3 py-2.5 transition',
                                                step.status === 'running' && 'border-blue-200 bg-white ring-1 ring-blue-50',
                                                step.status === 'completed' && 'border-gray-100 bg-gray-50',
                                                step.status === 'pending' && 'border-gray-100 bg-white'
                                            )}
                                        >
                                            <div className="flex flex-col gap-1">
                                                <div className="text-[13px] font-semibold text-gray-900">{step.title}</div>
                                                {step.description && (
                                                    <div className="text-[11px] text-gray-500">{step.description}</div>
                                                )}
                                            </div>

                                            {step.actions.length > 0 && (
                                                <div className="mt-2 flex flex-wrap items-center gap-2">
                                                    {step.actions.map((action, index) => (
                                                        <div key={index} className="flex items-center gap-2 text-xs">
                                                            <span
                                                                className={cn(
                                                                    'rounded-[4px] px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide',
                                                                    actionTone(action.type)
                                                                )}
                                                            >
                                                                {action.type}
                                                            </span>
                                                            <span className="max-w-[190px] truncate font-mono text-gray-600">
                                                                {action.details}
                                                            </span>
                                                        </div>
                                                    ))}
                                                </div>
                                            )}
                                        </div>
                                    </div>
                                ))}
                            </div>
                        </div>
                    </div>
                )}

                <div className={cn("flex flex-1 flex-col bg-[#F9FAFB]", !hasWorkflow && "rounded-l-2xl")}>
                    <div className="flex h-12 items-center justify-between border-b border-gray-200 bg-white px-4">
                        <div className="flex items-center gap-3">
                            <div className="flex h-7 w-7 items-center justify-center rounded-md border border-gray-200 bg-white text-gray-700">
                                <Terminal size={14} />
                            </div>
                            <div>
                                <div className="text-xs font-semibold text-gray-900">{agentName}</div>
                                <div className="flex items-center gap-2 text-[10px] text-gray-500">
                                    <span className={cn("h-1.5 w-1.5 rounded-full", presenceTone)} />
                                    {displayStatus ? (
                                        <span className="text-gray-500">{displayStatus}</span>
                                    ) : (
                                        <span className="uppercase">{presenceLabel}</span>
                                    )}
                                </div>
                            </div>
                        </div>
                        <div className="flex items-center gap-2">
                            {artifactStateLabel && (
                                <span className="text-[10px] uppercase tracking-wide text-gray-400">
                                    {artifactStateLabel}
                                </span>
                            )}
                            <button
                                onClick={canClose ? onClose : undefined}
                                className={cn(
                                    "rounded-full p-1 text-gray-400 transition",
                                    canClose ? "hover:bg-gray-100 hover:text-gray-600" : "cursor-not-allowed opacity-40"
                                )}
                                aria-label="Close"
                                aria-disabled={!canClose}
                            >
                                <X size={14} />
                            </button>
                        </div>
                    </div>

                    <div className="flex h-9 items-center gap-2 border-b border-gray-100 bg-white/80 px-4 text-xs text-gray-500">
                        <span className="rounded-md bg-gray-100 px-2 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-gray-500">
                            {onArtifactChange ? 'Edit' : 'Read'}
                        </span>
                        <span className="truncate font-mono text-[11px]">{displayFile || 'No active file'}</span>
                    </div>

                    <div className="flex-1 overflow-hidden p-4">
                        <div className="flex h-full flex-col overflow-hidden rounded-xl border border-gray-200 bg-white">
                            <div className="flex h-8 items-center border-b border-gray-100 bg-gray-50 px-3 text-[10px] text-gray-400">
                                <div className="flex-1 truncate text-center font-mono">
                                    Console
                                </div>
                            </div>
                            <div className="flex-1 overflow-auto bg-white px-4 py-4 font-mono text-xs text-gray-900">
                                {consoleLines.length > 0 ? (
                                    <div className="space-y-1 whitespace-pre-wrap">
                                        {consoleLines.map((line, index) => (
                                            <div key={`${line}-${index}`}>{line}</div>
                                        ))}
                                    </div>
                                ) : (
                                    <div className="flex items-center gap-2 text-gray-300">
                                        <Loader2 size={14} className="animate-spin opacity-50" />
                                        <span className="text-xs">Waiting for output...</span>
                                    </div>
                                )}
                            </div>
                        </div>
                    </div>
                </div>
            </div>
            </div>
        );
    }

    return (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 backdrop-blur-sm p-4 sm:p-8">
            <div className="relative flex h-[90vh] w-full max-w-[1400px] overflow-hidden rounded-2xl bg-[#F3F4F6] shadow-2xl">
                <div className="flex w-[35%] min-w-[320px] flex-col border-r border-gray-200 bg-white">
                    <div className="flex h-14 items-center justify-between border-b border-gray-100 px-5">
                        <span className="text-sm font-semibold text-gray-700">Workflow</span>
                    </div>

                    <div className="relative flex-1 overflow-y-auto px-5 py-6">
                        <div className="absolute bottom-6 left-7 top-6 w-px bg-gray-200" />
                        <div className="space-y-5">
                            {steps.map((step) => (
                                <div key={step.id} className="relative pl-8">
                                    <div
                                        className={cn(
                                            'absolute left-[6px] top-2 flex h-6 w-6 items-center justify-center rounded-full border border-white shadow-sm',
                                            step.status === 'running' && 'bg-blue-100 text-blue-600',
                                            step.status === 'completed' && 'bg-emerald-100 text-emerald-600',
                                            step.status === 'pending' && 'bg-gray-100 text-gray-400'
                                        )}
                                    >
                                        {step.status === 'running' && <Loader2 size={14} className="animate-spin" />}
                                        {step.status === 'completed' && <CheckCircle2 size={14} />}
                                        {step.status === 'pending' && <Circle size={12} />}
                                    </div>

                                    <div
                                        className={cn(
                                            'rounded-xl border px-4 py-3 shadow-sm transition',
                                            step.status === 'running' && 'border-blue-200 bg-white ring-1 ring-blue-100',
                                            step.status === 'completed' && 'border-gray-100 bg-gray-50',
                                            step.status === 'pending' && 'border-gray-100 bg-white'
                                        )}
                                    >
                                        <div className="flex flex-col gap-1">
                                            <div className="text-sm font-semibold text-gray-900">{step.title}</div>
                                            {step.description && (
                                                <div className="text-xs text-gray-500">{step.description}</div>
                                            )}
                                        </div>

                                        {step.actions.length > 0 && (
                                            <div className="mt-3 flex flex-wrap items-center gap-2">
                                                {step.actions.map((action, index) => (
                                                    <div key={index} className="flex items-center gap-2 text-xs">
                                                        <span
                                                            className={cn(
                                                                'rounded-[4px] px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide',
                                                                actionTone(action.type)
                                                            )}
                                                        >
                                                            {action.type}
                                                        </span>
                                                        <span className="max-w-[190px] truncate font-mono text-gray-600">
                                                            {action.details}
                                                        </span>
                                                    </div>
                                                ))}
                                            </div>
                                        )}
                                    </div>
                                </div>
                            ))}
                        </div>
                    </div>
                </div>

                <div className="flex flex-1 flex-col bg-[#F9FAFB]">
                    <div className="flex h-16 items-center justify-between border-b border-gray-200 bg-white px-6">
                        <div className="flex items-center gap-3">
                            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-black text-white shadow-sm">
                                <Terminal size={18} />
                            </div>
                            <div>
                                <div className="text-sm font-semibold text-gray-900">{agentName}</div>
                                <div className="flex items-center gap-2 text-xs text-gray-500">
                                    <span className={cn("h-1.5 w-1.5 rounded-full", presenceTone)} />
                                    {status ? (
                                        <span className="text-gray-500">{status}</span>
                                    ) : (
                                        <span className="uppercase">{presenceLabel}</span>
                                    )}
                                </div>
                            </div>
                        </div>
                        <button
                            onClick={onClose}
                            className="rounded-full p-2 text-gray-400 transition hover:bg-gray-100 hover:text-gray-600"
                            aria-label="Close"
                        >
                            <X size={16} />
                        </button>
                    </div>

                    <div className="flex h-9 items-center gap-2 border-b border-gray-100 bg-white/80 px-6 text-xs text-gray-500">
                        <FileCode size={12} />
                        <span className="truncate font-mono">{currentFile || 'No active file'}</span>
                    </div>

                    <div className="flex-1 overflow-hidden p-6">
                        <div className="flex h-full flex-col overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm">
                            <div className="flex h-9 items-center border-b border-gray-100 bg-gray-50 px-4 text-xs font-medium text-gray-600">
                                {currentFile ? currentFile.split('/').pop() : 'Console'}
                            </div>
                            <div className="flex-1 overflow-auto">
                                {codeContent ? (
                                    <div className="flex text-sm leading-6 text-gray-800">
                                        <div className="select-none border-r bg-gray-50/70 px-4 py-4 text-right font-mono text-xs text-gray-400">
                                            {codeLines.map((_, index) => (
                                                <div key={`line-${index}`}>{index + 1}</div>
                                            ))}
                                        </div>
                                        <pre className="flex-1 whitespace-pre px-4 py-4 font-mono">
                                            {codeContent}
                                        </pre>
                                    </div>
                                ) : (
                                    <div className="flex h-full flex-col items-center justify-center gap-2 text-gray-300">
                                        <Loader2 size={28} className="animate-spin opacity-50" />
                                        <span className="text-xs">Waiting for output...</span>
                                    </div>
                                )}
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    );
}