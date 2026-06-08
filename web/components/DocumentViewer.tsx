'use client';

import React, { useState, useRef, useEffect } from 'react';
import dynamic from 'next/dynamic';
import { marked } from 'marked';
import mammoth from 'mammoth';
import { X, FileText, Plus, Trash2, ChevronRight, ChevronLeft, List, Loader2 } from 'lucide-react';
import { cn } from '@/lib/utils';

const PdfWithHighlights = dynamic(() => import('@/components/PdfWithHighlights'), { ssr: false });

/** Check if document is DOCX with raw data for client-side conversion. */
function isDocxWithData(doc: DocumentViewerDocument): boolean {
    if (!doc.data) return false;
    const name = (doc.name || '').toLowerCase();
    return name.endsWith('.docx');
}

const AUTO_COLLAPSE_MS = 3000;

export type DocumentViewerDocument = {
    id: string;
    name: string;
    mimeType?: string;
    content?: string;
    /** Base64 or data-URL for raw file; used to display original PDF. */
    data?: string;
    /** Pre-rendered HTML for Office docs (docx/xlsx/pptx) from backend. */
    htmlContent?: string;
};

/** One inserted selection: text plus range in a specific document, for persistent highlight. */
export type InsertedSelectionRange = {
    text: string;
    start: number;
    end: number;
    documentId: string;
    /** PDF page number (1-based) where selection was made; used for per-page highlight matching. */
    pageNumber?: number;
    /** Exact PDF text item indices for precise highlighting; avoids text-search issues. */
    itemIndices?: number[];
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
    onInsertSelection?: (text: string, range: { start: number; end: number; documentId: string; pageNumber?: number; itemIndices?: number[] }) => void;
    /** Number of selections already inserted; used for next selection color when selecting. */
    insertedSelectionsCount?: number;
    /** All inserted selections; used to render persistent highlights in the current document. */
    insertedSelections?: InsertedSelectionRange[];
    /** Attachment RAG indexing status, shown in the header (undefined = idle). */
    indexStatus?: 'indexing' | 'ready' | 'error';
};

const FILE_ACCEPT = '.pdf,.docx,.xlsx,.pptx,.txt,.md,.json,.csv,.html,.htm';

/** Extract raw HTML from content (librarian wraps in ``` or content may be raw HTML). */
function extractHtmlContent(content: string): string | null {
    if (!content || typeof content !== 'string') return null;
    const s = content.trim();
    if (/<!doctype\s+html/i.test(s) || /<html[\s>]/i.test(s)) {
        const start = s.search(/<!doctype\s+html|<html[\s>]/i);
        if (start >= 0) {
            const endMatch = s.slice(start).match(/<\/html\s*>/i);
            return endMatch
                ? s.slice(start, start + endMatch.index! + endMatch[0].length)
                : s.slice(start);
        }
    }
    const codeBlock = s.match(/```\n?([\s\S]*?)\n?```/);
    if (codeBlock) {
        const inner = codeBlock[1].trim();
        if (/<!doctype\s+html|<html[\s>]/i.test(inner)) return inner;
    }
    return null;
}

/** Check if document is HTML (by name or content). */
function isHtmlDocument(doc: DocumentViewerDocument): boolean {
    const name = (doc.name || '').toLowerCase();
    if (name.endsWith('.html') || name.endsWith('.htm')) return true;
    return !!extractHtmlContent(doc.content ?? '');
}

/** Check if document is a PDF with raw data for original display. */
function isPdfWithData(doc: DocumentViewerDocument): boolean {
    if (!doc.data) return false;
    const name = (doc.name || '').toLowerCase();
    const mime = (doc.mimeType || '').toLowerCase();
    return name.endsWith('.pdf') || mime === 'application/pdf';
}

/** Check if document has pre-rendered HTML (docx/xlsx/pptx from backend). */
function isOfficeWithHtml(doc: DocumentViewerDocument): boolean {
    if (!doc.htmlContent) return false;
    const name = (doc.name || '').toLowerCase();
    return name.endsWith('.docx') || name.endsWith('.xlsx') || name.endsWith('.pptx');
}

/** Check if document is Markdown. */
function isMarkdownDocument(doc: DocumentViewerDocument): boolean {
    const name = (doc.name || '').toLowerCase();
    return name.endsWith('.md') || name.endsWith('.markdown');
}

/** Build data URL for PDF from base64 or existing data URL. */
function pdfDataUrl(doc: DocumentViewerDocument): string {
    const d = doc.data || '';
    if (d.startsWith('data:')) return d;
    return `data:application/pdf;base64,${d}`;
}

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

