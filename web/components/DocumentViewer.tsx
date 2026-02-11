'use client';

import React, { useState, useRef, useEffect } from 'react';
import { X, FileText, Plus, Trash2, ChevronRight, ChevronLeft, List } from 'lucide-react';
import { cn } from '@/lib/utils';

const AUTO_COLLAPSE_MS = 3000;

export type DocumentViewerDocument = {
    id: string;
    name: string;
    mimeType?: string;
    content?: string;
};

/** One inserted selection: text plus range in a specific document, for persistent highlight. */
export type InsertedSelectionRange = {
    text: string;
    start: number;
    end: number;
    documentId: string;
};

export type DocumentViewerProps = {
    isOpen: boolean;
    onClose: () => void;
    canClose?: boolean;
    title?: string;
    mode?: 'overlay' | 'dock';
    documents: DocumentViewerDocument[];
    onAddFiles: (files: File[]) => void;
    onRemoveDocument: (id: string) => void;
    /** Called when user selects text; range is used to keep highlight visible in the document. */
    onInsertSelection?: (text: string, range: { start: number; end: number; documentId: string }) => void;
    /** Number of selections already inserted; used for next selection color when selecting. */
    insertedSelectionsCount?: number;
    /** All inserted selections; used to render persistent highlights in the current document. */
    insertedSelections?: InsertedSelectionRange[];
};

const FILE_ACCEPT = '.pdf,.docx,.xlsx,.pptx,.txt,.md,.json,.csv';

/** Selection (and chip) color cycle: dark, orange, pink, blue, emerald. Export for use in chip styling. */
export const INSERTION_COLOR_CLASSES = [
    'selection:bg-gray-800 selection:text-white',
    'selection:bg-orange-500 selection:text-white',
    'selection:bg-pink-500 selection:text-white',
    'selection:bg-blue-500 selection:text-white',
    'selection:bg-emerald-600 selection:text-white',
] as const;
export const CHIP_BG_CLASSES = [
    'bg-gray-800 text-white',
    'bg-orange-500 text-white',
    'bg-pink-500 text-white',
    'bg-blue-500 text-white',
    'bg-emerald-600 text-white',
] as const;

function buildHighlightSegments(
    contentLength: number,
    ranges: { start: number; end: number; colorIndex: number }[]
): { start: number; end: number; colorIndex: number }[] {
    type Event = { pos: number; type: 'start' | 'end'; colorIndex: number };
    const events: Event[] = [];
    for (const r of ranges) {
        const start = Math.max(0, Math.min(r.start, contentLength));
        const end = Math.max(0, Math.min(r.end, contentLength));
        if (start >= end) continue;
        events.push({ pos: start, type: 'start', colorIndex: r.colorIndex });
        events.push({ pos: end, type: 'end', colorIndex: r.colorIndex });
    }
    events.sort((a, b) => (a.pos !== b.pos ? a.pos - b.pos : (a.type === 'end' ? 0 : 1) - (b.type === 'end' ? 0 : 1)));
    const stack: number[] = [];
    const segments: { start: number; end: number; colorIndex: number }[] = [];
    let prevPos = 0;
    for (const e of events) {
        if (e.pos > prevPos && stack.length > 0)
            segments.push({ start: prevPos, end: e.pos, colorIndex: stack[stack.length - 1] });
        if (e.type === 'start') stack.push(e.colorIndex);
        else stack.pop();
        prevPos = e.pos;
    }
    return segments;
}

