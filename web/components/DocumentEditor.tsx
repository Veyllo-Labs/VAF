'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import React, { useState, useEffect, useRef, useMemo, useCallback } from 'react';
import dynamic from 'next/dynamic';
import { X, Download, FileText, Save, Loader2, CheckCircle2, Circle, Plus, Trash2, ChevronDown, Bold, Italic, Underline, List, ListOrdered, AlignLeft, AlignCenter, AlignRight, Highlighter, Eraser, Printer } from 'lucide-react';
import { cn, getApiBase } from '@/lib/utils';
import { CHIP_BG_CLASSES, INSERTION_COLOR_CLASSES } from '@/components/DocumentViewer';
import NativeDocxEditor from '@/components/NativeDocxEditor';
import type { NativeDocxDocument } from '@/lib/docxNative';

const PdfWithHighlights = dynamic(() => import('@/components/PdfWithHighlights'), { ssr: false });

/** Document for attachments mode (extracted text/image). */
export type DocumentEditorAttachment = {
    id: string;
    name: string;
    mimeType?: string;
    content?: string;
    /** Base64 or data-URL for raw file; used to display original PDF. */
    data?: string;
};

/** Check if attachment is a PDF with raw data for original display. */
function isPdfWithData(doc: DocumentEditorAttachment): boolean {
    if (!doc.data) return false;
    const name = (doc.name || '').toLowerCase();
    const mime = (doc.mimeType || '').toLowerCase();
    return name.endsWith('.pdf') || mime === 'application/pdf';
}

/** Build data URL for PDF from base64 or existing data URL. */
function pdfDataUrl(doc: DocumentEditorAttachment): string {
    const d = doc.data || '';
    if (d.startsWith('data:')) return d;
    return `data:application/pdf;base64,${d}`;
}

/** One inserted selection for quote chips and persistent highlight. */
export type InsertedSelectionRange = {
    text: string;
    start: number;
    end: number;
    documentId: string;
    pageNumber?: number;
    itemIndices?: number[];
};

