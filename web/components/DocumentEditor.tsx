'use client';

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

/** Page geometry at 96dpi. A4 = 210×297mm, 25mm margins.
 *  PAGE_CONTENT_PX = inner content height (247mm); PADDING_PX = 25mm page padding;
 *  PAGE_FULL_PX = full A4 page height (297mm). */
const PAGE_CONTENT_PX = Math.round((297 - 50) * 96 / 25.4); // 934
const PADDING_PX = Math.round(25 * 96 / 25.4);              // 94
const PAGE_FULL_PX = Math.round(297 * 96 / 25.4);           // 1122

/** Text-level blocks whose TEXT may be split across a page boundary (Paged.js
 *  textBreak). Splitting + later merging round-trips losslessly via normalize().
 *  DIV is included so a leaf `<div>` (inline content only) also paginates. */
const TEXT_SPLIT_TAGS = new Set(['P', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'BLOCKQUOTE', 'PRE', 'DIV']);
/** Container blocks split only at CHILD boundaries (li/list-item), never mid-item,
 *  so child count is preserved across split/merge (no unbounded growth). */
const CHILD_SPLIT_TAGS = new Set(['UL', 'OL']);
/** Transparent structural wrappers that are unwrapped into their block children, so
 *  content authored as `<div class="report">…</div>` (docx/HTML exports) paginates
 *  block-by-block instead of being treated as one giant unsplittable element. */
const WRAPPER_TAGS = new Set(['DIV', 'SECTION', 'ARTICLE', 'MAIN', 'HEADER', 'FOOTER', 'ASIDE']);
/** Block-level tags used to decide whether a wrapper is structural (contains blocks). */
const BLOCK_LEVEL_TAGS = new Set(['P', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'UL', 'OL', 'TABLE', 'BLOCKQUOTE', 'PRE', 'DIV', 'SECTION', 'ARTICLE', 'FIGURE', 'HR']);

/** A wrapper is unwrapped only when it actually holds block-level children (so a
 *  leaf `<div>` with inline content stays a block and is text-split instead). */
function isStructuralWrapper(el: HTMLElement): boolean {
    if (!WRAPPER_TAGS.has(el.tagName)) return false;
    return Array.from(el.children).some((c) => BLOCK_LEVEL_TAGS.has(c.tagName));
}

/** Bottom Y (viewport px) of a page's usable content area. */
function pageLimitBottom(page: HTMLElement): number {
    return page.getBoundingClientRect().top + PADDING_PX + PAGE_CONTENT_PX;
}

function firstTextNode(el: HTMLElement): Text | null {
    const t = getTextNodesInOrder(el);
    return t.length ? t[0].node : null;
}

function appendPage(root: HTMLElement, doc: Document): HTMLElement {
    const page = doc.createElement('div');
    page.className = 'a4-page';
    root.appendChild(page);
    return page;
}

/** Binary-search the character offset in `block` where text crosses `limitBottom`
 *  (Paged.js `textBreak`), backing up to the nearest word boundary. */
function findTextBreak(block: HTMLElement, limitBottom: number, doc: Document): { node: Text; offset: number } | null {
    const texts = getTextNodesInOrder(block);
    if (texts.length === 0) return null;
    const range = doc.createRange();
    for (const { node } of texts) {
        const len = (node.textContent || '').length;
        range.setStart(node, 0);
        range.setEnd(node, len);
        if (range.getBoundingClientRect().bottom <= limitBottom + 1) continue; // node fits above limit
        // This text node crosses the limit: find the last char that still fits.
        let lo = 0, hi = len, ans = 0;
        while (lo <= hi) {
            const mid = (lo + hi) >> 1;
            range.setStart(node, 0);
            range.setEnd(node, mid);
            if (range.getBoundingClientRect().bottom <= limitBottom + 1) { ans = mid; lo = mid + 1; }
            else hi = mid - 1;
        }
        if (ans <= 0) return { node, offset: 0 };            // not even the first char fits
        const txt = node.textContent || '';
        const sp = txt.lastIndexOf(' ', ans);                // break at a word boundary
        const brk = sp > 0 ? sp + 1 : ans;
        if (brk >= len) return null;                         // nothing left to move
        return { node, offset: brk };
    }
    return null;
}

/** Split a text-level block at `bp`: extract the tail into a shallow clone of the
 *  block (Range.extractContents clones any crossed ancestors), marked data-vaf-cont. */