export default function DocumentViewer({
    isOpen,
    onClose,
    canClose = true,
    title = 'Document Viewer',
    mode = 'dock',
    documents,
    onAddFiles,
    onRemoveDocument,
    onInsertSelection,
    insertedSelectionsCount = 0,
    insertedSelections = [],
}: DocumentViewerProps) {
    const [selectedId, setSelectedId] = useState<string | null>(null);
    const [listExpanded, setListExpanded] = useState(true);
    const [listContentVisible, setListContentVisible] = useState(true);
    const fileInputRef = useRef<HTMLInputElement>(null);
    const contentAreaRef = useRef<HTMLDivElement>(null);
    const autoCollapseRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const expandContentRef = useRef<ReturnType<typeof setTimeout> | null>(null);

    const handleContentMouseUp = () => {
        if (!onInsertSelection || !selectedDoc) return;
        const sel = typeof window !== 'undefined' ? window.getSelection() : null;
        if (!sel || !contentAreaRef.current || !sel.rangeCount) return;
        const text = sel.toString().trim();
        if (!text) return;
        if (!contentAreaRef.current.contains(sel.anchorNode)) return;
        const pre = contentAreaRef.current.querySelector('pre');
        if (!pre) return;
        const r = sel.getRangeAt(0);
        const startRange = document.createRange();
        startRange.setStart(pre, 0);
        startRange.setEnd(r.startContainer, r.startOffset);
        const start = startRange.toString().length;
        const end = start + text.length;
        onInsertSelection(text, { start, end, documentId: selectedDoc.id });
    };

    useEffect(() => {
        if (!isOpen) return;
        setListExpanded(true);
        if (autoCollapseRef.current) clearTimeout(autoCollapseRef.current);
        autoCollapseRef.current = setTimeout(() => {
            setListExpanded(false);
            autoCollapseRef.current = null;
        }, AUTO_COLLAPSE_MS);
        return () => {
            if (autoCollapseRef.current) clearTimeout(autoCollapseRef.current);
        };
    }, [isOpen]);

    useEffect(() => {
        if (listExpanded) {
            setListContentVisible(false);
            if (expandContentRef.current) clearTimeout(expandContentRef.current);
            expandContentRef.current = setTimeout(() => {
                setListContentVisible(true);
                expandContentRef.current = null;
            }, 120);
        } else {
            setListContentVisible(true);
            if (expandContentRef.current) clearTimeout(expandContentRef.current);
        }
        return () => {
            if (expandContentRef.current) clearTimeout(expandContentRef.current);
        };
    }, [listExpanded]);

    const hasDocuments = documents.length > 0;
    const selectedDoc = documents.find((d) => d.id === selectedId) ?? documents[0];
    const displayContent = selectedDoc?.content ?? '';
    const isImage = selectedDoc?.mimeType?.startsWith('image/');

    const contentWithHighlights = React.useMemo(() => {
        const content = displayContent || '(Kein Textinhalt)';
        const rangesForDoc = insertedSelections
            .map((s, i) => ({ start: s.start, end: s.end, colorIndex: i, documentId: s.documentId }))
            .filter((s) => s.documentId === selectedDoc?.id)
            .map(({ start, end, colorIndex }) => ({ start, end, colorIndex }));
        if (rangesForDoc.length === 0) return null;
        const segments = buildHighlightSegments(content.length, rangesForDoc);
        const parts: React.ReactNode[] = [];
        let lastEnd = 0;
        for (let i = 0; i < segments.length; i++) {
            const seg = segments[i];
            if (seg.start > lastEnd) parts.push(content.slice(lastEnd, seg.start));
            parts.push(
                <span key={i} className={cn('rounded-sm', CHIP_BG_CLASSES[seg.colorIndex % CHIP_BG_CLASSES.length])}>
                    {content.slice(seg.start, seg.end)}
                </span>
            );
            lastEnd = seg.end;
        }
        if (lastEnd < content.length) parts.push(content.slice(lastEnd));
        return parts;
    }, [displayContent, selectedDoc?.id, insertedSelections]);

    const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
        const files = e.target.files ? Array.from(e.target.files) : [];
        if (files.length) {
            onAddFiles(files);
        }
        e.target.value = '';
    };

    if (!isOpen && mode === 'overlay') return null;

    if (mode === 'dock') {
        return (
            <div
                className={cn(
                    'relative h-full w-full overflow-hidden rounded-2xl border border-gray-200 bg-[#F7F8FA] transition-all duration-300 ease-out',
                    isOpen ? 'translate-x-0 opacity-100' : 'translate-x-8 opacity-0 pointer-events-none'
                )}
                aria-hidden={!isOpen}
            >
                <div className="flex h-full w-full min-w-0">
                    <div className="flex flex-1 flex-col min-w-0 w-0 bg-[#F9FAFB] rounded-r-2xl overflow-hidden">
                        <div className="flex h-12 items-center justify-between border-b border-gray-200 bg-white px-4">
                            <div className="flex items-center gap-3">
                                <div className="flex h-7 w-7 items-center justify-center rounded-md border border-gray-200 bg-white text-blue-600">
                                    <FileText size={14} />
                                </div>
                                <div>
                                    <div className="text-xs font-semibold text-gray-900">{title}</div>
                                    <div className="flex items-center gap-2 text-[10px] text-gray-500">
                                        <span className="h-1.5 w-1.5 rounded-full bg-gray-400" />
                                        <span className="uppercase">Ready</span>
                                    </div>
                                </div>
                            </div>
                            <button
                                onClick={onClose}
                                className="rounded-full p-1 text-gray-400 transition hover:bg-gray-100 hover:text-gray-600"
                                aria-label="Close"
                            >
                                <X size={14} />
                            </button>
                        </div>
                        <div className="flex h-9 items-center gap-2 border-b border-gray-100 bg-white/80 px-4 text-xs text-gray-500">
                            <span className="rounded-md bg-gray-100 px-2 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-gray-500">
                                Anhänge
                            </span>
                            <span className="truncate font-mono text-[11px]">
                                {selectedDoc?.name ?? 'Kein Dokument ausgewählt'}
                            </span>
                        </div>
                        <div className="flex-1 min-h-0 min-w-0 overflow-hidden p-4 flex flex-col">
                            <div className="flex flex-1 min-h-0 min-w-0 flex-col overflow-hidden rounded-xl border border-gray-200 bg-white">
                                <div className="flex h-8 shrink-0 items-center border-b border-gray-100 bg-gray-50 px-3 text-[10px] text-gray-400">
                                    <div className="flex-1 min-w-0 truncate text-center font-mono">
                                        {selectedDoc?.name ?? '—'}
                                    </div>
                                </div>
                                <div
                                    ref={contentAreaRef}
                                    onMouseUp={handleContentMouseUp}
                                    className="flex-1 min-h-0 min-w-0 overflow-auto bg-white p-4 w-full"
                                >
                                    {!hasDocuments ? (
                                        <div className="flex flex-col items-center justify-center h-full gap-2 text-gray-400 text-sm">
                                            <FileText size={32} className="opacity-50" />
                                            <p>Keine Dokumente. Klicke auf &quot;Dokument hinzufügen&quot;, um Anhänge zu öffnen.</p>
                                            <p className="text-xs">Der Assistent kann dann auf deren Inhalt antworten.</p>
                                        </div>
                                    ) : !selectedDoc ? (
                                        <div className="text-gray-400 text-sm">Dokument auswählen.</div>
                                    ) : isImage && selectedDoc.content?.startsWith('data:') ? (
                                        <img src={selectedDoc.content} alt={selectedDoc.name} className="max-w-full h-auto" />
                                    ) : (
                                        <pre
                                            className={cn(
                                                'w-full max-w-full whitespace-pre-wrap break-words font-sans text-sm text-gray-800',
                                                INSERTION_COLOR_CLASSES[insertedSelectionsCount % INSERTION_COLOR_CLASSES.length]
                                            )}
                                        >
                                            {contentWithHighlights ?? (displayContent || '(Kein Textinhalt)')}
                                        </pre>
                                    )}
                                </div>
                            </div>
                        </div>
                    </div>

                    <div
                        className={cn(
                            'flex flex-col border-l border-gray-200 bg-white overflow-hidden shrink-0',
                            'transition-[width] duration-350 ease-in-out',
                            listExpanded ? 'w-[36%] min-w-[280px]' : 'w-12'
                        )}
                    >
                        {listExpanded ? (
                            <>
                                <div className="flex h-12 items-center justify-between border-b border-gray-100 px-4 shrink-0">
                                    <span className="text-xs font-semibold uppercase tracking-wide text-gray-500">
                                        Dokumentliste
                                    </span>
                                    <button
                                        type="button"
                                        onClick={() => setListExpanded(false)}
                                        className="rounded p-1.5 text-gray-400 hover:bg-gray-100 hover:text-gray-600 transition"
                                        title="Liste zuklappen"
                                        aria-label="Liste zuklappen"
                                    >
                                        <ChevronRight size={16} />
                                    </button>
                                </div>
                                <div
                                    className={cn(
                                        'flex-1 overflow-y-auto px-4 py-3 flex flex-col gap-2 min-h-0 transition-opacity duration-200',
                                        listContentVisible ? 'opacity-100' : 'opacity-0'
                                    )}
                                >
                                    <button
                                        type="button"
                                        onClick={() => fileInputRef.current?.click()}
                                        className="flex items-center gap-2 rounded-xl border border-gray-200 bg-white px-3 py-2.5 text-left text-[13px] font-medium text-gray-700 hover:border-blue-200 hover:bg-blue-50/50 transition"
                                    >
                                        <Plus size={14} />
                                        Dokument hinzufügen
                                    </button>
                                    <input
                                        ref={fileInputRef}
                                        type="file"
                                        multiple
                                        accept={FILE_ACCEPT}
                                        className="hidden"
                                        onChange={handleFileChange}
                                    />
                                    {documents.map((doc) => (
                                        <div
                                            key={doc.id}
                                            className={cn(
                                                'flex items-center gap-2 rounded-xl border px-3 py-2.5 transition cursor-pointer',
                                                selectedId === doc.id
                                                    ? 'border-blue-200 bg-blue-50/50 ring-1 ring-blue-50'
                                                    : 'border-gray-100 bg-gray-50 hover:border-gray-200'
                                            )}
                                            onClick={() => setSelectedId(doc.id)}
                                        >
                                            <FileText size={14} className="shrink-0 text-gray-500" />
                                            <span className="flex-1 truncate text-[13px] font-medium text-gray-900" title={doc.name}>
                                                {doc.name}
                                            </span>
                                            <button
                                                type="button"
                                                onClick={(e) => {
                                                    e.stopPropagation();
                                                    onRemoveDocument(doc.id);
                                                    if (selectedId === doc.id) {
                                                        const next = documents.find((d) => d.id !== doc.id);
                                                        setSelectedId(next?.id ?? null);
                                                    }
                                                }}
                                                className="rounded p-1 text-gray-400 hover:bg-red-50 hover:text-red-600 transition"
                                                aria-label={`Remove ${doc.name}`}
                                            >
                                                <Trash2 size={12} />
                                            </button>
                                        </div>
                                    ))}
                                </div>
                            </>
                        ) : (
                            <div className="flex flex-col items-center py-3 gap-2 h-full">
                                <button
                                    type="button"
                                    onClick={() => setListExpanded(true)}
                                    className="flex flex-col items-center gap-1.5 rounded-lg p-2 text-gray-500 hover:bg-gray-100 hover:text-gray-700 transition w-full"
                                    title="Dokumentliste einblenden"
                                    aria-label="Dokumentliste einblenden"
                                >
                                    <ChevronLeft size={18} />
                                    <List size={16} />
                                    <span className="text-[9px] font-medium uppercase tracking-wider" style={{ writingMode: 'vertical-rl', textOrientation: 'mixed' }}>
                                        Liste
                                    </span>
                                </button>
                                {documents.length > 0 && (
                                    <span className="text-[10px] text-gray-400 tabular-nums">
                                        {documents.length}
                                    </span>
                                )}
                            </div>
                        )}
                    </div>
                </div>
            </div>
        );
    }

    return (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 backdrop-blur-sm p-4 sm:p-8">
            <div className="relative flex h-[90vh] w-full max-w-[1400px] overflow-hidden rounded-2xl bg-[#F3F4F6] shadow-2xl min-w-0">
                <div className="flex flex-1 flex-col min-w-0 w-0 bg-[#F9FAFB] overflow-hidden">
                    <div className="flex h-16 shrink-0 items-center justify-between border-b border-gray-200 bg-white px-6">
                        <div className="flex items-center gap-3">
                            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-blue-500 text-white shadow-sm">
                                <FileText size={18} />
                            </div>
                            <div>
                                <div className="text-sm font-semibold text-gray-900">{title}</div>
                                <div className="text-xs text-gray-500">Ready</div>
                            </div>
                        </div>
                        <button
                            onClick={onClose}
                            className="rounded-full p-2 text-gray-400 hover:bg-gray-100 hover:text-gray-600"
                            aria-label="Close"
                        >
                            <X size={16} />
                        </button>
                    </div>
                    <div className="flex h-9 items-center border-b border-gray-100 bg-white/80 px-6 text-xs text-gray-500">
                        <span className="truncate font-mono">{selectedDoc?.name ?? '—'}</span>
                    </div>
                    <div className="flex-1 min-h-0 min-w-0 overflow-hidden p-6 flex flex-col">
                        <div className="flex flex-1 min-h-0 min-w-0 flex-col overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm">
                            <div
                                ref={contentAreaRef}
                                onMouseUp={handleContentMouseUp}
                                className="flex-1 min-h-0 min-w-0 overflow-auto p-4 w-full"
                            >
                                {!hasDocuments ? (
                                    <div className="flex flex-col items-center justify-center h-full gap-2 text-gray-400 text-sm">
                                        <FileText size={40} className="opacity-50" />
                                        <p>Keine Dokumente. Dokument hinzufügen, um Anhänge zu öffnen.</p>
                                    </div>
                                ) : selectedDoc ? (
                                    isImage && selectedDoc.content?.startsWith('data:') ? (
                                        <img src={selectedDoc.content} alt={selectedDoc.name} className="max-w-full h-auto" />
                                    ) : (
                                        <pre
                                            className={cn(
                                                'w-full max-w-full whitespace-pre-wrap break-words font-sans text-sm text-gray-800',
                                                INSERTION_COLOR_CLASSES[insertedSelectionsCount % INSERTION_COLOR_CLASSES.length]
                                            )}
                                        >
                                            {contentWithHighlights ?? (displayContent || '(Kein Textinhalt)')}
                                        </pre>
                                    )
                                ) : null}
                            </div>
                        </div>
                    </div>
                </div>
                <div
                    className={cn(
                        'flex flex-col border-l border-gray-200 bg-white overflow-hidden shrink-0',
                        'transition-[width] duration-350 ease-in-out',
                        listExpanded ? 'w-[35%] min-w-[320px]' : 'w-14'
                    )}
                >
                    {listExpanded ? (
                        <>
                            <div className="flex h-14 items-center justify-between border-b border-gray-100 px-5 shrink-0">
                                <span className="text-sm font-semibold text-gray-700">Dokumentliste</span>
                                <button
                                    type="button"
                                    onClick={() => setListExpanded(false)}
                                    className="rounded p-2 text-gray-400 hover:bg-gray-100 hover:text-gray-600"
                                    title="Liste zuklappen"
                                    aria-label="Liste zuklappen"
                                >
                                    <ChevronRight size={18} />
                                </button>
                            </div>
                            <div
                                className={cn(
                                    'flex-1 overflow-y-auto px-5 py-4 flex flex-col gap-3 min-h-0 transition-opacity duration-200',
                                    listContentVisible ? 'opacity-100' : 'opacity-0'
                                )}
                            >
                                <button
                                    type="button"
                                    onClick={() => fileInputRef.current?.click()}
                                    className="flex items-center gap-2 rounded-xl border border-gray-200 bg-white px-4 py-3 text-left text-sm font-medium text-gray-700 hover:border-blue-200 hover:bg-blue-50/50 transition"
                                >
                                    <Plus size={16} />
                                    Dokument hinzufügen
                                </button>
                                <input
                                    ref={fileInputRef}
                                    type="file"
                                    multiple
                                    accept={FILE_ACCEPT}
                                    className="hidden"
                                    onChange={handleFileChange}
                                />
                                {documents.map((doc) => (
                                    <div
                                        key={doc.id}
                                        className={cn(
                                            'flex items-center gap-2 rounded-xl border px-4 py-3 transition cursor-pointer',
                                            selectedId === doc.id ? 'border-blue-200 bg-blue-50/50' : 'border-gray-100 bg-gray-50'
                                        )}
                                        onClick={() => setSelectedId(doc.id)}
                                    >
                                        <FileText size={16} className="shrink-0 text-gray-500" />
                                        <span className="flex-1 truncate text-sm font-medium text-gray-900">{doc.name}</span>
                                        <button
                                            type="button"
                                            onClick={(e) => {
                                                e.stopPropagation();
                                                onRemoveDocument(doc.id);
                                                if (selectedId === doc.id) {
                                                    const next = documents.find((d) => d.id !== doc.id);
                                                    setSelectedId(next?.id ?? null);
                                                }
                                            }}
                                            className="rounded p-1.5 text-gray-400 hover:bg-red-50 hover:text-red-600"
                                            aria-label={`Remove ${doc.name}`}
                                        >
                                            <Trash2 size={14} />
                                        </button>
                                    </div>
                                ))}
                            </div>
                        </>
                    ) : (
                        <div className="flex flex-col items-center py-4 gap-2 h-full">
                            <button
                                type="button"
                                onClick={() => setListExpanded(true)}
                                className="flex flex-col items-center gap-2 rounded-lg p-2 text-gray-500 hover:bg-gray-100 hover:text-gray-700 transition w-full"
                                title="Dokumentliste einblenden"
                                aria-label="Dokumentliste einblenden"
                            >
                                <ChevronLeft size={20} />
                                <List size={18} />
                                <span className="text-[10px] font-medium uppercase tracking-wider" style={{ writingMode: 'vertical-rl', textOrientation: 'mixed' }}>
                                    Liste
                                </span>
                            </button>
                            {documents.length > 0 && (
                                <span className="text-xs text-gray-400 tabular-nums">{documents.length}</span>
                            )}
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
}