export type DocumentEditorProps = {
    isOpen: boolean;
    onClose: () => void;
    canClose?: boolean;
    /** Kernel editor: path to file to edit. When set, attachments props are ignored. */
    filePath?: string;
    title?: string;
    initialContent?: string;
    /** Called whenever content changes (typing, load, toolbar). Parent can store per-session to restore on chat switch. */
    onContentChange?: (content: string) => void;
    initialDocxModel?: NativeDocxDocument | null;
    onDocxModelChange?: (document: NativeDocxDocument) => void;
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
    onInsertSelection?: (text: string, range: { start: number; end: number; documentId: string; pageNumber?: number; itemIndices?: number[] }) => void;
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
function isMarkdownPath(path: string): boolean {
    return Boolean(path && /\.(md|mdx|markdown)$/i.test(path));
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

/** Same colors as CHIP_BG – applied via setProperty(..., 'important') so they win in the iframe. */
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

/** Injected in document head. The .md / HTML editor is ONE continuous A4-width sheet
 *  that grows naturally ("infinitely" long) — no JS pagination (real pagination lives in
 *  the native .docx editor now). Print uses native CSS paged media so the browser
 *  fragments the flow across physical A4 pages line-by-line (no dead space). */
const A4_EDITOR_STYLE = `
                html, body, body * { box-sizing: border-box !important; }
                html, body { scrollbar-width: none !important; -ms-overflow-style: none !important; }
                html::-webkit-scrollbar, body::-webkit-scrollbar { display: none !important; }
                html { width: 100% !important; margin: 0 !important; padding: 0 !important; background: #e5e7eb !important; }

                /* body IS the continuous A4-width sheet, centered on a gray canvas. */
                html body {
                    width: 210mm !important; max-width: 210mm !important;
                    margin: 16px auto !important; padding: 25mm !important;
                    background: #ffffff !important; box-shadow: 0 2px 10px rgba(0,0,0,0.18) !important;
                    min-height: 297mm !important; overflow: visible !important; outline: none !important;
                }
                html body > * { max-width: 100% !important; }
                html body > *:first-child { margin-top: 0 !important; }

                /* Print: native paged media. Paragraphs fragment line-by-line across pages
                   (NO break-inside:avoid on <p>) -> no dead space in the PDF. */
                @page { size: A4; margin: 25mm; }
                @media print {
                    html { background: #ffffff !important; }
                    html body { width: auto !important; max-width: none !important; margin: 0 !important;
                        padding: 0 !important; box-shadow: none !important; min-height: 0 !important; }
                    body h1, body h2, body h3, body h4, body h5, body h6,
                    body li, body tr, body img, body figure, body blockquote, body pre { break-inside: avoid !important; }
                    body h1, body h2, body h3 { break-after: avoid !important; }
                    body thead { display: table-header-group !important; }
                }
            `;

/** Default iframe height is ~150px; expand so stacked A4 sheets are visible (outer panel scrolls). */
function resizeEditorIframe(iframe: HTMLIFrameElement | null): void {
    if (!iframe) return;
    const d = iframe.contentDocument;
    if (!d?.documentElement) return;
    const sh = Math.max(d.documentElement.scrollHeight, d.body?.scrollHeight ?? 0, 320);
    iframe.style.height = `${Math.ceil(sh)}px`;
}

function ensureA4EditorStyles(doc: Document): void {
    if (doc.querySelector('style[data-a4]')) return;
    const style = doc.createElement('style');
    style.setAttribute('data-a4', '1');
    style.textContent = A4_EDITOR_STYLE;
    const head = doc.head;
    if (head) head.appendChild(style);
    else doc.documentElement.appendChild(style);
}

/**
 * Research exports are full HTML documents. Feeding that string to doc.write() can yield fragile iframe layout;
 * we inject only body inner HTML plus original head styles so the editor shell (center + A4) is predictable.
 */
function extractEditorBodyAndHeadStyles(html: string): { bodyInner: string; headInjectHtml: string } {
    const t = html.trim();
    if (typeof window === 'undefined') {
        return { bodyInner: html, headInjectHtml: '' };
    }
    if (!/^\s*<!doctype\b|<html[\s>]/i.test(t)) {
        return { bodyInner: html, headInjectHtml: '' };
    }
    try {
        const parsed = new DOMParser().parseFromString(t, 'text/html');
        const headBits = Array.from(
            parsed.head.querySelectorAll('style, link[rel="stylesheet"], meta[charset], meta[name="viewport"]')
        )
            .map((el) => el.outerHTML)
            .join('');
        return { bodyInner: parsed.body.innerHTML, headInjectHtml: headBits };
    } catch {
        return { bodyInner: html, headInjectHtml: '' };
    }
}


function injectHighlightsInBody(body: HTMLElement, segments: { start: number; end: number; colorIndex: number }[], doc: Document): void {
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

export default function DocumentEditor(props: DocumentEditorProps) {
    if (isDocxPath(props.filePath || '')) {
        return (
            <NativeDocxEditor
                isOpen={props.isOpen}
                onClose={props.onClose}
                canClose={props.canClose}
                filePath={props.filePath || ''}
                title={props.title || 'Document Editor'}
                initialModel={props.initialDocxModel}
                onModelChange={props.onDocxModelChange}
                onInsertSelection={
                    props.onInsertSelection
                        ? (text, range) => props.onInsertSelection?.(text, range)
                        : undefined
                }
                insertedSelections={props.insertedSelections}
            />
        );
    }

    return <LegacyDocumentEditor {...props} />;
}

function LegacyDocumentEditor({
    isOpen,
    onClose,
    canClose = true,
    filePath = '',
    title = 'Document Editor',
    initialContent = '',
    onContentChange,
    initialDocxModel,
    onDocxModelChange,
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
    /**
     * Sync from parent when content is pushed from outside (e.g. agent replace_editor_selection).
     * Parent often keeps `content` undefined/'' while `loadDocument()` runs — must not wipe fetched HTML.
     */
    useEffect(() => {
        if (!isOpen || initialContent === undefined) return;
        if (initialContent === content) return;
        const ic = (initialContent || '').trim();
        // Parent often keeps '' while loadDocument() fills state — never wipe fetched HTML with empty prop.
        if (ic === '' && content.trim().length > 0) return;
        setContent(initialContent);
    }, [isOpen, initialContent, content]);
    const [isLoading, setIsLoading] = useState(false);
    const [isSaving, setIsSaving] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [saveMessage, setSaveMessage] = useState<string | null>(null);
    /** Current selection format so toolbar reflects it (like Word). */
    const [selectionFormat, setSelectionFormat] = useState<{
        fontName: string;
        fontSize: string;
        foreColor: string;
        bold: boolean;
        italic: boolean;
        underline: boolean;
        justifyLeft: boolean;
        justifyCenter: boolean;
        justifyRight: boolean;
    }>({
        fontName: 'Arial',
        fontSize: '3',
        foreColor: '#000000',
        bold: false,
        italic: false,
        underline: false,
        justifyLeft: true,
        justifyCenter: false,
        justifyRight: false,
    });
    const iframeRef = useRef<HTMLIFrameElement>(null);
    const fileInputRef = useRef<HTMLInputElement>(null);
    const attachmentsContentRef = useRef<HTMLDivElement>(null);
    const onInsertSelectionRef = useRef(onInsertSelection);
    onInsertSelectionRef.current = onInsertSelection;
    const insertedSelectionsRef = useRef(insertedSelections);
    insertedSelectionsRef.current = insertedSelections;
    const onContentChangeRef = useRef(onContentChange);
    onContentChangeRef.current = onContentChange;
    /** When user requests close, show browser confirm and only call onClose if they confirm. */
    const handleRequestClose = useCallback(() => {
        if (typeof window === 'undefined') {
            onClose();
            return;
        }
        const message = 'Do you really want to close the document editor? Unsaved changes may be lost.';
        if (window.confirm(message)) {
            onClose();
        }
    }, [onClose]);
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
        const text = attachmentDisplayContent || '(No text content)';
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

    // Load content from file when opened. Skip load when initialContent is set (restored from session).
    useEffect(() => {
        if (isOpen && filePath && !initialContent && !isBinaryDocument) {
            loadDocument();
        }
    }, [isOpen, filePath, isBinaryDocument, initialContent]);

    const FONT_SIZES = [['1', '10 pt'], ['2', '11 pt'], ['3', '12 pt'], ['4', '14 pt'], ['5', '16 pt'], ['6', '18 pt'], ['7', '24 pt']] as const;
    const FONT_FAMILIES = ['Arial', 'Helvetica', 'Times New Roman', 'Georgia', 'Courier New', 'Verdana'];

    /** Read current selection format from iframe and update toolbar state (Word-style). */
    const updateSelectionFormat = useCallback(() => {
        const doc = iframeRef.current?.contentDocument;
        if (!doc) return;
        const fontName = (doc.queryCommandValue('fontName') || '').replace(/['"]/g, '') || 'Arial';
        const fontSize = doc.queryCommandValue('fontSize') || '3';
        let foreColor = doc.queryCommandValue('foreColor') || '#000000';
        if (foreColor.startsWith('rgb')) {
            const m = foreColor.match(/\d+/g);
            if (m && m.length >= 3)
                foreColor = '#' + [Number(m[0]), Number(m[1]), Number(m[2])].map(x => x.toString(16).padStart(2, '0')).join('');
        }
        if (!foreColor.startsWith('#')) foreColor = '#000000';
        setSelectionFormat({
            fontName: FONT_FAMILIES.includes(fontName) ? fontName : 'Arial',
            fontSize: ['1', '2', '3', '4', '5', '6', '7'].includes(fontSize) ? fontSize : '3',
            foreColor,
            bold: doc.queryCommandState('bold'),
            italic: doc.queryCommandState('italic'),
            underline: doc.queryCommandState('underline'),
            justifyLeft: doc.queryCommandState('justifyLeft'),
            justifyCenter: doc.queryCommandState('justifyCenter'),
            justifyRight: doc.queryCommandState('justifyRight'),
        });
    }, []);

    // Update iframe content when content changes from outside (load/save); do NOT rewrite when user is typing (would steal focus)
    useEffect(() => {
        if (!iframeRef.current || !content) return;
        const doc = iframeRef.current.contentDocument;
        if (!doc) return;
        if (contentFromIframeRef.current) {
            contentFromIframeRef.current = false;
            return;
        }
        const { bodyInner, headInjectHtml } = extractEditorBodyAndHeadStyles(content);
        doc.open();
        doc.write(
            `<!DOCTYPE html><html><head><meta charset="utf-8"/>${headInjectHtml}</head><body></body></html>`
        );
        doc.close();
        // The body itself IS the continuous, editable A4-width sheet — no JS pagination.
        doc.body.innerHTML = bodyInner;
        ensureA4EditorStyles(doc);
        doc.body.contentEditable = 'true';
        doc.body.style.outline = 'none';

        // Size the iframe to the content once layout settles (and after webfonts/images),
        // so the outer panel scrolls through the one long sheet.
        const sizeIframe = () => resizeEditorIframe(iframeRef.current);
        requestAnimationFrame(() => requestAnimationFrame(sizeIframe));
        const lateTimers = [120, 400, 800, 1500].map((ms) => window.setTimeout(sizeIframe, ms));
        try { doc.fonts?.ready?.then(sizeIframe).catch(() => { /* ignore */ }); } catch { /* ignore */ }

        const focusBodyOnMouseDown = (e: MouseEvent) => {
            if (doc.body.contains(e.target as Node)) doc.body.focus();
        };
        doc.body.addEventListener('mousedown', focusBodyOnMouseDown);
        const captureContent = () => {
            contentFromIframeRef.current = true;
            const html = doc.body.innerHTML;   // body IS the flow — no decorations to strip
            setContent(html);
            onContentChangeRef.current?.(html);
            sizeIframe();
        };
        doc.body.addEventListener('input', captureContent);
        const handleMouseUp = () => {
            const fn = onInsertSelectionRef.current;
            if (!fn) return;
            try {
                const sel = doc.getSelection();
                if (!sel || sel.isCollapsed) return;
                const r = sel.getRangeAt(0);
                const text = r.toString().trim();
                if (!text) return;
                const root = doc.body as Node;
                if (!root.contains(r.startContainer) || !root.contains(r.endContainer)) return;
                const startRange = doc.createRange();
                startRange.selectNodeContents(root);
                startRange.setEnd(r.startContainer, r.startOffset);
                const start = startRange.toString().length;
                const end = start + text.length;
                const overlapsExisting = (insertedSelectionsRef.current || []).some(
                    (s) => s.documentId === 'editor' && start < s.end && end > s.start
                );
                if (overlapsExisting) return;
                fn(text, { start, end, documentId: 'editor' });
            } catch {
                // cross-origin or no selection
            }
        };
        let selectionChangeTimeoutId: ReturnType<typeof setTimeout> | null = null;
        const handleSelectionChange = () => {
            if (selectionChangeTimeoutId) clearTimeout(selectionChangeTimeoutId);
            selectionChangeTimeoutId = setTimeout(() => {
                selectionChangeTimeoutId = null;
                updateSelectionFormat();
            }, 50);
        };
        doc.body.addEventListener('mouseup', handleMouseUp);
        doc.addEventListener('selectionchange', handleSelectionChange);

        return () => {
            if (selectionChangeTimeoutId) clearTimeout(selectionChangeTimeoutId);
            lateTimers.forEach((t) => clearTimeout(t));
            doc.body.removeEventListener('mousedown', focusBodyOnMouseDown);
            doc.body.removeEventListener('input', captureContent);
            doc.body.removeEventListener('mouseup', handleMouseUp);
            doc.removeEventListener('selectionchange', handleSelectionChange);
        };
    }, [content, isOpen, updateSelectionFormat]);

    // ::selection color for next selection (same as DocumentViewer HtmlDocumentIframe)
    useEffect(() => {
        if (!iframeRef.current || !content || !isOpen) return;
        const iframeDoc = iframeRef.current.contentDocument;
        if (!iframeDoc?.head) return;
        let styleEl = iframeDoc.querySelector('style[data-selection-colors]');
        if (!styleEl) {
            styleEl = iframeDoc.createElement('style');
            styleEl.setAttribute('data-selection-colors', '1');
            iframeDoc.head.appendChild(styleEl);
        }
        const colors = INSERTION_HIGHLIGHT_COLORS[(insertedSelectionsCount ?? 0) % INSERTION_HIGHLIGHT_COLORS.length];
        styleEl.textContent = `*::selection { background-color: ${colors.bg} !important; color: ${colors.text} !important; }`;
    }, [content, isOpen, insertedSelectionsCount]);

    // Apply highlights when insertedSelections or content changes – do NOT rewrite iframe (same as DocumentViewer)
    // Run always (even when empty) to clear highlights on deletion. Delay only when content changed (pagination).
    const prevContentForHighlightsRef = useRef<string | null>(null);
    useEffect(() => {
        if (!iframeRef.current || !content || !isOpen) return;
        const doc = iframeRef.current.contentDocument;
        if (!doc?.body) return;
        const root = doc.body as HTMLElement;
        if (!root) return;
        const rangesForEditor = (insertedSelections || [])
            .map((s, i) => ({ start: s.start, end: s.end, colorIndex: i }))
            .filter((_, i) => (insertedSelections?.[i]?.documentId ?? '') === 'editor');
        const contentChanged = prevContentForHighlightsRef.current !== content;
        prevContentForHighlightsRef.current = content;
        const apply = () => {
            const bodyEl = root as HTMLElement;
            const segments = buildHighlightSegments((bodyEl.textContent || '').length, rangesForEditor);
            injectHighlightsInBody(bodyEl, segments, doc);
        };
        const delay = contentChanged ? 100 : 0;
        const id = setTimeout(() => requestAnimationFrame(apply), delay);
        return () => clearTimeout(id);
    }, [insertedSelections, content, isOpen]);

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
            let text = await response.text();
            if (!response.ok) {
                const detail = text || response.statusText || 'Failed to load document';
                throw new Error(detail);
            }
            if (isMarkdownPath(filePath)) {
                // Render Markdown (research/document reports) instead of showing raw
                // source text; saving converts the edited HTML back to Markdown on
                // the backend (/api/file/save).
                const { marked } = await import('marked');
                text = await marked.parse(text, { gfm: true, breaks: false });
            }
            setContent(text);
            onContentChangeRef.current?.(text);
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
            setSaveMessage(filePath || 'Saved.');
            setTimeout(() => setSaveMessage(null), 5000);
        } catch (e) {
            setError(e instanceof Error ? e.message : 'Failed to save document');
        } finally {
            setIsSaving(false);
        }
    };

    // Native bridges exposed by the desktop window (QtWebEngine). Undefined in a
    // real browser, where the iframe's own print dialog / html2pdf are used.
    const getDesktopApi = () => (window as unknown as {
        pywebview?: { api?: {
            render_pdf?: (html: string, name: string, mode: string) => Promise<unknown>;
            save_text_as?: (content: string, name: string) => Promise<unknown>;
        } };
    }).pywebview?.api;
    const printBaseName = () => (displayFile || 'document').replace(/\.[^.]+$/, '') || 'document';
    // Full rendered HTML of the editor iframe (head A4 styles + continuous body) — the
    // desktop bridge renders this off-screen and prints it to PDF. The body is one
    // continuous A4-width flow; the @page / break-inside rules let the browser fragment
    // it across physical A4 pages line-by-line (no dead space).
    const editorPrintHtml = () => {
        const d = iframeRef.current?.contentDocument;
        if (!d?.documentElement) return '';
        return d.documentElement.outerHTML;
    };

    const openPrintDialog = async () => {
        const iframe = iframeRef.current;
        if (!iframe?.contentWindow) return;
        // Desktop: render the A4 document to PDF and open it in the system viewer,
        // whose print dialog targets any printer or saves as PDF (iframe.print()
        // does not reliably reach QtWebEngine's print signal). Browser: native dialog.
        const api = getDesktopApi();
        if (api?.render_pdf) {
            await api.render_pdf(editorPrintHtml(), printBaseName(), 'print');
            return;
        }
        iframe.contentWindow.focus();
        iframe.contentWindow.print();
    };

    const [isExportingPdf, setIsExportingPdf] = useState(false);
    const exportAsPDF = async () => {
        const iframe = iframeRef.current;
        // Desktop: native save-as-PDF via QtWebEngine printToPdf (faithful A4,
        // unlike html2canvas which drops styles inside the iframe).
        const api = getDesktopApi();
        if (api?.render_pdf && iframe?.contentDocument) {
            await api.render_pdf(editorPrintHtml(), printBaseName(), 'pdf');
            return;
        }
        const body = iframe?.contentDocument?.body;
        if (!body) return;
        setIsExportingPdf(true);
        try {
            // Clone content into main document so html2canvas preserves fonts and formatting
            // (rendering from iframe body often drops styles in html2canvas)
            const wrapper = document.createElement('div');
            wrapper.style.position = 'absolute';
            wrapper.style.left = '-9999px';
            wrapper.style.top = '0';
            wrapper.style.width = '210mm';
            wrapper.style.background = 'white';
            wrapper.style.padding = '25mm';
            wrapper.style.boxSizing = 'border-box';
            const style = document.createElement('style');
            style.textContent = `
                .pdf-export-page { width: 210mm; min-height: 297mm; background: white; padding: 0; box-sizing: border-box; }
                .pdf-export-page b, .pdf-export-page strong { font-weight: bold; }
                .pdf-export-page i, .pdf-export-page em { font-style: italic; }
                .pdf-export-page u { text-decoration: underline; }
            `;
            wrapper.appendChild(style);
            const contentDiv = document.createElement('div');
            contentDiv.className = 'pdf-export-page';
            contentDiv.innerHTML = body.innerHTML;   // body IS the continuous flow
            wrapper.appendChild(contentDiv);
            document.body.appendChild(wrapper);
            try {
                const html2pdf = (await import('html2pdf.js')).default;
                const baseName = (displayFile || 'document').replace(/\.[^.]+$/, '') || 'document';
                const filename = `${baseName}.pdf`;
                await html2pdf().set({
                    margin: 10,
                    filename,
                    image: { type: 'jpeg', quality: 0.98 },
                    html2canvas: {
                        scale: 2,
                        useCss: true,
                        letterRendering: true,
                        logging: false,
                    },
                }).from(wrapper).save();
            } finally {
                wrapper.remove();
            }
        } catch (e) {
            setError(e instanceof Error ? e.message : 'PDF-Export fehlgeschlagen');
        } finally {
            setIsExportingPdf(false);
        }
    };

    const downloadHTML = async () => {
        // Desktop: native Save-As dialog writes the current content to disk.
        const api = getDesktopApi();
        if (api?.save_text_as) {
            await api.save_text_as(content, displayFile);
            return;
        }
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
            const html = doc.body.innerHTML;
            setContent(html);
            onContentChangeRef.current?.(html);
            resizeEditorIframe(iframeRef.current);
            setTimeout(() => updateSelectionFormat(), 0);
        } finally {
            savedSelectionRef.current = null;
        }
    }, [updateSelectionFormat]);

    const toolbar = (
        <div
            className="flex flex-wrap items-center gap-0.5 border-b border-gray-200 bg-gray-50 px-2 py-1 shrink-0"
            onMouseDown={saveSelectionFromIframe}
        >
            <button type="button" onClick={() => execEditorCommand('bold')} className={cn("p-1.5 rounded hover:bg-gray-200", selectionFormat.bold && "bg-gray-300")} title="Bold"><Bold size={16} /></button>
            <button type="button" onClick={() => execEditorCommand('italic')} className={cn("p-1.5 rounded hover:bg-gray-200", selectionFormat.italic && "bg-gray-300")} title="Italic"><Italic size={16} /></button>
            <button type="button" onClick={() => execEditorCommand('underline')} className={cn("p-1.5 rounded hover:bg-gray-200", selectionFormat.underline && "bg-gray-300")} title="Underline"><Underline size={16} /></button>
            <span className="w-px h-5 bg-gray-300 mx-0.5" />
            <select
                className="text-xs border border-gray-300 rounded px-1.5 py-1 bg-white min-w-[4rem]"
                value={selectionFormat.fontSize}
                onChange={(e) => execEditorCommand('fontSize', e.target.value)}
                onMouseDown={saveSelectionFromIframe}
                title="Font size"
            >
                {FONT_SIZES.map(([val, label]) => <option key={val} value={val}>{label}</option>)}
            </select>
            <select
                className="text-xs border border-gray-300 rounded px-1.5 py-1 bg-white min-w-[7rem]"
                value={selectionFormat.fontName}
                onChange={(e) => execEditorCommand('fontName', e.target.value)}
                onMouseDown={saveSelectionFromIframe}
                title="Font family"
            >
                {FONT_FAMILIES.map((f) => <option key={f} value={f}>{f}</option>)}
            </select>
            <span className="w-px h-5 bg-gray-300 mx-0.5" />
            <input
                type="color"
                className="w-7 h-7 cursor-pointer border border-gray-300 rounded p-0.5 bg-white"
                value={selectionFormat.foreColor}
                onMouseDown={saveSelectionFromIframe}
                onChange={(e) => execEditorCommand('foreColor', e.target.value)}
                title="Font color"
            />
            <button type="button" onClick={() => execEditorCommand('backColor', '#ffff00')} className="p-1.5 rounded hover:bg-gray-200" title="Highlight"><Highlighter size={16} /></button>
            <span className="w-px h-5 bg-gray-300 mx-0.5" />
            <button type="button" onClick={() => execEditorCommand('justifyLeft')} className={cn("p-1.5 rounded hover:bg-gray-200", selectionFormat.justifyLeft && "bg-gray-300")} title="Align left"><AlignLeft size={16} /></button>
            <button type="button" onClick={() => execEditorCommand('justifyCenter')} className={cn("p-1.5 rounded hover:bg-gray-200", selectionFormat.justifyCenter && "bg-gray-300")} title="Center"><AlignCenter size={16} /></button>
            <button type="button" onClick={() => execEditorCommand('justifyRight')} className={cn("p-1.5 rounded hover:bg-gray-200", selectionFormat.justifyRight && "bg-gray-300")} title="Align right"><AlignRight size={16} /></button>
            <span className="w-px h-5 bg-gray-300 mx-0.5" />
            <button type="button" onClick={() => execEditorCommand('insertUnorderedList')} className="p-1.5 rounded hover:bg-gray-200" title="Bulleted list"><List size={16} /></button>
            <button type="button" onClick={() => execEditorCommand('insertOrderedList')} className="p-1.5 rounded hover:bg-gray-200" title="Numbered list"><ListOrdered size={16} /></button>
            <span className="w-px h-5 bg-gray-300 mx-0.5" />
            <button type="button" onClick={() => execEditorCommand('removeFormat')} className="p-1.5 rounded hover:bg-gray-200" title="Clear formatting"><Eraser size={16} /></button>
            <span className="w-px h-5 bg-gray-300 mx-0.5" />
            <button type="button" onClick={saveDocument} disabled={isSaving} className="flex items-center gap-1.5 px-2.5 py-1 rounded text-xs font-medium text-gray-600 bg-white border border-gray-300 hover:bg-gray-100 disabled:opacity-50" title="Save">
                {isSaving ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}
                Save
            </button>
            <button type="button" onClick={openPrintDialog} className="flex items-center gap-1.5 px-2.5 py-1 rounded text-xs font-medium text-gray-600 bg-white border border-gray-300 hover:bg-gray-100" title="Print (browser dialog)">
                <Printer size={14} />
                Print
            </button>
            <button type="button" onClick={exportAsPDF} disabled={isExportingPdf} className="flex items-center gap-1.5 px-2.5 py-1 rounded text-xs font-medium text-white bg-blue-500 hover:bg-blue-600 disabled:opacity-50 dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-white dark:shadow-none" title="Download as PDF file">
                {isExportingPdf ? <Loader2 size={14} className="animate-spin" /> : <Download size={14} />}
                PDF
            </button>
            <button type="button" onClick={downloadHTML} className="flex items-center gap-1.5 px-2.5 py-1 rounded text-xs font-medium text-gray-600 bg-white border border-gray-300 hover:bg-gray-100" title="Download as HTML">
                <Download size={14} />
                Download
            </button>
            {saveMessage && (
                <span className="text-[10px] text-emerald-600 font-mono ml-1 truncate max-w-[200px]" title={saveMessage}>Saved: {saveMessage}</span>
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
                                    <span className="text-[10px] text-gray-500 uppercase tracking-wide shrink-0">Attachments</span>
                                    <button
                                        type="button"
                                        onClick={() => setAttachmentsDropdownOpen((o) => !o)}
                                        className="flex items-center gap-1 rounded-md border border-gray-200 bg-white px-2 py-1 text-xs text-gray-700 hover:bg-gray-50 min-w-0 max-w-[180px]"
                                    >
                                        <span className="truncate">{selectedAttachment?.name ?? 'No document'}</span>
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
                                                    Add document
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
                                onClick={handleRequestClose}
                                className="rounded-full p-1 text-gray-400 transition hover:bg-gray-100 hover:text-gray-600 shrink-0"
                                aria-label="Close"
                            >
                                <X size={14} />
                            </button>
                        </div>
                        <div className="flex h-9 items-center gap-2 border-b border-gray-100 bg-white/80 px-4 text-xs text-gray-500 shrink-0">
                            <span className="truncate font-mono text-[11px]">{selectedAttachment?.name ?? 'No document selected'}</span>
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
                                            <p>No documents. Click &quot;Add document&quot; to open attachments.</p>
                                            <p className="text-xs">The assistant can then respond to their content.</p>
                                        </div>
                                    ) : !selectedAttachment ? (
                                        <div className="text-gray-400 text-sm">Select a document.</div>
                                    ) : isAttachmentImage && selectedAttachment.content?.startsWith('data:') ? (
                                        <img src={selectedAttachment.content} alt={selectedAttachment.name} className="max-w-full h-auto" />
                                    ) : isPdfWithData(selectedAttachment) ? (
                                        <PdfWithHighlights
                                            src={pdfDataUrl(selectedAttachment)}
                                            title={selectedAttachment.name}
                                            className="w-full min-h-[500px] flex-1"
                                            insertedSelections={insertedSelections}
                                            nextSelectionColorIndex={insertedSelections?.length ?? 0}
                                            documentId={selectedAttachment.id}
                                            content={selectedAttachment.content}
                                            onInsertSelection={onInsertSelection}
                                        />
                                    ) : (
                                        <pre
                                            className={cn(
                                                'w-full max-w-full whitespace-pre-wrap break-words font-sans text-sm text-gray-800',
                                                INSERTION_COLOR_CLASSES[insertedSelectionsCount % INSERTION_COLOR_CLASSES.length]
                                            )}
                                        >
                                            {attachmentContentWithHighlights ?? (attachmentDisplayContent || '(No text content)')}
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
                <div className="flex h-full w-full min-w-0">
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

                    {/* Main content panel (right side) - same dimensions as DocumentViewer */}
                    <div className={cn("flex flex-1 flex-col min-w-0 w-0 bg-[#F9FAFB] overflow-hidden", !hasWorkflow ? "rounded-2xl" : "rounded-r-2xl")}>
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
                                        className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-blue-500 hover:bg-blue-600 rounded-md transition-colors dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-white dark:shadow-none"
                                    >
                                        <Download size={14} />
                                        Download
                                    </a>
                                )}
                                <button
                                    onClick={handleRequestClose}
                                    className="rounded-full p-1 text-gray-400 transition hover:bg-gray-100 hover:text-gray-600"
                                    aria-label="Close"
                                >
                                    <X size={14} />
                                </button>
                            </div>
                        </div>

                        {/* Content area – same structure as DocumentViewer */}
                        <div className="flex-1 min-h-0 min-w-0 flex flex-col overflow-hidden">
                            {isBinaryDocument ? (
                                <div className="flex flex-1 flex-col items-center justify-center gap-4 p-6 text-center bg-white">
                                    <FileText size={48} className="text-gray-300" />
                                    <p className="text-sm text-gray-600 max-w-md">
                                        PDF files cannot be edited here.
                                    </p>
                                    <p className="text-xs text-gray-500">
                                        Word (.docx), Excel (.xlsx), PowerPoint (.pptx), and HTML (.html, .htm) are loaded and edited as HTML in the editor.
                                    </p>
                                    <p className="font-mono text-[11px] text-gray-400 break-all px-2">{filePath}</p>
                                    <a
                                        href={`${getApiBase()}/api/file?path=${encodeURIComponent(filePath)}`}
                                        download={displayFile}
                                        className="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium text-white bg-blue-500 hover:bg-blue-600 rounded-lg transition-colors dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-white dark:shadow-none"
                                    >
                                        <Download size={16} />
                                        Download file
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
                                    <div className="flex-1 min-h-0 min-w-0 flex flex-col overflow-hidden">
                                        <div className="flex-1 min-h-0 min-w-0 overflow-auto bg-[#e5e7eb] w-full scrollbar-hide">
                                            <iframe
                                                ref={iframeRef}
                                                className="w-full min-h-[297mm] border-0 block bg-[#e5e7eb]"
                                                title="Document Editor"
                                                sandbox="allow-same-origin allow-scripts allow-modals"
                                                allow="microphone 'none'; camera 'none'"
                                            />
                                        </div>
                                    </div>
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
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 backdrop-blur-sm p-4 sm:p-8 max-md:p-0">
            <div className="relative flex h-[90vh] w-full max-w-[1400px] overflow-hidden rounded-2xl bg-[#F3F4F6] shadow-2xl min-w-0 max-md:max-w-none max-md:h-[100dvh] max-md:max-h-none max-md:rounded-none max-md:border-0">
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

                {/* Main content panel (right side) - same as DocumentViewer overlay */}
                <div className="flex flex-1 flex-col min-w-0 w-0 bg-[#F9FAFB] overflow-hidden">
                    {/* Header */}
                    <div className="flex h-16 items-center justify-between border-b border-gray-200 bg-white px-6 gap-2 min-w-0">
                        <div className="flex items-center gap-3 min-w-0 flex-1">
                            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-blue-500 text-white shadow-sm dark:bg-[#3a3a3a] dark:text-gray-100 dark:shadow-none">
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
                                    className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-blue-500 hover:bg-blue-600 rounded-lg transition-colors dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-white dark:shadow-none"
                                >
                                    <Download size={14} />
                                    Download
                                </a>
                            )}
                            <button
                                onClick={handleRequestClose}
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
                                    PDF files cannot be edited here.
                                </p>
                                <p className="text-xs text-gray-500">
                                    Word (.docx), Excel (.xlsx), PowerPoint (.pptx), and HTML (.html, .htm) are loaded and edited as HTML in the editor.
                                </p>
                                <p className="font-mono text-xs text-gray-400 break-all px-2">{filePath}</p>
                                <a
                                    href={`${getApiBase()}/api/file?path=${encodeURIComponent(filePath)}`}
                                    download={displayFile}
                                    className="inline-flex items-center gap-2 px-5 py-2.5 text-sm font-medium text-white bg-blue-500 hover:bg-blue-600 rounded-lg transition-colors dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-white dark:shadow-none"
                                >
                                    <Download size={18} />
                                    Download file
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
                                <div className="flex-1 min-h-0 min-w-0 flex flex-col overflow-hidden">
                                    <div className="flex-1 min-h-0 min-w-0 overflow-auto bg-[#e5e7eb] w-full scrollbar-hide">
                                        <iframe
                                            ref={iframeRef}
                                            className="w-full min-h-[297mm] border-0 block bg-[#e5e7eb]"
                                            title="Document Editor"
                                            sandbox="allow-same-origin allow-scripts allow-modals"
                                            allow="microphone 'none'; camera 'none'"
                                        />
                                    </div>
                                </div>
                            </>
                        )}
                    </div>
                </div>
            </div>
        </div>
    );
}