function splitTextBlock(block: HTMLElement, bp: { node: Text; offset: number }, doc: Document): HTMLElement {
    const range = doc.createRange();
    range.setStart(bp.node, bp.offset);
    range.setEnd(block, block.childNodes.length);
    const frag = range.extractContents();
    const tail = block.cloneNode(false) as HTMLElement;
    tail.removeAttribute('id');
    tail.setAttribute('data-vaf-cont', '1');
    tail.appendChild(frag);
    return tail;
}

/** Index of the first child of `block` whose bottom overflows `limitBottom`, or -1. */
function findChildBreak(block: HTMLElement, limitBottom: number): number {
    const kids = Array.from(block.children);
    for (let i = 0; i < kids.length; i++) {
        if (kids[i].getBoundingClientRect().bottom > limitBottom + 1) return i;
    }
    return -1;
}

/** Move children from `idx` onward into a shallow clone (data-vaf-cont). */
function splitChildrenBlock(block: HTMLElement, idx: number, doc: Document): HTMLElement {
    const tail = block.cloneNode(false) as HTMLElement;
    tail.removeAttribute('id');
    tail.setAttribute('data-vaf-cont', '1');
    const kids = Array.from(block.children);
    for (let i = idx; i < kids.length; i++) tail.appendChild(kids[i]);
    return tail;
}

/** Recursively collect leaf blocks from a container, unwrapping structural wrappers
 *  and merging continuation fragments back into their head block. */
function collectBlocks(container: HTMLElement, blocks: HTMLElement[], doc: Document): void {
    for (const node of Array.from(container.childNodes)) {
        if (node.nodeType === Node.TEXT_NODE) {
            if (!(node.textContent || '').trim()) continue;           // ignore inter-block whitespace
            const p = doc.createElement('p');
            p.appendChild(node.cloneNode(true));
            blocks.push(p);
            continue;
        }
        if (node.nodeType !== Node.ELEMENT_NODE) continue;
        const el = node as HTMLElement;
        const prev = blocks[blocks.length - 1];
        if (el.getAttribute('data-vaf-cont') === '1' && prev && prev.tagName === el.tagName) {
            while (el.firstChild) prev.appendChild(el.firstChild);
            if (TEXT_SPLIT_TAGS.has(prev.tagName)) prev.normalize();
            continue;
        }
        if (isStructuralWrapper(el)) { collectBlocks(el, blocks, doc); continue; } // unwrap
        el.removeAttribute('data-vaf-cont');
        blocks.push(el);
    }
}

/** Collect all leaf blocks from every page in document order (wrappers unwrapped,
 *  continuation fragments merged) so each pass re-splits from a clean flow. Detaches
 *  everything (root is cleared); the returned element references stay valid. */
function flattenBlocks(root: HTMLElement, doc: Document): HTMLElement[] {
    const blocks: HTMLElement[] = [];
    for (const pg of Array.from(root.querySelectorAll('.a4-page'))) collectBlocks(pg as HTMLElement, blocks, doc);
    root.innerHTML = '';
    return blocks;
}

/** Remove empty trailing pages left after distribution. */
function trimEmptyPages(root: HTMLElement): void {
    const pages = Array.from(root.querySelectorAll('.a4-page'));
    for (let i = pages.length - 1; i >= 1; i--) {
        const pg = pages[i] as HTMLElement;
        if (pg.children.length === 0 && !(pg.textContent || '').trim()) pg.remove();
        else break;
    }
}

/** Save the caret as a global character offset over the editing root. */
function saveCaretOffset(doc: Document, root: HTMLElement): number | null {
    const sel = doc.getSelection();
    if (!sel || sel.rangeCount === 0) return null;
    const r = sel.getRangeAt(0);
    if (!root.contains(r.startContainer)) return null;
    const pre = doc.createRange();
    pre.selectNodeContents(root);
    try { pre.setEnd(r.startContainer, r.startOffset); } catch { return null; }
    return pre.toString().length;
}

