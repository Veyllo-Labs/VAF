'use client';

import React, { useState, useEffect, useRef, useMemo, useCallback } from 'react';
import { X, Download, FileText, Save, Loader2, CheckCircle2, Circle, Plus, Trash2, ChevronDown, Bold, Italic, Underline, List, ListOrdered, AlignLeft, AlignCenter, AlignRight, Highlighter, Eraser, Printer } from 'lucide-react';
import { cn, getApiBase } from '@/lib/utils';
import { CHIP_BG_CLASSES, INSERTION_COLOR_CLASSES } from '@/components/DocumentViewer';

/** Document for attachments mode (extracted text/image). */
export type DocumentEditorAttachment = {
    id: string;
    name: string;
    mimeType?: string;
    content?: string;
};

/** One inserted selection for quote chips and persistent highlight. */
export type InsertedSelectionRange = {
    text: string;
    start: number;
    end: number;
    documentId: string;
};

export type DocumentEditorProps = {
    isOpen: boolean;
    onClose: () => void;
    canClose?: boolean;
    /** Kernel editor: path to file to edit. When set, attachments props are ignored. */
    filePath?: string;
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
    /** Attachments mode: list of documents (no right-side list). */
    documents?: DocumentEditorAttachment[];
    onAddFiles?: (files: File[]) => void;
    onRemoveDocument?: (id: string) => void;
    onInsertSelection?: (text: string, range: { start: number; end: number; documentId: string }) => void;
    insertedSelectionsCount?: number;
    insertedSelections?: InsertedSelectionRange[];
};

const FILE_ACCEPT = '.pdf,.docx,.xlsx,.pptx,.txt,.md,.json,.csv';

/** Only PDF cannot be edited in the editor. .docx, .xlsx, .pptx are loaded as HTML; .html/.htm are loaded and edited as HTML (rendered). */
const BINARY_DOCUMENT_EXTENSIONS = /\.pdf$/i;
function isBinaryDocumentPath(path: string): boolean {
    return Boolean(path && BINARY_DOCUMENT_EXTENSIONS.test(path));
}
/** Office formats: load via as-html, save via save-docx / save-xlsx / save-pptx. */
function isDocxPath(path: string): boolean {
    return Boolean(path && /\.docx?$/i.test(path));
}
function isXlsxPath(path: string): boolean {
    return Boolean(path && /\.xlsx$/i.test(path));
}
function isPptxPath(path: string): boolean {
    return Boolean(path && /\.pptx$/i.test(path));
}
function isOfficePath(path: string): boolean {
    return isDocxPath(path) || isXlsxPath(path) || isPptxPath(path);
}

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

