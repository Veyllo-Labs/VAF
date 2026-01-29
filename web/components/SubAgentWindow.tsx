'use client';

import React, { useMemo } from 'react';
import { X, Terminal, FileCode, CheckCircle2, Circle, Loader2 } from 'lucide-react';
import { cn } from '@/lib/utils';

interface SubAgentWindowProps {
    isOpen: boolean;
    onClose: () => void;
    agentName: string;
    status: string;
    currentFile: string;
    codeContent: string;
    steps: Array<{
        id: string;
        title: string;
        description?: string;
        status: 'pending' | 'running' | 'completed';
        actions: Array<{ type: string; details: string }>;
    }>;
}

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
    agentName,
    status,
    currentFile,
    codeContent,
    steps,
}: SubAgentWindowProps) {
    const codeLines = useMemo(() => (codeContent ? codeContent.split('\n') : []), [codeContent]);

    if (!isOpen) return null;

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
                                    <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-500" />
                                    {status}
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