/** Restore the caret to a previously saved global character offset. */
function restoreCaretOffset(doc: Document, root: HTMLElement, offset: number | null): void {
    if (offset == null) return;
    const texts = getTextNodesInOrder(root);
    if (texts.length === 0) return;
    let target = texts[texts.length - 1];
    let local = (target.node.textContent || '').length;
    for (const t of texts) {
        if (offset <= t.end) { target = t; local = offset - t.start; break; }
    }
    local = Math.max(0, Math.min(local, (target.node.textContent || '').length));
    const sel = doc.getSelection();
    if (!sel) return;
    const r = doc.createRange();
    try {
        r.setStart(target.node, local);
        r.collapse(true);
        sel.removeAllRanges();
        sel.addRange(r);
        (target.node.parentElement?.closest('.a4-page') as HTMLElement | null)?.scrollIntoView?.({ block: 'nearest' });
    } catch { /* ignore */ }
}

/** True if any page overflows its content area, or content can be pulled back from a
 *  following page (e.g. after a deletion). Lets the chunker skip work — and avoid a
 *  caret jump — when the current layout is already correct (steady state / typing
 *  inside a non-overflowing page). */
function needsRepaginate(root: HTMLElement): boolean {
    const pages = Array.from(root.querySelectorAll('.a4-page')) as HTMLElement[];
    if (pages.length === 0) return true;
    for (let i = 0; i < pages.length; i++) {
        const pg = pages[i];
        const limit = pageLimitBottom(pg);
        const last = pg.lastElementChild as HTMLElement | null;
        const lastBottom = last ? last.getBoundingClientRect().bottom : pg.getBoundingClientRect().top + PADDING_PX;
        if (lastBottom > limit + 1) return true;                     // overflow → must reflow
        if (i < pages.length - 1) {
            const first = pages[i + 1].firstElementChild as HTMLElement | null;
            if (first) {
                const h = first.getBoundingClientRect().height;
                if (h > 0 && h <= (limit - lastBottom) + 1) return true; // next block now fits here → pull back
            }
        }
    }
    return false;
}

/**
 * Paged.js-style fixed-box pagination, made live + caret-preserving. The editing
 * root `.a4-pages` holds fixed 297mm `.a4-page` boxes (overflow:hidden — they never
 * grow). This distributes the content into the boxes: each block is appended to the
 * current page; on overflow a text block is split at the line boundary (findTextBreak
 * + Range.extractContents), a list at a child boundary, and the remainder flows onto a
 * NEW page (cascading). Empty pages are trimmed and the caret is restored by global
 * character offset. ALL measurement is against LIVE attached boxes (never detached).
 */
function paginateIntoFixedPages(doc: Document): void {
    const win = doc.defaultView;
    const root = doc.querySelector('.a4-pages') as HTMLElement | null;
    if (!win || !root) return;
    if (!needsRepaginate(root)) return;

    const caret = saveCaretOffset(doc, root);
    const queue = flattenBlocks(root, doc);
    if (queue.length === 0) { appendPage(root, doc); return; }

    let page = appendPage(root, doc);
    let qi = 0;
    let guard = 0;
    while (qi < queue.length && guard++ < 20000) {
        const block = queue[qi];
        page.appendChild(block);
        const limit = pageLimitBottom(page);
        if (block.getBoundingClientRect().bottom <= limit + 1) { qi++; continue; }

        const isFirst = page.children.length === 1;
        let tail: HTMLElement | null = null;
        if (TEXT_SPLIT_TAGS.has(block.tagName)) {
            const bp = findTextBreak(block, limit, doc);
            if (bp && !(bp.node === firstTextNode(block) && bp.offset === 0)) tail = splitTextBlock(block, bp, doc);
        } else if (CHILD_SPLIT_TAGS.has(block.tagName)) {
            const idx = findChildBreak(block, limit);
            if (idx > 0) tail = splitChildrenBlock(block, idx, doc);
        }

        if (tail) {
            page = appendPage(root, doc);
            queue.splice(qi + 1, 0, tail);   // the tail is the first block of the new page
            qi++;
            continue;
        }
        if (isFirst) {
            // Block alone and taller than a page (giant image / unsplittable table):
            // leave it (clipped on screen, revealed in print); next block starts fresh.
            page = appendPage(root, doc);
            qi++;
            continue;
        }
        // Move the whole block onto a fresh page and re-evaluate it there.
        page.removeChild(block);
        page = appendPage(root, doc);
    }

    trimEmptyPages(root);
    restoreCaretOffset(doc, root, caret);
}

/** Serialize the editor content as a clean continuous flow for save/print/PDF:
 *  strip the page-box wrappers and merge continuation fragments back into their head
 *  block. Operates on CLONES — the live editor DOM is untouched. Highlight spans are
 *  preserved (they are re-applied on load just like before). */