export default function DocumentEditor({
    isOpen,
    onClose,
    canClose = true,
    filePath = '',
    title = 'Document Editor',
    initialContent = '',
    mode = 'overlay',
    status = '',
    presence,
    steps = [],
    documents = [],
    onAddFiles,
    onRemoveDocument,
    onInsertSelection,
    insertedSelectionsCount = 0,
    insertedSelections = [],
}: DocumentEditorProps) {
    const [content, setContent] = useState<string>(initialContent);
    const [isLoading, setIsLoading] = useState(false);
    const [isSaving, setIsSaving] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [saveMessage, setSaveMessage] = useState<string | null>(null);
    const iframeRef = useRef<HTMLIFrameElement>(null);
    const fileInputRef = useRef<HTMLInputElement>(null);
    const attachmentsContentRef = useRef<HTMLDivElement>(null);
    const onInsertSelectionRef = useRef(onInsertSelection);
    onInsertSelectionRef.current = onInsertSelection;
    /** When true, the last content update came from the iframe (user typing). Skip rewriting iframe to avoid focus loss. */
    const contentFromIframeRef = useRef(false);
    /** Saved selection when user clicks toolbar (so we can restore and execCommand). */
    const savedSelectionRef = useRef<{ doc: Document; range: Range } | null>(null);
    const [selectedAttachmentId, setSelectedAttachmentId] = useState<string | null>(null);
    const [attachmentsDropdownOpen, setAttachmentsDropdownOpen] = useState(false);

    const isAttachmentsMode = Array.isArray(documents) && documents.length >= 0 && !filePath;

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
    const isBinaryDocument = isBinaryDocumentPath(filePath);

    const selectedAttachment = documents.find((d) => d.id === selectedAttachmentId) ?? documents[0];
    const attachmentDisplayContent = selectedAttachment?.content ?? '';
    const isAttachmentImage = selectedAttachment?.mimeType?.startsWith('image/');
    const attachmentContentWithHighlights = useMemo(() => {
        const text = attachmentDisplayContent || '(Kein Textinhalt)';
        const rangesForDoc = insertedSelections
            .map((s, i) => ({ start: s.start, end: s.end, colorIndex: i, documentId: s.documentId }))
            .filter((s) => s.documentId === selectedAttachment?.id)
            .map(({ start, end, colorIndex }) => ({ start, end, colorIndex }));
        if (rangesForDoc.length === 0) return null;
        const segments = buildHighlightSegments(text.length, rangesForDoc);
        const parts: React.ReactNode[] = [];
        let lastEnd = 0;
        for (let i = 0; i < segments.length; i++) {
            const seg = segments[i];
            if (seg.start > lastEnd) parts.push(text.slice(lastEnd, seg.start));
            parts.push(
                <span key={i} className={cn('rounded-sm', CHIP_BG_CLASSES[seg.colorIndex % CHIP_BG_CLASSES.length])}>
                    {text.slice(seg.start, seg.end)}
                </span>
            );
            lastEnd = seg.end;
        }
        if (lastEnd < text.length) parts.push(text.slice(lastEnd));
        return parts;
    }, [attachmentDisplayContent, selectedAttachment?.id, insertedSelections]);

    const handleAttachmentsContentMouseUp = () => {
        if (!onInsertSelection || !selectedAttachment) return;
        const sel = typeof window !== 'undefined' ? window.getSelection() : null;
        if (!sel || !attachmentsContentRef.current || !sel.rangeCount) return;
        const text = sel.toString().trim();
        if (!text) return;
        if (!attachmentsContentRef.current.contains(sel.anchorNode)) return;
        const pre = attachmentsContentRef.current.querySelector('pre');
        if (!pre) return;
        const r = sel.getRangeAt(0);
        const startRange = document.createRange();
        startRange.setStart(pre, 0);
        startRange.setEnd(r.startContainer, r.startOffset);
        const start = startRange.toString().length;
        const end = start + text.length;
        onInsertSelection(text, { start, end, documentId: selectedAttachment.id });
    };

    const handleAttachmentFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
        const files = e.target.files ? Array.from(e.target.files) : [];
        if (files.length && onAddFiles) onAddFiles(files);
        e.target.value = '';
    };

    useEffect(() => {
        if (isAttachmentsMode && documents.length > 0 && !selectedAttachmentId)
            setSelectedAttachmentId(documents[0].id);
        if (isAttachmentsMode && documents.length > 0 && !documents.find((d) => d.id === selectedAttachmentId))
            setSelectedAttachmentId(documents[0].id);
    }, [isAttachmentsMode, documents, selectedAttachmentId]);

    // Load content from file when opened (skip binary formats: Word, Excel, PDF, etc.)
    useEffect(() => {
        if (isOpen && filePath && !initialContent && !isBinaryDocument) {
            loadDocument();
        }
    }, [isOpen, filePath, isBinaryDocument]);

    // Update iframe content when content changes from outside (load/save); do NOT rewrite when user is typing (would steal focus)
    useEffect(() => {
        if (!iframeRef.current || !content) return;
        const doc = iframeRef.current.contentDocument;
        if (!doc) return;
        if (contentFromIframeRef.current) {
            contentFromIframeRef.current = false;
            return;
        }
        doc.open();
        doc.write(content);
        doc.close();
        doc.body.contentEditable = 'true';
        doc.body.style.outline = 'none';
        doc.body.style.padding = '20px';
        doc.body.addEventListener('input', () => {
            contentFromIframeRef.current = true;
            setContent(doc.body.innerHTML);
        });
        const handleMouseUp = () => {
            const fn = onInsertSelectionRef.current;
            if (!fn) return;
            try {
                const sel = doc.getSelection();
                if (!sel || sel.isCollapsed) return;
                const text = sel.toString().trim();
                if (text) fn(text, { start: 0, end: text.length, documentId: 'editor' });
            } catch {
                // cross-origin or no selection
            }
        };
        doc.body.addEventListener('mouseup', handleMouseUp);
        return () => {
            doc.body.removeEventListener('mouseup', handleMouseUp);
        };
    }, [content, isOpen]);

    const loadDocument = async () => {
        setIsLoading(true);
        setError(null);
        try {
            const base = getApiBase();
            const pathForUrl = (filePath || '').replace(/\\/g, '/');
            const url = isOfficePath(filePath)
                ? `${base}/api/file/as-html?path=${encodeURIComponent(pathForUrl)}`
                : `${base}/api/file?path=${encodeURIComponent(pathForUrl)}`;
            const response = await fetch(url);
            const text = await response.text();
            if (!response.ok) {
                const detail = text || response.statusText || 'Failed to load document';
                throw new Error(detail);
            }
            setContent(text);
        } catch (e) {
            setError(e instanceof Error ? e.message : 'Failed to load document');
        } finally {
            setIsLoading(false);
        }
    };

    const saveDocument = async () => {
        setIsSaving(true);
        setError(null);
        setSaveMessage(null);
        try {
            const base = getApiBase();
            const endpoint = isDocxPath(filePath)
                ? '/api/file/save-docx'
                : isXlsxPath(filePath)
                    ? '/api/file/save-xlsx'
                    : isPptxPath(filePath)
                        ? '/api/file/save-pptx'
                        : '/api/file/save';
            const response = await fetch(`${base}${endpoint}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: filePath, content }),
            });
            if (!response.ok) throw new Error('Failed to save document');
            setSaveMessage(filePath || 'Gespeichert.');
            setTimeout(() => setSaveMessage(null), 5000);
        } catch (e) {
            setError(e instanceof Error ? e.message : 'Failed to save document');
        } finally {
            setIsSaving(false);
        }
    };

    const openPrintDialog = () => {
        const iframe = iframeRef.current;
        if (!iframe?.contentWindow) return;
        iframe.contentWindow.focus();
        iframe.contentWindow.print();
    };

    const [isExportingPdf, setIsExportingPdf] = useState(false);
    const exportAsPDF = async () => {
        const iframe = iframeRef.current;
        const body = iframe?.contentDocument?.body;
        if (!body) return;
        setIsExportingPdf(true);
        try {
            const html2pdf = (await import('html2pdf.js')).default;
            const baseName = (displayFile || 'document').replace(/\.[^.]+$/, '') || 'document';
            const filename = `${baseName}.pdf`;
            await html2pdf().set({
                margin: 10,
                filename,
                image: { type: 'jpeg', quality: 0.98 },
                html2canvas: { scale: 2 },
            }).from(body).save();
        } catch (e) {
            setError(e instanceof Error ? e.message : 'PDF-Export fehlgeschlagen');
        } finally {
            setIsExportingPdf(false);
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

    const saveSelectionFromIframe = useCallback(() => {
        const doc = iframeRef.current?.contentDocument;
        if (!doc) return;
        const sel = doc.getSelection();
        if (!sel || sel.rangeCount === 0) return;
        const range = sel.getRangeAt(0).cloneRange();
        savedSelectionRef.current = { doc, range };
    }, []);

    const execEditorCommand = useCallback((command: string, value?: string) => {
        const doc = iframeRef.current?.contentDocument;
        const win = iframeRef.current?.contentWindow;
        if (!doc || !win) return;
        doc.body.focus();
        const sel = doc.getSelection();
        if (sel && savedSelectionRef.current?.doc === doc) {
            sel.removeAllRanges();
            sel.addRange(savedSelectionRef.current.range);
        }
        try {
            doc.execCommand(command, false, value ?? '');
            contentFromIframeRef.current = true;
            setContent(doc.body.innerHTML);
        } finally {
            savedSelectionRef.current = null;
        }
    }, []);

    const FONT_SIZES = [['1', '10 pt'], ['2', '11 pt'], ['3', '12 pt'], ['4', '14 pt'], ['5', '16 pt'], ['6', '18 pt'], ['7', '24 pt']] as const;
    const FONT_FAMILIES = ['Arial', 'Helvetica', 'Times New Roman', 'Georgia', 'Courier New', 'Verdana'];

    const toolbar = (
        <div
            className="flex flex-wrap items-center gap-0.5 border-b border-gray-200 bg-gray-50 px-2 py-1 shrink-0"
            onMouseDown={saveSelectionFromIframe}
        >
            <button type="button" onClick={() => execEditorCommand('bold')} className="p-1.5 rounded hover:bg-gray-200" title="Fett"><Bold size={16} /></button>
            <button type="button" onClick={() => execEditorCommand('italic')} className="p-1.5 rounded hover:bg-gray-200" title="Kursiv"><Italic size={16} /></button>
            <button type="button" onClick={() => execEditorCommand('underline')} className="p-1.5 rounded hover:bg-gray-200" title="Unterstrichen"><Underline size={16} /></button>
            <span className="w-px h-5 bg-gray-300 mx-0.5" />
            <select
                className="text-xs border border-gray-300 rounded px-1.5 py-1 bg-white min-w-[4rem]"
                onChange={(e) => execEditorCommand('fontSize', e.target.value)}
                onMouseDown={saveSelectionFromIframe}
            >
                {FONT_SIZES.map(([val, label]) => <option key={val} value={val}>{label}</option>)}
            </select>
            <select
                className="text-xs border border-gray-300 rounded px-1.5 py-1 bg-white min-w-[7rem]"
                onChange={(e) => execEditorCommand('fontName', e.target.value)}
                onMouseDown={saveSelectionFromIframe}
            >
                {FONT_FAMILIES.map((f) => <option key={f} value={f}>{f}</option>)}
            </select>
            <span className="w-px h-5 bg-gray-300 mx-0.5" />
            <input
                type="color"
                className="w-7 h-7 cursor-pointer border border-gray-300 rounded p-0.5 bg-white"
                defaultValue="#000000"
                onMouseDown={saveSelectionFromIframe}
                onChange={(e) => execEditorCommand('foreColor', e.target.value)}
                title="Schriftfarbe"
            />
            <button type="button" onClick={() => execEditorCommand('backColor', '#ffff00')} className="p-1.5 rounded hover:bg-gray-200" title="Markieren"><Highlighter size={16} /></button>
            <span className="w-px h-5 bg-gray-300 mx-0.5" />
            <button type="button" onClick={() => execEditorCommand('justifyLeft')} className="p-1.5 rounded hover:bg-gray-200" title="Links"><AlignLeft size={16} /></button>
            <button type="button" onClick={() => execEditorCommand('justifyCenter')} className="p-1.5 rounded hover:bg-gray-200" title="Zentriert"><AlignCenter size={16} /></button>
            <button type="button" onClick={() => execEditorCommand('justifyRight')} className="p-1.5 rounded hover:bg-gray-200" title="Rechts"><AlignRight size={16} /></button>
            <span className="w-px h-5 bg-gray-300 mx-0.5" />
            <button type="button" onClick={() => execEditorCommand('insertUnorderedList')} className="p-1.5 rounded hover:bg-gray-200" title="Aufzählung"><List size={16} /></button>
            <button type="button" onClick={() => execEditorCommand('insertOrderedList')} className="p-1.5 rounded hover:bg-gray-200" title="Nummerierung"><ListOrdered size={16} /></button>
            <span className="w-px h-5 bg-gray-300 mx-0.5" />
            <button type="button" onClick={() => execEditorCommand('removeFormat')} className="p-1.5 rounded hover:bg-gray-200" title="Formatierung entfernen"><Eraser size={16} /></button>
            <span className="w-px h-5 bg-gray-300 mx-0.5" />
            <button type="button" onClick={saveDocument} disabled={isSaving} className="flex items-center gap-1.5 px-2.5 py-1 rounded text-xs font-medium text-gray-600 bg-white border border-gray-300 hover:bg-gray-100 disabled:opacity-50" title="Speichern">
                {isSaving ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}
                Save
            </button>
            <button type="button" onClick={openPrintDialog} className="flex items-center gap-1.5 px-2.5 py-1 rounded text-xs font-medium text-gray-600 bg-white border border-gray-300 hover:bg-gray-100" title="Drucken (Browser-Dialog)">
                <Printer size={14} />
                Print
            </button>
            <button type="button" onClick={exportAsPDF} disabled={isExportingPdf} className="flex items-center gap-1.5 px-2.5 py-1 rounded text-xs font-medium text-white bg-blue-500 hover:bg-blue-600 disabled:opacity-50" title="Als PDF-Datei herunterladen">
                {isExportingPdf ? <Loader2 size={14} className="animate-spin" /> : <Download size={14} />}
                PDF
            </button>
            <button type="button" onClick={downloadHTML} className="flex items-center gap-1.5 px-2.5 py-1 rounded text-xs font-medium text-gray-600 bg-white border border-gray-300 hover:bg-gray-100" title="Als HTML herunterladen">
                <Download size={14} />
                Download
            </button>
            {saveMessage && (
                <span className="text-[10px] text-emerald-600 font-mono ml-1 truncate max-w-[200px]" title={saveMessage}>Gespeichert: {saveMessage}</span>
            )}
        </div>
    );

    if (!isOpen && mode === 'overlay') return null;

    // Dock mode – attachments (no right-side list) or kernel editor
    if (mode === 'dock') {
        if (isAttachmentsMode) {
            const hasDocuments = documents.length > 0;
            return (
                <div
                    className={cn(
                        'relative h-full w-full overflow-hidden rounded-2xl border border-gray-200 bg-[#F7F8FA] transition-all duration-300 ease-out',
                        isOpen ? 'translate-x-0 opacity-100' : 'translate-x-8 opacity-0 pointer-events-none'
                    )}
                    aria-hidden={!isOpen}
                >
                    <div className="flex h-full w-full flex-col min-w-0 bg-[#F9FAFB] rounded-2xl overflow-hidden">
                        <div className="flex h-12 items-center justify-between border-b border-gray-200 bg-white px-4 shrink-0">
                            <div className="flex items-center gap-3 min-w-0">
                                <div className="flex h-7 w-7 items-center justify-center rounded-md border border-gray-200 bg-white text-blue-600 shrink-0">
                                    <FileText size={14} />
                                </div>
                                <div className="min-w-0">
                                    <div className="text-xs font-semibold text-gray-900 truncate">{title}</div>
                                    <div className="flex items-center gap-2 text-[10px] text-gray-500">
                                        <span className="h-1.5 w-1.5 rounded-full bg-gray-400 shrink-0" />
                                        <span className="uppercase">Ready</span>
                                    </div>
                                </div>
                                <div className="relative flex items-center gap-1">
                                    <span className="text-[10px] text-gray-500 uppercase tracking-wide shrink-0">Anhänge</span>
                                    <button
                                        type="button"
                                        onClick={() => setAttachmentsDropdownOpen((o) => !o)}
                                        className="flex items-center gap-1 rounded-md border border-gray-200 bg-white px-2 py-1 text-xs text-gray-700 hover:bg-gray-50 min-w-0 max-w-[180px]"
                                    >
                                        <span className="truncate">{selectedAttachment?.name ?? 'Kein Dokument'}</span>
                                        <ChevronDown size={12} className="shrink-0" />
                                    </button>
                                    {attachmentsDropdownOpen && (
                                        <>
                                            <div className="fixed inset-0 z-10" aria-hidden onClick={() => setAttachmentsDropdownOpen(false)} />
                                            <div className="absolute left-0 top-full mt-1 z-20 w-56 max-h-48 overflow-y-auto rounded-lg border border-gray-200 bg-white shadow-lg py-1">
                                                <button
                                                    type="button"
                                                    onClick={() => { fileInputRef.current?.click(); setAttachmentsDropdownOpen(false); }}
                                                    className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs font-medium text-gray-700 hover:bg-blue-50"
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
                                                    onChange={handleAttachmentFileChange}
                                                />
                                                {documents.map((doc) => (
                                                    <div
                                                        key={doc.id}
                                                        className={cn(
                                                            'flex items-center gap-2 px-3 py-2 group',
                                                            selectedAttachment?.id === doc.id ? 'bg-blue-50' : 'hover:bg-gray-50'
                                                        )}
                                                    >
                                                        <button
                                                            type="button"
                                                            onClick={() => { setSelectedAttachmentId(doc.id); setAttachmentsDropdownOpen(false); }}
                                                            className="flex-1 min-w-0 text-left text-xs truncate text-gray-900"
                                                        >
                                                            {doc.name}
                                                        </button>
                                                        {onRemoveDocument && (
                                                            <button
                                                                type="button"
                                                                onClick={(e) => { e.stopPropagation(); onRemoveDocument(doc.id); if (selectedAttachment?.id === doc.id) setSelectedAttachmentId(documents.filter((d) => d.id !== doc.id)[0]?.id ?? null); }}
                                                                className="rounded p-1 text-gray-400 opacity-0 group-hover:opacity-100 hover:bg-red-50 hover:text-red-600"
                                                                aria-label={`Remove ${doc.name}`}
                                                            >
                                                                <Trash2 size={12} />
                                                            </button>
                                                        )}
                                                    </div>
                                                ))}
                                            </div>
                                        </>
                                    )}
                                </div>
                            </div>
                            <button
                                onClick={onClose}
                                className="rounded-full p-1 text-gray-400 transition hover:bg-gray-100 hover:text-gray-600 shrink-0"
                                aria-label="Close"
                            >
                                <X size={14} />
                            </button>
                        </div>
                        <div className="flex h-9 items-center gap-2 border-b border-gray-100 bg-white/80 px-4 text-xs text-gray-500 shrink-0">
                            <span className="truncate font-mono text-[11px]">{selectedAttachment?.name ?? 'Kein Dokument ausgewählt'}</span>
                        </div>
                        <div className="flex-1 min-h-0 min-w-0 overflow-hidden p-4 flex flex-col">
                            <div className="flex flex-1 min-h-0 min-w-0 flex-col overflow-hidden rounded-xl border border-gray-200 bg-white">
                                <div
                                    ref={attachmentsContentRef}
                                    onMouseUp={handleAttachmentsContentMouseUp}
                                    className="flex-1 min-h-0 min-w-0 overflow-auto bg-white p-4 w-full"
                                >
                                    {!hasDocuments ? (
                                        <div className="flex flex-col items-center justify-center h-full gap-2 text-gray-400 text-sm">
                                            <FileText size={32} className="opacity-50" />
                                            <p>Keine Dokumente. Klicke auf &quot;Dokument hinzufügen&quot;, um Anhänge zu öffnen.</p>
                                            <p className="text-xs">Der Assistent kann dann auf deren Inhalt antworten.</p>
                                        </div>
                                    ) : !selectedAttachment ? (
                                        <div className="text-gray-400 text-sm">Dokument auswählen.</div>
                                    ) : isAttachmentImage && selectedAttachment.content?.startsWith('data:') ? (
                                        <img src={selectedAttachment.content} alt={selectedAttachment.name} className="max-w-full h-auto" />
                                    ) : (
                                        <pre
                                            className={cn(
                                                'w-full max-w-full whitespace-pre-wrap break-words font-sans text-sm text-gray-800',
                                                INSERTION_COLOR_CLASSES[insertedSelectionsCount % INSERTION_COLOR_CLASSES.length]
                                            )}
                                        >
                                            {attachmentContentWithHighlights ?? (attachmentDisplayContent || '(Kein Textinhalt)')}
                                        </pre>
                                    )}
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            );
        }

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
                        {/* Header: icon, filename (black) + path (gray), status, buttons */}
                        <div className="flex h-12 items-center justify-between border-b border-gray-200 bg-white px-4 gap-2 min-w-0">
                            <div className="flex items-center gap-3 min-w-0 flex-1">
                                <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-gray-200 bg-white text-blue-600">
                                    <FileText size={14} />
                                </div>
                                <div className="min-w-0 flex-1 flex flex-col">
                                    <div className="flex items-center gap-2 text-xs min-w-0">
                                        <span className="font-semibold text-gray-900 shrink-0">{displayFile || title}</span>
                                        <span className="text-gray-400 truncate font-mono text-[11px]" title={filePath || undefined}>{filePath || 'No file selected'}</span>
                                    </div>
                                    <div className="flex items-center gap-2 text-[10px] text-gray-500">
                                        <span className={cn("h-1.5 w-1.5 rounded-full shrink-0", presenceTone)} />
                                        {status ? (
                                            <span className="text-gray-500">{status}</span>
                                        ) : (
                                            <span className="uppercase">{presenceLabel}</span>
                                        )}
                                    </div>
                                </div>
                            </div>
                            <div className="flex items-center gap-2 shrink-0">
                                {isBinaryDocument && (
                                    <a
                                        href={`${getApiBase()}/api/file?path=${encodeURIComponent(filePath)}`}
                                        download={displayFile}
                                        className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-blue-500 hover:bg-blue-600 rounded-md transition-colors"
                                    >
                                        <Download size={14} />
                                        Download
                                    </a>
                                )}
                                <button
                                    onClick={onClose}
                                    className="rounded-full p-1 text-gray-400 transition hover:bg-gray-100 hover:text-gray-600"
                                    aria-label="Close"
                                >
                                    <X size={14} />
                                </button>
                            </div>
                        </div>

                        {/* Content area – no padding, inner editor fills all space */}
                        <div className="flex-1 min-h-0 overflow-hidden flex flex-col">
                            {isBinaryDocument ? (
                                <div className="flex flex-1 flex-col items-center justify-center gap-4 p-6 text-center bg-white">
                                    <FileText size={48} className="text-gray-300" />
                                    <p className="text-sm text-gray-600 max-w-md">
                                        PDF-Dateien können hier nicht bearbeitet werden.
                                    </p>
                                    <p className="text-xs text-gray-500">
                                        Word (.docx), Excel (.xlsx), PowerPoint (.pptx) und HTML (.html, .htm) werden im Editor als HTML geladen und bearbeitet.
                                    </p>
                                    <p className="font-mono text-[11px] text-gray-400 break-all px-2">{filePath}</p>
                                    <a
                                        href={`${getApiBase()}/api/file?path=${encodeURIComponent(filePath)}`}
                                        download={displayFile}
                                        className="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium text-white bg-blue-500 hover:bg-blue-600 rounded-lg transition-colors"
                                    >
                                        <Download size={16} />
                                        Datei herunterladen
                                    </a>
                                </div>
                            ) : isLoading ? (
                                <div className="flex flex-1 items-center justify-center gap-2 text-gray-300 bg-white">
                                    <Loader2 size={14} className="animate-spin opacity-50" />
                                    <span className="text-xs">Loading document...</span>
                                </div>
                            ) : error ? (
                                <div className="flex flex-1 flex-col items-center justify-center gap-2 bg-white">
                                    <p className="text-red-500 text-xs">{error}</p>
                                    <button
                                        onClick={loadDocument}
                                        className="text-xs text-blue-500 hover:underline"
                                    >
                                        Try again
                                    </button>
                                </div>
                            ) : (
                                <>
                                    {toolbar}
                                    <iframe
                                        ref={iframeRef}
                                        className="flex-1 w-full min-h-0 border-0 bg-white block"
                                        title="Document Editor"
                                        sandbox="allow-same-origin allow-scripts allow-modals"
                                    />
                                </>
                            )}
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
                    <div className="flex h-16 items-center justify-between border-b border-gray-200 bg-white px-6 gap-2 min-w-0">
                        <div className="flex items-center gap-3 min-w-0 flex-1">
                            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-blue-500 text-white shadow-sm">
                                <FileText size={18} />
                            </div>
                            <div className="min-w-0 flex-1 flex flex-col">
                                <div className="flex items-center gap-2 text-sm min-w-0">
                                    <span className="font-semibold text-gray-900 shrink-0">{displayFile || title}</span>
                                    <span className="text-gray-400 truncate font-mono text-xs" title={filePath || undefined}>{filePath || 'No file selected'}</span>
                                </div>
                                <div className="flex items-center gap-2 text-xs text-gray-500">
                                    <span className={cn("h-1.5 w-1.5 rounded-full shrink-0", presenceTone)} />
                                    {status ? (
                                        <span className="text-gray-500">{status}</span>
                                    ) : (
                                        <span className="uppercase">{presenceLabel}</span>
                                    )}
                                </div>
                            </div>
                        </div>
                        <div className="flex items-center gap-2 shrink-0">
                            {isBinaryDocument && (
                                <a
                                    href={`${getApiBase()}/api/file?path=${encodeURIComponent(filePath)}`}
                                    download={displayFile}
                                    className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-blue-500 hover:bg-blue-600 rounded-lg transition-colors"
                                >
                                    <Download size={14} />
                                    Download
                                </a>
                            )}
                            <button
                                onClick={onClose}
                                className="rounded-full p-2 text-gray-400 transition hover:bg-gray-100 hover:text-gray-600"
                                aria-label="Close"
                            >
                                <X size={16} />
                            </button>
                        </div>
                    </div>

                    {/* Content area – no padding, editor fills all space */}
                    <div className="flex-1 min-h-0 overflow-hidden flex flex-col">
                        {isBinaryDocument ? (
                            <div className="flex flex-1 flex-col items-center justify-center gap-4 p-8 text-center bg-white">
                                <FileText size={56} className="text-gray-300" />
                                <p className="text-sm text-gray-600 max-w-md">
                                    PDF-Dateien können hier nicht bearbeitet werden.
                                </p>
                                <p className="text-xs text-gray-500">
                                    Word (.docx), Excel (.xlsx), PowerPoint (.pptx) und HTML (.html, .htm) werden im Editor als HTML geladen und bearbeitet.
                                </p>
                                <p className="font-mono text-xs text-gray-400 break-all px-2">{filePath}</p>
                                <a
                                    href={`${getApiBase()}/api/file?path=${encodeURIComponent(filePath)}`}
                                    download={displayFile}
                                    className="inline-flex items-center gap-2 px-5 py-2.5 text-sm font-medium text-white bg-blue-500 hover:bg-blue-600 rounded-lg transition-colors"
                                >
                                    <Download size={18} />
                                    Datei herunterladen
                                </a>
                            </div>
                        ) : isLoading ? (
                            <div className="flex flex-1 items-center justify-center gap-2 text-gray-300 bg-white">
                                <Loader2 size={28} className="animate-spin opacity-50" />
                                <span className="text-xs">Loading document...</span>
                            </div>
                        ) : error ? (
                            <div className="flex flex-1 flex-col items-center justify-center gap-2 bg-white">
                                <p className="text-red-500 text-sm">{error}</p>
                                <button
                                    onClick={loadDocument}
                                    className="text-sm text-blue-500 hover:underline"
                                >
                                    Try again
                                </button>
                            </div>
                        ) : (
                            <>
                                {toolbar}
                                <iframe
                                    ref={iframeRef}
                                    className="flex-1 w-full min-h-0 border-0 bg-white block"
                                    title="Document Editor"
                                    sandbox="allow-same-origin allow-scripts allow-modals"
                                />
                            </>
                        )}
                    </div>
                </div>
            </div>
        </div>
    );
}