/** Hex colors for inline highlight spans in iframe (matches CHIP_BG_CLASSES). */
const INSERTION_HIGHLIGHT_COLORS: { bg: string; text: string }[] = [
    { bg: '#1f2937', text: '#ffffff' },
    { bg: '#f97316', text: '#ffffff' },
    { bg: '#ec4899', text: '#ffffff' },
    { bg: '#3b82f6', text: '#ffffff' },
    { bg: '#059669', text: '#ffffff' },
];

function getTextNodesInOrder(root: Node): { node: Text; start: number; end: number }[] {
    const result: { node: Text; start: number; end: number }[] = [];
    let offset = 0;
    const walk = (node: Node) => {
        if (node.nodeType === Node.TEXT_NODE) {
            const len = (node.textContent || '').length;
            if (len > 0) {
                result.push({ node: node as Text, start: offset, end: offset + len });
                offset += len;
            }
        } else {
            for (let i = 0; i < node.childNodes.length; i++) walk(node.childNodes[i]);
        }
    };
    walk(root);
    return result;
}

function injectHighlightsInBody(
    body: HTMLElement,
    segments: { start: number; end: number; colorIndex: number }[],
    doc: Document
): void {
    const existing = body.querySelectorAll('span[data-highlight]');
    existing.forEach((span) => {
        const parent = span.parentNode;
        if (!parent) return;
        while (span.firstChild) parent.insertBefore(span.firstChild, span);
        parent.removeChild(span);
    });
    if (segments.length === 0) return;
    const textNodes = getTextNodesInOrder(body);
    const totalLen = textNodes.length ? textNodes[textNodes.length - 1].end : 0;
    if (totalLen === 0) return;
    for (const seg of segments) {
        const segStart = Math.max(0, seg.start);
        const segEnd = Math.min(totalLen, seg.end);
        if (segEnd <= segStart) continue;
        const overlapping: { node: Text; localStart: number; localEnd: number }[] = [];
        for (const { node, start, end } of textNodes) {
            const overlapStart = Math.max(segStart, start);
            const overlapEnd = Math.min(segEnd, end);
            if (overlapEnd <= overlapStart) continue;
            overlapping.push({
                node,
                localStart: overlapStart - start,
                localEnd: overlapEnd - start,
            });
        }
        for (const { node, localStart, localEnd } of overlapping.reverse()) {
            const range = doc.createRange();
            try {
                range.setStart(node, localStart);
                range.setEnd(node, localEnd);
            } catch {
                continue;
            }
            let fragment: DocumentFragment | null = null;
            try {
                fragment = range.extractContents();
                if (!fragment || fragment.childNodes.length === 0) {
                    if (fragment) range.insertNode(fragment);
                    continue;
                }
                const span = doc.createElement('span');
                span.setAttribute('data-highlight', String(seg.colorIndex));
                const colors = INSERTION_HIGHLIGHT_COLORS[seg.colorIndex % INSERTION_HIGHLIGHT_COLORS.length];
                span.style.setProperty('background-color', colors.bg, 'important');
                span.style.setProperty('color', colors.text, 'important');
                span.style.setProperty('padding', '0 2px', 'important');
                span.appendChild(fragment);
                range.insertNode(span);
            } catch {
                if (fragment) try { range.insertNode(fragment); } catch { /* restore */ }
            }
        }
    }
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

/** Selection colors for ::selection in iframe (matches CHIP_BG_CLASSES). */
const IFRAME_SELECTION_COLORS = [
    { bg: '#1f2937', text: '#ffffff' },
    { bg: '#f97316', text: '#ffffff' },
    { bg: '#ec4899', text: '#ffffff' },
    { bg: '#3b82f6', text: '#ffffff' },
    { bg: '#059669', text: '#ffffff' },
];

/** HTML document in iframe: selection → onInsertSelection + persistent highlight injection (from Editor). */
function HtmlDocumentIframe({
    doc,
    htmlContent,
    onInsertSelection,
    insertedSelections,
    insertedSelectionsCount = 0,
}: {
    doc: DocumentViewerDocument;
    htmlContent: string;
    onInsertSelection?: (text: string, range: { start: number; end: number; documentId: string }) => void;
    insertedSelections: InsertedSelectionRange[];
    insertedSelectionsCount?: number;
}) {
    const iframeRef = React.useRef<HTMLIFrameElement>(null);
    const [iframeLoaded, setIframeLoaded] = React.useState(false);

    const applyHighlights = React.useCallback(() => {
        const iframe = iframeRef.current;
        const body = iframe?.contentDocument?.body;
        if (!body) return;
        const rangesForDoc = insertedSelections
            .map((s, i) => ({ start: s.start, end: s.end, colorIndex: i, documentId: s.documentId }))
            .filter((s) => s.documentId === doc.id)
            .map(({ start, end, colorIndex }) => ({ start, end, colorIndex }));
        const textLen = (body.textContent || '').length;
        const segments = buildHighlightSegments(textLen, rangesForDoc);
        injectHighlightsInBody(body, segments, body.ownerDocument);
    }, [doc.id, insertedSelections]);

    React.useEffect(() => {
        if (iframeLoaded) applyHighlights();
    }, [iframeLoaded, applyHighlights]);

    React.useEffect(() => {
        if (!iframeLoaded) return;
        const iframe = iframeRef.current;
        const iframeDoc = iframe?.contentDocument;
        if (!iframeDoc?.head) return;
        let styleEl = iframeDoc.querySelector('style[data-selection-colors]');
        if (!styleEl) {
            styleEl = iframeDoc.createElement('style');
            styleEl.setAttribute('data-selection-colors', '1');
            iframeDoc.head.appendChild(styleEl);
        }
        const colors = IFRAME_SELECTION_COLORS[insertedSelectionsCount % IFRAME_SELECTION_COLORS.length];
        styleEl.textContent = `*::selection { background-color: ${colors.bg} !important; color: ${colors.text} !important; }`;
    }, [iframeLoaded, insertedSelectionsCount]);

    React.useEffect(() => {
        if (!iframeLoaded || !onInsertSelection) return;
        const iframe = iframeRef.current;
        const body = iframe?.contentDocument?.body;
        if (!body) return;

        const handleMouseUp = () => {
            const iframeDoc = body.ownerDocument;
            const sel = iframeDoc.getSelection();
            if (!sel || sel.isCollapsed) return;
            const r = sel.getRangeAt(0);
            const text = r.toString().trim();
            if (!text) return;
            const startRange = iframeDoc.createRange();
            startRange.selectNodeContents(body);
            startRange.setEnd(r.startContainer, r.startOffset);
            const start = startRange.toString().length;
            const end = start + text.length;
            onInsertSelection(text, { start, end, documentId: doc.id });
            sel.removeAllRanges();
        };

        body.addEventListener('mouseup', handleMouseUp);
        return () => body.removeEventListener('mouseup', handleMouseUp);
    }, [iframeLoaded, doc.id, onInsertSelection]);

    const handleIframeLoad = React.useCallback(() => setIframeLoaded(true), []);

    return (
        <iframe
            ref={iframeRef}
            srcDoc={htmlContent}
            title={doc.name}
            className="w-full flex-1 min-h-[400px] border-0 rounded"
            sandbox="allow-same-origin"
            onLoad={handleIframeLoad}
        />
    );
}

/** Extract whole blocks in order. Tables, p, h1-h6, ul, ol, div stay intact (never split). */
function extractDocxBlocks(html: string): string[] {
    const s = (html || '').trim();
    if (!s) return [];
    const re = /<table[\s\S]*?<\/table>|<p[\s\S]*?<\/p>|<h[1-6][^>]*>[\s\S]*?<\/h[1-6]>|<ul[\s\S]*?<\/ul>|<ol[\s\S]*?<\/ol>/gi;
    const blocks: string[] = [];
    let match;
    let lastEnd = 0;
    while ((match = re.exec(s)) !== null) {
        const before = s.slice(lastEnd, match.index).trim();
        if (before) blocks.push(before);
        blocks.push(match[0]);
        lastEnd = re.lastIndex;
    }
    const rest = s.slice(lastEnd).trim();
    if (rest) blocks.push(rest);
    return blocks;
}

const DOCX_BLOCKS_PER_PAGE = 28;

/** Paginate DOCX into DIN A4 pages. Tables stay whole. Document-like styling. */
function wrapDocxAsDocument(bodyHtml: string): string {
    const blocks = extractDocxBlocks((bodyHtml || '').trim());
    const style = `
        html,body,*{box-sizing:border-box !important;}
        body{margin:0;padding:16px 0;min-height:100%;background:#d1d5db !important;
            font-family:'Times New Roman',Times,serif;font-size:11pt;line-height:1.35;color:#000;}
        .docx-doc{display:flex;flex-direction:column;align-items:center;gap:24px;width:210mm;margin:0 auto;}
        .docx-page-wrap{display:flex;flex-direction:column;align-items:flex-start;width:210mm;}
        .docx-page{width:210mm;min-height:297mm;background:#fff;box-shadow:0 2px 10px rgba(0,0,0,0.12);padding:25mm;}
        .docx-page-label{font-size:11px;color:#6b7280;margin-bottom:6px;}
        h1{font-size:18pt;margin:0 0 12pt;font-weight:bold;}
        h2,h3{font-size:12pt;margin:12pt 0 6pt;font-weight:bold;}
        p{margin:0 0 6pt;}
        table{border-collapse:collapse;width:100%;margin:6pt 0;}td,th{border:1px solid #333;padding:4pt 6pt;vertical-align:top;}
        ul,ol{margin:0 0 6pt;padding-left:20pt;}
        .docx-page u,.docx-placeholder{text-decoration:underline;text-underline-offset:1px;}
    `;
    if (blocks.length === 0) {
        return `<!DOCTYPE html><html><head><meta charset="utf-8"/><style>${style}</style></head><body><div class="docx-doc"><div class="docx-page-wrap"><span class="docx-page-label">Seite 1 von 1</span><div class="docx-page"><p></p></div></div></div></body></html>`;
    }
    const pageCount = Math.max(1, Math.ceil(blocks.length / DOCX_BLOCKS_PER_PAGE));
    const pages: string[] = [];
    for (let p = 0; p < pageCount; p++) {
        let chunk = blocks.slice(p * DOCX_BLOCKS_PER_PAGE, (p + 1) * DOCX_BLOCKS_PER_PAGE).join('');
        chunk = chunk.replace(/\[([^\]]+)\]/g, '<u class="docx-placeholder">[$1]</u>');
        pages.push(`<div class="docx-page-wrap"><span class="docx-page-label">Seite ${p + 1} von ${pageCount}</span><div class="docx-page">${chunk}</div></div>`);
    }
    return `<!DOCTYPE html><html><head><meta charset="utf-8"/><style>${style}</style></head><body><div class="docx-doc">${pages.join('')}</div></body></html>`;
}