function serializeFlow(root: HTMLElement): string {
    const doc = root.ownerDocument;
    const container = doc.createElement('div');
    const pages = Array.from(root.querySelectorAll('.a4-page'));
    for (const pg of pages) {
        for (const child of Array.from(pg.children)) {
            const el = child as HTMLElement;
            const clone = el.cloneNode(true) as HTMLElement;
            clone.removeAttribute('data-vaf-cont');
            const prev = container.lastElementChild as HTMLElement | null;
            if (el.getAttribute('data-vaf-cont') === '1' && prev && prev.tagName === clone.tagName) {
                while (clone.firstChild) prev.appendChild(clone.firstChild);
                if (TEXT_SPLIT_TAGS.has(prev.tagName)) prev.normalize();
            } else {
                container.appendChild(clone);
            }
        }
    }
    return container.innerHTML;
}

/** Injected in document head. The editor renders strictly separated, FIXED-size A4
 *  page boxes (`.a4-page`, height:297mm, overflow:hidden — they never grow). The
 *  chunker (`paginateIntoFixedPages`) distributes content into them and reflows on
 *  overflow. Print maps each fixed box directly to one physical page (WYSIWYG). */
const A4_EDITOR_STYLE = `
                html, body, .a4-pages, .a4-page, .a4-page * { box-sizing: border-box !important; }
                html, body { scrollbar-width: none !important; -ms-overflow-style: none !important; }
                html::-webkit-scrollbar, body::-webkit-scrollbar { display: none !important; }
                html, body { width: 100% !important; margin: 0 !important; padding: 0 !important;
                    background: #e5e7eb !important; }

                /* Editing root: one contentEditable region holding the page boxes,
                   centered on the gray canvas. */
                .a4-pages { display: block !important; width: 100% !important; padding: 16px 0 !important;
                    margin: 0 !important; outline: none !important; }

                /* Each page is a fixed A4 sheet. height + overflow:hidden guarantee it
                   never grows; the 25mm padding are the page margins. */
                .a4-page {
                    position: relative !important;
                    width: 210mm !important; height: 297mm !important;
                    margin: 0 auto 28px auto !important; padding: 25mm !important;
                    background: #ffffff !important; box-shadow: 0 2px 10px rgba(0,0,0,0.22) !important;
                    overflow: hidden !important; outline: none !important;
                }
                .a4-page > *:first-child { margin-top: 0 !important; }
                .a4-page > * { max-width: 100% !important; }

                /* Print: each fixed box becomes exactly one physical page. After a
                   settled chunk pass no box overflows, so nothing is clipped → the PDF
                   is pixel-faithful to the editor (real margins, no dead space). */
                @page { size: A4; margin: 0; }
                @media print {
                    html, body { background: #ffffff !important; }
                    .a4-pages { padding: 0 !important; }
                    .a4-page {
                        margin: 0 !important; box-shadow: none !important;
                        height: auto !important; min-height: 297mm !important;
                        overflow: visible !important; break-after: page !important; page-break-after: always !important;
                    }
                    .a4-page:last-child { break-after: auto !important; page-break-after: auto !important; }
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
        const message = 'Möchten Sie den Dokument-Editor wirklich schließen? Ungespeicherte Änderungen können verloren gehen.';
        if (window.confirm(message)) {
            onClose();
        }
    }, [onClose]);
    /** When true, the last content update came from the iframe (user typing). Skip rewriting iframe to avoid focus loss. */
    const contentFromIframeRef = useRef(false);
    /** While pagination moves DOM nodes, `input` can fire — syncing would re-enter useEffect and can exceed React update depth. */
    const paginatingIframeRef = useRef(false);
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
        // Build the fixed-page editing root: all content starts on page 1; the chunker
        // distributes it into fixed 297mm boxes and reflows on overflow.
        doc.body.innerHTML = '';
        const pagesRoot = doc.createElement('div');
        pagesRoot.className = 'a4-pages';
        const firstPage = doc.createElement('div');
        firstPage.className = 'a4-page';
        firstPage.innerHTML = bodyInner;
        pagesRoot.appendChild(firstPage);
        doc.body.appendChild(pagesRoot);
        pagesRoot.contentEditable = 'true';
        (pagesRoot.style as CSSStyleDeclaration).outline = 'none';
        ensureA4EditorStyles(doc);

        // One guarded pagination pass: distributes content into the fixed pages and
        // resizes the iframe to the full stacked height. Guarded so the input events
        // the DOM moves emit don't re-enter React / get persisted as edits.
        const runPaginatePass = () => {
            paginatingIframeRef.current = true;
            try { ensureA4EditorStyles(doc); paginateIntoFixedPages(doc); }
            finally { paginatingIframeRef.current = false; resizeEditorIframe(iframeRef.current); }
        };
        const schedulePaginate = () => requestAnimationFrame(() => requestAnimationFrame(runPaginatePass));
        schedulePaginate();
        // Retry passes catch async layout settling (webfonts, late images, the iframe
        // not yet sized on first paint). needsRepaginate() makes each a no-op once the
        // layout is already correct, so these are cheap and never cause churn.
        const lateTimers = [120, 400, 800, 1500].map((ms) => window.setTimeout(runPaginatePass, ms));
        try { doc.fonts?.ready?.then(() => runPaginatePass()).catch(() => { /* ignore */ }); } catch { /* ignore */ }

        const focusBodyOnMouseDown = (e: MouseEvent) => {
            if (pagesRoot.contains(e.target as Node)) pagesRoot.focus();
        };
        doc.body.addEventListener('mousedown', focusBodyOnMouseDown);
        const captureContent = () => {
            if (paginatingIframeRef.current) return;
            contentFromIframeRef.current = true;
            const html = serializeFlow(pagesRoot);   // clean continuous flow (no page boxes / cont markers)
            setContent(html);
            onContentChangeRef.current?.(html);
        };
        pagesRoot.addEventListener('input', captureContent);
        // Debounced re-pagination after edits: this is what reflows overflow onto the
        // next fixed page (and pulls content back on delete). 120ms keeps typing fluid.
        let breakTimer: ReturnType<typeof setTimeout> | null = null;
        const debouncedPaginate = () => {
            if (breakTimer) clearTimeout(breakTimer);
            breakTimer = setTimeout(schedulePaginate, 120);
        };
        pagesRoot.addEventListener('input', debouncedPaginate);
        const handleMouseUp = () => {
            const fn = onInsertSelectionRef.current;
            if (!fn) return;
            try {
                const sel = doc.getSelection();
                if (!sel || sel.isCollapsed) return;
                const r = sel.getRangeAt(0);
                const text = r.toString().trim();
                if (!text) return;
                const root = pagesRoot as Node;
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
        pagesRoot.addEventListener('mouseup', handleMouseUp);
        doc.addEventListener('selectionchange', handleSelectionChange);

        return () => {
            if (selectionChangeTimeoutId) clearTimeout(selectionChangeTimeoutId);
            if (breakTimer) clearTimeout(breakTimer);
            lateTimers.forEach((t) => clearTimeout(t));
            doc.body.removeEventListener('mousedown', focusBodyOnMouseDown);
            pagesRoot.removeEventListener('input', captureContent);
            pagesRoot.removeEventListener('input', debouncedPaginate);
            pagesRoot.removeEventListener('mouseup', handleMouseUp);
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
        const root = doc.querySelector('.a4-pages') as HTMLElement | null;
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
            setSaveMessage(filePath || 'Gespeichert.');
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
    // Full rendered HTML of the editor iframe (head A4 styles + fixed page boxes) —
    // the desktop bridge renders this off-screen and prints it to PDF. The fixed boxes
    // ARE the pages: the @media print rules map each `.a4-page` to one physical page
    // (height:auto, break-after:page), so the PDF is pixel-faithful to the editor.
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
            const flowRoot = body.querySelector('.a4-pages') as HTMLElement | null;
            contentDiv.innerHTML = flowRoot ? serializeFlow(flowRoot) : body.innerHTML;
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
        const pagesRoot = doc.querySelector('.a4-pages') as HTMLElement | null;
        (pagesRoot ?? doc.body).focus();
        const sel = doc.getSelection();
        if (sel && savedSelectionRef.current?.doc === doc) {
            sel.removeAllRanges();
            sel.addRange(savedSelectionRef.current.range);
        }
        try {
            doc.execCommand(command, false, value ?? '');
            contentFromIframeRef.current = true;
            const html = pagesRoot ? serializeFlow(pagesRoot) : doc.body.innerHTML;
            setContent(html);
            onContentChangeRef.current?.(html);
            // List/format commands can change block heights → reflow the fixed pages.
            if (pagesRoot) {
                paginatingIframeRef.current = true;
                requestAnimationFrame(() => requestAnimationFrame(() => {
                    try { paginateIntoFixedPages(doc); }
                    finally { paginatingIframeRef.current = false; resizeEditorIframe(iframeRef.current); }
                }));
            }
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
            <button type="button" onClick={() => execEditorCommand('bold')} className={cn("p-1.5 rounded hover:bg-gray-200", selectionFormat.bold && "bg-gray-300")} title="Fett"><Bold size={16} /></button>
            <button type="button" onClick={() => execEditorCommand('italic')} className={cn("p-1.5 rounded hover:bg-gray-200", selectionFormat.italic && "bg-gray-300")} title="Kursiv"><Italic size={16} /></button>
            <button type="button" onClick={() => execEditorCommand('underline')} className={cn("p-1.5 rounded hover:bg-gray-200", selectionFormat.underline && "bg-gray-300")} title="Unterstrichen"><Underline size={16} /></button>
            <span className="w-px h-5 bg-gray-300 mx-0.5" />
            <select
                className="text-xs border border-gray-300 rounded px-1.5 py-1 bg-white min-w-[4rem]"
                value={selectionFormat.fontSize}
                onChange={(e) => execEditorCommand('fontSize', e.target.value)}
                onMouseDown={saveSelectionFromIframe}
                title="Schriftgröße"
            >
                {FONT_SIZES.map(([val, label]) => <option key={val} value={val}>{label}</option>)}
            </select>
            <select
                className="text-xs border border-gray-300 rounded px-1.5 py-1 bg-white min-w-[7rem]"
                value={selectionFormat.fontName}
                onChange={(e) => execEditorCommand('fontName', e.target.value)}
                onMouseDown={saveSelectionFromIframe}
                title="Schriftart"
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
                title="Schriftfarbe"
            />
            <button type="button" onClick={() => execEditorCommand('backColor', '#ffff00')} className="p-1.5 rounded hover:bg-gray-200" title="Markieren"><Highlighter size={16} /></button>
            <span className="w-px h-5 bg-gray-300 mx-0.5" />
            <button type="button" onClick={() => execEditorCommand('justifyLeft')} className={cn("p-1.5 rounded hover:bg-gray-200", selectionFormat.justifyLeft && "bg-gray-300")} title="Links"><AlignLeft size={16} /></button>
            <button type="button" onClick={() => execEditorCommand('justifyCenter')} className={cn("p-1.5 rounded hover:bg-gray-200", selectionFormat.justifyCenter && "bg-gray-300")} title="Zentriert"><AlignCenter size={16} /></button>
            <button type="button" onClick={() => execEditorCommand('justifyRight')} className={cn("p-1.5 rounded hover:bg-gray-200", selectionFormat.justifyRight && "bg-gray-300")} title="Rechts"><AlignRight size={16} /></button>
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
                                onClick={handleRequestClose}
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
                                        className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-blue-500 hover:bg-blue-600 rounded-md transition-colors"
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
                                    <div className="flex-1 min-h-0 min-w-0 flex flex-col overflow-hidden">
                                        <div className="flex-1 min-h-0 min-w-0 overflow-auto bg-[#e5e7eb] w-full scrollbar-hide">
                                            <iframe
                                                ref={iframeRef}
                                                className="w-full min-h-[297mm] border-0 block bg-[#e5e7eb]"
                                                title="Document Editor"
                                                sandbox="allow-same-origin allow-scripts allow-modals"
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
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 backdrop-blur-sm p-4 sm:p-8">
            <div className="relative flex h-[90vh] w-full max-w-[1400px] overflow-hidden rounded-2xl bg-[#F3F4F6] shadow-2xl min-w-0">
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
                                <div className="flex-1 min-h-0 min-w-0 flex flex-col overflow-hidden">
                                    <div className="flex-1 min-h-0 min-w-0 overflow-auto bg-[#e5e7eb] w-full scrollbar-hide">
                                        <iframe
                                            ref={iframeRef}
                                            className="w-full min-h-[297mm] border-0 block bg-[#e5e7eb]"
                                            title="Document Editor"
                                            sandbox="allow-same-origin allow-scripts allow-modals"
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
