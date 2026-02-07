'use client';

import React, { useState, useEffect, useRef, useMemo } from 'react';
import { X, Download, FileText, Save, Loader2, CheckCircle2, Circle } from 'lucide-react';
import { cn, getApiBase } from '@/lib/utils';

export type DocumentEditorProps = {
    isOpen: boolean;
    onClose: () => void;
    canClose?: boolean;
    filePath: string;
    title?: string;
    initialContent?: string;
    mode?: 'overlay' | 'dock';
    status?: string;
    presence?: 'online' | 'idle' | 'error';
    steps?: Array<{
        id: string;
        title: string;
        description?: string;
        status: 'pending' | 'running' | 'completed';
    }>;
};

export default function DocumentEditor({
    isOpen,
    onClose,
    canClose = true,
    filePath,
    title = 'Document Editor',
    initialContent = '',
    mode = 'overlay',
    status = '',
    presence,
    steps = [],
}: DocumentEditorProps) {
    const [content, setContent] = useState<string>(initialContent);
    const [isLoading, setIsLoading] = useState(false);
    const [isSaving, setIsSaving] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const iframeRef = useRef<HTMLIFrameElement>(null);

    // Use presence from props if available, otherwise infer from status text
    const statusLower = (status || '').toLowerCase();
    const inferredPresence = presence
        ? presence
        : statusLower.includes('error') || statusLower.includes('fail')
            ? 'error'
            : statusLower.includes('saving') || statusLower.includes('loading')
                ? 'online'
                : 'idle';
    const presenceLabel = inferredPresence === 'online' ? 'Active' : inferredPresence === 'error' ? 'Error' : 'Ready';
    const presenceTone = inferredPresence === 'online'
        ? 'bg-emerald-500'
        : inferredPresence === 'error'
            ? 'bg-red-500'
            : 'bg-gray-400';

    const hasWorkflow = steps.length > 0;
    const displayFile = filePath?.split(/[/\\]/).pop() || 'document.html';

    // Load content from file when opened
    useEffect(() => {
        if (isOpen && filePath && !initialContent) {
            loadDocument();
        }
    }, [isOpen, filePath]);

    // Update iframe content when content changes
    useEffect(() => {
        if (iframeRef.current && content) {
            const doc = iframeRef.current.contentDocument;
            if (doc) {
                doc.open();
                doc.write(content);
                doc.close();
                // Make content editable
                doc.body.contentEditable = 'true';
                doc.body.style.outline = 'none';
                doc.body.style.padding = '20px';
                // Listen for changes
                doc.body.addEventListener('input', () => {
                    setContent(doc.body.innerHTML);
                });
            }
        }
    }, [content, isOpen]);

    const loadDocument = async () => {
        setIsLoading(true);
        setError(null);
        try {
            const base = getApiBase();
            const response = await fetch(`${base}/api/file?path=${encodeURIComponent(filePath)}`);
            if (!response.ok) throw new Error('Failed to load document');
            const text = await response.text();
            setContent(text);
        } catch (e) {
            setError(e instanceof Error ? e.message : 'Failed to load document');
        } finally {
            setIsLoading(false);
        }
    };

    const saveDocument = async () => {
        setIsSaving(true);
        try {
            const base = getApiBase();
            const response = await fetch(`${base}/api/file/save`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: filePath, content }),
            });
            if (!response.ok) throw new Error('Failed to save document');
        } catch (e) {
            setError(e instanceof Error ? e.message : 'Failed to save document');
        } finally {
            setIsSaving(false);
        }
    };

    const exportAsPDF = async () => {
        if (iframeRef.current?.contentWindow) {
            iframeRef.current.contentWindow.print();
        }
    };

    const downloadHTML = () => {
        const blob = new Blob([content], { type: 'text/html' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = displayFile;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    };

    if (!isOpen && mode === 'overlay') return null;

    // Dock mode - same layout as SubAgentWindow dock mode
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
                    {/* Workflow panel (left side) - only shown if steps exist */}
                    {hasWorkflow && (
                        <div className="flex w-[36%] min-w-[280px] flex-col border-r border-gray-200 bg-white">
                            <div className="flex h-12 items-center justify-between border-b border-gray-100 px-4">
                                <span className="text-xs font-semibold uppercase tracking-wide text-gray-500">Document Steps</span>
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
                                            </div>
                                        </div>
                                    ))}
                                </div>
                            </div>
                        </div>
                    )}

                    {/* Main content panel (right side) */}
                    <div className={cn("flex flex-1 flex-col bg-[#F9FAFB]", !hasWorkflow && "rounded-l-2xl")}>
                        {/* Header */}
                        <div className="flex h-12 items-center justify-between border-b border-gray-200 bg-white px-4">
                            <div className="flex items-center gap-3">
                                <div className="flex h-7 w-7 items-center justify-center rounded-md border border-gray-200 bg-white text-blue-600">
                                    <FileText size={14} />
                                </div>
                                <div>
                                    <div className="text-xs font-semibold text-gray-900">{title}</div>
                                    <div className="flex items-center gap-2 text-[10px] text-gray-500">
                                        <span className={cn("h-1.5 w-1.5 rounded-full", presenceTone)} />
                                        {status ? (
                                            <span className="text-gray-500">{status}</span>
                                        ) : (
                                            <span className="uppercase">{presenceLabel}</span>
                                        )}
                                    </div>
                                </div>
                            </div>
                            <div className="flex items-center gap-2">
                                <button
                                    onClick={saveDocument}
                                    disabled={isSaving}
                                    className="flex items-center gap-1 px-2 py-1 text-[10px] font-medium text-gray-600 bg-gray-100 hover:bg-gray-200 rounded-md transition-colors disabled:opacity-50"
                                >
                                    {isSaving ? <Loader2 size={10} className="animate-spin" /> : <Save size={10} />}
                                    Save
                                </button>
                                <button
                                    onClick={exportAsPDF}
                                    className="flex items-center gap-1 px-2 py-1 text-[10px] font-medium text-white bg-blue-500 hover:bg-blue-600 rounded-md transition-colors"
                                >
                                    <Download size={10} />
                                    PDF
                                </button>
                                <button
                                    onClick={onClose}
                                    className="rounded-full p-1 text-gray-400 transition hover:bg-gray-100 hover:text-gray-600"
                                    aria-label="Close"
                                >
                                    <X size={14} />
                                </button>
                            </div>
                        </div>

                        {/* File info bar */}
                        <div className="flex h-9 items-center gap-2 border-b border-gray-100 bg-white/80 px-4 text-xs text-gray-500">
                            <span className="rounded-md bg-gray-100 px-2 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-gray-500">
                                Edit
                            </span>
                            <span className="truncate font-mono text-[11px]">{filePath || 'No file selected'}</span>
                        </div>

                        {/* Content area */}
                        <div className="flex-1 overflow-hidden p-4">
                            <div className="flex h-full flex-col overflow-hidden rounded-xl border border-gray-200 bg-white">
                                <div className="flex h-8 items-center border-b border-gray-100 bg-gray-50 px-3 text-[10px] text-gray-400">
                                    <div className="flex-1 truncate text-center font-mono">
                                        {displayFile}
                                    </div>
                                </div>
                                <div className="flex-1 overflow-auto bg-white">
                                    {isLoading ? (
                                        <div className="flex items-center justify-center h-full gap-2 text-gray-300">
                                            <Loader2 size={14} className="animate-spin opacity-50" />
                                            <span className="text-xs">Loading document...</span>
                                        </div>
                                    ) : error ? (
                                        <div className="flex flex-col items-center justify-center h-full gap-2">
                                            <p className="text-red-500 text-xs">{error}</p>
                                            <button
                                                onClick={loadDocument}
                                                className="text-xs text-blue-500 hover:underline"
                                            >
                                                Try again
                                            </button>
                                        </div>
                                    ) : (
                                        <iframe
                                            ref={iframeRef}
                                            className="w-full h-full border-0"
                                            title="Document Editor"
                                            sandbox="allow-same-origin allow-scripts"
                                        />
                                    )}
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        );
    }

    // Overlay mode - same layout as SubAgentWindow overlay mode
    return (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 backdrop-blur-sm p-4 sm:p-8">
            <div className="relative flex h-[90vh] w-full max-w-[1400px] overflow-hidden rounded-2xl bg-[#F3F4F6] shadow-2xl">
                {/* Workflow panel (left side) - only shown if steps exist */}
                {hasWorkflow && (
                    <div className="flex w-[35%] min-w-[320px] flex-col border-r border-gray-200 bg-white">
                        <div className="flex h-14 items-center justify-between border-b border-gray-100 px-5">
                            <span className="text-sm font-semibold text-gray-700">Document Steps</span>
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
                                        </div>
                                    </div>
                                ))}
                            </div>
                        </div>
                    </div>
                )}

                {/* Main content panel (right side) */}
                <div className="flex flex-1 flex-col bg-[#F9FAFB]">
                    {/* Header */}
                    <div className="flex h-16 items-center justify-between border-b border-gray-200 bg-white px-6">
                        <div className="flex items-center gap-3">
                            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-blue-500 text-white shadow-sm">
                                <FileText size={18} />
                            </div>
                            <div>
                                <div className="text-sm font-semibold text-gray-900">{title}</div>
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
                        <div className="flex items-center gap-2">
                            <button
                                onClick={saveDocument}
                                disabled={isSaving}
                                className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-gray-700 bg-gray-100 hover:bg-gray-200 rounded-lg transition-colors disabled:opacity-50"
                            >
                                {isSaving ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}
                                Save
                            </button>
                            <button
                                onClick={exportAsPDF}
                                className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-blue-500 hover:bg-blue-600 rounded-lg transition-colors"
                            >
                                <Download size={14} />
                                Export PDF
                            </button>
                            <button
                                onClick={downloadHTML}
                                className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-gray-700 bg-gray-100 hover:bg-gray-200 rounded-lg transition-colors"
                            >
                                <Download size={14} />
                                Download
                            </button>
                            <button
                                onClick={onClose}
                                className="rounded-full p-2 text-gray-400 transition hover:bg-gray-100 hover:text-gray-600"
                                aria-label="Close"
                            >
                                <X size={16} />
                            </button>
                        </div>
                    </div>

                    {/* File info bar */}
                    <div className="flex h-9 items-center gap-2 border-b border-gray-100 bg-white/80 px-6 text-xs text-gray-500">
                        <FileText size={12} />
                        <span className="truncate font-mono">{filePath || 'No file selected'}</span>
                    </div>

                    {/* Content area */}
                    <div className="flex-1 overflow-hidden p-6">
                        <div className="flex h-full flex-col overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm">
                            <div className="flex h-9 items-center border-b border-gray-100 bg-gray-50 px-4 text-xs font-medium text-gray-600">
                                {displayFile}
                            </div>
                            <div className="flex-1 overflow-auto">
                                {isLoading ? (
                                    <div className="flex h-full flex-col items-center justify-center gap-2 text-gray-300">
                                        <Loader2 size={28} className="animate-spin opacity-50" />
                                        <span className="text-xs">Loading document...</span>
                                    </div>
                                ) : error ? (
                                    <div className="flex h-full flex-col items-center justify-center gap-2">
                                        <p className="text-red-500 text-sm">{error}</p>
                                        <button
                                            onClick={loadDocument}
                                            className="text-sm text-blue-500 hover:underline"
                                        >
                                            Try again
                                        </button>
                                    </div>
                                ) : (
                                    <iframe
                                        ref={iframeRef}
                                        className="w-full h-full border-0"
                                        title="Document Editor"
                                        sandbox="allow-same-origin allow-scripts"
                                    />
                                )}
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    );
}