/** DOCX viewer using mammoth.js (BSD-2-Clause) – converts in browser, no backend needed. */
function DocxMammothViewer({
    doc,
    onInsertSelection,
    insertedSelections,
    insertedSelectionsCount = 0,
}: {
    doc: DocumentViewerDocument;
    onInsertSelection?: (text: string, range: { start: number; end: number; documentId: string }) => void;
    insertedSelections: InsertedSelectionRange[];
    insertedSelectionsCount?: number;
}) {
    const [htmlContent, setHtmlContent] = React.useState<string | null>(doc.htmlContent || null);
    const [error, setError] = React.useState<string | null>(null);
    const [loading, setLoading] = React.useState(!doc.htmlContent && !!doc.data);

    React.useEffect(() => {
        if (doc.htmlContent) {
            setHtmlContent(doc.htmlContent);
            setLoading(false);
            setError(null);
            return;
        }
        if (!doc.data) {
            setLoading(false);
            setError('Keine Dateidaten');
            return;
        }
        let cancelled = false;
        setLoading(true);
        setError(null);
        const base64 = doc.data.startsWith('data:') ? doc.data.split(',', 2)[1] || '' : doc.data;
        const binary = atob(base64);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
        mammoth
            .convertToHtml({ arrayBuffer: bytes.buffer })
            .then((result) => {
                if (cancelled) return;
                const fullHtml = wrapDocxAsDocument(result.value);
                setHtmlContent(fullHtml);
                setLoading(false);
            })
            .catch((err) => {
                if (cancelled) return;
                setError(err?.message || 'Konvertierung fehlgeschlagen');
                setLoading(false);
                setHtmlContent(null);
            });
        return () => { cancelled = true; };
    }, [doc.id, doc.data, doc.htmlContent]);

    if (loading) {
        return (
            <div className="flex flex-1 min-h-[300px] items-center justify-center gap-2 text-gray-500">
                <Loader2 size={20} className="animate-spin" />
                <span className="text-sm">Dokument wird geladen…</span>
            </div>
        );
    }
    if (error || !htmlContent) {
        return (
            <div className="flex flex-1 min-h-[200px] items-center justify-center text-sm text-amber-600">
                {error || '(Kein Textinhalt)'}
            </div>
        );
    }
    return (
        <HtmlDocumentIframe
            doc={doc}
            htmlContent={htmlContent}
            onInsertSelection={onInsertSelection}
            insertedSelections={insertedSelections}
            insertedSelectionsCount={insertedSelectionsCount}
        />
    );
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
    indexStatus,
}: DocumentViewerProps) {
    const [selectedId, setSelectedId] = useState<string | null>(null);
    const [listExpanded, setListExpanded] = useState(true);
    const fileInputRef = useRef<HTMLInputElement>(null);
    const contentAreaRef = useRef<HTMLDivElement>(null);
    const autoCollapseRef = useRef<ReturnType<typeof setTimeout> | null>(null);

    const handleContentMouseUp = () => {
        if (!onInsertSelection) return;
        const sel = typeof window !== 'undefined' ? window.getSelection() : null;
        if (!sel || !contentAreaRef.current || !sel.rangeCount) return;
        const text = sel.toString().trim();
        if (!text) return;
        if (!contentAreaRef.current.contains(sel.anchorNode)) return;
        const pres = contentAreaRef.current.querySelectorAll('pre[data-document-id]');
        let pre: Element | null = null;
        for (const p of pres) {
            if (p.contains(sel.anchorNode)) {
                pre = p;
                break;
            }
        }
        if (!pre) return;
        const documentId = pre.getAttribute('data-document-id') ?? '';
        if (!documentId) return;
        const r = sel.getRangeAt(0);
        const startRange = document.createRange();
        startRange.setStart(pre, 0);
        startRange.setEnd(r.startContainer, r.startOffset);
        const start = startRange.toString().length;
        const end = start + text.length;
        onInsertSelection(text, { start, end, documentId });
        sel.removeAllRanges();
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
        if (!selectedId || !contentAreaRef.current) return;
        const el = contentAreaRef.current.querySelector(`[data-document-id="${selectedId}"]`);
        if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }, [selectedId]);

    const hasDocuments = documents.length > 0;
    const selectedDoc = documents.find((d) => d.id === selectedId) ?? documents[0];

    const getContentWithHighlightsForDoc = React.useCallback(
        (doc: DocumentViewerDocument) => {
            const content = doc.content ?? '(Kein Textinhalt)';
            const rangesForDoc = insertedSelections
                .map((s, i) => ({ start: s.start, end: s.end, colorIndex: i, documentId: s.documentId }))
                .filter((s) => s.documentId === doc.id)
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
        },
        [insertedSelections]
    );

    const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
        const files = e.target.files ? Array.from(e.target.files) : [];
        if (files.length) {
            onAddFiles(files);
        }
        e.target.value = '';
    };

    const idxDotClass = indexStatus === 'indexing' ? 'bg-amber-500'
        : indexStatus === 'error' ? 'bg-red-500'
            : indexStatus === 'ready' ? 'bg-green-500'
                : 'bg-gray-400';
    const idxLabelClass = indexStatus === 'indexing' ? 'text-amber-600'
        : indexStatus === 'error' ? 'text-red-600'
            : indexStatus === 'ready' ? 'text-green-600'
                : '';
    const idxLabel = indexStatus === 'indexing' ? 'Indexiere…'
        : indexStatus === 'error' ? 'Fehler'
            : indexStatus === 'ready' ? 'Bereit'
                : 'Ready';

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
                <div className="relative flex flex-row h-full w-full min-w-0">
                    <div
                        className={cn(
                            'flex flex-1 flex-col min-w-0 bg-[#F9FAFB] rounded-r-2xl overflow-hidden transition-[padding] duration-300 ease-out',
                            !listExpanded && 'pr-12'
                        )}
                    >
                        <div className="flex h-12 items-center justify-between gap-3 border-b border-gray-200 bg-white px-4 shrink-0">
                            <div className="flex items-center gap-3 min-w-0">
                                <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-gray-200 bg-white text-blue-600">
                                    <FileText size={14} />
                                </div>
                                <div className="min-w-0">
                                    <div className="text-xs font-semibold text-gray-900">{title}</div>
                                    <div className="flex items-center gap-2 text-[10px] text-gray-500 flex-wrap">
                                        <span
                                            className="relative flex h-1.5 w-1.5 shrink-0"
                                            title={indexStatus === 'indexing' ? 'Anhänge werden für die Suche aufbereitet' : undefined}
                                        >
                                            {indexStatus === 'indexing' && (
                                                <span className="absolute inline-flex h-full w-full rounded-full bg-amber-500 opacity-75 animate-ping" />
                                            )}
                                            <span className={cn('relative inline-flex h-1.5 w-1.5 rounded-full', idxDotClass)} />
                                        </span>
                                        <span className={cn('uppercase', idxLabelClass)}>{idxLabel}</span>
                                        <span className="text-gray-300">·</span>
                                        <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-gray-500 shrink-0">
                                            Anhänge
                                        </span>
                                        <span className="truncate font-mono text-[11px] text-gray-600">
                                            {documents.length === 0
                                                ? 'Kein Dokument'
                                                : documents.length === 1
                                                    ? documents[0].name
                                                    : `${documents.length} Dokumente (durchscrollen)`}
                                        </span>
                                    </div>
                                </div>
                            </div>
                            <button
                                onClick={onClose}
                                className="rounded-full p-1 text-gray-400 shrink-0 transition hover:bg-gray-100 hover:text-gray-600"
                                aria-label="Close"
                            >
                                <X size={14} />
                            </button>
                        </div>
                        <div className="flex-1 min-h-0 min-w-0 flex flex-col overflow-hidden">
                            <div
                                ref={contentAreaRef}
                                onMouseUp={handleContentMouseUp}
                                className="flex-1 min-h-0 min-w-0 overflow-auto bg-[#d1d5db] w-full"
                            >
                                {!hasDocuments ? (
                                    <div className="flex flex-col items-center justify-center h-full min-h-[200px] gap-2 text-gray-400 text-sm">
                                        <FileText size={32} className="opacity-50" />
                                        <p>Keine Dokumente. Klicke auf &quot;Dokument hinzufügen&quot;, um Anhänge zu öffnen.</p>
                                        <p className="text-xs">Der Assistent kann dann auf deren Inhalt antworten.</p>
                                    </div>
                                ) : (
                                    <div className="min-h-full flex flex-col items-center py-4 px-2 gap-6">
                                        {documents.map((doc, index) => {
                                            const isImg = doc.mimeType?.startsWith('image/') && doc.content?.startsWith('data:');
                                            const htmlContent = isHtmlDocument(doc) ? extractHtmlContent(doc.content ?? '') : null;
                                            const officeHtml = isOfficeWithHtml(doc) ? doc.htmlContent! : null;
                                            const mdHtml = isMarkdownDocument(doc) && doc.content
                                                ? `<div class="markdown-body">${marked(doc.content, { async: false })}</div>`
                                                : null;
                                            const displayHtml = htmlContent ?? (isDocxWithData(doc) ? null : officeHtml) ?? (mdHtml ? `<!DOCTYPE html><html><head><meta charset="utf-8"/><link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/github-markdown-css/5.2.0/github-markdown.min.css"/><style>.markdown-body{box-sizing:border-box;min-width:200px;max-width:980px;margin:0 auto;padding:45px;}</style></head><body>${mdHtml}</body></html>` : null);
                                            const showPdf = isPdfWithData(doc);
                                            const showDocx = isDocxWithData(doc);
                                            const showAsDocument = showPdf || showDocx || !!displayHtml;
                                            return (
                                                <div
                                                    key={`${doc.id}-${index}`}
                                                    className={cn(
                                                        'flex justify-center',
                                                        showAsDocument ? 'w-full max-w-4xl' : 'w-[210mm] max-w-full'
                                                    )}
                                                    data-document-id={doc.id}
                                                >
                                                    <div
                                                        className={cn(
                                                            'box-border rounded-sm flex flex-col',
                                                            showAsDocument ? 'w-full min-h-[calc(100vh-6rem)] p-2 pt-0 bg-transparent' : 'w-[210mm] max-w-full min-h-[297mm] py-[25mm] px-[25mm] bg-white shadow-sm',
                                                        )}
                                                        style={showAsDocument ? undefined : {
                                                            backgroundImage: 'repeating-linear-gradient(to bottom, transparent 0, transparent 297mm, rgba(0,0,0,0.06) 297mm, rgba(0,0,0,0.06) 298mm)',
                                                        }}
                                                    >
                                                        <div className="text-[10px] text-gray-400 font-mono mb-2 shrink-0 flex items-center justify-between gap-2">
                                                            <span>{doc.name}</span>
                                                            {onInsertSelection && (
                                                                <span className="text-[9px] text-gray-400" title="Text markieren → farbiger Anhang an Chat">Markieren → Anhang</span>
                                                            )}
                                                        </div>
                                                        {isImg ? (
                                                            <div className="flex flex-1 min-h-0 items-center justify-center">
                                                                <img src={doc.content} alt={doc.name} className="max-w-full max-h-full object-contain rounded shadow-sm" />
                                                            </div>
                                                        ) : showPdf ? (
                                                            <PdfWithHighlights
                                                                src={pdfDataUrl(doc)}
                                                                title={doc.name}
                                                                className="flex-1 min-h-0"
                                                                insertedSelections={insertedSelections}
                                                                nextSelectionColorIndex={insertedSelections?.length ?? 0}
                                                                documentId={doc.id}
                                                                content={doc.content}
                                                                onInsertSelection={onInsertSelection}
                                                            />
                                                        ) : showDocx ? (
                                                            <DocxMammothViewer
                                                                doc={doc}
                                                                onInsertSelection={onInsertSelection}
                                                                insertedSelections={insertedSelections}
                                                                insertedSelectionsCount={insertedSelectionsCount}
                                                            />
                                                        ) : displayHtml ? (
                                                            <HtmlDocumentIframe
                                                                doc={doc}
                                                                htmlContent={displayHtml}
                                                                onInsertSelection={onInsertSelection}
                                                                insertedSelections={insertedSelections}
                                                                insertedSelectionsCount={insertedSelectionsCount}
                                                            />
                                                        ) : (
                                                            <pre
                                                                data-document-id={doc.id}
                                                                className={cn(
                                                                    'w-full max-w-full whitespace-pre-wrap break-words font-sans text-sm text-gray-800 m-0 flex-1 min-h-0',
                                                                    INSERTION_COLOR_CLASSES[insertedSelectionsCount % INSERTION_COLOR_CLASSES.length]
                                                                )}
                                                            >
                                                                {getContentWithHighlightsForDoc(doc) ?? (doc.content ?? '(Kein Textinhalt)')}
                                                            </pre>
                                                        )}
                                                    </div>
                                                </div>
                                            );
                                        })}
                                    </div>
                                )}
                            </div>
                        </div>
                    </div>

                    <div
                        className={cn(
                            'absolute top-0 right-0 bottom-0 z-10 flex flex-col border-l border-gray-200 overflow-hidden bg-white shadow-[-4px_0_12px_rgba(0,0,0,0.08)]',
                            'transition-[width] duration-300 ease-out',
                            listExpanded ? 'w-[280px]' : 'w-12'
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
                                <div className="flex-1 overflow-y-auto px-4 py-3 flex flex-col gap-2 min-h-0 min-w-0">
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
                                    {documents.map((doc, index) => (
                                        <div
                                            key={`${doc.id}-${index}`}
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
                            <div className="flex flex-col items-center py-3 gap-2 h-full bg-white">
                                <button
                                    type="button"
                                    onClick={() => setListExpanded(true)}
                                    className="flex flex-col items-center gap-1.5 rounded-lg p-2 text-gray-600 hover:bg-gray-300 hover:text-gray-800 transition w-full"
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
            <div className="relative flex flex-row h-[90vh] w-full max-w-[1500px] overflow-hidden rounded-2xl bg-[#F3F4F6] shadow-2xl min-w-0">
                <div
                    className={cn(
                        'flex flex-1 flex-col min-w-0 bg-[#F9FAFB] overflow-hidden transition-[padding] duration-300 ease-out',
                        !listExpanded && 'pr-12'
                    )}
                >
                    <div className="flex h-14 shrink-0 items-center justify-between gap-3 border-b border-gray-200 bg-white px-6">
                        <div className="flex items-center gap-3 min-w-0">
                            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-blue-500 text-white shadow-sm">
                                <FileText size={18} />
                            </div>
                            <div className="min-w-0">
                                <div className="text-sm font-semibold text-gray-900">{title}</div>
                                <div className="flex items-center gap-2 text-xs text-gray-500 flex-wrap">
                                    <span>Ready</span>
                                    <span className="text-gray-300">·</span>
                                    <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-gray-500">
                                        Anhänge
                                    </span>
                                    <span className="truncate font-mono text-gray-600">
                                        {documents.length === 0 ? '—' : documents.length === 1 ? documents[0].name : `${documents.length} Dokumente (durchscrollen)`}
                                    </span>
                                </div>
                            </div>
                        </div>
                        <button
                            onClick={onClose}
                            className="rounded-full p-2 shrink-0 text-gray-400 hover:bg-gray-100 hover:text-gray-600"
                            aria-label="Close"
                        >
                            <X size={16} />
                        </button>
                    </div>
                    <div className="flex-1 min-h-0 min-w-0 flex flex-col overflow-hidden">
                        <div
                            ref={contentAreaRef}
                            onMouseUp={handleContentMouseUp}
                            className="flex-1 min-h-0 min-w-0 overflow-auto bg-[#d1d5db] w-full"
                        >
                            {!hasDocuments ? (
                                <div className="flex flex-col items-center justify-center h-full min-h-[280px] gap-2 text-gray-400 text-sm">
                                    <FileText size={40} className="opacity-50" />
                                    <p>Keine Dokumente. Dokument hinzufügen, um Anhänge zu öffnen.</p>
                                </div>
                            ) : (
                                <div className="min-h-full flex flex-col items-center py-6 px-4 gap-6">
                                    {documents.map((doc, index) => {
                                        const isImg = doc.mimeType?.startsWith('image/') && doc.content?.startsWith('data:');
                                        const htmlContent = isHtmlDocument(doc) ? extractHtmlContent(doc.content ?? '') : null;
                                        const officeHtml = isOfficeWithHtml(doc) ? doc.htmlContent! : null;
                                        const mdHtml = isMarkdownDocument(doc) && doc.content
                                            ? `<div class="markdown-body">${marked(doc.content, { async: false })}</div>`
                                            : null;
                                        const displayHtml = htmlContent ?? (isDocxWithData(doc) ? null : officeHtml) ?? (mdHtml ? `<!DOCTYPE html><html><head><meta charset="utf-8"/><link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/github-markdown-css/5.2.0/github-markdown.min.css"/><style>.markdown-body{box-sizing:border-box;min-width:200px;max-width:980px;margin:0 auto;padding:45px;}</style></head><body>${mdHtml}</body></html>` : null);
                                        const showPdfOverlay = isPdfWithData(doc);
                                        const showDocxOverlay = isDocxWithData(doc);
                                        const showAsDocumentOverlay = showPdfOverlay || showDocxOverlay || !!displayHtml;
                                        return (
                                            <div
                                                key={`${doc.id}-${index}`}
                                                className={cn(
                                                    'flex justify-center',
                                                    showAsDocumentOverlay ? 'w-full max-w-4xl' : 'w-[210mm] max-w-full'
                                                )}
                                                data-document-id={doc.id}
                                            >
                                                <div
                                                    className={cn(
                                                        'box-border rounded-sm flex flex-col',
                                                        showAsDocumentOverlay ? 'w-full min-h-[calc(100vh-8rem)] p-2 pt-0 bg-transparent' : 'w-[210mm] max-w-full min-h-[297mm] py-[25mm] px-[25mm] bg-white shadow-sm'
                                                    )}
                                                    style={showAsDocumentOverlay ? undefined : {
                                                        backgroundImage: 'repeating-linear-gradient(to bottom, transparent 0, transparent 297mm, rgba(0,0,0,0.06) 297mm, rgba(0,0,0,0.06) 298mm)',
                                                    }}
                                                >
                                                    <div className="text-xs text-gray-400 font-mono mb-2 shrink-0">
                                                        {doc.name}
                                                    </div>
                                                    {isImg ? (
                                                        <div className="flex flex-1 min-h-0 items-center justify-center">
                                                            <img src={doc.content} alt={doc.name} className="max-w-full max-h-full object-contain rounded shadow-sm" />
                                                        </div>
                                                    ) : showPdfOverlay ? (
                                                        <PdfWithHighlights
                                                            src={pdfDataUrl(doc)}
                                                            title={doc.name}
                                                            className="flex-1 min-h-0"
                                                            insertedSelections={insertedSelections}
                                                            nextSelectionColorIndex={insertedSelections?.length ?? 0}
                                                            documentId={doc.id}
                                                            content={doc.content}
                                                            onInsertSelection={onInsertSelection}
                                                        />
                                                    ) : showDocxOverlay ? (
                                                        <DocxMammothViewer
                                                            doc={doc}
                                                            onInsertSelection={onInsertSelection}
                                                            insertedSelections={insertedSelections}
                                                            insertedSelectionsCount={insertedSelectionsCount}
                                                        />
                                                    ) : displayHtml ? (
                                                        <HtmlDocumentIframe
                                                            doc={doc}
                                                            htmlContent={displayHtml}
                                                            onInsertSelection={onInsertSelection}
                                                            insertedSelections={insertedSelections}
                                                            insertedSelectionsCount={insertedSelectionsCount}
                                                        />
                                                    ) : (
                                                        <pre
                                                            data-document-id={doc.id}
                                                            className={cn(
                                                                'w-full max-w-full whitespace-pre-wrap break-words font-sans text-sm text-gray-800 m-0 flex-1 min-h-0',
                                                                INSERTION_COLOR_CLASSES[insertedSelectionsCount % INSERTION_COLOR_CLASSES.length]
                                                            )}
                                                        >
                                                            {getContentWithHighlightsForDoc(doc) ?? (doc.content ?? '(Kein Textinhalt)')}
                                                        </pre>
                                                    )}
                                                </div>
                                            </div>
                                        );
                                    })}
                                </div>
                            )}
                        </div>
                    </div>
                </div>
                <div
                    className={cn(
                        'absolute top-0 right-0 bottom-0 z-10 flex flex-col border-l border-gray-200 overflow-hidden bg-white shadow-[-4px_0_12px_rgba(0,0,0,0.08)]',
                        'transition-[width] duration-300 ease-out',
                        listExpanded ? 'w-[320px]' : 'w-12'
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
                                    'flex-1 overflow-y-auto px-5 py-4 flex flex-col gap-3 min-h-0 min-w-0'
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
                                {documents.map((doc, index) => (
                                    <div
                                        key={`${doc.id}-${index}`}
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
                        <div className="flex flex-col items-center py-4 gap-2 h-full bg-white">
                            <button
                                type="button"
                                onClick={() => setListExpanded(true)}
                                className="flex flex-col items-center gap-2 rounded-lg p-2 text-gray-600 hover:bg-gray-300 hover:text-gray-800 transition w-full"
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
                                <span className="text-xs text-gray-500 tabular-nums">{documents.length}</span>
                            )}
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
}
